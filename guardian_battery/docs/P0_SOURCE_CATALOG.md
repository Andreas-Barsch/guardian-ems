# Isolierter Quellenkatalog und CellSourceResolver – Phase 2b1

Historischer Vertragsstand von Phase2b1. Die lokalen Ergänzungen zu Inventarjobs,
eingefrorenen Fenstern und gemeinsamen Reads stehen in [P0_SOURCE_SNAPSHOTS.md](P0_SOURCE_SNAPSHOTS.md).
Die unten beschriebenen damaligen Lücken bleiben als Verlauf erhalten.

Basis: `815cc927661d76e77ba130bbe4d41e18cc56c205`. Kein produktiver Anschluss,
keine Komponenten-/Versionsänderung. Separater expliziter SQLite-Pfad, keine
Änderung des Projektionsspeichers. Alle A1–A9/T01–T16 insgesamt weiterhin offen.

## Tatsächlicher Vertrag

`SourceCatalog.register` übernimmt ausdrücklich Source-ID, Revision, absoluten
Pfad, Namensraum (`legacy-local`/`utc`), Writer-Epoche, Receiptkennung, bestätigte
Bytefrontier und optionalen Herkunftskontext. Dateiname ist ausschließlich Herkunft;
UTC-Zuordnung entsteht aus jedem gültigen Recordtimestamp. Eine Revision ist kein
Pfadalias: Rewrite/Truncation benötigen eine neue explizite Revision und CAS gegen
die bisher aktive Revision. Rotation benötigt gemäß Registryvertrag eine neue
Source-ID. Der Katalog errät weder Rotation noch neue Pfade/Identitäten.

`confirm_append` verlangt gleiche File-ID/Writer-Epoche, bisherigen Receipt und
einen neuen Receipt sowie nicht rückläufige Frontier/Größe. Das ist eine explizit
vertrauenswürdige Übergabe durch den späteren Registryadapter, **kein** Beweis,
dass der Writer fsync ausgeführt hat oder alte Bytes unverändert sind. Receiptnamen
werden noch nicht kryptografisch gegen das autoritative Journal verifiziert.
Normale mtime-/File-ID-/Größenabweichungen ohne passende Registrierung führen zu
Revisionsfehlern. Ein gleich langer fremder Rewrite mit wiederhergestellter mtime
ist bei reinem Status/Index-Warmstart nicht erkennbar. Jeder ausgewählte Record
wird beim begrenzten Lesen gegen den beim Indexieren gespeicherten SHA256 geprüft;
Manipulationen außerhalb des angefragten Bereichs werden nicht heimlich vollgehasht.

`ingest_page(job, expected_cursor, items, finish)` übernimmt höchstens 256 Einträge
**eines explizit bereitgestellten stabilen Inventar-/Registrymanifests**. Source-
und Hilfsdateien müssen darin getrennt klassifiziert sein. Registrierungen und
Manifestcursor werden gemeinsam committed. Fortschritt ist die Anzahl der
Manifesteinträge, kein wiederverwendbarer scandir-Offset. Wiederaufnahme liest
`migration_progress`; eine wiederholte bereits bestätigte Seite scheitert an CAS.
Hilfsdateien zählen nicht als Rawquellen; mehr als 512 Hilfsdateien sperren nicht.
`registered` bietet geordnete Katalogseiten. Es gibt keine Verzeichnisinventur im
Konstruktor, Status oder Resolver.

**Teilabdeckung:** Das Erzeugen/Verifizieren dieses stabilen Legacyinventars durch
einen expliziten Discoveryjob ist noch nicht implementiert. Ebenfalls offen:
Journalformat/Rotation, authentische Durable-Receipts, Discoveryvollständigkeit,
Pfadverlagerung alter rotierter Quellen und Übernahme alter Diagnoseartefakte.
Ein finished-Manifest beweist nur das Ende dieses übergebenen Manifests, nicht die
Vollständigkeit aller Betriebsquellen. Diese Arbeit ist keine vollständige Migration.

`index_step` liest höchstens 65536 Bytes und decodiert höchstens 256 vollständige
JSONL-Zeilen pro Aufruf, jeweils ab bestätigtem Indexcursor und nie über die
registrierte Frontier hinaus. Ausgaben (Zeit-/Byteindex bzw. Fehlerzeile) und
Cursor werden in derselben Transaktion gespeichert. Nach Prozessabbruch bleiben
entweder alter oder neuer Batch bestätigt. Bestätigte Batches werden beim Resume
nicht erneut decodiert. Über den Recordcount hinaus vorgelesene, unbestätigte Bytes
können im nächsten Batch erneut gelesen werden; die Byteobergrenze gilt je Aufruf.
Teilzeile bleibt vor ihrem Anfang, ohne Decode. `partial_line_or_budget` bedeutet:
noch kein Fortschritt möglich; Frontier muss vollständige Zeile enthalten bzw.
Schrittbudget ausreichend sein. Eine Zeile über 65536 Bytes wird nicht automatisch
weitergeladen oder übersprungen. Ungültiges JSON/Schema/Zeitstempel erhält einen
expliziten Fehler mit Byteoffset/Hash; es entsteht kein erfundener Messpunkt.

