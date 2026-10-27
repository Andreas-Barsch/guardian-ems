# Battery 0.8.6 / Research-MCP 0.8.4 – lokale Releasevorbereitung

Unveröffentlicht und nicht installiert; auf Quellstand
`b90c134db8171e742cb0a24bf15cd56b25e9e6a6` vorbereitet. Manifest und Laufzeit
werden lokal auf Battery 0.8.6 / MCP 0.8.4 angehoben; Tunnel 0.8.1 und
Diagnostic Engine 0.4.12 bleiben unverändert. Der finale Quellcommit steht
vor dem separat freizugebenden Releasecommit noch nicht fest.
Die unten erhaltenen Angaben vom 1. Oktober beschreiben den früheren Prüfstand.
Insbesondere dessen 10-/15-Sekunden-Grenzen und Prüfergebnisse belegen diese
neue Budgetlogik nicht.

## Budgetkette des Kandidaten

| Station | Grenze und Beginn |
| --- | --- |
| Battery Research-Gate | 60 s ab Slotannahme, einschließlich Identitätsladen, Lesen, Projektion und Antwortgrößenprüfung |
| Battery Parallelität | Weiterhin 2 Anfragen insgesamt, davon höchstens 1 explizite Vollauflösungsanfrage; keine Warteschlange, bei Belegung `busy` |
| MCP Warteschlange | Unabhängig weiterhin 15 s; höchstens 8 wartende Anfragen, 2 aktive / 1 volle Auflösung |
| MCP HTTP | Default und Validierungsmaximum 75 s ab HTTP-Aufruf, einschließlich Empfang des vollständigen Bodys; kein Retry |
| MCP maximale lokale Wartezeit | Bis 15 s Warteschlange + 75 s Transport, zuzüglich begrenzter Protokollverarbeitung; keine Zusage einer äußeren 90-s-Frist |
| Tunnel | Unverändert 2 gleichzeitige Anfragen; `MCP_STARTUP_WAIT_TIMEOUT=30s` betrifft Startbereitschaft, nicht das Abfragebudget |
| OpenAI Connector / Tunnelservice | Tatsächlich wirksame äußere Abbruchfrist unbekannt; durch diese Änderung nicht konfiguriert |

75 s geben dem 60-s-Battery-Budget 15 s Reserve für Verbindungsaufbau,
kooperativen Abbruch, Fehlerantwort und Übertragung. Diese Reserve ist eine
begründete lokale Einstellung, kein gemessener Ende-zu-Ende-Nachweis. Die Queue
wird nicht ebenfalls auf 75 s verlängert, damit belegte Kapazität keine langen
zusätzlichen Warteketten erzeugt. Kleinere explizite
`guardian_timeout_seconds`-Werte bleiben zulässig. Die bestehende Ladenreihenfolge
bleibt: `GUARDIAN_MCP_GUARDIAN_TIMEOUT_SECONDS` vor Optionsdatei vor Default.
Die HA-App-Maske exponiert diesen Wert weiterhin nicht als eigenes Schemafeld.
Ein alter Environment-/Options-Override muss vor Installation geprüft werden;
ein Code-Default beweist nicht die wirksame Laufzeitkonfiguration.

## Abbruch und unveränderte Datenverträge

Ein anfragegebundener Kontext trägt dieselbe monotone Deadline durch die
bestehenden synchronen Reader. Alarmdateien werden mit Prüfungen vor/nach dem
Lesen und der Projektion nur bis zum anfänglich beobachteten Byteende gelesen.
Identitäts-, History-, Hycube-, Display-, Phasen-, Diagnose-, Maintenance- und
SOC-Ereignis-Lesepfade prüfen zwischen ihren Arbeitseinheiten. Research-Lesesperren
für Maintenance/SOC respektieren das Budget; andere Aufrufer behalten ihr
bisheriges Sperrverhalten. Timeout darf nicht als beschädigte Quelle, leerer
Datensatz oder Fallback-Erfolg abgefangen werden.

