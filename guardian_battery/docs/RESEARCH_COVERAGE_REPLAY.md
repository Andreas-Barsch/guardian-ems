# Research-Reader 0.8.5 — aktuelle Vorbereitung

Der kleine I/O-Fix aus RESEARCH_READER_IO.md wird als Battery 0.8.5 vorbereitet.
Kein neuer Replay, keine SOC-Regeländerung und kein weiterer Ereignisimport.
Research-MCP bleibt 0.8.3; API-/MCP-Verträge, Coverage und Messwerte unverändert.
Die später separat genehmigten 70 historischen Ereignisse sind bereits importiert;
ihr Herkunftsnachweis bleibt erhalten. Die folgenden früheren Abschnitte und
Testzahlen dokumentieren den damaligen Stand und ersetzen keine Live-Abnahme
von 0.8.5. Timeout bleibt ein technischer Fehler, kein fehlender Messwert.

## Historische Vorbereitung vor 0.8.5

# Lokale Research-Korrektur und begrenzter SOC-Nachlauf

## Aktuelle Versionsvorbereitung: Battery 0.8.4 / Research-MCP 0.8.3

Lokal vorbereitet, unveröffentlicht. Funktionsbasis ist der geprüfte und lokal
committete Stand `3b3f3a442ed32a3aa8eb141152c6d05592bebacd`; die jetzige Vorbereitung
ändert nur Versionskennungen, Dokumentation und direkt betroffene Tests.
SOC-/Coverage-Code, Schema 2 / `guardian_soc_gap_v1`, Grenzen >=5 pp / <=300 s,
Konfigurationsschlüssel `soc_crash_gap_pp` und Legacy-Warnung bleiben unverändert.
Tunnel 0.8.1 und Diagnostic Engine 0.4.12 bleiben unverändert.

Battery bleibt beim lokalen Paketweg; spätere MCP-Veröffentlichung eng auf separat
geprüftem GitHub-main, ohne dessen andere Battery-Linie zu ersetzen. Der gepaarte
MCP-Vertragstest benötigt den passenden lokalen Battery-Stand und ist vom späteren
isolierten MCP-Veröffentlichungstest zu unterscheiden. Historische Testzahlen
unten belegen ihre jeweiligen Funktionsstände, nicht einen bereits geprüften
neuen Installationsstand oder einen MCP-only-main-Stand.

Vor Installation aktueller Image-/Options-/Share-Sicherungsnachweis. Alte Leser
überspringen neue Schema-2-Ereignisse; Datenbytes und passende alte Konfiguration
bei Rückkehr schützen, keinen vollständigen Abruf über alte Software behaupten.
Die 70 vorläufigen historischen Treffer bleiben ein externer Bericht, keine
Importfreigabe. Andere Detektor-IDs verhindern keine semantische Doppelzählung;
Schema 1 und 2 getrennt auswerten. Kein produktiver Import oder SOC-Startbackfill.

## Historischer Ausgangspunkt und damalige lokale Vorbereitung


Stand 2026-10-01, auf Battery-Quellcommit `2d026dd959559600faafb583ba8e2131427e8e41`.
Uncommittete lokale Vorbereitung, keine Versionsanhebung, Installation oder
Produktivereignisse. Die anschließende ausdrücklich bestätigte Abstandsregel
ändert gemeinsam Live-/Replay-Detektor und versioniert neue Ereignisse; Collector
und frühere Coverage-Korrekturen bleiben erhalten. Historische Prüfabschnitte
weiter unten beziehen sich ausdrücklich auf den jeweils damaligen Stand.
MCP-Anwendungscode entspricht als Ausgangspunkt dem lokal vorhandenen
`ef394bb`; zum benannten GitHub-Merge `70afd4b…` wurde kein Fetch durchgeführt.

## Befund und Grenzen des Timeout-Nachweises

Vorhandene MCP-Auditlogs (keine neuen Coverage-Live-Retrys):
Coverage scheiterte nach 10.164656 bzw. 10.144917 Sekunden mit `timeout`.
SOC-/Strom-/Zellabfragen im selben kleinen Zeitfenster waren nach 6.157659,
7.998458 bzw. 6.781402 Sekunden erfolgreich. Battery prüft eine 10-s-Abfragedeadline,
mit 15-s-Hardlimit; MCP-Transportdefault maximal 15 s. Die Laufzeiten passen zur
Battery-Deadline. Eine genaue Trennung von CPU-/I/O-Last auf dem Gerät ist damit
nicht bewiesen. Der einzelne SOC-Coverage-Timeout wird nicht allein durch die
Mehrfachabfrage erklärt; der alte Coverage-Pfad erzeugte auch für ein Dataset
alle Punktobjekte und wiederholt vollständige Identitätsprojektionen.

Lokal an einer gesicherten 2.49-MB-Tagesdatei mit 3.388 Zeilen reproduziert:
eine SOC-Abfrage und eine SOC-Coverage jeweils ungefähr 0.04 s, drei Coverage-
Datasets ungefähr 0.117 s und 10.164 JSON-Decodes (drei Vollscans). Das ist ein
Arbeitsmengennachweis, keine Messung der produktiven Hardwareleistung.

Korrektur: ein gemeinsamer Coverage-Durchlauf, ohne Punktlisten oder
Identitätsepochen pro Messpunkt. Zeitreihen und Coverage nutzen vorhandene,
validierte Blockindizes rein lesend; bei fehlendem/ungültigem Index folgt der
bisherige begrenzte Tagesdatei-Fallback. Es wird kein Index erstellt/repariert.
Die Deadline wurde nicht erhöht. Tagesdateinamen folgen wie CellHistoryWriter
der Laufzeit-Ortszeit; Rückgabezeitpunkte bleiben explizite UTC-Zeitpunkte.

