from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from maintenance import DEFAULT_MAINTENANCE_EVENT_FILE, MaintenanceEventLog
from maintenance_api import (
    API_ROUTE,
    MAX_REQUEST_BODY_BYTES,
    MaintenanceApi,
    error_json,
)
from maintenance_service import MaintenanceRepository, MaintenanceService
from maintenance_ui import render_maintenance_html
from timeline import DEFAULT_TECHNICAL_EVENT_FILE, TechnicalEventSource, TimelineService
from timeline_api import TIMELINE_API_ROUTE, TimelineApi
from timeline_ui import render_timeline_html
from event_overlay import EventOverlayAdapter
from history_api import HISTORY_API_ROUTE, HistoryApi
from history_series import DEFAULT_CELL_HISTORY_DIR, CellHistorySeries
from rs485_evidence import DEFAULT_RS485_HISTORY_DIR, Rs485HistorySeries
from history_ui import render_history_html
from guardian_header import render_guardian_header
from position_history import (DEFAULT_POSITION_HISTORY_FILE, PositionHistoryLog,
                              PositionHistoryService, classify_stack_change,
                              history_observation_ready, stable_observed_changes)
from position_history_api import POSITION_HISTORY_API_ROUTE, PositionHistoryApi
from module_information_ui import render_module_information_html
from config_history import ConfigHistory
from phase_engine import PhaseEngine
from version import GUARDIAN_VERSION, DIAGNOSTIC_ENGINE_VERSION

OPTIONS_FILE = Path('/data/options.json')
CONFIG_HISTORY_FILE = Path('/share/guardian_battery/config_history.jsonl')
SUPERVISOR = 'http://supervisor'
TOKEN = os.environ.get('SUPERVISOR_TOKEN', '')
_MAINTENANCE_API = None
_MAINTENANCE_API_LOCK = threading.Lock()
_TIMELINE_API = None
_HISTORY_API = None
_POSITION_HISTORY_API = None
_MAINTENANCE_LIVE_PUBLISHER = None
_RS485_STATUS_PROVIDER = None
_AUTOMATIC_POSITION_LOCK = threading.Lock()


def configure_maintenance_live_publisher(publisher):
    """Attach the runtime MQTT publisher without coupling API tests to MQTT."""
    global _MAINTENANCE_LIVE_PUBLISHER
    _MAINTENANCE_LIVE_PUBLISHER = publisher
    if _MAINTENANCE_API is not None:
        _MAINTENANCE_API.live_publisher = publisher


def configure_rs485_status_provider(provider):
    """Expose compact live RS485 observations to the ingress UI."""
    global _RS485_STATUS_PROVIDER
    _RS485_STATUS_PROVIDER = provider


def _get_maintenance_api():
    """Create the production service lazily inside the existing ingress app."""
    global _MAINTENANCE_API
    if _MAINTENANCE_API is None:
        with _MAINTENANCE_API_LOCK:
            if _MAINTENANCE_API is None:
                log = MaintenanceEventLog(DEFAULT_MAINTENANCE_EVENT_FILE)
                repository = MaintenanceRepository(log)
                _MAINTENANCE_API = MaintenanceApi(
                    MaintenanceService(repository),
                    live_publisher=_MAINTENANCE_LIVE_PUBLISHER,
                )
    return _MAINTENANCE_API


def _get_timeline_api():
    """Reuse the maintenance service and project the existing technical log."""
    global _TIMELINE_API
    maintenance = _get_maintenance_api().service
    position_history = _get_position_history_api().service
    if _TIMELINE_API is None:
        with _MAINTENANCE_API_LOCK:
            if _TIMELINE_API is None:
                _TIMELINE_API = TimelineApi(
                    TimelineService(maintenance, TechnicalEventSource(DEFAULT_TECHNICAL_EVENT_FILE),
                                    position_history)
                )
    return _TIMELINE_API


def _get_history_api():
    global _HISTORY_API
    timeline = _get_timeline_api().service
    if _HISTORY_API is None:
        with _MAINTENANCE_API_LOCK:
            if _HISTORY_API is None:
                _HISTORY_API = HistoryApi(
                    CellHistorySeries(DEFAULT_CELL_HISTORY_DIR),
                    EventOverlayAdapter(timeline),
                    PhaseEngine(ConfigHistory(CONFIG_HISTORY_FILE), _read_options),
                    Rs485HistorySeries(DEFAULT_RS485_HISTORY_DIR),
                )
    return _HISTORY_API

def _get_position_history_api():
    global _POSITION_HISTORY_API
    maintenance = _get_maintenance_api().service
    if _POSITION_HISTORY_API is None:
        with _MAINTENANCE_API_LOCK:
            if _POSITION_HISTORY_API is None:
                _POSITION_HISTORY_API = PositionHistoryApi(
                    PositionHistoryService(
                        PositionHistoryLog(DEFAULT_POSITION_HISTORY_FILE), maintenance),
                    module_count_provider=lambda: _read_options()["module_count"],
                )
    return _POSITION_HISTORY_API


