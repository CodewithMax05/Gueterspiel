from gevent import monkey, spawn, sleep  
monkey.patch_all()

import os
import random
import uuid
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, session, redirect, url_for, jsonify
from flask_socketio import SocketIO, emit, join_room, leave_room
from collections import defaultdict
from threading import Lock 

app = Flask(__name__)
is_production = os.environ.get('FLASK_ENV') == 'production'

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_key_123')

app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = is_production
app.config['SESSION_COOKIE_HTTPONLY'] = True

socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    manage_session=True,
    logger=not is_production,  # Logger nur in Development
    engineio_logger=not is_production,  # EngineIO Logger nur in Development
    async_mode='gevent'  # Wichtig für Production
)

# Datenstrukturen zur Verwaltung der Räume und Spieler
rooms = {}
players = {}

class GameRoom:
    def __init__(self, room_id, leader_id, settings):
        self.id = room_id
        self.leader_id = leader_id
        self.settings = settings
        self.players = []
        self.status = "waiting"  # waiting, playing, round_results, finished
        self.current_round = 0
        self.groups = []
        self.round_start_time = None
        self.submitted_players = set()
        self.round_results = None
        self.leader_name = "Leader"  # Füge leader_name hinzu
        
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
        group_size = self.settings.get('group_size', 4)
        has_correct_player_count = len(self.players) >= group_size and len(self.players) % group_size == 0
        
        # Prüfe ob alle Spieler ready sind
        all_players_ready = all(players[pid].ready for pid in self.players)
        
        return has_correct_player_count and all_players_ready
    
    def create_groups(self):
        group_size = self.settings.get('group_size', 4)
        player_list = self.players.copy()
        random.shuffle(player_list)
        
        self.groups = []
        for i in range(0, len(player_list), group_size):
            group = player_list[i:i + group_size]
            self.groups.append({
                'group_number': len(self.groups) + 1,
                'members': [players[pid].name for pid in group],
                'player_ids': group
            })
    
    def get_player_group(self, player_id):
        for group in self.groups:
            if player_id in group['player_ids']:
                return group
        return None
    
    def should_continue_game(self):
        """Prüft ob das Spiel weitergehen soll basierend auf den Einstellungen"""
        # Wenn noch keine Runde gespielt wurde, soll weitergemacht werden
        if self.current_round == 0:
            return True
            
        end_mode = self.settings['end_mode']
        
        if end_mode == 'fixed_rounds':
            return self.current_round < self.settings['max_rounds']
        elif end_mode == 'probability':
            # Mindestrunden garantieren
            if self.current_round < self.settings['min_rounds']:
                return True
            # Maximalrunden begrenzen
            if self.current_round >= self.settings['max_rounds_probability']:
                return False
            # Zwischen min_rounds und max_rounds_probability: Wahrscheinlichkeit prüfen
            return random.random() <= self.settings['continue_probability']
        return False
    
    def reset_for_next_round(self):
        """Bereitet den Raum für die nächste Runde vor"""
        self.status = "playing"
        self.current_round += 1
        
        if not self.settings['fixed_groups']:
            self.create_groups()
        
        # Setze Beiträge zurück
        for pid in self.players:
            players[pid].current_contribution = 0
        
        self.submitted_players.clear()
        self.round_results = None
    
    def calculate_round_results(self):
        results = []
        multiplier = self.settings.get('multiplier', 2)
        
        for group in self.groups:
            total_contribution = 0
            group_players = []
            
            for player_id in group['player_ids']:
                player = players[player_id]
                contribution = player.current_contribution
                total_contribution += contribution
                
                # Guthaben vor der Runde speichern
                old_balance = player.coins
                
                # Auszahlung berechnen
                payout = (total_contribution * multiplier) / len(group['player_ids'])
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
                'payout_per_player': round((total_contribution * multiplier) / len(group['player_ids']), 2),
                'players': group_players
            })
        
        self.round_results = results
        return results

