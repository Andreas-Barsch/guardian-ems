class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import json
import pytest
from projection_repository import ProjectionRepository, Conflict
from canonical_checkpoint_storage import SCHEMA, CountSequence
from derived_retention import DerivedRetention

POLICY={'scope':'unreferenced_counts','reason':'synthetic ownership proof','rollback_evidence':'retained payload roots'}


def setup(tmp_path):
    repo=ProjectionRepository(tmp_path/'db')
    with repo._connection() as db:
        db.executescript(SCHEMA+'CREATE TABLE retained_payload(id INTEGER PRIMARY KEY,payload TEXT);')
    with repo._write() as db:
        seq=CountSequence.from_values(db,[1,2,3,4]);root=seq.root
        db.execute('INSERT INTO retained_payload VALUES(1,?)',(json.dumps(seq.descriptor()),))
        orphan=CountSequence.from_values(db,[999]).root
    gc=DerivedRetention(repo);gc.initialize()
    return repo,gc,root,orphan


def index(gc):
    for _ in range(1000):
        result=gc.index_step(limit=2)
        assert result.get('examined',0)<=2
        if result['complete']:return
    pytest.fail('index did not finish')


def decide(gc,identifier='plan'):
    gc.begin(identifier,policy=POLICY)
    for _ in range(1000):
        state=gc.status(identifier)
        result=gc.decide_step(identifier,expected_version=state['version'],limit=2)
        assert result['examined']<=2
        if result['complete']:return
    pytest.fail('decision did not finish')


def test_decision_is_not_deletion_and_external_roots_preserved(tmp_path):
    repo,gc,root,orphan=setup(tmp_path)
    with pytest.raises(Conflict):gc.begin('plan',policy=POLICY)
    index(gc);decide(gc)
    with repo._connection() as db:assert db.execute('SELECT 1 FROM canonical_counts WHERE id=?',(orphan,)).fetchone()
    for _ in range(100):
        if gc.delete_step('plan',limit=2)['examined']==0:break
    with repo._connection() as db:
        assert not db.execute('SELECT 1 FROM canonical_counts WHERE id=?',(orphan,)).fetchone()
        assert list(CountSequence(db,root))==[1,2,3,4]


@pytest.mark.parametrize('boundary',['before_reference_commit','after_reference_commit','before_decision_commit','after_decision_commit','before_delete_commit','after_delete_commit'])
def test_crash_resume_keeps_referenced_tree(tmp_path,boundary):
    repo,gc,root,orphan=setup(tmp_path)
    def fail(stage):
        if stage==boundary:raise InjectedCrash('power loss')
    if 'reference' in boundary:
        with pytest.raises(InjectedCrash):gc.index_step(limit=2,fault=fail)
    index(gc);gc.begin('plan',policy=POLICY)
    if 'decision' in boundary:
        with pytest.raises(InjectedCrash):gc.decide_step('plan',expected_version=0,limit=2,fault=fail)
    decide(gc)
    if 'delete' in boundary:
        with pytest.raises(InjectedCrash):gc.delete_step('plan',fault=fail)
    reopened=DerivedRetention(ProjectionRepository(repo.path))
    while reopened.delete_step('plan',limit=2)['examined']:pass
    with repo._connection() as db:assert list(CountSequence(db,root))==[1,2,3,4]


def test_new_reference_after_decision_prevents_deletion(tmp_path):
    repo,gc,root,orphan=setup(tmp_path);index(gc);decide(gc)
    with repo._write() as db:
        db.execute('INSERT INTO retained_payload VALUES(2,?)',(json.dumps(CountSequence(db,orphan).descriptor()),))
    while gc.delete_step('plan')['examined']:pass
    with repo._connection() as db:assert list(CountSequence(db,orphan))==[999]


def test_schema_change_blocks_delete_and_no_implicit_policy(tmp_path):
    repo,gc,root,orphan=setup(tmp_path);index(gc);decide(gc)
    with repo._write() as db:db.execute('CREATE TABLE new_owner(id INTEGER PRIMARY KEY,payload TEXT)')
    with pytest.raises(Conflict,match='schema changed'):gc.delete_step('plan')
    with pytest.raises(ValueError):gc.begin('other',policy={})


def test_reference_added_during_backfill_and_trigger_loss(tmp_path):
    repo,gc,root,orphan=setup(tmp_path)
    gc.index_step(limit=1)
    with repo._write() as db:db.execute('INSERT INTO retained_payload VALUES(2,?)',(json.dumps(CountSequence(db,orphan).descriptor()),))
    index(gc);decide(gc)
    with repo._write() as db:db.execute('DROP TRIGGER retention_retained_payload_insert')
    with pytest.raises(Conflict,match='schema changed'):gc.delete_step('plan')


def test_many_revisions_bounded_pages_and_completed_resume(tmp_path):
    repo,gc,root,orphan=setup(tmp_path)
    with repo._write() as db:
        for i in range(200):CountSequence.from_values(db,[1000+i])
    index(gc);decide(gc)
    while True:
        result=gc.delete_step('plan',limit=2)
        assert result['examined']<=2 and result['deleted']<=2
        if not result['examined']:break
    assert gc.decide_step('plan',expected_version=gc.status('plan')['version'])['examined']==0
    with repo._connection() as db:assert list(CountSequence(db,root))==[1,2,3,4]


def test_retention_audit_pruning_requires_archive_and_rejects_reused_id(tmp_path):
    repo,gc,root,orphan=setup(tmp_path);index(gc);decide(gc)
    with pytest.raises(Conflict):gc.audit_page('plan')
    while gc.delete_step('plan')['examined']:pass
    archive=gc.audit_page('plan');nonce=archive['identity']['nonce']
    with pytest.raises(ValueError):gc.prune_audit_step('plan',nonce=nonce,archive_sha256='b'*64,retry_closed=False)
    while not gc.prune_audit_step('plan',nonce=nonce,archive_sha256='b'*64,retry_closed=True,limit=1)['complete']:pass
    gc.begin('plan',policy=POLICY)
    with pytest.raises(Conflict):gc.prune_audit_step('plan',nonce=nonce,archive_sha256='b'*64,retry_closed=True)
    with repo._connection() as db:assert list(CountSequence(db,root))==[1,2,3,4]
