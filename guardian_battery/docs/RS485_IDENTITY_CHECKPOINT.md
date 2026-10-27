# Persistente RS485-Identität — Battery 0.9.11 (vorbereitet)

## Vorhandene Wege und gezielte Änderung

Legacy `main.py` benutzte `restore_latest_identities`: jüngste Dateien zuerst,
maximal 100.000 Rohzeilen. Es gab schon Rohpersistenz, aber keinen kleinen
unabhängigen aktuellen Identitätscheckpoint. `prepared_rs485_identity.py`
hat dagegen bereits einen bestätigten ADR-Index (`rs485_restore`). Dieser
vorbereitete Modus bleibt bei seinem Index und benutzt die neue Datei nicht.
Ein späterer Moduswechsel muss Identitätsherkunft gesondert prüfen.

Legacy nutzt `/share/guardian_battery/rs485_identities.json`, Schema 1, maximal
256 Einträge / 1 MiB. Kein Erzeugen oder Rohscan beim leeren Start. Beschädigte
Datei: keine Teilzuordnung, Fehler in `identity_store_error`, Schreibschutz bis
zur ausdrücklich veranlassten Wiederherstellung. `.json.previous` ist die letzte
validierte Vorgängerkopie, kein automatisch als aktuell ausgegebener Fallback.
Schreiben: temporäre Datei, fsync, atomarer Ersatz, Verzeichnis-fsync.

Der passive Reader ist die gemeinsame Identitätsquelle für aktuelle Anzeige
und den laufenden Evidenzschreiber. Nur erfolgreich korrelierte und dekodierte
0x93-Antworten ändern sie. Der bestehende Decoder bleibt unverändert. Rohframes,
SHA-256-Referenz, Originalbeobachtungszeit und Provenienz werden gespeichert.
Beim Laden ist `identity_currently_confirmed=false`, auch wenn die gespeicherte
Beobachtung ursprünglich live erfasst wurde. 0x92 ändert nur Managementzeit.
Die Anzeige zeigt die Identitätszeit separat. Eine aktuelle Managementmeldung
belegt nicht, dass die historische Buszuordnung heute weiterhin gültig ist.

Neuere Beobachtung derselben Seriennummer an anderer Adresse entfernt die alte
Adresszuordnung. Neue Seriennummer an einer Adresse ersetzt deren alte Zuordnung.
Vorherige Adresse/Seriennummer wird im aktuellen Eintrag sichtbar gehalten;
die vollständige Abfolge bleibt Roh-Evidence. Ältere Beobachtungen überschreiben
keine neueren beteiligten Zuordnungen. Zeitgleiche widersprüchliche Beobachtungen
werden nicht nach Modulnummer entschieden: betroffene Zuordnungen fallen weg,
bis eine weitere eindeutige Beobachtung vorliegt. Doppelte Seriennummern im
Checkpoint machen den gesamten Checkpoint ungültig.

Wiederholte identische Identitätsframes aktualisieren RAM sofort. Persistenz
höchstens alle 300 Sekunden, Zuordnungsänderungen sofort. Nach hartem Abbruch
kann der gespeicherte letzte Bestätigungszeitpunkt entsprechend älter sein;
dies wird nicht durch eine erfundene Startzeit ersetzt. Schreibfehler lassen
Live-Erfassung weiterlaufen und erscheinen im Status; dann ist der persistierte
Stand ausdrücklich nicht als aktuell gesichert anzusehen.

## Einmaliger Anfangsbestand und Installation (noch nicht ausgeführt)

Sechs geprüfte letzte Antworten vom 22.10.2026, 12:59:01–12:59:17 UTC,
Adressen 2–7, sind lokal als separater historischer Seed vorbereitet. Ursprung:
`/share/guardian_battery/rs485_history/2026-10-22.jsonl`, Zeilen
1065/1067/1069/1071/1073/1075. SHA-256 der Quelldatei:
`1d388c282879a9b68f670bc7ce92b57be7110c1c0a0bbbcff9ebf53ba9f2662d`.
Die Originalframes werden nochmals durch Parser/Checksumme/Decoder geprüft.
Keine neuen Daten mit heutigem Datum und keine hartcodierte Stackposition.
Produktionsdaten/Seed bleiben außerhalb von Git und des Software-Quellpakets.

`rs485_identity_seed.seed_from_file` kann ausdrücklich aus genau einer genannten
Datei den Anfangsbestand erzeugen: maximal 32 MiB / 40.000 Zeilen, 64 KiB pro Zeile,
optional exakt erwartete Adressen. Bei Grenzüberschreitung oder inkonsistenter
Evidence abbrechen, niemals einen Teilstand installieren. Kein Start-Hook.

Vor einer später ausdrücklich freigegebenen Installation:
1. Laufenden Software-/Image-Stand und Betriebsmodus prüfen. Vorhandenen
   Checkpoint (falls inzwischen entstanden) und Optionen sichern; nicht ersetzen.
2. Bei gestoppter Battery Herkunft und alle sechs Seed-Einträge prüfen, gegen
   inzwischen neuere Identitätsbeobachtungen/Modulwechsel abgleichen. Nur wenn
   weiterhin passend und Ziel nicht vorhanden, atomar den Seed installieren.
3. 0.9.11 starten; genau einmal historisch markierte sechs Zuordnungen,
   Beobachtungszeit und zeitabhängige Stackposition prüfen. Kein RS485-Poll.
4. Spätere echte 0x93-Beobachtung bestätigt Live-Status; ein Neustart allein nicht.

Rückkehrziel: Battery 0.9.10, Basiscommit
`a453cd6c023b5e7ded3ddf1b9c1705f7b0bb3fa5`. Quellpaket liegt lokal bereits vor;
Verfügbarkeit eines direkt rückinstallierbaren Images vor Installation prüfen.
0.9.10 ignoriert den neuen Checkpoint und hat wieder die begrenzte Rohsuche.
Checkpoint und `.previous` für spätere Verwendung erhalten; keine History,
Maintenance-, Position-, Recorder- oder SSD-Daten zurücksetzen. Eine praktische
Rückkehr wurde mit dieser Änderung nicht ausgeführt oder belegt.
