from gevent import monkey, spawn, sleep
monkey.patch_all()

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
import random
import uuid
from collections import defaultdict
from threading import Lock
import os
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key')

# Verbesserte Socket.IO Konfiguration
socketio = SocketIO(
    app, 
    async_mode='gevent',
    manage_session=True,  # WICHTIG: Session-Management aktivieren
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e8,
    allow_upgrades=True,
    transports=['websocket', 'polling'],  # WebSocket zuerst
    cors_allowed_origins="*"
)

# In-Memory Speicher
rooms = {}
players = {}
game_state = {}
lock = Lock()
round_timers = {}
sid_to_player = {}

class RoundTimer:
    """Verbesserte Timer-Klasse basierend auf der alten Implementierung"""
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
        """Timer-Loop mit genauer Zeitberechnung"""
        start_time = time.time()
        while self.is_running:
            with self.lock:
                if not self.is_running:
                    break
                    
                elapsed = time.time() - start_time
                self.time_left = max(0, self.duration - int(elapsed))
                
                try:
                    self.socketio.emit('time_update', 
                                    {'time_left': self.time_left}, 
                                    room=self.room_id)
                except Exception as e:
                    print(f"Fehler beim Senden des Timer-Updates: {e}")
                
                if self.time_left <= 0:
                    try:
                        self.socketio.emit('round_time_up', room=self.room_id)
                    except Exception as e:
                        print(f"Fehler beim Timeout: {e}")
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
                return 0
            elapsed = time.time() - self.start_time
            return max(0, self.duration - int(elapsed))

