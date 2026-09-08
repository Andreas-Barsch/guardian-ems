from types import SimpleNamespace

from cell_acquisition import acquire_cell_round
from collector_timing import CollectorTiming


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def module(number):
    return SimpleNamespace(module=number, current_a=-1.0, soc_percent=50.0,
                           pwr_sample_at=1000.0)


def rows(_raw):
    return [{"voltage_mv": 3300, "temperature_c": 25.0,
             "balancing": False}] * 15


def test_bat_timeout_keeps_successful_raw_samples_and_command_order():
    clock = Clock()

    class Console:
        def __init__(self):
            self.commands = []

        def command(self, command):
            self.commands.append(command)
            clock.value += 5 if command == "bat 2" else 0.5
            if command == "bat 2":
                raise TimeoutError("synthetic BAT timeout")
            return command

    class Resolver:
        def identity_at(self, position, _timestamp):
            return f"SERIAL-{position}", f"PHS-{position}"

    class History:
        def __init__(self):
            self.records = []

        def append(self, record):
            self.records.append(record)

    console = Console()
    history = History()
    timing = CollectorTiming(10, 60)
    samples = acquire_cell_round(
        [module(1), module(2), module(3)], console, Resolver(), history,
        parse_bat_fn=rows, wall_clock=lambda: 1010 + clock.value,
        monotonic=clock, timing=timing)

    assert console.commands == ["bat 1", "bat 2", "bat 3"]
    assert [sample.module for sample in samples] == [1, 3]
    assert [record["module"] for record in history.records] == [1, 3]
    assert [record["module_serial"] for record in history.records] == [
        "SERIAL-1", "SERIAL-3"]
    state = timing.snapshot()
    assert state["rolling"]["bat_request"]["count"] == 3
    assert state["current_cycle"]["bat_requests_total_duration_seconds"] == 6.0


def test_each_sequential_sample_keeps_its_own_time_and_pwr_age():
    clock = Clock()

    class Console:
        def command(self, _command):
            clock.value += 0.5
            return "ok"

    class Resolver:
        def identity_at(self, position, _timestamp):
            return f"SERIAL-{position}", None

    class History:
        def __init__(self):
            self.records = []

        def append(self, record):
            self.records.append(record)

    history = History()
    acquire_cell_round(
        [module(1), module(2)], Console(), Resolver(), history,
        parse_bat_fn=rows, wall_clock=lambda: 1000 + clock.value,
        monotonic=clock)

    assert [record["cell_sample_at"] for record in history.records] == [1000.5, 1001.0]
    assert [record["pwr_age_seconds"] for record in history.records] == [0.5, 1.0]
