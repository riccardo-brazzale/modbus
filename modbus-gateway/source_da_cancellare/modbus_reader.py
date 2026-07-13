"""
MODBUS READER  —  versione industriale 2.0
===========================================

Responsabilità:
  1. Legge ciclicamente TUTTI i registri configurati (ro e rw) tramite
     lettura batch per tipo e blocco contiguo.
  2. Gestisce ExceptionResponse(exc=2) con binary splitting adattivo:
     dimezza il gruppo ricorsivamente fino al singolo registro.
     I gruppi ottimali vengono memorizzati per i cicli successivi.
  3. Salva in ``history`` SOLO i valori cambiati rispetto alla cache locale.
  4. Aggiorna ``current_state`` via UPSERT per ogni registro variato.
  5. Invalida la cache post-WRITE (throttled) leggendo le WRITE recenti
     dal Writer in history — SOLO per registri RW, mai per RO.
  6. Riconnessione automatica a Modbus e DB con backoff.
  7. Rilevamento e riconnessione automatica in caso di connessione DB persa
     (ping periodico + reconnect on error).

GARANZIE RO:
  - I registri RO vengono letti normalmente e le variazioni vengono salvate.
  - La cache RO non viene mai invalidata da eventi WRITE (impossibili per RO).
  - Non esiste nessun percorso di codice che permetta scrittura su RO.

OTTIMIZZAZIONI:
  - Lettura batch con binary splitting adattivo (gruppi ottimali persistenti).
  - Throttle log errori per evitare spam (un errore ogni N secondi).
  - Lock cache acquisito UNA VOLTA per ciclo, non per registro.
  - INSERT/UPSERT batch: una sola query multi-row per ciclo.
  - Ping DB periodico per rilevare connessioni silenziosamente cadute.
  - _sync_cache_from_writer() con throttle e filtro RW-only.
"""

import time
import threading
import mysql.connector
from mysql.connector import errors as mysql_errors
import configparser
from typing import Dict, List, Optional

from register_config import RegisterConfigManager
from modbus_client import OptimizedModbusClient, ModbusDeviceError
from logging_utils import setup_logger

log = setup_logger("reader", "log.log")

# ─── Costanti ────────────────────────────────────────────────────────────────

# Limite massimo di word per singola richiesta Modbus (pymodbus: 1 < count < 125)
MAX_WORDS_PER_REQUEST = 64

# Errori batch identici loggati al massimo ogni N secondi
BATCH_ERROR_LOG_INTERVAL = 30.0

# Secondi tra riconnessioni a Modbus/DB
RECONNECT_INTERVAL = 5

# Ping DB ogni N secondi per rilevare connessioni silenziosamente cadute
DB_PING_INTERVAL = 30.0


