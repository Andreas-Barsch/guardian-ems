"""Regressions for manual SOC reads with unavailable ancillary/day indexes."""
from datetime import datetime, timezone
from urllib.parse import urlencode

import pytest

from history_api import HistoryApi
from history_series_contract import normalize_series
from hycube_evidence import HycubePolicyHistory
from research_budget import ResearchQueryError
from test_history_api import api_env, append_sample
from test_display_history_reader import setup_reader, record, query, write


def target(start='2026-10-24T18:18:00Z', end='2026-10-25T18:18:00Z', **extra):
    return '/api/history/series?' + urlencode(dict(metric='soc', module_number=1,
        **{'from':start, 'to':end}, **extra))


def test_large_policy_context_does_not_discard_soc_or_invent_policy(tmp_path):
    _, directory, api = api_env(tmp_path)
    append_sample(directory, datetime(2026,10,24,18,19,tzinfo=timezone.utc).timestamp(), 1, 63)
    policy = tmp_path/'policy'; policy.mkdir()
    # The guard must reject by size before decoding or scanning this context.
    (policy/'2026-10-24.jsonl').write_text('!'*65537)
    api.hycube_policy_history = HycubePolicyHistory(policy)
    response = api.handle('GET', target(paged='true'))
    assert response.status == 200
    assert response.body['series']['points'][0]['value'] == 63
    timeline = response.body['soc_timeline']
    assert timeline['policy_series'] == []
    assert timeline['policy_evidence'] == 'unavailable'
    assert timeline['policy_evidence_reason'] == 'history_context_index_required'
    assert response.body['unavailable_intervals'][0]['source'] == 'hycube_policy'
    assert response.body['pagination']['next_cursor']


def test_unindexed_page_is_explicitly_unavailable_and_next_day_is_readable(tmp_path):
    _, directory, api = api_env(tmp_path)
    base = datetime(2026,10,24,23,tzinfo=timezone.utc).timestamp()
    for i in range(257):
        append_sample(directory, base+i, 1, 61)
    append_sample(directory, base+3700, 1, 62)
    start,end='2026-10-24T23:00:00Z','2026-10-25T01:00:00Z'
    first=api.handle('GET',target(start,end,paged='true'))
    assert first.status == 200
    assert first.body['series']['points'] == []
    assert first.body['series']['coverage']['quality'] == 'unavailable'
    assert first.body['unavailable_intervals'][0]['reason'] == 'history_index_required'
    second=api.handle('GET',target(start,end,paged='true',cursor=first.body['next_cursor']))
    assert second.status == 200
    assert second.body['series']['points'][0]['value'] == 62
    assert second.body['unavailable_intervals'] == []


@pytest.mark.parametrize('code', ['query_timeout', 'query_cancelled', 'history_record_too_large'])
def test_non_index_errors_still_abort(tmp_path, code):
    _, directory, api = api_env(tmp_path)
    class Failure:
        def query(self, **kwargs):
            raise ResearchQueryError(code, 'must remain a failure',503)
    api.hycube_policy_history=Failure()
    response=api.handle('GET',target(paged='true'))
    assert response.status == 503
    assert response.body['error']['code'] == code


def test_failed_hycube_index_does_not_hide_module_soc(tmp_path):
    _, directory, api = api_env(tmp_path)
    append_sample(directory, datetime(2026,10,24,18,19,tzinfo=timezone.utc).timestamp(),1,63)
    class Failure:
        def query(self, **kwargs):
            raise ResearchQueryError('history_index_required','index unavailable',503)
    api.hycube_series=Failure()
    response=api.handle('GET',target(paged='true'))
    assert response.status == 200
    assert response.body['series']['points'][0]['value'] == 63
    assert response.body['soc_timeline']['hycube_series'] is None
    assert response.body['unavailable_intervals'][0]['source'] == 'hycube'


def test_multiday_display_preserves_available_days_and_breaks_unread_gap(tmp_path):
    base=datetime(2026,9,1,tzinfo=timezone.utc).timestamp()
    reader,_,cell,_=setup_reader(tmp_path,[record(base+60),record(base+120)])
    write(cell/'2026-09-02.jsonl',[record(base+86400+i) for i in range(257)])
    write(cell/'2026-09-03.jsonl',[record(base+172860,soc=62)])
    from history_live_priority import historical_slot
    start,end='2026-09-01T00:00:00+00:00','2026-09-04T00:00:00+00:00'
    with historical_slot():
        result=query(reader,start,end)
    assert len(result['unavailable_intervals']) == 1
    series=normalize_series(result['series'][0],start,end)
    assert any(p['timestamp'].startswith('2026-09-01') for p in series['points'])
    later=[p for p in series['points'] if p['timestamp'].startswith('2026-09-03')]
    assert later and later[0]['_segment_start']
    assert series['coverage']['quality'] == 'partial'
