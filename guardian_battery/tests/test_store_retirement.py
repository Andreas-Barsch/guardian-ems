class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import json
import pytest
from projection_repository import Conflict, ProjectionRepository
from store_retirement import StoreRetirement
from test_publication_history_readers import setup

POLICY={'reason':'synthetic exact objects','rollback_evidence':'separate retained current publication','retry_closed':True}


def drain_index(life):
    for _ in range(10000):
        result=life.index_step(limit=4)
        assert result['rows']<=4
        if result['complete']:return
    pytest.fail('ownership index did not finish')


def drain_fences(life):
    for _ in range(1000):
        if life.fence_step('gc',limit=2)['complete']:return
    pytest.fail('fences did not finish')


def drain_validation(life):
    for _ in range(10000):
        result=life.validate_step('gc',limit=2)
        assert result['edges']<=2
        if result['complete']:return
    pytest.fail('validation did not finish')


def test_actual_store_ownership_migration_and_warm_resume(tmp_path):
    env,_,_,_=setup(tmp_path)
    life=StoreRetirement(env['repo']);life.initialize();drain_index(life)
    assert StoreRetirement(ProjectionRepository(env['repo'].path)).index_step()=={'complete':True,'rows':0}
    with env['repo']._connection() as db:
        assert db.execute("SELECT 1 FROM lifecycle_edge WHERE owner_table='canonical_day_job' LIMIT 1").fetchone()
        assert db.execute("SELECT 1 FROM lifecycle_edge WHERE target_table='canonical_counts' LIMIT 1").fetchone()


def kernel(tmp_path,extra=False):
    repo=ProjectionRepository(tmp_path/'repo')
    # A private draft and its values have no current/public owner.
    work=repo.begin('synthetic-private',expected_bundle=None)
    repo.stage(work,'synthetic','day',{'one':{'v':1},'two':{'v':2}},metadata={})
    life=StoreRetirement(repo);life.initialize();drain_index(life)
    if extra:
        with repo._write() as db:db.execute('INSERT INTO draft_value VALUES(?,?,?,?,?)',(work,'synthetic','day','external','{}'))
    from lifecycle_leases import LeaseOwners
    owners=LeaseOwners(repo);owner=owners.owner('draft:'+work)
    owners.fence(owner['id'],expected_nonce=owner['nonce'],proof={'reason':'synthetic owner explicitly ended','superseded_by':'test control'})
    owners.release_page(owner['id'],expected_nonce=owner['nonce'])
    life.begin('gc',policy=POLICY)
    with repo._connection() as db:
        for table in ('draft','draft_partition','draft_value'):
            keys=json.loads(db.execute('SELECT keys FROM lifecycle_layout WHERE name=?',(table,)).fetchone()[0])
            rows=[dict(r) for r in db.execute('SELECT * FROM '+table)]
            for row in rows:
                if table=='draft_value' and row['row_key']=='external':continue
                life.select('gc',table,[row[k] for k in keys])
    return repo,life,work


def test_orphan_draft_closure_deleted_in_bounded_steps(tmp_path):
    repo,life,work=kernel(tmp_path);drain_fences(life);drain_validation(life)
    for _ in range(30):
        result=life.delete_step('gc',limit=1)
        assert result['examined']<=1
        if result['complete']:break
    else:pytest.fail('retirement did not finish')
    with repo._connection() as db:
        assert not db.execute('SELECT 1 FROM draft WHERE id=?',(work,)).fetchone()
        assert not db.execute('SELECT 1 FROM draft_value LIMIT 1').fetchone()


def test_active_root_blocks_whole_plan_and_abort_preserves_data(tmp_path):
    repo,life,work=kernel(tmp_path,extra=True)
    # Use real FK owner introduced before schema sealing/indexing in new fixture.
    # An external draft value was not selected, therefore remains an owner.
    # Selected draft's hash unchanged; the external child is the blocker.
    drain_fences(life)
    with pytest.raises(Conflict,match='external owner'):
        drain_validation(life)
    with pytest.raises(Conflict):life.delete_step('gc')
    while not life.abort_step('gc',limit=1)['complete']:pass
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM draft_value').fetchone()[0]==3
        assert not db.execute('SELECT 1 FROM lifecycle_fence LIMIT 1').fetchone()


@pytest.mark.parametrize('boundary',['before_ownership_commit','after_ownership_commit','before_fence_commit','after_fence_commit','before_validate_commit','after_validate_commit','before_retirement_commit','after_retirement_commit'])
def test_crash_boundaries_resume_without_unsafe_cascade(tmp_path,boundary):
    repo,life,work=kernel(tmp_path)
    def fault(stage):
        if stage==boundary:raise InjectedCrash('power loss')
    if 'ownership' in boundary:
        # A newly explicit metadata-only cursor reset tests replay idempotence.
        with repo._write() as db:db.execute("UPDATE lifecycle_layout SET after_row=0,ready=0 WHERE name='draft_value'")
        with pytest.raises(InjectedCrash):life.index_step(limit=1,fault=fault)
        drain_index(life)
    if 'fence' in boundary:
        with pytest.raises(InjectedCrash):life.fence_step('gc',limit=1,fault=fault)
    drain_fences(life)
    if 'validate' in boundary:
        with pytest.raises(InjectedCrash):life.validate_step('gc',limit=1,fault=fault)
    drain_validation(life)
    if 'retirement' in boundary:
        with pytest.raises(InjectedCrash):life.delete_step('gc',limit=1,fault=fault)
    life=StoreRetirement(ProjectionRepository(repo.path))
    for _ in range(50):
        if life.delete_step('gc',limit=1)['complete']:break
    else:pytest.fail('resume unfinished')
    with repo._connection() as db:assert not db.execute('SELECT 1 FROM draft_value').fetchone()


