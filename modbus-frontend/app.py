#!/usr/bin/env python3
"""
MODBUS DASHBOARD — Flask Application
Interfaccia web industriale per monitoraggio e controllo registri Modbus.
"""

import configparser
import json
import time
import threading
import subprocess
from datetime import datetime
from collections import deque

import mysql.connector
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

# ──────────────────────────────────────────────
# CONFIGURAZIONE
# ──────────────────────────────────────────────

cfg = configparser.ConfigParser()
cfg.read("config.ini")

DB_CONFIG = {
    "host":     cfg["database"]["host"],
    "user":     cfg["database"]["user"],
    "password": cfg["database"]["password"],
    "database": cfg["database"]["database"],
}

MODBUS_IP   = cfg["modbus_server"]["ip"]
MODBUS_PORT = cfg["modbus_server"]["port"]

GATEWAY_SERVICE_NAME = "modbus_gateway.service"

TABLE_OUT   = cfg["database"]["base_table_out"]
TABLE_IN    = cfg["database"]["base_table_in"]

app = Flask(__name__)
CORS(app)

# ──────────────────────────────────────────────
# CHANGE HISTORY (in-memory ring buffer)
# ──────────────────────────────────────────────

MAX_HISTORY = 200
change_history = deque(maxlen=MAX_HISTORY)
history_lock   = threading.Lock()

# ──────────────────────────────────────────────
# DB HELPERS
# ──────────────────────────────────────────────

def get_db():
    return mysql.connector.connect(**DB_CONFIG)

def db_query(sql, params=None, fetchall=True):
    conn = get_db()
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(sql, params or ())
        return cur.fetchall() if fetchall else cur.fetchone()
    finally:
        conn.close()

