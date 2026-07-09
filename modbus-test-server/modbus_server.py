#!/usr/bin/env python3
"""
Modbus TCP Server con supporto IEEE 754.
Simula un robot e fornisce dati in virgola mobile su registri Modbus.
"""
import sys
import time
import threading
import json
import random
from struct import pack, unpack
from pyModbusTCP.server import ModbusServer, DataBank
from flask import Flask, jsonify, request
import configparser

# --- Configurazione ---
CONFIG_FILE = 'config.ini'
config = configparser.ConfigParser()
config.read(CONFIG_FILE)

# Parametri di default
SERVER_HOST = config.get('SERVER', 'host', fallback='0.0.0.0')
SERVER_PORT = config.getint('SERVER', 'port', fallback=5020)
REST_PORT = config.getint('SERVER', 'rest_port', fallback=5001)
UPDATE_INTERVAL = config.getfloat('SERVER', 'update_interval', fallback=1.0)

# Definizione dei registri (indirizzo di partenza per ogni dato)
# Un float occupa 2 registri (4 bytes)
REG_MAP = {
    'robot_x':        0,   # float: posizione X (metri)
    'robot_y':        2,   # float: posizione Y
    'robot_z':        4,   # float: posizione Z
    'robot_speed':    6,   # float: velocità (m/s)
    'robot_temp':     8,   # float: temperatura motore (°C)
    'robot_battery':  10,  # float: livello batteria (%)
    'robot_status':   12,  # int (16 bit): 0=idle, 1=running, 2=error
    'robot_charge':   14,  # float: corrente di carica (A)
}
# Totale registri necessari: (max addr in REG_MAP) + 2 (per l'ultimo float)
NB_REGISTERS = 16

# --- Inizializzazione DataBank (memoria Modbus) ---
data_bank = DataBank()
# Inizializza tutti i registri a zero
for i in range(NB_REGISTERS):
    data_bank.set_holding_register(i, 0)

# --- Variabili globali per la simulazione ---
battery_level = 85.0  # Inizializza la batteria al 85%

# --- Funzioni di utilità per IEEE 754 ---
def float_to_registers(value):
    """Converte un float in due registri a 16 bit (big-endian)."""
    packed = pack('>f', value)  # '>f' = big-endian float
    reg1 = int.from_bytes(packed[0:2], byteorder='big')
    reg2 = int.from_bytes(packed[2:4], byteorder='big')
    return reg1, reg2

def registers_to_float(reg1, reg2):
    """Converte due registri a 16 bit in un float (big-endian)."""
    packed = reg1.to_bytes(2, byteorder='big') + reg2.to_bytes(2, byteorder='big')
    return unpack('>f', packed)[0]

def set_float_register(address, value):
    """Scrive un float a partire dall'indirizzo specificato."""
    reg1, reg2 = float_to_registers(value)
    data_bank.set_holding_register(address, reg1)
    data_bank.set_holding_register(address + 1, reg2)

def get_float_register(address):
    """Legge un float a partire dall'indirizzo specificato."""
    reg1 = data_bank.get_holding_register(address)
    reg2 = data_bank.get_holding_register(address + 1)
    if reg1 is None or reg2 is None:
        return None
    return registers_to_float(reg1, reg2)

def set_int_register(address, value):
    """Scrive un intero a 16 bit."""
    data_bank.set_holding_register(address, value)

def get_int_register(address):
    """Legge un intero a 16 bit."""
    return data_bank.get_holding_register(address)

