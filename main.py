from gevent import monkey, spawn, sleep  
monkey.patch_all()

import os
import random
import uuid
import time
import secrets
import string
import math
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, jsonify, make_response
from flask_socketio import SocketIO, emit, join_room
from collections import defaultdict
from threading import Lock 
from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)
is_production = os.environ.get('FLASK_ENV') == 'production'

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(production: bool) -> logging.Logger:
    """Richtet das Application-Logging ein.

    - Datei: logs/gueterspiel.log (RotatingFileHandler, max 5 MB × 5 Backups)
    - Konsole: immer aktiv; Level DEBUG in Dev, WARNING in Prod
    - Datei-Level:  DEBUG in Dev, INFO in Prod
    - Format: Zeitstempel | Level | Modul | Nachricht
    """
    os.makedirs('logs', exist_ok=True)

    log = logging.getLogger('gueterspiel')
    log.setLevel(logging.DEBUG)          # Root-Level: alles durchlassen, Handler filtern

    fmt = logging.Formatter(
        '%(asctime)s [%(levelname)-8s] %(name)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # --- Datei-Handler ---
    file_handler = RotatingFileHandler(
        'logs/gueterspiel.log',
        maxBytes=5 * 1024 * 1024,   # 5 MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.INFO if production else logging.DEBUG)
    file_handler.setFormatter(fmt)

    # --- Konsolen-Handler ---
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.WARNING if production else logging.DEBUG)
    console_handler.setFormatter(fmt)

    log.addHandler(file_handler)
    log.addHandler(console_handler)

    # Flask-SocketIO und engineio loggen per-Paket-Spam auf DEBUG-Ebene.
    # Wir reduzieren sie auf WARNING, damit nur echte Fehler sichtbar bleiben.
    # (Das gilt unabhängig davon, ob production=True/False.)
    for noisy in ('socketio', 'engineio', 'engineio.server', 'socketio.server'):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    log.info("Logging initialisiert (production=%s)", production)
    return log


logger = setup_logging(is_production)
# ---------------------------------------------------------------------------

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY')
if not app.config['SECRET_KEY']:
    raise ValueError("SECRET_KEY nicht gesetzt!")

app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = is_production
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_PERMANENT'] = True
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)

socketio = SocketIO(
    app,
    cors_allowed_origins=os.environ.get('ALLOWED_ORIGIN', '*'),
    manage_session=True,
    logger=not is_production,  # Logger nur in Development
    engineio_logger=not is_production,  # EngineIO Logger nur in Development
    async_mode='gevent',  # Wichtig für Production
    ping_timeout=60, 
    ping_interval=25
)

# Datenstrukturen zur Verwaltung der Räume und Spieler
rooms = {}
players = {}
sid_to_player = {}
pending_removals = {}
player_sids = defaultdict(set)
finish_round_lock = Lock()
removal_lock = Lock()                   

class GameRoom:
    def __init__(self, room_id, leader_id, settings):
        self.id = room_id
        self.leader_id = leader_id
        self.settings = settings
        self.players = []
        self.status = "waiting"  # waiting | ready | playing | round_results | finished
        self.current_round = 0
        self.groups = []
        self.round_groups = {}        # round_num → Gruppen-Snapshot dieser Runde
        self.group_cooperation = {}
        self.round_start_time = None
        self.submitted_players = set()
        self.round_results = None
        self.leader_name = "Leader"
        self.timer_remaining = settings.get('round_duration', 60)
        self.submitted_count = 0
        self.total_players = 0
        self.timer_running = False
        self.incognito_mode = settings.get('incognito_mode', False)
        self.room_type = settings.get('room_type', 'public')
        self.created_at = time.time()
        self.access_code = settings.get('access_code')
        self.chat_enabled = settings.get('chat_enabled', True)
        self.round_end_time = None
        self.players_in_results = set()
        self.created_at = time.time()           # für 7-Tage-Cleanup aktiver Räume
        self.players_arrived_for_round = set()  # Spieler, die die Spielseite geladen haben
        self.players_waiting_for_round = set()  # Spieler, auf die der Timer wartet
        # Wahrscheinlichkeits-Modus: Entscheidung "weiterspielen?" wird pro Runde
        # genau EINMAL gewürfelt und gecacht (verhindert, dass Button-Label und
        # tatsächliche Entscheidung auseinanderlaufen oder ein Reload neu würfelt).
        self._continue_decision = None
        self._continue_decision_round = -1

    # ------------------------------------------------------------------
    # Spieler-Hilfsmethoden
    # ------------------------------------------------------------------

    def non_leader_pids(self):
        """Gibt alle Nicht-Leader-Spieler-IDs zurück, die im players-Dict existieren."""
        return [pid for pid in self.players if pid in players and not players[pid].is_leader]

    def add_player(self, player_id):
        if player_id not in self.players:
            self.players.append(player_id)
            return True
        return False

    def remove_player(self, player_id):
        if player_id in self.players:
            self.players.remove(player_id)
            return True
        return False

    def can_start_game(self):
        """True wenn genug Spieler bereit sind und die Anzahl durch die Gruppengröße teilbar ist."""
        group_size = self.settings.get('group_size', 4)
        normal_players = self.non_leader_pids()
        return (
            len(normal_players) >= group_size
            and len(normal_players) % group_size == 0
            and all(players[pid].ready for pid in normal_players)
        )

    def create_groups(self):
        """Teilt alle Nicht-Leader-Spieler zufällig in Gruppen ein und speichert
        einen unveränderlichen Snapshot in round_groups[current_round]."""
        group_size = self.settings.get('group_size', 4)
        player_list = self.non_leader_pids()
        random.shuffle(player_list)

        self.groups = []
        for i in range(0, len(player_list), group_size):
            group = player_list[i:i + group_size]
            self.groups.append({
                'group_number': len(self.groups) + 1,
                'members': [players[pid].name for pid in group],
                'player_ids': group
            })

        # Snapshot der Gruppen-Zusammensetzung für diese Runde festhalten
        self.round_groups[self.current_round] = [
            {
                'group_number': g['group_number'],
                'members':      list(g['members']),
                'player_ids':   list(g['player_ids']),
            }
            for g in self.groups
        ]

    def get_round_by_round_summary(self, players_dict):
        """Gibt für jede Runde die genaue Gruppenaufteilung zurück.
        Nur sinnvoll wenn fixed_groups=False.
        Nutzt round_groups-Snapshots, damit die Auswertung korrekt ist –
        nicht die finale Gruppenaufteilung.

        Pro Spieler werden zurückgegeben:
          contribution       – Einzahlung in dieser Runde
          balance_after      – Guthaben nach dieser Runde
          profit_this_round  – Gewinn/Verlust in dieser Runde
          overall_coop_rate  – Gesamte Koopquote über alle Runden
          cooperated         – True wenn Einzahlung > 0
        """
        initial_coins = self.settings.get('initial_coins', 0)
        summary = []
        for round_num in sorted(self.round_groups.keys()):
            round_groups    = self.round_groups[round_num]
            coop_data       = self.group_cooperation.get(round_num, {})
            round_entry     = {'round': round_num, 'groups': []}
            all_coop_rates  = []

            for group in round_groups:
                members            = []
                total_contribution = 0

                for pid in group['player_ids']:
                    p = players_dict.get(pid)
                    if not p:
                        continue
                    contributions = p.game_history.get('contributions', [])
                    balances      = p.game_history.get('balances', [])

                    # balances[0]          = Startguthaben (vor Runde 1)
                    # balances[round_num]  = Guthaben NACH Runde round_num
                    # contributions[round_num - 1] = Einzahlung IN Runde round_num
                    contrib        = contributions[round_num - 1] if (round_num - 1) < len(contributions) else 0
                    balance_after  = balances[round_num]          if round_num       < len(balances)      else 0
                    balance_before = balances[round_num - 1]      if (round_num - 1) < len(balances)      else initial_coins
                    profit_this_round = round(balance_after - balance_before, 2)

                    # Gesamte Koopquote über alle gespielten Runden
                    overall_coop = round(
                        (sum(1 for c in contributions if c > 0) / len(contributions)) * 100, 1
                    ) if contributions else 0

                    total_contribution += contrib
                    members.append({
                        'name':              p.name,
                        'contribution':      round(contrib, 2),
                        'balance_after':     round(balance_after, 2),
                        'profit_this_round': profit_this_round,
                        'overall_coop_rate': overall_coop,
                        'cooperated':        contrib > 0,
                    })

                coop_rate = round(coop_data.get(group['group_number'], 0), 1)
                all_coop_rates.append(coop_rate)

                round_entry['groups'].append({
                    'group_number':       group['group_number'],
                    'members':            members,
                    'total_contribution': round(total_contribution, 2),
                    'cooperation_rate':   coop_rate,
                })

            # Runden-Zusammenfassung (für den kollabieren Header)
            round_entry['group_count']     = len(round_groups)
            round_entry['avg_cooperation'] = round(
                sum(all_coop_rates) / len(all_coop_rates), 1
            ) if all_coop_rates else 0

            summary.append(round_entry)
        return summary

    def get_player_group(self, player_id):
        for group in self.groups:
            if player_id in group['player_ids']:
                return group
        return None

    # ------------------------------------------------------------------
    # Status & Timer
    # ------------------------------------------------------------------

    def update_timer_status(self, time_left, submitted_count, total_players, timer_running):
        self.timer_remaining = time_left
        self.submitted_count = submitted_count
        self.total_players = total_players
        self.timer_running = timer_running

    def update_status_based_on_conditions(self):
        """Aktualisiert den Raumstatus und informiert alle Clients."""
        if self.status in ["waiting", "ready"]:
            self.status = "ready" if self.can_start_game() else "waiting"

        non_leaders = self.non_leader_pids()
        try:
            socketio.emit('room_status_updated', {
                'status': self.status,
                'ready_count': sum(1 for pid in non_leaders if players[pid].ready),
                'total_players': len(non_leaders)
            }, room=self.id)
            # Globaler Broadcast damit join_game-Seite Spielerzahl/Status live aktualisiert
            socketio.emit('room_updated', {
                'room_id': self.id,
                'status': self.status,
                'player_count': len(non_leaders)
            })
        except Exception as e:
            logger.error("Fehler beim Senden des Status-Updates: %s", e)
    
    # ------------------------------------------------------------------
    # Auswertung
    # ------------------------------------------------------------------

    def get_group_comparison_data(self, players_dict, initial_coins):
        """Berechnet aggregierte Vergleichsdaten für alle Gruppen (Auswertungsseite)."""
        groups_data = []

        for group in self.groups:
            group_balances = []
            group_contributions = []
            group_cooperation_rates = []

            for player_id in group['player_ids']:
                if player_id in players_dict:
                    player = players_dict[player_id]
                    group_balances.append(player.coins)
                    group_contributions.extend(player.game_history['contributions'])

            for round_num in range(1, self.current_round + 1):
                rate = self.group_cooperation.get(round_num, {}).get(group['group_number'])
                if rate is not None:
                    group_cooperation_rates.append(rate)

            avg_balance     = (sum(group_balances)        / len(group_balances))        if group_balances        else 0
            avg_contribution= (sum(group_contributions)   / len(group_contributions))   if group_contributions   else 0
            total_profit    = sum(group_balances) - (initial_coins * len(group_balances))
            avg_cooperation = (sum(group_cooperation_rates) / len(group_cooperation_rates)) if group_cooperation_rates else 0

            groups_data.append({
                'group':            group,
                'avg_balance':      round(avg_balance, 2),
                'avg_contribution': round(avg_contribution, 2),
                'total_profit':     round(total_profit, 2),
                'avg_cooperation':  round(avg_cooperation, 2),
                'total_wealth':     sum(group_balances)
            })

        return sorted(groups_data, key=lambda x: x['total_wealth'], reverse=True)
    
    def should_continue_game(self):
        """True wenn das Spiel laut Einstellungen eine weitere Runde spielen soll.

        Im Wahrscheinlichkeits-Modus wird der Zufallswurf pro Runde nur EINMAL
        ausgeführt und gecacht. Dadurch liefern Button-Label (can_continue-API)
        und die tatsächliche Entscheidung (next_round) identische Ergebnisse,
        und ein Seiten-Reload verändert das Resultat nicht.
        """
        if self.current_round == 0:
            return True

        end_mode = self.settings.get('end_mode', 'fixed_rounds')

        if end_mode == 'fixed_rounds':
            return self.current_round < self.settings.get('max_rounds', 5)
        elif end_mode == 'probability':
            if self.current_round < self.settings.get('min_rounds', 1):
                return True
            if self.current_round >= self.settings.get('max_rounds_probability', 10):
                return False
            # Entscheidung für die aktuelle Runde nur einmal würfeln und cachen.
            if self._continue_decision_round != self.current_round:
                self._continue_decision = random.random() <= self.settings.get('continue_probability', 0.5)
                self._continue_decision_round = self.current_round
            return self._continue_decision
        return False

    def reset_for_next_round(self):
        """Setzt den Raum für eine neue Runde zurück."""
        self.status = "playing"
        self.current_round += 1

        if not self.settings.get('fixed_groups', False):
            self.create_groups()

        for pid in self.players:
            p = players.get(pid)
            if p:
                p.current_contribution = 0

        self.submitted_players.clear()
        self.round_results = None
        self.timer_remaining = self.settings.get('round_duration', 60)
        self.submitted_count = 0
        self.timer_running = True
    
    def calculate_round_results(self):
        results = []
        multiplier = self.settings.get('multiplier', 2)

        for group in self.groups:
            # 1) Zuerst gesamte Gruppensumme berechnen
            total_contribution = 0
            cooperative_players = 0

            for player_id in group['player_ids']:
                if player_id not in players:
                    continue
                total_contribution += players[player_id].current_contribution
                if players[player_id].current_contribution > 0:
                    cooperative_players += 1

            # Berechne Kooperationsquote für diese Gruppe
            active_ids = [pid for pid in group['player_ids'] if pid in players]
            cooperation_rate = (cooperative_players / len(active_ids)) * 100 if active_ids else 0

            if self.current_round not in self.group_cooperation:
                self.group_cooperation[self.current_round] = {}
            self.group_cooperation[self.current_round][group['group_number']] = cooperation_rate

            # Payout pro Spieler (basierend auf vollständiger Gruppensumme)
            payout_per_player = (total_contribution * multiplier) / len(active_ids) if active_ids else 0

            group_players = []
            # 2) Jetzt für jeden Spieler Auszahlung berechnen und Guthaben aktualisieren
            for player_id in group['player_ids']:
                if player_id not in players:
                    continue
                player = players[player_id]
                contribution = player.current_contribution

                # Guthaben vor der Runde speichern
                old_balance = player.coins

                # Auszahlung (für diesen Spieler gleich payout_per_player)
                payout = payout_per_player

                # Neues Guthaben = (altes Guthaben - eigener Beitrag) + Auszahlung
                player.coins = round((old_balance - contribution) + payout, 2)

                # Spieler-Daten für Ergebnisse
                group_players.append({
                    'id': player_id,
                    'name': player.name,
                    'contribution': round(contribution, 2),
                    'payout': round(payout, 2),
                    'new_balance': round(player.coins, 2),
                    'profit': round(payout - contribution, 2)
                })

                # Spielverlauf aktualisieren
                player.game_history['balances'].append(round(player.coins, 2))
                player.game_history['contributions'].append(round(contribution, 2))

            results.append({
                'group_number': group['group_number'],
                'total_contribution': round(total_contribution, 2),
                'total_pool': round(total_contribution * multiplier, 2),
                'payout_per_player': round(payout_per_player, 2),
                'cooperation_rate': round(cooperation_rate, 2),
                'players': group_players
            })

        self.round_results = results
        return results

