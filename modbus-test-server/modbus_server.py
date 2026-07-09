#!/usr/bin/env python3
"""
SERVER MODBUS TCP DI TEST CON VARIAZIONI RANDOM
================================================

Estende il server di test originale con un task che genera
variazioni random su tutti i registri (RO e RW) a intervalli
configurabili, simulando il comportamento di un PLC reale.

RISCRITTURA API (luglio 2026)
------------------------------
La tua versione di pymodbus (3.13.x) ha deprecato l'intera vecchia
API "datastore" (ModbusSequentialDataBlock, ModbusSparseDataBlock,
ModbusDeviceContext, ModbusServerContext): i wrapper deprecati non
espongono piu' getValues/setValues in modo utilizzabile a runtime
("AttributeError: 'ModbusSparseDataBlock' object has no attribute
'getValues'"). Verranno rimossi del tutto in pymodbus v4.

Questo file e' stato riscritto sulla nuova API "simulator"
(SimData / SimDevice / SimCore), che e' quella ufficialmente
supportata ora e in futuro. Il comportamento esterno e' identico:
stesso config.ini, stesso registers.json (con lo stesso campo
opzionale "tipo_dato"), stesso avvio "python modbus_server.py".

Supporto IEEE 754 (float32) sui registri HR a 32 bit
-----------------------------------------------------
I registri HR sono gestiti a coppie di word a 16 bit. Ogni indirizzo
puo' essere marcato in registers.json come "float" oppure "int" (o
"uint"), tramite il campo opzionale "tipo_dato". Se il campo non e'
presente, il comportamento e' un intero a 32 bit senza segno (come
in origine) -> nessuna modifica di utilizzo richiesta.

La codifica/decodifica IEEE 754 e' fatta manualmente con `struct`
(big-endian, stesso ordine word MSW/LSW gia' usato per gli interi)
perche' il protocollo Modbus stesso lavora solo su word a 16 bit
grezze: il DataType.FLOAT32 di SimData serve solo a riservare i 2
registri e a validare l'indirizzamento, non incide sul filo.

Configurazione aggiuntiva in config.ini:
  [sim]
  change_interval  = 5.0    ; secondi tra un ciclo di variazioni e il successivo
  change_ratio     = 0.2    ; frazione di registri che variano ad ogni ciclo (0.0-1.0)
  coil_flip_prob   = 0.3    ; probabilita' che un coil cambi stato
  float_min        = -100.0 ; valore minimo generato per registri float
  float_max        = 500.0  ; valore massimo generato per registri float

Formato registers.json (campo "tipo_dato" opzionale, solo per "hr"):
  [
    {"registro": 10200, "tipo_registro": "hr", "tipo_dato": "float"},
    {"registro": 10000, "tipo_registro": "hr"}                       ; default: int
  ]
"""

import asyncio
import configparser
import json
import logging
import random
import struct
from datetime import datetime
from typing import Dict, List, Optional

from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("modbus-server")

# ──────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ──────────────────────────────────────────────────────────────

cfg = configparser.ConfigParser()
cfg.read("config.ini")

TEST_HOST = cfg.get("modbus.server", "host",  fallback="0.0.0.0")
TEST_PORT = int(cfg.get("modbus.server", "port", fallback="8001"))

CHANGE_INTERVAL = float(cfg.get("sim", "change_interval", fallback="5.0"))
CHANGE_RATIO    = float(cfg.get("sim", "change_ratio",    fallback="0.2"))
COIL_FLIP_PROB  = float(cfg.get("sim", "coil_flip_prob",  fallback="0.3"))
FLOAT_MIN       = float(cfg.get("sim", "float_min",       fallback="-100.0"))
FLOAT_MAX       = float(cfg.get("sim", "float_max",       fallback="500.0"))
MONITOR_INTERVAL = 30

REGISTERS_PATH = "registers.json"

_ICONS = {
    "co": "⚡ COIL",
    "hr": "📝 HREG",
    "di": "🔌 DINP",
    "ir": "📊 IREG",
}

_FLOAT_TAGS = {"float", "float32", "real", "f32"}

# Funzione Modbus -> blocco (stessa mappa usata internamente da pymodbus)
_FX_TO_BLOCK = {2: "d", 4: "i", 3: "h", 6: "h", 16: "h", 22: "h", 23: "h",
                1: "c", 5: "c", 15: "c"}
_BLOCK_TO_TYPE = {"c": "co", "d": "di", "h": "hr", "i": "ir"}  # per _ICONS
_FC_READ  = {"co": 1, "di": 2, "hr": 3, "ir": 4}
_FC_WRITE_HR   = 16   # write multiple registers
_FC_WRITE_COIL = 5    # write single coil