class Player:
    def __init__(self, player_id, name, is_leader=False):
        self.id = player_id
        self.name = name
        self.is_leader = is_leader
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
        """Läuft in einem eigenen Greenlet und sendet Timer-Updates"""
        start_time = time.time()
        while self.is_running:
            with self.lock:
                if not self.is_running:
                    break
                    
                # Berechne verbleibende Zeit genau
                elapsed = time.time() - start_time
                self.time_left = max(0, self.duration - int(elapsed))
                
                # Sende Update an den Raum
                try:
                    self.socketio.emit('game_timer_update', 
                                    {'time_left': self.time_left}, 
                                    room=self.room_id)
                except Exception as e:
                    print(f"Fehler beim Senden des Timer-Updates: {e}")
                
                # Zeit abgelaufen?
                if self.time_left <= 0:
                    try:
                        # Runde automatisch beenden
                        print(f"DEBUG: Timer abgelaufen für Raum {self.room_id}, runde finish_round auf")
                        self.socketio.emit('game_time_out', room=self.room_id)
                        
                        # Runde direkt beenden (kein Event-Handler nötig)
                        if self.room_id in rooms:
                            room = rooms[self.room_id]
                            
                            # Für Spieler die nicht eingereicht haben, setze Beitrag auf 0
                            for pid in room.players:
                                if pid not in room.submitted_players and not players[pid].is_leader:
                                    players[pid].current_contribution = 0
                                    room.submitted_players.add(pid)
                            
                            # Berechne Ergebnisse
                            results = room.calculate_round_results()
                            room.status = "round_results"
                            room.submitted_players.clear()
                            
                            self.socketio.emit('round_finished', {
                                'results': results,
                                'current_round': room.current_round,
                                'room_id': self.room_id
                            }, room=self.room_id)
                            
                            # Leite Spieler automatisch zu round_results weiter
                            for pid in room.players:
                                if pid in players and not players[pid].is_leader:
                                    self.socketio.emit('redirect_to_results', {
                                        'room_id': self.room_id
                                    }, room=pid)
                            
                            print(f"Runde {room.current_round} automatisch beendet (Timer abgelaufen)")
                            
                    except Exception as e:
                        print(f"Fehler beim automatischen Rundenende: {e}")
                    self.is_running = False
                    break
            
            # Exakt 1 Sekunde warten
            next_update = start_time + (self.duration - self.time_left + 1)
            sleep_time = max(0, next_update - time.time())
            sleep(sleep_time)

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
                return self.duration
            elapsed = time.time() - self.start_time
            return max(0, self.duration - int(elapsed))
        
# Globale Variablen für Game-Timer
game_timers = {}
game_timer_lock = Lock()

def stop_game_timer(room_id):
    """Stoppt und entfernt Timer sicher"""
    with game_timer_lock:
        if room_id in game_timers:
            game_timers[room_id].stop()
            del game_timers[room_id]
            print(f"Game-Timer für Raum {room_id} gestoppt")

def get_or_create_game_timer(room_id, duration=60):
    """Erstellt oder gibt existierenden Timer zurück"""
    with game_timer_lock:
        # Stoppe vorhandenen Timer falls vorhanden
        if room_id in game_timers:
            game_timers[room_id].stop()
            del game_timers[room_id]
        
        # Erstelle neuen Timer
        timer = GameTimer(socketio, room_id, duration)
        game_timers[room_id] = timer
        timer.start()
        print(f"Neuer Game-Timer für Raum {room_id} gestartet (Dauer: {duration}s)")
        return timer