## Abdeckung und Vertrag

`complete` beschreibt die beobachtete Unterstützung des angefragten Fensters,
nicht vollständige Live-Aufzeichnung: erster/letzter beobachteter Wert müssen
die angefragten Grenzen erreichen; interne Lücken werden wie bisher relativ
zur beobachteten Median-Kadenz (> dreifache Kadenz) ausgewiesen. Diese Kadenz
ist eine deskriptive Coverage-Angabe, KEINE SOC-Erkennungsbedingung.
An den Rändern wird nicht extrapoliert. `missing_intervals.kind` unterscheidet
`leading`, `trailing`, `internal`, `entire_range`. `boundary_policy` ist
`no_extrapolation`. Vorhandene Zeilen ohne endlichen Wert der angefragten
Metrik zählen nicht als Messung; Nullwerte werden nicht erfunden, echte 0 bleibt 0.
Für Zellmetriken bedeutet Dataset-Abdeckung vorhandene endliche Zellwerte,
nicht den Beweis, dass jede der 15 Zellen beobachtet wurde.

Coverage bezieht sich auf sämtliche gefundenen Quellbeobachtungen im Fenster,
unabhängig von Pagination/Display-Ausdünnung. `truncated` und `next_cursor`
beschreiben weiterhin die ausgegebene Seite. Keine zeitlich vollständige
Auflösung aus dünneren Stichproben ableiten.

| Cell-History-Metrik | Einheit |
|---|---|
| soc | % (SOC-Abstand im aktuellen Detektor: Prozentpunkte) |
| module_voltage | V |
| module_current | A |
| module_temperature, cell_temperature | degC |
| cell_voltage, cell_deviation, cell_spread | mV |

Zeitreihen liefern `metric`, `unit`, `source`, `resolution`. Coverage-Zeilen für
Cell-History-Datasets tragen ebenfalls Einheit, Quelle und native Auflösung.
`timestamp` bleibt die Zeit der gespeicherten Cell-History-Zeile;
`cell_sample_at` und das gegebenenfalls vorhandene `pwr_sample_at` machen die
unterschiedlichen Messzeitpunkte sichtbar (Unix-Sekunden). Bei SOC/Strom ist
für Korrelationen der ursprüngliche PWR-Kontext zu beachten. Kein PWR-Zeitpunkt
wird bei fehlender Angabe erfunden.

MCP-Schemas beschreiben erlaubte Metriken, Datasets, Quellen und Auflösungen
als Enums. `auto` wählt bis 24 Stunden `full`, darüber `display`; maximal 7 Tage
full / 90 Tage display. Gültige Quelle/Metrik-Kombinationen prüft Battery:
Cell-History bietet die obigen Metriken; Display-History SOC, Modulstrom,
Zellspannung/-temperatur; Hycube battery_capacity/soc (%) und policy;
Canonical Phase liefert Phasenintervalle statt numerischer Messpunkte.
Policy-Coverage verwendet die bereits vom Policy-Reader gelieferten gültigen
Intervalle (`coverage_basis=policy_intervals`), keine Kadenz zwischen Änderungen.
Ein gültiges Segment über das gesamte Fenster bedeutet vollständige Abdeckung;
vor dem ersten gültigen Segment bleibt eine Randlücke. `first_observation` und
`last_observation` benennen die tatsächlichen Beobachtungszeiten der Segmente.
Wartung ist ereignisbasiert (`coverage_basis=event_occurrences`): Anzahl und
Zeitpunkte vorhandener Ereignisse bleiben sichtbar, zeitliche Abdeckung bleibt
`unknown`. Leere covered/missing-Intervalle und keine Kadenz bedeuten hier weder
lückenlose Aufzeichnung noch fehlende Messungen. Auch null gefundene Ereignisse
beweisen keine vollständige Historie. Nicht belegte SOH-/Cycles-Abdeckung bleibt
weiterhin `unknown`, nicht null Messungen.
`query_cell_history` erhält alle acht bisher von Battery unterstützten Metriken,
auch SOC, Modulspannung/-strom/-temperatur und `cell_spread`. Das Schema schränkt
die vorhandenen API-Möglichkeiten nicht auf einzelne Zellwerte ein.

Backend-/Transportfehler werden als MCP `isError=true` mit übereinstimmendem
Text und `structuredContent.error` zurückgegeben. Codes werden nicht zu
`invalid_argument` umgedeutet. `error.origin` trennt `guardian_api`,
`guardian_transport`, `guardian_response` und MCP-eigene Fehler/Queue.
Der alte Connector zeigte Text `timeout`, aber strukturiert `INVALID_ARGUMENT`;
der alte lokale MCP-Code warf einen ToolError mit dem richtigen Textcode.
Welche externe Schicht den strukturierten Ersatzcode erzeugte, ist unbelegt.
Der neue rohe MCP-Protokollvertrag wird lokal getestet; das Verhalten des
installierten Connectors nach einem Update ist separat abzunehmen.

## Offline-Nachlauf: ausdrücklich Stichproben, keine Live-Rekonstruktion

