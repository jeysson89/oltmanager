# OLT Manager

Веб-панель для управления OLT (EPON/GPON/VSOL) и абонентскими устройствами ONU/ONT. Позволяет операторам связи мониторить, сканировать и управлять оптическими терминалами, а также связывать их с биллинговой информацией.

## Основные возможности

- **Управление устройствами**: добавление, редактирование, удаление OLT.
- **Сканирование портов**: получение списка ONU, их MAC/SN, статусов, сигналов и температуры.
- **Интеграция с биллингом**: автоматический поиск адресов абонентов по MAC или SN.
- **Управление ONU**: перезагрузка, удаление, установка SLA 1 Гбит/с, добавление в blacklist.
- **Мониторинг доступности**: пинг устройств (5 попыток), цветовая индикация на главной странице.
- **Статистика сигналов**: история измерений, тренды (улучшение/ухудшение), графики.
- **Графики сигналов**: интерактивные графики изменения сигнала во времени (Chart.js).
- **Массовые операции**: перезагрузка и удаление нескольких ONU одновременно (чекбоксы).
- **Config ONT**: просмотр running-config для конкретного ONU (кнопка с шестерёнкой).
- **Автоматический опрос**: ежедневный фоновый опрос всех устройств.
- **Безопасность**: авторизация, роли (админ/пользователь), ограничение по IP, журнал входов.
- **Сортировка устройств**: перетаскивание (drag-and-drop) или кнопки ↑/↓.

## Структура файлов
/opt/oltmanager/
├── main.py # Точка входа Flask (register_blueprint)
├── config.py # Конфигурация: БД, биллинг, SNMP, автоопрос, IP-доступ
├── requirements.txt # Python-зависимости
├── install.sh # Интерактивный установщик
├── config.example.py # Пример конфигурации (без секретов)
├── README.md # Этот файл
├── app/
│ ├── init.py # Создание Flask приложения, LoginManager, запуск автополлера
│ ├── models.py # Модели БД: User, Device, LoginHistory, ScanCache, OnuSignalHistory
│ ├── routes.py # Все маршруты: устройства, API, статистика, настройки
│ ├── billing.py # Поиск адресов в биллинге (MAC/SN)
│ ├── olt_handler.py # Обработчик EPON (BDCOM) Telnet/SNMP
│ ├── gpon_handler.py # Обработчик GPON
│ ├── vsol_handler.py # Обработчик VSOL (EPON)
│ ├── auto_poller.py # Фоновый автопросмотр устройств + мониторинг доступности
│ ├── static/
│ │ ├── css/style.css # Основные стили (цвета сигналов, компактность)
│ │ ├── js/main.js # Общие JS-скрипты
│ │ ├── favicon.svg # Иконка сайта
│ │ └── favicon.ico
│ └── templates/
│ ├── base.html # Общий шаблон с навигацией
│ ├── index.html # Список устройств + форма добавления (модальное окно)
│ ├── device.html # Таблица ONU для EPON/GPON с кнопками, JS логика
│ ├── vsol_device.html # Таблица ONU для VSOL (аналогично, с кнопками)
│ ├── login.html # Авторизация
│ ├── edit_device.html # Редактирование устройства (используется модальное окно)
│ ├── settings.html # Настройки биллинга, SNMP, автопросмотра, мониторинга, IP
│ ├── users.html # Управление пользователями (только admin)
│ ├── add_user.html # Добавление пользователя
│ ├── change_password.html # Смена пароля
│ ├── login_history.html # История входов
│ ├── signal_stats.html # Статистика сигналов с трендами
│ └── signal_history.html # История сигналов конкретного ONU
├── oltmanager.service # systemd unit (устанавливается в /etc/systemd/system/)
└── ssl/ # Сертификаты (если нужен HTTPS)

text

## Взаимодействие файлов

1. **Запуск**: `main.py` создаёт Flask-приложение через `create_app()`, регистрирует Blueprint `main` из `routes.py`. При старте также запускается автополлер и мониторинг.
2. **Авторизация**: Flask-Login с моделью `User` (UserMixin). `user_loader` в `__init__.py` загружает пользователя.
3. **Маршрутизация**: Все URL обрабатываются в `routes.py`. Для каждого устройства создаётся соответствующий обработчик:
   - `device_type == 'epon'` → `OLTConnection` (BDCOM EPON)
   - `device_type == 'gpon'` → `GPONConnection`
   - `device_type == 'vsol'` → `VSOLConnection`