def db_execute(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()

# ──────────────────────────────────────────────
# VALIDAZIONE
# ──────────────────────────────────────────────

def validate_register_value(tipo_registro: str, value):
    try:
        if tipo_registro == "co":
            v = int(value)
            if v not in (0, 1):
                return False, "Coil: valore deve essere 0 o 1", None
            return True, "", v
        elif tipo_registro == "hr":
            v = float(value)
            if abs(v) > 3.40282347e38:
                return False, "Holding Register: valore fuori range per float 32-bit", None
            val_str = str(value).strip()
            if '.' in val_str:
                dec_part = val_str.split('.')[1]
                if len(dec_part) > 5:
                    return False, "Holding Register: massimo 5 decimali consentiti", None
            return True, "", v
        else:
            return False, f"Tipo registro non supportato: {tipo_registro}", None
    except (ValueError, TypeError):
        return False, f"Valore non numerico: {value}", None

# ──────────────────────────────────────────────
# BACKGROUND CHANGE TRACKER
# ──────────────────────────────────────────────

_last_snapshot = {}   # addr → valore
_tracker_lock  = threading.Lock()

def _track_changes():
    """Thread daemon: confronta current_state ogni 500ms e registra variazioni."""
    global _last_snapshot
    while True:
        try:
            rows = db_query(
                "SELECT indirizzo_modbus, valore, tipo_registro, accesso, timestamp "
                "FROM current_state"
            )
            new_snapshot = {r["indirizzo_modbus"]: r["valore"] for r in rows}

            with _tracker_lock:
                changes = []
                for addr, val in new_snapshot.items():
                    if addr in _last_snapshot and _last_snapshot[addr] != val:
                        changes.append({
                            "addr": addr,
                            "old":  _last_snapshot[addr],
                            "new":  val,
                            "ts":   datetime.now().isoformat(),
                        })
                _last_snapshot = new_snapshot

            if changes:
                with history_lock:
                    for c in changes:
                        change_history.appendleft(c)

        except Exception as e:
            print(f"[TRACKER ERROR] {e}")
        time.sleep(0.5)

# Avvia il tracker in un thread separato (non blocca Gunicorn)
tracker_thread = threading.Thread(target=_track_changes, daemon=True)
tracker_thread.start()

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", server_ip=MODBUS_IP, server_port=MODBUS_PORT)

@app.route("/api/status")
def api_status():
    try:
        conn = get_db()
        conn.close()
        db_ok = True
    except Exception:
        db_ok = False

    try:
        stats = db_query("SELECT accesso, COUNT(*) as cnt FROM current_state GROUP BY accesso")
        counts = {r["accesso"]: r["cnt"] for r in stats}
    except Exception:
        counts = {}

    return jsonify({
        "db_connected": db_ok,
        "server_ip": MODBUS_IP,
        "server_port": MODBUS_PORT,
        "timestamp": datetime.now().isoformat(),
        "counts": counts,
    })

@app.route("/api/gateway-service-status")
def api_gateway_service_status():
    """
    Interroga systemd per sapere se modbus_gateway.service è attivo.
    'systemctl is-active' non richiede privilegi speciali per la sola lettura.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-active", GATEWAY_SERVICE_NAME],
            capture_output=True, text=True, timeout=3,
        )
        state = result.stdout.strip()  # "active" | "inactive" | "failed" | "activating" | ...
        return jsonify({"active": state == "active", "state": state})
    except Exception as e:
        return jsonify({"active": False, "state": "unknown", "error": str(e)})

@app.route("/api/registers/ro")
def api_ro():
    since = request.args.get("since")
    sql = "SELECT indirizzo_modbus, registro_robot, descrizione, tipo_registro, valore, timestamp FROM current_state WHERE LOWER(accesso)='ro'"
    params = []
    if since:
        sql += " AND timestamp > %s"
        params.append(since)
    sql += " ORDER BY indirizzo_modbus"

    try:
        rows = db_query(sql, tuple(params))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for r in rows:
        if hasattr(r.get("timestamp"), "isoformat"):
            r["timestamp"] = r["timestamp"].isoformat()
    return jsonify({"data": rows, "server_ts": datetime.now().isoformat(), "count": len(rows)})

@app.route("/api/registers/rw")
def api_rw():
    try:
        rows = db_query("SELECT indirizzo_modbus, registro_robot, descrizione, tipo_registro, valore, timestamp FROM current_state WHERE LOWER(accesso)='rw' ORDER BY indirizzo_modbus")
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    for r in rows:
        if hasattr(r.get("timestamp"), "isoformat"):
            r["timestamp"] = r["timestamp"].isoformat()
    return jsonify({"data": rows, "count": len(rows)})

@app.route("/api/write", methods=["POST"])
def api_write():
    body = request.get_json(force=True, silent=True)
    if not body or "commands" not in body:
        return jsonify({"error": "Body JSON non valido o mancante"}), 400

    commands = body["commands"]
    if not isinstance(commands, list) or len(commands) == 0:
        return jsonify({"error": "Lista comandi vuota"}), 400
    if len(commands) > 100:
        return jsonify({"error": "Troppi comandi (max 100)"}), 400

    results = []
    errors  = []

    for cmd in commands:
        addr  = cmd.get("address")
        value = cmd.get("value")
        if addr is None or value is None:
            errors.append({"address": addr, "error": "address o value mancante"})
            continue
        try:
            reg = db_query("SELECT tipo_registro, accesso FROM current_state WHERE indirizzo_modbus=%s", (str(addr),), fetchall=False)
        except Exception as e:
            errors.append({"address": addr, "error": f"DB error: {e}"})
            continue
        if not reg:
            errors.append({"address": addr, "error": "Registro non trovato"})
            continue
        if reg["accesso"] != "rw":
            errors.append({"address": addr, "error": "Registro in sola lettura (ro)"})
            continue
        ok, msg, converted = validate_register_value(reg["tipo_registro"], value)
        if not ok:
            errors.append({"address": addr, "error": msg})
            continue
        try:
            db_execute(f"INSERT INTO `{TABLE_IN}` (indirizzo_modbus, tipo_registro, valore_da_impostare, timestamp) VALUES (%s,%s,%s,NOW())",
                       (str(addr), reg["tipo_registro"], converted))
            results.append({"address": addr, "value": converted, "status": "queued"})
        except Exception as e:
            errors.append({"address": addr, "error": str(e)})

    return jsonify({"queued": len(results), "errors": len(errors), "results": results, "error_details": errors, "timestamp": datetime.now().isoformat()}), (200 if not errors else 207)

@app.route("/api/history/changes")
def api_history_changes():
    limit = min(int(request.args.get("limit", 50)), MAX_HISTORY)
    with history_lock:
        data = list(change_history)[:limit]
    return jsonify({"data": data, "total": len(change_history)})

@app.route("/api/history/db")
def api_history_db():
    limit = min(int(request.args.get("limit", 100)), 500)
    addr  = request.args.get("addr")
    if addr:
        rows = db_query(f"SELECT indirizzo_modbus, registro_robot, descrizione, tipo_registro, valore, accesso, tipo_operazione, timestamp FROM `{TABLE_OUT}` WHERE indirizzo_modbus=%s ORDER BY timestamp DESC LIMIT %s", (addr, limit))
    else:
        rows = db_query(f"SELECT indirizzo_modbus, registro_robot, descrizione, tipo_registro, valore, accesso, tipo_operazione, timestamp FROM `{TABLE_OUT}` ORDER BY timestamp DESC LIMIT %s", (limit,))
    for r in rows:
        if hasattr(r.get("timestamp"), "isoformat"):
            r["timestamp"] = r["timestamp"].isoformat()
    return jsonify({"data": rows, "count": len(rows)})

# ──────────────────────────────────────────────
# ENTRYPOINT SOLO PER DEBUG, Gunicorn ignora
# ──────────────────────────────────────────────
if __name__ == "__main__":
    # debug=True solo per sviluppo, non in produzione
    app.run(host="0.0.0.0", port=5000, debug=True, threaded=True)