#!/usr/bin/env python3
"""
history_archive.py — Append-only JSONL archive for durable hardware-monitor history.

Purpose
-------
The live monitor keeps a rolling ~24h window (8640 samples @ 10s) in the
in-memory deque `H`, persisted to ~/.hermes/data/hardware-monitor-history.json
every ~100s. Anything older than 24h is lost on every write. This module adds
an append-only JSONL archive (one compact JSON line per collection tick) with
a default 14-day retention, so RAM/disk/GPU history survives restarts and
supports >24h trend analysis.

Design
------
- Append-only: each tick appends one line to `~/.hermes/data/hardware-monitor-history.jsonl`.
  The file is never rewritten on the hot path.
- Retention: `prune()` rewrites the archive keeping only lines with
  `t >= now - HISTORY_ARCHIVE_DAYS * 86400` (default 14 days). It is
  rate-limited to at most once per hour so the hot loop stays O(1).
- Failure isolation: any archive error is logged once and swallowed — the
  collector must never crash because archiving failed.
- Thread safety: the collector thread is the only writer; a lock guards the
  prune-vs-append race.

Config (env)
------------
- HISTORY_ARCHIVE_PATH   default ~/.hermes/data/hardware-monitor-history.jsonl
- HISTORY_ARCHIVE_DAYS   default 14
"""

import json
import os
import threading
import time

ARCHIVE_PATH = os.environ.get(
    'HISTORY_ARCHIVE_PATH',
    os.path.expanduser('~/.hermes/data/hardware-monitor-history.jsonl'),
)
RETENTION_DAYS = int(os.environ.get('HISTORY_ARCHIVE_DAYS', '14'))

_PRUNE_MIN_INTERVAL = 3600.0  # seconds between prune passes

_lock = threading.Lock()
_last_prune_ts = 0.0
_error_logged = False


def _log_error(msg):
    global _error_logged
    if not _error_logged:
        print(f"[MONITOR] archive error: {msg}")
        _error_logged = True


def append_record(record):
    """Append one JSON line to the archive. Never raises."""
    try:
        with _lock:
            os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
            with open(ARCHIVE_PATH, 'a') as f:
                f.write(json.dumps(record) + '\n')
    except Exception as e:
        _log_error(f"append failed: {e}")


def prune():
    """Drop lines older than the retention window (rate-limited to 1/h). Never raises."""
    global _last_prune_ts
    now = time.time()
    with _lock:
        if now - _last_prune_ts < _PRUNE_MIN_INTERVAL:
            return
        _last_prune_ts = now
        try:
            if not os.path.exists(ARCHIVE_PATH):
                return
            cutoff = now - RETENTION_DAYS * 86400
            tmp = ARCHIVE_PATH + '.tmp'
            kept = 0
            with open(ARCHIVE_PATH) as fin, open(tmp, 'w') as fout:
                for line in fin:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        if rec.get('t', 0) >= cutoff:
                            fout.write(line + '\n')
                            kept += 1
                    except Exception:
                        continue
            os.replace(tmp, ARCHIVE_PATH)
            print(f"[MONITOR] archive pruned to {RETENTION_DAYS}d retention ({kept} lines kept)")
        except Exception as e:
            _log_error(f"prune failed: {e}")


def stats():
    """Return {lines, oldest_t, newest_t, path} for health checks. Never raises."""
    result = {'path': ARCHIVE_PATH, 'lines': 0, 'oldest_t': None, 'newest_t': None}
    try:
        with _lock:
            if not os.path.exists(ARCHIVE_PATH):
                return result
            with open(ARCHIVE_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        t = rec.get('t')
                        if t is None:
                            continue
                        result['lines'] += 1
                        if result['oldest_t'] is None or t < result['oldest_t']:
                            result['oldest_t'] = t
                        if result['newest_t'] is None or t > result['newest_t']:
                            result['newest_t'] = t
                    except Exception:
                        continue
    except Exception as e:
        _log_error(f"stats failed: {e}")
    return result
