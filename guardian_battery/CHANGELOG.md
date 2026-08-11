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
