from gevent import monkey, spawn, sleep
monkey.patch_all()

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
from models import db, Room, Player, GameState, PlayerSession
import random
import uuid
from collections import defaultdict
from threading import Lock
import os
import time
from datetime import datetime, timezone, timedelta

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev_secret_key')

# Datenbank-Konfiguration
database_url = os.environ.get('DATABASE_URL', 'sqlite:///public_goods.db')
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)

# Socket.IO Konfiguration
socketio = SocketIO(
    app, 
    async_mode='gevent',
    manage_session=True,
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25,
    max_http_buffer_size=1e8,
    allow_upgrades=True,
    transports=['websocket', 'polling'],
    cors_allowed_origins="*"
)

# In-Memory Timer
round_timers = {}
sid_to_player = {}
lock = Lock()

class RoundTimer:
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
        
        room = Room(
            id=room_id,
            leader_id=leader_id,
            leader_name=leader_name,
            settings=settings,
            status='waiting'
        )
        
        db.session.add(room)
        db.session.commit()
        
        print(f"✅ Raum {room_id} erstellt durch {leader_name} ({leader_id})")
        return room_id
    
    @staticmethod
    def join_room(room_id, player_id, player_name, is_leader=False):
        room = Room.query.get(room_id)
        if not room:
            print(f"❌ Raum {room_id} nicht gefunden")
            return False
        
        # Prüfen ob Spieler bereits existiert
        player = Player.query.filter_by(id=player_id, room_id=room_id).first()
        if not player:
            initial_coins = room.settings.get('initial_coins', 10)
            player = Player(
                id=player_id,
                name=player_name,
                room_id=room_id,
                coins=initial_coins,
                balance_history=[initial_coins],
                is_leader=is_leader
            )
            db.session.add(player)
        else:
            player.name = player_name
            player.is_leader = is_leader
        
        db.session.commit()
        print(f"✅ {player_name} ({player_id}) → Raum {room_id}")
        return True
    
    @staticmethod
    def get_room_players(room_id):
        # Zeige nur Nicht-Leader-Spieler an
        players = Player.query.filter_by(room_id=room_id, is_leader=False).all()
        return [{
            'id': p.id,
            'name': p.name,
            'ready': p.ready,
            'coins': p.coins,
            'is_leader': p.is_leader
        } for p in players]
    
    @staticmethod
    def get_ready_players_count(room_id):
        # Zähle nur Nicht-Leader-Spieler
        return Player.query.filter_by(room_id=room_id, ready=True, is_leader=False).count()
        
    @staticmethod
    def start_game(room_id):
        from sqlalchemy.exc import SQLAlchemyError
        
        try:
            room = Room.query.get(room_id)
            if not room:
                return False
                
            can_start, message = GameManager.can_start_game(room_id)
            if not can_start:
                print(f"❌ Spielstart fehlgeschlagen: {message}")
                return False
            
            # Verwende Transaktion
            db.session.begin_nested()
            
            players = Player.query.filter_by(room_id=room_id, is_leader=False).all()
            
            # Raum aktualisieren
            room.status = 'playing'
            room.current_round = 1
            
            # Gruppen erstellen (ohne Leader)
            groups = GameManager._create_groups(players, room.settings)
            
            # GameState erstellen
            game_state = GameState(
                room_id=room_id,
                round=1,
                contributions={},
                group_results=[],
                round_start_time=time.time(),
                groups=groups
            )
            
            db.session.add(game_state)
            db.session.commit()
            
            print(f"✅ Spiel gestartet in Raum {room_id}!")
            GameManager._start_round_timer(room_id)
            return True
            
        except SQLAlchemyError as e:
            db.session.rollback()
            print(f"❌ Datenbankfehler beim Spielstart: {e}")
            return False
    
    @staticmethod
    def start_game(room_id):
        room = Room.query.get(room_id)
        if not room:
            return False
            
        can_start, message = GameManager.can_start_game(room_id)
        if not can_start:
            print(f"❌ Spielstart fehlgeschlagen: {message}")
            return False
        
        players = Player.query.filter_by(room_id=room_id).all()
        
        # Raum aktualisieren
        room.status = 'playing'
        room.current_round = 1
        
        # Gruppen erstellen
        groups = GameManager._create_groups(players, room.settings)
        
        # GameState erstellen
        game_state = GameState(
            room_id=room_id,
            round=1,
            contributions={},
            group_results=[],
            round_start_time=time.time(),
            groups=groups
        )
        
        db.session.add(game_state)
        db.session.commit()
        
        print(f"✅ Spiel gestartet in Raum {room_id}!")
        GameManager._start_round_timer(room_id)
        return True
    
    @staticmethod
    def _create_groups(players_list, settings):
        # Filtere Leader aus den Spielergruppen
        non_leader_players = [p for p in players_list if not p.is_leader]
        player_ids = [p.id for p in non_leader_players]
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
        room = Room.query.get(room_id)
        if not room:
            return
            
        round_duration = room.settings.get('round_duration', 60)
        
        if room_id in round_timers:
            round_timers[room_id].stop()
        
        timer = RoundTimer(socketio, room_id, round_duration)
        round_timers[room_id] = timer
        timer.start()
        
        print(f"⏰ Timer gestartet für Raum {room_id}: {round_duration}s")
    
    @staticmethod
    def submit_contribution(room_id, player_id, amount):
        room = Room.query.get(room_id)
        game_state = GameState.query.filter_by(room_id=room_id, round=room.current_round).first()
        player = Player.query.filter_by(id=player_id, room_id=room_id).first()
        
        if not room or not game_state or not player:
            return False
        
        max_contribution = min(
            player.coins,
            room.settings.get('max_contribution', 10)
        )
        
        if amount < 0 or amount > max_contribution:
            print(f"❌ Ungültiger Beitrag: {amount} (max: {max_contribution})")
            return False
        
        contributions = game_state.contributions or {}
        contributions[player_id] = amount
        game_state.contributions = contributions
        
        player.contribution_history.append(amount)
        
        db.session.commit()
        
        print(f"💰 {player.name} zahlt {amount} ein")
        
        total_players = Player.query.filter_by(room_id=room_id).count()
        if len(contributions) == total_players:
            if room_id in round_timers:
                round_timers[room_id].stop()
            
            print(f"✅ Alle Spieler haben eingezahlt in Raum {room_id}")
            spawn(GameManager._process_round_end, room_id)
        
        return True
    
    @staticmethod
    def _process_round_end(room_id):
        sleep(0.5)
        
        with lock:
            room = Room.query.get(room_id)
            if not room:
                return
                
            results = GameManager.calculate_round_results(room_id)
        
        socketio.emit('show_round_results', {
            'results': results,
            'room_id': room_id
        }, room=room_id)
    
    @staticmethod
    def calculate_round_results(room_id):
        room = Room.query.get(room_id)
        game_state = GameState.query.filter_by(room_id=room_id, round=room.current_round).first()
        players = {p.id: p for p in Player.query.filter_by(room_id=room_id).all()}
        
        multiplier = room.settings.get('multiplier', 2)
        results = []
        
        for group_idx, group in enumerate(game_state.groups):
            total_contribution = sum(game_state.contributions.get(pid, 0) for pid in group)
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
                contribution = game_state.contributions.get(player_id, 0)
                
                remaining = player.coins - contribution
                new_balance = remaining + payout_per_player
                profit = new_balance - player.coins
                
                player.coins = new_balance
                player.balance_history.append(new_balance)
                
                group_result['players'].append({
                    'id': player_id,
                    'name': player.name,
                    'contribution': contribution,
                    'payout': payout_per_player,
                    'new_balance': new_balance,
                    'profit': profit
                })
            
            results.append(group_result)
        
        game_state.group_results = results
        game_state.contributions = {}
        db.session.commit()
        
        print(f"📊 Rundenergebnisse berechnet für Raum {room_id}")
        return results
    
    @staticmethod
    def start_next_round(room_id):
        room = Room.query.get(room_id)
        if not room:
            return False
            
        if GameManager._should_continue_game(room):
            room.current_round += 1
            
            if not room.settings.get('fixed_groups', True):
                players = Player.query.filter_by(room_id=room_id).all()
                room.groups = GameManager._create_groups(players, room.settings)
                print(f"🔄 Neue Gruppen erstellt für Runde {room.current_round}")
            
            new_game_state = GameState(
                room_id=room_id,
                round=room.current_round,
                contributions={},
                group_results=[],
                round_start_time=time.time(),
                groups=room.groups
            )
            
            db.session.add(new_game_state)
            db.session.commit()
            
            print(f"▶️ Runde {room.current_round} startet in Raum {room_id}")
            GameManager._start_round_timer(room_id)
            return True
        else:
            room.status = 'finished'
            db.session.commit()
            print(f"🏁 Spiel beendet in Raum {room_id}")
            return False
    
    @staticmethod
    def _should_continue_game(room):
        settings = room.settings
        current_round = room.current_round
        
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
        rooms = Room.query.filter_by(status='waiting').all()
        available = []
        
        for room in rooms:
            player_count = Player.query.filter_by(room_id=room.id).count()
            available.append({
                'id': room.id,
                'players': player_count,
                'leader': room.leader_name
            })
        
        return available
    
    @staticmethod
    def get_player_group(room_id, player_id):
        room = Room.query.get(room_id)
        if not room:
            return None
            
        game_state = GameState.query.filter_by(room_id=room_id, round=room.current_round).first()
        if not game_state or not game_state.groups:
            return None
            
        for idx, group in enumerate(game_state.groups):
            if player_id in group:
                players = Player.query.filter(Player.id.in_(group)).all()
                return {
                    'group_number': idx + 1,
                    'members': [p.name for p in players]
                }
        return None

    @staticmethod
    def set_player_ready(room_id, player_id):
        player = Player.query.filter_by(id=player_id, room_id=room_id).first()
        if player:
            player.ready = True
            db.session.commit()
            return True
        return False

    @staticmethod
    def cleanup_room(room_id):
        """Räumt einen Raum komplett auf"""
        try:
            # Lösche alle abhängigen Daten
            Player.query.filter_by(room_id=room_id).delete()
            GameState.query.filter_by(room_id=room_id).delete()
            PlayerSession.query.filter_by(room_id=room_id).delete()
            Room.query.filter_by(id=room_id).delete()
            
            db.session.commit()
            
            # Timer stoppen
            if room_id in round_timers:
                round_timers[room_id].stop()
                del round_timers[room_id]
                
            print(f"🗑️ Raum {room_id} komplett aufgeräumt")
            return True
        except Exception as e:
            print(f"❌ Fehler beim Aufräumen von Raum {room_id}: {e}")
            db.session.rollback()
            return False

