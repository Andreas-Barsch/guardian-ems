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
