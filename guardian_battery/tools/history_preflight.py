"""Read-only selected-window inventory, not an index validator or a migration.

No raw content is opened, no directory is enumerated, and nothing is written.
Use --shell on the development host to print the exact BusyBox metadata query
for HA Core-SSH, which has no Python. Generated commands never prepare data.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shlex
import stat


def selected_paths(start, end):
    start = datetime.fromisoformat(start.replace('Z', '+00:00'))
    end = datetime.fromisoformat(end.replace('Z', '+00:00'))
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError('explicit timezone required')
    start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if not timedelta(0) < end-start <= timedelta(hours=48):
        raise ValueError('preflight requires a positive window of at most 48 hours')
    # Inclusive endpoints match the reader. Extra cell days are explicit context
    # candidates, never silently treated as requested observations.
    days = []
    day = start.date()
    while day <= end.date():
        days.append(day)
        day += timedelta(days=1)
    cell_days = sorted({day+timedelta(days=delta) for day in days for delta in (-1,0,1)})
    paths = {}
    def add(name, purpose):
        paths[name] = purpose
    for directory, dates in (('cell_history', cell_days), ('rs485_history', days),
                             ('hycube_history', days), ('hycube_history_projection', days)):
        for day in dates:
            name = f'{directory}/{day.isoformat()}.jsonl'
            add(name, 'source_metadata_only')
            add(name+'.idx', 'block_index_presence_not_validity')
            add(name+'.append.json', 'append_receipt_presence_not_validity')
    for day in days:
        for resolution in ('1m','5m','15m','60m'):
            name = f'display_history/{resolution}/{day.isoformat()}'
            add(name+'.jsonl.gz', 'display_legacy_algorithm_must_be_verified')
            add(name+'.meta.json', 'display_metadata')
            add(name+'.jsonl.gz.window.sqlite', 'display_window_presence_not_validity')
        add(f'display_history/.state/{day.isoformat()}.json', 'display_resume_state')
    for day in [days[0]-timedelta(days=1), *days]:
        name = f'canonical_phase_v2/{day.isoformat()}.json.gz'
        add(name, 'canonical_artifact_and_predecessor_candidate')
        add(name+'.window.sqlite', 'canonical_window_presence_not_validity')
    for name in ('position_history.jsonl','maintenance_events.jsonl','config_history.jsonl'):
        add(name, 'context_metadata_only')
    return paths


def inventory(root, paths):
    root = Path(root).resolve()
    rows = []
    for name, purpose in paths.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('relative paths without parent traversal required')
        path = root/relative
        row = {'path': name, 'purpose': purpose, 'validity': 'not_checked'}
        try:
            if not path.resolve().is_relative_to(root):
                row['status'] = 'outside_root_not_inspected'
            else:
                value = path.lstat()
                row.update(status='present' if stat.S_ISREG(value.st_mode) else 'not_regular',
                           bytes=value.st_size, mtime_ns=value.st_mtime_ns)
        except FileNotFoundError:
            row['status'] = 'missing'
        except OSError as exc:
            row.update(status='unreadable', error=type(exc).__name__)
        rows.append(row)
    return {'mode':'metadata_only', 'ready_for_preparation':False,
            'limits': ['No content/schema/signature/coverage validation',
                       'Policy inventory and predecessor continuity require separate bounded checks',
                       'No cross-process live-priority gate; this tool never builds indexes'],
            'files': rows}


def shell_query(root, paths):
    # Explicit paths only: no glob, discovery, read, write, or executable input.
    values = ' '.join(shlex.quote(str(Path(root)/name)) for name in paths)
    return "date -u; for p in "+values+"; do stat -c '%n | bytes=%s | mtime=%y' \"$p\"; done"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--from', dest='start', required=True)
    parser.add_argument('--to', dest='end', required=True)
    parser.add_argument('--shell', action='store_true')
    args = parser.parse_args()
    try:
        paths = selected_paths(args.start, args.end)
    except ValueError as exc:
        parser.error(str(exc))
    print(shell_query(args.root, paths) if args.shell else
          json.dumps(inventory(args.root, paths), indent=2))


if __name__ == '__main__':
    main()
