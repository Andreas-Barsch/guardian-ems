# P0-Nachtrag: Live-Start vor historischen Diagnosen – Battery 0.8.8

Lokaler Kandidat, unveröffentlicht. Ausgangscode und tatsächlich installierter
Stand: Battery 0.8.7 / 7cec2be89402daab2c8d8dbec5b075786dba4fdb.
MCP 0.8.4, Tunnel 0.8.1, Engine 0.4.12 bleiben unverändert.

## Beobachteter Fehler am 19.10.2026

Der 0.8.7-Prozess startete um 20:48:05 UTC. Der Current-Condition-Rebuild meldete
um 20:51:05 UTC 65 gescannte Dateien und 425859 gültige Samples. Danach lief der
synchrone Aggregat-Rebuild weiter vor dem ersten regulären PWR-Zyklus. Noch um
21:04:06 UTC hatte die letzte Cell-Raw-Datei den Änderungszeitpunkt 20:46:25 UTC.
Der zuletzt neu abgeschlossene Aggregattag war zu diesem Zeitpunkt der 28.09.
Vorhandene spätere Datumsschlüssel waren keine Belege neuen Fortschritts.
Dieser Diagnose-Startpfad war gegenüber 0.8.6 unverändert; Supervisor `started`
und eine erreichbare API bewiesen keine laufende Erfassung.

## Änderung und unveränderte Fachlichkeit

Der Main-Thread startet mit privaten leeren Platzhaltern und beginnt die
bestehenden PWR-/RS485-/Cell-Zyklen. Diese Platzhalter werden weder analysiert
noch gespeichert. Der separate Diagnoseaufbau leiht ausschließlich freie
Zeitfenster der bestehenden Live-Koordination; bereits vor dem ersten Live-Zyklus
ist historische Arbeit gesperrt. Kein zusätzlicher Hardwarepoll.

Der Worker verwendet dieselben Diagnose-Stores, Rohsample-/Identitätsauflösung,
Phasenklassifikation und Aggregate-Reconciliation. Ein temporärer SQLite-Abgleich
bewahrt die alte Slot-Autorität, Deduplizierung, chronologische Reihenfolge und
Retention je Identität, ohne alle historischen Samples gleichzeitig im RAM zu
halten. SQL-Fortschritt, Quellzeilen und JSON-Decodierung prüfen kooperativ
Live-Vorrang/Abbruch. Keine langen gemeinsamen Sperren mit der Erfassung.

Der Collector hält nach bereits erfolgter Raw-Persistenz eine begrenzte Queue
für die spätere Store-Übernahme. Überschneidungen mit bereits eingelesenen
Rohzeilen werden dedupliziert; ältere Kandidaten/Detektorregeln sind unberührt.
Erst nach vollständigem Aufbau und Replay kann Main die beiden Stores gemeinsam
übernehmen. Erst danach laufen normale Diagnoseanalyse und bestehende Persistenz.
Währenddessen bleiben die vorherigen Diagnose-Dateien unverändert. Alte
abgeleitete Ausgaben werden nicht als neue Berechnung ausgegeben.

`diagnostic_startup` im bestehenden RS485-Status meldet technischen Fortschritt,
Queue, gelesene Bytes, Reports und Fehler. `complete` bezeichnet nur die
Initialisierung, keine vollständige fachliche Quell-Coverage oder P0-Abnahme.
Keine neuen fachlichen Diagnosezustände, Schwellen, Phasen oder SOC-Regeln.

## Arbeitsgrenzen und Fehler

Strukturelle Schutzgrenzen, keine aus einem lokalen Benchmark abgeleiteten
HA-Freigabebudgets: je Cache 64 MiB, JSON-Token/Quellzeile 64 KiB,
8 Millionen JSON-Knoten und Tiefe 32; Quellinventur 512 Einträge, je Quelle
16 MiB; Identitätskontext 64 KiB; SQLite 512 MiB mit 1 MiB Page-Cache,
höchstens 64 Identitäten und 100000 materialisierte Retention-Samples
mit zusammen höchstens 64 MiB kodierten Sample-Daten;
Live-Replay-Queue 4096 Samples, Bearbeitung in höchstens sechs Samples je Block.
Die konfigurierbare fachliche Retention wird nicht reduziert. Bei Überschreitung
bleibt der Aufbau ausdrücklich unvollständig, statt still zu kürzen.

Die einzige wegwerfbare SQLite-Arbeitsdatei liegt im markierten, exklusiv
gesperrten Unterverzeichnis `.diagnostic-startup` neben den Diagnose-Caches.
Ein Neustart ersetzt ausschließlich diesen als eigenen Scratch ausgewiesenen
Slot; nie Raw-, Ereignis- oder Diagnose-Originaldateien. Ein unbekannter Besitzer,
Symlink oder parallel belegter Slot sperrt den Aufbau. Abbruch/Fehler publiziert
keinen Teilzustand; der nächste reguläre Start kann aus Original-Caches/Raw
rekonstruieren. Keine Konfigurationsänderung oder automatische Service-Neustarts.
Eine während des Aufbaus geänderte Positionshistorie verhindert eine Übernahme
mit veralteter Identitätszuordnung. Technischer Fehlerstatus ist sichtbar;
aktuelle Raw-Persistenz läuft weiter.