`tools/replay_soc_crashes.py` ist ein separat aufzurufendes lokales Werkzeug,
kein Collector-Hook und kein MCP-Tool. Standard ist Dry-run mit JSON-Bericht
auf stdout; keine Ereignisdatei, kein Lock und keine History-Änderung.
Ein oder zwei explizite gesicherte Tagesdateien, insgesamt maximal 64 MiB /
250.000 Zeilen und ein Zeitraum von höchstens 24 Stunden sind erforderlich.
Standard sind gemeinsame Gruppen mit mindestens zwei beobachteten Modulen;
`--modules` bleibt optional ausschließlich als expliziter Vergleichsfilter. Dateinamen aus der Sicherung ausdrücklich
wählen; es findet kein globaler History-Scan statt.

Cell-History enthält ursprüngliche `pwr_sample_at`-Werte. Nur identische
PWR-Zeitpunkte bilden einen Rahmen; nicht die späteren individuellen
Zellmesszeiten. Eine feste Sechserbesetzung oder externe Topologiebestätigung
ist keine Erkennungsbedingung. Fehlende PWR-Zeit, widersprüchliche Beobachtungen
blockieren den begrenzten Einzelfensterlauf; im Bestandslauf begrenzen diese
Eingabefehler weiterhin einen Abschnitt. Gruppen mit weniger als zwei Modulen
setzen nur den Vergleich aus, ohne Replay-Reset oder Verlängerung der Frist.
Auswertbare Gruppen mit wechselnder Zusammensetzung gehen unmittelbar an den
gemeinsamen Detektor, ohne zusätzlichen Reset. Fehlende Modulwerte werden nicht als Ausbau
interpretiert, nicht interpoliert und nicht mit letzten Werten ergänzt.

Die gesicherten PWR-Kontexte sind nur eine Teilmenge der Live-PWR-Folge.
Selbst vollständige gespeicherte Rahmen beweisen daher nicht jeden damaligen
Rollenwechsel oder den ersten möglichen Live-Erkennungszeitpunkt.
Der Bericht weist Abstände, Quellenhashes, Kandidaten, Ereignisse und Grenzen
aus. Ausgangszustand ist der erste Rahmen des ausdrücklich gewählten Fensters;
vorherige Kandidaten sind unbekannt. Kein exakter historischer Live-Crash-
Nachweis allein aus aggregierten oder einzelnen Modulreihen.

Die Erkennung selbst verwendet exakt `SocCrashDetector`: X=5 pp, Y=300 s
konfigurierbar, aktueller Abstand zum aktuellen Zweitminimum >=X bei <=Y seit
festem Kandidatenbeginn. Sofortige Erkennung auch in der Startmessung; kein
zusätzlicher Eigenabfall nötig. Ein Kandidat, Rollenwechsel vor Prüfung,
Referenzwechsel ohne Fristverlängerung. Eindeutiges Anfangsminimum ist nur
Ausgangsrolle, neue gemeinsame Minima warten auf Eindeutigkeit. Ein späterer
Gleichstand mit dem bisherigen Schwächsten erhält Rolle und Start. Nach
Bestätigung/Ablauf kein erneutes Ereignis ohne Rollenwechsel. Seriennummern sind Metadaten, keine weitere Bedingung.
Dokumentierte Record-Seriennummern haben Vorrang; optional liefern gesicherte
Positionssnapshots zeitgültige Zuordnung. Unbekannte Identität bleibt null,
Widersprüche werden gezählt, nicht zur Crash-Bedingung gemacht.

Beispiel (ausschließlich gesicherte lokale Dateien, Ausgabe auf stdout):

```sh
PYTHONDONTWRITEBYTECODE=1 python tools/replay_soc_crashes.py \
  --history-file /lokale/sicherung/cell_history/2026-10-01.jsonl \
  --position-history /lokale/sicherung/position_history.jsonl \
  --from 2026-10-01T00:00:00Z --to 2026-10-01T00:10:00Z
```

`--write-test-events /separater/lokaler/teststore/soc_crash_events.jsonl`
ist explizit und ausschließlich für die vorbereitete lokale Schreibprüfung.
Produktivpfade (/share, /data, /config, /backup, /Volumes) und Quellordner werden
abgelehnt. Persistierung verwendet den vorhandenen unveränderlichen EventStore;
Wiederholung/Neustart spielt dasselbe vollständige Fenster erneut ab und
hängt identische IDs nicht nochmals an. Abweichende unveränderliche Payloads
werden vor dem ersten Append abgelehnt. Kein Checkpoint setzt Kandidaten mit
fehlendem Vorzustand fort. Bei I/O-Fehlern ist ein möglicher Teilappend nicht
als null geschriebene Ereignisse auszugeben; Wiederholung dedupliziert.

Messzeit und Ausführungszeit bleiben getrennt. Neue Abstandsevents tragen
Schema 2 / `guardian_soc_gap_v1`; alte Schema-1-Eigenabfallereignisse behalten
unveränderte Bytes, IDs und Lesebedeutung. Details und Konfiguration stehen in
[SOC_CRASH_SIMPLE.md](SOC_CRASH_SIMPLE.md). Replay-Herkunft steht im separaten
Laufbericht, nicht als Behauptung einer gerade live erfolgten Erkennung.
Eine spätere produktive historische Befüllung benötigt eine eigene Freigabe
und eine Entscheidung über diese Auflösungsgrenzen; sie ist hier nicht erfolgt.

## Historischer begrenzter Probelauf vor Aufhebung der Sechserauswahl

