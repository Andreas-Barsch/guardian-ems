"""One focused regression: read existing products, never rebuild raw sources."""
import time
from types import SimpleNamespace


def test_existing_phases_and_daily_products_wait_and_report_absence(tmp_path, monkeypatch):
    import history_live_priority as priority
    from canonical_phase import CanonicalPhaseStore, SCHEMA_VERSION, SEMANTICS_VERSION, ALGORITHM_VERSION
    from test_research_api import environment
    from test_guardian_diagnostics import write_day
    from prepared_daily_reader import SelectedDailyRepository
    api, _, _ = environment(tmp_path)
    day = '2026-09-11'; start = day + 'T10:00:00+00:00'; end = day + 'T10:10:00+00:00'
    interval = {'canonical_from': start, 'canonical_to': end, 'phase': 'charge',
        'config_id': 'cfg', 'module_position': 4, 'canonical_sample_count': 1,
        'count_timestamps': [1789120800.0]}
    CanonicalPhaseStore(api.paths.canonical_phase).write_day(day, {
        'schema_version': SCHEMA_VERSION, 'semantics_version': SEMANTICS_VERSION,
        'algorithm_version': ALGORITHM_VERSION, 'source_signatures': {},
        'identities': {'S': {'physical_serial': 'SERIAL-M4', 'module_positions': [4],
            'diagnostic_intervals': [interval], 'visual_intervals': [interval],
            'relative_endpoints': [], 'eod_checkpoint': {}}}})
    write_day(api.paths.daily_diagnostics, day, aggregates=[])
    waits = []
    def sleep(_):
        waits.append(True); priority.idle_until(time.monotonic() + 5)
    monkeypatch.setattr(priority, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=sleep))
    original_text = SelectedDailyRepository._text
    read_paths = []
    def text(self, path):
        read_paths.append(path)
        assert day in str(path), 'must not scan other days'
        return original_text(self, path)
    monkeypatch.setattr(SelectedDailyRepository, '_text', text)
    from urllib.parse import urlencode
    targets = [
        'phases?' + urlencode({'physical_serial': 'SERIAL-M4', 'from': start, 'to': end}),
        'daily-diagnostics?date=' + day,
        'diagnostic-evidence?date=' + day,
        'daily-diagnostics?' + urlencode({'from': start, 'to': end}),
    ]
    try:
        for suffix in targets:
            priority.cycle_started()
            response = api.handle('GET', '/api/research/' + suffix)
            assert response.status == 200, response.body
            if suffix.startswith('phases?'):
                assert response.body['data']['available']
                assert response.body['data']['diagnostic_intervals'][0]['phase'] == 'charge'
        assert len(waits) == len(targets) and read_paths
        absent = api.handle('GET', '/api/research/diagnostic-evidence?date=2026-09-12')
        assert absent.status == 503 and absent.body['error']['code'] == 'history_preparation_required'
        invalid = api.handle('GET', '/api/research/daily-diagnostics?date=invalid')
        assert invalid.status == 400 and invalid.body['error']['code'] == 'invalid_argument'
        index = api.paths.daily_diagnostics / 'daily' / day / 'index.json'
        index.write_text('{}')
        corrupt = api.handle('GET', '/api/research/daily-diagnostics?date=' + day)
        assert corrupt.status == 503 and corrupt.body['error']['code'] == 'history_product_invalid'
        missing_phase = api.handle('GET', '/api/research/phases?' + urlencode({
            'physical_serial': 'SERIAL-M4', 'from': '2026-09-12T10:00:00Z', 'to': '2026-09-12T10:10:00Z'}))
        assert missing_phase.status == 200
        assert missing_phase.body['data']['available'] is False
        assert missing_phase.body['data']['reason'] == 'indexed_projection_unavailable'
    finally:
        priority.collector_stopped()
    assert priority._jobs.acquire(False)
    priority._jobs.release()