class GameManager:
    @staticmethod
    def create_room(leader_id, leader_name, settings):
        room_id = str(uuid.uuid4())[:8]
        with lock:
            rooms[room_id] = {
                'id': room_id,
                'leader': leader_id,
                'players': [],
                'settings': settings,
                'status': 'waiting',
                'ready_players': set(),
                'min_players': settings.get('group_size', 4),
                'leader_name': leader_name
            }
            
        print(f"✅ Raum {room_id} erstellt durch {leader_name} ({leader_id})")
        return room_id
    
    @staticmethod
    def join_room(room_id, player_id, player_name):
        with lock:
            if room_id in rooms:
                room = rooms[room_id]
                initial_coins = room['settings'].get('initial_coins', 10)
                
                player_data = {
                    'id': player_id,
                    'name': player_name,
                    'ready': False,
                    'coins': initial_coins,
                    'balance_history': [initial_coins],
                    'contribution_history': []
                }
                players[player_id] = player_data
                room['players'].append(player_data)
                print(f"✅ {player_name} ({player_id}) → Raum {room_id}")
                return True
        print(f"❌ Raum {room_id} nicht gefunden")
        return False
    
    @staticmethod
    def can_start_game(room_id):
        """Prüft ob Spiel gestartet werden kann"""
        if room_id not in rooms:
            return False, "Raum nicht gefunden"
            
        room = rooms[room_id]
        ready_count = len(room['ready_players'])
        total_players = len(room['players'])
        group_size = room['settings'].get('group_size', 4)
        
        if room['status'] != 'waiting':
            return False, "Spiel läuft bereits"
        
        if ready_count < group_size:
            return False, f"Mindestens {group_size} Spieler müssen bereit sein"
        
        if ready_count % group_size != 0:
            return False, f"Anzahl bereiter Spieler muss durch {group_size} teilbar sein"
        
        if total_players % group_size != 0:
            return False, f"Gesamtanzahl Spieler muss durch {group_size} teilbar sein"
        
        return True, "Spiel kann gestartet werden"
    
    @staticmethod
    def start_game(room_id):
        with lock:
            if room_id not in rooms:
                return False
                
            room = rooms[room_id]
            
            # ✅ Verbesserte Validierung
            can_start, message = GameManager.can_start_game(room_id)
            if not can_start:
                print(f"❌ Spielstart fehlgeschlagen: {message}")
                return False
            
            # ✅ Restliche Start-Logik
            room['status'] = 'playing'
            room['current_round'] = 1
            room['groups'] = GameManager._create_groups(room['players'], room['settings'])
            
            game_state[room_id] = {
                'round': 1,
                'contributions': {},
                'group_results': {},
                'round_start_time': time.time()
            }
            
            print(f"✅ Spiel gestartet in Raum {room_id}!")
            GameManager._start_round_timer(room_id)
            return True
    
    @staticmethod
    def _create_groups(players_list, settings):
        player_ids = [p['id'] for p in players_list]
        random.shuffle(player_ids)
        
        group_size = settings.get('group_size', 4)
        groups = []
        
        for i in range(0, len(player_ids), group_size):
            group = player_ids[i:i+group_size]
            if len(group) == group_size:
                groups.append(group)
        
        return groups
    
    @staticmethod
    def _start_round_timer(room_id):
        """Verbesserter Timer mit RoundTimer Klasse"""
        if room_id not in rooms:
            return
            
        room = rooms[room_id]
        round_duration = room['settings'].get('round_duration', 60)
        
        # Alten Timer stoppen falls vorhanden
        if room_id in round_timers:
            round_timers[room_id].stop()
        
        # Neuen Timer erstellen und starten
        timer = RoundTimer(socketio, room_id, round_duration)
        round_timers[room_id] = timer
        timer.start()
        
        print(f"⏰ Timer gestartet für Raum {room_id}: {round_duration}s")
    
    @staticmethod
    def submit_contribution(room_id, player_id, amount):
        with lock:
            if room_id in game_state and room_id in rooms:
                game_data = game_state[room_id]
                room = rooms[room_id]
                player = players.get(player_id)
                
                if not player:
                    return False
                
                max_contribution = min(
                    player['coins'],
                    room['settings'].get('max_contribution', 10)
                )
                
                if amount < 0 or amount > max_contribution:
                    print(f"❌ Ungültiger Beitrag: {amount} (max: {max_contribution})")
                    return False
                
                game_data['contributions'][player_id] = amount
                player['contribution_history'].append(amount)
                
                print(f"💰 {player['name']} zahlt {amount} ein")
                
                # Prüfe ob alle eingezahlt haben
                if len(game_data['contributions']) == len(room['players']):
                    # Timer stoppen
                    if room_id in round_timers:
                        round_timers[room_id].stop()
                    
                    print(f"✅ Alle Spieler haben eingezahlt in Raum {room_id}")
                    # Sofort Ergebnisse berechnen
                    spawn(GameManager._process_round_end, room_id)
                
                return True
        return False
    
    @staticmethod
    def _process_round_end(room_id):
        """Verarbeitet das Ende einer Runde"""
        sleep(0.5)  # Kurze Verzögerung für bessere UX
        
        with lock:
            if room_id not in rooms or room_id not in game_state:
                return
                
            results = GameManager.calculate_round_results(room_id)
            
        # Sende Ergebnisse an alle Clients
        socketio.emit('show_round_results', {
            'results': results,
            'room_id': room_id
        }, room=room_id)
    
    @staticmethod
    def calculate_round_results(room_id):
        room = rooms[room_id]
        game_data = game_state[room_id]
        contributions = game_data['contributions']
        
        multiplier = room['settings'].get('multiplier', 2)
        results = []
        
        for group_idx, group in enumerate(room['groups']):
            total_contribution = sum(contributions.get(pid, 0) for pid in group)
            total_pool = total_contribution * multiplier
            payout_per_player = total_pool / len(group)
            
            group_result = {
                'group_number': group_idx + 1,
                'total_contribution': total_contribution,
                'total_pool': total_pool,
                'payout_per_player': payout_per_player,
                'players': []
            }
            
            for player_id in group:
                player = players[player_id]
                contribution = contributions.get(player_id, 0)
                
                remaining = player['coins'] - contribution
                new_balance = remaining + payout_per_player
                profit = new_balance - player['coins']
                
                player['coins'] = new_balance
                player['balance_history'].append(new_balance)
                
                group_result['players'].append({
                    'id': player_id,
                    'name': player['name'],
                    'contribution': contribution,
                    'payout': payout_per_player,
                    'new_balance': new_balance,
                    'profit': profit
                })
            
            results.append(group_result)
        
        game_data['group_results'] = results
        game_data['contributions'] = {}
        
        print(f"📊 Rundenergebnisse berechnet für Raum {room_id}")
        return results
    
    @staticmethod
    def start_next_round(room_id):
        with lock:
            if room_id not in rooms:
                return False
                
            room = rooms[room_id]
            
            if GameManager._should_continue_game(room):
                room['current_round'] += 1
                
                if not room['settings'].get('fixed_groups', True):
                    room['groups'] = GameManager._create_groups(
                        room['players'], 
                        room['settings']
                    )
                    print(f"🔄 Neue Gruppen erstellt für Runde {room['current_round']}")
                
                game_state[room_id]['round'] = room['current_round']
                game_state[room_id]['round_start_time'] = time.time()
                
                print(f"▶️ Runde {room['current_round']} startet in Raum {room_id}")
                
                GameManager._start_round_timer(room_id)
                return True
            else:
                room['status'] = 'finished'
                print(f"🏁 Spiel beendet in Raum {room_id}")
                return False
    
    @staticmethod
    def _should_continue_game(room):
        settings = room['settings']
        current_round = room['current_round']
        
        end_mode = settings.get('end_mode', 'fixed_rounds')
        
        if end_mode == 'fixed_rounds':
            max_rounds = settings.get('max_rounds', 5)
            return current_round < max_rounds
        elif end_mode == 'probability':
            continue_prob = settings.get('continue_probability', 0.5)
            return random.random() < continue_prob
        else:
            min_prob = settings.get('min_probability', 0.3)
            max_prob = settings.get('max_probability', 0.8)
            prob = random.uniform(min_prob, max_prob)
            return random.random() < prob
    
    @staticmethod
    def get_available_rooms():
        available = []
        for room_id, room in rooms.items():
            if room['status'] == 'waiting':
                available.append({
                    'id': room_id,
                    'players': len(room['players']),
                    'leader': room.get('leader_name', 'Unbekannt')
                })
        return available
    
    @staticmethod
    def get_player_group(room_id, player_id):
        if room_id not in rooms:
            return None
            
        room = rooms[room_id]
        for idx, group in enumerate(room['groups']):
            if player_id in group:
                return {
                    'group_number': idx + 1,
                    'members': [players[pid]['name'] for pid in group if pid in players]
                }
        return None