# Datenbank initialisieren
def initialize_database():
    with app.app_context():
        db.create_all()
        print("✅ Datenbank initialisiert")

# Routes
@app.route('/')
def index():
    session.clear()
    return render_template('index.html')

@app.route('/create_game')
def create_game():
    session['player_id'] = str(uuid.uuid4())
    session['player_name'] = f"Leader_{random.randint(1000, 9999)}"
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
    
    # Leader dem Raum beitreten
    GameManager.join_room(room_id, session['player_id'], session['player_name'], is_leader=True)
    
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
    room = Room.query.get(room_id) if room_id else None
    
    if not room:
        return redirect(url_for('index'))
    
    is_leader = session.get('is_leader', False)
    player_data = Player.query.filter_by(id=session['player_id'], room_id=room_id).first()
    
    if not player_data:
        return redirect(url_for('index'))
    
    players = GameManager.get_room_players(room_id)
    ready_count = GameManager.get_ready_players_count(room_id)
    
    return render_template('game_room.html', 
                         room=room, 
                         player=player_data,
                         players=players,
                         ready_count=ready_count,
                         is_leader=is_leader)

@app.route('/game')
def game():
    room_id = session.get('room_id')
    if not room_id:
        return redirect(url_for('index'))
    
    room = Room.query.get(room_id)
    if not room or room.status != 'playing':
        return redirect(url_for('game_room'))
    
    player_data = Player.query.filter_by(id=session['player_id'], room_id=room_id).first()
    if not player_data:
        return redirect(url_for('index'))
    
    player_group = GameManager.get_player_group(room_id, session['player_id'])
    
    return render_template('game.html', 
                         room=room, 
                         player=player_data,
                         player_group=player_group,
                         current_round=room.current_round)