class Player:
    def __init__(self, player_id, name, is_leader=False, is_test=False):
        self.id = player_id
        self.name = name
        self.is_leader = is_leader
        self.is_test = is_test
        self.coins = 0
        self.current_contribution = 0
        self.ready = False
        self.room_id = None
        self.game_history = {
            'balances': [],
            'contributions': []
        }

class GameTimer:
    def __init__(self, socketio, room_id, duration=60):
        self.socketio = socketio
        self.room_id = room_id
        self.duration = duration
        self.time_left = duration
        self.is_running = False
        self.lock = Lock()
        self.start_time = None
        self.greenlet = None

    def start(self):
        with self.lock:
            if self.is_running:
                return
            self.is_running = True
            self.start_time = time.time()
            self.time_left = self.duration
            self.greenlet = spawn(self._run_timer)

    def _run_timer(self):
        end_time = time.time() + self.duration

        while self.is_running:
            sleep(1)
            with self.lock:
                now = time.time()
                self.time_left = math.ceil(max(0, end_time - now))

                # Raum-Metadaten aktualisieren (für Reconnect-Snapshots)
                if self.room_id in rooms:
                    room = rooms[self.room_id]
                    submitted_count = len(room.submitted_players)
                    total_players   = len(room.non_leader_pids())
                    try:
                        room.update_timer_status(self.time_left, submitted_count, total_players, True)
                    except Exception:
                        pass

                if self.time_left <= 0:
                    try:
                        self.socketio.emit('game_time_out', room=self.room_id)
                    except Exception:
                        pass
                    try:
                        finish_round(self.room_id)
                    except Exception as e:
                        logger.error("Fehler beim automatischen Beenden der Runde für Raum %s: %s",
                                     self.room_id, e)
                    finally:
                        self.is_running = False
                        if self.room_id in rooms:
                            try:
                                rooms[self.room_id].timer_running = False
                                rooms[self.room_id].timer_remaining = 0
                            except Exception:
                                pass
                    break

    def stop(self):
        with self.lock:
            self.is_running = False
            if self.greenlet:
                try:
                    self.greenlet.kill()
                except:
                    pass
                self.greenlet = None

    def get_time_left(self):
        with self.lock:
            if not self.is_running or not self.start_time:
                return self.duration # Oder self.time_left, je nach gewünschtem Verhalten bei "Pause"
            
            elapsed = time.time() - self.start_time

            return math.ceil(max(0, self.duration - elapsed))
        
# Globale Variablen für Game-Timer
game_timers = {}
game_timer_lock = Lock()

# ---------------------------------------------------------------------------
# History Store  (abgeschlossene Räume, TTL 7 Tage)
# ---------------------------------------------------------------------------
game_history_store = {}   # room_id → Snapshot-Dict

# ---------------------------------------------------------------------------
# History-Proxy-Klassen
# ---------------------------------------------------------------------------

class HistoryRoomProxy:
    """Minimaler Proxy, der die für evaluation.html nötigen Room-Felder bereitstellt."""
    def __init__(self, entry):
        self.id             = entry['room_id']
        self.settings       = entry['settings']
        self.current_round  = entry['current_round']
        self.groups         = entry['groups']
        self.leader_name    = entry['leader_name']
        self.incognito_mode = entry['settings'].get('incognito_mode', False)


class HistoryPlayerProxy:
    """Minimaler Proxy, der die für evaluation.html nötigen Player-Felder bereitstellt."""
    def __init__(self, pid, data):
        self.id           = pid
        self.name         = data.get('name', 'Unbekannt')
        self.coins        = data.get('coins', 0.0)
        self.is_leader    = data.get('is_leader', False)
        self.game_history = data.get('game_history', {'balances': [], 'contributions': []})


# ---------------------------------------------------------------------------

def close_room(room_id, message, reason=None):
    """Schließt einen Raum vollständig: benachrichtigt alle Spieler, stoppt den
    Timer, bricht laufende Gnadenfrist-Greenlets ab und räumt alle Daten auf."""
    room = rooms.get(room_id)
    if not room:
        return

    payload = {'message': message}
    if reason:
        payload['reason'] = reason
    socketio.emit('room_closed', payload, room=room_id)

    timer = game_timers.pop(room_id, None)
    if timer:
        try:
            timer.stop()
        except Exception:
            pass

    all_pids = list(room.players) + [room.leader_id]
    with removal_lock:
        for pid in all_pids:
            greenlet = pending_removals.pop(pid, None)
            if greenlet:
                try:
                    greenlet.kill()
                except Exception:
                    pass

    for pid in list(room.players):
        p = players.pop(pid, None)
        player_sids.pop(pid, None)
        if p:
            p.room_id = None

    players.pop(room.leader_id, None)
    player_sids.pop(room.leader_id, None)
    rooms.pop(room_id, None)

    socketio.emit('room_list_updated', {'action': 'deleted', 'room_id': room_id})
    logger.info("Raum %s geschlossen (Grund: %s)", room_id, reason or 'manuell')


def stop_game_timer(room_id):
    """Stoppt und entfernt Timer sicher."""
    with game_timer_lock:
        if room_id in game_timers:
            game_timers[room_id].stop()
            del game_timers[room_id]
            # Falls Raum noch existiert: Timer-Status im Raum setzen
            if room_id in rooms:
                room = rooms[room_id]
                room.timer_running = False
                # Setze verbleibende Zeit auf 0 wenn Runde bereits in Ergebnissen ist
                if room.status == "round_results":
                    room.timer_remaining = 0
            logger.debug("Game-Timer für Raum %s gestoppt", room_id)

def simulate_test_players_at_results(room_id):
    """Markiert Testspieler nach kurzem Delay als in round_results angekommen."""
    sleep(0.5)  # Kurz warten, damit der Leader-Client round_finished verarbeitet hat
    room = rooms.get(room_id)
    if not room or room.status != 'round_results':
        return

    non_leader_ids = [pid for pid in room.players if pid in players and not players[pid].is_leader]
    for pid in non_leader_ids:
        player = players.get(pid)
        if player and player.is_test:
            room.players_in_results.add(pid)

    total = len(non_leader_ids)
    arrived = len(room.players_in_results)
    socketio.emit('results_arrival_update', {
        'arrived': arrived,
        'total': total,
        'all_ready': arrived >= total
    }, room=room_id)


