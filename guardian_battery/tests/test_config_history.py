import json

from config_history import ConfigHistory, config_id, diagnostic_parameters


def options(**overrides):
    values = {
        "module_count": 5,
        "poll_interval_seconds": 10,
        "cell_diagnostics_enabled": True,
        "cell_diagnostics_interval_seconds": 60,
        "cell_diag_low_soc_percent": 30,
        "cell_diag_high_soc_percent": 80,
        "cell_diag_charge_current_a": 0.8,
        "cell_diag_discharge_current_a": 0.8,
    }
    values.update(overrides)
    return values


def read_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_config_id_is_stable_and_ignores_unrelated_options():
    first = options(raw_log=False)
    second = options(raw_log=True)
    assert config_id(diagnostic_parameters(first)) == config_id(diagnostic_parameters(second))


def test_first_configuration_is_recorded(tmp_path):
    path = tmp_path / "config_history.jsonl"
    history = ConfigHistory(path)

    record = history.record_if_changed(options())

    assert record is not None
    records = read_records(path)
    assert len(records) == 1
    assert records[0]["config_id"] == record["config_id"]
    assert records[0]["parameters"]["module_count"] == 5
    assert records[0]["schema_version"] == 1


def test_unchanged_configuration_is_not_duplicated(tmp_path):
    path = tmp_path / "config_history.jsonl"
    history = ConfigHistory(path)

    assert history.record_if_changed(options()) is not None
    assert history.record_if_changed(options()) is None
    assert len(read_records(path)) == 1


def test_diagnostic_change_creates_new_record(tmp_path):
    path = tmp_path / "config_history.jsonl"
    history = ConfigHistory(path)

    first = history.record_if_changed(options(module_count=5))
    second = history.record_if_changed(options(module_count=6))

    assert first is not None
    assert second is not None
    assert first["config_id"] != second["config_id"]
    assert len(read_records(path)) == 2
