"""Indexed, disposable canonical interval view; no HTTP-side rebuild."""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from research_budget import check_budget, budgeted


def _epoch(value):
    return float(value) if isinstance(value,(int,float)) else datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()


def path_for(path): return Path(str(path)+".window.sqlite")


def publish(path, artifact):
    with closing(sqlite3.connect(path_for(path), timeout=0)) as db:
        db.set_progress_handler(lambda: _progress(),1000)
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS intervals (id TEXT PRIMARY KEY, identity TEXT, module INTEGER, kind TEXT, start REAL, end REAL, payload TEXT, seen TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS interval_end ON intervals(module,end,start)")
            db.execute("CREATE TABLE IF NOT EXISTS samples (id TEXT, epoch REAL, timestamp TEXT, PRIMARY KEY(id,epoch))")
            previous=db.execute("SELECT value FROM meta WHERE key='artifact'").fetchone()
            if previous:
                previous=json.loads(previous[0])
                same_lineage=previous.get("config_signature")==artifact.get("config_signature")
                old_sources=previous.get("source_signatures",{})
                new_sources=artifact.get("source_signatures",{})
                same_lineage = same_lineage and old_sources.keys()==new_sources.keys()
                for key, old in old_sources.items():
                    new=new_sources.get(key,{})
                    same_lineage = same_lineage and (old==new or (
                        old.get("append_generation") is not None and
                        old.get("append_generation")==new.get("append_generation") and
                        old.get("inode")==new.get("inode") and old.get("device")==new.get("device") and
                        old.get("size",0)<=new.get("size",-1)))
                if not same_lineage:
                    db.execute("DELETE FROM samples")
                    db.execute("DELETE FROM intervals")
                    db.execute("DELETE FROM meta WHERE key LIKE 'checkpoint:%'")
            stat=Path(path).stat(); revision=f"{stat.st_mtime_ns}:{stat.st_size}"
            identities={}
            for identity, data in artifact["identities"].items():
                check_budget()
                identities[identity]={k:v for k,v in data.items() if k not in {"diagnostic_intervals","visual_intervals","relative_endpoints","eod_checkpoint"}}
                for kind in ("diagnostic_intervals","visual_intervals","relative_endpoints"):
                    for item in data[kind]:
                        check_budget()
                        start=item.get("canonical_from",item.get("timestamp"))
                        end=item.get("canonical_to",item.get("timestamp"))
                        key=hashlib.sha256(json.dumps((identity,kind,start,item.get("phase"),item.get("config_id"),item.get("module_position"))).encode()).hexdigest()
                        payload=json.dumps({k:v for k,v in item.items() if k!="count_timestamps"},sort_keys=True,separators=(",",":"))
                        db.execute("INSERT INTO intervals VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET end=excluded.end,payload=excluded.payload,seen=excluded.seen",(key,identity,item.get("module_position"),kind,_epoch(start),_epoch(end) if end else 1e100,payload,revision))
                        db.executemany("INSERT OR IGNORE INTO samples VALUES (?,?,?)",((key,_epoch(t),t) for t in budgeted(item.get("count_timestamps",()))))
                checkpoint=json.dumps(data.get("eod_checkpoint"),separators=(",",":"))
                db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)",("checkpoint:"+identity,checkpoint))
            db.execute("DELETE FROM samples WHERE id IN (SELECT id FROM intervals WHERE seen<>?)",(revision,))
            db.execute("DELETE FROM intervals WHERE seen<>?",(revision,))
            metadata={k:v for k,v in artifact.items() if k!="identities"}
            metadata["identities"]=identities
            db.execute("INSERT OR REPLACE INTO meta VALUES ('artifact',?)",(json.dumps(metadata),))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('revision',?)",(revision,))


def read(path, start, end, *, module=None, serial=None):
    result=None; decoded_bytes=0
    with closing(sqlite3.connect(path_for(path).as_uri()+"?mode=ro",uri=True,timeout=0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: _progress(),1000)
        stat=Path(path).stat()
        if db.execute("SELECT value FROM meta WHERE key='revision'").fetchone() != (f"{stat.st_mtime_ns}:{stat.st_size}",):
            raise ValueError("canonical publication revision mismatch")
        row=db.execute("SELECT value FROM meta WHERE key='artifact'").fetchone()
        if row is None: raise ValueError("canonical metadata missing")
        result=json.loads(row[0]);decoded_bytes+=len(row[0].encode())
        result["_window_revision"]=f"{stat.st_mtime_ns}:{stat.st_size}"
        identities={key:{**value,"diagnostic_intervals":[],"visual_intervals":[],"relative_endpoints":[]}
                    for key,value in result["identities"].items()
                    if (serial is None or value.get("physical_serial")==serial) and
                       (module is None or module in value.get("module_positions",()))}
        sql="SELECT id,identity,kind,payload FROM intervals WHERE end>=? AND start<=?"
        args=[_epoch(start),_epoch(end)]
        if module is not None: sql+=" AND module=?";args.append(module)
        for key,identity,kind,payload in db.execute(sql,args):
            check_budget()
            if identity not in identities: continue
            item=json.loads(payload);decoded_bytes+=len(payload.encode())
            if kind!="relative_endpoints":
                item["count_timestamps"]=[r[0] for r in db.execute("SELECT epoch FROM samples WHERE id=? AND epoch>=? AND epoch<=? ORDER BY epoch",(key,_epoch(start),_epoch(end)))]
            identities[identity][kind].append(item)
        result["identities"]=identities
    return result,decoded_bytes


def _progress():
    check_budget()
    return 0


def checkpoint(path, identity, revision):
    with closing(sqlite3.connect(path_for(path).as_uri()+"?mode=ro",uri=True,timeout=0)) as db:
        if db.execute("SELECT value FROM meta WHERE key='revision'").fetchone() != (revision,):
            raise ValueError("canonical publication changed")
        key="checkpoint:"+identity
        length=db.execute("SELECT length(value) FROM meta WHERE key=?",(key,)).fetchone()
        if not length or length[0]>65536:
            raise ValueError("bounded checkpoint unavailable")
        return json.loads(db.execute("SELECT value FROM meta WHERE key=?",(key,)).fetchone()[0])


def repository_page(projection, stream, day, *, bundle=None, after_key='', limit=64):
    """Bounded immutable rows with exact count-sequence descriptors.

    Use the canonical repository clipping operation for visible counts; never
    expand a complete phase's timestamp sequence merely to obtain a checkpoint.
    """
    return projection.read_page(stream, day, bundle=bundle,
                                after_key=after_key, limit=limit)


def repository_checkpoint(projection, stream, day, *, bundle=None):
    value = projection.published_state(stream, day, bundle)
    if value is None:
        raise ValueError('canonical predecessor checkpoint unavailable')
    return value