4. **Обработчики**:
   - `olt_handler.py`: BDCOM EPON. Методы: `connect()`, `send_command()`, `get_interfaces_snmp()`, `get_onu_data_for_scan()`, `get_onu_info()`, `get_mac_table()`, `get_running_config()`, `reboot_onu()`, `delete_onu()`, `set_1g_sla()`, `blacklist_onu()`, `get_lan_state()`.
   - `gpon_handler.py`: GPON. Аналогичные методы, но с GPON-командами (`show gpon onu-information`, `show gpon interface ... optical-transceiver-diagnosis`).
   - `vsol_handler.py`: VSOL (EPON). Команды в режиме `configure terminal`, `show onu auth-info all` (список ONU с MAC), `show onu opm-diag all` (сигналы и температура). Также поддержка MAC-таблицы через `show onu <id> mac-address-table`.
5. **Сбор данных**:
   - Список интерфейсов: SNMP (`snmpwalk`) → `get_interfaces_snmp()`.
   - Список ONU: `show onu auth-info all` (VSOL) или `show epon onu-information` (BDCOM) или `show gpon onu-information` (GPON).
   - Сигналы/температура: `show onu opm-diag all` (VSOL) или `show epon/gpon interface ... onu optical-transceiver-diagnosis`.
6. **Биллинг**: `billing.py` ищет адрес:
   - MAC (EPON/VSOL): через `dev_fields.value` → `dev_user` → `users_view_fsb_address`.
   - SN (GPON): через `dev_fields.device_sn` → `dev_user` → `users_view_fsb_address`.
7. **Кэширование**: `scan_cache` хранит результат последнего сканирования, `onu_signal_history` — историю сигналов (до 20 записей на ONU).
8. **Автопросмотр**: `auto_poller.py` в фоне опрашивает все устройства параллельно в заданное время (`AUTO_POLL_TIME`). Также там реализован **мониторинг доступности** (пинг) с параллельной проверкой.
9. **Фронтенд**: `device.html` и `vsol_device.html` — JS-логика: разворачивание портов, AJAX-запросы к API, отображение данных, кнопки действия, цветовая индикация сигналов, автоматическое копирование MAC в буфер обмена.

## Установка

### Автоматическая (рекомендуется)

Запустите интерактивный скрипт:

