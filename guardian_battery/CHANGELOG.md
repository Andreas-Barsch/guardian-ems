# Guardian Battery Changelog

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
