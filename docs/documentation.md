
📚 Documentazione del Progetto MODBUS
Gateway Modbus per Robot con Supporto IEEE 754
📋 Indice
Introduzione

Architettura del Sistema

Componenti

Requisiti di Sistema

Installazione e Configurazione

Struttura dei Registri Modbus

Formato Dati IEEE 754

Server Modbus IEEE 754

Gateway Modbus

Frontend Web

Protezione del Codice con PyArmor

Database MariaDB

Test e Debug

Risoluzione dei Problemi

Sviluppo Futuro

1. Introduzione
   1.1 Scopo del Progetto
   Il progetto MODBUS è un gateway sviluppato in Python per la gestione e il monitoraggio dei dati provenienti da un robot industriale. Il sistema consente di:

Leggere e scrivere dati Modbus da/per un robot

Memorizzare i dati in un database MariaDB

Visualizzare i dati attraverso un'interfaccia web

Proteggere il codice sorgente con crittografia PyArmor

1.2 Caratteristiche Principali
Caratteristica	Descrizione
Supporto IEEE 754	Gestione di numeri in virgola mobile a 32 bit
Multi-threading	Lettura e scrittura simultanea dei dati
Database	Persistenza dei dati su MariaDB
API REST	Esposizione dei dati per applicazioni esterne
Offuscamento	Protezione del codice con PyArmor
Configurabile	File di configurazione config.ini
Cross-platform	Funziona su Windows, Linux e macOS
1.3 Tecnologie Utilizzate
Tecnologia	Versione	Descrizione
Python	3.8+	Linguaggio di programmazione principale
pymodbus	3.13.1	Libreria per protocollo Modbus TCP
Flask	2.3.x	Framework web per API REST
MariaDB	10.5+	Database per persistenza dati
PyArmor	9.2.5	Offuscamento e protezione del codice
HTML/CSS/JS	-	Frontend web
2. Architettura del Sistema
2.1 Diagramma Architetturale
text
┌─────────────────────────────────────────────────────────────┐
│                        MODBUS SYSTEM                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              ROBOT (MACCHINA FISICA)                │  │
│  │              Dati in formato IEEE 754               │  │
│  └────────────────────┬─────────────────────────────────┘  │
│                       │                                    │
│                       │ Modbus TCP (Porta 5020)            │
│                       ▼                                    │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              MODBUS-GATEWAY                         │  │
│  │  ┌─────────────────────────────────────────────┐   │  │
│  │  │  Server Modbus IEEE 754 (pymodbus)         │   │  │
│  │  │  - Lettura registri (holding registers)    │   │  │
│  │  │  - Scrittura registri                      │   │  │
│  │  │  - Simulazione robot (opzionale)           │   │  │
│  │  └─────────────────────────────────────────────┘   │  │
│  │  ┌─────────────────────────────────────────────┐   │  │
│  │  │  Database Manager (MariaDB)                │   │  │
│  │  │  - Inserimento dati in tempo reale         │   │  │
│  │  │  - Storico dati                            │   │  │
│  │  └─────────────────────────────────────────────┘   │  │
│  └────────────────┬─────────────────────────────────────┘  │
│                   │                                       │
│                   │ HTTP/REST (Porta 5001)                │
│                   ▼                                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │              FRONTEND WEB (modbus-frontend)         │  │
│  │  - Dashboard in tempo reale                        │  │
│  │  - Visualizzazione grafici                         │  │
│  │  - Controllo robot                                 │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
2.2 Flusso dei Dati
Robot → Server Modbus: Il robot invia dati via Modbus TCP al server

Server Modbus → Database: I dati vengono salvati su MariaDB

Server Modbus → Frontend: I dati sono disponibili via API REST

Frontend → Server Modbus: Comandi di controllo inviati via API

3. Componenti
   3.1 Struttura delle Cartelle
   text
   MODBUS_RASPBERRY/
   ├── modbus-gateway/              # Gateway principale
   │   ├── source/                  # Codice sorgente (offuscato dopo deploy)
   │   ├── source_da_cancellare/    # Backup del codice originale
   │   ├── pyarmor_runtime_*/       # Runtime PyArmor (generato)
   │   ├── config.ini               # Configurazione del gateway
   │   ├── main.py                  # Entry point offuscato
   │   ├── modbus_client.py         # Client Modbus offuscato
   │   ├── modbus_reader.py         # Lettore registri offuscato
   │   ├── modbus_writer.py         # Scrittore registri offuscato
   │   ├── register_config.py       # Configurazione registri offuscato
   │   └── logging_utils.py         # Utility di logging offuscato
   │
   ├── modbus-frontend/             # Interfaccia web
   │   ├── static/                  # File statici (CSS, JS)
   │   ├── templates/               # Template HTML
   │   └── app.py                   # Applicazione Flask
   │
   ├── modbus-test-server/          # Server di test IEEE 754
   │   └── modbus_server.py         # Server di simulazione
   │
   ├── service_definition/          # Definizioni servizi
   ├── modbus-documentation/        # Documentazione
   ├── docs/                        # Documentazione tecnica
   ├── config.ini.example           # Esempio di configurazione
   ├── README.md                    # Questo file
   └── .gitignore                   # File ignorati da Git
   3.2 Descrizione Componenti
   Componente	Descrizione
   modbus-gateway	Gateway principale che gestisce la comunicazione Modbus
   modbus-frontend	Interfaccia web per visualizzazione e controllo
   modbus-test-server	Server di test per simulare il robot
   service_definition	Definizioni per servizi di sistema
   modbus-documentation	Documentazione del progetto
