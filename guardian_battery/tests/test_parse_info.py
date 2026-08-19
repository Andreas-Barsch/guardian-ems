"""Regression tests for the Pylontech ``info <module>`` parser.

The production module performs Home Assistant runtime initialization during
import.  Extracting these two definitions keeps the parser test independent
from ``pyserial`` and ``/share/guardian_battery``, as required by the runbook.
"""

import ast
from pathlib import Path


def _load_parse_info():
    source_path = Path(__file__).resolve().parents[1] / "app" / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "INFO_FIELDS" for target in node.targets)
        )
        or (isinstance(node, ast.FunctionDef) and node.name == "parse_info")
    ]
    namespace = {}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["parse_info"]


parse_info = _load_parse_info()


REAL_US2000C_INFO = """
Device address      : 1
Manufacturer        : Pylon
Device name         : US2000C
Board version       : V10R04
Board               : NF4.E2
Main Soft version   : B69.18.0.0
Soft version        : V2.8
Boot version        : V1.3
Comm version        : V2.0
Release Date        : 23-06-02
Barcode             : Y225004C32250185
Specification       : 48V/50Ah
Cell Number         : 15
Max Dischg Curr     : 50000
Max Charge Curr     : 50000
EPPNPort rate       : 115200
Console Port rate   : 115200
"""


def test_parse_info_reads_verified_us2000c_identity_fields():
    parsed = parse_info(REAL_US2000C_INFO)

    assert parsed["manufacturer"] == "Pylon"
    assert parsed["device_name"] == "US2000C"
    assert parsed["barcode"] == "Y225004C32250185"
    assert parsed["board_version"] == "V10R04"
    assert parsed["main_soft_version"] == "B69.18.0.0"
    assert parsed["comm_version"] == "V2.0"
    assert parsed["release_date"] == "23-06-02"
    assert parsed["specification"] == "48V/50Ah"


def test_parse_info_converts_numeric_fields_to_integers():
    parsed = parse_info(REAL_US2000C_INFO)

    assert parsed["device_address"] == 1
    assert parsed["cell_number"] == 15
    assert parsed["max_discharge_current_ma"] == 50000
    assert parsed["max_charge_current_ma"] == 50000
    assert parsed["eppn_port_rate"] == 115200
    assert parsed["console_port_rate"] == 115200


def test_parse_info_preserves_non_numeric_fallback_for_numeric_fields():
    parsed = parse_info("Device address: N/A\nCell Number: unknown")

    assert parsed == {"device_address": "N/A", "cell_number": "unknown"}


def test_parse_info_ignores_unknown_empty_and_malformed_lines():
    parsed = parse_info(
        "Console banner without delimiter\n"
        "Unknown Field: ignored\n"
        "Manufacturer:   \n"
        "Device name: US2000C\n"
    )

    assert parsed == {"device_name": "US2000C"}


def test_parse_info_splits_only_the_first_colon_and_trims_whitespace():
    parsed = parse_info("  Barcode : SERIES:WITH:COLONS  \n Board : NF4.E2 ")

    assert parsed == {"barcode": "SERIES:WITH:COLONS", "board": "NF4.E2"}
