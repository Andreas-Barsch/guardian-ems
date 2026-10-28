"""Bounded GET-only readers. No generic URL request or service-call interface.

Even though Hycube uses GET for some writes, no Hycube HTTP route is allowed.
Home Assistant access is limited to individual sensor/binary_sensor states.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from pathlib import Path
from .model import InputError, Policy, timestamp, iso, number

ENTITY = re.compile(r"(?:sensor|binary_sensor)\.[a-z0-9_]+\Z")


class SourceError(RuntimeError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError("HTTP-Weiterleitung ist im Nur-Lese-Client gesperrt")


class ReadClient:
    def __init__(self, base_url, role, token="", timeout=12, opener=None):
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username
                or parts.password or parts.query or parts.fragment
                or parts.path.rstrip("/") not in (("", "/core") if role == "ha" else ("",))):
            raise InputError("Ungültige Quelladresse; keine Zugangsdaten in URLs zulässig")
        if role not in ("guardian", "ha"):
            raise InputError("Unbekannte Nur-Lese-Quelle")
        self.base = base_url.rstrip("/")
        self.role, self.token, self.timeout = role, token, timeout
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def read(self, route, params=None):
        allowed = (route in ("/api/research/status", "/api/research/topology", "/api/research/timeseries")
                   if self.role == "guardian" else
                   route.startswith("/api/states/") and bool(ENTITY.fullmatch(route.removeprefix("/api/states/"))))
        if not allowed or "?" in route or "#" in route:
            raise SourceError("Nicht freigegebener Lesepfad")
        url = self.base+route+("?"+urlencode(params) if params else "")
        headers = {"Accept": "application/json", "Connection": "close"}
        if self.token:
            headers["Authorization"] = "Bearer "+self.token
        request = Request(url, headers=headers, method="GET")
        for attempt in range(2):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    if response.status != 200:
                        raise SourceError("Quelle lieferte keinen erfolgreichen Lesestatus")
                    data = response.read(2*1024*1024+1)
                    if len(data) > 2*1024*1024:
                        raise SourceError("Antwort überschreitet 2 MiB")
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError) as exc:
                    raise SourceError("Quelle lieferte ungültiges JSON") from exc
            except HTTPError as exc:
                if exc.code in (429, 503) and attempt == 0 and self.role != "guardian":
                    time.sleep(1)
                    continue
                raise SourceError(f"Lesefehler HTTP {exc.code}") from None
            except (URLError, TimeoutError, OSError) as exc:
                # Never expose credentials, endpoint query strings or response bodies.
                raise SourceError("Quelle nicht erreichbar oder Zeitlimit überschritten") from None
        raise SourceError("Lesebudget erschöpft")


def dotted(value, path):
    for name in path.split("."):
        if not isinstance(value, dict) or name not in value:
            raise InputError("Konfiguriertes Datenfeld fehlt: "+path)
        value = value[name]
    return value


def state_timestamp(state):
    for name in ("last_reported", "last_updated"):
        if state.get(name):
            return iso(timestamp(state[name]))
    raise InputError("HA-Messzeit fehlt")


def state_value(state, unit=None):
    attrs = state.get("attributes", {})
    if unit is not None and attrs.get("unit_of_measurement") != unit:
        raise InputError("Unerwartete Einheit des HA-Sensors")
    raw = state.get("state")
    if raw in (None, "unknown", "unavailable", ""):
        raise InputError("HA-Sensor hat keinen Messwert")
    try:
        value = float(raw)
    except (ValueError, TypeError):
        raise InputError("HA-Sensor ist nicht numerisch") from None
    return number(value, "HA-Messwert", low=-1e12)


def ha_forecast(state, spec):
    records = dotted(state, spec.get("records_path", "attributes.forecast"))
    if not isinstance(records, list):
        raise InputError("HA-Prognose muss explizite Intervallliste enthalten")
    result = []
    scale = number(spec.get("scale_to_kwh", 1), "scale_to_kwh", 0.0000001)
    for row in records:
        a = timestamp(row[spec.get("start_key", "start")])
        if spec.get("end_key", "end") in row:
            b = timestamp(row[spec.get("end_key", "end")])
        else:
            duration = number(spec.get("interval_minutes"), "interval_minutes", 0.001, 1440)
            b = a+timedelta(minutes=duration)
        energy = number(row[spec.get("energy_key", "energy_kwh")], "Forecastenergie")*scale
        # Optional conversion from average power, only with explicitly declared unit.
        if spec.get("value_type", "energy") == "power":
            energy *= (b-a).total_seconds()/3600
        elif spec.get("value_type", "energy") != "energy":
            raise InputError("value_type muss energy oder power sein")
        result.append({"start": iso(a), "end": iso(b), "energy_kwh": energy})
    generated = (dotted(state, spec["generated_at_path"]) if spec.get("generated_at_path") else state_timestamp(state))
    return {"generated_at": iso(timestamp(generated)), "intervals": result,
            "source": "home_assistant_sensor", "generation_time_basis": spec.get("generated_at_path", "HA last_reported/last_updated; upstream issue time not separately verified")}


def ha_solcast_forecast(day_states, metadata, issue_state, spec):
    """Read per-site cached half-hourly forecasts; never query Solcast itself."""
    site = spec["site_id"]
    if metadata.get("attributes", {}).get("resource_id") != site:
        raise InputError("Solcast-Anlagenkennung stimmt nicht mit bestätigter Zuordnung überein")
    attr = "detailedForecast_"+site.replace("-", "_")
    normal, p10, p90 = [], [], []
    for state in day_states:
        records = state.get("attributes", {}).get(attr)
        if not isinstance(records, list) or not records:
            raise InputError("Getrennte Solcast-Halbstundenprognose fehlt: "+state.get("entity_id", "?"))
        for row in records:
            start = timestamp(row["period_start"])
            end = start+timedelta(minutes=30)
            for key, target in (("pv_estimate", normal),("pv_estimate10", p10),("pv_estimate90", p90)):
                target.append({"start": iso(start), "end": iso(end),
                    "energy_kwh": number(row[key], key)*0.5})
    from .model import intervals
    intervals(normal, "Solcast "+site)
    return {"generated_at": iso(timestamp(issue_state["state"])),
            "intervals": normal, "intervals_p10": p10, "intervals_p90": p90,
            "source": "home_assistant_solcast_cached_per_site", "site_id": site,
            "site_name": metadata.get("attributes", {}).get("name"),
            "generation_time_basis": "HA Solcast last successful API poll sensor; not HA state refresh time",
            "units_basis": "pv_estimate* are interval-average kW; multiply by 0.5 h exactly once"}


def load_profile(start, policy, values, source):
    if values is None:
        return []
    from zoneinfo import ZoneInfo
    if isinstance(values, list):
        if len(values) != 24:
            raise InputError(source+": genau 24 Stundenwerte erforderlich")
        for v in values:
            number(v, source)
    else:
        number(values, source)
    edges = [start+timedelta(minutes=policy.step_minutes*i)
             for i in range(int(policy.horizon_hours*60/policy.step_minutes)+1)]
    result = []
    for a, b in zip(edges, edges[1:]):
        power = values[a.astimezone(ZoneInfo(policy.timezone)).hour] if isinstance(values, list) else values
        result.append({"start": iso(a), "end": iso(b),
                       "energy_kwh": power*(b-a).total_seconds()/3600})
    return result


def guardian_snapshot(client, now):
    status = client.read("/api/research/status")
    if status.get("read_only") is not True or status.get("enabled") is not True:
        raise SourceError("Guardian bestätigt keine aktive Nur-Lese-Schnittstelle")
    topology = client.read("/api/research/topology")
    positions = topology.get("data", {}).get("positions", [])
    if not positions or any(not p.get("resolved") or not p.get("physical_serial") for p in positions):
        raise SourceError("Guardian-Topologie ist nicht vollständig zugeordnet")
    query = {"source": "guardian.hycube", "metric": "battery_capacity",
             "from": iso(now-timedelta(minutes=10)), "to": iso(now), "max_points": 100}
    envelopes, points, cursor, seen = [], [], None, set()
    for _ in range(3):
        if cursor:
            query["cursor"] = cursor
        envelope = client.read("/api/research/timeseries", query)
        if (envelope.get("data_source") != "guardian.hycube" or envelope.get("evidence_class") != "OBSERVED"
                or envelope.get("authoritative") is not True or envelope.get("data", {}).get("unit") != "%"):
            raise SourceError("Unerwartete Guardian-Quelle oder Einheit")
        envelopes.append(envelope)
        points.extend(envelope.get("data", {}).get("points", []))
        pagination = envelope.get("pagination", {})
        cursor = envelope.get("next_cursor") or pagination.get("next_cursor")
        if not cursor:
            if envelope.get("truncated"):
                raise SourceError("Guardian-Antwort ist ohne Fortsetzung abgeschnitten")
            break
        if cursor in seen:
            raise SourceError("Guardian wiederholt einen Fortsetzungscursor")
        seen.add(cursor)
    else:
        raise SourceError("Guardian-Abfrage überschreitet das Seitenbudget")
    valid = []
    for point in points:
        try:
            v = number(point["value"], "Guardian BatteryCapacity", 0, 100)
            t = timestamp(point["timestamp"])
        except (InputError, KeyError):
            continue
        if now-timedelta(minutes=10) <= t <= now:
            valid.append((t, v, point))
    if not valid:
        raise SourceError("Keine aktuelle Guardian-Batterieanzeige")
    t, value, point = max(valid, key=lambda item: item[0])
    return {"status": status, "topology": topology, "series": envelopes,
            "latest_display_percent": value, "observed_at": iso(t),
            "parse_quality": point.get("parse_quality"),
            "source": "guardian.hycube BatteryCapacity; not a calibrated energy value"}


def read_token(spec, default_env):
    if spec.get("token_file"):
        try:
            return Path(spec["token_file"]).read_text().strip()
        except OSError:
            raise SourceError("Konfigurierte Token-Datei nicht lesbar") from None
    return os.environ.get(spec.get("token_env", default_env), "")


def estimate_soc_energy(observations, mapping):
    """SOC-based DC estimate ABOVE the configured technical floor; not a capacity test."""
    soc_row = observations[mapping.get("soc_observation", "hycube_soc")]
    floor_row = observations[mapping.get("floor_observation", "outage_protection_threshold")]
    if soc_row["unit"] != "%" or floor_row["unit"] != "%":
        raise InputError("SOC und Schutzgrenze müssen Prozentwerte sein")
    nominal = number(mapping.get("nominal_capacity_kwh"), "nominal_capacity_kwh", 0.001)
    floor = number(floor_row["value"], "technical floor", 0, 99.9)
    expected_floor = number(mapping.get("technical_floor_percent"), "configured floor", 0, 99.9)
    if abs(floor-expected_floor)>1e-6:
        raise InputError("Gelesene Schutzgrenze geändert; Kapazitätsannahme prüfen")
    soc = number(soc_row["value"], "Hycube SOC", 0, 100)
    return {"energy_kwh": nominal*max(0, soc-floor)/100,
            "estimated_capacity_kwh": nominal*(100-floor)/100,
            "observed_at": soc_row["observed_at"], "mapping_confirmed": True,
            "estimate": True, "soc_percent": soc, "technical_floor_percent": floor,
            "nominal_capacity_kwh": nominal,
            "basis": "User-approved linear SOC estimate from nominal capacity; no measured capacity calibration; no module SOC averaging"}


def collect(config, policy, now):
    snapshot = {"schema_version": 1, "kind": "live", "captured_at": iso(now),
                "battery": {}, "forecasts": {}, "loads": {}, "observations": {},
                "collection_issues": [], "evidence": {}}
    source = config.get("sources", {})
    guardian_spec = source.get("guardian")
    if guardian_spec:
        try:
            token = read_token(guardian_spec, "GUARDIAN_RESEARCH_TOKEN")
            client = ReadClient(guardian_spec["base_url"], "guardian", token)
            evidence = guardian_snapshot(client, now)
            snapshot["evidence"]["guardian"] = evidence
            mapping = config.get("battery_mapping", {})
            if mapping.get("confirmed") is True and policy.capacity_kwh is not None:
                floor = number(mapping.get("empty_display_percent"), "empty_display_percent", 0, 100)
                full = number(mapping.get("full_display_percent"), "full_display_percent", 0, 100)
                if full <= floor:
                    raise InputError("Kalibrierte Batterieanzeige hat kein gültiges Fenster")
                fraction = (evidence["latest_display_percent"]-floor)/(full-floor)
                if not 0 <= fraction <= 1:
                    raise InputError("Batterieanzeige liegt außerhalb des kalibrierten Fensters")
                snapshot["battery"] = {"energy_kwh": fraction*policy.capacity_kwh,
                    "observed_at": evidence["observed_at"], "mapping_confirmed": True,
                    "basis": "explicit user-confirmed linear calibration, not module averaging"}
        except (SourceError, InputError, KeyError) as exc:
            snapshot["collection_issues"].append("Guardian: "+str(exc))
    ha_spec = source.get("home_assistant")
    ha = None
    if ha_spec:
        token = read_token(ha_spec, "HA_TOKEN")
        if token:
            ha = ReadClient(ha_spec["base_url"], "ha", token)
        else:
            snapshot["collection_issues"].append("HA-Lesezugang fehlt; keine unautorisierten Netzwerkversuche")
    states = {}
    def get_state(entity):
        if not ha or not ENTITY.fullmatch(entity):
            raise InputError("HA-Sensorzuordnung oder HA-Quelle fehlt")
        if entity not in states:
            states[entity] = ha.read("/api/states/"+entity)
        return states[entity]
    for name in ("hycube", "secondary"):
        spec = source.get("forecasts", {}).get(name)
        if not spec:
            continue
        try:
            if spec["type"] == "ha_attribute":
                snapshot["forecasts"][name] = ha_forecast(get_state(spec["entity_id"]), spec)
            elif spec["type"] == "ha_solcast":
                snapshot["forecasts"][name] = ha_solcast_forecast(
                    [get_state(entity) for entity in spec["entity_ids"]],
                    get_state(spec["metadata_entity"]), get_state(spec["issue_entity"]), spec)
            elif spec["type"] == "file":
                data = json.loads(Path(spec["path"]).read_text())
                snapshot["forecasts"][name] = {"generated_at": data["generated_at"], "intervals": data["intervals"], "source": "local_forecast_file"}
            else:
                raise InputError("Unbekannter Prognoseadapter")
        except (InputError, SourceError, KeyError, OSError, ValueError) as exc:
            snapshot["collection_issues"].append("Prognose "+name+": "+str(exc))
    for name, spec in source.get("measurements", {}).items():
        try:
            state = get_state(spec["entity_id"])
            value = state_value(state, spec["unit"])*number(spec.get("scale", 1), "measurement scale", 0.000001)
            snapshot["observations"][name] = {"value": value, "unit": spec["output_unit"],
                "observed_at": state_timestamp(state), "source_entity": spec["entity_id"]}
            if name == "battery_energy" and spec.get("mapping_confirmed") is True:
                if spec["output_unit"] != "kWh":
                    raise InputError("Batterieenergie muss in kWh vorliegen")
                snapshot["battery"] = {"energy_kwh": value, "observed_at": state_timestamp(state), "mapping_confirmed": True, "basis": "confirmed HA usable energy sensor"}
        except (InputError, SourceError, KeyError) as exc:
            snapshot["collection_issues"].append("Messwert "+name+": "+str(exc))
    shared=source.get('guardian_shared')
    if shared:
        from .guardian_history import current,read_history
        try:
            soc,flows=current(shared,now)
            snapshot['observations']['hycube_soc']=soc
            snapshot['observations'].update(flows)
        except (InputError,OSError,ValueError,KeyError) as exc:
            snapshot['collection_issues'].append('Guardian aktuell: '+str(exc))
        try:snapshot['balancing_history']=read_history(shared,now)
        except (InputError,OSError,ValueError,KeyError) as exc:
            snapshot['balancing_collection_issue']='Guardian Historie: '+str(exc)
    mapping = config.get("battery_mapping", {})
    if mapping.get("mode") == "soc_estimate" and mapping.get("confirmed") is True:
        try:
            snapshot["battery"] = estimate_soc_energy(snapshot["observations"], mapping)
            expected_capacity = snapshot["battery"]["estimated_capacity_kwh"]
            if policy.capacity_kwh is None or abs(expected_capacity-policy.capacity_kwh)>1e-6:
                raise InputError("SOC-Kapazität und konfigurierter nutzbarer Modellbereich stimmen nicht überein")
        except (InputError, KeyError) as exc:
            snapshot["battery"] = {}
            snapshot["collection_issues"].append("SOC-Energieschätzung: "+str(exc))
    # Optional normalized Guardian history export. Read-only, independent of energy assessment.
    balancing_spec=source.get('balancing_history',{})
    try:
        if balancing_spec.get('type')=='file':
            snapshot['balancing_history']=json.loads(Path(balancing_spec['path']).read_text())
        elif balancing_spec.get('type')=='ha_attribute':
            state=get_state(balancing_spec['entity_id'])
            snapshot['balancing_history']=state['attributes'][balancing_spec['attribute']]
    except (InputError,SourceError,KeyError,OSError,ValueError) as exc:
        snapshot['balancing_collection_issue']='Balancinghistorie nicht lesbar: '+str(exc)
    snapshot["loads"] = {
        "minimum": load_profile(now, policy, config.get("load_profiles", {}).get("minimum_kw"), "minimum_kw")}
    return snapshot
