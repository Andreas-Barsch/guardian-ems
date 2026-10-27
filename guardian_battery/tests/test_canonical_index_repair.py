import gzip
import hashlib
import json
import time
from pathlib import Path

import pytest
import history_live_priority as priority
from canonical_config_compatibility import matches_historical_config
from canonical_window_store import read
from test_history_preparation import canonical_fixture, worker, request, run, BASE


@pytest.fixture(autouse=True)
def coordinated():
    priority.collector_stopped()
    priority.cycle_started()
    priority.idle_until(time.monotonic()+120)
    yield
    priority.collector_stopped()


def test_existing_partial_facts_reindexed_without_predecessor_or_rebuild(tmp_path):
    target = canonical_fixture(tmp_path)
    (target.parent/'2026-01-01.json.gz').unlink()
    artifact = json.loads(gzip.decompress(target.read_bytes()))
    artifact['complete'] = False
    target.write_bytes(gzip.compress(json.dumps(artifact).encode()))
    original = target.read_bytes()
    raw_source = (tmp_path/'cell_history/2026-01-02.jsonl').read_bytes()
    config = tmp_path/'config_history.jsonl'
    with config.open('a') as stream:
        stream.write(json.dumps(dict(schema_version=1,timestamp='2026-01-04T00:00:00Z',
                                    config_id='later',parameters={}))+'\n')
    old_index = Path(str(target)+'.window.sqlite')
    old_index.write_bytes(b'old index preserved')
    w=worker(tmp_path);w.submit(request('canonical_index'))
    result=run(w)
    assert result['state']=='complete'
    assert result['tasks'][0]['artifact_complete'] is False
    assert target.read_bytes()==original
    assert (tmp_path/'cell_history/2026-01-02.jsonl').read_bytes()==raw_source
    assert list(w.directory.rglob('previous.window.sqlite'))[0].read_bytes()==b'old index preserved'
    restored,_=read(target,BASE,BASE+600,module=1)
    assert restored['complete'] is False
    assert restored['identities']
    from canonical_phase import CanonicalPhaseReader
    reader=CanonicalPhaseReader(target.parent,tmp_path/'cell_history',config)
    response=reader.query('2026-01-02T00:00:00Z','2026-01-02T00:10:00Z',module_number=1)
    assert response['available'] is True
    # The request-level config verification uses the same bounded prefix rule.
    assert matches_historical_config(config,artifact['config_signature'],'2026-01-02T00:10:00Z')


@pytest.mark.parametrize('change',['retroactive','modified_prefix','missing_hash'])
def test_config_prefix_rejects_unsafe_changes(tmp_path,change):
    path=tmp_path/'config.jsonl'
    original=b'{"schema_version":1,"timestamp":"2026-01-01T00:00:00Z","parameters":{},"config_id":"old"}\n'
    path.write_bytes(original)
    expected={'size':len(original),'sha256':hashlib.sha256(original).hexdigest()}
    if change=='retroactive':
        path.write_bytes(original+b'{"schema_version":1,"timestamp":"2026-01-02T00:00:00Z","parameters":{},"config_id":"new"}\n')
    elif change=='modified_prefix':
        path.write_bytes(original.replace(b'old',b'bad'))
    else:
        expected.pop('sha256')
    assert not matches_historical_config(path,expected,'2026-01-02T12:00:00Z')
