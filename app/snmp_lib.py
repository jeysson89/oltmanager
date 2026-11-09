"""SNMP-утилиты: walk через системный snmpbulkwalk, парсинг, сборщики.
OID-константы проверены на живых OLT (2026-11-03)."""
import re
import subprocess
import time

SNMP_BIN = '/usr/bin/snmpbulkwalk'
SNMP_GET = '/usr/bin/snmpget'
SNMP_COMMUNITY = 'public'
SNMP_VERSION = '2c'

OID_IFDESCR = '.1.3.6.1.2.1.2.2.1.2'
OID_IFHCIN = '.1.3.6.1.2.1.31.1.1.1.6'
OID_IFHCOUT = '.1.3.6.1.2.1.31.1.1.1.10'
OID_HRCPU = '.1.3.6.1.2.1.25.3.3.1.2'
OID_SYSUPTIME = '.1.3.6.1.2.1.1.3.0'
OID_EPON_ONU_RX = '.1.3.6.1.4.1.3320.101.10.5.1.5'
OID_EPON_ONU_STATUS = '.1.3.6.1.4.1.3320.101.10.1.1.12'
OID_QBRIDGE_ADDR = '.1.3.6.1.2.1.17.7.1.2.2.1.1'
OID_QBRIDGE_PORT = '.1.3.6.1.2.1.17.7.1.2.2.1.2'


