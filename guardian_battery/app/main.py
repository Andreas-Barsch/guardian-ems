from __future__ import annotations

import json
import logging
import os
import re
import signal
import statistics
from collections import deque
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cell_diagnostics import CellDiagnosticStore, CellSample, DIAGNOSTIC_PARAMETER_META
from cell_history import CellHistoryWriter, cell_history_timing
from diagnostic_aggregates import DiagnosticAggregateStore
from diagnostic_backfill import DiagnosticAggregateBackfill
from current_condition_backfill import CurrentConditionBackfill
from config_history import ConfigHistory
from daily_diagnostics import DailyDiagnosticSources
from daily_diagnostic_worker import DailyDiagnosticWorker
from hycube_evidence import collector_from_options
from config_ui import (configure_maintenance_live_publisher,
                       configure_rs485_status_provider,
                       record_stable_observed_positions, start_config_server)
from maintenance_mqtt import MaintenanceMqttPublisher
from maintenance import DEFAULT_MAINTENANCE_EVENT_FILE, MaintenanceEventLog
from mqtt_projection import (MQTT_MAX_ATTRIBUTE_BYTES, MQTT_MAX_PAYLOAD_BYTES,
                             compact_battery_diagnostics, compact_cell_attributes,
                             compact_text)
from position_history import (DEFAULT_POSITION_HISTORY_FILE, documented_identity_at,
                              current_presence, history_observation_ready,
                              missing_expected_positions,
                              resolve_maintenance_event_identities, update_observed_stack,
                              update_rs485_observations)
from stack_soc import current_stack_soc
from serial_devices import discover_serial_devices, resolve_console_port, resolve_rs485_port, ensure_distinct_roles
from rs485_sniffer import PassiveRs485Reader
from rs485_evidence import (DEFAULT_RS485_HISTORY_DIR, Rs485EvidencePipeline,
                            Rs485EvidenceWriter, restore_latest_identities)
from rs485_mqtt import Rs485MqttProjection
from rs485_identity import project_current_management, resolve_rs485_identity
from version import DIAGNOSTIC_ENGINE_VERSION, GUARDIAN_VERSION

import paho.mqtt.client as mqtt
import serial

LOG = logging.getLogger("guardian_battery")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

RUNNING = True
OPTIONS_FILE = Path("/data/options.json")
SHARE_DIR = Path("/share/guardian_battery")
STATE_FILE = SHARE_DIR / "guardian_state.json"
EVENT_FILE = SHARE_DIR / "events.jsonl"
HISTORY_FILE = SHARE_DIR / "trend_history.json"
INCIDENT_FILE = SHARE_DIR / "incident_state.json"
CELL_DIAG_FILE = SHARE_DIR / "cell_diagnostics.json"
CELL_HISTORY_DIR = SHARE_DIR / "cell_history"
DIAGNOSTIC_AGGREGATE_FILE = SHARE_DIR / "diagnostic_aggregates.json"
CONFIG_HISTORY_FILE = SHARE_DIR / "config_history.jsonl"
DAILY_DIAGNOSTICS_ROOT = SHARE_DIR / "diagnostics"
HYCUBE_HISTORY_DIR = SHARE_DIR / "hycube_history"
HYCUBE_POLICY_HISTORY_DIR = SHARE_DIR / "hycube_policy_history"
SHARE_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class Module:
    module: int
    voltage_v: float
    current_a: float
    temperature_c: float
    min_temperature_c: float
    max_temperature_c: float
    min_cell_v: float
    max_cell_v: float
    state: str
    voltage_state: str
    current_state: str
    temperature_state: str
    soc_percent: float
    timestamp_text: str = ""
    battery_voltage_state: str = ""
    battery_temperature_state: str = ""
    mos_temperature_c: Optional[float] = None
    mos_temperature_state: str = ""
    pwr_sample_at: float | None = None

    @property
    def delta_mv(self) -> int:
        return round((self.max_cell_v - self.min_cell_v) * 1000)


def stop(*_args):
    global RUNNING
    RUNNING = False


def load_options() -> dict:
    return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))


def load_persistent_state() -> dict:
    if not STATE_FILE.exists():
        return {"alarm_counts": {}, "last_alarm_codes": [], "last_status": None}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        LOG.warning("Persistenter Zustand war nicht lesbar und wird neu aufgebaut.")
        return {"alarm_counts": {}, "last_alarm_codes": [], "last_status": None}


def save_persistent_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def append_event(event: dict) -> None:
    with EVENT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")