# Routes
@app.route('/')
def index():
    session.clear()
    return render_template('index.html')

@app.route('/create_game')
def create_game():
    session['player_id'] = str(uuid.uuid4())
    session['player_name'] = f"Leader_{random.randint(1000, 9999)}"  # Leader-Name
    session['is_leader'] = True
    return render_template('create_game.html')

@app.route('/join_game')
def join_game():
    session['player_id'] = str(uuid.uuid4())
    session['player_name'] = f"Spieler_{random.randint(1000, 9999)}"
    session['is_leader'] = False
    return render_template('join_game.html')

@app.route('/create_room', methods=['POST'])
def create_room_route():
    data = request.json
    
    settings = {
        'initial_coins': int(data.get('initial_coins', 10)),
        'max_contribution': int(data.get('max_contribution', 10)),
        'group_size': int(data.get('group_size', 4)),
        'multiplier': float(data.get('multiplier', 2)),
        'round_duration': int(data.get('round_duration', 60)),
        'fixed_groups': data.get('fixed_groups') == 'true',
        'end_mode': data.get('end_mode', 'fixed_rounds'),
        'max_rounds': int(data.get('max_rounds', 5)),
        'continue_probability': float(data.get('continue_probability', 0.5)),
        'min_probability': float(data.get('min_probability', 0.3)),
        'max_probability': float(data.get('max_probability', 0.8))
    }
    
    room_id = GameManager.create_room(session['player_id'], session['player_name'], settings)
    
    session['room_id'] = room_id
    return jsonify({'room_id': room_id})

@app.route('/join_room/<room_id>')
def join_room_route(room_id):
    if GameManager.join_room(room_id, session['player_id'], session['player_name']):
        session['room_id'] = room_id
        return redirect(url_for('game_room'))
    else:
        return "Raum nicht gefunden", 404