```bash
sudo bash install.sh
Скрипт:

<<<<<<< HEAD
Установит системные пакеты (Python, MySQL client, telnet, snmp, git, curl).

Создаст базу данных oltmanager и пользователя.

Создаст виртуальное окружение и установит Python-зависимости.

Создаст config.py на основе config.example.py с введёнными данными.

Создаст администратора admin с паролем admin.

Настроит systemd-сервис oltmanager.

Ручная установка
Установите зависимости:

bash
apt-get update
apt-get install -y python3 python3-venv python3-pip mysql-client telnet snmp snmp-mibs-downloader git curl
Скопируйте проект в /opt/oltmanager.

Создайте и активируйте виртуальное окружение:

bash
python3 -m venv venv
source venv/bin/activate
Установите зависимости:

bash
pip install -r requirements.txt
Скопируйте config.example.py в config.py и отредактируйте под свои нужды (БД, биллинг и т.д.).

Создайте базу данных и пользователя:

bash
mysql -u root -p
CREATE DATABASE oltmanager CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'oltuser'@'%' IDENTIFIED BY 'oltpassword';
GRANT ALL PRIVILEGES ON oltmanager.* TO 'oltuser'@'%';
FLUSH PRIVILEGES;
Запустите приложение:

bash
python main.py
Откройте браузер: http://localhost:5000, войдите admin/admin.

Конфигурация
Параметры в config.py:

Параметр	Описание
SECRET_KEY	Секретный ключ Flask
SQLALCHEMY_DATABASE_URI	Строка подключения к БД
BILLING_DB_HOST	Хост БД биллинга
BILLING_DB_PORT	Порт БД биллинга
BILLING_DB_USER	Пользователь БД биллинга
BILLING_DB_PASSWORD	Пароль БД биллинга
BILLING_DB_NAME	Имя БД биллинга
BILLING_ADDRESS_QUERY	SQL-запрос для поиска адреса по MAC/SN
SNMP_AUTO_SCAN	Автоматический SNMP-сбор при загрузке страницы
AUTO_POLL_ENABLED	Включить ежедневный автопросмотр
AUTO_POLL_TIME	Время запуска автопросмотра (например, "02:00")
MONITORING_ENABLED	Включить мониторинг доступности (пинг)
MONITORING_INTERVAL	Интервал проверки доступности (секунд)
ALLOWED_IPS	Список разрешённых IP (пусто = все)
Использование
Главная страница
Отображает список устройств с цветовым индикатором (зелёный — доступен, красный — недоступен, серый — нет данных).

Кнопки: «Открыть» (переход к устройству), «Изменить» (модальное окно), «Удалить».

Сортировка: перетаскивание карточек или кнопки ↑/↓.

Кнопка «Добавить устройство» открывает модальное окно с полями: имя, IP, логин, пароль, enable-пароль, тип.

Страница устройства
Таблица портов и ONU.

Для каждого ONU: статус, MAC/SN, адрес (из биллинга), температура, VLAN, LAN-статус, MAC-адрес (если получен), сигнал (dBm).

Кнопки: «Опросить» (сканирование), «MAC-таблица» (получить MAC и VLAN, автоматически копирует в буфер), «LAN-статус», «Перезагрузка», «Удалить», «Установить SLA 1G», «Blacklist», «График» (построение графика сигнала), «Config ONT» (просмотр running-config).

Массовые операции: чекбоксы для выбора нескольких ONU, кнопки «Перезагрузить» и «Удалить».

Настройки
Подключение к биллингу.

SNMP.

Автопросмотр.

Мониторинг доступности.

Ограничение по IP.

Статистика сигналов
Таблица последних замеров по каждому ONU.

Тренды (улучшение/ухудшение), мини-графики.

Приоритеты (красный — сильное ухудшение, жёлтый — улучшение, синий — новый).

Команды управления
bash
# Перезапуск сервиса
systemctl restart oltmanager

# Просмотр логов
journalctl -u oltmanager -f

# Сброс пароля admin
cd /opt/oltmanager && source venv/bin/activate
python3 << 'EOF'
from app import create_app
from app.models import db, User
app = create_app()
with app.app_context():
    user = User.query.filter_by(username='admin').first()
    user.set_password('новый_пароль')
    db.session.commit()
EOF

# Бекап проекта
tar -czf /root/backups/oltmanager_$(date +%Y%m%d).tar.gz --exclude='venv' /opt/oltmanager
mysqldump -u oltuser -poltpassword oltmanager > /root/backups/oltmanager_db_$(date +%Y%m%d).sql
Безопасность
После первого входа смените пароль администратора.

Ограничьте доступ по IP через ALLOWED_IPS в config.py.

Не храните config.py в репозитории (он в .gitignore).

Для продакшена рекомендуется использовать Nginx + HTTPS.

Типы устройств
Тип	Формат MAC/SN	Команда получения ONU	Команда сигналов
EPON (BDCOM)	80:F7:A6:82:A2:68	show epon onu-information	show epon interface epon X:Y onu ctc optical-transceiver-diagnosis
GPON	HWTC:50203CD3 (SN)	show gpon onu-information	show gpon interface gpon X:Y onu optical-transceiver-diagnosis
VSOL (EPON)	80:14:A8:59:7D:50	show onu auth-info all	show onu opm-diag all
Доработки, добавленные в ходе разработки
Мониторинг доступности: параллельная проверка пингом (5 попыток), цветовая индикация, блокировка кнопки «Открыть» при недоступности.

Сортировка устройств: drag-and-drop и кнопки ↑/↓, сохранение порядка в БД.

VSOL улучшения:

Исправлен send_command для корректного ожидания промптов VSOL.

Добавлена поддержка MAC-таблицы (show onu <id> mac-address-table) с автоматическим копированием MAC в буфер обмена.

Исправлен фильтр ONU по порту (чтобы таблица не дублировалась).

Модальное окно добавления/редактирования: удобная форма с кнопкой «глаз» для паролей.

Автоматическое копирование MAC при получении MAC-таблицы.

Графики сигналов: интерактивные графики через Chart.js.

Массовые операции: перезагрузка/удаление нескольких ONU через чекбоксы.

Config ONT: просмотр running-config для конкретного ONU.

Сохранение сигналов для всех ONU: даже офлайн-устройства попадают в историю (с NULL).

Вывод VLAN для каждого MAC: если несколько MAC, VLAN отображаются построчно.

Заключение
OLT Manager — гибкое решение для управления PON-сетями. Проект легко расширяется: можно добавить новые типы оборудования, улучшить интеграцию с биллингом, добавить уведомления и т.д. Благодарим за использование!
=======
## Дополнительные функции (добавлены в процессе разработки)
- **Графики сигналов** – интерактивные графики изменения сигнала (Chart.js).
- **Массовые операции** – перезагрузка/удаление нескольких ONU через чекбоксы.
- **Сохранение сигналов для всех ONU** – даже офлайн-устройства попадают в историю.

## Дополнительные функции (добавлены в процессе разработки)
- **Config ONT**: просмотр running-config для конкретного ONU (кнопка с шестерёнкой).
- **Сокращённые кнопки массовых операций**: "Перезагрузить" и "Удалить" с полной подсказкой при наведении.
- **Вывод VLAN для каждого MAC**: если несколько MAC, VLAN отображаются построчно.
>>>>>>> f1e29a2 (Добавлена функция Config ONT, сокращены кнопки, исправлен вывод VLAN, обновлён README)
