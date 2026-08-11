# Guardian Battery 0.4.3 – Explainable Diagnostics UI

## Goal
Implements the highest-priority UI hardening from Design Request v1.3 without changing the scientific interpretation of the 0.4.2 raw history.

## Implemented
- explicit units for physical values;
- explicit dimensionless meaning for status/confidence and rank semantics;
- per-entity Home Assistant JSON attributes: definition, source, unit, method, phase and interpretation limit;
- valid sample counts per diagnostic phase;
- charge/high-SOC rank and Highest evidence.

## Home Assistant interaction
Native Home Assistant MQTT entities do not provide an arbitrary desktop hover-tooltip API.
Therefore 0.4.3 implements the required explanation as **More info / Info-Tap** using entity attributes.
This works without a custom frontend dependency and is also usable on touch devices.

A later custom Lovelace card may mirror the same metadata as desktop mouse-over tooltips.
The metadata catalog in `cell_diagnostics.py` is the single source of truth for both surfaces.

## Scientific boundary
A high Lowest/Highest share alone is not a defect diagnosis.
Guardian status must be interpreted with deviation magnitude, valid sample count, persistence,
phase coverage and confidence. Pylontech BMS SOH remains separate.
