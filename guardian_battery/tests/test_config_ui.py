import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import config_ui
from config_ui import DEFAULTS, Handler, validate

def test_defaults_valid():
    assert validate(dict(DEFAULTS)) == []


def test_configuration_navigation_has_no_extra_portal_level():
    html = config_ui._config_html()
    assert "Zentrales Funktionsportal" not in html
    assert "Module &amp; Stack" in html
    assert 'href="configuration"' in html
    assert 'href="timeline"' in html
    assert 'href="history"' in html
    assert not hasattr(config_ui, "_portal_html")

def test_module_count_range():
    cfg=dict(DEFAULTS); cfg['module_count']=7
    assert any('Installierte Batteriemodule' in x for x in validate(cfg))

def test_phase_order_validation():
    cfg=dict(DEFAULTS); cfg['cell_diag_high_warning_deviation_mv']=50; cfg['cell_diag_high_critical_deviation_mv']=40
    assert any('High-SOC' in x for x in validate(cfg))

def test_confidence_order_validation():
    cfg=dict(DEFAULTS); cfg['cell_diag_confidence_medium_samples']=700
    assert any('Confidence-Reihenfolge' in x for x in validate(cfg))


def test_maintenance_api_route_detection_supports_ingress_prefix():
    handler = object.__new__(Handler)
    handler.path = '/ingress/session/api/maintenance/events?limit=10'

    assert handler._is_maintenance_api() is True


def test_maintenance_ui_route_and_dynamic_ingress_base():
    handler = object.__new__(Handler)
    handler.path = '/api/hassio_ingress/dynamic-token/maintenance?event_id=MEV-1'
    handler.headers = {'X-Ingress-Path': '/api/hassio_ingress/dynamic-token'}

    assert handler._is_maintenance_ui() is True
    assert handler._ingress_base() == '/api/hassio_ingress/dynamic-token'


def test_timeline_api_and_ui_routes_support_dynamic_ingress_prefix():
    handler = object.__new__(Handler)
    handler.path = '/api/hassio_ingress/dynamic-token/api/timeline?event_type=maintenance'
    handler.headers = {'X-Ingress-Path': '/api/hassio_ingress/dynamic-token'}
    assert handler._is_timeline_api() is True

    handler.path = '/api/hassio_ingress/dynamic-token/timeline'
    assert handler._is_timeline_ui() is True
    assert handler._ingress_base() == '/api/hassio_ingress/dynamic-token'


def test_history_series_api_and_ui_routes_support_dynamic_ingress_prefix():
    handler = object.__new__(Handler)
    handler.path = '/api/hassio_ingress/dynamic-token/api/history/series?metric=soc'
    handler.headers = {'X-Ingress-Path': '/api/hassio_ingress/dynamic-token'}
    assert handler._is_history_api() is True

    handler.path = '/api/hassio_ingress/dynamic-token/history'
    assert handler._is_history_ui() is True
    assert handler._ingress_base() == '/api/hassio_ingress/dynamic-token'


def test_maintenance_api_is_lazy_and_path_is_injectable_for_tests(tmp_path, monkeypatch):
    target = tmp_path / 'maintenance_events.jsonl'
    monkeypatch.setattr(config_ui, 'DEFAULT_MAINTENANCE_EVENT_FILE', target)
    monkeypatch.setattr(config_ui, '_MAINTENANCE_API', None)

    api = config_ui._get_maintenance_api()

    assert api.service.repository.log.path == target
    assert not target.exists()


def test_runtime_live_publisher_is_injected_into_existing_and_future_api(tmp_path, monkeypatch):
    target = tmp_path / 'maintenance_events.jsonl'
    publisher = object()
    monkeypatch.setattr(config_ui, 'DEFAULT_MAINTENANCE_EVENT_FILE', target)
    monkeypatch.setattr(config_ui, '_MAINTENANCE_API', None)
    monkeypatch.setattr(config_ui, '_MAINTENANCE_LIVE_PUBLISHER', None)

    config_ui.configure_maintenance_live_publisher(publisher)
    api = config_ui._get_maintenance_api()
    assert api.live_publisher is publisher

    replacement = object()
    config_ui.configure_maintenance_live_publisher(replacement)
    assert api.live_publisher is replacement