# Hilfsfunktion zum Überprüfen der Raum-Zugriffsberechtigung
def check_room_access(room_id, redirect_on_fail=True):
    if not is_production:
        print(f"DEBUG: check_room_access für Raum {room_id}")
        print(f"DEBUG: Session-Daten: {dict(session)}")
    
    if room_id not in rooms:
        print(f"DEBUG: Raum {room_id} nicht gefunden")
        if redirect_on_fail:
            return redirect(url_for('index'))
        return None, None
    
    room = rooms[room_id]
    player_id = session.get('player_id')
    
    print(f"DEBUG: Spieler-ID in Session: {player_id}")
    print(f"DEBUG: Spieler im Raum: {room.players}")
    
    if not player_id:
        print(f"DEBUG: Keine Spieler-ID in Session")
        if redirect_on_fail:
            return redirect(url_for('join_game'))
        return room, None
    
    if player_id not in players:
        print(f"DEBUG: Spieler {player_id} nicht in players dictionary")
        if redirect_on_fail:
            return redirect(url_for('join_game'))
        return room, None
    
    player = players[player_id]
    
    # Prüfe ob Spieler im Raum ist (außer für Leader)
    if not player.is_leader and player_id not in room.players:
        if room.status == "waiting":
            room.add_player(player_id)
            print(f"DEBUG: Spieler {player_id} wieder zu Raum {room_id} hinzugefügt")
        else:
            print(f"DEBUG: Spieler {player_id} nicht in Raum {room_id}")
            if redirect_on_fail:
                return redirect(url_for('join_game'))
            return room, None
    
    print(f"DEBUG: Zugriff gewährt für Spieler {player_id} in Raum {room_id}")
    return room, player

# Routen
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/create_game', methods=['GET', 'POST'])
def create_game():
    if request.method == 'POST':
        # Einstellungen aus dem Formular verarbeiten
        end_mode = request.form.get('end_mode', 'fixed_rounds')

        try:
            initial_coins = float(request.form.get('initial_coins', '10').replace(',', '.'))
            max_contribution = float(request.form.get('max_contribution', '10').replace(',', '.'))
        except ValueError:
            # Fallback zu Standardwerten bei Parse-Fehlern
            initial_coins = 10.0
            max_contribution = 10.0
        
        # Basis-Einstellungen
        settings = {
            'initial_coins': initial_coins,
            'max_contribution': max_contribution,
            'group_size': int(request.form.get('group_size', 4)),
            'multiplier': float(request.form.get('multiplier', 2)),
            'round_duration': int(request.form.get('round_duration', 60)),
            'fixed_groups': request.form.get('fixed_groups') == 'true',
            'end_mode': end_mode,
        }
        
        # Modus-spezifische Einstellungen
        if end_mode == 'fixed_rounds':
            max_rounds = int(request.form.get('max_rounds', 5))
            # Stelle sicher, dass max_rounds nicht über 20 liegt
            settings['max_rounds'] = min(max_rounds, 20)
        elif end_mode == 'probability':
            min_rounds = int(request.form.get('min_rounds', 1))
            max_rounds_probability = int(request.form.get('max_rounds_probability', 10))
            continue_probability = float(request.form.get('continue_probability', 0.5))
            
            # Stelle sicher, dass Rundenanzahlen nicht über 20 liegen
            settings['min_rounds'] = min(min_rounds, 20)
            settings['max_rounds_probability'] = min(max_rounds_probability, 20)
            settings['continue_probability'] = continue_probability
            
            # Korrigiere falls min_rounds > max_rounds_probability
            if settings['min_rounds'] > settings['max_rounds_probability']:
                settings['min_rounds'] = settings['max_rounds_probability']
        
        # Raum erstellen
        room_id = str(uuid.uuid4())[:8]
        leader_id = str(uuid.uuid4())
        
        # Leader als Spieler erstellen (für die Verwaltung)
        leader = Player(leader_id, "Leader", is_leader=True)
        leader.room_id = room_id
        players[leader_id] = leader
        
        # Raum erstellen
        room = GameRoom(room_id, leader_id, settings)
        rooms[room_id] = room
        
        # Leader-Session speichern
        session['player_id'] = leader_id
        session['room_id'] = room_id
        session['is_leader'] = True
        
        # TEST: 7 Test-Spieler mit Ready-Status erstellen
        test_names = ["Test-Spieler 1", "Test-Spieler 2", "Test-Spieler 3", "Test-Spieler 4", 
                     "Test-Spieler 5", "Test-Spieler 6", "Test-Spieler 7"]
        for name in test_names:
            player_id = str(uuid.uuid4())
            player = Player(player_id, name)
            player.room_id = room_id
            player.coins = room.settings['initial_coins']
            player.game_history['balances'].append(player.coins)
            player.ready = True  # Als bereit markieren
            
            players[player_id] = player
            room.add_player(player_id)
        
        return redirect(url_for('game_room', room_id=room_id))
    
    return render_template('create_game.html')