def simulate_test_players(room_id):
    """Lässt Test-Spieler zu zufälligen Zeitpunkten zufällige Beiträge einreichen"""
    room = rooms.get(room_id)
    if not room:
        return

    for pid in list(room.players):
        player = players.get(pid)
        if player and player.is_test:
            spawn(_test_player_submit, pid, room_id)

def _test_player_submit(player_id, room_id):
    room = rooms.get(room_id)
    player = players.get(player_id)
    if not room or not player:
        return

    # Zufällig warten (zwischen 2s und 90% der Rundendauer)
    duration = room.settings.get('round_duration', 60)
    wait_time = random.uniform(2, duration * 0.9)
    sleep(wait_time)

    # Abbrechen falls Runde schon vorbei
    if room.status != 'playing' or player_id in room.submitted_players:
        return

    # Zufälliger Beitrag zwischen 0 und aktuellem Guthaben
    contribution = random.randint(0, int(player.coins))
    player.current_contribution = contribution
    room.submitted_players.add(player_id)

    submitted_count = len(room.submitted_players)
    total_players   = len(room.non_leader_pids())

    socketio.emit('contribution_submitted', {
        'submitted_count': submitted_count,
        'total_players': total_players
    }, room=room_id)

    # Falls alle eingereicht haben: Runde sofort beenden
    if submitted_count == total_players:
        stop_game_timer(room_id)
        finish_round(room_id)

def get_or_create_game_timer(room_id, duration=60):
    """Erstellt oder gibt existierenden Timer zurück - threadsicher"""
    with game_timer_lock:
        # Stoppe vorhandenen Timer falls vorhanden
        if room_id in game_timers:
            old_timer = game_timers[room_id]
            if old_timer.is_running:
                old_timer.stop()
            del game_timers[room_id]
        
        # Erstelle neuen Timer
        timer = GameTimer(socketio, room_id, duration)
        game_timers[room_id] = timer
        
        if room_id in rooms:
            room = rooms[room_id]
            room.update_timer_status(duration, 0, len(room.non_leader_pids()), True)
        
        timer.start()
        logger.info("Neuer Game-Timer für Raum %s gestartet (Dauer: %ss)", room_id, duration)
        return timer

def save_to_history(room_id):
    """Speichert einen abgeschlossenen Raum als unveränderlichen Snapshot in game_history_store."""
    room = rooms.get(room_id)
    if not room:
        return

    initial_coins = room.settings['initial_coins']

    # Spielerdaten snapshot (vor jeglicher Modifikation der Player-Objekte)
    players_snapshot = {}
    for pid in list(room.players) + [room.leader_id]:
        p = players.get(pid)
        if p:
            players_snapshot[pid] = {
                'name':       p.name,
                'coins':      p.coins,
                'is_leader':  p.is_leader,
                'is_test':    getattr(p, 'is_test', False),
                'game_history': {
                    'balances':      list(p.game_history.get('balances', [])),
                    'contributions': list(p.game_history.get('contributions', [])),
                },
            }

    fixed_groups = room.settings.get('fixed_groups', True)

    # Auswertungsdaten vorberechnen
    # Bei zufälligen Gruppen ist group_comparison wenig aussagekräftig –
    # wir liefern eine leere Liste; die Auswertungsseite zeigt dann die
    # Rundenübersicht stattdessen.
    group_comparison = room.get_group_comparison_data(players, initial_coins) if fixed_groups else []
    top_players_data = get_top_players(room, players, initial_coins)

    # Rundenübersicht (nur bei zufälligen Gruppen – wird vorab berechnet,
    # da players-Dict nach dem Cleanup nicht mehr verfügbar ist)
    round_by_round_summary = room.get_round_by_round_summary(players) if not fixed_groups else []

    # Detaildaten für /api/history/<id>/evaluation_details
    groups_details = []
    for group in room.groups:
        members_data = []
        for pid in group['player_ids']:
            p = players.get(pid)
            if not p:
                continue
            contributions = p.game_history.get('contributions', [])
            balances      = p.game_history.get('balances', [])
            coop_rate = round(
                (sum(1 for c in contributions if c > 0) / len(contributions)) * 100, 1
            ) if contributions else 0
            members_data.append({
                'name':          p.name,
                'final_balance': round(p.coins, 2),
                'total_profit':  round(p.coins - initial_coins, 2),
                'coop_rate':     coop_rate,
                'contributions': [round(c, 2) for c in contributions],
                'balances':      [round(b, 2) for b in balances],
            })
        groups_details.append({
            'group_number': group['group_number'],
            'members':      members_data,
        })

    # Nur echte (nicht-Test-)Spieler zählen
    real_player_count = sum(
        1 for pid in room.players
        if players.get(pid) and not players[pid].is_leader and not getattr(players[pid], 'is_test', False)
    )

    game_history_store[room_id] = {
        'room_id':                room_id,
        'completed_at':           time.time(),
        'leader_id':              room.leader_id,
        'leader_name':            room.leader_name,
        'settings':               dict(room.settings),
        'current_round':          room.current_round,
        'groups':                 [dict(g) for g in room.groups],
        'group_cooperation':      {k: dict(v) for k, v in room.group_cooperation.items()},
        'players_snapshot':       players_snapshot,
        'group_comparison':       group_comparison,
        'top_players':            top_players_data,
        'groups_details':         groups_details,
        'round_by_round_summary': round_by_round_summary,
        'fixed_groups':           fixed_groups,
        'total_players':          real_player_count,
    }

    logger.info("Raum %s in History gespeichert (%d Runden, %d Spieler)",
                room_id, room.current_round, real_player_count)


# Hilfsfunktion zum Überprüfen der Raum-Zugriffsberechtigung
# Gibt immer (room, player) zurück. room ist None wenn der Raum nicht existiert,
# player ist None wenn kein gültiger Spieler in der Session ist.
def check_room_access(room_id):
    if room_id not in rooms:
        return None, None

    room = rooms[room_id]
    player_id = session.get('player_id')

    if not player_id or player_id not in players:
        return room, None

    player = players[player_id]

    # Prüfe ob Spieler im Raum ist (Leader wird übersprungen)
    if not player.is_leader and player_id not in room.players:
        # Spieler gehört zu einem anderen Raum → Zugriff verweigern
        if player.room_id != room_id:
            return room, None

        # Spieler war im Raum und kann wieder beitreten (Reconnect)
        if room.status != "finished":
            room.add_player(player_id)

    return room, player



# Routen
@app.route('/')
def index():
    player_id = session.get('player_id')
    if player_id and player_id in players:
        p = players[player_id]
        if p.room_id and p.room_id in rooms:
            r = rooms[p.room_id]
            if r.status not in ['finished']:
                # Sofort zur richtigen Seite weiterleiten
                if r.status in ['waiting', 'ready']:
                    return redirect(url_for('game_room', room_id=r.id))
                elif r.status == 'playing':
                    if p.is_leader:
                        return redirect(url_for('leader_dashboard', room_id=r.id))
                    return redirect(url_for('game', room_id=r.id))
                elif r.status == 'round_results':
                    if p.is_leader:
                        return redirect(url_for('leader_dashboard', room_id=r.id))
                    return redirect(url_for('round_results', room_id=r.id))
    return render_template('index.html')

@app.route('/create_game', methods=['GET', 'POST'])
def create_game():
    if request.method == 'POST':
        # Bestehende Einstellungen verarbeiten
        end_mode = request.form.get('end_mode', 'fixed_rounds')
        leader_name = request.form.get('leader_name', 'Prof Maeß').strip()

        try:
            initial_coins = float(request.form.get('initial_coins', '10').replace(',', '.'))
            multiplier = float(request.form.get('multiplier', '2').replace(',', '.'))
        except ValueError:
            initial_coins = 10.0
            multiplier = 2.0
        
        if multiplier <= 0:
            return "Multiplikator muss größer als 0 sein", 400
        
        # Basis-Einstellungen
        settings = {
            'initial_coins': initial_coins,
            'group_size': int(request.form.get('group_size', 4)),
            'multiplier': multiplier,
            'round_duration': int(request.form.get('round_duration', 60)),
            'fixed_groups': request.form.get('fixed_groups') == 'true',
            'end_mode': end_mode,
            'incognito_mode': request.form.get('incognito_mode') == 'true',
            
            'room_type': request.form.get('room_type', 'public'),
            'chat_enabled': request.form.get('chat_enabled') == 'true'
        }
        
        # Zugangscode für private Räume generieren
        if settings['room_type'] == 'private':
            settings['access_code'] = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
        
        # Modus-spezifische Einstellungen (bestehend)
        if end_mode == 'fixed_rounds':
            settings['max_rounds'] = max(1, min(int(request.form.get('max_rounds', 5)), 50))
        elif end_mode == 'probability':
            settings['min_rounds'] = max(1, min(int(request.form.get('min_rounds', 1)), 50))
            settings['max_rounds_probability'] = max(1, min(int(request.form.get('max_rounds_probability', 10)), 50))
            settings['continue_probability'] = max(0.0, min(1.0, float(request.form.get('continue_probability', 0.5))))

            if settings['min_rounds'] > settings['max_rounds_probability']:
                settings['min_rounds'] = settings['max_rounds_probability']
        
        # Raum erstellen
        room_id = str(uuid.uuid4())[:8]
        leader_id = str(uuid.uuid4())
        
        # Leader als Spieler erstellen
        leader = Player(leader_id, leader_name, is_leader=True)
        leader.room_id = room_id
        players[leader_id] = leader
        
        # Raum erstellen
        room = GameRoom(room_id, leader_id, settings)
        room.leader_name = leader_name
        rooms[room_id] = room
        
        # Leader-Session speichern
        session['player_id'] = leader_id
        session['room_id'] = room_id
        session['is_leader'] = True
        
        test_names = [f"Test-Spieler {i}" for i in range(1, 40)]
        for name in test_names:
            test_id = str(uuid.uuid4())
            test_player = Player(test_id, name, is_test=True)
            test_player.room_id = room_id
            test_player.coins = room.settings['initial_coins']
            test_player.game_history['balances'].append(test_player.coins)
            test_player.ready = True
            players[test_id] = test_player
            room.add_player(test_id)

        socketio.emit('room_list_updated', {
            'action': 'created',
            'room_id': room_id
        })

        logger.info("Raum erstellt – id=%s, leader='%s', Modus=%s, Gruppen=%d, Multiplikator=%.2f, Startguthaben=%.2f",
                    room_id, leader_name,
                    settings['end_mode'], settings['group_size'],
                    settings['multiplier'], settings['initial_coins'])

        return redirect(url_for('game_room', room_id=room_id))
    
    return render_template('create_game.html')

