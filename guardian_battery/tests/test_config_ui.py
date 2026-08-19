import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import config_ui
from config_ui import DEFAULTS, Handler, validate

def test_defaults_valid():
    assert validate(dict(DEFAULTS)) == []

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
