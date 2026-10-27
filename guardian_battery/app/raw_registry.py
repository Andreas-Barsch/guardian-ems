"""Unactivated single-writer raw registry, independent of any SQLite database.

Immutable bounded receipt files are grouped into numbered segments. A tiny atomic
head is the commit authority. One durable intent permits bounded crash recovery;
unacknowledged raw bytes are never truncated. No implicit directories/production
paths, discovery, hardware access or claim of device power-loss durability.
"""
from contextlib import contextmanager
from pathlib import Path
import base64
import fcntl
import hashlib
import json
import os
import uuid

MAX_BLOCK = 65536
MAX_META = 16384
MAX_INTENT = 131072
ZERO = '0' * 64


class RegistryError(RuntimeError):pass
class RegistryConflict(RegistryError):pass
class RegistryCorrupt(RegistryError):pass
class UnconfirmedTail(RegistryConflict):pass


def encoded(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def hashed(value):return hashlib.sha256(value).hexdigest()


def file_signature(path):
    s=Path(path).stat()
    return [s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]


class RawRegistry:
    def __init__(self,root,*,fault=lambda _:None):
        self.root=Path(root);self.fault=fault;self.reset_metrics()
        self.config=self._read(self.root/'format.json',MAX_META)
        if self.config.get('schema')!=1 or not 1<=self.config.get('segment_records',0)<=256:
            raise RegistryCorrupt('unsupported registry format')
        self._head()  # Constant metadata only; no recovery mutation or directory scan.

    @classmethod
    def create(cls,root,*,segment_records=64):
        if not 1<=segment_records<=256:raise ValueError('invalid segment size')
        root=Path(root);root.mkdir(exist_ok=False)
        obj=object.__new__(cls);obj.root=root;obj.fault=lambda _:None;obj.reset_metrics()
        (root/'segments').mkdir();(root/'sources').mkdir();(root/'writer.lock').touch(exist_ok=False)
        obj._dirsync(root);obj._dirsync(root.parent)
        obj.config={'schema':1,'registry_id':uuid.uuid4().hex,'segment_records':segment_records}
        obj._atomic(root/'format.json',obj.config,'format')
        obj._atomic(root/'head.json',{'registry_id':obj.config['registry_id'],'seq':0,'hash':ZERO},'head')
        return cls(root)

    def reset_metrics(self):
        self.metrics={'metadata_reads':0,'metadata_bytes':0,'raw_reads':0,'raw_bytes_read':0,
                      'raw_bytes_written':0,'file_fsyncs':0,'directory_fsyncs':0}

    def _read(self,path,limit):
        try:
            with Path(path).open('rb') as h:raw=h.read(limit+1)
            self.metrics['metadata_reads']+=1;self.metrics['metadata_bytes']+=len(raw)
            if len(raw)>limit:raise RegistryCorrupt('metadata limit exceeded')
            envelope=json.loads(raw);body=envelope['body']
            if hashed(encoded(body))!=envelope['hash']:raise RegistryCorrupt('metadata checksum mismatch')
            return body
        except (OSError,ValueError,KeyError,TypeError) as error:
            raise RegistryCorrupt('registry metadata missing or invalid: '+Path(path).name) from error

    def _dirsync(self,path):
        fd=os.open(path,os.O_RDONLY)
        try:os.fsync(fd);self.metrics['directory_fsyncs']+=1
        finally:os.close(fd)

    def _atomic(self,path,body,label,limit=MAX_META):
        raw=encoded({'body':body,'hash':hashed(encoded(body))})
        if len(raw)>limit:raise ValueError('metadata limit exceeded')
        temporary=Path(str(path)+'.pending')
        with temporary.open('wb') as h:
            h.write(raw);h.flush();self.fault(label+':file_flush')
            os.fsync(h.fileno());self.metrics['file_fsyncs']+=1;self.fault(label+':file_fsync')
        os.replace(temporary,path);self._dirsync(Path(path).parent);self.fault(label+':dir_fsync')

    @contextmanager
    def _writer(self):
        with (self.root/'writer.lock').open('rb') as lock:
            try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError as error:raise RegistryConflict('raw registry writer busy') from error
            try:yield
            finally:fcntl.flock(lock.fileno(),fcntl.LOCK_UN)

    def _head(self):
        h=self._read(self.root/'head.json',MAX_META)
        if h.get('registry_id')!=self.config['registry_id'] or not isinstance(h.get('seq'),int) or h['seq']<0:
            raise RegistryCorrupt('invalid registry head')
        if not isinstance(h.get('hash'),str) or len(h['hash'])!=64 or (h['seq']==0 and h['hash']!=ZERO):
            raise RegistryCorrupt('invalid head hash')
        return h

    def _receipt_path(self,seq):
        segment=(seq-1)//self.config['segment_records']
        return self.root/'segments'/f'{segment:016d}'/f'{seq:020d}.json'

    def _receipt(self,seq):
        r=self._read(self._receipt_path(seq),MAX_META)
        if r.get('seq')!=seq or r.get('registry_id')!=self.config['registry_id']:
            raise RegistryCorrupt('receipt identity mismatch')
        return r

    def _state_path(self,source):return self.root/'sources'/(hashed(source.encode())+'.json')

    def _checkpoint(self,receipt):
        marker=self._state_path(receipt['source']).with_suffix('.known')
        if not marker.exists():
            self._atomic(marker,{'registry_id':self.config['registry_id'],'source':receipt['source']},'source_marker')
        self._atomic(self._state_path(receipt['source']),{'receipt':receipt,'hash':hashed(encoded(receipt))},'checkpoint')

    def _state(self,source,head,*,repair_tip=False):
        path=self._state_path(source)
        marker=path.with_suffix('.known')
        if marker.exists():
            if self._read(marker,MAX_META)!={'registry_id':self.config['registry_id'],'source':source}:
                raise RegistryCorrupt('source marker mismatch')
            if not path.exists() and not repair_tip:raise RegistryCorrupt('source checkpoint missing')
        if not path.exists():return None
        state=self._read(path,MAX_META);r=state['receipt']
        if (r['source']!=source or r['registry_id']!=self.config['registry_id'] or r['seq']>head['seq']
                or hashed(encoded(r))!=state['hash'] or self._receipt(r['seq'])!=r):
            raise RegistryCorrupt('source checkpoint inconsistent')
        return r

    def _repair_tip(self,head):
        if not head['seq']:return
        r=self._receipt(head['seq'])
        if hashed(encoded(r))!=head['hash']:raise RegistryCorrupt('head receipt mismatch')
        current=self._state(r['source'],head,repair_tip=True)
        if current is None or current['seq']<r['seq']:self._checkpoint(r)

    def state(self,source):
        """Read confirmed receipt; use recover explicitly after uncertain writer ACK."""
        h=self._head()
        if h['seq']:
            tip=self._receipt(h['seq'])
            if hashed(encoded(tip))!=h['hash']:raise RegistryCorrupt('head receipt mismatch')
            if tip['source']==source:return tip
        return self._state(source,h)

    def _raw_range(self,path,start,end):
        if not 0<=end-start<=MAX_BLOCK:raise RegistryCorrupt('raw proof exceeds block budget')
        with Path(path).open('rb') as h:h.seek(start);raw=h.read(end-start)
        self.metrics['raw_reads']+=1;self.metrics['raw_bytes_read']+=len(raw)
        if len(raw)!=end-start:raise RegistryConflict('raw source truncated')
        return raw

    def _verify_last_block(self,state):
        if state and state['end']>state['start']:
            if hashed(self._raw_range(state['path'],state['start'],state['end']))!=state['block_hash']:
                raise RegistryConflict('confirmed block changed; explicit new revision required')

    def _intent(self):
        path=self.root/'intent.json'
        return self._read(path,MAX_INTENT) if path.exists() else None

    def _clear_intent(self):
        (self.root/'intent.json').unlink();self._dirsync(self.root)

    def _replacement_recovery_proof(self,*,allow_replacement=True):
        intent=self._intent()
        if intent is None or intent['mode']!='replace':return None
        if not allow_replacement:
            raise RegistryConflict('explicit maintenance replacement recovery required')
        path=Path(intent['path']);sig=file_signature(path)
        if sig[:2]!=intent['file_id']:return None
        change=path.stat().st_ctime_ns
        # Recovery after rename proves all prepared bytes before taking the
        # shared writer lock. No slow hashing is hidden inside its commit.
        if self._replacement_hash(path)!=(intent['replacement_size'],intent['replacement_hash']):
            raise RegistryConflict('renamed replacement bytes changed')
        if file_signature(path)!=sig or path.stat().st_ctime_ns!=change:
            raise RegistryConflict('replacement changed during recovery proof')
        return {'operation':intent['operation'],'signature':sig,'change_time_ns':change}

    def recover(self,*,allow_replacement=True):
        proof=self._replacement_recovery_proof(allow_replacement=allow_replacement)
        with self._writer():return self._recover(replacement_proof=proof)

    def _recover(self,*,replacement_proof=None):
        head=self._head();self._repair_tip(head);intent=self._intent()
        if intent is None:return None
        if intent['registry_id']!=self.config['registry_id']:raise RegistryCorrupt('foreign intent')
        if head['seq']==intent['seq']:
            receipt=self._receipt(head['seq'])
            if any(receipt[k]!=intent[k] for k in ('operation','source','revision','mode','block_hash','source_previous','previous_hash')):
                raise RegistryCorrupt('committed intent mismatch')
            if intent['mode']=='replace':self._finish_replacement_file(intent,receipt,replacement_proof)
            self._clear_intent();return receipt
        if head['seq']+1!=intent['seq'] or head['hash']!=intent['previous_hash']:
            raise RegistryCorrupt('pending intent not adjacent to head')
        return self._finish(intent,head,replacement_proof=replacement_proof)

    def _finish(self,intent,head,*,replacement_proof=None):
        source=intent['source'];previous=self._state(source,head)
        expected=None if previous is None else hashed(encoded(previous))
        if expected!=intent['source_previous']:raise RegistryConflict('source receipt compare-and-swap failed')
        if intent['mode'] not in ('register','replace'):self._verify_last_block(previous)
        data=base64.b64decode(intent['payload'],validate=True)
        if len(data)>MAX_BLOCK or hashed(data)!=intent['block_hash']:raise RegistryCorrupt('intent payload invalid')
        path=Path(intent['path'])
        if intent['mode']=='replace':
            self._finish_replacement_file(intent,previous,replacement_proof)
        sig=file_signature(path)
        if sig[:2]!=intent['file_id']:raise RegistryConflict('raw file identity changed')
        start=intent['start'];end=start+len(data);mode=intent['mode']
        if mode=='append':
            if not start<=sig[2]<=end:raise UnconfirmedTail('raw tail outside pending append')
            prefix=self._raw_range(path,start,sig[2])
            if prefix!=data[:len(prefix)]:raise UnconfirmedTail('pending raw prefix differs; never truncated')
            with path.open('r+b') as h:
                h.seek(sig[2]);written=h.write(data[len(prefix):]);self.metrics['raw_bytes_written']+=written
                if written!=len(data)-len(prefix):raise OSError('short raw write')
                self.fault('after_raw_write');h.flush();self.fault('after_raw_flush')
                os.fsync(h.fileno());self.metrics['file_fsyncs']+=1;self.fault('after_raw_fsync')
        elif mode=='adopt':
            if self._raw_range(path,start,end)!=data:raise UnconfirmedTail('observed tail changed')
            with path.open('rb') as h:os.fsync(h.fileno());self.metrics['file_fsyncs']+=1
            self.fault('after_raw_fsync')
        elif mode in ('register','replace'):
            if data:raise RegistryCorrupt('registration payload must be empty')
            with path.open('rb') as h:os.fsync(h.fileno());self.metrics['file_fsyncs']+=1
            self.fault('after_raw_fsync')
        else:raise RegistryCorrupt('unknown pending operation')
        self._dirsync(path.parent);self.fault('after_raw_dir_fsync')
        sig=file_signature(path)
        if sig[:2]!=intent['file_id'] or sig[2]<end:raise RegistryConflict('raw source changed during persistence')
        receipt={key:intent[key] for key in ('registry_id','seq','previous_hash','source','revision','writer_epoch',
                    'namespace','path','file_id','operation','mode','source_previous','previous_revision','start','block_hash','block_previous')}
        receipt.update(end=end,signature=sig,change_time_ns=path.stat().st_ctime_ns,lines=data.count(b'\n'),
                       block_chain=hashed(encoded([intent['block_previous'],start,end,intent['block_hash']])))
        target=self._receipt_path(receipt['seq'])
        if not target.parent.exists():target.parent.mkdir();self._dirsync(target.parent.parent)
        if target.exists():
            if self._receipt(receipt['seq'])!=receipt:raise RegistryCorrupt('orphan receipt differs from pending operation')
        else:self._atomic(target,receipt,'receipt')
        self.fault('after_receipt')
        self._atomic(self.root/'head.json',{'registry_id':self.config['registry_id'],'seq':receipt['seq'],
                                          'hash':hashed(encoded(receipt))},'head')
        self.fault('after_head');self._checkpoint(receipt);self.fault('after_checkpoint')
        self._clear_intent();self.fault('after_pending_clear')
        return receipt

    def register(self,source,revision,path,*,writer_epoch,namespace,operation,expected_receipt=None,allow_existing=False):
        """Register before first confirmed bytes; existing data stays unconfirmed."""
        if any(not isinstance(v,str) or not v or len(v)>512 for v in (source,revision,writer_epoch,operation)):
            raise ValueError('bounded explicit identifiers required')
        path=Path(path)
        if not path.is_absolute() or namespace not in ('legacy-local','utc'):raise ValueError('explicit absolute path/namespace required')
        with self._writer():
            self._recover();head=self._head();old=self._state(source,head)
            if old and old['operation']==operation:
                if (old['mode'],old['revision'],old['path'],old['writer_epoch'],old['namespace']) != ('register',revision,str(path),writer_epoch,namespace):
                    raise RegistryConflict('operation reused with different registration')
                return old
            if (None if old is None else hashed(encoded(old)))!=expected_receipt:raise RegistryConflict('source receipt changed')
            if old and (revision==old['revision'] or str(path)!=old['path']):raise RegistryConflict('new explicit revision or source ID required')
            if not path.exists():
                with path.open('xb') as h:h.flush();os.fsync(h.fileno());self.metrics['file_fsyncs']+=1
                self._dirsync(path.parent)
            sig=file_signature(path)
            if old and sig[:2]!=old['file_id']:raise RegistryConflict('rotation requires new source ID')
            if sig[2] and not allow_existing:raise UnconfirmedTail('nonempty source requires explicit unconfirmed registration')
            intent=self._make_intent(head,old,source,revision,str(path),writer_epoch,namespace,operation,'register',b'',sig,0)
            self._atomic(self.root/'intent.json',intent,'intent',MAX_INTENT);self.fault('after_intent')
            return self._finish(intent,head)

    @staticmethod
    def _replacement_hash(path):
        # Explicit mutation/recovery only, never a GET fallback. Each byte read
        # is bounded and cooperatively cancellable; total selected-file cost is
        # inherent in the existing hard-delete rewrite contract.
        from research_budget import check_budget
        digest=hashlib.sha256();size=0
        with Path(path).open('rb') as handle:
            while True:
                check_budget();block=handle.read(MAX_BLOCK)
                if not block:break
                digest.update(block);size+=len(block)
        return size,digest.hexdigest()

    def _finish_replacement_file(self,intent,previous,proof):
        path=Path(intent['path']);temporary=Path(intent['replacement_path'])
        if temporary.parent!=path.parent or temporary==path:
            raise RegistryCorrupt('replacement path must be a distinct sibling')
        current=file_signature(path)
        prepared_signature=intent['replacement_signature']
        if current[:2]==intent['file_id']:
            # Rename completed before an interrupted receipt/head publication.
            if (proof is None or proof['operation']!=intent['operation'] or proof['signature']!=current
                    or proof['change_time_ns']!=path.stat().st_ctime_ns):
                raise RegistryConflict('explicit replacement recovery proof required')
            return
        if previous is None or current!=previous['signature'] or ('change_time_ns' in previous and path.stat().st_ctime_ns!=previous['change_time_ns']):
            raise RegistryConflict('replacement original changed; no overwrite')
        self._verify_last_block(previous)
        observed=file_signature(temporary)
        if observed!=prepared_signature or temporary.stat().st_ctime_ns!=intent['replacement_change_time_ns']:
            raise RegistryConflict('prepared replacement changed')
        with temporary.open('rb') as handle:os.fsync(handle.fileno())
        self.fault('before_replacement_rename')
        if file_signature(path)!=current or file_signature(temporary)!=observed or temporary.stat().st_ctime_ns!=intent['replacement_change_time_ns']:
            raise RegistryConflict('replacement input changed before rename')
        os.replace(temporary,path)
        self.fault('after_replacement_rename')
        self._dirsync(path.parent)
        self.fault('after_replacement_dir_fsync')

    def replace_file(self,source,revision,temporary,*,expected_receipt,operation):
        """Explicit owned file replacement, followed by bounded adopt_tail calls.

        A durable rename intent binds both inodes, the prior receipt and all
        prepared bytes. Normal register/append continue to reject rotation.
        Consumers must have consumed the old prefix before this operation;
        the application deletion coordinator enforces that prerequisite.
        """
        self._operation(operation)
        if not isinstance(revision,str) or not revision or len(revision)>512:
            raise ValueError('bounded replacement revision required')
        temporary=Path(temporary).resolve()
        # Preparation can be large, unlike the commit. Hash outside the shared
        # registry writer lock; the commit/recovery binds the exact signature.
        prepared=None
        if temporary.exists():
            sig=file_signature(temporary);change_time=temporary.stat().st_ctime_ns
            size,digest=self._replacement_hash(temporary)
            if size!=sig[2] or file_signature(temporary)!=sig or temporary.stat().st_ctime_ns!=change_time:
                raise RegistryConflict('replacement preparation changed')
            prepared=(sig,size,digest,change_time)
        proof=self._replacement_recovery_proof()
        with self._writer():
            self._recover(replacement_proof=proof);head=self._head();old=self._state(source,head)
            if old and old['operation']==operation:
                if old['mode']!='replace' or old['revision']!=revision:
                    raise RegistryConflict('replacement operation reused')
                return old
            if old is None or hashed(encoded(old))!=expected_receipt or old['revision']==revision:
                raise RegistryConflict('replacement receipt/revision changed')
            path=Path(old['path'])
            if temporary.parent!=path.parent or temporary==path:
                raise RegistryConflict('replacement must be a distinct owned sibling')
            if file_signature(path)!=old['signature']:
                raise RegistryConflict('replacement source changed')
            self._verify_last_block(old)
            if prepared is None:raise RegistryConflict('replacement preparation missing')
            sig,size,digest,change_time=prepared
            if file_signature(temporary)!=sig or temporary.stat().st_ctime_ns!=change_time:raise RegistryConflict('replacement preparation changed')
            intent=self._make_intent(head,old,source,revision,str(path),old['writer_epoch'],
                old['namespace'],operation,'replace',b'',sig,0)
            intent.update(replacement_path=str(temporary),replacement_size=size,replacement_hash=digest,replacement_signature=sig,replacement_change_time_ns=change_time)
            self._atomic(self.root/'intent.json',intent,'intent',MAX_INTENT)
            self.fault('after_intent')
            return self._finish(intent,head)

    @staticmethod
    def _operation(operation):
        if not isinstance(operation,str) or not operation or len(operation)>512:
            raise ValueError('bounded operation identifier required')

    def _make_intent(self,head,old,source,revision,path,epoch,namespace,operation,mode,data,sig,start):
        return dict(registry_id=self.config['registry_id'],seq=head['seq']+1,previous_hash=head['hash'],source=source,
                    revision=revision,path=path,writer_epoch=epoch,namespace=namespace,operation=operation,mode=mode,
                    source_previous=None if old is None else hashed(encoded(old)),previous_revision=None if old is None else old['revision'],
                    file_id=sig[:2],start=start,block_hash=hashed(data),
                    block_previous=ZERO if mode in ('register','replace') else old['block_chain'],payload=base64.b64encode(data).decode())

    def append(self,source,data,*,expected_receipt,operation):
        self._operation(operation)
        if not isinstance(data,bytes) or not 0<len(data)<=MAX_BLOCK or not data.endswith(b'\n'):
            raise ValueError('bounded complete JSONL-line batch required')
        with self._writer():
            self._recover();head=self._head();old=self._state(source,head)
            if old is None:raise RegistryConflict('source must be registered first')
            if old['operation']==operation:
                if old['mode']!='append' or old['block_hash']!=hashed(data):raise RegistryConflict('operation reused with different payload')
                return old
            if hashed(encoded(old))!=expected_receipt:raise RegistryConflict('source receipt changed')
            sig=file_signature(old['path'])
            if sig!=old['signature'] or ('change_time_ns' in old and Path(old['path']).stat().st_ctime_ns!=old['change_time_ns']):raise RegistryConflict('source changed; recover tail or register revision explicitly')
            if sig[2]!=old['end']:raise UnconfirmedTail('unconfirmed tail exists')
            intent=self._make_intent(head,old,source,old['revision'],old['path'],old['writer_epoch'],old['namespace'],operation,'append',data,sig,old['end'])
            self._atomic(self.root/'intent.json',intent,'intent',MAX_INTENT);self.fault('after_intent')
            return self._finish(intent,head)

    def inspect_tail(self,source,*,max_bytes=MAX_BLOCK):
        if not 1<=max_bytes<=MAX_BLOCK:raise ValueError('invalid tail budget')
        old=self.state(source)
        if old is None:raise RegistryConflict('source unregistered')
        sig=file_signature(old['path'])
        if sig[:2]!=old['file_id'] or sig[2]<old['end']:raise RegistryConflict('source identity/truncation requires revision')
        data=self._raw_range(old['path'],old['end'],min(sig[2],old['end']+max_bytes))
        length=data.rfind(b'\n')+1
        return {'unconfirmed_bytes':sig[2]-old['end'],'complete_bytes':length,'bytes_read':len(data),'payload':data[:length]}

    def adopt_tail(self,source,*,expected_receipt,operation,max_bytes=MAX_BLOCK):
        """Explicit confirmation of owned unacknowledged bytes; never auto-import."""
        self._operation(operation)
        with self._writer():
            self._recover();head=self._head();old=self._state(source,head)
            if old and old['operation']==operation and old['mode']=='adopt':return old
            if old is None or hashed(encoded(old))!=expected_receipt:raise RegistryConflict('source receipt changed')
            tail=self.inspect_tail(source,max_bytes=max_bytes);data=tail['payload']
            if not data:raise UnconfirmedTail('no complete tail line within budget')
            sig=file_signature(old['path'])
            intent=self._make_intent(head,old,source,old['revision'],old['path'],old['writer_epoch'],old['namespace'],operation,'adopt',data,sig,old['end'])
            self._atomic(self.root/'intent.json',intent,'intent',MAX_INTENT);self.fault('after_intent')
            return self._finish(intent,head)

    def read_page(self,*,next_seq=1,previous_hash=ZERO,max_records=32,max_bytes=MAX_BLOCK):
        if not 1<=max_records<=64 or not MAX_META<=max_bytes<=MAX_BLOCK:raise ValueError('invalid journal page budget')
        head=self._head();records=[];used=0;chain=previous_hash
        if not isinstance(next_seq,int) or not 1<=next_seq<=head['seq']+1:
            raise RegistryConflict('invalid journal cursor')
        if (next_seq==1 and previous_hash!=ZERO) or (next_seq==head['seq']+1 and previous_hash!=head['hash']):
            raise RegistryConflict('journal cursor hash mismatch')
        for seq in range(next_seq,min(head['seq']+1,next_seq+max_records)):
            path=self._receipt_path(seq)
            try:size=path.stat().st_size
            except OSError as error:raise RegistryCorrupt('committed journal receipt missing') from error
            if size>MAX_META:raise RegistryCorrupt('oversized committed receipt')
            if used+size>max_bytes:break
            r=self._receipt(seq);used+=size
            if r['previous_hash']!=chain:raise RegistryCorrupt('journal chain mismatch')
            chain=hashed(encoded(r));records.append(r)
        if next_seq+len(records)==head['seq']+1 and chain!=head['hash']:
            raise RegistryCorrupt('journal tip differs from head')
        return {'records':records,'next_seq':next_seq+len(records),'previous_hash':chain,'metadata_bytes':used,
                'head_seq':head['seq'],'caught_up':next_seq+len(records)>head['seq']}