class ModbusReader(threading.Thread):
    """
    Thread daemon che legge ciclicamente i registri Modbus e
    persiste le variazioni su MySQL (history + current_state).
    """

    # ──────────────────────────────────────────────────────────────────────────
    # INIT
    # ──────────────────────────────────────────────────────────────────────────

    def __init__(
        self,
        config_path: str = "config.ini",
        registers_path: str = "registers.json",
    ):
        super().__init__(daemon=True, name="ModbusReader")

        self._load_config(config_path)
        self._init_modbus_client()
        self._init_register_manager(registers_path)
        self._init_batch_groups()
        self._init_cache()
        self._init_state()

        log.info(
            f"📖 Reader pronto: {len(self.registers)} registri "
            f"({self._ro_count} ro + {self._rw_count} rw) | "
            f"poll ogni {self.interval}s"
        )

    # ──────────────────────────────────────────────────────────────────────────
    # INIT HELPERS
    # ──────────────────────────────────────────────────────────────────────────

    def _load_config(self, config_path: str) -> None:
        cfg = configparser.ConfigParser()
        cfg.read(config_path)

        m = cfg["modbus_server"]
        self.mb_ip      = m["ip"]
        self.mb_port    = int(m["port"])
        self.mb_timeout = float(m["timeout"])
        self.interval   = float(m["poll_interval"])

        db = cfg["database"]
        self.db_host     = db["host"]
        self.db_user     = db["user"]
        self.db_password = db["password"]
        self.db_database = db["database"]
        self.table_out   = db["base_table_out"]

        # Throttle sync cache post-WRITE (secondi)
        self._sync_interval = 5.0

    def _init_modbus_client(self) -> None:
        self.mb = OptimizedModbusClient(
            ip=self.mb_ip,
            port=self.mb_port,
            timeout=self.mb_timeout,
        )
        self.db:  Optional[mysql.connector.MySQLConnection] = None
        self.cur: Optional[mysql.connector.cursor.MySQLCursor] = None

    def _init_register_manager(self, registers_path: str) -> None:
        self.reg_mgr   = RegisterConfigManager(registers_path)
        self.registers = self.reg_mgr.all_records()   # {addr: meta_dict}
        self._rw_count = len(self.reg_mgr.writable_addresses())
        self._ro_count = len(self.registers) - self._rw_count

    def _init_batch_groups(self) -> None:
        """Calcola i gruppi di lettura batch iniziali per coil e HR."""
        self._coil_groups = self._build_groups(
            sorted(self.reg_mgr.addresses_by_type("co")), step=1
        )
        self._hr_groups = self._build_groups(
            sorted(self.reg_mgr.addresses_by_type("hr")), step=2
        )
        log.info(
            f"📦 Batch groups: {len(self._coil_groups)} coil, "
            f"{len(self._hr_groups)} HR"
        )

    def _init_cache(self) -> None:
        """Inizializza la cache locale e le strutture di supporto."""
        self.cache:      Dict[int, object]  = {}   # addr → valore (None = invalido)
        self.cache_ts:   Dict[int, float]   = {}   # addr → ts ultimo salvataggio DB
        self.cache_lock: threading.Lock     = threading.Lock()

        self._last_sync_ts:    float = 0.0
        self._last_db_ping_ts: float = 0.0

        # Throttle log errori batch: key → timestamp ultimo log
        self._batch_error_ts: Dict[str, float] = {}

    def _init_state(self) -> None:
        """Inizializza i flag di stato del thread."""
        self.running:   bool = True
        self.connected: bool = False   # connessione Modbus attiva

    # ──────────────────────────────────────────────────────────────────────────
    # BATCH GROUPS
    # ──────────────────────────────────────────────────────────────────────────

    def _build_groups(self, addrs: List[int], step: int) -> List[dict]:
        """
        Raggruppa indirizzi logici in blocchi contigui per minimizzare
        i roundtrip Modbus.

        step=1  → coil   (1 indirizzo logico = 1 word/bit Modbus)
        step=2  → HR32   (1 indirizzo logico = 2 word Modbus consecutive)

        Un gruppo viene spezzato se:
          - il gap tra indirizzi consecutivi è > step (non contigui)
          - aggiungere il prossimo supererebbe MAX_WORDS_PER_REQUEST
        """
        if not addrs:
            return []

        groups:  List[dict] = []
        g_start: int        = addrs[0]
        g_addrs: List[int]  = [addrs[0]]

        for addr in addrs[1:]:
            gap               = addr - g_addrs[-1]
            prospective_words = (addr - g_start + step)
            too_far           = gap > step
            too_big           = prospective_words > MAX_WORDS_PER_REQUEST

            if too_far or too_big:
                groups.append(self._make_group(g_start, g_addrs, step))
                g_start = addr
                g_addrs = [addr]
            else:
                g_addrs.append(addr)

        groups.append(self._make_group(g_start, g_addrs, step))
        return groups

    @staticmethod
    def _make_group(start: int, addrs: List[int], step: int) -> dict:
        last       = addrs[-1]
        word_count = (last - start) + step
        return {"start": start, "word_count": word_count, "addrs": list(addrs)}

    # ──────────────────────────────────────────────────────────────────────────
    # THROTTLED LOGGING
    # ──────────────────────────────────────────────────────────────────────────

    def _log_batch_error(self, key: str, message: str) -> None:
        """
        Logga un errore batch al massimo ogni BATCH_ERROR_LOG_INTERVAL secondi.
        Evita lo spam di log identici ad ogni ciclo rapido (es. 0.1s).
        """
        now = time.time()
        if now - self._batch_error_ts.get(key, 0.0) >= BATCH_ERROR_LOG_INTERVAL:
            self._batch_error_ts[key] = now
            log.error(message)

    # ──────────────────────────────────────────────────────────────────────────
    # BINARY SPLITTING — COIL
    # ──────────────────────────────────────────────────────────────────────────

    def _read_coils_with_split(self, start: int, addrs: List[int]) -> Dict[int, bool]:
        """
        Legge un gruppo di coil con binary splitting adattivo.

        Se il server risponde ExceptionResponse(exc=2) — Illegal Data Address —
        dimezza il gruppo e riprova ricorsivamente fino al singolo coil.

        Restituisce {addr: bool} per gli indirizzi letti con successo.
        Ritorna {} per indirizzi irrisolvibili (loggati con throttle).
        """
        if not addrs:
            return {}

        count = len(addrs)   # coil: 1 indirizzo = 1 count

        try:
            response = self.mb.client.read_coils(start, count=count)

            if not response.isError():
                return {
                    addr: bool(response.bits[addr - start])
                    for addr in addrs
                }

            # ExceptionResponse
            exc_code = getattr(response, "exception_code", -1)
            if exc_code == 2 and count > 1:
                return self._split_and_read_coils(addrs)

            self._log_batch_error(
                f"co_exc_{start}_{count}",
                f"❌ Coil @{start}+{count}: exc={exc_code} | saltati: {addrs}",
            )
            return {}

        except ModbusDeviceError as e:
            if e.exception_code == 2 and count > 1:
                return self._split_and_read_coils(addrs)
            self._log_batch_error(
                f"co_dev_{start}_{count}",
                f"❌ Coil @{start} DeviceError(exc={e.exception_code}): {e} | saltato",
            )
            return {}

        except Exception as e:
            log.error(f"❌ Connessione batch coil @{start}: {type(e).__name__}: {e}")
            self.connected = False
            return {}

    def _split_and_read_coils(self, addrs: List[int]) -> Dict[int, bool]:
        """Dimezza la lista di indirizzi e legge ciascuna metà separatamente."""
        mid    = len(addrs) // 2
        result = {}
        result.update(self._read_coils_with_split(addrs[0],   addrs[:mid]))
        result.update(self._read_coils_with_split(addrs[mid], addrs[mid:]))
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # BINARY SPLITTING — HOLDING REGISTER 32-BIT
    # ──────────────────────────────────────────────────────────────────────────

    def _read_hr_with_split(self, start: int, addrs: List[int]) -> Dict[int, float]:
        """
        Legge un gruppo di HR 32-bit con binary splitting adattivo come float.

        Ogni indirizzo logico occupa 2 word Modbus consecutive (big-endian):
          valore = float (IEEE 754 a precisione singola)

        Se il server risponde ExceptionResponse(exc=2) dimezza il gruppo
        e riprova ricorsivamente fino al singolo HR (2 word).

        Restituisce {addr: float} per gli indirizzi letti con successo.
        """
        if not addrs:
            return {}

        # Numero di word da leggere: dalla prima alla fine dell'ultimo HR (2 word)
        word_count = (addrs[-1] - start) + 2

        try:
            response = self.mb.client.read_holding_registers(start, count=word_count)

            if not response.isError():
                result: Dict[int, float] = {}
                import struct
                for addr in addrs:
                    offset       = addr - start
                    high         = response.registers[offset]
                    low          = response.registers[offset + 1]
                    bytes_value  = struct.pack(">HH", high, low)
                    result[addr] = struct.unpack(">f", bytes_value)[0]
                return result

            exc_code = getattr(response, "exception_code", -1)
            if exc_code == 2 and len(addrs) > 1:
                return self._split_and_read_hr(addrs)

            self._log_batch_error(
                f"hr_exc_{start}_{word_count}",
                f"❌ HR @{start}+{word_count}w: exc={exc_code} | saltati: {addrs}",
            )
            return {}

        except ModbusDeviceError as e:
            if e.exception_code == 2 and len(addrs) > 1:
                return self._split_and_read_hr(addrs)
            self._log_batch_error(
                f"hr_dev_{start}_{word_count}",
                f"❌ HR @{start} DeviceError(exc={e.exception_code}): {e} | saltato",
            )
            return {}

        except Exception as e:
            log.error(f"❌ Connessione batch HR @{start}: {type(e).__name__}: {e}")
            self.connected = False
            return {}

    def _split_and_read_hr(self, addrs: List[int]) -> Dict[int, int]:
        """Dimezza la lista di indirizzi e legge ciascuna metà separatamente."""
        mid    = len(addrs) // 2
        result = {}
        result.update(self._read_hr_with_split(addrs[0],   addrs[:mid]))
        result.update(self._read_hr_with_split(addrs[mid], addrs[mid:]))
        return result

    # ──────────────────────────────────────────────────────────────────────────
    # LETTURA BATCH COMPLETA
    # ──────────────────────────────────────────────────────────────────────────

    def _read_all(self) -> Dict[int, object]:
        """
        Legge tutti i registri configurati (coil + HR) in batch.
        Restituisce {addr: valore} per gli indirizzi letti con successo.
        Si interrompe non appena la connessione Modbus cade.
        """
        readings: Dict[int, object] = {}

        for group in self._coil_groups:
            if not self.connected:
                return readings
            readings.update(
                self._read_coils_with_split(group["start"], group["addrs"])
            )

        for group in self._hr_groups:
            if not self.connected:
                return readings
            readings.update(
                self._read_hr_with_split(group["start"], group["addrs"])
            )

        return readings

    # ──────────────────────────────────────────────────────────────────────────
    # CONNESSIONI
    # ──────────────────────────────────────────────────────────────────────────

    def _wait_for_modbus(self) -> bool:
        """
        Tenta la connessione Modbus in loop fino a successo o stop.
        Ritorna True se connesso, False se il thread è stato fermato.
        """
        log.info(f"⏳ Attendo Modbus ({self.mb_ip}:{self.mb_port})...")
        while self.running:
            try:
                if self.mb.connect():
                    self.connected = True
                    log.info("✅ Modbus connesso")
                    return True
            except Exception as e:
                log.warning(
                    f"Modbus non disponibile — {type(e).__name__}: {e} | "
                    f"retry tra {RECONNECT_INTERVAL}s"
                )
            time.sleep(RECONNECT_INTERVAL)
        return False

    def _connect_db(self) -> bool:
        """
        Tenta la connessione MySQL in loop fino a successo o stop.
        Ritorna True se connesso, False se il thread è stato fermato.
        """
        log.info(f"⏳ Attendo DB ({self.db_host}/{self.db_database})...")
        while self.running:
            try:
                conn = mysql.connector.connect(
                    host=self.db_host,
                    user=self.db_user,
                    password=self.db_password,
                    database=self.db_database,
                    connection_timeout=10,
                    autocommit=True,
                )
                self.db  = conn
                self.cur = conn.cursor(dictionary=True)
                log.info("✅ DB connesso")
                return True
            except Exception as e:
                log.warning(
                    f"DB non disponibile — {type(e).__name__}: {e} | "
                    f"retry tra {RECONNECT_INTERVAL}s"
                )
                time.sleep(RECONNECT_INTERVAL)
        return False

    def _ping_db(self) -> None:
        """
        Verifica periodicamente che la connessione DB sia ancora attiva.
        Se caduta silenziosamente, tenta riconnessione.
        Chiamato una volta ogni DB_PING_INTERVAL secondi.
        """
        now = time.time()
        if now - self._last_db_ping_ts < DB_PING_INTERVAL:
            return
        self._last_db_ping_ts = now

        try:
            self.db.ping(reconnect=False)
        except Exception:
            log.warning("⚠️  Connessione DB persa — riconnessione...")
            self._close_db()
            self._connect_db()

    def _close_db(self) -> None:
        """Chiude cursore e connessione DB ignorando eventuali errori."""
        for obj in (self.cur, self.db):
            try:
                if obj is not None:
                    obj.close()
            except Exception:
                pass
        self.cur = None
        self.db  = None

    # ──────────────────────────────────────────────────────────────────────────
    # CACHE — INIZIALIZZAZIONE
    # ──────────────────────────────────────────────────────────────────────────

    def _load_initial_cache(self) -> None:
        """
        Carica da ``history`` l'ultimo valore noto per ogni registro.
        Se il DB è vuoto inizializza tutto a None (forza la prima scrittura).
        In caso di errore fa lo stesso, loggando il problema.
        """
        try:
            self.cur.execute(f"SELECT 1 FROM `{self.table_out}` LIMIT 1")
            has_data = self.cur.fetchone() is not None

            if not has_data:
                log.info("🔭 DB vuoto → cache a None (forza prima lettura)")
                self._reset_cache()
                return

            # Legge l'ultimo valore registrato per ogni indirizzo
            q = f"""
                SELECT t.indirizzo_modbus,
                       t.valore,
                       UNIX_TIMESTAMP(t.timestamp) AS ts
                FROM `{self.table_out}` t
                JOIN (
                    SELECT indirizzo_modbus, MAX(timestamp) AS ts
                    FROM `{self.table_out}`
                    GROUP BY indirizzo_modbus
                ) last ON  t.indirizzo_modbus = last.indirizzo_modbus
                       AND t.timestamp        = last.ts
            """
            self.cur.execute(q)
            rows = self.cur.fetchall()

            with self.cache_lock:
                for row in rows:
                    addr = int(row["indirizzo_modbus"])
                    if addr in self.registers:
                        self.cache[addr]    = row["valore"]
                        self.cache_ts[addr] = float(row["ts"])

                # Indirizzi non presenti in history → None
                for addr in self.registers:
                    if addr not in self.cache:
                        self.cache[addr]    = None
                        self.cache_ts[addr] = 0.0

            log.info(
                f"📥 Cache caricata: {len(self.cache)} indirizzi "
                f"({sum(1 for v in self.cache.values() if v is not None)} con valore noto)"
            )

        except Exception as e:
            log.error(f"❌ Errore caricamento cache: {type(e).__name__}: {e} | reset a None")
            self._reset_cache()

    def _reset_cache(self) -> None:
        """Azzera la cache: tutti gli indirizzi → None, ts → 0."""
        with self.cache_lock:
            for addr in self.registers:
                self.cache[addr]    = None
                self.cache_ts[addr] = 0.0

    # ──────────────────────────────────────────────────────────────────────────
    # CACHE — SINCRONIZZAZIONE POST-WRITE
    # ──────────────────────────────────────────────────────────────────────────

    def _sync_cache_from_writer(self) -> None:
        """
        Invalida la cache per i registri RW scritti dal Writer negli ultimi 30s,
        così il reader ri-leggerà il valore reale dal bus nel ciclo successivo.

        NOTA: i registri RO vengono esplicitamente esclusi — non possono
        essere scritti da comandi, quindi la loro cache non va mai invalidata
        da eventi WRITE (che non arriveranno mai per RO).

        Throttle: eseguita al massimo ogni _sync_interval secondi.
        """
        now = time.time()
        if now - self._last_sync_ts < self._sync_interval:
            return
        self._last_sync_ts = now

        try:
            q = f"""
                SELECT indirizzo_modbus,
                       UNIX_TIMESTAMP(MAX(timestamp)) AS ts
                FROM `{self.table_out}`
                WHERE tipo_operazione = 'WRITE'
                  AND timestamp > DATE_SUB(NOW(), INTERVAL 30 SECOND)
                GROUP BY indirizzo_modbus
            """
            self.cur.execute(q)
            rows = self.cur.fetchall()

            if not rows:
                return

            invalidated: List[int] = []
            with self.cache_lock:
                for row in rows:
                    addr = int(row["indirizzo_modbus"])
                    ts   = float(row["ts"])

                    # Salta i registri RO: non possono arrivare da WRITE
                    if self.reg_mgr.is_readonly(addr):
                        continue

                    if addr in self.cache and ts > self.cache_ts.get(addr, 0.0):
                        self.cache[addr] = None
                        invalidated.append(addr)

            if invalidated:
                log.debug(
                    f"🔄 Cache invalidata post-WRITE (RW only): "
                    f"{len(invalidated)} indirizzi → {invalidated}"
                )

        except Exception as e:
            log.error(f"❌ Errore sync cache da Writer: {type(e).__name__}: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # NORMALIZZAZIONE VALORE
    # ──────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_value(val: object, tipo_registro: str) -> float:
        """
        Converte il valore letto dal bus nel tipo numerico da salvare in DB.

        coil → int(0 o 1)  poi float  (evita ambiguità True/False in MySQL)
        hr   → float        (preserva il valore float/int 32-bit con i decimali)
        """
        if tipo_registro == "co":
            return float(int(bool(val)))
        return float(val)

    # ──────────────────────────────────────────────────────────────────────────
    # CICLO PRINCIPALE
    # ──────────────────────────────────────────────────────────────────────────

    def cycle(self) -> None:
        """
        Un ciclo completo di lettura:
          1. Ping DB (throttled) per rilevare connessioni cadute.
          2. Sync cache post-WRITE dal Writer (throttled, solo RW).
          3. Lettura batch di tutti i registri dal bus Modbus.
          4. Confronto con cache → raccoglie le variazioni.
          5. INSERT batch in history + UPSERT batch in current_state.
        """
        # ── 1. Ping DB ────────────────────────────────────────────────────────
        self._ping_db()

        # ── 2. Sync cache post-WRITE (RW only) ───────────────────────────────
        self._sync_cache_from_writer()

        # ── 3. Lettura batch dal bus ──────────────────────────────────────────
        readings = self._read_all()

        if not self.connected:
            return

        if not readings:
            log.warning(f"⚠️  Nessun valore letto (attesi {len(self.registers)})")
            return

        n_read    = len(readings)
        n_total   = len(self.registers)
        n_missing = n_total - n_read
        if n_missing > 0:
            missing = sorted(set(self.registers.keys()) - set(readings.keys()))
            self._log_batch_error(
                f"partial_{n_missing}",
                f"⚠️  Lettura parziale: {n_read}/{n_total} — "
                f"mancanti: {missing}",
            )

        # ── 4. Confronto con cache ────────────────────────────────────────────
        to_history:      List[tuple] = []
        to_current_state: List[tuple] = []
        now = time.time()

        with self.cache_lock:
            for addr, raw_val in readings.items():
                meta      = self.registers[addr]
                norm_val  = self._normalize_value(raw_val, meta["tipo_registro"])

                # Confronto con valore in cache (None forzato sempre a variazione)
                cached = self.cache.get(addr)
                if cached is not None and float(cached) == norm_val:
                    continue

                # Variazione rilevata → aggiorna cache
                self.cache[addr]    = norm_val
                self.cache_ts[addr] = now

                row_base = (
                    str(addr),
                    meta["registro_robot"],
                    meta["descrizione"],
                    meta["tipo_registro"],
                    norm_val,
                    meta["accesso"],
                )
                to_history.append(row_base + ("READ",))
                to_current_state.append(row_base)

        if not to_history:
            return   # nessuna variazione in questo ciclo

        # ── 5. Persistenza batch ──────────────────────────────────────────────
        self._persist(to_history, to_current_state)

    def _persist(
        self,
        to_history:       List[tuple],
        to_current_state: List[tuple],
    ) -> None:
        """
        Salva le variazioni rilevate su MySQL con due query batch:
          - INSERT in history       (append-only, tipo_operazione='READ')
          - UPSERT in current_state (unica riga per indirizzo)

        In caso di errore MySQL tenta di riconnettersi al DB.
        """
        q_hist = f"""
            INSERT INTO `{self.table_out}`
                (indirizzo_modbus, registro_robot, descrizione,
                 tipo_registro, valore, accesso, tipo_operazione)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """
        q_state = """
            INSERT INTO current_state
                (indirizzo_modbus, registro_robot, descrizione,
                 tipo_registro, valore, accesso)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                registro_robot = VALUES(registro_robot),
                descrizione    = VALUES(descrizione),
                tipo_registro  = VALUES(tipo_registro),
                valore         = VALUES(valore),
                accesso        = VALUES(accesso),
                timestamp      = CURRENT_TIMESTAMP
        """
        try:
            self.cur.executemany(q_hist,  to_history)
            self.cur.executemany(q_state, to_current_state)
            log.info(
                f"💾 {len(to_history)} variazioni → history + current_state"
            )

        except (mysql_errors.OperationalError,
                mysql_errors.InterfaceError,
                mysql_errors.DatabaseError) as e:
            log.error(
                f"❌ Errore DB durante persist — {type(e).__name__}: {e} | "
                f"tentativo riconnessione..."
            )
            self._close_db()
            self._connect_db()

        except Exception as e:
            log.error(
                f"❌ Errore imprevisto durante persist — "
                f"{type(e).__name__}: {e} | "
                f"righe tentate: {len(to_history)}"
            )

    # ──────────────────────────────────────────────────────────────────────────
    # RUN
    # ──────────────────────────────────────────────────────────────────────────

    def run(self) -> None:
        log.info("▶️  ModbusReader avviato")

        if not self._wait_for_modbus():
            log.warning("🛑 Reader fermato durante attesa Modbus")
            return

        if not self._connect_db():
            log.warning("🛑 Reader fermato durante attesa DB")
            return

        self._load_initial_cache()

        log.info(
            f"🚀 Reader ONLINE — poll ogni {self.interval}s | "
            f"{len(self._coil_groups)} gruppi coil, "
            f"{len(self._hr_groups)} gruppi HR"
        )

        while self.running:
            try:
                # Riconnessione Modbus se necessario
                if not self.connected:
                    self.mb.close()
                    if not self._wait_for_modbus():
                        break
                    # Dopo riconnessione: reset throttle errori e cache invalida
                    self._batch_error_ts.clear()
                    self._reset_cache()
                    log.info("🔄 Cache resettata dopo riconnessione Modbus")

                self.cycle()

            except Exception as e:
                log.error(
                    f"❌ Errore ciclo Reader — {type(e).__name__}: {e} | "
                    f"connected={self.connected}"
                )
                self.connected = False

            time.sleep(self.interval)

        log.info("⏹️  Loop Reader terminato")

    # ──────────────────────────────────────────────────────────────────────────
    # STOP
    # ──────────────────────────────────────────────────────────────────────────

    def stop(self) -> None:
        """
        Arresta il thread in modo pulito:
          - segnala l'uscita dal loop (self.running = False)
          - chiude la connessione Modbus
          - chiude cursore e connessione DB
        """
        self.running = False

        try:
            self.mb.close()
        except Exception:
            pass

        self._close_db()

        log.info("🛑 ModbusReader fermato")