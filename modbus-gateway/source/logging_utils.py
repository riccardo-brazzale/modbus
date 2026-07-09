"""
SISTEMA DI LOGGING UNIFICATO
Gestisce logging a video e su file con rotazione giornaliera.
"""

import logging
from logging.handlers import TimedRotatingFileHandler
import configparser
import os

class UnifiedLogger:
    """Gestore centralizzato del logging per tutti i moduli."""
    
    @staticmethod
    def setup_logger(name: str, filename: str, config_path: str = "config.ini") -> logging.Logger:
        """
        Configura un logger con rotazione giornaliera dei file.
        
        Args:
            name: Nome del logger (es. "gateway", "reader", "writer")
            filename: Nome del file di log (es. "gateway.log")
            config_path: Percorso del file di configurazione
        
        Returns:
            Logger configurato con output a video e file
        """
        # Lettura configurazione
        cfg = configparser.ConfigParser()
        cfg.read(config_path)
        
        # Estrazione parametri
        try:
            LOG_DIR = cfg["logging"]["log_dir"]
            RETENTION = int(cfg["logging"]["retention_days"])
            LEVEL = getattr(logging, cfg["logging"]["level"].upper(), logging.INFO)
        except KeyError as e:
            print(f"⚠️  Configurazione logging non trovata, uso valori di default: {e}")
            LOG_DIR = "logs"
            RETENTION = 7
            LEVEL = logging.INFO
        
        # Crea directory log se non esiste
        os.makedirs(LOG_DIR, exist_ok=True)
        
        # Crea o recupera logger
        logger = logging.getLogger(name)
        logger.setLevel(LEVEL)
        logger.propagate = False  # Evita propagazione al logger root
        
        # Se il logger ha già handler configurati, restituiscilo
        if logger.handlers:
            return logger
        
        # Formattazione avanzata con colori per la console
        class ColorFormatter(logging.Formatter):
            """Formattatore con colori ANSI per la console."""
            GREY = "\x1b[38;20m"
            GREEN = "\x1b[32;20m"
            YELLOW = "\x1b[33;20m"
            RED = "\x1b[31;20m"
            BOLD_RED = "\x1b[31;1m"
            RESET = "\x1b[0m"
            
            FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
            
            FORMATS = {
                logging.DEBUG: GREY + FORMAT + RESET,
                logging.INFO: GREEN + FORMAT + RESET,
                logging.WARNING: YELLOW + FORMAT + RESET,
                logging.ERROR: RED + FORMAT + RESET,
                logging.CRITICAL: BOLD_RED + FORMAT + RESET
            }
            
            def format(self, record):
                log_fmt = self.FORMATS.get(record.levelno)
                formatter = logging.Formatter(log_fmt, "%Y-%m-%d %H:%M:%S")
                return formatter.format(record)
        
        # Formattazione per file (senza colori)
        file_formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
            "%Y-%m-%d %H:%M:%S"
        )
        
        # Handler per file con rotazione giornaliera
        file_handler = TimedRotatingFileHandler(
            os.path.join(LOG_DIR, filename),
            when="midnight",
            backupCount=RETENTION,
            encoding="utf-8"
        )
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(LEVEL)
        
        # Handler per console (stdout) con colori
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(ColorFormatter())
        console_handler.setLevel(LEVEL)
        
        # Aggiungi entrambi gli handler
        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        
        return logger

def setup_logger(name: str, filename: str) -> logging.Logger:
    """
    Funzione wrapper per compatibilità con codice esistente.
    
    Args:
        name: Nome del logger
        filename: Nome del file di log
    
    Returns:
        Logger configurato
    """
    return UnifiedLogger.setup_logger(name, filename)

# Test del modulo
if __name__ == "__main__":
    print("🧪 Test sistema di logging...")
    test_logger = setup_logger("test", "test.log")
    test_logger.debug("Messaggio di debug")
    test_logger.info("Messaggio informativo")
    test_logger.warning("Messaggio di avviso")
    test_logger.error("Messaggio di errore")
    test_logger.critical("Messaggio critico")
    print("✅ Test completato!")