Sicherung `Guardian-Rueckkehrsicherung-2026-10-01/guardian_battery`,
Datei `cell_history/2026-10-01.jsonl`, SHA256
`ff0b8b256318de14f6089d06c7c57a49e8d0343b8b0d7c2751681c4d534d2291`.
Fenster 2026-10-01 00:00–00:10 UTC; 3.388 Zeilen / 2.488.128 Bytes gelesen;
10 vollständige Rahmen aller sechs dokumentierten Positionen; Abstände
49.923–70.080 s; 0 Kandidaten, 0 Crashes, 0 geschriebene Ereignisse;
Seriennummern für alle Beobachtungen vorhanden. Laufzeit rund 0.029 s am Mac.
Keine zusätzliche Batterieabfrage, kein produktiver Testlauf.

Mangels realem Fund in diesem kleinen Fenster ist die End-to-End-Abnahme
synthetisch gekennzeichnet: bestehender Detektor → isolierter EventStore →
aktuelle Battery-API → echter lokaler MCP-Protokollclient → Ereignisdetails,
SOC/Strom/Zellspannung und Rand-Coverage. Der anschließende Bestandslauf und der reale lokale Abrufnachweis stehen
im folgenden Abschnitt. Die produktive Timeout-/Connector-Abnahme bleibt offen.


## Historisch eingeschränkter Sechser-Bestandslauf und echter lokaler Abrufnachweis

Nach ausdrücklichem Folgeauftrag wurde der vorhandene Sicherungsbestand
vollständig inventarisiert: 47 Tagesdateien (16.08.–01.10.2026),
192.861.412 Bytes, 288.961 Zeilen. Keine Quelle wurde geändert.
122.413 Zeilen bis 02.09.2026 18:18:14 UTC haben keinen ursprünglichen
PWR-Zeitpunkt und werden nicht als PWR-Rahmen erfunden.

Der CLI-Modus `--saved-directory /lokale/sicherung/cell_history --modules 1,2,3,4,5,6`
(mit optionaler `--position-history`) ist ausdrücklich ein **reiner Dry-run**.
Er begrenzt die Inventur auf höchstens 62 Tagesdateien, 256 MiB und 500.000
Zeilen und bietet keinen Schreibmodus. Dateien werden nur gelesen/gehasht.
Vollständige Gruppen laufen über Dateigrenzen hinweg im selben Detektor;
kleinere/widersprüchliche Gruppen beenden einen belegbaren Eingabeabschnitt.
Der nächste belegte Abschnitt beginnt mit einem neuen, unveränderten Detektor.
Das ist eine Grenze der rekonstruierbaren Eingabe, keine zusätzliche Crash-Regel.
Kandidaten über solche Grenzen bleiben unbekannt. Bei fehlendem ursprünglichem
PWR-Zeitpunkt wird nur der nach dessen letzter Cell-Zeit liegende Suffix geprüft.

Für die Sechs-Modul-Prüfung sind 37.653 PWR-Gruppen vom 02.09.2026 18:22:45 UTC
bis 01.10.2026 07:25:33 UTC vorhanden. 8.130 Gruppen enthalten alle sechs
Positionen; 29.523 kleinere Gruppen werden nicht als vollständig behauptet.
Ihre damalige tatsächliche Modulanzahl ist dadurch nicht bewiesen: Eine
Positionszuordnung allein belegt nicht die Vollständigkeit eines PWR-Polls.
Solche Gruppen bleiben unbewertet, bis unabhängige Vollständigkeitsbelege
vorliegen. Keine Ergänzung mit letzten/angenommenen SOC-Werten.

89 getrennte vollständige Abschnitte, 57 Kandidaten und fünf Treffer in der
**gespeicherten Stichprobenfolge**, ohne Produktivereignisse:

| Bestätigung (UTC, 2026) | Modul | weiterer Abfall | Zeit seit Kandidatenbeginn |
|---|---:|---:|---:|
| 03.09. 15:23:13 | 5 | 9 pp (94 → 85) | 184.015 s |
| 07.09. 01:52:22 | 6 | 11 pp (40 → 29) | 221.040 s |
| 08.09. 02:50:48 | 6 | 8 pp (39 → 31) | 139.973 s |
| 09.09. 02:40:17 | 6 | 6 pp (42 → 36) | 120.044 s |
| 14.09. 18:22:59 | 5 | 6 pp (66 → 60) | 120.010 s |

Keine widersprüchlichen gespeicherten Gruppen, keine Identitätskonflikte,
keine unbekannten Seriennummern in den ausgewerteten Rahmen. Größte Lücke
zwischen vorhandenen PWR-Gruppen 4.439,917 s; 59 Abstände >300 s. Die Zeitgrenze
ist hier zusätzlich eine beschreibende Lückenstatistik, kein neuer Filter.
Laufzeiten auf dem Mac 18,108 s bzw. 22,620 s. Der zweite vollständige Lauf
lieferte exakt dieselben fünf Ereignispayloads/IDs; alle 47 Quellenhashes
waren unverändert. Weiterhin kein Beweis der vollständigen Livefolge oder
aller damaligen ersten Erkennungszeitpunkte.

