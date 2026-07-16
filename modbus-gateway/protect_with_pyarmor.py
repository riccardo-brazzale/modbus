#!/usr/bin/env python3
"""
Script per offuscare e proteggere il codice del modulo modbus-gateway con PyArmor.
La protezione è basata sul MACHINE ID restituito da PyArmor (pyarmor.cli.hdinfo),
non più sul seriale hard disk grezzo: il Machine ID è calcolato da PyArmor stesso
a partire dall'hardware della macchina ed è il valore garantito coerente tra
generazione del binding e verifica a runtime.

Il processo:
1. Esecuzione di PyArmor sulla cartella source/ per generare i file offuscati
2. Spostamento dei file originali da source/ a source_da_cancellare/ (svuotando source/)
3. Copia dei file offuscati da dist/source/ a modbus-gateway/ (root del progetto)
4. Copia del runtime di PyArmor in modbus-gateway/
5. Pulizia della cartella dist/ e source/ (se necessario)
"""

import os
import re
import shutil
import subprocess
import sys
import platform
from pathlib import Path

# --- Configurazione ---
PROJECT_ROOT = Path(__file__).parent  # Cartella modbus-gateway
SOURCE_DIR = PROJECT_ROOT / "source"
BACKUP_DIR = PROJECT_ROOT / "source_da_cancellare"
DIST_DIR = PROJECT_ROOT / "dist"  # Cartella temporanea per PyArmor

# --- 1. Funzioni di utilità ---
def print_header(message):
    print("\n" + "=" * 60)
    print(f" {message}")
    print("=" * 60)

def print_step(message):
    print(f"\n➡️  {message}")

def check_pyarmor():
    try:
        result = subprocess.run(["pyarmor", "--version"], capture_output=True, text=True, check=True)
        print(f"✅ PyArmor trovato: {result.stdout.strip()}")
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("❌ PyArmor non trovato. Installalo con: pip install pyarmor")
        return False

