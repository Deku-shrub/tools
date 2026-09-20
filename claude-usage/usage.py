#!/usr/bin/env python3
"""
Read the plan usage that statusline.py recorded, and decide whether it is safe to keep working.

    python usage.py                     # print the current figures
    python usage.py --json              # machine-readable
    python usage.py --check 90          # exit 0 if every window is under 90%, 10 if any is at or over
    python usage.py --check 90 --window five_hour
    python usage.py --wait-below 90     # sleep until usage is under 90% (waits for the window to reset), then exit 0

Exit codes:  0 ok / under the limit    10 at or over the limit    3 no usable data (never recorded, or stale)

Figures only update while a Claude Code CLI session with the status line configured is running. If the data
is older than --max-age minutes (default 30) it is treated as missing rather than trusted: a stale 40% is
not evidence that you are still at 40%.
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

USAGE_DIR = Path(os.environ.get('CLAUDE_USAGE_DIR') or Path.home() / '.claude' / 'usage')
LATEST = USAGE_DIR / 'latest.json'
WINDOWS = ('five_hour', 'seven_day')
OK, OVER, NO_DATA = 0, 10, 3


def load(path=LATEST):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def age_minutes(record, now=None):
    return ((now if now is not None else time.time()) - record.get('updated_at', 0)) / 60


def usable(record, max_age, now=None):
    return bool(record and record.get('available') and age_minutes(record, now) <= max_age)


def over_limit(record, limit, windows=WINDOWS, now=None):
    """[(window, used, resets_at)] for every selected window at or over `limit`.

    A window whose reset time has passed is treated as reset: the recorded figure describes the old window."""
    now = now if now is not None else time.time()
    hits = []
    for name in windows:
        entry = record.get(name)
        if not entry:
            continue
        if entry.get('resets_at') and entry['resets_at'] <= now:
            continue
        if entry['used_percentage'] >= limit:
            hits.append((name, entry['used_percentage'], entry.get('resets_at')))
    return hits


def describe(record, now=None):
    now = now if now is not None else time.time()
    if not record:
        return 'No usage recorded yet. Is the status line configured, and has a CLI session run? (see README.md)'
    if not record.get('available'):
        detail = record.get('error') or f"payload had no rate_limits (keys: {', '.join(record.get('payload_keys', [])) or 'none'})"
        return f'Usage unavailable: {detail}'
    lines = [f'recorded {age_minutes(record, now):.0f} min ago']
    for name in WINDOWS:
        entry = record.get(name)
        if entry:
            reset = ''
            if entry.get('resets_at'):
                seconds = entry['resets_at'] - now
                reset = ' (window has reset)' if seconds <= 0 else f' (resets in {int(seconds // 3600)}h {int(seconds % 3600 // 60)}m)'
            lines.append(f"{name.replace('_', '-')}: {entry['used_percentage']:.0f}% used{reset}")
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--check', type=float, metavar='PERCENT')
    parser.add_argument('--wait-below', type=float, metavar='PERCENT')
    parser.add_argument('--window', choices=WINDOWS, help='only consider this window (default: both)')
    parser.add_argument('--max-age', type=float, default=30, help='minutes before recorded data counts as stale')
    args = parser.parse_args(argv)
    windows = (args.window,) if args.window else WINDOWS

    record = load()
    if args.json:
        print(json.dumps(record))
    elif not (args.check or args.wait_below):
        print(describe(record))

    limit = args.check or args.wait_below
    if not limit:
        return OK if usable(record, args.max_age) else NO_DATA
    if not usable(record, args.max_age):
        print('NO DATA: nothing recorded recently; treat as unsafe to continue.', file=sys.stderr)
        return NO_DATA

    hits = over_limit(record, limit, windows)
    if not hits:
        return OK
    for name, used, resets in hits:
        print(f'{name} is at {used:.0f}% (limit {limit:.0f}%)', file=sys.stderr)
    if args.check:
        return OVER

    # --wait-below: sleep until the latest reset among the blocking windows, then re-read.
    while True:
        resets = [r for _, _, r in hits if r]
        if not resets:
            print('Over the limit and no reset time known; cannot wait.', file=sys.stderr)
            return NO_DATA
        pause = max(resets) - time.time() + 30
        print(f'waiting {int(pause // 60)} min for the window to reset...', file=sys.stderr)
        time.sleep(max(pause, 30))
        record = load()
        if not usable(record, args.max_age * 1000):  # after a long sleep the data is old by design
            return OK  # the window has reset; nothing recorded since means no evidence of being over
        hits = over_limit(record, limit, windows)
        if not hits:
            return OK


if __name__ == '__main__':
    sys.exit(main())
