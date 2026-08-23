from app import create_app
from app.routes import main

app = create_app()
app.register_blueprint(main)

# Запускаем мониторинг доступности (один раз при старте)
from app.auto_poller import start_monitoring
start_monitoring()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
