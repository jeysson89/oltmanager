import threading
import time
import sys
import json
from datetime import datetime, timedelta
from app import create_app
from app.olt_handler import OLTConnection
from app.billing import get_address_from_billing
import concurrent.futures

class AutoPoller:
    def __init__(self):
        self.running = False
        self.thread = None
        self.last_poll_date = None

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.thread.start()
        print("[AUTO] Auto poller started", file=sys.stderr)

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        print("[AUTO] Auto poller stopped", file=sys.stderr)

    def _get_scheduled_time(self):
        """Получает время запуска из конфига."""
        from config import Config
        time_str = getattr(Config, 'AUTO_POLL_TIME', '02:00')
        try:
            hour, minute = map(int, time_str.split(':'))
            return hour, minute
        except:
            return 2, 0

    def _poll_loop(self):
        from config import Config
        
        while self.running:
            try:
                if not getattr(Config, 'AUTO_POLL_ENABLED', False):
                    time.sleep(10)
                    continue

                now = datetime.now()
                hour, minute = self._get_scheduled_time()
                
                # Проверяем, настало ли время опроса
                if now.hour == hour and now.minute == minute:
                    # Проверяем, не опрашивали ли уже сегодня
                    if self.last_poll_date != now.date():
                        print(f"[AUTO] Starting scheduled poll at {now.strftime('%H:%M')}", file=sys.stderr)
                        self._poll_all_devices_parallel()
                        self.last_poll_date = now.date()
                        print(f"[AUTO] Scheduled poll completed", file=sys.stderr)
                
                time.sleep(30)  # Проверяем каждые 30 секунд
            except Exception as e:
                print(f"[AUTO] Poll loop error: {e}", file=sys.stderr)
                time.sleep(30)

    def _poll_all_devices_parallel(self):
        """Опрашивает все устройства параллельно (каждое в отдельном потоке)."""
        app = create_app()
        with app.app_context():
            from app.models import db, Device
            
            devices = Device.query.all()
            
            if not devices:
                print("[AUTO] No devices found", file=sys.stderr)
                return
            
            print(f"[AUTO] Polling {len(devices)} devices in parallel", file=sys.stderr)
            
            # Используем ThreadPoolExecutor для параллельного опроса
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(devices)) as executor:
                futures = {executor.submit(self._poll_device, device): device for device in devices}
                
                for future in concurrent.futures.as_completed(futures):
                    device = futures[future]
                    try:
                        future.result()
                        print(f"[AUTO] Completed polling {device.name}", file=sys.stderr)
                    except Exception as e:
                        print(f"[AUTO] Error polling {device.name}: {e}", file=sys.stderr)

    def _poll_device(self, device):
        """Опрашивает все EPON-порты устройства (вызывается в отдельном потоке)."""
        olt = OLTConnection(device.ip, device.username, device.password, device.enable_password)
        
        # Получаем список интерфейсов через SNMP
        interfaces = olt.get_interfaces_snmp()
        
        if interfaces is None:
            # Fallback на Telnet
            if not olt.connect():
                print(f"[AUTO] Cannot connect to {device.name}", file=sys.stderr)
                return
            interfaces, _ = olt.get_interfaces_and_statuses_telnet()
        
        for interface in interfaces:
            print(f"[AUTO] [{device.name}] Polling {interface}", file=sys.stderr)
            self._poll_interface(device, interface, olt)
        
        olt.disconnect()

    def _poll_interface(self, device, interface, olt):
        """Опрашивает один EPON-порт."""
        try:
            if not olt.session:
                if not olt.connect():
                    return
            
            onu_statuses, onu_macs = olt.get_onu_data_for_scan(interface)
            
            addresses = {}
            for onu_id, mac in onu_macs.items():
                if mac:
                    address = get_address_from_billing(mac)
                    if address:
                        addresses[onu_id] = address
                    else:
                        # Ищем в базе клиентов
                        from app.billing import get_client_name_from_mac
                        client_name = get_client_name_from_mac(mac)
                        if client_name:
                            addresses[onu_id] = client_name
            
            result = {
                'status': 'ok',
                'interface': interface,
                'onu_macs': onu_macs,
                'onu_statuses': onu_statuses,
                'addresses': addresses
            }
            
            # Сохраняем в кэш
            from pymysql import connect
            conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO scan_cache (device_id, interface, scan_data, scanned_at)
                VALUES (%s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE scan_data = VALUES(scan_data), scanned_at = NOW()
            """, (device.id, interface, json.dumps(result)))
            conn.commit()
            cursor.close()
            conn.close()
            
            # Опрашиваем сигналы для каждого ONU (даже если сигнал недоступен)
            for onu_id in onu_macs:
                info = olt.get_onu_info(interface, onu_id)
                signal = info.get('signal') if info else None
                temperature = info.get('temperature') if info else None
                conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO onu_signal_history (device_id, interface, onu_id, mac_onu, address, signal_db, temperature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (device.id, interface, onu_id, onu_macs.get(onu_id, ''), addresses.get(onu_id, ''), signal, temperature))
                conn.commit()
                cursor.close()
                conn.close()
            
            print(f"[AUTO] [{device.name}] Completed {interface}", file=sys.stderr)
        except Exception as e:
            print(f"[AUTO] [{device.name}] Error polling {interface}: {e}", file=sys.stderr)

# Глобальный словарь для мониторинга ONU
onu_monitor_tasks = {}  # task_id -> {'status': 'running'/'done', 'device_id':..., 'interface':..., 'onu_id':..., 'start_time':..., 'duration':..., 'interval':..., 'data': [], 'result': None}

def monitor_onu_worker(task_id, device, interface, onu_id, duration, interval):
    """Фоновый процесс мониторинга ONU."""
    import time
    from app.billing import get_address_from_billing
    from app.olt_handler import OLTConnection
    from app.gpon_handler import GPONConnection
    from app.vsol_handler import VSOLConnection
    
    task = onu_monitor_tasks[task_id]
    # Добавляем имя и IP устройства для отображения
    task['device_name'] = device.name
    task['device_ip'] = device.ip
    start_time = time.time()
    end_time = start_time + duration * 60  # duration в минутах
    
    # Определяем тип соединения
    if device.device_type == 'gpon':
        conn_cls = GPONConnection
    elif device.device_type == 'vsol':
        conn_cls = VSOLConnection
    else:
        conn_cls = OLTConnection
    
    collected_data = []
    
    while time.time() < end_time and task['status'] == 'running':
        # Создаём новое подключение к OLT
        olt = conn_cls(device.ip, device.username, device.password, device.enable_password)
        if olt.connect():
            # Получаем статистику порта
            stats = olt.get_port_statistics(interface, onu_id)
            # Получаем информацию о сигнале
            onu_info = olt.get_onu_info(interface, onu_id)
            # Получаем состояние LAN
            lan_state = olt.get_lan_state(interface, onu_id)
            # Получаем MAC-таблицу
            mac_data = olt.get_mac_table(f"EPON{interface}:{onu_id}")
            
            snapshot = {
                'timestamp': time.time(),
                'stats': stats,
                'signal': onu_info.get('signal') if onu_info else None,
                'temperature': onu_info.get('temperature') if onu_info else None,
                'lan_state': lan_state,
                'macs': mac_data.get('macs', []) if mac_data else [],
                'vlan': mac_data.get('vlans', []) if mac_data else []
            }
            collected_data.append(snapshot)
            olt.disconnect()
        else:
            # Не удалось подключиться, добавляем пустую запись
            collected_data.append({'timestamp': time.time(), 'error': 'connect_failed'})
        
        # Обновляем прогресс в задаче
        task['data'] = collected_data
        task['last_update'] = time.time()
        
        # Ждём интервал
        time.sleep(interval * 60)  # interval в минутах
    
    # Формируем отчёт
    result = analyze_onu_monitor_data(collected_data, interface, onu_id)
    task['status'] = 'done'
    task['result'] = result

def analyze_onu_monitor_data(data, interface, onu_id):
    """Анализирует собранные данные и формирует отчёт."""
    report = {
        'interface': interface,
        'onu_id': onu_id,
        'duration': len(data),
        'anomalies': [],
        'summary': {}
    }
    
    if not data:
        report['summary'] = {'error': 'Нет данных'}
        return report
    
    # Базовые проверки
    first = data[0]
    last = data[-1]
    
    # Проверка ошибок в статистике
    if first.get('stats') and last.get('stats'):
        for key in ['In Bad Octets', 'In FCS Error Frames', 'Collisions Frames', 'Excessive Frames', 'Late Frames']:
            if key in last['stats'] and key in first['stats']:
                diff = last['stats'][key] - first['stats'][key]
                if diff > 0:
                    report['anomalies'].append(f"Увеличение {key}: +{diff}")
    
    # Проверка сигнала
    if first.get('signal') is not None and last.get('signal') is not None:
        try:
            sig_diff = float(last['signal']) - float(first['signal'])
            if sig_diff < -3:  # ухудшение более чем на 3 дБ
                report['anomalies'].append(f"Ухудшение сигнала: {first['signal']} -> {last['signal']}")
            elif sig_diff > 3:
                report['anomalies'].append(f"Улучшение сигнала: {first['signal']} -> {last['signal']}")
        except:
            pass
    
    # Проверка LAN-статуса
    if first.get('lan_state') and last.get('lan_state'):
        if first['lan_state'] != last['lan_state']:
            report['anomalies'].append(f"Изменение LAN-статуса: {first['lan_state']} -> {last['lan_state']}")
    
    # Проверка MAC-адресов
    if first.get('macs') and last.get('macs'):
        if set(first['macs']) != set(last['macs']):
            report['anomalies'].append(f"Изменение MAC-адресов: {first['macs']} -> {last['macs']}")
    
    # Добавим сводку
    report['summary'] = {
        'samples': len(data),
        'first_signal': first.get('signal'),
        'last_signal': last.get('signal'),
        'first_lan': first.get('lan_state'),
        'last_lan': last.get('lan_state'),
        'total_rx_octets': last.get('stats', {}).get('In Good Octets', 0) - first.get('stats', {}).get('In Good Octets', 0),
        'total_tx_octets': last.get('stats', {}).get('Out Octets', 0) - first.get('stats', {}).get('Out Octets', 0),
    }
    
    return report

# Глобальный словарь для статусов устройств
device_status = {}  # device_id -> {'status': 'up'/'down', 'last_check': datetime}

# Глобальный словарь для мониторинга ONU
onu_monitor_tasks = {}  # task_id -> {'status': 'running'/'done', 'device_id':..., 'interface':..., 'onu_id':..., 'start_time':..., 'duration':..., 'interval':..., 'data': [], 'result': None}

def monitor_onu_worker(task_id, device, interface, onu_id, duration, interval):
    """Фоновый процесс мониторинга ONU."""
    import time
    from app.billing import get_address_from_billing
    from app.olt_handler import OLTConnection
    from app.gpon_handler import GPONConnection
    from app.vsol_handler import VSOLConnection
    
    task = onu_monitor_tasks[task_id]
    # Добавляем имя и IP устройства для отображения
    task['device_name'] = device.name
    task['device_ip'] = device.ip
    start_time = time.time()
    end_time = start_time + duration * 60  # duration в минутах
    
    # Определяем тип соединения
    if device.device_type == 'gpon':
        conn_cls = GPONConnection
    elif device.device_type == 'vsol':
        conn_cls = VSOLConnection
    else:
        conn_cls = OLTConnection
    
    collected_data = []
    
    while time.time() < end_time and task['status'] == 'running':
        # Создаём новое подключение к OLT
        olt = conn_cls(device.ip, device.username, device.password, device.enable_password)
        if olt.connect():
            # Получаем статистику порта
            stats = olt.get_port_statistics(interface, onu_id)
            # Получаем информацию о сигнале
            onu_info = olt.get_onu_info(interface, onu_id)
            # Получаем состояние LAN
            lan_state = olt.get_lan_state(interface, onu_id)
            # Получаем MAC-таблицу
            mac_data = olt.get_mac_table(f"EPON{interface}:{onu_id}")
            
            snapshot = {
                'timestamp': time.time(),
                'stats': stats,
                'signal': onu_info.get('signal') if onu_info else None,
                'temperature': onu_info.get('temperature') if onu_info else None,
                'lan_state': lan_state,
                'macs': mac_data.get('macs', []) if mac_data else [],
                'vlan': mac_data.get('vlans', []) if mac_data else []
            }
            collected_data.append(snapshot)
            olt.disconnect()
        else:
            # Не удалось подключиться, добавляем пустую запись
            collected_data.append({'timestamp': time.time(), 'error': 'connect_failed'})
        
        # Обновляем прогресс в задаче
        task['data'] = collected_data
        task['last_update'] = time.time()
        
        # Ждём интервал
        time.sleep(interval * 60)  # interval в минутах
    
    # Формируем отчёт
    result = analyze_onu_monitor_data(collected_data, interface, onu_id)
    task['status'] = 'done'
    task['result'] = result

def analyze_onu_monitor_data(data, interface, onu_id):
    """Анализирует собранные данные и формирует отчёт."""
    report = {
        'interface': interface,
        'onu_id': onu_id,
        'duration': len(data),
        'anomalies': [],
        'summary': {}
    }
    
    if not data:
        report['summary'] = {'error': 'Нет данных'}
        return report
    
    # Базовые проверки
    first = data[0]
    last = data[-1]
    
    # Проверка ошибок в статистике
    if first.get('stats') and last.get('stats'):
        for key in ['In Bad Octets', 'In FCS Error Frames', 'Collisions Frames', 'Excessive Frames', 'Late Frames']:
            if key in last['stats'] and key in first['stats']:
                diff = last['stats'][key] - first['stats'][key]
                if diff > 0:
                    report['anomalies'].append(f"Увеличение {key}: +{diff}")
    
    # Проверка сигнала
    if first.get('signal') is not None and last.get('signal') is not None:
        try:
            sig_diff = float(last['signal']) - float(first['signal'])
            if sig_diff < -3:  # ухудшение более чем на 3 дБ
                report['anomalies'].append(f"Ухудшение сигнала: {first['signal']} -> {last['signal']}")
            elif sig_diff > 3:
                report['anomalies'].append(f"Улучшение сигнала: {first['signal']} -> {last['signal']}")
        except:
            pass
    
    # Проверка LAN-статуса
    if first.get('lan_state') and last.get('lan_state'):
        if first['lan_state'] != last['lan_state']:
            report['anomalies'].append(f"Изменение LAN-статуса: {first['lan_state']} -> {last['lan_state']}")
    
    # Проверка MAC-адресов
    if first.get('macs') and last.get('macs'):
        if set(first['macs']) != set(last['macs']):
            report['anomalies'].append(f"Изменение MAC-адресов: {first['macs']} -> {last['macs']}")
    
    # Добавим сводку
    report['summary'] = {
        'samples': len(data),
        'first_signal': first.get('signal'),
        'last_signal': last.get('signal'),
        'first_lan': first.get('lan_state'),
        'last_lan': last.get('lan_state'),
        'total_rx_octets': last.get('stats', {}).get('In Good Octets', 0) - first.get('stats', {}).get('In Good Octets', 0),
        'total_tx_octets': last.get('stats', {}).get('Out Octets', 0) - first.get('stats', {}).get('Out Octets', 0),
    }
    
    return report

# Глобальный словарь для статусов устройств
device_status = {}  # device_id -> {'status': 'up'/'down', 'last_check': datetime}
_status_lock = threading.Lock()  # защита словаря при параллельной записи

def check_device_ping(device_id, ip):
    """Проверяет доступность устройства через ping (5 пингов)."""
    import subprocess
    try:
        result = subprocess.run(
            ['/bin/ping', '-c', '5', '-W', '1', ip],
            capture_output=True, text=True, timeout=10
        )
        status = 'up' if result.returncode == 0 else 'down'
        with _status_lock:
            device_status[device_id] = {'status': status, 'last_check': datetime.now()}
        return status
    except Exception as e:
        print(f"[MONITOR] Ping error for {ip}: {e}", file=sys.stderr)
        with _status_lock:
            device_status[device_id] = {'status': 'down', 'last_check': datetime.now()}
        return 'down'

def monitor_loop():
    """Фоновый цикл мониторинга доступности с параллельной проверкой."""
    import concurrent.futures
    
    from config import Config
    from app import create_app
    from app.models import Device, db
    
    # Создаём приложение ОДИН раз
    app = create_app()
    
    while True:
        try:
            if not getattr(Config, 'MONITORING_ENABLED', False):
                time.sleep(10)
                continue
            
            interval = getattr(Config, 'MONITORING_INTERVAL', 60)
            with app.app_context():
                devices = Device.query.all()
                # Запускаем проверку всех устройств параллельно
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(check_device_ping, device.id, device.ip): device for device in devices}
                    for future in concurrent.futures.as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            print(f"[MONITOR] Ping task error: {e}", file=sys.stderr)
                # Закрываем сессию после каждого цикла, чтобы не копить соединения
                db.session.remove()
            time.sleep(interval)
        except Exception as e:
            print(f"[MONITOR] Loop error: {e}", file=sys.stderr)
            try:
                db.session.remove()
            except:
                pass
            time.sleep(30)


_monitoring_started = False

def start_monitoring():
    """Запускает поток мониторинга (только один раз)."""
    global _monitoring_started
    if _monitoring_started:
        return
    _monitoring_started = True
    t = threading.Thread(target=monitor_loop, daemon=True)
    t.start()
    print("[MONITOR] Monitoring started", file=sys.stderr)

# Глобальный экземпляр
auto_poller = AutoPoller()
