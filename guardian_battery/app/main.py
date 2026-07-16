from __future__ import annotations

import glob
import json
import logging
import os
import re
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

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

    @property
    def delta_mv(self) -> int:
        return round((self.max_cell_v - self.min_cell_v) * 1000)


def stop(*_args):
    global RUNNING
    RUNNING = False


def load_options() -> dict:
    return json.loads(OPTIONS_FILE.read_text(encoding="utf-8"))


def find_port(configured: str) -> str:
    if configured and configured.lower() != "auto":
        return configured

    candidates = sorted(glob.glob("/dev/serial/by-id/*"))
    if candidates:
        return candidates[0]

    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if candidates:
        return candidates[0]

    raise RuntimeError("Kein serieller Adapter gefunden.")


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
        if module_no > expected_modules:
            idx += 1
            continue

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


def module_health(module: Module, options: dict) -> tuple[str, str]:
    if module.state.lower() == "syserror":
        return "critical", "SysError"

    abnormal = []
    for field, value in (
        ("Spannung", module.voltage_state),
        ("Strom", module.current_state),
        ("Temperatur", module.temperature_state),
        ("Batteriespannung", module.battery_voltage_state),
        ("Batterietemperatur", module.battery_temperature_state),
        ("MOS-Temperatur", module.mos_temperature_state),
    ):
        if value and value.lower() not in {"normal", "-", "unknown"}:
            abnormal.append(f"{field}: {value}")

    if module.delta_mv >= int(options["critical_cell_delta_mv"]):
        return "critical", f"Zellspreizung {module.delta_mv} mV"
    if abnormal or module.delta_mv >= int(options["warning_cell_delta_mv"]):
        reason = "; ".join(abnormal) if abnormal else f"Zellspreizung {module.delta_mv} mV"
        return "warning", reason
    return "ok", "Normal"


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
        self.client.publish(f"{self.prefix}/battery/availability", "online", retain=True)

    def close(self) -> None:
        self.client.publish(f"{self.prefix}/battery/availability", "offline", retain=True)
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
            "sw_version": "0.2.0",
        }

        sensors = [
            ("stack_status", "Guardian Batteriestatus", None, None, "mdi:shield-battery"),
            ("modules_present", "Guardian erkannte Module", None, None, "mdi:battery-multiple"),
            ("active_alarms", "Guardian aktive Batteriealarme", None, None, "mdi:alert-circle"),
            ("last_alarm", "Guardian letzter Batteriealarm", None, None, "mdi:message-alert"),
            ("last_update", "Guardian letzte Aktualisierung", None, "timestamp", "mdi:clock-check"),
        ]

        for module in range(1, module_count + 1):
            sensors.extend([
                (f"module_{module}_health", f"Modul {module} Gesundheit", None, None, "mdi:shield-check"),
                (f"module_{module}_health_reason", f"Modul {module} Bewertung", None, None, "mdi:text-box-check"),
                (f"module_{module}_voltage", f"Modul {module} Spannung", "V", "voltage", "mdi:sine-wave"),
                (f"module_{module}_current", f"Modul {module} Strom", "A", "current", "mdi:current-dc"),
                (f"module_{module}_temperature", f"Modul {module} Temperatur", "°C", "temperature", "mdi:thermometer"),
                (f"module_{module}_soc", f"Modul {module} SOC", "%", "battery", "mdi:battery"),
                (f"module_{module}_state", f"Modul {module} Zustand", None, None, "mdi:battery-sync"),
                (f"module_{module}_min_cell", f"Modul {module} minimale Zellspannung", "V", "voltage", "mdi:arrow-down-bold"),
                (f"module_{module}_max_cell", f"Modul {module} maximale Zellspannung", "V", "voltage", "mdi:arrow-up-bold"),
                (f"module_{module}_cell_delta", f"Modul {module} Zellspreizung", "mV", None, "mdi:delta"),
                (f"module_{module}_mos_temperature", f"Modul {module} MOS-Temperatur", "°C", "temperature", "mdi:thermometer-lines"),
            ])

        for object_id, name, unit, device_class, icon in sensors:
            payload = {
                "name": name,
                "unique_id": f"guardian_battery_{object_id}",
                "state_topic": f"{self.prefix}/battery/sensor/{object_id}/state",
                "availability_topic": f"{self.prefix}/battery/availability",
                "device": device,
                "icon": icon,
            }
            if unit:
                payload["unit_of_measurement"] = unit
            if device_class:
                payload["device_class"] = device_class
            if device_class in {"voltage", "current", "temperature", "battery"} or object_id.endswith("_cell_delta"):
                payload["state_class"] = "measurement"

            self.client.publish(
                f"homeassistant/sensor/guardian_battery/{object_id}/config",
                json.dumps(payload),
                retain=True,
            )

    def state(self, name: str, value) -> None:
        if value is None:
            return
        if isinstance(value, float):
            value = f"{value:.4f}".rstrip("0").rstrip(".")
        self.client.publish(
            f"{self.prefix}/battery/sensor/{name}/state",
            str(value),
            retain=True,
        )

    def publish(self, modules: list[Module], status: str, alarms: list[dict], options: dict) -> None:
        self.state("stack_status", status)
        self.state("modules_present", len(modules))
        self.state("active_alarms", len(alarms))
        self.state("last_alarm", alarms[0]["message"] if alarms else "kein Alarm")
        self.state("last_update", time.strftime("%Y-%m-%dT%H:%M:%S%z"))

        payload_modules = []
        for module in modules:
            health, health_reason = module_health(module, options)
            payload_modules.append(
                asdict(module) | {
                    "delta_mv": module.delta_mv,
                    "health": health,
                    "health_reason": health_reason,
                }
            )

        payload = {
            "timestamp": time.time(),
            "status": status,
            "alarms": alarms,
            "modules": payload_modules,
        }
        self.client.publish(f"{self.prefix}/battery/state", json.dumps(payload, ensure_ascii=False), retain=True)
        self.client.publish(f"{self.prefix}/battery/alarms", json.dumps(alarms, ensure_ascii=False), retain=True)

        for module in modules:
            health, health_reason = module_health(module, options)
            base = f"module_{module.module}"
            values = {
                f"{base}_health": health,
                f"{base}_health_reason": health_reason,
                f"{base}_voltage": module.voltage_v,
                f"{base}_current": module.current_a,
                f"{base}_temperature": module.temperature_c,
                f"{base}_soc": module.soc_percent,
                f"{base}_state": module.state,
                f"{base}_min_cell": module.min_cell_v,
                f"{base}_max_cell": module.max_cell_v,
                f"{base}_cell_delta": module.delta_mv,
                f"{base}_mos_temperature": module.mos_temperature_c,
            }
            for key, value in values.items():
                self.state(key, value)


