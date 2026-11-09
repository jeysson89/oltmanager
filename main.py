from app import create_app
from app.routes import main

app = create_app()
app.register_blueprint(main)

# Запускаем мониторинг доступности (один раз при старте)
from app.auto_poller import start_monitoring
start_monitoring()

# Фоновый SNMP-опросчик (здоровье/трафик/статусы ONU)
from app.snmp_poller import start_snmp_poller
start_snmp_poller(app)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
