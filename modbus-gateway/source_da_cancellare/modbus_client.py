"""
CLIENT MODBUS TCP ROBUSTO  (pymodbus 3.x)
==========================================

Gestione errori in due livelli distinti:

  ModbusDeviceError  (sottoclasse di Exception, NON di OSError)
    → ExceptionResponse dal server: l'indirizzo esiste ma il server
      ha rifiutato l'operazione (exception_code 1-4).
      È un errore PERMANENTE per quel registro/valore specifico.
      Il chiamante deve scartare il comando, NON fare retry.

  Errori di connessione (OSError, BrokenPipe, timeout, ecc.)
    → Il client va offline, il chiamante deve riconnettersi e riprovare.

Questo è il punto centrale che impedisce i loop infiniti osservati nei log
quando il server risponde con ExceptionResponse (exception_code=4).
"""

import time
import errno
from typing import Optional
from pymodbus.client import ModbusTcpClient
from pymodbus.pdu import ExceptionResponse
from logging_utils import setup_logger

log = setup_logger("modbus_client", "log.log")


# ──────────────────────────────────────────────────────────────────────────────
# ECCEZIONE PUBBLICA
# ──────────────────────────────────────────────────────────────────────────────

class ModbusDeviceError(Exception):
    """
    Il server Modbus ha risposto con un'eccezione (ExceptionResponse).
    L'operazione è fallita in modo permanente per questa richiesta specifica.
    Non ha senso riprovare con gli stessi parametri.

    Attributi:
        address       : indirizzo Modbus coinvolto
        exception_code: codice eccezione Modbus (1=illegal function,
                        2=illegal address, 3=illegal value, 4=device failure)
        response      : oggetto ExceptionResponse originale
    """
    def __init__(self, address: int, response: ExceptionResponse):
        self.address        = address
        self.exception_code = getattr(response, "exception_code", -1)
        self.response       = response
        super().__init__(
            f"Modbus ExceptionResponse @{address} "
            f"(fc={getattr(response, 'function_code', '?')}, "
            f"exc={self.exception_code}): {response}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# CLIENT
# ──────────────────────────────────────────────────────────────────────────────

class OptimizedModbusClient:
    """
    Client Modbus TCP robusto per uso industriale.
    Supporta lettura/scrittura di Coils e Holding Registers a 32-bit.
    """

    def __init__(self, ip: str, port: int = 502, timeout: float = 3.0):
        self.ip      = ip
        self.port    = port
        self.timeout = timeout

        self.client = ModbusTcpClient(ip, port=port, timeout=timeout)

        self.is_connected      = False
        self._offline          = True
        self._last_error_key   = None

        self.last_operation_time    = 0
        self.min_operation_interval = 0.001

        log.info(f"🔧 Client Modbus creato per {ip}:{port}")

    # ──────────────────────────────────────────────────────────────────────────
    # UTILS
    # ──────────────────────────────────────────────────────────────────────────

    def _ensure_interval(self):
        elapsed = time.time() - self.last_operation_time
        if elapsed < self.min_operation_interval:
            time.sleep(self.min_operation_interval - elapsed)
        self.last_operation_time = time.time()

    def _log_once(self, key: str, message: str, level: str = "error"):
        if self._last_error_key == key:
            return
        self._last_error_key = key
        getattr(log, level)(message)

    def _set_offline(self, reason: str):
        self.is_connected = False
        self._offline     = True
        self._log_once(reason, f"❌ Modbus OFFLINE: {reason}")
        try:
            self.client.close()
        except Exception:
            pass

    def _set_online(self):
        self.is_connected    = True
        self._offline        = False
        self._last_error_key = None
        log.info("✅ Modbus ONLINE")

    # ──────────────────────────────────────────────────────────────────────────
    # CONNESSIONE
    # ──────────────────────────────────────────────────────────────────────────

    def connect(self) -> bool:
        try:
            if self.client.connect():
                self._set_online()
                return True
            self._set_offline("Connessione fallita")
            return False
        except Exception as e:
            self._set_offline(str(e))
            return False

    def close(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.is_connected = False
        self._offline     = True
        log.info("🔌 Connessione Modbus chiusa")

    # ──────────────────────────────────────────────────────────────────────────
    # GESTIONE ERRORI  —  cuore della logica di classificazione
    # ──────────────────────────────────────────────────────────────────────────

    def _check_response(self, response, address: int):
        """
        Analizza la risposta pymodbus e solleva l'eccezione appropriata:

          ModbusDeviceError  → ExceptionResponse (errore permanente del server)
          ConnectionError    → errore di trasporto TCP (errore transiente)

        Se la risposta è valida non fa nulla.
        """
        if not response.isError():
            return

        # ExceptionResponse: il server Modbus ha rifiutato la richiesta
        if isinstance(response, ExceptionResponse):
            raise ModbusDeviceError(address, response)

        # Qualsiasi altro tipo di errore pymodbus → trattato come conn error
        raise ConnectionError(f"Risposta errore Modbus @{address}: {response}")

    def _handle_connection_error(self, err: Exception):
        """
        Classifica e gestisce gli errori di connessione (non Modbus device errors).
        ModbusDeviceError NON deve passare qui — va gestito dal chiamante.
        """
        if isinstance(err, ModbusDeviceError):
            # Non deve arrivare qui, ma per sicurezza ri-solleva
            raise

        if isinstance(err, OSError):
            if err.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNREFUSED,
                             errno.ENOTCONN, 10038):   # 10038 = WSAENOTSOCK (Windows)
                self._set_offline(err.strerror or str(err))
                raise

        msg = str(err)
        if any(k in msg for k in ("Failed to connect", "Connection", "timed out")):
            self._set_offline(msg)
            raise

        # Errore non critico (e.g. warning dal layer pymodbus)
        log.error(f"Errore Modbus non critico: {err}")

    # ──────────────────────────────────────────────────────────────────────────
    # LETTURA COIL
    # ──────────────────────────────────────────────────────────────────────────

    def read_coil(self, address: int) -> Optional[bool]:
        self._ensure_interval()
        if not self.is_connected:
            return None
        try:
            response = self.client.read_coils(address, count=1)
            self._check_response(response, address)
            return bool(response.bits[0])
        except ModbusDeviceError:
            raise   # il chiamante decide cosa fare
        except Exception as e:
            self._handle_connection_error(e)
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # LETTURA HOLDING REGISTER 32-BIT
    # ──────────────────────────────────────────────────────────────────────────

    def read_holding_register_32bit(self, address: int) -> Optional[float]:
        self._ensure_interval()
        if not self.is_connected:
            return None
        try:
            response = self.client.read_holding_registers(address, count=2)
            self._check_response(response, address)
            high, low = response.registers
            import struct
            bytes_value = struct.pack(">HH", high, low)
            return struct.unpack(">f", bytes_value)[0]
        except ModbusDeviceError:
            raise
        except Exception as e:
            self._handle_connection_error(e)
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # SCRITTURA COIL
    # ──────────────────────────────────────────────────────────────────────────

    def write_coil(self, address: int, value: bool) -> bool:
        """
        Scrive un singolo coil.

        Solleva ModbusDeviceError se il server risponde con ExceptionResponse
        (es. exception_code=4 device failure, exception_code=2 illegal address).
        Il chiamante deve scartare il comando, non fare retry.
        """
        self._ensure_interval()
        if not self.is_connected:
            return False
        try:
            # Passa int(0/1) — alcuni server rifiutano bool Python nativi
            int_value = 1 if value else 0
            response  = self.client.write_coil(address, int_value)
            self._check_response(response, address)
            log.info(f"✅ Coil @{address} = {int_value}")
            return True
        except ModbusDeviceError:
            raise
        except Exception as e:
            self._handle_connection_error(e)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # SCRITTURA HOLDING REGISTER 32-BIT
    # ──────────────────────────────────────────────────────────────────────────

    def write_holding_register_32bit(self, address: int, value: float) -> bool:
        """
        Scrive un holding register a 32-bit (2 word consecutive, big-endian) come float.

        Solleva ModbusDeviceError se il server risponde con ExceptionResponse.
        """
        self._ensure_interval()
        if not self.is_connected:
            return False
        try:
            fv = float(value)
            import struct
            bytes_value = struct.pack(">f", fv)
            msw = int.from_bytes(bytes_value[:2], byteorder="big")
            lsw = int.from_bytes(bytes_value[2:], byteorder="big")

            response = self.client.write_registers(address, [msw, lsw])
            self._check_response(response, address)
            log.info(f"✅ HR32 @{address} = {fv}  (0x{msw:04X}{lsw:04X})")
            return True
        except ModbusDeviceError:
            raise
        except Exception as e:
            self._handle_connection_error(e)
            return False
