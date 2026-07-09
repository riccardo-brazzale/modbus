#!/usr/bin/env python3
"""
Modbus TCP Server con supporto IEEE 754 usando pymodbus.
Simula un robot e fornisce dati in virgola mobile su registri Modbus.
Solo server Modbus - nessuna API REST.
"""
import time
import threading
import random
import logging
from struct import pack, unpack

from pymodbus.server import StartTcpServer
from pymodbus.datastore import ModbusSlaveContext, ModbusServerContext
from pymodbus.datastore import ModbusSequentialDataBlock
from pymodbus.transaction import ModbusRtuFramer

import configparser

# --- Configurazione Logging ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# --- Configurazione ---
CONFIG_FILE = 'config.ini'
config = configparser.ConfigParser()
config.read(CONFIG_FILE)

# Parametri di default
SERVER_HOST = config.get('SERVER', 'host', fallback='0.0.0.0')
SERVER_PORT = config.getint('SERVER', 'port', fallback=5020)
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
# Totale registri necessari
NB_REGISTERS = 16

# --- Inizializzazione DataStore (memoria Modbus) ---
store = ModbusSlaveContext(
    di=ModbusSequentialDataBlock(0, [0]*100),      # Discrete Inputs
    co=ModbusSequentialDataBlock(0, [0]*100),      # Coils
    hr=ModbusSequentialDataBlock(0, [0]*NB_REGISTERS),  # Holding Registers
    ir=ModbusSequentialDataBlock(0, [0]*100)       # Input Registers
)
context = ModbusServerContext(slaves=store, single=True)

# --- Variabili globali per la simulazione ---
battery_level = 85.0

# --- Funzioni di utilità per IEEE 754 ---
def float_to_registers(value):
    """Converte un float in due registri a 16 bit (big-endian)."""
    packed = pack('>f', value)
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
    context[0].setValues(3, address, [reg1, reg2])  # 3 = Holding Register

def get_float_register(address):
    """Legge un float a partire dall'indirizzo specificato."""
    regs = context[0].getValues(3, address, count=2)
    if regs is None or len(regs) < 2:
        return None
    return registers_to_float(regs[0], regs[1])

def set_int_register(address, value):
    """Scrive un intero a 16 bit."""
    context[0].setValues(3, address, [value])

def get_int_register(address):
    """Legge un intero a 16 bit."""
    regs = context[0].getValues(3, address, count=1)
    return regs[0] if regs else None

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
    status = 1
    charge = 0.0
    
    logger.info("🔄 Simulazione robot avviata")
    
    while True:
        try:
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
                    battery_level += random.uniform(0.0, 0.5)
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
            
            # Log di debug (ogni 10 cicli per non inondare)
            if random.random() < 0.1:  # ~10% dei cicli
                logger.debug(f"X: {x:.2f}, Y: {y:.2f}, Z: {z:.2f}, "
                           f"Speed: {speed:.2f}, Temp: {temp:.1f}, "
                           f"Battery: {battery_level:.1f}%, Status: {status}")
            
        except Exception as e:
            logger.error(f"Errore nella simulazione: {e}")
        
        time.sleep(UPDATE_INTERVAL)

# --- Avvio Principale ---
def main():
    """Funzione principale che avvia il server Modbus."""
    logger.info("=== Modbus IEEE 754 Server (pymodbus) ===")
    logger.info(f"📡 Server Modbus TCP: {SERVER_HOST}:{SERVER_PORT}")
    logger.info("🔄 Simulazione robot in esecuzione...")
    logger.info("Premi CTRL+C per terminare")
    
    # Avvia il thread per la simulazione
    sim_thread = threading.Thread(target=simulation_loop, daemon=True)
    sim_thread.start()
    
    # Avvia il server Modbus (bloccante)
    try:
        StartTcpServer(
            context=context,
            address=(SERVER_HOST, SERVER_PORT),
            allow_reuse_address=True,
            framer=ModbusRtuFramer
        )
    except KeyboardInterrupt:
        logger.info("\n🛑 Server terminato dall'utente")
    except Exception as e:
        logger.error(f"❌ Errore nel server Modbus: {e}")
        raise

if __name__ == '__main__':
    main()