# Mappa indirizzo HR -> "float" | "int", popolata da run_server() prima
# di avviare il server. E' un dict globale mutato in-place (mai
# riassegnato), cosi' la closure trace_action() la vede sempre aggiornata.
HR_FORMAT: Dict[int, str] = {}

# Contatore scritture totali (equivalente del vecchio TracingDataBlock.total_writes)
class WriteCounter:
    total: int = 0

    @classmethod
    def bump(cls):
        cls.total += 1

# ──────────────────────────────────────────────────────────────
# CODIFICA IEEE 754 (float32) SU COPPIE DI WORD 16 BIT
# ──────────────────────────────────────────────────────────────
# Ordine word: MSW poi LSW (big-endian a livello di word), lo stesso
# ordine gia' usato per gli interi a 32 bit ("words[0]<<16 | words[1]").

def words_to_float32(msw: int, lsw: int) -> float:
    raw = struct.pack(">HH", msw & 0xFFFF, lsw & 0xFFFF)
    return struct.unpack(">f", raw)[0]

def float32_to_words(value: float):
    raw = struct.pack(">f", value)
    return struct.unpack(">HH", raw)

def words_to_uint32(msw: int, lsw: int) -> int:
    return ((msw & 0xFFFF) << 16) | (lsw & 0xFFFF)

def uint32_to_words(value: int):
    value = max(0, min(int(value), 0xFFFF_FFFF))
    return (value >> 16) & 0xFFFF, value & 0xFFFF

# ──────────────────────────────────────────────────────────────
# CARICAMENTO REGISTRI
# ──────────────────────────────────────────────────────────────

