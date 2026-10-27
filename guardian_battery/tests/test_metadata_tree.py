import random
from projection_repository import ProjectionRepository
from metadata_tree import MetadataTree,SCHEMA


def test_immutable_pages_update_delete_and_old_revision(tmp_path):
    repo=ProjectionRepository(tmp_path/'db')
    with repo._connection() as db:db.executescript(SCHEMA)
    with repo._write() as db:
        tree=MetadataTree(db);keys=list(range(400));random.Random(3).shuffle(keys)
        for key in keys:tree.put(f'{key:05d}',{'v':key})
        old=tree.root
        for key in keys[::2]:tree.delete(f'{key:05d}')
        assert tree.page(limit=256)==[(f'{key:05d}',{'v':key}) for key in sorted(keys[1::2])]
        original=MetadataTree(db,old)
        assert original.get('00011')=={'v':11}
        assert original.floor('00011z')==('00011',{'v':11})
        assert original.page(after='00395',limit=16)==[(f'{i:05d}',{'v':i}) for i in range(396,400)]
        assert tree._height(tree.root)<16


def test_append_cost_tracks_tree_depth_not_all_history(tmp_path):
    repo=ProjectionRepository(tmp_path/'db')
    with repo._connection() as db:db.executescript(SCHEMA)
    with repo._write() as db:
        tree=MetadataTree(db)
        for i in range(2000):tree.put(f'{i:06d}',{'v':i})
        tree.reads=tree.writes=0;tree.put('002000',{'v':2000})
        assert tree.writes<32 and tree.reads<256
        tree.reads=0
        assert tree.page(after='001990',limit=8)==[(f'{i:06d}',{'v':i}) for i in range(1991,1999)]
        assert tree.reads<64
