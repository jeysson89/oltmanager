import threading
import uuid
from flask import Blueprint, render_template, redirect, url_for, request, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from functools import wraps
from app.models import db, User, Device, LoginHistory
from app.olt_handler import OLTConnection
from app.gpon_handler import GPONConnection
from app.vsol_handler import VSOLConnection
from app.billing import get_address_from_billing
import os
import sys
import json
from datetime import datetime, timedelta
import time

# Глобальный кэш VSOL
_vsol_cache_global = {}

main = Blueprint('main', __name__)

def admin_required(f):
    @wraps(f)
    @login_required
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            return "Доступ запрещён", 403
        return f(*args, **kwargs)
    return decorated_function

def check_ip_access():
    """Проверяет, разрешён ли IP-адрес для доступа."""
    from config import Config
    allowed_ips = getattr(Config, 'ALLOWED_IPS', [])
    if not allowed_ips:
        return True
    
    client_ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
    
    for allowed_ip in allowed_ips:
        # Поддержка полного IP или подсети (например, 192.168.1.0/24)
        if allowed_ip == client_ip:
            return True
        if '/' in allowed_ip:
            # Простая проверка подсети
            import ipaddress
            try:
                if ipaddress.ip_address(client_ip) in ipaddress.ip_network(allowed_ip, strict=False):
                    return True
            except:
                pass
    
    return False

# Применяем проверку IP ко всем запросам
@main.before_request
def before_request():
    if not check_ip_access():
        return "Доступ запрещён с вашего IP", 403

# ---------- Вспомогательная функция для трендов ----------
def get_trend(device_id, interface, onu_id, current_signal):
    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT signal_db FROM onu_signal_history
            WHERE device_id = %s AND interface = %s AND onu_id = %s
            ORDER BY scanned_at DESC LIMIT 21
        """, (device_id, interface, onu_id))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        
        history = [row[0] for row in rows if row[0] is not None]
        history.reverse()
        
        is_new = len(history) <= 1
        trend = 'stable'
        diff = 0
        if len(history) >= 2:
            diff = round(history[-1] - history[-2], 2)
            if diff > 0.5:
                trend = 'up'
            elif diff < -0.5:
                trend = 'down'
        
        priority = 0
        if trend == 'down' and abs(diff) >= 2:
            priority = 2
        elif trend == 'up' and abs(diff) >= 2:
            priority = 1
        elif is_new:
            priority = 3
        
        return {'trend': trend, 'diff': abs(diff), 'history': history[-20:], 'is_new': is_new, 'priority': priority}
    except Exception as e:
        print(f"Trend error: {e}", file=sys.stderr)
        return {'trend': 'stable', 'diff': 0, 'history': [], 'is_new': False, 'priority': 0}

@main.app_context_processor
def utility_processor():
    from config import Config
    def _c(name, default):
        return getattr(Config, name, default)
    return dict(
        get_trend=get_trend,
        # Цветовые настройки (тёмная тема)
        dark_bg_color=_c('DARK_BG_COLOR', '#0f172a'),
        dark_card_color=_c('DARK_CARD_COLOR', '#1e293b'),
        dark_text_color=_c('DARK_TEXT_COLOR', '#f1f5f9'),
        dark_secondary_text=_c('DARK_TEXT_COLOR', '#f1f5f9'),
        dark_signal_has_color=_c('DARK_SIGNAL_HAS_COLOR', '#1a3a2a'),
        dark_signal_nosignal_color=_c('DARK_SIGNAL_NOSIGNAL_COLOR', '#3a1a1a'),
        dark_accent_color=_c('DARK_ACCENT_COLOR', '#3b82f6'),
        dark_signal_green=_c('DARK_SIGNAL_GREEN', '#10b981'),
        dark_signal_yellow=_c('DARK_SIGNAL_YELLOW', '#f59e0b'),
        dark_signal_red=_c('DARK_SIGNAL_RED', '#ef4444'),
        # Светлая тема
        light_bg_color=_c('LIGHT_BG_COLOR', '#f1f5f9'),
        light_card_color=_c('LIGHT_CARD_COLOR', '#ffffff'),
        light_text_color=_c('LIGHT_TEXT_COLOR', '#1e293b'),
        light_signal_has_color=_c('LIGHT_SIGNAL_HAS_COLOR', '#d1fae5'),
        light_signal_nosignal_color=_c('LIGHT_SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
        light_accent_color=_c('LIGHT_ACCENT_COLOR', '#2563eb'),
        # Отзывчивость
        ui_auto_refresh_enabled=_c('UI_AUTO_REFRESH_ENABLED', True),
        ui_auto_refresh_interval=_c('UI_AUTO_REFRESH_INTERVAL', 30),
        background_scans_enabled=_c('BACKGROUND_SCANS_ENABLED', True),
        history_retention_days=_c('HISTORY_RETENTION_DAYS', 180),
        # Общие (совместимость)
        signal_has_color=_c('SIGNAL_HAS_COLOR', '#d1fae5'),
        signal_nosignal_color=_c('SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
    )

# ---------- Главная и авторизация ----------
def has_permission(user, perm):
    """Проверяет, имеет ли пользователь право на раздел."""
    if user.is_admin:
        return True
    perms = user.permissions.split(',') if user.permissions else []
    return perm in perms

@main.route('/')
@login_required
def index():
    devices = Device.query.order_by(Device.sort_order, Device.id).all()
    return render_template('index.html', devices=devices)

@main.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            ip = request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()
            entry = LoginHistory(username=username, ip_address=ip, timestamp=datetime.utcnow())
            db.session.add(entry)
            db.session.commit()
            return redirect(url_for('main.index'))
        return render_template('login.html', error='Неверные учетные данные')
    return render_template('login.html')

@main.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.login'))

# ---------- Устройства ----------
@main.route('/add_device', methods=['POST'])
@login_required
def add_device():
    # Определяем следующий порядок
    max_sort = db.session.query(db.func.max(Device.sort_order)).scalar()
    if max_sort is None:
        max_sort = 0
    device = Device(
        name=request.form['name'],
        ip=request.form['ip'],
        username=request.form['username'],
        password=request.form['password'],
        enable_password=request.form['enable_password'],
        device_type=request.form.get('device_type', 'epon'),
        sort_order=max_sort + 1
    )
    db.session.add(device)
    db.session.commit()
    return redirect(url_for('main.index'))

@main.route('/edit_device/<int:device_id>', methods=['GET', 'POST'])
@login_required
def edit_device(device_id):
    device = Device.query.get_or_404(device_id)
    if request.method == 'POST':
        device.name = request.form['name']
        device.ip = request.form['ip']
        device.username = request.form['username']
        device.password = request.form['password']
        device.enable_password = request.form['enable_password']
        db.session.commit()
        return redirect(url_for('main.index'))
    return render_template('edit_device.html', device=device)

@main.route('/delete_device/<int:device_id>', methods=['POST'])
@login_required
def delete_device(device_id):
    device = Device.query.get_or_404(device_id)
    db.session.delete(device)
    db.session.commit()
    return redirect(url_for('main.index'))
@main.route('/api/reorder_devices', methods=['POST'])
@login_required
def reorder_devices():
    data = request.get_json()
    ids = data.get('ids', [])
    for index, device_id in enumerate(ids):
        device = Device.query.get(device_id)
        if device:
            device.sort_order = index
    db.session.commit()
    return jsonify({'status': 'ok'})
@main.route('/api/device_status')
@login_required
def device_status():
    from app.auto_poller import device_status as status_dict
    return jsonify(status_dict)



def _get_device_connection(device):
    """Возвращает соединение нужного типа для устройства."""
    if device.device_type == 'gpon':
        from app.gpon_handler import GPONConnection
        return GPONConnection(device.ip, device.username, device.password, device.enable_password)
    elif device.device_type == 'vsol':
        from app.vsol_handler import VSOLConnection
        return VSOLConnection(device.ip, device.username, device.password, device.enable_password)
    else:
        return OLTConnection(device.ip, device.username, device.password, device.enable_password)


@main.route('/device/<int:device_id>')
@login_required
def device_view(device_id):
    device = Device.query.get_or_404(device_id)
    interfaces = []
    onu_statuses = {}
    cached_data = {}
    last_scan_time = None

    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        cursor.execute("SELECT interface, scan_data, scanned_at FROM scan_cache WHERE device_id = %s", (device_id,))
        for row in cursor.fetchall():
            iface = row[0]
            scan_data = json.loads(row[1])
            cached_data[iface] = scan_data
            if last_scan_time is None or row[2] > last_scan_time:
                last_scan_time = row[2]
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Cache load error: {e}", file=sys.stderr)

    olt = _get_device_connection(device)
    snmp_interfaces = olt.get_interfaces_snmp()

    if snmp_interfaces is not None:
        interfaces = snmp_interfaces
    elif hasattr(olt, 'get_interfaces_and_statuses_telnet'):
        if olt.connect():
            interfaces, onu_statuses = olt.get_interfaces_and_statuses_telnet()
            olt.disconnect()
    else:
        print(f"device_view: SNMP вернул None, telnet-метода нет ({device.name})", file=sys.stderr)

    template_name = 'vsol_device.html' if device.device_type == 'vsol' else 'device.html'
    from config import Config
    return render_template(template_name, device=device, interfaces=interfaces,
                         onu_statuses=onu_statuses, cached_data=cached_data,
                         last_scan_time=last_scan_time,
                         signal_has_color=getattr(Config, 'SIGNAL_HAS_COLOR', '#ffffff'),
                         signal_nosignal_color=getattr(Config, 'SIGNAL_NOSIGNAL_COLOR', '#f8d7da'))


# ---------- API сканирования ----------
@main.route('/api/device/<int:device_id>/scan_interface/<path:interface>')
@login_required
def scan_interface(device_id, interface):
    device = Device.query.get_or_404(device_id)
    result, err = _do_scan_impl(device, device_id, interface)
    if err:
        return jsonify({'status': 'error', 'message': err}), 500
    return jsonify(result)


def _do_scan_impl(device, device_id, interface):
    """Скан порта: возвращает (result, None) либо (None, сообщение об ошибке)."""
    import subprocess
    # Быстрая проверка доступности
    try:
        res = subprocess.run(['/bin/ping', '-c', '2', '-W', '1', device.ip], capture_output=True, timeout=5)
        if res.returncode != 0:
            return None, f'Устройство {device.ip} недоступно'
    except Exception:
        return None, f'Устройство {device.ip} недоступно'
    olt = _get_device_connection(device)
    if not olt.connect():
        return None, 'Ошибка подключения'
    # Для VSOL используем одну сессию — не создаём параллельные подключения
    if device.device_type == 'vsol':
        olt.max_parallel = 1

    result_data = olt.get_onu_data_for_scan(interface)
    # Сбрасываем кэш VSOL
    if device.device_type == 'vsol':
        global _vsol_cache_global
        cache_key = f"{device_id}:{interface}"
        _vsol_cache_global.pop(cache_key, None)
    
    # Собираем сигналы ДО отключения только для VSOL (там один запрос на все ONU)
    signals_data = {}
    if isinstance(result_data, dict) and 'onu_macs' in result_data:
        onu_list = result_data['onu_macs']
    else:
        onu_list = result_data[1]
    
    if device.device_type == 'vsol':
        try:
            all_info = olt.get_all_onu_info(interface)
            for onu_id in onu_list:
                signals_data[onu_id] = all_info.get(onu_id)
        except Exception as e:
            print(f"[VSOL] Error getting all info: {e}", file=sys.stderr)
            for onu_id in onu_list:
                signals_data[onu_id] = None
    # Для EPON/GPON — сигналы подгружаются на клиенте асинхронно

    olt.disconnect()

    if isinstance(result_data, dict) and 'onu_macs' in result_data:
        # Формат от GPON (словарь ONU)
        onu_statuses = result_data['onu_statuses']
        onu_macs = result_data['onu_macs']
    else:
        # Формат от EPON (кортеж)
        onu_statuses, onu_macs = result_data

    # Адреса подгружаются на клиенте через /api/device/<id>/address/<intf>/<onu>
    addresses = {}

    result = {
        'status': 'ok',
        'interface': interface,
        'onu_macs': onu_macs,
        'onu_statuses': onu_statuses,
        'addresses': addresses
    }

    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scan_cache (device_id, interface, scan_data, scanned_at)
            VALUES (%s, %s, %s, NOW())
            ON DUPLICATE KEY UPDATE scan_data = VALUES(scan_data), scanned_at = NOW()
        """, (device_id, interface, json.dumps(result)))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Cache save error: {e}", file=sys.stderr)

    # Удаляем ONU, которых больше нет на порту (удалены с OLT)
    try:
        if onu_macs:
            from pymysql import connect as _connect
            _conn = _connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
            _cur = _conn.cursor()
            _ph = ', '.join(['%s'] * len(onu_macs))
            _cur.execute(
                "DELETE FROM onu_signal_history WHERE device_id = %s AND interface = %s "
                "AND onu_id NOT IN (" + _ph + ") AND scanned_at < NOW() - INTERVAL 30 DAY",
                [device_id, interface] + list(onu_macs.keys())
            )
            _removed = _cur.rowcount
            _conn.commit()
            _cur.close()
            _conn.close()
            if _removed:
                print(f"[SCAN] {interface}: removed {_removed} deleted ONU(s) from stats", file=sys.stderr)
    except Exception as e:
        print(f"ONU cleanup error: {e}", file=sys.stderr)

    return result, None