def test_confirmed_bms_identity_creates_append_only_system_event_and_snapshot(tmp_path, monkeypatch):
    import position_history
    maintenance_path=tmp_path/'maintenance.jsonl'; position_path=tmp_path/'positions.jsonl'
    monkeypatch.setattr(config_ui,'DEFAULT_MAINTENANCE_EVENT_FILE',maintenance_path)
    monkeypatch.setattr(config_ui,'DEFAULT_POSITION_HISTORY_FILE',position_path)
    monkeypatch.setattr(config_ui,'_MAINTENANCE_API',None)
    monkeypatch.setattr(config_ui,'_POSITION_HISTORY_API',None)
    monkeypatch.setattr(position_history,'_OBSERVED_STACK',{})
    monkeypatch.setattr(position_history,'_OBSERVATION_CANDIDATES',{})
    monkeypatch.setattr(position_history,'_PRESENCE_SOURCES',{})
    monkeypatch.setattr(position_history,'_MISSING_CANDIDATES',{})
    monkeypatch.setattr(position_history,'_COMMUNICATION_HEALTHY',None)
    for _ in range(3): position_history.update_observed_stack({2:{'barcode':'SN-A'}})
    assert config_ui.record_stable_observed_positions() is True
    assert config_ui.record_stable_observed_positions() is False
    snapshot=config_ui._get_position_history_api().service.current()
    event=config_ui._get_maintenance_api().service.get(snapshot.maintenance_event_id)
    assert snapshot.positions['2']=='SN-A'
    assert event.source['kind']=='guardian_bms_identity'
    assert event.source['change_kind']=='initial_identification'
    assert event.category=='module_identification'
    assert event.title=='Erstidentifikation der Stackbelegung'
    assert len(position_path.read_text().splitlines())==1


def test_confirmed_removal_and_readd_each_create_one_full_snapshot(tmp_path, monkeypatch):
    import position_history
    maintenance_path = tmp_path / 'maintenance.jsonl'
    position_path = tmp_path / 'positions.jsonl'
    monkeypatch.setattr(config_ui, 'DEFAULT_MAINTENANCE_EVENT_FILE', maintenance_path)
    monkeypatch.setattr(config_ui, 'DEFAULT_POSITION_HISTORY_FILE', position_path)
    monkeypatch.setattr(config_ui, '_MAINTENANCE_API', None)
    monkeypatch.setattr(config_ui, '_POSITION_HISTORY_API', None)
    monkeypatch.setattr(position_history, '_OBSERVED_STACK', {})
    monkeypatch.setattr(position_history, '_OBSERVATION_CANDIDATES', {})
    monkeypatch.setattr(position_history, '_PRESENCE_SOURCES', {})
    monkeypatch.setattr(position_history, '_MISSING_CANDIDATES', {})
    monkeypatch.setattr(position_history, '_COMMUNICATION_HEALTHY', None)

    infos = {1: {'barcode': 'SN-A'}, 2: {'barcode': 'SN-B'}}
    for timestamp in (0, 1, 2):
        position_history.update_observed_stack(
            infos, present_positions={1, 2}, expected_module_count=2,
            observed_at=timestamp)
    assert config_ui.record_stable_observed_positions() is True
    assert config_ui.record_stable_observed_positions() is False

    # A short gap and a global outage are not topology transitions.
    position_history.update_observed_stack(
        infos, present_positions={1}, expected_module_count=2, observed_at=100)
    position_history.update_observed_stack(
        {}, present_positions=set(), communication_healthy=False,
        expected_module_count=2, observed_at=120)
    assert config_ui.record_stable_observed_positions() is False

    for timestamp in (130, 145, 161):
        position_history.update_observed_stack(
            infos, present_positions={1}, expected_module_count=2,
            observed_at=timestamp)
    assert config_ui.record_stable_observed_positions() is True
    assert config_ui.record_stable_observed_positions() is False

    for timestamp in (170, 171, 172):
        position_history.update_observed_stack(
            infos, present_positions={1, 2}, expected_module_count=2,
            observed_at=timestamp)
    assert config_ui.record_stable_observed_positions() is True
    assert config_ui.record_stable_observed_positions() is False

    snapshots = config_ui._get_position_history_api().service.list()
    assert [item.positions for item in snapshots] == [
        {'1': 'SN-A', '2': 'SN-B', '3': None, '4': None, '5': None, '6': None},
        {'1': 'SN-A', '2': None, '3': None, '4': None, '5': None, '6': None},
        {'1': 'SN-A', '2': 'SN-B', '3': None, '4': None, '5': None, '6': None},
    ]
