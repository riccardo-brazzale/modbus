#!/usr/bin/env python3
"""
SERVER MODBUS TCP DI TEST CON VARIAZIONI RANDOM
================================================

Estende il server di test originale con un task che genera
variazioni random su tutti i registri (RO e RW) a intervalli
configurabili, simulando il comportamento di un PLC reale.

Configurazione aggiuntiva in config.ini:
  [sim]
  change_interval  = 5.0    ; secondi tra un ciclo di variazioni e il successivo
  change_ratio     = 0.2    ; frazione di registri che variano ad ogni ciclo (0.0–1.0)
  coil_flip_prob   = 0.3    ; probabilità che un coil cambi stato
"""

import asyncio
import configparser
import json
import logging
import random
from datetime import datetime
from typing import Dict, List

from pymodbus.datastore import (
    ModbusServerContext,
    ModbusDeviceContext,
    ModbusSparseDataBlock,
)
from pymodbus.server import StartAsyncTcpServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("modbus-server")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ──────────────────────────────────────────────────────────────────────────────

cfg = configparser.ConfigParser()
cfg.read("config.ini")

TEST_HOST = cfg.get("modbus.server", "host",  fallback="0.0.0.0")
TEST_PORT = int(cfg.get("modbus.server", "port", fallback="8001"))

# Parametri simulazione variazioni random
CHANGE_INTERVAL = float(cfg.get("sim", "change_interval", fallback="5.0"))
CHANGE_RATIO    = float(cfg.get("sim", "change_ratio",    fallback="0.2"))
COIL_FLIP_PROB  = float(cfg.get("sim", "coil_flip_prob",  fallback="0.3"))
MONITOR_INTERVAL = 30

REGISTERS_PATH = "registers.json"

_ICONS = {
    "co": "⚡ COIL",
    "hr": "📝 HREG",
    "di": "🔌 DINP",
    "ir": "📊 IREG",
}

# ──────────────────────────────────────────────────────────────────────────────
# CARICAMENTO REGISTRI
# ──────────────────────────────────────────────────────────────────────────────

