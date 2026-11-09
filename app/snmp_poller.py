"""Фоновый SNMP-опросчик: здоровье OLT, трафик портов, статусы/RX ONU (EPON)."""
import re
import sys
import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from app import snmp_lib


def start_snmp_poller(app):
    t = threading.Thread(target=_loop, args=(app,), daemon=True)
    t.start()
    print("[SNMP-POLL] Poller started", file=sys.stderr)


def _loop(app):
    while True:
        try:
            from config import Config
            if getattr(Config, 'SNMP_POLLS_ENABLED', True):
                _cycle(app)
            interval = int(getattr(Config, 'SNMP_POLL_INTERVAL', 300))
            time.sleep(max(60, interval))
        except Exception as e:
            print(f"[SNMP-POLL] loop error: {e}", file=sys.stderr)
            time.sleep(60)


def _cleanup():
    """Ротация истории: трафик/RX 14 дней, health 30, переходы статусов 60."""
    try:
        conn = _db()
        cur = conn.cursor()
        for tbl, days in (('port_traffic', 14), ('onu_rx_history', 14),
                          ('device_health', 30), ('onu_status_log', 60)):
            cur.execute(f'DELETE FROM {tbl} WHERE ts < NOW() - INTERVAL {days} DAY')
        cur.close(); conn.close()
        print('[SNMP-POLL] Cleanup done', file=sys.stderr)
    except Exception as e:
        print(f'[SNMP-POLL] cleanup error: {e}', file=sys.stderr)


def _cycle(app):
    global _cycle_no
    _cycle_no += 1
    do_rx = (_cycle_no % 6 == 1)
    if _cycle_no % 288 == 1:
        _cleanup()
    from config import Config
    with app.app_context():
        from app.models import Device
        devices = Device.query.all()
        workers = int(getattr(Config, 'SNMP_POLL_WORKERS', 6))
        ids = [d.id for d in devices]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(_poll_device, app, did, do_rx) for did in ids]
        for f in futs:
            try:
                f.result(timeout=900)
            except Exception as e:
                print(f"[SNMP-POLL] device error: {e}", file=sys.stderr)
    print(f"[SNMP-POLL] Cycle done: {len(ids)} devices, rx={do_rx}", file=sys.stderr)


_cycle_no = 0


def _is_port_name(n):
    """Физический/PON порт: EPON0/1, GigaEthernet0/1, GE0/3 — без ONU-суффикса ':N'."""
    n = (n or '').strip()
    return bool(re.match(r'^[A-Za-z-]+\d+/\d+$', n)) and ':' not in n


def _db():
    import pymysql
    return pymysql.connect(host='localhost', user='oltuser', password='oltpassword',
                           database='oltmanager', autocommit=True)


def _last_traffic(device_id):
    """{ifname: (in_octets, out_octets, ts)} — последний срез по каждому порту."""
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT t.ifname, t.in_octets, t.out_octets, t.ts FROM port_traffic t
        JOIN (SELECT ifname, MAX(ts) AS m FROM port_traffic WHERE device_id=%s GROUP BY ifname) x
          ON t.device_id=%s AND t.ifname=x.ifname AND t.ts=x.m
        WHERE t.in_octets IS NOT NULL AND t.out_octets IS NOT NULL""", (device_id, device_id))
    res = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
    cur.close(); conn.close()
    return res


def _last_status_map(device_id):
    """{(iface, onu): bool} — последнее известное состояние каждой ONU."""
    conn = _db()
    cur = conn.cursor()
    cur.execute("""
        SELECT o.iface, o.onu_id, o.online FROM onu_status_log o
        JOIN (SELECT iface, onu_id, MAX(ts) AS m FROM onu_status_log
              WHERE device_id=%s GROUP BY iface, onu_id) x
          ON o.device_id=%s AND o.iface=x.iface AND o.onu_id=x.onu_id AND o.ts=x.m""",
        (device_id, device_id))
    res = {(r[0], r[1]): bool(r[2]) for r in cur.fetchall()}
    cur.close(); conn.close()
    return res


def _poll_device(app, device_id, do_rx):
    with app.app_context():
        from app import db
        from app.models import Device, DeviceHealth, PortTraffic, OnuStatusLog, OnuRxHistory
        dev = Device.query.get(device_id)
        if dev is None:
            return
        host = dev.ip
        now = datetime.utcnow()
        try:
            # --- Здоровье (все типы) ---
            try:
                cpu = snmp_lib.get_cpu_load(host)
                up = snmp_lib.get_uptime_s(host)
                if cpu is not None or up is not None:
                    db.session.add(DeviceHealth(device_id=dev.id, cpu=cpu, uptime_s=up))
            except Exception as e:
                print(f"[SNMP-POLL] {dev.name} health err: {e}", file=sys.stderr)

            # --- Трафик портов (все типы) ---
            try:
                names = snmp_lib.get_ifdescr_map(host)
                traffic = snmp_lib.get_traffic(host)
                prev = _last_traffic(dev.id)
                rows = 0
                for idx, counters in traffic.items():
                    try:
                        name = names.get(int(idx))
                    except (ValueError, TypeError):
                        name = None
                    if not name or not _is_port_name(name):
                        continue
                    inb = counters.get('in')
                    outb = counters.get('out')
                    row = PortTraffic(device_id=dev.id, ifname=name,
                                      in_octets=inb, out_octets=outb)
                    p = prev.get(name)
                    if p and inb is not None and outb is not None:
                        dt = (now - p[2]).total_seconds()
                        if 10 < dt < 7200 and inb >= p[0] and outb >= p[1]:
                            row.in_bps = int((inb - p[0]) * 8 / dt)
                            row.out_bps = int((outb - p[1]) * 8 / dt)
                    db.session.add(row)
                    rows += 1
                if rows == 0:
                    print(f"[SNMP-POLL] {dev.name}: нет портов для трафика", file=sys.stderr)
            except Exception as e:
                print(f"[SNMP-POLL] {dev.name} traffic err: {e}", file=sys.stderr)

            # --- EPON: статусы ONU (только переходы) + RX (реже) ---
            if dev.device_type not in ('gpon', 'vsol'):
                try:
                    st = snmp_lib.get_epon_onu_status(host, timeout=120)
                    last = _last_status_map(dev.id)
                    changes = 0
                    for key, online in st.items():
                        if ':' not in key:
                            continue
                        iface, onu = key.rsplit(':', 1)
                        if last.get((iface, onu)) != online:
                            db.session.add(OnuStatusLog(device_id=dev.id, iface=iface,
                                                        onu_id=onu, online=1 if online else 0))
                            changes += 1
                    print(f"[SNMP-POLL] {dev.name}: ONU {len(st)}, переходов {changes}", file=sys.stderr)
                except Exception as e:
                    print(f"[SNMP-POLL] {dev.name} status err: {e}", file=sys.stderr)
                if do_rx:
                    try:
                        rx = snmp_lib.get_epon_onu_rx(host, timeout=120)
                        for key, val in rx.items():
                            iface, onu = key.rsplit(':', 1)
                            db.session.add(OnuRxHistory(device_id=dev.id, iface=iface,
                                                        onu_id=onu, rx_db=val))
                        print(f"[SNMP-POLL] {dev.name}: RX точек {len(rx)}", file=sys.stderr)
                    except Exception as e:
                        print(f"[SNMP-POLL] {dev.name} rx err: {e}", file=sys.stderr)

            db.session.commit()
        except Exception as e:
            db.session.rollback()
            print(f"[SNMP-POLL] {dev.name} общий сбой: {e}", file=sys.stderr)
