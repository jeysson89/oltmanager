#!/usr/bin/env bash
set -e

if [[ $EUID -ne 0 ]]; then
   echo "Запустите с sudo: sudo bash install.sh"
   exit 1
fi

echo "========================================="
echo " Установка OLT Manager"
echo "========================================="

apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip mysql-client telnet snmp snmp-mibs-downloader git curl

read -p "Введите хост MySQL [localhost]: " MYSQL_HOST
MYSQL_HOST=${MYSQL_HOST:-localhost}
read -p "Введите порт MySQL [3306]: " MYSQL_PORT
MYSQL_PORT=${MYSQL_PORT:-3306}
read -p "Введите пользователя root для MySQL: " MYSQL_ROOT_USER
read -sp "Введите пароль root MySQL: " MYSQL_ROOT_PASS
echo
read -p "Создать отдельного пользователя для приложения? (y/n): " CREATE_APP_USER
if [[ "$CREATE_APP_USER" =~ ^[Yy]$ ]]; then
    read -p "Введите имя пользователя для приложения [oltuser]: " APP_DB_USER
    APP_DB_USER=${APP_DB_USER:-oltuser}
    read -sp "Введите пароль для пользователя приложения: " APP_DB_PASS
    echo
else
    APP_DB_USER=$MYSQL_ROOT_USER
    APP_DB_PASS=$MYSQL_ROOT_PASS
fi

mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_ROOT_USER" -p"$MYSQL_ROOT_PASS" <<SQL
CREATE DATABASE IF NOT EXISTS oltmanager CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$APP_DB_USER'@'%' IDENTIFIED BY '$APP_DB_PASS';
GRANT ALL PRIVILEGES ON oltmanager.* TO '$APP_DB_USER'@'%';
FLUSH PRIVILEGES;
SQL

PROJECT_DIR="/opt/oltmanager"
cd "$PROJECT_DIR"

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f config.py ]; then
    cp config.example.py config.py
    sed -i "s/BILLING_DB_HOST = .*/BILLING_DB_HOST = '127.0.0.1'/" config.py
    sed -i "s/BILLING_DB_PORT = .*/BILLING_DB_PORT = 3306/" config.py
    sed -i "s/BILLING_DB_USER = .*/BILLING_DB_USER = '$APP_DB_USER'/" config.py
    sed -i "s/BILLING_DB_PASSWORD = .*/BILLING_DB_PASSWORD = '$APP_DB_PASS'/" config.py
    sed -i "s/BILLING_DB_NAME = .*/BILLING_DB_NAME = 'oltmanager'/" config.py
    sed -i "s/SQLALCHEMY_DATABASE_URI = .*/SQLALCHEMY_DATABASE_URI = 'mysql+pymysql:\/\/$APP_DB_USER:$APP_DB_PASS@$MYSQL_HOST:$MYSQL_PORT\/oltmanager'/" config.py
fi

python -c "
from app import create_app
from app.models import db, User
app = create_app()
with app.app_context():
    if not User.query.filter_by(username='admin').first():
        admin = User(username='admin', is_admin=True)
        admin.set_password('admin')
        db.session.add(admin)
        db.session.commit()
        print('Администратор создан: admin/admin')
    else:
        print('Администратор уже существует')
"

cat > /etc/systemd/system/oltmanager.service <<EOF
[Unit]
Description=OLT Manager Web Server
After=network.target

[Service]
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/venv/bin/python $PROJECT_DIR/main.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable oltmanager
systemctl start oltmanager

echo "========================================="
echo " Установка завершена!"
echo "========================================="
echo "Веб-интерфейс: http://localhost:5000"
echo "Логин: admin Пароль: admin"
echo "========================================="
