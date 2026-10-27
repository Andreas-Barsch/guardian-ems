import json
from datetime import datetime, timedelta, timezone
import pytest
from config_history import ConfigHistory
from phase_engine import PhaseEngine
from indexed_phase_engine import IndexedPhaseEngine
from metadata_revision import MetadataRevision, RevisionMetadataReader
from projection_repository import ProjectionRepository
from test_metadata_revision import publish
from test_phase_engine import PARAMS, sample


@pytest.mark.parametrize('mode', ['historical', 'current', 'what_if'])
def test_original_phase_methods_match_across_config_boundary(tmp_path, mode):
    path = tmp_path/'config.jsonl'
    records = [{'schema_version':1, 'timestamp':f'2026-08-{day:02d}T00:00:00+00:00',
                'config_id':str(day), 'parameters':{**PARAMS, 'cell_diag_charge_current_a':day/10}}
               for day in range(1, 22)]
    path.write_text(''.join(json.dumps(record)+'\n' for record in records))
    repo = ProjectionRepository(tmp_path/'repo')
    revision = MetadataRevision(repo, 'initial')
    revision.initialize()
    revision.begin(provenance={'synthetic':'config window parity'})
    for index, record in enumerate(records):
        revision.apply_page('config', [record], source='config', sequence=index+1,
                            expected_version=revision.state()['version'], provenance={'synthetic':True})
    publish(revision)
    current = lambda: {**PARAMS, 'cell_diag_charge_current_a':3}
    legacy = PhaseEngine(ConfigHistory(path), current)
    metadata = RevisionMetadataReader(repo, 'initial')
    bounded = IndexedPhaseEngine(metadata, current)
    values = [sample('2026-08-20T23:59:00+00:00', 2.05), sample('2026-08-21T00:01:00+00:00', 2.05)]
    kwargs = dict(mode=mode, what_if=PARAMS if mode=='what_if' else None, window_to='2026-08-21T00:02:00+00:00')
    assert bounded.analyse(values, **kwargs) == legacy.analyse(values, **kwargs)
    assert len(metadata.config_window(values[0]['timestamp'], kwargs['window_to'])) == 2
