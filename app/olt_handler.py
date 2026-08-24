import pexpect
import re
import sys
import time
import subprocess

class OLTConnection:
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
            print(f"Connection error: {e}", file=sys.stderr)
            return False

    def disconnect(self):
        try:
            if self.session:
                self.session.sendline('exit')
                self.session.close()
        except:
            pass

    def send_command(self, cmd, timeout=60):
        print(f"[TELNET] Sending: {cmd}", file=sys.stderr)
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
        print(f"[TELNET] Response for '{cmd}':\n{clean[:500]}...", file=sys.stderr)
        return clean

    def get_running_config(self, interface, onu_id):
        """Получает running-config для конкретного ONU."""
        full_if = f"EPON{interface}:{onu_id}"
        cmd = f"show running-config interface {full_if}"
        print(f"[TELNET] Getting running-config: {cmd}", file=sys.stderr)
        try:
            raw = self.send_command(cmd)
            # Очистим лишние строки, если нужно
            return raw
        except Exception as e:
            print(f"[TELNET] Error getting config: {e}", file=sys.stderr)
            return None

    def reboot_onu(self, interface, onu_id):
        full_if = f"epon{interface}:{onu_id}"
        cmd = f"epon reboot onu interface EPON {interface}:{onu_id}"
        print(f"[TELNET] Rebooting ONU: {cmd}", file=sys.stderr)
        try:
            self.session.sendline(cmd)
            idx = self.session.expect(['(y/n)?', '#', pexpect.TIMEOUT], timeout=30)
            if idx == 0:
                self.session.sendline('y')
                self.session.expect('#', timeout=60)
                output = self.session.before
                print(f"[TELNET] Reboot response: {output}", file=sys.stderr)
                if "Error" in output or "failed" in output.lower():
                    return False, f"Ошибка при перезагрузке {full_if}"
                return True, f"ONU {full_if} успешно перезагружен"
            elif idx == 1:
                return False, "Команда не запросила подтверждение, возможно, ONU не существует"
            else:
                return False, "Таймаут ожидания подтверждения"
        except Exception as e:
            return False, f"Ошибка: {e}"

    def delete_onu(self, interface, onu_id):
        full_if = f"epon{interface}:{onu_id}"
        try:
            self.session.sendline('enable')
            self.session.expect('#', timeout=10)
            self.session.sendline('config')
            self.session.expect('_config#', timeout=10)
            self.session.sendline(f'interface epon {interface}:{onu_id}')
            self.session.expect(f'_config_epon{interface}:{onu_id}#', timeout=10)
            self.session.sendline(f'no epon bind-onu sequence {onu_id}')
            idx = self.session.expect([f'_config_epon{interface}#', '#', pexpect.TIMEOUT], timeout=30)
            if idx == 2:
                return False, f"Таймаут при удалении {full_if}"
            self.session.sendline('exit')
            self.session.expect('_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            self.session.sendline('write all')
            self.session.expect('OK!', timeout=60)
            self.session.expect('#', timeout=60)
            return True, f"ONU {full_if} успешно удалён и конфигурация сохранена"
        except Exception as e:
            return False, f"Ошибка удаления {full_if}: {e}"

    def get_lan_state(self, interface, onu_id):
        cmd = f"show epon interface EPON {interface}:{onu_id} onu port 1 state"
        raw = self.send_command(cmd)
        match = re.search(r'Hardware state is (.+)', raw, re.IGNORECASE)
        if match:
            state = match.group(1).strip()
            speed_match = re.search(r'Speed is (.+)', raw, re.IGNORECASE)
            duplex_match = re.search(r'Duplex is (.+)', raw, re.IGNORECASE)
            result = state
            if speed_match:
                result += " " + speed_match.group(1)
            if duplex_match:
                result += " " + duplex_match.group(1)
            return result
        elif "has no uni ports" in raw.lower():
            return "No UNI ports"
        else:
            return "Link-Down"

    def get_onu_info(self, interface, onu_id):
        cmd = f"show epon interface epon {interface}:{onu_id} onu ctc optical-transceiver-diagnosis"
        raw = self.send_command(cmd)
        signal_match = re.search(r'received power\(DBm\):\s*([-+]?\d+\.\d+)', raw, re.IGNORECASE)
        temp_match = re.search(r'operating temperature\(degree\):\s*(\d+)', raw, re.IGNORECASE)
        result = {}
        if signal_match:
            result['signal'] = signal_match.group(1)
        if temp_match:
            result['temperature'] = temp_match.group(1)
        return result if result else None

    def set_1g_sla(self, interface, onu_id):
        full_if = f"epon{interface}:{onu_id}"
        try:
            # Входим в режим конфигурации
            self.session.sendline('config')
            self.session.expect('_config#', timeout=10)
            self.session.sendline(f'interface epon {interface}:{onu_id}')
            self.session.expect(f'_config_epon{interface}:{onu_id}#', timeout=10)
            
            # Отправляем SLA команды
            cmd_up = "epon sla upstream pir 1000000 cir 10000"
            print(f"[TELNET] Setting 1G SLA upstream: {cmd_up}", file=sys.stderr)
            self.session.sendline(cmd_up)
            self.session.expect(f'_config_epon{interface}:{onu_id}#', timeout=30)
            
            cmd_down = "epon sla downstream pir 1000000 cir 10000"
            print(f"[TELNET] Setting 1G SLA downstream: {cmd_down}", file=sys.stderr)
            self.session.sendline(cmd_down)
            self.session.expect(f'_config_epon{interface}:{onu_id}#', timeout=30)
            
            # Сохраняем конфигурацию
            self.session.sendline('exit')
            self.session.expect('_config#', timeout=10)
            self.session.sendline('exit')
            self.session.expect('#', timeout=10)
            self.session.sendline('write all')
            self.session.expect('OK!', timeout=60)
            self.session.expect('#', timeout=60)
            
            return True, f"ONU {full_if}: SLA установлен в 1Gbps (PIR 1000000, CIR 10000)"
        except Exception as e:
            return False, f"Ошибка установки SLA для {full_if}: {e}"

    def blacklist_onu(self, interface, onu_id, mac_address):
        full_if = f"epon{interface}:{onu_id}"
        try:
            self.session.sendline("config")
            self.session.expect("_config#", timeout=10)
            self.session.sendline(f"interface epon {interface}")
            self.session.expect(f"_config_epon{interface}#", timeout=10)
            cmd = f"epon onu-blacklist mac {mac_address}"
            print(f"[TELNET] Blacklisting MAC: {cmd}", file=sys.stderr)
            self.session.sendline(cmd)
            self.session.expect(f"_config_epon{interface}#", timeout=10)
            self.session.sendline("exit")
            self.session.expect("_config#", timeout=10)
            self.session.sendline("exit")
            self.session.expect("#", timeout=10)
            return True, f"MAC {mac_address} добавлен в blacklist на {full_if}"
        except Exception as e:
            return False, f"Ошибка blacklist для {full_if}: {e}"

    def get_interfaces_snmp(self):
        try:
            print("[SNMP] Trying SNMP for interface list...", file=sys.stderr)
            result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.2.1.2.2.1.2'],
                capture_output=True, text=True, timeout=30
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
                print(f"[SNMP] Success: {len(interfaces)} EPON interfaces", file=sys.stderr)
                return interfaces
            else:
                print("[SNMP] No EPON interfaces found", file=sys.stderr)
                return None
        except Exception as e:
            print(f"[SNMP] Failed: {e}", file=sys.stderr)
            return None

    def get_all_onu_data_snmp(self):
        """Собирает данные ONU через SNMP. Возвращает {iface: {onu_id: {mac, signal, status}}}"""
        result_data = {}
        
        # Получаем MAC-адреса
        try:
            mac_result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.4.1.3320.101.10.4.1.1'],
                capture_output=True, text=True, timeout=30
            )
            for line in mac_result.stdout.splitlines():
                oid_match = re.search(r'\.(\d+)\.(\d+)\s*=\s*Hex-STRING:\s*([0-9A-Fa-f\s]+)', line)
                if oid_match:
                    port_num = int(oid_match.group(1))
                    onu_num = int(oid_match.group(2))
                    hex_str = oid_match.group(3).replace(' ', '')
                    if len(hex_str) == 12:
                        mac = ':'.join(hex_str[i:i+2] for i in range(0, 12, 2))
                        iface = f"0/{port_num}"
                        onu_id = str(onu_num)
                        if iface not in result_data:
                            result_data[iface] = {}
                        result_data[iface][onu_id] = {"mac": mac, "signal": None, "status": "up"}
        except Exception as e:
            print(f"SNMP MAC collect error: {e}", file=sys.stderr)
        
        # Получаем сигналы
        try:
            signal_result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.4.1.3320.101.10.5.1.5'],
                capture_output=True, text=True, timeout=30
            )
            for line in signal_result.stdout.splitlines():
                oid_match = re.search(r'\.(\d+)\.(\d+)\s*=\s*INTEGER:\s*(-?\d+)', line)
                if oid_match:
                    port_num = int(oid_match.group(1))
                    onu_num = int(oid_match.group(2))
                    signal_raw = int(oid_match.group(3))
                    signal_db = signal_raw / 10
                    iface = f"0/{port_num}"
                    onu_id = str(onu_num)
                    if iface in result_data and onu_id in result_data[iface]:
                        result_data[iface][onu_id]["signal"] = str(signal_db)
        except Exception as e:
            print(f"SNMP signal collect error: {e}", file=sys.stderr)
        
        # Получаем статусы
        try:
            status_result = subprocess.run(
                ['/usr/bin/snmpwalk', '-v2c', '-c', 'public', self.host, '.1.3.6.1.4.1.3320.101.10.1.1.26'],
                capture_output=True, text=True, timeout=30
            )
            for line in status_result.stdout.splitlines():
                oid_match = re.search(r'\.(\d+)\.(\d+)\s*=\s*INTEGER:\s*(\d+)', line)
                if oid_match:
                    port_num = int(oid_match.group(1))
                    onu_num = int(oid_match.group(2))
                    status_val = int(oid_match.group(3))
                    iface = f"0/{port_num}"
                    onu_id = str(onu_num)
                    if iface in result_data and onu_id in result_data[iface]:
                        result_data[iface][onu_id]["status"] = "up" if status_val in (1, 3) else "down"
        except Exception as e:
            print(f"SNMP status collect error: {e}", file=sys.stderr)
        
        # Статистика
        for iface in sorted(result_data.keys()):
            print(f"[SNMP] Port {iface}: {len(result_data[iface])} ONUs", file=sys.stderr)
        
        total = sum(len(onus) for onus in result_data.values())
        print(f"[SNMP] Total: {total} ONUs across {len(result_data)} ports", file=sys.stderr)
        
        return result_data if result_data else None

    def get_interfaces_and_statuses_telnet(self):
        print("[TELNET] SNMP failed, getting interfaces and statuses via Telnet...", file=sys.stderr)
        raw = self.send_command('show interface brief')
        interfaces = sorted(set(re.findall(r'epon(\d+/\d+)\s', raw)))
        statuses = {}
        for line in raw.splitlines():
            match = re.match(r'(epon\d+/\d+:\d+)\s+(up|down)', line, re.IGNORECASE)
            if match:
                full = match.group(1)
                status = match.group(2).lower()
                short = full.replace('epon', '')
                statuses[short] = status
        print(f"[TELNET] Found {len(interfaces)} interfaces, {len(statuses)} ONU statuses", file=sys.stderr)
        return interfaces, statuses

    def get_onu_data_for_scan(self, interface):
        cmd = f'show epon onu-information interface EPON {interface}'
        raw = self.send_command(cmd)
        statuses = {}
        macs = {}
        for line in raw.splitlines():
            line = line.strip()
            if re.match(r'epon\d+/\d+:\d+', line, re.IGNORECASE):
                parts = line.split()
                if len(parts) >= 4:
                    onu_id = parts[0].split(':')[-1]
                    mac_candidate = parts[3] if len(parts) > 3 else ''
                    if re.match(r'[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4}', mac_candidate):
                        macs[onu_id] = mac_candidate
                        statuses[f"{interface}:{onu_id}"] = "up"
        return statuses, macs

    def get_mac_table(self, interface):
        cmd = f'show mac address-table interface {interface}'
        raw = self.send_command(cmd)
        macs = []
        vlan = None
        for line in raw.splitlines():
            mac_match = re.search(r'([0-9a-fA-F]{4}\.[0-9a-fA-F]{4}\.[0-9a-fA-F]{4})', line)
            if mac_match:
                parts = line.split()
                if parts and parts[0].isdigit():
                    vlan = parts[0]
                macs.append(mac_match.group(1))
        return {'macs': macs, 'vlan': vlan}
