#!/usr/bin/env bash
###############################################################################
# install.sh — Installazione automatica e idempotente del progetto MODBUS
#
# Uso:
#   sudo ./install.sh
#
# Variabili opzionali (override):
#   MODBUS_DB_PASS=xxxxx sudo -E ./install.sh   # password DB personalizzata
#
# Il repository deve essere già stato clonato in /opt/modbus prima di
# eseguire questo script (git clone <repo> /opt/modbus).
###############################################################################
set -euo pipefail

# ─────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────

PROJECT_ROOT="/opt/modbus"
VENV_DIR="${PROJECT_ROOT}/.venv"
SERVICE_USER="modbus"
SERVICE_GROUP="modbus"

DB_NAME="modbus_db"
DB_USER="modbus"
DB_PASS="${MODBUS_DB_PASS:-modbus}"
DB_HOST="localhost"

GATEWAY_DIR="${PROJECT_ROOT}/modbus-gateway"
FRONTEND_DIR="${PROJECT_ROOT}/modbus-frontend"
SERVICE_DEF_DIR="${PROJECT_ROOT}/service_definition"

MIN_PY_MAJOR=3
MIN_PY_MINOR=10   # richiesto dalla sintassi "Tipo | None" in main.py (PEP 604)

# ─────────────────────────────────────────────────────────────────────────
# UTILITY DI LOG
# ─────────────────────────────────────────────────────────────────────────
c_blue="\033[1;34m"; c_yellow="\033[1;33m"; c_red="\033[1;31m"; c_green="\033[1;32m"; c_reset="\033[0m"
log()   { echo -e "\n${c_blue}==> $*${c_reset}"; }
ok()    { echo -e "${c_green}   ✔ $*${c_reset}"; }
warn()  { echo -e "${c_yellow}   ⚠ $*${c_reset}"; }
err()   { echo -e "${c_red}   ✖ $*${c_reset}" >&2; }

# ─────────────────────────────────────────────────────────────────────────
# CONTROLLI PRELIMINARI
# ─────────────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    err "Questo script deve essere eseguito come root (sudo ./install.sh)."
    exit 1
fi

if [[ ! -d "$PROJECT_ROOT" ]]; then
    err "$PROJECT_ROOT non esiste. Clona prima il repository con:"
    err "  git clone <repo-url> $PROJECT_ROOT"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 1 — AGGIORNAMENTO SISTEMA E PACCHETTI
# ─────────────────────────────────────────────────────────────────────────
log "Aggiornamento indice pacchetti e installazione dipendenze di sistema"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y

# Pacchetti realmente necessari:
#  - python3 / python3-venv / python3-pip : runtime e ambiente virtuale
#  - mariadb-server / mariadb-client      : database
#  - git                                  : per futuri aggiornamenti (git pull)
# NON installiamo gcc/build-essential: tutte le dipendenze Python del progetto
# (flask, flask-cors, gunicorn, mysql-connector-python, pymodbus, pyarmor)
# sono pacchetti puri Python o distribuiti con wheel precompilate, quindi non
# richiedono compilazione nativa.
apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    mariadb-server \
    mariadb-client \
    git

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if (( PY_MAJOR < MIN_PY_MAJOR || (PY_MAJOR == MIN_PY_MAJOR && PY_MINOR < MIN_PY_MINOR) )); then
    err "Python >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR} richiesto (rilevato ${PY_VER})."
    err "modbus-gateway/source/main.py usa la sintassi 'ModbusReader | None' (PEP 604, Python 3.10+)."
    err "Su Raspberry Pi OS 'Bullseye' (Python 3.9) questo script si interrompe volutamente:"
    err "aggiorna a Raspberry Pi OS 'Bookworm' (Python 3.11) o installa Python 3.10+ da deadsnakes."
    exit 1
fi
ok "Python ${PY_VER} rilevato"

# ─────────────────────────────────────────────────────────────────────────
# FASE 1b — UTENTE DI SISTEMA DEDICATO
# ─────────────────────────────────────────────────────────────────────────
log "Configurazione utente di sistema '${SERVICE_USER}'"

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$PROJECT_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Utente di sistema '${SERVICE_USER}' creato"
else
    ok "Utente di sistema '${SERVICE_USER}' già presente"
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — AMBIENTE VIRTUALE PYTHON (condiviso gateway + frontend)
# ─────────────────────────────────────────────────────────────────────────
log "Creazione ambiente virtuale Python in ${VENV_DIR}"

if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    ok "Virtualenv creato"
else
    ok "Virtualenv già esistente, riutilizzo"
fi

# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
pip install --upgrade pip setuptools wheel