# ---------- Фоновые сканы и живое обновление ----------

_scan_tasks = {}
_scan_tasks_lock = threading.Lock()


@main.route('/api/device/<int:device_id>/scan_start/<path:interface>', methods=['POST'])
@login_required
def scan_start(device_id, interface):
    """Запускает скан порта в фоновом потоке, возвращает task_id."""
    import time as _time
    import uuid as _uuid
    from config import Config
    device = Device.query.get_or_404(device_id)
    if not getattr(Config, 'BACKGROUND_SCANS_ENABLED', True):
        return jsonify({'status': 'error', 'message': 'Фоновые сканы отключены в настройках'}), 400
    task_id = str(_uuid.uuid4())
    from flask import current_app
    app_obj = current_app._get_current_object()
    with _scan_tasks_lock:
        # прибираемся: не более 30 задач в памяти
        if len(_scan_tasks) > 30:
            for k in [k for k, v in _scan_tasks.items() if v.get('status') != 'running'][:len(_scan_tasks) - 30]:
                _scan_tasks.pop(k, None)
        _scan_tasks[task_id] = {'status': 'running', 'interface': interface,
                                'device': device.name,
                                'started_at': _time.strftime('%H:%M:%S')}
    t = threading.Thread(target=_scan_worker, args=(app_obj, task_id, device.id, interface), daemon=True)
    t.start()
    return jsonify({'status': 'ok', 'task_id': task_id})


def _scan_worker(app_obj, task_id, device_id, interface):
    """Фоновый воркер скана порта."""
    with app_obj.app_context():
        try:
            device = Device.query.get(device_id)
            if device is None:
                raise RuntimeError('Устройство не найдено')
            result, err = _do_scan_impl(device, device_id, interface)
            with _scan_tasks_lock:
                if err:
                    _scan_tasks[task_id].update({'status': 'error', 'message': err})
                else:
                    _scan_tasks[task_id].update({'status': 'ok', 'result': result})
        except Exception as e:
            with _scan_tasks_lock:
                _scan_tasks[task_id].update({'status': 'error', 'message': str(e)})


@main.route('/api/device/<int:device_id>/scan_status/<task_id>')
@login_required
def scan_status(device_id, task_id):
    """Статус фонового скана."""
    with _scan_tasks_lock:
        task = _scan_tasks.get(task_id)
        if not task:
            return jsonify({'status': 'error', 'message': 'Задача не найдена'}), 404
        snapshot = dict(task)
    if snapshot.get('status') == 'running':
        snapshot.pop('result', None)
    return jsonify(snapshot)


@main.route('/api/device/<int:device_id>/onu_live')
@login_required
def onu_live(device_id):
    """Свежие сигналы/температуры по всем ONU из истории (без опроса OLT).

    Используется для живого обновления таблиц на страницах устройств.
    """
    Device.query.get_or_404(device_id)
    out = {}
    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cur = conn.cursor()
        cur.execute("""
            SELECT h.interface, h.onu_id, h.signal_db, h.temperature, h.scanned_at, h.mac_onu
            FROM onu_signal_history h
            WHERE h.device_id = %s
              AND h.id IN (
                SELECT MAX(id) FROM onu_signal_history
                WHERE device_id = %s
                GROUP BY interface, onu_id
              )
        """, (device_id, device_id))
        for iface, onu_id, sig, temp, ts, mac in cur.fetchall():
            out[f'{iface}:{onu_id}'] = {
                'signal_db': sig,
                'temperature': temp,
                'scanned_at': ts.strftime('%d.%m %H:%M') if ts else None,
                'mac': mac,
            }
        cur.close(); conn.close()
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    return jsonify({'status': 'ok', 'onus': out})


@main.route('/api/device/<int:device_id>/address/<path:interface>/<onu>')
@login_required
def get_onu_address(device_id, interface, onu):
    """Возвращает адрес абонента для ONU (из биллинга или клиентов)."""
    from app.billing import get_address_from_billing, get_client_name_from_mac
    from pymysql import connect
    device = Device.query.get_or_404(device_id)
    # Берём MAC/SN из scan_cache
    mac_data = None
    try:
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        cursor.execute("SELECT scan_data FROM scan_cache WHERE device_id = %s AND interface = %s", (device_id, interface))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        if row:
            scan = json.loads(row[0])
            mac_data = scan.get('onu_macs', {}).get(onu)
    except Exception as e:
        print(f"Address cache error: {e}", file=sys.stderr)

    if not mac_data:
        return jsonify({'status': 'ok', 'address': '—'})

    mac = ''
    sn = ''
    if isinstance(mac_data, str):
        mac = mac_data
    elif isinstance(mac_data, dict):
        mac = mac_data.get('mac', '')
        sn = mac_data.get('sn', '')

    address = None
    if sn:
        address = get_address_from_billing(sn)
    elif mac:
        address = get_address_from_billing(mac)

    if not address and mac:
        address = get_client_name_from_mac(mac)

    if not address:
        address = 'Удалён или не внесён'
    else:
        # Сохраняем адрес: в последнюю строку истории (для статистики) и в кэш скана
        try:
            conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE onu_signal_history SET address = %s "
                "WHERE device_id = %s AND interface = %s AND onu_id = %s ORDER BY id DESC LIMIT 1",
                (address, device_id, interface, onu)
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Address persist error: {e}", file=sys.stderr)
        try:
            conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
            cursor = conn.cursor()
            cursor.execute("SELECT scan_data FROM scan_cache WHERE device_id = %s AND interface = %s", (device_id, interface))
            row = cursor.fetchone()
            if row:
                scan = json.loads(row[0])
                scan.setdefault('addresses', {})[onu] = address
                cursor.execute(
                    "UPDATE scan_cache SET scan_data = %s WHERE device_id = %s AND interface = %s",
                    (json.dumps(scan, ensure_ascii=False), device_id, interface)
                )
                conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Cache address update error: {e}", file=sys.stderr)

    return jsonify({'status': 'ok', 'address': address})


