import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from rs485_event_analyzer import analyze_file, main, render_text


def frame(timestamp, *, adr=2, command=0x92, info="0011", decoded=None, **overrides):
    record = {
        "record_type": "frame", "timestamp": timestamp, "adr": adr,
        "direction": "response", "paired_command": command,
        "checksum_valid": True, "frame_complete": True,
        "request_matched": True, "decoder_supported": command == 0x92,
        "info_raw": info,
    }
    if decoded is not None:
        record["decoded"] = decoded
    record.update(overrides)
    return record


def management(ccl=25.0, dcl=-25.0, charge=True, discharge=True, cvl=53.25, dvl=45.0,
               immediate_1=False, immediate_2=False, full=False):
    return {
        "charge_current_limit_a": ccl, "discharge_current_limit_a": dcl,
        "charge_enable": charge, "discharge_enable": discharge,
        "charge_voltage_limit_v": cvl, "discharge_voltage_limit_v": dvl,
        "charge_immediately_1": immediate_1, "charge_immediately_2": immediate_2,
        "full_charge_request": full,
    }


def write_jsonl(path, records, malformed=False):
    text = "\n".join(json.dumps(item) for item in records)
    if malformed:
        text += "\n{broken"
    path.write_text(text + "\n", encoding="utf-8")


def analyze(path, **kwargs):
    return analyze_file(path, "2026-08-31T19:00:00+00:00",
                        "2026-08-31T19:40:00+00:00", **kwargs)


def test_time_window_and_utc_to_plus_two_conversion(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T18:59:59+00:00", decoded=management()),
        frame("2026-08-31T19:10:00+00:00", decoded=management(ccl=20)),
        frame("2026-08-31T19:40:01+00:00", decoded=management(ccl=15)),
    ])
    result = analyze_file(path, "2026-08-31T21:00:00+02:00",
                          "2026-08-31T21:40:00+02:00")
    assert result["totals"]["valid_0x92"] == 1
    assert "21:10:00.000" in render_text(result)


def test_adr_and_command_filters(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:10:00+00:00", adr=2, decoded=management()),
        frame("2026-08-31T19:11:00+00:00", adr=3, decoded=management()),
        frame("2026-08-31T19:12:00+00:00", adr=2, command=0x44),
    ])
    result = analyze(path, adrs={2}, commands={0x92})
    assert result["adrs_observed"] == [2]
    assert result["totals"]["valid_0x92"] == 1
    assert result["totals"]["valid_0x44"] == 0


def test_0x92_baseline_change_and_identical_suppression(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:01:00+00:00", decoded=management()),
        frame("2026-08-31T19:02:00+00:00", decoded=management()),
        frame("2026-08-31T19:03:00+00:00", decoded=management(dcl=-10)),
    ])
    result = analyze(path, changes_only=True)
    events = [item for item in result["events"] if item["type"] == "0x92"]
    assert [item["marker"] for item in events] == ["BASELINE", "CHANGE"]
    assert events[-1]["decoded"]["discharge_current_limit_a"] == -10


def test_0x92_pre_window_state_detects_first_visible_change(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T18:59:00+00:00", decoded=management(dcl=-25)),
        frame("2026-08-31T19:00:01+00:00", decoded=management(dcl=-10)),
    ])
    event = analyze(path, changes_only=True)["events"][0]
    assert event["marker"] == "CHANGE"


def test_state_change_is_rendered(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [{
        "record_type": "state_change", "timestamp": "2026-08-31T19:05:00+00:00",
        "adr": 2, "field": "discharge_enable", "old_value": True,
        "new_value": False,
    }])
    text = render_text(analyze(path, changes_only=True))
    assert "CHANGE | discharge_enable | True -> False" in text
    assert "State changes: 1" in text


def test_0x44_identical_has_no_change(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:01:00+00:00", command=0x44, info="0011"),
        frame("2026-08-31T19:02:00+00:00", command=0x44, info="0011"),
    ])
    assert analyze(path)["totals"]["changes_0x44"] == 0


@pytest.mark.parametrize(("before", "after", "expected"), [
    ("0011", "0012", [(1, "11", "12")]),
    ("001122", "FF1123", [(0, "00", "FF"), (2, "22", "23")]),
])
def test_0x44_reports_one_or_multiple_changed_bytes(tmp_path, before, after, expected):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:01:00+00:00", command=0x44, info=before),
        frame("2026-08-31T19:02:00+00:00", command=0x44, info=after),
    ])
    result = analyze(path)
    changes = result["events"][0]["changes"]
    assert [(item["offset"], item["old"], item["new"]) for item in changes] == expected
    assert f"changed_bytes={len(expected)}" in render_text(result)


def test_0x44_uses_last_valid_baseline_before_window(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T18:50:00+00:00", command=0x44, info="0011"),
        frame("2026-08-31T19:00:01+00:00", command=0x44, info="0012"),
    ])
    event = analyze(path, changes_only=True)["events"][0]
    assert event["type"] == "0x44_change"
    assert event["changes"] == [{"offset": 1, "old": "11", "new": "12"}]


def test_malformed_json_and_invalid_checksum_are_counted_not_displayed(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:01:00+00:00", decoded=management(), checksum_valid=False),
        frame("2026-08-31T19:02:00+00:00", decoded=management()),
    ], malformed=True)
    result = analyze(path)
    assert result["totals"]["valid_0x92"] == 1
    assert result["counters"]["malformed_json"] == 1
    assert result["counters"]["invalid_evidence"] == 1


def test_changes_only_keeps_relevant_event_types_only(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [
        frame("2026-08-31T19:01:00+00:00", decoded=management()),
        frame("2026-08-31T19:02:00+00:00", decoded=management()),
        frame("2026-08-31T19:03:00+00:00", command=0x44, info="00"),
        frame("2026-08-31T19:04:00+00:00", command=0x44, info="01"),
    ])
    assert [item["type"] for item in analyze(path, changes_only=True)["events"]] == [
        "0x92", "0x44_change"]


def test_summary_contains_counts_and_last_management(tmp_path):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [frame("2026-08-31T19:01:00+00:00", adr=3,
                              decoded=management(ccl=25, dcl=-25))])
    text = render_text(analyze(path))
    assert "Records examined: 1" in text
    assert "Valid 0x92 responses: 1" in text
    assert "ADRs observed: 03" in text
    assert "last CCL: +25.0 A" in text and "last DCL: -25.0 A" in text


def test_cli_json_output(tmp_path, capsys):
    path = tmp_path / "history.jsonl"
    write_jsonl(path, [frame("2026-08-31T19:01:00+00:00", decoded=management())])
    assert main(["--file", str(path), "--from", "2026-08-31T19:00:00+00:00",
                 "--to", "2026-08-31T19:40:00+00:00", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["totals"]["valid_0x92"] == 1


def test_source_is_strictly_read_only():
    source = (Path(__file__).resolve().parents[1] / "tools" /
              "rs485_event_analyzer.py").read_text(encoding="utf-8")
    assert '.open("r", encoding="utf-8")' in source
    for forbidden in ('.open("w"', '.open("a"', ".write(", ".rename(", ".unlink(",
                      ".truncate("):
        assert forbidden not in source