# ─────────────────────────────────────────────────────────────────────────
# FASE 3 — REQUIREMENTS
# ─────────────────────────────────────────────────────────────────────────
log "Installazione dipendenze Python (gateway + frontend + pyarmor)"

if [[ ! -f "${GATEWAY_DIR}/requirements.txt" ]]; then
    err "${GATEWAY_DIR}/requirements.txt non trovato."
    exit 1
fi
if [[ ! -f "${FRONTEND_DIR}/requirements.txt" ]]; then
    err "${FRONTEND_DIR}/requirements.txt non trovato."
    exit 1
fi

pip install -r "${GATEWAY_DIR}/requirements.txt"
pip install -r "${FRONTEND_DIR}/requirements.txt"

# pyarmor serve per la Fase 6 (offuscamento): non è runtime-dependency del
# servizio, ma è necessario per generare/rigenerare il build offuscato.
pip install "pyarmor>=9.0,<10.0"

ok "Dipendenze installate"

deactivate

# ─────────────────────────────────────────────────────────────────────────
# FASE 4 — DATABASE MARIADB
# ─────────────────────────────────────────────────────────────────────────
log "Configurazione MariaDB"

systemctl enable --now mariadb

# Su Debian/Raspberry Pi OS l'utente root di MariaDB usa di default il plugin
# unix_socket: eseguendo questo script come root possiamo quindi collegarci
# senza password. Idempotente: CREATE USER IF NOT EXISTS / GRANT sono
# rieseguibili senza errori.
mysql -u root <<SQL
CREATE USER IF NOT EXISTS '${DB_USER}'@'${DB_HOST}' IDENTIFIED BY '${DB_PASS}';
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\` DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'${DB_HOST}';
FLUSH PRIVILEGES;
SQL

ok "Utente '${DB_USER}' e database '${DB_NAME}' pronti"

# ─────────────────────────────────────────────────────────────────────────
# FASE 5 — CONFIGURAZIONE (config.ini)
# ─────────────────────────────────────────────────────────────────────────
log "Generazione file di configurazione"

generate_config() {
    local example_file="$1"
    local target_file="$2"
    if [[ -f "$target_file" ]]; then
        ok "$(basename "$target_file") già presente in $(dirname "$target_file") — non sovrascritto"
        return
    fi
    if [[ ! -f "$example_file" ]]; then
        err "File di esempio mancante: $example_file"
        exit 1
    fi
    cp "$example_file" "$target_file"
    ok "Creato $target_file da $(basename "$example_file")"
}

generate_config "${GATEWAY_DIR}/config.ini.example"  "${GATEWAY_DIR}/config.ini"
generate_config "${FRONTEND_DIR}/config.ini.example" "${FRONTEND_DIR}/config.ini"

# Precompila automaticamente le sole voci relative al database, che sono
# sempre note in questo contesto di installazione. L'IP del server Modbus
# del robot NON viene precompilato: dipende dall'impianto e va impostato
# manualmente dall'utente.
for cfg in "${GATEWAY_DIR}/config.ini" "${FRONTEND_DIR}/config.ini"; do
    sed -i \
        -e "s/^host = .*/host = ${DB_HOST}/" \
        -e "s/^user = .*/user = ${DB_USER}/" \
        -e "s/^password = .*/password = ${DB_PASS}/" \
        "$cfg"
    # Il campo "database" nel frontend è già valorizzato (modbus_db) nel file
    # .example; nel gateway è vuoto: lo impostiamo esplicitamente.
    sed -i -e "s/^database = *$/database = ${DB_NAME}/" "$cfg"
done

CONFIG_INCOMPLETE=0
if grep -q "ip = xxx.yyy.zzz.www" "${GATEWAY_DIR}/config.ini" 2>/dev/null; then
    CONFIG_INCOMPLETE=1
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 6 — INIZIALIZZAZIONE DATABASE (schema + registers.json)
# ─────────────────────────────────────────────────────────────────────────
log "Inizializzazione schema database (install_database.py)"

pushd "$GATEWAY_DIR" >/dev/null
"${VENV_DIR}/bin/python" install_database.py
popd >/dev/null

ok "Schema database inizializzato/verificato"

# ─────────────────────────────────────────────────────────────────────────
# FASE 7 — OFFUSCAMENTO CON PYARMOR (idempotente)
# ─────────────────────────────────────────────────────────────────────────
log "Offuscamento codice gateway con PyArmor"

SOURCE_HAS_PY=$(find "${GATEWAY_DIR}/source" -maxdepth 1 -name "*.py" 2>/dev/null | wc -l)
RUNTIME_OK=0
if compgen -G "${GATEWAY_DIR}/pyarmor_runtime_*/pyarmor_runtime*" > /dev/null 2>&1; then
    RUNTIME_OK=1
fi

if [[ "$SOURCE_HAS_PY" -gt 0 && "$RUNTIME_OK" -eq 0 ]]; then
    warn "Runtime PyArmor mancante o non generato su questa macchina: avvio build offuscato"
    pushd "$GATEWAY_DIR" >/dev/null
    "${VENV_DIR}/bin/python" protect_with_pyarmor.py
    popd >/dev/null
    ok "Build offuscato generato e legato al Machine ID di questa macchina"
elif [[ "$RUNTIME_OK" -eq 1 ]]; then
    ok "Runtime PyArmor già presente e funzionante: nessuna rigenerazione necessaria"
else
    warn "Cartella source/ vuota e runtime assente: impossibile generare il build offuscato automaticamente."
    warn "Verifica manualmente ${GATEWAY_DIR}/source/ e ${GATEWAY_DIR}/source_da_cancellare/"
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 8 — PERMESSI
# ─────────────────────────────────────────────────────────────────────────
log "Impostazione permessi e ownership"

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$PROJECT_ROOT"
chmod 750 "$PROJECT_ROOT"
chmod 640 "${GATEWAY_DIR}/config.ini" "${FRONTEND_DIR}/config.ini" 2>/dev/null || true

mkdir -p "${GATEWAY_DIR}/logs"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${GATEWAY_DIR}/logs"

ok "Permessi applicati (proprietario ${SERVICE_USER}, config.ini in 640)"

# ─────────────────────────────────────────────────────────────────────────
# FASE 9 — SERVIZI SYSTEMD
# ─────────────────────────────────────────────────────────────────────────
log "Installazione servizi systemd"

cp "${SERVICE_DEF_DIR}/modbus_gateway.service"  /etc/systemd/system/modbus_gateway.service
cp "${SERVICE_DEF_DIR}/modbus_frontend.service" /etc/systemd/system/modbus_frontend.service

systemctl daemon-reload
systemctl enable modbus_gateway.service
systemctl enable modbus_frontend.service

if [[ "$CONFIG_INCOMPLETE" -eq 1 ]]; then
    warn "modbus-gateway/config.ini contiene ancora l'IP placeholder (xxx.yyy.zzz.www)."
    warn "I servizi sono stati ABILITATI (partiranno al boot) ma NON avviati ora."
    warn "Modifica ${GATEWAY_DIR}/config.ini con l'IP reale del robot, poi esegui:"
    warn "  sudo systemctl start modbus_gateway.service modbus_frontend.service"
else
    systemctl restart modbus_gateway.service
    systemctl restart modbus_frontend.service
    ok "Servizi avviati"
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 10 — CRON: PULIZIA STORICO DATABASE
# ─────────────────────────────────────────────────────────────────────────
log "Configurazione cron per la pulizia dello storico (purge_history.py)"

CRON_CMD="cd ${GATEWAY_DIR} && ${VENV_DIR}/bin/python purge_history.py >> ${GATEWAY_DIR}/logs/cron_purge.log 2>&1"
CRON_LINE="0 3 * * * ${CRON_CMD}"

# Idempotente: installa il cron per l'utente di sistema 'modbus' solo se non
# già presente (confronto sul comando, non sull'intera riga, per tollerare
# eventuali modifiche future all'orario).
EXISTING_CRON=$(crontab -u "$SERVICE_USER" -l 2>/dev/null || true)
if echo "$EXISTING_CRON" | grep -qF "purge_history.py"; then
    ok "Cron di pulizia già presente per l'utente ${SERVICE_USER}"
else
    { echo "$EXISTING_CRON"; echo "$CRON_LINE"; } | grep -v '^$' | crontab -u "$SERVICE_USER" -
    ok "Cron installato: pulizia storico ogni giorno alle 03:00"
fi

# ─────────────────────────────────────────────────────────────────────────
# RIEPILOGO FINALE
# ─────────────────────────────────────────────────────────────────────────
log "Installazione completata"
echo "  • Progetto:            ${PROJECT_ROOT}"
echo "  • Virtualenv:          ${VENV_DIR}"
echo "  • Database:            ${DB_NAME} (utente: ${DB_USER})"
echo "  • Utente servizi:      ${SERVICE_USER}"
echo "  • Servizio gateway:    systemctl status modbus_gateway.service"
echo "  • Servizio frontend:   systemctl status modbus_frontend.service  (http://<host>:8000)"
echo "  • Log gateway:         ${GATEWAY_DIR}/logs/"
echo "  • Cron pulizia storico: crontab -u ${SERVICE_USER} -l"
if [[ "$CONFIG_INCOMPLETE" -eq 1 ]]; then
    echo -e "${c_yellow}  • ATTENZIONE: completa manualmente l'IP del robot in ${GATEWAY_DIR}/config.ini${c_reset}"
fi
echo