4. Requisiti di Sistema
   4.1 Hardware
   Raspberry Pi 3/4 (consigliato) o PC con Windows/Linux

Minimo 1 GB di RAM

2 GB di spazio su disco

Connessione di rete (Ethernet o WiFi)

4.2 Software
Python 3.8 o superiore

MariaDB 10.5 o superiore (o MySQL)

pip (gestore pacchetti Python)

Git (per il versionamento)

4.3 Dipendenze Python
bash

# File: requirements.txt

pymodbus==3.13.1
flask==2.3.3
mysql-connector-python==8.2.0
python-dotenv==1.0.0
requests==2.31.0
pyarmor==9.2.5
5. Installazione e Configurazione
5.1 Clonazione del Repository
bash
git clone https://github.com/riccardo-brazzale/MODBUS.git
cd MODBUS
5.2 Creazione Ambiente Virtuale
bash

# Windows

python -m venv .venv
.venv\Scripts\activate

# Linux / macOS

python3 -m venv .venv
source .venv/bin/activate
5.3 Installazione Dipendenze
bash
pip install -r requirements.txt
5.4 Configurazione
Creare il file config.ini a partire dall'esempio:

bash
cp config.ini.example config.ini
Modificare config.ini con i parametri del proprio sistema:

ini
[SERVER]
host = 0.0.0.0              # Indirizzo IP del server (0.0.0.0 = tutte le interfacce)
port = 5020                 # Porta Modbus TCP
rest_port = 5001            # Porta API REST
update_interval = 1.0       # Intervallo di aggiornamento (secondi)

[DATABASE]
host = localhost            # Host MariaDB
user = modbus_user          # Utente database
password = your_password    # Password database
database = modbus_data      # Nome database
table = robot_data          # Tabella dati

[LOGGING]
level = INFO                # Livello di logging (DEBUG, INFO, WARNING, ERROR)
file = logs/modbus.log      # File di log
5.5 Setup Database MariaDB
sql
-- Connettersi a MariaDB
mysql -u root -p

-- Creare il database
CREATE DATABASE modbus_data;

-- Creare l'utente
CREATE USER 'modbus_user'@'localhost' IDENTIFIED BY 'your_password';

-- Assegnare permessi
GRANT ALL PRIVILEGES ON modbus_data.* TO 'modbus_user'@'localhost';
FLUSH PRIVILEGES;

-- Creare la tabella dei dati
USE modbus_data;

CREATE TABLE robot_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    robot_x FLOAT,
    robot_y FLOAT,
    robot_z FLOAT,
    robot_speed FLOAT,
    robot_temp FLOAT,
    robot_battery FLOAT,
    robot_status INT,
    robot_charge FLOAT
);
6. Struttura dei Registri Modbus
6.1 Mappa dei Registri
Il server utilizza i seguenti Holding Registers (indirizzi 0-based):

Indirizzo	Dato	Tipo	Descrizione
0-1	robot_x	Float	Posizione X (metri)
2-3	robot_y	Float	Posizione Y (metri)
4-5	robot_z	Float	Posizione Z (metri)
6-7	robot_speed	Float	Velocità (m/s)
8-9	robot_temp	Float	Temperatura motore (°C)
10-11	robot_battery	Float	Batteria (%)
12	robot_status	UInt16	Stato robot (0=idle, 1=running, 2=error)
14-15	robot_charge	Float	Corrente di carica (A)
Nota: I valori Float occupano 2 registri (32 bit), in formato Big-Endian (IEEE 754).

6.2 Codici di Stato del Robot
Codice	Stato	Descrizione
0	Idle	Robot in attesa
1	Running	Robot in movimento
2	Error	Errore / Allarme
7. Formato Dati IEEE 754
7.1 Conversione Float ↔ Registri
Il server utilizza lo standard IEEE 754 per i numeri a virgola mobile a 32 bit (float). Ogni float occupa 2 registri Modbus (4 byte).

