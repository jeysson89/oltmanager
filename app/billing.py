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

def get_client_name_from_mac(mac):
    """Ищет название организации по MAC в таблице clients."""
    from app.models import Client
    cleaned = re.sub(r'[^0-9a-fA-F]', '', mac).lower()
    if len(cleaned) != 12:
        return None
    # Ищем в БД
    try:
        client = Client.query.filter_by(mac=cleaned).first()
        if client:
            return client.name
    except Exception as e:
        print(f"[CLIENT] Error searching client: {e}", file=sys.stderr)
    return None

def _normalize_sn_variants(sn):
    """Генерирует варианты SN для поиска в биллинге."""
    variants = set()
    # Оригинал
    variants.add(sn)
    # Без двоеточий
    no_colon = sn.replace(':', '')
    variants.add(no_colon)
    # Верхний, нижний регистр
    variants.add(no_colon.upper())
    variants.add(no_colon.lower())
    # С двоеточием в разных регистрах
    if ':' not in sn and len(no_colon) > 4:
        variants.add(no_colon[:4] + ':' + no_colon[4:])
        variants.add(no_colon[:4].upper() + ':' + no_colon[4:].upper())
        variants.add(no_colon[:4].lower() + ':' + no_colon[4:].lower())
    # Только hex-часть (без префикса HWTC и т.п.)
    hex_only = re.sub(r'[^0-9a-fA-F]', '', no_colon)
    if len(hex_only) >= 8:
        # Последние 8 символов (часто SN = префикс + 8 hex)
        variants.add(hex_only[-8:])
        variants.add(hex_only[-8:].upper())
        variants.add(hex_only[-8:].lower())
    return list(variants)


def _get_address_by_sn(sn):
    """Поиск адреса по SN для GPON с перебором вариантов."""
    variants = _normalize_sn_variants(sn)
    print(f"[BILLING] SN variants to try: {variants}", file=sys.stderr)
    
    for variant in variants:
        try:
            conn = _get_db_connection()
            cursor = conn.cursor()
            
            cursor.execute("SELECT devid FROM dev_fields WHERE `key` = 'device_sn' AND value = %s", (variant,))
            row = cursor.fetchone()
            if not row:
                cursor.close()
                conn.close()
                print(f"[BILLING] SN variant '{variant}' not found", file=sys.stderr)
                continue
            
            devid = row[0]
            print(f"[BILLING] Found devid {devid} for variant '{variant}'", file=sys.stderr)
            
            cursor.execute("SELECT uid FROM dev_user WHERE devid = %s", (devid,))
            user_row = cursor.fetchone()
            if not user_row:
                cursor.close()
                conn.close()
                print(f"[BILLING] uid not found for devid {devid}", file=sys.stderr)
                continue
            
            uid = user_row[0]
            
            cursor.execute("SELECT CONCAT(lane, ' ', house, IF(app != '', CONCAT('/', app), '')) FROM users_view_fsb_address WHERE uid = %s", (uid,))
            addr_row = cursor.fetchone()
            cursor.close()
            conn.close()
            
            if addr_row:
                address = addr_row[0]
                print(f"[BILLING] Found address by SN '{variant}': {address}", file=sys.stderr)
                return address
        except Exception as e:
            print(f"[BILLING] Error with SN variant '{variant}': {e}", file=sys.stderr)
    
    print(f"[BILLING] No address found for SN {sn}", file=sys.stderr)
    return None
