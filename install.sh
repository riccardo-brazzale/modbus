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
SERVICE_DEF_DIR="${PROJECT_ROOT}/services"

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

# Numerazione a video delle fasi principali dello script (12 in totale).
# step() incrementa il contatore ad ogni fase eseguita: se aggiungi o togli
# una fase, aggiorna TOTAL_STEPS di conseguenza.
TOTAL_STEPS=13
STEP_NUM=0
step() {
    STEP_NUM=$((STEP_NUM + 1))
    echo -e "\n${c_blue}==> [Passo ${STEP_NUM}/${TOTAL_STEPS}] $*${c_reset}"
}

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
# FASE 0 — PERMESSI DI ESECUZIONE SUGLI SCRIPT DEL REPO
# ─────────────────────────────────────────────────────────────────────────
# git non preserva il bit +x a meno che non sia stato committato: dopo un
# clone fresco, questi script potrebbero non essere eseguibili. Non è
# strettamente necessario per gli script Python (li invochiamo sempre come
# "<venv>/bin/python script.py"), ma li rendiamo eseguibili comunque per
# poterli lanciare anche a mano in futuro senza doverselo ricordare.
step "Impostazione permessi di esecuzione sugli script del repository"

chmod +x \
    "${PROJECT_ROOT}/install.sh" \
    "${GATEWAY_DIR}/install_database.py" \
    "${GATEWAY_DIR}/protect_with_pyarmor.py" \
    "${GATEWAY_DIR}/purge_history.py" \
    2>/dev/null || true

ok "Permessi di esecuzione impostati"

# ─────────────────────────────────────────────────────────────────────────
# FASE 1 — AGGIORNAMENTO SISTEMA E PACCHETTI
# ─────────────────────────────────────────────────────────────────────────
step "Aggiornamento indice pacchetti e installazione dipendenze di sistema"

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
step "Configurazione utente di sistema '${SERVICE_USER}'"

# IMPORTANTE: creiamo il gruppo ESPLICITAMENTE prima dell'utente. Affidarsi
# a "useradd --system" per creare anche un gruppo omonimo non è affidabile:
# il comportamento dipende da USERGROUPS_ENAB in /etc/login.defs e per gli
# account di sistema spesso non crea alcun gruppo dedicato. Se il gruppo
# "modbus" non esiste, systemd non riesce a risolvere "Group=modbus" nei
# file .service e i servizi falliscono con status=216/GROUP
# ("Failed to determine group credentials: No such process").
if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupadd --system "$SERVICE_GROUP"
    ok "Gruppo di sistema '${SERVICE_GROUP}' creato"
else
    ok "Gruppo di sistema '${SERVICE_GROUP}' già presente"
fi

if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --gid "$SERVICE_GROUP" --home-dir "$PROJECT_ROOT" --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "Utente di sistema '${SERVICE_USER}' creato (gruppo primario: ${SERVICE_GROUP})"
else
    # L'utente esiste già (magari creato manualmente prima di questo fix):
    # assicuriamoci comunque che il gruppo primario sia quello corretto.
    usermod --gid "$SERVICE_GROUP" "$SERVICE_USER"
    ok "Utente di sistema '${SERVICE_USER}' già presente (gruppo primario verificato: ${SERVICE_GROUP})"
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 2 — AMBIENTE VIRTUALE PYTHON (condiviso gateway + frontend)
# ─────────────────────────────────────────────────────────────────────────
step "Creazione ambiente virtuale Python in ${VENV_DIR}"

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
step "Installazione dipendenze Python (gateway + frontend + pyarmor)"

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
step "Configurazione MariaDB"

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
step "Generazione file di configurazione"

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
# FASE 6 — OFFUSCAMENTO CON PYARMOR (idempotente)
# ─────────────────────────────────────────────────────────────────────────
# NOTA D'ORDINE IMPORTANTE: questo passo deve avvenire PRIMA
# dell'inizializzazione del database. install_database.py vive nella root
# di modbus-gateway/ e fa "from logging_utils import setup_logger": Python
# risolve quell'import sul logging_utils.py della STESSA cartella, che è
# la versione già offuscata da PyArmor (importa pyarmor_runtime_000000),
# non quella in chiaro dentro source/. Se il runtime PyArmor non è ancora
# stato generato su questa macchina, install_database.py fallisce con
# "ModuleNotFoundError: No module named 'pyarmor_runtime_000000'".
step "Offuscamento codice gateway con PyArmor"

SOURCE_HAS_PY=$(find "${GATEWAY_DIR}/source" -maxdepth 1 -name "*.py" 2>/dev/null | wc -l)
RUNTIME_OK=0
if compgen -G "${GATEWAY_DIR}/pyarmor_runtime_*/pyarmor_runtime*" > /dev/null 2>&1; then
    RUNTIME_OK=1
fi

