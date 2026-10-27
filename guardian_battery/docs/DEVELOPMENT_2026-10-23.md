# Lokaler Entwicklungsstand vom 23.10.2026 – unveröffentlicht

Basis: Worktree `codex/battery-p0-history-087`, Commit
`75c5d14ff33b3ed8fece32c6ff9e18238c1c95fe`. Die vorhandenen begrenzten Leser,
Zeitbudgets, Messpriorität und manuelle UI-Bedienung werden wiederverwendet.
Diese Basis enthält einen unveröffentlichten 0.9.0-Kandidaten; sie ist kein
Beleg für einen bewährten Produktivstand. Keine neue Versionskennung,
Installation, Migration, Hardwareabfrage oder produktive Änderung in dieser Runde.

## Erfassung und spätere Optionsumstellung

| Option | Bisheriger Vorgabewert | Neuer Vorgabewert |
|---|---:|---:|
| `poll_interval_seconds` (direkte PWR-Abfrage) | 10 | 20 |
| `hycube_interval_seconds` (Systemabfrage) | 5 | 20 |
| `cell_diagnostics_interval_seconds` (Zellen) | 60 | 60, unverändert |

Manifest, Konfigurationsoberfläche und Hycube-Laufzeitfallback stimmen überein.
PWR verwendet die geladene Option, keinen zweiten neuen Laufzeitdefault.
**Bestehende HA-Optionen ändern sich durch neue Defaults nicht.** Bei einer später
freigegebenen Installation dieser Anlage sind die tatsächlich gespeicherten
Optionen zunächst zu lesen und zu sichern. Anschließend sind ausschließlich
`poll_interval_seconds: 20` und `hycube_interval_seconds: 20` ausdrücklich zu
setzen, Zellen bleiben 60. Nach dem dann freigegebenen Wiederanlauf sind wirksame
Optionen und tatsächliche Messabstände zu prüfen. Hier wurde nichts davon ausgeführt.

## Begrenzte manuelle Abfragen

Der bisherige Research-Pfad konnte ohne aktiven Historienkontext auf einen
vollständigen Rohdateiscan zurückfallen. Dieser Rückfall wurde entfernt:
`bounded_ranges` und `range_lines` lesen bestehende Indexbereiche bzw. höchstens
einen unindexierten Block (256 Datensätze). Fehlende/veraltete größere Indizes
melden `history_index_required`; eine Anfrage baut keine Indizes auf.

UI und die drei MCP-Werkzeuge `query_module_history`, `query_cell_history`,
`query_timeseries` benutzen jetzt denselben Zeitseitenadapter. Eine Anfrage
verarbeitet höchstens eine Stunde eines maximal 90 Tage langen Zeitfensters.
Die bisherigen Punkt- und Antwortgrößenlimits sowie Zeitbudgets gelten weiter.
`auto` richtet seine Full-/Display-Auswahl nach dem ursprünglichen Gesamtfenster.
Eine explizite Full-Abfrage kann über Seiten auch mehr als sieben Tage umfassen;
eine einzelne Verarbeitung bleibt auf eine Stunde beschränkt.

Der äußere `next_cursor` ist signiert und an Endpunkt und vollständige normalisierte
Abfrage gebunden. Auch `data.next_cursor` enthält diesen äußeren Cursor, sofern
vorhanden. Bestehende innere Punktseiten werden erst vollständig abgearbeitet,
bevor die nächste Zeitseite beginnt. Leere Seiten können eine Fortsetzung haben.
Clients wiederholen nur auf ausdrücklichen Wunsch dieselben Argumente mit dem
neuen Cursor. Ein veränderter Filter benötigt einen Neustart ohne Cursor.
Nach Prozessneustart können Cursor ungültig sein. Es gibt weder automatische
Folgeanfragen noch einen Cache oder einen über mehrere Seiten eingefrorenen Datenstand.
Änderungen einer Quelldatei können einen inneren Cursor ungültig machen.

`pagination.requested_range` beschreibt die Gesamtauswahl, `page_range` nur die
aktuell gelesene Strecke. Coverage und Lücken gelten ausschließlich für diese
Strecke; ungelesene Zeit ist kein nachgewiesener Ausfall. Grenzen sind in UTC auf
ISO-Mikrosekundenauflösung inklusiv und überschneidungsfrei. Die nächste Zeitseite
beginnt eine Mikrosekunde nach dem Ende der vorherigen. Keine Interpolation.

