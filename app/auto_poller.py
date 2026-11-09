import threading
import time
import sys
import json
import re
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
                        self._cleanup_history()
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

    def _cleanup_history(self):
        """Удаляет историю сигналов старше HISTORY_RETENTION_DAYS."""
        from config import Config
        from sqlalchemy import text
        from app.models import db
        days = int(getattr(Config, 'HISTORY_RETENTION_DAYS', 180))
        try:
            res = db.session.execute(
                text("DELETE FROM onu_signal_history WHERE scanned_at < NOW() - INTERVAL :d DAY"),
                {'d': days})
            db.session.commit()
            print(f"[AUTO] History cleanup: removed {res.rowcount} rows older than {days}d", file=sys.stderr)
        except Exception as e:
            db.session.rollback()
            print(f"[AUTO] History cleanup error: {e}", file=sys.stderr)

    def _poll_device(self, device):
        """Опрашивает все порты устройства любого типа: EPON, VSOL, GPON."""
        if device.device_type == 'vsol':
            from app.vsol_handler import VSOLConnection
            olt = VSOLConnection(device.ip, device.username, device.password, device.enable_password)
        elif device.device_type == 'gpon':
            from app.gpon_handler import GPONConnection
            olt = GPONConnection(device.ip, device.username, device.password, device.enable_password)
        else:
            olt = OLTConnection(device.ip, device.username, device.password, device.enable_password)

        try:
            # Получаем список интерфейсов через SNMP
            interfaces = olt.get_interfaces_snmp()

            if interfaces is None:
                # Fallback: подключаемся и берём список портов из таблицы ONU
                if not olt.connect():
                    print(f"[AUTO] Cannot connect to {device.name}", file=sys.stderr)
                    return
                try:
                    if device.device_type == 'vsol':
                        # 'show onu auth-info all' на части прошивок работает только в config-режиме
                        olt.session.sendline('configure terminal')
                        olt.session.expect(r'\(config\)#', timeout=10)
                        raw = olt.send_command('show onu auth-info all')
                        olt.session.sendline('exit')
                        olt.session.expect('#', timeout=10)
                        ifaces = set(re.findall(r'(EPON\d+/\d+):\d+', raw))
                        interfaces = sorted({i.replace('EPON', '').lower() for i in ifaces})
                    elif device.device_type == 'gpon':
                        raw = olt.send_command('show gpon onu-information')
                        ifaces = set(re.findall(r'(GPON\d+/\d+):\d+', raw, re.IGNORECASE))
                        interfaces = sorted({i.upper().replace('GPON', '').lower() for i in ifaces})
                    else:
                        interfaces, _ = olt.get_interfaces_and_statuses_telnet()
                except Exception as e:
                    print(f"[AUTO] [{device.name}] Interface list error: {e}", file=sys.stderr)
                    interfaces = None

            if not interfaces:
                print(f"[AUTO] [{device.name}] No interfaces found, skipping", file=sys.stderr)
                return

            for interface in interfaces:
                print(f"[AUTO] [{device.name}] Polling {interface}", file=sys.stderr)
                self._poll_interface(device, interface, olt)
        finally:
            # Гарантированно закрываем сессию (для VSOL это освобождает глобальный замок)
            try:
                olt.disconnect()
            except Exception:
                pass

    def _poll_interface(self, device, interface, olt):
        """Опрашивает один EPON-порт."""
        try:
            if not olt.session:
                if not olt.connect():
                    return
            
            scan_data = olt.get_onu_data_for_scan(interface)

            # Нормализация: EPON возвращает tuple, VSOL/GPON — dict
            if isinstance(scan_data, dict) and 'onu_macs' in scan_data:
                onu_statuses = scan_data.get('onu_statuses', {})
                raw_macs = scan_data.get('onu_macs', {})
            else:
                onu_statuses, raw_macs = scan_data

            # Приводим MAC к строке (VSOL/GPON возвращают словари)
            onu_macs = {}
            for _oid, _val in raw_macs.items():
                if isinstance(_val, dict):
                    onu_macs[_oid] = _val.get('mac') or _val.get('sn') or _val.get('loid') or ''
                else:
                    onu_macs[_oid] = _val or ''

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
            
            # Для VSOL — один запрос на все ONU (opm-diag all)
            all_info = None
            if device.device_type == 'vsol':
                try:
                    all_info = olt.get_all_onu_info(interface)
                except Exception as e:
                    print(f"[AUTO] [{device.name}] get_all_onu_info error {interface}: {e}", file=sys.stderr)

            # Опрашиваем сигналы для каждого ONU (даже если сигнал недоступен)
            for onu_id in onu_macs:
                if all_info is not None:
                    info = all_info.get(onu_id)
                else:
                    info = olt.get_onu_info(interface, onu_id)
                signal = info.get('signal') if info else None
                temperature = info.get('temperature') if info else None
                conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO onu_signal_history (device_id, interface, onu_id, mac_onu, address, signal_db, temperature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (device.id, interface, onu_id, onu_macs.get(onu_id, ''), addresses.get(onu_id, 'Не внесён'), signal, temperature))
                conn.commit()
                cursor.close()
                conn.close()
            
            # Удаляем ONU, которых больше нет на порту (удалены с OLT)
            if onu_macs:
                try:
                    _conn = connect(host='localhost', user='oltuser', password='oltpassword', database='oltmanager')
                    _cur = _conn.cursor()
                    _ph = ', '.join(['%s'] * len(onu_macs))
                    _cur.execute(
                        "DELETE FROM onu_signal_history WHERE device_id = %s AND interface = %s "
                        "AND onu_id NOT IN (" + _ph + ") AND scanned_at < NOW() - INTERVAL 30 DAY",
                        [device.id, interface] + list(onu_macs.keys())
                    )
                    _removed = _cur.rowcount
                    _conn.commit()
                    _cur.close()
                    _conn.close()
                    if _removed:
                        print(f"[AUTO] [{device.name}] {interface}: removed {_removed} deleted ONU(s)", file=sys.stderr)
                except Exception as e:
                    print(f"[AUTO] [{device.name}] ONU cleanup error {interface}: {e}", file=sys.stderr)

            print(f"[AUTO] [{device.name}] Completed {interface}", file=sys.stderr)
        except Exception as e:
            print(f"[AUTO] [{device.name}] Error polling {interface}: {e}", file=sys.stderr)

