# Guardian Battery Changelog

## 0.7.31 – Production History Request Subtiming

- Ergänzt einen eigenen bounded `HistoryRequestTimingState` mit prozesslokal monotonen Request-IDs, dem neuesten laufenden Request und dem tatsächlich zuletzt abgeschlossenen Request. Parallele Requests bleiben getrennt; abgeschlossene Werte werden nicht historisiert oder nachträglich verändert.
- Misst den vollständigen im Add-on sichtbaren Backendpfad als monotone Wall-Zeit und Request-Thread-CPU. Belastbare Stufen umfassen Discovery, kombinierte Cell- und Hycube-Read/Parse/Filter-Grenzen, Downsampling, Policy, Maintenance, Phase, finale HTTP-Serialisierung, handlerseitiges Response Write und Other Accounting.
- Kennzeichnet nicht ausgeführte Stufen als `not_executed` und ohne invasive Refaktorierung nicht trennbare Unterzeiten als `not_available`; es werden keine künstlichen Nullzeiten erzeugt.
- Erfasst ausschließlich bounded technische Counts für Dateien, Bytes, Zeilen, Prefilter, Parse-/Invalid-Records, Fenster-/Modulauswahl, Downsampling, Policy, Maintenance, Phase und Response-Größe. Raw Records, JSONL-Zeilen, Zellarrays und Responseinhalte werden nicht im Timing-State gespeichert.
- Beobachtet den unveränderten 24-Entry-History-Cache als Hit/Miss. Cache-Key, `mtime_ns`-/Dateigrößeninvalidierung, Requestparameter, fachliche Response, Punktreihenfolge und Downsampling bleiben unverändert.
- Zeigt Current und Last Completed/Failed Request, Total Wall/CPU, Cache, Backend-Breakdown, Counts und Observability Error Count unter **Guardian Maintenance → History Timing**. Die Anzeige ist technische Evidence und keine Batterie-, Modul- oder Alarmbewertung.
- Die tatsächliche finale HTTP-Serialisierung wird ohne zusätzliche Observability-Serialisierung gemessen. Response Write endet mit der Rückkehr des Handler-Calls und umfasst ausdrücklich weder Supervisor-Ingress noch Browser Parse oder Chart Rendering.
- Der lokale 24-h-SOC-Referenzbenchmark für Modul 1 ergab 0,102797 s Baseline-Median und 0,102909 s instrumentierten Median, entsprechend 0,109 % Overhead. Unter künstlicher CPU-Konkurrenz stieg primär Wall gegenüber Request-Thread-CPU; daraus wird keine produktive Ursache abgeleitet.
- Dieser Release enthält keine History-Performanceoptimierung, Cacheänderung, Frontend-Instrumentierung oder Änderung an Collector, Scheduler, Analysis Worker, Derived MQTT Worker, Persistence, MQTT, Raw Evidence, Diagnostics, Risk, RS485 oder Hycube Control.
- Guardian Battery und Add-on sind `0.7.31`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.30 – Asynchronous Derived MQTT Worker

- Entfernt ausschließlich Derived-only Cell Diagnostics aus dem zeitkritischen Collector-Main-Thread. Live- und gemischte MQTT-Werte, Live-Alarme, RS485, Topologie und Discovery bleiben synchron im bestehenden Collector-Pfad.
- Verwendet genau einen bounded Worker mit höchstens einem aktiven und einem neuesten Pending-Job. Neuere Analysis Generations ersetzen nur wartende MQTT-Projektionen; Raw Evidence, Analysis Results, Derived Persistence, Daily Diagnostics und Risk werden nicht koalesziert.
- Der Worker verwendet den bestehenden Paho-Client und dessen bestehende Verbindung. Es gibt keine zweite MQTT-Verbindung, kein Batching, keine Payloadreduktion und keine Änderung an Topics, Payloads, Reihenfolge, QoS oder Retain.
- Jeder Job bindet Analysis Generation, Config-ID, kanonische Position-zu-`physical_module_serial`-Provenienz, Reconnect-Epoche und Erstellungszeit. Ein Reconnect invalidiert den Erfolgsmarker und macht den aktuellen Derived State erneut publizierbar.
- Exception, MQTT-Returncode ungleich Success und Connection Loss erzeugen keinen falschen Erfolgsmarker. Workerfehler bleiben von Collector, Acquisition, Live MQTT, Alarmen und Evidence isoliert; es gibt keine unbounded Retry-Schleife.
- Beim Shutdown werden keine neuen Jobs angenommen, Pending verworfen und ein aktiver Job nur mit begrenztem Timeout gejoint.
- Ergänzt separate bounded Worker-Observability mit Active/Pending, Generationen, Zeitstempeln, Wall-/CPU-, Build-, JSON- und Publish-Zeiten, Calls, Bytes, Maximaldauer, Erfolg/Fehler sowie Coalesced-/Failure-Zählern. Abgeschlossene Collector-Zyklen bleiben unverändert.
- Produktive Ausgangsevidence waren etwa 3.692 Cell-Diagnostic-Publishes mit rund 47–61 Sekunden Publish Wall. Im lokalen Sechs-Modul-/90-Zell-Test sank der synchrone Main-Pfad von 3.649 auf 169 Publishes; Submit kostete etwa 0,11 ms. Dies ist keine produktive Laufzeitgarantie.
- Scheduler, Analysis Worker, Derived Persistence, Raw Evidence, Diagnostics, Risk, Alarmgrenzen, RS485, Hycube und History bleiben unverändert.
- Guardian Battery und Add-on sind `0.7.30`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.29 – Generation-Aware Derived MQTT Projection

- Publiziert ausschließlich verifizierte Derived-only Cell Diagnostics bei einer neuen vollständig abgeschlossenen Analysis Generation. Modul-Zellmedian, Diagnosezustände, Confidence, Evidence, Trend, Maintenance Risk sowie Zell-Diagnose-States und -Attribute werden bei unveränderter bereits erfolgreich publizierter Generation nicht redundant wiederholt.
- Live- und gemischte MQTT-Werte bleiben in ihrer bisherigen Poll-Cadence: Spannung, Strom, SOC, Temperatur, aktuelle Zell-Min/Max, Zellspreizung, Live-Trends, Alarme, `cell_diag_config`, `module_*_cell_diag_live` und `battery/state` werden weiterhin regulär aktualisiert.
- Der bounded process-lokale Publish-Schlüssel verwendet die vorhandene Analysis Generation und Config-ID sowie die kanonische Position-zu-`physical_module_serial`-Provenienz und eine Reconnect-Epoche. Positionen werden nicht als physische Identität behandelt; es gibt keinen persistenten Dedup-State, Topic-Cache, Payload-Hash oder Payloadvergleich.
- Ein erfolgreicher MQTT-Reconnect invalidiert den Derived-Publish-State und löst beim nächsten sicheren Poll einen vollständigen Republish des aktuellen Derived State aus. Der Analysis Result State selbst bleibt erhalten.
- Eine Generation gilt erst nach dem vollständigen vorgesehenen Derived-Burst als erfolgreich publiziert. Exception, MQTT-Returncode ungleich Success oder Connection Loss verhindern den Marker; ein partieller Burst wird beim nächsten geeigneten Poll vollständig erneut versucht.
- Das MQTT-Per-Publish-Subtiming aus 0.7.28 bleibt erhalten und ergänzt performed/skipped, aktuelle und zuletzt erfolgreich publizierte Generation, process-lokale Erfolgs-/Skip-/Failure-Zähler sowie Reconnect-Invalidierung.
- Produktiv wurden zuvor in einem problematischen Poll 4.014 Publishes beobachtet, davon 3.692 Cell-Diagnostics-Publishes mit 47,18 Sekunden Publish Wall. Lokal reduzierte die unveränderte bereits publizierte Generation den repräsentativen Ein-Modul-Pfad von 649 auf 69 Calls und von 303.792 auf 5.629 Payload-Bytes; dies ist keine produktive Laufzeitgarantie.
- Der einmalige Burst einer neuen Analysis Generation bleibt synchron und kann weiterhin mehrere tausend Publishes und erhebliche Wall-Time verursachen. Dieser Release führt keinen MQTT Worker, kein Batching und keine weitere Performanceoptimierung ein.
- Topics, Payloads, Reihenfolge, QoS, Retain und Discovery eines tatsächlich ausgeführten Derived-Bursts bleiben unverändert. Scheduler, Acquisition, Analysis Worker, Derived Persistence, Raw Evidence, Diagnostics, Risk, Alarme, RS485, Hycube und History bleiben unverändert.
- Guardian Battery und Add-on sind `0.7.29`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.28 – MQTT Per-Publish Production Observability

- Ergänzt cycle-accurate MQTT-Subtiming für den letzten vollständig abgeschlossenen Collector-Zyklus: MQTT Projection, Guardian Build, JSON-Serialisierung, Publish Invocation und Remaining Other werden jeweils als Wall- und Thread-CPU-Zeit ausgewiesen. RS485 MQTT bleibt eine zusätzliche diagnostische Grenze; seine JSON-/Publish-Anteile werden in der überschneidungsfreien Gesamtrechnung nicht doppelt bilanziert.
- Erfasst ausschließlich bounded technische Evidence: JSON- und Publish-Anzahl, JSON- und Payload-Bytes, Maximum eines einzelnen Publish-Aufrufs sowie die feste Gruppe dieses Maximums. Topics, Payloadtexte, JSON-Texte, Cell-Diagnostic-Inhalte und Publish-History werden nicht gespeichert.
- Gliedert Publish-Evidence in die festen Gruppen `stack_battery`, `modules`, `cell_diagnostics`, `rs485` und `other`; pro Gruppe werden Aufrufe, Bytes, Publish- und JSON-Zeiten sowie Maximalzeiten ausgewiesen.
- Beobachtet Paho-Returncodes als Success, No Connection, Queue Full oder Other Error und verwendet ausschließlich den öffentlichen Verbindungsstatus vor und nach der MQTT-Projektion. Private Paho-Queue-Zustände bleiben ausdrücklich `unavailable_private_paho_state`; es gibt keine Retrylogik und kein `wait_for_publish`.
- Zeigt die Evidence unter **Guardian Maintenance → Collector Timing → Letzter abgeschlossener Collector-Zyklus → MQTT Subtiming**. Alle Werte sind exakt dessen `cycle_id` zugeordnet und werden nach Zyklusabschluss nicht durch Background-Arbeit verändert.
- Die repräsentative lokale Messung stieg von etwa 43,40 ms auf 47,51 ms beziehungsweise um etwa 9,46 %. Diese temporäre technische Produktionsobservability liegt unter der verbindlichen 10-%-Grenze, ist aber nicht kostenlos und stellt keine dauerhafte Performanceaussage dar.
- Dieser Release enthält keine MQTT-Performanceoptimierung. Publish-Anzahl, Reihenfolge, Topics, Payloads, QoS, Retain, Discovery, Live State, Cell Diagnostics und RS485 MQTT bleiben fachlich unverändert. Zweck ist die produktive Ursachenbestimmung der beobachteten 46,23-s-MQTT-Projektion.
- Scheduler, Analysis Worker, Raw Evidence, Diagnostics, Risk, Alarme, RS485-Semantik, Hycube und History bleiben unverändert. Guardian Battery und Add-on sind `0.7.28`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.27 – Cycle-Accurate Collector Observability