def evaluate(modules: list[Module], options: dict) -> tuple[str, list[dict]]:
    alarms: list[dict] = []
    present = {module.module for module in modules}
    expected = set(range(1, int(options["module_count"]) + 1))

    for missing in sorted(expected - present):
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

        for field, value in (
            ("voltage_state", module.voltage_state),
            ("current_state", module.current_state),
            ("temperature_state", module.temperature_state),
            ("battery_voltage_state", module.battery_voltage_state),
            ("battery_temperature_state", module.battery_temperature_state),
            ("mos_temperature_state", module.mos_temperature_state),
        ):
            if value and value.lower() not in {"normal", "-", "unknown"}:
                alarms.append({
                    "level": "warning",
                    "code": "abnormal_state",
                    "module": module.module,
                    "message": f"Modul {module.module}: {field} = {value}.",
                })

    if any(alarm["level"] == "critical" for alarm in alarms):
        return "critical", alarms
    if alarms:
        return "warning", alarms
    return "ok", alarms


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
    port = find_port(options["serial_port"])
    console = PylontechConsole(
        port,
        int(options["baudrate"]),
        float(options["command_timeout_seconds"]),
    )
    publisher = Mqtt(options)
    publisher.discovery(int(options["module_count"]))

    try:
        while RUNNING:
            started = time.monotonic()
            modules: list[Module] = []
            try:
                raw = console.command(options["command"])

                if options["raw_log"]:
                    (SHARE_DIR / "last_raw_pwr.txt").write_text(raw, encoding="utf-8")

                modules = parse_pwr(raw, int(options["module_count"]))
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

                log_result(modules, status, alarms, bool(options["detailed_log"]))
                publisher.publish(modules, status, alarms, options)

            except Exception as exc:
                LOG.exception("Abfrage fehlgeschlagen: %s", exc)
                try:
                    console.close()
                except Exception:
                    pass

            elapsed = time.monotonic() - started
            time.sleep(max(1, int(options["poll_interval_seconds"]) - elapsed))
    finally:
        console.close()
        publisher.close()


if __name__ == "__main__":
    main()