Die UI zeigt jeweils eine Teilstrecke und bietet „Nächste Zeitstrecke laden“.
Sie sammelt nicht unbegrenzt alle Seiten im Browser. Eine neue Auswahl startet
wieder am Anfang. Abbruch, verspätete Antworten und Fehler erhalten den bisherigen
Schutz; auch der Research-Pfad ohne vorbereiteten Scheduler beachtet nun explizite
Abbruchsignale und Fristen. Direkte ältere API-Aufrufe ohne `paged=true` behalten
ihren bisherigen Umfang; die Blockbegrenzung gilt dennoch.

Sehr lange Übersichten benötigen entsprechend viele manuelle Seiten. Diese Runde
liefert keine neue Gesamtfenster-Aggregation und keine automatische Suchfunktion
zum nächsten belegten Zeitraum.

## Datenherkunft und Last

Messzeit, Modul-/Zellidentität und Identitätsepoche bleiben erhalten.
Research-Punkte geben zusätzlich vorhandene PWR-Alter-/Qualitätsangaben,
Empfangszeit und zellbezogene Balancinganzeigen weiter. Fehlende Empfangszeit
bleibt `null`/`unavailable`; Messzeit wird nicht als Empfangszeit umbenannt.
PWR- und Zellmesszeit bleiben getrennt. Der PWR/BAT-Abstand ist nur dann belegt,
wenn die gespeicherten Zeitangaben ihn tragen. BMS-Schutzflags sind in diesem
Zellarchiv nicht aufgezeichnet und werden ausdrücklich als nicht vorhanden
gekennzeichnet. Vorhandene separate RS485-/BMS-Lesepfade bleiben unverändert;
keine zusätzlichen Abfragen an der Batterie. Modulspannung/-temperatur und
Zellabweichung behalten ihre bestehende Kennzeichnung als abgeleitete Werte.

Die vorhandene Erfassung verwendet `PeriodicDeadline` und räumt historische
Arbeit über `history_idle_until` in freie Zeit. Analyse, Diagnosepersistenz und
abgeleitete MQTT-Ausgaben liegen bereits in getrennten Workern; MQTT verwirft
wiederholte identische Generationen. Diese Mechanismen wurden nicht neu gebaut.
Reduzierte System-/PWR-Frequenz und begrenzte Lesefenster verringern die angebotene
Last. Daraus folgt keine Garantie für rechtzeitige Hardwareantworten oder langsame
Datenträger. Ursachenanalyse und Ladeempfehlungen bleiben extern, keine Ladeautomatik.

## Gezielte lokale Validierung

Testschutz: vorhandenes `work/test-guard/sitecustomize.py`, isoliertes Test-venv,
keine Bytecode-/pytest-Cache-Dateien. Produktive Pfade `/share`, `/data`, `/config`,
`/backup` und Verbindungen außerhalb Loopback sind gesperrt.

- 6 Battery-/UI-Tests bestanden: Seiten-/Cursorgrenzen, innere Fortsetzung,
  90-Tage-Fenster, leere Seiten, Abbruch/Wiederholung, Datenlücken und fehlende
  Signale, Defaults, ausgeführte UI-Lade-/Abbruch-/Weiterladelogik.
- 5 bestehende MCP-Budget-/Abbruchtests bestanden.
- 1 neuer echter MCP-Protokolltest bestanden: zwölf Aufrufe für sechs synthetische
  Module mit Fortsetzung, einschließlich leerer Seiten, gleichzeitig mit
  simulierter Erfassung. Transport lokal über Loopback; Battery-Adapter lokal.
- Python-Syntax aller 11 geänderten/neuen Python-Dateien, vollständiger erzeugter
  UI-JavaScript-Code und `git diff --check` ohne Befund.

Messung mit 30 synthetischen Archivtagen: drei UI-Abfragen 20,3–27,4 ms,
18 Research-Abfragen 6,7–12,2 ms. Pro Anfrage wurden 1.932 Byte / 12 Datensätze
im Zellarchiv gelesen, jeweils eine Datei; diese Bytezahl umfasst weder
Hycube-/Metadatenzugriffe noch Netzwerkverkehr. Abgefragt wurden 24 Stunden in
der UI und 29 Tage im Research-Pfad, verarbeitet wurde jeweils die erste Stunde.

