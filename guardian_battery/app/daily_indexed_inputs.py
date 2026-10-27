"""Daily scientific inputs over the same confirmed indexed window reader.

The 14 Guardian-day risk context is explicit, including DST. No API request
prepares these catalogs. Metadata carries original records, not timeline DTOs.
"""
from datetime import date, timedelta
from contextlib import contextmanager
from pathlib import Path
import hashlib
import math
import sqlite3
from daily_diagnostics import SourceSlice, _canonical, _timestamp, guardian_day
from research_budget import ResearchQueryError
from indexed_history_window import IndexedHistoryWindow


class DailyIndexedInputs:
    def __init__(self, catalogs, *, registries=None):
        expected = {'cell', 'rs485', 'position_history', 'config_history', 'maintenance_history'}
        if set(catalogs) != expected:
            raise ValueError('every daily source must be explicitly bound or declared absent')
        if registries is not None and set(registries) != expected:
            raise ValueError('every Daily registry must be explicitly bound or absent')
        from confirmed_prefix_reader import ConfirmedPrefixReader
        self.windows = {name: IndexedHistoryWindow(catalog,
            prefix_reader=ConfirmedPrefixReader(registries[name]) if registries is not None and registries[name] is not None else None)
            if catalog is not None else None for name,catalog in catalogs.items()}
        self.metrics = {}

    @staticmethod
    def _prior(window, start):
        # A reverse indexed predecessor lookup, never reading earlier Raw.
        steps = 0
        with window.resolver.catalog.connection() as db:
            def budget():
                nonlocal steps
                steps += 1
                return steps >= 50000
            db.set_progress_handler(budget, 1)
            try:
                row = db.execute('SELECT e.stamp FROM entry e INDEXED BY entry_window '
                    'JOIN active a ON a.source=e.source AND a.revision=e.revision '
                    'WHERE (e.day,e.stamp)<(?,?) ORDER BY e.day DESC,e.stamp DESC LIMIT 1',
                    (math.floor(start / 86400), start)).fetchone()
            except sqlite3.OperationalError as exc:
                if steps >= 50000:
                    raise ResearchQueryError('history_window_budget', 'Daily predecessor SQL budget exhausted', 503) from exc
                raise
            finally:
                db.set_progress_handler(None, 0)
        return None if row is None else row[0]

    @staticmethod
    def _partitions(start,end):
        # Daily owns an explicit finite evidence window (up to fourteen
        # Guardian days), rather than an interactive single-page request.
        # Keep every source scan within fifteen elapsed minutes and its unchanged
        # per-page byte/record limits. Adjacent half-open pieces neither lose
        # nor duplicate boundary records, including DST days.
        while start<end:
            stop=min(end,start+900)
            yield start,stop
            start=stop

    @staticmethod
    @contextmanager
    def _snapshots(window,ranges):
        # Capture the finite metadata plan before Raw work. Later Live appends
        # cannot keep invalidating an already frozen historical calculation.
        # Each capture remains bounded; no long shared transaction/Raw lock.
        from research_budget import check_budget
        from projection_repository import Conflict
        from cell_source_resolver import WindowBudgetExceeded
        resolver=window.resolver;snapshots=[];prefixes={}
        try:
            index=0
            while index<len(ranges):
                check_budget()
                first,last=ranges[index]
                # Seek the next actual observation directly, including long
                # empty parts of the fourteen-day context. An explicit older
                # predecessor retains its own tiny separate range.
                search_end=ranges[-1][1] if ranges[-1][1]-first<=32*86400 else last
                hit=resolver.plan_window(first,search_end,limit=1)['refs']
                if not hit:
                    if search_end==ranges[-1][1]:break
                    index+=1;continue
                stamp=hit[0].timestamp
                while index<len(ranges) and ranges[index][1]<=stamp:index+=1
                if index==len(ranges):break
                first,last=ranges[index]
                if stamp<first:continue
                index+=1
                snapshot=resolver.open_window(first,last)
                snapshots.append((first,last,snapshot))
                with resolver.catalog.write() as db:
                    rows=db.execute('SELECT w.*,s.cursor,s.frontier FROM window_source w JOIN source s '
                        'ON s.source=w.source AND s.revision=w.revision WHERE w.snapshot=?',(snapshot,)).fetchall()
                    for row in rows:
                        key=row['source'];value=(row['revision'],row['receipt'],row['prefix_end'])
                        if key in prefixes:
                            frozen=prefixes[key]
                            if frozen[0]!=value[0]:raise Conflict('Daily selected source revision changed during capture')
                            db.execute('UPDATE window_source SET receipt=?,prefix_end=? WHERE snapshot=? AND source=?',
                                (frozen[1],frozen[2],snapshot,key))
                        else:prefixes[key]=value
                    if len(prefixes)>256:raise WindowBudgetExceeded('Daily selected source count exceeds frozen plan budget')
            # Unknown/unindexed inputs retain the global pending guard: no
            # timestamps are available to prove they lie outside the selection.
            # Fully indexed unrelated Live appends need no global-generation
            # equality; selected immutable prefixes remain pinned instead.
            if not snapshots:
                first,last=ranges[0]
                snapshots.append((first,last,resolver.open_window(first,last)))
            yield snapshots
        finally:
            for first,last,snapshot in snapshots:resolver.release_window(snapshot)

    def _slice(self, name, start, end, *, prior=False):
        window = self.windows[name]
        if window is None:
            return SourceSlice(name, (), (), 0, 0, 0, 0, True)
        ranges = []
        if prior:
            stamp = self._prior(window, start)
            if stamp is not None:
                ranges.append((stamp, math.nextafter(stamp,math.inf)))
        ranges.extend(self._partitions(start,end))
        records, provenance = [], []
        totals = {'raw_bytes': 0, 'decodes': 0, 'files_opened': 0, 'pages': 0, 'sql_vm_steps': 0}
        invalid = 0
        with self._snapshots(window,ranges) as snapshots:
            for first, last, snapshot in snapshots:
                with window.read(first, math.nextafter(last,-math.inf),prepared_snapshot=snapshot) as rows:
                    records.extend(record for record, ref in rows)
                metrics = window.metrics
                if metrics.get('pending_sources'):
                    raise ResearchQueryError('history_preparation_required', 'Daily input has unindexed confirmed sources', 503)
                invalid = max(invalid, metrics.get('invalid_records', 0))
                provenance.append({'source_mode': 'confirmed_indexed_window', 'from': first, 'to': last,
                                   'source_fingerprint': metrics['source_fingerprint'],
                                   'coverage_basis': metrics['coverage_basis'],
                                   'invalid_scope': 'catalog; timestamps of invalid records unknown'})
                for key in totals:
                    totals[key] += metrics[key]
        field = {'position_history': 'effective_at', 'maintenance_history': 'occurred_at'}.get(name, 'timestamp')
        records.sort(key=lambda item: (_timestamp(item[field]), hashlib.sha256(_canonical(item)).hexdigest()))
        self.metrics[name] = totals
        return SourceSlice(name, tuple(records), tuple(provenance), len(records) + invalid,
                           len(records), invalid, 0, False)

    @staticmethod
    def _verify_change_token(resolver,ref):
        # mtime can be restored by ordinary tools. ctime is maintained by the
        # filesystem, so same-size/same-mtime rewrites cannot masquerade as an
        # unchanged prepared source. New confirmed writers also reject such a
        # change before a subsequent append could replace this checkpoint.
        from projection_repository import Conflict,Unavailable
        if resolver.prefix_reader is not None:
            receipt=resolver.prefix_reader.registry.state(ref.source_id)
            if receipt is None or 'change_time_ns' not in receipt:
                raise Unavailable('Daily reuse requires prepared filesystem change tokens')
            expected=receipt['change_time_ns']
        else:
            with resolver.catalog.connection() as db:
                source=resolver.catalog._source(db,ref.source_id,ref.revision)
                if 'ctime' not in source.keys():raise Unavailable('Daily reuse change token not prepared')
                expected=source['ctime']
        if Path(ref.path).stat().st_ctime_ns!=expected:
            raise Conflict('Daily source changed after confirmed preparation')

    def confirmed_guard(self,day):
        """Hash the selected indexed inputs, never decode their Raw payloads.

        This is a reuse guard for an already validated scientific result, not
        a substitute scientific fingerprint. Include the identical 14-day and
        predecessor selection used by read(), and verify each selected source.
        """
        from research_budget import check_budget
        from projection_repository import Conflict,Unavailable
        from cell_source_resolver import WindowBudgetExceeded
        start,end=day.start.timestamp(),day.end.timestamp()
        risk_start=guardian_day(date.fromisoformat(day.diagnostic_date)-timedelta(days=13),day.timezone).start.timestamp()
        selection={'cell':(risk_start,end,False),'rs485':(start,end,False),
            'position_history':(risk_start,end,True),'config_history':(start,end,True),
            'maintenance_history':(start,end,False)}
        proof={}
        for name,(first,last,prior) in selection.items():
            window=self.windows[name]
            if window is None:
                proof[name]={'absent':True};continue
            resolver=window.resolver
            ranges=[]
            if prior:
                stamp=self._prior(window,first)
                if stamp is not None:ranges.append((stamp,math.nextafter(stamp,math.inf)))
            ranges.extend(self._partitions(first,last));digest=hashlib.sha256();count=0
            try:
                with self._snapshots(window,ranges) as snapshots:
                    for lower,upper,snapshot in snapshots:
                        after=None;verified={};selected={}
                        for _ in range(window.max_pages):
                            check_budget();plan=resolver.plan_snapshot(snapshot,after=after,limit=256)
                            if plan['pending_sources']:
                                raise Unavailable('Daily reuse guard requires a stable indexed prefix')
                            digest.update(_canonical({'range':[lower,upper],'invalid':plan['invalid_records']})) if after is None else None
                            for ref in plan['refs']:
                                check_budget()
                                key=(ref.source_id,ref.revision)
                                if key not in selected:
                                    resolver._validate_ref(ref,verified)
                                    self._verify_change_token(resolver,ref)
                                    selected[key]=ref
                                digest.update(_canonical([ref.source_id,ref.revision,ref.path,ref.storage_convention,
                                    ref.start,ref.end,ref.timestamp,ref.module,ref.identity_context,ref.source_context,ref.record_hash]))
                                count+=1
                            if plan['next'] is None:break
                            after=plan['next']
                        else:raise WindowBudgetExceeded('Daily reuse guard window page budget')
                        # Recheck after planning: a rewrite is never accepted as
                        # unchanged merely because the derived rows still exist.
                        for ref in selected.values():
                            resolver._validate_ref(ref,{})
                            self._verify_change_token(resolver,ref)
            except (Conflict,Unavailable,WindowBudgetExceeded,OSError) as exc:
                raise ResearchQueryError('history_preparation_required',str(exc),503) from exc
            proof[name]={'hash':digest.hexdigest(),'selected_records':count,'ranges':[list(value) for value in ranges]}
        return {'contract':'guardian-daily-confirmed-reuse-1','sources':proof}

    def read(self, day):
        start, end = day.start.timestamp(), day.end.timestamp()
        risk_start = guardian_day(date.fromisoformat(day.diagnostic_date) - timedelta(days=13), day.timezone).start.timestamp()
        self.metrics = {'context': {'guardian_days': 14, 'from': risk_start, 'to_exclusive': end}}
        # One shared Cell scan serves both BMS's day and Risk's explicit context.
        risk = self._slice('cell', risk_start, end)
        cell_records = tuple(row for row in risk.records if start <= _timestamp(row['timestamp']) < end)
        cells = SourceSlice('cell', cell_records, risk.provenance, len(cell_records) + risk.records_invalid,
                            len(cell_records), risk.records_invalid, 0, risk.missing)
        slices = {'cell': cells, 'rs485': self._slice('rs485', start, end),
                  'position_history': self._slice('position_history', risk_start, end, prior=True),
                  'config_history': self._slice('config_history', start, end, prior=True),
                  'maintenance_history': self._slice('maintenance_history', start, end)}
        return slices, risk.records
