"""Explicit existing hard-delete operation over confirmed prepared stores.

No automatic deletion, tombstones or new API. The ordinary service holds its
stable transaction lock. A bounded durable intent resumes an interrupted
requested deletion; it contains no event payload and is removed on completion.
"""
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid
from raw_registry import MAX_BLOCK,MAX_META,encoded,hashed,file_signature
from projection_repository import Conflict,Unavailable
from research_budget import check_budget


class PreparedMaintenanceMutation:
    def __init__(self,log,reader,*,subscribers=(),progress=None,fault=lambda stage:None):
        self.log,self.reader=log,reader
        self.writer=reader.writer;self.tail=reader.tail
        self.ledger=log.path.with_suffix(log.path.suffix+'.retired-requests')
        self.intent_path=log.path.with_suffix(log.path.suffix+'.operation')
        self.subscribers=tuple(subscribers)
        self.progress=progress or self.tail.step
        self.fault=fault;self._lock=threading.RLock()
        if self.tail.mapping.get(self.writer.source(self.ledger))!='retired':
            raise ValueError('explicit retired-request source mapping required')

    def pending(self):return self.intent_path.exists()

    def assert_writable(self):
        if self.pending():raise Unavailable('requested maintenance deletion is being recovered')

    def _state(self):
        return self.writer.registry._read(self.intent_path,MAX_META) if self.pending() else None

    def _save(self,state):
        self.writer.registry._atomic(self.intent_path,state,'maintenance_operation')

    def _caught_up(self,frontier=None):
        head=self.writer.registry._head()['seq'] if frontier is None else frontier
        if self.tail._consumer(self.tail.current()).cursor()[0]<=head:return False
        for subscriber in self.subscribers:
            consumer=getattr(subscriber,'consumer',subscriber)
            if consumer.cursor()[0]<=head:return False
            if hasattr(subscriber,'catalog') and subscriber.catalog.status()['pending']:return False
        return True

    def request_retired(self,request_id):
        self.assert_writable()
        digest=hashlib.sha256(request_id.encode('ascii')).hexdigest()
        receipt=self.writer.registry.state(self.writer.source(self.ledger))
        if receipt is None:
            if self.ledger.exists() and self.ledger.stat().st_size:
                raise Unavailable('existing retired-request ledger requires explicit preparation')
            return False
        with self.reader.reader.capture() as reader:
            with self.tail.repository._connection() as db:
                row=db.execute('SELECT receipt FROM metadata_registry_source WHERE revision=? AND registry=? AND source=?',
                    (reader.revision,self.writer.registry.config['registry_id'],self.writer.source(self.ledger))).fetchone()
            if row is None or row[0]!=hashed(encoded(receipt)):
                raise Unavailable('retired-request metadata pending')
            return reader.maintenance_request_retired(digest)

    def delete(self,event_id,request_id=None):
        with self._lock:
            self.assert_writable()
            receipt=self.writer.registry.state(self.reader.source)
            if receipt is not None and not self._caught_up(receipt['seq']):
                raise Unavailable('maintenance deletion awaits confirmed metadata consumers')
            if receipt is None or file_signature(self.log.path)!=receipt['signature']:
                raise Conflict('maintenance source differs from confirmed deletion input')
            operation=uuid.uuid4().hex
            temporary=self.log.path.with_name('.maintenance-replacement-'+operation)
            installed=False;removed=0
            try:
                with temporary.open('xb') as target,self.log.path.open('rb') as source:
                    os.fchmod(target.fileno(),self.log.path.stat().st_mode & 0o777)
                    while True:
                        check_budget();line=source.readline(MAX_BLOCK+1)
                        if not line:break
                        if len(line)>MAX_BLOCK or not line.endswith(b'\n'):
                            raise Conflict('maintenance input exceeds complete record budget')
                        if line.strip() and json.loads(line)['maintenance_event_id']==event_id:
                            removed+=1
                        else:target.write(line)
                    target.flush();os.fsync(target.fileno())
                if not removed:raise Conflict('selected maintenance event absent from confirmed file')
                if file_signature(self.log.path)!=receipt['signature']:
                    raise Conflict('maintenance source changed while preparing deletion')
                self.writer.registry._dirsync(temporary.parent)
                state={'contract':'guardian-maintenance-delete-1','operation':operation,
                    'source':self.reader.source,'path':str(self.log.path.resolve()),
                    'temporary':str(temporary.resolve()),'expected_receipt':hashed(encoded(receipt)),
                    'revision':'delete-'+operation,'request_digest':None if request_id is None else hashlib.sha256(request_id.encode('ascii')).hexdigest(),
                    'phase':'prepared'}
                self._save(state);installed=True;self.fault('after_delete_intent')
                while self.pending():
                    check_budget();self.step()
            finally:
                if not installed and temporary.exists():temporary.unlink()

    def step(self):
        if not self._lock.acquire(blocking=False):return {'status':'busy','raw_bytes':0,'decodes':0}
        try:
            state=self._state()
            if state is None:return {'status':'caught_up','raw_bytes':0,'decodes':0}
            if (state['contract']!='guardian-maintenance-delete-1' or state['source']!=self.reader.source
                    or state['path']!=str(self.log.path.resolve())):
                raise Conflict('maintenance deletion binding changed')
            phase=state['phase'];registry=self.writer.registry
            if phase=='prepared':
                if state['request_digest'] is not None:
                    self.writer.append(self.ledger,(state['request_digest']+'\n').encode('ascii'),
                        operation='retire:'+state['operation'])
                self.fault('after_retired_request')
                state['phase']='retired';self._save(state)
            elif phase=='retired':
                registry.replace_file(state['source'],state['revision'],state['temporary'],
                    expected_receipt=state['expected_receipt'],operation='delete:'+state['operation'])
                self.fault('after_deleted_file')
                state['phase']='replaced';self._save(state)
            elif phase=='replaced':
                registry.recover();receipt=registry.state(state['source'])
                if receipt['revision']!=state['revision']:raise Conflict('maintenance deletion revision changed')
                if receipt['end']<self.log.path.stat().st_size:
                    confirmed=registry.adopt_tail(state['source'],expected_receipt=hashed(encoded(receipt)),
                        operation='delete-adopt:'+state['operation']+':'+str(receipt['end']))
                    return {'status':'confirming_replacement','raw_bytes':confirmed['end']-confirmed['start'],'decodes':0}
                state['phase']='publishing';state['frontier']=registry._head()['seq'];self._save(state)
            elif phase=='publishing':
                if not self._caught_up(state['frontier']):
                    result=self.progress()
                    return {'status':'publishing_deletion',**result}
                self.fault('after_deleted_metadata')
                self.intent_path.unlink();registry._dirsync(self.intent_path.parent)
            else:raise Conflict('unknown maintenance deletion phase')
            return {'status':'deleting','raw_bytes':0,'decodes':0}
        finally:self._lock.release()
