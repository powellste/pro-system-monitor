#!/usr/bin/env python3
"""
history_archive.py — Append-only JSONL archive for durable hardware-monitor history.

Purpose
-------
The live monitor keeps a rolling ~20 min window (120 samples @ ~10s, HISTORY_MAX)
in the in-memory deque `H`, persisted to ~/.hermes/data/hardware-monitor-history.json
every ~100s. Anything older than that is lost on every write. This module adds
an append-only JSONL archive (one compact JSON line per collection tick) with
a default 14-day retention, so RAM/disk/GPU history survives restarts and
supports >24h trend analysis (read back via read_range()).

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
import math
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


def read_range(start_t, end_t, max_points=1500):
    """Return archived records with start_t <= t <= end_t, bucket-decimated.

    Streaming scan of the append-only JSONL archive (records are in write
    order, so `t` is monotonic). When more than `max_points` records fall in
    the window they are bucket-averaged down to <= max_points (numeric fields
    averaged, `t` set to the bucket midpoint) so a 24h window stays
    phone-friendly. On ANY error returns [] and logs once — the dashboard
    degrades to live-deque data, never 500s.
    """
    try:
        with _lock:
            if not os.path.exists(ARCHIVE_PATH):
                return []
            records = []
            with open(ARCHIVE_PATH) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    t = rec.get('t')
                    if not isinstance(t, (int, float)):
                        continue
                    if t < start_t:
                        continue
                    if t > end_t:
                        # Append-only order: nothing later can be in window.
                        break
                    records.append(rec)
        if max_points and len(records) > max_points:
            return _decimate(records, max_points)
        return records
    except Exception as e:
        _log_error(f"read_range failed: {e}")
        return []


def _decimate(records, max_points):
    """Bucket-average `records` down to <= max_points points.

    Numeric fields (excluding the timestamp) are averaged per bucket; the
    bucket timestamp is the midpoint of its first/last sample. Non-numeric
    fields (e.g. bool flags) keep the last present value in the bucket.
    """
    if max_points <= 0:
        return records
    bucket_size = math.ceil(len(records) / max_points)
    out = []
    for i in range(0, len(records), bucket_size):
        bucket = records[i:i + bucket_size]
        ts = [r['t'] for r in bucket if isinstance(r.get('t'), (int, float))]
        if not ts:
            continue
        mid = {'t': (min(ts) + max(ts)) / 2.0}
        keys = set()
        for r in bucket:
            keys.update(k for k in r if k != 't')
        for k in sorted(keys):
            nums = [r[k] for r in bucket
                    if isinstance(r.get(k), (int, float)) and not isinstance(r.get(k), bool)]
            if nums:
                mid[k] = sum(nums) / len(nums)
            else:
                for r in reversed(bucket):  # last present value wins
                    if k in r:
                        mid[k] = r[k]
                        break
        out.append(mid)
    return out