Die Grenze ist **kooperativ, nicht präemptiv**. Ein einzelner Betriebssystem-
Leseaufruf, eine große JSON-/Gzip-Dekodierung, Sortierung oder andere nicht
unterteilte Bibliotheksoperation kann erst bei Rückkehr geprüft werden. Eine
harte maximale reale Laufzeit von exakt 60 s wird deshalb nicht behauptet.
Keine neuen Threads, Prozesse, Hintergrundjobs oder automatischen Wiederholungen.
Die existierenden Request-Threads behalten ihren Slot, bis die Arbeit tatsächlich
beendet ist. Ein MCP-/Client-Abbruch schließt den MCP-HTTP-Aufruf und gibt dessen
Slot frei; er wird **nicht** als sofortiger Battery-Abbruch weitergeleitet. Ein
bereits gestarteter Battery-Reader läuft höchstens bis zu seiner nächsten
Budgetprüfung weiter (unter dem genannten Vorbehalt blockierender Einzelaufrufe).

Battery zählt abgewiesene Vollauflösungsanfragen nicht mehr fälschlich von anderen
aktiven Anfragen ab. Antwortgrößenprüfung erfolgt schrittweise im Budget; das
Audit serialisiert nicht erneut unter dem Gate-Lock. Slots werden auch bei
Fehlern der Größenprüfung oder Auditführung freigegeben. HTTP-Antwortserialisierung
und Socket-Übertragung nach dem Gate sind nicht Teil seines Verarbeitungsbudgets.

Unverändert: 2 MiB Antwortgrenze, bestehende Zeitbereichs-/Punkt-/Zellgrenzen,
Pagination, Filter, Reihenfolge, Einheiten, Identitätssemantik, SOC-/Gap-Regeln,
Ereignisformat, historische Lesbarkeit und persistierte Bytes. Vollständigkeit
bedeutet weiterhin `full` und alle vorhandenen Folgeseiten, nicht das Entfernen
der Schutzgrenzen. Unbegrenzte Datenmengen sind auch mit 60 s nicht zugesagt.

## Lokale Prüfung dieses Kandidaten

Im vorhandenen isolierten `test-venv`, Produktionspfad-/Netzwerkschutz aktiv,
Bytecode und pytest-Cache deaktiviert:

- 279 gezielte Battery-Tests bestanden (1,90 s), einschließlich aller unmittelbar
  geänderten Reader und ihrer bestehenden Datenverträge.
- 32 MCP-Tests bestanden (3,94 s) mit ausschließlich lokalen Loopback-Testservern.
  Anschließend 5 Budget-Tests bestanden (0,74 s): vier wiederholte Tests plus
  ein neuer Verdrahtungstest. Das sind **33 verschiedene MCP-Tests**, keine 37.
- Kontrollierte monotone Uhr: vollständige unveränderte Antwort mit 174 Alarmen
  nach rechnerisch 52,2 s; Erfolg vor 60 s, Timeout bei/über 60 s. Dies ist kein
  52-s-Lasttest oder produktiver Zeitnachweis.
- Datei-/Mutex-Lesesperren mit kurzen realen Fristen, Dateischließung, Kontext-
  isolation zwischen Threads, Wiederbenutzbarkeit der Slots, abgewiesene
  Vollauflösungsanfragen, fehlgeschlagene Kodierung, 2-MiB-Grenze, unveränderte
  Coverage/Pagination, historische SOC-Ereignisse und MCP-Abbruch ohne Retry.
- Syntaxprüfung aller 19 geänderten/neuen Python-Dateien und `git diff --check`
  bestanden. AST-Vergleich der neun gemeinsam genutzten Reader bestätigt:
  ausschließlich Budgetprüfungen/Weitergabe und begrenzte Research-Lesesperren.

