import pexpect
import re
import sys
import time
import subprocess
import threading

_vsol_lock = threading.Lock()

class VSOLConnection:
    """Обработчик для VSOL OLT устройств."""
    def __init__(self, host, username, password, enable_password):
        self.host = host
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.session = None

    def connect(self):
        global _vsol_lock
        _vsol_lock.acquire()
        try:
            self.session = pexpect.spawn(f'/usr/bin/telnet {self.host}', timeout=30, encoding='utf-8')
            # VSOL запрашивает Login: (с большой L)
            idx = self.session.expect(['Login:', 'Username:', 'login:', '>', '#'], timeout=15)
            if idx < 3:
                self.session.sendline(self.username)
                self.session.expect(['(?i)Password:', '(?i)password:'], timeout=10)
                self.session.sendline(self.password)
                idx = self.session.expect(['>', '#'], timeout=10)
            if idx == 0:  # '>'
                self.session.sendline('enable')
                idx_enable = self.session.expect(['(?i)Password:', '(?i)password:', '#'], timeout=10)
                if idx_enable < 2:
                    self.session.sendline(self.enable_password)
                    self.session.expect('#', timeout=10)
            return True
        except Exception as e:
            print(f"VSOL Connection error: {e}", file=sys.stderr)
            return False

    def disconnect(self):
        global _vsol_lock
        try:
            if self.session:
                self.session.sendline('exit')
                self.session.close()
        except:
            pass
        finally:
            _vsol_lock.release()

    def send_command(self, cmd, timeout=60):
        print(f"[VSOL] Sending: {cmd}", file=sys.stderr)
        self.session.sendline(cmd)
        
        # Ждем и собираем вывод
        output = ""
        start_time = time.time()
        
        # Сначала ждем промпт (команда может вернуть промпт сразу)
        # Потом ждем еще немного для получения полного вывода
        prompts = [
            r'epon-olt-usp\(config-pon-\d+/\d+\)#',  # epon-olt-usp(config-pon-0/1)#
            r'epon-olt-usp\(config\)#',                 # epon-olt-usp(config)#
            r'epon-olt-usp#',                             # epon-olt-usp#
            r'epon-olt-usp>',                             # epon-olt-usp>
            r'\(config-pon-\d+/\d+\)#',              # (config-pon-0/1)#
            r'\(config\)#',                             # (config)#
            r'#',                                         # #
            r'>',                                         # >
            '--More--',                                   # --More--
            pexpect.TIMEOUT                               # Таймаут
        ]
        
        while time.time() - start_time < 120:
            try:
                idx = self.session.expect(prompts, timeout=timeout)
                output += self.session.before
                
                if idx <= 7:
                    # Получили промпт - но нам нужно подождать еще для полного вывода
                    # Проверяем, есть ли еще данные
                    time.sleep(0.5)
                    # Пытаемся прочитать еще немного
                    try:
                        more = self.session.read_nonblocking(size=1000, timeout=1)
                        if more:
                            output += more
                    except:
                        pass
                    break
                elif idx == 8:
                    # --More-- - отправляем пробел для продолжения
                    self.session.send(' ')
                else:
                    # Таймаут
                    print(f"[VSOL] Timeout waiting for prompt after '{cmd}'", file=sys.stderr)
                    break
                    
            except pexpect.EOF:
                print(f"[VSOL] EOF while waiting for '{cmd}'", file=sys.stderr)
                break
        
        ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean = ansi.sub('', output)
        lines = clean.splitlines()
        if lines and cmd in lines[0]:
            lines.pop(0)
        clean = '\n'.join(lines)
        print(f"[VSOL] Response for '{cmd}':\n{clean[:500]}...", file=sys.stderr)
        return clean
    def get_interfaces_snmp(self):
        try:
            print("[SNMP] Trying SNMP for VSOL interface list...", file=sys.stderr)
            result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.2.1.2.2.1.2'],
                capture_output=True, text=True, timeout=15
            )
            snmp_output = result.stdout
            interfaces = []
            for line in snmp_output.splitlines():
                iface_match = re.search(r'STRING:\s*"?(EPON\d+/\d+)"?\s*$', line)
                if iface_match:
                    iface = iface_match.group(1).replace('EPON', '').lower()
                    interfaces.append(iface)
            interfaces = sorted(set(interfaces))
            if interfaces:
                print(f"[SNMP] VSOL interfaces: {interfaces}", file=sys.stderr)
                return interfaces
            return None
        except:
            return None

    def get_onu_data_for_scan(self, interface):
        """Получает данные ONU для VSOL порта."""
        self.session.sendline('configure terminal')
        self.session.expect(r'\(config\)#', timeout=10)
        
        # Проверяем формат интерфейса
        # interface может быть "0/1" или "0/1"
        # Нормализуем к формату EPON0/1
        if not interface.startswith('EPON'):
            interface_norm = f'EPON{interface}'
        else:
            interface_norm = interface
        
        cmd = 'show onu auth-info all'
        raw = self.send_command(cmd)
        
        statuses = {}
        onus = {}
        
        for line in raw.splitlines():
            line_clean = ' '.join(line.split())
            # Парсим строки вида EPON0/1:1   27     online    e0:e8:e6:49:83:13    850     sadovaya19
            match = re.match(r'(EPON\d+/\d+:\d+)\s+\d+\s+(online|offline)\s+([0-9a-fA-F:]+)\s+\d+\s+(\S+)', line_clean)
            if match:
                full_if = match.group(1)
                # Проверяем, относится ли ONU к запрошенному интерфейсу
                if full_if.startswith(interface_norm + ':'):
                    onu_id = full_if.split(':')[-1]
                    status = 'up' if match.group(2).lower() == 'online' else 'down'
                    mac = match.group(3)
                    description = match.group(4)
                    
                    onus[onu_id] = {
                        'mac': mac,
                        'description': description,
                        'status': status
                    }
                    statuses[f"{interface}:{onu_id}"] = status
        
        self.session.sendline('exit')
        self.session.expect('#', timeout=10)
        
        return {'onu_macs': onus, 'onu_statuses': statuses}

    def get_onu_info(self, interface, onu_id):
        """Получает информацию о сигнале VSOL ONU."""
        self.session.sendline('configure terminal')
        self.session.expect(r'\(config\)#', timeout=10)
        
        cmd = 'show onu opm-diag all'
        raw = self.send_command(cmd)
        
        result = {}
        for line in raw.splitlines():
            line_clean = ' '.join(line.split())
            # Проверяем, относится ли строка к запрошенному интерфейсу
            if f"EPON{interface}:{onu_id}" in line_clean:
                parts = line_clean.split()
                if len(parts) >= 6:
                    result['temperature'] = parts[1].split('.')[0]
                    result['signal'] = parts[5]
        
        self.session.sendline('exit')
        self.session.expect('#', timeout=10)
        
        return result if result else None

    def get_all_onu_info(self, interface):
        """Получает информацию обо всех ONU за один запрос."""
        self.session.sendline('configure terminal')
        self.session.expect(r'\(config\)#', timeout=10)
        
        cmd = 'show onu opm-diag all'
        raw = self.send_command(cmd)
        
        self.session.sendline('exit')
        self.session.expect('#', timeout=10)
        
        result = {}
        for line in raw.splitlines():
            line_clean = ' '.join(line.split())
            # Ищем строки вида EPON0/1:1   29.82   3.34   15.75   1.41   -22.52
            match = re.match(r'(EPON\d+/\d+:\d+)\s+([\d.]+)\s+[\d.]+\s+[\d.]+\s+[\d.-]+\s+([\d.-]+)', line_clean)
            if match:
                onu_id = match.group(1).split(':')[-1]
                result[onu_id] = {
                    'temperature': match.group(2).split('.')[0],
                    'signal': match.group(3)
                }
        
        return result

    def get_onu_mac_table(self, interface, onu_id):
        """Получает MAC-таблицу для конкретного ONU."""
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline(f'int epon {interface}')
            self.session.expect(r'\(config-pon-', timeout=10)
            cmd = f'show onu {onu_id} mac-address-table'
            raw = self.send_command(cmd)
            self.session.sendline('exit')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            
            # Парсим MAC-адреса и VLAN из ответа
            macs = []
            vlan = None
            for line in raw.splitlines():
                line_clean = ' '.join(line.split())
                # Ищем MAC-адреса в формате XX:XX:XX:XX:XX:XX
                mac_match = re.search(r'([0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2})', line_clean)
                if mac_match:
                    mac = mac_match.group(1)
                    macs.append(mac)
                    # Ищем VLAN - это второй элемент (после Index)
                    parts = line_clean.split()
                    if len(parts) >= 3 and parts[1].isdigit():
                        vlan = parts[1]
            
            return True, f"MAC таблица для ONU {onu_id} получена", macs, vlan
        except Exception as e:
            return False, f"Ошибка: {e}", [], None

    def get_onu_link_state(self, interface, onu_id):
        """Получает состояние порта ONU."""
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline(f'int epon {interface}')
            self.session.expect(r'\(config-pon-', timeout=10)
            cmd = f'show onu {onu_id} ctc eth 1 linkstate'
            raw = self.send_command(cmd)
            self.session.sendline('exit')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            return True, f"Link state для ONU {onu_id} получен"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def reset_onu(self, interface, onu_id):
        """Сбрасывает ONU."""
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline(f'int epon {interface}')
            self.session.expect(r'\(config-pon-', timeout=10)
            cmd = f'onu {onu_id} ctc reset'
            self.session.sendline(cmd)
            self.session.expect(r'\(config-pon-', timeout=30)
            self.session.sendline('exit')
            self.session.expect(r'\(config\)#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            return True, f"ONU {onu_id} сброшен"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def get_mac_table(self, interface):
        cmd = f'show mac address-table interface {interface}'
        raw = self.send_command(cmd)
        macs = []
        vlan = None
        for line in raw.splitlines():
            line_clean = ' '.join(line.split())
            mac_match = re.search(r'([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})', line_clean)
            if mac_match:
                parts = line_clean.split()
                if parts and parts[0].isdigit():
                    vlan = parts[0]
                macs.append(mac_match.group(1))
        return {'macs': macs, 'vlan': vlan}

    def get_lan_state(self, interface, onu_id):
        cmd = f"show onu port state interface EPON {interface}:{onu_id}"
        raw = self.send_command(cmd)
        if 'up' in raw.lower():
            return 'Link-Up'
        elif 'down' in raw.lower():
            return 'Link-Down'
        return 'Link-Down'

    def get_running_config(self, interface, onu_id):
        """Получает running-config для конкретного VSOL ONU."""
        full_if = f"EPON{interface}:{onu_id}"
        cmd = f"show running-config interface {full_if}"
        print(f"[VSOL] Getting running-config: {cmd}", file=sys.stderr)
        try:
            raw = self.send_command(cmd)
            return raw
        except Exception as e:
            print(f"[VSOL] Error getting config: {e}", file=sys.stderr)
            return None

    def reboot_onu(self, interface, onu_id):
        full_if = f"EPON{interface}:{onu_id}"
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            cmd = f"onu reboot interface EPON {interface}:{onu_id}"
            self.session.sendline(cmd)
            self.session.expect(r'\(config\)#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            return True, f"ONU {full_if}: команда перезагрузки отправлена"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def delete_onu(self, interface, onu_id):
        full_if = f"EPON{interface}:{onu_id}"
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            cmd = f"no onu interface EPON {interface}:{onu_id}"
            self.session.sendline(cmd)
            self.session.expect(r'\(config\)#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            self.session.sendline('write')
            self.session.expect('#', timeout=30)
            return True, f"ONU {full_if} удалён"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def set_1g_sla(self, interface, onu_id):
        full_if = f"EPON{interface}:{onu_id}"
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            cmd_up = f"onu sla upstream pir 1000000 cir 10000 interface EPON {interface}:{onu_id}"
            self.session.sendline(cmd_up)
            self.session.expect(r'\(config\)#', timeout=30)
            cmd_down = f"onu sla downstream pir 1000000 cir 10000 interface EPON {interface}:{onu_id}"
            self.session.sendline(cmd_down)
            self.session.expect(r'\(config\)#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            return True, f"ONU {full_if}: SLA 1Gbps установлен"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def blacklist_onu(self, interface, onu_id, mac_address):
        full_if = f"EPON{interface}:{onu_id}"
        try:
            self.session.sendline('configure terminal')
            self.session.expect(r'\(config\)#', timeout=10)
            cmd = f"onu blacklist mac {mac_address}"
            self.session.sendline(cmd)
            self.session.expect(r'\(config\)#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            return True, f"MAC {mac_address} в blacklist на {full_if}"
        except Exception as e:
            return False, f"Ошибка: {e}"