@main.route('/api/device/<int:device_id>/signal_history/<path:interface>/<onu>')
@login_required
def signal_history(device_id, interface, onu):
    from pymysql import connect
    try:
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT signal_db, temperature, scanned_at
            FROM onu_signal_history
            WHERE device_id = %s AND interface = %s AND onu_id = %s
            ORDER BY scanned_at DESC
            LIMIT 50
        """, (device_id, interface, onu))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        history = []
        for row in reversed(rows):
            history.append({
                'signal': row[0],
                'temperature': row[1],
                'time': row[2].strftime('%d.%m.%Y %H:%M') if row[2] else '',
                'source': 'scan'
            })
        # + точки RX из SNMP-опросчика (onu_rx_history)
        try:
            from app.models import OnuRxHistory
            rx_rows = (OnuRxHistory.query
                       .filter_by(device_id=device_id, iface=interface, onu_id=onu)
                       .order_by(OnuRxHistory.ts)
                       .all())
            rx_pts = [{'signal': float(r.rx_db) if r.rx_db is not None else None,
                       'temperature': None,
                       'time': r.ts.strftime('%d.%m.%Y %H:%M'),
                       'source': 'snmp'} for r in rx_rows]
            history = sorted(history + rx_pts, key=lambda p: p['time'])
        except Exception as e:
            print(f"RX merge error: {e}", file=sys.stderr)
        return jsonify({'status': 'ok', 'history': history})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
@main.route('/api/device/<int:device_id>/info/<path:interface>/<onu>')
@login_required
def onu_info(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    
    if device.device_type == 'vsol':
        global _vsol_cache_global
        cache_key = f"{device_id}:{interface}"
        vsol_cache = _vsol_cache_global
        
        if cache_key not in vsol_cache:
            olt = _get_device_connection(device)
            if not olt.connect():
                return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
            all_info = olt.get_all_onu_info(interface)
            olt.disconnect()
            vsol_cache[cache_key] = all_info
            _vsol_cache_global = vsol_cache
        
        all_info = vsol_cache[cache_key]
        info = all_info.get(onu, {})
        return jsonify({'status': 'ok', 'signal': info.get('signal'), 'temperature': info.get('temperature')})
    
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    info = olt.get_onu_info(interface, onu) or {}
    olt.disconnect()
    
    if info.get('signal'):
        try:
            from pymysql import connect
            conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
            cursor = conn.cursor()
            
            mac_onu = None
            address = None
            cursor.execute("SELECT scan_data FROM scan_cache WHERE device_id = %s AND interface = %s", (device_id, interface))
            row = cursor.fetchone()
            if row:
                cache = json.loads(row[0])
                mac_onu = cache.get('onu_macs', {}).get(onu, '')
                if isinstance(mac_onu, dict):
                    mac_onu = mac_onu.get('sn', '') or mac_onu.get('loid', '') or ''
                address = cache.get('addresses', {}).get(onu, '')
                # Если адреса нет в кэше — ищем в биллинге/клиентах, чтобы он попал в историю
                if not address and mac_onu:
                    try:
                        from app.billing import get_address_from_billing, get_client_name_from_mac
                        address = get_address_from_billing(mac_onu) or get_client_name_from_mac(mac_onu) or 'Не внесён'
                    except Exception as _e:
                        print(f"Address lookup fallback error: {_e}", file=sys.stderr)
            
            cursor.execute("""
                INSERT INTO onu_signal_history (device_id, interface, onu_id, mac_onu, address, signal_db, temperature)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (device_id, interface, onu, mac_onu, address, info['signal'], info.get('temperature')))
            
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Signal history save error: {e}", file=sys.stderr)
    
    return jsonify({'status': 'ok', 'signal': info.get('signal'), 'temperature': info.get('temperature')})

@main.route('/api/device/<int:device_id>/mac/<path:interface>/<onu>')
@login_required
def get_mac(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    
    if device.device_type == 'gpon':
        full_intf = f"GPON{interface}:{onu}"
        from app.gpon_handler import GPONConnection
        from app.vsol_handler import VSOLConnection
        olt = GPONConnection(device.ip, device.username, device.password, device.enable_password)
    else:
        full_intf = f"EPON{interface}:{onu}"
        olt = OLTConnection(device.ip, device.username, device.password, device.enable_password)
    
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    result = olt.get_mac_table(full_intf)
    olt.disconnect()
    if not result:
        return jsonify({'status': 'error', 'message': 'Нет данных'}), 500
    return jsonify({'status': 'ok', 'macs': result.get('macs', []), 'vlan': result.get('vlans', [])})

@main.route('/api/device/<int:device_id>/reboot/<path:interface>/<onu>')
@login_required
def reboot_onu(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    success, message = olt.reboot_onu(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})
@main.route('/api/device/<int:device_id>/monitor_start', methods=['POST'])
@login_required
def monitor_start(device_id):
    import threading, uuid
    from app.auto_poller import create_monitor_task, monitor_onu_worker
    device = Device.query.get_or_404(device_id)
    data = request.get_json()
    interface = data.get('interface')
    onu_id = data.get('onu_id')
    duration = int(data.get('duration', 10))
    interval = int(data.get('interval', 1))
    if not interface or not onu_id:
        return jsonify({'status': 'error', 'message': 'Не указаны интерфейс или ONU'}), 400
    if duration < 1 or interval < 1:
        return jsonify({'status': 'error', 'message': 'Некорректные параметры'}), 400
    
    task_id = str(uuid.uuid4())
    create_monitor_task(task_id, device_id, interface, onu_id, duration, interval)
    
    # Передаём app и device_id для работы в фоновом потоке
    from flask import current_app
    app = current_app._get_current_object()
    t = threading.Thread(target=monitor_onu_worker, args=(app, task_id, device_id, interface, onu_id, duration, interval))
    t.daemon = True
    t.start()
    
    return jsonify({'status': 'ok', 'task_id': task_id, 'message': f'Мониторинг запущен на {duration} мин.'})


@main.route('/api/device/<int:device_id>/monitor_status/<task_id>')
@login_required
def monitor_status(device_id, task_id):
    from app.auto_poller import get_monitor_task, get_monitor_snapshots
    task = get_monitor_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Задача не найдена'}), 404
    now = datetime.utcnow()
    elapsed = int((now - task.start_time).total_seconds()) if task.start_time else 0
    remaining = max(0, task.duration * 60 - elapsed)
    snapshots = get_monitor_snapshots(task_id)
    # Живые данные: последние точки + последний снимок целиком
    history = []
    for s in snapshots[-30:]:
        try:
            d = json.loads(s.data)
        except Exception:
            continue
        ts = s.timestamp.strftime('%H:%M:%S') if s.timestamp else ''
        if d.get('error'):
            history.append({'t': ts, 'error': d['error']})
        else:
            history.append({'t': ts, 'signal': d.get('signal'),
                            'temperature': d.get('temperature'),
                            'lan_state': d.get('lan_state'),
                            'macs': len(d.get('macs') or [])})
    last = None
    if snapshots:
        try:
            last = json.loads(snapshots[-1].data)
        except Exception:
            last = None
    return jsonify({
        'status': task.status,
        'elapsed': elapsed,
        'remaining': remaining,
        'samples': len(snapshots),
        'last_update': snapshots[-1].timestamp.strftime('%H:%M:%S') if snapshots else None,
        'interface': task.interface,
        'onu_id': task.onu_id,
        'duration': task.duration,
        'interval': task.interval,
        'history': history,
        'last': last
    })


@main.route('/api/device/<int:device_id>/monitor_stop/<task_id>', methods=['POST'])
@login_required
def monitor_stop(device_id, task_id):
    """Останавливает выполняющийся мониторинг ONU."""
    from app.auto_poller import get_monitor_task, update_monitor_task
    task = get_monitor_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Задача не найдена'}), 404
    if task.status != 'running':
        return jsonify({'status': 'error', 'message': f'Мониторинг уже завершён (статус: {task.status})'}), 400
    update_monitor_task(task_id, status='interrupted', end_time=datetime.utcnow())
    print(f"[MONITOR] Task {task_id} stopped by user", file=sys.stderr)
    return jsonify({'status': 'ok', 'message': 'Мониторинг остановлен'})


@main.route('/api/monitoring/tasks')
@login_required
def monitoring_tasks():
    from app.auto_poller import get_all_monitor_tasks, get_monitor_snapshots
    tasks = []
    for task in get_all_monitor_tasks():
        now = datetime.utcnow()
        elapsed = int((now - task.start_time).total_seconds()) if task.start_time else 0
        remaining = max(0, task.duration * 60 - elapsed) if task.status == 'running' else 0
        snapshots = get_monitor_snapshots(task.task_id)
        device = Device.query.get(task.device_id)
        task_info = {
            'task_id': task.task_id,
            'device_id': task.device_id,
            'device_name': device.name if device else '',
            'device_ip': device.ip if device else '',
            'interface': task.interface,
            'onu_id': task.onu_id,
            'status': task.status,
            'start_time': task.start_time.timestamp() if task.start_time else None,
            'elapsed': elapsed,
            'remaining': remaining,
            'duration': task.duration,
            'interval': task.interval,
            'samples': len(snapshots),
            'has_result': task.result is not None
        }
        tasks.append(task_info)
    tasks.sort(key=lambda x: x['start_time'] or 0, reverse=True)
    return jsonify({'status': 'ok', 'tasks': tasks})



@main.route('/api/monitor/delete/<task_id>', methods=['POST'])
@login_required
def delete_monitor_task(task_id):
    from app.models import MonitorTask, MonitorSnapshot, db
    # Удаляем снимки
    MonitorSnapshot.query.filter_by(task_id=task_id).delete()
    # Удаляем задачу
    task = MonitorTask.query.filter_by(task_id=task_id).first()
    if task:
        db.session.delete(task)
        db.session.commit()
        return jsonify({'status': 'ok', 'message': 'Задача удалена'})
    return jsonify({'status': 'error', 'message': 'Задача не найдена'}), 404
@main.route('/api/device/<int:device_id>/monitor_result/<task_id>')
@login_required
def monitor_result(device_id, task_id):
    from app.auto_poller import get_monitor_task
    task = get_monitor_task(task_id)
    if not task:
        return jsonify({'status': 'error', 'message': 'Задача не найдена'}), 404
    if task.status not in ('done', 'interrupted'):
        return jsonify({'status': 'error', 'message': 'Мониторинг ещё выполняется'}), 400
    result = json.loads(task.result) if task.result else None
    return jsonify({'status': 'ok', 'result': result})


@main.route('/api/device/<int:device_id>/clear_mac/<path:interface>/<onu>')
@login_required
def clear_mac(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    try:
        success, message = olt.clear_mac_table(interface, onu)
    except AttributeError:
        olt.disconnect()
        return jsonify({'status': 'error', 'message': 'Функция доступна только для BDCOM EPON/GPON'}), 400
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})


