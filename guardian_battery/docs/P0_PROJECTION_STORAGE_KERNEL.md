# Lokaler Projektions-Speicherkern – Phase 2a

Basis: Battery 0.8.9 / `3739a5526bda1d214fe68353d9abce5017657a1d`.
Dieser Baustein ist **nicht an die Anwendung angeschlossen**. Keine Versionserhöhung,
keine produktive Datenbank, keine Raw-/Writer-/Liveintegration. Architekturkorrektur
A1–A9 und Gesamtfälle T01–T16 bleiben offen. Dies ist kein Releasekandidat.

## Vertrag

`ProjectionRepository` benötigt einen expliziten SQLite-Dateipfad; es gibt keinen
Defaultpfad und keine automatische Verzeichniserstellung. Internes Speicherschema 2
(keine Änderung einer Komponenten-/Releaseversion). Das unintegrierte Testschema 1
aus Commit `16a6927` wird ohne Änderung abgewiesen; keine Migration erfunden.
Nur dedizierte neue
Datenbanken verwenden. Unbekannte Schemafassungen werden abgewiesen; Migrationen
sind nicht implementiert. WAL, foreign_keys und synchronous=FULL sind aktiviert.
Dies ist keine Zusage gegen Stromausfall oder Geräte-/Dateisystemfehler.

- `register_source` registriert eine explizite Quellrevision mit Herkunftsmetadaten
  und bestätigter Bytefrontier. `confirm_source` erweitert diese mit Vergleich des
  bisherigen Werts. Der Kern liest keine Rawdateien und prüft keine Writerreceipts.
  Der spätere Adapter muss Revision, Teilzeilen und Haltbarkeit tatsächlich belegen.
- `begin` persistiert einen privaten Entwurf mit erwartetem Bundlehead; `stage`
  schreibt ausschließlich Zeilendeltas, `None` bedeutet Retire. `stage_cursor`
  hinterlegt erwartete Offset-/Generationspaare und neue Offsets. Bestätigte Cursor
  und Leser sehen davon nichts. `discard` entfernt nur diesen privaten Entwurf.
- `publish` vergleicht Head, Partitionstips und Cursor, prüft Quellfrontiers und
  bestätigt alle Deltas, Cursor, Publikationen, Bundlemitglieder, Eingangsvektor
  und Head in einer Transaktion. Cursor ohne Produktoutput sind unzulässig.
  Mehrere Heads dürfen dieselbe Partition nicht unabhängig überschreiben.
- Publizierte Payloads sind unveränderliche MVCC-Zeilen. Nur einmaliges Setzen der
  Retirementgeneration ist erlaubt. Ungeänderte Zeilen werden geteilt. Retire
  entfernt keinen Payload aus einer weiterhin referenzierten alten Generation.
  DB-Trigger schützen Updates; beliebige externe SQL-Schreibzugriffe gehören
  ausdrücklich nicht zum unterstützten Vertrag.
- `read` liest eine Schlüsselbereichsseite einer ausdrücklich benannten Generation
  und prüft Payload-SHA256. Metadaten gehören zur unveränderlichen Publikation.
  `inputs` liefert deren Bundle-Cursorvektor, nicht den späteren aktuellen Cursor.
  Nicht gleichzeitig geänderte Produkte behalten sichtbar ihre eigene Generation.
- Abgeleitete Produkte können über `requires` ältere Eingangs-Bundles schützen.
  Es gibt keine automatisch behauptete fachliche Konsistenz: Adapter müssen exakte
  Eingänge, Semantikversion, Epochen, Coverage und Zeitangaben bereitstellen. Diese
  produktbezogenen Metadatenvalidierungen sind noch nicht implementiert.
- `pin` hält ein Bundle über mehrere getrennte Lesetransaktionen; `release` gibt
  es explizit frei. Headlesen und nachträgliches Pinnen können mit GC konkurrieren:
  ein fehlgeschlagener Pin ist kein neuer Snapshot. Keine stillschweigende Auswahl
  eines neueren Heads. Automatische Leaseabläufe/Ownererkennung fehlen bewusst.
- `collect` bearbeitet persistente Kandidatenseiten und schützt Heads, Pins,
  Entwurfsbasen, Bundleabhängigkeiten und aktuelle Partitionstips. Publikationen
  schützen ihre sichtbaren Payloadversionen. Abhängigkeiten zeigen nur rückwärts;
  freigewordene Vorgänger werden gegebenenfalls in einem späteren Umlauf entfernt.
  Keine Raw-Retention, kein VACUUM und keine selbst gewählte Zeitaufbewahrung.
  Replay-/Outbox-/Migrationsadapter müssen später ihre Roots explizit anbinden.

