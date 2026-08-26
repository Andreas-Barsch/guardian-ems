# Guardian Battery Changelog

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
