import json
import logging
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from hycube_evidence import (MAX_RESPONSE_BODY_BYTES, HycubeBatteryCapacitySeries, HycubeCollector,
                             HycubeEvidenceWriter, collector_from_options,
                             data_row_url, evidence_enabled, observation)


PAYLOAD = {"BatteryPower": -1200, "BatteryCapacity": 42, "GridPower": 300,
           "HomePower": 1500, "solarPower": 0, "ExternalPower": 20,
           "Date2": "2026-09-02T12:00:00+02:00"}


class Response:
    def __init__(self, payload=PAYLOAD, status=200):
        self.raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
        self.status = status
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def getcode(self): return self.status
    def read(self, size=-1): return self.raw if size < 0 else self.raw[:size]


class Opener:
    def __init__(self, result=None):
        self.result = result or Response()
        self.calls = []
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.result, Exception): raise self.result
        return self.result


@pytest.mark.parametrize("bad", ["", "ftp://192.168.1.2", "http://user:x@192.168.1.2",
                                  "http://8.8.8.8", "http://example.com",
                                  "http://192.168.1.2/?target=http://evil"])
def test_hycube_target_rejects_nonlocal_credentials_and_ssrf_forms(bad):
    with pytest.raises(ValueError): data_row_url(bad)


def test_hycube_target_is_fixed_read_only_endpoint():
    assert data_row_url("http://192.168.1.22:8080/ignored") == \
        "http://192.168.1.22:8080/data_row/"


@pytest.mark.parametrize("value", [False, None, "", "false", "true", 0, 1])
def test_hycube_evidence_requires_literal_boolean_true(value):
    assert evidence_enabled(value) is False


def test_hycube_evidence_literal_true_is_enabled():
    assert evidence_enabled(True) is True


