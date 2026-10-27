"""Explicit injected writer binding; no production paths or implicit selection.

The existing writer chooses its existing filename/record format. This adapter
adds the durable RawRegistry receipt contract and a scheduler Raw section. UTC
namespace selection stays forbidden until a separate compatibility gate exists.
"""
from contextlib import nullcontext
from pathlib import Path
import json
import threading
import uuid
from raw_registry import MAX_BLOCK,encoded,hashed,RegistryConflict


class ApplicationWriter:
    def __init__(self,registry,root,*,stream,writer_epoch,scheduler=None,notify=None,namespace='legacy-local'):
        if namespace!='legacy-local':raise ValueError('UTC writer selection requires unavailable F/R release approval')
        self.registry=registry;self.root=Path(root).resolve();self.stream=stream
        self.writer_epoch=writer_epoch;self.scheduler=scheduler;self.notify=notify
        self._lock=threading.RLock()
    def source(self,path):
        path=Path(path).resolve()
        try:relative=path.relative_to(self.root)
        except ValueError as exc:raise RegistryConflict('writer path outside explicit candidate root') from exc
        return self.stream+'/'+relative.as_posix()
    def append(self,path,payload,*,operation=None):
        if not isinstance(payload,bytes) or not payload or len(payload)>MAX_BLOCK or not payload.endswith(b'\n'):
            raise ValueError('complete bounded Raw block required')
        path=Path(path).resolve();source=self.source(path)
        with self._lock,(self.scheduler.raw_section() if self.scheduler else nullcontext()):
            self.registry.recover(allow_replacement=False)
            state=self.registry.state(source)
            if state is None:
                path.parent.mkdir(parents=True,exist_ok=True)
                state=self.registry.register(source,'initial',path,writer_epoch=self.writer_epoch,namespace='legacy-local',operation='register:'+source)
            if state['path']!=str(path) or state['writer_epoch']!=self.writer_epoch:raise RegistryConflict('writer binding changed')
            receipt=self.registry.append(source,payload,expected_receipt=hashed(encoded(state)),operation=uuid.uuid4().hex if operation is None else operation)
        # A missed wake cannot lose durable work: the consumer resumes its journal.
        if self.notify is not None:self.notify()
        return receipt
    def last_record(self,path):
        self.registry.recover(allow_replacement=False)
        source=self.source(path);state=self.registry.state(source)
        if state is None or not state['end']:return None
        raw=self.registry._raw_range(state['path'],state['start'],state['end'])
        if hashed(raw)!=state['block_hash']:raise RegistryConflict('writer tip changed')
        lines=[line for line in raw.splitlines() if line.strip()]
        return json.loads(lines[-1]) if lines else None