Parallel entstanden 570 synthetische Modulproben in 95 Scheduler-Durchläufen.
Der beschleunigte Simulationstakt war 2 ms; maximale zusätzliche Verzögerung
1,98 ms. Beim echten MCP-Protokolltest: 12 Aufrufe, 127 Durchläufe / 762 Proben,
maximale zusätzliche Verzögerung 6,91 ms bei ebenfalls 2 ms Solltakt.
Dies ist ein Test der Zusammenarbeit mit priorisierten Erfassungsabschnitten,
keine Echtzeitgarantie und kein Hardware-/Datenträgerbenchmark. Die simulierten
Proben liegen im Speicher; dauerhafte Speicherung parallel zur Erfassung sowie
Netzwerktunnel-Latenz wurden nicht gemessen. Keine produktive Taktabnahme.

Der erste Battery-Lauf hatte drei Testaufbaufehler (fehlendes Identitätsbackend,
falscher Optionsname, fehlender UI-Knopfstub); nach konkreter Korrektur 6/6.
Der erste MCP-Protokollversuch scheiterte an der Sandbox-Portfreigabe, nicht am
Protokoll. Nach Freigabe ausschließlich des lokalen Tests bestanden. Danach wurde
nur dieser Test um gleichzeitige Erfassung ergänzt und erneut ausgeführt.
Keine vollständige Testsuite und keine Controller-Prüfschleife.

Vorhandene Protokolle dieser Runde:
- `/tmp/guardian-development-20261023-battery.log` (erster Lauf)
- `/tmp/guardian-development-20261023-battery-corrected.log` (6 bestanden, Messwerte)
- `/tmp/guardian-development-20261023-mcp.log` (5 bestanden, Portblockade)
- `/tmp/guardian-development-20261023-mcp-loopback.log` (Protokolltest bestanden)
- `/tmp/guardian-development-20261023-mcp-concurrent.log` (finaler Protokolltest)
Diese temporären Protokolle sind keine dauerhaft gesicherte Releaseakte.

## Spätere Installation und Rückkehr – noch nicht freigegeben

Vor Entscheidung tatsächlichen HA-Stand, verfügbare Installationsartefakte und
den nachgewiesenen Umfang vorhandener Sicherungen erneut feststellen. Insbesondere
ist der frühere Wiederherstellungsabschluss kein hier bestätigter Zustand.
Battery und MCP benötigen für die neue Seitensemantik einen abgestimmten Stand;
Clients müssen `next_cursor` und seitenbezogene Coverage verstehen. Der Tunnel
bleibt unverändert. Keine vorhandene 0.9.0-Migration automatisch starten.

Erst nach gesonderter Freigabe: geprüfte Originaldaten und bisherige Optionen
sichern, verfügbaren bisherigen Battery-/MCP-Artefaktstand für die Rückkehr
festhalten, den zusammenpassenden Kandidaten installieren, Optionen wie oben
umstellen und reale Messabstände sowie manuelle UI-/MCP-Abfragen abnehmen.
Bei Rückkehr die nachgewiesenen bisherigen Artefakte und Optionen verwenden;
Originaldaten erhalten. Quellcode allein ist kein verfügbares Rückkehrartefakt.
In dieser Runde kein Build, Commit, Push, Deployment oder HA-Zugriff.


## Ergänzung 23.10.2026: lokale Fortsetzungssicherung, nicht installiert

Basis: Battery 0.9.1, Commit 530fdfa8bd53780a54eb294ded9b7ae57b28d4d1.
Die Ergänzung ist uncommittet. Die bisherigen 20s/20s/60s- und manuellen
History-Seitenänderungen bleiben erhalten. Keine fachlichen Diagnoseformeln,
SOC-Regeln, Rohdatenformate oder externen Schnittstellen geändert. Kein HA-Zugriff,
keine Dienste gestoppt, kein Merge/Push; MCP-PR #4 bleibt unverändert.

### Tatsächliche Änderungen

- `resume_publication.py`: dauerhafte, prüfsummengeschützte Schreibabsicht für
  einen abgeleiteten Speicherabschnitt. Dateien werden mit fsync und atomarem
  Ersetzen gesichert; Fortschritt zuletzt. Wiederaufnahme beendet denselben
  Abschnitt idempotent. Angefangene Anhänge werden gegen ihren gespeicherten
  Bytebereich geprüft und nur um den fehlenden Rest ergänzt. Keine Rohdatei wird
  durch diesen Mechanismus zurückgesetzt. Maximal 64 MiB serialisierte Absicht;
  Überschreitung wird ausdrücklich abgelehnt.
- Anzeigeprojektion: kein pauschales Löschen des offenen Tages beim Start.
  Gzip-Anhänge, SQLite-Fenster, Metadaten und offene Buckets/Quelloffsets bilden
  einen wiederholbar abschließbaren Abschnitt. Ein gültiger Neustart liest keine
  bereits verarbeiteten Rohzeilen. Ungültiger Zustand oder geänderte Altquelle
  wird mit konkretem Grund für den betroffenen Tag gemeldet; kein stiller Reset.
