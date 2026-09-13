import sys
import threading
from pathlib import Path
from unittest.mock import patch


APP_DIR = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP_DIR))

# main creates the production data root at import time. Keep this lifecycle test
# entirely on mocked paths without requiring or touching /share.
with patch.object(Path, "mkdir", return_value=None):
    import main

import config_ui
from config_ui import Handler


class StopLifecycle(BaseException):
    pass


class FakeProjection:
    def __init__(self, *_args):
        self.rebuild = lambda: True

    def status(self):
        return {
            "enabled": True,
            "worker_active": True,
            "days": 1,
            "open_days": 1,
            "last_success": "2026-09-13T12:00:00+00:00",
        }


class FakeDisplayWorker:
    latest = None

    def __init__(self, projection):
        self.projection = projection
        self.started = False
        type(self).latest = self

    def start(self):
        self.started = True
        return True

    def status(self):
        return self.projection.status()

    def request_historical_rebuild(self):
        return True


class FakeConfigHistory:
    def __init__(self, *_args):
        pass

    def record_if_changed(self, _options):
        return None


class FakeConsole:
    def __init__(self, *_args):
        pass


class FakeMqtt:
    def __init__(self, *_args):
        self.maintenance_events = object()

    def discovery(self, _module_count):
        pass


class FakeStore:
    def __init__(self, *_args):
        pass


def options():
    return {
        "poll_interval_seconds": 10,
        "cell_diagnostics_interval_seconds": 60,
        "serial_port": "auto",
        "baudrate": 115200,
        "command_timeout_seconds": 1,
        "rs485_sniffer_enabled": False,
        "rs485_sniffer_stale_seconds": 600,
        "module_count": 6,
        "cell_diag_history_max_samples": 100,
    }


def prepare(monkeypatch):
    config_ui.reset_display_projection_startup()
    monkeypatch.setattr(config_ui, "_DISPLAY_PROJECTION_PROVIDER", None)
    monkeypatch.setattr(config_ui, "_DISPLAY_PROJECTION_REBUILD_ACTION", None)
    FakeDisplayWorker.latest = None
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(main, "load_options", options)
    monkeypatch.setattr(main, "start_config_server", lambda: object())
    monkeypatch.setattr(main, "DisplayHistoryProjection", FakeProjection)
    monkeypatch.setattr(main, "DisplayHistoryProjectionWorker", FakeDisplayWorker)
    monkeypatch.setattr(main, "ConfigHistory", FakeConfigHistory)
    monkeypatch.setattr(main, "load_persistent_state", lambda: {})


def assert_display_ready_and_endpoint_available():
    status = config_ui.display_projection_startup_status()
    assert status["startup_stage"] == "DISPLAY_INIT_08_COMPLETE"
    assert status["startup_completed"] is True
    assert FakeDisplayWorker.latest is not None
    assert FakeDisplayWorker.latest.started is True
    assert config_ui._DISPLAY_PROJECTION_PROVIDER is not None

    handler = object.__new__(Handler)
    handler._ingress_allowed = lambda: True
    captured = []
    handler._send = lambda code, body, *args, **kwargs: captured.append((code, body))
    handler.path = "/api/hassio_ingress/token/api/display-projection/status"
    handler.do_GET()
    assert captured[0][0] == 200
    assert captured[0][1]["startup_stage"] == "DISPLAY_INIT_08_COMPLETE"
    assert captured[0][1]["startup_completed"] is True
    assert captured[0][1]["enabled"] is True


def run_until_blocked(blocker, release):
    failures = []

    def target():
        try:
            main.main()
        except StopLifecycle:
            pass
        except BaseException as exc:  # surfaced in the test thread
            failures.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    assert blocker.wait(2), "main() did not reach the controlled blocking call"
    assert_display_ready_and_endpoint_available()
    release.set()
    thread.join(2)
    assert not thread.is_alive()
    assert failures == []


def blocking_call(blocker, release):
    blocker.set()
    assert release.wait(2)
    raise StopLifecycle()


def test_display_startup_completes_before_console_discovery(monkeypatch):
    prepare(monkeypatch)
    blocker = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        main, "find_port", lambda *_args: blocking_call(blocker, release))

    run_until_blocked(blocker, release)


def test_display_startup_completes_before_mqtt_discovery(monkeypatch):
    prepare(monkeypatch)
    blocker = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(main, "find_port", lambda *_args: "/dev/null")
    monkeypatch.setattr(main, "PylontechConsole", FakeConsole)
    monkeypatch.setattr(main, "create_rs485_reader", lambda *_args: None)

    class BlockingMqtt(FakeMqtt):
        def discovery(self, _module_count):
            blocking_call(blocker, release)

    monkeypatch.setattr(main, "Mqtt", BlockingMqtt)

    run_until_blocked(blocker, release)


def test_display_startup_completes_before_current_condition_backfill(monkeypatch):
    prepare(monkeypatch)
    blocker = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(main, "find_port", lambda *_args: "/dev/null")
    monkeypatch.setattr(main, "PylontechConsole", FakeConsole)
    monkeypatch.setattr(main, "create_rs485_reader", lambda *_args: None)
    monkeypatch.setattr(main, "Mqtt", FakeMqtt)
    monkeypatch.setattr(main, "configure_maintenance_live_publisher", lambda *_args: None)
    monkeypatch.setattr(main, "configure_rs485_status_provider", lambda *_args: None)
    monkeypatch.setattr(main, "CellDiagnosticStore", FakeStore)

    class BlockingBackfill:
        def __init__(self, *_args):
            pass

        def run(self, *_args):
            return blocking_call(blocker, release)

    monkeypatch.setattr(main, "CurrentConditionBackfill", BlockingBackfill)

    run_until_blocked(blocker, release)