@app.route('/join_game')
def join_game():
    return render_template('join_game.html')

@app.route('/rejoin')
def rejoin():
    """Stellt die Session aus localStorage-Daten wieder her und leitet weiter."""
    player_id = request.args.get('player_id')
    room_id   = request.args.get('room_id')

    if not player_id or not room_id:
        return redirect(url_for('index'))
    if player_id not in players or room_id not in rooms:
        return redirect(url_for('index'))

    player = players[player_id]
    room   = rooms[room_id]

    # Sicherheitscheck: Spieler gehört wirklich zu diesem Raum
    if player.room_id != room_id:
        return redirect(url_for('index'))

    # Session wiederherstellen
    session['player_id'] = player_id
    session['room_id']   = room_id
    session['is_leader'] = player.is_leader
    session.permanent    = True
    session.modified     = True

    # Zur richtigen Seite weiterleiten
    if room.status in ['waiting', 'ready']:
        return redirect(url_for('game_room', room_id=room_id))
    elif room.status == 'playing':
        if player.is_leader:
            return redirect(url_for('leader_dashboard', room_id=room_id))
        return redirect(url_for('game', room_id=room_id))
    elif room.status == 'round_results':
        if player.is_leader:
            return redirect(url_for('leader_dashboard', room_id=room_id))
        return redirect(url_for('round_results', room_id=room_id))
    elif room.status == 'finished':
        return redirect(url_for('evaluation', room_id=room_id))

    return redirect(url_for('game_room', room_id=room_id))

@app.route('/game_room/<room_id>')
def game_room(room_id):
    # Prüfe ob Spieler-ID als URL-Parameter übergeben wurde
    player_id_from_param = request.args.get('player_id')
    
    if player_id_from_param and player_id_from_param in players:
        # Setze Session-Daten aus URL-Parameter
        session['player_id'] = player_id_from_param
        session['room_id'] = room_id
        session['is_leader'] = False
        session.modified = True
        logger.debug("Session gesetzt von URL-Parameter: player_id=%s, room_id=%s", player_id_from_param, room_id)
    
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    is_leader = player.id == room.leader_id

    # Session an den aktuellen Raum angleichen – wichtig wenn der Spieler nach
    # "Erneut spielen" in einen neuen Raum weitergeleitet wurde. base.html liest
    # currentRoomId aus session.get("room_id"); stimmt sie nicht, sendet der
    # Client join_game_room an den falschen SocketIO-Raum und Bereit-Events
    # kommen nie an.
    if session.get('room_id') != room_id or session.get('player_id') != player.id:
        session['room_id']   = room_id
        session['player_id'] = player.id
        session['is_leader'] = is_leader
        session.modified     = True

    # Während laufendem Spiel: Spieler zur richtigen Seite weiterleiten
    if not is_leader and room.status == 'playing':
        return redirect(url_for('game', room_id=room_id))
    if not is_leader and room.status == 'round_results':
        return redirect(url_for('round_results', room_id=room_id))
    if room.status == 'finished':
        return redirect(url_for('evaluation', room_id=room_id))

    # Spielerliste für Template vorbereiten
    player_list = []
    for pid in room.players:
        p = players[pid]
        player_list.append({
            'id': p.id,
            'name': p.name,
            'coins': p.coins,
            'ready': p.ready
        })
    
    return render_template('game_room.html',
                         room=room,
                         players=player_list,
                         ready_count=sum(1 for pid in room.players if players[pid].ready),
                         is_leader=is_leader,
                         current_player_ready=player.ready if not is_leader else False)

@app.route('/game/<room_id>')
def game(room_id):
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    if player.is_leader:
        return redirect(url_for('leader_dashboard', room_id=room_id))

    # Falsche Seite für aktuellen Status → korrekte Seite
    if room.status == 'round_results':
        return redirect(url_for('round_results', room_id=room_id))
    if room.status == 'finished':
        return redirect(url_for('evaluation', room_id=room_id))
    if room.status in ['waiting', 'ready']:
        return redirect(url_for('game_room', room_id=room_id))
    
    # Initialisiere Spieler-Guthaben beim ersten Betreten des Spiels
    if player.coins == 0 and len(player.game_history['balances']) == 0:
        player.coins = room.settings['initial_coins']
        player.game_history['balances'].append(player.coins)
    
    player_group = room.get_player_group(player.id) if room.groups else None
    
    return render_template('game.html', 
                         room=room, 
                         player=player,
                         player_group=player_group,
                         current_round=room.current_round)

@app.route('/round_results/<room_id>')
def round_results(room_id):
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    is_leader = player.id == room.leader_id
    results = room.round_results or []
    player_group = next(
        (g for g in results if any(p['id'] == player.id for p in g['players'])),
        None
    )

    return render_template('round_results.html',
                         room=room,
                         player=player,
                         player_group=player_group,
                         is_leader=is_leader)

@app.route('/leader_dashboard/<room_id>')
def leader_dashboard(room_id):
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    is_leader = player.id == room.leader_id
    
    if not is_leader:
        return redirect(url_for('game_room', room_id=room_id))
    
    return render_template('leader_dashboard.html',
                         room=room,
                         player=player,
                         is_leader=is_leader)

def get_top_players(room, players_dict, initial_coins, n=3):
    """Gibt die n Spieler mit dem höchsten Gesamtgewinn zurück."""
    medals = ['🥇', '🥈', '🥉']

    # Gruppen-Lookup: PID → Gruppennummer
    pid_to_group = {}
    for g in room.groups:
        for pid in g.get('player_ids', []):
            pid_to_group[pid] = g['group_number']

    result = []
    for pid in room.players:
        p = players_dict.get(pid)
        if not p or p.is_leader:
            continue
        contributions = p.game_history.get('contributions', [])
        coop_rate = round(
            (sum(1 for c in contributions if c > 0) / len(contributions)) * 100, 1
        ) if contributions else 0
        # Kumulierter Gewinn pro Runde (für Liniendiagramm)
        balances = p.game_history.get('balances', [])
        profit_history = [round(b - initial_coins, 2) for b in balances]
        result.append({
            'name': p.name,
            'profit': round(p.coins - initial_coins, 2),
            'coop_rate': coop_rate,
            'group_number': pid_to_group.get(pid),
            'profit_history': profit_history,
        })
    result.sort(key=lambda x: x['profit'], reverse=True)
    for i, entry in enumerate(result[:n]):
        entry['rank'] = i + 1
        entry['medal'] = medals[i] if i < len(medals) else ''
    return result[:n]


@app.route('/history')
def history_list():
    """History-Seite: listet alle abgeschlossenen Spielsessions auf."""
    return render_template('history.html')


@app.route('/history/<room_id>')
def history_view(room_id):
    """Zeigt die Auswertung eines History-Eintrags im Read-Only-Modus."""
    entry = game_history_store.get(room_id)
    if not entry:
        return redirect(url_for('history_list'))

    leader_id   = entry['leader_id']
    leader_data = entry['players_snapshot'].get(leader_id, {})

    return render_template('evaluation.html',
                           room=HistoryRoomProxy(entry),
                           player=HistoryPlayerProxy(leader_id, leader_data),
                           initial_coins=entry['settings']['initial_coins'],
                           group_comparison=entry['group_comparison'],
                           top_players=entry['top_players'],
                           fixed_groups=entry.get('fixed_groups', True),
                           is_leader=True,
                           history_mode=True)


