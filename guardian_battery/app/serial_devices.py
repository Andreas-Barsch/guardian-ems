"""Role-safe serial device discovery for Guardian Console and RS485 input."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from serial.tools import list_ports


WAVESHARE_VID = 0x1A86
WAVESHARE_PID = 0x55D3


class SerialResolutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SerialDevice:
    canonical_path: str
    paths: tuple[str, ...]
    vid: int | None = None
    pid: int | None = None
    description: str = ""
    manufacturer: str = ""
    product: str = ""
    serial_number: str = ""
    hwid: str = ""

    def preferred_path(self) -> str:
        by_id = sorted(path for path in self.paths if "/serial/by-id/" in path)
        return by_id[0] if by_id else sorted(self.paths)[0]

    def searchable_text(self) -> str:
        return " ".join((*self.paths, self.description, self.manufacturer,
                         self.product, self.serial_number, self.hwid)).lower()

    def is_waveshare(self) -> bool:
        text = self.searchable_text()
        return ((self.vid, self.pid) == (WAVESHARE_VID, WAVESHARE_PID)
                or ("usb-1a86_usb_single_serial" in text)
                or ("usb single serial" in text and "1a86" in text and "55d3" in text))

    def is_prolific(self) -> bool:
        return "prolific" in self.searchable_text()


def discover_serial_devices(
    port_entries: Iterable[object] | None = None,
    aliases: Iterable[str] | None = None,
    realpath: Callable[[str], str] = os.path.realpath,
) -> list[SerialDevice]:
    """Inventory serial nodes, grouping by their resolved device node."""
    entries = list(list_ports.comports() if port_entries is None else port_entries)
    alias_paths = list(aliases if aliases is not None else (
        glob.glob("/dev/serial/by-id/*")
        + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    ))
    grouped: dict[str, dict] = {}

    def group(path: str) -> dict:
        canonical = realpath(path)
        return grouped.setdefault(canonical, {"canonical_path": canonical, "paths": set()})

    for entry in entries:
        path = str(getattr(entry, "device", "") or "")
        if not path:
            continue
        value = group(path)
        value["paths"].add(path)
        for name in ("vid", "pid", "description", "manufacturer", "product",
                     "serial_number", "hwid"):
            candidate = getattr(entry, name, None)
            if candidate not in (None, ""):
                value[name] = candidate
    for path in alias_paths:
        value = group(str(path))
        value["paths"].add(str(path))

    return sorted((SerialDevice(
        canonical_path=value["canonical_path"],
        paths=tuple(sorted(value["paths"])),
        vid=value.get("vid"), pid=value.get("pid"),
        description=str(value.get("description", "")),
        manufacturer=str(value.get("manufacturer", "")),
        product=str(value.get("product", "")),
        serial_number=str(value.get("serial_number", "")),
        hwid=str(value.get("hwid", "")),
    ) for value in grouped.values()), key=lambda item: item.preferred_path())


def _explicit(configured: str, devices: list[SerialDevice], role: str) -> SerialDevice:
    configured_real = os.path.realpath(configured)
    matches = [item for item in devices if configured in item.paths
               or configured_real == item.canonical_path]
    if not matches:
        if not Path(configured).exists():
            raise SerialResolutionError("serial_port_missing", f"Serial port does not exist: {configured}")
        matches = [SerialDevice(configured_real, (configured,))]
    device = matches[0]
    if role == "console" and device.is_waveshare():
        raise SerialResolutionError("hardware_role_mismatch", "Waveshare cannot be used as Console port")
    if role == "rs485" and device.is_prolific():
        raise SerialResolutionError("hardware_role_mismatch", "Prolific Console adapter cannot be used as RS485 sniffer")
    return device


def resolve_rs485_port(configured: str, devices: list[SerialDevice]) -> SerialDevice:
    if configured and configured.lower() != "auto":
        return _explicit(configured, devices, "rs485")
    matches = [device for device in devices if device.is_waveshare()]
    if not matches:
        raise SerialResolutionError("rs485_port_unavailable", "No verified Waveshare RS485 adapter found")
    if len(matches) > 1:
        raise SerialResolutionError("ambiguous_rs485_port", "Multiple Waveshare RS485 adapters found")
    return matches[0]


def resolve_console_port(configured: str, devices: list[SerialDevice]) -> SerialDevice:
    if configured and configured.lower() != "auto":
        return _explicit(configured, devices, "console")
    candidates = [device for device in devices if not device.is_waveshare()]
    prolific = [device for device in candidates if device.is_prolific()]
    if len(prolific) == 1:
        return prolific[0]
    if len(prolific) > 1 or len(candidates) > 1:
        raise SerialResolutionError("ambiguous_console_port", "Console serial adapter is ambiguous")
    if len(candidates) == 1:
        return candidates[0]
    raise SerialResolutionError("console_port_unavailable", "No Console serial adapter found")


def ensure_distinct_roles(console_port: str, rs485_port: str) -> None:
    if os.path.realpath(console_port) == os.path.realpath(rs485_port):
        raise SerialResolutionError("serial_role_conflict", "Console and RS485 resolve to the same device")