- Trennt den aktuell laufenden Collector-Zyklus strikt vom letzten vollständig abgeschlossenen Zyklus. Prozesslokal monotone Cycle IDs und die atomare Übernahme beim Zyklusabschluss verhindern zeitlich gemischte Timinganzeigen.
- Ergänzt eine eigene vollständige Cell-Cycle-Evidence mit Collector Cycle ID, Cell Generation, Deadline, tatsächlichem Start, Lateness, Start-zu-Start-Intervall, Acquisition-, Post-Processing- und Main-Thread-Gesamtdauer sowie übersprungenen Slots.
- Trennt Analysis Worker und Derived Persistence Worker in generation-aware Statusbereiche. Background-Laufzeiten verändern abgeschlossene Collector-Zyklen nicht; Diagnostic Store Save und Aggregate Write werden ausschließlich der Persistence-Generation zugeordnet.
- Misst synchrone Payload-Erzeugung für Cell Store und Aggregate Store separat von der Background-Persistierung. Maintenance Refresh, Analysis Snapshot Build/Submit, Result Adoption, MQTT Projection und Topology/Position bleiben eindeutig dem Main Thread zugeordnet.
- Zerlegt den unveränderten Analysis Snapshot Build in Identity-, Sample-, Aggregate-, Maintenance-, Options/Config- und sonstige Laufzeit. Bounded Modulwerte enthalten Position, Seriennummer, Samplezahl, Aggregatezahl sowie Wall- und Thread-CPU-Zeit.
- Ergänzt Wall-vs-Thread-CPU-Evidence ohne automatische Interpretation oder Diagnoseklassifikation. Negative Collector- oder Cell-Accounting-Differenzen werden nicht kaschiert, sondern bleiben sichtbar und erhöhen den Observability Error Count.
- Gliedert Collector Timing in letzten vollständigen Collector-Zyklus, letzte vollständige Cell-Runde, Analysis Worker, Derived Persistence Worker und bestehendes Cell Analysis Profiling. Die UI unterscheidet Messwert, „nicht ausgeführt“ und „nicht verfügbar“.
- Dieser Release enthält keine Performanceoptimierung und keine Scheduleränderung. Er dient der produktiven Root-Cause-Messung; Acquisition, Console, Raw Evidence, History, MQTT-Semantik, Diagnose- und Risk-Algorithmen bleiben unverändert.
- Guardian Battery und Add-on sind `0.7.27`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.26 – Asynchronous Cell Analysis / Acquisition Recovery

- Entkoppelt die schwere historische Cell Analysis vom zeitkritischen Acquisition-Pfad. PWR/BAT-Erfassung, Raw Cell History, Live State und bestehende Live-Alarme bleiben synchron; Current Condition, Confidence, Evidence, Trend, Resistance, Capacity/Curves, Rest, Balancing, Maintenance Analysis und ICA Readiness werden als Derived State verarbeitet.
- Führt genau einen bounded Analysis Worker mit höchstens einem aktiven und einem neuesten Pending-Job ein. Neuere Generationen ersetzen ausschließlich wartende Derived Jobs; Raw Evidence wird weder koalesziert noch verworfen.
- Übernimmt vollständige Derived Results atomar und generation-aware. Jeder Job hält Erstellungszeit, Source-Sample-Zeit, Config-ID, Modul-/Seriennummernbezug und Snapshotgröße fest; Ergebnisse einer inzwischen ersetzten physischen Identität werden nicht auf das neue Modul projiziert.
- MQTT behält Topics und Discovery-IDs bei. Livewerte werden weiterhin im regulären Collector-Zyklus publiziert; Derived Diagnosewerte zeigen den letzten vollständig abgeschlossenen Stand mit Generation, Source-Zeit, Analysezeit und Alter.
- Isoliert Workerfehler von Acquisition, Raw History, Live-Alarmen und MQTT. Der letzte gute Derived State bleibt erhalten und altert sichtbar; spätere Generationen können ohne Add-on-Neustart wieder erfolgreich laufen. Shutdown bleibt bounded und verwirft rekonstruierbare Pending-Jobs.
- Erweitert die prozesslokale Collector-Observability um Snapshot-/Submit-Zeit, Worker active/pending, aktive/abgeschlossene/eingereichte Generation, letzte Laufzeit, Abschlusszeit, Alter, Coalesced Count und Failure Count. Das 0.7.25 Cell Analysis Profiling bleibt erhalten und ist der letzten abgeschlossenen Generation zugeordnet.
- Diagnose-, Evidence-, Maintenance-, Balancing- und Risk-Algorithmen, Live-Alarmgrenzen, Scheduler-Zielwerte, Console/BAT-Protokoll, Raw Evidence, History, RS485 und Hycube bleiben unverändert. Die produktive Samplingverbesserung muss nach Deployment noch über mehrere Cell-Runden verifiziert werden.
- Guardian Battery und Add-on sind `0.7.26`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.25 – Cell Analysis Productive Profiling

- Ergänzt bounded, prozesslokale Laufzeit-Evidence für die bestehende Cell Analysis: Gesamtzeit je Modul sowie Main-Unterzeiten für Identitätsauflösung, Aggregateprojektion, Store-Analyse und Ergebnisaufbau.
- Zerlegt `CellDiagnosticStore.analyse()` in reine Messabschnitte für Sampleauswahl/-validierung, Cache-Signatur und Hit/Miss, Current-Condition-Statistik/-Reduktion, Evidence-Gesamtzeit und Ergebnisaufbau. Die Cachelogik selbst bleibt unverändert.
- Misst die bestehenden Evidence-Stufen für Sortierung, Ranking, Resistance, Segments, Capacity/Curves, Rest, Balancing, ICA Readiness, Maintenance und Assembly einzeln, ohne Diagnosealgorithmen oder Ergebnisse zu verändern.
- Zeigt ausschließlich skalare Counts für aktuelle Samples, Store-/Identity-/Unknown-Puffer, Aggregate global/je Identität/je aktueller Config, Maintenance Events sowie Derived Writer active/pending. Es werden keine Raw Samples, Zellarrays, Aggregate- oder Maintenance-Inhalte ausgegeben.
- Der bestehende Collector-Timing-Bereich zeigt maximal sechs aktuelle Modulprofile. Die Daten werden nicht historisiert und dienen ausschließlich der produktiven Ursachenbestimmung.
- Dieser Release behebt die produktiv beobachteten rund 74 Sekunden Cell Analysis noch nicht. Er legt keine Ursache fest und verändert weder Scheduler, Acquisition, Console, History, Raw Evidence, MQTT, RS485 noch Risk-/Diagnosebewertung.
- Guardian Battery und Add-on sind `0.7.25`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.24 – Collector Timing Visibility

- Ergänzt in den Modulinformationen einen read-only Bereich für die bereits vorhandene `collector_timing`-Evidence aus `/api/rs485/status`. Er zeigt Ziel, letzte Dauer und effektives Intervall für Hauptpoll und Cell-Sampling sowie Deadline-Lateness, Overrun-Zähler/-Maximum und Stack-Sample-Spread.
- Macht die gemessenen Komponentenlaufzeiten für PWR Request/Processing, STAT/INFO, BAT Requests, Identity Resolution, Cell History Write, Cell Analysis, Diagnostic Store Save, Aggregate Write, MQTT Projection, Topology/Position und ausdrücklich `Remaining Other` sichtbar. Fehlende Werte bleiben „nicht verfügbar“ und werden nicht als null Sekunden dargestellt.
- Zeigt BAT Count, Median und Maximum sowie das letzte effektive Cell-Intervall je physischer Seriennummer, nach dokumentierter Modulposition und danach Seriennummer sortiert.
- Die Timingwerte sind prozesslokale technische Betriebs-Evidence. Rolling- und Overrun-Werte beginnen nach einem Neustart neu; sie sind weder Batteriequalitätsbewertung noch Alarmstatus und verändern keinen Stack- oder Modulstatus.
- Gegenüber 0.7.23 wurden Collector, Scheduler, Deadlines, Console, RS485, MQTT, History und Diagnosealgorithmen nicht verändert. Der bestehende Home-Assistant-Ingress-Schutz bleibt unverändert.
- Guardian Battery und Add-on sind `0.7.24`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.23 – History UX/Performance + Collector Timing Recovery