- Kanonische Phasen: Tagesartefakt, Fensterindex und laufende Zustandsmaschinen
  einschließlich Offset werden zusammen wiederanlauffähig veröffentlicht.
  Beschädigte oder unpassende Tageszustände werden nicht still ab Offset null
  neu berechnet.
- Hycube-Projektion: im vorhandenen Standardpfad konsistente Veröffentlichung
  von Anhang und bestätigtem Rohdaten-Endoffset, sowohl live als auch im bestehenden
  Nachführungsworker. Der separat vorbereitete Backend-Pfad bleibt erhalten.
- Diagnose/ Aggregate: gemeinsame gesicherte Ergebnisdateien mit Prüfsummen,
  Quellenpositionen, relevanter Diagnosekonfiguration und der tatsächlich
  zugrunde liegenden Modulzuordnung. Der bestehende Persistenzworker speichert
  diesen Verbund regelmäßig und leert seine Warteschlange beim begrenzten Stop.
  Ein Neustart prüft gespeicherte Ergebnisse und Quellenmetadaten; gültige alte
  Rohdaten werden nicht erneut gelesen. Neue Rohdaten werden ab dem bestätigten
  Punkt nachgeführt. Laufende Erfassung liefert ihre Schreibpositionen mit.
- Tagesdiagnostik: Wiederverwendung vorhandener Ergebnisse anhand der vollständigen
  Quellenmetadaten einschließlich 14-Tage-Risikozeitraum und Nicht-Datumsdateien.
  Bei geänderten Metadaten bleibt die bisherige semantische Prüfung maßgeblich;
  eine irrelevante Änderung erzwingt damit keine globale Neuberechnung.
- Rohdatenindizes: bestehende Fortschrittsdateien werden nun dauerhaft atomar
  geschrieben. Der gespeicherte History-Auftrag erhält zusätzlich die
  Verzeichnis-Synchronisierung nach seinem bereits vorhandenen fsync/replace.

### Grenzen und Installation

Dies ist KEINE nachgewiesene Komplettlösung für jeden vorhandenen Altbestand.
Ohne neuen gemeinsamen Diagnose-Checkpoint ist nur eine begrenzte initiale
Aufbereitung bis 1 MiB Rohdaten zugelassen; größere Bestände werden ausdrücklich
blockiert, während Rohdatenerfassung weiterlaufen kann. Auch ein nachzuholender
Diagnoseabschnitt über 1 MiB pro Quelldatei oder eine geänderte/entfernte Altquelle
führt zu einem sichtbaren Grund statt automatischem Vollscan. Die Übernahme eines
produktiven 0.8.9-Altbestands in einen solchen konsistenten Checkpoint ist damit
noch separat zu klären, nicht durch diesen Patch heimlich als Migration aktiviert.

Die vorhandenen Stop-Timeouts bleiben bestehen. Unterbrochene Veröffentlichungen
bleiben zur Wiederaufnahme erhalten; das ist keine Garantie gegen defekte
Datenträger, ausbleibende Betriebssystem-I/O oder einen Ausfall von HA selbst.
Die Integration der einzelnen Worker wurde gezielt geprüft, nicht ein echter
Stromausfall und nicht die gesamte produktive Shutdown-Reihenfolge.

Der gespeicherte History-Auftrag und seine spezialisierten Staging-/Indexschritte
wurden nicht vollständig neu implementiert oder an jeder Absturzstelle geprüft.
Auch die mehrteilige Tagesdiagnose-Veröffentlichung bleibt der bisherige
idempotente Ablauf; der neue Nachweis dort betrifft Quellenprüfung und Wiederverwendung,
nicht eine neue gemeinsame Transaktion über sämtliche Diagnose-Ereignisdateien.
Fehlende/ungültige Teilzustände werden nicht als erfolgreiche Fortsetzung behauptet.

### Gezielte Nachweise dieser Ergänzung

Vorhandene isolierte Testumgebung und Testschutz, keine produktiven Pfade oder
Netzwerkzugriffe. Keine vollständigen Battery-/MCP-/Tunnel-Suiten erneut gestartet.