## UTC-Fenster und Revisionssicht

`CellSourceResolver.plan_window(start,end)` verwendet halboffene UTC-Sekunden,
maximal 32 Tage und 256 Treffer je Seite. SQL-Zeitindex `(UTC-Tag, Timestamp,
Source-ID, Revision, Offset)` begrenzt die Suche; keine Rawdatei, kein Verzeichnis
und kein Indexaufbau wird im Planer geöffnet/ausgelöst. Aktive Revisionen werden
explizit verknüpft. Technische Sortierung erhält alle Records; keine fachliche
Deduplizierung bei gleichen Timestamps, auch nicht über Namensräume hinweg.
Modulnummer/Seriennummer und bekannte Epochfelder bleiben Beobachtungen; fehlende
Identität bleibt null. Registrierter Herkunftskontext bleibt getrennt sichtbar.

Jeder `SourceRef` enthält Quellrevision, Pfad/Konvention, exakten Bytebereich,
Timestamp, Identitäts-/Herkunftskontext, Recordhash und File-Signatur. `read_refs`
liest nur diese Bereiche mit maximal 65536 Bytes pro Aufruf und prüft Hash und
Signatur. Zurzeit wird jeder Bereich separat geöffnet; gemeinsame Filehandles
sind nicht behauptet. Fileöffnungen zählen daher wirklich pro Recordbereich.
Keine zusätzlichen Batterie- oder RS485-Aufrufe.

Pagination führt einen kleinen Kataloggenerationsvergleich mit. Bei zwischenzeitlicher
Katalogänderung muss der Auftrag explizit neu geplant werden; kein Auto-Retry und
kein stiller Mischstand. Das ist ein konservativer CAS-Vertrag, noch kein alter,
gepinnter Katalogsnapshot über beliebige Updates. Unabhängig davon bleiben erhaltene
SourceRefs gegen ihre Bytes prüfbar. Veraltete Revisionen werden nicht gelöscht;
liegt ihre frühere Datei nicht mehr am ursprünglichen Pfad, ist Lesen nicht garantiert.

SQL-Materialisierung ist auf Seite+1 begrenzt; ein zusätzlicher VM-Schrittzähler
bricht bei Erschöpfung explizit mit `WindowBudgetExceeded` ab, ohne leere Historie
oder einen Rebuild vorzutäuschen. Standard 50000, technisch höchstens100000 Schritte.
Das sind strukturelle Schutzgrenzen, keine HA-Latenz-/I/O-/Anlagengrenzen. Inaktive
Revisionen und Modulfilter können innerhalb eines Fensters zusätzliche SQL-Arbeit
verursachen; bei zu vielen Kandidaten ist Budgetabbruch möglich. Physische
SQLite-Pages/Bytes, Schedulerfairness und Produktionslimits sind noch nicht belegt.

Coverage heißt ausdrücklich `indexed_records_only; undiscovered_sources_unknown`.
Ein kleines Zählerobjekt meldet aktive noch nicht eingeholte Quellen und kumulierte
ungültige Records aller registrierten Revisionen. Fehlende Quellen sind unbekannt;
keine Treffer sind kein Vollständigkeits- oder Entwarnungsbeleg.

## Lokale Tests und Grenzen

Gezielte synthetische Tests decken Legacy-only, UTC-only, Mischquellen, 23:30Z in
Europe/Berlin, 23-/25-Stunden-Tage, mehrere Quellen je UTC-Tag, ungeordnete Records,
Timestampkonflikte/Identitätskontext, Teilzeilen, Append/Rewrite/Truncation/Rotation,
Stat-erhaltenden Rewrite im gelesenen Bereich, >512Hilfsdateien und Prozessabbrüche
zwischen Index/Manifestausgabe und Cursorcommit ab. Keine Zeitzonen-Fachregel wird
in den Katalog verlagert: der Aufrufer rechnet lokale Tagesgrenzen in UTC um.

Gleiches24h-Fenster bei1/30/365/730Tagen: Rawbytes, echte Fileöffnungen, Decodes,
materialisierteSQL-Zeilen, VM-Schritte und EXPLAIN-Pläne stehen im Testlog. Kalter
hier bedeutet neue Repository-/Resolverinstanz, **nicht** geleerter Betriebssystem-
Cache. Warmplan wird separat ohne Raw-I/O geprüft. Keine globale Alters-/Last-
garantie aus dieser kleinen synthetischen Verteilung ableiten.

Alle64vorhandenen Speicherkernregressionen bleiben unverändert. Keine Anbindung an
main, Writer/Reader-Factories, echte Registry, produktive Migration, UTC-Writer,
Tagesdiagnose, UI, MCP oder HA. Kein vollständiger T05/T07/T12-Abnahmenachweis.
Weitere Integration erst nach Kontrollpunkt2b1 und eigener Phasenfreigabe.
