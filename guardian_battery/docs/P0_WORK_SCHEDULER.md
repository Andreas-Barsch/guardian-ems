# Lokale Worker-Komposition – Phase 3b1

Dieser Baustein ist ausschließlich explizit lokal auswählbar. `main.py`,
Versionen, öffentliche Routen und die produktive Worker-Auswahl bleiben unverändert.
Keine HA-Abnahme, Veröffentlichung oder Deploymentfreigabe.

## Ausführung und Fortschritt

`WorkScheduler.execute` verwendet den aufrufenden vorhandenen Worker-/Request-Thread;
er erzeugt keine Threads, Prozesse, Timer oder Hintergrund-Retries. Höchstens 16
Aufträge einschließlich laufender Arbeit werden standardmäßig aufgenommen.
READ und DERIVE haben je eine FIFO-Round-Robin-Warteschlange. Beide Klassen können
nebeneinander arbeiten. Innerhalb einer Klasse wird nach 32 kooperativen Checks
an den nächsten wartenden Auftrag abgegeben. Diese einstellbaren strukturellen
Testgrenzen sind keine gemessenen HA-Latenzbudgets oder zugesicherte Zeitscheiben.

Pausieren behält den Python-Aufrufstack. Insbesondere wird die exakte finale
Modulreduktion nicht nach jedem Quantum von vorne begonnen. Rechenformeln und
Ergebnisse bleiben unverändert. Die vorhandenen dauerhaften Repository-Cursor,
Beiträge und Publikationen bleiben die Wiederanlaufautorität. Nach Prozessende
existiert kein pausierter Stack; eine explizite Wiederaufnahme anhand der
vorhandenen Jobreferenz verwendet bestätigte Schritte. HTTP-Aufträge werden bei
Neustart nicht automatisch erneut ausgeführt. Schedulerwarteschlange und letzte
64 Statusdatensätze sind bewusst prozesslokal, keine neue persistente Jobquelle.

Ein manuelles `LocalWorkComposition.request` führt den vorhandenen Handler genau
einmal aus. Es wartet intern abbrechbar, statt bei einem normalen laufenden
Collector-Zyklus ein erneutes manuelles Absenden zu verlangen. Volle Queue,
Abbruch oder tatsächliche Requestfrist werden ausdrücklich gemeldet. Das ändert
keinen Browsercode und aktiviert keinen neuen Produktions-Reader. Die getrennte
Prüfung physisch begrenzter öffentlicher Reader bleibt erforderlich.

## Tatsächliche Komponenten

- Diagnose: vorhandener `CellAnalysisWorker` mit `DiagnosticRepository`-Referenz;
  keine Kopie des vollständigen Legacy-Arbeitscaches.
- Persistenz: vorhandener `DerivedPersistenceWorker` und `RepositoryPairWriter`.
  Ein erfasster endlicher Quellpräfix wird in bestätigten Schritten bearbeitet.
  Im Scheduler-Modus wird am bisherigen Schrittlimit kooperativ fortgesetzt;
  die gespeicherte Liste der Schrittmetriken bleibt begrenzt. Ohne Scheduler
  gilt weiterhin der bisherige Schrittbudgetfehler.
- Daily: die tatsächliche `check_once`-Arbeit einschließlich Discovery, Probe,
  Analyse und Ausgabe läuft unter einer Admission. Checkpoints sitzen auch in
  BMS-/Risk-Schleifen. Abbruch kann nicht als erfolgreiche Teilanalyse gelten.
- Display: tatsächlicher Zyklus einschließlich Wiederaufnahme und Discovery.
- Canonical/Startup: vorhandene `RepositoryPreparation`/`repository_step` mit
  den tatsächlichen kanonischen und Display-Schritten und dauerhaften Cursorn.
- MQTT: vorhandener `DerivedMqttWorker` mit unveränderter kompakter Projektion;
  Checkpoints zwischen Modulen, Zellen und Publikationsschritten. Die Tests
  verwenden einen speichernden Testclient, keinen Broker.

`WorkCancelled` wird erst außerhalb der wissenschaftlichen Operation in den
bestehenden Fehlervertrag des Workers übersetzt. Worker-Stopp kann laufende
lokal geplante Analyse/Persistenz/MQTT/Display/Daily-Arbeit abbrechen. Bereits
bestätigte Transaktionen werden nicht rückgängig gemacht.

## Sperren und Raw-Vorrang

Nur explizit zugeordnete schmale Raw-Abschnitte pausieren die abgeleitete Arbeit.
Die Raw-Seite wartet nicht auf eine DERIVE-/READ-Lease. Eine bereits laufende
Berechnung hält erst am nächsten Checkpoint an; dies ist keine harte Präemption.
Ein breites altes `cycle_started`-Flag sperrt lokal geplante Reads nicht.

Katalog-, Epochen- und Projektions-Schreibtransaktionen geben ihre Scheduler-Lease
nicht mitten in einer gemeinsamen Schreibsperre ab. Abbruch wird dort bis zum
Ende der begrenzten Transaktion aufgeschoben. Leser warten kooperativ auf
konkurrierende Python-/Dateisperren. Atomare Abschnittsdauer und Diagnose-SQL-
Sperrwartezeit werden erfasst; fsync/Systemaufrufe sind nicht hart unterbrechbar.

## Evidenz und Grenzen

Neue Regressionen: `test_work_scheduler.py`, `test_local_work_composition.py`,
`test_work_scheduler_load.py`. Sie prüfen Admission, FIFO-Fortschritt, getrennte
READ-Lane, Raw-Vorrang, Cancel/Frist/Shutdown, Sperrraces, exakte Analyse ohne
Quantum-Neustart, bestätigte Wiederaufnahme sowie tatsächliche Worker gemeinsam.
Der Lasttest protokolliert CPU/Wandzeit, Python-Peak, Prozess-RSS, Abstände/Jitter,
verpasste synthetische Slots, Raw-Ausgabebytes, logisches Dateiwachstum, SQL-
Sperrwartezeit, Queuealter/-wartezeit und Fortschritt. 10 ms ist ein synthetischer
Stressreiz, kein neuer PWR-/Zelltakt. Hardware, RS485 und Produktivdaten werden
nicht verwendet. Hardwarequeue/-drops und physische Geräte-I/O bleiben unbekannt.

Kein Gesamt-P0-Nachweis: Die bestehende Daily-Quellwahl und ihre exakte
Materialisierung/14-Tage-Risk-Historie bleiben erhalten. Chunkweise Raw-Lesechecks
und kooperative JSON-Ausgabe begrenzen nicht den gesamten Daily-Speicherbedarf.
Sortieren, einzelne große Datensätze, Bibliotheksaufrufe und fsync können weiterhin
ununterbrechbare Abschnitte bilden. Kein Anspruch auf konstante HA-Latenz oder
Fortschritt bei 100 % zwingender Hardware-/Raw-Auslastung. Vollständige
Raw-Queueintegration, HA-Budgets, produktive Auswahl, Hycube, Reader-Umschaltung,
Retention/GC, Migration/Rückkehr und reale Abnahme bleiben gesonderte Gates.