- Fortsetzung/Absturzgrenzen, Diagnose-Startup und Persistenz: 36 bestanden.
  Enthält geordneten Stop, Abbruch nach Schreibabsicht/Teilpublikation,
  Wiederholung ohne doppelte Anhänge, laufende Phasenzustände, ausschließlich
  neue Rohzeilen, beschädigte Cache-Paare, nachträglich geänderte Quellen,
  große Erstbestände und gleichzeitiges Anhängen während der Initialisierung.
- Bestehende Hycube-Projektionsprüfungen: 22 bestanden; bestehende inkrementelle
  Phasenprüfung: 1 bestanden (im gemeinsamen früheren 44-Test-Lauf enthalten).
- Betroffene Anzeige-Neustart-/Fortsetzungsprüfungen: 13 bestanden.
- Betroffene Tagesdiagnose-/Stop-/Nachlieferungsprüfungen: 8 bestanden.
- Bestehende History-Auftrags-/Index-Fortsetzung und Tagesdiagnose-Schreibfehler:
  10 gezielte Fälle bestanden. Ein Test musste seine simulierte Unterbrechung
  auf den neuen Fenster-Schreibweg richten; danach bestand auch die tatsächliche
  Staging-Wiederaufnahme ohne doppelte Buckets. Keine weitere Prüfausweitung.
- Python-Syntax der 17 betroffenen Battery-Python-Dateien und git diff --check
  bestanden. Frühere Fehlerläufe sind keine Abnahmebelege.

Protokolle dieser lokalen Sitzung: /tmp/guardian-restart-diagnostic-final.log,
/tmp/guardian-restart-frontier-tests.log, /tmp/guardian-restart-display-tests.log,
/tmp/guardian-restart-daily-tests.log. /tmp ist keine dauerhafte externe Sicherung.

Ergänzende Protokolle: /tmp/guardian-restart-existing-jobs.log (9 bestanden,
1 zunächst nicht ausgelöste Fehlerstelle), /tmp/guardian-restart-staging-final.log
(dieser korrigierte Unterbrechungsnachweis: 1 bestanden). Insgesamt 90 unterschiedliche
gezielte Testfälle mit abschließend bestandenem Ergebnis; keine vollständige Suite.

### Nachtrag: die beiden offenen Fortsetzungs-/Stop-Lücken lokal geschlossen

Dieser Nachtrag ersetzt die oben genannten Einschränkungen „Erstbestand über
1 MiB blockiert“ und „neuer Rest über 1 MiB blockiert“. Er ist ein neuer lokaler
Prüfstand, keine nachträgliche Erweiterung der damaligen 90 Testergebnisse.

- Fehlende oder ungültige Diagnose-Zwischenstände führen jetzt zur sichtbaren
  Aufbereitung mit Grund, Quellanzahl, Fortschritt und Einheitenbudget. Dafür
  wird die vorhandene SQLite-Slot-Zusammenführung dauerhaft fortsetzbar genutzt
  (`.diagnostic-resume-preparation/merge.sqlite`). Rohdaten werden nicht verändert.
- Eine Einheit liest höchstens 256 Datensätze und 256 KiB. Quelloffset, sortierbare
  Zellzustände und bisherige Aggregate werden gemeinsam transaktional gesichert.
  Stop innerhalb einer Einheit verwirft nur deren unbestätigten Fortschritt;
  bestätigte Einheiten werden beim nächsten Start nicht erneut gelesen.
- Der bestehende Live-Prioritätsmechanismus bleibt wirksam. Ein großer neuer
  Rest wird in begrenzten Einheiten mit dem vorhandenen Publikationsjournal
  gesichert. Bei einer langen Aufbereitung kann eine durch Raw-Schreibbelege
  gedeckte Live-Warteschlange aus der gesicherten Rohhistorie nachgezogen werden,
  statt wegen ihrer Kapazität dauerhaft stehenzubleiben. Ohne Raw-Beleg bleibt
  der vorhandene Fehlerfall sichtbar; es werden keine Samples still verworfen.
- Gültige private Zwischenstände werden wiederverwendet. Passend belegte
  unveränderte Legacy-Quellen können ebenfalls übersprungen werden. Bei einem
  ungültigen gemeinsamen Cache-Paar oder veränderten Quellen ist dessen Zustand
  nicht sicher je Quelle subtrahierbar: Die gemeinsame Diagnose wird privat aus
  den verfügbaren Quellen aufbereitet. Die bisherigen öffentlichen Cache-Dateien
  bleiben bis zur vollständigen Ersatzberechnung erhalten. Andere Projektionen
  und Rohdateien werden dabei nicht zurückgesetzt.