## Prüfung und Integration

Gezielte synthetische Tests prüfen den ersten PWR vor historischen Zugriffen,
JSON-Parität, Diagnose-/Aggregate-Parität einschließlich Retention, Live-Pause
und gleichzeitigen Raw-Append, Replay ohne Duplikate, Budgetfehler, Abbruch,
Neustart und unveränderte Originaldateien. Endgültige Suite-/Imageergebnisse und
Commitkennung werden im externen Kandidatenbericht festgehalten.

HA-Test dieser konkreten Änderung steht aus. Der vorhandene P0-Pilot bleibt
auf sein freigegebenes Fenster beschränkt; offene Tage und fehlende Checkpoints
bleiben gesperrt. Kein Ersatz durch alte Rebuild-Endpunkte. Keine Gesamt-P0-
Freigabe aus Unit-Tests, Image-Smoke oder Supervisor-Status ableiten.

## Historische lokale Prüfungen des Startpfad-Vorläufers 202dece

Mit dokumentiertem Produktionspfad-/Netzwerkschutz: 1274 Battery-Tests bestanden
(87,58 s), 63 MCP-/Tunnel-Tests bestanden (13,91 s). Der erste vollständige
Sandbox-Lauf scheiterte ausschließlich an vier gesperrten Loopback-Binds; dieser
Fehllauf bleibt separat protokolliert. Keine produktiven Zugriffe durch Tests.
Python-3.12-Syntaxprüfung und Diff-Prüfung bestanden.

Synthetischer Mac-Versuch (Python 3.13, keine HA-Messung): 90 Dateien,
270000 Samples, acht historische Identitäten, 69120 Retention-Samples.
Kaltstart 44,82 s / maximal beobachteter Prozess-RSS 262881280 Byte;
Warmstart 12,34 s / kumulativer RSS-Höchststand 300318720 Byte.
Kalt wurden beide Diagnoseansichten vollständig rekonstruiert; warm wurden
alle 90 Rohdateien je Ansicht anhand ihrer Marker übersprungen (null Raw-Decodes).
Die Aggregate verwenden in diesem großen Größenversuch Ruhe-Samples; fachliche
Phasenparität wird separat mit den Regressionstests geprüft. Rohdatei-Prüfsummen
blieben identisch. Diese Zahlen sind weder Ziel-Hardware-Budget noch
Betriebsabnahme; Image-/HA-Ergebnisse stehen separat im externen Bericht.

## Ergänzung: begrenzte Stufenzeiten ohne Debugumschaltung

Der normale lesende `/api/rs485/status` gibt nun die bereits gesammelten
skalaren Zeiten/CPU-Zeiten und Ausführungszustände für PWR, BAT gesamt,
Identität/Raw-Schreiben, Storeupdates, Persistenz-Payloadbau, Analyse-Snapshot,
Adoption, MQTT und Restzeit aus. Feste Feldliste, keine Rohsamples,
Seriennummernlisten oder per-Modul-Profile in der neuen Projektion. Laufender,
letzter abgeschlossener Hauptzyklus und letzter Cell-Zyklus bleiben getrennt
mit ihren IDs/Zeitpunkten. Nicht gemessen bleibt fehlend/null; `not_executed`
wird nicht als gemessene Null ausgegeben. Keine neuen Polls, Reads oder Logs.
Vorhandene Workerzeiten bleiben getrennt und dürfen nicht addiert werden.
`bat_request` ist die letzte Einzelmessung; `bat_requests_total` die Summe.
`cell_cycle` enthält auch Store-/Payloadarbeit; seine Unterstufen nicht nochmals
zur Summe addieren. Normale laufende Cell-Verarbeitung bleibt unverändert.

Damit kann der nächste freigegebene Build die bislang herausgefilterte
Ursachentrennung liefern. Dies ist noch kein HA-Messbeleg oder Timingfix.
Vorläufer 202decedf897b321baa6fc92e59e972999eae69c bleibt historisch gesichert;
maßgeblich wird der separat geprüfte Folgecommit samt neuem vollständigem Image.

Prüfung der ergänzten Statusprojektion: 99 direkt betroffene Research-API-,
Collector-Timing-, Config-UI- und Modul-UI-Tests bestanden (3,40 s), mit
Produktionspfad-/Netzwerkschutz und lokalen Loopback-Testservern. Tatsächlicher
GET-Handler, getrennte Zyklus-IDs, Nichtausführung, fehlende Messwerte und
von großen ausgeschlossenen Profilen unabhängige Ausgabe geprüft. Syntax
(Python3.12) und Diffprüfung bestanden. Die oben genannten1274/63 Tests
gehören zum Vorläufer, nicht zu einer erneut ausgeführten Gesamtsuite.