@app.route('/game_room')
def game_room():
    room_id = session.get('room_id')
    if not room_id or room_id not in rooms:
        return redirect(url_for('index'))
    
    room = rooms[room_id]
    is_leader = session.get('is_leader', False)
    
    # Für Leader erstellen wir ein Dummy-Player-Objekt
    if is_leader:
        player_data = {
            'id': session['player_id'],
            'name': session['player_name'],
            'ready': True,  # Leader ist immer bereit
            'coins': 0,     # Leader hat keine Coins
            'balance_history': [],
            'contribution_history': [],
            'is_leader': True  # Zusätzliches Flag für Moderator
        }
    else:
        player_data = players.get(session['player_id'])
        if player_data:
            player_data['is_leader'] = False
    
    return render_template('game_room.html', 
                         room=room, 
                         player=player_data,
                         is_leader=is_leader)

@app.route('/game')
def game():
    room_id = session.get('room_id')
    if not room_id or room_id not in rooms:
        return redirect(url_for('index'))
    
    room = rooms[room_id]
    if room['status'] != 'playing':
        return redirect(url_for('game_room'))
    
    player_data = players.get(session['player_id'])
    player_group = GameManager.get_player_group(room_id, session['player_id'])
    
    return render_template('game.html', 
                         room=room, 
                         player=player_data,
                         player_group=player_group,
                         current_round=room.get('current_round', 1))

@app.route('/round_results')
def round_results():
    room_id = session.get('room_id')
    if not room_id or room_id not in rooms or room_id not in game_state:
        return redirect(url_for('index'))
    
    room = rooms[room_id]
    player_data = players.get(session['player_id'])
    results = game_state[room_id].get('group_results', [])
    
    return render_template('round_results.html',
                         room=room,
                         player=player_data,
                         results=results,
                         is_leader=session.get('is_leader', False))

@app.route('/evaluation')
def evaluation():
    room_id = session.get('room_id')
    if not room_id or room_id not in rooms:
        return redirect(url_for('index'))
    
    room = rooms[room_id]
    player_data = players.get(session['player_id'])
    
    initial_coins = room['settings'].get('initial_coins', 10)
    total_rounds = room.get('current_round', 1)
    
    return render_template('evaluation.html', 
                         room=room, 
                         player=player_data,
                         initial_coins=initial_coins,
                         total_rounds=total_rounds)

@app.route('/api/available_rooms')
def available_rooms():
    return jsonify(GameManager.get_available_rooms())

# Verbesserte SocketIO Events
@socketio.on('connect')
def handle_connect():
    print(f"🔌 Client verbunden: {request.sid}")
    player_id = session.get('player_id')
    room_id = session.get('room_id')  # WICHTIG: Raum aus Session lesen
    
    if player_id:
        sid_to_player[request.sid] = player_id
        print(f"✅ Session player_id {player_id} für SID {request.sid}")
    
    # 🔥 AUTOMATISCH DEM RAUM BEITRETEN BEI RECONNECT
    if room_id and room_id in rooms:
        join_room(room_id)
        print(f"✅ Automatisch Raum {room_id} beigetreten nach Reconnect")
    
    emit('connection_success', {
        'message': 'Verbunden', 
        'sid': request.sid,
        'player_id': player_id,
        'room_id': room_id,  # Raum-ID mitsenden
        'timestamp': time.time()
    })

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    player_id = sid_to_player.pop(sid, None)
    print(f"🔌 Client getrennt: {sid} (player_id={player_id})")

    if not player_id:
        return

    # Finde und bereinige den Raum des Spielers
    affected_room = None
    for room_id, room in rooms.items():
        for p in list(room['players']):
            if p['id'] == player_id:
                room['players'].remove(p)
                affected_room = room_id
                break
        if affected_room:
            room['ready_players'].discard(player_id)
            break

    players.pop(player_id, None)

    if affected_room:
        room = rooms[affected_room]
        emit('room_update', {
            'players': [{
                'id': p['id'],
                'name': p['name'],
                'ready': p['ready']
            } for p in room['players']],
            'ready_count': len(room['ready_players']),
            'total_players': len(room['players']),
            'group_size': room['settings'].get('group_size', 4)
        }, room=affected_room)

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    # WICHTIG: player_id aus Session nehmen, nicht aus data
    player_id = session.get('player_id')
    
    print(f"👥 join_room Event: room_id={room_id}, player_id={player_id}, sid={request.sid}")

    if not player_id:
        emit('error', {'message': 'Keine player_id in Session'})
        return

    if not room_id or room_id not in rooms:
        print(f"❌ Raum {room_id} nicht gefunden")
        emit('error', {'message': 'Raum nicht gefunden'})
        return

    # Dem Socket-Room beitreten
    join_room(room_id)
    sid_to_player[request.sid] = player_id
    print(f"✅ Socket {request.sid} → Room {room_id} (player {player_id})")

    # Bestätigung zurücksenden
    emit('room_joined', {
        'room_id': room_id,
        'message': 'Erfolgreich beigetreten',
        'player_id': player_id
    })

    # Raum-Update an alle senden
    room = rooms[room_id]
    emit('room_update', {
        'players': [{
            'id': p['id'],
            'name': p['name'],
            'ready': p['ready']
        } for p in room['players']],
        'ready_count': len(room['ready_players']),
        'total_players': len(room['players']),
        'group_size': room['settings'].get('group_size', 4)
    }, room=room_id)