if [[ "$SOURCE_HAS_PY" -gt 0 && "$RUNTIME_OK" -eq 0 ]]; then
    warn "Runtime PyArmor mancante o non generato su questa macchina: avvio build offuscato"
    # protect_with_pyarmor.py invoca l'eseguibile "pyarmor" tramite
    # subprocess.run(["pyarmor", ...]), risolto cercando nel PATH.
    # Lanciarlo con l'interprete assoluto ".venv/bin/python" NON è
    # sufficiente: senza attivare il venv, ".venv/bin" non è in PATH e
    # quella subprocess.run fallisce con FileNotFoundError. Va quindi
    # attivato esplicitamente il virtualenv prima di eseguire lo script.
    pushd "$GATEWAY_DIR" >/dev/null
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
    python protect_with_pyarmor.py
    deactivate
    popd >/dev/null
    RUNTIME_OK=0
    if compgen -G "${GATEWAY_DIR}/pyarmor_runtime_*/pyarmor_runtime*" > /dev/null 2>&1; then
        RUNTIME_OK=1
    fi
    if [[ "$RUNTIME_OK" -eq 1 ]]; then
        ok "Build offuscato generato e legato al Machine ID di questa macchina"
    else
        err "protect_with_pyarmor.py terminato ma il runtime non risulta presente."
        err "Controlla l'output sopra (es. licenza PyArmor scaduta/non valida)."
        exit 1
    fi
elif [[ "$RUNTIME_OK" -eq 1 ]]; then
    ok "Runtime PyArmor già presente e funzionante: nessuna rigenerazione necessaria"
else
    err "Cartella source/ vuota e runtime PyArmor assente: impossibile generare il build offuscato."
    err "Verifica manualmente ${GATEWAY_DIR}/source/ e ${GATEWAY_DIR}/source_da_cancellare/"
    err "(senza runtime, anche install_database.py fallirebbe: dipende da logging_utils.py offuscato)"
    exit 1
fi

# ─────────────────────────────────────────────────────────────────────────
# FASE 7 — INIZIALIZZAZIONE DATABASE (schema + registers.json)
# ─────────────────────────────────────────────────────────────────────────
step "Inizializzazione schema database (install_database.py)"

pushd "$GATEWAY_DIR" >/dev/null
"${VENV_DIR}/bin/python" install_database.py
popd >/dev/null

ok "Schema database inizializzato/verificato"

# ─────────────────────────────────────────────────────────────────────────
# FASE 8 — PERMESSI
# ─────────────────────────────────────────────────────────────────────────
step "Impostazione permessi e ownership"

chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$PROJECT_ROOT"
chmod 750 "$PROJECT_ROOT"
chmod 640 "${GATEWAY_DIR}/config.ini" "${FRONTEND_DIR}/config.ini" 2>/dev/null || true

mkdir -p "${GATEWAY_DIR}/logs"
chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${GATEWAY_DIR}/logs"

ok "Permessi applicati (proprietario ${SERVICE_USER}, config.ini in 640)"

# ─────────────────────────────────────────────────────────────────────────
# FASE 9 — SERVIZI SYSTEMD
# ─────────────────────────────────────────────────────────────────────────
step "Installazione servizi systemd"

cp "${SERVICE_DEF_DIR}/modbus_gateway.service"  /etc/systemd/system/modbus_gateway.service
cp "${SERVICE_DEF_DIR}/modbus_frontend.service" /etc/systemd/system/modbus_frontend.service

chmod +x /etc/systemd/system/modbus_gateway.service
chmod +x /etc/systemd/system/modbus_frontend.service

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
step "Configurazione cron per la pulizia dello storico (purge_history.py)"

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
# FASE 11 — RIMOZIONE SORGENTI IN CHIARO
# ─────────────────────────────────────────────────────────────────────────
# A questo punto il build offuscato è stato generato e verificato in FASE 6:
# i servizi systemd importano solo i moduli offuscati (pyarmor_runtime_*),
# quindi i sorgenti in chiaro non servono più a runtime. Li rimuoviamo per
# non lasciarli mai in chiaro sulla macchina di destinazione.
step "Rimozione sorgenti in chiaro"

SRC_TO_DELETE="${GATEWAY_DIR}/source_da_cancellare"
if [[ -d "$SRC_TO_DELETE" ]]; then
    rm -rf -- "$SRC_TO_DELETE"
    ok "Cartella sorgenti in chiaro copia rimossa: ${SRC_TO_DELETE}"
else
    ok "Cartella sorgenti in chiaro copia già assente: ${SRC_TO_DELETE}"
fi

SRC_TO_DELETE="${GATEWAY_DIR}/source"
if [[ -d "$SRC_TO_DELETE" ]]; then
    rm -rf -- "$SRC_TO_DELETE"
    ok "Cartella sorgenti in chiaro rimossa: ${SRC_TO_DELETE}"
else
    ok "Cartella sorgenti in chiaro già assente: ${SRC_TO_DELETE}"
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

# ─────────────────────────────────────────────────────────────────────────
# AUTODISTRUZIONE DELLO SCRIPT
# ─────────────────────────────────────────────────────────────────────────
# Rimuoviamo install.sh stesso: un utente non autorizzato non potrà
# rilanciarlo per errore (o deliberatamente) e rigenerare/alterare
# l'installazione o un runtime PyArmor legato a un Machine ID diverso.
# rm sul proprio $0 mentre lo script gira è sicuro: bash ha già bufferizzato
# tutto il codice necessario prima di arrivare a questo punto.
rm -f -- "${PROJECT_ROOT}/install.sh"