@app.route('/round_results')
def round_results():
    room_id = session.get('room_id')
    if not room_id:
        return redirect(url_for('index'))
    
    room = Room.query.get(room_id)
    if not room:
        return redirect(url_for('index'))
    
    player_data = Player.query.filter_by(id=session['player_id'], room_id=room_id).first()
    if not player_data:
        return redirect(url_for('index'))
    
    game_state = GameState.query.filter_by(room_id=room_id, round=room.current_round).first()
    results = game_state.group_results if game_state else []
    
    return render_template('round_results.html',
                         room=room,
                         player=player_data,
                         results=results,
                         is_leader=session.get('is_leader', False))

@app.route('/evaluation')
def evaluation():
    room_id = session.get('room_id')
    if not room_id:
        return redirect(url_for('index'))
    
    room = Room.query.get(room_id)
    if not room:
        return redirect(url_for('index'))
    
    player_data = Player.query.filter_by(id=session['player_id'], room_id=room_id).first()
    if not player_data:
        return redirect(url_for('index'))
    
    initial_coins = room.settings.get('initial_coins', 10)
    total_rounds = room.current_round
    
    return render_template('evaluation.html', 
                         room=room, 
                         player=player_data,
                         initial_coins=initial_coins,
                         total_rounds=total_rounds)

