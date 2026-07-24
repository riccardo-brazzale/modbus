#!/usr/bin/env python3
"""
INSTALLAZIONE DATABASE MODBUS / ROBOT

Crea:
 - database
 - registers
 - command
 - history
 - current_state (stato macchina)

Include:
 - Trigger anti-scrittura su registri ro
 - Indici ottimizzati
 - Bootstrap automatico di current_state da history
"""

import mysql.connector
import configparser
import sys
import json
from logging_utils import setup_logger
from contextlib import contextmanager

log = setup_logger("install", "log.log")


# ==========================================================
# CONTEXT MANAGER
# ==========================================================

@contextmanager
def db_connection(config: dict):
    conn = None
    try:
        conn = mysql.connector.connect(**config)
        yield conn
    except mysql.connector.Error as e:
        log.error(f"MySQL error: {e}")
        print(f"❌ Errore MySQL: {e}")
        yield None
    finally:
        if conn:
            conn.close()


@contextmanager
def db_cursor(conn):
    if not conn:
        yield None
        return
    cursor = conn.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


# ==========================================================
# INSTALLER
# ==========================================================

class DatabaseInstaller:

    def __init__(self, config_path="config.ini"):
        self.config_path = config_path
        self._load_config()

        print("=" * 60)
        print("🛠️  INSTALLAZIONE DATABASE MODBUS / ROBOT")
        print("=" * 60)

    # ------------------------------------------------------
    def _load_config(self):
        cfg = configparser.ConfigParser()
        cfg.read(self.config_path)

        self.db_config = {
            "host":     cfg["database"].get("host"),
            "user":     cfg["database"].get("user"),
            "password": cfg["database"].get("password"),
        }

        self.database_name = cfg["database"].get("database")
        self.table_in      = cfg["database"].get("base_table_in")
        self.table_out     = cfg["database"].get("base_table_out")

    # ------------------------------------------------------
    def test_mysql_connection(self):
        print(f"🔌 Test connessione MySQL su {self.db_config['host']}...")
        with db_connection(self.db_config) as conn:
            if conn:
                print("✅ Connessione MySQL OK")
                return True
        return False

    # ------------------------------------------------------
    def create_database(self):
        if not self.test_mysql_connection():
            return False

        with db_connection(self.db_config) as conn, db_cursor(conn) as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{self.database_name}` "
                f"DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(f"USE `{self.database_name}`")
            print(f"📦 Database `{self.database_name}` pronto")

        self.db_config["database"] = self.database_name
        return True

    # ------------------------------------------------------
    def create_tables(self):

        if not self.create_database():
            return False

        with db_connection(self.db_config) as conn, db_cursor(conn) as cursor:

            print("\n📊 Creazione tabelle...")

            # ==================================================
            # REGISTERS
            # ==================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS registers (
                    registro_modbus  VARCHAR(32) PRIMARY KEY,
                    tipo_registro    VARCHAR(2)  NOT NULL,
                    registro_robot   VARCHAR(32),
                    descrizione      VARCHAR(255),
                    accesso          ENUM('ro','rw') NOT NULL DEFAULT 'ro',
                    data_type        ENUM('int','float') NULL,
                    CHECK ((tipo_registro = 'hr' AND data_type IS NOT NULL)
                        OR (tipo_registro <> 'hr' AND data_type IS NULL))
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # ==================================================
            # COMMAND
            # ==================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{self.table_in}` (
                    id                  INT AUTO_INCREMENT PRIMARY KEY,
                    timestamp           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    indirizzo_modbus    VARCHAR(32) NOT NULL,
                    tipo_registro       VARCHAR(2),
                    valore_da_impostare DOUBLE,
                    INDEX idx_timestamp (timestamp)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # ==================================================
            # HISTORY (event store)
            # ==================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS `{self.table_out}` (
                    id               INT AUTO_INCREMENT PRIMARY KEY,
                    timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    indirizzo_modbus VARCHAR(32) NOT NULL,
                    registro_robot   VARCHAR(32),
                    descrizione      VARCHAR(255),
                    tipo_registro    VARCHAR(2),
                    valore           DOUBLE,
                    accesso          ENUM('ro','rw') NOT NULL DEFAULT 'ro',
                    data_type        ENUM('int','float') NULL,
                    tipo_operazione  ENUM('READ','WRITE') NOT NULL,
                    CHECK ((tipo_registro = 'hr' AND data_type IS NOT NULL)
                        OR (tipo_registro <> 'hr' AND data_type IS NULL)),
                    INDEX idx_modbus_timestamp (indirizzo_modbus, timestamp),
                    INDEX idx_operazione (tipo_operazione),
                    INDEX idx_timestamp  (timestamp)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # ==================================================
            # CURRENT_STATE (stato macchina)
            # ==================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS current_state (
                    indirizzo_modbus VARCHAR(32) PRIMARY KEY,
                    registro_robot   VARCHAR(32),
                    descrizione      VARCHAR(255),
                    tipo_registro    VARCHAR(2),
                    valore           DOUBLE,
                    accesso          ENUM('ro','rw') NOT NULL DEFAULT 'ro',
                    data_type        ENUM('int','float') NULL,
                    timestamp        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                     ON UPDATE CURRENT_TIMESTAMP,
                    CHECK ((tipo_registro = 'hr' AND data_type IS NOT NULL)
                        OR (tipo_registro <> 'hr' AND data_type IS NULL)),
                    INDEX idx_timestamp (timestamp),
                    INDEX idx_tipo (tipo_registro),
                    INDEX idx_accesso (accesso)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            """)

            # Migrazione per installazioni esistenti: CREATE TABLE IF NOT EXISTS
            # non aggiunge colonne alle tabelle gia' create.
            self._ensure_column(cursor, "registers", "data_type", "ENUM('int','float') NULL")
            self._ensure_column(cursor, self.table_out, "data_type", "ENUM('int','float') NULL")
            self._ensure_column(cursor, "current_state", "data_type", "ENUM('int','float') NULL")

            conn.commit()
            print("  ✔ Tabelle create/verificate")

        #self._create_trigger()
        return True

    # ------------------------------------------------------
    @staticmethod
    def _ensure_column(cursor, table_name, column_name, definition):
        """Aggiunge una colonna solo se non e' gia' presente."""
        cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = %s
              AND COLUMN_NAME = %s
            """,
            (table_name, column_name),
        )
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {definition}"
            )

    # ------------------------------------------------------
    #def _create_trigger(self):

    #    trigger_name = f"trg_block_ro_{self.table_in}"

    #    trigger_sql = f"""
    #        CREATE TRIGGER `{trigger_name}`
    #        BEFORE INSERT ON `{self.table_in}`
    #        FOR EACH ROW
    #        BEGIN
    #            DECLARE v_accesso VARCHAR(2) DEFAULT 'rw';

    #            SELECT accesso INTO v_accesso
    #            FROM registers
    #            WHERE registro_modbus = NEW.indirizzo_modbus
    #            LIMIT 1;

    #            IF v_accesso = 'ro' THEN
    #                SIGNAL SQLSTATE '45000'
    #                SET MESSAGE_TEXT = 'Scrittura vietata: registro di sola lettura (ro)';
    #            END IF;
    #        END
    #    """

    #    try:
    #        conn = mysql.connector.connect(**self.db_config)
    #        cursor = conn.cursor()

    #        cursor.execute(f"DROP TRIGGER IF EXISTS `{trigger_name}`")
    #        cursor.execute(trigger_sql)

    #        conn.commit()
    #        cursor.close()
    #        conn.close()

    #        print("  ✔ Trigger anti-scrittura ro attivo")

    #    except mysql.connector.Error as e:
    #        log.warning(f"Trigger non creato: {e}")
    #        print("  ⚠️  Trigger non creato — protezione attiva solo nel Writer")

    # ------------------------------------------------------
    def bootstrap_current_state(self):

        print("\n🔄 Bootstrap completo current_state...")

        with db_connection(self.db_config) as conn, db_cursor(conn) as cursor:

            # 1️⃣ Inserisce TUTTI i registri se non esistono
            cursor.execute("""
                INSERT INTO current_state
                    (indirizzo_modbus, registro_robot, descrizione,
                    tipo_registro, valore, accesso, data_type)
                SELECT
                    registro_modbus,
                    registro_robot,
                    descrizione,
                    tipo_registro,
                    0 AS valore,
                    accesso,
                    data_type
                FROM registers
                ON DUPLICATE KEY UPDATE
                    registro_robot = VALUES(registro_robot),
                    descrizione    = VALUES(descrizione),
                    tipo_registro  = VALUES(tipo_registro),
                    accesso        = VALUES(accesso),
                    data_type      = VALUES(data_type);
            """)

            # 2️⃣ Sovrascrive con ultimo valore da history se esiste
            cursor.execute(f"""
                UPDATE current_state cs
                JOIN (
                    SELECT t.indirizzo_modbus,
                        t.valore,
                        t.timestamp
                    FROM {self.table_out} t
                    JOIN (
                        SELECT indirizzo_modbus, MAX(timestamp) AS ts
                        FROM {self.table_out}
                        GROUP BY indirizzo_modbus
                    ) last
                    ON t.indirizzo_modbus = last.indirizzo_modbus
                    AND t.timestamp = last.ts
                ) h
                ON cs.indirizzo_modbus = h.indirizzo_modbus
                SET cs.valore    = h.valore,
                    cs.timestamp = h.timestamp;
            """)

            conn.commit()

        print("  ✔ current_state inizializzato con TUTTI i registri")

    # ------------------------------------------------------
    def load_registers_from_json(self, json_path="registers.json"):

        print(f"\n📥 Caricamento registers da {json_path}...")

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                registers = json.load(f)
        except Exception as e:
            print(f"❌ Errore lettura {json_path}: {e}")
            return False

        with db_connection(self.db_config) as conn, db_cursor(conn) as cursor:
            for r in registers:
                accesso = r.get("accesso", "ro").lower()
                reg_type = r["tipo_registro"]
                data_type = r.get("data_type")
                if reg_type == "hr" and data_type not in {"int", "float"}:
                    raise ValueError(
                        f"Registro {r['registro']}: data_type obbligatorio per hr (int o float)"
                    )
                if reg_type != "hr" and data_type is not None:
                    raise ValueError(
                        f"Registro {r['registro']}: data_type consentito solo per hr"
                    )

                cursor.execute("""
                    INSERT INTO registers
                        (registro_modbus, tipo_registro, registro_robot,
                         descrizione, accesso, data_type)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                        tipo_registro  = VALUES(tipo_registro),
                        registro_robot = VALUES(registro_robot),
                        descrizione    = VALUES(descrizione),
                        accesso        = VALUES(accesso),
                        data_type      = VALUES(data_type)
                """, (
                    str(r["registro"]),
                    reg_type,
                    r.get("registro_robot", ""),
                    r.get("descrizione", ""),
                    accesso,
                    data_type,
                ))

            conn.commit()

        print(f"  ✔ {len(registers)} registri caricati")
        return True

    # ------------------------------------------------------
    def run_installation(self):

        print("\n🚀 Avvio installazione...")

        if not self.create_tables():
            sys.exit("❌ Errore creazione tabelle")

        self.load_registers_from_json()
        self.bootstrap_current_state()

        print("\n🎉 Installazione completata con successo!")


# ==========================================================

if __name__ == "__main__":
    try:
        DatabaseInstaller().run_installation()
    except Exception as e:
        log.error(str(e))
        print(f"❌ ERRORE CRITICO: {e}")
        sys.exit(1)