@app.route('/join_game')
def join_game():
    return render_template('join_game.html')

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
        print(f"DEBUG: Session gesetzt von URL-Parameter: player_id={player_id_from_param}, room_id={room_id}")
    
    result = check_room_access(room_id)
    if result and not isinstance(result, tuple):  # Redirect wurde zurückgegeben
        return result
    
    room, player = result
    is_leader = player.id == room.leader_id
    
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
                         is_leader=is_leader)

@app.route('/game/<room_id>')
def game(room_id):
    result = check_room_access(room_id)
    if result and not isinstance(result, tuple):
        return result
    
    room, player = result

    if player.is_leader:
        return redirect(url_for('leader_dashboard', room_id=room_id))
    
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
    result = check_room_access(room_id)
    if result and not isinstance(result, tuple):
        return result
    
    room, player = result
    is_leader = player.id == room.leader_id
    
    return render_template('round_results.html',
                         room=room,
                         player=player,
                         results=room.round_results or [],
                         is_leader=is_leader)

@app.route('/leader_dashboard/<room_id>')
def leader_dashboard(room_id):
    result = check_room_access(room_id)
    if result and not isinstance(result, tuple):
        return result
    
    room, player = result
    is_leader = player.id == room.leader_id
    
    if not is_leader:
        return redirect(url_for('game_room', room_id=room_id))
    
    return render_template('leader_dashboard.html',
                         room=room,
                         player=player,
                         is_leader=is_leader)

@app.route('/evaluation/<room_id>')
def evaluation(room_id):
    result = check_room_access(room_id)
    if result and not isinstance(result, tuple):
        return result
    
    room, player = result
    initial_coins = room.settings['initial_coins']
    
    return render_template('evaluation.html',
                         room=room,
                         player=player,
                         initial_coins=initial_coins,
                         is_leader=player.id == room.leader_id)

# API Routes
@app.route('/api/rooms')
def api_rooms():
    available_rooms = []
    for room_id, room in rooms.items():
        if room.status == "waiting":
            # Nur normale Spieler zählen (keine Leader)
            normal_players = [pid for pid in room.players if pid in players and not players[pid].is_leader]
            
            available_rooms.append({
                'id': room_id,
                'player_count': len(normal_players),
                'current_round': room.current_round,
                'settings': room.settings
            })
    return jsonify(available_rooms)

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
    return jsonify({
        'status': room.status,
        'current_round': room.current_round
    })

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