- Die Stop-Reihenfolge signalisiert unabhängige Worker zuerst und wartet dann
  mit ihren bestehenden Zeitgrenzen. RS485-Speicherung endet erst nach dem
  bestätigten Ende ihres Lieferanten. Noch laufende Threads bleiben bei einem
  Timeout referenziert; dieser wird als unvollständiger Stop gemeldet.
- Der Hycube-Thread führt seinen letzten Projektions-Flush selbst nach dem Ende
  der Erfassung aus. Kein konkurrierendes Schließen durch den Hauptthread.
  Der RS485-Writer berücksichtigt auch einen bereits aus der Queue genommenen
  Restbatch. Diagnose-Persistenz leert aktive und neueste ausstehende Generation;
  Schreibfehler werden nicht als erfolgreicher Stop gemeldet.

Gezielte neue Prüfung mit bestehender isolierter Testumgebung/Testschutz:
**16 bestanden in 4,48 s**: 14 neue Fälle ausschließlich zu diesen beiden Lücken
und zwei entsprechend ersetzte Erwartungen (großer Erstbestand, beschädigtes
Cache-Paar). Die bisherigen 90 Fälle wurden nicht erneut als Suite ausgeführt.
Nachweis: `/tmp/guardian-restart-gaps-complete.log`. Frühere Versuche:
`/tmp/guardian-restart-gaps-tests.log` (4 fehlgeschlagen, 5 bestanden), danach
`/tmp/guardian-restart-gaps-final.log` (9 bestanden). Die ersten Fehler betrafen
u. a. Testreferenzspeicherung, tagesbezogene Zählererwartungen und die überprüfbare
Unterbrechungsstelle beim Wiederverwenden eines vollständigen Zwischenstands;
maßgeblich ist der abschließende 16-Fall-Lauf. Keine rekonstruierten Ergebnisse.

Fortbestehende Grenzen: bestehende Inventar-, Zeilen-, Cache- und SQLite-Budgets;
kein Nachweis auf echter HA-Hardware, bei Stromausfall oder defektem Datenträger.
Ein dauerhaft hängender I/O-Aufruf oder ein erzwungenes Prozessende nach Timeout
ist keine zugesicherte erfolgreiche Speicherung. Rohhistorie und bereits durable
Publikationsabsichten bleiben die Grundlage der Wiederaufnahme. Die Aufbereitung
verwendet nur vorhandene Quellen; sie kann fehlende Rohdaten nicht rekonstruieren.
Kein neues Erfassungsintervall, keine Diagnoseformel und keine API geändert;
keine Versionsanhebung, Installation oder GitHub-Aktion in diesem Nachtrag.

## Lokale Folgekorrektur: Messabfragen bei laufender Erfassung (23.10.2026)

Ausgangscommit: `6b5c82d0a8b4bd44b3d5918e5f85a1d09d4f7bf2` auf
`codex/battery-p0-history-087`. Dieser Nachtrag beschreibt uncommittete lokale
Änderungen, keine neue Veröffentlichung, Installation oder HA-Abnahme.
Die vorgefundenen MCP-Paging-Änderungen in README, tools.py und
`tests/test_manual_history_paging.py` bleiben erhalten.

### Ursache und enger Änderungsumfang

Der normale Research-Pfad ohne vorbereiteten Scheduler verwendete dieselbe
sofort abbrechende Historical-Zulassung wie Hintergrundarbeit. Ein aktiver
Collector-Zyklus oder Raw-Rückstau führte zu `live_acquisition_priority`;
ein belegter historischer Slot zu `history_busy`. Der Request wurde zudem auf
das Ende des aktuellen Collector-Leerlaufs begrenzt. MCP kannte diese Codes
nicht und meldete stattdessen pauschal `source_unavailable`.

Der lockfreie, bereits durch Block-/Byte-/Metadatengrenzen geschützte
Cell-History-Leser wartet nun kooperativ innerhalb des bestehenden gesamten
Requestbudgets. Das gilt für `module-history`/`timeseries` mit Quelle
`guardian.cell_history`, `cell-history` und reine Coverage-Auswahlen aus
`soc,module_voltage,module_current,cell_voltage,cell_temperature`.
Bei Live-Arbeit oder Raw-Rückstau pausiert er am vorhandenen Record-Prüfpunkt
und setzt denselben Request fort. Kein Wiederholen/Neustarten des Scans,
kein automatischer Folgeabruf. Die 10-ms-Warteschritte sind eine lokale
Koordinationsgranularität, kein gemessenes HA-Latenzbudget.

