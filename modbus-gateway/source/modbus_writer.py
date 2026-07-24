#!/usr/bin/env python3
"""
MODBUS WRITER  —  versione industriale
========================================

Responsabilità:
  1. Legge i comandi dalla tabella ``command`` (FIFO per timestamp).
  2. Verifica che il registro esista e sia rw.
  3. Scrive il valore sul bus Modbus.
  4. Archivia la WRITE su ``history``.
  5. Aggiorna ``current_state`` via UPSERT (ottimistico: il Reader
     farà la verifica post-WRITE nel ciclo successivo con la READ reale).
  6. Rimuove il record da ``command``.

Aggiornamento current_state:
  Il Writer aggiorna current_state SUBITO dopo la scrittura Modbus riuscita.
  Questo garantisce che current_state sia coerente anche nei cicli in cui
  il Reader non ha ancora completato la lettura di verifica.
  Il Reader, nel ciclo successivo, sovrascriverà il valore con la READ
  effettiva dal bus (fonte di verità definitiva).
"""

import time
import threading
import mysql.connector
import configparser
from typing import Tuple

from register_config import RegisterConfigManager
from modbus_client import OptimizedModbusClient, ModbusDeviceError
from logging_utils import setup_logger
from register_validation import validate_value

log = setup_logger("writer", "log.log")


