from flask import Flask
from flask_login import LoginManager
from config import Config
from app.models import db, User

login_manager = LoginManager()

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = 'main.login'

    with app.app_context():
        db.create_all()

    # Запускаем автополлер
    from app.auto_poller import auto_poller
    auto_poller.start()
    
    return app

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))
