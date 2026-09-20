#!/usr/bin/env python3
"""
Claude Code status line command that records plan usage to a file.

Claude Code runs this after each assistant turn and pipes a JSON document to stdin. For Pro/Max subscribers
that document carries `rate_limits` (five_hour and seven_day, each with used_percentage and resets_at).
This script writes those to ~/.claude/usage/latest.json for usage.py to read, and prints a short status line.

It never fails loudly: a status line that errors is worse than none, so any problem is written into
latest.json (see "error") and the line just says usage is unavailable.

Status line support is CLI-only; the VS Code extension does not run it. See README.md.
"""

import json
import os
import sys
import time
from pathlib import Path

USAGE_DIR = Path(os.environ.get('CLAUDE_USAGE_DIR') or Path.home() / '.claude' / 'usage')
LATEST = USAGE_DIR / 'latest.json'
LAST_PAYLOAD = USAGE_DIR / 'last-payload.json'


def window(rate_limits, name):
    """{'used_percentage': float, 'resets_at': int|None} for a window, or None if absent."""
    entry = (rate_limits or {}).get(name)
    if not isinstance(entry, dict) or entry.get('used_percentage') is None:
        return None
    try:
        used = float(entry['used_percentage'])
    except (TypeError, ValueError):
        return None
    resets = entry.get('resets_at')
    return {'used_percentage': used, 'resets_at': int(resets) if isinstance(resets, (int, float)) else None}


def build_record(payload, now=None):
    rate_limits = payload.get('rate_limits') if isinstance(payload, dict) else None
    record = {
        'updated_at': int(now if now is not None else time.time()),
        'available': False,
        'five_hour': window(rate_limits, 'five_hour'),
        'seven_day': window(rate_limits, 'seven_day'),
        'session_id': payload.get('session_id') if isinstance(payload, dict) else None,
    }
    record['available'] = bool(record['five_hour'] or record['seven_day'])
    if not record['available']:
        # Help diagnose a version or plan that does not send rate_limits.
        record['payload_keys'] = sorted(payload) if isinstance(payload, dict) else []
    return record


def format_line(record, now=None):
    if not record['available']:
        return 'usage: n/a'
    now = now if now is not None else time.time()
    parts = []
    for label, key in (('5h', 'five_hour'), ('7d', 'seven_day')):
        entry = record[key]
        if not entry:
            continue
        text = f"{label} {entry['used_percentage']:.0f}%"
        if entry['resets_at']:
            seconds = max(0, entry['resets_at'] - now)
            text += f" (resets {int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}m)" if seconds >= 3600 else f" (resets {int(seconds // 60)}m)"
        parts.append(text)
    return ' | '.join(parts)


def write_atomic(path, data):
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def main():
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except ValueError as error:
        payload, record = {}, None
        record = {'updated_at': int(time.time()), 'available': False, 'error': f'bad JSON on stdin: {error}'}
    else:
        record = build_record(payload)
    try:
        write_atomic(LATEST, record)
        if os.environ.get('CLAUDE_USAGE_DEBUG'):
            write_atomic(LAST_PAYLOAD, payload)
    except OSError as error:
        record = {**record, 'error': f'could not write {LATEST}: {error}'}
    print(format_line(record) if 'error' not in record or record.get('available') else 'usage: n/a')


if __name__ == '__main__':
    main()
