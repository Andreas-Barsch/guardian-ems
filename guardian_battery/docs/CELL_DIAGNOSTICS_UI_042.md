# Cell Diagnostics UI 0.4.2

0.4.2 publishes per-cell MQTT entities for all 15 cells of each configured module.

Per cell: status, confidence, voltage, current deviation from module median, evidence deviation, low-SOC deviation, discharge deviation, charge deviation, high-SOC deviation, low-SOC Lowest share, discharge Lowest share, low-SOC mean rank and discharge mean rank.

The dashboard is intentionally not auto-installed. Home Assistant assigns the final entity_id after MQTT discovery; first verify those IDs on the real system, then build the module -> 15 cells -> cell detail drill-down without stale/nonexistent module references.
