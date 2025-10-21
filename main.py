from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
import random
import uuid
from collections import defaultdict
from threading import Lock, Thread, Timer
import os
import time

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key')

socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    logger=True,
    engineio_logger=True
)

# In-Memory Speicher
rooms = {}
players = {}
game_state = {}
lock = Lock()
round_timers = {}

class GameManager:
    @staticmethod
    def create_room(leader_id, settings):
        room_id = str(uuid.uuid4())[:8]
        with lock:
            rooms[room_id] = {
                'id': room_id,
                'leader': leader_id,
                'players': [],
                'settings': settings,
                'status': 'waiting',
                'ready_players': set(),
                'min_players': settings.get('group_size', 4)
            }
        print(f"✅ Raum {room_id} erstellt durch {leader_id}")
        print(f"   Settings: {settings}")
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
    def start_game(room_id):
        with lock:
            if room_id not in rooms:
                return False
                
            room = rooms[room_id]
            ready_count = len(room['ready_players'])
            total_players = len(room['players'])
            group_size = room['settings'].get('group_size', 4)
            
            print(f"🎮 Startversuch Raum {room_id}: {ready_count}/{total_players} bereit")
            
            if (room['status'] == 'waiting' and 
                ready_count >= group_size and 
                ready_count % group_size == 0 and 
                total_players % group_size == 0):
                
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
                print(f"   Gruppen: {len(room['groups'])} Gruppen à {group_size}")
                
                # Timer starten
                GameManager._start_round_timer(room_id)
                return True
        return False
    
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
        """Server-seitiger Timer für Runden"""
        if room_id not in rooms:
            return
            
        room = rooms[room_id]
        round_duration = room['settings'].get('round_duration', 60)
        
        def timer_callback():
            with lock:
                if room_id in rooms and room_id in game_state:
                    room = rooms[room_id]
                    game_data = game_state[room_id]
                    
                    # Für Spieler die nicht eingezahlt haben: 0 setzen
                    for player in room['players']:
                        if player['id'] not in game_data['contributions']:
                            game_data['contributions'][player['id']] = 0
                    
                    # Runde beenden
                    print(f"⏰ Timer abgelaufen für Raum {room_id}")
                    socketio.emit('round_time_up', room=room_id)
                    
                    # Ergebnisse berechnen
                    Thread(target=GameManager._process_round_end, args=(room_id,)).start()
        
        timer = Timer(round_duration, timer_callback)
        timer.start()
        round_timers[room_id] = timer
        
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
                
                # Validierung
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
                    # Timer abbrechen
                    if room_id in round_timers:
                        round_timers[room_id].cancel()
                    
                    print(f"✅ Alle Spieler haben eingezahlt in Raum {room_id}")
                    Thread(target=GameManager._process_round_end, args=(room_id,)).start()
                
                return True
        return False
    
    @staticmethod
    def _process_round_end(room_id):
        """Verarbeitet das Ende einer Runde"""
        time.sleep(0.5)  # Kurze Verzögerung für bessere UX
        
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
                
                # Berechne neues Guthaben
                remaining = player['coins'] - contribution
                new_balance = remaining + payout_per_player
                profit = new_balance - player['coins']
                
                # Aktualisiere Spieler
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
        """Startet die nächste Runde oder beendet das Spiel"""
        with lock:
            if room_id not in rooms:
                return False
                
            room = rooms[room_id]
            
            if GameManager._should_continue_game(room):
                room['current_round'] += 1
                
                # Neue Gruppen bei dynamischer Gruppierung
                if not room['settings'].get('fixed_groups', True):
                    room['groups'] = GameManager._create_groups(
                        room['players'], 
                        room['settings']
                    )
                    print(f"🔄 Neue Gruppen erstellt für Runde {room['current_round']}")
                
                game_state[room_id]['round'] = room['current_round']
                game_state[room_id]['round_start_time'] = time.time()
                
                print(f"▶️ Runde {room['current_round']} startet in Raum {room_id}")
                
                # Neuer Timer
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
        else:  # probability_range
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
                    'leader': players[room['leader']]['name'] if room['leader'] in players else 'Unbekannt'
                })
        return available
    
    @staticmethod
    def get_player_group(room_id, player_id):
        """Gibt die Gruppe eines Spielers zurück"""
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
    session['player_name'] = f"Spieler_{random.randint(1000, 9999)}"
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
    
    room_id = GameManager.create_room(session['player_id'], settings)
    GameManager.join_room(room_id, session['player_id'], session['player_name'])
    
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
    player_data = players.get(session['player_id'])
    
    return render_template('game_room.html', 
                         room=room, 
                         player=player_data,
                         is_leader=session.get('is_leader', False))

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
    
    # Berechne Statistiken
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

# SocketIO Events
@socketio.on('connect')
def handle_connect():
    print(f"🔌 Client verbunden: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"🔌 Client getrennt: {request.sid}")

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    if room_id and player_id:
        join_room(room_id)
        print(f"👥 {player_id} → Socket-Room {room_id}")
        
        if player_id in players:
            emit('player_joined', {
                'player_id': player_id,
                'player_name': players[player_id]['name']
            }, room=room_id)

@socketio.on('set_ready')
def handle_set_ready(data):
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    
    print(f"✋ Ready-Event: room={room_id}, player={player_id}")
    
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
    
    emit('player_ready', {
        'player_id': player_id,
        'player_name': player['name']
    }, room=room_id, include_self=True)
    
    # Status-Update
    room = rooms[room_id]
    ready_count = len(room['ready_players'])
    total_players = len(room['players'])
    group_size = room['settings'].get('group_size', 4)
    
    can_start = (ready_count >= group_size and 
                ready_count % group_size == 0 and 
                total_players % group_size == 0)
    
    emit('start_button_update', {
        'can_start': can_start,
        'ready_count': ready_count,
        'total_players': total_players
    }, room=room_id)

@socketio.on('start_game')
def handle_start_game():
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    
    print(f"🎮 Start-Event von {player_id} für Raum {room_id}")
    
    if not room_id or room_id not in rooms:
        emit('start_failed', {'message': 'Raum nicht gefunden'})
        return
        
    room = rooms[room_id]
    
    if player_id != room['leader']:
        emit('start_failed', {'message': 'Nur der Leader kann starten'})
        return
    
    if GameManager.start_game(room_id):
        emit('game_started', {'room_id': room_id}, room=room_id)
    else:
        emit('start_failed', {'message': 'Startbedingungen nicht erfüllt'})

@socketio.on('submit_contribution')
def handle_submit_contribution(data):
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    amount = int(data.get('amount', 0))
    
    if GameManager.submit_contribution(room_id, player_id, amount):
        emit('contribution_submitted', {
            'player_id': player_id,
            'player_name': players[player_id]['name']
        }, room=room_id)
    else:
        emit('contribution_failed', {
            'message': 'Ungültiger Beitrag'
        })

@socketio.on('continue_to_next_round')
def handle_continue_next_round():
    room_id = session.get('room_id')
    
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