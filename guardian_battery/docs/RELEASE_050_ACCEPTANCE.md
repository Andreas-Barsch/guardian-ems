# Guardian Battery 0.5.0 – Deployment- und Abnahmeplan

## Geltungsbereich

Dieser Plan gilt für den Release von Guardian/Add-on `0.5.0` bei unveränderter
Diagnostic Engine `0.4.12`. Er autorisiert kein Deployment. Vor dem Update
sind HA-Backup und separates Guardian-Datenbackup gemäß
`ENVIRONMENT_RUNBOOK.md` zu erstellen und zu verifizieren.

## Deploymentfolge nach ausdrücklicher Freigabe

1. Sauberen Release-Commit und vollständige Tests bestätigen.
2. Release-Branch zu GitHub pushen, `main` kontrolliert per Fast-forward
   übernehmen und den veröffentlichten Commit dokumentieren.
3. Auf Home Assistant den App-Store manuell über **Nach Updates suchen/Neu
   laden** aktualisieren.
4. Vor der Installation bestätigen, dass `0.5.0` als neue Version erkannt
   wird. Die historisch erfolglosen CLI-Reloads sind kein Ersatz dafür.
5. Update der bestehenden App `3195b09a_guardian_battery` installieren und
   anschließend neu starten.
6. Die folgenden Prüfungen durchführen; bei einem kritischen Fehler keine
   weiteren Maintenance-Schreibtests beginnen und den Rollbackplan anwenden.

## A. Start und Regression

- Add-on startet ohne Fehler.
- Runtime meldet Guardian `0.5.0` und Diagnostic Engine `0.4.12`.
- Pylontech-Kommunikation, Polling und erkannte Module bleiben unverändert.
- Bestehende MQTT-Entities, Availability und Gerätezuordnung bleiben intakt.
- Vorhandene Runtime-Dateien und Messhistorien sind unverändert vorhanden.

## B. Maintenance-Persistenz

- Über die Ingress-UI den ersten echten Abnahmeeintrag anlegen.
- Entstehung von `/share/guardian_battery/maintenance_events.jsonl` und
  `.lock` prüfen.
- Owner, Group, Mode, Größe und Zeitstempel dokumentieren.
- Event-ID, Revision 1, `occurred_at` und `created_at` dokumentieren.
- Bestätigen, dass keine andere Runtime- oder Recorder-Datei manipuliert wird.

## C. Logbuch und Konfliktmodell

- Create, Liste, Detail und Deep-Link prüfen.
- Event mit korrekter `expected_revision` bearbeiten und Revision 2 prüfen.
- Veraltete Revision absichtlich verwenden und Konflikt ohne Schreibvorgang
  bestätigen.
- Archivieren und Wiederherstellen; jede Operation muss dieselbe ID und die
  jeweils nächste Revision verwenden.
- Vollständige append-only Revisionshistorie prüfen; kein DELETE.

## D. Rückdatierung und Timeline

- Einen eindeutig rückdatierten Eintrag erzeugen.
- Trennung von `occurred_at` und `created_at` prüfen.
- Marker ausschließlich an `occurred_at` positionieren.
- System-, Modul- und Zellzuordnung sowie zentralen Deep-Link prüfen.

## E. History-Overlays

Für denselben Zeitraum SOC, Strom, Zellspannung und Zelltemperatur öffnen.
Der Maintenance-Marker muss in jeder gemeinsamen Overlay-Komponente am
gleichen `occurred_at` erscheinen. Messreihen und Events bleiben getrennt;
Home-Assistant-Recorder und Standard-History-Karten bleiben unverändert.

## F. MQTT Live Event

- Einen neuen manuellen Eintrag innerhalb des 300-Sekunden-Fensters anlegen.
- Genau eine Nachricht auf `guardian/battery/event/maintenance` prüfen.
- Home-Assistant-Event-Entity und Zuordnung zum Guardian-Gerät prüfen.
- `maintenance_event_id`, `occurred_at`, `created_at`, `category`, `title`,
  betroffenes System, optional Modul/Zelle und Deep-Link validieren.
- Mit einem erst danach verbundenen Subscriber beziehungsweise Broker-Werkzeug
  bestätigen, dass die Eventnachricht nicht retained wurde.

## G. Non-Publish

Es darf keine neue MQTT-Maintenance-Nachricht entstehen bei:

- Rückdatierung um mehr als 300 Sekunden,
- Bearbeitung,
- Archivierung,
- Wiederherstellung,
- Guardian-Neustart.

## H. Guardian-Neustart

- Vorher IDs, Revisionen, Archivstatus, Dateigröße und Prüfsumme erfassen.
- Guardian neu starten.
- Einträge, IDs, Revisionen, Sortierung und UI erneut prüfen.
- Bestätigen, dass kein bestehendes Maintenance-Event erneut publiziert wird.

## I. Home-Assistant-Neustart

- Home Assistant kontrolliert neu starten.
- Persistenz, Ingress-UI, Deep-Links, MQTT Discovery, Event Entity und
  Gerätezuordnung erneut prüfen.
- Erneut bestätigen, dass kein Replay alter Maintenance-Events stattfindet.

## Rollback

Bei einem Releasefehler den Code/das Add-on auf die real verifizierte
0.4.12-Basis `f37b6df` zurückführen. Vorhandene Dateien unter
`/share/guardian_battery` – insbesondere `maintenance_events.jsonl` und deren
Lock-Datei – weder löschen noch verändern. Nach Rollback Start, Version,
Pylontech-Kommunikation, bestehende MQTT-Entities und Runtime-Daten prüfen.

## Nächstes Diagnose-Arbeitspaket: Phase Overlay

Die spätere gemeinsame deterministische Phase Engine muss drei getrennte
Projektionsmodi anbieten:

1. **Historisch:** Messdaten bei `t` plus der letzte bei `t` gültige Datensatz
   aus `config_history.jsonl`; dies ist die historische Referenz.
2. **Aktuelle Parameter:** historische Messdaten plus heute wirksame
   Konfiguration, ausschließlich als alternative Analyseprojektion.
3. **What-if:** historische Messdaten plus explizit gewählte hypothetische
   Phasengrenzen, ausschließlich als temporäre Analyseprojektion.

Kein Modus verändert `cell_history`, `config_history`, Maintenance-Daten oder
historische Diagnosen. UI und eine spätere KI verwenden dieselbe Guardian
Phase Engine und implementieren keine eigene Phasenklassifikation. Die
Architekturfolge lautet: deterministische Guardian-Berechnung, Explainable UI,
danach optionale KI-Interpretation und Hypothesenbildung.