class ModbusWriter(threading.Thread):

    def __init__(self, config_path: str = "config.ini",
                 registers_path: str = "registers.json"):
        super().__init__(daemon=True, name="ModbusWriter")

        self.config_path    = config_path
        self.registers_path = registers_path

        self._load_config()
        self._init_register_manager()

        self.running   = True
        self.connected = False

        rw = len(self.reg_mgr.writable_addresses())
        ro = len(self.reg_mgr.readable_addresses()) - rw
        log.info(f"✏️  Writer pronto: {rw} registri rw, {ro} ro")

    # ──────────────────────────────────────────────────────────────────────────
    # CONFIG
    # ──────────────────────────────────────────────────────────────────────────

    def _load_config(self):
        cfg = configparser.ConfigParser()
        cfg.read(self.config_path)

        self.modbus_ip      = cfg["modbus_server"]["ip"]
        self.modbus_port    = int(cfg["modbus_server"]["port"])
        self.modbus_timeout = float(cfg["modbus_server"]["timeout"])
        self.write_interval = float(cfg["modbus_server"]["write_interval"])

        self.db_config = {
            "host":     cfg["database"]["host"],
            "user":     cfg["database"]["user"],
            "password": cfg["database"]["password"],
            "database": cfg["database"]["database"],
        }
        self.table_in  = cfg["database"]["base_table_in"]
        self.table_out = cfg["database"]["base_table_out"]

    def _init_register_manager(self):
        self.reg_mgr = RegisterConfigManager(self.registers_path)

    # ──────────────────────────────────────────────────────────────────────────
    # CONNESSIONI
    # ──────────────────────────────────────────────────────────────────────────

    def _wait_for_modbus(self) -> bool:
        log.info(f"⏳ Attendo Modbus ({self.modbus_ip}:{self.modbus_port})...")
        while self.running:
            try:
                if self.mb_client.connect():
                    self.connected = True
                    log.info("✅ Modbus connesso")
                    return True
            except Exception as e:
                log.warning(f"Modbus non disponibile: {e} | retry tra 5s")
            time.sleep(5)
        return False

    def _wait_for_db(self) -> bool:
        log.info(f"⏳ Attendo DB ({self.db_config['host']})...")
        while self.running:
            try:
                self.db = mysql.connector.connect(**self.db_config)
                self.db.autocommit = True
                log.info("✅ DB connesso")
                return True
            except Exception as e:
                log.warning(f"DB non disponibile: {e} | retry tra 5s")
                time.sleep(5)
        return False

    # ──────────────────────────────────────────────────────────────────────────
    # SCRITTURA MODBUS
    # ──────────────────────────────────────────────────────────────────────────

    # Valore massimo scrivibile per un holding register a 32-bit (unsigned)
    HR_MAX = 0xFFFF_FFFF   # 4_294_967_295

    def _validate_value(self, address: int, value) -> Tuple[bool, str]:
        """
        Controlla che il valore sia compatibile con il tipo di registro.
        Ritorna (valido, motivo_errore).
        """
        rtype = self.reg_mgr.get_type(address)
        try:
            if rtype == "co":
                iv = int(value)
                if iv not in (0, 1):
                    return False, f"coil accetta solo 0/1, ricevuto: {iv}"
            elif rtype == "hr":
                fv = float(value)
                if abs(fv) > 3.40282347e38:
                    return False, f"holding register float 32-bit fuori range: {fv}"
                # controlla massimo 5 decimali
                val_str = str(value)
                if '.' in val_str:
                    dec_part = val_str.split('.')[1]
                    if len(dec_part) > 5:
                        return False, f"holding register float 32-bit accetta max 5 decimali, ricevuto: {fv}"
            else:
                return False, f"tipo registro non scrivibile: {rtype}"
        except (ValueError, TypeError) as e:
            return False, f"valore non convertibile: {value!r} — {e}"
        return True, ""

    def _write_modbus(self, address: int, value) -> bool:
        """
        Scrive sul bus Modbus.

        Solleva ModbusDeviceError se il server risponde con ExceptionResponse
        (errore permanente: il record va scartato, non riprovato).
        Ritorna False se c'è un errore di connessione transiente (retry).
        """
        rtype = self.reg_mgr.get_type(address)
        try:
            if rtype == "co":
                return self.mb_client.write_coil(address, bool(int(float(value))))
            if rtype == "hr":
                return self.mb_client.write_holding_register_32bit(address, float(value))
            log.error(f"⛔ Tipo registro non scrivibile: {rtype} @{address}")
            return False
        except ModbusDeviceError:
            raise   # il ciclo cycle() lo cattura e scarta il record
        except Exception as e:
            log.error(f"❌ Errore connessione scrittura @{address}: {e}")
            self.connected = False
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # CICLO PRINCIPALE
    # ──────────────────────────────────────────────────────────────────────────

    def cycle(self) -> bool:
        """
        Elabora il record più vecchio in command.
        Ritorna True se ha trovato un record (anche se poi scartato/fallito),
        False se la tabella è vuota (il chiamante applica il write_interval).
        """
        cursor = self.db.cursor(dictionary=True)
        try:
            cursor.execute(
                f"SELECT * FROM `{self.table_in}` ORDER BY timestamp ASC LIMIT 1"
            )
            record = cursor.fetchone()

            if not record:
                return False   # nulla da fare

            record_id = record["id"]
            addr      = int(record["indirizzo_modbus"])
            value     = record.get("valore_da_impostare")

            # ── Registro configurato? ─────────────────────────────────────────
            if not self.reg_mgr.exists(addr):
                log.error(f"⛔ id={record_id}  @{addr} non configurato → scartato")
                cursor.execute(
                    f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
                )
                return True

            # ── Registro scrivibile? ──────────────────────────────────────────
            if self.reg_mgr.is_readonly(addr):
                reg_name = self.reg_mgr.get(addr)["registro_robot"]
                log.error(
                    f"⛔ id={record_id}  @{addr} ({reg_name}) è ro → scartato"
                )
                cursor.execute(
                    f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
                )
                return True

            # ── Valore presente? ──────────────────────────────────────────────
            if value is None:
                log.error(f"⛔ id={record_id}  @{addr} valore NULL → scartato")
                cursor.execute(
                    f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
                )
                return True

            meta     = self.reg_mgr.get(addr)
            reg_name = meta["registro_robot"]

            # ── Validazione valore (range / tipo) ─────────────────────────────
            valid, reason, normalized_value = validate_value(
                meta["tipo_registro"], meta["data_type"], value
            )
            if not valid:
                log.error(
                    f"⛔ id={record_id}  @{addr} ({reg_name}) "
                    f"valore non valido → scartato: {reason}"
                )
                cursor.execute(
                    f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
                )
                return True

            log.info(f"✏️  Scrittura @{addr} ({reg_name}) = {value}")

            # ── Scrittura su Modbus ───────────────────────────────────────────
            try:
                write_ok = self._write_modbus(addr, normalized_value)
            except ModbusDeviceError as exc:
                log.error(
                    f"⛔ id={record_id}  @{addr} ({reg_name}) "
                    f"ExceptionResponse dal server (exc={exc.exception_code}) "
                    f"→ record scartato: {exc}"
                )
                cursor.execute(
                    f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
                )
                return True

            if not write_ok:
                log.error(f"❌ Scrittura fallita @{addr} — verrà riprovata")
                return True

            log.info(f"✅ Scrittura completata @{addr} ({reg_name}) = {value}")
            # ── Valore numerico normalizzato ──────────────────────────────────
            rtype = meta["tipo_registro"]
            if rtype == "co":
                norm_value = float(bool(int(normalized_value)))
            else:
                norm_value = normalized_value

            params = (
                str(addr),
                meta["registro_robot"],
                meta.get("descrizione", ""),
                meta["tipo_registro"],
                norm_value,
                meta["accesso"],
                meta["data_type"],
            )

            # ── Archivia WRITE su history ─────────────────────────────────────
            cursor.execute(
                f"""INSERT INTO `{self.table_out}`
                        (indirizzo_modbus, registro_robot, descrizione,
                         tipo_registro, valore, accesso, data_type, tipo_operazione)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'WRITE')""",
                params,
            )

            # ── UPSERT su current_state ───────────────────────────────────────
            cursor.execute(
                """INSERT INTO current_state
                       (indirizzo_modbus, registro_robot, descrizione,
                        tipo_registro, valore, accesso, data_type)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                       registro_robot = VALUES(registro_robot),
                       descrizione    = VALUES(descrizione),
                       tipo_registro  = VALUES(tipo_registro),
                       valore         = VALUES(valore),
                       accesso        = VALUES(accesso),
                       data_type      = VALUES(data_type),
                       timestamp      = CURRENT_TIMESTAMP""",
                params,
            )

            # ── Rimuovi da command ────────────────────────────────────────────
            cursor.execute(
                f"DELETE FROM `{self.table_in}` WHERE id=%s", (record_id,)
            )
            log.info(f"🗑️  Record id={record_id} rimosso da command")
            return True

        except Exception as e:
            log.error(f"❌ Errore ciclo Writer: {type(e).__name__}: {e}")
            return False

        finally:
            cursor.close()

    # ──────────────────────────────────────────────────────────────────────────
    # THREAD RUN
    # ──────────────────────────────────────────────────────────────────────────

    def run(self):
        log.info("▶️  ModbusWriter avviato")

        self.mb_client = OptimizedModbusClient(
            ip=self.modbus_ip,
            port=self.modbus_port,
            timeout=self.modbus_timeout,
        )

        if not self._wait_for_modbus():
            return
        if not self._wait_for_db():
            return

        log.info(f"🚀 Writer ONLINE — monitor tabella: {self.table_in}")

        while self.running:
            try:
                if not self.connected:
                    self.mb_client.close()
                    if not self._wait_for_modbus():
                        break

                if not self.cycle():
                    time.sleep(self.write_interval)

            except Exception as e:
                log.error(f"❌ Errore ciclo principale Writer: {e}")
                self.connected = False
                time.sleep(5)

    # ──────────────────────────────────────────────────────────────────────────
    # STOP
    # ──────────────────────────────────────────────────────────────────────────

    def stop(self):
        self.running = False
        try:
            self.mb_client.close()
        except Exception:
            pass
        try:
            self.db.close()
        except Exception:
            pass
        log.info("🛑 ModbusWriter fermato")