def snmp_walk(host, oid, timeout=60):
    """[(хвост-индекса, сырое значение)]"""
    try:
        r = subprocess.run([SNMP_BIN, '-v' + SNMP_VERSION, '-c', SNMP_COMMUNITY, '-On', host, oid],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines():
        if '=' not in line:
            continue
        oidpart, val = line.split('=', 1)
        oidpart = oidpart.strip()
        if not oidpart.startswith(oid):
            continue
        out.append((oidpart[len(oid):].strip('.'), val.strip()))
    return out


def parse_value(raw):
    m = re.match(r'([A-Za-z0-9-]+):\s*(.*)', raw, re.S)
    if not m:
        return ('RAW', raw.strip())
    typ, val = m.group(1), m.group(2).strip()
    if typ in ('INTEGER', 'Gauge32', 'Counter32', 'Counter64'):
        try:
            return (typ, int(val.split()[0]))
        except Exception:
            return (typ, None)
    if typ == 'Hex-STRING':
        return (typ, re.sub(r'[^0-9A-Fa-f]', '', val))
    if typ == 'Timeticks':
        m2 = re.match(r'\((\d+)\)', val)
        return (typ, int(m2.group(1)) // 100 if m2 else None)
    return (typ, val.strip('"'))


def walk_map(host, oid, timeout=60):
    """{хвост: значение}"""
    res = {}
    for tail, raw in snmp_walk(host, oid, timeout):
        res[tail] = parse_value(raw)[1]
    return res


def hex_to_mac(hexs):
    if not hexs or len(hexs) != 12:
        return None
    return ':'.join(hexs[i:i + 2] for i in range(0, 12, 2))


def normalize_mac(mac):
    mac = re.sub(r'[^0-9A-Fa-f]', '', mac or '')
    if len(mac) != 12:
        return None
    return ':'.join(mac[i:i + 2].lower() for i in range(0, 12, 2))


def get_ifdescr_map(host, timeout=45):
    """{ifIndex: 'EPON0/1:1' / 'GE0/3' / ...}"""
    res = {}
    for tail, raw in snmp_walk(host, OID_IFDESCR, timeout):
        try:
            res[int(tail)] = str(parse_value(raw)[1]).strip()
        except Exception:
            pass
    return res


def get_uptime_s(host, timeout=15):
    try:
        r = subprocess.run([SNMP_GET, '-v' + SNMP_VERSION, '-c', SNMP_COMMUNITY, '-On', host, OID_SYSUPTIME],
                           capture_output=True, text=True, timeout=timeout)
        m = re.search(r'\((\d+)\)', r.stdout)
        return int(m.group(1)) // 100 if m else None
    except Exception:
        return None


def get_cpu_load(host, timeout=30):
    """hrProcessorLoad: среднее по ядрам, %."""
    vals = [v for v in walk_map(host, OID_HRCPU, timeout).values() if isinstance(v, int)]
    return round(sum(vals) / len(vals)) if vals else None


def get_traffic(host, timeout=90):
    """{ifIndex: {'in': octets, 'out': octets}}"""
    res = {}
    for idx, val in walk_map(host, OID_IFHCIN, timeout).items():
        if isinstance(val, int):
            res.setdefault(idx, {})['in'] = val
    for idx, val in walk_map(host, OID_IFHCOUT, timeout).items():
        if isinstance(val, int):
            res.setdefault(idx, {})['out'] = val
    return res


def _onu_name_to_key(name):
    m = re.match(r'(?:EPON|GPON|epon|gpon)(\d+/\d+):(\d+)$', name or '')
    return f"{m.group(1)}:{m.group(2)}" if m else None


def get_epon_onu_status(host, timeout=90):
    """{'0/1:1': True/False} — онлайн-статус ONU (индекс: port.onu либо ifIndex)."""
    raw = walk_map(host, OID_EPON_ONU_STATUS, timeout)
    res = {}
    ifindex_names = None
    for tail, val in raw.items():
        if val not in (0, 1, '0', '1'):
            continue
        parts = tail.split('.')
        if len(parts) == 2:
            res[f"0/{parts[0]}:{parts[1]}"] = (int(val) == 1)
        elif len(parts) == 1:
            if ifindex_names is None:
                ifindex_names = get_ifdescr_map(host, 45)
            key = _onu_name_to_key(ifindex_names.get(int(parts[0]), ''))
            if key:
                res[key] = (int(val) == 1)
    return res


def get_epon_onu_rx(host, timeout=120):
    """{'0/1:1': rx_db} — RX из оптической таблицы (индекс ifIndex)."""
    raw = walk_map(host, OID_EPON_ONU_RX, timeout)
    names = get_ifdescr_map(host, 45)
    res = {}
    for tail, val in raw.items():
        if len(tail.split('.')) != 1 or not isinstance(val, int):
            continue
        key = _onu_name_to_key(names.get(int(tail), ''))
        if key:
            res[key] = val / 10.0
    return res


def get_vsol_mac_table(host, timeout=90):
    """{mac_norm: ifIndex} — Q-BRIDGE (работает на VSOL)."""
    addr = {}
    for tail, raw in snmp_walk(host, OID_QBRIDGE_ADDR, timeout):
        typ, val = parse_value(raw)
        mac = hex_to_mac(val) if typ == 'Hex-STRING' else None
        if mac:
            addr[tail] = mac
    ports = walk_map(host, OID_QBRIDGE_PORT, timeout)
    res = {}
    for tail, mac in addr.items():
        if tail in ports and isinstance(ports[tail], int):
            res[mac] = ports[tail]
    return res


def find_mac_vsol(host, mac, timeout=90):
    """(имя_интерфейса, None) либо (None, сообщение)."""
    want = normalize_mac(mac)
    if not want:
        return None, 'Некорректный MAC'
    table = {k.lower(): v for k, v in get_vsol_mac_table(host, timeout).items()}
    if want in table:
        names = get_ifdescr_map(host, 60)
        if not names:
            time.sleep(2)
            names = get_ifdescr_map(host, 60)
        return names.get(table[want], f"ifIndex {table[want]}"), None
    return None, 'MAC не найден в SNMP-таблице'


OID_IFOPER = '.1.3.6.1.2.1.2.2.1.8'
OID_EPON_ONU_MAC = '.1.3.6.1.4.1.3320.101.10.1.1.3'
OID_EPON_ONU_TX = '.1.3.6.1.4.1.3320.101.10.5.1.6'


def _onu_names(host, timeout=45):
    """{ifIndex: имя} только для ONU-интерфейсов (с ':')."""
    return {i: n for i, n in get_ifdescr_map(host, timeout).items()
            if n and ':' in n}


def get_epon_onu_macs(host, timeout=90):
    """{'0/1:1': '80:f7:a6:82:a2:68'} — MAC ONU (поле .1.1.3, индекс ifIndex)."""
    names = _onu_names(host)
    res = {}
    for tail, raw in snmp_walk(host, OID_EPON_ONU_MAC, timeout):
        typ, val = parse_value(raw)
        if typ != 'Hex-STRING':
            continue
        mac = hex_to_mac(val)
        if not mac:
            continue
        parts = tail.split('.')
        if len(parts) == 1:
            key = _onu_name_to_key(names.get(int(parts[0]), ''))
        elif len(parts) == 2:
            key = f"0/{parts[0]}:{parts[1]}"
        else:
            continue
        if key:
            res[key] = mac.lower()
    return res


def get_epon_onu_tx(host, timeout=90):
    """{'0/1:1': tx_dbm} — передаваемая мощность (поле .5.1.6, x10)."""
    names = _onu_names(host)
    res = {}
    for tail, val in walk_map(host, OID_EPON_ONU_TX, timeout).items():
        if not isinstance(val, int) or len(tail.split('.')) != 1:
            continue
        key = _onu_name_to_key(names.get(int(tail), ''))
        if key:
            res[key] = val / 10.0
    return res


def get_port_oper_status(host, timeout=60):
    """{'0/1': True/False, 'GigaEthernet0/1': True, ...} — ifOperStatus физических/PON портов."""
    names = get_ifdescr_map(host, timeout)
    oper = walk_map(host, OID_IFOPER, timeout)
    res = {}
    for idx, val in oper.items():
        try:
            name = names.get(int(idx))
        except (ValueError, TypeError):
            continue
        if not name or ':' in name:
            continue
        res[name] = (val == 1)
    return res