@main.route('/api/device/<int:device_id>/find_mac', methods=['POST'])
@login_required
def find_mac_api(device_id):
    device = Device.query.get_or_404(device_id)
    data = request.get_json()
    mac = data.get('mac', '').strip()
    if not mac:
        return jsonify({'status': 'error', 'message': 'Укажите MAC-адрес'}), 400
    if device.device_type == 'vsol':
        # VSOL: мгновенный поиск через SNMP Q-BRIDGE
        try:
            from app import snmp_lib
            port_name, err = snmp_lib.find_mac_vsol(device.ip, mac)
        except Exception as e:
            port_name, err = None, f'Ошибка SNMP: {e}'
        if err is None:
            return jsonify({'status': 'ok', 'result': {
                'mac': snmp_lib.normalize_mac(mac), 'port': port_name,
                'interface': port_name, 'onu_id': '', 'vlan': '-',
                'source': 'snmp'}})
        return jsonify({'status': 'error', 'message': err})
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    try:
        result, error = olt.find_mac(mac)
    except AttributeError:
        olt.disconnect()
        return jsonify({'status': 'error', 'message': 'Поиск MAC доступен только для BDCOM EPON/GPON'}), 400
    olt.disconnect()
    if error:
        return jsonify({'status': 'error', 'message': error})
    return jsonify({'status': 'ok', 'result': result})


# ---------- SNMP-опросник: страницы и API ----------

_snmp_scan_cache = {}  # device_id: (time.time(), data)


@main.route('/snmp_scan')
@login_required
def snmp_scan():
    devices = Device.query.order_by(Device.sort_order, Device.id).all()
    return render_template('snmp_scan.html', devices=devices)


@main.route('/snmp_scan/<int:device_id>')
@login_required
def snmp_scan_device(device_id):
    device = Device.query.get_or_404(device_id)
    return render_template('snmp_device.html', device=device)


def _sortkey_onu(k):
    port, _, onu = k.partition(':')
    try:
        pp = [int(x) for x in port.split('/')]
    except ValueError:
        pp = [9999]
    return (pp, int(onu) if onu.isdigit() else 0)


