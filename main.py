from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
import random
import uuid
from collections import defaultdict
from threading import Lock
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key')

socketio = SocketIO(
    app, 
    cors_allowed_origins="*",
    async_mode='threading',
    logger=True,
    engineio_logger=True
)

# In-Memory Speicher für Spiele und Spieler
rooms = {}
players = {}
game_state = {}
lock = Lock()

class GameManager:
    @staticmethod
    def create_room(leader_id, settings):
        room_id = str(uuid.uuid4())[:8]
        with lock:
            rooms[room_id] = {
                'id': room_id,  # WICHTIG: Raum-ID hinzufügen
                'leader': leader_id,
                'players': [],
                'settings': settings,
                'status': 'waiting',
                'ready_players': set(),
                'min_players': 4
            }
        print(f"Raum erstellt: {room_id} durch Spieler {leader_id}")
        return room_id
    
    @staticmethod
    def join_room(room_id, player_id, player_name):
        with lock:
            if room_id in rooms:
                player_data = {
                    'id': player_id,
                    'name': player_name,
                    'ready': False,
                    'coins': 10,
                    'balance_history': [10]
                }
                players[player_id] = player_data
                rooms[room_id]['players'].append(player_data)
                print(f"Spieler {player_name} ({player_id}) hat Raum {room_id} betreten")
                return True
        print(f"FEHLER: Raum {room_id} nicht gefunden für Spieler {player_name}")
        return False
    
    @staticmethod
    def start_game(room_id):
        with lock:
            if room_id not in rooms:
                return False
                
            room = rooms[room_id]
            ready_count = len(room['ready_players'])
            total_players = len(room['players'])
            
            print(f"Versuche Spiel zu starten: {ready_count} bereit von {total_players} Spielern")
            
            if (room['status'] == 'waiting' and 
                ready_count >= room['min_players'] and 
                ready_count % 4 == 0 and 
                total_players % 4 == 0):
                
                room['status'] = 'playing'
                room['current_round'] = 1
                room['groups'] = GameManager._create_groups(room['players'], room['settings'])
                game_state[room_id] = {
                    'round': 1,
                    'contributions': {},
                    'group_results': {}
                }
                print(f"Spiel in Raum {room_id} gestartet!")
                return True
        return False
    
    @staticmethod
    def _create_groups(players, settings):
        player_ids = [p['id'] for p in players]
        random.shuffle(player_ids)
        
        if settings.get('fixed_groups', True):
            # Feste Gruppen für das gesamte Spiel
            groups = []
            for i in range(0, len(player_ids), 4):
                group = player_ids[i:i+4]
                if len(group) >= 3:  # Mindestens 3 Spieler pro Gruppe
                    groups.append(group)
            return groups
        else:
            # Zufällige Gruppen jede Runde
            return GameManager._create_random_groups(player_ids)
    
    @staticmethod
    def _create_random_groups(player_ids):
        random.shuffle(player_ids)
        groups = []
        for i in range(0, len(player_ids), 4):
            group = player_ids[i:i+4]
            if len(group) >= 3:
                groups.append(group)
        return groups
    
    @staticmethod
    def submit_contribution(room_id, player_id, amount):
        with lock:
            if room_id in game_state:
                game_state[room_id]['contributions'][player_id] = amount
                return True
        return False
    
    @staticmethod
    def calculate_round_results(room_id):
        room = rooms[room_id]
        game_data = game_state[room_id]
        contributions = game_data['contributions']
        
        results = {}
        
        for group_idx, group in enumerate(room['groups']):
            total_contribution = sum(contributions.get(player_id, 0) for player_id in group)
            group_payout = (total_contribution * 2) / len(group)
            
            group_results = []
            for player_id in group:
                player_contribution = contributions.get(player_id, 0)
                player = players[player_id]
                
                # Berechne neues Guthaben
                remaining_coins = player['coins'] - player_contribution
                new_balance = remaining_coins + group_payout
                
                # Aktualisiere Spielerdaten
                player['coins'] = new_balance
                player['balance_history'].append(new_balance)
                
                group_results.append({
                    'player_id': player_id,
                    'contribution': player_contribution,
                    'new_balance': new_balance
                })
            
            results[group_idx] = {
                'total_contribution': total_contribution,
                'group_payout': group_payout,
                'players': group_results
            }
        
        game_data['group_results'] = results
        game_data['contributions'] = {}  # Zurücksetzen für nächste Runde
        
        # Prüfe ob Spiel weitergeht
        if GameManager._should_continue_game(room):
            room['current_round'] += 1
            if not room['settings'].get('fixed_groups', True):
                room['groups'] = GameManager._create_random_groups([p['id'] for p in room['players']])
        else:
            room['status'] = 'finished'
        
        return results
    
    @staticmethod
    def _should_continue_game(room):
        settings = room['settings']
        
        if settings.get('end_mode') == 'fixed_rounds':
            max_rounds = settings.get('max_rounds', 5)
            return room['current_round'] < max_rounds
        else:
            # Wahrscheinlichkeitsmodus
            continue_probability = settings.get('continue_probability', 0.5)
            return random.random() < continue_probability
        
    @staticmethod
    def get_available_rooms():
        """Gibt eine Liste von verfügbaren Räumen zurück"""
        available_rooms = []
        for room_id, room in rooms.items():
            if room['status'] == 'waiting':
                available_rooms.append({
                    'id': room_id,
                    'players': len(room['players']),
                    'leader': players[room['leader']]['name'] if room['leader'] in players else 'Unbekannt'
                })
        return available_rooms

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
def create_room():
    settings = {
        'fixed_groups': request.json.get('fixed_groups', True),
        'end_mode': request.json.get('end_mode', 'probability'),
        'max_rounds': request.json.get('max_rounds', 5),
        'continue_probability': request.json.get('continue_probability', 0.5)
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
    return render_template('game.html', 
                         room=room, 
                         player=player_data,
                         current_round=room.get('current_round', 1))

@app.route('/evaluation')
def evaluation():
    room_id = session.get('room_id')
    if not room_id or room_id not in rooms:
        return redirect(url_for('index'))
    
    room = rooms[room_id]
    player_data = players.get(session['player_id'])
    
    return render_template('evaluation.html', 
                         room=room, 
                         player=player_data)

@app.route('/api/available_rooms')
def available_rooms():
    return jsonify(GameManager.get_available_rooms())

# SocketIO Events
@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    print(f"Client disconnected: {request.sid}")

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    if room_id and player_id:
        join_room(room_id)
        print(f"Spieler {player_id} hat Socket-Room {room_id} betreten")
        emit('player_joined', {
            'player_id': player_id,
            'player_name': players.get(player_id, {}).get('name', 'Unbekannt')
        }, room=room_id)

@socketio.on('set_ready')
def handle_set_ready(data):
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    
    print(f"Set_ready erhalten: room_id={room_id}, player_id={player_id}")
    
    if room_id and room_id in rooms and player_id and player_id in players:
        with lock:
            rooms[room_id]['ready_players'].add(player_id)
            players[player_id]['ready'] = True
        
        print(f"Spieler {player_id} ist jetzt bereit in Raum {room_id}")
        emit('player_ready', {
            'player_id': player_id,
            'player_name': players[player_id]['name']
        }, room=room_id)
        
        # Prüfe ob Spiel gestartet werden kann
        room = rooms[room_id]
        ready_count = len(room['ready_players'])
        total_players = len(room['players'])
        
        can_start = (ready_count >= room['min_players'] and 
                    ready_count % 4 == 0 and 
                    total_players % 4 == 0)
        
        print(f"Startbedingungen: {ready_count}/{total_players} Spieler bereit, kann starten: {can_start}")
        emit('start_button_update', {
            'can_start': can_start,
            'ready_count': ready_count,
            'total_players': total_players
        }, room=room_id)

@socketio.on('start_game')
def handle_start_game():
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    
    print(f"Start_game erhalten von {player_id} für Raum {room_id}")
    
    if not room_id or room_id not in rooms:
        emit('start_failed', {'message': 'Raum nicht gefunden'})
        return
        
    room = rooms[room_id]
    
    # Überprüfe ob der Spieler der Leader ist
    if player_id != room['leader']:
        emit('start_failed', {'message': 'Nur der Leader kann das Spiel starten'})
        return
    
    ready_count = len(room['ready_players'])
    total_players = len(room['players'])
    
    print(f"Startversuch: {ready_count} von {total_players} Spielern bereit")
    
    # Überprüfe die Voraussetzungen
    if (ready_count >= room['min_players'] and 
        ready_count % 4 == 0 and 
        total_players % 4 == 0):
        
        if GameManager.start_game(room_id):
            emit('game_started', {'room_id': room_id}, room=room_id)
            print(f"Spiel erfolgreich gestartet für Raum {room_id}")
        else:
            emit('start_failed', {
                'message': 'Spiel konnte nicht gestartet werden.'
            })
    else:
        error_msg = (
            f'Spiel kann nicht gestartet werden. '
            f'Es müssen mindestens 4 Spieler bereit sein '
            f'und die Anzahl muss durch 4 teilbar sein. '
            f'Aktuell: {ready_count}/{total_players} Spieler bereit.'
        )
        emit('start_failed', {'message': error_msg})

@socketio.on('submit_contribution')
def handle_submit_contribution(data):
    room_id = session.get('room_id')
    player_id = session.get('player_id')
    amount = int(data.get('amount', 0))
    
    if GameManager.submit_contribution(room_id, player_id, amount):
        emit('contribution_submitted', {'player_id': player_id}, room=room_id)
        
        # Prüfe ob alle Beiträge eingereicht wurden
        room = rooms[room_id]
        game_data = game_state[room_id]
        if len(game_data['contributions']) == len(room['players']):
            results = GameManager.calculate_round_results(room_id)
            emit('round_results', results, room=room_id)
            
            if rooms[room_id]['status'] == 'finished':
                emit('game_finished', room=room_id)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_ENV') != 'production'
    
    if debug_mode:
        print("🔧 Starting in DEVELOPMENT mode")
    else:
        print("🚀 Starting in PRODUCTION mode")
    
    socketio.run(
        app, 
        host='0.0.0.0', 
        port=port, 
        debug=debug_mode,
        use_reloader=False
    )