Der vorhandene exklusive Historical-Slot bleibt erhalten. Raw-Schreiber
verwenden diesen Slot nicht und werden durch das Warten nicht blockiert.
Der Diagnose-Startworker gibt wartenden manuellen Messabfragen an seinem
bestehenden kooperativen Prüfpunkt Vorrang. Andere Projektionen, Phasen,
Diagnose-/Evidence-Inhalte und der Chart-Pfad bleiben unverändert. Keine
Änderung an Erfassungsintervallen, Datenformaten, fachlichen Formeln,
Identitäten, Tunnel, Authentifizierung, Migration oder autoritativen Daten.

MCP erhält explizite, fest formulierte Fehlermeldungen für Live-Vorrang,
historische Belegung, fehlende Indizes/Kontextindizes/Vorbereitung,
übergroße Records und Abbruch. Beliebige Backendmeldungen, Pfade und
Geheimwerte werden weiterhin nicht durchgereicht. Fehler sind keine
leeren erfolgreichen Messantworten.

### Tatsächlich ausgeführte gezielte Prüfungen

Vorhandenes isoliertes Test-venv mit `work/test-guard/sitecustomize.py`,
`PYTHONDONTWRITEBYTECODE=1` und deaktiviertem pytest-Cache. Der Testschutz
sperrt `/share`, `/data`, `/config`, `/backup` und Nicht-Loopback-Verbindungen.
Keine produktiven Daten oder zusätzlichen Batterieabfragen verwendet.

- Battery: **50 bestanden**, 3,21 s. Dateien:
  `test_measurement_live_admission.py`, `test_research_budget.py`,
  `test_history_p0.py`, `test_manual_history_pages.py`.
  Protokoll: `/tmp/guardian-measurement-admission-battery-final.log`.
- MCP: **15 bestanden**, 1,51 s. Dateien:
  `test_history_error_codes.py`, `test_research_budget.py`,
  `test_manual_history_paging.py`.
  Protokoll: `/tmp/guardian-measurement-admission-mcp-final.log`.
- Neue Regressionen verwenden echte synthetische Messdateien und den
  Standard-Research-Pfad ohne Scheduler. Nachgewiesen sind Modul-SOC,
  einzelne Zellspannung und Coverage, Pause während der Decodierung ohne
  Wiederholungsdecodes, Raw-Rückstau, Abbruch, Timeout, unverändertes
  Fail-closed bei fehlendem Index sowie Vorrang vor Startup-Historienarbeit.
  Der zusätzliche MCP-Protokolltest ruft alle drei Messwege erfolgreich über
  einen Loopback-Testserver bei simulierten Collector-Zyklen auf.
- Erster Battery-Lauf: 49 bestanden, ein Testfehler durch zwischen Tests
  erhaltenen synthetischen Queue-Rückstau. Die neuen Tests isolieren nun die
  Writer-Registrierung; separater Neun-Test-Lauf und obiger 50-Test-Lauf
  bestanden. Erstprotokoll bleibt `/tmp/guardian-measurement-admission-battery.log`.
- Erster MCP-Lauf: 13 bestanden, ein durch Sandbox-Bind-Verbot blockierter
  Loopback-Protokolltest. Mit freigegebenem Loopback-Testlauf zunächst 14,
  nach zusätzlichem Standardpfad-Protokolltest die oben genannten 15 bestanden.
  Erstprotokoll: `/tmp/guardian-measurement-admission-mcp.log`.

### Verbleibender Integrations- und Abnahmeschritt

Die Änderung löst die belegte sofortige Zulassungsverweigerung für die genannten
Messwege. Sie erzeugt keine fehlenden historischen Indizes und garantiert bei
Dauerrückstau oder fehlender Vorbereitung keine Datenantwort. Diese Zustände
bleiben explizite Fehler. Eine vollständige P0-/HA-Abnahme ist nicht erfolgt.

Versionskennungen wurden in dieser Korrekturrunde nicht geändert. Der lokale
Branch führt Battery 0.9.1 und MCP 0.8.4; letzteres ist nicht der bereits
separat installierte/veröffentlichte MCP-0.8.5-Stand. Für eine Veröffentlichung
ist der kleine MCP-Fehlermapping-/Testdiff auf den verifizierten 0.8.5-Nachfolger
zu übertragen, ohne dessen Versionskennung auf 0.8.4 zurückzusetzen. Vorhandene
Paging-Arbeit nicht doppelt übernehmen. Das lokale `main` ist kein Nachweis
für aktuelles GitHub-main. Ein PR muss genau diese Abgrenzung belegen; kein
pauschales Zusammenführen des gesamten Entwicklungszweigs empfohlen.