def _collect_snmp_scan(device):
    from app import snmp_lib
    from app.billing import get_client_name_from_mac
    host = device.ip
    out = {'device': device.name, 'type': device.device_type,
           'onus': [], 'ports': [], 'note': ''}

    if device.device_type not in ('gpon', 'vsol'):
        # EPON: полная таблица ONU по SNMP
        macs = snmp_lib.get_epon_onu_macs(host, 90)
        status = snmp_lib.get_epon_onu_status(host, 120)
        tx = snmp_lib.get_epon_onu_tx(host, 90)
        rx = snmp_lib.get_epon_onu_rx(host, 120)
        # Адреса — та же технология, что в telnet-опросе: берём из scan_cache
        # (туда их кладёт биллинг-поиск по MAC), отсутствующие добираем из биллинга
        cached_addr = {}
        from pymysql import connect as _conn
        import json as _json
        conn = _conn(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cur = conn.cursor()
        cur.execute("SELECT interface, scan_data FROM scan_cache WHERE device_id=%s", (device.id,))
        for row in cur.fetchall():
            try:
                sd = _json.loads(row[1])
                for onu, addr in (sd.get('addresses') or {}).items():
                    cached_addr[f"{row[0]}:{onu}"] = addr
            except Exception:
                pass
        cur.close(); conn.close()

        keys = sorted(set(list(macs) + list(status) + list(rx) + list(tx)), key=_sortkey_onu)
        for key in keys:
            mac = macs.get(key, '')
            addr = cached_addr.get(key)
            if addr is None and mac:
                try:
                    addr = get_address_from_billing(mac) or get_client_name_from_mac(mac) or 'Не внесён'
                except Exception:
                    addr = 'Ошибка биллинга'
            up = bool(status.get(key))
            out['onus'].append({
                'iface': key, 'mac': mac,
                'address': addr if addr is not None else '—',
                'up': up,
                'tx': tx.get(key) if up else None,
                'rx': rx.get(key) if up else None})
        if out['onus']:
            offline = _snmp_record_poll(device.id, out['onus'])
            for o in out['onus']:
                o['offline'] = offline.get(o['iface'])
        out['note'] = f'ONU: {len(out["onus"])}' if out['onus'] else 'ONU не найдены'
    else:
        # GPON/VSOL: ONU-уровень по SNMP прошивка не отдаёт — порты + последний срез трафика
        out['note'] = 'Статусы/сигналы ONU по SNMP для этого типа недоступны (прошивка). Показаны порты.'
        names = snmp_lib.get_ifdescr_map(host, 45)
        oper = snmp_lib.get_port_oper_status(host, 60)
        from pymysql import connect as _conn
        conn = _conn(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cur = conn.cursor()
        cur.execute("""SELECT t.ifname, t.in_bps, t.out_bps FROM port_traffic t
            JOIN (SELECT ifname, MAX(ts) m FROM port_traffic WHERE device_id=%s GROUP BY ifname) x
              ON t.device_id=%s AND t.ifname=x.ifname AND t.ts=x.m""", (device.id, device.id))
        tr = {r[0]: (r[1] or 0, r[2] or 0) for r in cur.fetchall()}
        cur.close(); conn.close()
        for name in sorted(names.values()):
            if ':' in name:
                continue
            inb, outb = tr.get(name, (0, 0))
            out['ports'].append({'iface': name, 'up': oper.get(name),
                                 'in_mbps': round(inb / 1e6, 1),
                                 'out_mbps': round(outb / 1e6, 1)})
    return out


@main.route('/api/snmp_scan/<int:device_id>')
@login_required
def snmp_scan_api(device_id):
    device = Device.query.get_or_404(device_id)
    force = request.args.get('force') == '1'
    import time as _time
    ts, data = _snmp_scan_cache.get(device_id, (0, None))
    now = _time.time()
    if not force and data is not None and now - ts < 300:
        data = dict(data)
        data['cached'] = True
        data['age'] = int(now - ts)
        return jsonify({'status': 'ok', 'data': data})
    try:
        data = _collect_snmp_scan(device)
        _snmp_scan_cache[device_id] = (now, data)
        return jsonify({'status': 'ok', 'data': data})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _snmp_db():
    import pymysql
    return pymysql.connect(host='localhost', user='oltuser',
                           password='oltpassword', database='oltmanager')


def _snmp_record_poll(device_id, onus):
    """История опросов (последние 50) + offline-таймеры. Возвращает {iface: since_ts}."""
    import time as _t
    now = int(_t.time())
    snap = {o['iface']: [1 if o.get('up') else 0,
                         o['rx'] if isinstance(o.get('rx'), (int, float)) else None]
            for o in onus}
    up = sum(1 for v in snap.values() if v[0] == 1)
    conn = _snmp_db()
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS snmp_scan_history (
        id INT AUTO_INCREMENT PRIMARY KEY,
        device_id INT NOT NULL, ts DATETIME NOT NULL,
        onu_total INT, onu_up INT, onu_down INT, data LONGTEXT,
        INDEX devts (device_id, ts))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS snmp_offline_since (
        device_id INT NOT NULL, iface VARCHAR(32) NOT NULL,
        since_ts BIGINT NOT NULL, PRIMARY KEY (device_id, iface))""")
    cur.execute("INSERT INTO snmp_scan_history (device_id, ts, onu_total, onu_up, onu_down, data) "
                "VALUES (%s, FROM_UNIXTIME(%s), %s, %s, %s, %s)",
                (device_id, now, len(snap), up, len(snap) - up, json.dumps(snap)))
    cur.execute("DELETE FROM snmp_scan_history WHERE device_id=%s AND id NOT IN "
                "(SELECT id FROM (SELECT id FROM snmp_scan_history WHERE device_id=%s "
                "ORDER BY id DESC LIMIT 50) x)", (device_id, device_id))
    offline = {}
    for o in onus:
        k = o['iface']
        if o.get('up') is False:
            cur.execute("SELECT since_ts FROM snmp_offline_since WHERE device_id=%s AND iface=%s",
                        (device_id, k))
            row = cur.fetchone()
            if row:
                offline[k] = int(row[0])
            else:
                cur.execute("INSERT INTO snmp_offline_since (device_id, iface, since_ts) "
                            "VALUES (%s, %s, %s)", (device_id, k, now))
                offline[k] = now
        else:
            cur.execute("DELETE FROM snmp_offline_since WHERE device_id=%s AND iface=%s",
                        (device_id, k))
    conn.commit()
    cur.close(); conn.close()
    return offline


def _snmp_offline_map(device_id):
    conn = _snmp_db()
    cur = conn.cursor()
    cur.execute("SELECT iface, since_ts FROM snmp_offline_since WHERE device_id=%s", (device_id,))
    res = {r[0]: int(r[1]) for r in cur.fetchall()}
    cur.close(); conn.close()
    return res


# ---------- SNMP-опросник: SSE-стрим живого опроса ----------

def _sse_pack(event, obj):
    return 'event: ' + event + '\ndata: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'


def _scan_addr_cache(device_id):
    """Адреса из scan_cache (решены биллингом при telnet-опросах)."""
    import pymysql
    cached = {}
    conn = pymysql.connect(host='localhost', user='oltuser',
                           password='oltpassword', database='oltmanager')
    cur = conn.cursor()
    cur.execute("SELECT interface, scan_data FROM scan_cache WHERE device_id=%s", (device_id,))
    for row in cur.fetchall():
        try:
            sd = json.loads(row[1])
            for onu, addr in (sd.get('addresses') or {}).items():
                cached[f"{row[0]}:{onu}"] = addr
        except Exception:
            pass
    cur.close(); conn.close()
    return cached


def _scan_billing_addr(mac):
    from app.billing import get_address_from_billing, get_client_name_from_mac
    try:
        return get_address_from_billing(mac) or get_client_name_from_mac(mac) or 'Не внесён'
    except Exception:
        return 'Ошибка биллинга'


def _scan_walk_proc(host, oid):
    import subprocess
    return subprocess.Popen(
        ['/usr/bin/snmpbulkwalk', '-v2c', '-c', 'public', '-On', host, oid],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def _scan_parse_line(line):
    """(idx, typ, val) из строки snmpbulkwalk -On; None если мусор."""
    from app import snmp_lib
    if ' = ' not in line or 'No Such' in line:
        return None
    oid, raw = line.split(' = ', 1)
    tail = oid.rsplit('.', 1)[1]
    if not tail.isdigit():
        return None
    typ, val = snmp_lib.parse_value(raw.strip())
    return int(tail), typ, val


def _stream_epon(device):
    import select, os as _os, time as _t
    from app import snmp_lib
    host = device.ip

    names = snmp_lib.get_ifdescr_map(host, 45)
    idx_key = {}
    for i, n in names.items():
        k = snmp_lib._onu_name_to_key(n or '')
        if k:
            idx_key[i] = k
    cached_addr = _scan_addr_cache(device.id)

    init_rows = [{'iface': idx_key[i], 'address': cached_addr.get(idx_key[i])}
                 for i in sorted(idx_key)]
    yield _sse_pack('init', {'type': 'epon', 'total': len(init_rows), 'onus': init_rows})

    procs = {
        'mac': _scan_walk_proc(host, snmp_lib.OID_EPON_ONU_MAC),
        'st': _scan_walk_proc(host, snmp_lib.OID_EPON_ONU_STATUS),
        'tx': _scan_walk_proc(host, snmp_lib.OID_EPON_ONU_TX),
        'rx': _scan_walk_proc(host, snmp_lib.OID_EPON_ONU_RX),
    }
    bufs = {k: b'' for k in procs}
    eof = {k: False for k in procs}
    data = {'mac': {}, 'st': {}, 'tx': {}, 'rx': {}}
    addr_cache = {}
    last_sig = {}
    last_emit = 0.0
    t_start = _t.time()
    last_out = {k: t_start for k in procs}
    last_tick = 0.0
    walk_names = {'mac': 'MAC', 'st': 'статусы', 'tx': 'TX', 'rx': 'RX'}
    deadline = _t.time() + 240

    def row_of(i):
        k = idx_key.get(i)
        if not k:
            return None
        mac = data['mac'].get(i) or ''
        addr = cached_addr.get(k)
        if addr is None and mac:
            if k not in addr_cache:
                addr_cache[k] = _scan_billing_addr(mac)
            addr = addr_cache[k]
        up = data['st'].get(i)
        # у упавшей ONU оптика в таблице — устаревшие остатки BDCOM: не показываем
        if up is False:
            return {'iface': k, 'mac': mac, 'address': addr,
                    'up': up, 'tx': None, 'rx': None}
        return {'iface': k, 'mac': mac, 'address': addr, 'up': up,
                'tx': data['tx'].get(i), 'rx': data['rx'].get(i)}

    while not all(eof.values()):
        fdmap = {procs[k].stdout.fileno(): k for k in procs if not eof[k]}
        try:
            ready, _, _ = select.select(list(fdmap), [], [], 1.0)
        except Exception:
            ready = []
        for fd in ready:
            k = fdmap[fd]
            try:
                chunk = _os.read(fd, 65536)
            except OSError:
                chunk = b''
            if not chunk:
                eof[k] = True
                continue
            last_out[k] = _t.time()
            bufs[k] += chunk
            while b'\n' in bufs[k]:
                line, bufs[k] = bufs[k].split(b'\n', 1)
                pl = _scan_parse_line(line.decode('utf-8', 'replace'))
                if not pl:
                    continue
                idx, typ, val = pl
                if k == 'mac' and typ == 'Hex-STRING':
                    m = snmp_lib.hex_to_mac(val)
                    if m:
                        data['mac'][idx] = m
                elif k == 'st' and typ == 'INTEGER' and val in (0, 1):
                    data['st'][idx] = (val == 1)
                elif k == 'tx' and typ == 'INTEGER' and isinstance(val, int):
                    data['tx'][idx] = val / 10.0
                elif k == 'rx' and typ == 'INTEGER' and isinstance(val, int):
                    data['rx'][idx] = val / 10.0
        now2 = _t.time()
        # walk молчит 45с — убиваем, опрос продолжается по остальным
        for k in procs:
            if not eof[k] and now2 - last_out[k] > 45:
                try:
                    procs[k].kill()
                except Exception:
                    pass
                eof[k] = True
        if now2 > deadline:
            for p in procs.values():
                try:
                    p.kill()
                except Exception:
                    pass
            break
        if now2 - last_tick > 5:
            last_tick = now2
            got = len(set(list(data['mac']) + list(data['st']) +
                          list(data['tx']) + list(data['rx'])))
            yield _sse_pack('tick', {'elapsed': int(now2 - t_start), 'got': got,
                                     'waiting': [walk_names[k] for k in procs if not eof[k]]})
        rows, seen = [], set(list(data['mac']) + list(data['st']) +
                             list(data['tx']) + list(data['rx']))
        for i in seen:
            sig = (data['mac'].get(i), data['st'].get(i),
                   data['tx'].get(i), data['rx'].get(i))
            if last_sig.get(i) == sig:
                continue
            r = row_of(i)
            if r:
                rows.append(r)
        now = _t.time()
        if rows and (now - last_emit > 0.7 or all(eof.values())):
            for r in rows:
                for i, k in idx_key.items():
                    if k == r['iface']:
                        last_sig[i] = (data['mac'].get(i), data['st'].get(i),
                                       data['tx'].get(i), data['rx'].get(i))
            last_emit = now
            yield _sse_pack('rows', {'rows': rows})

    for p in procs.values():
        try:
            p.kill()
        except Exception:
            pass
    incomplete = [walk_names[k] for k in procs if not eof[k]]
    final_rows = [row_of(i) for i in sorted(idx_key)]
    final_rows = [r for r in final_rows if r]
    note = f'ONU: {len(final_rows)}'
    if incomplete:
        note += ' · НЕПОЛНЫЙ: не ответили ' + ', '.join(incomplete)
    out = {'device': device.name, 'type': device.device_type,
           'onus': final_rows, 'ports': [], 'note': note}
    offline = _snmp_record_poll(device.id, final_rows)
    for o in final_rows:
        o['offline'] = offline.get(o['iface'])
    _snmp_scan_cache[device.id] = (_t.time(), out)
    yield _sse_pack('done', {'note': note, 'total': len(final_rows),
                             'offline': offline, 'partial': incomplete})


def _stream_ports(device):
    data = _collect_snmp_scan(device)
    _snmp_scan_cache[device.id] = (time.time(), data)
    yield _sse_pack('init', {'type': 'ports', 'total': len(data['ports']),
                             'ports': data['ports']})
    yield _sse_pack('done', {'note': data['note'], 'total': len(data['ports'])})


@main.route('/api/snmp_scan_history/<int:device_id>')
@login_required
def snmp_scan_history_api(device_id):
    Device.query.get_or_404(device_id)
    conn = _snmp_db()
    cur = conn.cursor()
    cur.execute("""SELECT id, UNIX_TIMESTAMP(ts), onu_total, onu_up, onu_down, data
        FROM snmp_scan_history WHERE device_id=%s ORDER BY id DESC LIMIT 50""", (device_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    polls, prev = [], None
    for rid, ts, total, up, down, data in reversed(rows):
        snap = json.loads(data or '{}')
        item = {'id': rid, 'ts': ts, 'total': total, 'up': up, 'down': down,
                'gone': [], 'back': [], 'worse': [], 'better': [], 'new': []}
        if prev is not None:
            for k, v in snap.items():
                pv = prev.get(k)
                if pv is None:
                    item['new'].append(k)
                    continue
                if pv[0] == 1 and v[0] == 0:
                    item['gone'].append(k)
                elif pv[0] == 0 and v[0] == 1:
                    item['back'].append(k)
                elif pv[0] == 1 and v[0] == 1 and v[1] is not None and pv[1] is not None:
                    d = round(v[1] - pv[1], 1)
                    if d <= -3:
                        item['worse'].append({'iface': k, 'delta': d})
                    elif d >= 3:
                        item['better'].append({'iface': k, 'delta': d})
        polls.append(item)
        prev = snap
    polls.reverse()
    return jsonify({'status': 'ok', 'polls': polls})


@main.route('/api/snmp_scan_rxhist/<int:device_id>')
@login_required
def snmp_scan_rxhist(device_id):
    """История RX для всех ONU (спарклайны): telnet-сканы + SNMP, месяц, ~100 точек/ONU."""
    Device.query.get_or_404(device_id)
    conn = _snmp_db()
    cur = conn.cursor()
    hist = {}
    for sql in (
        "SELECT iface, onu_id, MIN(UNIX_TIMESTAMP(ts)), AVG(rx_db) FROM onu_rx_history "
        "WHERE device_id=%s AND ts > NOW() - INTERVAL 30 DAY "
        "GROUP BY iface, onu_id, FLOOR(UNIX_TIMESTAMP(ts)/3600)",
        "SELECT interface, onu_id, MIN(UNIX_TIMESTAMP(scanned_at)), AVG(signal_db) FROM onu_signal_history "
        "WHERE device_id=%s AND scanned_at > NOW() - INTERVAL 30 DAY "
        "GROUP BY interface, onu_id, FLOOR(UNIX_TIMESTAMP(scanned_at)/3600)",
    ):
        cur.execute(sql, (device_id,))
        for iface, onu, ts, rx in cur.fetchall():
            if rx is None or ts is None:
                continue
            try:
                rx_f = float(rx)
                if not (-60.0 < rx_f < 0.0):
                    continue  # сентинелы (-6553.5) и нули — мусор, не сигнал
                onu_i = int(str(onu).strip())
            except (ValueError, TypeError):
                continue
            hist.setdefault(f"{iface}:{onu_i}", []).append((float(ts), rx_f))
    cur.close(); conn.close()
    MAXPTS = 100
    for k in hist:
        pts = sorted(hist[k])
        if len(pts) > MAXPTS:
            step = (len(pts) - 1) / (MAXPTS - 1)
            pts = [pts[int(i * step)] for i in range(MAXPTS - 1)] + [pts[-1]]
        hist[k] = [list(p) for p in pts]
    return jsonify({'status': 'ok', 'hist': hist})


@main.route('/api/snmp_scan_stream/<int:device_id>')
@login_required
def snmp_scan_stream(device_id):
    from flask import Response, stream_with_context
    device = Device.query.get_or_404(device_id)

    def gen():
        try:
            if device.device_type not in ('gpon', 'vsol'):
                yield from _stream_epon(device)
            else:
                yield from _stream_ports(device)
        except Exception as e:
            yield _sse_pack('error_evt', {'message': str(e)})

    resp = Response(stream_with_context(gen()), mimetype='text/event-stream')
    resp.headers['Cache-Control'] = 'no-cache'
    resp.headers['X-Accel-Buffering'] = 'no'
    return resp


# ---------- API SNMP-данных: трафик/здоровье/RX/дашборд ----------

@main.route('/api/device/<int:device_id>/traffic')
@login_required
def device_traffic_api(device_id):
    Device.query.get_or_404(device_id)
    try:
        hours = float(request.args.get('hours', 6))
    except (TypeError, ValueError):
        hours = 6.0
    since = datetime.utcnow() - timedelta(hours=hours)
    from app.models import PortTraffic
    rows = (PortTraffic.query
            .filter(PortTraffic.device_id == device_id, PortTraffic.ts >= since)
            .order_by(PortTraffic.ts)
            .all())
    series = {}
    for r in rows:
        series.setdefault(r.ifname, []).append({
            't': r.ts.strftime('%d.%m %H:%M'),
            'in': round((r.in_bps or 0) / 1e6, 2),
            'out': round((r.out_bps or 0) / 1e6, 2)})
    return jsonify({'status': 'ok', 'series': series})


@main.route('/api/device/<int:device_id>/health')
@login_required
def device_health_api(device_id):
    Device.query.get_or_404(device_id)
    try:
        hours = float(request.args.get('hours', 24))
    except (TypeError, ValueError):
        hours = 24.0
    since = datetime.utcnow() - timedelta(hours=hours)
    from app.models import DeviceHealth
    rows = (DeviceHealth.query
            .filter(DeviceHealth.device_id == device_id, DeviceHealth.ts >= since)
            .order_by(DeviceHealth.ts)
            .all())
    fmt = lambda r: r.ts.strftime('%d.%m %H:%M')
    return jsonify({'status': 'ok',
                    'cpu': [{'t': fmt(r), 'v': r.cpu} for r in rows if r.cpu is not None],
                    'uptime': [{'t': fmt(r), 'v': r.uptime_s} for r in rows if r.uptime_s is not None]})


@main.route('/api/device/<int:device_id>/onu_rx')
@login_required
def onu_rx_api(device_id):
    Device.query.get_or_404(device_id)
    from app.models import OnuRxHistory
    rows = (db.session.query(OnuRxHistory.iface, OnuRxHistory.onu_id,
                             OnuRxHistory.rx_db, OnuRxHistory.ts)
            .filter(OnuRxHistory.device_id == device_id)
            .order_by(OnuRxHistory.iface, OnuRxHistory.onu_id, OnuRxHistory.ts)
            .all())
    latest = {}
    for iface, onu, rx, ts in rows:
        latest[f'{iface}:{onu}'] = {'iface': iface, 'onu': onu,
                                    'rx': float(rx) if rx is not None else None,
                                    'ts': ts.strftime('%d.%m %H:%M')}
    return jsonify({'status': 'ok', 'onus': list(latest.values())})


@main.route('/api/dashboard')
@login_required
def api_dashboard():
    from pymysql import connect
    out = {'status': 'ok', 'devices': {}, 'onus': {}}
    try:
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cur = conn.cursor()
        cur.execute("""
            SELECT d.id,
                   (SELECT h.cpu FROM device_health h WHERE h.device_id=d.id ORDER BY h.ts DESC LIMIT 1),
                   (SELECT h.uptime_s FROM device_health h WHERE h.device_id=d.id ORDER BY h.ts DESC LIMIT 1)
            FROM device d""")
        for did, cpu, up in cur.fetchall():
            out['devices'][str(did)] = {
                'cpu': cpu, 'uptime_s': up, 'in_mbps': 0.0, 'out_mbps': 0.0}
        cur.execute("""
            SELECT t.device_id, SUM(t.in_bps), SUM(t.out_bps)
            FROM port_traffic t
            JOIN (SELECT device_id, MAX(ts) m FROM port_traffic GROUP BY device_id) x
              ON t.device_id=x.device_id AND t.ts=x.m
            GROUP BY t.device_id""")
        for did, inb, outb in cur.fetchall():
            k = str(did)
            if k in out['devices']:
                out['devices'][k]['in_mbps'] = round(float(inb or 0) / 1e6, 1)
                out['devices'][k]['out_mbps'] = round(float(outb or 0) / 1e6, 1)
        # ONU: зарегистрировано / онлайн — точный живой состав по onu_status_log (EPON):
        # берём последнюю запись каждой ONU (по MAX(id)), чтобы сумма не задваивалась
        cur.execute("""
            SELECT o.device_id, COUNT(*) AS reg, SUM(o.online) AS onl
            FROM onu_status_log o
            JOIN (SELECT device_id, iface, onu_id, MAX(id) mid FROM onu_status_log
                  GROUP BY device_id, iface, onu_id) x ON o.id = x.mid
            GROUP BY o.device_id""")
        for did, reg, onl in cur.fetchall():
            out['onus'][str(did)] = {'total': int(reg or 0), 'online': int(onl or 0)}
        # Устройства без живого состава (GPON/VSOL): «зарегистрировано» из последнего опроса
        cur.execute("""
            SELECT s.device_id,
                   SUM(JSON_LENGTH(JSON_EXTRACT(s.scan_data, '$.onu_macs')))
            FROM scan_cache s GROUP BY s.device_id""")
        for did, reg in cur.fetchall():
            k = str(did)
            if k not in out['onus']:
                out['onus'][k] = {'total': int(reg or 0), 'online': None}
        # Если живого состава нет — онлайн берём из последнего SNMP-скана устройства
        cur.execute("""
            SELECT h.device_id, h.onu_total, h.onu_up, h.onu_down
            FROM snmp_scan_history h
            JOIN (SELECT device_id, MAX(id) mid FROM snmp_scan_history GROUP BY device_id) x
              ON h.id = x.mid""")
        for did, tot, up, down in cur.fetchall():
            k = str(did)
            if k not in out['onus']:
                out['onus'][k] = {'total': int(tot or 0), 'online': int(up or 0)}
            elif out['onus'][k].get('online') is None:
                out['onus'][k]['total'] = int(tot or 0)
                out['onus'][k]['online'] = int(up or 0)
        # offline = total - online (когда онлайн известен)
        for _o in out['onus'].values():
            if isinstance(_o.get('online'), int):
                _o['offline'] = max(0, _o['total'] - _o['online'])
        # ONU с критическим RX (< -27 dBm)
        cur.execute("""
            SELECT o.device_id, COUNT(*)
            FROM onu_rx_history o
            JOIN (SELECT device_id, iface, onu_id, MAX(ts) m FROM onu_rx_history
                  GROUP BY device_id, iface, onu_id) x
              ON o.device_id=x.device_id AND o.iface=x.iface AND o.onu_id=x.onu_id AND o.ts=x.m
            WHERE o.rx_db < -27
            GROUP BY o.device_id""")
        for did, cnt2 in cur.fetchall():
            if str(did) in out['devices']:
                out['devices'][str(did)]['rx_bad'] = int(cnt2)
        cur.close(); conn.close()
        return jsonify(out)
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@main.route('/api/device/<int:device_id>/interface_shutdown', methods=['POST'])
@login_required
def interface_shutdown(device_id):
    import threading, time
    device = Device.query.get_or_404(device_id)
    data = request.get_json()
    interface = data.get('interface')
    seconds = int(data.get('seconds', 10))
    if not interface or seconds < 1:
        return jsonify({'status': 'error', 'message': 'Некорректные параметры'}), 400
    
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    
    success, msg = olt.shutdown_interface(interface)
    olt.disconnect()
    
    if not success:
        return jsonify({'status': 'error', 'message': msg}), 500
    
    # Планируем включение через N секунд
    def enable_later():
        # Переподключаемся и включаем интерфейс
        olt2 = _get_device_connection(device)
        if olt2.connect():
            olt2.enable_interface(interface)
            olt2.disconnect()
    
    timer = threading.Timer(seconds, enable_later)
    timer.daemon = True
    timer.start()
    
    return jsonify({'status': 'ok', 'message': f'Интерфейс {interface} выключен, включится через {seconds} сек.'})

@main.route('/api/device/<int:device_id>/running_config/<path:interface>/<onu>')
@login_required
def running_config(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    config = olt.get_running_config(interface, onu)
    olt.disconnect()
    if config is None:
        return jsonify({'status': 'error', 'message': 'Не удалось получить конфигурацию'}), 500
    return jsonify({'status': 'ok', 'config': config})

@main.route('/api/device/<int:device_id>/bulk_reboot', methods=['POST'])
@login_required
def bulk_reboot(device_id):
    data = request.get_json()
    interface = data.get('interface')
    onu_ids = data.get('onus', [])
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    results = []
    for onu in onu_ids:
        success, msg = olt.reboot_onu(interface, onu)
        results.append({'onu': onu, 'success': success, 'message': msg})
    olt.disconnect()
    return jsonify({'status': 'ok', 'results': results})


@main.route('/api/device/<int:device_id>/bulk_delete', methods=['POST'])
@login_required
def bulk_delete(device_id):
    data = request.get_json()
    interface = data.get('interface')
    onu_ids = data.get('onus', [])
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    results = []
    total = len(onu_ids)
    for idx, onu in enumerate(onu_ids):
        # Сохраняем конфиг только после последнего ONU
        save = (idx == total - 1)
        success, msg = olt.delete_onu(interface, onu, save=save)
        results.append({'onu': onu, 'success': success, 'message': msg})
    olt.disconnect()
    return jsonify({'status': 'ok', 'results': results})


@main.route('/api/device/<int:device_id>/delete/<path:interface>/<onu>')
@login_required
def delete_onu(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    success, message = olt.delete_onu(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})

@main.route('/api/device/<int:device_id>/lan/<path:interface>/<onu>')
@login_required
def lan_state(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    state = olt.get_lan_state(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok', 'state': state})

@main.route('/api/device/<int:device_id>/set1g/<path:interface>/<onu>')
@login_required
def set_1g(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    success, message = olt.set_1g_sla(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})

@main.route('/api/device/<int:device_id>/blacklist/<path:interface>/<onu>/<path:mac>')
@login_required
def blacklist_onu(device_id, interface, onu, mac):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения к OLT'}), 500
    success, message = olt.blacklist_onu(interface, onu, mac)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})

# ---------- VSOL специфичные API ----------
@main.route('/api/device/<int:device_id>/vsol_mac/<path:interface>/<onu>')
@login_required
def vsol_mac(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    success, message, macs, vlan = olt.get_onu_mac_table(interface, onu)
    olt.disconnect()
    return jsonify({
        'status': 'ok' if success else 'error', 
        'message': message,
        'macs': macs,
        'vlan': vlan
    })

@main.route('/api/device/<int:device_id>/vsol_phy/<path:interface>/<onu>')
@login_required
def vsol_phy(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    success, message = olt.get_onu_link_state(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})

@main.route('/api/device/<int:device_id>/vsol_reset/<path:interface>/<onu>')
@login_required
def vsol_reset(device_id, interface, onu):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    success, message = olt.reset_onu(interface, onu)
    olt.disconnect()
    return jsonify({'status': 'ok' if success else 'error', 'message': message})

# ---------- Статистика сигналов ----------
@main.route('/device/<int:device_id>/signal_stats')
@login_required
def signal_stats(device_id):
    device = Device.query.get_or_404(device_id)
    filter_interface = request.args.get('interface', '')
    
    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        
        sql = """
            SELECT t1.interface, t1.onu_id, t1.mac_onu, t1.address, t1.signal_db, t1.temperature, t1.scanned_at
            FROM onu_signal_history t1
            WHERE t1.device_id = %s
            AND t1.scanned_at = (
                SELECT MAX(t2.scanned_at) FROM onu_signal_history t2
                WHERE t2.device_id = t1.device_id AND t2.interface = t1.interface AND t2.onu_id = t1.onu_id
            )
        """
        params = [device_id]

        if filter_interface:
            sql += " AND t1.interface = %s"
            params.append(filter_interface)

        sql += " ORDER BY t1.interface, CAST(t1.onu_id AS UNSIGNED)"
        cursor.execute(sql, params)
        
        signals = []
        for row in cursor.fetchall():
            signals.append({
                'interface': row[0],
                'onu_id': row[1],
                'mac_onu': row[2],
                'address': row[3],
                'signal_db': row[4],
                'temperature': row[5],
                'scanned_at': row[6],
                'days_offline': None,
                'last_online': None
            })
        
        # Расчёт дней офлайн для каждого ONU
        from datetime import datetime
        for s in signals:
            if s['signal_db'] is None:
                try:
                    cursor.execute('''
                        SELECT MAX(scanned_at) FROM onu_signal_history
                        WHERE device_id = %s AND interface = %s AND onu_id = %s AND signal_db IS NOT NULL
                    ''', (device_id, s['interface'], s['onu_id']))
                    last_online = cursor.fetchone()[0]
                    if last_online:
                        s['days_offline'] = (datetime.now() - last_online).days
                        s['last_online'] = last_online.strftime('%d.%m.%Y %H:%M')
                except Exception as e:
                    print(f"Days offline error: {e}", file=sys.stderr)

        for s in signals:
            trend = get_trend(device_id, s['interface'], s['onu_id'], s.get('signal_db') or 0)
            s['priority'] = trend['priority']
            # Если сигнала нет, ставим приоритет 0 (вниз)
            if s.get('signal_db') is None:
                s['priority'] = 0
        signals.sort(key=lambda x: (-x['priority'], x['interface'], int(x['onu_id']) if x['onu_id'].isdigit() else 0))
        
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Signal stats error: {e}", file=sys.stderr)
        signals = []
    
    from config import Config
    template = 'signal_stats_content.html' if request.args.get('modal') == '1' else 'signal_stats.html'
    return render_template(template, device=device, signals=signals, filter_interface=filter_interface,
                         signal_has_color=getattr(Config, 'SIGNAL_HAS_COLOR', '#ffffff'),
                         signal_nosignal_color=getattr(Config, 'SIGNAL_NOSIGNAL_COLOR', '#f8d7da'))

# ---------- История входов ----------
@main.route('/login_history')
@admin_required
def login_history():
    now = datetime.utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

    stats = {
        'today': LoginHistory.query.filter(LoginHistory.timestamp >= today_start).count(),
        'week': LoginHistory.query.filter(LoginHistory.timestamp >= week_start).count(),
        'month': LoginHistory.query.filter(LoginHistory.timestamp >= month_start).count(),
        'year': LoginHistory.query.filter(LoginHistory.timestamp >= year_start).count(),
    }

    records = LoginHistory.query.order_by(LoginHistory.timestamp.desc()).limit(200).all()
    return render_template('login_history.html', records=records, stats=stats)

# ---------- Настройки ----------
@main.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    from config import Config
    if request.method == 'POST':
        try:
            query = request.form['billing_query'].replace('\\', '\\\\').replace("'", "\\'")
            snmp_auto = request.form.get('snmp_auto_scan', 'False')
            auto_poll_enabled = request.form.get('auto_poll_enabled', 'False')
            auto_poll_time = request.form.get('auto_poll_time', '02:00')
            monitoring_enabled = request.form.get('monitoring_enabled', 'False')
            monitoring_interval = int(request.form.get('monitoring_interval', '60'))
            ui_auto_refresh_enabled = request.form.get('ui_auto_refresh_enabled', 'False')
            ui_auto_refresh_interval = int(request.form.get('ui_auto_refresh_interval', '30'))
            background_scans_enabled = request.form.get('background_scans_enabled', 'False')
            history_retention_days = int(request.form.get('history_retention_days', '180'))
            signal_has_color = request.form.get('signal_has_color', '#d1fae5')
            signal_nosignal_color = request.form.get('signal_nosignal_color', '#fee2e2')
            dark_bg_color = request.form.get('dark_bg_color', '#0f172a')
            dark_card_color = request.form.get('dark_card_color', '#1e293b')
            dark_text_color = request.form.get('dark_text_color', '#f1f5f9')
            dark_signal_has_color = request.form.get('dark_signal_has_color', '#1a3a2a')
            dark_signal_nosignal_color = request.form.get('dark_signal_nosignal_color', '#3a1a1a')
            dark_accent_color = request.form.get('dark_accent_color', '#3b82f6')
            dark_signal_green = request.form.get('dark_signal_green', '#10b981')
            dark_signal_yellow = request.form.get('dark_signal_yellow', '#f59e0b')
            dark_signal_red = request.form.get('dark_signal_red', '#ef4444')
            light_bg_color = request.form.get('light_bg_color', '#f1f5f9')
            light_card_color = request.form.get('light_card_color', '#ffffff')
            light_text_color = request.form.get('light_text_color', '#1e293b')
            light_signal_has_color = request.form.get('light_signal_has_color', '#d1fae5')
            light_signal_nosignal_color = request.form.get('light_signal_nosignal_color', '#fee2e2')
            light_accent_color = request.form.get('light_accent_color', '#2563eb')
            allowed_ips_text = request.form.get('allowed_ips', '')
            allowed_ips = [ip.strip() for ip in allowed_ips_text.split(',') if ip.strip()]
            
            new_config_content = f'''import os

class Config:
    SECRET_KEY = os.urandom(24).hex()
    SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://oltuser:oltpassword@localhost/oltmanager'
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    BILLING_DB_HOST = '{request.form['billing_host']}'
    BILLING_DB_PORT = {int(request.form['billing_port'])}
    BILLING_DB_USER = '{request.form['billing_user']}'
    BILLING_DB_PASSWORD = '{request.form['billing_password']}'
    BILLING_DB_NAME = '{request.form['billing_db']}'
    BILLING_ADDRESS_QUERY = """{query}"""
    SNMP_AUTO_SCAN = {snmp_auto}
    AUTO_POLL_ENABLED = {auto_poll_enabled}
    AUTO_POLL_TIME = "{auto_poll_time}"
    ALLOWED_IPS = {allowed_ips}
    MONITORING_ENABLED = {monitoring_enabled}
    MONITORING_INTERVAL = {monitoring_interval}
    UI_AUTO_REFRESH_ENABLED = {ui_auto_refresh_enabled}
    UI_AUTO_REFRESH_INTERVAL = {ui_auto_refresh_interval}
    BACKGROUND_SCANS_ENABLED = {background_scans_enabled}
    HISTORY_RETENTION_DAYS = {history_retention_days}
    SIGNAL_HAS_COLOR = '{signal_has_color}'
    SIGNAL_NOSIGNAL_COLOR = '{signal_nosignal_color}'
    DARK_BG_COLOR = '{dark_bg_color}'
    DARK_CARD_COLOR = '{dark_card_color}'
    DARK_TEXT_COLOR = '{dark_text_color}'
    DARK_SIGNAL_HAS_COLOR = '{dark_signal_has_color}'
    DARK_SIGNAL_NOSIGNAL_COLOR = '{dark_signal_nosignal_color}'
    DARK_ACCENT_COLOR = '{dark_accent_color}'
    DARK_SIGNAL_GREEN = '{dark_signal_green}'
    DARK_SIGNAL_YELLOW = '{dark_signal_yellow}'
    DARK_SIGNAL_RED = '{dark_signal_red}'
    LIGHT_BG_COLOR = '{light_bg_color}'
    LIGHT_CARD_COLOR = '{light_card_color}'
    LIGHT_TEXT_COLOR = '{light_text_color}'
    LIGHT_SIGNAL_HAS_COLOR = '{light_signal_has_color}'
    LIGHT_SIGNAL_NOSIGNAL_COLOR = '{light_signal_nosignal_color}'
    LIGHT_ACCENT_COLOR = '{light_accent_color}'
'''
            with open('/opt/oltmanager/config.py', 'w') as f:
                f.write(new_config_content)
            print("Настройки сохранены, перезагрузка...", file=sys.stderr)
            os._exit(0)
        except Exception as e:
            return render_template('settings.html',
                                   billing_host=Config.BILLING_DB_HOST,
                                   billing_port=Config.BILLING_DB_PORT,
                                   billing_user=Config.BILLING_DB_USER,
                                   billing_password=Config.BILLING_DB_PASSWORD,
                                   billing_db=Config.BILLING_DB_NAME,
                                   billing_query=Config.BILLING_ADDRESS_QUERY,
                                   snmp_auto_scan=getattr(Config, 'SNMP_AUTO_SCAN', False),
                                   auto_poll_enabled=getattr(Config, 'AUTO_POLL_ENABLED', False),
                                   auto_poll_time=getattr(Config, 'AUTO_POLL_TIME', '02:00'),
                                   allowed_ips=', '.join(getattr(Config, 'ALLOWED_IPS', [])),
                                   monitoring_enabled=getattr(Config, 'MONITORING_ENABLED', False),
                                   monitoring_interval=getattr(Config, 'MONITORING_INTERVAL', 60),
                                   ui_auto_refresh_enabled=getattr(Config, 'UI_AUTO_REFRESH_ENABLED', True),
                                   ui_auto_refresh_interval=getattr(Config, 'UI_AUTO_REFRESH_INTERVAL', 30),
                                   background_scans_enabled=getattr(Config, 'BACKGROUND_SCANS_ENABLED', True),
                                   history_retention_days=getattr(Config, 'HISTORY_RETENTION_DAYS', 180),
                                   signal_has_color=getattr(Config, 'SIGNAL_HAS_COLOR', '#d1fae5'),
                                   signal_nosignal_color=getattr(Config, 'SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
                                   dark_bg_color=getattr(Config, 'DARK_BG_COLOR', '#0f172a'),
                                   dark_card_color=getattr(Config, 'DARK_CARD_COLOR', '#1e293b'),
                                   dark_text_color=getattr(Config, 'DARK_TEXT_COLOR', '#f1f5f9'),
                                   dark_signal_has_color=getattr(Config, 'DARK_SIGNAL_HAS_COLOR', '#1a3a2a'),
                                   dark_signal_nosignal_color=getattr(Config, 'DARK_SIGNAL_NOSIGNAL_COLOR', '#3a1a1a'),
                                   dark_accent_color=getattr(Config, 'DARK_ACCENT_COLOR', '#3b82f6'),
                                   dark_signal_green=getattr(Config, 'DARK_SIGNAL_GREEN', '#10b981'),
                                   dark_signal_yellow=getattr(Config, 'DARK_SIGNAL_YELLOW', '#f59e0b'),
                                   dark_signal_red=getattr(Config, 'DARK_SIGNAL_RED', '#ef4444'),
                                   light_bg_color=getattr(Config, 'LIGHT_BG_COLOR', '#f1f5f9'),
                                   light_card_color=getattr(Config, 'LIGHT_CARD_COLOR', '#ffffff'),
                                   light_text_color=getattr(Config, 'LIGHT_TEXT_COLOR', '#1e293b'),
                                   light_signal_has_color=getattr(Config, 'LIGHT_SIGNAL_HAS_COLOR', '#d1fae5'),
                                   light_signal_nosignal_color=getattr(Config, 'LIGHT_SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
                                   light_accent_color=getattr(Config, 'LIGHT_ACCENT_COLOR', '#2563eb'),
                                   message=('danger', f'Ошибка сохранения: {e}'))
    return render_template('settings.html',
                           billing_host=Config.BILLING_DB_HOST,
                           billing_port=Config.BILLING_DB_PORT,
                           billing_user=Config.BILLING_DB_USER,
                           billing_password=Config.BILLING_DB_PASSWORD,
                           billing_db=Config.BILLING_DB_NAME,
                           billing_query=Config.BILLING_ADDRESS_QUERY,
                           snmp_auto_scan=getattr(Config, 'SNMP_AUTO_SCAN', False),
                           auto_poll_enabled=getattr(Config, 'AUTO_POLL_ENABLED', False),
                           auto_poll_time=getattr(Config, 'AUTO_POLL_TIME', '02:00'),
                           allowed_ips=', '.join(getattr(Config, 'ALLOWED_IPS', [])),
                           monitoring_enabled=getattr(Config, 'MONITORING_ENABLED', False),
                           monitoring_interval=getattr(Config, 'MONITORING_INTERVAL', 60),
                           ui_auto_refresh_enabled=getattr(Config, 'UI_AUTO_REFRESH_ENABLED', True),
                           ui_auto_refresh_interval=getattr(Config, 'UI_AUTO_REFRESH_INTERVAL', 30),
                           background_scans_enabled=getattr(Config, 'BACKGROUND_SCANS_ENABLED', True),
                           history_retention_days=getattr(Config, 'HISTORY_RETENTION_DAYS', 180),
                           signal_has_color=getattr(Config, 'SIGNAL_HAS_COLOR', '#d1fae5'),
                           signal_nosignal_color=getattr(Config, 'SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
                           dark_bg_color=getattr(Config, 'DARK_BG_COLOR', '#0f172a'),
                           dark_card_color=getattr(Config, 'DARK_CARD_COLOR', '#1e293b'),
                           dark_text_color=getattr(Config, 'DARK_TEXT_COLOR', '#f1f5f9'),
                           dark_signal_has_color=getattr(Config, 'DARK_SIGNAL_HAS_COLOR', '#1a3a2a'),
                           dark_signal_nosignal_color=getattr(Config, 'DARK_SIGNAL_NOSIGNAL_COLOR', '#3a1a1a'),
                           dark_accent_color=getattr(Config, 'DARK_ACCENT_COLOR', '#3b82f6'),
                           dark_signal_green=getattr(Config, 'DARK_SIGNAL_GREEN', '#10b981'),
                           dark_signal_yellow=getattr(Config, 'DARK_SIGNAL_YELLOW', '#f59e0b'),
                           dark_signal_red=getattr(Config, 'DARK_SIGNAL_RED', '#ef4444'),
                           light_bg_color=getattr(Config, 'LIGHT_BG_COLOR', '#f1f5f9'),
                           light_card_color=getattr(Config, 'LIGHT_CARD_COLOR', '#ffffff'),
                           light_text_color=getattr(Config, 'LIGHT_TEXT_COLOR', '#1e293b'),
                           light_signal_has_color=getattr(Config, 'LIGHT_SIGNAL_HAS_COLOR', '#d1fae5'),
                           light_signal_nosignal_color=getattr(Config, 'LIGHT_SIGNAL_NOSIGNAL_COLOR', '#fee2e2'),
                           light_accent_color=getattr(Config, 'LIGHT_ACCENT_COLOR', '#2563eb'))

# ---------- Перезагрузка сервера ----------
@main.route('/restart')
@admin_required
def restart():
    print("Перезагрузка сервера по запросу администратора...", file=sys.stderr)
    os._exit(0)

# ---------- Мониторинг ----------
@main.route('/monitoring')
@login_required
def monitoring():
    if not has_permission(current_user, 'monitoring'):
        return "Доступ запрещён", 403
    return render_template('monitoring.html')
@main.route('/edit_permissions/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def edit_permissions(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        perms = []
        if request.form.get('perm_clients'):
            perms.append('clients')
        if request.form.get('perm_monitoring'):
            perms.append('monitoring')
        if request.form.get('perm_history'):
            perms.append('history')
        if request.form.get('perm_users'):
            perms.append('users')
        if request.form.get('perm_settings'):
            perms.append('settings')
        if request.form.get('perm_restart'):
            perms.append('restart')
        user.permissions = ','.join(perms)
        db.session.commit()
        return redirect(url_for('main.user_list'))
    return render_template('edit_permissions.html', user=user)

# ---------- Клиенты ----------
@main.route('/clients')
@login_required
def clients():
    if not has_permission(current_user, 'clients'):
        return "Доступ запрещён", 403
    from app.models import Client
    clients = Client.query.order_by(Client.name).all()
    return render_template('clients.html', clients=clients)

@main.route('/clients/add', methods=['POST'])
@login_required
def add_client():
    from app.models import Client
    mac = request.form.get('mac', '').strip()
    name = request.form.get('name', '').strip()
    if not mac or not name:
        return redirect(url_for('main.clients'))
    # Нормализуем MAC
    import re
    cleaned = re.sub(r'[^0-9a-fA-F]', '', mac).lower()
    if len(cleaned) != 12:
        return redirect(url_for('main.clients'))
    # Проверяем дубликат
    existing = Client.query.filter_by(mac=cleaned).first()
    if existing:
        existing.name = name
    else:
        new_client = Client(mac=cleaned, name=name)
        db.session.add(new_client)
    db.session.commit()
    return redirect(url_for('main.clients'))

@main.route('/clients/edit/<int:client_id>', methods=['POST'])
@login_required
def edit_client(client_id):
    from app.models import Client
    client = Client.query.get_or_404(client_id)
    client.name = request.form.get('name', '').strip()
    db.session.commit()
    return redirect(url_for('main.clients'))

@main.route('/clients/delete/<int:client_id>', methods=['POST'])
@login_required
def delete_client(client_id):
    from app.models import Client
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    return redirect(url_for('main.clients'))

# ---------- Управление пользователями ----------
@main.route('/users')
@admin_required
def user_list():
    users = User.query.all()
    return render_template('users.html', users=users)

@main.route('/add_user', methods=['GET', 'POST'])
@admin_required
def add_user():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        is_admin = 'is_admin' in request.form
        if User.query.filter_by(username=username).first():
            return render_template('add_user.html', error='Пользователь уже существует')
        user = User(username=username, is_admin=is_admin)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('main.user_list'))
    return render_template('add_user.html')

@main.route('/change_password/<int:user_id>', methods=['GET', 'POST'])
@admin_required
def change_password(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        new_password = request.form['new_password']
        user.set_password(new_password)
        db.session.commit()
        return redirect(url_for('main.user_list'))
    return render_template('change_password.html', user=user)
