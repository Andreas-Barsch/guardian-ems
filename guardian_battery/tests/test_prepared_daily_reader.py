from pathlib import Path
from datetime import date,timedelta
import pytest
from prepared_daily_reader import SelectedDailyRepository
from research_api import GuardianResearchApi
from test_research_api import environment,get
from test_guardian_diagnostics import write_day,aggregate,write_risk


@pytest.mark.parametrize('old_count',[1,300])
@pytest.mark.parametrize('route',['daily-diagnostics?date=2026-08-31',
    'diagnostic-evidence?date=2026-08-31',
    'daily-diagnostics?from=2026-08-31T00:00:00Z&to=2026-08-31T23:59:59Z'])
def test_existing_daily_dtos_with_only_selected_day_reads(tmp_path,monkeypatch,old_count,route):
    old,_,_=environment(tmp_path)
    root=old.paths.daily_diagnostics
    for i in range(old_count):
        write_day(root,(date(2026,8,30)-timedelta(days=i)).isoformat())
    write_day(root,'2026-08-31',aggregates=[aggregate()]);write_risk(root)
    expected=get(old,route)
    readers=[]
    def factory(*args):
        reader=SelectedDailyRepository(*args);readers.append(reader);return reader
    candidate=GuardianResearchApi(old.paths,cursor_secret=b'test',window_backend=object(),
        identity_backend=old.identity,daily_repository_factory=factory)
    monkeypatch.setattr(Path,'iterdir',lambda *a:pytest.fail('scanned old Daily directories'))
    monkeypatch.setattr(Path,'glob',lambda *a:pytest.fail('scanned old Daily files'))
    actual=get(candidate,route)
    assert actual.status==expected.status==200,actual.body
    assert actual.body==expected.body
    assert readers[0].files_read in (3,4)
    assert readers[0].bytes_read<10000


def test_selected_daily_oversize_is_explicit_and_never_decoded(tmp_path,monkeypatch):
    old,_,_=environment(tmp_path);root=old.paths.daily_diagnostics
    write_day(root,'2026-08-31')
    index=root/'daily'/'2026-08-31'/'index.json'
    index.write_bytes(b' '*(SelectedDailyRepository.MAX_BYTES+1))
    candidate=GuardianResearchApi(old.paths,cursor_secret=b'test',window_backend=object(),
        identity_backend=old.identity,daily_repository_factory=SelectedDailyRepository)
    response=get(candidate,'daily-diagnostics?date=2026-08-31')
    assert response.status==503,response.body
    assert response.body['error']['code']=='history_window_budget'