- Beschleunigt History-Abfragen durch frühe Modulfilterung, einen Scan für mehrere ausgewählte Module, getrennte Extrema-Sammler, einmaliges Sammeln der SOC-System-Evidence, einmaliges Lesen der Maintenance History, einen abfragespezifischen Cache und begrenztes extrema-erhaltendes Downsampling. Der lokale Referenzbenchmark lag bei etwa 0,020 s für einen, 0,053 s für drei, 0,132 s für sieben und 0,555 s für 30 Tage; dies sind keine produktiven Messwerte.
- Vereinheitlicht Einzel- und Vergleichsfilter: SOC unterstützt ein Modul oder alle Module, zellbezogene Einzelansichten erfordern ein konkretes Modul und Vergleiche verwenden explizit ausgewählte Messgrößen, Module und Zellen. Hycube Battery Capacity und Hycube Policy bleiben davon unabhängige System-Evidence.
- Überarbeitet die History UX mit breiter Messgrößenauswahl, konsistenten Controls, Modul-Mehrfachauswahl sowie den Zuständen CURRENT, DIRTY und LOADING. Beim Laden erscheinen Spinner und Overlay unmittelbar, während vorhandene Diagramme sichtbar bleiben.
- Sortiert normale Diagnostics-Modulkarten numerisch. Predictive Risk Top-10 bleibt score-basiert und ergänzt eine sichtbare Detailaktion, Maus-/Touch-/Tastaturbedienung, Rücknavigation, deutsche Balancing- und Confidence-Texte sowie verständliche Trenddarstellung.
- Priorisiert die Messwerterfassung: erfolgreiche sequenzielle BAT-Abfragen werden mit individueller Samplezeit und zeitgültiger physischer Identität unmittelbar append-only in Raw Cell History geschrieben, bevor rekonstruierbarer Derived State verarbeitet wird. Raw Evidence wird weder koalesziert noch verworfen.
- Ersetzt dauerhafte Poll-Drift durch getrennte monotonic Deadlines für Haupt- und Cell-Poll. Verpasste Slots werden ohne Catch-up-Kaskade übersprungen; Pylontech Console und alle PWR-/STAT-/INFO-/BAT-Kommandos bleiben single-owner und strikt sequenziell.
- Lädt Position History einmal je Cell-Runde und löst alle Module weiterhin zum jeweiligen historischen Samplezeitpunkt auf. Es gibt keine ADR→Position-Abkürzung und keine rückwirkende Verwendung heutiger Positionen.
- Verlegt große Diagnostic- und Aggregate-Snapshot-Persistierung in einen bounded Single-Writer mit höchstens einer pending Derived Generation. Persistierung bleibt atomar und restartfähig; Fehlerstatus und begrenzter Shutdown sind sichtbar. Cell-Diagnostic-, Alarm-, MQTT- und Risk-Semantik bleiben unverändert.
- Ergänzt bounded Process-Lifetime-Observability für Cycle-/Cell-Intervalle, Deadline-Lateness, Overruns, PWR-/STAT-/INFO-/BAT-Dauern, Raw-History-, Identity-, Analyse-, Store-, Aggregate-, MQTT- und Topology-Zeiten, Stack-Sample-Spread sowie effektive Cell-Intervalle je physischer Seriennummer. Diese Werte sind Betriebs-Evidence und keine Batteriediagnose.
- Vor dem Fix zeigte die produktive read-only Cell History einen Mediananstieg von etwa 69 s am 03.–04.09.2026 auf etwa 101–102 s am 05.–07.09.2026; M5 stieg von 68,97 auf 101,88 s und M6 von 68,98 auf 101,57 s. Die Verschlechterung ist verifiziert, ihr konkreter synchroner Verursacher nicht abschließend bestimmt.
- **Noch produktiv zu verifizieren:** Nach Installation müssen effektives Cell-Sampling, Hauptpoll und Overrun-Verteilung über mehrere Stunden gemessen werden. Zielwerte sind Cell-Median ≤70 s, Cell-P95 ≤90 s und Hauptpoll-Median ohne Cell-Runde ≤11 s. Vor dieser Abnahme wird nicht behauptet, das Samplingproblem sei vollständig behoben.
- Guardian Battery und Add-on sind `0.7.23`; Diagnostic Engine bleibt `0.4.12`, Cell Risk bleibt `guardian_cell_risk_v2_1` mit Formel `2.0.0` und Klassifikation `1.0.0`.

## 0.7.22 – SOC UI Cleanup + Predictive Cell Risk V2

- Bereinigt die SOC-History-Ansichten: „Einzel“ zeigt nur das gewählte Pylontech-Modul, „Vergleich“ alle vorhandenen Modul-SOCs. Beide Ansichten behalten Hycube Battery Capacity, drei zeitgültige Policy-Bereichsgrenzen, Phasenflächen und Maintenance-Marker. Die Modulauswahl bleibt im reinen SOC-Vergleich verborgen, intern aber für die Rückkehr zur Einzelansicht erhalten.
- Korrigiert messgrößenspezifische Tooltips und gruppiert die SOC-Legende nach Pylontech, Hycube und Hycube-Bereichsgrenzen. Modul-SOC, Modulstrom und Hycube-Werte erhalten keine Zellbezeichnung; Policy-Grenzen zeigen vollständigen Namen, Grenzwert und vier Policywerte ohne Abschaltbehauptung.
- Ergänzt Predictive Cell Risk V2 (`guardian_cell_risk_v2_1`, Formel `2.0.0`, Klassifikation `1.0.0`) mit deterministischer Top-10 und Detailansicht. Primäridentität ist `physical_serial + cell_number`; Positionen stammen ausschließlich zeitbezogen aus Position History. Die Zellgruppen sind G1/Z1–5, G2/Z6–10 und G3/Z11–15.
- Kombiniert relative Modul-/Gruppenabweichung, qualifizierte Lowest-Persistenz, Lastempfindlichkeit und Verschlechterungstrend über zwei unveränderte Erkennungspfade. Confidence beschreibt nur die Datenqualität und beeinflusst weder Score noch Rang.
- Persistiert kompakte Daily-Risk-History unter `aggregates/cell_risk/YYYY-MM-DD.json`. API und Guardian Diagnostics lesen Top-10, Zelldetails und Risk History aus Derived Data, ohne bei UI-Aufrufen Raw Cell History zu scannen oder zu verändern.
- Zeigt Balancing-/Maintenance-Kontext separat. Es gibt kein automatisches Balancing, keinen Score-Abzug allein wegen Maintenance und keine Verbesserungsbehauptung ohne vergleichbare Vorher-/Nachher-Evidence.
- Der Risk Score ist ein Guardian-Engineering-Priorisierungswert, ausdrücklich kein SOH, keine Ausfallwahrscheinlichkeit, keine Restlebensdauer, kein Herstellerwert und kein bestehender Alarmstatus. Cell-, Modul- und Stackstatus, Alarmgrenzen, MQTT, BMS Management, Hycube Policy und Kausalitätsentscheidung bleiben unverändert; die Diagnostic Engine bleibt `0.4.12`.
- Der synthetische Entwicklungs-Audit mit 37.061 Modulstichproben ergab etwa 11,105 s initiale Analysezeit, 212,41 MiB Peak-RAM, 149,04 KiB Risk-Aggregat sowie etwa 0,934 ms Top-10- und 1,914 ms Detail-API-Latenz. Dies sind keine produktiven Messwerte.
- Guardian Battery und Add-on sind `0.7.22`.

## 0.7.21 – Hycube Policy Boundaries + SOC Timeline

- Zeigt Hycube Battery Capacity weiterhin als separate, read-only erfasste Systemangabe im SOC-Zeitverlauf; sie wird nicht als Stack-SOC oder automatisch als Mittelwert der Modul-SOCs interpretiert (`aggregation_rule=not_verified`).
- Liest die Hycube-Batteriepolicy strikt read-only über `GET /Bat/getCustomBat/`: `normalMode` (Normalbetrieb), `bufferMode` (Passiv), `emergency` (Notstrom) und `batProtection` (Batterieschutz), jeweils in Prozent. Es gibt keine Queryparameter, Redirects oder Control-Endpunkte.
- Validiert nur vollständige, endliche numerische Werte von 0 bis 100 mit exakter Summe 100 als authoritative Policy. Ungültige Beobachtungen bleiben nachvollziehbar, ersetzen aber nicht die letzte gültige Policy.
- Historisiert jede Policy-Beobachtung append-only und kennzeichnet Änderungen über `policy_changed` und `effective_at`. Eine Policy gilt erst ab ihrer Beobachtung; heutige Werte werden nicht rückwirkend auf ältere Zeiträume angewendet.
- Ergänzt im SOC-Diagramm drei zeitgültige, bei Änderungen stufenförmige Bereichsgrenzen für Normalbetrieb/Passiv, Passiv/Notstrom und Notstrom/Batterieschutz. Für 82/3/10/5 liegen sie bei 18, 15 und 5 Prozent.
- Die Policy-Abfrage erfolgt einmal beim Start und danach alle 300 Sekunden im bestehenden Hycube-Collector-Thread. Fehler bleiben von `/data_row/`, Pylontech Console, RS485, MQTT, Daily Diagnostics, History UI und Guardian-Hauptprozess isoliert.
- Die Darstellung behauptet weder Abschaltursache noch Gleichheit mit Pylontech Under Voltage Protect, DCL oder Enable. Diagnosealgorithmen und Kausalitätsentscheidung bleiben unverändert (`causality=not_determined`); die Diagnostic Engine bleibt `0.4.12`.
- Guardian Battery und Add-on sind `0.7.21`.

## 0.7.20 – D1.2a Evidence Acquisition

- Erfasst passiv beobachtete Pylontech-`0x47`-Requests und -Responses vollständig als zeitbezogene Evidence und dekodiert gültige Herstellerparameter für Zell-/Modulspannungs-, Temperatur- und Stromgrenzen. Rawwerte und `info_raw` bleiben erhalten; Identität und historische Position werden nur aus zeitgültiger Evidence aufgelöst. Guardian sendet keine `0x47`-Anfrage und erzeugt keine zusätzliche RS485- oder Console-Busaktivität.
- Ergänzt einen optionalen strikt read-only Hycube-Collector für `GET /data_row/`. Er historisiert den empfangenen Body samt SHA-256 sowie vorhandene Werte für `BatteryPower`, `BatteryCapacity`, `GridPower`, `HomePower`, `solarPower`, `ExternalPower` und `Date2` mit Empfangs-, Gerätezeit- und Samplingqualität. Redirects, Login-Automation, Proxy- und Control-Endpunkte sind ausgeschlossen; Responses sind auf 1 MiB begrenzt.
- Hycube Evidence bleibt global standardmäßig deaktiviert und startet nur bei einem echten Boolean `true`. Nach explizitem Enable beträgt das konstante, konfigurierbare Standardintervall fünf Sekunden; es gibt keine adaptive oder ereignisabhängige Hochfrequenzabfrage.
- Führt User/System Policy als fachlich getrennte Evidence-Ebene. Da keine verifizierte read-only Quelle für Notstromreserve, Minimum-SOC, globale Entladegrenze, Discharge Permission oder Battery Operating Mode vorliegt, bleibt sie ausdrücklich `unavailable` mit `no_verified_read_only_source`. Policy, Pylontech-Management, Cell Evidence und Hycube-Systemreaktion werden nicht gleichgesetzt.
- Ergänzt `cell_sample_at`, `pwr_sample_at`, `pwr_age_seconds` und explizite Zeitqualität, weil BAT-Zellwerte und PWR-Strom/SOC nicht atomar gleichzeitig gemessen sein müssen. Bestehende Cell History bleibt kompatibel.
- D1.2a sammelt ausschließlich Evidence und erzeugt noch keine Ereignisketten-UI, KI-Interpretation oder Diagnoseentscheidung. Cell-/Modul-/Stackstatus, Alarmgrenzen, Confidence, Daily Diagnostics, BMS Management und Kausalität bleiben unverändert; `causality=not_determined`, Diagnostic Engine `0.4.12`.
- Guardian Battery und Add-on sind `0.7.20`.

## 0.7.19 – Guardian Diagnostics Ingress Navigation Fix