def record_stable_observed_positions() -> bool:
    """Append one documentary snapshot after BMS identity changed stably.

    The generated system event is persisted but deliberately not emitted as a
    live MQTT maintenance event. Missing or unstable reads never reach here.
    """
    with _AUTOMATIC_POSITION_LOCK:
        if not history_observation_ready():
            return False
        position_service = _get_position_history_api().service
        current = position_service.current()
        changes = stable_observed_changes(current.positions if current else None)
        if not changes:
            return False
        positions = dict(current.positions) if current else {str(index): None for index in range(1, 7)}
        previous = ", ".join(f"P{position}: {positions[str(position)] or 'unbekannt'}" for position in sorted(changes))
        for position, serial in changes.items():
            # A physical serial can occupy only one position in one snapshot.
            if serial is not None:
                for key, value in list(positions.items()):
                    if value == serial:
                        positions[key] = None
            positions[str(position)] = serial
        removed = {str(position) for position, serial in changes.items() if serial is None}
        semantics = classify_stack_change(current.positions if current else None, positions,
                                          confirmed_empty_positions=removed)
        now = datetime.now(timezone.utc)
        event = _get_maintenance_api().service.create(
            occurred_at=now, category=semantics["category"],
            title=semantics["title"],
            affected_system="Pylontech Stack",
            description="Guardian hat eine wiederholt stabile Seriennummernzuordnung erkannt.",
            previous_state=previous,
            result=", ".join(f"P{position}: {serial or 'leer'}"
                             for position, serial in sorted(changes.items())),
            source={"kind": "guardian_bms_identity", "change_kind": semantics["kind"],
                    "confirmation_reads": 3, "absence_min_seconds": 30},
        )
        position_service.record(
            effective_at=now.isoformat(), maintenance_event_id=event.maintenance_event_id,
            positions=positions,
            expected_latest_snapshot_id=current.position_history_id if current else None,
        )
        return True

DEFAULTS = {
 'serial_port':'auto','baudrate':115200,'poll_interval_seconds':10,'module_count':6,'command':'pwr','command_timeout_seconds':5,
 'mqtt_topic_prefix':'guardian','publish_discovery':True,'warning_cell_delta_mv':30,'critical_cell_delta_mv':80,
 'warning_soc_deviation_pct':10,'critical_soc_deviation_pct':30,'missing_module_is_critical':True,'raw_log':False,'detailed_log':True,
 'trend_window_minutes':60,'trend_min_change_mv':10,'incident_hold_minutes':30,'cell_diagnostics_enabled':True,
 'cell_diagnostics_interval_seconds':60,'cell_diag_low_soc_percent':30,'cell_diag_high_soc_percent':80,'cell_diag_charge_current_a':0.8,
 'cell_diag_discharge_current_a':0.8,'cell_diag_min_phase_samples':30,'cell_diag_confidence_medium_samples':120,
 'cell_diag_confidence_high_samples':600,'cell_diag_observe_deviation_mv':10,'cell_diag_warning_deviation_mv':20,
 'cell_diag_critical_deviation_mv':40,'cell_diag_history_max_samples':8640,'bms_stat_interval_seconds':3600,
 'cell_diag_discharge_observe_deviation_mv':10,'cell_diag_discharge_warning_deviation_mv':20,'cell_diag_discharge_critical_deviation_mv':40,
 'cell_diag_low_observe_deviation_mv':10,'cell_diag_low_warning_deviation_mv':20,'cell_diag_low_critical_deviation_mv':40,
 'cell_diag_charge_observe_deviation_mv':10,'cell_diag_charge_warning_deviation_mv':20,'cell_diag_charge_critical_deviation_mv':40,
 'cell_diag_high_observe_deviation_mv':10,'cell_diag_high_warning_deviation_mv':20,'cell_diag_high_critical_deviation_mv':40,
 'cell_diag_trend_min_days':3,'cell_diag_trend_min_rank_change':0.5,'cell_diag_trend_min_deviation_change_mv':2,
 'cell_diag_resistance_min_delta_current_a':5,'cell_diag_resistance_max_step_seconds':90,'cell_diag_resistance_min_events':3,
 'cell_diag_resistance_window_samples':2,'cell_diag_resistance_max_current_span_a':0.5,
 'cell_diag_resistance_max_relative_mad':0.25,'cell_diag_quality_max_temperature_change_c':2,
 'cell_diag_sequence_max_gap_seconds':120,'cell_diag_sequence_min_samples':10,'cell_diag_sequence_min_duration_seconds':600,
 'cell_diag_sequence_min_charge_ah':0.2,'cell_diag_rest_max_current_a':0.3,'cell_diag_rest_min_duration_seconds':900,
 'cell_diag_sequence_min_segments':2,
 'cell_diag_balancing_min_active_samples':3,'cell_diag_ica_min_samples':60,'cell_diag_ica_max_current_cv':0.1,
 'cell_diag_ica_min_voltage_steps':20,
 'cell_diag_maintenance_context_window_hours':72,
 'cell_diag_relative_trend_change_percent':20,
 'cell_diag_aggregate_retention_days':730,'cell_diag_capacity_boundary_fraction':0.9,
 'cell_diag_capacity_max_crossing_mad_fraction':0.05,'cell_diag_curve_grid_points':21,
 'cell_diag_curve_max_rms_mad_mv':5,
}

