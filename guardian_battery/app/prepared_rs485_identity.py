"""Bounded restart identity from explicitly prepared direct 0x93 evidence."""
import json
from types import SimpleNamespace
from projection_repository import Unavailable,Conflict
from confirmed_prefix_reader import ConfirmedPrefixReader
from research_budget import check_budget


def restore_identities(catalog,consumer):
    # The factory cannot silently omit a newer, unconsumed direct identity.
    if consumer.cursor()[0]<=consumer.registry._head()['seq'] or catalog.status()['pending']:
        raise Unavailable('RS485 restart identity requires completed confirmed catalog preparation')
    guard=ConfirmedPrefixReader(consumer.registry)
    result={}
    with catalog.connection() as db:
        db.execute('BEGIN')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='rs485_restore'").fetchone() is None:
            raise Unavailable('RS485 restart index requires explicit preparation')
        for adr in range(256):
            check_budget()
            row=db.execute('SELECT * FROM rs485_restore WHERE adr=? ORDER BY path DESC,offset DESC LIMIT 1',(adr,)).fetchone()
            if row is None:continue
            source=catalog._source(db,row['source'],row['revision'])
            active=db.execute('SELECT revision FROM active WHERE source=?',(row['source'],)).fetchone()
            if active is None or active[0]!=row['revision']:
                raise Conflict('RS485 restart checkpoint revision changed')
            guard.verify(db,source,SimpleNamespace(source_id=source['source'],revision=source['revision'],
                path=source['path'],receipt=source['receipt'],prefix_end=source['frontier']))
            receipt=guard.registry.state(source['source'])
            from pathlib import Path
            if receipt is None or 'change_time_ns' not in receipt:
                raise Unavailable('RS485 restart identity change token requires preparation')
            if Path(source['path']).stat().st_ctime_ns!=receipt['change_time_ns']:
                raise Conflict('RS485 restart source changed after preparation')
            value=json.loads(row['identity'])
            if value['adr']!=adr:raise Conflict('RS485 restart checkpoint address differs')
            result[adr]={**value,'identity_known':True,'identity_currently_confirmed':False}
    return result
