from __future__ import annotations

import json
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path

from evidence_diagnostics import EvidenceDiagnostics


def classify_phases(sample, options):
    """Canonical deterministic phase rule shared by live and history analysis."""
    result = []
    current = float(sample["current_a"])
    soc = float(sample["soc_percent"])
    mean_voltage = statistics.fmean(sample["voltages_mv"]) / 1000
    if current >= options["cell_diag_charge_current_a"]:
        result.append("charge")
    elif current <= -options["cell_diag_discharge_current_a"]:
        result.append("discharge")
    else:
        result.append("rest")
    if soc <= options["cell_diag_low_soc_percent"] or mean_voltage <= 3.22:
        result.append("low")
    if soc >= options["cell_diag_high_soc_percent"] or mean_voltage >= 3.38:
        result.append("high")
    return result


@dataclass
class CellSample:
    timestamp: float
    module: int
    voltages_mv: list[int]
    current_a: float
    soc_percent: float
    temperatures_c: list[float]
    balancing: list[bool]
    module_serial: str | None = None
    position_history_id: str | None = None


DIAGNOSTIC_PARAMETER_META = {
    "status": {
        "label": "Bewertung",
        "unit": "dimensionslos",
        "source": "Guardian-Berechnung",
        "definition": "Phasenbezogener diagnostischer Status der Zelle.",
        "interpretation":
            "NORMAL / BEOBACHTEN / AUFFÄLLIG / KRITISCH / LERNPHASE. "
            "Kein SOH-Prozentwert.",
    },
    "confidence": {
        "label": "Confidence",
        "unit": "dimensionslos",
        "source": "Guardian-Berechnung",
        "definition":
            "Vertrauensstufe aus Zahl geeigneter Messpunkte und Datenabdeckung.",
        "interpretation":
            "LOW / MEDIUM / HIGH. Ein auffälliger Status mit LOW Confidence "
            "ist ein vorläufiger Hinweis.",
    },
    "voltage": {
        "label": "Zellspannung",
        "unit": "mV",
        "source": "Pylontech BMS / bat",
        "definition": "Aktuell gemessene Spannung der Zellgruppe.",
        "interpretation":
            "Ein Einzelwert allein erlaubt keine Aussage über Zellgesundheit.",
    },
    "deviation": {
        "label": "Abweichung zum Modulmedian",
        "unit": "mV",
        "source": "Guardian-Berechnung",
        "definition": "ΔVᵢ = Vᵢ − Median(V₁…V₁₅).",
        "interpretation":
            "Negativ = unter Modulmedian; positiv = über Modulmedian. "
            "Bedeutung ist phasenabhängig.",
    },
    "evidence": {
        "label": "Evidenzabweichung",
        "unit": "mV",
        "source": "Guardian-Berechnung",
        "definition":
            "Größte absolute phasenbezogene Medianabweichung aus ausreichend "
            "belegten relevanten Phasen.",
        "interpretation":
            "Die Bewertung erfolgt mit den für die jeweilige Betriebsphase "
            "konfigurierten Grenzwerten.",
    },
    "lowest": {
        "label": "Lowest-Anteil",
        "unit": "%",
        "source": "Guardian-Berechnung",
        "definition":
            "Anteil gültiger Messpunkte einer Phase, in denen die Zelle "
            "die niedrigste Spannung im Modul hatte.",
        "interpretation":
            "Ein hoher Anteil allein beweist keinen Defekt.",
    },
    "highest": {
        "label": "Highest-Anteil",
        "unit": "%",
        "source": "Guardian-Berechnung",
        "definition":
            "Anteil gültiger Messpunkte einer Phase, in denen die Zelle "
            "die höchste Spannung im Modul hatte.",
        "interpretation":
            "Ein hoher Anteil allein beweist keinen Defekt.",
    },
    "rank": {
        "label": "Mittlerer Rang",
        "unit": "Rang von 15",
        "source": "Guardian-Berechnung",
        "definition":
            "Mittlere Spannungsrangposition innerhalb der 15 Zellgruppen; "
            "Rang 1 = höchste, Rang 15 = niedrigste Spannung.",
        "interpretation":
            "Dimensionsloser relativer Kennwert.",
    },
    "samples": {
        "label": "Gültige Messpunkte",
        "unit": "Messpunkte",
        "source": "Guardian-Berechnung",
        "definition":
            "Anzahl der Messpunkte, die der jeweiligen Betriebsphase "
            "zugeordnet wurden.",
        "interpretation":
            "Kleine Stichproben reduzieren die Aussagekraft und damit "
            "die Confidence.",
    },
}