- Korrigiert den in 0.7.18 ausgelieferten separaten HA-Dashboard-Einstieg: Dort wurde der statische Add-on-Slug `3195b09a_guardian_battery` fälschlich als dynamischer Supervisor-Ingress-Session-Token verwendet, was im separaten Guardian-Diagnostics-Dashboard zu `503 Service Unavailable` führen konnte. Daily Diagnostics und das Diagnostics-Backend waren davon nicht betroffen.
- Integriert Guardian Diagnostics als internen Bereich in den gemeinsamen Guardian-Header. Der Benutzer öffnet ihn innerhalb der bestehenden dynamischen Ingress-Sitzung über den relativen Pfad `diagnostics`; der API-Prefix folgt weiterhin dem aktuellen `X-Ingress-Path`.
- Entfernt die fehlerhafte Source `homeassistant/dashboards/guardian_diagnostics.yaml` und ihre Registrierung `guardian-diagnostics` aus `homeassistant/configuration.yaml`. Die anderen Guardian-Dashboards bleiben unverändert.
- Verwendet keinen statischen Session-Token, keinen zweiten Server oder Port, keinen direkten Zugriff auf Port 8099 und kein ingress-proxyendes Lovelace-Dashboard. Overview, Tagesauswahl, vorhandene Daily Results und BMS Management Evidence bleiben fachlich unverändert.
- Daily Diagnostic Worker, Daily Core, BMS Management Evidence, Diagnostics-Backend und -API, Cell Diagnostics, RS485, MQTT, Position History sowie alle Diagnosebewertungen und -grenzen bleiben unverändert. RS485 und Guardian Diagnostics bleiben passiv und read-only; die Diagnostic Engine bleibt `0.4.12`.
- Guardian Battery und Add-on sind `0.7.19`.

## 0.7.18 – Guardian Diagnostics

- Macht die automatisch erzeugten Daily Diagnostics erstmals direkt in einem eigenständigen Benutzerbereich sichtbar. Gesamtübersicht, Datumsauswahl und Tagesdetails fassen vorhandene deterministische Evidence verständlich zusammen, ohne dass JSON-Dateien, Logs oder einzelne BMS-Ereignisse manuell rekonstruiert werden müssen.
- Ergänzt eine vollständig read-only Diagnostics API für Overview, vorhandene Tage, validierte Tagesresultate sowie BMS-Management-Aggregate und -Events. Sie liest ausschließlich Derived Data unter `/share/guardian_battery/diagnostics`, analysiert bei Browserzugriff weder Cell History noch RS485 Raw History neu und bietet keine POST-/PUT-/PATCH-/DELETE-, Daily-Run-, Backfill-, Worker- oder Anlagensteuerung.
- Zeigt 7-/30-Tage-Zusammenfassungen, Datenqualität (`complete`, `partial`, `failed`) und BMS Management Evidence je `physical_serial`. `partial` bedeutet eingeschränkte Datenlage und nicht „unauffällig“; `failed` bezeichnet ein fehlendes gültiges Tagesresultat und keinen kritischen Batteriezustand.
- Stellt CCL/DCL, Enable, Cell Context, Current Context, Coverage und Duty Cycle getrennt und ohne feste Normalgrenze dar. Wiederkehrende rohe `0x44`-Byteübergänge bleiben reine zeitliche Evidence ohne Interpretation als Protection, MOSFET, Shutdown oder Fault. Korrelation wird nicht als Ursache behauptet; BMS Management V1 bleibt `causality=not_determined`.
- Ergänzt eine responsive Hilfe-/Erklärungsansicht sowie die HA-Dashboard-Source `homeassistant/dashboards/guardian_diagnostics.yaml` und ihre Source-Registrierung. Dashboarddatei und produktive `/config/configuration.yaml` müssen bei einem späteren Deployment separat und kontrolliert aktualisiert werden; ein Add-on-Update kopiert sie nicht automatisch.
- V1 bindet ausschließlich die deterministische Daily Component **BMS Management Evidence** an. Die Architektur bleibt für weitere Components offen, enthält aber keine KI, neue Diagnosebewertung oder neue Grenzwerte. Cell Status, Confidence, Maintenance Risk, Phasenklassifikation, Gesamtbewertung und relative Endpoints bleiben unverändert.
- RS485 bleibt passiv; die Diagnostic Engine bleibt `0.4.12`. Guardian Battery und Add-on sind `0.7.18`.

## 0.7.17 – Deterministic Daily Diagnostics

- Ergänzt deterministische BMS-Management-Evidence nach physischer Seriennummer und zeitgültiger Position: CCL-/DCL-Reduktionen, Zero Events und Recoveries einschließlich `Limit=0` trotz Enable, Cell-/Lowest-Cell-/Medianabweichungs-/Spread-/Modulstromkontext, rekonstruierter Stackstrom-Provenienz sowie roher `0x44`-Korrelation ohne Bitsemantik oder Kausalitätsdiagnose (`causality=not_determined`). Peer-relative CCL-/DCL-Werte und Daily Aggregates setzen keinen festen Normalwert voraus.
- Führt einen side-effect-freien Daily-Diagnostics-Core für den Guardian-Tag in `Europe/Berlin` ein. Halb offene Zeitfenster `[day_start, day_end)`, timestamp-basiertes Slicing und Zeitzonenlogik bilden DST-Tage mit 23, 24 oder 25 Stunden korrekt ab.
- Persistiert physische Source-Provenienz, semantische Input-Fingerprints, deterministische Result-IDs und immutable Resultrevisionen atomar. Der Event Store ist idempotent; Komponentenfehler bleiben isoliert und Ergebnisse werden als `complete`, `partial` oder `failed` klassifiziert. Veränderte späte Evidenz kann eine neue Revision auslösen.
- Startet den isolierten `DailyDiagnosticWorker` nach der Live-Acquisition. Er prüft alle fünf Minuten, wartet nach Tagesende 15 Minuten und auf zwei stabile Input-Beobachtungen, verarbeitet initial höchstens drei Tage, danach höchstens einen Backlog-Tag pro Zyklus, begrenzt automatische Historie auf sieben vergangene Tage und beobachtet drei Tage auf Late Data. Aktueller und zukünftiger Tag bleiben ausgeschlossen; persistenter State, Result Index und Crash Recovery sichern den Lifecycle.
- Ergänzt strukturierte First-Deployment-Logs für Workerstart, Catch-up, Stability, Grace Period, Runs, Resultzusammenfassung und isolierte Fehler, ohne Rawframes, vollständige Cell Arrays oder Eventpayloads zu protokollieren.
- Schreibt ausschließlich Derived Data unter `/share/guardian_battery/diagnostics`; gespeicherte Raw History bleibt unverändert. Es gibt in 0.7.17 keine automatische Retention, Daily UI, Daily-MQTT-Projektion, Daily-HTTP/API oder KI-Interpretation.
- Daily Diagnostics liest ausschließlich gespeicherte Evidence und erzeugt keine RS485 Writes, Console Commands, MQTT Commands oder Hycube Actions. RS485 bleibt passiv; Cell Status, Diagnostic Confidence, Maintenance Risk, Phasenklassifikation, Gesamtbewertung, Diagnosegrenzen und relative Endpoints bleiben unverändert. Die Diagnostic Engine bleibt `0.4.12`.
- Guardian Battery und Add-on sind `0.7.17`.

## 0.7.16 – RS485 Management Timestamp Contract Hotfix

- Korrigiert den RS485-Management-Timestamp-Vertrag: `management[ADR]["timestamp"]` bleibt ausschließlich der direkte numerische Zeitpunkt des 0x92-Samples und wird nicht mehr durch den `datetime` des Identity Resolvers überschrieben.
- Behebt dadurch den real beobachteten MQTT-/Freshness-Fehler `float() argument must be a string or a real number, not 'datetime.datetime'` sowie den JSON-Fehler `Object of type datetime is not JSON serializable` in `/api/rs485/status`.
- Ersetzt den pauschalen Management-/Resolver-Dict-Merge durch eine explizite Identity-Feldprojektion. Resolver-Timestamp und Resolver-Quality gelangen nicht in das Management-DTO; direkte ADR, 0x92-Quality und Messwert-Provenienz bleiben erhalten.
- Entfernt Rawframes ausschließlich aus dem MQTT-/API-Management-DTO. Reader und Evidence Writer bleiben unverändert und behalten die Raw Evidence.
- RS485-Decodierung, Entity-IDs, Topics, Discovery, Availability und Diagnosemethodik bleiben unverändert. RS485 bleibt passiv; die Diagnostic Engine bleibt `0.4.12`.
- Guardian Battery und Add-on sind `0.7.16`.

## 0.7.15 – Position History Integrity + Diagnostic Topology UI

- Schützt die Position History vor partiellen Startup- und Poll-Zuständen: Erst ein vollständig erfolgreicher, gesunder Poll darf History-Kandidaten bestätigen. Poll-Exceptions und Reconnects bestätigen keine Änderung und setzen Guard beziehungsweise Kandidaten kontrolliert zurück.
- Verschärft die Removal-Bestätigung auf genau eine erwartete fehlende Position bei weiterhin gesunder Restkommunikation. Bestätigte Addition, Reintegration und Removal werden atomar auf den letzten vollständigen Snapshot angewendet; partielle Zustände erzeugen keine falschen Leereinträge.
- Stellt physisch vorhandene, aber nicht zur Solltopologie gehörende Module weiterhin vollständig diagnostisch dar. `present + not_expected` behält seinen aktuellen Diagnosewert und seine Diagnosefarbe und erhält separat die Kennzeichnung `NICHT ERWARTET`.
- Trennt die Live-Semantik für `stale`, `absent` und `unknown`: veraltete Befunde werden als `VERALTET` markiert, entfernte Module nicht als aktuell diagnostiziert und unbekannte Zustände nicht als sichere aktuelle Diagnose dargestellt. Die 15-Zellen-Detailsicht bleibt für vorhandene Zusatzmodule einschließlich Topologiehinweis und beobachteter Seriennummer erreichbar.
- Diagnosealgorithmen, Grenzwerte und Farben bleiben unverändert; die Diagnostic Engine bleibt `0.4.12`. RS485 bleibt passiv.
- Guardian Battery und Add-on sind `0.7.15`.

## 0.7.14 – Runtime Identity / Discovery Hotfix