# Импортируем модели для работы с БД
from app.models import MonitorTask, MonitorSnapshot, db

# Функции для работы с БД
def create_monitor_task(task_id, device_id, interface, onu_id, duration, interval):
    task = MonitorTask(
        task_id=task_id,
        device_id=device_id,
        interface=interface,
        onu_id=onu_id,
        duration=duration,
        interval=interval,
        status='running'
    )
    db.session.add(task)
    db.session.commit()
    return task

def update_monitor_task(task_id, **kwargs):
    task = MonitorTask.query.filter_by(task_id=task_id).first()
    if task:
        for key, value in kwargs.items():
            setattr(task, key, value)
        db.session.commit()

def add_monitor_snapshot(task_id, data):
    snapshot = MonitorSnapshot(task_id=task_id, data=json.dumps(data))
    db.session.add(snapshot)
    db.session.commit()

def get_monitor_task(task_id):
    return MonitorTask.query.filter_by(task_id=task_id).first()

def get_monitor_snapshots(task_id):
    return MonitorSnapshot.query.filter_by(task_id=task_id).order_by(MonitorSnapshot.timestamp).all()

def get_all_monitor_tasks():
    return MonitorTask.query.order_by(MonitorTask.start_time.desc()).all()

# Глобальный словарь для активных задач (будем использовать только для потоков)
_active_monitor_threads = {}