Der erste reale Treffer wurde **nur in einen temporären lokalen Teststore**
übernommen: `SCS-33c2a5daad49d448d5b999018dd0eb76a229819c8e0826b0306e6abd2fa1eba6`.
Unveränderte ID/Payload über echte Battery-API-Logik und lokalen MCP-
Protokollserver abgerufen; wiederholtes Anhängen dedupliziert. Zeitfenster
15:15:09–15:28:13 UTC am 03.09., Seriennummer Y225004C32250226:
SOC, Strom und Zellspannung Zelle 1 jeweils elf Punkte; `pwr_sample_at` und
SOC-Werte für Kandidatenstart und Bestätigung exakt zugeordnet (Toleranz nur
für die vorhandene Mikrosekunden-Serialisierung). Coverage liefert die
Randlücken; der gemeinsame Abruf dauerte lokal etwa 0,052 s.
Kein realer Treffer wurde in die produktive Ereignisdatei geschrieben.

Zwei anfängliche Starts des separaten lokalen Abnahme-Testservers scheiterten
an dessen 5-s-Startfrist. Ein Thread-Diagnoseauszug zeigte langsames Nachladen
des AnyIO-Backends; Vorabladen dieser Testbibliothek vor dem Starttimer behob
den Testaufbau. Kein Anwendungs- oder Abfragetimeout wurde geändert.

## Prüfstand vor der anschließenden Review-Korrektur

- Battery: 1.087 bestanden / 1 fehlgeschlagen im finalen Gesamtlauf.
  Der unveränderte MQTT-Profiltest vergleicht zwei `last_update`-Zeitstempel
  über einen Sekundenwechsel (13:28:42/43); isoliert ebenso 13:29:34/35.
  Kontrollierter Lauf mit festgehaltener Testuhr: 1 bestanden, Assertions und
  Repository-Testdatei unverändert. Kein vollständig grüner finaler Einzellauf
  behauptet. Vor der letzten Erweiterung: 1.084 Tests in einem Lauf bestanden.
- MCP: 26 bestanden, einschließlich strukturiertem Timeout/Fehlerursprung,
  Schemas und synthetischer Ende-zu-Ende-Abnahme.
- Historischer Bestandslauf zweimal deterministisch; zusätzlicher echter
  lokaler API-/MCP-Abrufnachweis wie oben erfolgreich.
- Python-AST und Diff-Prüfung bestanden. Vorhandener Produktionsschutz aktiv;
  Netzwerk nur lokale Testserver, zusätzlich gezielt vorhandene HA-MCP-Logs
  gelesen; keine neuen produktiven Coverage-Retrys.

Offen bleiben produktive Laufzeit-/Connector-Abnahme nach separater Freigabe,
die genaue Lastursache der bisherigen Einzel-Dataset-Timeouts sowie die
Vollständigkeit kleinerer historischer PWR-Gruppen. Keine Versions-, Commit-,
Push-, Installations- oder Produktivschreibaktion gehört zu dieser Vorbereitung.


## Review-Korrektur und gezielte Nachprüfung am 01.10.2026

Die lesende Abschlussprüfung fand zwei Regressionen in der lokalen Vorbereitung:
Policy-Segmente wurden als Punkte ausgewertet, und das MCP-Enum für
`query_cell_history` schloss zuvor unterstützte Metriken aus. Beide wurden im
bestehenden lokalen Auftrag korrigiert. Die Änderungen betreffen
`research_api.py`, `tools.py`, die vorhandenen neuen Coverage-/MCP-Vertragstests
und dieses Dokument. Keine Änderung am Detektor, an X=5/Y=300, Ereignisformat,
Collector, Versionen, Tunnel oder Diagnostic Engine.

Policy-Coverage erhält vollständige Gültigkeitsintervalle des vorhandenen
Readers; Wartungsereignisse werden nicht als kontinuierliche Messreihe behandelt.
Tests prüfen ein vor/bei Fensterbeginn geltendes Segment, einen späteren Beginn,
fehlende Segmente sowie null/ein/mehrere Wartungsereignisse. Ein echter lokaler
MCP-Protokolltest ruft alle acht Metriken über die Battery-API ab, vergleicht
Schema und Ergebnis und prüft konkrete Werte/Einheiten einschließlich cell_spread.

Neu ausgeführt mit bestehender isolierter Testumgebung, aktiviertem Testschutz
gegen Produktivpfade/externe Verbindungen und deaktivierten Bytecode-/pytest-Caches:

- Battery: `test_research_coverage.py` und `test_research_api.py`: **51 bestanden
  in 0,86 s**. Erster Anlauf: 49 bestanden / 2 fehlgeschlagen wegen eines fehlenden
  Pflichtfelds `affected_system` in der neuen Testfixture; Fixture korrigiert.
- Research-MCP: alle vorhandenen Tests: **27 bestanden in 3,65 s**. Erster
  Sandbox-Anlauf: 17 bestanden / 10 fehlgeschlagen, weil lokale Socket-Binds
  verboten waren. Wiederholung mit erlaubtem Loopback und unverändertem
  Produktionsschutz erfolgreich. Fehlgeschlagene Protokolle werden erhalten.
- AST-Prüfung aller 13 geänderten/neuen Python-Dateien und `git diff --check`
  bestanden. Die oben genannten unveränderten Quelldateien/Manifeste wurden
  zusätzlich bytegleich mit HEAD geprüft.

Kein neuer vollständiger Battery-Gesamtlauf: Der frühere Befund **1.087 bestanden /
1 fehlgeschlagen** und der gesonderte kontrollierte Uhrlauf gelten ausdrücklich
für den damaligen Stand. Der MQTT-Zeitvergleich wurde weder verändert noch durch
die gezielten Erfolge als vollständig grüner Gesamtlauf ersetzt.

### Getrennte historische Vollständigkeitsprüfung (nur lesend)

