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
    return dict(get_trend=get_trend)

# ---------- Главная и авторизация ----------
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

    # Определяем тип соединения
    from app.gpon_handler import GPONConnection
    from app.vsol_handler import VSOLConnection
    if device.device_type == 'gpon':
        olt = GPONConnection(device.ip, device.username, device.password, device.enable_password)
    else:
        olt = OLTConnection(device.ip, device.username, device.password, device.enable_password)

    snmp_interfaces = olt.get_interfaces_snmp()

    if snmp_interfaces is not None:
        interfaces = snmp_interfaces
    else:
        if olt.connect():
            interfaces, onu_statuses = olt.get_interfaces_and_statuses_telnet()
            olt.disconnect()

    template_name = 'vsol_device.html' if device.device_type == 'vsol' else 'device.html'
    return render_template(template_name, device=device, interfaces=interfaces,
                         onu_statuses=onu_statuses, cached_data=cached_data,
                         last_scan_time=last_scan_time)

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
        print(f"[INFO] Page loaded with SNMP interfaces", file=sys.stderr)
    else:
        if olt.connect():
            interfaces, onu_statuses = olt.get_interfaces_and_statuses_telnet()
            olt.disconnect()
            print(f"[INFO] Page loaded with Telnet interfaces and statuses", file=sys.stderr)
        else:
            print(f"[ERROR] Cannot connect to OLT", file=sys.stderr)

    template_name = 'vsol_device.html' if device.device_type == 'vsol' else 'device.html'
    return render_template(template_name, device=device, interfaces=interfaces,
                         onu_statuses=onu_statuses, cached_data=cached_data,
                         last_scan_time=last_scan_time)

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

# ---------- API сканирования ----------
@main.route('/api/device/<int:device_id>/scan_interface/<path:interface>')
@login_required
def scan_interface(device_id, interface):
    device = Device.query.get_or_404(device_id)
    olt = _get_device_connection(device)
    if not olt.connect():
        return jsonify({'status': 'error', 'message': 'Ошибка подключения'}), 500
    # Для VSOL используем одну сессию — не создаём параллельные подключения
    if device.device_type == 'vsol':
        olt.max_parallel = 1

    result_data = olt.get_onu_data_for_scan(interface)
    # Сбрасываем кэш VSOL
    if device.device_type == 'vsol':
        global _vsol_cache_global
        cache_key = f"{device_id}:{interface}"
        _vsol_cache_global.pop(cache_key, None)
    
    # Собираем сигналы ДО отключения (иначе данные не получить)
    signals_data = {}
    if isinstance(result_data, dict) and 'onu_macs' in result_data:
        onu_list = result_data['onu_macs']
    else:
        onu_list = result_data[1]
    for onu_id in onu_list:
        info = olt.get_onu_info(interface, onu_id)
        if info:
            signals_data[onu_id] = info
        else:
            signals_data[onu_id] = None

    olt.disconnect()

    if isinstance(result_data, dict) and 'onu_macs' in result_data:
        # Формат от GPON (словарь ONU)
        onu_statuses = result_data['onu_statuses']
        onu_macs = result_data['onu_macs']
    else:
        # Формат от EPON (кортеж)
        onu_statuses, onu_macs = result_data

    addresses = {}
    for onu_id, mac_data in onu_macs.items():
        address = None
        if isinstance(mac_data, str):
            address = get_address_from_billing(mac_data)
        elif isinstance(mac_data, dict):
            # Для GPON — SN, для VSOL — MAC
            sn = mac_data.get('sn', '')
            mac = mac_data.get('mac', '')
            if sn:
                address = get_address_from_billing(sn)
            elif mac:
                address = get_address_from_billing(mac)
        if address:
            addresses[onu_id] = address
        else:
            addresses[onu_id] = 'Удалён или не внесён'

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

    # Сохраняем сигналы для всех ONU (используем собранные до disconnect данные)
    try:
        from pymysql import connect
        conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
        cursor = conn.cursor()
        for onu_id, mac_data in onu_macs.items():
            info = signals_data.get(onu_id)
            signal = info.get('signal') if info else None
            temperature = info.get('temperature') if info else None
            mac = mac_data if isinstance(mac_data, str) else (mac_data.get('mac') or mac_data.get('sn') or '')
            address = addresses.get(onu_id, '')
            cursor.execute("""
                INSERT INTO onu_signal_history (device_id, interface, onu_id, mac_onu, address, signal_db, temperature)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (device_id, interface, onu_id, mac, address, signal, temperature))
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        print(f"Signal history save error: {e}", file=sys.stderr)

    return jsonify(result)


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
                'time': row[2].strftime('%d.%m.%Y %H:%M') if row[2] else ''
            })
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
    for onu in onu_ids:
        success, msg = olt.delete_onu(interface, onu)
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
                'scanned_at': row[6]
            })
        
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
    
    return render_template('signal_stats.html', device=device, signals=signals, filter_interface=filter_interface)

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
                           monitoring_interval=getattr(Config, 'MONITORING_INTERVAL', 60))

# ---------- Перезагрузка сервера ----------
@main.route('/restart')
@admin_required
def restart():
    print("Перезагрузка сервера по запросу администратора...", file=sys.stderr)
    os._exit(0)

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
