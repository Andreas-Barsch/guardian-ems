import pytest
from projection_repository import ProjectionRepository,Conflict
from store_retirement import StoreRetirement
from lifecycle_leases import LeaseOwners
from research_budget import check_budget
from work_scheduler import WorkCancelled
from test_store_retirement import drain_index,drain_fences,drain_validation,publications,POLICY


def revoke(owners,identifier):
    owner=owners.owner(identifier)
    owners.fence(identifier,expected_nonce=owner['nonce'],proof={'reason':'explicit synthetic supersession','superseded_by':'new local controller'})
    return owner


def test_active_draft_protected_until_explicit_owner_ends(tmp_path):
    repo=ProjectionRepository(tmp_path/'repo')
    work=repo.begin('private',expected_bundle=None)
    life=StoreRetirement(repo);life.initialize();drain_index(life)
    life.begin('gc',policy=POLICY)
    with pytest.raises(Conflict,match='private draft owner'):life.select('gc','draft',[work])
    owners=LeaseOwners(repo);owner=revoke(owners,'draft:'+work)
    with pytest.raises(Conflict,match='fenced'):
        repo.stage(work,'p','day',{'x':1},metadata={})
    with pytest.raises(Conflict,match='private draft owner'):life.select('gc','draft',[work])
    owners.release_page(owner['id'],expected_nonce=owner['nonce'])
    life.select('gc','draft',[work]);drain_fences(life);drain_validation(life)
    assert life.delete_step('gc')['complete']
    assert owners.owner(owner['id']) is None


def test_fenced_reader_stops_at_checkpoint_and_releases_its_pin(tmp_path):
    repo,life,old,new=publications(tmp_path)
    owners=LeaseOwners(repo)
    with pytest.raises(WorkCancelled,match='fenced'):
        with repo.lease(old,owner='local-history-request') as token:
            revoke(owners,token)
            check_budget()
    with repo._connection() as db:
        assert not db.execute('SELECT 1 FROM pin WHERE token=?',(token,)).fetchone()
        assert not db.execute('SELECT 1 FROM lifecycle_owner WHERE id=?',(token,)).fetchone()
    assert repo.read(new,'p','day')['rows'][0][1]['n']==2


def test_fenced_reader_cannot_return_without_a_further_checkpoint(tmp_path):
    repo,life,old,new=publications(tmp_path)
    with pytest.raises(WorkCancelled):
        with repo.lease(old,owner='diagnostic-reader') as token:
            revoke(LeaseOwners(repo),token)


def test_lost_reader_requires_nonce_and_explicit_release(tmp_path):
    repo,life,old,new=publications(tmp_path);owners=LeaseOwners(repo)
    token=repo.pin(old,owner='window-reader')
    with pytest.raises(Conflict):life.select('gc','pin',[token])
    with pytest.raises(Conflict):owners.fence(token,expected_nonce='wrong',proof={'reason':'test','superseded_by':'test'})
    owner=revoke(owners,token)
    with pytest.raises(Conflict):life.select('gc','pin',[token])
    owners.release_page(token,expected_nonce=owner['nonce'])
    life.select('gc','pin',[token]);drain_fences(life);drain_validation(life)
    for _ in range(30):
        if life.delete_step('gc',limit=2)['complete']:break
    else:pytest.fail('released lost reader group not collected')
    assert owners.owner(token) is None


def test_classic_migration_evidence_retirement_requires_no_reader(tmp_path):
    from test_metadata_migration import setup,drain
    from test_diagnostic_retirement import select_rows,finish
    from test_store_retirement import drain_index,POLICY
    from store_retirement import StoreRetirement
    from projection_repository import Conflict
    import pytest
    original,path,migration,reader=setup(tmp_path);drain(migration)
    repo=migration.repository;before=path.read_bytes()
    life=StoreRetirement(repo);life.initialize();drain_index(life);life.begin('legacy',policy=POLICY)
    with repo._connection() as db:
        rows={table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in ('read_metadata','read_serial','read_topology','read_position','read_identity_epoch','read_timeline','metadata_migration','metadata_input','metadata_position_stage','metadata_page_proof')}
    for table,values in rows.items():select_rows(life,'legacy',table,values)
    with repo._write() as db:db.execute("INSERT INTO read_metadata_reader VALUES('active','revision')")
    while not life.fence_step('legacy')['complete']:pass
    with pytest.raises(Conflict):
        while not life.validate_step('legacy')['complete']:pass
    from lifecycle_leases import release_reader
    release_reader(repo,'active','read_metadata_reader')
    finish(life,'legacy')
    assert path.read_bytes()==before
    with repo._connection() as db:
        for table in rows:assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
