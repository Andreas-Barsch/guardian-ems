"""Exact read-only MCP tool catalogue backed only by Guardian Research API."""
from __future__ import annotations

import time
import re
from urllib.parse import quote
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from errors import GatewayError
from gateway import QueryGate, ServiceState, audit, request_id
from guardian_client import GuardianResearchClient

Resolution = Literal["auto", "full", "display"]
Source = Literal["guardian.cell_history", "guardian.hycube", "guardian.display_history", "guardian.canonical_phase"]
Metric = Literal["soc", "module_voltage", "module_current", "module_temperature",
                 "cell_voltage", "cell_temperature", "cell_deviation", "cell_spread",
                 "battery_capacity", "policy"]
CellMetric = Literal["soc", "module_voltage", "module_current", "module_temperature",
                     "cell_voltage", "cell_temperature", "cell_deviation", "cell_spread"]
Dataset = Literal["soc", "module_voltage", "module_current", "cell_voltage",
                  "cell_temperature", "hycube", "policy", "maintenance", "rs485",
                  "soh", "cycles", "ah_coulomb", "daily_diagnostics"]

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def register_tools(server: MCPServer, client: GuardianResearchClient,
                   gate: QueryGate, state: ServiceState) -> None:
    async def invoke(tool: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        call_id = request_id()
        started = time.monotonic()
        payload: dict | None = None
        byte_count = 0
        status = "failed"
        try:
            async with gate.slot(
                full_resolution=params.get("resolution") == "full",
                timeout=client.settings.guardian_timeout_seconds,
            ):
                payload, byte_count = await client.get(endpoint, params)
            state.guardian_reachable = True
            state.last_error = None
            status = "ok"
            return payload
        except GatewayError as exc:
            if exc.code == "source_unavailable":
                state.guardian_reachable = False
            state.last_error = exc.code
            status = exc.code
            return exc.as_tool_result()
        finally:
            audit(request_id=call_id, tool=tool, client_id=None, params=params,
                  duration=time.monotonic() - started, payload=payload,
                  byte_count=byte_count, status=status)

    @server.tool(annotations=READ_ONLY)
    async def guardian_status() -> dict[str, Any]:
        """Return Guardian Research API status; evidence is not interpreted."""
        return await invoke("guardian_status", "status", {})

    @server.tool(annotations=READ_ONLY)
    async def get_stack_topology_at(timestamp: str | None = None) -> dict[str, Any]:
        """Return observed topology at a time; position is time-dependent."""
        return await invoke("get_stack_topology_at", "topology", {"timestamp": timestamp})

    @server.tool(annotations=READ_ONLY)
    async def get_identity_epochs(physical_serial: str | None = None,
                                  timestamp_from: str | None = None,
                                  timestamp_to: str | None = None) -> dict[str, Any]:
        """Return derived identity epochs; physical_serial is primary identity."""
        return await invoke("get_identity_epochs", "identity-epochs", {
            "physical_serial": physical_serial, "from": timestamp_from, "to": timestamp_to})

    @server.tool(annotations=READ_ONLY)
    async def get_maintenance_events(physical_serial: str | None,
                                     timestamp_from: str, timestamp_to: str,
                                     category: str | None = None,
                                     action: str | None = None,
                                     max_records: int = 1000,
                                     cursor: str | None = None) -> dict[str, Any]:
        """Return observed maintenance evidence without causal claims."""
        return await invoke("get_maintenance_events", "maintenance", {
            "physical_serial": physical_serial, "from": timestamp_from, "to": timestamp_to,
            "category": category, "action": action, "max_records": max_records, "cursor": cursor})

    @server.tool(annotations=READ_ONLY)
    async def get_data_coverage(physical_serial: str, datasets: list[Dataset],
                                timestamp_from: str, timestamp_to: str) -> dict[str, Any]:
        """Return observed coverage including uncovered request edges; absent is never zero.
        Cell-history units: SOC %, module voltage V, current A, cell voltage mV, temperature degC."""
        return await invoke("get_data_coverage", "coverage", {
            "physical_serial": physical_serial, "datasets": datasets,
            "from": timestamp_from, "to": timestamp_to})

    @server.tool(annotations=READ_ONLY)
    async def query_module_history(physical_serial: str, metric: Metric,
                                   timestamp_from: str, timestamp_to: str,
                                   resolution: Resolution = "auto", max_points: int = 6000,
                                   cursor: str | None = None,
                                   source: Source = "guardian.cell_history") -> dict[str, Any]:
        """Return module evidence: SOC %, voltage V, current A, temperature degC; no causality.
        Source/metric combinations are validated by Battery; auto selects full up to 24h."""
        return await invoke("query_module_history", "module-history", {
            "physical_serial": physical_serial, "metric": metric,
            "from": timestamp_from, "to": timestamp_to, "resolution": resolution,
            "max_points": max_points, "cursor": cursor, "source": source})

    @server.tool(annotations=READ_ONLY)
    async def query_cell_history(physical_serial: str, metric: CellMetric,
                                 cell_numbers: list[int], timestamp_from: str,
                                 timestamp_to: str, resolution: Resolution = "auto",
                                 max_points: int = 6000,
                                 cursor: str | None = None) -> dict[str, Any]:
        """Return existing cell-history metrics, including supported module metrics.
        Units: SOC %, module voltage V, current A, temperature degC, cell voltage/deviation/spread mV.
        Coverage describes source observations, independent of pagination/downsampling."""
        return await invoke("query_cell_history", "cell-history", {
            "physical_serial": physical_serial, "metric": metric,
            "cell_numbers": cell_numbers, "from": timestamp_from, "to": timestamp_to,
            "resolution": resolution, "max_points": max_points, "cursor": cursor})

    @server.tool(annotations=READ_ONLY)
    async def query_phase_history(physical_serial: str, timestamp_from: str,
                                  timestamp_to: str) -> dict[str, Any]:
        """Return deterministic canonical phase intervals without adding interpretation."""
        return await invoke("query_phase_history", "phases", {
            "physical_serial": physical_serial, "from": timestamp_from, "to": timestamp_to})

    @server.tool(annotations=READ_ONLY)
    async def query_daily_diagnostics(date: str | None = None,
                                      physical_serial: str | None = None,
                                      timestamp_from: str | None = None,
                                      timestamp_to: str | None = None,
                                      component: str | None = None) -> dict[str, Any]:
        """Return existing derived daily diagnostics; no new diagnosis is computed."""
        return await invoke("query_daily_diagnostics", "daily-diagnostics", {
            "date": date, "physical_serial": physical_serial,
            "from": timestamp_from, "to": timestamp_to, "component": component})

    @server.tool(annotations=READ_ONLY)
    async def query_diagnostic_evidence(date: str | None = None,
                                        physical_serial: str | None = None,
                                        timestamp_from: str | None = None,
                                        timestamp_to: str | None = None,
                                        component: str | None = None) -> dict[str, Any]:
        """Return existing diagnostic evidence with its original provenance and class."""
        return await invoke("query_diagnostic_evidence", "diagnostic-evidence", {
            "date": date, "physical_serial": physical_serial,
            "from": timestamp_from, "to": timestamp_to, "component": component})

    @server.tool(annotations=READ_ONLY)
    async def list_soc_crash_events(timestamp_from: str, timestamp_to: str,
                                    max_records: int = 100) -> dict[str, Any]:
        """Read persisted SOC events in at most 31 days, 1..500 records; no replay or causality."""
        return await invoke("list_soc_crash_events", "soc-crash-events", {
            "from": timestamp_from, "to": timestamp_to, "max_records": max_records})

    @server.tool(annotations=READ_ONLY)
    async def find_low_voltage_events(physical_serial: str, timestamp_from: str,
                                      timestamp_to: str, resolution: Resolution = "auto",
                                      max_points: int = 6000) -> dict[str, Any]:
        """Return observed low-voltage evidence; missing evidence is not a zero value."""
        return await invoke("find_low_voltage_events", "events/low-voltage", {
            "physical_serial": physical_serial, "from": timestamp_from, "to": timestamp_to,
            "resolution": resolution, "max_points": max_points})

    @server.tool(annotations=READ_ONLY)
    async def query_alarm_history(timestamp_from: str, timestamp_to: str,
                                  physical_serial: str | None = None,
                                  severity: str | None = None,
                                  alarm_type: str | None = None) -> dict[str, Any]:
        """Return observed Guardian alarm history without inferring absent alarms."""
        return await invoke("query_alarm_history", "alarms", {
            "physical_serial": physical_serial, "from": timestamp_from, "to": timestamp_to,
            "severity": severity, "type": alarm_type})

    @server.tool(annotations=READ_ONLY)
    async def query_timeseries(source: Source, metric: Metric, physical_serial: str,
                               timestamp_from: str, timestamp_to: str,
                               resolution: Resolution = "auto", max_points: int = 6000,
                               cell_numbers: list[int] | None = None,
                               cursor: str | None = None) -> dict[str, Any]:
        """Return a registered Research timeseries unchanged; position remains time-dependent."""
        return await invoke("query_timeseries", "timeseries", {
            "source": source, "metric": metric, "physical_serial": physical_serial,
            "from": timestamp_from, "to": timestamp_to, "resolution": resolution,
            "max_points": max_points, "cell_numbers": cell_numbers, "cursor": cursor})

    @server.tool(annotations=READ_ONLY)
    async def get_soc_crash_event(event_id: str) -> dict[str, Any]:
        """Read exactly one persisted SOC event; never build evidence or follow up automatically."""
        if re.fullmatch(r"SCS-[0-9a-f]{64}", event_id) is None:
            raise GatewayError("invalid_argument", "invalid SOC event ID").as_tool_error()
        return await invoke("get_soc_crash_event", "soc-crash-events/" + quote(event_id, safe=""), {})
