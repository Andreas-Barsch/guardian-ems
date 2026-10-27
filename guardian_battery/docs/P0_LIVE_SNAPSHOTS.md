# P0: konsistente Diagnoseabbilder ohne tiefe Kopie im Collector

## Reale HA-Beobachtung vom 20.10.2026

Installiert: Battery 0.8.8 / 02d5e1e1ad9a9e9bc8644b70c25389448ecdb575.
Diagnoseinitialisierung ready_at 01:21:26 MESZ nach 6199,19 Sekunden;
spätere Statusantwort bestätigt adopted/complete, pending=0, kein Fehler.
Current-Abgleich: 66 Dateien, 2 gelesen, 64 übersprungen, 293 Zeilen.
Aggregate-Abgleich: 66 Dateien, 62 gelesen, 4 übersprungen, 430097 Zeilen,
3484 Samples ohne sichere Identität, keine ungültigen Zeilen/Dateifehler.
Insgesamt 365577272 gelesene Startup-Bytes. Das ist keine vollständige
fachliche Coverage und kein Nachweis eines vollständig inkrementellen Starts.

Status um 10:41 MESZ: Zyklus 1276 dauerte 57,56 s bei 70,02 s Startabstand.
Cell-Anteil 54,77 s, BAT gesamt 4,81 s, Raw-Schreiben 2,81 s;
Aggregate-Speicherabbild 20,92 s Wall / 5,71 s Thread-CPU;
Analyseabbild 17,21 s Wall / 2,52 s Thread-CPU, darin Aggregateprojektion
12,76 s Wall / 2,46 s CPU. Analyseworker zuletzt 116,36 s Wall / 34,63 s CPU.
Status um 10:46 MESZ: Zyklus1281=50,16 s, Startabstand59,99 s.
Beide Antworten: Rawqueue0, Drops0, keine Workerfehler. Keine Aussage über
lückenlose Messung: fehlende Raw-Drops beweisen keine eingehaltenen Polltermine.
Keine zusätzlichen Hardwarepolls und kein History-Pilot ausgeführt.

## Lokale Korrektur 0.8.9

Der allein schreibende Collector ersetzt bei add() nur den betroffenen
Cell-/Phasenrecord samt Histogramm. Bereits an Worker übergebene Records
werden nicht weiter verändert. Backfill-Merge übernimmt eigene Kopien;
Completion-Marker werden ersetzt und ihre Eingabesignaturen kopiert.
Speicherpayloads kopieren nur die äußeren Mappings, keine historischen
Histogramme. Pruning verändert bereits veröffentlichte Abbilder nicht.
Diese internen Abbilder sind nur für lesende Worker bestimmt.

Das Analyseabbild hält eine feste Auswahl derselben unveränderlichen Records
pro Identität. Erst der bestehende Analyseworker berechnet daraus mit derselben
Projektionsfunktion Median, Rang-/Extremanteile und Sortierung. Fachliche
Analyse, Identitätsselektion, Raw-Persistenz und Dateiformat bleiben gleich.
Die bestehenden Snapshot-Timingfelder aggregate_projection_* messen jetzt
nur das Erfassen der Recordauswahl; die Auswertung selbst steht im
Workerprofil aggregate_for_identity. Keine zusätzliche Instrumentierungs-API.

## Prüfungen und Grenzen

Gezielte Tests: eingefrorene Abbilder bei späterem Live-Append, Backfill,
Pruning und Markeränderung; unveränderte Werte nach Schreiben/Lesen;
Auswertung erst im Worker; Diagnoseparität und unveränderte Identitäten.
Exakte finale Testergebnisse und Gitkennung stehen im externen Bericht.

Synthetischer Mac-Vergleich, 11880 Records / 8608824 JSON-Bytes:
Speicherabbild 0,40053 -> 0,000129 s; Analyseabbild 0,11110 -> 0,01131 s.
Speicher-/Analysewerte im Vergleich mit 02d5e1e identisch (ausgenommen
Erstellzeit). Kein HA-Laufzeitnachweis, keine numerische Freigabegrenze.

Der Fix beseitigt gezielt zwei belegte Collector-Arbeiten. Vollständige
Serialisierung und fachliche Analyse kosten weiterhin CPU im Worker;
deren Einfluss unter HA-Last ist nicht durch die lokale Verbesserung bewiesen.
Nach separat freigegebener Installation sind mehrere normale PWR-/Cell-Zyklen,
Workerzustände, Rawqueue, CPU/RAM/I/O und Artefaktprovenienz zu prüfen.
History-Pilot, identische 24-h-Vorher-/Nachher-Abfrage, reale Segmentdarstellung
und separate produktive HA-Dashboarddateien bleiben offene Abnahmegates.
Kein Push, kein Deployment und keine Gesamt-P0-Freigabe durch diesen Kandidaten.
