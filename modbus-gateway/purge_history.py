#!/usr/bin/env python3
"""
Script per la pulizia della history del database MODBUS.
Mantiene solo i record degli ultimi N giorni (configurabile).
Da eseguire periodicamente via cron o task scheduler.

Basato sulla struttura del progetto:
- Database: modbus_db
- Tabella: history (configurabile tramite base_table_out in config.ini)
- Colonna timestamp: timestamp (CURRENT_TIMESTAMP)
"""

import logging
import configparser
from datetime import datetime, timedelta
from pathlib import Path
import mysql.connector
from mysql.connector import Error

# --- Configurazione ---
CONFIG_FILE = 'config.ini'
# Giorni di retention (modifica questo valore)
RETENTION_DAYS = 30

# Setup logging
LOG_DIR = Path(__file__).parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / 'purge_history.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def load_config():
    """
    Carica la configurazione dal file config.ini.
    Si aspetta una sezione [database] con i parametri di connessione
    (stessa sezione usata da install_database.py, modbus_reader.py e
    modbus_writer.py — configparser è case-sensitive sui nomi di sezione).
    """
    config = configparser.ConfigParser()
    config.read(CONFIG_FILE)

    if 'database' not in config:
        logger.error("❌ Sezione [database] non trovata in config.ini")
        logger.error("   Assicurati che il file config.ini esista e sia configurato correttamente.")
        return None

    db_config = {
        'host': config.get('database', 'host', fallback='localhost'),
        'user': config.get('database', 'user', fallback='modbus'),
        'password': config.get('database', 'password', fallback=''),
        'database': config.get('database', 'database', fallback='modbus_db'),
        # 'history' è la tabella di storico creata da install_database.py
        # (nome configurabile tramite base_table_out in config.ini).
        'table': config.get('database', 'base_table_out', fallback='history')
    }

    logger.info(f"📊 Connessione al database: {db_config['database']} su {db_config['host']}")
    return db_config

def purge_old_records(db_config, days=RETENTION_DAYS):
    """
    Elimina i record più vecchi di 'days' giorni dalla tabella robot_data.
    Utilizza la colonna 'timestamp' per determinare l'età dei record.
    """
    if not db_config:
        return False
    
    table_name = db_config.pop('table', 'robot_data')
    
    try:
        # Connessione al database
        conn = mysql.connector.connect(**db_config)
        cursor = conn.cursor()
        
        # Calcola la data limite
        cutoff_date = datetime.now() - timedelta(days=days)
        cutoff_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')
        
        logger.info(f"📅 Data limite per la retention: {cutoff_str} ({days} giorni fa)")
        
        # Prima di eliminare, conta i record da rimuovere
        count_query = f"SELECT COUNT(*) FROM {table_name} WHERE timestamp < %s"
        cursor.execute(count_query, (cutoff_str,))
        count_to_delete = cursor.fetchone()[0]
        
        if count_to_delete == 0:
            logger.info(f"✅ Nessun record da eliminare (tutti i record sono più recenti di {days} giorni).")
            cursor.close()
            conn.close()
            return True
        
        # Controlla anche il record più vecchio rimanente (per debug)
        oldest_query = f"SELECT MIN(timestamp) FROM {table_name}"
        cursor.execute(oldest_query)
        oldest_record = cursor.fetchone()[0]
        if oldest_record:
            logger.info(f"📅 Record più vecchio nel database: {oldest_record}")
        
        # Esegui la cancellazione
        delete_query = f"DELETE FROM {table_name} WHERE timestamp < %s"
        cursor.execute(delete_query, (cutoff_str,))
        conn.commit()
        
        deleted_rows = cursor.rowcount
        logger.info(f"✅ Cancellati {deleted_rows} record più vecchi di {days} giorni (prima del {cutoff_str}).")
        
        # Verifica il numero di record rimasti
        remaining_query = f"SELECT COUNT(*) FROM {table_name}"
        cursor.execute(remaining_query)
        remaining_rows = cursor.fetchone()[0]
        logger.info(f"📊 Record rimanenti nel database: {remaining_rows}")
        
        cursor.close()
        conn.close()
        return True
        
    except Error as e:
        logger.error(f"❌ Errore durante la pulizia del database: {e}")
        return False
    except Exception as e:
        logger.error(f"❌ Errore generico: {e}")
        return False

def main():
    """Funzione principale."""
    logger.info("=" * 60)
    logger.info("🚀 Avvio script di pulizia history")
    logger.info("=" * 60)
    
    # Carica la configurazione
    db_config = load_config()
    if not db_config:
        logger.error("Impossibile caricare la configurazione del database. Verifica config.ini")
        return
    
    logger.info(f"🔧 Retention configurata: {RETENTION_DAYS} giorni")
    
    # Esegui la pulizia
    success = purge_old_records(db_config, RETENTION_DAYS)
    
    if success:
        logger.info("✅ Pulizia completata con successo.")
    else:
        logger.error("❌ Pulizia fallita. Controlla i log per maggiori dettagli.")
    
    logger.info("=" * 60)
    logger.info("Script terminato.\n")

if __name__ == "__main__":
    main()