"""Repeated closed inventories preserve source, index, receipt and provenance."""
import copy
import json
from pathlib import Path

import pytest
from projection_repository import Conflict
from source_catalog import SourceCatalog
from cell_source_resolver import CellSourceResolver
from legacy_source_inventory import LegacySourceInventory
from test_source_catalog import catalog,record,write,index_all
from test_source_inventory_snapshot import finish_inventory


def inventory(c,root,namespace='legacy-local'):
    d=LegacySourceInventory(c);job=d.create([(root,namespace)]);finish_inventory(d,job)
    return job,[r['item'] for r in d.page(job)]


def ingest(c,job,items):
    return c.ingest_page('manifest:'+job,expected_cursor=0,items=items,finish=True)


def source(c):
    return c.registered()[0]


def test_two_completed_inventories_preserve_source_cursor_coverage(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();write(root/'raw.jsonl',[record(1),record(2)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    s=source(catalog);catalog.index_step(s['source'],s['revision'],expected_cursor=0,max_records=1)
    before=source(catalog);coverage=catalog.status();first_observation=catalog.inventory_observations(first)
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert first!=second and items[0]['registration']==items2[0]['registration']
    assert source(catalog)==before and catalog.status()==coverage
    assert catalog.inventory_observations(first)==first_observation
    latest=catalog.inventory_observations(second)
    assert latest[0]['outcome']=='known_prefix' and latest[0]['index_cursor']==before['cursor']
    assert latest[0]['catalog_revision']==before['revision']
    assert len(catalog.registered())==1


def test_new_job_after_scope_change_accepts_known_and_added_file(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();write(root/'a.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    s=source(catalog);index_all(catalog,s['source'],s['revision']);before=source(catalog)
    d=LegacySourceInventory(catalog);bad=d.create([(root,'legacy-local')]);d.step(bad,max_entries=1)
    write(root/'b.jsonl',[record(2)])
    assert d.step(bad)['status']=='scope_changed'
    with pytest.raises(Conflict):d.page(bad)
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert catalog.status()['sources']==2
    assert next(r for r in catalog.registered() if r['source']==before['source'])==before
    assert {o['outcome'] for o in catalog.inventory_observations(second)}=={'known_prefix','registered'}
    assert catalog.inventory_observations(first)[0]['source']==before['source']


def append_confirmed(c,path,s):
    with path.open('ab') as h:
        h.write(json.dumps(record(3)).encode()+b'\n');h.write(json.dumps(record(4)).encode()+b'\n')
    c.confirm_append(s['source'],s['revision'],expected_receipt=s['receipt'],receipt='confirmed-next',
                     frontier=path.stat().st_size,writer_epoch=s['writer_epoch'])


def test_reinventory_after_confirmed_append_keeps_active_revision_and_partial_index(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'a.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    s=source(catalog);index_all(catalog,s['source'],s['revision']);s=source(catalog)
    append_confirmed(catalog,path,s)
    catalog.index_step(s['source'],s['revision'],expected_cursor=s['cursor'],max_records=1)
    before=source(catalog);coverage=catalog.status()
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    observed=catalog.inventory_observations(second)[0]
    assert observed['observed_revision']!=observed['catalog_revision']==s['revision']
    assert observed['receipt']=='confirmed-next' and observed['index_cursor']==before['cursor']
    assert observed['outcome']=='known_prefix'
    assert source(catalog)==before and catalog.status()==coverage
    assert sum(x['decodes'] for x in index_all(catalog,s['source'],s['revision']))==1


@pytest.mark.parametrize('decision',['append','revision'])
def test_changed_source_needs_explicit_decision_without_blocking_unchanged_neighbours(catalog,tmp_path,decision):
    root=tmp_path/'root';root.mkdir();path=write(root/'a.jsonl',[record(1)]);write(root/'b.jsonl',[record(2)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    for s in catalog.registered():index_all(catalog,s['source'],s['revision'])
    before={s['path']:s for s in catalog.registered()}
    with path.open('ab') as h:h.write(json.dumps(record(3)).encode()+b'\n')
    write(root/'c.jsonl',[record(4)])
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert {o['outcome'] for o in catalog.inventory_observations(second)}=={'needs_receipt_or_revision','known_prefix','registered'}
    current={s['path']:s for s in catalog.registered()}
    assert current[str(path)]==before[str(path)] and current[str(root/'b.jsonl')]==before[str(root/'b.jsonl')]
    s=before[str(path)];pending=catalog.inventory_observations(second)
    if decision=='append':
        catalog.confirm_append(s['source'],s['revision'],expected_receipt=s['receipt'],receipt='explicit-append',
                               frontier=path.stat().st_size,writer_epoch=s['writer_epoch'])
        expected_revision=s['revision']
    else:
        registration=next(i['registration'] for i in items2 if i['registration']['path']==str(path))
        catalog.register(**{**registration,'expected_revision':s['revision']})
        expected_revision=registration['revision']
    third,items3=inventory(catalog,root);ingest(catalog,third,items3)
    binding=next(o for o in catalog.inventory_observations(third) if o['source']==s['source'])
    assert binding['outcome']=='known_prefix' and binding['catalog_revision']==expected_revision
    assert catalog.inventory_observations(second)==pending  # Decision history never rewritten.
    old=next(r for r in catalog.registered() if r['source']==s['source'] and r['revision']==s['revision'])
    assert old['cursor']==s['cursor'] and old['records']==s['records']


def test_conflicting_namespace_and_tampered_registration_remain_rejected(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();write(root/'raw.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items);before=source(catalog)
    second,items2=inventory(catalog,root,namespace='utc')
    with pytest.raises(Conflict,match='namespace'):ingest(catalog,second,items2)
    assert source(catalog)==before and catalog.migration_progress('manifest:'+second) is None
    third,items3=inventory(catalog,root);forged=copy.deepcopy(items3)
    forged[0]['registration']['context']['invented']='not observed'
    with pytest.raises(Conflict,match='closed manifest'):ingest(catalog,third,forged)
    with pytest.raises(Conflict):catalog.register(**{**items[0]['registration'],'namespace':'utc'})
    assert source(catalog)==before


def test_old_job_encoded_provenance_preserved_with_additive_observation_capability(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'raw.jsonl',[record(1)])
    first,items=inventory(catalog,root)
    # Explicit synthetic fixture of the persisted 38886bb inventory encoding.
    old=copy.deepcopy(items[0]);old.pop('observation')
    old['registration']['writer_epoch']='inventory:'+first
    old['registration']['context']['inventory']=first
    with catalog.write() as db:
        db.execute('UPDATE inventory_entry SET item=? WHERE inventory=?',(json.dumps(old,sort_keys=True),first))
        db.execute('DROP TABLE source_observation')
    catalog=SourceCatalog(catalog.path)
    with catalog.connection() as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='source_observation'").fetchone() is None
    ingest(catalog,first,[old]);s=source(catalog);index_all(catalog,s['source'],s['revision']);s=source(catalog)
    append_confirmed(catalog,path,s);index_all(catalog,s['source'],s['revision'])
    before=source(catalog)
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert source(catalog)==before
    assert json.loads(source(catalog)['context'])['inventory']==first
    assert source(catalog)['writer_epoch']=='inventory:'+first
    binding=catalog.inventory_observations(second)[0]
    assert binding['receipt']=='confirmed-next' and binding['outcome']=='known_prefix'
    with catalog.connection() as db:
        assert json.loads(db.execute('SELECT item FROM inventory_entry WHERE inventory=?',(first,)).fetchone()[0])==old
        assert db.execute('SELECT version FROM catalog_schema').fetchone()[0]==2


def test_replayed_acknowledged_observation_is_historical_not_cursor_reset(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'raw.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items);binding=catalog.inventory_observations(first)
    s=source(catalog);index_all(catalog,s['source'],s['revision']);s=source(catalog)
    append_confirmed(catalog,path,s);index_all(catalog,s['source'],s['revision']);before=source(catalog)
    catalog.ingest_page('replay',expected_cursor=0,items=items,finish=True)
    assert source(catalog)==before and catalog.inventory_observations(first)==binding
    assert binding[0]['receipt']!=before['receipt']  # Deliberately historical acknowledgement.


def test_replaced_file_is_new_source_not_alias_of_previous_progress(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'raw.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items);s=source(catalog)
    index_all(catalog,s['source'],s['revision']);before=source(catalog)
    r=CellSourceResolver(catalog);refs=r.plan_snapshot(r.open_window(0,10))['refs']
    path.rename(root/'previous.bak');write(path,[record(2)])
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert catalog.status()['sources']==2
    assert next(row for row in catalog.registered() if row['source']==before['source'])==before
    assert catalog.inventory_observations(second)[0]['source']!=before['source']
    with pytest.raises(Conflict):r.read_refs(refs)


def test_reinventory_preserves_invalid_record_coverage(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();write(root/'raw.jsonl',[record(1),record(True)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    s=source(catalog);index_all(catalog,s['source'],s['revision'])
    before=source(catalog);coverage=catalog.status();assert coverage['errors']==1
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert source(catalog)==before and catalog.status()==coverage


def test_same_size_rewrite_is_pending_not_identical(catalog,tmp_path):
    root=tmp_path/'root';root.mkdir();path=write(root/'raw.jsonl',[record(1,value=111)])
    first,items=inventory(catalog,root);ingest(catalog,first,items)
    s=source(catalog);index_all(catalog,s['source'],s['revision']);before=source(catalog)
    path.write_bytes(path.read_bytes().replace(b'111',b'999'))
    second,items2=inventory(catalog,root);ingest(catalog,second,items2)
    assert catalog.inventory_observations(second)[0]['outcome']=='needs_receipt_or_revision'
    assert source(catalog)==before
    with pytest.raises(Conflict):catalog.index_step(s['source'],s['revision'],expected_cursor=before['cursor'])


@pytest.mark.parametrize('stage',['after_registrations','after_migration_cursor','after_migration_commit'])
def test_observation_ack_and_manifest_cursor_share_transaction(catalog,tmp_path,stage):
    root=tmp_path/'root';root.mkdir();write(root/'raw.jsonl',[record(1)])
    first,items=inventory(catalog,root);ingest(catalog,first,items);before=source(catalog)
    second,items2=inventory(catalog,root)
    def fail(at):
        if at==stage:raise RuntimeError('injected')
    with pytest.raises(RuntimeError):
        catalog.ingest_page('manifest:'+second,expected_cursor=0,items=items2,finish=True,fault=fail)
    if stage=='after_migration_commit':
        assert len(catalog.inventory_observations(second))==1
        assert catalog.migration_progress('manifest:'+second)['cursor']==1
    else:
        assert catalog.inventory_observations(second)==[] and catalog.migration_progress('manifest:'+second) is None
        ingest(catalog,second,items2)
    assert source(catalog)==before
