import sys
import pytest
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


@pytest.fixture(autouse=True)
def isolate_legacy_startup_root(tmp_path, monkeypatch):
    # The legacy entry now creates its root at main(), not module import.
    # Redirect that one existing startup write; the production guard stays on.
    monkeypatch.setattr(main, "SHARE_DIR", tmp_path / "legacy-startup")


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
    monkeypatch.setattr(main, "require_source_commit", lambda: "a" * 40)
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
    assert captured[0][1]["startup_status"] == "started"
    assert captured[0][1]["started_at"] is not None
    assert "startup_stage" not in captured[0][1]
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


def test_display_startup_completes_before_diagnostic_worker_start(monkeypatch):
    prepare(monkeypatch)
    blocker = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(main, "find_port", lambda *_args: "/dev/null")
    monkeypatch.setattr(main, "PylontechConsole", FakeConsole)
    monkeypatch.setattr(main, "create_rs485_reader", lambda *_args: None)
    monkeypatch.setattr(main, "Mqtt", FakeMqtt)
    monkeypatch.setattr(main, "configure_maintenance_live_publisher", lambda *_args: None)
    monkeypatch.setattr(main, "configure_rs485_status_provider", lambda *_args: None)

    class BlockingStartup:
        def __init__(self, *_args):
            pass

        def start(self):
            return blocking_call(blocker, release)

    import diagnostic_startup
    monkeypatch.setattr(diagnostic_startup, "DiagnosticStartup", BlockingStartup)

    run_until_blocked(blocker, release)


@pytest.fixture(autouse=True)
def reset_live_gate_after_lifecycle_test():
    yield
    from history_live_priority import collector_stopped
    collector_stopped()


def test_first_pwr_runs_before_any_historical_diagnostic_read(monkeypatch, tmp_path):
    from unittest.mock import MagicMock
    import diagnostic_startup
    import history_live_priority
    from config_ui import DEFAULTS
    prepare(monkeypatch)
    monkeypatch.setattr(main, "load_options", lambda: {**DEFAULTS, "rs485_sniffer_enabled": False})
    monkeypatch.setattr(main, "find_port", lambda *_: "/dev/null")
    monkeypatch.setattr(main, "create_rs485_reader", lambda *_: None)
    monkeypatch.setattr(main, "RUNNING", True)
    for name in ("CELL_DIAG_FILE", "DIAGNOSTIC_AGGREGATE_FILE", "CELL_HISTORY_DIR",
                 "DEFAULT_POSITION_HISTORY_FILE", "DEFAULT_MAINTENANCE_EVENT_FILE", "SHARE_DIR"):
        monkeypatch.setattr(main, name, tmp_path / name)
    def forbidden(*_args, **_kwargs):
        pytest.fail("Historical diagnostic source accessed before first PWR")
    monkeypatch.setattr(diagnostic_startup.CellDiagnosticStore, "_load", forbidden)
    monkeypatch.setattr(diagnostic_startup.DiagnosticAggregateStore, "_load", forbidden)
    called=[]
    class Console:
        def __init__(self,*_): pass
        def command(self,_):
            called.append("pwr")
            raise StopLifecycle()
        def close(self): pass
    monkeypatch.setattr(main,"PylontechConsole",Console)
    for name in ("Mqtt", "DerivedMqttWorker", "CellAnalysisWorker",
                 "DailyDiagnosticWorker", "CanonicalPhaseWorker", "SelectedHistoryPreparation"):
        monkeypatch.setattr(main,name,lambda *_args,**_kwargs:MagicMock())
    monkeypatch.setattr(main,"collector_from_options",lambda *_:None)
    monkeypatch.setattr(main,"configure_maintenance_live_publisher",lambda *_:None)
    monkeypatch.setattr(main,"configure_rs485_status_provider",lambda *_:None)
    with pytest.raises(StopLifecycle):
        main.main()
    assert called==["pwr"]
    assert not history_live_priority.live_coordination_ready()