# group, label, unit, min, max, step, consequence, level
META = {
 'module_count':('Anlage','Installierte Batteriemodule','Module',1,6,1,'Ändert die Soll-Topologie. Nur Module 1 bis zur eingestellten Anzahl werden erwartet; höhere Modulnummern dürfen keine Missing-/Unavailable-Warnung erzeugen.','normal'),
 'poll_interval_seconds':('Anlage','BMS-Abfrageintervall','s',5,3600,1,'Kleinere Werte erhöhen Aktualität und serielle Last; größere Werte verzögern Status-, Alarm- und Trendreaktionen.','normal'),
 'warning_cell_delta_mv':('Bewertungsgrenzen','Stack-Warnschwelle Zellspreizung','mV',1,1000,1,'Niedrigere Werte erzeugen früher Warnungen aus der allgemeinen Zellspreizung; höhere Werte machen diese Bewertung toleranter.','normal'),
 'critical_cell_delta_mv':('Bewertungsgrenzen','Stack-Kritischschwelle Zellspreizung','mV',1,1000,1,'Niedrigere Werte führen früher zu kritischen Alarmen; muss oberhalb der Warnschwelle liegen.','normal'),
 'warning_soc_deviation_pct':('Bewertungsgrenzen','SOC-Abweichung Warnung','%',1,100,1,'Bestimmt, ab welcher Modul-SOC-Abweichung Guardian warnt.','normal'),
 'critical_soc_deviation_pct':('Bewertungsgrenzen','SOC-Abweichung kritisch','%',1,100,1,'Bestimmt, ab welcher Modul-SOC-Abweichung Guardian kritisch bewertet; muss oberhalb der Warnschwelle liegen.','normal'),
 'missing_module_is_critical':('Bewertungsgrenzen','Fehlendes erwartetes Modul kritisch','bool',None,None,None,'Aktiviert: fehlende konfigurierte Module werden kritisch bewertet. Deaktiviert: die Abweichung bleibt relevant, eskaliert aber nicht kritisch.','normal'),
 'cell_diagnostics_enabled':('Zelldiagnostik','Zelldiagnostik aktiv','bool',None,None,None,'Deaktivieren stoppt neue Zell-Samples und phasenbezogene Evidenz; vorhandene History bleibt erhalten.','normal'),
 'cell_diagnostics_interval_seconds':('Zelldiagnostik','Zell-Diagnoseintervall','s',10,3600,1,'Kleinere Werte liefern mehr Samples und schnellere Evidenz, erhöhen aber serielle Last und Datenmenge.','normal'),
 'cell_diag_history_max_samples':('History & Datenerfassung','Maximale Diagnose-Samples','Samples',100,100000,100,'Begrenzt den Diagnose-Ringpuffer. Ein kleinerer Wert verkürzt die verfügbare Evidenzbasis; Roh-History-Dateien werden dadurch nicht rückwirkend umgeschrieben.','normal'),
 'bms_stat_interval_seconds':('History & Datenerfassung','BMS-Statistikintervall','s',60,86400,60,'Kleinere Werte lesen BMS-Statistik häufiger; größere Werte reduzieren Last, aktualisieren Herstellerwerte aber seltener.','normal'),
 'trend_window_minutes':('History & Datenerfassung','Trendfenster','min',10,1440,10,'Ändert den Zeitraum für Zellspreizungs-Trends. Kürzer reagiert schneller, länger glättet stärker.','normal'),
 'trend_min_change_mv':('History & Datenerfassung','Minimale Trendänderung','mV',1,500,1,'Bestimmt, ab welcher Änderung ein Trend als steigend/fallend statt stabil gilt.','normal'),
 'incident_hold_minutes':('History & Datenerfassung','Incident-Haltezeit','min',1,1440,1,'Bestimmt, wie lange ein Incident nach dem letzten Alarm offen bleibt.','normal'),
 'cell_diag_low_soc_percent':('Phasenerkennung','Low-SOC-Grenze','%',0,50,1,'Eine höhere Grenze ordnet mehr Samples der Low-SOC-Phase zu und verändert deren Evidenz und Ranking.','normal'),
 'cell_diag_high_soc_percent':('Phasenerkennung','High-SOC-Grenze','%',50,100,1,'Eine niedrigere Grenze ordnet mehr Samples der High-SOC-Phase zu und verändert deren Evidenz und Ranking.','normal'),
 'cell_diag_discharge_current_a':('Phasenerkennung','Entlade-Stromgrenze','A',0.1,20,0.1,'Eine niedrigere Schwelle klassifiziert mehr Betriebszustände als Entladung; eine höhere verlangt stärkere Entladung.','normal'),
 'cell_diag_charge_current_a':('Phasenerkennung','Lade-Stromgrenze','A',0.1,20,0.1,'Eine niedrigere Schwelle klassifiziert mehr Betriebszustände als Ladung; eine höhere verlangt stärkere Ladung.','normal'),
 'cell_diag_min_phase_samples':('Zelldiagnostik','Mindest-Samples pro Phase','Samples',5,10000,1,'Höhere Werte verzögern die erste belastbare Phasenbewertung, erhöhen aber die Mindest-Evidenz.','normal'),
 'cell_diag_confidence_medium_samples':('Zelldiagnostik','Confidence MEDIUM ab','Samples',10,100000,10,'Höhere Werte verzögern MEDIUM-Confidence. Muss oberhalb der Mindest-Samples und unterhalb HIGH liegen.','normal'),
 'cell_diag_confidence_high_samples':('Zelldiagnostik','Confidence HIGH ab','Samples',10,100000,10,'Höhere Werte verlangen mehr Evidenz für HIGH-Confidence. Muss oberhalb MEDIUM liegen.','normal'),
 'cell_diag_high_observe_deviation_mv':('Bewertungsgrenzen','High-SOC · Beobachten','mV',1,100,1,'Niedriger = empfindlichere High-SOC-Beobachtung. Muss unter Warnung und Kritisch liegen.','normal'),
 'cell_diag_high_warning_deviation_mv':('Bewertungsgrenzen','High-SOC · Warnung','mV',1,200,1,'Niedriger = frühere High-SOC-Warnung.','normal'),
 'cell_diag_high_critical_deviation_mv':('Bewertungsgrenzen','High-SOC · Kritisch','mV',1,500,1,'Niedriger = frühere kritische High-SOC-Bewertung.','normal'),
 'cell_diag_discharge_observe_deviation_mv':('Bewertungsgrenzen','Entladung · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Beobachtung während Entladung.','normal'),
 'cell_diag_discharge_warning_deviation_mv':('Bewertungsgrenzen','Entladung · Warnung','mV',1,200,1,'Niedriger = frühere Warnung während Entladung.','normal'),
 'cell_diag_discharge_critical_deviation_mv':('Bewertungsgrenzen','Entladung · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Bewertung während Entladung.','normal'),
 'cell_diag_low_observe_deviation_mv':('Bewertungsgrenzen','Low-SOC · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Low-SOC-Beobachtung.','normal'),
 'cell_diag_low_warning_deviation_mv':('Bewertungsgrenzen','Low-SOC · Warnung','mV',1,200,1,'Niedriger = frühere Low-SOC-Warnung.','normal'),
 'cell_diag_low_critical_deviation_mv':('Bewertungsgrenzen','Low-SOC · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Low-SOC-Bewertung.','normal'),
 'cell_diag_charge_observe_deviation_mv':('Bewertungsgrenzen','Ladung · Beobachten','mV',1,100,1,'Niedriger = empfindlichere Beobachtung während Ladung.','normal'),
 'cell_diag_charge_warning_deviation_mv':('Bewertungsgrenzen','Ladung · Warnung','mV',1,200,1,'Niedriger = frühere Warnung während Ladung.','normal'),
 'cell_diag_charge_critical_deviation_mv':('Bewertungsgrenzen','Ladung · Kritisch','mV',1,500,1,'Niedriger = frühere kritische Bewertung während Ladung.','normal'),
 'serial_port':('Erweitert / System','Serielle Schnittstelle','Text',None,None,None,'Falsche Auswahl unterbricht die BMS-Kommunikation. „auto“ nutzt die automatische Erkennung.','advanced'),
 'baudrate':('Erweitert / System','Baudrate','Bd',None,None,None,'Eine falsche Baudrate verhindert die Kommunikation mit dem BMS.','advanced'),
 'command':('Erweitert / System','Pylontech Poll-Kommando','Text',None,None,None,'Änderung kann Parser und Datenerfassung vollständig außer Funktion setzen.','advanced'),
 'command_timeout_seconds':('Erweitert / System','Kommando-Timeout','s',1,30,1,'Zu kurz kann gültige Antworten abbrechen; zu lang verzögert Fehlererkennung.','advanced'),
 'mqtt_topic_prefix':('Erweitert / System','MQTT Topic Prefix','Text',None,None,None,'Änderung verschiebt MQTT-Themen und kann bestehende Home-Assistant-Entities entkoppeln.','advanced'),
 'publish_discovery':('Erweitert / System','MQTT Discovery veröffentlichen','bool',None,None,None,'Deaktivieren verhindert neue Discovery-Publikationen; bestehende Entities können erhalten bleiben.','advanced'),
 'raw_log':('Erweitert / System','Raw-PWR-Log','bool',None,None,None,'Aktivieren schreibt die letzte rohe PWR-Antwort und erhöht Schreibzugriffe.','advanced'),
 'detailed_log':('Erweitert / System','Detailliertes Laufzeitlog','bool',None,None,None,'Aktivieren erhöht Logumfang; Diagnoseberechnung selbst bleibt unverändert.','advanced'),
 'cell_diag_observe_deviation_mv':('Erweitert / System','Legacy/Fallback · Beobachten','mV',1,100,1,'Fallback-Grenze für Diagnosepfade ohne phasenspezifischen Wert. Änderungen können Bewertungen beeinflussen.','advanced'),
 'cell_diag_warning_deviation_mv':('Erweitert / System','Legacy/Fallback · Warnung','mV',1,200,1,'Fallback-Warnschwelle. Muss über Beobachten liegen.','advanced'),
 'cell_diag_critical_deviation_mv':('Erweitert / System','Legacy/Fallback · Kritisch','mV',1,500,1,'Fallback-Kritischschwelle. Muss über Warnung liegen.','advanced'),
 'cell_diag_trend_min_days':('Erweitert / System','Experimentell · Trend Mindesttage','Tage',2,90,1,'Quality Gate: weniger Tage ergeben unklar, nicht automatisch einen Alarm.','advanced'),
 'cell_diag_trend_min_rank_change':('Erweitert / System','Experimentell · relevante Rangänderung','Rang/Tag',0.1,5,0.1,'Kennzeichnet nur eine robuste Trendrichtung; ändert den Zellstatus nicht.','advanced'),
 'cell_diag_trend_min_deviation_change_mv':('Erweitert / System','Experimentell · relevante Abweichungsänderung','mV/Tag',0.1,50,0.1,'Kennzeichnet nur eine robuste Trendrichtung; ändert den Zellstatus nicht.','advanced'),
 'cell_diag_resistance_min_delta_current_a':('Erweitert / System','Widerstand · Mindest-Stromsprung','A',0.5,100,0.5,'Quality Gate für natürliche Laständerungen; keine Alarmschwelle.','advanced'),
 'cell_diag_resistance_max_step_seconds':('Erweitert / System','Widerstand · maximales Sprungfenster','s',1,3600,1,'Größere Fenster schwächen die zeitliche Zuordnung von Strom- und Spannungsänderung.','advanced'),
 'cell_diag_resistance_min_events':('Erweitert / System','Widerstand · Mindestereignisse','Ereignisse',2,1000,1,'Weniger Ereignisse bleiben nicht bewertbar.','advanced'),
 'cell_diag_resistance_window_samples':('Erweitert / System','Widerstand · Samples je Vergleichsfenster','Messpunkte',1,20,1,'Bestimmt die stabilen Vor-/Nachfenster eines Stromsprungs.','advanced'),
 'cell_diag_resistance_max_current_span_a':('Erweitert / System','Widerstand · maximale Stromspanne im Fenster','A',0.01,20,0.01,'Verwirft instabile Vor-/Nachfenster.','advanced'),
 'cell_diag_resistance_max_relative_mad':('Erweitert / System','Widerstand · maximale relative Streuung','Index',0.01,2,0.01,'Quality Gate für Reproduzierbarkeit; keine Defektschwelle.','advanced'),
 'cell_diag_quality_max_temperature_change_c':('Erweitert / System','Diagnose · maximale Temperaturspanne','°C',0.1,20,0.1,'Verwirft Widerstands- und Ruhefenster mit zu großer Temperaturänderung.','advanced'),
 'cell_diag_sequence_max_gap_seconds':('Erweitert / System','Sequenz · maximale Messlücke','s',10,3600,10,'Größere Lücken erlauben weniger zusammenhängende Kurven.','advanced'),
 'cell_diag_sequence_min_samples':('Erweitert / System','Sequenz · Mindestmesspunkte','Messpunkte',3,10000,1,'Quality Gate für Capacity-/Kurvenevidenz.','advanced'),
 'cell_diag_sequence_min_duration_seconds':('Erweitert / System','Sequenz · Mindestdauer','s',60,86400,60,'Quality Gate für Capacity-/Kurvenevidenz.','advanced'),
 'cell_diag_sequence_min_charge_ah':('Erweitert / System','Sequenz · Mindestladung','Ah',0.01,100,0.01,'Quality Gate für relative Sequenzevidenz; keine Kapazitätsangabe.','advanced'),
 'cell_diag_sequence_min_segments':('Erweitert / System','Sequenz · Mindestanzahl','Sequenzen',2,100,1,'Verlangt Wiederholung, bevor Capacity-/Kurvenevidenz bewertbar wird.','advanced'),
 'cell_diag_rest_max_current_a':('Erweitert / System','Ruhe · maximaler Strom','A',0.01,10,0.01,'Definiert nur die Erkennung einer Ruhephase.','advanced'),
 'cell_diag_rest_min_duration_seconds':('Erweitert / System','Ruhe · Mindestdauer','s',60,86400,60,'Kürzere Ruheabschnitte bleiben nicht bewertbar.','advanced'),
 'cell_diag_balancing_min_active_samples':('Erweitert / System','Balancing · aktive Mindestsamples','Messpunkte',1,10000,1,'Verlangt real gemeldeten BMS-Balancing-Status; erfindet keine Gelegenheit.','advanced'),
 'cell_diag_ica_min_samples':('Erweitert / System','ICA/DVA Readiness · Mindestmesspunkte','Messpunkte',10,100000,10,'Prüft nur Datenbereitschaft; aktiviert keine ICA/DVA-Auswertung.','advanced'),
 'cell_diag_ica_max_current_cv':('Erweitert / System','ICA/DVA Readiness · maximale Stromstreuung','CV',0.01,1,0.01,'Quality Gate für einen gleichmäßigen Ladeabschnitt.','advanced'),
 'cell_diag_ica_min_voltage_steps':('Erweitert / System','ICA/DVA Readiness · Spannungsstufen','Stufen',5,1000,1,'Quality Gate für reale Spannungsauflösung ohne synthetische Glättung.','advanced'),
 'cell_diag_maintenance_context_window_hours':('Erweitert / System','Maintenance-Kontext · Vor-/Nachfenster','h',1,2160,1,'Bestimmt das symmetrische Korrelationsfenster; begründet keine Kausalität.','advanced'),
 'cell_diag_relative_trend_change_percent':('Erweitert / System','Experimentell · relative Trendänderung','%',1,200,1,'Quality Gate für eine robuste Trendrichtung; keine Alarm- oder Defektschwelle.','advanced'),
 'cell_diag_aggregate_retention_days':('History & Datenerfassung','Diagnoseaggregate · Aufbewahrung','Tage',30,3650,1,'Begrenzt ausschließlich abgeleitete Tagesaggregate; Rohhistorien bleiben unverändert.','advanced'),
 'cell_diag_capacity_boundary_fraction':('Erweitert / System','Capacity · Sequenzfortschritt für Grenzbereich','Anteil',0.6,0.99,0.01,'Definiert den relativen gemeinsamen Sequenzbereich; keine Zellkapazitäts- oder Alarmschwelle.','advanced'),
 'cell_diag_capacity_max_crossing_mad_fraction':('Erweitert / System','Capacity · maximale Crossing-Streuung','Q-Anteil',0.001,0.5,0.001,'Quality Gate für reproduzierbare relative Grenzbereichsreihenfolge.','advanced'),
 'cell_diag_curve_grid_points':('Erweitert / System','Kurvenanalyse · Q-Rasterpunkte','Punkte',11,101,2,'Bestimmt das gemeinsame Interpolationsraster ohne Extrapolation.','advanced'),
 'cell_diag_curve_max_rms_mad_mv':('Erweitert / System','Kurvenanalyse · maximale RMS-Streuung','mV',0.1,100,0.1,'Quality Gate für reproduzierbare relative Kurvenabweichung; keine Alarmschwelle.','advanced'),
}

