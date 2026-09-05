from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Device(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    ip = db.Column(db.String(15), nullable=False)
    username = db.Column(db.String(64), nullable=False)
    password = db.Column(db.String(256), nullable=False)
    enable_password = db.Column(db.String(256), nullable=False)
    description = db.Column(db.String(256))
    device_type = db.Column(db.String(10), default='epon')  # 'epon' или 'gpon'
    sort_order = db.Column(db.Integer, default=0)  # порядок отображения

class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    mac = db.Column(db.String(20), unique=True, nullable=False, index=True)
    name = db.Column(db.String(256), nullable=False)

class MonitorTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey('device.id'), nullable=False)
    interface = db.Column(db.String(20), nullable=False)
    onu_id = db.Column(db.String(10), nullable=False)
    duration = db.Column(db.Integer, nullable=False)  # минуты
    interval = db.Column(db.Integer, nullable=False)  # минуты
    status = db.Column(db.String(10), nullable=False, default='running')  # running/done/interrupted
    start_time = db.Column(db.DateTime, default=datetime.utcnow)
    end_time = db.Column(db.DateTime, nullable=True)
    result = db.Column(db.Text, nullable=True)  # JSON с результатом
    device = db.relationship('Device', backref=db.backref('monitor_tasks', lazy=True))

class MonitorSnapshot(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.String(36), db.ForeignKey('monitor_task.task_id'), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    data = db.Column(db.Text, nullable=False)  # JSON снимка

class LoginHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False)
    ip_address = db.Column(db.String(45), nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
