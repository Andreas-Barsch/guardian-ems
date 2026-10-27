"""Explicit flat-root discovery job. Never invoked by a reader or warm start.

Directory iterators are not durable. After process loss, bounded calls replay the
explicit root from its beginning, skipping durable seen entries. Replay work is
reported; it is not sold as incremental normal startup. One live iterator/owner.
"""
import json
import os
from pathlib import Path
import stat
import uuid

from projection_repository import Conflict,Unavailable
from source_catalog import MAX_PAGE,digest


class LegacySourceInventory:
    def __init__(self,catalog):
        self.catalog=catalog
        self._iterator=None
        self._key=None

    def close(self):
        if self._iterator is not None:self._iterator.close()
        self._iterator=None;self._key=None

    @staticmethod
    def _root_signature(path):
        s=Path(path).lstat()
        if not stat.S_ISDIR(s.st_mode):raise ValueError('explicit non-symlink directory root required')
        return (s.st_dev,s.st_ino,s.st_mtime_ns)

    def create(self,roots):
        if not 1<=len(roots)<=16:raise ValueError('1..16 explicit roots required')
        normalized=[]
        for path,namespace in roots:
            path=Path(path)
            if not path.is_absolute() or namespace not in ('legacy-local','utc'):
                raise ValueError('absolute root and namespace required')
            normalized.append((str(path),namespace,*self._root_signature(path)))
        if len({r[0] for r in normalized})!=len(normalized):raise ValueError('duplicate root')
        job=uuid.uuid4().hex
        with self.catalog.write() as db:
            db.execute("INSERT INTO inventory(id,status) VALUES(?,'private')",(job,))
            db.executemany('INSERT INTO inventory_root VALUES(?,?,?,?,?,?,?)',
                           [(job,i,*r) for i,r in enumerate(normalized)])
        return job

    def status(self,job):
        with self.catalog.connection() as db:
            row=db.execute('SELECT * FROM inventory WHERE id=?',(job,)).fetchone()
            if row is None:raise Unavailable('inventory absent')
            roots=[dict(r) for r in db.execute('SELECT * FROM inventory_root WHERE inventory=? ORDER BY ordinal',(job,))]
            return {**dict(row),'roots':roots,'scope':'flat explicit roots; contents observed individually; unknown coverage retained'}

    def step(self,job,*,max_entries=MAX_PAGE,fault=lambda _:None):
        if not 1<=max_entries<=MAX_PAGE:raise ValueError('invalid discovery page')
        examined=new=0
        try:
            with self.catalog.write() as db:
                state=db.execute('SELECT * FROM inventory WHERE id=?',(job,)).fetchone()
                if state is None:raise Unavailable('inventory absent')
                if state['status']=='complete':return {'examined':0,'new':0,'status':'complete'}
                if state['status']!='private':raise Conflict('inventory scope changed; start a new explicit job')
                root=db.execute('SELECT * FROM inventory_root WHERE inventory=? AND ordinal=?',
                                (job,state['current_root'])).fetchone()
                if root is None:raise Conflict('missing root checkpoint')
                expected=(root['dev'],root['ino'],root['mtime'])
                if self._root_signature(root['path'])!=expected:
                    self.close();db.execute("UPDATE inventory SET status='scope_changed' WHERE id=?",(job,))
                    return {'examined':0,'new':0,'status':'scope_changed'}
                key=(job,root['ordinal'])
                if key!=self._key:
                    self.close();self._iterator=os.scandir(root['path']);self._key=key
                finished=False
                for _ in range(max_entries):
                    try:entry=next(self._iterator)
                    except StopIteration:finished=True;break
                    examined+=1
                    if db.execute('SELECT 1 FROM inventory_entry WHERE inventory=? AND root=? AND name=?',
                                  (job,root['ordinal'],entry.name)).fetchone():continue
                    path=str(Path(root['path'])/entry.name)
                    st=entry.stat(follow_symlinks=False)
                    if stat.S_ISREG(st.st_mode):
                        kind='source' if entry.name.endswith('.jsonl') else 'helper'
                    else:kind='unknown'
                    item={'kind':kind,'path':path}
                    if kind=='source':
                        sig=(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
                        source='legacy:'+digest(json.dumps([path,st.st_dev,st.st_ino]).encode())
                        revision=digest(json.dumps(sig).encode())
                        item={'kind':'source','registration':dict(source=source,revision=revision,path=path,
                              namespace=root['namespace'],writer_epoch='legacy-file:'+source.split(':',1)[1],receipt='observed:'+revision,
                              frontier=st.st_size,observed_signature=sig,
                              context={'root':root['path'],'filename':entry.name,
                                       'durability':'legacy_observation_not_writer_fsync'}),
                              'observation':{'inventory':job,'root':root['ordinal'],'name':entry.name}}
                    db.execute('INSERT INTO inventory_entry(inventory,root,name,kind,item) VALUES(?,?,?,?,?)',
                               (job,root['ordinal'],entry.name,kind,json.dumps(item,sort_keys=True)))
                    column={'source':'sources','helper':'helpers','unknown':'unknown'}[kind]
                    db.execute(f'UPDATE inventory SET {column}={column}+1 WHERE id=?',(job,));new+=1
                fault('after_discovery_entries')
                if self._root_signature(root['path'])!=expected:
                    self.close();db.execute("UPDATE inventory SET status='scope_changed' WHERE id=?",(job,))
                    finished=False
                elif finished:
                    self.close()
                    following=db.execute('SELECT 1 FROM inventory_root WHERE inventory=? AND ordinal=?',
                                         (job,root['ordinal']+1)).fetchone()
                    db.execute('UPDATE inventory SET current_root=current_root+1,status=? WHERE id=?',
                               ('private' if following else 'complete',job))
                db.execute('UPDATE inventory SET examined=examined+? WHERE id=?',(examined,job))
                fault('after_discovery_checkpoint')
                result=db.execute('SELECT status FROM inventory WHERE id=?',(job,)).fetchone()[0]
            fault('after_discovery_commit')
            return {'examined':examined,'new':new,'replayed':examined-new,'status':result}
        except BaseException:
            # A rolled-back batch consumed iterator entries: restart/replay safely.
            self.close();raise

    def page(self,job,*,after_id=0,limit=MAX_PAGE):
        if not 1<=limit<=MAX_PAGE:raise ValueError('invalid inventory page')
        with self.catalog.connection() as db:
            db.execute('BEGIN')
            state=db.execute('SELECT status FROM inventory WHERE id=?',(job,)).fetchone()
            if state is None or state[0]!='complete':raise Conflict('inventory scope not closed')
            rows=db.execute('SELECT id,item FROM inventory_entry WHERE inventory=? AND id>? ORDER BY id LIMIT ?',
                            (job,after_id,limit)).fetchall()
            return [{'id':r['id'],'item':json.loads(r['item'])} for r in rows]
