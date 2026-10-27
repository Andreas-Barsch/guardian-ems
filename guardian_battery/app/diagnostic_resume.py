"""Durable matched diagnostic caches with source frontiers, not raw migration."""
import hashlib
import json
from pathlib import Path
from config_history import config_id, diagnostic_parameters
from history_append_receipt import signature
import resume_publication as durable

MAX_TAIL_BYTES = 1024 * 1024

def paths(cell_path):
    checkpoint = Path(cell_path).with_suffix(".resume.json")
    return checkpoint, checkpoint.with_suffix(".publication.json")

def identity(path):
    if not Path(path).exists(): return None
    return signature(path)

def save(cell_path, aggregate_path, cell_payload, aggregate_payload, context):
    checkpoint, intent = paths(cell_path)
    durable.recover(intent)
    cell = durable.encoded(cell_payload); aggregate = durable.encoded(aggregate_payload)
    metadata = {**context, "version": 1, "cell_sha256": hashlib.sha256(cell).hexdigest(),
                "aggregate_sha256": hashlib.sha256(aggregate).hexdigest()}
    durable.publish(intent, [durable.replace(cell_path, cell),
        durable.replace(aggregate_path, aggregate),
        durable.replace(checkpoint, durable.encoded(metadata))])

def load(cell_path, aggregate_path, options, position_path):
    checkpoint, intent = paths(cell_path)
    durable.recover(intent)
    if not checkpoint.exists(): return None
    value = json.loads(checkpoint.read_text())
    if (value.get("version") != 1 or
            value.get("config_id") != config_id(diagnostic_parameters(options))):
        raise ValueError("diagnostic checkpoint incompatible calculation configuration")
    if value.get("position") != identity(position_path):
        raise ValueError("diagnostic checkpoint position history changed")
    for path, key in ((cell_path, "cell_sha256"), (aggregate_path, "aggregate_sha256")):
        from diagnostic_startup_io import MAX_CACHE_BYTES, checkpoint as yield_checkpoint
        if Path(path).stat().st_size > MAX_CACHE_BYTES:
            raise ValueError("diagnostic checkpoint cache exceeds byte budget")
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            while chunk := handle.read(65536):
                yield_checkpoint()
                digest.update(chunk)
        if digest.hexdigest() != value[key]:
            raise ValueError("diagnostic checkpoint result changed: " + Path(path).name)
    return value

def context(options, position_path, sources):
    return {"config_id": config_id(diagnostic_parameters(options)),
            "position": identity(position_path), "sources": dict(sources)}


def source_inventory(directory):
    """Bound metadata work; never open raw history merely to find its size."""
    import os
    from diagnostic_startup_io import MAX_FILES, checkpoint, StartupIncomplete
    result = {}
    if Path(directory).exists():
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                checkpoint()
                if count >= MAX_FILES:
                    raise StartupIncomplete('source_inventory_budget')
                if entry.is_file() and entry.name.endswith('.jsonl'):
                    result[entry.name] = signature(Path(directory) / entry.name)
    return dict(sorted(result.items()))


