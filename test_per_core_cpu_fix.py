"""Regression tests for the per-core CPU% fix (monitor-per-core-cpu-garbage).

Bug: _collect_cpu() made two back-to-back NON-BLOCKING psutil.cpu_percent()
calls -- one for 'percent', then one for 'per_core'. A non-blocking call
measures utilization since the calling thread's LAST same-mode call, so the
second call sampled only the microseconds between the two calls -> garbage
per-core values (phantom 100.0s / all-zeros / random spikes).

Fix (approved spec, kanban t_0256affe):
  A1: ONE psutil.cpu_percent(interval=None, percpu=True) call in
      _collect_cpu(); 'percent' is derived from the same window (mean of the
      cores), so the two can never disagree.
  A2: _background_collector() seeds its thread's psutil baseline at start so
      tick 1 measures the full history_interval window, not microseconds.
  B:  get_status()'s fresh-collect path (live=1 / boot window) serves the
      collector's cached per_core; before the first tick it takes a short
      BLOCKING measurement (interval=0.1) -- a real window, never garbage.
      The aggregate 'percent' stays the fresh reading.
  C:  templates/index.html clamps per_core to [0,100] and greys out invalid
      payloads ('?' cells) instead of red 'pegged' cells. (Frontend -- no
      unit test here; verified against the live dashboard.)

Importing hardware_monitor_pro has no side effects (the server and the
background collector only start under __main__), so this test can import the
live checkout directly.
"""
import math
import time

import hardware_monitor_pro as m


def _plausible_per_core(per_core, n_expected):
    """Every core must be a finite number in [0, 100] with the right count."""
    assert isinstance(per_core, list), per_core
    assert len(per_core) == n_expected, (len(per_core), n_expected)
    for p in per_core:
        assert isinstance(p, (int, float)), p
        assert math.isfinite(p), p
        assert 0.0 <= p <= 100.0, p


def test_collect_cpu_single_window_per_core_plausible():
    """A1: one non-blocking call; per_core plausible; percent == mean(per_core).

    This is the incident repro: the OLD code measured per_core over a
    microseconds window, so per_core values were garbage (0.0/100.0 spikes)
    and could not match the aggregate percent measured over a real window.
    """
    n = m._collect_cpu()['count_logical']
    # Seed this thread's psutil baseline, then measure over a real window.
    m._collect_cpu()
    time.sleep(1.1)
    cpu = m._collect_cpu()
    _plausible_per_core(cpu['per_core'], n)
    # 'percent' is derived from the very same per_core array (single window),
    # so the two must agree within rounding of each core to 0.1.
    mean_core = sum(cpu['per_core']) / len(cpu['per_core'])
    assert abs(cpu['percent'] - mean_core) <= 0.6, cpu


def test_live_status_per_core_is_cache_authoritative():
    """B: live=1 serves the collector's cached per_core, not fresh garbage.

    Simulates the incident: a fresh request thread whose _collect_cpu()
    returns absurd per_core (999.0 per core). B must replace it with the
    collector's cache; 'percent' must stay the fresh reading.
    """
    n = m._collect_cpu()['count_logical']
    cached_per_core = [round(i * 10 + 0.5, 1) for i in range(n)]
    with m._cached_status_lock:
        saved = m._cached_status
        m._cached_status = {'cpu': {'per_core': cached_per_core},
                            'alerts': [], 'health_score': 100}
    orig_collect = m._collect_cpu
    m._collect_cpu = lambda: {  # fresh collect returns a known-garbage per_core
        'temps': {}, 'freq': {'current': 0, 'max': 0},
        'percent': 42.0,
        'per_core': [999.0] * n,   # must be replaced by B
        'load_avg': [0, 0, 0],
        'count_logical': n, 'count_physical': n,
    }
    try:
        client = m.app.test_client()
        resp = client.get('/api/status?live=1')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['cpu']['per_core'] == cached_per_core, data['cpu']['per_core']
        # aggregate percent stays the fresh reading (B is per_core-only)
        assert data['cpu']['percent'] == 42.0, data['cpu']['percent']
    finally:
        m._collect_cpu = orig_collect
        with m._cached_status_lock:
            m._cached_status = saved


def test_live_status_boot_window_uses_blocking_measurement():
    """B boot window: no cache yet -> short BLOCKING per_core, plausible."""
    n = m._collect_cpu()['count_logical']
    with m._cached_status_lock:
        saved = m._cached_status
        m._cached_status = None
    try:
        client = m.app.test_client()
        resp = client.get('/api/status?live=1')
        assert resp.status_code == 200
        data = resp.get_json()
        _plausible_per_core(data['cpu']['per_core'], n)
    finally:
        with m._cached_status_lock:
            m._cached_status = saved