Danach sind geprüfter Quellcommit, eindeutige Folgeversionen/Buildherkunft,
separat freigegebene Installation und eine begrenzte externe MCP-Abnahme
gegen reale kleine Messfenster erforderlich. In dieser Runde kein Commit,
Push, Merge, Build, HA-Eingriff oder Deployment.


### Folgeprüfung und Sicherungskandidat 0.9.2 (23.10.2026)

Die unmittelbar vorangehende Aussage zu unveränderten Phasen/Tageslesern ist
hiermit fortgeschrieben: Der vorhandene kanonische Fensterleser wird ebenfalls
kooperativ zugelassen, ausschließlich für bereits vorhandene Produkte. Der
Research-Standardpfad liest keine Roh-Tails zur Phasen-Neuberechnung. Fehlende
Phasenindizes bleiben als `available=false`, `reason=indexed_projection_unavailable`
sichtbar. Tagesabrufe hatten keine unmittelbare Historical-Zulassungssperre;
`DiagnosticsNotFound`/`DiagnosticsCorrupt` landeten aber im allgemeinen Fehlerpfad.
Sie werden jetzt als `history_preparation_required` bzw. `history_product_invalid`
ausgegeben. Der bereits vorhandene `SelectedDailyRepository` ersetzt den
unbegrenzten Tagesverzeichnis-Scan: ausgewählte Tage, bestehendes 2-MiB-Eingabebudget
und bestehende 32-Tage-Fenstergrenze. Keine Neuberechnung und kein Backfill.

Genau ein Zusatztest `test_existing_product_admission.py` bestand (0,28 s):
vorhandene Ladephase, Tagesdiagnose und Diagnostic-Evidence, Pausieren bei
Erfassung, fehlendes/ungültiges Tagesprodukt und fehlender Phasenindex.
Protokoll `/tmp/guardian-existing-product-admission.log`. Die vorherigen
50+15 Tests wurden nicht erneut ausgeführt. Diese Ergebnisse bleiben Belege
für ihren jeweiligen oben dokumentierten Stand, nicht für eine HA-Abnahme.

Battery Manifest/Laufzeit werden für den eindeutig unterscheidbaren Folgefix
auf 0.9.2 vorbereitet. MCP-Fehlermapping wird getrennt auf dem sauberen
veröffentlichten Inhaltsstand 0.8.5 (`21798f08142d1aa6fa22272edcb6edcee250fcdd`)
als 0.8.6 vorbereitet. Safari bestätigte GitHub-main am 23.10.2026 auf
`c4aebbe281d9516111cb1ab6761e5707c34b4895`, Merge von PR #4; dessen acht MCP-Dateien
waren laut damaligem PR-Nachweis bytegleich zum lokalen 21798f0-Inhalt.
Lokales main bleibt davon getrennt und wird nicht verschoben.

Installationsplan: den neuen vollständigen Battery-Quellcommit mit passender
BUILD_VERSION/Buildherkunft über den bestehenden lokalen Supervisor-App-Weg
paketieren; keine alten /share-Daten zurückspielen oder Migration aktivieren.
MCP 0.8.6 über einen separaten kleinen PR ausschließlich unter guardian_research_mcp
auf GitHub-main veröffentlichen, anschließend über die bestehende Repository-App
installieren. Kein Battery-Code aus GitHub-main übernehmen. Optionen, Auth,
Tunnel und Identitäten erhalten. Merge und konkrete Installation bleiben separat
freizugeben. Externe Abnahme anschließend: ein kleines SOC-/Zellfenster und dessen
Coverage, dann vorhandene Phasen/Tagesprodukte oder ihre präzisen Fehlzustände.
Keine automatische Erzeugung fehlender Produkte. Ein dauerhafter Raw-Rückstau
kann weiterhin einen begrenzten Timeout statt Messdaten verursachen.

MCP-Folgeintegration auf 21798f0: zwölf direkt betroffene Mapping-/Versionsprüfungen
bestanden (1,06 s), Protokoll `/tmp/guardian-mcp086-release-targeted.log`.
Keine Wiederholung der vorherigen 50+15-Funktionsprüfungen. Der separate MCP-PR
enthält acht Dateien: errors.py, neuen isolierten Mappingtest, Manifest,
Serverkennung, User-Agent, drei Versionsassertions und README/Changelog.
Die Tools-/Paging-Implementierung bleibt gegenüber 0.8.5 bytegleich.
