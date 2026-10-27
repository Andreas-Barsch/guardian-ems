import json
from dataclasses import replace
from datetime import datetime, timezone
from metadata_revision import MetadataRevision, RevisionMetadataReader
from projection_repository import ProjectionRepository
from research_api import GuardianResearchApi
from maintenance import MaintenanceEventLog
from test_research_api import environment, get
from test_timeline import build, add, write_technical
from test_metadata_revision import publish


def test_actual_research_routes_match_latest_maintenance_and_original_alarm_order(tmp_path, monkeypatch):
    api, first, second = environment(tmp_path)
    service, technical, timeline = build(tmp_path)
    event = add(service, occurred_at='2026-09-11T10:00:00+00:00', module_number=4,
                module_serial='SERIAL-M4')
    service.update(event.maintenance_event_id, expected_revision=1, changes={'title':'Revised'})
    service.archive(event.maintenance_event_id, expected_revision=2)
    start = datetime(2026, 9, 11, 10, tzinfo=timezone.utc).timestamp()
    records = [{'type':'alarm_started','timestamp':start+offset,
                'alarm':{'code':'test','message':str(offset),'module':4,'level':'warning'}}
               for offset in (30, 10)]
    write_technical(technical, records)
    paths = replace(api.paths, technical_events=technical)
    baseline = GuardianResearchApi(paths, cursor_secret=b'synthetic-test')
    repo = ProjectionRepository(tmp_path/'metadata-repository')
    revision = MetadataRevision(repo, 'prepared')
    revision.initialize()
    revision.begin(provenance={'synthetic':'original metadata inputs'})
    for kind, rows in [('position',[first.to_dict(), second.to_dict()]),
                       ('maintenance',[record.to_dict() for record in MaintenanceEventLog(paths.maintenance).read_all()]),
                       ('technical',records)]:
        revision.apply_page(kind, rows, source=kind, sequence=1,
                            expected_version=revision.state()['version'], provenance={'first_line':1})
    publish(revision)
    candidate = GuardianResearchApi(paths, cursor_secret=b'synthetic-test', window_backend=object(),
                                    identity_backend=RevisionMetadataReader(repo, 'prepared'))
    queries = [kind+'?from=2026-09-11T00:00:00Z&to=2026-09-12T00:00:00Z' for kind in ('maintenance','alarms')]
    expected = [get(baseline, query) for query in queries]
    monkeypatch.setattr(MaintenanceEventLog, 'read_all', lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy log scan')))
    from timeline import TechnicalEventSource
    monkeypatch.setattr(TechnicalEventSource, 'read', lambda *a, **k: (_ for _ in ()).throw(AssertionError('legacy technical scan')))
    for query, old in zip(queries, expected):
        new = get(candidate, query)
        assert new.status == old.status == 200, new.body
        assert new.body == old.body