@pytest.mark.parametrize("options", [
    {}, {"hycube_evidence_enabled": False},
    {"hycube_evidence_enabled": "false", "hycube_base_url": "http://localhost"},
])
def test_default_off_does_not_construct_or_validate_a_target(tmp_path, options):
    assert collector_from_options(options, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_observation_retains_raw_hash_fields_and_separate_times():
    raw = json.dumps(PAYLOAD, separators=(",", ":")).encode()
    item = observation(raw, 1_788_343_200.0)
    assert item["raw_payload"] == raw.decode()
    assert len(item["payload_sha256"]) == 64
    assert all(item[key] == value for key, value in PAYLOAD.items())
    assert item["device_timestamp"] == "2026-09-02T10:00:00+00:00"
    assert item["timezone_semantics"] == "explicit"
    assert item["causality"] == "not_determined"


def test_missing_fields_are_null_not_zero_and_naive_time_is_unknown():
    item = observation(b'{"BatteryPower":12,"Date2":"2026-09-02 12:00:00"}', 1)
    assert item["BatteryCapacity"] is None
    assert item["parse_quality"] == "partial"
    assert item["timezone_semantics"] == "unknown"
    assert item["device_receive_offset_seconds"] is None


def test_invalid_json_is_preserved_as_non_authoritative_raw():
    item = observation(b"not-json", 1)
    assert item["parse_quality"] == "invalid"
    assert item["raw_payload"] == "not-json"
    assert item["BatteryPower"] is None


def test_collector_only_gets_fixed_route_and_appends_across_restart(tmp_path):
    writer = HycubeEvidenceWriter(tmp_path)
    for timestamp in (1_788_343_200.0, 1_788_343_201.0):
        opener = Opener()
        collector = HycubeCollector("http://192.168.1.2", writer, opener=opener,
                                    clock=lambda value=timestamp: value)
        collector.collect_once()
        request, timeout = opener.calls[0]
        assert request.full_url.endswith("/data_row/")
        assert request.get_method() == "GET"
        assert timeout == .8
        assert collector.interval_seconds == 5
    lines = next(tmp_path.glob("*.jsonl")).read_text().splitlines()
    assert len(lines) == 2


def test_sampling_metadata_records_configured_and_actual_interval(tmp_path):
    timestamps = iter((100.0, 105.25))
    collector = HycubeCollector("http://localhost", HycubeEvidenceWriter(tmp_path),
                                opener=Opener(), clock=lambda: next(timestamps))
    first = collector.collect_once()
    second = collector.collect_once()
    assert first["configured_interval_seconds"] == 5
    assert first["actual_interval_seconds"] is None
    assert first["actual_interval_quality"] == "first_observation"
    assert second["actual_interval_seconds"] == 5.25
    assert second["actual_interval_quality"] == "observed"


def test_policy_evidence_is_unavailable_without_verified_read_only_source():
    item = observation(json.dumps(PAYLOAD).encode(), 1)
    assert item["source_semantics"] == "hycube_system_response"
    assert item["policy_evidence"] == "unavailable"
    assert item["policy_evidence_reason"] == "no_verified_read_only_source"


def test_oversized_body_is_rejected_without_partial_evidence(tmp_path):
    response = Response(b"x" * (MAX_RESPONSE_BODY_BYTES + 1))
    collector = HycubeCollector("http://localhost", HycubeEvidenceWriter(tmp_path),
                                opener=Opener(response))
    with pytest.raises(ValueError, match="exceeds 1 MiB"):
        collector.collect_once()
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("failure", [URLError("refused"), TimeoutError(),
    HTTPError("http://192.168.1.2/data_row/", 500, "server", {}, None)])
def test_collector_failure_isolated_and_contains_no_target_or_credentials(
        tmp_path, caplog, failure):
    collector = HycubeCollector("http://192.168.1.2", HycubeEvidenceWriter(tmp_path),
                                opener=Opener(failure), interval_seconds=.01,
                                max_backoff_seconds=.02)
    with caplog.at_level(logging.WARNING):
        collector.start()
        assert collector._stop.wait(.04) is False
        collector.stop()
    assert collector.status()["failures"] >= 1
    assert "192.168.1.2" not in caplog.text
    assert not list(tmp_path.glob("*.jsonl"))


def test_start_is_singleton_and_stop_is_bounded(tmp_path):
    collector = HycubeCollector("http://localhost", HycubeEvidenceWriter(tmp_path),
                                opener=Opener(), interval_seconds=10)
    assert collector.start() is True
    assert collector.start() is False
    collector.stop(.2)
    assert collector.status()["state"] == "disabled"


def test_battery_capacity_history_is_windowed_read_only_and_extrema_preserving(tmp_path, monkeypatch):
    history = tmp_path / "history"
    history.mkdir()
    old = history / "2026-09-01.jsonl"
    current = history / "2026-09-02.jsonl"
    old.write_text(json.dumps({"record_type": "hycube_system_observation",
                               "received_at": "2026-09-01T12:00:00+00:00",
                               "BatteryCapacity": 10}) + "\n")
    rows = []
    for index in range(100):
        value = 5 if index == 40 else 99 if index == 60 else 50
        rows.append(json.dumps({"record_type": "hycube_system_observation",
                                "received_at": f"2026-09-02T00:{index // 60:02d}:{index % 60:02d}+00:00",
                                "BatteryCapacity": value, "Date2": None,
                                "device_timestamp": None, "timezone_semantics": "unavailable",
                                "parse_quality": "complete"}))
    current.write_text("\n".join(rows) + "\n")
    before = {path: path.read_bytes() for path in (old, current)}
    opened = []
    original_open = Path.open

    def counted_open(path, *args, **kwargs):
        if path.parent == history:
            opened.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    result = HycubeBatteryCapacitySeries(history).query(
        timestamp_from="2026-09-02T00:00:00+00:00",
        timestamp_to="2026-09-02T01:00:00+00:00", max_points=20)
    assert opened == [current]
    assert len(result["points"]) <= 20
    assert {5.0, 99.0} <= {point["value"] for point in result["points"]}
    assert all(point["timestamp"].startswith("2026-09-02") for point in result["points"])
    assert {path: path.read_bytes() for path in (old, current)} == before
