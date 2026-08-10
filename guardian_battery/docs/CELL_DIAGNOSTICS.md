# Guardian Cell Diagnostics 0.4.1

Cell Diagnostics is additive to the existing Guardian Battery Health Engine. It does not replace the 0.4.0 stack/module Health Score.

The new diagnostic engine stores `bat <module>` samples and evaluates all 15 cell channels using phase-resolved deviation from the module median and voltage ranks. Each cell exposes an evidence status and confidence. No cell SOH percentage is invented.

The Pylontech `stat` SOH and cycle count remain manufacturer/BMS values and are published separately.

Advanced methods (dynamic resistance, capacity consistency, rest/drift and ICA/DVA) are intentionally not interpreted until real acquisition timing/data quality has been validated.