Die Sechs-Modul-Inventur ist keine durchgehend belegte historische Stackbelegung.
Ihre 8.130 Rahmen liegen an elf UTC-Kalendertagen: 02.–09.09., 14.09., 30.09.
und 01.10. Die 89 Abschnittsspannen summieren sich auf etwa 180,37 Stunden in
685,05 Stunden Gesamtzeit; das ist keine lückenlose Messzeit. Vom 15.–29.09.
wurde kein Rahmen ausgewertet. 26.413 ausgeschlossene Gruppen enthalten genau
Positionen 1–4. Deshalb bleiben die fünf Treffer ausdrücklich eine Untermenge.

Für das geprüfte Fenster **22.09.2026 16:45–16:50 UTC** stehen in der Sicherung:

- `maintenance_events.jsonl`, Zeile 25: M6 elektrisch/kommunikationsseitig getrennt
  ab 09.09.15:23 UTC; Zeilen 26/27: M5 getrennt ab 10.09.14:15 UTC.
- `position_history.jsonl`, Zeilen 5/6: Sechserzuordnung vom 01.09. bleibt bis zur
  neuen Zuordnung vom 30.09. bestehen; kein zeitnaher Vierer-Snapshot.
- `config_history.jsonl`, Zeile 13: letzter gespeicherter module_count=5 vom
  09.09.18:55 UTC. Diese Konfiguration ist kein Beleg eines vollständigen Polls.
- `cell_history/2026-09-22.jsonl`, Zeilen 4496 ff.: um 16:46:13.550043 M1/M4=41 %;
  um 16:47:13.542922 M1=40 %, M4=38 %; um 16:49:13.558017 M4=32 %. Für einen
  belegten vollständigen Viererstack ergäben sich damit Kandidatenstart 38 % und
  weitere 6 Prozentpunkte in 120,015 Sekunden. Im Sechserlauf sind diese Gruppen
  ausgeschlossen. Hier wurde kein neues Ereignis erzeugt und kein neuer
  Detektorlauf ausgeführt.

Die dokumentierten Trennungen stützen eine kleinere Besetzung, beweisen aber
nicht allein Vollständigkeit und ununterbrochene Gültigkeit dieses konkreten
PWR-Rahmens: Am 14.09. gab es beispielsweise wieder 107 Sechsergruppen ohne
passenden neuen Positionssnapshot. Ein hinreichender unabhängiger Beleg für die
vollständige aktive Besetzung am 22.09. fehlt in den geprüften Unterlagen.
Keine pauschale Viererannahme, neue Aktivitätsregel oder Parameteränderung.
Die Datenquelle/Aggregation des HA-Monatsdiagramms ist nicht als identisch zur
Sicherung belegt. Keine produktive Abfrage wurde dafür ausgeführt.

Die frühere externe Nachweissammlung `Research-Replay-20261001-133222` bleibt
unverändert als historischer Prüfstand erhalten. Die Nachprüfung wird separat
mit aktualisiertem Bericht, aktuellem Diff, neuen Dateien und vollständigen
Testprotokollen gesichert. Keine Commit-, Veröffentlichungs-, Installations-
oder Produktivbefüllungsaktion; Betriebs-/Connector-Abnahme bleibt offen.


## Historischer 23er-Prüfstand: Modulauswahl vor Einführung der Abstandsregel

Dieser Abschnitt dokumentiert die frühere Eigenabfallregel >X, nicht die jetzt
bestätigte Abstandsregel >=X. Auch das damalige Verwerfen von Einmodulgruppen
wurde inzwischen durch Aussetzen ohne Reset ersetzt. Zahlen und Nachweise
bleiben als historischer Vergleich erhalten.

### Damalige Korrektur: mindestens zwei beobachtete Module

Die ausdrückliche Nutzerklarstellung hebt die bisherige Sechserauswahl auf.
Sie war eine zu enge historische Auswahl und keine vereinbarte fachliche Regel.
Die oben dokumentierten Sechserergebnisse bleiben als historischer Vergleich
bestehen; daraus folgt keine neue Vollständigkeitshürde für den aktuellen Replay.

Beide lokalen Replaymodi akzeptieren standardmäßig gemeinsame PWR-Gruppen mit
mindestens zwei Modulen, ohne Seriennummern-/Topologiepflicht. Ein optionales
`--modules` schränkt nur einen ausdrücklich gewünschten Vergleichslauf ein.
Es verändert keine Detektorregel. Inventur hat weiterhin keinen Schreibmodus.
X=5 Prozentpunkte und Y=300 Sekunden, strikt >X/<=Y bleiben unverändert.

Zusammensetzungswechsel werden protokolliert, nicht als physischer Ausbau
interpretiert. Der bestehende Detektor verarbeitet jede auswertbare Gruppe:
Fehlt nur ein anderes Modul, darf sein vorhandener Kandidat fortbestehen;
fehlt der bisher Schwächste, greift dessen vorhandener Zustandspfad; ein neu
beobachtetes schwächstes Modul ersetzt den alten Kandidaten vor der Bestätigung.
Keine zusätzliche Reset-/Crashregel wegen geänderter Gruppengröße. Gruppen mit
weniger als zwei Modulen oder widersprüchlichen Beobachtungen und fehlende
PWR-Zeit behalten die vorhandene Eingabeabschnittsbehandlung. Aussagen über
ungespeicherte Zwischenwerte oder tatsächlich aktive fehlende Module bleiben offen.