@app.route('/api/available_rooms')
def available_rooms():
    return jsonify(GameManager.get_available_rooms())

@app.route('/cleanup_old_rooms', methods=['POST'])
def cleanup_old_rooms():
    """Bereinigt alte Räume automatisch"""
    try:
        from sqlalchemy import text
        
        # Lösche Räume, die älter als 2 Stunden sind und im Status 'finished' oder 'waiting'
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=2)
        
        # Verwende SQL für bessere Performance bei großen Datenmengen
        old_rooms = db.session.execute(
            text("SELECT id FROM rooms WHERE created_at < :cutoff AND status IN ('finished', 'waiting')"),
            {'cutoff': cutoff_time}
        ).fetchall()
        
        cleaned_count = 0
        for room in old_rooms:
            if GameManager.cleanup_room(room[0]):
                cleaned_count += 1
        
        return jsonify({
            'cleaned_count': cleaned_count, 
            'message': f'{cleaned_count} alte Räume bereinigt'
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# WebSocket Events
@socketio.on('connect')
def handle_connect():
    print(f"🔌 Client verbunden: {request.sid}")
    player_id = session.get('player_id')
    room_id = session.get('room_id')
    
    if player_id:
        sid_to_player[request.sid] = player_id
        
        # PlayerSession erstellen/aktualisieren
        session_record = PlayerSession.query.filter_by(player_id=player_id).first()
        if not session_record:
            session_record = PlayerSession(
                player_id=player_id,
                session_id=request.sid,
                room_id=room_id or ''
            )
            db.session.add(session_record)
        else:
            session_record.session_id = request.sid
            session_record.connected = True
            session_record.last_seen = datetime.now(timezone.utc)
            session_record.room_id = room_id or ''
        
        db.session.commit()
    
    if room_id and room_id in [room.id for room in Room.query.all()]:
        join_room(room_id)
        print(f"✅ Automatisch Raum {room_id} beigetreten nach Reconnect")
    
    emit('connection_success', {
        'message': 'Verbunden', 
        'sid': request.sid,
        'player_id': player_id,
        'room_id': room_id,
        'timestamp': time.time()
    })

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    player_id = sid_to_player.pop(sid, None)
    print(f"🔌 Client getrennt: {sid} (player_id={player_id})")

    if player_id:
        session_record = PlayerSession.query.filter_by(player_id=player_id).first()
        if session_record:
            session_record.connected = False
            db.session.commit()

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    print(f"👥 join_room Event: room_id={room_id}, player_id={player_id}, sid={request.sid}")

    if not player_id:
        emit('error', {'message': 'Keine player_id in Session'})
        return

    room = Room.query.get(room_id)
    if not room:
        print(f"❌ Raum {room_id} nicht gefunden")
        emit('error', {'message': 'Raum nicht gefunden'})
        return

    join_room(room_id)
    sid_to_player[request.sid] = player_id
    
    # PlayerSession aktualisieren
    session_record = PlayerSession.query.filter_by(player_id=player_id).first()
    if session_record:
        session_record.room_id = room_id
        db.session.commit()
    
    print(f"✅ Socket {request.sid} → Room {room_id} (player {player_id})")

    emit('room_joined', {
        'room_id': room_id,
        'message': 'Erfolgreich beigetreten',
        'player_id': player_id
    })

    # Raum-Update an alle senden
    players = GameManager.get_room_players(room_id)
    ready_count = GameManager.get_ready_players_count(room_id)
    
    emit('room_update', {
        'players': players,
        'ready_count': ready_count,
        'total_players': len(players),
        'group_size': room.settings.get('group_size', 4)
    }, room=room_id)

@socketio.on('set_ready')
def handle_set_ready(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    print(f"✋ Ready-Event: room={room_id}, player={player_id}, sid={request.sid}")
    
    room = Room.query.get(room_id)
    if not room:
        print(f"❌ Raum {room_id} nicht gefunden")
        emit('error', {'message': 'Raum nicht gefunden'})
        return
        
    player = Player.query.filter_by(id=player_id, room_id=room_id).first()
    if not player:
        print(f"❌ Spieler {player_id} nicht gefunden")
        emit('error', {'message': 'Spieler nicht gefunden'})
        return
    
    player.ready = True
    db.session.commit()
    
    print(f"✅ {player.name} ist bereit")
    
    # Aktualisierten Raumstatus an alle senden
    players = GameManager.get_room_players(room_id)
    ready_count = GameManager.get_ready_players_count(room_id)
    
    emit('player_ready', {
        'player_id': player_id,
        'player_name': player.name,
        'ready_count': ready_count,
        'total_players': len(players)
    }, room=room_id)
    
    # Start-Button Status prüfen
    group_size = room.settings.get('group_size', 4)
    
    can_start = (ready_count >= group_size and 
                ready_count % group_size == 0 and 
                len(players) % group_size == 0)
    
    emit('start_button_update', {
        'can_start': can_start,
        'ready_count': ready_count,
        'total_players': len(players),
        'group_size': group_size
    }, room=room_id)

@socketio.on('start_game')
def handle_start_game(data):
    room_id = data.get('room_id')
    player_id = data.get('player_id')
    
    print(f"🎮 Start-Event von {player_id} für Raum {room_id}")
    
    room = Room.query.get(room_id)
    if not room:
        emit('start_failed', {'message': 'Raum nicht gefunden'})
        return
        
    # Leader-Überprüfung
    if player_id != room.leader_id:
        emit('start_failed', {'message': 'Nur der Leader kann das Spiel starten'})
        return
    
    # Validierung vor Start
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
    
    # Prüfe ob Spieler Leader ist
    player = Player.query.filter_by(id=player_id).first()
    if player and player.is_leader:
        emit('contribution_failed', {
            'message': 'Leader kann keine Beiträge leisten'
        })
        return
    
    if GameManager.submit_contribution(room_id, player_id, amount):
        emit('contribution_submitted', {
            'player_id': player_id,
            'player_name': player.name,
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
        room = Room.query.get(room_id)
        emit('next_round_started', {
            'round': room.current_round
        }, room=room_id)
    else:
        emit('game_finished', room=room_id)

@socketio.on('leave_room')
def handle_leave_room(data):
    room_id = data.get('room_id')
    player_id = session.get('player_id')
    
    if room_id and player_id:
        leave_room(room_id)
        
        # Spieler aus Datenbank entfernen
        player = Player.query.filter_by(id=player_id, room_id=room_id).first()
        if player:
            db.session.delete(player)
            db.session.commit()
            
        # Prüfen ob Raum leer ist
        remaining_players = Player.query.filter_by(room_id=room_id).count()
        if remaining_players == 0:
            GameManager.cleanup_room(room_id)
        
        print(f"🚪 Spieler {player_id} hat Raum {room_id} verlassen")

if __name__ == '__main__':
    initialize_database()
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