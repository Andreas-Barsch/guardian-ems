"""Pure chronological energy model; all energies are kWh, powers kW, times UTC.

Capacity and state of energy refer to usable DC energy ABOVE the technical floor.
Forecast inputs refer to AC-equivalent energy at the house bus. Conversion losses
are applied once. This is an energy model, not an inverter or BMS emulator.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

UTC = timezone.utc


class InputError(ValueError):
    pass


def timestamp(value: str) -> datetime:
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError, TypeError) as exc:
        raise InputError("Zeitstempel muss ISO-8601 mit UTC-Offset sein") from exc
    if t.tzinfo is None or t.utcoffset() is None:
        raise InputError("Zeitstempel ohne UTC-Offset ist nicht zulässig")
    return t.astimezone(UTC)


def iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def number(value: Any, name: str, low=0.0, high=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{name}: endliche Zahl erforderlich")
    v = float(value)
    if not isfinite(v) or v < low or (high is not None and v > high):
        raise InputError(f"{name}: außerhalb des zulässigen Bereichs")
    return v


@dataclass(frozen=True)
class Policy:
    horizon_hours: int = 72
    outage_hours: int = 24
    start_window_hours: int = 24
    step_minutes: int = 60
    capacity_kwh: float | None = None
    minimum_reserve_kwh: float = 0.0
    terminal_reserve_kwh: float = 0.0
    safety_margin_kwh: float = 0.0
    charge_efficiency: float = 0.92
    discharge_efficiency: float = 0.92
    charge_limit_kw: float = 2.0
    discharge_limit_kw: float = 3.0
    island_pv_limit_kw: float = 3.0
    standby_kw: float = 0.04
    solar_correction_factor: float = 0.8
    forecast_max_age_hours: float = 6.0
    battery_max_age_minutes: float = 10.0
    profile_confirmed: bool = False
    power_limits_confirmed: bool = False
    secondary_can_charge_battery: bool = False
    timezone: str = "Europe/Berlin"

    @classmethod
    def parse(cls, raw: dict) -> "Policy":
        unknown = set(raw) - set(cls.__dataclass_fields__)
        if unknown:
            raise InputError("Unbekannte Modellparameter: " + ", ".join(sorted(unknown)))
        p = cls(**raw)
        for name in ("horizon_hours", "outage_hours", "start_window_hours", "step_minutes"):
            if type(getattr(p, name)) is not int:
                raise InputError(name + ": Ganzzahl erforderlich")
        if not (1 <= p.horizon_hours <= 168 and 1 <= p.outage_hours <= 144):
            raise InputError("Planungsfenster höchstens sieben Tage")
        if not (0 <= p.start_window_hours <= 48):
            raise InputError("Startfenster muss zwischen 0 und 48 Stunden liegen")
        if p.horizon_hours < p.outage_hours + p.start_window_hours:
            raise InputError("Horizont deckt nicht jede volle Zielausfalldauer ab")
        if p.step_minutes not in (15, 30, 60):
            raise InputError("Schrittweite muss 15, 30 oder 60 Minuten sein")
        for name in ("profile_confirmed", "power_limits_confirmed", "secondary_can_charge_battery"):
            if type(getattr(p, name)) is not bool:
                raise InputError(name + ": bool erforderlich")
        if p.capacity_kwh is not None:
            number(p.capacity_kwh, "capacity_kwh", 0.001)
        for name in ("minimum_reserve_kwh", "terminal_reserve_kwh", "safety_margin_kwh",
                     "charge_limit_kw", "discharge_limit_kw", "island_pv_limit_kw", "standby_kw"):
            number(getattr(p, name), name)
        for name in ("charge_efficiency", "discharge_efficiency"):
            number(getattr(p, name), name, 0.01, 1)
        for name in ("solar_correction_factor",):
            number(getattr(p, name), name, 0, 1)
        for name in ("forecast_max_age_hours", "battery_max_age_minutes"):
            number(getattr(p, name), name, 0.001)
        try:
            ZoneInfo(p.timezone)
        except Exception as exc:
            raise InputError("Unbekannte Zeitzone") from exc
        return p


def intervals(records: list[dict], name: str) -> list[tuple]:
    if not isinstance(records, list):
        raise InputError(name + ": Intervallliste erforderlich")
    result = []
    for row in records:
        a, b = timestamp(row["start"]), timestamp(row["end"])
        if a >= b:
            raise InputError(name + ": Intervallende muss nach Beginn liegen")
        result.append((a, b, number(row["energy_kwh"], name + ".energy_kwh")))
    result.sort()
    for previous, current in zip(result, result[1:]):
        if current[0] < previous[1]:
            raise InputError(name + ": überlappende Intervalle")
    return result


def resample(records: list[dict], edges: list[datetime], name: str):
    """Energy-preserving overlap; absent intervals remain None, never implicit zero.

    Partial interval overlap assumes uniform power inside the original interval.
    This does not create new forecast information or evidence of short power peaks.
    """
    series = intervals(records, name)
    values = []
    for a, b in zip(edges, edges[1:]):
        seconds, covered, energy = (b-a).total_seconds(), 0.0, 0.0
        for left, right, val in series:
            overlap = max(0.0, (min(b, right)-max(a, left)).total_seconds())
            if overlap:
                covered += overlap
                energy += val * overlap / (right-left).total_seconds()
        values.append(energy if abs(covered-seconds) < 0.001 else None)
    return values


def flows(pv_kw: float, load_kw: float, p: Policy):
    """DC deficit, potential DC gain, unmet AC power, all per one hour."""
    pv = min(pv_kw, p.island_pv_limit_kw)
    deficit = max(load_kw-pv, 0)
    gain = min(max(pv-load_kw, 0), p.charge_limit_kw) * p.charge_efficiency
    return deficit / p.discharge_efficiency, gain, max(deficit-p.discharge_limit_kw, 0)


def required_energy(pv: list[float], load: list[float], p: Policy, dt: float):
    r = p.terminal_reserve_kwh
    maximum = r
    power_failure = False
    for solar, consumption in reversed(list(zip(pv, load))):
        d, g, unmet = flows(solar, consumption+p.standby_kw, p)
        power_failure |= unmet > 1e-9
        r = max(0.0, r+(d-g)*dt)
        maximum = max(maximum, r)
    target = max(p.minimum_reserve_kwh, r+p.safety_margin_kwh)
    return target, maximum, power_failure


def forward(pv, load, initial, p: Policy, dt: float, edges):
    energy = initial
    rows, unserved, first_gap = [], 0.0, None
    for i, (solar, demand) in enumerate(zip(pv, load)):
        total_load = demand+p.standby_kw
        deficit, gain, power_unmet = flows(solar, total_load, p)
        ac_deficit = deficit*p.discharge_efficiency*dt
        discharge = min(energy, p.discharge_limit_kw*dt/p.discharge_efficiency, deficit*dt)
        missing = max(0.0, ac_deficit-discharge*p.discharge_efficiency)
        energy -= discharge
        accepted = min(p.capacity_kwh-energy, gain*dt)
        energy += accepted
        unserved += missing
        if missing > 1e-8 and first_gap is None:
            first_gap = iso(edges[i])
        rows.append({"start": iso(edges[i]), "end": iso(edges[i+1]),
                     "hycube_pv_kw": round(min(solar, p.island_pv_limit_kw), 6),
                     "secondary_pv_kw": 0.0, "grid_kw": 0.0,
                     "minimum_load_kw": round(total_load, 6),
                     "battery_energy_kwh": round(energy, 6),
                     "unserved_ac_kwh": round(missing, 6),
                     "unused_dc_gain_kwh": round(max(0, gain*dt-accepted), 6),
                     "discharge_power_limit_exceeded": power_unmet > 1e-9})
    return {"rows": rows, "end_energy_kwh": round(energy, 6),
            "unserved_ac_kwh": round(unserved, 6), "first_shortfall_at": first_gap,
            "terminal_reserve_met": energy+1e-8 >= p.terminal_reserve_kwh}


def shortage_windows(rows):
    """A supply gap is defined by useful solar versus minimum load, not sunrise."""
    result, window = [], None
    for row in rows:
        if row["hycube_pv_kw"]+1e-9 < row["minimum_load_kw"]:
            if window is None:
                window = {"start": row["start"], "end": row["end"], "load_kwh": 0.0}
            duration = (timestamp(row["end"])-timestamp(row["start"])).total_seconds()/3600
            window["end"] = row["end"]
            window["load_kwh"] += row["minimum_load_kw"]*duration
        elif window is not None:
            result.append(window); window = None
    if window is not None:
        result.append(window)
    for win in result:
        win["hours"] = (timestamp(win["end"])-timestamp(win["start"])).total_seconds()/3600
        win["load_kwh"] = round(win["load_kwh"], 6)
    return result



def simulate(snapshot: dict, policy: Policy, now: datetime | None = None) -> dict:
    start = timestamp(snapshot["captured_at"])
    now = now or start
    if now.tzinfo is None:
        raise InputError("now muss einen UTC-Offset haben")
    issues, qualifications = list(snapshot.get("collection_issues", [])), []
    if start > now+timedelta(minutes=1):
        issues.append("Snapshot liegt in der Zukunft")
    if now-start > timedelta(minutes=policy.battery_max_age_minutes):
        issues.append("Snapshot ist für einen neuen aktuellen Lauf zu alt")
    dt = policy.step_minutes/60
    n = int(policy.horizon_hours/dt)
    count = int(policy.outage_hours/dt)
    starts = int(policy.start_window_hours/dt)
    edges = [start+timedelta(minutes=policy.step_minutes*i) for i in range(n+1)]
    forecasts = snapshot.get("forecasts", {})
    values = {}
    for key in ("hycube", "secondary"):
        entry = forecasts.get(key, {})
        if not entry:
            issues.append("Solarprognose fehlt: "+key)
        else:
            age = now-timestamp(entry["generated_at"])
            if age < -timedelta(minutes=1) or age > timedelta(hours=policy.forecast_max_age_hours):
                issues.append("Solarprognose veraltet oder zukünftig: "+key)
        values[key] = resample(entry.get("intervals", []), edges, key)
        if any(v is None for v in values[key]):
            issues.append("Solarprognose deckt Horizont nicht vollständig ab: "+key)
    for key in ("minimum",):
        values[key] = resample(snapshot.get("loads", {}).get(key, []), edges, key)
        if any(v is None for v in values[key]):
            if key == "minimum":
                issues.append("Lastprofil deckt Horizont nicht vollständig ab: "+key)
            else:
                qualifications.append("Normallastprofil fehlt: keine Vorhersage des Batterievorrats vor einem später beginnenden Ausfall")
    battery = snapshot.get("battery", {})
    if policy.capacity_kwh is None:
        issues.append("Nutzbare Batteriekapazität noch nicht festgelegt")
    energy = battery.get("energy_kwh")
    if energy is None:
        issues.append("Keine belegte nutzbare Batterieenergie in kWh")
    else:
        number(energy, "battery.energy_kwh", 0, policy.capacity_kwh)
    if not battery.get("mapping_confirmed", False):
        issues.append("Zuordnung von Batterieanzeige zu nutzbaren kWh nicht bestätigt")
    if not battery.get("observed_at"):
        issues.append("Messzeit der Batterieenergie fehlt")
    else:
        age = now-timestamp(battery["observed_at"])
        if age < -timedelta(minutes=1) or age > timedelta(minutes=policy.battery_max_age_minutes):
            issues.append("Batterieenergie veraltet oder zukünftig")
    if battery.get("estimate"):
        qualifications.append("Batterieenergie aus Hycube-SOC und Nennenergie geschätzt; Alterung und SOC-Abweichungen nicht kalibriert")
        if policy.capacity_kwh is None or abs(number(battery.get("estimated_capacity_kwh"), "SOC estimated capacity",0.001)-policy.capacity_kwh)>1e-6:
            issues.append("SOC-Schätzung hat einen unpassenden Kapazitätsbezug")
    if not policy.profile_confirmed:
        qualifications.append("Mindestlastprofil ist eine unbestätigte Annahme")
    if not policy.power_limits_confirmed:
        qualifications.append("Leistungs-/Inselgrenzen und Anlaufströme noch nicht abgenommen")
    result = {"schema_version": 1, "software_version": "0.5.1", "read_only": True,
              "kind": snapshot.get("kind", "live"), "issued_at": iso(now),
              "simulation_start": iso(start), "policy": asdict(policy),
              "status": "DATENLUECKE" if issues else "VORLAEUFIG",
              "issues": issues, "qualifications": qualifications,
              "balancing": {"status": "NICHT_AUSGEWERTET", "reason": "v0.1 simuliert Energie; kein Balancingnachweis"},
              "assumptions": ["Anlage 2 im Inselbetrieb immer null", "Kein Netz im Inselbetrieb",
                              "Prognose innerhalb jedes Eingangsintervalls gleichmäßig verteilt",
                              "Keine Normalbetriebssimulation; Vollladebedarf nur aus Historie"],
              "battery": battery,
              "scenarios": []}
    learned=snapshot.get('solar_learning',{})
    factors={key:number(learned.get(key,{}).get('factor',policy.solar_correction_factor),'solar factor '+key,0) for key in ('hycube','secondary')}
    result['solar_learning']=learned
    result['effective_solar_factors']=factors
    minimum_window = values["minimum"][:count]
    if all(v is not None for v in minimum_window):
        house = sum(minimum_window)
        standby = policy.standby_kw*policy.outage_hours
        result["minimum_load_budget"] = {
            "outage_hours": policy.outage_hours,
            "house_ac_kwh": round(house, 6),
            "additional_standby_ac_kwh": round(standby, 6),
            "zero_solar_dc_kwh_without_reserves": round((house+standby)/policy.discharge_efficiency, 6),
            "basis": "Configured load, assumed standby and efficiency; not an observed usable battery capacity or supply confirmation"}
    # A requirement can be estimated without inventing available battery energy.
    # It never changes DATENLUECKE into a supply confirmation.
    if all(v is not None for v in values["hycube"]) and all(v is not None for v in values["minimum"]):
        minimum_power = [v/dt for v in values["minimum"]]
        result["reserve_requirements_only"] = []
        for name, factor in (("corrected_forecast", factors["hycube"]),):
            solar_power = [v/dt*factor for v in values["hycube"]]
            curve = []
            for index in range(starts+1):
                needed, peak, power_failure = required_energy(solar_power[index:index+count], minimum_power[index:index+count], policy, dt)
                curve.append({"outage_start": iso(edges[index]), "required_energy_kwh": round(needed,6),
                    "maximum_intermediate_requirement_kwh": round(peak,6),
                    "power_limit_exceeded": power_failure})
            result["reserve_requirements_only"].append({"name": name, "required_now_kwh": curve[0]["required_energy_kwh"],
                "maximum_required_start_kwh": max(row["required_energy_kwh"] for row in curve),
                "curve": curve, "supply_confirmed": False,
                "qualification": "Requirement only; assumed efficiencies and limits; no claim of available energy"})
    from .planning import reserve_advice, balancing, autonomy
    result['reserve_advice'] = reserve_advice(result)
    result['balancing'] = balancing(snapshot.get('balancing_history'), now)
    result['balancing']['collection_issue'] = snapshot.get('balancing_collection_issue')
    observed=snapshot.get('observations',{})
    if 'normal_passive_threshold' in observed and 'passive_outage_threshold' in observed:
        result['reserve_settings']={'passive_percent':max(0,observed['normal_passive_threshold']['value']-observed['passive_outage_threshold']['value'])}
    result['current_flows'] = {k:v for k,v in snapshot.get('observations',{}).items()
        if k in ('house_power','hycube_pv_power','secondary_pv_power','grid_power','battery_power','external_power')}
    result['current_flows_note'] = 'Quellenleistungen werden nur angezeigt; Vorzeichen müssen je Sensor bestätigt sein. Notstromlast ersetzt die gemessene Normallast.'
    if issues:
        result["fallback"] = "Keine Reserveabsenkung ableiten; keine gedeckte Versorgung behaupten. Fehlende Solarwerte sind unbekannt, nicht beobachtete Nullwerte."
        return result
    primary = [v/dt for v in values["hycube"]]
    secondary = [v/dt*factors["secondary"] for v in values["secondary"]]
    minimum = [v/dt for v in values["minimum"]]
    normal = None  # Normalbetriebssimulation ausdrücklich entfernt.
    for name, factor in (("corrected_forecast", factors["hycube"]),):
        pv = [v*factor for v in primary]
        targets, corridor = [], []
        for index in range(starts+1):
            req, maximum, power_gap = required_energy(pv[index:index+count], minimum[index:index+count], policy, dt)
            targets.append(req)
            corridor.append({"outage_start": iso(edges[index]), "outage_end": iso(edges[index+count]),
                             "required_energy_kwh": round(req, 6),
                             "capacity_sufficient": max(req, maximum) <= policy.capacity_kwh+1e-8,
                             "power_sufficient": not power_gap})
        full_outage = forward(pv, minimum, energy, policy, dt, edges)
        reach = autonomy(full_outage, {'energy_kwh':energy,'efficiency':policy.discharge_efficiency,'at':iso(start)},policy.horizon_hours)
        outage = forward(pv[:count], minimum[:count], energy, policy, dt, edges[:count+1])
        baseline = []
        stocks = [energy]+[r["battery_energy_kwh"] for r in baseline]
        for i, row in enumerate(corridor):
            available = stocks[i] if i < len(stocks) else None
            row["normal_baseline_energy_kwh"] = round(available,6) if available is not None else None
            row["reserve_gap_kwh"] = round(max(0,targets[i]-available),6) if available is not None else None
        maximum_gap = max(r["reserve_gap_kwh"] for r in corridor) if normal is not None else None
        current_gap = max(0, targets[0]-energy)
        assessed_gap = maximum_gap if maximum_gap is not None else current_gap
        # Constant ADDITIONAL AC load sustained for the whole requested outage.
        # This is deliberately conservative and not a per-night/peak permission.
        lo, hi = 0.0, policy.discharge_limit_kw
        feasible = outage["unserved_ac_kwh"] < 1e-8 and outage["terminal_reserve_met"]
        if feasible:
            for _ in range(35):
                mid = (lo+hi)/2
                trial = forward(pv[:count], [x+mid for x in minimum[:count]], max(0, energy-policy.safety_margin_kwh), policy, dt, edges[:count+1])
                if trial["unserved_ac_kwh"] < 1e-8 and trial["terminal_reserve_met"]:
                    lo = mid
                else:
                    hi = mid
        result["scenarios"].append({"name": name, "solar_factor": factor,
            "required_now_kwh": round(targets[0], 6),
            "reserve_gap_now_kwh": round(max(0, targets[0]-energy), 6),
            "maximum_future_gap_kwh": round(maximum_gap, 6) if maximum_gap is not None else None, "reserve_corridor": corridor,
            "outage_now": outage, "autonomy": reach, "continuous_outage": full_outage, "normal_baseline": baseline,
            "supply_gap_windows": shortage_windows(outage["rows"]),
            "constant_extra_load_kw": round(lo, 6) if feasible else 0.0,
            "extra_load_scope": "Zusätzliche konstante AC-Leistung über die gesamte Zielausfalldauer; keine Freigabe von Spitzenlasten",
            "model_scope": "outage_now_and_future_normal_baseline" if normal is not None else "outage_now; future reserve requirements only",
            "model_status": "UNTERDECKUNG" if (assessed_gap > 1e-6 or not feasible or (normal is not None and not all(r["capacity_sufficient"] and r["power_sufficient"] for r in corridor))) else "MODELL_GEDECKT"})
    result["reserve_advice"] = reserve_advice(result)
    from .budget import plan_budget
    result['energy_plan']=plan_budget(pv,minimum,energy,policy,edges,battery,result['balancing'])
    reserve = result["scenarios"][0]
    result["status"] = reserve["model_status"] if not qualifications else "VORLAEUFIG"
    if snapshot.get("kind") == "synthetic_demo":
        result["status"] = "DEMO"
    result["recommendation"] = ("Reservebedarf rechnerisch gedeckt; keine Geräteaktion."
        if reserve["model_status"] == "MODELL_GEDECKT" else
        "Reservebedarf nicht gedeckt. Ladefehlbetrag beziehungsweise Leistungsgrenze prüfen; keine Geräteaktion.")
    return result