@app.route('/history/<room_id>/export')
def history_export(room_id):
    """Liefert die History-Auswertung als eigenständige HTML-Datei zum Download."""
    entry = game_history_store.get(room_id)
    if not entry:
        return redirect(url_for('history_list'))

    fixed_groups_hist = entry.get('fixed_groups', True)
    html = render_template('evaluation_export.html',
                           room=HistoryRoomProxy(entry),
                           group_comparison=entry.get('group_comparison', []),
                           groups_details=entry.get('groups_details', []),
                           top_players=entry['top_players'],
                           initial_coins=entry['settings']['initial_coins'],
                           num_rounds=entry['current_round'],
                           fixed_groups=fixed_groups_hist,
                           round_by_round_summary=entry.get('round_by_round_summary', []),
                           player_export=None,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M Uhr'))

    response = make_response(html)
    response.headers['Content-Disposition'] = f'attachment; filename=Auswertung_Raum_{room_id}.html'
    response.headers['Content-Type']         = 'text/html; charset=utf-8'
    return response



@app.route('/evaluation/<room_id>')
def evaluation(room_id):
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    initial_coins = room.settings['initial_coins']

    # Gruppenvergleichsdaten berechnen
    group_comparison = room.get_group_comparison_data(players, initial_coins)
    
    top_players = get_top_players(room, players, initial_coins)

    return render_template('evaluation.html',
                         room=room,
                         player=player,
                         initial_coins=initial_coins,
                         group_comparison=group_comparison,
                         top_players=top_players,
                         fixed_groups=room.settings.get('fixed_groups', True),
                         is_leader=player.id == room.leader_id,
                         history_mode=False)

@app.route('/evaluation/<room_id>/export')
def evaluation_export(room_id):
    """Liefert die Auswertung als eigenständige HTML-Datei zum Download."""
    room, player = check_room_access(room_id)
    if room is None:
        return redirect(url_for('index'))
    if player is None:
        return redirect(url_for('join_game'))

    initial_coins = room.settings['initial_coins']
    fixed_groups  = room.settings.get('fixed_groups', True)
    is_leader     = player.id == room.leader_id

    group_comparison       = room.get_group_comparison_data(players, initial_coins) if fixed_groups else []
    round_by_round_summary = [] if fixed_groups else room.get_round_by_round_summary(players)

    # Persönliche Spielerdaten (nur für Nicht-Leader)
    player_export = None
    if not is_leader:
        contributions = player.game_history.get('contributions', [])
        balances      = player.game_history.get('balances', [])
        player_export = {
            'name':               player.name,
            'final_balance':      round(player.coins, 2),
            'profit':             round(player.coins - initial_coins, 2),
            'total_contribution': round(sum(contributions), 2),
            'contributions':      [round(c, 2) for c in contributions],
            'balances':           [round(b, 2) for b in balances],
        }

    # Gruppendetails für alle Gruppen berechnen (nur bei fixen Gruppen)
    groups_details = []
    for group in room.groups:
        members_data = []
        for pid in group['player_ids']:
            if pid not in players:
                continue
            p = players[pid]
            contributions = p.game_history['contributions']
            coop_rate = round(
                (sum(1 for c in contributions if c > 0) / len(contributions)) * 100, 1
            ) if contributions else 0
            members_data.append({
                'name': p.name,
                'final_balance': round(p.coins, 2),
                'total_profit': round(p.coins - initial_coins, 2),
                'coop_rate': coop_rate,
                'contributions': [round(c, 2) for c in contributions],
            })
        groups_details.append({
            'group_number': group['group_number'],
            'members': members_data,
        })

    top_players = get_top_players(room, players, initial_coins)

    html = render_template('evaluation_export.html',
                           room=room,
                           group_comparison=group_comparison,
                           groups_details=groups_details,
                           top_players=top_players,
                           initial_coins=initial_coins,
                           num_rounds=room.current_round,
                           fixed_groups=fixed_groups,
                           round_by_round_summary=round_by_round_summary,
                           player_export=player_export,
                           now=datetime.now().strftime('%d.%m.%Y %H:%M Uhr'))

    response = make_response(html)
    response.headers['Content-Disposition'] = f'attachment; filename=Auswertung_Raum_{room_id}.html'
    response.headers['Content-Type'] = 'text/html; charset=utf-8'
    return response

# API Routes
@app.route('/api/check_rejoin')
def api_check_rejoin():
    """Prüft ob ein Spieler (aus localStorage) noch rejoinen kann."""
    player_id = request.args.get('player_id')
    room_id   = request.args.get('room_id')

    if not player_id or not room_id:
        return jsonify({'valid': False})
    if player_id not in players or room_id not in rooms:
        return jsonify({'valid': False})

    player = players[player_id]
    room   = rooms[room_id]

    if player.room_id != room_id or room.status == 'finished':
        return jsonify({'valid': False})

    status_labels = {
        'waiting':       'Lobby',
        'ready':         'Lobby',
        'playing':       'Spiel läuft',
        'round_results': 'Rundenauswertung',
    }
    return jsonify({
        'valid':        True,
        'status':       room.status,
        'status_label': status_labels.get(room.status, room.status),
    })


@app.route('/api/rooms')
def api_rooms():
    page  = max(1, int(request.args.get('page',  1)))
    limit = max(1, int(request.args.get('limit', 10)))

    available_rooms = []
    for room_id, room in rooms.items():
        if room.status in ["waiting", "ready"]:
            normal_players = [pid for pid in room.players if pid in players and not players[pid].is_leader]
            available_rooms.append({
                'id':           room_id,
                'player_count': len(normal_players),
                'current_round': room.current_round,
                'settings':     room.settings,
                'status':       room.status,
                'room_type':    room.room_type,
                'created_at':   getattr(room, 'created_at', 0),
            })

    # Neueste Räume zuerst
    available_rooms.sort(key=lambda r: r['created_at'], reverse=True)

    start      = (page - 1) * limit
    page_items = available_rooms[start:start + limit]
    return jsonify({
        'items':    page_items,
        'has_more': (start + limit) < len(available_rooms),
        'total':    len(available_rooms),
    })

@app.route('/api/room/<room_id>/check_access')
def api_room_check_access(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404
    
    room = rooms[room_id]
    return jsonify({
        'room_type': room.room_type,
        'exists': True
    })

@app.route('/api/room/<room_id>/requirements')
def api_room_requirements(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404
    
    room = rooms[room_id]
    group_size = room.settings.get('group_size', 4)
    
    # Nur normale Spieler zählen
    normal_players = [pid for pid in room.players if pid in players and not players[pid].is_leader]
    current_players = len(normal_players)
    
    # Ready-Spieler zählen
    ready_players = sum(1 for pid in normal_players if players[pid].ready)
    
    can_start = current_players >= group_size and current_players % group_size == 0 and ready_players == current_players
    
    # Detaillierte Fehlermeldung
    message = ''
    if not can_start:
        if current_players < group_size:
            message = f'Zu wenige Spieler. Benötigt mindestens {group_size}, haben {current_players}'
        elif current_players % group_size != 0:
            message = f'Spieleranzahl muss durch {group_size} teilbar sein. Aktuell: {current_players}'
        else:
            message = f'Nicht alle Spieler sind bereit. Bereit: {ready_players}/{current_players}'
    
    return jsonify({
        'can_start': can_start,
        'current_players': current_players,
        'ready_players': ready_players,
        'required_players': group_size,
        'message': message
    })

@app.route('/api/room/<room_id>/status')
def api_room_status(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404
    
    room = rooms[room_id]
    
    # Status automatisch aktualisieren bevor gesendet wird
    if room.status in ["waiting", "ready"]:
        room.update_status_based_on_conditions()
    
    return jsonify({
        'status': room.status,
        'current_round': room.current_round
    })

@app.route('/api/room/<room_id>/dashboard_status')
def api_room_dashboard_status(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404

    room = rooms[room_id]

    # Wenn die Runde bereits in 'round_results' ist, benutze den gespeicherten finalen Zähler.
    if room.status == 'round_results':
        submitted = getattr(room, 'submitted_count', len(room.submitted_players))
        total = getattr(room, 'total_players', len([pid for pid in room.players if pid in players and not players[pid].is_leader]))
    else:
        submitted = len(room.submitted_players)
        total = len([pid for pid in room.players if pid in players and not players[pid].is_leader])

    return jsonify({
        'success': True,
        'time_left': room.timer_remaining,
        'submitted_count': submitted,
        'total_players': total,
        'timer_running': room.timer_running,
        'status': room.status
    })

@app.route('/api/room/<room_id>/group_details')
def api_group_details(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404

    room = rooms[room_id]
    groups_data = []

    # Ergebnisse der aktuellen Runde als Lookup vorbereiten (für round_results Status)
    results_lookup = {}
    if room.status == 'round_results' and room.round_results:
        for group_result in room.round_results:
            for p in group_result['players']:
                results_lookup[p['id']] = {
                    'contribution': p['contribution'],
                    'new_balance': p['new_balance'],
                    'profit': p['profit']
                }

    for group in room.groups:
        members_data = []

        for pid in group['player_ids']:
            if pid not in players:
                continue
            player = players[pid]

            # Bisherige Kooperationsquote (aus abgeschlossenen Runden)
            contributions = player.game_history['contributions']

            # Aktuelle Runde einberechnen falls bereits eingezahlt
            current_contribution = player.current_contribution
            if pid in room.submitted_players and current_contribution is not None:
                all_contributions = contributions + [current_contribution]
            elif room.status == 'round_results' and pid in results_lookup:
                all_contributions = contributions  # bereits in game_history enthalten
            else:
                all_contributions = contributions

            coop_rate = round(
                (sum(1 for c in all_contributions if c > 0) / len(all_contributions)) * 100, 1
            ) if all_contributions else 0

            # Status abhängig vom Rundenstand bestimmen
            if room.status == 'round_results' and pid in results_lookup:
                # Runde beendet: Daten aus Ergebnissen nehmen
                result = results_lookup[pid]
                has_submitted = True
                contribution = result['contribution']
                display_coins = result['new_balance']
            elif pid in room.submitted_players:
                # Runde läuft, Spieler hat eingezahlt: temporären Abzug anzeigen
                contribution = player.current_contribution or 0
                has_submitted = True
                display_coins = round(player.coins - contribution, 2)
            else:
                # Noch nicht eingezahlt
                has_submitted = False
                contribution = None
                display_coins = round(player.coins, 2)

            members_data.append({
                'name': player.name,
                'coins': display_coins,
                'has_submitted': has_submitted,
                'contribution': round(contribution, 2) if contribution is not None else None,
                'coop_rate': coop_rate,
                'profit': results_lookup[pid]['profit'] if room.status == 'round_results' and pid in results_lookup else None  # NEU
            })

        total_balance = sum(m['coins'] for m in members_data)

        groups_data.append({
            'group_number': group['group_number'],
            'total_balance': round(total_balance, 2),
            'members': members_data
        })

    return jsonify(groups_data)

@app.route('/api/room/<room_id>/can_continue')
def api_room_can_continue(room_id):
    """Prüft ob das Spiel weitergehen kann"""
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404
    
    room = rooms[room_id]
    can_continue = room.should_continue_game()
    
    return jsonify({
        'can_continue': can_continue,
        'current_round': room.current_round,
        'max_rounds': room.settings.get('max_rounds', 5)
    })

@app.route('/api/history')
def api_history():
    """Gibt eine nach Datum sortierte, paginierte Liste aller History-Einträge zurück."""
    page  = max(1, int(request.args.get('page',  1)))
    limit = max(1, int(request.args.get('limit', 10)))
    sort  = request.args.get('sort', 'newest')
    query = (request.args.get('q', '') or '').strip().lower()

    entries = list(game_history_store.values())

    # Optionale Suche nach Raum-ID oder Leader-Name
    if query:
        entries = [
            e for e in entries
            if query in str(e.get('room_id', '')).lower()
            or query in str(e.get('leader_name', '')).lower()
        ]

    # Sortierung: 'oldest' = aufsteigend, sonst (Standard) neueste zuerst
    reverse_order = (sort != 'oldest')
    all_entries   = sorted(entries, key=lambda x: x['completed_at'], reverse=reverse_order)

    start       = (page - 1) * limit
    page_slice  = all_entries[start:start + limit]

    items = []
    for entry in page_slice:
        items.append({
            'room_id':       entry['room_id'],
            'completed_at':  entry['completed_at'],
            'leader_name':   entry['leader_name'],
            'current_round': entry['current_round'],
            'total_players': entry['total_players'],
            'settings': {
                'initial_coins': entry['settings']['initial_coins'],
                'multiplier':    entry['settings']['multiplier'],
                'group_size':    entry['settings']['group_size'],
            },
        })
    return jsonify({
        'items':    items,
        'has_more': (start + limit) < len(all_entries),
        'total':    len(all_entries),
    })


@app.route('/api/history/<room_id>/evaluation_details')
def api_history_evaluation_details(room_id):
    """Gibt Gruppen-Detaildaten für einen History-Eintrag zurück."""
    entry = game_history_store.get(room_id)
    if not entry:
        return jsonify({'error': 'Nicht gefunden'}), 404
    if entry.get('fixed_groups', True):
        return jsonify({'mode': 'fixed',  'data': entry['groups_details']})
    return jsonify({'mode': 'random', 'data': entry['round_by_round_summary']})


@app.route('/api/room/<room_id>/evaluation_details')
def api_evaluation_details(room_id):
    if room_id not in rooms:
        return jsonify({'error': 'Raum nicht gefunden'}), 404

    room = rooms[room_id]
    initial_coins = room.settings['initial_coins']
    fixed_groups  = room.settings.get('fixed_groups', True)

    # Zufällige Gruppen → Rundenübersicht zurückgeben
    if not fixed_groups:
        return jsonify({'mode': 'random', 'data': room.get_round_by_round_summary(players)})

    # Feste Gruppen → bisheriges Format
    groups_data = []
    for group in room.groups:
        members_data = []
        for pid in group['player_ids']:
            if pid not in players:
                continue
            player = players[pid]
            contributions = player.game_history['contributions']
            balances      = player.game_history['balances']
            coop_rate = round(
                (sum(1 for c in contributions if c > 0) / len(contributions)) * 100, 1
            ) if contributions else 0
            members_data.append({
                'name':          player.name,
                'final_balance': round(player.coins, 2),
                'total_profit':  round(player.coins - initial_coins, 2),
                'coop_rate':     coop_rate,
                'contributions': [round(c, 2) for c in contributions],
                'balances':      [round(b, 2) for b in balances],
            })
        groups_data.append({'group_number': group['group_number'], 'members': members_data})

    return jsonify({'mode': 'fixed', 'data': groups_data})


# WebSocket Events
@socketio.on('connect')
def handle_connect():
    player_id = session.get('player_id')
    logger.debug("Client verbunden: sid=%s, player_id=%s", request.sid, player_id)

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    player_id = sid_to_player.pop(sid, None)
    
    if not player_id:
        return
        
    player_sids[player_id].discard(sid)

    # Wenn noch andere Sockets/Tabs dieses Players existieren -> nichts tun
    if player_sids[player_id]:
        logger.debug("Disconnect: Player %s hat noch offene Sockets – kein Entfernen", player_id)
        return

    # --- Vollständiger Disconnect (alle SIDs/Tabs für diesen Player sind weg) ---
    player = players.get(player_id)
    if not player:
        return

    room_id = player.room_id
    if not room_id or room_id not in rooms:
        return

    room = rooms[room_id]

    # --- Leader-Disconnect ---
    if player.is_leader:
        if room.status not in ["finished"]:
            # 5-Minuten-Gnadenfrist starten – danach Raum schließen
            with removal_lock:
                if player_id in pending_removals:
                    pending_removals[player_id].kill()
                greenlet = spawn(delayed_leader_disconnect, player_id, room_id, 300)
                pending_removals[player_id] = greenlet
            logger.info("Leader %s disconnected – Raum %s hat 300s Gnadenfrist", player_id, room_id)
        return

    # --- Normaler Spieler ---
    # Spiel läuft: Spieler NICHT entfernen, aber Mitspielern Bescheid geben
    if room.status not in ["waiting", "ready"]:
        socketio.emit('player_connection_changed', {
            'player_id': player_id,
            'player_name': player.name,
            'connected': False
        }, room=room_id)
        return

    # --- Lobby: Gnadenfrist von 10s starten ---
    with removal_lock:
        if player_id in pending_removals:
            pending_removals[player_id].kill()
        greenlet = spawn(delayed_remove_player, player_id, room_id, 10)
        pending_removals[player_id] = greenlet

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    player_name = data.get('player_name')
    access_code = data.get('access_code', '')
    
    logger.debug("join_room aufgerufen – Raum: %s, Spieler: %s", room_id, player_name)
    
    if not room_id or not player_name:
        emit('error', {'message': 'Ungültige Daten'})
        return
    
    if room_id not in rooms:
        emit('error', {'message': 'Raum nicht gefunden'})
        return
    
    room = rooms[room_id]

    # Prüfe Zugangscode für private Räume
    if room.room_type == 'private':
        if not access_code or access_code != room.access_code:
            emit('error', {'message': 'Ungültiger Zugangscode für privaten Raum'})
            return
    
    # Prüfe ob Spielername bereits im Raum existiert
    existing_names = [players[pid].name for pid in room.players if pid in players and not players[pid].is_leader]
    if player_name in existing_names:
        emit('error', {'message': 'Spielername bereits vergeben'})
        return
    
    # Neuen Spieler erstellen
    player_id = str(uuid.uuid4())
    player = Player(player_id, player_name)
    player.room_id = room_id
    player.coins = room.settings['initial_coins']
    player.game_history['balances'].append(player.coins)

    session['player_id'] = player_id
    
    players[player_id] = player
    
    # Spieler zum Raum hinzufügen
    if room.add_player(player_id):
        join_room(room_id)
        sid_to_player[request.sid] = player_id
        player_sids[player_id].add(request.sid)
        room.update_status_based_on_conditions()

        with game_timer_lock:
            timer = game_timers.get(room_id)
            if timer and timer.is_running:
                emit('game_timer_update', {
                    'start_time':    int(timer.start_time * 1000) if timer.start_time else None,
                    'duration':      timer.duration,
                    'time_left':     timer.get_time_left(),
                    'timer_running': True,
                    'server_now_ms': int(time.time() * 1000),
                }, room=request.sid)
        
        logger.info("Spieler '%s' (id=%s) zu Raum %s hinzugefügt – Spieleranzahl: %d",
                    player_name, player_id, room_id, len(room.players))
        
        # Benachrichtige andere Spieler über neuen Spieler
        player_data = {
            'id': player_id,
            'name': player_name,
            'coins': player.coins,
            'ready': False
        }
        emit('player_joined', player_data, room=room_id)
        
        # Sende Erfolgsmeldung mit player_id für Weiterleitung
        emit('room_joined', {
            'room_id': room_id,
            'player_id': player_id
        })
        
        logger.debug("room_joined Event gesendet – Raum %s, player_id %s", room_id, player_id)

@socketio.on('join_game_room')
def handle_join_game_room(data):
    """Client sendet beim Laden der Spielseite: { room_id: ... }"""
    room_id = data.get('room_id')
    player_id = session.get('player_id')

    if not player_id or player_id not in players:
        return
    
    # Laufenden Entfernungs-Task abbrechen (Reconnect in Gnadenfrist)
    with removal_lock:
        if player_id in pending_removals:
            greenlet = pending_removals.pop(player_id)
            try:
                greenlet.kill()
            except Exception:
                pass

    # Reconnect-Benachrichtigung an den Raum senden
    player = players.get(player_id)
    if player and room_id and room_id in rooms:
        room = rooms[room_id]
        if room.status not in ["waiting", "ready", "finished"]:
            if player.is_leader:
                socketio.emit('leader_reconnected', {
                    'message': 'Der Spielleiter ist wieder verbunden.'
                }, room=room_id)
            else:
                socketio.emit('player_connection_changed', {
                    'player_id': player_id,
                    'player_name': player.name,
                    'connected': True
                }, room=room_id)

    try:
        # Join personal room
        join_room(player_id)
        sid_to_player[request.sid] = player_id
        player_sids[player_id].add(request.sid)

        # Join game room
        if room_id:
            join_room(room_id)

            room = rooms.get(room_id)
            if room:
                # Ankunft registrieren – löst den Timer-Start aus, sobald alle da sind
                player = players.get(player_id)
                if (player
                        and room.status == 'playing'
                        and player_id in room.players_waiting_for_round):
                    room.players_arrived_for_round.add(player_id)
                    logger.debug("Spieler %s angekommen – %d/%d",
                                 player_id,
                                 len(room.players_arrived_for_round),
                                 len(room.players_waiting_for_round))

            if room:
                # Timer-Status
                timer = game_timers.get(room_id)
                if timer and timer.is_running:
                    payload = {
                        'time_left':     timer.get_time_left(),
                        'start_time':    int(timer.start_time * 1000) if timer.start_time else None,
                        'duration':      timer.duration,
                        'timer_running': True,
                        'server_now_ms': int(time.time() * 1000),
                    }
                else:
                    payload = {
                        'time_left': room.timer_remaining,
                        'start_time': None,
                        'duration': room.settings.get('round_duration', 60),
                        'timer_running': bool(room.timer_running)
                    }

                non_leader_ids = room.non_leader_pids()
                if room.status == 'round_results':
                    submitted_c = getattr(room, 'submitted_count', len(room.submitted_players))
                    total_p     = getattr(room, 'total_players', len(non_leader_ids))
                else:
                    submitted_c = len(room.submitted_players)
                    total_p     = len(non_leader_ids)

                # Prüfen ob Spieler bereits eingereicht hat
                player = players.get(player_id)
                has_submitted = player_id in room.submitted_players
                player_contribution = round(player.current_contribution, 2) if player and has_submitted and player.current_contribution is not None else 0

                emit('room_state_update', {
                    'timer': payload,
                    'submitted_count': submitted_c,
                    'total_players': total_p,
                    'room_status': room.status,
                    'current_round': room.current_round,
                    'has_submitted': has_submitted,
                    'player_contribution': player_contribution
                }, room=request.sid)

                # Leader-Reconnect im round_results-Status: Ankunftszähler direkt nachliefern
                if player and player.is_leader and room.status == 'round_results':
                    arrived = len(room.players_in_results)
                    total_non_leader = len(non_leader_ids)
                    emit('results_arrival_update', {
                        'arrived': arrived,
                        'total': total_non_leader,
                        'all_ready': arrived >= total_non_leader and total_non_leader > 0
                    }, room=request.sid)

    except Exception as e:
        logger.error("Fehler in join_game_room (sid=%s): %s", request.sid, e, exc_info=True)

@socketio.on('player_ready')
def handle_player_ready():
    # sid_to_player ist zuverlässiger als die Session (die beim WS-Handshake eingefroren ist)
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        # Spieler nicht gefunden – Button-Reset an Client schicken damit er nicht hängt
        emit('player_ready_error', {}, room=request.sid)
        return

    player = players[player_id]
    room_id = player.room_id

    if room_id and room_id in rooms and not player.is_leader:
        player.ready = not player.ready

        room = rooms[room_id]
        room.update_status_based_on_conditions()

        ready_count = sum(1 for pid in room.players if pid in players and players[pid].ready and not players[pid].is_leader)

        emit('player_ready_changed', {
            'player_id': player_id,
            'ready': player.ready,
            'ready_count': ready_count,
            'room_status': room.status
        }, room=room_id)
    else:
        emit('player_ready_error', {}, room=request.sid)

@socketio.on('start_game')
def handle_start_game():
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return
    
    player = players[player_id]
    room_id = player.room_id
    
    if room_id and room_id in rooms and player.is_leader:
        room = rooms[room_id]
        
        # Prüfe ob Spiel gestartet werden kann
        if room.can_start_game():
            room.status = "playing"
            room.current_round = 1
            room.create_groups()
            duration = room.settings.get('round_duration', 60)

            for pid in room.players:
                players[pid].ready = False

            # Timer startet, sobald der Leader seine Seite geladen hat.
            # Der Leader sendet join_game_room wenn leader_dashboard bereit ist.
            waiting_for = frozenset([room.leader_id]) if room.leader_id in players else frozenset()
            room.players_arrived_for_round = set()
            room.players_waiting_for_round = waiting_for

            logger.info("Spiel gestartet – Raum %s, Runde %d, Timer %ds, Spieler: %d",
                        room_id, room.current_round, duration, len(room.non_leader_pids()))

            # game_started zuerst – Clients starten sofort die Seiten-Navigation
            emit('game_started', {
                'room_id': room_id,
                'current_round': room.current_round
            }, room=room_id)

            # Timer startet erst, wenn alle echten Spieler join_game_room gesendet
            # haben (= Spielseite vollständig geladen). Fallback nach 15s.
            def _wait_for_players_and_start(rid, dur, expected):
                deadline = time.time() + 5
                while time.time() < deadline:
                    r = rooms.get(rid)
                    if not r or r.status != 'playing':
                        return
                    if not expected or expected.issubset(r.players_arrived_for_round):
                        break
                    sleep(0.2)

                r = rooms.get(rid)
                if not r or r.status != 'playing':
                    return

                get_or_create_game_timer(rid, dur)
                simulate_test_players(rid)
                t = game_timers.get(rid)
                if t:
                    socketio.emit('game_timer_update', {
                        'start_time':    int(t.start_time * 1000),
                        'duration':      dur,
                        'time_left':     t.get_time_left(),
                        'timer_running': True,
                        'server_now_ms': int(time.time() * 1000),
                    }, room=rid)
                logger.info("Timer Raum %s gestartet – %d/%d Spieler geladen",
                            rid, len(r.players_arrived_for_round), len(expected))

            spawn(_wait_for_players_and_start, room_id, duration, waiting_for)
        else:
            group_size    = room.settings.get('group_size', 4)
            non_leaders   = room.non_leader_pids()
            player_count  = len(non_leaders)
            ready_count   = sum(1 for pid in non_leaders if players[pid].ready)

            if player_count < group_size:
                error_msg = f"Zu wenige Spieler. Benötigt mindestens {group_size}, haben {player_count}"
            elif player_count % group_size != 0:
                error_msg = f"Spieleranzahl muss durch {group_size} teilbar sein. Aktuell: {player_count}"
            else:
                error_msg = f"Nicht alle Spieler sind bereit. Bereit: {ready_count}/{player_count}"

            emit('error', {'message': error_msg})

@socketio.on('submit_contribution')
def handle_submit_contribution(data):
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return

    try:
        contribution = round(float(data.get('contribution', 0)), 2)
    except (TypeError, ValueError):
        return
    if contribution < 0:
        emit('error', {'message': 'Ungültiger Beitrag'}, room=request.sid)
        return

    player = players[player_id]
    room_id = player.room_id

    if not (room_id and room_id in rooms and not player.is_leader):
        return

    room = rooms[room_id]

    # Wir wollen race-conditions mit finish_round vermeiden.
    need_finish = False

    with finish_round_lock:
        # Falls die Runde bereits in Ergebnissen ist, IGNORIERE dieses Submit (zu spät)
        if room.status in ["round_results", "finished"]:
            return

        # Prüfe ob genügend Coins vorhanden sind
        if contribution > player.coins:
            emit('error', {'message': 'Nicht genügend Coins verfügbar'}, room=request.sid)
            return

        # Beitrag speichern (atomar)
        player.current_contribution = contribution
        room.submitted_players.add(player_id)

        submitted_count = len(room.submitted_players)
        total_players   = len(room.non_leader_pids())

        # Aktiviere Timer-Status Update
        if room_id in game_timers:
            timer = game_timers[room_id]
            try:
                room.update_timer_status(timer.get_time_left(), submitted_count, total_players, timer.is_running)
            except Exception:
                pass

        # Sende Zwischenstand an den Raum
        try:
            emit('contribution_submitted', {
                'submitted_count': submitted_count,
                'total_players': total_players
            }, room=room_id)
        except Exception:
            pass

        # Falls alle eingereicht haben -> Runde beenden (außerhalb des Locks)
        if submitted_count == total_players:
            need_finish = True

    # Achtung: finish_round kann die gleiche Lock benutzen - rufe es *außerhalb* des Locks auf
    if need_finish:
        try:
            stop_game_timer(room_id)
        except Exception:
            pass
        try:
            finish_round(room_id)
        except Exception as e:
            logger.error("Fehler beim Beenden der Runde nach vollem Submit (Raum %s): %s", room_id, e, exc_info=True)

@socketio.on('retract_contribution')
def handle_retract_contribution():
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return

    player = players[player_id]
    room_id = player.room_id

    if not room_id or room_id not in rooms:
        return

    room = rooms[room_id]

    with finish_round_lock:
        if room.status != 'playing':
            return

        # Beitrag zurückziehen
        player.current_contribution = 0
        room.submitted_players.discard(player_id)

        submitted_count = len(room.submitted_players)
        total_players   = len(room.non_leader_pids())

    socketio.emit('contribution_submitted', {
        'submitted_count': submitted_count,
        'total_players': total_players
    }, room=room_id)

def delayed_remove_player(player_id, room_id, delay_seconds=10):
    """
    Wartet 'delay_seconds' und entfernt dann den Spieler, 
    falls er sich nicht wiederverbunden hat UND das Spiel noch nicht gestartet ist.
    """
    sleep(delay_seconds)
    
    with removal_lock:
        # Prüfen, ob der Task bereits abgebrochen wurde (durch reconnect)
        if player_id not in pending_removals:
            logger.debug("delayed_remove für %s abgebrochen (bereits reconnected)", player_id)
            return
        
        # Entferne den Task-Eintrag
        del pending_removals[player_id]

    # Erneute Prüfung NACH dem Sleep: Ist der Spieler wirklich weg?
    player = players.get(player_id)
    room = rooms.get(room_id)
    
    if not player or not room:
        return
        
    # Hat sich der Spieler in der Zwischenzeit mit einer neuen SID verbunden?
    if player_sids.get(player_id):
        logger.debug("delayed_remove für %s abgebrochen (neue SID gefunden)", player_id)
        return

    # Ist das Spiel in der Zwischenzeit gestartet? Dann nicht entfernen.
    if room.status not in ["waiting", "ready"]:
        logger.debug("delayed_remove für %s abgebrochen (Spiel läuft, Status: %s)", player_id, room.status)
        return

    # --- OK, Spieler ist weg, 10s sind um, Spiel ist noch in der Lobby ---
    # Jetzt den Spieler wirklich entfernen
    logger.info("Gnadenfrist für Spieler '%s' abgelaufen – entferne aus Raum %s", player.name, room_id)
    
    try:
        room.remove_player(player_id)
        
        # Benachrichtige Raum
        socketio.emit('player_disconnected', {
            'player_id': player_id,
            'player_name': player.name
        }, room=room_id)
        
        room.update_status_based_on_conditions()
        
        # Spieler-Objekte aufräumen
        try:
            del players[player_id]
        except KeyError:
            pass
        try:
            del player_sids[player_id]
        except KeyError:
            pass
            
    except Exception as e:
        logger.error("Fehler bei delayed_remove_player (player_id=%s): %s", player_id, e, exc_info=True)

def delayed_leader_disconnect(player_id, room_id, delay_seconds=300):
    """
    Wartet delay_seconds (Standard: 2 Minuten). Ist der Leader danach
    immer noch weg, wird der Raum geschlossen und alle Spieler gekickt.
    """
    sleep(delay_seconds)

    with removal_lock:
        if player_id not in pending_removals:
            return  # Leader hat sich rechtzeitig reconnectet
        del pending_removals[player_id]

    # Nochmal prüfen ob Leader wirklich noch offline ist
    if player_sids.get(player_id):
        return

    room = rooms.get(room_id)
    if not room or room.status == "finished":
        return

    logger.info("Leader %s seit %ds offline – Raum %s wird geschlossen", player_id, delay_seconds, room_id)
    close_room(room_id, 'Der Spielleiter war zu lange offline. Der Raum wurde geschlossen.', reason='leader_timeout')

def _cleanup_room(room_id, reason):
    """Entfernt einen Raum und alle zugehörigen Spieler/Timers sauber aus dem Speicher."""
    room = rooms.pop(room_id, None)
    if not room:
        return
    all_pids = list(room.players) + ([room.leader_id] if room.leader_id not in room.players else [])
    for pid in all_pids:
        players.pop(pid, None)
        player_sids.pop(pid, None)
        with removal_lock:
            greenlet = pending_removals.pop(pid, None)
            if greenlet:
                try:
                    greenlet.kill()
                except Exception:
                    pass
    timer = game_timers.pop(room_id, None)
    if timer:
        try:
            timer.stop()
        except Exception:
            pass
    logger.info("Raum %s aufgeräumt (%s)", room_id, reason)


def cleanup_old_rooms():
    """Hintergrund-Task: läuft täglich um Mitternacht.

    - Beendete Live-Räume:  nach 2 Stunden aufgeräumt.
    - Aktive Live-Räume:    nach 7 Tagen aufgeräumt (Zombie-Räume).
    - History-Einträge:     nach 7 Tagen gelöscht.
    """
    SEVEN_DAYS = 7 * 24 * 3600

    while True:
        # Bis zur nächsten Mitternacht (00:00:00 Lokalzeit) schlafen
        now_dt   = datetime.now()
        midnight = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep((midnight - now_dt).total_seconds())

        now = time.time()
        logger.info("Mitternachts-Cleanup gestartet")

        for room_id, room in list(rooms.items()):
            # Beendete Räume nach 2 Stunden entfernen
            if room.status == "finished":
                ref_time = room.round_end_time or getattr(room, 'created_at', now)
                if (now - ref_time) > 7200:
                    _cleanup_room(room_id, "finished > 2 h")
                    continue

            # Aktive / wartende Räume nach 7 Tagen entfernen
            created_at = getattr(room, 'created_at', None)
            if created_at and (now - created_at) > SEVEN_DAYS:
                _cleanup_room(room_id, "aktiv > 7 Tage")

        # History-Einträge nach 7 Tagen aufräumen
        for room_id, entry in list(game_history_store.items()):
            if (now - entry['completed_at']) > SEVEN_DAYS:
                game_history_store.pop(room_id, None)
                logger.info("History-Eintrag %s nach 7 Tagen aufgeräumt", room_id)

def finish_round(room_id):
    """Beendet die Runde sicher: fehlende Beiträge auf 0 setzen, finale Submit-Status senden,
    Ergebnisse berechnen, Ergebnisse senden und erst danach aufräumen.
    Geschützt durch finish_round_lock, damit nicht mehrfach parallel ausgeführt wird."""
    if room_id not in rooms:
        return

    with finish_round_lock:
        room = rooms.get(room_id)
        if not room:
            return

        # Falls bereits in Ergebnissen, nichts tun
        if room.status == "round_results":
            return

        # 1) Fehlende Beiträge auf 0 setzen und submitted_players ergänzen
        for pid in list(room.players):
            if pid not in room.submitted_players and pid in players and not players[pid].is_leader:
                players[pid].current_contribution = 0
                room.submitted_players.add(pid)

        # Setze Status *so früh wie möglich*, damit keine späteren submits mehr akzeptiert werden
        room.status = "round_results"
        room.round_end_time = time.time()
        room.players_in_results = set()   # Tracking für neue Runde zurücksetzen
        room.timer_running = False
        room.timer_remaining = 0

        # 2) Formuliere payload (einmal) und sende finalen Submit-Status
        submitted_count = len(room.submitted_players)
        total_players   = len(room.non_leader_pids())
        payload = {
            'submitted_count': submitted_count,
            'total_players': total_players
        }

        # Speichere die finalen Werte im Room, damit sie nach einem Reload sichtbar bleiben
        try:
            room.submitted_count = submitted_count
            room.total_players = total_players
        except Exception:
            pass

        try:
            socketio.emit('contribution_submitted', payload, room=room_id)
        except Exception as e:
            logger.warning("Fehler beim Senden des finalen contribution_submitted an Raum %s: %s", room_id, e)

        # 3) Berechne Ergebnisse
        try:
            results = room.calculate_round_results()
        except Exception as e:
            logger.error("Fehler beim Berechnen der Ergebnisse für Raum %s: %s", room_id, e, exc_info=True)
            results = {}

        # 4) History update & reset temporäre contribution (safety)
        for pid in room.players:
            if pid in players:
                p = players[pid]
                # setze auf None, wie zuvor (wird in reset_for_next_round wieder auf 0 gesetzt)
                p.current_contribution = None

        # 5) Sende Ergebnisse an den Raum — Spieler leiten sich via base.html selbst weiter
        try:
            socketio.emit('round_finished', {
                'results': results,
                'current_round': room.current_round,
                'room_id': room_id
            }, room=room_id)
        except Exception as e:
            logger.error("Fehler beim Senden von round_finished für Raum %s: %s", room_id, e)

        # 6) Aufräumen: submitted_players erst jetzt leeren
        try:
            room.submitted_players.clear()
        except Exception:
            pass

        # Testspieler simulieren Ankunft in round_results (verzögert, damit
        # der Leader-Client erst round_finished verarbeitet)
        spawn(simulate_test_players_at_results, room_id)

        logger.info("Runde %d in Raum %s abgeschlossen – %d/%d Einreichungen",
                    room.current_round, room_id, submitted_count, total_players)

@socketio.on('arrived_at_results')
def handle_arrived_at_results():
    """Spieler meldet, dass er round_results geladen hat."""
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return
    player = players[player_id]
    room_id = player.room_id
    if not room_id or room_id not in rooms:
        return
    room = rooms[room_id]
    if room.status != 'round_results':
        return

    room.players_in_results.add(player_id)

    total   = len(room.non_leader_pids())
    arrived = len(room.players_in_results)

    # Leader informieren
    socketio.emit('results_arrival_update', {
        'arrived': arrived,
        'total': total,
        'all_ready': arrived >= total
    }, room=room_id)


@socketio.on('next_round')
def handle_next_round():
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return
    
    player = players[player_id]
    room_id = player.room_id
    
    if room_id and room_id in rooms and player.is_leader:
        room = rooms[room_id]

        # Prüfe ob Spiel weitergeht
        if room.should_continue_game():
            # Nächste Runde starten
            room.reset_for_next_round()
            
            # Timer für neue Runde starten
            duration = room.settings.get('round_duration', 60)
            get_or_create_game_timer(room_id, duration)
            simulate_test_players(room_id)

            _t = game_timers.get(room_id)
            emit('next_round_started', {
                'current_round': room.current_round,
                'room_id': room_id,
                'timer': {
                    'start_time':    int(_t.start_time * 1000) if _t and _t.start_time else None,
                    'duration':      _t.duration if _t else duration,
                    'timer_running': True,
                    'server_now_ms': int(time.time() * 1000),
                }
            }, room=room_id)
            logger.info("Nächste Runde gestartet – Raum %s, Runde %d", room_id, room.current_round)

        else:
            # Spiel beenden und in History sichern
            save_to_history(room_id)
            room.status = "finished"
            logger.info("Spiel beendet – Raum %s nach %d Runde(n)", room_id, room.current_round)
            emit('game_finished', {
                'room_id': room_id
            }, room=room_id)

@socketio.on('leave_room')
def handle_leave_room():
    """Spieler oder Leader verlässt den Raum freiwillig."""
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        emit('left_room', {})
        return

    player = players[player_id]
    room_id = player.room_id

    if not room_id or room_id not in rooms:
        emit('left_room', {})
        return

    room = rooms[room_id]

    if player.is_leader:
        close_room(room_id, 'Der Spielleiter hat den Raum verlassen. Der Raum wurde geschlossen.')

    else:
        lobby_statuses = ['waiting', 'ready', 'finished']
        if room.status in lobby_statuses:
            # Lobby: Spieler sofort vollständig entfernen
            room.remove_player(player_id)
            player.room_id = None

            with removal_lock:
                greenlet = pending_removals.pop(player_id, None)
                if greenlet:
                    try:
                        greenlet.kill()
                    except Exception:
                        pass

            socketio.emit('player_left', {
                'player_id': player_id,
                'player_name': player.name
            }, room=room_id)

            room.update_status_based_on_conditions()
            players.pop(player_id, None)
            player_sids.pop(player_id, None)
        else:
            # Spiel läuft: Spieler bleibt im Raum erhalten (kann rejoin),
            # aber wir informieren die anderen über seinen Weggang
            socketio.emit('player_connection_changed', {
                'player_id': player_id,
                'player_name': player.name,
                'connected': False
            }, room=room_id)

    emit('left_room', {})


@socketio.on('abort_game')
def handle_abort_game():
    """Leader bricht das laufende Spiel manuell ab."""
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return

    player = players[player_id]
    if not player.is_leader:
        return

    room_id = player.room_id
    if not room_id or room_id not in rooms:
        return

    logger.info("Leader %s hat Spiel in Raum %s manuell abgebrochen", player_id, room_id)
    close_room(room_id, 'Das Spiel wurde vom Spielleiter abgebrochen.', reason='leader_abort')


@socketio.on('return_to_lobby')
def handle_return_to_lobby():
    """Leader startet eine neue Session mit denselben Einstellungen.

    Der abgeschlossene Raum wird in der History gesichert. Anschließend wird
    ein neuer Raum erstellt, echte Spieler werden dorthin überführt und alle
    Clients werden weitergeleitet.
    """
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return

    player = players[player_id]
    if not player.is_leader:
        emit('error', {'message': 'Nur der Spielleiter kann das Spiel neu starten.'})
        return

    room_id = player.room_id
    if not room_id or room_id not in rooms:
        return

    room = rooms[room_id]

    # 1) Snapshot in History sichern (vor jeder Modifikation)
    if room_id not in game_history_store:
        save_to_history(room_id)

    # 2) Laufenden Timer stoppen
    timer = game_timers.pop(room_id, None)
    if timer:
        try:
            timer.stop()
        except Exception:
            pass

    # 3) Neuen Raum mit denselben Einstellungen anlegen
    new_room_id   = str(uuid.uuid4())[:8]
    settings      = dict(room.settings)
    initial_coins = settings['initial_coins']

    new_room = GameRoom(new_room_id, player_id, settings)
    new_room.leader_name = room.leader_name
    rooms[new_room_id] = new_room

    # 4) Leader in neuen Raum verschieben
    player.room_id    = new_room_id
    session['room_id'] = new_room_id

    # 5) Echte (nicht-Test-)Spieler übernehmen und zurücksetzen
    for pid in list(room.players):
        p = players.get(pid)
        if not p or p.is_test or p.is_leader:
            continue
        p.room_id              = new_room_id
        p.ready                = False
        p.coins                = initial_coins
        p.current_contribution = 0
        p.game_history         = {'balances': [initial_coins], 'contributions': []}
        new_room.add_player(pid)

    # 6) Test-Spieler des alten Raums löschen; frische für den neuen Raum erstellen
    for pid in list(room.players):
        p = players.get(pid)
        if p and p.is_test:
            players.pop(pid, None)
            player_sids.pop(pid, None)

    test_names = [f"Test-Spieler {i}" for i in range(1, 40)]
    for name in test_names:
        test_id = str(uuid.uuid4())
        test_player = Player(test_id, name, is_test=True)
        test_player.room_id = new_room_id
        test_player.coins   = initial_coins
        test_player.game_history['balances'].append(initial_coins)
        test_player.ready   = True
        players[test_id]    = test_player
        new_room.add_player(test_id)

    # 7) Alten Raum entfernen
    rooms.pop(room_id, None)
    socketio.emit('room_list_updated', {'action': 'deleted',  'room_id': room_id})
    socketio.emit('room_list_updated', {'action': 'created', 'room_id': new_room_id})

    # 8) Alle Clients in den neuen Raum weiterleiten
    socketio.emit('return_to_lobby', {'room_id': new_room_id}, room=room_id)

    logger.info("Neues Spiel erstellt – alter Raum %s → neuer Raum %s", room_id, new_room_id)


@socketio.on('remove_player')
def handle_remove_player(data):
    player_id = sid_to_player.get(request.sid) or session.get('player_id')
    if not player_id or player_id not in players:
        return
    
    requesting_player = players[player_id]
    room_id = requesting_player.room_id
    
    if not room_id or room_id not in rooms:
        return
    
    room = rooms[room_id]
    
    # Nur der Leader kann Spieler entfernen
    if not requesting_player.is_leader:
        emit('error', {'message': 'Nur der Spielleiter kann Spieler entfernen'})
        return
    
    target_player_id = data.get('player_id')

    # Spieler muss zumindest in der Raumliste sein um entfernt werden zu können
    if target_player_id not in room.players:
        emit('error', {'message': 'Spieler nicht gefunden'})
        return

    target_player = players.get(target_player_id)

    # Leader kann sich nicht selbst entfernen
    if target_player and target_player.is_leader:
        emit('error', {'message': 'Der Spielleiter kann sich nicht selbst entfernen'})
        return

    player_name = target_player.name if target_player else f'Spieler ({target_player_id[:8]})'

    # Aus Raumliste entfernen (auch wenn players-Dict inkonsistent ist)
    room.remove_player(target_player_id)

    if target_player:
        target_player.room_id = None

    # Laufenden Gnadenfrist-Timer abbrechen
    with removal_lock:
        greenlet = pending_removals.pop(target_player_id, None)
        if greenlet:
            try:
                greenlet.kill()
            except Exception:
                pass

    players.pop(target_player_id, None)
    player_sids.pop(target_player_id, None)

    logger.info("Spieler '%s' wurde von Leader '%s' aus Raum %s entfernt",
                player_name, requesting_player.name, room_id)

    # Raumstatus aktualisieren
    room.update_status_based_on_conditions()

    # Alle im Raum benachrichtigen
    emit('player_removed', {
        'player_id': target_player_id,
        'player_name': player_name,
        'removed_player_id': target_player_id,
        'removed_by': requesting_player.name
    }, room=room_id)

    # Separate Benachrichtigung an den entfernten Spieler selbst
    emit('player_removed', {
        'player_id': target_player_id,
        'player_name': player_name,
        'removed_player_id': target_player_id,
        'removed_by': requesting_player.name,
        'message': 'Sie wurden aus dem Raum entfernt.'
    }, room=target_player_id)

# Hintergrund-Cleanup starten
spawn(cleanup_old_rooms)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    if is_production:
        # In Production mit gunicorn kompatibel
        socketio.run(app, host='0.0.0.0', port=port, debug=False)
    else:
        # In Development mit Debug-Modus
        socketio.run(app, host='0.0.0.0', port=port, debug=True)