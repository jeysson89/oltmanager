import pexpect
import re
import sys
import time
import subprocess

class GPONConnection:
    """Обработчик для GPON устройств."""
    def __init__(self, host, username, password, enable_password):
        self.host = host
        self.username = username
        self.password = password
        self.enable_password = enable_password
        self.session = None

    def connect(self):
        try:
            self.session = pexpect.spawn(f'/usr/bin/telnet {self.host}', timeout=30, encoding='utf-8')
            idx = self.session.expect(['Username:', 'login:', '>', '#'], timeout=15)
            if idx < 2:
                self.session.sendline(self.username)
                self.session.expect(['(?i)password:', '#'], timeout=10)
                self.session.sendline(self.password)
                idx = self.session.expect(['>', '#'], timeout=10)
            if idx == 0:
                self.session.sendline('enable')
                idx_enable = self.session.expect(['(?i)password:', '#'], timeout=10)
                if idx_enable == 0:
                    self.session.sendline(self.enable_password)
                    self.session.expect('#', timeout=10)
            return True
        except Exception as e:
            print(f"GPON Connection error: {e}", file=sys.stderr)
            return False

    def disconnect(self):
        try:
            if self.session:
                self.session.sendline('exit')
                self.session.close()
        except:
            pass

    def send_command(self, cmd, timeout=60):
        print(f"[GPON] Sending: {cmd}", file=sys.stderr)
        self.session.sendline(cmd)
        output = ""
        start_time = time.time()
        while time.time() - start_time < 120:
            try:
                idx = self.session.expect(['--More--', '#', pexpect.TIMEOUT], timeout=timeout)
                output += self.session.before
                if idx == 0:
                    self.session.send(' ')
                elif idx == 1:
                    break
                else:
                    break
            except pexpect.EOF:
                break
        ansi = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        clean = ansi.sub('', output)
        lines = clean.splitlines()
        if lines and cmd in lines[0]:
            lines.pop(0)
        clean = '\n'.join(lines)
        print(f"[GPON] Response for '{cmd}':\n{clean[:500]}...", file=sys.stderr)
        return clean

    def get_interfaces_snmp(self):
        """Получает список GPON-интерфейсов через SNMP."""
        try:
            print("[SNMP] Trying SNMP for GPON interface list...", file=sys.stderr)
            result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.2.1.2.2.1.2'],
                capture_output=True, text=True, timeout=30
            )
            snmp_output = result.stdout
            interfaces = []
            for line in snmp_output.splitlines():
                iface_match = re.search(r'STRING:\s*"?(GPON\d+/\d+)"?\s*$', line)
                if iface_match:
                    iface = iface_match.group(1).replace('GPON', '').lower()
                    interfaces.append(iface)
            interfaces = sorted(set(interfaces))
            if interfaces:
                print(f"[SNMP] GPON interfaces: {interfaces}", file=sys.stderr)
                return interfaces
            return None
        except Exception as e:
            print(f"GPON SNMP error: {e}", file=sys.stderr)
            return None

    def get_onu_data_for_scan(self, interface):
        """Получает данные ONU для GPON порта."""
        cmd = f'show gpon onu-information interface GPON {interface}'
        raw = self.send_command(cmd)
        statuses = {}
        onus = {}
        
        for line in raw.splitlines():
            line = line.strip()
            # Ищем строки вида GPON0/1:1      HWTC      T51X         HWTC:50418D4E    N/A
            match = re.match(r'(gpon\d+/\d+:\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)', line, re.IGNORECASE)
            if match:
                onu_id = match.group(1).split(':')[-1]
                vendor = match.group(2)
                model = match.group(3)
                sn = match.group(4)
                loid = match.group(5)
                onus[onu_id] = {
                    'vendor': vendor,
                    'model': model,
                    'sn': sn,
                    'loid': loid,
                    'status': 'up'
                }
                statuses[f"{interface}:{onu_id}"] = "up"
                continue
            
            # Ищем строки со статусом (active/down)
            status_match = re.match(r'(active|down|inactive)\s+(\S+)\s+(\S+)', line, re.IGNORECASE)
            if status_match:
                # Предыдущая строка была ONU
                if onus:
                    last_onu = list(onus.keys())[-1]
                    if status_match.group(1).lower() != 'active':
                        onus[last_onu]['status'] = 'down'
                        statuses[f"{interface}:{last_onu}"] = "down"
        
        # Возвращаем в формате {'onu_macs': {...}, 'onu_statuses': {...}}
        return {'onu_macs': onus, 'onu_statuses': statuses}

    def get_onu_info(self, interface, onu_id):
        """Получает информацию о сигнале GPON ONU."""
        cmd = f"show gpon interface gpon {interface}:{onu_id} onu optical-transceiver-diagnosis"
        raw = self.send_command(cmd)
        
        # Ищем строку с данными ONU: gpon0/3:1    36.4   3.3   13.4   -16.2
        data_line = None
        for line in raw.splitlines():
            if re.match(r'gpon\d+/\d+:\d+', line.strip(), re.IGNORECASE):
                data_line = line.strip()
                break
        
        result = {}
        if data_line:
            parts = data_line.split()
            # parts[0] = gpon0/3:1, parts[1] = temp, parts[2] = voltage, parts[3] = current, parts[4] = rx_power
            if len(parts) >= 5:
                result['temperature'] = parts[1]
                result['signal'] = parts[4]
        
        return result if result else None

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
        cmd = f"show gpon interface GPON {interface}:{onu_id} onu port 1 state"
        raw = self.send_command(cmd)
        # Ищем "down" или "up" в выводе
        match = re.search(r'uni-port \d+\s+(down|up)', raw, re.IGNORECASE)
        if match:
            state = match.group(1).strip()
            if state.lower() == 'up':
                return 'Link-Up'
            else:
                return 'Link-Down'
        elif "has no uni ports" in raw.lower():
            return "No UNI ports"
        else:
            return "Link-Down"

    def get_running_config(self, interface, onu_id):
        """Получает running-config для конкретного GPON ONU."""
        full_if = f"GPON{interface}:{onu_id}"
        cmd = f"show running-config interface {full_if}"
        print(f"[GPON] Getting running-config: {cmd}", file=sys.stderr)
        try:
            raw = self.send_command(cmd)
            return raw
        except Exception as e:
            print(f"[GPON] Error getting config: {e}", file=sys.stderr)
            return None

    def shutdown_interface(self, interface):
        """Выключает GPON интерфейс."""
        full_if = f"GPON{interface}"
        cmd = f"interface {full_if}"
        print(f"[GPON] Shutdown interface: {cmd}", file=sys.stderr)
        try:
            self.session.sendline('config')
            self.session.expect(r'_config#', timeout=10)
            self.session.sendline(cmd)
            self.session.expect(r'_config_gpon\d+/\d+#', timeout=10)
            self.session.sendline('shutdown')
            self.session.expect(r'_config_gpon\d+/\d+#', timeout=10)
            self.session.sendline('exit')
            self.session.expect(r'_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect(r'#', timeout=10)
            return True, f"Интерфейс {full_if} выключен"
        except Exception as e:
            print(f"[GPON] Shutdown error: {e}", file=sys.stderr)
            return False, f"Ошибка: {e}"

    def enable_interface(self, interface):
        """Включает GPON интерфейс."""
        full_if = f"GPON{interface}"
        cmd = f"interface {full_if}"
        print(f"[GPON] Enable interface: {cmd}", file=sys.stderr)
        try:
            self.session.sendline('config')
            self.session.expect(r'_config#', timeout=10)
            self.session.sendline(cmd)
            self.session.expect(r'_config_gpon\d+/\d+#', timeout=10)
            self.session.sendline('no shutdown')
            self.session.expect(r'_config_gpon\d+/\d+#', timeout=10)
            self.session.sendline('exit')
            self.session.expect(r'_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect(r'#', timeout=10)
            return True, f"Интерфейс {full_if} включен"
        except Exception as e:
            print(f"[GPON] Enable error: {e}", file=sys.stderr)
            return False, f"Ошибка: {e}"

    def reboot_onu(self, interface, onu_id):
        full_if = f"gpon{interface}:{onu_id}"
        cmd = f"gpon reboot onu interface GPON {interface}:{onu_id}"
        print(f"[GPON] Rebooting ONU: {cmd}", file=sys.stderr)
        try:
            self.session.sendline(cmd)
            idx = self.session.expect(['(y/n)?', '#', pexpect.TIMEOUT], timeout=30)
            if idx == 0:
                self.session.sendline('y')
                self.session.expect('#', timeout=60)
                return True, f"ONU {full_if} успешно перезагружен"
            return False, "Ошибка перезагрузки"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def delete_onu(self, interface, onu_id):
        full_if = f"gpon{interface}:{onu_id}"
        try:
            self.session.sendline('config')
            self.session.expect('_config#', timeout=10)
            self.session.sendline(f'interface gpon {interface}')
            self.session.expect(f'_config_gpon{interface}#', timeout=10)
            self.session.sendline(f'no gpon bind-onu sequence {onu_id}')
            self.session.expect(f'_config_gpon{interface}#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            self.session.sendline('write all')
            self.session.expect('OK!', timeout=60)
            return True, f"ONU {full_if} успешно удалён"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def set_1g_sla(self, interface, onu_id):
        full_if = f"gpon{interface}:{onu_id}"
        try:
            self.session.sendline('config')
            self.session.expect('_config#', timeout=10)
            self.session.sendline(f'interface gpon {interface}:{onu_id}')
            self.session.expect(f'_config_gpon{interface}:{onu_id}#', timeout=10)
            self.session.sendline("gpon sla upstream pir 1000000 cir 10000")
            self.session.expect(f'_config_gpon{interface}:{onu_id}#', timeout=30)
            self.session.sendline("gpon sla downstream pir 1000000 cir 10000")
            self.session.expect(f'_config_gpon{interface}:{onu_id}#', timeout=30)
            self.session.sendline('exit')
            self.session.expect('_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            self.session.sendline('write all')
            self.session.expect('OK!', timeout=60)
            return True, f"ONU {full_if}: SLA установлен в 1Gbps"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def blacklist_onu(self, interface, onu_id, mac_address):
        full_if = f"gpon{interface}:{onu_id}"
        try:
            self.session.sendline("config")
            self.session.expect("_config#", timeout=10)
            self.session.sendline(f"interface gpon {interface}")
            self.session.expect(f"_config_gpon{interface}#", timeout=10)
            cmd = f"gpon onu-blacklist mac {mac_address}"
            self.session.sendline(cmd)
            self.session.expect(f"_config_gpon{interface}#", timeout=10)
            self.session.sendline("exit")
            self.session.expect("_config#", timeout=10)
            self.session.sendline("exit")
            self.session.expect("#", timeout=10)
            return True, f"MAC {mac_address} добавлен в blacklist на {full_if}"
        except Exception as e:
            return False, f"Ошибка: {e}"
