import pymysql
from config import Config
import sys
import re

def _normalize_mac_variants(mac: str):
    """Генерирует список возможных вариантов MAC для поиска в биллинге."""
    cleaned = re.sub(r'[^0-9a-fA-F]', '', mac)
    if len(cleaned) != 12:
        return [mac]
    dotted = f"{cleaned[0:4]}.{cleaned[4:8]}.{cleaned[8:12]}"
    colon = f"{cleaned[0:2]}:{cleaned[2:4]}:{cleaned[4:6]}:{cleaned[6:8]}:{cleaned[8:10]}:{cleaned[10:12]}"
    # Добавляем вариант без разделителей (для VSOL)
    no_sep = cleaned.lower()
    no_sep_upper = cleaned.upper()
    return list(set([
        dotted.lower(), dotted.upper(), dotted,
        colon.lower(), colon.upper(), colon,
        no_sep, no_sep_upper
    ]))

def _get_db_connection():
    password = Config.BILLING_DB_PASSWORD
    if isinstance(password, str):
        password = password.encode('utf-8')
    return pymysql.connect(
        host=Config.BILLING_DB_HOST,
        port=Config.BILLING_DB_PORT,
        user=Config.BILLING_DB_USER,
        password=password,
        database=Config.BILLING_DB_NAME,
        charset='utf8mb4',
        connect_timeout=5
    )

def get_address_from_billing(mac):
    # Проверяем, является ли это SN (для GPON)
    if ':' in mac and not re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', mac):
        return _get_address_by_sn(mac)
    
    # Для MAC-адресов (EPON)
    variants = _normalize_mac_variants(mac)
    for variant in variants:
        try:
            query_template = Config.BILLING_ADDRESS_QUERY
            query = query_template.replace('{mac}', variant)
            print(f"[BILLING] Trying query: {query}", file=sys.stderr)
            conn = _get_db_connection()
            cursor = conn.cursor()
            cursor.execute(query)
            row = cursor.fetchone()
            cursor.close()
            conn.close()
            if row:
                address = row[0]
                print(f"[BILLING] Found address with variant '{variant}': {address}", file=sys.stderr)
                return address
        except Exception as e:
            print(f"[BILLING] Error with variant '{variant}': {e}", file=sys.stderr)
    
    print(f"[BILLING] No address found for MAC {mac}", file=sys.stderr)
    return None

def _get_address_by_sn(sn):
    """Поиск адреса по SN для GPON."""
    # Приводим SN к нижнему регистру без двоеточия
    sn_clean = sn.replace(':', '').lower()
    print(f"[BILLING] Searching address by SN: {sn} -> {sn_clean}", file=sys.stderr)
    
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        
        # Ищем devid в dev_fields по key='device_sn' и value=sn_clean
        cursor.execute("SELECT devid FROM dev_fields WHERE `key` = 'device_sn' AND value = %s", (sn_clean,))
        row = cursor.fetchone()
        if not row:
            print(f"[BILLING] SN not found in dev_fields", file=sys.stderr)
            cursor.close()
            conn.close()
            return None
        
        devid = row[0]
        print(f"[BILLING] Found devid: {devid}", file=sys.stderr)
        
        # Получаем uid
        cursor.execute("SELECT uid FROM dev_user WHERE devid = %s", (devid,))
        user_row = cursor.fetchone()
        if not user_row:
            print(f"[BILLING] uid not found for devid {devid}", file=sys.stderr)
            cursor.close()
            conn.close()
            return None
        
        uid = user_row[0]
        print(f"[BILLING] Found uid: {uid}", file=sys.stderr)
        
        # Получаем адрес
        cursor.execute("SELECT CONCAT(lane, ' ', house, IF(app != '', CONCAT('/', app), '')) FROM users_view_fsb_address WHERE uid = %s", (uid,))
        addr_row = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if addr_row:
            address = addr_row[0]
            print(f"[BILLING] Found address by SN: {address}", file=sys.stderr)
            return address
        
        print(f"[BILLING] No address for uid {uid}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[BILLING] Error searching by SN: {e}", file=sys.stderr)
        return None