Der JSON-Bericht kennzeichnet sämtliche Ergebnisse als `provisional=true`,
`historical_completeness=unverified`, zählt beobachtete Zusammensetzungen und
protokolliert ihre Wechsel. Ereignisformat/Detektorkennung bleiben unverändert;
die Vorläufigkeit steht im externen Laufbericht. `frames_replayed` zählt die
ausgewerteten Gruppen. Das alte Vergleichsfeld `complete_frames_replayed` ist
im Standardmodus null und behauptet keine physische Vollständigkeit. Zu jedem
Treffer werden die beobachteten Module bei Bestätigung separat dokumentiert.
Die Tageszählung enthält UTC und Europe/Berlin ausdrücklich getrennt.

Neu ausgeführter schreibfreier Lauf auf derselben Sicherung: 37.639 von 37.653
Gruppen ausgewertet, 14 Einmodulgruppen verworfen, 11 Eingabeabschnitte,
602 beobachtete Zusammensetzungswechsel, 221 Kandidaten und **23 vorläufige
Treffer**; davon 15 mit vier, 3 mit fünf und 5 mit sechs beobachteten Modulen
bei Bestätigung. Alle bisherigen fünf Ereignis-IDs bleiben enthalten.
Alle 47 Quellen mit gleichen Größen/Zeilenzahlen/SHA256 wie im alten Lauf;
0 angehängte Ereignisse, Laufzeit 13,929 s am Mac. Keine produktive Übernahme.
122.413 frühere Zeilen ohne ursprüngliche PWR-Zeit bleiben nicht rekonstruierbar.

Die verbleibenden starken Abstiege sind nicht automatisch Regelverletzungen:
24.09.UTC beginnt M4 erst 20:06:45.476616 bei 31 %, anschließend bis 26 %: genau 5 pp.
27.09.UTC beginnt M4 erst 21:02:04.858891 bei 29 %, anschließend bis 25 %: 4 pp.
28.09.UTC beginnt M4 erst 21:00:58.936028 bei 28 %, anschließend bis 24 %: 4 pp.
Der größere Abfall davor zählt nach bestätigter Definition nicht mit.

Neue gezielte Tests: **93 bestanden in 1,21 s** (Replay, unveränderter Detektor,
Ereignisse). Abgedeckt sind Standardgruppen von 2–6 Modulen ohne Identitätsbeleg,
Gruppenwechsel mit fortbestehendem Kandidaten, Verschwinden des Schwächsten,
neues schwächstes Modul vor möglicher Altbestätigung, Einmodulgrenze,
Wiederholbarkeit, schreibfreier CLI-Standard und getrennte UTC-/Berlin-Tage.
Frühere Gesamtprüfungen bleiben historische Nachweise, kein neuer vollständiger
Battery-Gesamtlauf. Externe alte Auswertungen werden nicht überschrieben.

Zusätzlich nach dieser Anpassung: **3 API-/MCP-Vertragstests bestanden in 1,39 s**,
AST aller 13 geänderten/neuen Python-Dateien und Diff-Prüfung bestanden. Die fünf
alten Ereignispayloads (nicht nur IDs) sind byteinhaltlich unverändert enthalten.
Es wurden keine Ereignisdateien geöffnet oder angehängt; die JSON-Ausgabe des
Inventurlaufs ist ausschließlich ein externer vorläufiger Bericht.


## Bestätigte Abstandsregel und aktuelle Prüfung am 01.10.2026

Nach ausdrücklicher Bestätigung aller offenen Punkte wurde lokal die frühere
Eigenabfallbedingung durch den aktuellen Abstand zum aktuellen Zweitminimum
ersetzt: **>=5 pp innerhalb einschließlich 300 s** seit gemessenem Kandidatenstart,
beide Parameter weiterhin konfigurierbar. Sofortbestätigung auf der Startmessung
ist erlaubt. Zwei neue gemeinsame Minima warten ohne Modulnummernauswahl auf
Eindeutigkeit; weniger als zwei Beobachtungen setzen den Vergleich ohne Reset
und ohne Fristverlängerung aus. Rollenwechsel wird zuerst verarbeitet; wechselnde
Zweitminimum-Referenzen setzen die Frist nicht zurück. Keine weitere Eigenabfall-,
Median-, Identitäts- oder feste Sechserbedingung. Die vollständigen Zustandsregeln
stehen in [SOC_CRASH_SIMPLE.md](SOC_CRASH_SIMPLE.md).

Neues Schema 2 / `guardian_soc_gap_v1` dokumentiert aktuellen Abstand, alle am
Zweitminimum gleichstehenden Referenzen und getrennt die vorherige schwächste
Rolle. Alte Schema-1-Felder, IDs, Bytes und historische >=-Eigenabfalllesbarkeit
bleiben unverändert. Neue und alte Ereignisse sind gemeinsam lesbar und eindeutig
gekennzeichnet; keine rückwirkende Umdeutung. Listen verwenden
`guardian_soc_events_v2`, Einzelabrufe die jeweilige Detektorkennung. Endpunkte
und MCP-Tools bleiben bestehen. Der neue Konfigurationsschlüssel
`soc_crash_gap_pp` hat den bestätigten Default 5; `soc_crash_drop_pp` ist nur noch
optional akzeptiert, wird nicht umgedeutet und erzeugt eine Startwarnung.
Komponentenversionen bleiben Battery 0.8.3 / MCP 0.8.2 / Tunnel 0.8.1 / Engine 0.4.12.

### Neu ausgeführte Prüfungen dieses Stands

