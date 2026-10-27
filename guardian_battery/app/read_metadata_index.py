"""Explicit immutable prepared metadata; real identity/timeline DTOs unchanged."""
import json
import uuid
from functools import wraps
import hashlib
from projection_repository import Conflict
import math
from datetime import datetime,timezone
from research_budget import check_budget,ResearchQueryError
from maintenance import normalize_utc_timestamp
from research_identity import ResearchIdentityResolver
from timeline import TimelineEvent

SCHEMA='''
CREATE TABLE IF NOT EXISTS read_metadata(revision TEXT PRIMARY KEY,identity_ready INTEGER DEFAULT 0,timeline_ready INTEGER DEFAULT 0,identity_input TEXT,timeline_input TEXT);
CREATE TABLE IF NOT EXISTS read_metadata_reader(token TEXT PRIMARY KEY,revision TEXT REFERENCES read_metadata);
CREATE INDEX IF NOT EXISTS read_metadata_reader_revision ON read_metadata_reader(revision);
CREATE TABLE IF NOT EXISTS read_serial(revision TEXT,serial TEXT,PRIMARY KEY(revision,serial));
CREATE TABLE IF NOT EXISTS read_topology(revision TEXT,stamp REAL,payload TEXT,PRIMARY KEY(revision,stamp));
CREATE TABLE IF NOT EXISTS read_position(revision TEXT,stamp REAL,priority TEXT,payload TEXT,PRIMARY KEY(revision,stamp,priority));
CREATE TABLE IF NOT EXISTS read_identity_epoch(revision TEXT,serial TEXT,stamp REAL,identity TEXT,payload TEXT,PRIMARY KEY(revision,serial,stamp,identity));
CREATE TABLE IF NOT EXISTS read_timeline(revision TEXT,stamp REAL,identity TEXT,payload TEXT,PRIMARY KEY(revision,stamp,identity));
'''

def stamp(value):return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()


def protected_read(method):
    @wraps(method)
    def read(self,*args,**kwargs):
        from lifecycle_leases import read_scope,release_reader
        token=uuid.uuid4().hex
        try:
            with self.repository._write() as db:
                if not db.execute('SELECT 1 FROM read_metadata WHERE revision=?',(self.revision,)).fetchone():raise ResearchQueryError('history_preparation_required','Prepared metadata revision unavailable',503)
                db.execute('INSERT INTO read_metadata_reader VALUES(?,?)',(token,self.revision))
        except Conflict as exc:
            raise ResearchQueryError('history_preparation_required',str(exc),503) from exc
        try:
            with read_scope(self.repository,token,'read_metadata_reader'):
                return method(self,*args,**kwargs)
        finally:release_reader(self.repository,token,'read_metadata_reader')
    return read