def _load_registers(path: str) -> Dict[str, List[int]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    by_type: Dict[str, List[int]] = {}
    for entry in data:
        rtype = entry["tipo_registro"].strip().lower()
        addr  = int(entry["registro"])
        by_type.setdefault(rtype, []).append(addr)
    for rtype in by_type:
        by_type[rtype] = sorted(set(by_type[rtype]))
    return by_type

# ──────────────────────────────────────────────────────────────────────────────
# TRACING DATABLOCK (invariato rispetto all'originale)
# ──────────────────────────────────────────────────────────────────────────────

class TracingDataBlock:
    total_writes: int = 0

    def __init__(self, name: str, datablock: ModbusSparseDataBlock):
        self.name      = name
        self.datablock = datablock
        self._writes   = 0

    def validate(self, address: int, count: int = 1) -> bool:
        return self.datablock.validate(address, count)

    def getValues(self, address: int, count: int = 1):
        return self.datablock.getValues(address, count)

    def setValues(self, address: int, values):
        try:
            old = self.datablock.getValues(address, len(values))
        except Exception:
            old = [None] * len(values)

        result = self.datablock.setValues(address, values)

        icon = _ICONS.get(self.name, "❓")
        ts   = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        changed = False
        for i, (o, n) in enumerate(zip(old, values)):
            if o != n:
                o_s = bool(o) if self.name == "co" else o
                n_s = bool(n) if self.name == "co" else n
                log.info(f"[{ts}] {icon} #{address + i:>6}  {o_s} → {n_s}")
                changed = True
        if changed:
            self._writes += 1
            TracingDataBlock.total_writes += 1
        return result

    def dump(self, logical_addresses: List[int], word_step: int = 1) -> List[str]:
        lines = []
        for addr in logical_addresses:
            try:
                if word_step == 2:
                    words = self.datablock.getValues(addr, 2)
                    val   = (words[0] << 16) | words[1]
                    lines.append(f"    @{addr:>6} = {val}")
                else:
                    val = self.datablock.getValues(addr, 1)[0]
                    lines.append(f"    @{addr:>6} = {bool(val)!s:<5}  ({val})")
            except Exception:
                lines.append(f"    @{addr:>6} = ???")
        return lines

# ──────────────────────────────────────────────────────────────────────────────
# COSTRUZIONE DATABLOCK
# ──────────────────────────────────────────────────────────────────────────────

_SENTINEL_COUNT = {1: 2, 2: 3}

def _make_sparse_block(
    logical_addresses: List[int],
    word_step: int = 1,
    name: str = "",
    default: int = 0,
) -> TracingDataBlock:
    if not logical_addresses:
        log.warning(f"  [{name}] nessun indirizzo — blocco dummy creato @0")
        return TracingDataBlock(name, ModbusSparseDataBlock({0: default}))

    word_map: Dict[int, int] = {}
    for addr in logical_addresses:
        for w in range(word_step):
            word_map[addr + w] = default

    max_word    = max(word_map.keys())
    n_sentinels = _SENTINEL_COUNT.get(word_step, word_step + 1)
    for s in range(1, n_sentinels + 1):
        word_map[max_word + s] = default

    block = ModbusSparseDataBlock(word_map)
    log.info(
        f"  SparseBlock [{name}]: "
        f"{len(logical_addresses)} indirizzi logici, "
        f"{len(word_map) - n_sentinels} word reali + {n_sentinels} sentinelle — "
        f"range reale [{min(word_map):>6} … {max_word:>6}]"
    )
    return TracingDataBlock(name, block)

# ──────────────────────────────────────────────────────────────────────────────
# TASK: VARIAZIONI RANDOM  ← NUOVO
# ──────────────────────────────────────────────────────────────────────────────

async def random_changes(
    co_block: TracingDataBlock,
    hr_block: TracingDataBlock,
    co_addresses: List[int],
    hr_addresses: List[int],
) -> None:
    """
    Genera variazioni random su tutti i registri (RO e RW) a intervalli
    definiti da CHANGE_INTERVAL.

    Logica:
      - Seleziona una frazione CHANGE_RATIO degli indirizzi disponibili.
      - Coil: con probabilità COIL_FLIP_PROB inverte il bit corrente.
      - HR 32-bit: valore random in [0, 0xFFFF_FFFF] con range ristretto
        per valori semantici (es. programmi in [100, 399]).
    """
    log.info(
        f"🎲 RandomChanger avviato — "
        f"intervallo={CHANGE_INTERVAL}s, "
        f"ratio={CHANGE_RATIO:.0%}, "
        f"coil_flip={COIL_FLIP_PROB:.0%}"
    )

    while True:
        await asyncio.sleep(CHANGE_INTERVAL)
        ts = datetime.now().strftime("%H:%M:%S")

        # ── Variazioni COIL ──────────────────────────────────────────────────
        if co_addresses:
            n_co = max(1, int(len(co_addresses) * CHANGE_RATIO))
            selected_co = random.sample(co_addresses, n_co)
            for addr in selected_co:
                if random.random() < COIL_FLIP_PROB:
                    try:
                        current = co_block.datablock.getValues(addr, 1)[0]
                        new_val = 0 if current else 1
                        co_block.setValues(addr, [new_val])
                    except Exception as e:
                        log.warning(f"  RandomChanger coil @{addr}: {e}")

        # ── Variazioni HR 32-bit ─────────────────────────────────────────────
        if hr_addresses:
            n_hr = max(1, int(len(hr_addresses) * CHANGE_RATIO))
            selected_hr = random.sample(hr_addresses, n_hr)
            for addr in selected_hr:
                try:
                    # Genera valore random nel range plausibile per il registro.
                    # I registri noti per programmi usano range [100, 399],
                    # gli altri usano [0, 9999] come contatori/stati plausibili.
                    if 10100 <= addr <= 10104:
                        # Programmi stazione 1-3: 100-399
                        new_val = random.randint(100, 399)
                    elif addr in range(10000, 10100):
                        # Contatori produzione RO: variazioni incrementali
                        words   = hr_block.datablock.getValues(addr, 2)
                        current = (words[0] << 16) | words[1]
                        # Incremento random piccolo per simulare contatore
                        new_val = min(current + random.randint(0, 5), 0xFFFF_FFFF)
                    else:
                        new_val = random.randint(0, 9999)

                    msw = (new_val >> 16) & 0xFFFF
                    lsw =  new_val        & 0xFFFF
                    hr_block.setValues(addr, [msw, lsw])
                except Exception as e:
                    log.warning(f"  RandomChanger HR @{addr}: {e}")

        log.debug(f"[{ts}] 🎲 Ciclo variazioni completato")

# ──────────────────────────────────────────────────────────────────────────────
# MONITOR
# ──────────────────────────────────────────────────────────────────────────────

async def monitor(
    blocks: Dict[str, TracingDataBlock],
    logical: Dict[str, List[int]],
) -> None:
    word_step_map = {"co": 1, "hr": 2, "di": 1, "ir": 1}
    while True:
        await asyncio.sleep(MONITOR_INTERVAL)
        ts    = datetime.now().strftime("%H:%M:%S")
        total = TracingDataBlock.total_writes
        log.info(f"[{ts}] ── DUMP SERVER  (scritture totali: {total}) ──────────")
        for rtype, block in blocks.items():
            addrs = logical.get(rtype, [])
            if not addrs:
                continue
            step = word_step_map.get(rtype, 1)
            icon = _ICONS.get(rtype, "❓")
            log.info(f"  {icon}  {len(addrs)} addr logici | {block._writes} scritture")
            for line in block.dump(addrs, word_step=step):
                log.info(line)
        log.info(f"[{ts}] ── FINE DUMP ───────────────────────────────────────────")

# ──────────────────────────────────────────────────────────────────────────────
# AVVIO
# ──────────────────────────────────────────────────────────────────────────────

async def run_server() -> None:
    log.info("=" * 65)
    log.info("🛠️  SERVER MODBUS TCP DI TEST — con variazioni random")
    log.info("=" * 65)

    by_type = _load_registers(REGISTERS_PATH)

    co_block = _make_sparse_block(by_type.get("co", []), word_step=1, name="co")
    hr_block = _make_sparse_block(by_type.get("hr", []), word_step=2, name="hr")
    di_block = _make_sparse_block(by_type.get("di", []), word_step=1, name="di")
    ir_block = _make_sparse_block(by_type.get("ir", []), word_step=1, name="ir")

    blocks  = {"co": co_block, "hr": hr_block, "di": di_block, "ir": ir_block}
    logical = by_type

    context = ModbusServerContext(
        ModbusDeviceContext(co=co_block, hr=hr_block, di=di_block, ir=ir_block),
        single=True,
    )

    log.info(f"🚀 Server in ascolto su {TEST_HOST}:{TEST_PORT}")
    log.info(f"🎲 Variazioni random ogni {CHANGE_INTERVAL}s (ratio={CHANGE_RATIO:.0%})")
    log.info("=" * 65)

    monitor_task = asyncio.create_task(monitor(blocks, logical))
    changer_task = asyncio.create_task(
        random_changes(
            co_block, hr_block,
            by_type.get("co", []),
            by_type.get("hr", []),
        )
    )

    try:
        await StartAsyncTcpServer(context, address=(TEST_HOST, TEST_PORT))
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