Isolierte bestehende Testumgebung, Produktionspfade/externe Verbindungen gesperrt,
Bytecode- und pytest-Caches deaktiviert:

- Gezielt: **197 bestanden in 0,85 s** vor Ergänzung des tatsächlichen
  PWR-Hook-/Replay-Gleichheitstests. Frühere Anpassungsanläufe (116 bestanden /
  7 fehlgeschlagen und 145 bestanden / 1 fehlgeschlagen) bleiben erhalten;
  sie zeigten alte Replay-Erwartungen bzw. einen falschen Startzeitpunkt in einer
  angepassten Testassertion. Kein grüner Befund für diese Zwischenstände behauptet.
- Vollständige Battery-Suite einschließlich neuem Hook-/Replay-Test:
  **1.129 bestanden in 27,18 s**. Ein vorausgehender Sandboxlauf hatte
  1.124 bestanden / 4 fehlgeschlagene lokale HTTP-Binds. Wiederholung mit
  zugelassenem Loopback, unverändertem Produktionsschutz erfolgreich. Der
  bekannte unveränderte MQTT-Zeitvergleich war in diesem Lauf erfolgreich;
  seine historische Flakiness wird dadurch nicht als behoben dargestellt.
- Vollständige MCP-Suite: **28 bestanden in 3,26 s**, einschließlich echter
  lokaler Protokollabfragen gemischter alter/neuer Ereignisse mit Gleichheits-
  Altbestand und mehreren Zweitminimum-Referenzen, unveränderten Bytes/IDs und
  expliziter Semantikversion. Der lokal installierte Connector wird nicht geprüft.
- AST aller 20 geänderten/neuen Python-Dateien und `git diff --check` bestanden.
  Keine Tunnel-/Engine-, Abhängigkeits- oder Buildänderung, kein Versionsbump.

Grenztests umfassen Abstand 4,9 / 5 / 5,1 pp, Zeit 0 / 100 / 299 / 300 / 301 s,
sofortige Erkennung, Anfangs-/Folgegleichstände, Rollen-/Referenzwechsel,
Einmodulpausen, kein erneutes Ereignis ohne Rollenwechsel, feste Zeitbasis,
Live-/Replay-Payloads und IDs, Kandidatenzählung, gemischte Speicherung/API/MCP,
Legacy-Lesbarkeit und schreibfreie Probeläufe. Es wurden ausschließlich
synthetische Ereignisse in temporäre Teststores geschrieben.

### Neuer vorläufiger historischer Vergleich (ausschließlich lokale Sicherung)

47 unveränderte Tagesdateien, 192.861.412 Bytes, 288.961 Zeilen;
37.653 ursprüngliche PWR-Gruppen, davon 37.639 vergleichbar und 14 nur als
Einmodulpause verarbeitet. Ein fortlaufender Eingabeabschnitt, 201 Kandidaten,
**70 vorläufige Abstandstreffer**: 43 sofort beim Kandidatenstart, 7 mit genau
5 pp Abstand. Bei Bestätigung 55 Vierer-, 5 Fünfer- und 10 Sechsergruppen;
keine Aussage über ungespeicherte Module. Fünf Ereignisse haben mehrere
Zweitminimum-Referenzen, zwei keine eindeutig vorherige schwächste Rolle.

Beide aktuellen Durchläufe liefern identische Ereignispayloads/IDs; alle 47
Quellenhashes sowie die Identitätsquelle stimmen mit dem erhaltenen 23er-Lauf
überein und wurden nach dem Lauf erneut geprüft. Laufzeiten 13,625 / 13,676 s.
Der zweite Lauf sperrte zusätzlich sämtliche Dateiänderungs-, Netzwerk- und
Unterprozessoperationen per Audit-Hook; nur die zuvor geöffnete stdout-Ausgabe
enthält den Bericht. **0 angehängte Ereignisse**, kein produktiver EventStore,
keine neue Batterie-/HA-Abfrage. Die alten externen Prüfstände bleiben erhalten.

Alle 23 alten Modul-/Kandidatenstart-Paare sind in den neuen 70 Treffern vertreten.
Das bedeutet keine Vertragsgleichheit: Neue IDs enthalten eine andere
Detektorkennung; Abstand und Eigenabfall sind unterschiedliche Größen, teils
mit früherer Bestätigung. Der alte 23er-Lauf wird nicht nachträglich umetikettiert.
Tageszahlen und sämtliche Treffer werden im neuen externen Nachweis ausdrücklich
getrennt in UTC und Europe/Berlin gegenübergestellt.

Beispiel 24.09.2026: Kandidat M4 startet 20:06:45.476616 UTC (22:06:45.476616 Berlin)
bei 31 %. Bereits 20:07:45.470207 UTC (22:07:45.470207 Berlin) steht M4 bei 28 %,
Zweitminimum M1 bei 33 %: genau 5 pp Abstand nach 59,993591 s. Bestätigung zu
dieser ersten passenden gespeicherten Messung; kein Warten auf weiteren Eigenabfall.

Grenzen bleiben: 122.413 ältere Zeilen ohne ursprüngliche PWR-Zeit, Stichproben
statt vollständiger Livefolge, unbekannte ungespeicherte Rollenwechsel und
keine produktive Connector-/Laufzeitabnahme. Die 70 Treffer sind eine Auswertung
der gespeicherten Beobachtungen, kein Beleg aller damaligen Liveereignisse oder
bestimmter Ursachen. Kein Commit, Push, Release, Installieren oder Nachbefüllen.
