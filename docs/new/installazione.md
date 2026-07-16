**Sulla macchina vergine**:

1. aggiornare il sistema: sudo apt update && sudo apt upgrade -y
2. installare git: apt install git
3. spostarsi nell directory /opt: cd /opt
4. clonare in lcale il progetto: git clone https://githib.com/riccardo-brazzale/modbus
5. Spostarsi nella cartella modbus: cd modbus
6. Impostare il flag di permesso per l'esecuzione di install.sh: chmod+x install sh
7. Eseguire l'installazione e la configurazione del sisytema: ./install.sh
8. Impostare l'indirizzo del server sul file /opt/modbus/modbus-gateway/config.ini : nano /opt/modbus/modbus-gateway/config.ini
9. Riavviare la macchina: reboot
10. Verificare i servizi: systemctl status modbus_forontend.service e systemctl status modbus_frontend.service che devono essere attivi
11. Collegarsi con un browser all'ip della macchina porta 8000 per le verifiche del caso.
12. Eliminare la cartella /opt/modbus/modbus-gateway/source_da_cancellare