GROUP_ORDER=['Anlage','Zelldiagnostik','Phasenerkennung','Bewertungsgrenzen','History & Datenerfassung','Erweitert / System']
PHASE_KEYS=[
 ('High-SOC','cell_diag_high_observe_deviation_mv','cell_diag_high_warning_deviation_mv','cell_diag_high_critical_deviation_mv'),
 ('Entladung','cell_diag_discharge_observe_deviation_mv','cell_diag_discharge_warning_deviation_mv','cell_diag_discharge_critical_deviation_mv'),
 ('Low-SOC','cell_diag_low_observe_deviation_mv','cell_diag_low_warning_deviation_mv','cell_diag_low_critical_deviation_mv'),
 ('Ladung','cell_diag_charge_observe_deviation_mv','cell_diag_charge_warning_deviation_mv','cell_diag_charge_critical_deviation_mv'),
]

def _read_options():
    data=json.loads(OPTIONS_FILE.read_text(encoding='utf-8'))
    return {**DEFAULTS, **data}

def _last_record():
    if not CONFIG_HISTORY_FILE.exists(): return {}
    try:
        lines=[x for x in CONFIG_HISTORY_FILE.read_text(encoding='utf-8').splitlines() if x.strip()]
        return json.loads(lines[-1]) if lines else {}
    except Exception: return {}