class PreparedMetadata:
    def __init__(self,repository,revision):
        self.repository,self.revision=repository,revision

    def initialize(self):
        with self.repository._connection() as db:db.executescript('BEGIN IMMEDIATE;'+SCHEMA+'COMMIT;')
        with self.repository._write() as db:db.execute('INSERT OR IGNORE INTO read_metadata(revision) VALUES(?)',(self.revision,))

    def _ready(self,field):
        with self.repository._connection() as db:row=db.execute('SELECT '+field+' FROM read_metadata WHERE revision=?',(self.revision,)).fetchone()
        if not row or not row[0]:raise ResearchQueryError('history_preparation_required','Prepared metadata revision unavailable',503)

    def _claim(self,field,fingerprint):
        with self.repository._write() as db:
            row=db.execute('SELECT '+field+' FROM read_metadata WHERE revision=?',(self.revision,)).fetchone()
            if row is None:raise Conflict('metadata preparation not initialized')
            if row[0] is not None and row[0]!=fingerprint:raise Conflict('immutable metadata input changed; use a new revision')
            db.execute('UPDATE read_metadata SET '+field+'=? WHERE revision=?',(fingerprint,self.revision))

    def prepare_identity(self,resolver):
        """Explicit preparation from the actual resolver, never called by reads.

        The original resolver computes epoch origins/IDs. Preparation can be
        retried only with the same immutable input revision.
        """
        digest=hashlib.sha256()
        for snapshot in resolver.snapshots:digest.update(json.dumps(snapshot.to_dict(),sort_keys=True).encode())
        self._claim('identity_input',digest.hexdigest())
        with self.repository._connection() as db:
            row=db.execute('SELECT identity_ready FROM read_metadata WHERE revision=?',(self.revision,)).fetchone()
            if row and row[0]:return
        for ordinal,item in enumerate(resolver.epochs()):
            check_budget()
            with self.repository._write() as db:
                db.execute('INSERT OR IGNORE INTO read_serial VALUES(?,?)',(self.revision,item['physical_serial']))
                db.execute('INSERT OR REPLACE INTO read_identity_epoch VALUES(?,?,?,?,?)',
                    (self.revision,item['physical_serial'],stamp(item['valid_from']),str(ordinal)+':'+item['identity_epoch_id'],json.dumps(item)))
        for snapshot in resolver.snapshots:
            check_budget();topology=resolver.topology_at(snapshot.effective_at)
            with self.repository._write() as db:
                db.execute('INSERT OR REPLACE INTO read_topology VALUES(?,?,?)',(self.revision,stamp(snapshot.effective_at),json.dumps(topology)))
                db.execute('INSERT OR REPLACE INTO read_position VALUES(?,?,?,?)',(self.revision,stamp(snapshot.effective_at),snapshot.created_at+'|'+snapshot.position_history_id,json.dumps(snapshot.to_dict())))
        with self.repository._write() as db:db.execute('UPDATE read_metadata SET identity_ready=1 WHERE revision=?',(self.revision,))

    def prepare_timeline(self,events):
        from dataclasses import asdict
        events=list(events)
        digest=hashlib.sha256()
        for event in events:digest.update(json.dumps(asdict(event),sort_keys=True).encode())
        self._claim('timeline_input',digest.hexdigest())
        with self.repository._connection() as db:
            row=db.execute('SELECT timeline_ready FROM read_metadata WHERE revision=?',(self.revision,)).fetchone()
            if row and row[0]:return
        for event in events:
            check_budget();payload=json.dumps(asdict(event),sort_keys=True)
            with self.repository._write() as db:
                db.execute('INSERT OR IGNORE INTO read_timeline VALUES(?,?,?,?)',
                    (self.revision,stamp(event.timestamp),hashlib.sha256(payload.encode()).hexdigest(),payload))
        with self.repository._write() as db:db.execute('UPDATE read_metadata SET timeline_ready=1 WHERE revision=?',(self.revision,))

    @protected_read
    def topology_at(self,timestamp):
        self._ready('identity_ready');target=normalize_utc_timestamp(timestamp,'timestamp')
        with self.repository._connection() as db:
            row=db.execute('SELECT payload FROM read_topology WHERE revision=? AND stamp<=? ORDER BY stamp DESC LIMIT 1',(self.revision,stamp(target))).fetchone()
        if row:return {**json.loads(row[0]),'timestamp':target}
        return ResearchIdentityResolver([]).topology_at(target)

    @protected_read
    def position_at(self,physical_serial,timestamp):
        topology=self.topology_at(timestamp)
        matches=[x for x in topology['positions'] if x['physical_serial']==physical_serial]
        item=matches[0] if len(matches)==1 else None
        return {'physical_serial':physical_serial,'position_at_time':None if item is None else item['position'],
                'resolved':item is not None,'position_history_id':topology['position_history_id'],
                'identity_epoch_id':None if item is None else item['identity_epoch_id']}

    @protected_read
    def serial_at(self,position,timestamp):
        if not isinstance(position,int) or not 1<=position<=6:raise ValueError('position must be between 1 and 6')
        return {**self.topology_at(timestamp)['positions'][position-1],'position_at_time':position}

    @protected_read
    def epochs(self,physical_serial=None,timestamp_from=None,timestamp_to=None):
        self._ready('identity_ready')
        first=-math.inf if timestamp_from is None else stamp(normalize_utc_timestamp(timestamp_from,'from'))
        last=math.inf if timestamp_to is None else stamp(normalize_utc_timestamp(timestamp_to,'to'))
        with self.repository._connection() as db:
            if physical_serial is None:
                rows=db.execute('SELECT serial FROM read_serial WHERE revision=? ORDER BY serial LIMIT 257',(self.revision,)).fetchall()
                if len(rows)>256:raise ResearchQueryError('history_window_budget','Select a physical serial for this metadata set',503)
                serials=[r[0] for r in rows]
            else:serials=[physical_serial]
            result=[]
            for serial in serials:
                check_budget()
                prior_stamp=db.execute('SELECT MAX(stamp) FROM read_identity_epoch WHERE revision=? AND serial=? AND stamp<?',(self.revision,serial,first)).fetchone()[0]
                prior=db.execute('SELECT payload FROM read_identity_epoch WHERE revision=? AND serial=? AND stamp=? ORDER BY rowid LIMIT 257',(self.revision,serial,prior_stamp)).fetchall()
                rows=db.execute('SELECT payload FROM read_identity_epoch WHERE revision=? AND serial=? AND stamp>=? AND stamp<=? ORDER BY stamp,rowid LIMIT 257',(self.revision,serial,first,last)).fetchall()
                if len(rows)>256 or len(prior)>256:raise ResearchQueryError('history_window_budget','Identity window exceeds metadata page budget',503)
                result.extend(x for row in prior+rows if (x:=json.loads(row[0]))['valid_to'] is None or stamp(x['valid_to'])>=first)
        return result

    @protected_read
    def epoch_at(self,physical_serial,timestamp):
        target=normalize_utc_timestamp(timestamp,'timestamp')
        matches=[x for x in self.epochs(physical_serial,target,target) if x['valid_from']<=target and (x['valid_to'] is None or target<x['valid_to'])]
        return matches[-1] if matches else None

    @protected_read
    def position_window(self,start,end):
        self._ready('identity_ready')
        with self.repository._connection() as db:
            prior=db.execute('SELECT MAX(stamp) FROM read_position WHERE revision=? AND stamp<=?',(self.revision,start)).fetchone()[0]
            rows=db.execute('SELECT payload FROM read_position WHERE revision=? AND stamp>=? AND stamp<? ORDER BY stamp,priority LIMIT 257',(self.revision,start if prior is None else prior,end)).fetchall()
        if len(rows)>256:raise ResearchQueryError('history_window_budget','Position metadata window exceeds page budget',503)
        return [json.loads(row[0]) for row in rows]

    @protected_read
    def query(self,*,timestamp_from,timestamp_to,event_types=(),include_archived=False,**kwargs):
        self._ready('timeline_ready')
        with self.repository._connection() as db:
            rows=db.execute('SELECT payload FROM read_timeline WHERE revision=? AND stamp>=? AND stamp<=? ORDER BY stamp,identity LIMIT 10001',(self.revision,stamp(timestamp_from),stamp(timestamp_to))).fetchall()
        if len(rows)>10000:raise ResearchQueryError('history_window_budget','Timeline window exceeds metadata budget',503)
        result=[]
        for row in rows:
            check_budget();item=TimelineEvent(**json.loads(row[0]))
            if event_types and item.event_type not in event_types:continue
            if not include_archived and item.metadata.get('archived_at'):continue
            if kwargs.get('module_number') is not None and item.module_number!=kwargs['module_number']:continue
            if kwargs.get('cell_number') is not None and item.cell_number!=kwargs['cell_number']:continue
            if kwargs.get('category') is not None and (item.event_type!='maintenance' or item.metadata.get('category')!=kwargs['category']):continue
            result.append(item)
        return sorted(result,key=lambda event:(event.timestamp,event.event_type,event.projection_key),reverse=True)