- Behebt den Runtime-Fehler, bei dem ein Resolver-`datetime` den numerischen direkten `0x93`-Frame-Timestamp überschrieb und `update_rs485_observations()` anschließend an `float(timestamp)` scheiterte. Numerische, ISO- und `datetime`-Zeitstempel werden kontrolliert verarbeitet; ungültige Identitäten bleiben pro Eintrag isoliert und beenden nicht den gesamten Poll-Zyklus.
- Trennt Discovery/Acquisition von der Solltopologie: `parse_pwr()` erfasst alle tatsächlich gemeldeten Module; `module_count` begrenzt nur erwartete Positionen und Missing-Alarme. Console-, Zell-, Identitäts- und History-Daten zusätzlicher Module werden nicht verworfen.
- Stellt bei Soll=5 und sechs physisch beobachteten Modulen M1–M5 als `expected=true, present` und M6 als `expected=false, present` beziehungsweise „vorhanden, aber nicht erwartet“ dar. Home meldet `5 / 5 (+1 nicht erwartet)` statt eines missverständlichen `6 / 5`; M6 bleibt verfügbar und erzeugt keinen Missing-Alarm.
- Vereinheitlicht den Dokumentationsvergleich von sichtbarer Tabelle und Abweichungsbanner auf `last_documented_serials()`. Nichtbeobachtung bleibt eine Presence-/Availability-Frage und wird nicht automatisch als Identitätsabweichung gezählt.
- Eine stabil bestätigte Reintegration eines zusätzlichen Moduls kann weiterhin genau einen vollständigen Position-History-Snapshot erzeugen, unabhängig davon, ob seine Position zur Solltopologie gehört.
- Diagnoseberechnung und Diagnostic Engine bleiben unverändert `0.4.12`; RS485 bleibt vollständig passiv.
- Real abzunehmen bleiben sechs von Guardian erkannte Module, Home `5 / 5` plus Zusatzhinweis, M6 present/not expected ohne Missing, RS485-Management, der Reintegration-Snapshot und ein konsistenter Abweichungsbanner.
- Guardian Battery und Add-on sind `0.7.14`.

## 0.7.13 – Unified Live Topology & RS485 Identity Restore

- Vereinheitlicht aktuelle Projektionen auf der zentralen Solltopologie und Presence mit den getrennten Zuständen `present`, `stale`, `absent`, `unknown` und `not_expected`. `module_count` bestimmt den Nenner und die alarmwirksamen Positionen; historisch bekannte höhere Positionen bleiben ausschließlich Inventar und Historie.
- Trennt Availability-/Missing-Ursachen vom unabhängig diagnostisch auffälligsten Modul. Bei Solltopologie 5 und vier aktuell vorhandenen Modulen lautet die Home-Zusammenfassung `4 / 5`; Position 6 erweitert weder Nenner noch Missing-Semantik.
- Schaltet Presence vor Live-Messwerte und Cell-Diagnostics-Projektionen. Retained Werte von entfernten, veralteten oder nicht erwarteten Modulen bleiben historisch erhalten, werden aber nicht als aktuelle Livewerte angeboten; es entstehen keine Nullwerte und keine neue Diagnoseberechnung.
- Erhält alle bekannten physischen Module als Inventar und ergänzt deren aktuellen Topologiestatus. Die Positionshistorie zeigt lokale Zeit als `DD.MM.YY HH:MM`, ohne Snapshots zu verändern oder fehlende Ereignisse zu synthetisieren.
- Rekonstruiert beim Start bekannte ADR↔Seriennummer-Identitäten begrenzt und read-only aus append-only RS485-Evidence mit der zentralen `0x93`-Decoderlogik. `identity_known` bleibt von `identity_currently_confirmed` getrennt; historische Identität erzeugt keine frische Presence, während ein neues direktes `0x93` bestätigt oder ersetzt.
- Ergänzt kompakte `0x93`-Zähler und änderungsbezogene Identity-Logs ohne Rawframes. Bestehende ADR-basierte MQTT-IDs, Topics und Discovery Keys bleiben kompatibel; Live-Availability wird positionsbezogen aktualisiert, ohne Registry-Migration oder Entity-Duplikate.
- Nach Installation real zu prüfen bleiben Home `4 / 5`, Availability von M5/M6, Cell-Diagnostics-Liveprojektion, Inventory-Presence, Startup-Restore und anschließende Live-Bestätigung durch ein neues `0x93`, Position-History-Uhrzeit sowie die Discovery-Aktualisierung bestehender MQTT-Entities.
- Guardian Battery und Add-on sind `0.7.13`; RS485 bleibt passiv und diagnoseisoliert, die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.12 – RS485 Identity & Topology Presence

- Dekodiert passive Pylontech-V3.3-Responses für Kommando `0x93` als Command plus exakt 16 unveränderte ASCII-Bytes und erhält die Rawbytes. Historische gültige Raw Evidence kann deterministisch mit Provenienz `historical_raw_redecode` erneut dekodiert werden; bereits gespeicherte Dekodierung bleibt als `stored_decoded` kenntlich.
- Verwendet die direkt beobachtete Zuordnung ADR → physische Seriennummer. Eine Position wird ausschließlich über die zum Beobachtungszeitpunkt wirksame Positionshistorie aufgelöst; es gibt keine ADR→Position-Formel und unbekannte Identität bleibt `unresolved`.
- Entfernt die ADR-Auswahl aus normalen Zeitverlaufsansichten. Modul und Seriennummer bilden die Benutzeridentität im Zeitverlauf und in der RS485-/BMS-Managementansicht; ADR bleibt technische Provenienz- und API-Dimension.
- Trennt Solltopologie, aktuelle Presence und Positionshistorie. Console und aktuelle `0x93`-Evidenz speisen die Zustände `present`, `stale`, `absent`, `unknown` und `not_expected`; alte Console-Caches und identische alte `0x93`-Beobachtungen erneuern beziehungsweise vervielfachen Presence-Bestätigungen nicht.
- Erzeugt vollständige Positionssnapshots nur nach bestätigten Topologieänderungen. Neustarts, kurze Kommunikationsausfälle, globale Ausfälle und identische Zustände erzeugen keinen Snapshot; die letzte dokumentierte Seriennummer bleibt nach einem Removal historisch sichtbar.
- Begrenzt Missing-Alarme auf die konfigurierte Solltopologie `1..module_count`. Historisch dokumentierte höhere Positionen bleiben erhalten und werden aktuell als `not_expected` statt fehlend behandelt.
- Erhält die bestehenden ADR-basierten MQTT-Entity-IDs und Topics. Friendly Names und Attribute können nach sicherer Identitätsauflösung unter demselben Discovery-Key aktualisiert werden, ohne Registry-Migration oder Entity-Duplikate.
- RS485 bleibt vollständig passiv. CCL/DCL und Enable-Zustände werden unverändert und ohne Kausalitätsbehauptung dargestellt; Cell Diagnostics, Gesamtbewertung, Confidence, Evidence Diagnostics, Maintenance Risk und Phasenklassifikation bleiben isoliert.
- Guardian Battery und Add-on sind `0.7.12`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.11 – RS485 Management Freshness

- Trennt den RS485-Reader-/Busstatus von der Aktualität der zuletzt gültig beobachteten 0x92-Managementwerte. Ein älterer Wert bleibt mit `management_freshness=stale` und `sample_age_seconds` erhalten und wird bei einem weiterhin `listening` meldenden Bus nicht allein wegen seines Alters `unavailable`.
- Verwendet die bestehende konfigurierbare Freshness-Schwelle mit einem konservativen Default von 600 Sekunden. Sie klassifiziert ausschließlich `current`/`stale` und berücksichtigt die real beobachteten variablen 0x92-Blockabstände von mehr als 300 Sekunden.
- Erhält CCL/DCL einschließlich Vorzeichen sowie Enable als letzten beobachteten Zustand `ENABLED`/`STOP REQUEST`; es entstehen keine Nullwerte, künstlichen History-Samples, Diagnose- oder Kausalitätsaussagen.
- Guardian Battery und Add-on sind `0.7.11`; die Diagnostic Engine bleibt fachlich unverändert `0.4.12`.

## 0.7.10 – RS485 Evidence Runtime Instrumentation

- Protokolliert den Start des RS485 Evidence Writers mit Zielpfad, den ersten erfolgreich persistierten Evidence Record ohne Raw Frame sowie Writerfehler mit Pfadkontext.
- Ergänzt `last_error` im Writerstatus und einen Integrationstest der tatsächlichen `main.py`-Verdrahtung von aktiviertem Reader, 0x92-Callback, Writerstart, automatischer Verzeichniserzeugung und append-only Tagesdatei.
- Evidence-Store-Pfade, RS485-Decoder und Acquisition, MQTT, History, UI, Diagnosemethoden und Diagnostic Engine bleiben fachlich unverändert. Die Diagnostic Engine bleibt `0.4.12`.
- Guardian Battery und Add-on sind `0.7.10`.

## 0.7.9 – RS485 Evidence Persistence & Projection

- Persistiert passive RS485-Evidenz append-only in `rs485_history/YYYY-MM-DD.jsonl`: vollständige 0x92-Managementevidenz, 0x44 Raw Evidence und gedrosselte 0x42-Samples zur Cross-Validation. Eine begrenzte Writer-Queue mit Drop-Metrik schützt die Acquisition vor blockierendem I/O.
- Erfasst deterministische RS485-Zustandsänderungen als Beobachtungen mit old/new, ADR, Frame-Provenienz und Data-Quality-Feldern. Daraus folgt ausdrücklich keine bestätigte Kausalität oder Diagnosewirkung.
- Projiziert Busstatus sowie CCL, DCL, CVL, DVL, Charge/Discharge Enable und weitere Managementwerte kompakt über MQTT/Home Assistant. Das DCL-Vorzeichen bleibt erhalten; Enable wird als `STOP REQUEST` beziehungsweise `ENABLED` dargestellt.
- Erweitert History API, Guardian-Zeitverlauf und die RS485-/BMS-Managementansicht um ADR-basierte Einzel-/Gemeinsam-Verläufe und eine zustandserhaltende Enable-Step-Darstellung. ADR wird nicht als Modulposition oder physische Identität interpretiert.
- Guardian sendet weiterhin keine RS485-Kommandos. Cell Diagnostics, Evidence Diagnostics, Maintenance Risk, Status, Confidence und Phasenklassifikation bleiben isoliert; die Diagnostic Engine bleibt unverändert `0.4.12`.
- Offen für die reale Home-Assistant-/Hardware-Abnahme bleiben Phase-C-Persistenz, MQTT-/History-/UI-Verhalten, physischer Disconnect/Reconnect, ein möglicher Open-Pegelimpuls, die reale Langzeitdatenmenge und ADR↔physische Identität. 0x44 bleibt Raw-only, soweit keine eindeutige Dekodierung belegt ist.
- Guardian Battery und Add-on sind `0.7.9`.

## 0.7.8 – Passive RS485 Evidence Acquisition – Runtime Foundation

