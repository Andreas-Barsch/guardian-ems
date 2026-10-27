class InjectedCrash(Exception):
    """Only the explicitly injected crash, never a product failure."""


import pytest
from projection_repository import ProjectionRepository,Conflict
from metadata_tree import MetadataTree,SCHEMA
from store_retirement import StoreRetirement
from derived_page_sweep import DerivedPageSweep
from test_store_retirement import drain_index,POLICY

@pytest.mark.parametrize('fault_point',['before_sweep_commit','after_sweep_commit'])
def test_repeated_revision_pages_reclaimed_with_current_tree_unchanged(tmp_path,fault_point):
    repo=ProjectionRepository(tmp_path/'repo')
    with repo._connection() as db:db.executescript(SCHEMA)
    # Real root ownership, native FK, retained reader revision.
    from metadata_revision import MetadataRevision
    revision=MetadataRevision(repo,'kept');revision.initialize();revision.begin(provenance={'synthetic':'immutable roots'})
    with repo._write() as db:
        tree=MetadataTree(db)
        for i in range(300):tree.put(str(i).zfill(5),{'value':i})
        db.execute("UPDATE metadata_root SET node=? WHERE revision='kept' AND name='config'",(tree.root,))
        root=tree.root
        before=db.execute('SELECT count(*) FROM metadata_node').fetchone()[0]
    life=StoreRetirement(repo);life.initialize();sweep=DerivedPageSweep(repo);sweep.initialize();drain_index(life)
    crashed=False;deleted=0
    for round in range(80):
        identifier='pages-'+str(round);sweep.begin(identifier,'metadata_node',policy=POLICY)
        while True:
            def fault(point):
                nonlocal crashed
                if not crashed and point==fault_point:crashed=True;raise InjectedCrash('power loss')
            try:result=sweep.step(identifier,limit=7,fault=fault)
            except InjectedCrash:continue
            assert result['examined']<=7
            if result['complete']:break
        while not life.fence_step(identifier)['complete']:pass
        while not life.validate_step(identifier)['complete']:pass
        removed=0
        for _ in range(10000):
            result=life.delete_step(identifier,limit=8);removed+=result['deleted']
            if result['complete']:break
        else:pytest.fail('page delete incomplete')
        deleted+=removed
        if not removed:break
    else:pytest.fail('tree garbage not drained')
    with repo._connection() as db:
        assert db.execute('SELECT count(*) FROM metadata_node').fetchone()[0]==300
        assert deleted==before-300
        tree=MetadataTree(db,root)
        assert tree.get('00299')=={'value':299}
        assert len(tree.page(limit=256))==256
    assert crashed