## Grenzen und Nachweise

Strukturelle Obergrenzen: 1024 kleine Deltas je Entwurf, Partitionen/Bundlemitglieder,
Cursorvektoreinträge und Abhängigkeiten; JSON-Wert maximal 65536 UTF-8-Bytes;
Zeilenschlüssel nicht leer und maximal 512 Zeichen; Leseseiten und GC-Kandidaten
höchstens 1024. Überschreitungen werden zurückgerollt, nicht abgeschnitten.
Diese Grenzen sind keine freigegebenen Anlagen-, Retentions- oder Latenzbudgets.
Für große Korrekturen gilt der nachfolgende separate Seitenvertrag. Die Grenzen
begrenzen Materialisierung, beweisen jedoch keine konstante SQLite-I/O-Zahl:
MVCC-Filter, Indexhistorie und Referenzprüfungen benötigen spätere Skalierungs- und
Budgetnachweise. Generationen/Quellrevisionen werden noch nicht bereinigt.

Synthetische Tests: `tests/test_projection_repository.py`. Testlauf ausschließlich
in der vorhandenen isolierten Umgebung mit vorgeschaltetem `test-guard`, ohne
Bytecode- und pytest-Cache. Datenbanken liegen unter pytest-Temporärpfaden.

Getestet werden persistierte private Entwürfe/Wiederaufnahme, Quellfrontier-/Cursor-/
Headkonflikte, neue Lesetransaktion auf Generation N nach N+1, unveränderte Payloads,
Mehrprodukt-Atomizität, schlüsselbasierte Seiten, Delta-Sharing, Retire, geschützte
GC und Freigabe, abgeleitete Eingangsabhängigkeiten sowie Schemaabwehr. Fehlerhooks
prüfen Transaktionsgrenzen. Separate Kindprozesse enden absichtlich mit `os._exit`
zwischen Produkt-, Cursor-, Manifest-, Bundle-, Head- und Commit-Schritten sowie
vor/nach GC-Commit. Das sind Prozessabbruchtests, keine Stromausfalltests.

Keine Behauptung zu Writerack, Tagesabschluss, UTC-/Legacy-Migration, selektiver
fachlicher Invalidierung, Dirty-Acks laufender Worker, UI, Timing, Hardware oder
HA-Abnahme. Keine Änderung an MCP, Tunnel, Engine, SOC oder Diagnoseformeln.
Nächster Schritt ist die Kontrolle dieses Bausteins; weitere Integration benötigt
die nächste Phasenfreigabe.


## Nacharbeit: große private Partitionskorrekturen

Die Kontrolle von `16a6927` zeigte: `stage` erlaubt insgesamt nur 1024 Deltas im
Entwurf. Ein zweiter Aufruf mit einer weiteren Zeile wurde korrekt abgewiesen,
aber größere private Korrekturen waren damit nicht möglich. Der kleine atomare
Deltapfad bleibt mit dieser Grenze erhalten; sein Limit wurde nicht hochgesetzt.

Die konkrete Alternative für große Korrekturen ist ein **vollständiger privater
Partitionsersatz**. Keine wachsende Overlaykette, kein riesiger Final-Commit:

1. `begin` fixiert den erwarteten alten Bundlehead N.
2. `begin_replacement(work, product, partition, metadata=...)` reserviert eine
   private Lineage (getrennter Zeilennamensraum) und einen kleinen Checkpoint.
   Dieselbe Partition darf nicht zusätzlich kleine Deltas im selben Work erhalten.
3. `stage_replacement` nimmt höchstens 1024 Zeilen je Aufruf an. Der Aufrufer liefert
   die **vollständigen gewünschten Inhalte**, aufsteigend nach stabilen Schlüsseln.
   Zwischen Seiten müssen die Schlüssel strikt weiterlaufen. Die Gesamtzahl ist
   nicht auf 1024 beschränkt. Jede Seite schreibt nur neue Payloads/Hashes und den
   Checkpoint `(Zeilenzahl, letzter Schlüssel, sealed)` in einer Transaktion.
   Physische Arbeit mit der alten Historie liegt nicht in diesem Aufruf.
