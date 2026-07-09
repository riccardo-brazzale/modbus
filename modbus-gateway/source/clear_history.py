#!/usr/bin/env python3
"""
CLEAR HISTORY — Pulizia tabella history
========================================

Script autonomo di manutenzione per la tabella ``history``.
Può essere eseguito manualmente oppure invocato da un cron job.

Modalità disponibili:
  --days N        Elimina i record più vecchi di N giorni (default: config.ini)
  --all           Tronca completamente la tabella (ATTENZIONE: irreversibile)
  --dry-run       Mostra quanti record verrebbero eliminati, senza eliminare
  --optimize      Esegue OPTIMIZE TABLE dopo la pulizia
  --stats         Mostra statistiche attuali della tabella ed esce

Esempi:
  python clear_history.py --dry-run
  python clear_history.py --days 30
  python clear_history.py --days 90 --optimize
  python clear_history.py --all
  python clear_history.py --stats
"""

import argparse
import configparser
import sys
import mysql.connector
from mysql.connector import errors as mysql_errors
from logging_utils import setup_logger

log = setup_logger("clear_history", "log.log")

# Batch size per le DELETE: evita lock prolungati sulla tabella
DELETE_BATCH_SIZE = 5000


# ──────────────────────────────────────────────────────────────────────────────

def load_config(config_path: str = "config.ini") -> dict:
    cfg = configparser.ConfigParser()
    cfg.read(config_path)
    return {
        "host":     cfg["database"]["host"],
        "user":     cfg["database"]["user"],
        "password": cfg["database"]["password"],
        "database": cfg["database"]["database"],
        "table":    cfg["database"].get("base_table_out", "history"),
        "retention_days": int(cfg["database"].get("history_retention_days", 90)),
    }