- Trennt Pylontech Console und passiven RS485-Sniffer in sichere serielle Rollen. Waveshare `1A86:55D3` wird erkannt und von Console-auto ausgeschlossen; Mehrdeutigkeit schlägt kontrolliert fehl statt einen zufälligen Port zu wählen.
- Ergänzt einen rein lesenden Runtimepfad für 115200/8N1 mit Pylontech-HEX-ASCII-Framing, LENGTH/LENID, LCHKSUM, CHKSUM, zeitlich begrenzter Request-/Response-Korrelation und kontrolliertem Reconnect.
- Dekodiert 0x92 als direkte Protokollevidenz: CVL/DVL, CCL/DCL einschließlich Rohwert und Vorzeichen, Charge/Discharge Enable, Charge Immediately 1/2 und Full Charge Request. Begrenzte Runtime-Metriken und kompakte Abnahmelogs erzeugen keine Raw-Frame-Logflut.
- `rs485_sniffer_enabled` bleibt standardmäßig `false`; ohne explizite Aktivierung wird kein RS485-Port geöffnet. Guardian sendet über diese RS485-Schicht keine Protokolltelegramme.
- Ein möglicher hardware- oder treiberbedingter Pegelimpuls beim pyserial-Open kann erst am realen Adapter geprüft werden.
- Nicht enthalten sind RS485-Persistenz, MQTT-/HA-Entities, Dashboard-/Config-UI, History-Projektion, Diagnosekopplung oder Kausalitätsbewertung. Die Diagnostic Engine bleibt `0.4.12`.

## 0.7.7 – HA collapsible cards and local day boundaries

- Lädt die Guardian-Custom-Card in YAML-Lovelace-Konfigurationen über den wirksamen `resource_mode: yaml` und die cache-gebrochene Ressource `/local/guardian-collapsible-card.js?v=2`.
- Erzeugt die Markdown-Child-Card lifecycle-sicher und idempotent, erhält unabhängige Open-States bei normalen `hass`-Updates und zeigt kontrollierte Lade- beziehungsweise Fehlerzustände. Die reale Home-Assistant-Abnahme dieses Collapsible-Card-Fixes ist PASS.
- Markiert in mehrtägigen Guardian-Zeitverläufen jede innere lokale Tagesgrenze dezent bei 00:00 mit Datumslabel; Start und Ende werden nicht künstlich markiert und Sommer-/Winterzeit mit 23-/25-Stunden-Tagen wird berücksichtigt.
- Tagesmarker gelten für Einzel- und Gemeinsam-Ansicht sowie alle Größen der gemeinsamen History-/Chart-Infrastruktur, ohne Messwerte, Phasenflächen, Maintenance-/Lifecycle-Marker, Tooltips oder Datenabfragen zu verändern.
- Diagnostic Engine `0.4.12`, Diagnosealgorithmen, Evidence Diagnostics, Diagnosegrenzen, Maintenance Risk, SOC-Modulmedian und relative Endpoint-Semantik bleiben unverändert.
- Nach regulärer Installation von 0.7.7 bleibt die reale HA-Abnahme der Mitternachtsmarker, Datumslabel, Markerüberlagerungen, mehrtägigen Einzel-/Gemeinsam-Ansichten und Smartphone-Darstellung offen.
- Guardian/Add-on sind `0.7.7`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.6 – HA UI fixes and diagnostic robustness

- Ersetzt die in Home-Assistant-Markdown nicht zuverlässig interaktiven Diagnosebereiche durch eine HA-kompatible Custom Card; alle vier Bereiche je Zellansicht sind unabhängig und initial geschlossen.
- Ordnet die aktuelle physische Stackdarstellung von Position 6 oben bis Position 1 unten an.
- Entfernt die redundante Spalte „aktuell“ aus der Positionshistorie, zeigt frühere Zustände von neu nach alt und bereitet die horizontale Navigation mit fester Positionsspalte vor.
- Validiert Zellspannungsarrays im `CellDiagnosticStore` defensiv; unvollständige, überzählige oder nicht-endliche Zellarrays verursachen weder einen Absturz noch ein Diagnosesample.
- Diagnostic Engine `0.4.12`, Diagnosegrenzen, Phasenlogik, Evidence Diagnostics, Maintenance Risk, SOC-Modulmedian und relative Endpoint-Semantik bleiben unverändert.
- Offen für die reale Home-Assistant-Abnahme bleiben die produktive Custom Card, der Open-State bei einem vollständigen View-Rebuild, reales horizontales Scrollen und die Smartphone-Darstellung.
- Guardian/Add-on sind `0.7.6`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.5 – Stack SOC and maintenance diagnostics

- Macht die vier Diagnosebereiche aller Zellansichten unabhängig einklappbar, initial geschlossen, und vereinheitlicht die fünfteilige Navigation unter „Guardian Maintenance“.
- Ordnet die Positionshistorie vom aktuellen Stand links bis zum ältesten Stand rechts und verhindert redundante unveränderte Positionssnapshots.
- Ergänzt den aktuellen und historischen Stack-SOC-Median sowie die vorzeichenbehaftete Modulabweichung in Prozentpunkten anhand der zum Messzeitpunkt dokumentierten physischen Stackbelegung; Ausbau, Wiedereinbau, Positionswechsel und Modultausch bleiben identitätssicher getrennt.
- Erweitert Maintenance-Marker und projiziert dokumentierte Eingriffe, einschließlich manuellem Balancing, konservativ als Lifecycle-Grenze oder Diagnosekontext, ohne Rohmessungen zu verändern.
- Ergänzt relative Lade-/Entladeendpunkte ausschließlich als Beobachtung sowie additive Evidenz- und Kausalitätsmetadaten; daraus folgt keine automatische BMS-, Wechselrichter-, Zellfehler- oder Kausalitätsaussage.
- Cell Diagnostics, statuswirksame Grenzwerte, Confidence, Evidence Diagnostics, Maintenance Risk, Rohdatenerfassung und absolute Phasenlogik bleiben unverändert; die Diagnostic Engine bleibt `0.4.12`.
- Offene Abnahmepunkte bleiben das noch nicht acquisition-validierte 30-s-Synchronitätsfenster, fehlende individuelle Live-Staleness-Timestamps, der noch nicht synchronisierte Peer-Zyklusvergleich und die reale Home-Assistant-Browserprüfung der nativen `<details>`-Elemente.
- Guardian/Add-on sind `0.7.5`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.4 – Compact MQTT projection

- Ersetzt die vollständige Diagnoseobjekt-Serialisierung in `guardian/battery/state` durch stabile kompakte Modulprojektionen und verhindert dadurch wiederkehrende Mosquitto-Trennungen wegen übergroßer Pakete.
- Reduziert Zellstatus-Attribute auf Current Condition, Confidence, Phasenstatus/-samplezahlen, Trend, Maintenance Risk, Qualitätsstatus, kurze Begründung, Methodenzusammenfassungen und Provenienz-ID.
- Hält vollständige Advanced Diagnostics, Methods, Evidence Families, Sequenzarrays, Maintenance-Kontextlisten, Rohsamples und Aggregate aus MQTT fern; intern bleiben sie vollständig verfügbar.
- Erzwingt maximal 65.536 Byte je MQTT-Payload und maximal 16.384 Byte je Entity-Attributpayload; Texte werden deterministisch begrenzt.
- Home-Assistant-Discovery, Retained-Verhalten und sämtliche Diagnosemethoden bleiben unverändert.
- Guardian/Add-on sind `0.7.4`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.3 – Physical-identity Current Condition

- Verwendet die physische Modulseriennummer als historische Primärachse der klassischen Current Condition; sicher identifizierte Samples bleiben bei Umpositionierungen positionsübergreifend zusammenhängend.
- Trennt Modultausche strikt nach Serienidentität und hält `cell_diag_history_max_samples` je physischem Modul statt je historischer Position.
- Führt Coverage-Schema 2 mit materialisierter Identitäts-, Samplezahl- und Zeitbereichsabdeckung ein; 0.7.2-Marker lösen einmalig einen vollständigen Raw-History-Neuaufbau aus.
- Übergibt dieselbe korrigierte identitätszentrierte Samplefolge an Balancing- und Advanced-Evidence-Verfahren, ohne deren Fachmethodik zu verändern.
- Current-Condition-Formel, Vier-Phasen-Logik, Grenzwerte, Status und Confidence bleiben unverändert; `diagnostic_aggregates.json` bleibt eine getrennte Langzeitquelle.
- Guardian/Add-on sind `0.7.3`; die Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.2 – Current-Condition raw-history rebuild

- Rekonstruiert den begrenzten klassischen Current-Condition-Arbeitscache beim Start aus geeigneten Rohsamples der vorhandenen `cell_history/*.jsonl`-Tagesdateien.
- Verwendet explizite Sample-Seriennummern oder ausschließlich die dokumentierte Positionshistorie zum Samplezeitpunkt; unbekannte physische Identität wird nicht geraten.
- Dedupliziert Cache- und Rohsamples, hält das konfigurierte Ringpufferlimit ein und liest unveränderte vollständig erfasste Dateien anhand persistenter Coverage-/Dateisignaturen nicht erneut.
- Schreibt nur `cell_diagnostics.json` atomar neu; Rohhistorie und getrennte `diagnostic_aggregates.json` bleiben unverändert.
- Die Current Condition verwendet weiterhin unverändert die Originalmethodik und bewertet den rekonstruierten Ringpuffer mit der aktuell aktiven Diagnosekonfiguration; eine retrospektive As-was-Neubewertung ist nicht enthalten.
- Guardian/Add-on sind `0.7.2`; die statuswirksame Diagnostic Engine bleibt fachlich unverändert `0.4.12`.

## 0.7.1 – Historical aggregate backfill

- Aggregiert beim Start fehlende geeignete Samples aus vorhandenen append-only `cell_history/*.jsonl`-Tagesdateien in die versionierten Evidence-Diagnostics-Aggregate nach.
- Erkennt unveränderte vollständig abgedeckte Quellen anhand Dateisignatur, Config-ID und gespeicherter Aggregatabdeckung; wiederholte Starts zählen keine Samples doppelt.
- Baut teilweise vorhandene Tagesaggregate kanonisch aus der Rohhistorie auf und arbeitet danach normal inkrementell weiter.
- Verwendet explizite Seriennummern oder die Positionshistorie zum Samplezeitpunkt; unbekannte physische Identität wird nicht geraten.
- Überspringt beschädigte Einzelzeilen robust und verändert, migriert oder löscht keine historische JSONL-Datei.
- Guardian/Add-on sind `0.7.1`; die statuswirksame Diagnostic Engine bleibt unverändert `0.4.12`.

