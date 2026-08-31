from types import SimpleNamespace
import ast
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from serial_devices import (SerialResolutionError, discover_serial_devices,
                            ensure_distinct_roles, resolve_console_port,
                            resolve_rs485_port)


def entry(path, **values):
    return SimpleNamespace(device=path, vid=values.get("vid"), pid=values.get("pid"),
                           description=values.get("description", ""),
                           manufacturer=values.get("manufacturer", ""),
                           product=values.get("product", ""),
                           serial_number=values.get("serial_number", ""),
                           hwid=values.get("hwid", ""))


def inventory(kind="both"):
    entries = []
    aliases = []
    mapping = {}
    if kind in ("both", "prolific"):
        entries.append(entry("/dev/ttyUSB0", description="Prolific USB Serial"))
        aliases.append("/dev/serial/by-id/usb-Prolific_Console-if00")
        mapping[aliases[-1]] = "/dev/ttyUSB0"
    if kind in ("both", "waveshare"):
        entries.append(entry("/dev/ttyACM0", vid=0x1A86, pid=0x55D3,
                             description="USB Single Serial"))
        aliases.append("/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B97005529-if00")
        mapping[aliases[-1]] = "/dev/ttyACM0"
    return discover_serial_devices(entries, aliases, lambda path: mapping.get(path, path))


def test_dual_inventory_resolves_roles_by_identity_and_prefers_by_id():
    devices = inventory()
    assert "Prolific" in resolve_console_port("auto", devices).preferred_path()
    assert "usb-1a86" in resolve_rs485_port("auto", devices).preferred_path()


def test_console_excludes_waveshare_and_single_legacy_uart_still_works():
    with pytest.raises(SerialResolutionError) as error:
        resolve_console_port("auto", inventory("waveshare"))
    assert error.value.code == "console_port_unavailable"
    unknown = discover_serial_devices([entry("/dev/ttyUSB7")], [], lambda value: value)
    assert resolve_console_port("auto", unknown).preferred_path() == "/dev/ttyUSB7"


def test_missing_and_ambiguous_rs485_fail_closed():
    with pytest.raises(SerialResolutionError) as missing:
        resolve_rs485_port("auto", inventory("prolific"))
    assert missing.value.code == "rs485_port_unavailable"
    two = discover_serial_devices([
        entry("/dev/ttyACM0", vid=0x1A86, pid=0x55D3),
        entry("/dev/ttyACM1", vid=0x1A86, pid=0x55D3),
    ], [], lambda value: value)
    with pytest.raises(SerialResolutionError) as ambiguous:
        resolve_rs485_port("auto", two)
    assert ambiguous.value.code == "ambiguous_rs485_port"


def test_two_unknown_console_uarts_are_ambiguous():
    devices = discover_serial_devices([entry("/dev/ttyUSB0"), entry("/dev/ttyUSB1")], [], lambda value: value)
    with pytest.raises(SerialResolutionError) as error:
        resolve_console_port("auto", devices)
    assert error.value.code == "ambiguous_console_port"


def test_explicit_ports_win_but_known_role_mismatch_is_rejected(tmp_path):
    devices = inventory()
    assert resolve_console_port("/dev/ttyUSB0", devices).canonical_path == "/dev/ttyUSB0"
    assert resolve_rs485_port("/dev/ttyACM0", devices).canonical_path == "/dev/ttyACM0"
    with pytest.raises(SerialResolutionError) as console_error:
        resolve_console_port("/dev/ttyACM0", devices)
    assert console_error.value.code == "hardware_role_mismatch"
    with pytest.raises(SerialResolutionError) as rs485_error:
        resolve_rs485_port("/dev/ttyUSB0", devices)
    assert rs485_error.value.code == "hardware_role_mismatch"
    missing = str(tmp_path / "missing")
    with pytest.raises(SerialResolutionError) as missing_error:
        resolve_console_port(missing, devices)
    assert missing_error.value.code == "serial_port_missing"