def monitor_onu_worker(app, task_id, device_id, interface, onu_id, duration, interval):
    """Фоновый процесс мониторинга ONU с сохранением в БД."""
    import time, json
    with app.app_context():
        from app.models import MonitorTask, MonitorSnapshot, db, Device
        from app.billing import get_address_from_billing
        from app.olt_handler import OLTConnection
        from app.gpon_handler import GPONConnection
        from app.vsol_handler import VSOLConnection
        
        # Заново получаем устройство в контексте приложения
        device = Device.query.get(device_id)
        if not device:
            return
        
        start_time = time.time()
        end_time = start_time + duration * 60  # duration в минутах
        
        if device.device_type == 'gpon':
            conn_cls = GPONConnection
        elif device.device_type == 'vsol':
            conn_cls = VSOLConnection
        else:
            conn_cls = OLTConnection
        
        collected_data = []
        lan_ports = None  # видимые UNI-порты; определим на первом успешном снимке
        
        while time.time() < end_time:
            # Проверяем статус задачи (если остановлена – выходим)
            task = MonitorTask.query.filter_by(task_id=task_id).first()
            if not task or task.status == 'interrupted':
                break
            
            # Создаём новое подключение к OLT
            olt = conn_cls(device.ip, device.username, device.password, device.enable_password)
            if olt.connect():
                # Однократно: сколько UNI-портов ONU видит OLT (BDCOM обычно только 1)
                if lan_ports is None:
                    if hasattr(olt, 'detect_lan_ports'):
                        try:
                            lan_ports = olt.detect_lan_ports(interface, onu_id) or [1]
                        except Exception:
                            lan_ports = [1]
                    else:
                        lan_ports = [1]
                # Получаем статистику порта
                stats = olt.get_port_statistics(interface, onu_id)
                # Получаем информацию о сигнале
                onu_info = olt.get_onu_info(interface, onu_id)
                # Получаем состояние LAN
                lan_state = olt.get_lan_state(interface, onu_id)
                # Получаем MAC-таблицу
                mac_data = olt.get_mac_table(f"EPON{interface}:{onu_id}")
                # Дополнительные UNI-порты (если ONU отдаёт их OLT'у)
                extra_ports = {}
                for _p in (lan_ports or [1])[1:]:
                    try:
                        extra_ports[str(_p)] = {
                            'lan_state': olt.get_lan_state(interface, onu_id, port=_p),
                            'stats': olt.get_port_statistics(interface, onu_id, port=_p) or {},
                        }
                    except Exception as _e:
                        print(f"[MONITOR] LAN{_p} ошибка: {_e}", file=sys.stderr)
                
                snapshot = {
                    'timestamp': time.time(),
                    'stats': stats,
                    'signal': onu_info.get('signal') if onu_info else None,
                    'temperature': onu_info.get('temperature') if onu_info else None,
                    'lan_state': lan_state,
                    'macs': mac_data.get('macs', []) if mac_data else [],
                    'vlan': mac_data.get('vlans', []) if mac_data else [],
                    'extra_ports': extra_ports
                }
                collected_data.append(snapshot)
                # Сохраняем снимок в БД
                add_monitor_snapshot(task_id, snapshot)
                olt.disconnect()
            else:
                snapshot = {'timestamp': time.time(), 'error': 'connect_failed'}
                collected_data.append(snapshot)
                add_monitor_snapshot(task_id, snapshot)
            
            # Обновляем last_update
            update_monitor_task(task_id, last_update=datetime.utcnow())
            
            # Спим мелкими шагами, чтобы «Остановить» срабатывало быстро
            slept = 0.0
            target_sleep = interval * 60
            while slept < target_sleep:
                step = min(5.0, target_sleep - slept)
                time.sleep(step)
                slept += step
                t = MonitorTask.query.filter_by(task_id=task_id).first()
                if not t or t.status == 'interrupted':
                    break
            t = MonitorTask.query.filter_by(task_id=task_id).first()
            if not t or t.status == 'interrupted':
                break
        
        # Формируем отчёт
        result = analyze_onu_monitor_data(collected_data, interface, onu_id)
        # Сохраняем результат; статус 'interrupted' (остановлен пользователем) не перезаписываем
        t = MonitorTask.query.filter_by(task_id=task_id).first()
        if t and t.status == 'interrupted':
            update_monitor_task(task_id, end_time=datetime.utcnow(), result=json.dumps(result))
        else:
            update_monitor_task(task_id, status='done', end_time=datetime.utcnow(), result=json.dumps(result))

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

    # Дополнительные LAN-порты (если ONU отдаёт больше одного UNI)
    def _num(v):
        try:
            return int(str(v).replace(',', ''))
        except Exception:
            return None

    fe = first.get('extra_ports') or {}
    le = last.get('extra_ports') or {}
    for p, first_port in fe.items():
        last_port = le.get(p) or {}
        flan = first_port.get('lan_state')
        llan = last_port.get('lan_state')
        s1 = first_port.get('stats') or {}
        s2 = last_port.get('stats') or {}
        for key in ['In Bad Octets', 'In FCS Error Frames', 'Collisions Frames', 'Excessive Frames', 'Late Frames']:
            a, b = _num(s1.get(key)), _num(s2.get(key))
            if a is not None and b is not None and b - a > 0:
                report['anomalies'].append(f"LAN{p}: Увеличение {key}: +{b - a}")
        if flan and llan and flan != llan:
            report['anomalies'].append(f"LAN{p}: Изменение LAN-статуса: {flan} -> {llan}")
        if s1 or s2:
            rx, rx0 = _num(s2.get('In Good Octets')), _num(s1.get('In Good Octets'))
            tx, tx0 = _num(s2.get('Out Octets')), _num(s1.get('Out Octets'))
            report['summary'][f'LAN{p}'] = (
                f"{flan or '?'} -> {llan or '?'}; "
                f"RX октетов: {'?' if None in (rx, rx0) else rx - rx0}; "
                f"TX октетов: {'?' if None in (tx, tx0) else tx - tx0}")

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