def connect(cfg: dict) -> mysql.connector.MySQLConnection:
    return mysql.connector.connect(
        host=cfg["host"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        connection_timeout=10,
        autocommit=True,
    )


# ──────────────────────────────────────────────────────────────────────────────

def show_stats(conn, table: str) -> None:
    cur = conn.cursor(dictionary=True)

    cur.execute(f"""
        SELECT
            COUNT(*)                                AS totale_righe,
            MIN(timestamp)                          AS prima_riga,
            MAX(timestamp)                          AS ultima_riga,
            SUM(tipo_operazione = 'READ')           AS letture,
            SUM(tipo_operazione = 'WRITE')          AS scritture,
            ROUND(
                (SELECT data_length + index_length
                 FROM information_schema.TABLES
                 WHERE table_schema = DATABASE()
                   AND table_name = %s) / 1024 / 1024, 2
            ) AS dimensione_MB
        FROM `{table}`
    """, (table,))

    row = cur.fetchone()
    cur.close()

    print("\n" + "=" * 55)
    print(f"📊 STATISTICHE TABELLA `{table}`")
    print("=" * 55)
    print(f"  Righe totali   : {row['totale_righe']:,}")
    print(f"  Prima riga     : {row['prima_riga']}")
    print(f"  Ultima riga    : {row['ultima_riga']}")
    print(f"  Letture (READ) : {row['letture']:,}")
    print(f"  Scritture (WR) : {row['scritture']:,}")
    print(f"  Dimensione     : {row['dimensione_MB']} MB")
    print("=" * 55 + "\n")


def count_old_rows(conn, table: str, days: int) -> int:
    cur = conn.cursor()
    cur.execute(
        f"SELECT COUNT(*) FROM `{table}` WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY)",
        (days,)
    )
    count = cur.fetchone()[0]
    cur.close()
    return count


def delete_old_rows(conn, table: str, days: int) -> int:
    """
    Elimina i record più vecchi di ``days`` giorni in batch da DELETE_BATCH_SIZE.
    Restituisce il numero totale di righe eliminate.
    """
    total_deleted = 0
    cur = conn.cursor()

    while True:
        cur.execute(
            f"DELETE FROM `{table}` WHERE timestamp < DATE_SUB(NOW(), INTERVAL %s DAY) LIMIT %s",
            (days, DELETE_BATCH_SIZE)
        )
        deleted = cur.rowcount
        total_deleted += deleted
        log.debug(f"Batch eliminato: {deleted} righe (totale finora: {total_deleted})")
        if deleted < DELETE_BATCH_SIZE:
            break   # nessun altro record da eliminare

    cur.close()
    return total_deleted


def truncate_table(conn, table: str) -> None:
    cur = conn.cursor()
    cur.execute(f"TRUNCATE TABLE `{table}`")
    cur.close()


def optimize_table(conn, table: str) -> None:
    print(f"⚙️  OPTIMIZE TABLE `{table}` in corso (potrebbe richiedere qualche minuto)...")
    cur = conn.cursor()
    cur.execute(f"OPTIMIZE TABLE `{table}`")
    cur.fetchall()
    cur.close()
    print("✅ OPTIMIZE completata")


# ──────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Pulizia tabella history del gateway Modbus",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--days",     type=int,        help="Elimina record più vecchi di N giorni")
    parser.add_argument("--all",      action="store_true", help="Tronca completamente la tabella")
    parser.add_argument("--dry-run",  action="store_true", help="Simula senza eliminare")
    parser.add_argument("--optimize", action="store_true", help="Esegui OPTIMIZE TABLE dopo la pulizia")
    parser.add_argument("--stats",    action="store_true", help="Mostra statistiche ed esci")
    parser.add_argument("--config",   default="config.ini", help="Percorso del file config.ini")
    args = parser.parse_args()

    # Almeno un'azione deve essere specificata
    if not any([args.days, args.all, args.dry_run, args.stats, args.optimize]):
        parser.print_help()
        sys.exit(0)

    # ── Caricamento configurazione ────────────────────────────────────────────
    try:
        cfg = load_config(args.config)
    except Exception as e:
        print(f"❌ Errore lettura configurazione: {e}")
        sys.exit(1)

    table        = cfg["table"]
    default_days = cfg["retention_days"]
    days         = args.days if args.days is not None else default_days

    # ── Connessione DB ────────────────────────────────────────────────────────
    try:
        conn = connect(cfg)
        log.info(f"✅ Connesso a {cfg['host']}/{cfg['database']}")
    except mysql_errors.Error as e:
        print(f"❌ Connessione DB fallita: {e}")
        log.error(f"Connessione DB fallita: {e}")
        sys.exit(1)

    try:
        # ── Solo statistiche ──────────────────────────────────────────────────
        if args.stats:
            show_stats(conn, table)
            return

        # Mostra sempre le statistiche prima di operare
        show_stats(conn, table)

        # ── Truncate completo ─────────────────────────────────────────────────
        if args.all:
            if args.dry_run:
                cur = conn.cursor()
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                total = cur.fetchone()[0]
                cur.close()
                print(f"🔍 DRY-RUN: TRUNCATE eliminerebbe {total:,} righe totali")
                return

            confirm = input(
                f"\n⚠️  Stai per eliminare TUTTI i record dalla tabella `{table}`.\n"
                f"   Questa operazione è IRREVERSIBILE. Confermi? [sì/no]: "
            ).strip().lower()
            if confirm not in ("sì", "si", "yes", "y"):
                print("❌ Operazione annullata.")
                return

            truncate_table(conn, table)
            log.info(f"TRUNCATE TABLE {table} eseguita")
            print(f"✅ Tabella `{table}` svuotata completamente")

        # ── Eliminazione per data ─────────────────────────────────────────────
        else:
            count = count_old_rows(conn, table, days)
            print(f"🔎 Record più vecchi di {days} giorni: {count:,}")

            if count == 0:
                print("ℹ️  Nessun record da eliminare.")
                if args.optimize:
                    optimize_table(conn, table)
                return

            if args.dry_run:
                print(f"🔍 DRY-RUN: {count:,} record verrebbero eliminati (nessuna modifica eseguita)")
                return

            print(f"🗑️  Eliminazione in corso (batch da {DELETE_BATCH_SIZE} righe)...")
            deleted = delete_old_rows(conn, table, days)
            log.info(f"Eliminati {deleted} record da `{table}` (retention: {days} giorni)")
            print(f"✅ Eliminati {deleted:,} record da `{table}`")

        # ── Ottimizzazione ────────────────────────────────────────────────────
        if args.optimize:
            optimize_table(conn, table)

        # Statistiche finali
        print("\n📊 Stato dopo la pulizia:")
        show_stats(conn, table)

    except mysql_errors.Error as e:
        log.error(f"Errore MySQL: {e}")
        print(f"❌ Errore MySQL: {e}")
        sys.exit(1)

    finally:
        try:
            conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