def prepare(startup, cell, aggregate, reason):
    """Resume the existing exact slot merge in durable, live-priority units.

    Shared caches lack subtractable source contributions. Reconcile their shared
    state privately; existing public products remain untouched until handover.
    Raw offsets, merged slots and aggregate state commit in one SQLite transaction.
    """
    from dataclasses import asdict
    from diagnostic_startup_merge import StartupMerge, MAX_MATERIALIZED_BYTES
    from diagnostic_startup_io import checkpoint as cooperate, StartupIncomplete
    from history_preparation import CHUNK_BYTES
    from history_append_receipt import matches_prefix
    from current_condition_backfill import CurrentConditionBackfill
    from diagnostic_aggregates import DiagnosticAggregateStore
    from cell_diagnostics import CellDiagnosticStore
    resolver = CurrentConditionBackfill(startup.history_directory, startup.positions_path)
    try: identities = resolver._identity_index()
    except (OSError, ValueError, KeyError, TypeError): identities = ([], [])
    plan = source_inventory(startup.history_directory)
    binding = {k: v for k, v in startup.resume_context().items() if k != 'sources'}
    binding['max_samples'] = cell.max_samples
    binding['retention_days'] = aggregate.retention_days
    merge = StartupMerge(startup.history_directory.parent, cell.max_samples, durable=True)
    db = merge.connection
    def packed(value):
        parts = []; size = 0
        for n, part in enumerate(json.JSONEncoder(separators=(',', ':')).iterencode(value)):
            if n % 64 == 0: cooperate()
            size += len(part.encode())
            if size > MAX_MATERIALIZED_BYTES:
                raise StartupIncomplete('diagnostic_preparation_state_budget')
            parts.append(part)
        return ''.join(parts)
    def persist(state):
        db.execute('INSERT OR REPLACE INTO progress VALUES (1,?)', (packed(state),))
        db.commit()
        startup._reports['preparation'] = {
            'reason': state['reason'], 'scope': 'shared_diagnostic_caches',
            'completed_sources': state['file'], 'sources': len(state['plan']),
            'source_offset': state['offset'], 'units': state['units'],
            'chunk_bytes_limit': CHUNK_BYTES, 'chunk_records_limit': 256,
            'invalid_lines': state['invalid_lines'], 'identity_unknown': state['identity_unknown']}
        startup._preparation_checkpoint()
    try:
        row = db.execute('SELECT value FROM progress WHERE id=1').fetchone()
        state = json.loads(row[0]) if row else None
        compatible = (state and state.get('binding') == binding
                      and set(state['plan']).issubset(plan)
                      and all(matches_prefix(startup.history_directory/name, sig)
                              for name, sig in state['plan'].items()))
        if not compatible:
            db.execute('DELETE FROM samples'); db.execute('DELETE FROM progress')
            merge.identities.clear(); merge.sequence = 0
            for value in cell.iter_all_samples():
                cooperate(); merge[resolver._sample_key(value)] = dict(value)
            state = {'binding': binding, 'plan': plan, 'file': 0, 'offset': 0,
                     'reason': reason if not state else reason + '; preparation source/context changed',
                     'units': 0, 'invalid_lines': 0, 'identity_unknown': 0,
                     'records': aggregate.records, 'backfill_sources': aggregate.backfill_sources,
                     'temporary': {}}
            persist(state)
        if compatible:
            state['reason'] = reason
            persist(state)
        aggregate.records = state['records']; aggregate.backfill_sources = state['backfill_sources']
        names = list(state['plan'])
        while state['file'] < len(names):
            cooperate()
            if identity(startup.positions_path) != binding['position']:
                raise StartupIncomplete('position_history_changed_during_preparation')
            name = names[state['file']]; path = startup.history_directory/name
            frozen = state['plan'][name]
            if not matches_prefix(path, frozen):
                raise StartupIncomplete('diagnostic_preparation_source_changed:' + name)
            # Preserve a legacy source only when both retained cells and the
            # aggregate coverage prove it complete under this configuration.
            previous = cell.rebuild_sources.get(name, {})
            pos = ({k: binding['position'][k] for k in ('size', 'mtime_ns', 'device', 'inode')}
                   if binding['position'] else {'missing': True})
            file_sig = {k: frozen[k] for k in ('size', 'mtime_ns', 'device', 'inode')}
            aggregate_sig = {'cell_history': {k: frozen[k] for k in ('size', 'mtime_ns')},
                             'position_history': ({k: pos[k] for k in ('size', 'mtime_ns')}
                                                  if binding['position'] else {'missing': True})}
            if (state['offset'] == 0 and previous.get('schema_version') == 2
                    and previous.get('file') == file_sig and previous.get('position_history') == pos
                    and cell.materialized_coverage_satisfied()
                    and aggregate.source_complete(path, aggregate_sig, binding['config_id'])):
                state['file'] += 1; state['units'] += 1
                persist(state)
                continue
            temporary = DiagnosticAggregateStore.in_memory(aggregate.phase_classifier, aggregate.retention_days)
            temporary.records = state['temporary']
            count = used = 0
            with path.open('rb') as stream:
                stream.seek(state['offset'])
                while stream.tell() < frozen['size'] and count < 256 and used < CHUNK_BYTES:
                    cooperate()
                    start = stream.tell()
                    line = stream.readline(min(65537, frozen['size'] - start))
                    if not line.endswith(b'\n') or len(line) > 65536:
                        raise StartupIncomplete('source_partial_line_or_oversized_record')
                    if count and used + len(line) > CHUNK_BYTES:
                        stream.seek(start); break
                    count += 1; used += len(line)
                    if not line.strip(): continue
                    try: sample = resolver._sample(json.loads(line), identities)
                    except (ValueError, KeyError, TypeError, IndexError):
                        state['invalid_lines'] += 1; continue
                    if sample is None:
                        state['identity_unknown'] += 1; continue
                    value = asdict(sample); key = resolver._sample_key(value)
                    for old in merge.slot_keys(key[1:]): merge.pop(old)
                    merge[key] = value
                    temporary.add(sample, startup.options)
                state['offset'] = stream.tell()
            startup._budget.bytes_read += used
            state['temporary'] = temporary.records
            if state['offset'] == frozen['size']:
                aggregate.merge_backfill_records(temporary.records)
                aggregate.mark_source_complete(path,
                    {'cell_history': {k: frozen[k] for k in ('size','mtime_ns')},
                     'position_history': ({k: binding['position'][k] for k in ('size','mtime_ns')}
                                          if binding['position'] else {'missing': True})},
                    binding['config_id'], set(temporary.records), {})
                state['records'] = aggregate.records
                state['backfill_sources'] = aggregate.backfill_sources
                state['temporary'] = {}; state['offset'] = 0; state['file'] += 1
            state['units'] += 1
            persist(state)
            # Release the historical lease after each durable unit.
            startup._budget.close()
        cell.replace_samples(merge.values())
        if identities[1]: cell.set_current_identities(identities[1][-1].positions)
        cell.expected_materialized_coverage = cell.coverage_snapshot()
        startup.resume_sources = dict(state['plan'])
        days = [Path(name).stem for name in names if len(Path(name).stem) == 10
                and Path(name).stem[4:5] == '-' and Path(name).stem[7:8] == '-']
        if days: aggregate.prune_through(max(days))
        aggregate._dirty = True
        return cell, aggregate
    finally:
        merge.close()