def test_role_conflict_compares_resolved_paths(monkeypatch):
    monkeypatch.setattr("serial_devices.os.path.realpath", lambda path: "/dev/ttyACM0")
    with pytest.raises(SerialResolutionError) as error:
        ensure_distinct_roles("/dev/serial/by-id/console", "/dev/ttyACM0")
    assert error.value.code == "serial_role_conflict"


def test_tty_number_changes_do_not_change_waveshare_identity():
    for path in ("/dev/ttyACM0", "/dev/ttyACM9"):
        devices = discover_serial_devices([
            entry(path, vid=0x1A86, pid=0x55D3, description="USB Single Serial")
        ], [], lambda value: value)
        assert resolve_rs485_port("auto", devices).canonical_path == path


def test_by_id_identity_works_without_pyserial_metadata():
    alias = "/dev/serial/by-id/usb-1a86_USB_Single_Serial_5B97005529-if00"
    devices = discover_serial_devices([], [alias], lambda _: "/dev/ttyACM4")
    assert resolve_rs485_port("auto", devices).canonical_path == "/dev/ttyACM4"


def load_main_port_helpers(namespace):
    path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name in {"find_port", "create_rs485_reader"}]
    namespace.setdefault("LOG", logging.getLogger("guardian-test"))
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


def test_runtime_helper_disabled_does_not_discover_or_create_reader():
    namespace = load_main_port_helpers({
        "discover_serial_devices": lambda: pytest.fail("must not discover"),
        "resolve_rs485_port": resolve_rs485_port,
        "ensure_distinct_roles": ensure_distinct_roles,
        "PassiveRs485Reader": lambda *_: pytest.fail("must not create reader"),
    })
    assert namespace["create_rs485_reader"]({}, "/dev/ttyUSB0") is None


def test_runtime_helper_enabled_resolves_waveshare_and_checks_conflict():
    devices = inventory()
    captured = {}
    class Reader:
        def __init__(self, resolver, baudrate):
            self.port_resolver = resolver
            captured.update(resolver=resolver, baudrate=baudrate)
    namespace = load_main_port_helpers({
        "discover_serial_devices": lambda: devices,
        "resolve_rs485_port": resolve_rs485_port,
        "ensure_distinct_roles": ensure_distinct_roles,
        "PassiveRs485Reader": Reader,
    })
    reader = namespace["create_rs485_reader"]({
        "rs485_sniffer_enabled": True,
        "rs485_sniffer_port": "auto",
        "rs485_sniffer_baudrate": 115200,
    }, "/dev/ttyUSB0")
    assert isinstance(reader, Reader)
    assert captured["baudrate"] == 115200
    assert "usb-1a86" in captured["resolver"]()

    conflict_namespace = load_main_port_helpers({
        "discover_serial_devices": lambda: devices,
        "resolve_rs485_port": lambda *_: SimpleNamespace(preferred_path=lambda: "/dev/ttyUSB0"),
        "ensure_distinct_roles": ensure_distinct_roles,
        "PassiveRs485Reader": Reader,
    })
    conflict = conflict_namespace["create_rs485_reader"]({"rs485_sniffer_enabled": True}, "/dev/ttyUSB0")
    with pytest.raises(SerialResolutionError) as error:
        conflict.port_resolver()
    assert error.value.code == "serial_role_conflict"


def test_missing_sniffer_is_deferred_to_reader_and_cannot_break_console_creation():
    class Reader:
        def __init__(self, resolver, baudrate):
            self.port_resolver = resolver
    namespace = load_main_port_helpers({
        "discover_serial_devices": lambda: inventory("prolific"),
        "resolve_rs485_port": resolve_rs485_port,
        "ensure_distinct_roles": ensure_distinct_roles,
        "PassiveRs485Reader": Reader,
    })
    reader = namespace["create_rs485_reader"]({"rs485_sniffer_enabled": True}, "/dev/ttyUSB0")
    assert isinstance(reader, Reader)
    with pytest.raises(SerialResolutionError) as error:
        reader.port_resolver()
    assert error.value.code == "rs485_port_unavailable"
