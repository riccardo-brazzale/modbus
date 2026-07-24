"""
GESTORE CONFIGURAZIONE REGISTRI MODBUS
Campo "accesso": "ro" (read-only) / "rw" (read-write).

Regole:
  ro → può SOLO essere letto
  rw → può essere letto e scritto
"""

import json
from typing import Dict, Set, Optional
from logging_utils import setup_logger

log = setup_logger("register_config", "log.log")

ACCESS_RO    = "ro"
ACCESS_RW    = "rw"
VALID_ACCESS = {ACCESS_RO, ACCESS_RW}
VALID_DATA_TYPES = {"int", "float"}

REGISTER_TYPES = {
    "co": "Coils",
    "hr": "Holding Registers",
    "di": "Discrete Inputs",
    "ir": "Input Registers",
}


class RegisterConfigManager:
    """
    Carica registers.json e fornisce metodi di interrogazione su
    tipo, accesso e metadati di ogni registro Modbus.
    """

    def __init__(self, config_path: str = "registers.json", print_summary: bool = True):
        self.config_path = config_path

        self._registers:   Dict[int, dict]      = {}
        self._by_type:     Dict[str, Set[int]]  = {k: set() for k in REGISTER_TYPES}
        self._ro_addresses: Set[int] = set()
        self._rw_addresses: Set[int] = set()

        self._load()
        if print_summary:
            self._print_summary()

    # ------------------------------------------------------------------ #

    def _load(self):
        log.info(f"📄 Caricamento registri da {self.config_path}")

        with open(self.config_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        for entry in data:
            addr     = int(entry["registro"])
            reg_type = entry["tipo_registro"].strip().lower()
            accesso  = entry.get("accesso", ACCESS_RO).strip().lower()

            if reg_type not in REGISTER_TYPES:
                log.warning(f"⚠️  @{addr}: tipo '{reg_type}' non valido, skip")
                continue

            if accesso not in VALID_ACCESS:
                log.warning(f"⚠️  @{addr}: accesso '{accesso}' sconosciuto → ro (fail-safe)")
                accesso = ACCESS_RO

            data_type = self._parse_data_type(addr, reg_type, entry)

            record = {
                "registro":       addr,
                "tipo_registro":  reg_type,
                "registro_robot": entry.get("registro_robot", f"REG_{addr}"),
                "descrizione":    entry.get("descrizione", ""),
                "accesso":        accesso,
                "data_type":      data_type,
            }
            self._registers[addr] = record
            self._by_type[reg_type].add(addr)

            if accesso == ACCESS_RO:
                self._ro_addresses.add(addr)
            else:
                self._rw_addresses.add(addr)

        log.info(
            f"✅ {len(self._registers)} registri caricati "
            f"({len(self._ro_addresses)} ro, {len(self._rw_addresses)} rw)"
        )

    @staticmethod
    def _parse_data_type(addr: int, reg_type: str, entry: dict) -> Optional[str]:
        data_type = entry.get("data_type")
        if reg_type == "hr":
            if data_type not in VALID_DATA_TYPES:
                raise ValueError(f"@{addr}: data_type obbligatorio per hr (int o float)")
            return data_type
        if data_type is not None:
            raise ValueError(f"@{addr}: data_type consentito solo per hr")
        return None

    def _print_summary(self):
        print("\n" + "=" * 65)
        print("📋 CONFIGURAZIONE REGISTRI MODBUS")
        print("=" * 65)
        for reg_type, label in REGISTER_TYPES.items():
            addrs = sorted(self._by_type[reg_type])
            if not addrs:
                continue
            ro = sum(1 for a in addrs if a in self._ro_addresses)
            rw = len(addrs) - ro
            preview = ", ".join(map(str, addrs[:8]))
            if len(addrs) > 8:
                preview += f"  … +{len(addrs) - 8} altri"
            print(f"\n  {label}  ({len(addrs)} indirizzi — {ro} ro / {rw} rw)")
            print(f"    {preview}")
        print("\n" + "-" * 65)
        print(
            f"  TOTALE: {len(self._registers)}  |  "
            f"🔒 ro: {len(self._ro_addresses)}  |  "
            f"✏️  rw: {len(self._rw_addresses)}"
        )
        print("=" * 65 + "\n")

    # ------------------------------------------------------------------ #
    #  API                                                                 #
    # ------------------------------------------------------------------ #

    def get(self, address: int) -> Optional[dict]:
        return self._registers.get(address)

    def exists(self, address: int) -> bool:
        return address in self._registers

    def get_type(self, address: int) -> Optional[str]:
        rec = self._registers.get(address)
        return rec["tipo_registro"] if rec else None

    def get_access(self, address: int) -> Optional[str]:
        rec = self._registers.get(address)
        return rec["accesso"] if rec else None

    def get_data_type(self, address: int) -> Optional[str]:
        rec = self._registers.get(address)
        return rec["data_type"] if rec else None

    def is_readable(self, address: int) -> bool:
        return address in self._registers

    def is_writable(self, address: int) -> bool:
        return address in self._rw_addresses

    def is_readonly(self, address: int) -> bool:
        return address in self._ro_addresses

    def all_addresses(self) -> Set[int]:
        return set(self._registers.keys())

    def readable_addresses(self) -> Set[int]:
        return set(self._registers.keys())

    def writable_addresses(self) -> Set[int]:
        return set(self._rw_addresses)

    def addresses_by_type(self, reg_type: str) -> Set[int]:
        return set(self._by_type.get(reg_type, set()))

    def all_records(self) -> Dict[int, dict]:
        return dict(self._registers)

    def get_statistics(self) -> dict:
        stats = {"total": len(self._registers), "ro": len(self._ro_addresses),
                 "rw": len(self._rw_addresses), "by_type": {}}
        for reg_type, label in REGISTER_TYPES.items():
            addrs = sorted(self._by_type[reg_type])
            ro = [a for a in addrs if a in self._ro_addresses]
            rw = [a for a in addrs if a in self._rw_addresses]
            stats["by_type"][label] = {
                "count": len(addrs), "ro": len(ro), "rw": len(rw),
                "min": min(addrs) if addrs else None,
                "max": max(addrs) if addrs else None,
            }
        return stats
