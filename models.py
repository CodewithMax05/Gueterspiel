from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, timezone
import uuid

db = SQLAlchemy()

class Room(db.Model):
    __tablename__ = 'rooms'
    
    id = db.Column(db.String(8), primary_key=True)
    leader_id = db.Column(db.String(36), nullable=False)
    leader_name = db.Column(db.String(150), nullable=False)
    settings = db.Column(db.JSON, nullable=False)
    status = db.Column(db.String(20), default='waiting', index=True)  # Index für häufige Abfragen
    current_round = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    updated_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
    
    # Relationships mit expliziten foreign_keys
    players = db.relationship('Player', backref='room_ref', lazy='dynamic', cascade='all, delete-orphan')
    game_states = db.relationship('GameState', backref='room_ref', lazy='dynamic', cascade='all, delete-orphan')

class Player(db.Model):
    __tablename__ = 'players'
    
    id = db.Column(db.String(36), primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    room_id = db.Column(db.String(8), db.ForeignKey('rooms.id'), nullable=False, index=True)
    ready = db.Column(db.Boolean, default=False, index=True)
    coins = db.Column(db.Float, default=10.0)
    balance_history = db.Column(db.JSON, default=list)
    contribution_history = db.Column(db.JSON, default=list)
    is_leader = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class GameState(db.Model):
    __tablename__ = 'game_states'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.String(8), db.ForeignKey('rooms.id'), nullable=False, index=True)
    round = db.Column(db.Integer, nullable=False, index=True)
    contributions = db.Column(db.JSON, default=dict)
    group_results = db.Column(db.JSON, default=list)
    round_start_time = db.Column(db.Float, default=0.0)
    groups = db.Column(db.JSON, default=list)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class PlayerSession(db.Model):
    __tablename__ = 'player_sessions'
    
    id = db.Column(db.Integer, primary_key=True)
    player_id = db.Column(db.String(36), nullable=False, index=True)
    session_id = db.Column(db.String(36), nullable=False, index=True)
    room_id = db.Column(db.String(8), nullable=False, index=True)
    connected = db.Column(db.Boolean, default=True)
    last_seen = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), index=True)