def _load_registers(path: str):
    """
    Ritorna:
      by_type  : Dict[str, List[int]]   -> indirizzi per tipo registro
      hr_format: Dict[int, str]         -> "float" | "int" per ogni indirizzo HR
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    by_type: Dict[str, List[int]] = {}
    hr_format: Dict[int, str] = {}

    for entry in data:
        rtype = entry["tipo_registro"].strip().lower()
        addr  = int(entry["registro"])
        by_type.setdefault(rtype, []).append(addr)

        if rtype == "hr":
            tipo_dato = str(entry.get("tipo_dato", "int")).strip().lower()
            hr_format[addr] = "float" if tipo_dato in _FLOAT_TAGS else "int"

    for rtype in by_type:
        by_type[rtype] = sorted(set(by_type[rtype]))

    return by_type, hr_format

# ──────────────────────────────────────────────────────────────
# COSTRUZIONE SIMDATA (sostituisce i vecchi ModbusSparseDataBlock)
# ──────────────────────────────────────────────────────────────

def _build_bit_simdata(addresses: List[int], name: str) -> List[SimData]:
    """Coil (co) e discrete input (di): 1 bit per indirizzo logico."""
    if not addresses:
        log.warning(f"  [{name}] nessun indirizzo - blocco dummy creato @0")
        return [SimData(address=0, count=1, values=False, datatype=DataType.BITS)]
    log.info(
        f"  SimData [{name}]: {len(addresses)} indirizzi bit - "
        f"range [{min(addresses):>6} ... {max(addresses):>6}]"
    )
    return [SimData(address=a, count=1, values=False, datatype=DataType.BITS)
            for a in addresses]

def _build_ir_simdata(addresses: List[int]) -> List[SimData]:
    """Input register (ir): 1 registro a 16 bit per indirizzo logico."""
    if not addresses:
        log.warning("  [ir] nessun indirizzo - blocco dummy creato @0")
        return [SimData(address=0, count=1, values=0, datatype=DataType.UINT16)]
    log.info(
        f"  SimData [ir]: {len(addresses)} indirizzi - "
        f"range [{min(addresses):>6} ... {max(addresses):>6}]"
    )
    return [SimData(address=a, count=1, values=0, datatype=DataType.UINT16)
            for a in addresses]

def _build_hr_simdata(addresses: List[int], hr_format: Dict[int, str]) -> List[SimData]:
    """Holding register (hr): 32 bit (2 word) per indirizzo logico,
    float32 IEEE 754 o uint32 a seconda di hr_format."""
    if not addresses:
        log.warning("  [hr] nessun indirizzo - blocco dummy creato @0")
        return [SimData(address=0, count=1, values=0, datatype=DataType.UINT32)]

    n_float = sum(1 for a in addresses if hr_format.get(a) == "float")
    log.info(
        f"  SimData [hr]: {len(addresses)} indirizzi (di cui {n_float} float32) - "
        f"range [{min(addresses):>6} ... {max(addresses):>6}]"
    )

    out = []
    for a in addresses:
        if hr_format.get(a) == "float":
            out.append(SimData(address=a, count=1, values=0.0, datatype=DataType.FLOAT32))
        else:
            out.append(SimData(address=a, count=1, values=0, datatype=DataType.UINT32))
    return out

# ──────────────────────────────────────────────────────────────
# TRACING SCRITTURE (sostituisce TracingDataBlock.setValues)
# ──────────────────────────────────────────────────────────────
# Agganciato come SimDevice.action: viene chiamato per OGNI richiesta
# (lettura o scrittura, da client reali o dal nostro RandomChanger)
# PRIMA che il valore venga applicato, quindi current_registers
# riflette ancora lo stato precedente. Ritornando None si lascia
# proseguire la richiesta normalmente.

async def trace_action(function_code, start_address, address, count,
                        current_registers, set_values):
    if set_values is None:
        return None  # e' una lettura, non logghiamo

    block_id = _FX_TO_BLOCK.get(function_code, "?")
    icon = _ICONS.get(_BLOCK_TO_TYPE.get(block_id, ""), "❓")
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]

    if block_id in ("c", "d"):
        # Coil / discrete input: logghiamo il nuovo stato per bit.
        # (L'offset per i blocchi a bit e' in unita' diverse da
        # quello dei registri, quindi qui mostriamo solo il nuovo
        # valore per restare semplici e corretti al 100%.)
        for i, new_val in enumerate(set_values):
            log.info(f"[{ts}] {icon} #{address + i:>6}  -> {bool(new_val)}")
            WriteCounter.bump()
        return None

    # Holding / input register: qui offset e' in unita' di registro,
    # quindi current_registers[offset:offset+count] e' affidabile.
    offset = address - start_address
    old_words = current_registers[offset:offset + count]
    fmt = HR_FORMAT.get(address, "int") if block_id == "h" else "int"

    if block_id == "h" and count == 2 and fmt == "float":
        old_val = words_to_float32(old_words[0], old_words[1])
        new_val = words_to_float32(set_values[0], set_values[1])
        if old_val != new_val:
            log.info(f"[{ts}] {icon} #{address:>6}  {old_val:.4f} -> {new_val:.4f}  (float32)")
            WriteCounter.bump()
    else:
        changed = False
        for i in range(count):
            if old_words[i] != set_values[i]:
                log.info(f"[{ts}] {icon} #{address + i:>6}  {old_words[i]} -> {set_values[i]}")
                changed = True
        if changed:
            WriteCounter.bump()

    return None

# ──────────────────────────────────────────────────────────────
# TASK: VARIAZIONI RANDOM (IEEE 754 aware sui registri HR float)
# ──────────────────────────────────────────────────────────────

async def random_changes(context, co_addresses: List[int], hr_addresses: List[int],
                          hr_format: Dict[int, str]) -> None:
    """
    Genera variazioni random su tutti i registri (RO e RW) a intervalli
    definiti da CHANGE_INTERVAL, usando l'API async_getValues/async_setValues
    del SimCore (context) del server in esecuzione.
    """
    log.info(
        f"🎲 RandomChanger avviato - "
        f"intervallo={CHANGE_INTERVAL}s, "
        f"ratio={CHANGE_RATIO:.0%}, "
        f"coil_flip={COIL_FLIP_PROB:.0%}, "
        f"float_range=[{FLOAT_MIN}, {FLOAT_MAX}]"
    )

    while True:
        await asyncio.sleep(CHANGE_INTERVAL)

        # ── Variazioni COIL ──────────────────────────────────────
        if co_addresses:
            n_co = max(1, int(len(co_addresses) * CHANGE_RATIO))
            for addr in random.sample(co_addresses, n_co):
                if random.random() >= COIL_FLIP_PROB:
                    continue
                try:
                    current = await context.async_getValues(0, 1, addr, 1)
                    if isinstance(current, ExcCodes):
                        continue
                    new_val = not current[0]
                    res = await context.async_setValues(0, _FC_WRITE_COIL, addr, [new_val])
                    if isinstance(res, ExcCodes):
                        log.warning(f"  RandomChanger coil @{addr}: {res}")
                except Exception as e:
                    log.warning(f"  RandomChanger coil @{addr}: {e}")

        # ── Variazioni HR 32-bit (int o float IEEE 754) ──────────
        if hr_addresses:
            n_hr = max(1, int(len(hr_addresses) * CHANGE_RATIO))
            for addr in random.sample(hr_addresses, n_hr):
                try:
                    fmt = hr_format.get(addr, "int")

                    if fmt == "float":
                        new_val = random.uniform(FLOAT_MIN, FLOAT_MAX)
                        msw, lsw = float32_to_words(new_val)

                    elif 10100 <= addr <= 10104:
                        # Programmi stazione 1-3: 100-399
                        new_val = random.randint(100, 399)
                        msw, lsw = uint32_to_words(new_val)

                    elif addr in range(10000, 10100):
                        # Contatori produzione RO: variazioni incrementali
                        cur = await context.async_getValues(0, 3, addr, 2)
                        if isinstance(cur, ExcCodes):
                            continue
                        current = words_to_uint32(cur[0], cur[1])
                        new_val = min(current + random.randint(0, 5), 0xFFFF_FFFF)
                        msw, lsw = uint32_to_words(new_val)

                    else:
                        new_val = random.randint(0, 9999)
                        msw, lsw = uint32_to_words(new_val)

                    res = await context.async_setValues(0, _FC_WRITE_HR, addr, [msw, lsw])
                    if isinstance(res, ExcCodes):
                        log.warning(f"  RandomChanger HR @{addr}: {res}")
                except Exception as e:
                    log.warning(f"  RandomChanger HR @{addr}: {e}")

# ──────────────────────────────────────────────────────────────
# MONITOR
# ──────────────────────────────────────────────────────────────

async def monitor(context, by_type: Dict[str, List[int]], hr_format: Dict[int, str]) -> None:
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        ts = datetime.now().strftime("%H:%M:%S")
        log.info(f"[{ts}] -- DUMP SERVER  (scritture totali: {WriteCounter.total}) --------")

        for rtype in ("co", "hr", "di", "ir"):
            addrs = by_type.get(rtype, [])
            if not addrs:
                continue
            icon = _ICONS.get(rtype, "❓")
            log.info(f"  {icon}  {len(addrs)} addr logici")
            fc = _FC_READ[rtype]

            for addr in addrs:
                try:
                    if rtype == "hr":
                        result = await context.async_getValues(0, fc, addr, 2)
                        if isinstance(result, ExcCodes):
                            log.info(f"    @{addr:>6} = ???")
                            continue
                        if hr_format.get(addr) == "float":
                            val = words_to_float32(result[0], result[1])
                            log.info(f"    @{addr:>6} = {val:.4f}  (float32)")
                        else:
                            val = words_to_uint32(result[0], result[1])
                            log.info(f"    @{addr:>6} = {val}")
                    else:
                        result = await context.async_getValues(0, fc, addr, 1)
                        if isinstance(result, ExcCodes):
                            log.info(f"    @{addr:>6} = ???")
                            continue
                        val = result[0]
                        if rtype in ("co", "di"):
                            log.info(f"    @{addr:>6} = {bool(val)!s:<5}  ({int(val)})")
                        else:  # ir
                            log.info(f"    @{addr:>6} = {val}")
                except Exception:
                    log.info(f"    @{addr:>6} = ???")

        log.info(f"[{ts}] -- FINE DUMP --------------------------------------")

# ──────────────────────────────────────────────────────────────
# AVVIO
# ──────────────────────────────────────────────────────────────

async def run_server() -> None:
    log.info("=" * 65)
    log.info("🛠️  SERVER MODBUS TCP DI TEST - con variazioni random + IEEE 754")
    log.info("=" * 65)

    by_type, hr_format = _load_registers(REGISTERS_PATH)
    HR_FORMAT.clear()
    HR_FORMAT.update(hr_format)

    co_addrs = by_type.get("co", [])
    di_addrs = by_type.get("di", [])
    hr_addrs = by_type.get("hr", [])
    ir_addrs = by_type.get("ir", [])

    co_simdata = _build_bit_simdata(co_addrs, "co")
    di_simdata = _build_bit_simdata(di_addrs, "di")
    hr_simdata = _build_hr_simdata(hr_addrs, hr_format)
    ir_simdata = _build_ir_simdata(ir_addrs)

    device = SimDevice(
        id=0,
        simdata=(co_simdata, di_simdata, hr_simdata, ir_simdata),
        action=trace_action,
    )

    server = ModbusTcpServer(device, address=(TEST_HOST, TEST_PORT))

    log.info(f"🚀 Server in ascolto su {TEST_HOST}:{TEST_PORT}")
    log.info(f"🎲 Variazioni random ogni {CHANGE_INTERVAL}s (ratio={CHANGE_RATIO:.0%})")
    log.info("=" * 65)

    monitor_task = asyncio.create_task(monitor(server.context, by_type, hr_format))
    changer_task = asyncio.create_task(
        random_changes(server.context, co_addrs, hr_addrs, hr_format)
    )

    try:
        await server.serve_forever()
    finally:
        monitor_task.cancel()
        changer_task.cancel()
        for t in [monitor_task, changer_task]:
            try:
                await t
            except asyncio.CancelledError:
                pass
        log.info("🛑 Server fermato")

if __name__ == "__main__":
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        pass