# --- Simulazione Dati Robot ---
def simulation_loop():
    """Loop che aggiorna i dati a intervalli regolari."""
    global battery_level
    
    # Valori iniziali
    x = 0.0
    y = 0.0
    z = 1.0
    speed = 0.0
    temp = 25.0
    status = 1  # running
    charge = 0.0
    
    while True:
        # Movimento casuale (camminata casuale)
        x += random.uniform(-0.5, 0.5)
        y += random.uniform(-0.5, 0.5)
        z += random.uniform(-0.2, 0.2)
        
        # Limita il movimento nello spazio
        x = max(-10.0, min(10.0, x))
        y = max(-10.0, min(10.0, y))
        z = max(0.0, min(5.0, z))
        
        # Velocità
        speed = random.uniform(0.0, 2.5)
        
        # Temperatura motore (varia con la velocità)
        temp = 25.0 + (speed * 10.0) + random.uniform(-2.0, 2.0)
        temp = max(20.0, min(80.0, temp))
        
        # Stato del robot
        status = random.choices([0, 1, 2], weights=[0.1, 0.85, 0.05])[0]
        
        # Simula scarica e ricarica batteria
        if status != 2:  # Se non in errore
            battery_level -= random.uniform(0.0, 0.2)
            if battery_level < 5.0:
                battery_level += random.uniform(0.0, 0.5)  # ricarica lenta
        battery_level = max(0.0, min(100.0, battery_level))
        
        # Corrente di carica (solo se la batteria è bassa)
        if battery_level < 30.0 and status != 2:
            charge = random.uniform(5.0, 15.0)
        else:
            charge = 0.0
        
        # Scrittura nei registri Modbus
        set_float_register(REG_MAP['robot_x'], x)
        set_float_register(REG_MAP['robot_y'], y)
        set_float_register(REG_MAP['robot_z'], z)
        set_float_register(REG_MAP['robot_speed'], speed)
        set_float_register(REG_MAP['robot_temp'], temp)
        set_float_register(REG_MAP['robot_battery'], battery_level)
        set_float_register(REG_MAP['robot_charge'], charge)
        set_int_register(REG_MAP['robot_status'], status)
        
        # Debug (opzionale)
        # print(f"X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, Speed: {speed:.2f}, Temp: {temp:.1f}, Battery: {battery_level:.1f}%, Status: {status}")
        
        time.sleep(UPDATE_INTERVAL)

# --- Server Modbus TCP (su thread separato) ---
def start_modbus_server():
    """Avvia il server Modbus TCP."""
    server = ModbusServer(host=SERVER_HOST, port=SERVER_PORT, data_bank=data_bank)
    print(f"✅ Server Modbus TCP avviato su {SERVER_HOST}:{SERVER_PORT}")
    server.start()

# --- API REST (Flask) ---
app = Flask(__name__)

@app.route('/api/robot/data', methods=['GET'])
def get_robot_data():
    """Restituisce tutti i dati del robot in formato JSON (float leggibili)."""
    data = {}
    for name, addr in REG_MAP.items():
        if name == 'robot_status':
            data[name] = get_int_register(addr)
        else:
            data[name] = get_float_register(addr)
    return jsonify(data)

@app.route('/api/robot/data/<register>', methods=['GET'])
def get_register_data(register):
    """Restituisce un singolo registro in formato JSON."""
    if register not in REG_MAP:
        return jsonify({'error': 'Registro non trovato'}), 404
    addr = REG_MAP[register]
    if register == 'robot_status':
        value = get_int_register(addr)
    else:
        value = get_float_register(addr)
    return jsonify({register: value})

@app.route('/api/robot/data/<register>', methods=['POST'])
def set_register_data(register):
    """Imposta un registro (per test). Richiede JSON con 'value'."""
    if register not in REG_MAP:
        return jsonify({'error': 'Registro non trovato'}), 404
    data = request.get_json()
    if 'value' not in data:
        return jsonify({'error': 'Campo "value" mancante'}), 400
    addr = REG_MAP[register]
    if register == 'robot_status':
        set_int_register(addr, int(data['value']))
    else:
        set_float_register(addr, float(data['value']))
    return jsonify({'status': 'ok', 'register': register, 'value': data['value']})

@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint per il health check."""
    return jsonify({'status': 'running'})

def start_rest_server():
    """Avvia il server REST Flask."""
    print(f"✅ Server REST avviato su http://{SERVER_HOST}:{REST_PORT}")
    app.run(host=SERVER_HOST, port=REST_PORT, debug=False, threaded=True)

# --- Avvio Principale ---
if __name__ == '__main__':
    print("=== Modbus IEEE 754 Server ===")
    print(f"📡 Server Modbus: {SERVER_HOST}:{SERVER_PORT}")
    print(f"🌐 Server REST: http://{SERVER_HOST}:{REST_PORT}")
    print("🔄 Simulazione robot in esecuzione...")
    print("Premi CTRL+C per terminare")
    
    # Avvia il thread per la simulazione
    sim_thread = threading.Thread(target=simulation_loop, daemon=True)
    sim_thread.start()
    
    # Avvia il server Modbus in un thread separato
    modbus_thread = threading.Thread(target=start_modbus_server, daemon=True)
    modbus_thread.start()
    
    # Avvia il server REST (bloccante, in esecuzione sul thread principale)
    start_rest_server()