def test_new_owner_is_fenced_and_unknown_reader_pin_not_selectable(tmp_path):
    import sqlite3
    repo,life,work=kernel(tmp_path);drain_fences(life)
    with pytest.raises(Conflict,match='fenced'):
        with repo._write() as db:db.execute('INSERT INTO draft_value VALUES(?,?,?,?,?)',(work,'synthetic','day','late','{}'))
    with pytest.raises(Conflict):life.select('gc','head',['anything'])


def publications(tmp_path):
    repo=ProjectionRepository(tmp_path/'repo')
    work=repo.begin('current',expected_bundle=None)
    repo.stage(work,'p','day',{'v':{'n':1}},metadata={});old=repo.publish(work)
    work=repo.begin('current',expected_bundle=old)
    repo.stage(work,'p','day',{'v':{'n':2}},metadata={});new=repo.publish(work)
    life=StoreRetirement(repo);life.initialize();drain_index(life);life.begin('gc',policy=POLICY)
    with repo._connection() as db:
        publication=db.execute('SELECT publication_id FROM member WHERE bundle_id=?',(old,)).fetchone()[0]
        value=db.execute('SELECT id FROM value_version WHERE retired IS NOT NULL').fetchone()[0]
    life.select('gc','bundle',[old]);life.select('gc','member',[old,'p','day'])
    life.select('gc','publication',[publication]);life.select('gc','value_version',[value])
    return repo,life,old,new


def test_publication_retirement_preserves_current_and_new_pin_fenced(tmp_path):
    import sqlite3
    repo,life,old,new=publications(tmp_path);drain_fences(life)
    with pytest.raises(Conflict,match='fenced'):repo.pin(old,owner='new-reader')
    drain_validation(life)
    for _ in range(50):
        result=life.delete_step('gc',limit=1)
        assert life.last_sql_vm_steps<200000
        if result['complete']:break
    else:pytest.fail('publication retirement incomplete')
    assert repo.read(new,'p','day')['rows']==[('v',{'n':2})]
    with pytest.raises(Exception,match='absent|collected'):repo.read(old,'p','day')


@pytest.mark.parametrize('owner',['active-reader','compatible-rollback'])
def test_reader_and_rollback_protect_entire_publication_group(tmp_path,owner):
    repo,life,old,new=publications(tmp_path)
    token=repo.pin(old,owner=owner)
    with pytest.raises(Conflict,match='pin cannot be retired'):life.select('gc','pin',[token])
    drain_fences(life)
    with pytest.raises(Conflict,match='external owner'):drain_validation(life)
    assert repo.read(old,'p','day')['rows']==[('v',{'n':1})]
    assert repo.read(new,'p','day')['rows']==[('v',{'n':2})]
    repo.release(token)
    drain_validation(life)
    for _ in range(50):
        if life.delete_step('gc',limit=2)['complete']:break
    else:pytest.fail('released publication was not collected')


def test_private_correction_base_is_protected(tmp_path):
    repo,life,old,new=publications(tmp_path)
    # Another head can legitimately start correction from the old bundle.
    with repo._write() as db:db.execute('INSERT INTO draft VALUES(?,?,?)',('correction','other-head',old))
    drain_fences(life)
    with pytest.raises(Conflict,match='external owner'):drain_validation(life)
    assert repo.read(old,'p','day')['rows'][0][1]['n']==1


@pytest.mark.parametrize('boundary',['before_audit_prune_commit','after_audit_prune_commit'])
def test_terminal_audit_archive_prune_and_id_reuse_fence(tmp_path,boundary):
    import hashlib
    repo,life,work=kernel(tmp_path);drain_fences(life);drain_validation(life)
    for _ in range(50):
        if life.delete_step('gc',limit=2)['complete']:break
    archive=tmp_path/'audit.jsonl';after=('', '');header=None
    with archive.open('w') as sink:
        while True:
            page=life.audit_page('gc',after=after,limit=2);header=page['plan']
            sink.write(json.dumps(page)+'\n')
            if page['next'] is None:break
            after=page['next']
    proof={'nonce':header['nonce'],'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'retry_closed':True}
    def fault(stage):
        if stage==boundary:raise InjectedCrash('power loss')
    with pytest.raises(InjectedCrash):life.prune_audit_step('gc',proof=proof,limit=1,fault=fault)
    while not life.prune_audit_step('gc',proof=proof,limit=1)['complete']:pass
    with repo._connection() as db:assert not db.execute('SELECT 1 FROM lifecycle_candidate WHERE plan=?',('gc',)).fetchone()
    life.begin('gc',policy=POLICY)
    while not life.abort_step('gc')['complete']:pass
    with pytest.raises(Conflict,match='another run'):life.prune_audit_step('gc',proof=proof)


def test_missing_native_ownership_constraint_rejects_lookalike_schema(tmp_path):
    repo=ProjectionRepository(tmp_path/'repo')
    with repo._write() as db:
        db.execute('DROP TABLE draft_value')
        db.execute('CREATE TABLE draft_value(draft_id TEXT,product TEXT,partition_key TEXT,row_key TEXT,payload TEXT,PRIMARY KEY(draft_id,product,partition_key,row_key))')
    with pytest.raises(Conflict,match='unknown/changed store schema'):
        StoreRetirement(repo).initialize()
