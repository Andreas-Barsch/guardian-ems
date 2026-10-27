"""Disk-bounded exact slot reconciliation for diagnostic cold starts."""
import json
import sqlite3
import fcntl
from pathlib import Path
from diagnostic_startup_io import checkpoint, StartupIncomplete

MAX_MATERIALIZED_BYTES = 64 * 1024 * 1024

class StartupMerge:
    def __init__(self, directory, max_samples, *, durable=False):
        self.connection = None
        self.lock = None
        self.owned = False
        self.durable = durable
        self.root = Path(directory) / ('.diagnostic-resume-preparation' if durable else '.diagnostic-startup')
        if self.root.is_symlink():
            raise StartupIncomplete('scratch_path_invalid')
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.dbpath = self.root / 'merge.sqlite'
        marker = self.root / 'OWNER'
        identity = 'guardian-diagnostic-resume-v1' if durable else 'guardian-diagnostic-startup-scratch-v1'
        if marker.is_symlink() or (self.root / 'lock').is_symlink():
            raise StartupIncomplete('scratch_path_invalid')
        for entry in self.root.iterdir():
            if entry.name not in ('OWNER', 'lock', 'merge.sqlite', 'merge.sqlite-journal'):
                raise StartupIncomplete('unrecognized_scratch_content')
        try:
            self.lock = (self.root / 'lock').open('a+')
            fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if marker.exists():
                with marker.open() as handle:
                    if handle.read(128) != identity:
                        raise StartupIncomplete('scratch_owner_invalid')
            else:
                if self.dbpath.exists():
                    raise StartupIncomplete('unowned_scratch_database')
                marker.write_text(identity)
            if self.dbpath.is_symlink():
                raise StartupIncomplete('scratch_path_invalid')
            self.owned = True
            # The sole, explicitly marked disposable slot is recreated after a
            # crash. Never delete raw sources or diagnostic cache files.
            if not durable: self.dbpath.unlink(missing_ok=True)
            self.connection = sqlite3.connect(str(self.dbpath))
            self.connection.execute('PRAGMA page_size=4096')
            self.connection.execute('PRAGMA cache_size=-1024')
            self.connection.execute('PRAGMA max_page_count=131072')
            self.connection.execute('PRAGMA journal_mode=DELETE' if durable else 'PRAGMA journal_mode=OFF')
            if durable: self.connection.execute('PRAGMA synchronous=FULL')
            def progress():
                try:
                    checkpoint()
                    return 0
                except StartupIncomplete:
                    return 1
            self.connection.set_progress_handler(progress, 1000)
            self.connection.execute('CREATE TABLE IF NOT EXISTS samples (serial TEXT, module INTEGER, ts REAL, sequence INTEGER PRIMARY KEY, value TEXT, UNIQUE(serial,module,ts))')
            self.connection.execute('CREATE INDEX IF NOT EXISTS slot ON samples(module,ts)')
            self.connection.execute('CREATE INDEX IF NOT EXISTS retained ON samples(serial,ts DESC,sequence DESC)')
            self.connection.execute('CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY, value TEXT)')
            self.sequence = self.connection.execute('SELECT COALESCE(MAX(sequence),0) FROM samples').fetchone()[0]
            self.max_samples = max_samples
            self.identities = {row[0] for row in self.connection.execute('SELECT DISTINCT serial FROM samples')}
        except BaseException:
            self.close()
            raise

    def __contains__(self, key):
        return self.connection.execute('SELECT 1 FROM samples WHERE serial=? AND module=? AND ts=?',key).fetchone() is not None

    def __setitem__(self, key, value):
        checkpoint()
        self.identities.add(key[0])
        if len(self.identities) > 64:
            raise StartupIncomplete("identity_inventory_budget")
        existing = self.connection.execute('SELECT sequence FROM samples WHERE serial=? AND module=? AND ts=?', key).fetchone()
        self.sequence += 1
        sequence = existing[0] if existing else self.sequence
        encoded = []
        size = 0
        for index, chunk in enumerate(json.JSONEncoder(separators=(',', ':')).iterencode(value)):
            if index % 64 == 0:
                checkpoint()
            size += len(chunk.encode('utf-8'))
            if size > 65536:
                raise StartupIncomplete('sample_materialization_budget')
            encoded.append(chunk)
        try:
            self.connection.execute('INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?)',
                (*key, sequence, ''.join(encoded)))
        except sqlite3.Error as exc:
            raise StartupIncomplete('reconciliation_storage_budget_or_error') from exc

    def pop(self, key, default=None):
        self.connection.execute('DELETE FROM samples WHERE serial=? AND module=? AND ts=?',key)

    def slot_keys(self, slot):
        return [tuple(row) for row in self.connection.execute('SELECT serial,module,ts FROM samples WHERE module=? AND ts=?',slot)]

    def values(self):
        serials = [row[0] for row in self.connection.execute('SELECT DISTINCT serial FROM samples LIMIT 65')]
        if len(serials)>64:
            raise StartupIncomplete('identity_inventory_budget')
        retained = []
        retained_bytes = 0
        for serial in serials:
            rows = self.connection.execute('SELECT ts,sequence,value FROM samples WHERE serial=? ORDER BY ts DESC,sequence DESC LIMIT ?', (serial,self.max_samples))
            for row in rows:
                checkpoint()
                retained_bytes += len(row[2].encode("utf-8"))
                if retained_bytes > MAX_MATERIALIZED_BYTES:
                    raise StartupIncomplete("retained_byte_budget")
                retained.append(row)
                if len(retained)>100000:
                    raise StartupIncomplete('retained_sample_budget')
        for _, _, value in sorted(retained, key=lambda row:(row[0],row[1])):
            checkpoint()
            yield json.loads(value)

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        if self.owned and not self.durable:
            self.dbpath.unlink(missing_ok=True)
            self.owned = False
        if self.lock is not None:
            self.lock.close()
            self.lock = None

class StartupSlots:
    def __init__(self, merge): self.merge=merge
    def setdefault(self, key, default): return set()
    def get(self,key,default): return self.merge.slot_keys(key)
    def __setitem__(self,key,value): pass


def repository_predecessor(projection, stream, day, *, bundle=None):
    """Explicit warm continuation without creating a SortedSamples scratch DB.

    This is a canonical checkpoint, not a legacy diagnostic cache pair. Missing
    provenance fails visibly; no neutral state or source replay is substituted.
    """
    from canonical_window_store import repository_checkpoint
    return repository_checkpoint(projection, stream, day, bundle=bundle)
