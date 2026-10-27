"""Explicit, resumable preparation of selected derived history artifacts.

No work is performed by HTTP submission/status. The worker shares the collector's
historical slot. Open source days stay owned by live writers. Canonical artifacts
require a validated predecessor or explicit retained-source bootstrap; no new phase semantics.
"""
from __future__ import annotations
import copy
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import math
import os
import sqlite3
from pathlib import Path
import threading
import time
import uuid

from history_append_receipt import signature, current_receipt
from history_block_index import (_advance_identity, _epoch, _edge_hash, _load_valid,
                                 BlockIndexError, policy_reference)
from history_live_priority import historical_slot, live_coordination_ready
from research_budget import ResearchQueryError, check_budget, cancellation_scope

MAX_JSON = 4 * 1024 * 1024
CHUNK_BYTES = 256 * 1024
COMPONENTS = {'cell_index', 'rs485_index', 'hycube_index', 'display', 'canonical', 'canonical_index', 'hycube_source_index', 'policy_index'}


class PreparationBlocked(ValueError):
    pass


def _load(path, limit=MAX_JSON):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise PreparationBlocked('checkpoint_size_bound')
    return json.loads(raw)


def _save(path, value):
    path = Path(path)
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > MAX_JSON:
        raise PreparationBlocked('checkpoint_size_bound')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    with temporary.open('wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    from resume_publication import sync_dir
    sync_dir(path.parent)


def validate_request(value):
    if not isinstance(value, dict) or set(value) - {'canonical_bootstrap_from', 'selected_sources_only'} != {
            'timestamp_from', 'timestamp_to', 'modules', 'components', 'shared_day_artifacts'}:
        raise ValueError('explicit window, modules, components and shared_day_artifacts required')
    if value['shared_day_artifacts'] is not True:
        raise ValueError('shared day artifacts cover all recorded channels; acknowledge explicitly')
    if not all(isinstance(value[k], str) for k in ('timestamp_from', 'timestamp_to')):
        raise ValueError('timestamp strings required')
    start = datetime.fromisoformat(value['timestamp_from'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(value['timestamp_to'].replace('Z', '+00:00'))
    if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end-start <= timedelta(hours=48):
        raise ValueError('explicit timezone and positive preparation window <=48h required')
    modules = value['modules']; components = value['components']
    if (not isinstance(modules, list) or not 1 <= len(modules) <= 32 or
            any(type(m) is not int or not 1 <= m <= 255 for m in modules)):
        raise ValueError('explicit module selection required')
    if (not isinstance(components, list) or not components or
            any(not isinstance(c, str) or c not in COMPONENTS for c in components)):
        raise ValueError('unknown or empty preparation components')
    if 'selected_sources_only' in value and value['selected_sources_only'] is not True:
        raise ValueError('selected_sources_only must be true when supplied')
    bootstrap = value.get('canonical_bootstrap_from')
    if 'canonical_bootstrap_from' in value:
        if not isinstance(bootstrap, str) or 'canonical' not in components:
            raise ValueError('canonical bootstrap requires explicit ISO origin and canonical component')
        origin = date.fromisoformat(bootstrap)
        first = start.astimezone(timezone.utc).date()
        if origin.isoformat() != bootstrap or not 1 <= (first-origin).days <= 90:
            raise ValueError('canonical bootstrap origin must precede selection by 1..90 days')
    result = dict(timestamp_from=start.astimezone(timezone.utc).isoformat(),
                timestamp_to=end.astimezone(timezone.utc).isoformat(),
                modules=sorted(set(modules)), components=sorted(set(components)),
                shared_day_artifacts=True)
    if value.get("selected_sources_only"): result["selected_sources_only"] = True
    if bootstrap is not None: result['canonical_bootstrap_from'] = bootstrap
    return result


def tasks_for(request):
    first = datetime.fromisoformat(request['timestamp_from']).date()
    last = datetime.fromisoformat(request['timestamp_to']).date()
    days = [first+timedelta(days=i) for i in range((last-first).days+1)]
    result = []
    for component in request['components']:
        # Cell names are local dates. The reader's explicit adjacent-day envelope
        # is preserved; no older-directory discovery is performed.
        selected = (sorted({d+timedelta(days=i) for d in days for i in (-1, 0, 1)})
                    if component == 'cell_index' else days)
        if component == 'canonical' and request.get('canonical_bootstrap_from'):
            origin = date.fromisoformat(request['canonical_bootstrap_from'])
            selected = [origin+timedelta(days=i) for i in range((last-origin).days+1)]
        if component == 'cell_index' and request.get('selected_sources_only'):
            local_first=datetime.fromisoformat(request['timestamp_from']).astimezone().date()
            local_last=datetime.fromisoformat(request['timestamp_to']).astimezone().date()
            lower,upper=min(first,local_first),max(last,local_last)
            selected=[lower+timedelta(days=i) for i in range((upper-lower).days+1)]
        for day in selected:
            result.append({'component': component, 'day': day.isoformat(), 'state': 'pending'})
    return result


def _unchanged(path, expected):
    if signature(path) != expected:
        raise PreparationBlocked('source_revision_changed')


def _closed(path, now):
    # Conservative for UTC/local naming: only dates earlier in both calendars.
    today = min(datetime.fromtimestamp(now).date(), datetime.fromtimestamp(now, timezone.utc).date())
    if date.fromisoformat(path.name[:10]) >= today:
        raise PreparationBlocked('open_source_owned_by_live_writer')


def _source_hash(path):
    # A bounded, streaming verification unit, outside HTTP. On preemption this
    # atomic verification is retried; no partially verified digest is trusted.
    if path.stat().st_size > 16 * 1024 * 1024:
        raise PreparationBlocked('canonical_source_validation_size_bound')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            check_budget()
            raw = stream.read(CHUNK_BYTES)
            if not raw: break
            digest.update(raw)
    return digest.hexdigest()


def _copy_atomic(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name+'.preparing')
    with source.open('rb') as src, temporary.open('wb') as dst:
        while True:
            check_budget()
            raw = src.read(CHUNK_BYTES)
            if not raw: break
            dst.write(raw)
        dst.flush(); os.fsync(dst.fileno())
    check_budget()
    os.replace(temporary, target)


class SelectedHistoryPreparation:
    def __init__(self, root, *, clock=time.time, interval=1.0):
        self.root = Path(root)
        self.directory = self.root/'.history-preparation'
        self.path = self.directory/'request.json'
        self.clock, self.interval = clock, interval
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.cancel_event = threading.Event()
        self.thread = None
        try:
            self.state = _load(self.path, 65536)
            validate_request(self.state['selection'])
            if (not isinstance(self.state.get('id'), str) or
                    str(uuid.UUID(self.state['id'])) != self.state['id']):
                raise ValueError('invalid job id')
            expected = tasks_for(self.state['selection'])
            if [(t['component'], t['day']) for t in self.state['tasks']] != [(t['component'], t['day']) for t in expected]:
                raise ValueError('invalid task selection')
            if self.state.get('cancel_requested'): self.cancel_event.set()
        except FileNotFoundError:
            self.state = {'state': 'idle'}
        except (ValueError, KeyError, TypeError, OSError):
            self.state = {'state': 'blocked', 'reason': 'invalid_persisted_request'}

    def status(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def submit(self, value):
        selection = validate_request(value)
        with self.lock:
            if self.state['state'] in {'pending', 'running', 'paused'}:
                raise ValueError('preparation already active')
            state = {'id': str(uuid.uuid4()), 'selection': selection,
                     'tasks': tasks_for(selection), 'state': 'pending', 'reason': None}
            _save(self.path, state)  # small request only; no history inspection
            self.state = state
            self.cancel_event.clear()
        return self.status()

    def cancel(self):
        self.cancel_event.set()
        with self.lock:
            state = copy.deepcopy(self.state)
            if state['state'] in {'pending', 'running', 'paused'}:
                state['cancel_requested'] = True
                _save(self.path, state)
                self.state = state
        return self.status()

    def _commit_state(self, state):
        # Only the small request/checkpoint summary is locked, never raw reads,
        # decoding, projection construction or SQLite work.
        with self.lock:
            if self.state.get('cancel_requested'):
                state['cancel_requested'] = True
            if self.state == state:
                return
            _save(self.path, state)
            self.state = state

    def step(self):
        state = self.status()
        if state['state'] not in {'pending', 'running', 'paused'}: return state
        if self.cancel_event.is_set():
            state.update(state='cancelled', reason='cancelled')
            self._commit_state(state); return state
        if not live_coordination_ready():
            state.update(state='paused', reason='live_coordination_unavailable')
            self._commit_state(state); return state
        task = next((t for t in state['tasks'] if t['state'] not in {'complete', 'blocked', 'absent', 'writer_owned'}), None)
        if task is None:
            state.update(state='blocked' if any(t['state']=='blocked' for t in state['tasks']) else 'complete', reason=None)
            self._commit_state(state); return state
        try:
            with cancellation_scope(lambda: self.stop_event.is_set() or self.cancel_event.is_set()):
                with historical_slot():
                    stage = self.directory/state['id']/(task['component']+'-'+task['day'])
                    try:
                        if task['component'] == 'canonical_index':
                            done = self._canonical_index_step(task, stage)
                        elif task['component'].endswith('_index'):
                            done = self._index_step(task, stage)
                        elif task['component'] == 'display':
                            done = self._display_step(task, stage)
                        else:
                            done = self._canonical_step(task, stage)
                    except sqlite3.Error:
                        check_budget()
                        raise PreparationBlocked('sqlite_preparation_failed')
                    if done and task['state'] not in {'absent', 'writer_owned'}: task['state'] = 'complete'
                    state.update(state='running', reason=None)
        except ResearchQueryError as exc:
            state.update(state='paused', reason=exc.code)
        except PreparationBlocked as exc:
            task.update(state='blocked', reason=str(exc))
            state.update(state='running', reason=None)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            task.update(state='blocked', reason='invalid_artifact:'+type(exc).__name__)
            state.update(state='running', reason=None)
        self._commit_state(state)
        return self.status()

    def _index_step(self, task, stage):
        kind = task['component']
        directory = {'cell_index':'cell_history', 'rs485_index':'rs485_history',
                     'hycube_index':'hycube_history_projection',
                     'hycube_source_index':'hycube_history',
                     'policy_index':'hycube_policy_history'}[kind]
        source = self.root/directory/(task['day']+'.jsonl')
        if kind == 'hycube_source_index' and source.is_file():
            from hycube_projection import projection_plan
            plan=projection_plan(source,self.root/'hycube_history_projection')
            # Closed complete projections are the reader's source. Otherwise
            # index the authoritative Raw fallback, never an unusable projection.
            if plan['mode'] == 'projection':
                source=plan['projection_path']
        task['source_directory']=source.parent.name
        if not source.is_file(): task['state']='absent'; return True
        try:
            _closed(source, self.clock())
        except PreparationBlocked:
            selection = self.status()['selection']
            last = datetime.fromisoformat(selection['timestamp_to']).date()
            if self.status()['selection'].get('selected_sources_only') or (kind == 'cell_index' and date.fromisoformat(task['day']) > last):
                # Adjacent-day envelope only: never take over the live writer.
                # This task does not claim an available or complete index.
                task.update(state='writer_owned', reason='open_source_owned_by_live_writer')
                return True
            raise
        field = ('observed_at' if kind == 'policy_index' else
                 'timestamp' if kind == 'cell_index' or kind == 'rs485_index' else 'received_at')
        iso = kind != 'cell_index'
        checkpoint = stage/'index.json'
        try: state = _load(checkpoint)
        except FileNotFoundError:
            try:
                existing = _load_valid(source,timestamp_field=field,iso_timestamp=iso)
            except BlockIndexError:
                existing = None
            if existing and existing['indexed_size'] == source.stat().st_size:
                task.update(mode='existing_index_reused',bytes_indexed=existing['indexed_size'],
                            source_bytes=source.stat().st_size)
                return True
            state = {'source':signature(source), 'offset':0, 'blocks':[], 'identities':{},
                     'policy_refs':[]}
            if existing:
                state.update(offset=existing['indexed_size'],blocks=existing['blocks'],
                             identities=existing.get('identities_after',{}),
                             policy_refs=existing.get('policy_refs',[]))
                task['mode']='existing_prefix_extended'
            else:
                task['mode']='missing_or_invalid_index_prepared'
        _unchanged(source, state['source'])
        cursor = 0
        for block in state['blocks']:
            if (block['start_offset'] != cursor or not cursor < block['end_offset'] <= state['source']['size'] or
                    not 1 <= block['record_count'] <= 256 or
                    not all(math.isfinite(block[k]) for k in ('min_timestamp','max_timestamp')) or
                    block['min_timestamp'] > block['max_timestamp']):
                raise PreparationBlocked('invalid_index_checkpoint')
            cursor = block['end_offset']
        if state['offset'] != cursor or not isinstance(state['identities'],dict):
            raise PreparationBlocked('invalid_index_checkpoint')
        start = state['offset']; identities = dict(state['identities']); before = dict(identities)
        epochs = []; policy_refs=list(state.get('policy_refs',[]))
        with source.open('rb') as handle:
            handle.seek(start)
            for _ in range(256):
                check_budget()
                if handle.tell()-start >= CHUNK_BYTES: break
                record_start=handle.tell()
                raw = handle.readline(65537)
                if len(raw)>65536: raise PreparationBlocked('source_record_size_bound')
                if not raw: break
                if not raw.strip():
                    # Existing index contract cannot represent a blank-only block.
                    raise PreparationBlocked('blank_source_record')
                record = json.loads(raw)
                if kind == 'policy_index':
                    ref=policy_reference(record,record_start,handle.tell())
                    if ref: policy_refs.append(ref)
                epochs.append(_epoch(record, field, kind!='cell_index'))
                _advance_identity(record, identities)
            end = handle.tell()
        _unchanged(source, state['source']); check_budget()
        if epochs:
            state['blocks'].append(dict(start_offset=start,end_offset=end,
                min_timestamp=min(epochs),max_timestamp=max(epochs),record_count=len(epochs),
                identities_before=before))
        state.update(offset=end,identities=identities,policy_refs=policy_refs)
        _save(checkpoint,state)
        task.update(bytes_indexed=end, source_bytes=state['source']['size'])
        if end < state['source']['size']: return False
        head, tail = _edge_hash(source,end)
        stat = state['source']
        value = dict(schema_version=1,source_filename=source.name,source_device=stat['device'],
            source_inode=stat['inode'],source_size=stat['size'],source_mtime_ns=stat['mtime_ns'],
            indexed_size=end,complete=True,block_records=256,timestamp_field=field,
            timestamp_encoding='unix_seconds' if kind=='cell_index' else 'iso8601',
            prefix_head_sha256=head,prefix_tail_sha256=tail,blocks=state['blocks'],
            identities_after=identities,append_generation=(current_receipt(source) or {}).get('generation'))
        if kind == 'policy_index':
            value.update(policy_index_version=1,policy_refs=policy_refs)
        check_budget(); _unchanged(source,stat)
        previous=Path(str(source)+'.idx')
        backup=stage/'previous.idx'
        if previous.is_file() and not backup.exists():
            _copy_atomic(previous,backup)
        _save(previous,value)
        return True

    def _display_step(self, task, stage):
        from display_history_projection import DisplayHistoryProjection, RESOLUTIONS
        day = task['day']
        self._recover_display(stage, day)
        projection = DisplayHistoryProjection(self.root/'cell_history', self.root/'hycube_history_projection',
            stage/'display', max_records=256, max_bytes=CHUNK_BYTES, clock=self.clock,
            incremental_historical=True)
        sources = projection._sources(day)
        if not sources: task['state']='absent'; return True
        for _, _, path in sources: _closed(path, self.clock())
        manifest = stage/'sources.json'
        actual = {str(path.relative_to(self.root)):signature(path) for _,_,path in sources}
        try: expected = _load(manifest)
        except FileNotFoundError: expected=actual; _save(manifest,expected)
        if actual != expected: raise PreparationBlocked('source_revision_changed')
        # The isolated staging tree has no old projection state. Never mix the
        # new algorithm with an existing gzip, nor overwrite live-day output.
        state_path = projection._state_path(day)
        if state_path.exists() and state_path.stat().st_size > MAX_JSON:
            raise PreparationBlocked('display_checkpoint_size_bound')
        complete = all(projection._meta_path(r,day).is_file() and
                       _load(projection._meta_path(r,day),65536).get('status')=='complete' for r in RESOLUTIONS)
        if not complete:
            self._journal_display(stage,day)
            result = projection.process_chunk(day,finalize=True)
            (stage/'display-journal.json').unlink()
            task['records_processed'] = result['records_processed']
            if not result['complete']: return False
        for relative, saved in expected.items(): _unchanged(self.root/relative,saved)
        for resolution in RESOLUTIONS:
            for suffix in ('.jsonl.gz', '.jsonl.gz.window.sqlite', '.meta.json'):
                relative = Path(resolution)/(day+suffix)
                _copy_atomic(stage/'display'/relative,self.root/'display_history'/relative)
        return True

    @staticmethod
    def _display_journal_paths(day):
        from display_history_projection import RESOLUTIONS
        return [Path('.state')/(day+'.json'), *[Path(r)/(day+'.meta.json') for r in RESOLUTIONS]]

    def _journal_display(self,stage,day):
        from display_history_projection import RESOLUTIONS
        snapshots={}
        for relative in self._display_journal_paths(day):
            path=stage/'display'/relative
            snapshots[str(relative)]=_load(path) if path.is_file() else None
        sizes={r:(stage/'display'/r/(day+'.jsonl.gz')).stat().st_size
               if (stage/'display'/r/(day+'.jsonl.gz')).is_file() else None for r in RESOLUTIONS}
        _save(stage/'display-journal.json',{'snapshots':snapshots,'sizes':sizes})

    def _recover_display(self,stage,day):
        from display_history_projection import RESOLUTIONS
        journal=stage/'display-journal.json'
        if not journal.is_file(): return
        value=_load(journal)
        names={str(p) for p in self._display_journal_paths(day)}
        if set(value['snapshots'])!=names or set(value['sizes'])!=set(RESOLUTIONS):
            raise PreparationBlocked('invalid_display_checkpoint')
        # Only isolated derived staging files are rolled back. SQLite rows are
        # idempotent by bucket identity and will be republished on replay; no
        # reader sees this staging tree. Raw/live/previous public files untouched.
        for name,snapshot in value['snapshots'].items():
            path=stage/'display'/name
            if snapshot is None: path.unlink(missing_ok=True)
            else: _save(path,snapshot)
        for resolution,size in value['sizes'].items():
            path=stage/'display'/resolution/(day+'.jsonl.gz')
            if size is None: path.unlink(missing_ok=True)
            else:
                if not path.is_file() or not 0<=size<=path.stat().st_size:
                    raise PreparationBlocked('invalid_display_checkpoint')
                with path.open('r+b') as stream: stream.truncate(size)
        journal.unlink()

    def _bootstrap_context(self, stage):
        """Explicit retained-source origin, never a neutral selected-window start.

        The existing full builder starts at the first retained source. Prove that
        origin with a capped nonrecursive metadata inventory, and pin the entire
        explicitly selected prefix plus config/identity context across restarts.
        No source content outside that prefix is read here.
        """
        selection = self.state['selection']
        origin = selection.get('canonical_bootstrap_from')
        if origin is None: return None
        first = datetime.fromisoformat(selection['timestamp_from']).date()
        start = date.fromisoformat(origin)
        expected = {(start+timedelta(days=i)).isoformat()
                    for i in range((first-start).days)}
        found = set()
        with os.scandir(self.root/'cell_history') as entries:
            for count, entry in enumerate(entries, 1):
                check_budget()
                if count > 512: raise PreparationBlocked('bootstrap_inventory_entry_bound')
                if not entry.name.endswith('.jsonl'): continue
                try: day = date.fromisoformat(entry.name[:-6])
                except ValueError: raise PreparationBlocked('bootstrap_unrecognized_source_name')
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise PreparationBlocked('bootstrap_nonregular_source')
                if day < first: found.add(day.isoformat())
        if found != expected:
            raise PreparationBlocked('bootstrap_retained_origin_or_contiguous_prefix_unproved')
        revisions = {}
        for day in sorted(expected):
            check_budget()
            path = self.root/'cell_history'/(day+'.jsonl')
            _closed(path,self.clock())
            if path.stat().st_size > 16*1024*1024:
                raise PreparationBlocked('canonical_source_validation_size_bound')
            revisions[str(path.relative_to(self.root))] = signature(path)
        for name in ('config_history.jsonl','position_history.jsonl'):
            path = self.root/name
            if not path.is_file():
                if name == 'config_history.jsonl':
                    raise PreparationBlocked('canonical_config_context_unavailable')
                revisions[name] = None
                continue
            if path.stat().st_size > 65536:
                raise PreparationBlocked('bootstrap_context_size_bound')
            revisions[name] = {**signature(path), 'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        context = {'origin':origin,'first_selected_day':first.isoformat(),'revisions':revisions}
        path = stage.parent/'bootstrap-context.json'
        if path.is_file():
            if _load(path) != context: raise PreparationBlocked('bootstrap_context_changed')
        else:
            _save(path,context)
        return origin

    def _canonical_index_step(self, task, stage):
        """Republish existing facts only; never finalize or recompute a phase."""
        from canonical_phase import SCHEMA_VERSION, SEMANTICS_VERSION, ALGORITHM_VERSION
        from canonical_window_store import publish, path_for
        from canonical_config_compatibility import matches_historical_config
        from history_append_receipt import matches_prefix
        day = task['day']
        target = self.root/'canonical_phase_v2'/(day+'.json.gz')
        source = self.root/'cell_history'/(day+'.jsonl')
        if not target.is_file():
            raise PreparationBlocked('canonical_existing_artifact_required')
        _closed(source, self.clock())
        revisions = {p:signature(p) for p in
                     (target, source, self.root/'config_history.jsonl')}
        with gzip.open(target, 'rb') as stream:
            raw = stream.read(MAX_JSON+1)
        if len(raw)>MAX_JSON:
            raise PreparationBlocked('canonical_artifact_size_bound')
        artifact = json.loads(raw)
        if (artifact.get('day') != day or
                artifact.get('schema_version') != SCHEMA_VERSION or
                artifact.get('semantics_version') != SEMANTICS_VERSION or
                artifact.get('algorithm_version') != ALGORITHM_VERSION):
            raise PreparationBlocked('canonical_contract_mismatch')
        expected = artifact.get('source_signatures',{}).get(day, {})
        if (source.stat().st_size != expected.get('size') or
                not matches_prefix(source, expected)):
            raise PreparationBlocked('canonical_source_revision_changed')
        if expected.get('sha256') and _source_hash(source) != expected['sha256']:
            raise PreparationBlocked('canonical_source_hash_mismatch_or_missing')
        # Artifacts use local source-day naming. Require new configs to be later
        # than the entire day, not merely later than the requested short window.
        day_end = (datetime.fromisoformat(day)+timedelta(days=1)).astimezone().isoformat()
        if not matches_historical_config(self.root/'config_history.jsonl',
                                         artifact.get('config_signature'), day_end):
            raise PreparationBlocked('canonical_config_revision_changed')
        stage.mkdir(parents=True, exist_ok=True)
        staged = stage/target.name
        _copy_atomic(target, staged)
        os.utime(staged, ns=(target.stat().st_atime_ns,revisions[target]['mtime_ns']))
        publish(staged, artifact)
        for path, saved in revisions.items():
            _unchanged(path, saved)
        # Preserve both original product and previous disposable index.
        backup = stage/'previous.window.sqlite'
        if path_for(target).is_file() and not backup.exists():
            _copy_atomic(path_for(target), backup)
        _copy_atomic(path_for(staged), path_for(target))
        task.update(mode='existing_facts_reindexed',
                    artifact_complete=artifact.get('complete') is True)
        return True

    def _canonical_step(self, task, stage):
        from canonical_phase import SCHEMA_VERSION, SEMANTICS_VERSION, ALGORITHM_VERSION, CanonicalPhaseMachine
        from canonical_window_store import publish, path_for
        day = task['day']; previous = (date.fromisoformat(day)-timedelta(days=1)).isoformat()
        origin = self._bootstrap_context(stage)
        at_origin = day == origin
        if origin is not None and any(t['component']=='canonical' and t['day']<day and
                                      t['state']!='complete' for t in self.state['tasks']):
            raise PreparationBlocked('bootstrap_predecessor_task_incomplete')
        if (stage/'rebuild.json').is_file():
            return self._canonical_rebuild(task, stage)
        source = self.root/'cell_history'/(day+'.jsonl')
        if not source.is_file(): task['state']='absent'; return True
        _closed(source,self.clock())
        paths = [self.root/'canonical_phase_v2'/(d+'.json.gz') for d in (previous,day)]
        revisions = {}; artifacts = []
        building = origin is not None or not paths[-1].is_file()
        if not building:
            with gzip.open(paths[-1], 'rb') as stream: raw = stream.read(MAX_JSON+1)
            if len(raw)>MAX_JSON: raise PreparationBlocked('canonical_artifact_size_bound')
            building = json.loads(raw).get('complete') is not True
        config = self.root/'config_history.jsonl'
        if not config.is_file() or config.stat().st_size>65536:
            raise PreparationBlocked('canonical_config_context_unavailable')
        config_state = signature(config)
        config_hash = hashlib.sha256(config.read_bytes()).hexdigest()
        for path in ([] if at_origin else paths[:1] if building else paths):
            check_budget()
            if not path.is_file(): raise PreparationBlocked('canonical_predecessor_or_artifact_missing')
            revisions[path] = signature(path)
            with gzip.open(path,'rb') as stream: raw=stream.read(MAX_JSON+1)
            if len(raw)>MAX_JSON: raise PreparationBlocked('canonical_artifact_size_bound')
            artifact=json.loads(raw)
            if (artifact.get('schema_version')!=SCHEMA_VERSION or artifact.get('semantics_version')!=SEMANTICS_VERSION or
                    artifact.get('algorithm_version')!=ALGORITHM_VERSION or artifact.get('complete') is not True or artifact.get('day') != path.name[:10]):
                raise PreparationBlocked('canonical_contract_or_complete_checkpoint_missing')
            cfg=artifact.get('config_signature') or {}
            if cfg.get('sha256')!=config_hash or cfg.get('size')!=config_state['size'] or cfg.get('mtime_ns')!=config_state['mtime_ns']:
                raise PreparationBlocked('canonical_config_revision_changed')
            d=path.name[:10]; raw_source=self.root/'cell_history'/(d+'.jsonl')
            if not raw_source.is_file(): raise PreparationBlocked('canonical_source_missing')
            _closed(raw_source,self.clock())
            src=signature(raw_source); old=artifact.get('source_signatures',{}).get(d,{})
            if (any(src[k]!=old.get(k) for k in ('size','mtime_ns')) or
                    any(k in old and src[k]!=old[k] for k in ('device','inode'))):
                raise PreparationBlocked('canonical_source_revision_changed')
            if not old.get('sha256') or _source_hash(raw_source)!=old['sha256']:
                raise PreparationBlocked('canonical_source_hash_mismatch_or_missing')
            revisions[raw_source]=src
            if not artifact.get('identities'): raise PreparationBlocked('canonical_identity_checkpoint_missing')
            for data in artifact['identities'].values():
                check_budget()
                checkpoint=data.get('eod_checkpoint')
                if not checkpoint or len(json.dumps(checkpoint).encode())>65536:
                    raise PreparationBlocked('canonical_identity_checkpoint_missing')
                CanonicalPhaseMachine.restore(checkpoint)
            artifacts.append(artifact)
        _unchanged(config,config_state)
        for path,saved in revisions.items(): _unchanged(path,saved)
        if building:
            source_state = signature(source)
            source_digest = _source_hash(source)
            _unchanged(source, source_state)
            revisions[source] = source_state
            revisions[config] = config_state
            position = self.root/'position_history.jsonl'
            if position.is_file():
                if position.stat().st_size > 65536:
                    raise PreparationBlocked('canonical_position_context_size_bound')
                revisions[position] = signature(position)
            predecessor = artifacts[0] if artifacts else {'identities':{}}
            state = {'revisions':{str(p.relative_to(self.root)):v for p,v in revisions.items()},
                'config_hash':config_hash,'source_hash':source_digest,
                'position_present':position.is_file(), 'cursor':None,
                'machines':{key:value['eod_checkpoint'] for key,value in predecessor['identities'].items()},
                'positions':{key:value.get('module_positions',[]) for key,value in predecessor['identities'].items()},
                'emitted':{},'phase':'sort', 'predecessor':None if at_origin else previous,
                'retained_origin':origin if at_origin else None}
            _save(stage/'rebuild.json',state)
            return False
        # Re-index validated existing canonical facts; never restart a phase at
        # the selected boundary or overwrite the canonical gzip/checkpoint.
        artifact=artifacts[-1]; target=paths[-1]
        staged=stage/target.name
        _copy_atomic(target,staged)
        # Reader revision belongs to the authoritative existing gzip, so preserve
        # its mtime while building the disposable SQLite sidecar in isolation.
        os.utime(staged,ns=(target.stat().st_atime_ns,revisions[target]['mtime_ns']))
        publish(staged,artifact)
        _unchanged(config,config_state)
        for path,saved in revisions.items(): _unchanged(path,saved)
        _copy_atomic(path_for(staged),path_for(target))
        task['predecessor']=previous
        return True

    def _canonical_rebuild(self, task, stage):
        from contextlib import closing
        from canonical_phase import (CanonicalPhaseMachine, CanonicalPhaseProjection,
            CanonicalPhaseStore, IdentityEpochResolver, SCHEMA_VERSION, SEMANTICS_VERSION,
            ALGORITHM_VERSION, _iso, _epoch as phase_epoch)
        from config_history import ConfigHistory
        from canonical_window_store import path_for
        day=task['day']; previous=(date.fromisoformat(day)-timedelta(days=1)).isoformat()
        state=_load(stage/'rebuild.json')
        at_origin = day == self.state['selection'].get('canonical_bootstrap_from')
        expected_names={f'cell_history/{day}.jsonl','config_history.jsonl'}
        if not at_origin:
            expected_names.update({f'cell_history/{previous}.jsonl',f'canonical_phase_v2/{previous}.json.gz'})
        if state.get('position_present'): expected_names.add('position_history.jsonl')
        if (set(state['revisions']) != expected_names or
                state['predecessor'] != (None if at_origin else previous) or
                state.get('retained_origin') != (day if at_origin else None)):
            raise PreparationBlocked('invalid_canonical_checkpoint')
        for name,saved in state['revisions'].items(): _unchanged(self.root/name,saved)
        position=self.root/'position_history.jsonl'
        if position.is_file()!=state['position_present']:
            raise PreparationBlocked('canonical_position_context_changed')
        source=self.root/'cell_history'/(day+'.jsonl'); _closed(source,self.clock())
        source_state=state['revisions'][str(source.relative_to(self.root))]
        spool=stage/'ordered.sqlite'
        # Sorting is disk-backed and resumable. No full-day Python record list,
        # no scan of earlier days, and no assumption that raw input is sorted.
        if state['phase'] != 'sort':
            if not spool.is_file(): raise PreparationBlocked('canonical_sort_checkpoint_missing')
            _unchanged(spool,state['spool_revision'])
        connection = (sqlite3.connect(spool,timeout=0) if state['phase']=='sort' else
                      sqlite3.connect(spool.as_uri()+'?mode=ro',uri=True,timeout=0))
        with closing(connection) as db:
            db.set_progress_handler(lambda: (check_budget() or 0),1000)
            db.execute('PRAGMA temp_store=FILE')
            db.execute('PRAGMA cache_size=-1024')
            if state['phase']=='sort':
                with db:
                    db.execute('CREATE TABLE IF NOT EXISTS rows (offset INTEGER PRIMARY KEY, epoch REAL, payload TEXT)')
                    db.execute('CREATE INDEX IF NOT EXISTS ordering ON rows(epoch,offset)')
                    db.execute('CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY, offset INTEGER)')
            if spool.stat().st_size > 64*1024*1024:
                raise PreparationBlocked('canonical_sort_storage_bound')
            if state['phase']=='sort':
                saved=db.execute('SELECT offset FROM progress WHERE id=1').fetchone()
                start=saved[0] if saved else 0
                if not 0<=start<=source_state['size']:
                    raise PreparationBlocked('invalid_canonical_sort_checkpoint')
                with db, source.open('rb') as handle:
                    handle.seek(start)
                    for _ in range(256):
                        check_budget()
                        offset=handle.tell()
                        if offset-start>=CHUNK_BYTES: break
                        raw=handle.readline(65537)
                        if len(raw)>65536: raise PreparationBlocked('source_record_size_bound')
                        if not raw: break
                        if not raw.strip(): continue
                        record=json.loads(raw); timestamp=_epoch(record,'timestamp',False)
                        if datetime.fromtimestamp(timestamp).date().isoformat()!=day:
                            raise PreparationBlocked('canonical_source_day_mismatch')
                        db.execute('INSERT OR REPLACE INTO rows VALUES (?,?,?)',(offset,timestamp,raw.decode()))
                    offset=handle.tell()
                    check_budget(); _unchanged(source,source_state)
                    db.execute('INSERT OR REPLACE INTO progress VALUES (1,?)',(offset,))
                task['bytes_sorted']=offset
                if offset==source_state['size']:
                    state['phase']='process'; state['spool_revision']=signature(spool)
                    _save(stage/'rebuild.json',state)
                return False
            machines={key:CanonicalPhaseMachine.restore(value) for key,value in state['machines'].items()}
            configs=ConfigHistory(self.root/'config_history.jsonl').records()
            times=[record['timestamp'] for record in configs]
            resolver=IdentityEpochResolver(position if state['position_present'] else None)
            cursor=state['cursor']
            sql='SELECT offset,epoch,payload FROM rows'
            args=()
            if cursor is not None:
                sql+=' WHERE epoch>? OR (epoch=? AND offset>?)';args=(cursor[0],cursor[0],cursor[1])
            sql+=' ORDER BY epoch,offset LIMIT 256'
            consumed=0; processed=0
            previous_max=max((phase_epoch(m.last_timestamp) for m in machines.values() if m.last_timestamp),default=None)
            signature_value={'filename':source.name,'size':source_state['size'],
                             'mtime_ns':source_state['mtime_ns'],'sha256':state['source_hash']}
            for offset,timestamp,payload in db.execute(sql,args):
                check_budget()
                if consumed>=CHUNK_BYTES: break
                if cursor is None and previous_max is not None and timestamp<previous_max:
                    raise PreparationBlocked('canonical_cross_day_ordering_violation')
                record=json.loads(payload); consumed+=len(payload.encode()); processed+=1
                identity,serial=resolver.resolve(record,timestamp)
                machine=machines.setdefault(identity,CanonicalPhaseMachine(identity,serial))
                positions=state['positions'].setdefault(identity,[])
                if int(record['module']) not in positions: positions.append(int(record['module']))
                options,config_id=CanonicalPhaseProjection._resolve(configs,times,_iso(timestamp))
                machine.process(record,options,config_id=config_id,
                    source_cursor={'source_day':day,'source_offset':offset},source_signature=signature_value)
                state['cursor']=[timestamp,offset]
            if processed:
                for identity,machine in machines.items():
                    emitted=state['emitted'].setdefault(identity,{'diagnostic_intervals':[],
                        'visual_intervals':[],'relative_endpoints':[]})
                    for key,values in CanonicalPhaseProjection._drain(machine).items(): emitted[key].extend(values)
                state['machines']={key:m.snapshot() for key,m in machines.items()}
                for name,saved in state['revisions'].items(): _unchanged(self.root/name,saved)
                _save(stage/'rebuild.json',state)
                task['last_chunk_records']=processed
                return False
        # Finalization is a bounded, replayable atomic publication unit. The
        # previous completed checkpoint is retained until publication succeeds.
        if _source_hash(source)!=state['source_hash']:
            raise PreparationBlocked('canonical_source_hash_changed')
        identities={}
        for identity,machine in machines.items():
            check_budget()
            pending=machine.materialized(); emitted=state['emitted'].get(identity,{})
            values={key:copy.deepcopy(emitted.get(key,[])+pending[key]) for key in
                    ('diagnostic_intervals','visual_intervals','relative_endpoints')}
            machine.final_diagnostic=[];machine.final_visual=[];machine.endpoints=[]
            CanonicalPhaseProjection._clear_daily_counts(machine)
            identities[identity]={'physical_serial':machine.physical_serial,'identity_epoch_id':identity,
                'module_positions':sorted(state['positions'].get(identity,[])),**values,
                'eod_checkpoint':copy.deepcopy(machine.snapshot())}
        cfg=state['revisions']['config_history.jsonl']
        artifact={'schema_version':SCHEMA_VERSION,'semantics_version':SEMANTICS_VERSION,
            'algorithm_version':ALGORITHM_VERSION,'day':day,'source_signatures':{day:signature_value},
            'config_signature':{'filename':'config_history.jsonl','size':cfg['size'],
                'mtime_ns':cfg['mtime_ns'],'sha256':state['config_hash']},
            'complete':True,'identities':identities}
        if len(json.dumps(artifact).encode())>MAX_JSON:
            raise PreparationBlocked('canonical_artifact_size_bound')
        store=CanonicalPhaseStore(stage/'result');store.write_day(day,artifact)
        for name,saved in state['revisions'].items(): _unchanged(self.root/name,saved)
        target=self.root/'canonical_phase_v2'/(day+'.json.gz'); staged=store.path(day)
        _copy_atomic(staged,target)
        os.utime(target,ns=(staged.stat().st_atime_ns,staged.stat().st_mtime_ns))
        _copy_atomic(path_for(staged),path_for(target))
        task['predecessor']=None if at_origin else previous
        task['mode']='rebuilt_from_retained_origin' if at_origin else 'rebuilt_from_validated_predecessor'
        return True

    def start(self):
        if self.thread and self.thread.is_alive(): return False
        self.stop_event.clear()
        self.thread=threading.Thread(target=self._run,name='guardian-selected-history-preparation',daemon=True)
        self.thread.start(); return True

    def _run(self):
        while not self.stop_event.is_set():
            try: self.step()
            except Exception:
                # A checkpoint IO failure must not kill acquisition or trigger
                # a hidden retry loop. Keep evidence and require explicit review.
                with self.lock: self.state.update(state='blocked',reason='checkpoint_write_failed')
            self.stop_event.wait(self.interval)

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout)
        return not self.thread or not self.thread.is_alive()


def create_local_repository_preparation(*, repository_path, catalog_path,
                                        epoch_path, initialize=False):
    """Explicit local composition; no production factory or HTTP activation.

    Existing SelectedHistoryPreparation remains unchanged. The repository path
    has its own durable queue and never invokes the legacy full-cache builders.
    """
    from history_repository_preparation import local_composition
    return local_composition(repository_path=repository_path,
                             catalog_path=catalog_path, epoch_path=epoch_path,
                             initialize=initialize)