Fehlgeschlagene Anläufe bleiben als Nachweise erhalten: eine Testauswahl enthielt
zunächst einen nicht vorhandenen Dateinamen (keine Tests ausgeführt); der erste
MCP-Protokolllauf durfte in der Sandbox nicht auf Loopback binden (21 bestanden,
11 Bind-Fehler); ein späterer Battery-Lauf fand einen fehlenden Import (276
bestanden, 3 Fehler). Dieser Fehler ist korrigiert und im erfolgreichen
279er-Lauf erneut geprüft. Frühere Ergebnisse gelten nicht als neue Abnahme.

## Gezieltes Abschlussreview vom 2. Oktober 2026

Der Eingangsstand aller 22 Dateien entsprach bytegenau dem gesicherten
Reviewmanifest `Researchbudget-20261002T125639Z`; dessen Prüfsummen wurden
verifiziert. Zwei konkrete Fehler des neuen Alarmreaders wurden korrigiert:

- Das anfängliche Byteende stammt nun aus `fstat` des **geöffneten** Deskriptors,
  nicht aus einem möglicherweise inzwischen ersetzten Pfad. Ein Austausch
  während des Öffnens darf keine unbemerkte Teilantwort erzeugen.
- EOF vor diesem Byteende bricht als Quellenfehler ab, statt eine unvollständige
  Ereignisliste als vollständig zurückzugeben.

Die neuen Regressionstests scheiterten vorher in genau diesen beiden Fällen;
der zusätzliche Auditfehler-Test bestätigte die Slotfreigabe bereits. Nach der
Korrektur bestanden **64 gezielt betroffene Tests** (0,55 s: Budget, Timeline,
Timeline-API, Research-API). Die früheren 279 Battery-/33 unterschiedlichen MCP-
Tests wurden nicht pauschal wiederholt; sie sind Nachweise des unmittelbar
vorherigen Standes. Die 52,2 s bleiben eine simulierte Uhr, keine reale Lastabnahme.
Keine weiteren blockierenden Codebefunde im Review; verbleibende Betriebsgrenzen
und Installationsvoraussetzungen gelten weiter.

Versionsvorschlag für einen gesonderten Releaseauftrag: Battery **0.8.6**,
Research-MCP **0.8.4**, Tunnel **0.8.1**, Diagnostic Engine **0.4.12**.
Die vorgeschlagenen neuen Komponentenkennungen wurden in der lokal erreichbaren
Historie ihrer Versionsdateien nicht gefunden. Keine aktuelle GitHub-Kollisions-
prüfung erfolgt und keine Versionsfreigabe daraus abgeleitet.

Lokale historische Installationsbelege vom 01.10.2026 nennen
`local_guardian_battery` 0.8.5 / `b90c134` und
`3195b09a_guardian_research_mcp` 0.8.3 aus dem damaligen GitHub-main. Das geprüft
archivierte Backup `aefbaf4f` enthält jedoch Battery **0.8.4** / `fd2e708`, Optionen
und `/share`, ausdrücklich kein aktuelles MCP-Image. Ein gemeinsames verfügbares
0.8.5-/0.8.3-Rückkehrpaket ist damit **nicht belegt**, Wiederherstellung nicht
erprobt. Vor einem neuen Update müssen passende Images, Optionen und aktuelle
Daten separat gesichert/nachgewiesen werden; ältere `/share`-Snapshots dürfen
neuere Ereignisse/Herkunft nicht überschreiben. Das Review greift weder auf HA
noch auf Sicherungsarchive oder produktive Daten zu.

Der bisherige MCP-Updateweg benötigt eine separat freigegebene, auf MCP begrenzte
Bereitstellung in seiner tatsächlichen Repositoryquelle. Ein nur lokaler Commit
macht die neue MCP-Version dort nicht verfügbar. Keine Übernahme des gesamten
Battery-Branches nach main, keine neue lokale MCP-Installation mit anderer Identität.
Release-/Bereitstellungsfreigabe und anschließender kontrollierter gemeinsamer
Updateauftrag sind deshalb konkret zu trennen. Kein solcher Schritt erfolgt hier.