def get_machine_id():
    """
    Recupera il Machine ID calcolato da PyArmor tramite il modulo
    pyarmor.cli.hdinfo (disponibile dalla 8.4.6 in poi, quindi anche in 9.2.5).

    Esegue: python -m pyarmor.cli.hdinfo
    e cerca nell'output una riga tipo:
        Machine ID: 'mc92c9f22c732b482fb485aad31d789f1'
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyarmor.cli.hdinfo"],
            capture_output=True, text=True, check=True
        )
        output = result.stdout + result.stderr
    except Exception as e:
        print(f"❌ Errore nell'esecuzione di 'python -m pyarmor.cli.hdinfo': {e}")
        return None

    match = re.search(r"Machine ID:\s*'([^']+)'", output)
    if not match:
        print("❌ Machine ID non trovato nell'output di pyarmor.cli.hdinfo.")
        print("--- Output ricevuto ---")
        print(output)
        print("------------------------")
        return None

    machine_id = match.group(1)
    return machine_id

def setup_directories():
    print_step("Preparazione delle directory...")
    BACKUP_DIR.mkdir(exist_ok=True)
    print(f"📁 Cartella backup: {BACKUP_DIR}")
    DIST_DIR.mkdir(exist_ok=True)
    print(f"📁 Cartella dist: {DIST_DIR}")

def run_pyarmor_obfuscation():
    """Esegue PyArmor sulla cartella source (che contiene i file originali)."""
    print_step("Esecuzione di PyArmor sulla cartella source...")

    if not SOURCE_DIR.exists():
        print(f"❌ Cartella source non trovata: {SOURCE_DIR}")
        return False

    # Conta i file .py prima di eseguire PyArmor
    py_files = list(SOURCE_DIR.rglob("*.py"))
    print(f"📄 Trovati {len(py_files)} file .py in source/")

    if not py_files:
        print("⚠️  Nessun file .py trovato in source/. Verifica il contenuto.")
        return False

    print_step("Recupero Machine ID tramite pyarmor.cli.hdinfo...")
    machine_id = get_machine_id()
    if not machine_id:
        print("❌ Impossibile recuperare il Machine ID. Interruzione.")
        return False
    print(f"🔑 Machine ID della macchina: {machine_id}")

    cmd = [
        "pyarmor", "gen",
        "--bind-device", machine_id,
        "--output", str(DIST_DIR),
        "--recursive",
        str(SOURCE_DIR)
    ]

    print(f"\n📦 Comando: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print("❌ Errore durante l'esecuzione di PyArmor:")
            print(result.stderr)
            return False
        print("✅ PyArmor completato con successo.")
        if result.stdout:
            print(result.stdout)
    except Exception as e:
        print(f"❌ Errore nell'esecuzione di PyArmor: {e}")
        return False

    # --- Genera anche la outer runtime key con LO STESSO Machine ID ---
    # Fondamentale: se il binding qui non combacia esattamente con quello
    # usato sopra, a runtime si ottiene "RuntimeError: this license key
    # is not for this machine".
    print_step("Generazione outer runtime key bindata allo stesso Machine ID...")
    key_cmd = [
        "pyarmor", "gen", "key",
        "--bind-device", machine_id,
        "--output", str(DIST_DIR),
    ]
    print(f"\n📦 Comando: {' '.join(key_cmd)}")
    try:
        key_result = subprocess.run(key_cmd, capture_output=True, text=True)
        if key_result.returncode != 0:
            print("❌ Errore durante la generazione della runtime key:")
            print(key_result.stderr)
            return False
        print("✅ Runtime key generata con successo.")
        if key_result.stdout:
            print(key_result.stdout)
    except Exception as e:
        print(f"❌ Errore nella generazione della runtime key: {e}")
        return False

    return True

def move_original_to_backup():
    """
    Sposta i file originali da source/ a source_da_cancellare/.
    Questa operazione SVUOTA la cartella source/.
    """
    print_step("Spostamento dei file originali in source_da_cancellare/...")

    if not SOURCE_DIR.exists():
        print(f"❌ Cartella source non trovata: {SOURCE_DIR}")
        return False

    moved_count = 0

    # Sposta TUTTI i file (non solo .py) per sicurezza
    for item in SOURCE_DIR.rglob("*"):
        if item.is_file():
            rel_path = item.relative_to(SOURCE_DIR)
            dest_path = BACKUP_DIR / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(dest_path))
            moved_count += 1
            print(f"   📄 Spostato: {rel_path}")

    # Rimuovi eventuali cartelle vuote in source/
    for item in sorted(SOURCE_DIR.rglob("*"), reverse=True):
        if item.is_dir() and not any(item.iterdir()):
            item.rmdir()
            print(f"   🗑️  Rimossa cartella vuota: {item.relative_to(SOURCE_DIR)}")

    print(f"✅ Spostati {moved_count} file in {BACKUP_DIR}")
    print(f"📁 Cartella source ora è VUOTA")
    return True

def deploy_obfuscated_files():
    """
    Copia i file offuscati da dist/source/ DIRETTAMENTE in modbus-gateway/.
    """
    print_step("Distribuzione dei file offuscati in modbus-gateway/...")

    if not DIST_DIR.exists():
        print(f"❌ Cartella dist non trovata: {DIST_DIR}")
        return False

    # PyArmor genera i file in dist/source/ (con la stessa struttura di source/)
    dist_source = DIST_DIR / "source"
    if not dist_source.exists():
        print(f"❌ Cartella dist/source/ non trovata. Verifica l'esecuzione di PyArmor.")
        # Mostra il contenuto di dist/ per debug
        print(f"📁 Contenuto di {DIST_DIR}:")
        for item in DIST_DIR.rglob("*"):
            print(f"   {item.relative_to(DIST_DIR)}")
        return False

    # Copia i file .py offuscati da dist/source/ a modbus-gateway/ (root)
    copied_count = 0
    for item in dist_source.rglob("*"):
        if item.is_file() and item.suffix == ".py":
            # Calcola il percorso relativo da dist/source/
            rel_path = item.relative_to(dist_source)
            # La destinazione è direttamente in PROJECT_ROOT (modbus-gateway/)
            dest_path = PROJECT_ROOT / rel_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(item), str(dest_path))
            copied_count += 1
            print(f"   📄 Copiato offuscato: {rel_path} -> {dest_path}")

    print(f"✅ Copiati {copied_count} file .py offuscati in {PROJECT_ROOT}")

    # --- COPIA IL RUNTIME DI PYARMOR ---
    runtime_copied = False
    for runtime_dir in DIST_DIR.glob("pyarmor_runtime_*"):
        dest_runtime = PROJECT_ROOT / runtime_dir.name
        if dest_runtime.exists():
            shutil.rmtree(dest_runtime)
            print(f"   🗑️  Rimosso runtime esistente: {runtime_dir.name}")
        shutil.copytree(runtime_dir, dest_runtime)
        runtime_copied = True
        print(f"   📁 Copiato runtime: {runtime_dir.name} in {PROJECT_ROOT}")

    if not runtime_copied:
        print("⚠️  Runtime PyArmor non trovato in dist/. Verifica l'esecuzione di PyArmor.")
        return False

    # Copia il file della outer runtime key (pyarmor.rkey), se generato
    for rkey_file in DIST_DIR.glob("*.rkey"):
        shutil.copy2(rkey_file, PROJECT_ROOT / rkey_file.name)
        print(f"   📄 Copiata runtime key: {rkey_file.name}")

    # Copia eventuali file .lic (licenza, versioni/modalità precedenti)
    for lic_file in DIST_DIR.glob("*.lic"):
        shutil.copy2(lic_file, PROJECT_ROOT / lic_file.name)
        print(f"   📄 Copiato file licenza: {lic_file.name}")

    return True

def cleanup():
    """Pulisce le cartelle temporanee."""
    print_step("Pulizia dei file temporanei...")

    # Rimuovi dist/
    if DIST_DIR.exists():
        shutil.rmtree(DIST_DIR)
        print(f"🗑️  Rimossa cartella dist")

    # Verifica che source/ sia vuota
    if SOURCE_DIR.exists():
        remaining = list(SOURCE_DIR.rglob("*"))
        if remaining:
            print(f"⚠️  Cartella source non è vuota. Contiene {len(remaining)} elementi.")
            for item in remaining:
                print(f"   {item.relative_to(SOURCE_DIR)}")
        else:
            print(f"📁 Cartella source è vuota (come previsto)")

    print("✅ Pulizia completata.")

# --- Main ---
def main():
    print_header("🛡️  Protezione del codice con PyArmor (binding su Machine ID)")

    if not check_pyarmor():
        sys.exit(1)

    if not SOURCE_DIR.exists():
        print(f"❌ Cartella source non trovata in: {SOURCE_DIR}")
        print("   Assicurati di essere nella cartella modbus-gateway/")
        sys.exit(1)

    try:
        # 1. Prepara le directory
        setup_directories()

        # 2. ESEGUI PYARMOR (sui file originali in source/, bind su Machine ID)
        if not run_pyarmor_obfuscation():
            print("❌ Offuscamento fallito. Interruzione.")
            sys.exit(1)

        # 3. SPOSTA i file originali da source/ a source_da_cancellare/ (svuota source/)
        if not move_original_to_backup():
            print("❌ Backup fallito. Interruzione.")
            sys.exit(1)

        # 4. COPIA i file offuscati in modbus-gateway/ (root)
        if not deploy_obfuscated_files():
            print("❌ Distribuzione fallita. Interruzione.")
            sys.exit(1)

        # 5. Pulizia
        cleanup()

        print_header("✅ Processo di offuscamento completato con successo!")
        print("\n📌 RIEPILOGO:")
        print(f"   • File originali → {BACKUP_DIR}/")
        print(f"   • File offuscati → {PROJECT_ROOT}/ (root di modbus-gateway)")
        print(f"   • Runtime PyArmor → {PROJECT_ROOT}/pyarmor_runtime_*/")
        print(f"   • Cartella source → VUOTA")
        print("\n🔑 La licenza è legata al Machine ID calcolato da PyArmor (pyarmor.cli.hdinfo).")
        print("   Per eseguire il gateway, assicurati che il runtime e la .rkey siano presenti.")
        print("\n⚠️  Dopo aver verificato il funzionamento, puoi cancellare:")
        print(f"   {BACKUP_DIR}")

    except Exception as e:
        print(f"\n❌ Errore durante il processo: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()