STATUS_ORDER = {
    "LERNPHASE": 0,
    "NORMAL": 1,
    "BEOBACHTEN": 2,
    "AUFFÄLLIG": 3,
    "KRITISCH": 4,
}


STATUS_PHASES = (
    "discharge",
    "low",
    "charge",
    "high",
)


ALL_PHASES = (
    "discharge",
    "low",
    "charge",
    "high",
    "rest",
)


class CellDiagnosticStore:
    def __init__(
        self,
        path: Path,
        max_samples_per_module: int = 8640,
    ):
        self.path = path
        self.max_samples = max_samples_per_module
        self.identity_samples = defaultdict(lambda: deque(maxlen=max_samples_per_module))
        self.unknown_samples = defaultdict(lambda: deque(maxlen=max_samples_per_module))
        self._latest_by_position = {}
        self._documented_current_identity = {}
        self._analysis_cache = {}
        self.rebuild_sources = {}
        self.expected_materialized_coverage = {}
        self.load_error = False

        self._load()

    def _load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())

                sources = data.get("raw_history_sources", {})
                if isinstance(sources, dict):
                    self.rebuild_sources = sources
                coverage = data.get("expected_materialized_coverage", {})
                if isinstance(coverage, dict):
                    self.expected_materialized_coverage = coverage
                if isinstance(data.get("identity_samples"), dict):
                    for serial, values in data["identity_samples"].items():
                        if isinstance(serial, str) and serial:
                            self.identity_samples[serial].extend(values[-self.max_samples:])
                    for module, values in data.get("unknown_samples", {}).items():
                        self.unknown_samples[int(module)].extend(values[-self.max_samples:])
                    self._rebuild_latest_positions()
                else:
                    legacy = [value for values in data.get("samples", {}).values() for value in values]
                    self.replace_samples(legacy)
        except Exception:
            self.identity_samples.clear()
            self.unknown_samples.clear()
            self._latest_by_position.clear()
            self.rebuild_sources = {}
            self.expected_materialized_coverage = {}
            self.load_error = True

    def _rebuild_latest_positions(self):
        self._latest_by_position.clear()
        for value in self.iter_all_samples():
            module = int(value["module"])
            current = self._latest_by_position.get(module)
            if current is None or float(value["timestamp"]) >= float(current["timestamp"]):
                self._latest_by_position[module] = value

    def iter_all_samples(self):
        for values in self.identity_samples.values():
            yield from values
        for values in self.unknown_samples.values():
            yield from values

    @property
    def samples(self):
        """Compatibility projection grouped by historical module position."""
        result = defaultdict(lambda: deque(maxlen=self.max_samples))
        for value in sorted(self.iter_all_samples(), key=lambda item: float(item["timestamp"])):
            result[int(value["module"])].append(value)
        return result

    def replace_samples(self, values):
        self.identity_samples.clear()
        self.unknown_samples.clear()
        ordered = sorted(values, key=lambda item: float(item["timestamp"]))
        for value in ordered:
            serial = value.get("module_serial")
            target = self.identity_samples[str(serial)] if serial else self.unknown_samples[int(value["module"])]
            target.append(dict(value))
        self._rebuild_latest_positions()
        self._analysis_cache.clear()

    def current_serial(self, module):
        if int(module) in self._documented_current_identity:
            return self._documented_current_identity[int(module)]
        latest = self._latest_by_position.get(int(module))
        return latest.get("module_serial") if latest else None

    def set_current_identities(self, positions):
        self._documented_current_identity = {
            int(module): serial for module, serial in positions.items()
        }
        self._analysis_cache.clear()

    def values_for_module(self, module):
        serial = self.current_serial(module)
        if serial:
            return list(self.identity_samples.get(serial, ()))
        latest = self._latest_by_position.get(int(module))
        if not latest:
            return []
        latest_documented = max(
            (float(value["timestamp"]) for values in self.identity_samples.values()
             for value in values if int(value["module"]) == int(module)),
            default=float("-inf"),
        )
        return [value for value in self.unknown_samples.get(int(module), ())
                if float(value["timestamp"]) > latest_documented]

    def coverage_snapshot(self):
        result = {}
        for serial, values in self.identity_samples.items():
            if values:
                result[serial] = {
                    "sample_count": len(values),
                    "oldest_timestamp": float(values[0]["timestamp"]),
                    "newest_timestamp": float(values[-1]["timestamp"]),
                }
        return result

    def materialized_coverage_satisfied(self):
        actual = self.coverage_snapshot()
        for serial, expected in self.expected_materialized_coverage.items():
            current = actual.get(serial)
            if not current or current["sample_count"] < int(expected.get("sample_count", 0)):
                return False
            if current["newest_timestamp"] < float(expected.get("newest_timestamp", 0)):
                return False
            if (int(expected.get("sample_count", 0)) < self.max_samples
                    and current["oldest_timestamp"] > float(expected.get("oldest_timestamp", 0))):
                return False
        return True

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")

        tmp.write_text(
            json.dumps(
                {
                    "identity_samples": {
                        serial: list(values) for serial, values in self.identity_samples.items()
                    },
                    "unknown_samples": {
                        str(module): list(values) for module, values in self.unknown_samples.items()
                    },
                    "raw_history_sources": self.rebuild_sources,
                    "expected_materialized_coverage": self.expected_materialized_coverage,
                },
                separators=(",", ":"),
            )
        )

        tmp.replace(self.path)

    def add(self, sample):
        value = asdict(sample)
        if sample.module_serial:
            self.identity_samples[sample.module_serial].append(value)
        else:
            self.unknown_samples[sample.module].append(value)
        current = self._latest_by_position.get(sample.module)
        if current is None or sample.timestamp >= float(current["timestamp"]):
            self._latest_by_position[sample.module] = value
        self._analysis_cache.clear()

    @staticmethod
    def phases(sample, options):
        return classify_phases(sample, options)

    @staticmethod
    def phase_thresholds(
        phase,
        options,
    ):
        """
        Liefert die Statusgrenzen der jeweiligen Betriebsphase.

        Die alten globalen Parameter bleiben als Fallback erhalten,
        damit bestehende Konfigurationen kompatibel bleiben.
        """

        return {
            "observe": options.get(
                f"cell_diag_{phase}_observe_deviation_mv",
                options["cell_diag_observe_deviation_mv"],
            ),
            "warning": options.get(
                f"cell_diag_{phase}_warning_deviation_mv",
                options["cell_diag_warning_deviation_mv"],
            ),
            "critical": options.get(
                f"cell_diag_{phase}_critical_deviation_mv",
                options["cell_diag_critical_deviation_mv"],
            ),
        }

    @classmethod
    def phase_status(
        cls,
        phase,
        deviation_mv,
        options,
    ):
        thresholds = cls.phase_thresholds(
            phase,
            options,
        )

        value = abs(float(deviation_mv))

        if value >= thresholds["critical"]:
            return "KRITISCH"

        if value >= thresholds["warning"]:
            return "AUFFÄLLIG"

        if value >= thresholds["observe"]:
            return "BEOBACHTEN"

        return "NORMAL"

    def analyse(
        self,
        module,
        options,
        maintenance_events=(),
        aggregate_records=(),
    ):
        values = self.values_for_module(module)

        maintenance_signature = tuple(
            (
                getattr(event, "maintenance_event_id", None)
                or (event.get("maintenance_event_id") if isinstance(event, dict) else None),
                getattr(event, "revision", None)
                or (event.get("revision") if isinstance(event, dict) else None),
            )
            for event in maintenance_events
        )
        signature = (
            len(values),
            values[-1].get("timestamp") if values else None,
            json.dumps(options, sort_keys=True, default=str),
            maintenance_signature,
            tuple((item.get("day"), item.get("phase"), item.get("cell"),
                   item.get("sample_count"), item.get("config_id"))
                  for item in aggregate_records),
        )
        cached = self._analysis_cache.get(module)
        if cached and cached[0] == signature:
            return cached[1]

        if not values:
            return {
                "module": module,
                "status": "LERNPHASE",
                "confidence": "LOW",
                "sample_count": 0,
                "current_median_mv": None,
                "cells": [],
                "method":
                    "Phase-Resolved Cell Voltage Consistency",
            }

        cell_count = 15

        statistics_by_phase = {
            phase: [
                {
                    "n": 0,
                    "dev": [],
                    "low": 0,
                    "high": 0,
                    "ranks": [],
                }
                for _ in range(cell_count)
            ]
            for phase in ALL_PHASES
        }

        for sample in values:
            voltages = sample["voltages_mv"]

            if len(voltages) != 15:
                continue

            median_voltage = statistics.median(
                voltages
            )

            lowest_voltage = min(voltages)
            highest_voltage = max(voltages)

            ordered = sorted(
                set(voltages),
                reverse=True,
            )

            ranks = {}
            position = 1

            for voltage in ordered:
                count = voltages.count(voltage)

                ranks[voltage] = (
                    position
                    + position
                    + count
                    - 1
                ) / 2

                position += count

            for phase in self.phases(
                sample,
                options,
            ):
                for index, voltage in enumerate(
                    voltages
                ):
                    entry = (
                        statistics_by_phase[
                            phase
                        ][index]
                    )

                    entry["n"] += 1

                    entry["dev"].append(
                        voltage
                        - median_voltage
                    )

                    entry["ranks"].append(
                        ranks[voltage]
                    )

                    entry["low"] += (
                        voltage
                        == lowest_voltage
                    )

                    entry["high"] += (
                        voltage
                        == highest_voltage
                    )

        cells = []

        for index in range(cell_count):
            phase_results = {}

            valid_phase_evidence = []

            for phase in ALL_PHASES:
                entry = (
                    statistics_by_phase[
                        phase
                    ][index]
                )

                if entry["n"]:
                    median_deviation = round(
                        statistics.median(
                            entry["dev"]
                        ),
                        1,
                    )

                    phase_result = {
                        "samples": entry["n"],
                        "median_deviation_mv":
                            median_deviation,
                        "mean_rank": round(
                            statistics.fmean(
                                entry["ranks"]
                            ),
                            2,
                        ),
                        "lowest_percent": round(
                            100
                            * entry["low"]
                            / entry["n"],
                            1,
                        ),
                        "highest_percent": round(
                            100
                            * entry["high"]
                            / entry["n"],
                            1,
                        ),
                    }

                    if phase in STATUS_PHASES:
                        thresholds = (
                            self.phase_thresholds(
                                phase,
                                options,
                            )
                        )

                        phase_result[
                            "thresholds_mv"
                        ] = {
                            "observe":
                                thresholds[
                                    "observe"
                                ],
                            "warning":
                                thresholds[
                                    "warning"
                                ],
                            "critical":
                                thresholds[
                                    "critical"
                                ],
                        }

                        if (
                            entry["n"]
                            >= options[
                                "cell_diag_min_phase_samples"
                            ]
                        ):
                            phase_result[
                                "status"
                            ] = self.phase_status(
                                phase,
                                median_deviation,
                                options,
                            )

                            valid_phase_evidence.append(
                                {
                                    "phase":
                                        phase,
                                    "status":
                                        phase_result[
                                            "status"
                                        ],
                                    "deviation_mv":
                                        abs(
                                            median_deviation
                                        ),
                                }
                            )

                        else:
                            phase_result[
                                "status"
                            ] = "LERNPHASE"

                    else:
                        phase_result[
                            "status"
                        ] = "NICHT STATUSWIRKSAM"

                    phase_results[
                        phase
                    ] = phase_result

                else:
                    phase_results[
                        phase
                    ] = {
                        "samples": 0
                    }

            valid_samples = max(
                (
                    phase_results[phase][
                        "samples"
                    ]
                    for phase
                    in STATUS_PHASES
                ),
                default=0,
            )

            if not valid_phase_evidence:
                status = "LERNPHASE"
                evidence_deviation = 0
                evidence_phase = None

            else:
                worst_phase = max(
                    valid_phase_evidence,
                    key=lambda evidence: (
                        STATUS_ORDER[
                            evidence["status"]
                        ],
                        evidence[
                            "deviation_mv"
                        ],
                    ),
                )

                status = worst_phase[
                    "status"
                ]

                evidence_deviation = (
                    worst_phase[
                        "deviation_mv"
                    ]
                )

                evidence_phase = (
                    worst_phase[
                        "phase"
                    ]
                )

            if (
                valid_samples
                >= options[
                    "cell_diag_confidence_high_samples"
                ]
            ):
                confidence = "HIGH"

            elif (
                valid_samples
                >= options[
                    "cell_diag_confidence_medium_samples"
                ]
            ):
                confidence = "MEDIUM"

            else:
                confidence = "LOW"

            latest_voltages = (
                values[-1][
                    "voltages_mv"
                ]
            )

            latest_median = (
                statistics.median(
                    latest_voltages
                )
            )

            cells.append(
                {
                    "cell": index + 1,
                    "status": status,
                    "confidence":
                        confidence,
                    "current_voltage_mv":
                        latest_voltages[
                            index
                        ],
                    "current_deviation_mv":
                        round(
                            latest_voltages[
                                index
                            ]
                            - latest_median,
                            1,
                        ),
                    "evidence_deviation_mv":
                        evidence_deviation,
                    "evidence_phase":
                        evidence_phase,
                    "phases":
                        phase_results,
                }
            )

        worst = max(
            cells,
            key=lambda cell: (
                STATUS_ORDER[
                    cell["status"]
                ],
                cell[
                    "evidence_deviation_mv"
                ],
            ),
        )

        advanced = EvidenceDiagnostics(self.phases).analyse(
            values, options, cells, maintenance_events, aggregate_records
        )
        for cell, evidence in zip(cells, advanced["cells"]):
            cell["diagnostics"] = evidence

        result = {
            "module": module,
            "status": worst[
                "status"
            ],
            "confidence": worst[
                "confidence"
            ],
            "sample_count": len(
                values
            ),
            "current_median_mv": round(
                statistics.median(
                    values[-1][
                        "voltages_mv"
                    ]
                ),
                1,
            ),
            "evidence_worst_cell":
                worst["cell"],
            "evidence_deviation_mv":
                worst[
                    "evidence_deviation_mv"
                ],
            "evidence_phase":
                worst[
                    "evidence_phase"
                ],
            "method":
                "Phase-Resolved Cell Voltage Consistency",
            "cells": cells,
            "advanced_diagnostics": advanced,
            "trend": advanced["module"]["trend"],
            "maintenance_risk": advanced["module"]["maintenance_risk"],
            "trend_risk_confidence": advanced["module"]["trend_risk_confidence"],
            "evidence_families": advanced["module"]["evidence_families"],
            "contributing_evidence": advanced["module"]["contributing_evidence"],
        }
        self._analysis_cache[module] = (signature, result)
        return result
