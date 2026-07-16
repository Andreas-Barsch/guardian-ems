# Guardian EMS Apps

Home-Assistant-App-Repository für das Guardian EMS.

## Enthaltene Apps

### Guardian Battery

Direkte Überwachung eines Pylontech-US2000C-Stacks über den Console-Port:

- Spannung und Strom je Modul
- SOC und Betriebszustand
- Temperaturen
- minimale und maximale Zellspannung
- Zellspreizung
- Gesundheitsstatus je Modul
- MQTT Discovery
- Home-Assistant-Dashboard-Unterstützung

## Installation in Home Assistant

Nach dem Hochladen dieses Repositorys zu GitHub:

1. Home Assistant öffnen.
2. Einstellungen → Apps → App-Store.
3. Drei-Punkte-Menü → Repositories.
4. GitHub-URL dieses Repositorys eintragen.
5. Guardian Battery installieren.

## Sicherheit

Guardian Battery sendet ausschließlich den lesenden Pylontech-Console-Befehl `pwr`.
Es werden keine Reset-, Firmware-, MOS- oder Konfigurationsbefehle verwendet.