# WebSocket Events
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    player_id = session.get('player_id')
    if player_id and player_id in players:
        player = players[player_id]
        room_id = player.room_id
        
        if room_id and room_id in rooms:
            room = rooms[room_id]
            
            # Leader wird nicht aus Raum entfernt
            if not player.is_leader:
                # Spieler bleibt im Raum für Reconnect-Möglichkeit
                print(f"DEBUG: Spieler {player.name} disconnected, bleibt im Raum für Reconnect")
                
                # Optional: Benachrichtige andere Spieler über Disconnect
                emit('player_disconnected', {
                    'player_id': player_id, 
                    'player_name': player.name
                }, room=room_id)
                
            # Raum nur löschen wenn keine Spieler mehr (außer Leader)
            active_players = [pid for pid in room.players if not players[pid].is_leader]
            if len(active_players) == 0:
                # Lösche Raum nach 5 Minuten Inaktivität
                pass  # Raum bleibt für Leader erhalten

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    player_name = data.get('player_name')
    
    print(f"DEBUG: join_room aufgerufen - Raum: {room_id}, Spieler: {player_name}")
    
    if not room_id or not player_name:
        emit('error', {'message': 'Ungültige Daten'})
        return
    
    if room_id not in rooms:
        emit('error', {'message': 'Raum nicht gefunden'})
        return
    
    room = rooms[room_id]
    
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
    
    players[player_id] = player
    
    # Spieler zum Raum hinzufügen
    if room.add_player(player_id):
        join_room(room_id)
        
        with game_timer_lock:
            timer = game_timers.get(room_id)
            if timer and timer.is_running:
                time_left = timer.get_time_left()
                emit('game_timer_update', {'time_left': time_left}, room=request.sid)
        
        print(f"DEBUG: Spieler {player_name} zu Raum {room_id} hinzugefügt. Aktuelle Spieler: {room.players}")
        
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
        
        print(f"DEBUG: room_joined Event gesendet für Raum {room_id} mit player_id {player_id}")

@socketio.on('join_room_reconnect')
def handle_join_room_reconnect(data):
    """Handler für Spieler, die bereits im Raum sind und die Seite neu laden"""
    room_id = data.get('room_id')
    player_id = data.get('player_id')
    
    print(f"DEBUG: join_room_reconnect - Raum: {room_id}, Spieler: {player_id}")
    
    if not room_id or not player_id:
        return
    
    if room_id in rooms and player_id in players:
        room = rooms[room_id]
        player = players[player_id]
        
        # Stelle sicher, dass Spieler im Raum ist
        if player_id not in room.players and not player.is_leader:
            room.add_player(player_id)
        
        join_room(room_id)
        
        with game_timer_lock:
            timer = game_timers.get(room_id)
            if timer and timer.is_running:
                time_left = timer.get_time_left()
                emit('game_timer_update', {'time_left': time_left}, room=request.sid)
        
        print(f"DEBUG: Spieler {player_id} erneut Raum {room_id} beigetreten")

@socketio.on('player_ready')
def handle_player_ready():
    """Handler für Ready-Status ohne data Parameter"""
    player_id = session.get('player_id')
    if not player_id or player_id not in players:
        print(f"DEBUG: player_ready - Spieler nicht gefunden: {player_id}")
        return
    
    player = players[player_id]
    room_id = player.room_id
    
    if room_id and room_id in rooms and not player.is_leader:
        player.ready = not player.ready  # Toggle ready status
        
        room = rooms[room_id]
        ready_count = sum(1 for pid in room.players if players[pid].ready and not players[pid].is_leader)
        
        print(f"DEBUG: player_ready - Spieler {player.name} ready: {player.ready}, Count: {ready_count}")
        
        emit('player_ready_changed', {
            'player_id': player_id,
            'ready': player.ready,
            'ready_count': ready_count
        }, room=room_id)
    else:
        print(f"DEBUG: player_ready - Ungültiger Zustand: room_id={room_id}, is_leader={player.is_leader}")

@socketio.on('start_game')
def handle_start_game():
    player_id = session.get('player_id')
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
            get_or_create_game_timer(room_id, duration)
            print(f"DEBUG: Spiel gestartet - Timer für Raum {room_id} mit {duration}s gestartet")
            
            # Setze alle Spieler auf nicht bereit für nächste Runde
            for pid in room.players:
                players[pid].ready = False
            
            emit('game_started', {
                'room_id': room_id,
                'current_round': room.current_round
            }, room=room_id)
        else:
            # Detaillierte Fehlermeldung
            group_size = room.settings.get('group_size', 4)
            player_count = len(room.players)
            ready_count = sum(1 for pid in room.players if players[pid].ready)
            
            if player_count < group_size:
                error_msg = f"Zu wenige Spieler. Benötigt mindestens {group_size}, haben {player_count}"
            elif player_count % group_size != 0:
                error_msg = f"Spieleranzahl muss durch {group_size} teilbar sein. Aktuell: {player_count}"
            else:
                error_msg = f"Nicht alle Spieler sind bereit. Bereit: {ready_count}/{player_count}"
            
            emit('error', {'message': error_msg})

