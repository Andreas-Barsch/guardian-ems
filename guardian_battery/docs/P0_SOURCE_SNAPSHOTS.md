# Isolierte Inventare, eingefrorene Quellenfenster und gemeinsame Reads – Phase 2b2

Basis `36b8c2f99b30da5de502ae02efab3a70fc9d5915`. Nur lokaler Baustein; keine
Anbindung an main, Writer, Reader-Factories, HTTP, HA oder Hardware. Keine fachliche
Diagnose-/SOC-Regel oder Komponenten-/Releaseversion geändert. Internes, noch
unintegriertes Katalogschema2 erweitert Schema1 um Inventare, Receipts und Fenster.
Schema1 wird ausdrücklich ohne Mutation abgewiesen; keine Bestandsmigration erfunden.

## Expliziter Legacy-Inventarjob

`LegacySourceInventory.create` nimmt1..16 **explizite absolute Verzeichnisroots**
mit `legacy-local`/`utc` entgegen. Keine automatische Rootsuche, kein Start aus
Status, Reader oder Katalogkonstruktor. Ein separates dauerhaftes Inventar mit UUID
speichert Scope, Root-File-ID/mtime, Fortschritt, getrennte Source-/Hilfs-/Unbekannt-
Zähler und private Einträge. In der bestehenden Katalogdatenbank, keine neue
Laufzeit-/Dienstinfrastruktur. Katalogdatenbank außerhalb der Discoveryroots halten.

`step` betrachtet höchstens256 Verzeichniseinträge je Aufruf; Zähler und neu erfasste
Einträge werden atomar bestätigt. Es gibt höchstens einen lebenden scandir-Iterator
pro Jobobjekt, über Schritte hinweg. Nach Prozessabbruch ist der OS-Iterator verloren:
ein neuer expliziter Discoverylauf durch denselben Root beginnt am Anfang und
überspringt durable bereits gesehene Namen. Auch übersprungene Einträge zählen
gegen das Schrittbudget und als `replayed`. Der erneute Präfixdurchlauf ist echte
Migrationsarbeit, **kein** angeblich kostenloser Warmstart. Der Iterator bleibt
zwischen diesen begrenzten Aufrufen offen, sonst würde ein großer Root nie enden.
Bei Ausnahme wird ein möglicherweise vorausgelaufener Iterator geschlossen und
beim nächsten Aufruf sicher neu begonnen. Eigentümer ruft `close` bei Abbruch auf.

Alle Roots werden flach erfasst: reguläre `*.jsonl` als Sourcekandidaten, andere
reguläre Dateien als Hilfsdateien. Unterverzeichnisse, Symlinks und Sonderdateien
bleiben `unknown`; kein Folgen außerhalb des Scopes. Source-ID beinhaltet Pfad und
File-ID, Kandidatenrevision die beobachtete Größe/mtime/File-ID. Die Bindung an
eine bereits bekannte Revision beschreibt die Nacharbeit unten. Rohbytes/Namen unverändert.
Individuell beobachtete Metadaten sind keine atomare Dateiinhaltsaufnahme und kein
Writer-fsync-Beleg. Import prüft die gespeicherte Beobachtung erneut und lehnt
inzwischen veränderte Quellen ab, statt neue Bytes unter altem Beleg zu übernehmen.

Erst nach EOF aller unveränderten Rootidentitäten wird Scope `complete`; bis dahin
liefert `page` keine veröffentlichten Inventarseiten. Änderung der Root-File-ID/mtime
führt zu `scope_changed`: vorhandene privaten Einträge bleiben erhalten; ein neuer
expliziter Job ist nötig. Ein kompletter flacher Namensscope ist keine Aussage über
unbekannte Unterverzeichnisse, Datenabdeckung oder Raw-Dauerhaftigkeit. Pages sind
über gespeicherte IDs keyset-paginiert. Bekannte Source-/Helperitems können über den
bestehenden `ingest_page`-Vertrag übernommen werden; `unknown` bedarf einer separaten
Entscheidung und wird dort nicht still als Helper/Source importiert.

## Eingefrorener UTC-Fenstervertrag

`open_window(start,end,modules=...)` legt einen kleinen dauerhaften Fenstersnapshot
an: Anfrageparameter und höchstens256 passende registrierte Quellrevisionen mit
Receiptbezug und **damals indiziertem Endoffset**. Kein Kopieren aller Records und
keine große dauerhafte Lesetransaktion. Quellwahl erfolgt über den Zeitindex mit
explizitem SQL-Budget; bei Überschreitung gibt es keinen Teilsnapshot. Ein Fenster
umfasst weiterhin höchstens32Tage. Noch nicht indexierte Quellen/Bytes bleiben
sichtbar außerhalb der zugesagten Coverage; spätere neue Quellen werden nicht
nachträglich Teil derselben Abfrage.

