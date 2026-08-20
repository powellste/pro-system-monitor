#!/usr/bin/env python3
"""Test script to verify the API returns correct data structure.

Run directly (`python3 test_api.py`) for live-server smoke checks against
:5001. Also pytest-discoverable: the test_* functions at the bottom are unit
tests for the JSONL archive read path and the /api/history/range mapping.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import requests

base_url = "http://localhost:5001"


def _api_key():
    """Resolve MONITOR_API_KEY: env var first, then the systemd unit files.

    The API is authenticated via the X-API-Key header (guard in
    hardware_monitor_pro.py returns 401 when no key is set on the server and
    the request carries none/wrong). Key value is never printed.
    """
    key = os.environ.get("MONITOR_API_KEY", "")
    if key:
        return key
    for unit in ("hermes-sysmon.service", "hardware-monitor.service"):
        p = Path.home() / ".config/systemd/user" / unit
        try:
            for line in p.read_text().splitlines():
                line = line.strip()
                if line.startswith("Environment=") and "MONITOR_API_KEY=" in line:
                    return line.split("MONITOR_API_KEY=", 1)[1].strip().strip('"')
        except OSError:
            continue
    return ""


def _live_smoke():
    """Existing live-server checks (run only when executed directly)."""
    _headers = {"X-API-Key": _api_key()} if _api_key() else {}

    print("=" * 60)
    print("Testing Hardware Monitor API")
    print("=" * 60)

    # Test status endpoint
    print("\n1. Testing /api/status...")
    try:
        r = requests.get(f"{base_url}/api/status", headers=_headers)
        data = r.json()
        print(f"   ✓ Status retrieved")
        print(f"   CPU Temp: {data.get('cpu_temps', {}).get('k10temp', 'N/A')}°C")
        print(f"   RAM: {data.get('ram_percent', 'N/A')}%")
        print(f"   Disk: {data.get('disk_usage', 'N/A')}%")
    except Exception as e:
        print(f"   ✗ Error: {e}")

    # Test history endpoint
    print("\n2. Testing /api/history...")
    try:
        r = requests.get(f"{base_url}/api/history?hours=1&limit=5", headers=_headers)
        history = r.json()
        print(f"   ✓ History retrieved")
        print(f"   Total records: {history.get('total_records', 0)}")

        # Check RAM data
        ram_data = history.get('ram', [])
        if ram_data:
            print(f"   RAM entries: {len(ram_data)}")
            if ram_data:
                ram_entry = ram_data[0]
                if isinstance(ram_entry, list):
                    print(f"   RAM structure: list of {len(ram_entry)} values")
                    if len(ram_entry) >= 3:
                        print(f"   RAM percent (index 2): {ram_entry[2]}%")
                else:
                    print(f"   RAM structure: {type(ram_entry)}")

        # Check disk data
        disk_data = history.get('disk', [])
        if disk_data:
            print(f"   Disk entries: {len(disk_data)}")
            if disk_data:
                disk_entry = disk_data[0]
                if isinstance(disk_entry, list):
                    print(f"   Disk structure: list of {len(disk_entry)} values")
                    if len(disk_entry) >= 4:
                        print(f"   Disk percent (index 3): {disk_entry[3]}%")

        # Check network data
        net_data = history.get('network', [])
        if net_data:
            print(f"   Network entries: {len(net_data)}")
            if net_data:
                net_entry = net_data[0]
                if isinstance(net_entry, list):
                    print(f"   Network structure: list of {len(net_entry)} values")
                    if len(net_entry) >= 4:
                        print(f"   Network rx (index 2): {net_entry[2]}")
                        print(f"   Network tx (index 3): {net_entry[3]}")

    except Exception as e:
        print(f"   ✗ Error: {e}")

    print("\n" + "=" * 60)
    print("Test complete!")
    print("=" * 60)


# =========================================================================
# Unit tests (pytest) — archive read path + /api/history/range mapping
# =========================================================================
import history_archive
import hardware_monitor_pro


def _write_archive(path, records):
    with open(path, 'w') as f:
        for r in records:
            f.write(json.dumps(r) + '\n')


def _flat_record(t):
    """One archive line exactly as the collector writes it (see _archive_record)."""
    return {
        't': t,
        'cpu_temp': 51.0, 'cpu_freq': 3200.0, 'cpu_percent': 22.5,
        'ram_percent': 60.0, 'ram_used_gb': 9.5, 'ram_total_gb': 15.6,
        'disk_percent': 80.0, 'disk_free_gb': 40.0, 'disk_total_gb': 200.0,
        'net_rx_mbps': 1.2, 'net_tx_mbps': 0.8,
        'swap_percent': 12.0,
        'gpu_temp': 55.0, 'gpu_util': 33.0, 'gpu_vram_pct': 44.0, 'gpu_power_w': 120.0,
        'llama_alive': True, 'llama_kv_pct': 27.5,
    }


# --- history_archive.read_range -----------------------------------------

def test_read_range_returns_window(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    now = time.time()
    records = [{'t': now - 3600 + i * 10, 'cpu_temp': 40.0} for i in range(200)]
    _write_archive(arch, records)
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    out = history_archive.read_range(now - 7200, now, max_points=0)
    assert len(out) == 200
    assert out[0]['t'] < out[-1]['t']
    # Ordered output, all inside the window
    assert all(now - 7200 <= r['t'] <= now for r in out)


def test_read_range_empty_when_no_overlap(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    _write_archive(arch, [{'t': 1_000.0, 'cpu_temp': 40.0}])
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    assert history_archive.read_range(2_000.0, 3_000.0) == []


def test_read_range_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(tmp_path / 'nope.jsonl'))
    assert history_archive.read_range(0, time.time()) == []


def test_read_range_skips_corrupt_lines(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    arch.write_text('{"t": 1000, "cpu_temp": 40}\nNOT JSON\n{"t": 1100, "cpu_temp": 41}\n')
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    out = history_archive.read_range(0, 10_000)
    assert len(out) == 2


def test_read_range_decimates_avg_midpoint(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    records = [{'t': 1_000.0 + i * 10, 'cpu_temp': 40.0 + i} for i in range(100)]
    _write_archive(arch, records)
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    out = history_archive.read_range(0, 10_000, max_points=10)
    assert len(out) <= 10
    # First bucket: indices 0..9 -> t 1000..1090, midpoint 1045; avg temp 44.5
    assert out[0]['t'] == 1045.0
    assert abs(out[0]['cpu_temp'] - 44.5) < 1e-9


def test_read_range_no_decimate_under_cap(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    records = [{'t': 1_000.0 + i * 10, 'cpu_temp': 40.0 + i} for i in range(5)]
    _write_archive(arch, records)
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    out = history_archive.read_range(0, 10_000, max_points=100)
    assert len(out) == 5


def test_read_range_keeps_last_non_numeric(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    records = [
        {'t': 1_000.0, 'cpu_temp': 40.0, 'llama_alive': True},
        {'t': 1_010.0, 'cpu_temp': 41.0, 'llama_alive': False},
    ]
    _write_archive(arch, records)
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    out = history_archive.read_range(0, 10_000, max_points=1)
    assert out[0]['llama_alive'] is False  # last present value wins
    assert abs(out[0]['cpu_temp'] - 40.5) < 1e-9  # numeric mean


# --- _archive_series mapping round-trip ---------------------------------

def test_archive_series_scalar_metrics():
    recs = [_flat_record(1_000.0), _flat_record(1_010.0)]
    expected = {'cpu_temp': 51.0, 'cpu_freq': 3200.0, 'cpu_percent': 22.5,
                'gpu_vram': 44.0, 'gpu_power': 120.0}
    for m, v in expected.items():
        pts = hardware_monitor_pro._archive_series(recs, m)
        assert len(pts) == 2
        for p in pts:
            assert set(p.keys()) == {'t', 'v'}
            assert p['t'] in (1_000.0, 1_010.0)
        assert pts[0]['v'] == v


def test_archive_series_multi_field_metrics():
    recs = [_flat_record(1_000.0)]
    ram = hardware_monitor_pro._archive_series(recs, 'ram')[0]
    assert ram == {'t': 1_000.0, 'percent': 60.0, 'used_gb': 9.5, 'total_gb': 15.6}
    disk = hardware_monitor_pro._archive_series(recs, 'disk')[0]
    assert disk == {'t': 1_000.0, 'percent': 80.0, 'free_gb': 40.0, 'total_gb': 200.0}
    net = hardware_monitor_pro._archive_series(recs, 'network')[0]
    assert net == {'t': 1_000.0, 'rx_speed_mbps': 1.2, 'tx_speed_mbps': 0.8}
    swap = hardware_monitor_pro._archive_series(recs, 'swap')[0]
    assert swap == {'t': 1_000.0, 'percent': 12.0}
    llama = hardware_monitor_pro._archive_series(recs, 'llama')[0]
    assert llama == {'t': 1_000.0, 'alive': True, 'kv_usage_pct': 27.5}


def test_archive_series_gpu_nested_shape():
    recs = [_flat_record(1_000.0)]
    pts = hardware_monitor_pro._archive_series(recs, 'gpu')
    g = pts[0]['gpus'][0]
    assert pts[0]['t'] == 1_000.0
    assert g['temperature'] == 55.0
    assert g['utilization_gpu'] == 33.0
    assert g['power_w'] == 120.0
    # VRAM stored as percent; presented as used/total=100 so the chart's
    # used/total*100 math reproduces the same percentage.
    assert g['memory_used_gb'] == 44.0 and g['memory_total_gb'] == 100


def test_archive_series_unsupported_metrics_empty():
    for m in ('gpu_freq', 'disk_io', 'net_errors', 'process_snapshot', 'bogus'):
        assert hardware_monitor_pro._archive_series([_flat_record(1_000.0)], m) == []


def test_archive_series_skips_missing_fields():
    recs = [{'t': 1_000.0, 'cpu_temp': 51.0}]  # partial record
    pts = hardware_monitor_pro._archive_series(recs, 'ram')
    assert pts == [{'t': 1_000.0}]  # no percent/used/total keys present


# --- GET /api/history/range (Flask test client) -------------------------

def _client_with_archive(tmp_path, monkeypatch, n_records=30):
    arch = tmp_path / 'hist.jsonl'
    now = time.time()
    records = [_flat_record(now - 3000 + i * 10) for i in range(n_records)]
    _write_archive(arch, records)
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    return hardware_monitor_pro.app.test_client()


def test_range_route_defaults(tmp_path, monkeypatch):
    c = _client_with_archive(tmp_path, monkeypatch)
    r = c.get('/api/history/range')
    assert r.status_code == 200
    d = r.get_json()
    assert d['total_records'] == 30
    # Same contract as /api/history: one array per metric + window metadata
    assert set(d.keys()) >= {'cpu_temp', 'ram', 'gpu', 'gpu_freq', 'llama',
                             'total_records', 'window_start', 'window_end'}
    assert d['gpu_freq'] == []            # never archived
    assert len(d['cpu_temp']) == 30
    assert d['window_start'] < d['window_end']


def test_range_route_single_metric(tmp_path, monkeypatch):
    c = _client_with_archive(tmp_path, monkeypatch)
    r = c.get('/api/history/range?metric=ram&hours=1')
    assert r.status_code == 200
    d = r.get_json()
    # jsonify sorts keys alphabetically — compare as sets.
    assert set(d.keys()) == {'ram', 'total_records', 'window_start', 'window_end'}
    assert d['ram'][0]['percent'] == 60.0


def test_range_route_unknown_metric_404(tmp_path, monkeypatch):
    c = _client_with_archive(tmp_path, monkeypatch)
    assert c.get('/api/history/range?metric=nope').status_code == 404


def test_range_route_empty_window_200(tmp_path, monkeypatch):
    arch = tmp_path / 'hist.jsonl'
    _write_archive(arch, [_flat_record(time.time() - 90_000)])  # yesterday-ish
    monkeypatch.setattr(history_archive, 'ARCHIVE_PATH', str(arch))
    c = hardware_monitor_pro.app.test_client()
    r = c.get('/api/history/range?hours=1')
    assert r.status_code == 200
    d = r.get_json()
    assert d['total_records'] == 0
    assert d['cpu_temp'] == []


def test_range_route_hours_clamped(tmp_path, monkeypatch):
    c = _client_with_archive(tmp_path, monkeypatch)
    assert c.get('/api/history/range?hours=9999').status_code == 200  # capped
    assert c.get('/api/history/range?hours=abc').status_code == 200    # default
    assert c.get('/api/history/range?hours=-5').status_code == 200     # default


def test_range_route_decimation(tmp_path, monkeypatch):
    c = _client_with_archive(tmp_path, monkeypatch, n_records=200)
    r = c.get('/api/history/range?hours=1&max_points=20')
    assert r.status_code == 200
    d = r.get_json()
    assert d['total_records'] <= 20
    assert len(d['cpu_temp']) <= 20


if __name__ == '__main__':
    _live_smoke()
