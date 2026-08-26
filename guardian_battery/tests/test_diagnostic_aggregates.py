import json
from pathlib import Path

from cell_diagnostics import CellDiagnosticStore, CellSample
from config_history import config_id, diagnostic_parameters
from config_ui import DEFAULTS
from diagnostic_aggregates import DiagnosticAggregateStore
from evidence_diagnostics import EvidenceDiagnostics


def sample(timestamp, serial="SN-A", delta=0):
    values = [3300] * 15
    values[0] += delta
    return CellSample(timestamp, 1, values, -2.0, 50.0, [25.0] * 15,
                      [False] * 15, serial, "PHS-1")


def test_aggregates_survive_restart_with_versions_config_and_identity(tmp_path):
    path = tmp_path / "aggregates.json"
    store = DiagnosticAggregateStore(path, CellDiagnosticStore.phases, 730)
    for day, delta in enumerate((2, 5, 9)):
        assert store.add(sample(day * 86400 + 1, delta=delta), DEFAULTS)
    assert store.save()

    reloaded = DiagnosticAggregateStore(path, CellDiagnosticStore.phases, 730)
    records = reloaded.for_identity(1, "SN-A")
    discharge = [item for item in records if item["phase"] == "discharge" and item["cell"] == 1]
    assert len(discharge) == 3
    assert all(item["physical_module_serial"] == "SN-A" for item in discharge)
    assert all(item["guardian_version"] == "0.7.6" for item in discharge)
    assert all(item["diagnostic_engine_version"] == "0.4.12" for item in discharge)
    assert all(item["config_id"] == config_id(diagnostic_parameters(DEFAULTS)) for item in discharge)
    assert [item["median_deviation_mv"] for item in discharge] == [2, 5, 9]


def test_duplicate_sample_is_not_counted_twice(tmp_path):
    path = tmp_path / "aggregates.json"
    store = DiagnosticAggregateStore(path, CellDiagnosticStore.phases)
    value = sample(100)
    assert store.add(value, DEFAULTS)
    assert not store.add(value, DEFAULTS)
    store.save()
    records = store.for_identity(1, "SN-A")
    assert all(item["sample_count"] == 1 for item in records)


def test_aggregate_store_never_rewrites_raw_history(tmp_path):
    raw = tmp_path / "cell_history" / "2026-01-01.jsonl"
    raw.parent.mkdir()
    raw.write_text('{"raw":"unchanged"}\n')
    before = raw.read_bytes()
    store = DiagnosticAggregateStore(tmp_path / "diagnostic_aggregates.json",
                                     CellDiagnosticStore.phases)
    store.add(sample(100), DEFAULTS)
    store.save()
    assert raw.read_bytes() == before


def test_physical_serials_are_separate_aggregate_namespaces(tmp_path):
    store = DiagnosticAggregateStore(tmp_path / "aggregates.json",
                                     CellDiagnosticStore.phases)
    store.add(sample(100, "SN-X", 5), DEFAULTS)
    store.add(sample(200, "SN-Y", 20), DEFAULTS)
    assert {item["physical_module_serial"] for item in store.for_identity(1, "SN-X")} == {"SN-X"}
    assert {item["physical_module_serial"] for item in store.for_identity(1, "SN-Y")} == {"SN-Y"}


def test_ranking_uses_persisted_aggregates_after_reload(tmp_path):
    aggregate_path = tmp_path / "aggregates.json"
    aggregates = DiagnosticAggregateStore(aggregate_path, CellDiagnosticStore.phases)
    cell_path = tmp_path / "cells.json"
    cells = CellDiagnosticStore(cell_path)
    for day, delta in enumerate((2, 8, 16)):
        value = sample(day * 86400 + 1, "SN-A", -delta)
        aggregates.add(value, DEFAULTS)
        cells.add(value)
    aggregates.save(); cells.save()

    reloaded_cells = CellDiagnosticStore(cell_path)
    reloaded_aggregates = DiagnosticAggregateStore(aggregate_path, CellDiagnosticStore.phases)
    result = reloaded_cells.analyse(
        1, {**DEFAULTS, "cell_diag_min_phase_samples": 1}, (),
        reloaded_aggregates.for_identity(1, "SN-A"),
    )
    ranking = result["cells"][0]["diagnostics"]["methods"]["ranking_drift"]
    assert ranking["phases"]["discharge"]["daily_aggregates"] == 3
    assert ranking["phases"]["discharge"]["observation_period"]["seconds"] >= 2 * 86400


def test_persisted_file_has_bounded_versioned_container(tmp_path):
    path = tmp_path / "aggregates.json"
    store = DiagnosticAggregateStore(path, CellDiagnosticStore.phases, 30)
    store.add(sample(100), DEFAULTS); store.save()
    value = json.loads(path.read_text())
    assert value["schema_version"] == 1
    assert value["retention_days"] == 30
    assert isinstance(value["records"], dict)


def test_aggregate_classification_runs_once_per_module_sample(tmp_path):
    calls = []

    def classify(value, _options):
        calls.append(value["timestamp"])
        return ["discharge"]

    store = DiagnosticAggregateStore(tmp_path / "aggregates.json", classify)
    store.add(sample(100), DEFAULTS)
    assert calls == [100]
    assert len(store.for_identity(1, "SN-A")) == 15


def test_analysis_cache_invalidates_for_data_config_and_aggregate_changes(tmp_path, monkeypatch):
    store = CellDiagnosticStore(tmp_path / "cells.json")
    store.add(sample(100))
    calls = 0
    original = EvidenceDiagnostics.analyse

    def counted(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(EvidenceDiagnostics, "analyse", counted)
    options = {**DEFAULTS, "cell_diag_min_phase_samples": 1}
    first = store.analyse(1, options)
    assert store.analyse(1, options) is first
    assert calls == 1

    changed_options = {**options, "cell_diag_observe_deviation_mv": 11}
    store.analyse(1, changed_options)
    assert calls == 2

    aggregate = [{"day": "2026-01-01", "phase": "discharge", "cell": 1,
                  "sample_count": 1, "config_id": "different-config"}]
    store.analyse(1, changed_options, aggregate_records=aggregate)
    assert calls == 3

    store.add(sample(200))
    store.analyse(1, changed_options, aggregate_records=aggregate)
    assert calls == 4