def load_json_file(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        LOG.warning("Datei %s war nicht lesbar.", path)
    return default


def save_json_file(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def update_trends(modules: list[Module], options: dict) -> dict[int, dict]:
    history = load_json_file(HISTORY_FILE, {})
    now = time.time()
    cutoff = now - int(options["trend_window_minutes"]) * 60
    min_change = int(options["trend_min_change_mv"])
    results: dict[int, dict] = {}

    for module in modules:
        key = str(module.module)
        points = history.get(key, [])
        points.append({"t": now, "delta_mv": module.delta_mv, "soc": module.soc_percent})
        points = [p for p in points if p.get("t", 0) >= cutoff][-1000:]
        history[key] = points

        if len(points) < 3:
            trend = "insufficient_data"
            delta_change = 0
            soc_change = 0
        else:
            delta_change = round(points[-1]["delta_mv"] - points[0]["delta_mv"], 1)
            soc_change = round(points[-1]["soc"] - points[0]["soc"], 1)
            if delta_change >= min_change:
                trend = "rising"
            elif delta_change <= -min_change:
                trend = "falling"
            else:
                trend = "stable"

        results[module.module] = {
            "cell_delta_trend": trend,
            "cell_delta_change_mv": delta_change,
            "soc_change_pct": soc_change,
            "window_minutes": int(options["trend_window_minutes"]),
        }

    save_json_file(HISTORY_FILE, history)
    return results


def update_incident_state(status: str, alarms: list[dict], options: dict) -> dict:
    state = load_json_file(INCIDENT_FILE, {
        "active": False,
        "started_at": None,
        "last_alarm_at": None,
        "cleared_at": None,
        "last_summary": "kein Incident",
    })
    now = time.time()
    has_alarm = bool(alarms)

    if has_alarm:
        if not state.get("active"):
            state["active"] = True
            state["started_at"] = now
        state["last_alarm_at"] = now
        state["last_summary"] = alarms[0]["message"]
    elif state.get("active"):
        hold = int(options["incident_hold_minutes"]) * 60
        last_alarm_at = state.get("last_alarm_at") or now
        if now - last_alarm_at >= hold:
            state["active"] = False
            state["cleared_at"] = now
            state["last_summary"] = "Incident beendet"

    save_json_file(INCIDENT_FILE, state)
    return state


def find_port(configured: str) -> str:
    device = resolve_console_port(configured, discover_serial_devices())
    port = device.preferred_path()
    source = ("explicit" if configured and configured.lower() != "auto"
              else "prolific_identity" if device.is_prolific()
              else "single_non_waveshare_uart")
    LOG.info("Console serial resolved: port=%s identity_source=%s", port, source)
    return port


def create_rs485_reader(options: dict, console_port: str, frame_callback=None):
    if not bool(options.get("rs485_sniffer_enabled", False)):
        LOG.info("RS485 passive sniffer disabled")
        return None

    LOG.info(
        "RS485 passive sniffer enabled: port_config=%s baud=%s",
        options.get("rs485_sniffer_port", "auto"),
        int(options.get("rs485_sniffer_baudrate", 115200)),
    )

    def resolve_port() -> str:
        device = resolve_rs485_port(
            str(options.get("rs485_sniffer_port", "auto")), discover_serial_devices()
        )
        selected = device.preferred_path()
        ensure_distinct_roles(console_port, selected)
        return selected

    arguments = ([resolve_port, int(options.get("rs485_sniffer_baudrate", 115200))])
    return (PassiveRs485Reader(*arguments, frame_callback=frame_callback)
            if frame_callback is not None else PassiveRs485Reader(*arguments))


class PylontechConsole:
    def __init__(self, port: str, baudrate: int, timeout: float):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial: Optional[serial.Serial] = None

    def open(self) -> None:
        if self.serial and self.serial.is_open:
            return

        LOG.info("Öffne %s mit %s Baud", self.port, self.baudrate)
        self.serial = serial.Serial(
            self.port,
            baudrate=self.baudrate,
            timeout=0.1,
            write_timeout=2,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        time.sleep(0.4)
        self.serial.reset_input_buffer()
        self.serial.write(b"\r\n")
        self.serial.flush()
        time.sleep(0.25)
        self._drain()

    def close(self) -> None:
        if self.serial:
            self.serial.close()

    def _drain(self) -> bytes:
        assert self.serial is not None
        chunks: list[bytes] = []
        while self.serial.in_waiting:
            chunks.append(self.serial.read(self.serial.in_waiting))
            time.sleep(0.02)
        return b"".join(chunks)

    def command(self, command: str) -> str:
        self.open()
        assert self.serial is not None

        self.serial.reset_input_buffer()
        self.serial.write(command.strip().encode("ascii", errors="ignore") + b"\r\n")
        self.serial.flush()

        deadline = time.monotonic() + self.timeout
        chunks: list[bytes] = []
        last_rx = time.monotonic()

        while time.monotonic() < deadline:
            if self.serial.in_waiting:
                chunks.append(self.serial.read(self.serial.in_waiting))
                last_rx = time.monotonic()
            elif chunks and time.monotonic() - last_rx > 0.45:
                break
            time.sleep(0.03)

        return (
            b"".join(chunks)
            .decode("utf-8", errors="replace")
            .replace("\x00", "")
            .replace("\r", "")
        )


def parse_pwr(text: str, expected_modules: int) -> list[Module]:
    lines = [re.sub(r"\s+", " ", line.strip()) for line in text.splitlines()]
    modules: list[Module] = []

    main_pattern = re.compile(
        r"^(?P<module>\d{1,2})\s+"
        r"(?P<voltage>-?\d+)\s+"
        r"(?P<current>-?\d+)\s+"
        r"(?P<temperature>-?\d+)\s+"
        r"(?P<tlow>-?\d+)\s+"
        r"(?P<thigh>-?\d+)\s+"
        r"(?P<vlow>-?\d+)\s+"
        r"(?P<vhigh>-?\d+)\s+"
        r"(?P<state>\S+)\s+"
        r"(?P<voltage_state>\S+)\s+"
        r"(?P<current_state>\S+)\s+"
        r"(?P<temperature_state>\S+)\s+"
        r"(?P<soc>\d+(?:\.\d+)?)%"
    )
    continuation_pattern = re.compile(
        r"^(?P<date>\d{2}-\d{2}-\d{2})\s+"
        r"(?P<time>\d{2}:\d{2}:\d{2})\s+"
        r"(?P<bvstate>\S+)\s+"
        r"(?P<btstate>\S+)\s+"
        r"(?P<mostemp>-?\d+)\s+"
        r"(?P<mosstate>\S+)"
    )

    idx = 0
    while idx < len(lines):
        match = main_pattern.match(lines[idx])
        if not match:
            idx += 1
            continue

        module_no = int(match.group("module"))
        continuation = continuation_pattern.match(lines[idx + 1]) if idx + 1 < len(lines) else None

        module = Module(
            module=module_no,
            voltage_v=int(match.group("voltage")) / 1000,
            current_a=int(match.group("current")) / 1000,
            temperature_c=int(match.group("temperature")) / 1000,
            min_temperature_c=int(match.group("tlow")) / 1000,
            max_temperature_c=int(match.group("thigh")) / 1000,
            min_cell_v=int(match.group("vlow")) / 1000,
            max_cell_v=int(match.group("vhigh")) / 1000,
            state=match.group("state"),
            voltage_state=match.group("voltage_state"),
            current_state=match.group("current_state"),
            temperature_state=match.group("temperature_state"),
            soc_percent=float(match.group("soc")),
        )

        if continuation:
            module.timestamp_text = f"{continuation.group('date')} {continuation.group('time')}"
            module.battery_voltage_state = continuation.group("bvstate")
            module.battery_temperature_state = continuation.group("btstate")
            module.mos_temperature_c = int(continuation.group("mostemp")) / 1000
            module.mos_temperature_state = continuation.group("mosstate")
            idx += 1

        modules.append(module)
        idx += 1

    unique = {module.module: module for module in modules}
    return [unique[number] for number in sorted(unique)]



BAT_ROW = re.compile(r"^(\d{1,2})\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\d+(?:\.\d+)?)%\s+(-?\d+)\s+mAH\s+([YN])$")
def parse_bat(text: str) -> list[dict]:
    rows=[]
    for raw in text.splitlines():
        m=BAT_ROW.match(re.sub(r"\s+"," ",raw.strip()))
        if m: rows.append({'cell':int(m.group(1))+1,'voltage_mv':int(m.group(2)),'temperature_c':int(m.group(4))/1000,'balancing':m.group(11)=='Y'})
    return rows if len(rows)==15 else []

def parse_stat(text: str) -> dict:
    out={}; wanted={'CYCLE Times':'cycles','SOH':'soh_percent','LifeWarn Times':'life_warn','LifeAlarm Times':'life_alarm'}
    for raw in text.splitlines():
        if ':' in raw:
            k,v=(x.strip() for x in raw.split(':',1))
            if k in wanted:
                try: out[wanted[k]]=int(v)
                except ValueError: pass
    return out


INFO_FIELDS = {
    "Device address": "device_address",
    "Manufacturer": "manufacturer",
    "Device name": "device_name",
    "Board version": "board_version",
    "Board": "board",
    "Main Soft version": "main_soft_version",
    "Soft version": "soft_version",
    "Boot version": "boot_version",
    "Comm version": "comm_version",
    "Release Date": "release_date",
    "Barcode": "barcode",
    "Specification": "specification",
    "Cell Number": "cell_number",
    "Max Dischg Curr": "max_discharge_current_ma",
    "Max Charge Curr": "max_charge_current_ma",
    "EPPNPort rate": "eppn_port_rate",
    "Console Port rate": "console_port_rate",
}

def parse_info(text: str) -> dict:
    """Parse the key/value fields returned by Pylontech `info <module>`."""
    out: dict = {}
    for raw in text.splitlines():
        if ":" not in raw:
            continue
        key, value = (part.strip() for part in raw.split(":", 1))
        target = INFO_FIELDS.get(key)
        if not target or not value:
            continue
        if target in {
            "device_address", "cell_number", "max_discharge_current_ma",
            "max_charge_current_ma", "eppn_port_rate", "console_port_rate",
        }:
            try:
                out[target] = int(value)
            except ValueError:
                out[target] = value
        else:
            out[target] = value
    return out


def abnormal_states(module: Module) -> list[str]:
    result = []
    for label, value in (
        ("Spannung", module.voltage_state),
        ("Strom", module.current_state),
        ("Temperatur", module.temperature_state),
        ("Batteriespannung", module.battery_voltage_state),
        ("Batterietemperatur", module.battery_temperature_state),
        ("MOS-Temperatur", module.mos_temperature_state),
    ):
        if value and value.lower() not in {"normal", "-", "unknown"}:
            result.append(f"{label}: {value}")
    return result


def health_assessment(module: Module, median_soc: float, options: dict, alarm_count: int) -> dict:
    score = 100
    findings: list[str] = []
    recommendation = "Keine Maßnahme erforderlich."

    soc_deviation = abs(module.soc_percent - median_soc)
    warning_soc = int(options["warning_soc_deviation_pct"])
    critical_soc = int(options["critical_soc_deviation_pct"])

    if module.state.lower() == "syserror":
        score -= 45
        findings.append("SysError")
        recommendation = "Modul prüfen und Support kontaktieren."

    if module.delta_mv >= int(options["critical_cell_delta_mv"]):
        score -= 35
        findings.append(f"kritische Zellspreizung {module.delta_mv} mV")
        recommendation = "Lade-/Entladevorgang beobachten und Supportfall dokumentieren."
    elif module.delta_mv >= int(options["warning_cell_delta_mv"]):
        score -= 15
        findings.append(f"erhöhte Zellspreizung {module.delta_mv} mV")
        recommendation = "Weiter beobachten."

    if soc_deviation >= critical_soc:
        score -= 30
        findings.append(f"SOC-Abweichung {soc_deviation:.0f} Prozentpunkte")
        recommendation = "SOC nach vollständigem Ladezyklus erneut prüfen; bei Fortbestand Supportfall vorbereiten."
    elif soc_deviation >= warning_soc:
        score -= 12
        findings.append(f"SOC-Abweichung {soc_deviation:.0f} Prozentpunkte")
        recommendation = "SOC-Synchronisation beobachten."

    abnormal = abnormal_states(module)
    if abnormal:
        score -= min(25, 8 * len(abnormal))
        findings.extend(abnormal)
        recommendation = "Abweichende BMS-Statuswerte prüfen."

    if alarm_count:
        score -= min(15, alarm_count)
        findings.append(f"{alarm_count} gespeicherte Alarmereignisse")

    score = max(0, min(100, score))

    if score < 50:
        level = "critical"
    elif score < 80:
        level = "warning"
    else:
        level = "ok"

    if not findings:
        findings.append("Alle überwachten Werte normal")

    return {
        "score": score,
        "level": level,
        "assessment": "; ".join(findings),
        "recommendation": recommendation,
        "soc_deviation_pct": round(soc_deviation, 1),
    }


def evaluate(modules: list[Module], options: dict) -> tuple[str, list[dict]]:
    alarms: list[dict] = []
    median_soc = statistics.median([m.soc_percent for m in modules]) if modules else 0

    for missing in missing_expected_positions(
            int(options["module_count"]), (module.module for module in modules)):
        alarms.append({
            "level": "critical" if options["missing_module_is_critical"] else "warning",
            "code": "module_missing",
            "module": missing,
            "message": f"Modul {missing} liefert keine Daten.",
        })

    for module in modules:
        if module.state.lower() == "syserror":
            alarms.append({
                "level": "critical",
                "code": "system_error",
                "module": module.module,
                "message": f"Modul {module.module} meldet SysError.",
            })

        if module.delta_mv >= int(options["critical_cell_delta_mv"]):
            alarms.append({
                "level": "critical",
                "code": "cell_delta_critical",
                "module": module.module,
                "message": (
                    f"Modul {module.module}: Zellspreizung {module.delta_mv} mV "
                    f"(min {module.min_cell_v:.3f} V, max {module.max_cell_v:.3f} V)."
                ),
            })
        elif module.delta_mv >= int(options["warning_cell_delta_mv"]):
            alarms.append({
                "level": "warning",
                "code": "cell_delta_warning",
                "module": module.module,
                "message": (
                    f"Modul {module.module}: Zellspreizung {module.delta_mv} mV "
                    f"(min {module.min_cell_v:.3f} V, max {module.max_cell_v:.3f} V)."
                ),
            })

        soc_deviation = abs(module.soc_percent - median_soc)
        if soc_deviation >= int(options["critical_soc_deviation_pct"]):
            alarms.append({
                "level": "warning",
                "code": "soc_deviation_critical",
                "module": module.module,
                "message": (
                    f"Modul {module.module}: SOC {module.soc_percent:.0f} %, "
                    f"Median des Stacks {median_soc:.0f} % "
                    f"(Abweichung {soc_deviation:.0f} Prozentpunkte)."
                ),
            })
        elif soc_deviation >= int(options["warning_soc_deviation_pct"]):
            alarms.append({
                "level": "warning",
                "code": "soc_deviation_warning",
                "module": module.module,
                "message": (
                    f"Modul {module.module}: SOC-Abweichung {soc_deviation:.0f} Prozentpunkte."
                ),
            })

        for item in abnormal_states(module):
            alarms.append({
                "level": "warning",
                "code": "abnormal_state",
                "module": module.module,
                "message": f"Modul {module.module}: {item}.",
            })

    if any(alarm["level"] == "critical" for alarm in alarms):
        return "critical", alarms
    if alarms:
        return "warning", alarms
    return "ok", alarms


class Mqtt:
    def __init__(self, options: dict):
        host = os.environ["MQTT_HOST"]
        port = int(os.environ.get("MQTT_PORT", "1883"))
        username = os.environ.get("MQTT_USERNAME", "")
        password = os.environ.get("MQTT_PASSWORD", "")

        self.prefix = options["mqtt_topic_prefix"].rstrip("/")
        self.discovery_enabled = bool(options["publish_discovery"])
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="guardian_battery")
        if username:
            self.client.username_pw_set(username, password)

        self.client.will_set(f"{self.prefix}/battery/availability", "offline", retain=True)
        self.client.connect(host, port, keepalive=60)
        self.client.loop_start()
        self._publish(f"{self.prefix}/battery/availability", "online", retain=True)
        self.maintenance_events = MaintenanceMqttPublisher(self.client, self.prefix)

    def close(self) -> None:
        self._publish(f"{self.prefix}/battery/availability", "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()

    def discovery(self, module_count: int) -> None:
        if not self.discovery_enabled:
            return

        device = {
            "identifiers": ["guardian_battery"],
            "name": "Guardian Battery",
            "manufacturer": "Guardian EMS",
            "model": "Pylontech US2000C Stack Monitor",
            "sw_version": GUARDIAN_VERSION,
        }

        self.maintenance_events.discovery(device)

        sensors = [
            ("stack_status", "Guardian Batteriestatus", None, None, "mdi:shield-battery"),
            ("stack_health_score", "Guardian Health Score", "%", None, "mdi:heart-pulse"),
            ("stack_recommendation", "Guardian Empfehlung", None, None, "mdi:lightbulb-alert"),
            ("stack_status_reason", "Guardian Statusursache", None, None, "mdi:alert-circle-check"),
            ("critical_module", "Guardian auffälligstes Modul", None, None, "mdi:battery-alert"),
            ("modules_present", "Guardian erkannte Module", None, None, "mdi:battery-multiple"),
            ("unexpected_modules_present", "Guardian zusätzliche Module", None, None, "mdi:battery-plus"),
            ("active_alarms", "Guardian aktive Batteriealarme", None, None, "mdi:alert-circle"),
            ("last_alarm", "Guardian letzter Batteriealarm", None, None, "mdi:message-alert"),
            ("last_update", "Guardian letzte Aktualisierung", None, "timestamp", "mdi:clock-check"),
            ("incident_active", "Guardian Incident aktiv", None, None, "mdi:alert-decagram"),
            ("incident_summary", "Guardian Incident Zusammenfassung", None, None, "mdi:text-box-alert"),
            ("bms_soh", "Pylontech BMS SOH", "%", None, "mdi:battery-heart"),
            ("bms_cycles", "Pylontech BMS Zyklen", None, None, "mdi:counter"),
            ("cell_diag_config", "Zelldiagnostik Konfiguration", None, None, "mdi:tune-variant"),
            ("stack_soc_median", "Guardian SOC-Modulmedian", "%", "battery", "mdi:chart-bell-curve"),
        ]

        # All six inventory positions retain their technical entity IDs. Current
        # topology is expressed through presence and per-position availability.
        for module in range(1, 7):
            sensors.extend([
                (f"module_{module}_presence", f"Modul {module} Topologiestatus", None, None, "mdi:battery-check"),
                (f"module_{module}_info", f"Modul {module} BMS-Info", None, None, "mdi:information-outline"),
                (f"module_{module}_health_score", f"Modul {module} Health Score", "%", None, "mdi:heart-pulse"),
                (f"module_{module}_health", f"Modul {module} Gesundheit", None, None, "mdi:shield-check"),
                (f"module_{module}_health_reason", f"Modul {module} Bewertung", None, None, "mdi:text-box-check"),
                (f"module_{module}_recommendation", f"Modul {module} Empfehlung", None, None, "mdi:lightbulb-on"),
                (f"module_{module}_soc_deviation", f"Modul {module} SOC-Abweichung", "%", None, "mdi:chart-bell-curve"),
                (f"module_{module}_soc_peer_deviation", f"Modul {module} SOC-Abweichung zum Stackmedian", "pp", None, "mdi:swap-vertical"),
                (f"module_{module}_cell_delta_trend", f"Modul {module} Zellspreizung Trend", None, None, "mdi:trending-up"),
                (f"module_{module}_cell_delta_change", f"Modul {module} Zellspreizung Änderung", "mV", None, "mdi:delta"),
                (f"module_{module}_soc_change", f"Modul {module} SOC Änderung", "%", None, "mdi:chart-timeline-variant"),
                (f"module_{module}_voltage", f"Modul {module} Spannung", "V", "voltage", "mdi:sine-wave"),
                (f"module_{module}_current", f"Modul {module} Strom", "A", "current", "mdi:current-dc"),
                (f"module_{module}_temperature", f"Modul {module} Temperatur", "°C", "temperature", "mdi:thermometer"),
                (f"module_{module}_soc", f"Modul {module} SOC", "%", "battery", "mdi:battery"),
                (f"module_{module}_state", f"Modul {module} Zustand", None, None, "mdi:battery-sync"),
                (f"module_{module}_min_cell", f"Modul {module} minimale Zellspannung", "V", "voltage", "mdi:arrow-down-bold"),
                (f"module_{module}_max_cell", f"Modul {module} maximale Zellspannung", "V", "voltage", "mdi:arrow-up-bold"),
                (f"module_{module}_cell_delta", f"Modul {module} Zellspreizung", "mV", None, "mdi:delta"),
                (f"module_{module}_cell_median", f"Modul {module} Zellmedian", "mV", None, "mdi:chart-bell-curve"),
                (f"module_{module}_mos_temperature", f"Modul {module} MOS-Temperatur", "°C", "temperature", "mdi:thermometer-lines"),
                (f"module_{module}_cell_diag_status", f"Modul {module} Zelldiagnostik Status", None, None, "mdi:battery-search"),
                (f"module_{module}_cell_diag_live", f"Modul {module} Zelldiagnostik Live", None, None, "mdi:battery-search"),
                (f"module_{module}_cell_diag_confidence", f"Modul {module} Zelldiagnostik Confidence", None, None, "mdi:check-decagram"),
                (f"module_{module}_cell_diag_samples", f"Modul {module} Zelldiagnostik Messpunkte", None, None, "mdi:counter"),
                (f"module_{module}_cell_diag_worst_cell", f"Modul {module} auffälligste Zelle", None, None, "mdi:battery-alert"),
                (f"module_{module}_cell_diag_evidence", f"Modul {module} Zelldiagnostik Evidenzabweichung", "mV", None, "mdi:delta"),
                (f"module_{module}_cell_diag_trend", f"Modul {module} Zelldiagnostik Trend", None, None, "mdi:trending-up"),
                (f"module_{module}_cell_diag_maintenance_risk", f"Modul {module} Maintenance Risk", None, None, "mdi:wrench-clock"),
                (f"module_{module}_cell_diag_trend_risk_confidence", f"Modul {module} Trend Risk Confidence", None, None, "mdi:check-decagram-outline"),
            ])
            for cell in range(1, 16):
                sensors.extend([
                    (f"module_{module}_cell_{cell}_status", f"Modul {module} Zelle {cell} Bewertung", None, None, "mdi:battery-medium"),
                    (f"module_{module}_cell_{cell}_confidence", f"Modul {module} Zelle {cell} Confidence", None, None, "mdi:check-decagram"),
                    (f"module_{module}_cell_{cell}_voltage", f"Modul {module} Zelle {cell} Spannung", "mV", None, "mdi:sine-wave"),
                    (f"module_{module}_cell_{cell}_deviation", f"Modul {module} Zelle {cell} Abweichung", "mV", None, "mdi:delta"),
                    (f"module_{module}_cell_{cell}_evidence", f"Modul {module} Zelle {cell} Evidenzabweichung", "mV", None, "mdi:chart-bell-curve"),
                    (f"module_{module}_cell_{cell}_low_deviation", f"Modul {module} Zelle {cell} Tiefbereich Abweichung", "mV", None, "mdi:arrow-collapse-down"),
                    (f"module_{module}_cell_{cell}_discharge_deviation", f"Modul {module} Zelle {cell} Entladung Abweichung", "mV", None, "mdi:battery-minus"),
                    (f"module_{module}_cell_{cell}_charge_deviation", f"Modul {module} Zelle {cell} Ladung Abweichung", "mV", None, "mdi:battery-plus"),
                    (f"module_{module}_cell_{cell}_high_deviation", f"Modul {module} Zelle {cell} Hochbereich Abweichung", "mV", None, "mdi:arrow-collapse-up"),
                    (f"module_{module}_cell_{cell}_low_lowest", f"Modul {module} Zelle {cell} Tiefbereich Lowest", "%", None, "mdi:arrow-down-bold"),
                    (f"module_{module}_cell_{cell}_discharge_lowest", f"Modul {module} Zelle {cell} Entladung Lowest", "%", None, "mdi:arrow-down-bold-circle"),
                    (f"module_{module}_cell_{cell}_low_rank", f"Modul {module} Zelle {cell} Tiefbereich mittlerer Rang", None, None, "mdi:order-numeric-ascending"),
                    (f"module_{module}_cell_{cell}_discharge_rank", f"Modul {module} Zelle {cell} Entladung mittlerer Rang", None, None, "mdi:order-numeric-ascending"),
                    (f"module_{module}_cell_{cell}_charge_rank", f"Modul {module} Zelle {cell} Ladung mittlerer Rang", None, None, "mdi:order-numeric-ascending"),
                    (f"module_{module}_cell_{cell}_high_rank", f"Modul {module} Zelle {cell} Hochbereich mittlerer Rang", None, None, "mdi:order-numeric-ascending"),
                    (f"module_{module}_cell_{cell}_charge_highest", f"Modul {module} Zelle {cell} Ladung Highest", "%", None, "mdi:arrow-up-bold-circle"),
                    (f"module_{module}_cell_{cell}_high_highest", f"Modul {module} Zelle {cell} Hochbereich Highest", "%", None, "mdi:arrow-up-bold"),
                    (f"module_{module}_cell_{cell}_low_samples", f"Modul {module} Zelle {cell} Tiefbereich Messpunkte", "Messpunkte", None, "mdi:counter"),
                    (f"module_{module}_cell_{cell}_discharge_samples", f"Modul {module} Zelle {cell} Entladung Messpunkte", "Messpunkte", None, "mdi:counter"),
                    (f"module_{module}_cell_{cell}_charge_samples", f"Modul {module} Zelle {cell} Ladung Messpunkte", "Messpunkte", None, "mdi:counter"),
                    (f"module_{module}_cell_{cell}_high_samples", f"Modul {module} Zelle {cell} Hochbereich Messpunkte", "Messpunkte", None, "mdi:counter"),
                ])

        for object_id, name, unit, device_class, icon in sensors:
            match = re.match(r"module_([1-6])_", object_id)
            is_presence_or_inventory = (object_id.endswith("_presence")
                                        or object_id.endswith("_info")
                                        or object_id.endswith("_cell_diag_live"))
            availability_topic = f"{self.prefix}/battery/availability"
            if match and not is_presence_or_inventory:
                availability_topic = f"{self.prefix}/battery/module_{match.group(1)}/availability"
            payload = {
                "name": name,
                "unique_id": f"guardian_battery_{object_id}",
                "state_topic": f"{self.prefix}/battery/sensor/{object_id}/state",
                "availability_topic": availability_topic,
                "device": device,
                "icon": icon,
                "json_attributes_topic": f"{self.prefix}/battery/sensor/{object_id}/attributes",
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if device_class in {"voltage", "current", "temperature", "battery"} or unit in {"%", "mV"}:
                payload["state_class"] = "measurement"

            self._publish(
                f"homeassistant/sensor/guardian_battery/{object_id}/config",
                json.dumps(payload),
                retain=True,
            )

    def state(self, name: str, value) -> None:
        if value is None:
            return
        if isinstance(value, float):
            value = f"{value:.4f}".rstrip("0").rstrip(".")
        self._publish(
            f"{self.prefix}/battery/sensor/{name}/state",
            compact_text(value, 4096),
            retain=True,
        )

    def attributes(self, name: str, attributes: dict) -> None:
        self._publish(
            f"{self.prefix}/battery/sensor/{name}/attributes",
            json.dumps(attributes, ensure_ascii=False),
            retain=True,
            limit=MQTT_MAX_ATTRIBUTE_BYTES,
        )

    def _publish(self, topic: str, payload, *, retain: bool, limit: int = MQTT_MAX_PAYLOAD_BYTES):
        encoded = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        packet_size = len(encoded) + len(topic.encode("utf-8")) + 7
        if packet_size > limit:
            raise ValueError(
                f"MQTT packet exceeds {limit} bytes for {topic}: "
                f"payload={len(encoded)}, packet<={packet_size}"
            )
        return self.client.publish(topic, payload, retain=retain)

    @staticmethod
    def _diag_meta(metric: str, phase: str | None = None) -> dict:
        meta = dict(DIAGNOSTIC_PARAMETER_META.get(metric, {}))
        if phase:
            phase_labels = {
                "low": "Tiefbereich", "discharge": "Entladung",
                "charge": "Ladung", "high": "Hochbereich", "rest": "Ruhe"
            }
            meta["betriebsphase"] = phase_labels.get(phase, phase)
        meta["method"] = "Phase-Resolved Cell Voltage Consistency"
        meta["soh_hinweis"] = "Guardian-Zellkonsistenzdiagnostik; nicht mit Pylontech BMS SOH verrechnen."
        return meta

    def publish(
        self,
        modules: list[Module],
        status: str,
        alarms: list[dict],
        options: dict,
        persistent: dict,
        trends: dict[int, dict],
        incident: dict,
        cell_results: dict[int, dict] | None = None,
        bms_stat: dict | None = None,
        module_infos: dict[int, dict] | None = None,
        live_topology: dict[int, dict] | None = None,
    ) -> None:
        soc_peers = current_stack_soc(modules)
        median_soc = soc_peers["median"] if soc_peers["median"] is not None else 0
        cell_results = cell_results or {}
        bms_stat = bms_stat or {}
        module_infos = module_infos or {}
        live_topology = live_topology or {
            position: {"position": position, "expected": position <= int(options["module_count"]),
                       "status": "present" if position in {item.module for item in modules}
                       else "not_expected" if position > int(options["module_count"]) else "unknown"}
            for position in range(1, 7)
        }
        assessments = {}

        for module in modules:
            alarm_count = int(persistent["alarm_counts"].get(str(module.module), 0))
            assessments[module.module] = health_assessment(module, median_soc, options, alarm_count)

        stack_score = round(statistics.mean([a["score"] for a in assessments.values()])) if assessments else 0
        critical_module = min(assessments, key=lambda n: assessments[n]["score"]) if assessments else "-"
        critical_assessment = assessments.get(critical_module, {})
        diagnostic_recommendation = critical_assessment.get("recommendation", "Keine Maßnahme erforderlich.")
        primary_alarm = alarms[0]["message"] if alarms else None
        stack_recommendation = primary_alarm or diagnostic_recommendation
        status_reason = primary_alarm or "Kein aktiver System-/Verfügbarkeitsalarm"
        present_expected = sum(
            1 for value in live_topology.values()
            if value.get("expected")
            and value.get("status", value.get("presence_status")) == "present")
        expected_count = sum(1 for value in live_topology.values() if value.get("expected"))
        unexpected_present = sum(
            1 for value in live_topology.values()
            if not value.get("expected")
            and value.get("status", value.get("presence_status")) == "present")

        self.state("stack_status", status)
        self.state("stack_health_score", stack_score)
        self.state("stack_recommendation", stack_recommendation)
        self.state("stack_status_reason", status_reason)
        self.state("critical_module", f"Diagnostischer Hinweis: Modul {critical_module}" if critical_module != "-" else "-")
        modules_summary = f"{present_expected} / {expected_count}"
        if unexpected_present:
            modules_summary += f" (+{unexpected_present} nicht erwartet)"
        self.state("modules_present", modules_summary)
        self.state("unexpected_modules_present", unexpected_present)
        self.attributes("modules_present", {"present_expected": present_expected,
                                             "expected_module_count": expected_count,
                                             "unexpected_present": unexpected_present})
        self.state("active_alarms", len(alarms))
        self.state("last_alarm", alarms[0]["message"] if alarms else "kein Alarm")
        self.state("last_update", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        self.state("incident_active", "on" if incident.get("active") else "off")
        self.state("incident_summary", incident.get("last_summary", "kein Incident"))
        self.state("bms_soh", bms_stat.get("soh_percent"))
        self.state("bms_cycles", bms_stat.get("cycles"))
        self.state("stack_soc_median", soc_peers["median"])
        if soc_peers["median"] is not None:
            self.attributes("stack_soc_median", {
                "definition": "Median der SOC-Werte aller aktuell vorhandenen Module.",
                "unit": "%", "peer_level": "module_to_stack",
                "active_module_count": soc_peers["module_count"],
                "evidence_level": "observation",
                "causality": "not_determined",
            })

        # Aktive Zelldiagnostik-Konfiguration für UI/Explainability veröffentlichen.
        # Die Werte stammen direkt aus den App-Optionen, damit Dashboard und
        # Bewertungslogik immer die tatsächlich verwendete Parametrierung zeigen.
        self.state("cell_diag_config", "aktiv" if options.get("cell_diagnostics_enabled") else "deaktiviert")
        self.attributes(
            "cell_diag_config",
            {
                "version": GUARDIAN_VERSION,
                "diagnostic_engine_version": DIAGNOSTIC_ENGINE_VERSION,
                "methode": "Phase-Resolved Cell Voltage Consistency",
                "aktiv": bool(options.get("cell_diagnostics_enabled")),
                "intervall_s": options.get("cell_diagnostics_interval_seconds"),
                "min_phase_samples": options.get("cell_diag_min_phase_samples"),
                "confidence_medium_samples": options.get("cell_diag_confidence_medium_samples"),
                "confidence_high_samples": options.get("cell_diag_confidence_high_samples"),
                "low_soc_percent": options.get("cell_diag_low_soc_percent"),
                "high_soc_percent": options.get("cell_diag_high_soc_percent"),
                "charge_current_a": options.get("cell_diag_charge_current_a"),
                "discharge_current_a": options.get("cell_diag_discharge_current_a"),
                "discharge_observe_mv": options.get("cell_diag_discharge_observe_deviation_mv", options.get("cell_diag_observe_deviation_mv")),
                "discharge_warning_mv": options.get("cell_diag_discharge_warning_deviation_mv", options.get("cell_diag_warning_deviation_mv")),
                "discharge_critical_mv": options.get("cell_diag_discharge_critical_deviation_mv", options.get("cell_diag_critical_deviation_mv")),
                "low_observe_mv": options.get("cell_diag_low_observe_deviation_mv", options.get("cell_diag_observe_deviation_mv")),
                "low_warning_mv": options.get("cell_diag_low_warning_deviation_mv", options.get("cell_diag_warning_deviation_mv")),
                "low_critical_mv": options.get("cell_diag_low_critical_deviation_mv", options.get("cell_diag_critical_deviation_mv")),
                "charge_observe_mv": options.get("cell_diag_charge_observe_deviation_mv", options.get("cell_diag_observe_deviation_mv")),
                "charge_warning_mv": options.get("cell_diag_charge_warning_deviation_mv", options.get("cell_diag_warning_deviation_mv")),
                "charge_critical_mv": options.get("cell_diag_charge_critical_deviation_mv", options.get("cell_diag_critical_deviation_mv")),
                "high_observe_mv": options.get("cell_diag_high_observe_deviation_mv", options.get("cell_diag_observe_deviation_mv")),
                "high_warning_mv": options.get("cell_diag_high_warning_deviation_mv", options.get("cell_diag_warning_deviation_mv")),
                "high_critical_mv": options.get("cell_diag_high_critical_deviation_mv", options.get("cell_diag_critical_deviation_mv")),
                "rest_statuswirksam": False,
                "hinweis": "Aktive Guardian-Konfiguration; kein SOH-Wert.",
            },
        )

        payload_modules = []
        for module in modules:
            assessment = assessments[module.module]
            payload_modules.append(asdict(module) | {"delta_mv": module.delta_mv, **assessment, **trends.get(module.module, {})})

        payload = {
            "timestamp": time.time(),
            "status": status,
            "health_score": stack_score,
            "critical_module": critical_module,
            "recommendation": stack_recommendation,
            "status_reason": status_reason,
            "incident": incident,
            "alarms": alarms,
            "modules": payload_modules,
            "cell_diagnostics": compact_battery_diagnostics(cell_results, module_infos),
            "bms_stat": bms_stat,
            "module_info": module_infos,
            "live_topology": live_topology,
        }
        self._publish(f"{self.prefix}/battery/state", json.dumps(payload, ensure_ascii=False), retain=True)
        self._publish(f"{self.prefix}/battery/alarms", json.dumps(alarms, ensure_ascii=False), retain=True)

        for position, topology in sorted(live_topology.items()):
            presence_status = topology.get("status", topology.get("presence_status", "unknown"))
            expected = bool(topology.get("expected"))
            self._publish(f"{self.prefix}/battery/module_{position}/availability",
                          "online" if presence_status == "present" else "offline", retain=True)
            self.state(f"module_{position}_presence", presence_status)
            self.attributes(f"module_{position}_presence", {
                "position": position, "expected": bool(topology.get("expected")),
                "physical_serial": topology.get("physical_serial"),
                "currently_observed_serial": topology.get(
                    "observed_serial", topology.get("currently_observed_serial")),
                "last_seen": topology.get("last_observed_at", topology.get("last_seen")),
                "source": topology.get("sources", topology.get("source", [])),
            })
            diagnostic_status = cell_results.get(position, {}).get("status")
            topology_label = ""
            if presence_status == "present":
                if not expected:
                    topology_label = "NICHT ERWARTET"
                self.state(f"module_{position}_cell_diag_live",
                           diagnostic_status or "LERNPHASE")
            elif presence_status == "stale":
                topology_label = "VERALTET"
                # Preserve the retained last diagnosis; only its topology label
                # changes. No synthetic diagnostic value is created.
            elif presence_status == "absent":
                topology_label = "ENTFERNT / NICHT VERFÜGBAR"
                self.state(f"module_{position}_cell_diag_live", topology_label)
            elif presence_status == "not_expected":
                topology_label = "NICHT ERWARTET · NICHT VORHANDEN"
                self.state(f"module_{position}_cell_diag_live", topology_label)
            else:
                topology_label = "STATUS UNBEKANNT"
                self.state(f"module_{position}_cell_diag_live", topology_label)
            self.attributes(f"module_{position}_cell_diag_live", {
                "module_position": position,
                "expected": expected,
                "presence_status": presence_status,
                "physical_serial": topology.get(
                    "observed_serial", topology.get(
                        "currently_observed_serial", topology.get("physical_serial"))),
                "topology_label": topology_label,
                "diagnostic_status": diagnostic_status if presence_status == "present" else None,
                "diagnostic_engine_version": DIAGNOSTIC_ENGINE_VERSION,
            })

        for module in modules:
            assessment = assessments[module.module]
            base = f"module_{module.module}"
            module_info = module_infos.get(module.module, {})
            if module_info:
                info_state = module_info.get("device_name") or module_info.get("manufacturer") or "verfügbar"
                self.state(f"{base}_info", info_state)
                info_attributes = dict(module_info)
                info_attributes["module"] = module.module
                info_attributes["source"] = f"Pylontech info {module.module}"
                info_attributes["guardian_version"] = GUARDIAN_VERSION
                info_attributes["hinweis"] = "Hersteller-/Identitätsinformationen; kein Guardian-SOH und kein Cycle Count."
                self.attributes(f"{base}_info", info_attributes)
            values = {
                f"{base}_health_score": assessment["score"],
                f"{base}_health": assessment["level"],
                f"{base}_health_reason": assessment["assessment"],
                f"{base}_recommendation": assessment["recommendation"],
                f"{base}_soc_deviation": assessment["soc_deviation_pct"],
                f"{base}_soc_peer_deviation": (
                    round(soc_peers["deviations"][module.module], 2)
                    if module.module in soc_peers["deviations"] else None),
                f"{base}_cell_delta_trend": trends.get(module.module, {}).get("cell_delta_trend", "insufficient_data"),
                f"{base}_cell_delta_change": trends.get(module.module, {}).get("cell_delta_change_mv", 0),
                f"{base}_soc_change": trends.get(module.module, {}).get("soc_change_pct", 0),
                f"{base}_voltage": module.voltage_v,
                f"{base}_current": module.current_a,
                f"{base}_temperature": module.temperature_c,
                f"{base}_soc": module.soc_percent,
                f"{base}_state": module.state,
                f"{base}_min_cell": module.min_cell_v,
                f"{base}_max_cell": module.max_cell_v,
                f"{base}_cell_delta": module.delta_mv,
                f"{base}_cell_median": None,
                f"{base}_mos_temperature": module.mos_temperature_c,
            }
            diag = cell_results.get(module.module, {})
            values[f"{base}_cell_median"] = diag.get("current_median_mv")
            values.update({
                f"{base}_cell_diag_status": diag.get("status"),
                f"{base}_cell_diag_confidence": diag.get("confidence"),
                f"{base}_cell_diag_samples": diag.get("sample_count"),
                f"{base}_cell_diag_worst_cell": diag.get("evidence_worst_cell"),
                f"{base}_cell_diag_evidence": diag.get("evidence_deviation_mv"),
                f"{base}_cell_diag_trend": diag.get("trend"),
                f"{base}_cell_diag_maintenance_risk": diag.get("maintenance_risk"),
                f"{base}_cell_diag_trend_risk_confidence": diag.get("trend_risk_confidence"),
            })
            for key, value in values.items(): self.state(key, value)
            self.attributes(f"{base}_soc_peer_deviation", {
                "definition": "Modul-SOC minus Median der gleichzeitig vorhandenen Stackmodule.",
                "unit": "Prozentpunkte", "peer_level": "module_to_stack",
                "physical_identity": module_info.get("barcode") if module_info else None,
                "evidence_level": "observation", "causality": "not_determined",
            })
            if diag.get("current_median_mv") is not None:
                self.attributes(
                    f"{base}_cell_median",
                    {
                        "label": "Modul-Zellmedian",
                        "unit": "mV",
                        "source": "Guardian-Berechnung aus Pylontech bat <module>",
                        "definition": "Median der 15 gleichzeitig erfassten Zellspannungen des Moduls.",
                        "interpretation": "Referenzlinie für die relative Zellkonsistenz. Einzelne Zellspannungen werden relativ zu diesem Median betrachtet.",
                        "method": "Median(V1…V15)",
                        "soh_hinweis": "Kein SOH-Wert und keine eigenständige Zellgesundheitsbewertung.",
                    },
                )
            for cell in diag.get("cells", []):
                cp=f"{base}_cell_{cell['cell']}"
                self.state(f"{cp}_status", cell.get("status")); self.state(f"{cp}_confidence", cell.get("confidence"))
                self.state(f"{cp}_voltage", cell.get("current_voltage_mv")); self.state(f"{cp}_deviation", cell.get("current_deviation_mv")); self.state(f"{cp}_evidence", cell.get("evidence_deviation_mv"))
                phases = cell.get("phases", {})
                self.state(f"{cp}_low_deviation", phases.get("low", {}).get("median_deviation_mv"))
                self.state(f"{cp}_discharge_deviation", phases.get("discharge", {}).get("median_deviation_mv"))
                self.state(f"{cp}_charge_deviation", phases.get("charge", {}).get("median_deviation_mv"))
                self.state(f"{cp}_high_deviation", phases.get("high", {}).get("median_deviation_mv"))
                self.state(f"{cp}_low_lowest", phases.get("low", {}).get("lowest_percent"))
                self.state(f"{cp}_discharge_lowest", phases.get("discharge", {}).get("lowest_percent"))
                self.state(f"{cp}_low_rank", phases.get("low", {}).get("mean_rank"))
                self.state(f"{cp}_discharge_rank", phases.get("discharge", {}).get("mean_rank"))
                self.state(f"{cp}_charge_rank", phases.get("charge", {}).get("mean_rank"))
                self.state(f"{cp}_high_rank", phases.get("high", {}).get("mean_rank"))
                self.state(f"{cp}_charge_highest", phases.get("charge", {}).get("highest_percent"))
                self.state(f"{cp}_high_highest", phases.get("high", {}).get("highest_percent"))
                for phase in ("low", "discharge", "charge", "high"):
                    self.state(f"{cp}_{phase}_samples", phases.get(phase, {}).get("samples"))

                # Bounded transport projection for Home Assistant's entity metadata.
                status_meta = self._diag_meta("status") | compact_cell_attributes(
                    cell, diag.get("advanced_diagnostics", {})
                )
                status_meta["physical_group"] = ((int(cell["cell"]) - 1) // 5) + 1
                status_meta["physical_group_cells"] = {1: "1-5", 2: "6-10", 3: "11-15"}[status_meta["physical_group"]]
                status_meta["evidence_phase"] = cell.get("evidence_phase")
                status_meta["evidence_deviation_mv"] = cell.get("evidence_deviation_mv")
                self.attributes(f"{cp}_status", status_meta)
                self.attributes(f"{cp}_confidence", self._diag_meta("confidence"))
                self.attributes(f"{cp}_voltage", self._diag_meta("voltage"))
                self.attributes(f"{cp}_deviation", self._diag_meta("deviation"))
                self.attributes(f"{cp}_evidence", self._diag_meta("evidence"))
                for phase in ("low", "discharge", "charge", "high"):
                    deviation_meta = self._diag_meta("deviation", phase)
                    phase_result = phases.get(phase, {})
                    deviation_meta["status"] = phase_result.get("status", "LERNPHASE")
                    deviation_meta["thresholds_mv"] = phase_result.get("thresholds_mv", {})
                    deviation_meta["samples"] = phase_result.get("samples", 0)
                    self.attributes(f"{cp}_{phase}_deviation", deviation_meta)
                    self.attributes(f"{cp}_{phase}_rank", self._diag_meta("rank", phase))
                    self.attributes(f"{cp}_{phase}_samples", self._diag_meta("samples", phase))
                self.attributes(f"{cp}_low_lowest", self._diag_meta("lowest", "low"))
                self.attributes(f"{cp}_discharge_lowest", self._diag_meta("lowest", "discharge"))
                self.attributes(f"{cp}_charge_highest", self._diag_meta("highest", "charge"))
                self.attributes(f"{cp}_high_highest", self._diag_meta("highest", "high"))


def update_events(status: str, alarms: list[dict], persistent: dict) -> None:
    current_codes = sorted(
        f"{alarm.get('module')}:{alarm.get('code')}" for alarm in alarms
    )
    previous_codes = sorted(persistent.get("last_alarm_codes", []))

    new_codes = sorted(set(current_codes) - set(previous_codes))
    cleared_codes = sorted(set(previous_codes) - set(current_codes))

    for alarm in alarms:
        key = f"{alarm.get('module')}:{alarm.get('code')}"
        if key in new_codes:
            module = alarm.get("module")
            if module is not None:
                module_key = str(module)
                persistent["alarm_counts"][module_key] = int(
                    persistent["alarm_counts"].get(module_key, 0)
                ) + 1
            append_event({
                "timestamp": time.time(),
                "type": "alarm_started",
                "status": status,
                "alarm": alarm,
            })

    for code in cleared_codes:
        append_event({
            "timestamp": time.time(),
            "type": "alarm_cleared",
            "code": code,
        })

    if persistent.get("last_status") != status:
        append_event({
            "timestamp": time.time(),
            "type": "status_changed",
            "from": persistent.get("last_status"),
            "to": status,
        })

    persistent["last_alarm_codes"] = current_codes
    persistent["last_status"] = status
    save_persistent_state(persistent)


def log_result(modules: list[Module], status: str, alarms: list[dict], detailed: bool) -> None:
    LOG.info("%d Module, Status %s, %d Alarm(e)", len(modules), status, len(alarms))

    if detailed:
        for module in modules:
            LOG.info(
                "M%d: %.3f V, %.3f A, SOC %.0f%%, Zustand %s, Zellen %.3f–%.3f V, Δ %d mV",
                module.module,
                module.voltage_v,
                module.current_a,
                module.soc_percent,
                module.state,
                module.min_cell_v,
                module.max_cell_v,
                module.delta_mv,
            )

    for alarm in alarms:
        level = logging.ERROR if alarm["level"] == "critical" else logging.WARNING
        LOG.log(level, "ALARM [%s] %s: %s", alarm["level"].upper(), alarm["code"], alarm["message"])


def main() -> None:
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    options = load_options()
    config_server = start_config_server()
    LOG.info("Guardian Konfigurationsmenü auf Port 8099 gestartet")
    config_history = ConfigHistory(CONFIG_HISTORY_FILE)
    try:
        config_record = config_history.record_if_changed(options)
        if config_record:
            LOG.info("Diagnostic configuration recorded: %s", config_record["config_id"])
    except Exception as exc:
        LOG.warning("Config History: %s", exc)
    persistent = load_persistent_state()
    port = find_port(options["serial_port"])
    console = PylontechConsole(
        port,
        int(options["baudrate"]),
        float(options["command_timeout_seconds"]),
    )
    rs485_writer = None
    rs485_pipeline = None
    if bool(options.get("rs485_sniffer_enabled", False)):
        rs485_writer = Rs485EvidenceWriter(DEFAULT_RS485_HISTORY_DIR)
        rs485_pipeline = Rs485EvidencePipeline(rs485_writer)
    rs485_reader = create_rs485_reader(options, port, rs485_pipeline)
    publisher = Mqtt(options)
    rs485_mqtt = Rs485MqttProjection(
        publisher, stale_seconds=int(options.get("rs485_sniffer_stale_seconds", 600)))
    publisher.discovery(int(options["module_count"]))
    configure_maintenance_live_publisher(publisher.maintenance_events)
    configure_rs485_status_provider(
        (lambda: {"status": {**rs485_reader.status(), "management_freshness_seconds": int(
                                  options.get("rs485_sniffer_stale_seconds", 600))},
                  "management": project_current_management(
                      rs485_reader.management(), rs485_reader.identities(),
                      position_history_path=DEFAULT_POSITION_HISTORY_FILE),
                  "identities": rs485_reader.identities(),
                  "history": rs485_writer.status() if rs485_writer else {}})
        if rs485_reader else None)
    cell_store = CellDiagnosticStore(CELL_DIAG_FILE, int(options["cell_diag_history_max_samples"]))
    try:
        current_backfill = CurrentConditionBackfill(
            CELL_HISTORY_DIR, DEFAULT_POSITION_HISTORY_FILE
        ).run(cell_store)
        LOG.info(
            "Current-Condition-Rebuild: discovered=%s scanned=%s skipped=%s "
            "incremental=%s valid=%s merged=%s duplicates=%s identity_unknown=%s "
            "invalid=%s errors=%s",
            current_backfill["files_discovered"], current_backfill["files_scanned"],
            current_backfill["files_skipped"], current_backfill["incremental_files"],
            current_backfill["valid_samples"], current_backfill["samples_merged"],
            current_backfill["duplicates"], current_backfill["identity_unknown"],
            current_backfill["invalid_lines"], current_backfill["file_errors"],
        )
    except Exception as exc:
        LOG.warning("Current-Condition-Rebuild fehlgeschlagen: %s", exc)
    aggregate_store = DiagnosticAggregateStore(
        DIAGNOSTIC_AGGREGATE_FILE,
        CellDiagnosticStore.phases,
        int(options.get("cell_diag_aggregate_retention_days", 730)),
    )
    try:
        backfill = DiagnosticAggregateBackfill(
            CELL_HISTORY_DIR, DEFAULT_POSITION_HISTORY_FILE
        ).run(aggregate_store, options)
        LOG.info(
            "Diagnostic aggregate backfill: discovered=%s scanned=%s skipped=%s "
            "valid=%s aggregated=%s identity_unknown=%s invalid=%s errors=%s",
            backfill["files_discovered"], backfill["files_scanned"],
            backfill["files_skipped"], backfill["valid_samples"],
            backfill["aggregated_samples"], backfill["identity_unknown"],
            backfill["invalid_lines"], backfill["file_errors"],
        )
    except Exception as exc:
        LOG.warning("Diagnostic aggregate backfill fehlgeschlagen: %s", exc)
    maintenance_log = MaintenanceEventLog(DEFAULT_MAINTENANCE_EVENT_FILE)
    maintenance_events = ()
    maintenance_mtime_ns = None
    cell_history = CellHistoryWriter(CELL_HISTORY_DIR)
    last_cell_poll = 0.0
    last_stat_poll = 0.0
    last_info_poll = 0.0
    bms_stat = {}
    module_infos: dict[int, dict] = {}
    identity_resolution_log: dict[int, tuple[str, int | None]] = {}
    daily_worker = None
    hycube_collector = None

    if rs485_reader is not None:
        restored_identities = restore_latest_identities(DEFAULT_RS485_HISTORY_DIR)
        rs485_reader.restore_identities(restored_identities)
        rs485_writer.start()
        rs485_reader.start()
    try:
        daily_worker = DailyDiagnosticWorker(
            DailyDiagnosticSources(
                cell_history_root=CELL_HISTORY_DIR,
                rs485_history_root=DEFAULT_RS485_HISTORY_DIR,
                position_history_path=DEFAULT_POSITION_HISTORY_FILE,
                config_history_path=CONFIG_HISTORY_FILE,
                maintenance_history_path=DEFAULT_MAINTENANCE_EVENT_FILE,
            ),
            DAILY_DIAGNOSTICS_ROOT,
        )
        daily_worker.start()
    except Exception as exc:
        daily_worker = None
        LOG.warning("Daily diagnostics worker konnte nicht gestartet werden: %s", exc)
    try:
        hycube_collector = collector_from_options(
            options, HYCUBE_HISTORY_DIR, HYCUBE_POLICY_HISTORY_DIR)
        if hycube_collector is not None:
            hycube_collector.start()
    except Exception as exc:
        hycube_collector = None
        LOG.warning("Hycube read-only collector konnte nicht gestartet werden: %s",
                    type(exc).__name__)
    try:
        while RUNNING:
            started = time.monotonic()
            modules: list[Module] = []
            try:
                raw = console.command(options["command"])
                pwr_sample_at = time.time()

                if options["raw_log"]:
                    (SHARE_DIR / "last_raw_pwr.txt").write_text(raw, encoding="utf-8")

                modules = parse_pwr(raw, int(options["module_count"]))
                for module in modules:
                    module.pwr_sample_at = pwr_sample_at
                if not modules:
                    (SHARE_DIR / "last_unparsed_pwr.txt").write_text(raw, encoding="utf-8")
                    status = "critical"
                    alarms = [{
                        "level": "critical",
                        "code": "communication_or_parser_error",
                        "module": None,
                        "message": "Keine Moduldaten aus der Pylontech-Antwort erkannt.",
                    }]
                else:
                    status, alarms = evaluate(modules, options)

                update_events(status, alarms, persistent)
                trends = update_trends(modules, options)
                incident = update_incident_state(status, alarms, options)
                log_result(modules, status, alarms, bool(options["detailed_log"]))
                now_wall=time.time(); cell_results={}
                if modules and now_wall-last_stat_poll >= int(options["bms_stat_interval_seconds"]):
                    try:
                        parsed=parse_stat(console.command("stat"))
                        if parsed: bms_stat=parsed
                    except Exception as exc: LOG.warning("BMS stat: %s",exc)
                    last_stat_poll=now_wall

                # Hersteller-/Identitätsdaten ändern sich praktisch nicht. Deshalb
                # werden sie mit dem bereits vorhandenen langsamen BMS-Stat-Intervall
                # abgefragt und im Speicher zwischengespeichert.
                if modules and now_wall-last_info_poll >= int(options["bms_stat_interval_seconds"]):
                    for module in modules:
                        try:
                            parsed_info = parse_info(console.command(f"info {module.module}"))
                            if parsed_info:
                                module_infos[module.module] = parsed_info
                        except Exception as exc:
                            LOG.warning("BMS info Modul %s: %s", module.module, exc)
                    last_info_poll = now_wall

                update_observed_stack(
                    module_infos, present_positions={module.module for module in modules},
                    communication_healthy=bool(modules),
                    expected_module_count=int(options["module_count"]), observed_at=now_wall,
                    confirm_history=False,
                )
                resolved_identities = {}
                if rs485_reader is not None:
                    for adr, identity in rs485_reader.identities().items():
                        try:
                            resolved = resolve_rs485_identity(
                                int(adr), identity["serial_string"], identity["serial_raw"],
                                datetime.fromtimestamp(now_wall, timezone.utc),
                                decode_source=identity.get("decode_source", "stored_decoded"),
                                position_history_path=DEFAULT_POSITION_HISTORY_FILE,
                            )
                        except (KeyError, TypeError, ValueError, OSError) as exc:
                            LOG.warning("RS485 identity projection skipped: adr=%02X error=%s",
                                        int(adr), exc)
                            continue
                        log_key = (identity["serial_string"], resolved.get("position"))
                        if identity_resolution_log.get(int(adr)) != log_key:
                            if resolved.get("identity_resolved"):
                                LOG.info("RS485 identity resolved: adr=%02X serial=%s position=%s",
                                         int(adr), identity["serial_string"], resolved["position"])
                            else:
                                LOG.warning("RS485 identity unresolved: adr=%02X serial=%s reason=no_current_position",
                                            int(adr), identity["serial_string"])
                            identity_resolution_log[int(adr)] = log_key
                        if (resolved.get("identity_resolved")
                                and identity.get("identity_currently_confirmed") is True):
                            # Preserve the direct frame timestamp. The resolver's
                            # datetime is the position-resolution time, not a new
                            # RS485 observation.
                            resolved_identities[int(adr)] = {**resolved, **identity}
                    update_rs485_observations(
                        resolved_identities, observed_at=now_wall, confirm_history=False)

                # Identity is resolved before a new raw sample is persisted, so
                # the first sample after a confirmed swap cannot inherit the
                # physical serial previously installed at this position.
                if options["cell_diagnostics_enabled"] and modules and now_wall-last_cell_poll >= int(options["cell_diagnostics_interval_seconds"]):
                    for module in modules:
                        try:
                            rows=parse_bat(console.command(f"bat {module.module}"))
                            if rows:
                                sample_time = time.time()
                                try:
                                    serial, position_history_id = documented_identity_at(
                                        DEFAULT_POSITION_HISTORY_FILE, module.module,
                                        datetime.fromtimestamp(sample_time, timezone.utc),
                                    )
                                except Exception as identity_exc:
                                    LOG.warning("Physische Identität Modul %s unklar: %s", module.module, identity_exc)
                                    serial, position_history_id = None, None
                                sample = CellSample(
                                    sample_time, module.module,
                                    [r['voltage_mv'] for r in rows], module.current_a,
                                    module.soc_percent, [r['temperature_c'] for r in rows],
                                    [r['balancing'] for r in rows], serial, position_history_id,
                                )
                                cell_store.add(sample)
                                aggregate_store.add(sample, options)
                                try:
                                    cell_history.append({**asdict(sample), "module_serial": serial,
                                                         "position_history_id": position_history_id,
                                                         "identity_source": "position_history" if serial else "unknown",
                                                         **cell_history_timing(
                                                             sample_time,
                                                             module.pwr_sample_at)})
                                except Exception as history_exc:
                                    LOG.warning("Cell History Modul %s: %s", module.module, history_exc)
                        except Exception as exc: LOG.warning("Zelldiagnostik Modul %s: %s",module.module,exc)
                    last_cell_poll=now_wall; cell_store.save(); aggregate_store.save()

                try:
                    current_mtime_ns = maintenance_log.path.stat().st_mtime_ns if maintenance_log.path.exists() else None
                    if current_mtime_ns != maintenance_mtime_ns:
                        maintenance_events = resolve_maintenance_event_identities(
                            maintenance_log.read_all(), DEFAULT_POSITION_HISTORY_FILE
                        )
                        maintenance_mtime_ns = current_mtime_ns
                except Exception as exc:
                    LOG.warning("Maintenance-Kontext nicht lesbar: %s", exc)
                    maintenance_events = ()
                for module in modules:
                    latest_serial = cell_store.current_serial(module.module)
                    analysis = cell_store.analyse(
                        module.module, options, maintenance_events,
                        aggregate_store.for_identity(module.module, latest_serial),
                    )
                    cell_results[module.module] = {
                        **analysis, "physical_module_serial": latest_serial
                    }
                publisher.publish(
                    modules, status, alarms, options, persistent, trends, incident,
                    cell_results, bms_stat, module_infos,
                    current_presence(expected_module_count=int(options["module_count"])),
                )
                if rs485_reader is not None:
                    rs485_management = project_current_management(
                        rs485_reader.management(), rs485_reader.identities(),
                        position_history_path=DEFAULT_POSITION_HISTORY_FILE)
                    rs485_mqtt.publish(rs485_reader.status(),
                                       rs485_management,
                                       rs485_writer.status())

                # Durable topology confirmation happens only after the complete
                # poll and every projection/publish step succeeded. Live presence
                # above remains immediate and independent.
                update_observed_stack(
                    module_infos, present_positions={module.module for module in modules},
                    communication_healthy=bool(modules),
                    expected_module_count=int(options["module_count"]),
                    observed_at=now_wall, confirm_history=True,
                )
                if resolved_identities:
                    update_rs485_observations(
                        resolved_identities, observed_at=now_wall, confirm_history=True)
                if history_observation_ready():
                    try:
                        record_stable_observed_positions()
                    except Exception as exc:
                        LOG.warning("Positionshistorie konnte nicht fortgeschrieben werden: %s", exc)

            except Exception as exc:
                LOG.exception("Abfrage fehlgeschlagen: %s", exc)
                update_observed_stack(
                    {}, present_positions=set(), communication_healthy=False,
                    expected_module_count=int(options["module_count"]),
                    confirm_history=False,
                )
                try:
                    console.close()
                except Exception:
                    pass

            elapsed = time.monotonic() - started
            time.sleep(max(1, int(options["poll_interval_seconds"]) - elapsed))
    finally:
        if hycube_collector is not None:
            try:
                hycube_collector.stop()
            except Exception as exc:
                LOG.warning("Hycube read-only collector konnte nicht gestoppt werden: %s", exc)
        if daily_worker is not None:
            try:
                daily_worker.stop()
            except Exception as exc:
                LOG.warning("Daily diagnostics worker konnte nicht gestoppt werden: %s", exc)
        if rs485_reader is not None:
            rs485_reader.stop()
        if rs485_writer is not None:
            rs485_writer.stop()
        console.close()
        publisher.close()


if __name__ == "__main__":
    main()