def _api(method,path,payload=None):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(SUPERVISOR+path,data=body,method=method,headers={'Authorization':'Bearer '+TOKEN,'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=10) as r: return json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode(errors='replace') or str(e))

def validate(cfg):
    errors=[]
    for key,meta in META.items():
        if key not in cfg: errors.append(f'{key}: fehlt'); continue
        unit,lo,hi=meta[2],meta[3],meta[4]
        if unit=='bool' and not isinstance(cfg[key],bool): errors.append(f'{meta[1]}: muss boolesch sein')
        if lo is not None:
            try: v=float(cfg[key])
            except Exception: errors.append(f'{meta[1]}: ungültige Zahl'); continue
            if v<lo or v>hi: errors.append(f'{meta[1]}: zulässig {lo} bis {hi} {unit}')
    if cfg.get('warning_cell_delta_mv',0)>=cfg.get('critical_cell_delta_mv',0): errors.append('Stack-Zellspreizung: Warnung muss kleiner als Kritisch sein.')
    if cfg.get('warning_soc_deviation_pct',0)>=cfg.get('critical_soc_deviation_pct',0): errors.append('SOC-Abweichung: Warnung muss kleiner als Kritisch sein.')
    if cfg.get('cell_diag_low_soc_percent',0)>=cfg.get('cell_diag_high_soc_percent',100): errors.append('Low-SOC-Grenze muss kleiner als High-SOC-Grenze sein.')
    if not (cfg.get('cell_diag_min_phase_samples',0) < cfg.get('cell_diag_confidence_medium_samples',0) < cfg.get('cell_diag_confidence_high_samples',0)):
        errors.append('Confidence-Reihenfolge muss Mindest-Samples < MEDIUM < HIGH sein.')
    for name,o,w,c in PHASE_KEYS+[('Fallback','cell_diag_observe_deviation_mv','cell_diag_warning_deviation_mv','cell_diag_critical_deviation_mv')]:
        if not (cfg.get(o,0)<cfg.get(w,0)<cfg.get(c,0)): errors.append(f'{name}: Beobachten < Warnung < Kritisch ist erforderlich.')
    return errors

def _config_html(maintenance_path='maintenance', timeline_path='timeline', history_path='history'):
    page = r'''<!doctype html><html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Guardian Konfiguration</title>
<style>:root{color-scheme:light dark;--b:#1976d2;--warn:#ef6c00}body{font:14px system-ui;margin:0;background:var(--primary-background-color,#fafafa);color:var(--primary-text-color,#222)}header{padding:18px 22px;background:#0d47a1;color:white}header nav{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}header nav a{color:white;text-decoration:none;padding:8px 12px;border:1px solid #ffffff66;border-radius:8px}header nav a.active{background:white;color:#0d47a1}main{max-width:1100px;margin:auto;padding:16px}.intro,.group{background:var(--card-background-color,#fff);border-radius:12px;padding:16px;margin:12px 0;box-shadow:0 1px 4px #0002}.group h2{margin-top:0}.row{display:grid;grid-template-columns:minmax(240px,1fr) minmax(160px,260px);gap:12px;padding:12px 0;border-top:1px solid #8883}.label{font-weight:650}.meta{font-size:12px;opacity:.72;margin-top:3px}.impact{font-size:12px;margin-top:6px;padding:7px 9px;border-left:3px solid var(--warn);background:#ff980012}input,select{width:100%;box-sizing:border-box;padding:9px;border:1px solid #8888;border-radius:7px;background:transparent;color:inherit}.advanced{border-left:4px solid #777}.actions{position:sticky;bottom:0;background:var(--card-background-color,#fff);padding:12px 16px;border-radius:12px;box-shadow:0 -2px 8px #0002;display:flex;gap:10px;align-items:center}button{padding:10px 16px;border:0;border-radius:8px;cursor:pointer}button.primary{background:var(--b);color:white}.status{margin-left:auto;font-weight:600}.changed{outline:2px solid #ff980088}.sys{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px}.sys div{padding:8px;background:#8881;border-radius:7px}@media(max-width:650px){.row{grid-template-columns:1fr}.actions{flex-wrap:wrap}.status{width:100%;margin:0}}</style></head>
<body>__HEADER__<main>
<div class="intro"><b>Wirkung von Änderungen</b><p>Änderungen verändern die zukünftige Erfassung und/oder Bewertung. Historische Rohdaten werden nicht umgeschrieben. Nach erfolgreichem Übernehmen wird Guardian neu gestartet; diagnostisch relevante Änderungen erzeugen einen neuen Provenienz-Eintrag.</p><div class="sys" id="sys"></div></div>
<form id="form"></form><div class="actions"><button type="button" onclick="resetDefaults()">Auf Standard zurücksetzen</button><button type="button" onclick="reloadCfg()">Änderungen verwerfen</button><button class="primary" type="button" onclick="save()">Validieren & Übernehmen</button><span class="status" id="status"></span></div></main>
<script>let model,current={};const esc=s=>String(s).replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){let r=await fetch('api/config');model=await r.json();current=structuredClone(model.current);render();}
function render(){document.getElementById('sys').innerHTML=`<div><b>Guardian</b><br>${esc(model.guardian_version)}</div><div><b>Diagnostic Engine</b><br>${esc(model.engine_version)}</div><div><b>Config-ID</b><br>${esc(model.config_id||'—')}</div>`;let out='';for(const g of model.groups){out+=`<section class="group ${g.startsWith('Erweitert')?'advanced':''}"><h2>${esc(g)}</h2>`;for(const k of model.order.filter(k=>model.meta[k].group===g)){const m=model.meta[k],v=current[k],d=model.defaults[k];let ctl;if(m.unit==='bool')ctl=`<select data-k="${k}" onchange="chg(this)"><option value="true" ${v===true?'selected':''}>Aktiv</option><option value="false" ${v===false?'selected':''}>Inaktiv</option></select>`;else ctl=`<input data-k="${k}" value="${esc(v)}" ${m.min!=null?`type="number" min="${m.min}" max="${m.max}" step="${m.step}"`:'type="text"'} oninput="chg(this)">`;out+=`<div class="row"><div><div class="label">${esc(m.label)}</div><div class="meta">Standard: ${esc(d)}${m.unit&&m.unit!=='Text'&&m.unit!=='bool'?' '+esc(m.unit):''}${m.min!=null?' · Bereich: '+m.min+'–'+m.max+' '+esc(m.unit):''}</div><div class="impact">⚠ ${esc(m.consequence)}</div></div><div>${ctl}</div></div>`;}out+='</section>';}document.getElementById('form').innerHTML=out;document.getElementById('status').textContent='';}
function chg(el){const k=el.dataset.k,m=model.meta[k];let v=el.value;if(m.unit==='bool')v=v==='true';else if(m.min!=null)v=Number(v);current[k]=v;el.classList.toggle('changed',JSON.stringify(v)!==JSON.stringify(model.current[k]));}
function resetDefaults(){current=structuredClone(model.defaults);render();document.getElementById('status').textContent='Standardwerte vorgemerkt – noch nicht gespeichert.';}
function reloadCfg(){current=structuredClone(model.current);render();}
async function save(){const s=document.getElementById('status');s.textContent='Validierung…';let r=await fetch('api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(current)}),j=await r.json();if(!r.ok){s.textContent='Nicht übernommen: '+(j.errors||[j.error]).join(' | ');return} s.textContent=j.changed?'Übernommen. Guardian startet neu…':'Keine Änderung – nichts gespeichert.';if(j.changed)setTimeout(()=>location.reload(),7000);}
load().catch(e=>document.getElementById('status').textContent='Fehler: '+e);</script></body></html>'''
    escaped_maintenance=maintenance_path.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    escaped_timeline=timeline_path.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    escaped_history=history_path.replace('&','&amp;').replace('"','&quot;').replace('<','&lt;').replace('>','&gt;')
    header = render_guardian_header(
        active="configuration",
        paths={"modules": "./", "configuration": "configuration",
               "maintenance": maintenance_path, "timeline": timeline_path,
               "history": history_path},
        subtitle="Diagnoseparameter kontrolliert, validiert und nachvollziehbar ändern",
    )
    return page.replace('__HEADER__', header)

class Handler(BaseHTTPRequestHandler):
    def _ingress_allowed(self):
        return self.client_address[0] == '172.30.32.2'
    def log_message(self,*_): pass
    def _send(self,code,body,ctype='application/json',headers=None):
        raw=body.encode() if isinstance(body,str) else json.dumps(body,ensure_ascii=False).encode()
        self.send_response(code); self.send_header('Content-Type',ctype+'; charset=utf-8'); self.send_header('Content-Length',str(len(raw)))
        for key,value in (headers or {}).items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(raw)
    def _is_maintenance_api(self):
        return API_ROUTE in self.path.split('?',1)[0]
    def _is_timeline_api(self):
        return TIMELINE_API_ROUTE in self.path.split('?',1)[0]
    def _is_history_api(self):
        return HISTORY_API_ROUTE in self.path.split('?',1)[0]
    def _is_position_history_api(self):
        return POSITION_HISTORY_API_ROUTE in self.path.split('?',1)[0]
    def _ingress_base(self):
        header=self.headers.get('X-Ingress-Path','').rstrip('/')
        if header: return header
        path=urlsplit(self.path).path.rstrip('/')
        for suffix in ('/maintenance','/timeline','/history','/module-information','/configuration'):
            if path.endswith(suffix): return path[:-len(suffix)]
        return path
    def _is_maintenance_ui(self):
        return urlsplit(self.path).path.rstrip('/').endswith('/maintenance')
    def _is_timeline_ui(self):
        return urlsplit(self.path).path.rstrip('/').endswith('/timeline')
    def _is_history_ui(self):
        return urlsplit(self.path).path.rstrip('/').endswith('/history')
    def _is_module_information_ui(self):
        return urlsplit(self.path).path.rstrip('/').endswith('/module-information')
    def _is_configuration_ui(self):
        return urlsplit(self.path).path.rstrip('/').endswith('/configuration')
    def _maintenance_request(self,method):
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError:
            self._send(400,error_json('invalid_request','Content-Length must be an integer')); return
        if length<0:
            self._send(400,error_json('invalid_request','Content-Length must not be negative')); return
        if length>MAX_REQUEST_BODY_BYTES:
            self.close_connection=True
            self._send(413,error_json('request_too_large',f'Request body exceeds {MAX_REQUEST_BODY_BYTES} bytes')); return
        body=self.rfile.read(length) if length else b''
        response=_get_maintenance_api().handle(method,self.path,dict(self.headers.items()),body)
        self._send(response.status,response.body,headers=response.headers)
    def do_GET(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self._is_history_api():
            response=_get_history_api().handle('GET',self.path)
            self._send(response.status,response.body,headers=response.headers); return
        if self.path.rstrip('/').endswith('/api/rs485/status'):
            payload = (_RS485_STATUS_PROVIDER() if _RS485_STATUS_PROVIDER else
                       {"status": {"state": "disabled"}, "management": {}, "history": {}})
            # Raw protocol frames are intentionally never exposed through ingress.
            management = {str(adr): {key: value for key, value in item.items()
                          if key != "raw_frame"} for adr, item in payload.get("management", {}).items()}
            self._send(200, {**payload, "management": management}); return
        if self._is_position_history_api():
            response=_get_position_history_api().handle('GET',self.path,dict(self.headers.items()))
            self._send(response.status,response.body,headers=response.headers); return
        if self._is_timeline_api():
            response=_get_timeline_api().handle('GET',self.path)
            self._send(response.status,response.body,headers=response.headers); return
        if self._is_maintenance_api(): self._maintenance_request('GET'); return
        if self._is_timeline_ui():
            base=self._ingress_base(); self._send(200,render_timeline_html(configuration_path=(base+'/configuration') or '/configuration',maintenance_path=(base+'/maintenance') or '/maintenance',history_path=(base+'/history') or '/history'),'text/html'); return
        if self._is_history_ui():
            base=self._ingress_base(); self._send(200,render_history_html(configuration_path=(base+'/configuration') or '/configuration',maintenance_path=(base+'/maintenance') or '/maintenance',timeline_path=(base+'/timeline') or '/timeline'),'text/html'); return
        if self._is_module_information_ui():
            base=self._ingress_base(); self._send(200,render_module_information_html(configuration_path=(base+'/configuration') or '/configuration',maintenance_path=(base+'/maintenance') or '/maintenance'),'text/html'); return
        if self._is_maintenance_ui():
            base=self._ingress_base(); self._send(200,render_maintenance_html(configuration_path=(base+'/configuration') or '/configuration',timeline_path=(base+'/timeline') or '/timeline',history_path=(base+'/history') or '/history'),'text/html'); return
        if self._is_configuration_ui():
            base=self._ingress_base(); self._send(200,_config_html((base+'/maintenance') or '/maintenance',(base+'/timeline') or '/timeline',(base+'/history') or '/history'),'text/html'); return
        if self.path.rstrip('/').endswith('/api/config'):
            rec=_last_record(); meta={k:{'group':v[0],'label':v[1],'unit':v[2],'min':v[3],'max':v[4],'step':v[5],'consequence':v[6],'level':v[7]} for k,v in META.items()}
            self._send(200,{'current':_read_options(),'defaults':DEFAULTS,'meta':meta,'groups':GROUP_ORDER,'order':list(META),'config_id':rec.get('config_id'),'guardian_version':rec.get('guardian_version',GUARDIAN_VERSION),'engine_version':rec.get('diagnostic_engine_version',DIAGNOSTIC_ENGINE_VERSION)}); return
        base=self._ingress_base(); self._send(200,render_module_information_html(configuration_path=(base+'/configuration') or '/configuration',maintenance_path=(base+'/maintenance') or '/maintenance'),'text/html')
    def do_POST(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self._is_position_history_api():
            try: length=int(self.headers.get('Content-Length','0'))
            except ValueError: self._send(400,error_json('invalid_request','Content-Length must be an integer')); return
            body=self.rfile.read(length) if length else b''
            response=_get_position_history_api().handle('POST',self.path,dict(self.headers.items()),body)
            self._send(response.status,response.body,headers=response.headers); return
        if self._is_maintenance_api(): self._maintenance_request('POST'); return
        if not self.path.rstrip('/').endswith('/api/config'): self._send(404,{'error':'not found'}); return
        try:
            n=int(self.headers.get('Content-Length','0')); proposed=json.loads(self.rfile.read(n) or b'{}')
            cfg={**_read_options(),**{k:proposed[k] for k in DEFAULTS if k in proposed}}
            errors=validate(cfg)
            if errors: self._send(400,{'errors':errors}); return
            if cfg==_read_options(): self._send(200,{'changed':False}); return
            validated=_api('POST','/addons/self/options/validate',cfg)
            data=validated.get('data',validated)
            if data.get('valid') is not True: self._send(400,{'errors':[data.get('message','Supervisor-Validierung fehlgeschlagen')]}); return
            saved=_api('POST','/addons/self/options',{'options':cfg})
            if isinstance(saved,dict) and saved.get('result') not in (None,'ok'):
                self._send(502,{'error':saved.get('message','Supervisor hat die Konfiguration nicht übernommen.')}); return
            info=_api('GET','/addons/self/info')
            info_data=info.get('data',info) if isinstance(info,dict) else {}
            stored=info_data.get('options') if isinstance(info_data,dict) else None
            if stored is not None and stored != cfg:
                self._send(502,{'error':'Supervisor-Bestätigung stimmt nicht mit der angeforderten Konfiguration überein.'}); return
            self._send(200,{'changed':True})
            def restart():
                time.sleep(1.0)
                try:_api('POST','/addons/self/restart',{})
                except Exception: pass
            threading.Thread(target=restart,daemon=True).start()
        except Exception as exc: self._send(500,{'error':str(exc)})
    def do_PATCH(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self._is_maintenance_api(): self._maintenance_request('PATCH'); return
        self._send(404,{'error':'not found'})
    def do_PUT(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self._is_maintenance_api(): self._maintenance_request('PUT'); return
        self._send(404,{'error':'not found'})
    def do_DELETE(self):
        if not self._ingress_allowed(): self._send(403,{'error':'Ingress only'}); return
        if self._is_maintenance_api(): self._maintenance_request('DELETE'); return
        self._send(404,{'error':'not found'})

def start_config_server(port=8099, maintenance_live_publisher=None, bind_host="0.0.0.0"):
    configure_maintenance_live_publisher(maintenance_live_publisher)
    server=ThreadingHTTPServer((bind_host,port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True,name='guardian-config-ui').start()
    return server