## 0.7.0 – Evidence-based cell diagnostics

- Ergänzt phasengetrennte historische Ranking-Drift aus robusten Tages-/Phasenaggregaten, ohne Rangwechsel allein als Alarm zu verwenden.
- Bewertet natürliche Stromsprünge ausschließlich als relativen dynamischen Widerstandsindex; absolute mΩ werden bei der vorhandenen Abtastung nicht ausgewiesen.
- Ergänzt relative Capacity-Consistency für obere/untere Spannungsbereiche und Lade-/Entladerichtung sowie Vᵢ(Q)-Kurvenevidenz auf einem gemeinsamen, interpolierten Q-Raster ohne Extrapolation. Reproduzierbarkeit und Ruhe-/Relaxationsdrift besitzen strikt ablehnende Quality Gates.
- Persistiert kompakte, versionierte Tages-/Phasenaggregate getrennt von Rohdaten, damit Langzeit-Ranking nach Neustarts ohne wiederholte Rohhistorien-Scans verfügbar bleibt.
- Ordnet real gemeldete BMS-Balancing-Samples und dokumentierte Maintenance-Ereignisse anhand der physischen Serienidentität als erklärenden Kontext zu, ohne Positionsgleichheit, Herstellerkriterien oder Kausalität zu erfinden.
- Trennt Current Condition, Trend, Maintenance Risk sowie Current-Condition- und Trend/Risk-Confidence. Kapazitäts- und Kurvenevidenz zählen als eine unabhängige Evidenzfamilie; ein Wartungshinweis erfordert eine harte Current-Condition-Regel oder konvergierende qualifizierte Familien. Es gibt keinen neuen Health Score, keine RUL-, Ausfallzeit- oder Ausfallwahrscheinlichkeitsprognose.
- Prüft ICA/DVA ausschließlich auf Datenbereitschaft; eine ICA-/DVA-Berechnung wird nicht aktiviert.
- Current Condition, Trend und Maintenance Risk bleiben getrennte Dimensionen; Trend/Risk Confidence berücksichtigt Datenabdeckung, Beobachtungsdauer, Ereignisse, Reproduzierbarkeit und unabhängige Evidenzfamilien.
- Alle Quality Gates sind experimentell und müssen mit realen Felddaten validiert werden. Unzureichende Evidenz liefert regulär `NICHT BEWERTBAR`.
- Guardian/Add-on sind `0.7.0`; die bestehende statuswirksame Diagnostic Engine bleibt fachlich unverändert `0.4.12`.

## 0.6.8 – Native Guardian Maintenance sidebar entry

- Benennt den nativen Add-on-Ingress-Seitenleisteneintrag in `Guardian Maintenance` um.
- Verwendet für den nativen Ingress-Eintrag das Icon `mdi:wrench-clock`.
- Add-on-Name und Slug bleiben unverändert; Diagnostic Engine weiterhin `0.4.12`.

## 0.6.7 - Combined history view and dynamic phase limits

- Ergänzt den Zeitverlauf um den Modus „Gemeinsam“ mit Mehrfachauswahl von
  SOC, Strom, Zellspannung und Zelltemperatur.
- Zeigt getrennte, zeitsynchronisierte Diagrammspuren mit eigener Y-Skalierung
  und unabhängiger Zellenauswahl für Zellspannung und Zelltemperatur.
- Verwendet eine gemeinsame Phasen- und Maintenance-Projektion sowie
  Single-Pass-Verarbeitung für kombinierte Zeitreihen.
- Zeigt die phasenspezifischen Diagnosegrenzen dynamisch aus der aktiven
  Guardian-Konfiguration mit einheitlicher Statussemantik NORMAL, BEOBACHTEN,
  AUFFÄLLIG und KRITISCH.
- Guardian/Add-on `0.6.7`; Diagnostic Engine unverändert `0.4.12`.

## 0.6.6 - Restore the cell diagnostics overview

- Stellt die geordnete Zelldiagnostik-Hauptübersicht wieder her.
- Entfernt unbeabsichtigt eingebaute Navigations- und Ingress-Karten.
- Erhält die Zell-Gesamtbewertung und phasenbezogene Evidenz aus 0.6.5
  vollständig; Diagnostic Engine unverändert `0.4.12`.

## 0.6.5 - Cell diagnostic explainability

- Vereinfacht die Bereichserklärung im Zeitverlauf und trennt sie von der
  diagnostischen Bewertungsmethodik.
- Kennzeichnet die Zell-Gesamtbewertung und ihren maßgeblichen Diagnosebereich
  eindeutig.
- Bewertet die vier Evidenzbereiche unmittelbar in der Reihenfolge Entladung,
  Tiefbereich, Ladung und Hochbereich und zeigt ihre phasenspezifischen
  Grenzwerte.
- Zeigt bei unzureichender Evidenz klar die LERNPHASE; Confidence bleibt
  ausschließlich eine Eigenschaft der Zell-Gesamtbewertung.
- Trennt Bereichsfarben von Statusfarben und stellt die Bewertungs-Hilfe direkt
  in der Zelldiagnostik bereit.
- Guardian/Add-on `0.6.5`; Diagnostic Engine unverändert `0.4.12`.

## 0.6.4 - Direct navigation and operational analysis controls

- Entfernt das zusätzliche Funktionsportal: Der stabile Add-on-Ingress öffnet
  direkt „Module & Stack“, während Guardian Home Andreas unmittelbar zu
  Modulen, Analyse, Maintenance-Verlauf und Konfiguration verlinkt.
- Zeigt die Zell-Mehrfachauswahl nur für Zellspannung und Zelltemperatur und
  verbindet Einzel-, Alle- und Keine-Auswahl sichtbar mit dem API-Query-State.
- Entfernt Aktivitäts- und Phasenschalter aus der Analyse; die getrennte Visual
  Phase Projection bleibt permanent aktiv und beschriftet Phasen deutsch.
- Stellt eine direkt erreichbare Hilfe für Diagnostic Phase, visuelle Glättung,
  Confidence und dynamisch geladene phasenspezifische Grenzwerte bereit.
- Guardian/Add-on `0.6.4`; Diagnostic Engine unverändert `0.4.12`.

## 0.6.3 - Guardian portal and history usability

- Zentraler Ingress-Einstieg als Funktionsportal für Information, Analyse,
  Maintenance und Konfiguration; direkte Dashboard-Kacheln für Analyse und Module.
- Maintenance-Verlauf startet unbegrenzt mit Maintenance-Ereignissen, sortiert neueste
  zuerst und zeigt historisch belegte Seriennummern zum Ereigniszeitpunkt.
- Zeitverlaufsanalyse unterstützt eine effiziente Mehrfachauswahl von Zellen mit
  gemeinsamer Legende, bestehendem Single-Pass-Scan, Cache und Downsampling.
- Rücklinks transportieren den Zustand der aufrufenden Verlauf-/Analyseansicht.
- Guardian/Add-on `0.6.3`; Diagnostic Engine unverändert `0.4.12`.

## 0.6.2 - Analysis navigation and stack-centred position history

- Ergänzt auf dem Guardian-Dashboard die eigenständige Kachel
  „Zeitverläufe & Analyse“ als direkten Einstieg in die vorhandene
  Guardian-History mit Visual Phase Projection.
- Belässt Home-Assistant-Standard-`history-graph`-Karten ausdrücklich
  unverändert und verwendet keine fragile zweite Sidebar-Registrierung.
- Stellt bestätigte Positionsänderungen als stackzentrierte Matrix dar:
  Positionen in Zeilen, vollständige Change-Date-Snapshots in Spalten und ein
  klar markierter aktueller Stackzustand.
- Zeigt zunächst 20 Change-Dates und lädt ältere in weiteren 20er-Schritten,
  ohne die append-only Historie zu begrenzen oder umzuschreiben.
- Unterscheidet Erstidentifikation, Positionsänderung, Modulaustausch,
  hinzugefügtes und entferntes Modul semantisch. Unbekannt → Seriennummer wird
  nicht länger als Modultausch bezeichnet.
- Guardian/Add-on `0.6.2`; Diagnostic Engine unverändert `0.4.12`.

## 0.6.1 - Visual phases, scalable history and physical identity

- Trennt diagnostische Phasen strikt von einer zeitstabilisierten Visual Phase
  Projection mit Mindestdauer, Hysterese und Short-Gap-Merging.
- Stellt die zentrale Projektion für SOC, Strom, Zellspannung und
  Zelltemperatur bereit; der Benutzer kann die Phasenebene ausblenden.
- Liest Guardian-JSONL-History nur einmal pro Anfrage, begrenzt die
  Darstellungsdaten extrema-erhaltend und invalidiert einen LRU-Cache anhand
  der Quelldateisignatur. Rohdaten bleiben unverändert.
- Verknüpft neue Messsamples mit der zum Messzeitpunkt dokumentierten
  physischen Seriennummer; frühere unbekannte Identitäten bleiben unbekannt.
- Stabilisiert BMS-Seriennummern über drei erfolgreiche Lesungen und schreibt
  bestätigte Positionsänderungen über System-Maintenance-Event und append-only
  Positionssnapshot fort.
- Zeigt physische Seriennummern mit ihren Positionszeiträumen in den
  Modulinformationen.
- Guardian/Add-on `0.6.1`; Diagnostic Engine unverändert `0.4.12`.

## 0.5.1 - Maintenance identity and history UI patch

- Patch-Release des unter `6144cfa` abgenommenen Korrekturpakets für einen
  regulären Home-Assistant-Updatepfad von Guardian/Add-on 0.5.0 auf 0.5.1.
- Maintenance verwendet in der normalen UI Aktiv/Nicht aktiv statt
  Archivieren/Wiederherstellen; die bestehende append-only Revisionshistorie
  bleibt ohne Datenmigration erhalten.
- Stackposition und optionale physische Modulidentität/Seriennummer bleiben
  getrennt. Unbekannte historische Identität wird nicht rückwirkend geraten.
- Zellmetriken unterstützen „Alle Zellen / Modulebene“.
- Überarbeitet responsives Layout und gemeinsame History-Charts mit scharfem
  SVG-Rendering, lokaler X-Zeitachse, verbesserter Y-Achse, Grid und Tooltip.
- Maintenance-Marker bleiben eine getrennte Overlay-Schicht.
- Diagnostic Engine bleibt unverändert `0.4.12`.
- Phase Engine und Phase Overlay sind nicht Bestandteil von 0.5.1.

## 0.5.0 - Maintenance logbook, timeline and Home Assistant events