@socketio.on('set_ready')
def handle_set_ready(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    print(f"✋ Ready-Event: room={room_id}, player={player_id}, sid={request.sid}")
    
    if not room_id or room_id not in rooms:
        print(f"❌ Raum {room_id} nicht gefunden")
        emit('error', {'message': 'Raum nicht gefunden'})
        return
        
    if not player_id or player_id not in players:
        print(f"❌ Spieler {player_id} nicht gefunden")
        emit('error', {'message': 'Spieler nicht gefunden'})
        return
    
    with lock:
        room = rooms[room_id]
        player = players[player_id]
        
        room['ready_players'].add(player_id)
        player['ready'] = True
    
    print(f"✅ {player['name']} ist bereit")
    
    # Aktualisierten Raumstatus an alle senden
    emit('player_ready', {
        'player_id': player_id,
        'player_name': player['name'],
        'ready_count': len(room['ready_players']),
        'total_players': len(room['players'])
    }, room=room_id)
    
    # Start-Button Status prüfen
    ready_count = len(room['ready_players'])
    total_players = len(room['players'])
    group_size = room['settings'].get('group_size', 4)
    
    can_start = (ready_count >= group_size and 
                ready_count % group_size == 0 and 
                total_players % group_size == 0)
    
    emit('start_button_update', {
        'can_start': can_start,
        'ready_count': ready_count,
        'total_players': total_players,
        'group_size': room['settings'].get('group_size', 4)
    }, room=room_id)

@socketio.on('start_game')
def handle_start_game(data):
    room_id = data.get('room_id')
    player_id = data.get('player_id')  # WICHTIG: player_id aus data, nicht session
    
    print(f"🎮 Start-Event von {player_id} für Raum {room_id}")
    
    if not room_id or room_id not in rooms:
        emit('start_failed', {'message': 'Raum nicht gefunden'})
        return
        
    room = rooms[room_id]
    
    # ✅ Bessere Leader-Überprüfung
    if player_id != room['leader']:
        emit('start_failed', {'message': 'Nur der Leader kann das Spiel starten'})
        return
    
    # ✅ Validierung vor Start
    can_start, message = GameManager.can_start_game(room_id)
    if not can_start:
        emit('start_failed', {'message': message})
        return
    
    if GameManager.start_game(room_id):
        emit('game_started', {'room_id': room_id}, room=room_id)
    else:
        emit('start_failed', {'message': 'Spiel konnte nicht gestartet werden'})

@socketio.on('submit_contribution')
def handle_submit_contribution(data):
    room_id = data.get('room_id')
    player_id = data.get('player_id')
    amount = int(data.get('amount', 0))
    
    if GameManager.submit_contribution(room_id, player_id, amount):
        emit('contribution_submitted', {
            'player_id': player_id,
            'player_name': players[player_id]['name'],
            'amount': amount
        }, room=room_id)
    else:
        emit('contribution_failed', {
            'message': 'Ungültiger Beitrag'
        })

@socketio.on('continue_to_next_round')
def handle_continue_next_round(data):
    room_id = data.get('room_id')
    
    if GameManager.start_next_round(room_id):
        emit('next_round_started', {
            'round': rooms[room_id]['current_round']
        }, room=room_id)
    else:
        emit('game_finished', room=room_id)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_ENV') != 'production'
    
    print("=" * 50)
    if debug_mode:
        print("🔧 DEVELOPMENT MODE")
    else:
        print("🚀 PRODUCTION MODE")
    print("=" * 50)
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        use_reloader=False
    )