@socketio.on('submit_contribution')
def handle_submit_contribution(data):
    player_id = session.get('player_id')
    if not player_id or player_id not in players:
        return
    
    contribution = int(data.get('contribution', 0))
    player = players[player_id]
    room_id = player.room_id
    
    if room_id and room_id in rooms and not player.is_leader:
        room = rooms[room_id]
        
        # Prüfe ob der Spieler genug Coins hat
        if contribution > player.coins:
            emit('error', {'message': 'Nicht genügend Coins verfügbar'})
            return
        
        # Beitrag speichern
        player.current_contribution = contribution
        room.submitted_players.add(player_id)
        
        # Benachrichtige andere Spieler
        submitted_count = len(room.submitted_players)
        total_players = len([pid for pid in room.players if not players[pid].is_leader])
        
        emit('contribution_submitted', {
            'submitted_count': submitted_count,
            'total_players': total_players
        }, room=room_id)
        
        # Prüfe ob alle Spieler eingezahlt haben
        if submitted_count == total_players:
            stop_game_timer(room_id)
            finish_round(room_id)

def finish_round(room_id):
    """Beendet die aktuelle Runde und berechnet Ergebnisse"""
    if room_id not in rooms:
        return
    
    room = rooms[room_id]
    
    # Für Spieler die nicht eingereicht haben, setze Beitrag auf 0
    for pid in room.players:
        if pid not in room.submitted_players and not players[pid].is_leader:
            players[pid].current_contribution = 0
            room.submitted_players.add(pid)
    
    # Berechne Ergebnisse
    results = room.calculate_round_results()
    room.status = "round_results"
    room.submitted_players.clear()
    
    # Sende Event an alle im Raum
    socketio.emit('round_finished', {
        'results': results,
        'current_round': room.current_round,
        'room_id': room_id
    }, room=room_id)
    
    # Leite Spieler automatisch zu round_results weiter
    for player_id in room.players:
        if player_id in players and not players[player_id].is_leader:
            # Für normale Spieler: Weiterleitung zu round_results
            socketio.emit('redirect_to_results', {
                'room_id': room_id
            }, room=player_id)
    
    print(f"Runde {room.current_round} abgeschlossen, Spieler werden zu Ergebnissen weitergeleitet")

@socketio.on('next_round')
def handle_next_round():
    player_id = session.get('player_id')
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
            
            emit('next_round_started', {
                'current_round': room.current_round,
                'room_id': room_id
            }, room=room_id)
            
            # Leite Spieler zurück zum Spiel
            for pid in room.players:
                if pid in players and not players[pid].is_leader:
                    socketio.emit('redirect_to_game', {
                        'room_id': room_id
                    }, room=pid)
                    
        else:
            # Spiel beenden
            room.status = "finished"
            emit('game_finished', {
                'room_id': room_id
            }, room=room_id)

@socketio.on('game_time_out')
def handle_game_time_out(data=None):
    """Wird aufgerufen wenn der Timer abläuft - mit optionalem data Parameter"""
    player_id = session.get('player_id')
    if not player_id or player_id not in players:
        return
    
    player = players[player_id]
    room_id = player.room_id
    
    if room_id and room_id in rooms:
        print(f"DEBUG: Timer abgelaufen für Raum {room_id}, beende Runde")
        finish_round(room_id)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    
    if is_production:
        # In Production mit gunicorn kompatibel
        socketio.run(app, host='0.0.0.0', port=port, debug=False)
    else:
        # In Development mit Debug-Modus
        socketio.run(app, host='0.0.0.0', port=port, debug=True)