#!/usr/bin/env python3
"""
ENTRYPOINT SERVIZIO MODBUS
===========================

Avvia Reader e Writer come thread daemon e li supervisiona.
Se un thread muore (es. per perdita connessione non recuperata),
viene riavviato automaticamente dopo un breve backoff.

Gestione segnali:
  SIGTERM / SIGINT  →  arresto pulito con join dei thread
"""

import os
import time
import signal
import sys
import threading

from modbus_reader import ModbusReader
from modbus_writer import ModbusWriter
from logging_utils import setup_logger

# Assicura che il CWD sia la directory dello script (necessario per systemd)
os.chdir(os.path.dirname(os.path.abspath(__file__)))

log = setup_logger("main", "log.log")

SUPERVISE_INTERVAL = 5    # secondi tra un check dei thread e il successivo
RESTART_BACKOFF    = 10   # secondi di attesa prima di riavviare un thread morto


class ModbusService:
    """
    Contenitore del servizio: gestisce avvio, supervisione e arresto
    di Reader e Writer come thread daemon riavviabili.
    """

    def __init__(self):
        self.running = True
        self._lock   = threading.Lock()

        self.reader: ModbusReader | None = None
        self.writer: ModbusWriter | None = None

    # ──────────────────────────────────────────────────────────────────────────

    def _new_reader(self) -> ModbusReader:
        t = ModbusReader()
        t.daemon = True
        return t

    def _new_writer(self) -> ModbusWriter:
        t = ModbusWriter()
        t.daemon = True
        return t

    # ──────────────────────────────────────────────────────────────────────────

    def start(self):
        log.info("🟢 Avvio servizio Modbus (Reader + Writer)")

        self.reader = self._new_reader()
        self.writer = self._new_writer()

        self.reader.start()
        self.writer.start()

        log.info(f"🚀 Servizio ONLINE — supervisione ogni {SUPERVISE_INTERVAL}s")

        while self.running:
            time.sleep(SUPERVISE_INTERVAL)
            self._supervise()

    def _supervise(self):
        """
        Controlla che i thread siano vivi.
        Se un thread è morto, lo ricrea e lo riavvia dopo un backoff.
        """
        with self._lock:
            if not self.running:
                return

            if self.reader is not None and not self.reader.is_alive():
                log.error(
                    f"❌ ModbusReader terminato inaspettatamente! "
                    f"Riavvio tra {RESTART_BACKOFF}s..."
                )
                time.sleep(RESTART_BACKOFF)
                try:
                    self.reader = self._new_reader()
                    self.reader.start()
                    log.info("🔄 ModbusReader riavviato")
                except Exception as e:
                    log.error(f"❌ Impossibile riavviare ModbusReader: {e}")

            if self.writer is not None and not self.writer.is_alive():
                log.error(
                    f"❌ ModbusWriter terminato inaspettatamente! "
                    f"Riavvio tra {RESTART_BACKOFF}s..."
                )
                time.sleep(RESTART_BACKOFF)
                try:
                    self.writer = self._new_writer()
                    self.writer.start()
                    log.info("🔄 ModbusWriter riavviato")
                except Exception as e:
                    log.error(f"❌ Impossibile riavviare ModbusWriter: {e}")

    # ──────────────────────────────────────────────────────────────────────────

    def stop(self):
        with self._lock:
            if not self.running:
                return
            self.running = False

        log.info("🔴 Arresto servizio in corso...")

        for component, name in [
            (self.reader, "ModbusReader"),
            (self.writer, "ModbusWriter"),
        ]:
            if component is None:
                continue
            try:
                component.stop()
                component.join(timeout=10)
                if component.is_alive():
                    log.warning(f"⚠️  {name} non terminato entro 10s")
                else:
                    log.info(f"✅ {name} fermato")
            except Exception as e:
                log.warning(f"Errore stop {name}: {e}")

        log.info("✅ Servizio arrestato")


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    service = ModbusService()

    def shutdown(sig, frame):
        sig_name = signal.Signals(sig).name
        log.info(f"📴 Segnale ricevuto: {sig_name} — arresto in corso")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    try:
        service.start()
    except Exception as e:
        log.error(f"❌ Errore critico: {e}")
        service.stop()
        sys.exit(1)


if __name__ == "__main__":
    main()