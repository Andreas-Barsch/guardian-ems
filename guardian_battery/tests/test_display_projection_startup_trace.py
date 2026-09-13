import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import config_ui
from config_ui import Handler


class FakeProjection:
    pass


class FakeWorker:
    def __init__(self, projection):
        self.projection = projection
        self.started = False

    def status(self):
        return {"enabled": True, "state": "idle"}

    def request_historical_rebuild(self):
        return True

    def start(self):
        self.started = True
        return True


def prepare(monkeypatch):
    config_ui.reset_display_projection_startup()
    monkeypatch.setattr(config_ui, "_DISPLAY_PROJECTION_PROVIDER", None)
    monkeypatch.setattr(config_ui, "_DISPLAY_PROJECTION_REBUILD_ACTION", None)


def test_clean_start_records_every_stage_and_completes(monkeypatch):
    prepare(monkeypatch)
    stages = [config_ui.display_projection_startup_status()["startup_stage"]]
    original = config_ui.update_display_projection_startup

    def record(stage, **kwargs):
        stages.append(stage)
        original(stage, **kwargs)

    monkeypatch.setattr(config_ui, "update_display_projection_startup", record)
    record("DISPLAY_INIT_01_MAIN_REACHED")
    record("DISPLAY_INIT_02_DEPENDENCIES_READY")
    worker = config_ui.start_display_projection_traced(FakeProjection, FakeWorker)

    assert stages == [f"DISPLAY_INIT_{index:02d}_{name}" for index, name in enumerate((
        "NOT_STARTED", "MAIN_REACHED", "DEPENDENCIES_READY", "CONSTRUCTOR_ENTER",
        "CONSTRUCTOR_EXIT", "PROVIDER_REGISTERED", "WORKER_START_ENTER",
        "WORKER_STARTED", "COMPLETE"))]
    status = config_ui.display_projection_startup_status()
    assert worker.started and status["startup_completed"] is True
    assert status["startup_reached_at"] is not None
    assert status["startup_error_type"] is None
    assert status["startup_error_message"] is None


def test_constructor_failure_records_last_stage_and_reraises(monkeypatch):
    prepare(monkeypatch)

    def fail_constructor(*_args):
        raise ValueError("constructor broken")

    with pytest.raises(ValueError, match="constructor broken"):
        config_ui.start_display_projection_traced(fail_constructor, FakeWorker)
    status = config_ui.display_projection_startup_status()
    assert status["startup_stage"] == "DISPLAY_INIT_03_CONSTRUCTOR_ENTER"
    assert status["startup_completed"] is False
    assert status["startup_error_type"] == "ValueError"
    assert status["startup_error_message"] == "constructor broken"


def test_provider_registration_failure_preserves_constructor_exit(monkeypatch):
    prepare(monkeypatch)

    def fail_provider(*_args):
        raise RuntimeError("provider broken")

    monkeypatch.setattr(config_ui, "configure_display_projection", fail_provider)
    with pytest.raises(RuntimeError, match="provider broken"):
        config_ui.start_display_projection_traced(FakeProjection, FakeWorker)
    status = config_ui.display_projection_startup_status()
    assert status["startup_stage"] == "DISPLAY_INIT_04_CONSTRUCTOR_EXIT"
    assert status["startup_error_type"] == "RuntimeError"
    assert status["startup_error_message"] == "provider broken"


def test_worker_start_failure_preserves_start_enter(monkeypatch):
    prepare(monkeypatch)

    class BrokenWorker(FakeWorker):
        def start(self):
            raise OSError("thread unavailable")

    with pytest.raises(OSError, match="thread unavailable"):
        config_ui.start_display_projection_traced(FakeProjection, BrokenWorker)
    status = config_ui.display_projection_startup_status()
    assert status["startup_stage"] == "DISPLAY_INIT_06_WORKER_START_ENTER"
    assert status["startup_error_type"] == "OSError"
    assert status["startup_error_message"] == "thread unavailable"


def test_not_reached_fallback_endpoint_contains_process_startup_state(monkeypatch):
    prepare(monkeypatch)
    handler = object.__new__(Handler)
    handler._ingress_allowed = lambda: True
    captured = []
    handler._send = lambda code, body, *args, **kwargs: captured.append((code, body))
    handler.path = "/api/hassio_ingress/token/api/display-projection/status"
    handler.do_GET()
    assert captured == [(200, {
        "enabled": False,
        "state": "disabled",
        "startup_status": "starting",
        "started_at": None,
        "last_startup_error": None,
    })]