- Ergänzt ein eigenständiges Maintenance-Logbuch mit append-only
  JSONL-Persistenz unter `/share/guardian_battery/maintenance_events.jsonl`.
- Verwendet stabile `maintenance_event_id`-Werte, lückenlose Revisionen und
  Optimistic Concurrency; Änderungen erzeugen neue Revisionen.
- Archiviert und restauriert Einträge ohne Löschen ihrer Historie.
- Ergänzt Maintenance API und Ingress-UI einschließlich Detailansicht und
  zentral wiederverwendbaren Maintenance-Deep-Links.
- Trennt den fachlichen Ereigniszeitpunkt `occurred_at` vom Erfassungszeitpunkt
  `created_at`; rückdatierte Ereignisse werden historisch korrekt positioniert.
- Ergänzt eine zentrale Guardian-Timeline und read-only Maintenance-Marker in
  SOC-, Strom-, Zellspannungs- und Zelltemperatur-History-Ansichten.
- Ergänzt eine Home-Assistant-MQTT-Event-Entity über MQTT Discovery.
- Publiziert nur neue manuell erfasste Live-Ereignisse mit höchstens
  300 Sekunden Abstand zwischen `occurred_at` und `created_at`.
- Maintenance-Events werden mit `retain=false` gesendet und nach einem
  Neustart nicht erneut abgespielt.
- Backfill, Bearbeitung, Archivierung und Wiederherstellung lösen keine
  MQTT-Maintenance-Nachricht aus.
- Verändert weder Home-Assistant-Recorder noch bestehende Messhistorien und
  erzeugt keine künstlichen Sensorwerte.
- Guardian- und Add-on-Version werden auf `0.5.0` angehoben. Die unveränderte
  diagnostische Bewertungslogik behält Diagnostic Engine `0.4.12`.
- Nach produktiver Erstabnahme wird die Bediensemantik rückwärtskompatibel von
  Archivierung auf Aktiv/Nicht aktiv präzisiert; die append-only Daten bleiben
  unverändert lesbar.
- Trennt Stackposition und optionale physische Modulseriennummer ausdrücklich,
  ergänzt die Modulebene für Zellmetriken und verbessert die gemeinsame
  History-Komponente um skalierbares SVG, Zeit-/Wertachsen, Grid und Tooltip.

## 0.4.0

- Trendanalyse für Zellspreizung und SOC
- konfigurierbares Trendfenster
- persistenter Incident-Status mit Haltezeit
- neue MQTT-Sensoren für Trend und Incident
- Datenablage unter `/share/guardian_battery/`

## 0.4.1
- Guardian Cell Diagnostics: separate evidence-based assessment of all 15 cell channels per detected module.
- Phase-resolved voltage consistency for charge, discharge, low-SOC, high-SOC and rest.
- Per-cell status and confidence; no synthetic cell-health percentage.
- Pylontech BMS SOH and cycle count published separately.
- Data collection placeholders for dynamic resistance, capacity consistency, rest/drift and ICA/DVA.
- Existing 0.4.0 Health Engine, trend and incident logic retained unchanged in purpose.

## 0.4.2
- Complete per-cell MQTT publication for Cell Diagnostics.
- Adds phase-resolved low/discharge/charge/high deviations per cell.
- Adds Lowest shares and mean ranks for low-SOC/discharge evidence.
- Keeps Pylontech BMS SOH separate from Guardian cell consistency assessment.

## 0.4.3
- Explainable Diagnostics UI foundation.
- Every new diagnostic numeric entity has an explicit unit or a clear dimensionless/rank meaning.
- MQTT diagnostic entities publish Home Assistant attributes with definition, source, unit, method, phase and interpretation limits.
- Adds valid sample counts for low-SOC, discharge, charge and high-SOC phases.
- Adds mean ranks for charge/high-SOC and Highest shares for charge/high-SOC.
- Keeps Guardian cell-consistency evidence strictly separate from Pylontech BMS SOH.
- No retroactive modification of 0.4.2 raw cell history.

## 0.4.4
- Publishes the current median of all 15 cell voltages for every detected module as a dedicated MQTT/Home Assistant sensor.
- Adds explainability metadata for the module median; unit is mV.
- Enables direct overlay of each cell voltage with the module median in Guardian Cell Diagnostics.
- Does not change historical 0.4.2/0.4.3 raw cell samples or the Cell Voltage Consistency assessment thresholds.

## 0.4.5
- Reconstructs the last 24 hours of module-cell median history from persisted Guardian `cell_diagnostics.json`.
- Uses 5-minute buckets to keep MQTT attribute size bounded.
- Publishes reconstructed history as `history_24h` on every module Zellmedian sensor.
- Does not write into or manipulate the Home Assistant Recorder database.
- Adds support for a Guardian custom Lovelace card that overlays historical cell voltage with the reconstructed module median.

## 0.4.6
- Fixes historical median publication: `CellDiagnosticStore` is now used at the main-loop call site instead of being incorrectly dereferenced from the MQTT publisher.
- Historical median reconstruction is failure-isolated per module; a UI-history failure can no longer abort the battery polling cycle.
- Keeps the 0.4.5 historical reconstruction method and MQTT attribute format unchanged.

## 0.4.8 - History Foundation and physical cell groups
- Added append-only daily JSONL cell history with schema versioning and failure isolation.
- Added physical cell group metadata: G1 cells 1-5, G2 cells 6-10, G3 cells 11-15.
- Added native Home Assistant section backgrounds for the three physical groups in each module cell overview.
- Existing 0.4.7 phase-resolved diagnostic thresholds and evaluation remain unchanged.

## 0.4.9 - Configuration provenance foundation
- Adds a central Guardian/diagnostic engine version source for runtime publication.
- Adds append-only `config_history.jsonl` in `/share/guardian_battery`.
- Records diagnostically relevant configuration only when the effective parameter set changes.
- Adds deterministic Config IDs for reproducible future As-was/As-now analysis.
- Keeps existing schema-1 cell history unchanged and backward compatible.
- Adds regression tests for configuration provenance.

## 0.4.9 – Dashboard-Konfiguration (finaler Ausbau)
- Vollständiges, strukturiertes Guardian-Konfigurationsmenü via Home-Assistant-Ingress.
- Aktuelle 0.4.9-Produktivwerte als unveränderte Standardwerte.
- Konsequenzhinweise, Wertebereiche, Reset ohne Sofort-Speicherung und explizites Validieren/Übernehmen.
- Fachliche Cross-Validierung (Warnung/Kritisch, Confidence-Reihenfolge, Phasengrenzen).
- Persistenz über die echten Supervisor-App-Optionen; keine parallele Konfigurationsquelle.
- Neustart nach erfolgreicher Übernahme; Config-Provenienz zeichnet diagnostisch relevante Änderungen auf.
- Modulanzahl 1–6 als Soll-Konfiguration; Auto-Discovery überschreibt den Sollwert nicht.
- Erweiterte technische Parameter separat gekennzeichnet.

## 0.4.10 - Config UI release
- Versionsanhebung von Guardian und Diagnostic Engine auf `0.4.10`.
- Add-on-Version in `config.yaml` auf `0.4.10` angehoben.
- Config UI verwendet für Guardian- und Diagnostic-Engine-Version die zentralen Konstanten aus `version.py` statt fest codierter `0.4.9`-Fallbackwerte.
- Verifizierter Commit: `fbf9dac` (`Guardian Battery 0.4.10 config UI release`).

## 0.4.11 - Ingress panel
- Versionsanhebung auf Guardian Battery `0.4.11`.
- Home-Assistant-Ingress-Panel für Guardian Battery aktiviert.
- Verifizierter Commit: `8db6339` (`Guardian Battery 0.4.11 enable ingress panel`).
- Weitere Detailänderungen dieses Commits sind in dieser Dokumentationsbereinigung nicht behauptet, solange sie nicht separat verifiziert wurden.

## 0.4.12 - Pylontech module information
- Versionsanhebung von Guardian Battery und Diagnostic Engine auf `0.4.12`.
- Ergänzt `info <module>` über den bestehenden seriellen Guardian-Zugriff für erkannte Pylontech-Module.
- Ergänzt Hersteller-/Identitäts- und BMS-Metadaten für die modulbezogene Home-Assistant-Info-Darstellung.
- Cycle Count / modulbezogener SOH ist ausdrücklich nicht Bestandteil dieses Arbeitsschritts.
- Isolierter `parse_info()`-Test: `5 passed`.
- Gesamte Regression-Suite einschließlich Info-Test: `26 passed` mit `PYTHONPATH=app`.
- Hardware-/Home-Assistant-Integrationstest von 0.4.12 steht nach tatsächlicher 0.4.12-Installation noch aus.
## 0.6.0 - Positionshistorie und deterministische Phasenprojektion

- Ergänzt eine persistente, append-only Positionshistorie unter
  `/share/guardian_battery/position_history.jsonl` mit vollständigen
  Stack-Snapshots für Position 1 bis 6.
- Behandelt Stackposition und physische Seriennummer als getrennte Identitäten;
  historische Identitäten werden nur aus belegten Snapshots aufgelöst und nie
  rückwirkend erfunden.
- Verknüpft jede dokumentierte Stackänderung mit einem Maintenance Event und
  bildet Austausch sowie Umpositionierung zeitabhängig ab.
- Wählt in Maintenance die zum Ereigniszeitpunkt dokumentierte Seriennummer
  automatisch vor und kennzeichnet frühere beziehungsweise spätere belegte
  Seriennummern eindeutig. Gespeicherte Event-Identitäten bleiben beim
  Bearbeiten erhalten.
- Ergänzt unter Modulinformationen die Positionshistorie sowie den getrennten
  Vergleich von dokumentierter und aktuell beobachteter Stackbelegung.
  BMS-Beobachtungen verändern die historische Wahrheit nicht automatisch.
- Zentralisiert die vorhandenen deterministischen Phasenregeln und ergänzt die
  Bewertungsmodi `historical`, `current` und flüchtiges `what_if`.
- Ergänzt das Phase Overlay in Guardian History hinter Messkurven und
  Maintenance-Markern.
- Zeigt bei fehlenden Messdaten einen Empty State ohne künstliche Y-Skala;
  zeitbezogene Maintenance-Schraubenschlüssel bleiben sichtbar und interaktiv.
- Verbessert die Darstellung aller 15 Zellkurven durch eindeutige Farben,
  Legende und Hover-Highlighting, ohne Aggregation oder Glättung.
- Verändert keine bestehenden Messwerte oder JSONL-Messhistorien und schreibt
  nicht in den Home-Assistant Recorder.
- Enthält keine KI oder lernende Klassifikation. Diagnostic Engine bleibt
  unverändert `0.4.12`.
