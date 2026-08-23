# Guardian Cell Diagnostics 0.4.1

## Guardian Battery 0.7.0

Die bestehende statuswirksame Methode bleibt **Phase-Resolved Cell Voltage Consistency** in der festen Reihenfolge Entladung, Tiefbereich, Ladung und Hochbereich. Die Diagnostic Engine bleibt `0.4.12`.

Ergänzende Evidenzen werden getrennt ausgewiesen: historische Ranking-Drift pro Tag und Diagnosebereich, relativer dynamischer Widerstandsindex aus qualifizierten natürlichen Stromsprüngen, relative obere/untere Capacity-Consistency und Vᵢ(Q)-Kurvenkonsistenz für Lade- und Entladesequenzen, relative Ruhe-/Relaxationsdrift, real gemeldeter BMS-Balancing-Status, zeitliche Maintenance-Korrelation und ICA/DVA-Datenbereitschaft ohne aktivierte ICA/DVA-Berechnung. Vᵢ(Q) verwendet integrierten, normierten Q, lineare Interpolation auf einem gemeinsamen Raster und die punktweise Modulmediankurve; außerhalb des belegten Bereichs wird nicht extrapoliert.

Jedes Verfahren kann mit einem konkreten Grund `NICHT BEWERTBAR` liefern. Kapazitäts- und Kurvenauswertung bilden gemeinsam genau eine Evidenzfamilie; Maintenance Risk eskaliert nur durch eine harte Current-Condition-Regel oder mehrere qualifizierte unabhängige, konvergierende Familien. Current-Condition-Confidence und Trend/Risk-Confidence sind getrennt. Letztere basiert zentral auf Datenabdeckung, Beobachtungsdauer, Ereignissen, Reproduzierbarkeit und unabhängigen Familien und ist kein Prozentwert.

Langzeit-Ranking nutzt kompakte, versionierte Tages-/Phasenaggregate mit Config-ID. Sie liegen getrennt von der unveränderten append-only Rohhistorie und sind nach dokumentierter physischer Modulseriennummer getrennt. Maintenance-Ereignisse werden zum Ereigniszeitpunkt über die Positionshistorie aufgelöst; unbekannte Identität bleibt unbekannt. Absolute Zellkapazität, absolute Zellwiderstände, Hersteller-Balancing-Schwellen, Health Score, Restlebensdauer und Ausfallwahrscheinlichkeit werden nicht aus ungeeigneten Daten abgeleitet.

Die konfigurierbaren Quality Gates sind experimentell und benötigen weiterhin empirische Feldvalidierung. Ein nicht bestandenes Quality Gate oder eine unzureichende Datenbasis führt regulär zu `NICHT BEWERTBAR`, nicht zu einer geratenen Diagnose.

Cell Diagnostics is additive to the existing Guardian Battery Health Engine. It does not replace the 0.4.0 stack/module Health Score.

The new diagnostic engine stores `bat <module>` samples and evaluates all 15 cell channels using phase-resolved deviation from the module median and voltage ranks. Each cell exposes an evidence status and confidence. No cell SOH percentage is invented.

The Pylontech `stat` SOH and cycle count remain manufacturer/BMS values and are published separately.

Advanced methods (dynamic resistance, capacity consistency, rest/drift and ICA/DVA) are intentionally not interpreted until real acquisition timing/data quality has been validated.