`plan_snapshot(snapshot, after=...)` paginiert über diese eingefrorenen Präfixe.
Bestätigte Raw-Appends, spätere Indexfortschritte, neue Quellen oder andere Tage
ändern den Snapshot nicht. Ein SourceRef enthält Snapshot-ID, Receipt und Endoffset;
auch vor einem Append erzeugte SourceRefs bleiben nach einem **bestätigten** Append
verwendbar. Das ist der neue Vertrag für eine später anzubindende manuelle Abfrage.
`plan_window` bleibt als bisheriger kurzlebiger CAS-Planer erhalten, einschließlich
seiner konservativen globalen Generationsprüfung; er ist nicht der stabile Vertrag.
Es gibt keine automatische HTTP-Wiederholung oder stillschweigende Neuplanung.
`release_window` entfernt nur das eigene kleine Snapshotmanifest explizit.

Der Katalog persistiert dafür eine append-only Receiptreihe je Source/Revision mit
Sequenz, Vorgänger, Frontier und verkettetem Metadatenhash. Registrierung beginnt
die Reihe; `confirm_append` hängt nur an den erwarteten aktuellen Receipt bei
unveränderter Writer-Epoche/File-ID und nicht sinkender Frontier/Größe an. Ein
Snapshot referenziert einen unveränderlichen alten Receipt; der Leser prüft diesen
Bezug, die fortgesetzte registrierte Reihe und den eingefrorenen Endoffset, ohne
die komplette Kette oder das Rawpräfix zu lesen. Datenzugriff prüft zusätzlich
sämtliche ausgewählten Recordhashes und aktuelle registrierte Filemetadaten.
Revisionwechsel oder echte Truncation/Rotation/Rewrite ergeben einen Fehler. Ein
noch nicht durch einen passenden Receipt bestätigter Append wird ebenfalls nicht
still akzeptiert; keine eigenständige Reparatur/Wiederholung wird ausgelöst.

**Grenze:** Das ist ein lokal kontrollierter Übergabevertrag, noch kein echter
Rawwriter-Registryjournal-/fsync-Nachweis. Receiptargumente sind weiterhin explizite
vertrauenswürdige Eingaben. Metadatenhashkette ersetzt weder authentische Durable-
Receipts noch Integritätsprüfung ungefragter Rawbereiche. Ein fremder Rewrite mit
wiederhergestellter Größe/mtime außerhalb der gelesenen Recordbereiche bleibt
unbekannt. Eine als Append falsch deklarierte Änderung ist ein Vertragsbruch,
keine durch diesen Baustein bewiesene unveränderliche Gesamthistorie.

## Gemeinsame physisch begrenzte Reads

`read_refs` gruppiert nach ausgewähltem Pfad/File-ID, öffnet jede ausgewählte Datei
einmal und vereinigt nur angrenzende oder überlappende Bytebereiche. Lücken werden
nicht mitgelesen. Höchstens256Refs und65536angefragteBytes je Aufruf. Pro ursprünglichem
Record wird sein Hash geprüft; Rückgabe bleibt in ursprünglicher Planreihenfolge,
mit Timestampkonflikten, Herkunft und sämtlichen im Record vorhandenen Kanälen.
Doppelt angefragte Records können denselben physischen Read nutzen, bleiben aber
als doppelt angefragte Ergebnisse erhalten: keine fachliche Deduplizierung.

Zähler: `selected_files` = ausgewählte physische Pfad/File-ID-Gruppen,
`files_opened` = tatsächliche Opens, `read_calls` = tatsächliche zusammengefasste
Reads, `raw_bytes` = tatsächlich gelesene Bytes, `decodes` = zurückgegebene
Recorddecodes. Nicht als Dateianzahl ausgegebene Ergebnispunktzahl. SQL-/stat-
Metadaten-I/O ist davon getrennt und kein gemessener physischer SQLite-Bytewert.

## Abdeckung und weiterhin offen

Synthetische Tests: >512Helper, private Inventarseiten, Crash-/Ausnahmewiederaufnahme,
Rootänderungen/unbekannte Einträge, Ablehnung veränderter Inventarquellen, stabile
Pagination trotz Raw-Appends/Indexfortschritt/fremder Katalogänderungen, Receipt-
Fortsetzung, Rewrite-/Truncation-/Rotationfehler, gemeinsame6Module/mehrereKanäle bei
1/30/365/730Tagen, tatsächliche ausgewählte Dateipfade und lückenfreie Bytebudgets.
Alle89bisherigen Regressionen unverändert erhalten. Logs enthalten genaue Zahlen.

Keine automatische Discovery, echte Writerjournalanbindung, Scopeerweiterung in
Unterverzeichnisse, Produktivmigration, Epoch-/Tages-/Fachsemantikmigration, GC alter
Quellrevisionen oder automatische Snapshotabläufe implementiert. Inaktive Revisionen
können zusätzliche SQL-Kandidatenarbeit erzeugen; VM-Budgetabbruch bleibt möglich.
Keine HA-Latenz-/Last-/Stromausfallgarantie. A1–A9/T01–T16 bleiben insgesamt offen.
Nachweis dieses lokalen Teilbausteins ist Kontrollpunkt2b2, keine Integrationsfreigabe.

## Nacharbeit: wiederholte Inventare und stabile Provenienz