4. `replacement_progress` liest den persistenten Checkpoint ohne COUNT/Inventur.
   Nach Neustart oder unklarem Ack dort fortsetzen. Erwarteter Checkpoint ist CAS;
   doppelte/überlappende Seiten werden abgewiesen, nicht doppelt angehängt.
5. `seal_replacement(expected=...)` erklärt den gelieferten Stream für vollständig
   und friert ihn ein; auch ein ausdrücklich leerer Stream ist zulässig. Die
   Vollständigkeit ist eine Verpflichtung des aufrufenden Projektors, keine aus
   Zeilenzahl oder Seal abgeleitete fachliche Coveragebehauptung. Keine Publikation
   oder bestätigter Cursor ändert sich durch Stage oder Seal.
6. `publish` prüft alle erwarteten Heads/Cursor/Partitionstips und Seals. Es legt
   lediglich begrenzte Publikations-/Bundlemetadaten an, referenziert die vorbereiteten
   Lineages und tauscht Cursor und Head gemeinsam. Es liest/kopiert/retiriert die
   großen Payloadbestände nicht. Ein Fehler vor Commit lässt N sichtbar und Work
   fortsetzbar; nach Commit ist nur der komplette neue Bundlezustand bestätigt.
7. Jede Publication fixiert ihre Lineage und Readgeneration. Leser N sehen weiterhin
   ausschließlich die alte Lineage. Ausgelassene Schlüssel sind im Ersatz entfernt,
   niemals still aus dem alten Bestand geerbt. Spätere **kleine Deltas** arbeiten
   MVCC-basiert in der neuen Lineage; sie kopieren den Ersatzbestand nicht.
8. GC schützt private Lineages über Work und publizierte Lineages über Publications.
   `discard` entfernt nur kleine Workmetadaten. Die großen aufgegebenen Payloads
   werden anschließend in bestehenden GC-Kandidatenseiten entfernt, nicht kaskadiert.

Dieser Weg ist eine Ausnahme für explizite vollständige Korrekturen, kein Ersatz
für normale inkrementelle Appendverarbeitung. Er kostet über alle Vorbereitungsschritte
O(Partitionsgröße) zusätzliche Schreibarbeit und Platz. Der Aufrufer muss Ausgabe
geordnet/budgetiert erzeugen; dieser Kern sortiert nicht nachträglich einen ganzen
Tag und baut keine Rawdaten neu auf. Nach falscher bereits gespeicherter Seite ist
dieser Stream zu verwerfen und neu vorzubereiten; keine verdeckte In-place-Reparatur.
Diskreserve, Cancellation-/Schedulerbudgets und fachlicher Rechen-/Resumezustand sind
weiter Integrationsaufgaben. Die neue Grenze ist also ausdrücklich: große Korrektur
als vollständiger geordneter Ersatz, nicht beliebig großer atomarer Delta-Overlay.
Maximal 1024 Partitions-/Cursor-/Bundlemetadateneinträge und höchstens 1024 kleine
zusätzliche Deltas im Final-Commit bleiben bestehen. Keine Größen-/Latenzgarantie
für eine Produktionsanlage aus diesen strukturellen Grenzen ableiten.

Zusätzliche Tests: >1024 Zeilen in kleinen Aufrufen, Wiederöffnen nach jeder Seite,
Seiten-/Checkpoint-Crash vor/nach Commit, alte Leser, beide Produkte und beide
Cursor separat an Fehlerpunkten, GC privater Lineages, fehlendes Seal, stale CAS,
leerer Ersatz, gemischter Delta-/Ersatzcommit und kleine Deltas nach großem Ersatz.
Alle bisherigen 32 Fälle bleiben enthalten; Produkt-Fehlerpunkte sind nun für
Current UND Aggregate parametrisiert, unabhängig von SQL-Ergebnisreihenfolgen.
Ein instrumentierter synthetischer Vergleich mit 129 und 4097 Zeilen je Produkt
prüft gleiche geänderte Zeilenzahl, SQL-VM-Schritte und Statementzahl beim finalen
Publish. Der SQL-Trace enthält dort keinen Payloadtabellenzugriff. Das ist kein
Beleg für konstante physische I/O-/Latenzkosten oder HA-Timing; Vorbereitungskosten
sind ausdrücklich getrennt. Die genauen Messwerte stehen im neuen Testprotokoll.