## Äußere Grenze und spätere Abnahme

Am 02.10.2026 wurden die offiziellen Seiten
[Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)
und [MCP servers](https://developers.openai.com/api/docs/guides/tools-connectors-mcp)
geöffnet. Sie belegen den Transportweg, nennen aber keine für diese Installation
belastbare äußere Request-Frist. Die lokale Tunnel-Konfiguration pinnt
`tunnel-client` 0.0.14; daraus wird keine aktuelle Dienstfrist abgeleitet.

Vor Produktmaßnahmen sind ein prüfbarer Release-/Buildstand, passende
Battery-/MCP-Rückkehrartefakte und eine gesonderte Installationsfreigabe nötig.
Battery und MCP benötigen gemeinsam die Änderungen; Tunnel und Diagnostic Engine
bleiben unverändert. Keine Datenrückspielung, keine Änderung der importierten
70 Ereignisse oder ihrer Herkunft. Keine SSD-Aktionen. Die temporäre Diagnose-App
bleibt gestoppt und geschützt.

Nach einer gesondert freigegebenen Installation: zuerst effektive Budgets prüfen,
dann einen vollständigen bekannten Abruf seriell über den tatsächlichen
Guardian-Connector ausführen und Anzahl/Identitäten/Zeitfenster/Kürzungsstatus
mit dem vorhandenen Nachweis vergleichen. Betrieb nur anhand bestehender
Status-/Timingdaten beobachten. Lokale Tests ersetzen diese Abnahme nicht.
Bricht die äußere Verbindung vorzeitig ab, ist das 60-s-Ende-zu-Ende-Ziel nicht
abgenommen. Als bestehende Alternative kleinere, zusammen lückenlos abdeckende
Zeitfenster seriell abrufen und vorhandene Cursor verfolgen; keine parallelen
Retry-Stürme, keine stille Datenkürzung, keine neue Release-Infrastruktur.

---

# Research-Reader: lokale I/O-Korrektur vom 1. Oktober 2026

Status: lokal vorbereiteter Battery-0.8.5-Kandidat auf Battery-Quellstand
`fd2e708d8cf3900322481d8b8a0183cc85f7dd56`; Versionskennungen für den Kandidaten auf 0.8.5.
Installiert sind Battery 0.8.4 und MCP 0.8.3; die externe Live-Abnahme
dieser Korrektur steht aus. Die gespeicherten SOC-Ereignisse und ihre
Erkennungsregeln werden nicht verändert.

## Befund und Grenze der Ursachenzuordnung

Ein externer `query_cell_history`-Abruf für H221005E22212538,
24.09.2026 20:05–20:10 UTC, alle 15 Zellen, `full`, `max_points=1000`,
scheiterte erneut nach 12.107 Sekunden mit `timeout`, `origin=guardian_api`.
Das ist ein technischer Abbruch, kein Beleg für fehlende Messwerte.

Ein isolierter lesender Aufruf des unveränderten Readers im vorhandenen
HA-Studio-Code-Server lieferte dagegen alle 75 Werte in 0.967 Sekunden.
Die Tagesdatei hat 4.165.571 Bytes / 5.667 Zeilen. Ihr vorhandener Index
wurde dort wegen abweichender Dateisignatur verworfen; insbesondere war
`source_device` 45832 gegenüber aktuell 2049. Keine Prüfung von Datenträgern
oder Mounts, keine Indexreparatur. Ob der Battery-Container exakt dieselbe
Gerätekennung sieht, wurde nicht direkt festgestellt. Der Fallback bleibt
für fehlende oder ungültige Indizes erforderlich und unverändert zulässig.

Der Reader machte je Zeile `BufferedReader.tell()` und nutzte kleine
Standard-Lesepuffer. Viele I/O-Systemaufrufe geben den Python-GIL frei
und können beim Wiedererlangen mit der im selben Prozess laufenden
Datenerfassung konkurrieren. Lokal an der gesicherten Tagesdatei mit einem
konkurrierenden CPU-Thread gemessen: vorher 3.014 Sekunden, nachher 0.239
Sekunden, jeweils dieselben 75 Werte. Diese Messung belegt den unnötigen
I/O-Aufwand und seine Verstärkung unter Last; sie ist kein direktes Profil
des laufenden Battery-Prozesses und beweist nicht die alleinige Ursache
jedes produktiven Timeouts.

## Eng begrenzte Änderung

Nur `ResearchTimeseriesService._records`: 256-KiB-Lesepuffer und Fortschritt
anhand tatsächlich gelesener Bytezahlen statt Dateipositionsabfrage pro
Zeile. Vorhandene Indexbereiche, Fallback, Reihenfolge, Zeitfilter,
Identitätsbezüge, fehlende Werte, Deadline und Dateischließung bleiben
erhalten. Ein Regressionstest begrenzt die tatsächlichen Raw-I/O-Aufrufe
bei 6.000 Zeilen. Keine neuen Threads, Jobs, Caches oder Indexschreibungen.
Zellenausgabe entsteht weiterhin in einem gemeinsamen Scan, nicht in 15 Scans.

Zeitgrenzen bleiben: Reader-Deadline 10 s, nachgelagerte Battery-Hardprüfung
15 s; MCP-Transport und MCP-Warteschlange jeweils höchstens 15 s laut Code.
Die Hardprüfung ist kein präemptiver Prozessabbruch. Der Reader prüft
weiterhin jede Zeile und schließt bei Timeout seine Datei; es werden keine
Hintergrundscans gestartet. Die beobachteten Add-on-Optionen enthalten
keinen eigenen `guardian_timeout_seconds`-Wert (null); der Code-Default ist
15 s. Ein möglicher Laufzeit-Environment-Override wurde nicht inspiziert.
Der Tunnel setzt maximal zwei parallele Anfragen; sein 30-s-Startup-Wait
ist keine Abfragefrist. Eine zusätzliche harte Grenze des externen
Connectors/Tunnelservices ist nicht belegt. Der reproduzierte Fehler kam
aus der Battery-API, nicht aus einem nachgewiesenen Tunnel-Abbruch.

## Tatsächlich ausgeführte lokale Prüfungen

Bestehendes isoliertes test-venv, Produktionspfad-/Socket-Schutz aktiv,
Bytecode und pytest-Cache deaktiviert:

- 74 gezielte Battery-/Reader-/Index-/Coverage-Tests bestanden (2.44 s).
- 28 MCP-Tests bestanden (3.42 s), einschließlich Timeout-Weitergabe,
  Client-Abbruch und Warteschlangenfreigabe. Der erste Sandbox-Anlauf
  scheiterte ausschließlich an 11 lokalen Server-Binds; 17 bestanden.
  Der vollständige fehlgeschlagene Lauf bleibt im Nachweisordner erhalten.
- Gesicherte Daten: Spreizung 40 min (41 Werte), Strom 15 min (15 Werte),
  15 Zellen / 5 min (75 Werte): exakte Gleichheit aller Payloadfelder mit
  dem bisherigen Reader. Neuer Reader unter CPU-Konkurrenz: 0.163 / 0.185 /
  0.224 s; kein Timeout, keine Kürzung.
- Sieben Tage / 15 Zellen: 149.970 Quellpunkte, erste Seite 1.000 Punkte,
  3.317 s unter CPU-Konkurrenz. Erste und zweite Seite exakt wie bisher,
  ohne Überschneidung; es wurden nicht alle 150 Seiten abgerufen.
- Lokaler MCP-Protokollweg mit tatsächlicher Battery-API und gesicherten
  Daten (HTTP-Backendadapter im Test): dieselben drei Problemfenster
  erfolgreich, Payload exakt, keine Kürzung. End-to-End 4.060 / 0.358 /
  0.383 s einschließlich Protokollaufwand; aktive Queries danach null.
- Synthetische Tests für vollständige Pagination über alle 15 Zellen,
  leere Reihen, fehlende Metriken ohne Nullauffüllung, ungeordnete Zeilen,
  offene Indexenden und Datei-/Gate-Freigabe bei Timeout.

Diese lokalen Nachweise ersetzen keine externe Abnahme auf HA. Vorhandene
Pagination liest pro Seite weiterhin den Quellbereich erneut; die
Materialisierung großer Fenster und die 2-MiB-Antwortgrenze wurden nicht
neu gestaltet. Für unveränderte vollständige Werte `resolution=full`
explizit verwenden und vorhandene `next_cursor` bis zum Ende verfolgen.
Die Prüfung verwendete `max_points=1000`; größere Seiten können weiterhin
die bestehende Antwortgrenze erreichen. Ein Timeout darf niemals als
leere Datenreihe oder fachlicher Ausschluss eines SOC-Events gelten.

## Spätere Installations- und Abnahmevorbereitung

Nur das Battery-Paket benötigt den Reader-Fix; MCP, Tunnel und Diagnostic
Engine benötigen nach diesem Diff keine Änderung. Noch kein Build,
Commit, Push oder Deployment dieses Kandidaten ausgeführt.
Ein freigegebener Release-/Buildstand und kontrollierter Battery-Stopp /
Wiederanlauf sind vor einer späteren Installation getrennt zu planen.
Die 70 importierten Ereignisse, alle neueren Ereignisse sowie ihr
Herkunftsnachweis müssen unverändert erhalten bleiben. Keine Datenrückspielung.

Externe Abnahme nach einer separat autorisierten Installation: die drei
genannten Fenster über den vorhandenen Guardian-Connector seriell mit
`full` / `max_points=1000` wiederholen; Identitäten, Zeitbezüge, Einheiten,
41/15/75 Werte, Kürzungsstatus und exakte Payloads mit dem Nachweis prüfen.
Anschließend einen größeren Bereich mit expliziten Folgeseiten prüfen.
Regulären Collector anhand vorhandener Logs kontrollieren; kein Lasttest
und keine zusätzliche Batterieabfrage. Bei erneutem Timeout ist die
Korrektur nicht abgenommen; nicht auf lokale Dateien als Analyseersatz
ausweichen und keine Zeitgrenzen ohne neuen Befund erhöhen.

## Abschlussreview und genehmigte Versionsvorbereitung

Zehn zusätzliche Grenzfalltests bestanden (0.21 s), ohne Wiederholung der
vorherigen 74 Battery-/28 MCP-Tests: UTF-8/CRLF, letzte gültige/ungültige Zeile,
getrennte Indexbereiche, Append nach Bereichswahl, Rotation nach Öffnen sowie
Deadline mitten im Puffer und explizites Schließen. Keine neue blockierende
Abweichung durch den Diff gefunden. Keine atomare Snapshotgarantie für eine
Ersetzung zwischen Signaturprüfung und Öffnen oder Änderungen innerhalb einer
Datei; diese vorhandenen Grenzen wurden nicht neu gestaltet.

Die Versionsvorbereitung 0.8.5 ist genehmigt. Vor einer Installation aktuelle
0.8.4-Rückkehrsicherung und vollständigen Buildkontext mit finaler Commit-SHA
prüfen. Live-Abnahme seriell über den vorhandenen Connector: drei oben benannte
Fenster; anschließend dasselbe 75-Punkte-Zellfenster mit max_points=20 vollständig
über vier Seiten (20/20/20/15) abrufen, Payloadgleichheit und regulären Messbetrieb
belegen. Keine Erfolgsaussage vor dieser Abnahme; keine weitere Produktionsaktion
wird durch dieses Dokument allein autorisiert.