Am Commit38886bb war eine zweite Inventarisierung derselben unveränderten Quelle
nicht idempotent: `writer_epoch=inventory:<job>` und `context.inventory` kollidierten
mit derselben Source-ID/Revision. Dieser Fehler wird nicht durch pauschales Ignorieren
von Registrierungsabweichungen behoben. Der allgemeine `register`-Vertrag bleibt streng.

Neue Inventare trennen jetzt:

- Stabile Quellmerkmale: Pfad/File-ID, Namensraum, stabile Legacy-Writerkennung,
  Root/Dateiname und Herkunftsart. Die beobachtete Größen-/mtime-Kennung ist eine
  **Kandidatenrevision**, nicht automatisch eine neu zu aktivierende Revision.
- Jobbezogene Beobachtung: unverändertes geschlossenes Inventar mit eigener ID,
  Eintrag, beobachteter Signatur und ursprünglicher Kandidatenregistrierung.
- Unveränderliche Bestätigung `source_observation`: Bezug/Hash des Inventareintrags,
  tatsächlich gebundene Katalogrevision, Receipt, Frontier und damaliger Indexcursor
  sowie Ergebnis der Übernahme. Kein Überschreiben des alten Quellkontexts.

Nur Sourceitems, die exakt zu einem gespeicherten geschlossenen Inventareintrag
passen, nutzen diesen Sonderweg. Ein manipuliertes Item oder eine widersprüchliche
Namespace-/File-ID-/Herkunftsregistrierung wird abgewiesen. Allgemeine Registrierungen
werden dadurch nicht gelockert. Beobachtungen und Manifestcursor committen zusammen.
`inventory_observations` liefert die Bestätigungen in begrenzten Seiten.

Ergebnisse:

- `registered`: neue beobachtete Quelle ausdrücklich registriert.
- `known_prefix`: bekannte Quellmerkmale und aktuell registrierte Signatur/Receipt
  passen; vorhandene Revision, Cursor, Index, Coverage und Receipt bleiben unverändert.
  Das gilt auch, wenn ein seit dem ersten Inventar bestätigter Append die beobachtete
  Kandidatenrevision verändert hat: die bereits belegte aktive Revision bleibt bestehen.
- `known_prefix_unconfirmed_tail`: bekannte bestätigte Frontier liegt vor der
  beobachteten Dateigröße. Keine Aufwertung dieses unbestätigten Rests.
- `needs_receipt_or_revision`: neue, noch nicht belegte Größen-/mtime-Beobachtung
  derselben physischen Quelle. Keine automatische Appendannahme, neue Revision,
  Cursoränderung oder Reanalyse. Unveränderte Nachbarn und neue Dateien können
  in derselben Seite übernommen werden. Der offene Eintrag benötigt später einen
  ausdrücklichen Appendreceipt **oder** eine explizite neue Revisionsregistrierung.
  Eine frische Beobachtung kann diese Entscheidung danach belegen; das historische
  offene Ergebnis wird dabei nicht umgeschrieben.

Ein fertiger Importcursor bedeutet also weiterhin nur verarbeitete Manifesteinträge,
nicht, dass alle offenen Sourceentscheidungen oder die Datenabdeckung erledigt sind.
Wenn die Datei bereits seit der konkreten Inventarbeobachtung erneut verändert wurde,
wird die veraltete Beobachtung abgewiesen und nicht als aktuelle registriert. Bereits
bestätigte historische Beobachtungen dürfen idempotent erneut quittiert werden;
das erzeugt keine neue Coveragebehauptung und setzt niemals Fortschritt zurück.

Das frühere jobabhängige Format wird nur erkannt, wenn sein gespeicherter ursprünglicher
Inventareintrag die alte Source-ID, Revision, Writerkennung und Herkunft tatsächlich
belegt. Diese Felder und ihr erster Jobbezug bleiben bytegleich in der Quellzeile;
neu ist ausschließlich der separate Beobachtungsbezug. Die ergänzende Bestätigungstabelle
wird innerhalb des unveränderten unintegrierten Katalogschemas2 geführt. Bei einer
älteren Schema2-Datenbank wird sie nur beim **expliziten** Beobachtungsimport additiv
angelegt, nicht bei normalem Warmstart/Reader. Keine Quell-, Receipt- oder Indexmigration.

Rootänderung mit zusätzlicher Datei: alten Job `scope_changed` erhalten, neuen Job
explizit abschließen; unveränderte bekannte Quellen wiedererkennen, neue Dateien
registrieren. Rotation mit anderer File-ID erzeugt eine andere Source-ID; sie wird
nicht als alter Fortschritt ausgegeben. Alte Referenzen/Quellen werden nicht heimlich
umgebogen oder gelöscht. Helperzähler im Gesamtkatalog bleiben historische
(job,path)-Beobachtungszähler; der einzelne Inventarscope zählt seine Einträge getrennt.

Die bisherige Grenze bleibt: eine fremde Änderung mit vollständig wiederhergestellter
Größe/mtime ist ohne Lesen der betreffenden Bytes nicht erkennbar. Ausgewählte Records
werden weiterhin gehasht; ein Metadatenvergleich ist kein Vollinhaltsbeweis. Kein
verdeckter Gesamthash und keine neue Fachregel wurden eingeführt.