python
def float_to_registers(value: float) -> tuple[int, int]:
    """Converte un float in due registri (Big-Endian)."""
    packed = pack('>f', value)  # >f = Big-Endian Float
    reg1 = int.from_bytes(packed[0:2], 'big')
    reg2 = int.from_bytes(packed[2:4], 'big')
    return reg1, reg2

def registers_to_float(reg1: int, reg2: int) -> float:
    """Converte due registri in un float (Big-Endian)."""
    packed = reg1.to_bytes(2, 'big') + reg2.to_bytes(2, 'big')
    return unpack('>f', packed)[0]
7.2 Esempio di Conversione
Valore Float	Registro 1	Registro 2
3.14	16457	205
-5.5	49344	0
123.456	17475	18369
8. Server Modbus IEEE 754
8.1 Avvio del Server
bash
cd modbus-gateway
python main.py
8.2 Server di Test (Simulazione)
Se non si dispone di un robot fisico, è possibile utilizzare il server di test:

bash
cd modbus-test-server
python modbus_server.py
8.3 Endpoint Modbus
Funzione	Codice	Descrizione
read_holding_registers	0x03	Lettura di registri a 16 bit
write_single_register	0x06	Scrittura singolo registro
write_multiple_registers	0x10	Scrittura multipla di registri
9. Gateway Modbus
9.1 Client Modbus
Il client Modbus legge i dati dal robot e li scrive nel database.

python
from pymodbus.client import ModbusTcpClient

client = ModbusTcpClient(host='localhost', port=5020)
client.connect()

# Lettura di 8 registri (4 float)

result = client.read_holding_registers(0, 8, slave=1)
if not result.isError():
    # Elaborazione dei dati
    pass

client.close()
9.2 Loop di Lettura
Il gateway esegue un loop continuo per la lettura dei dati:

Connettersi al robot via Modbus TCP

Leggere i registri configurati

Convertire i registri in valori float

Salvare i dati nel database

Attendere l'intervallo di aggiornamento

10. Frontend Web
    10.1 Avvio del Frontend
    bash
    cd modbus-frontend
    python app.py
    10.2 Accesso al Dashboard
    Aprire un browser e andare a:

text
http://localhost:5001/dashboard
10.3 API REST Disponibili
Endpoint	Metodo	Descrizione
/api/robot/data	GET	Ottieni tutti i dati del robot
/api/robot/data/<nome></nome>	GET	Ottieni un dato specifico
/api/robot/data/<nome></nome>	POST	Imposta un valore (JSON)
/api/health	GET	Health check del server
/dashboard	GET	Dashboard web
10.4 Esempio di Richiesta API
bash

# Ottieni tutti i dati

curl http://localhost:5001/api/robot/data

# Ottieni la posizione X

curl http://localhost:5001/api/robot/data/robot_x

# Imposta la posizione X

curl -X POST http://localhost:5001/api/robot/data/robot_x 
    -H "Content-Type: application/json"
    -d '{"value": 3.14}'
11. Protezione del Codice con PyArmor
11.1 Installazione PyArmor
bash
pip install pyarmor==9.2.5
11.2 Ottenere il Seriale della Macchina
bash
python -c "from pyarmor.cli.hdinfo import main; import sys; main(['-v'])"
11.3 Offuscamento del Codice
bash

# Generare i file offuscati

pyarmor gen 
    --bind-device "IL_TUO_SERIALE"
    --output ./dist
    --recursive ./source

# Copiare i file generati

cp -r dist/source/*.py ./
cp -r dist/pyarmor_runtime_*/ ./
11.4 Script Automatico (protect_with_pyarmor.py)
Lo script protect_with_pyarmor.py automatizza l'intero processo:

bash
python protect_with_pyarmor.py
Il processo esegue:

Esecuzione di PyArmor sulla cartella source/

Spostamento dei file originali in source_da_cancellare/

Copia dei file offuscati nella root di modbus-gateway/

Copia del runtime PyArmor

11.5 Struttura Dopo l'Offuscamento
text
modbus-gateway/
├── main.py                    # File OFFUSCATO
├── modbus_client.py           # File OFFUSCATO
├── modbus_reader.py           # File OFFUSCATO
├── modbus_writer.py           # File OFFUSCATO
├── register_config.py         # File OFFUSCATO
├── logging_utils.py           # File OFFUSCATO
├── pyarmor_runtime_000000/    # Runtime PyArmor
├── source/                    # VUOTA
└── source_da_cancellare/      # Backup codice originale
12. Database MariaDB
12.1 Struttura della Tabella
sql
CREATE TABLE robot_data (
    id INT AUTO_INCREMENT PRIMARY KEY,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    robot_x FLOAT,
    robot_y FLOAT,
    robot_z FLOAT,
    robot_speed FLOAT,
    robot_temp FLOAT,
    robot_battery FLOAT,
    robot_status INT,
    robot_charge FLOAT
);
12.2 Query Utili
sql
-- Ultimi 10 valori
SELECT * FROM robot_data ORDER BY timestamp DESC LIMIT 10;

