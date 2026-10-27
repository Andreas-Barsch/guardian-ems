"""Disposable indexed display buckets. Writers only; HTTP readers never build.

SQLite is Python's standard-library store. Each channel bucket is individually
addressable; reading a window never inflates the complete daily gzip artifact.
"""
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
from research_budget import check_budget


def path_for(gzip_path):
    return Path(str(gzip_path)+".window.sqlite")


def publish(gzip_path, records, revision, *, reset=False):
    target=path_for(gzip_path)
    with closing(sqlite3.connect(target, timeout=0)) as db:
        db.set_progress_handler(lambda: _progress(),1000)
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS buckets (id TEXT PRIMARY KEY, source TEXT, module INTEGER, metric TEXT, cell INTEGER, start REAL, end REAL, payload TEXT)")
            db.execute("CREATE INDEX IF NOT EXISTS requested_window ON buckets(source,module,metric,cell,start)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
            if reset: db.execute("DELETE FROM buckets")
            for item in records:
                check_budget()
                payload=json.dumps(item,sort_keys=True,separators=(",",":"))
                identity=tuple(item.get(k) for k in ("source","module_position","physical_serial","identity_quality","position_history_id","identity_epoch_id","metric","cell_number","bucket_start"))
                key=hashlib.sha256(json.dumps(identity).encode()).hexdigest()
                db.execute("INSERT OR REPLACE INTO buckets VALUES (?,?,?,?,?,?,?,?)",
                    (key,item["source"],item.get("module_position") or 0,item["metric"],item.get("cell_number") or 0,
                     datetime.fromisoformat(item["bucket_start"]).timestamp(),
                     datetime.fromisoformat(item["bucket_end"]).timestamp(),payload))
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('revision',?)",(revision,))


def read(gzip_path, *, revision, start, end, bucket_seconds, requests, modules, source):
    path=path_for(gzip_path)
    result=[]; payload_bytes=0; boundaries=[]
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=0)) as db:
        db.execute("PRAGMA query_only=ON")
        db.set_progress_handler(lambda: _progress(), 1000)
        actual=db.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()
        if actual != (revision,): raise ValueError("display publication revision mismatch")
        for module in modules:
            for request in requests:
                metric=request["metric"]
                cells=request.get("cell_numbers") or ((request["cell_number"],) if request.get("cell_number") else
                    tuple(range(1,16)) if metric in {"cell_voltage","cell_temperature"} else (0,))
                for cell in cells:
                    check_budget()
                    args=(source,module,metric,cell)
                    boundary=db.execute("SELECT end FROM buckets WHERE source=? AND module=? AND metric=? AND cell=? ORDER BY start DESC LIMIT 1",args).fetchone()
                    if boundary: boundaries.append(boundary[0])
                    for (payload,) in db.execute("SELECT payload FROM buckets WHERE source=? AND module=? AND metric=? AND cell=? AND start>=? AND start<=? AND end>=? ORDER BY start,id", (*args,start-bucket_seconds,end,start)):
                        check_budget()
                        payload_bytes+=len(payload.encode())
                        result.append(json.loads(payload))
    return result,payload_bytes,min(boundaries) if boundaries else None


def _progress():
    check_budget()
    return 0


def repository_page(projection, day, *, bundle=None, after_key='', limit=64):
    """Explicit paginated repository contract, not the legacy gzip-window API.

    Values retain the existing display bucket/coverage fields. The immutable
    bundle and validity metadata must accompany continuation pages.
    """
    return projection.read_page(day, bundle=bundle, after_key=after_key, limit=limit)
