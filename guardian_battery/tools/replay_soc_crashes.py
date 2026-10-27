#!/usr/bin/env python3
"""Offline, bounded SOC replay. Default: stdout report only; no event writes."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from soc_crash_replay import ReplayInputError, replay_saved_pwr, audit_saved_history


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sources = parser.add_mutually_exclusive_group(required=True)
    sources.add_argument("--history-file", action="append")
    sources.add_argument("--saved-directory", help="bounded local saved inventory, dry-run only")
    parser.add_argument("--from", dest="start")
    parser.add_argument("--to", dest="end")
    parser.add_argument("--modules", help="optional exact-position comparison filter; default observes groups of at least two modules")
    parser.add_argument("--position-history", help="optional saved historical position snapshots")
    parser.add_argument("--x-pp", type=float, default=5.0)
    parser.add_argument("--y-seconds", type=int, default=300)
    parser.add_argument("--write-test-events", help="explicit separate local soc_crash_events.jsonl; never production")
    args = parser.parse_args(argv)
    try:
        modules=[int(v) for v in args.modules.split(",")] if args.modules else None
        if args.saved_directory:
            if args.write_test_events or args.start or args.end:
                raise ReplayInputError("inventory mode is dry-run only and covers the bounded saved inventory")
            result=audit_saved_history(args.saved_directory,expected_modules=modules,
                position_history=args.position_history,x_pp=args.x_pp,y_seconds=args.y_seconds)
        else:
            if not args.start or not args.end:raise ReplayInputError("--from and --to are required")
            result = replay_saved_pwr(args.history_file, start=args.start, end=args.end,
                expected_modules=modules, x_pp=args.x_pp, y_seconds=args.y_seconds,
                event_output=args.write_test_events, position_history=args.position_history)
    except (ReplayInputError, ValueError, OSError) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc), "events_written": None if args.write_test_events else 0}))
        return 2
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