-- Media della temperatura nelle ultime 24 ore
SELECT AVG(robot_temp) FROM robot_data
WHERE timestamp > NOW() - INTERVAL 1 DAY;

-- Valore massimo della batteria oggi
SELECT MAX(robot_battery) FROM robot_data
WHERE DATE(timestamp) = CURDATE();
12.3 Backup del Database
bash
mysqldump -u modbus_user -p modbus_data > backup_$(date +%Y%m%d).sql
13. Test e Debug
13.1 Test del Server Modbus
Con mbpoll (tool da riga di comando):

bash

# Leggi 8 registri come float

mbpoll -a 1 -r 0 -t 4:float -c 8 localhost 5020

# Leggi lo stato

mbpoll -a 1 -r 12 -t 4:int -c 1 localhost 5020
Con Python (client):

python
from pymodbus.client import ModbusTcpClient
from struct import unpack

client = ModbusTcpClient('localhost', port=5020)
client.connect()

result = client.read_holding_registers(0, 8)
if not result.isError():
    for i in range(0, 8, 2):
        packed = result.registers[i].to_bytes(2, 'big') + 
    result.registers[i+1].to_bytes(2, 'big')
        value = unpack('>f', packed)[0]
        print(f"Registro {i}: {value:.2f}")

client.close()
13.2 Logging
I log vengono scritti in logs/modbus.log:

python
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('logs/modbus.log'),
        logging.StreamHandler()
    ]
)
13.3 Debug Avanzato
Attivare il logging DEBUG nel file config.ini:

ini
[LOGGING]
level = DEBUG
14. Risoluzione dei Problemi
14.1 Errore: "this license key is not for this machine"
Causa: La licenza PyArmor è legata a un seriale diverso.

Soluzione:

bash

# Ottenere il seriale corretto

python -c "from pyarmor.cli.hdinfo import main; import sys; main(['-v'])"

# Rigenerare la licenza

pyarmor gen --bind-device "NUOVO_SERIALE" --output ./dist --recursive ./source
14.2 Errore: "ModbusSlaveContext" non trovato
Causa: Versione di pymodbus non compatibile.

Soluzione:

bash
pip install pymodbus==3.13.1
14.3 Errore: Connessione al database fallita
Causa: Credenziali o host errati.

Soluzione:

Verificare config.ini

Controllare che MariaDB sia in esecuzione

bash

# Windows

net start MariaDB

# Linux

sudo systemctl start mariadb
14.4 Porta 5020 già in uso
Causa: Un altro server Modbus è in esecuzione.

Soluzione:

bash

# Trovare il processo che usa la porta 5020

# Windows

netstat -ano | findstr :5020

# Linux

sudo lsof -i :5020

# Terminare il processo (sostituisci PID con il PID trovato)

kill -9 PID
14.5 Errore di Importazione PyArmor
Causa: Versione PyArmor non compatibile.

Soluzione:

bash
pip uninstall pyarmor
pip install pyarmor==9.2.5
15. Sviluppo Futuro
15.1 Miglioramenti Pianificati
Supporto Modbus RTU (seriale) oltre a TCP

Dashboard avanzata con grafici in tempo reale (Chart.js)

Notifiche via email/telegram per allarmi

Export dati in formato CSV/Excel

API di autenticazione con JWT

Containerizzazione con Docker

15.2 Estensioni Possibili
Integrazione con MQTT per IoT

Supporto multi-robot (più slave)

Interfaccia mobile (PWA)

Machine Learning per analisi predittiva

15.3 Come Contribuire
Fork del repository

Crea un branch per la tua feature

Commit delle modifiche

Push sul branch

Apri una Pull Request

📄 Licenza
Questo progetto è distribuito sotto licenza MIT. Consultare il file LICENSE per maggiori dettagli.

📞 Contatti
Autore: Riccardo Brazzale

GitHub: riccardo-brazzale/MODBUS

Issues: Segnala un problema

Ultimo aggiornamento: Luglio 2026

✨ Buon lavoro con il tuo progetto MODBUS! ✨

text

---

## 💾 Come scaricare il file

1. **Copia tutto il contenuto** qui sopra (dalla riga `# 📚 Documentazione del Progetto MODBUS` fino all'ultima riga)
2. **Apri un editor di testo** (Notepad, VS Code, ecc.)
3. **Incolla il contenuto**
4. **Salva il file** con nome `DOCUMENTATION.md` nella cartella `/docs` del tuo progetto

Il file è ora pronto per essere visualizzato su GitHub, GitLab o qualsiasi altro sistema che supporti il Markdown! 🎉
Questa risposta è generate da IA. Controllarne l'accuratezza.
