#!/usr/bin/env python3
"""
PRO Hardware Monitor v2 — Professional system monitoring server.
Collects CPU, RAM, Disk, Network, GPU metrics + top processes.
Serves REST API + modern dashboard UI on port 5001.
"""
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
import psutil
import time
import threading
import json
import os
import signal
import subprocess
import urllib.request
from collections import deque

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})
app_start_time = time.time()

# ---------------------------------------------------------------------------
# Basic API key auth
# ---------------------------------------------------------------------------
API_KEY = os.environ.get('MONITOR_API_KEY', '')
if not API_KEY:
    print("[MONITOR] WARNING: No MONITOR_API_KEY set. Dashboard has NO authentication.")

@app.before_request
def _check_auth():
    """Require API key for /api/ and /export routes, skip static/index."""
    if not API_KEY:
        return
    if request.endpoint == 'index':
        return
    if request.path.startswith('/api/'):
        key = request.args.get('key', '') or request.headers.get('X-API-Key', '')
        if key != API_KEY:
            return jsonify({'error': 'Unauthorized. Provide ?key= or X-API-Key header.'}), 401

# ---------------------------------------------------------------------------
# GPU via NVML
# ---------------------------------------------------------------------------
device_count = 0
try:
    from pynvml import nvmlInit, nvmlDeviceGetCount, nvmlDeviceGetHandleByIndex, \
        nvmlDeviceGetTemperature, nvmlDeviceGetMemoryInfo, nvmlDeviceGetUtilizationRates, \
        nvmlDeviceGetName, nvmlDeviceGetFanSpeed, nvmlDeviceGetClockInfo, \
        nvmlDeviceGetMaxClockInfo, nvmlDeviceGetTemperatureThreshold, nvmlDeviceGetPowerUsage, \
        nvmlDeviceGetMinMaxFanSpeed, \
        NVML_TEMPERATURE_THRESHOLD_SLOWDOWN, NVML_TEMPERATURE_THRESHOLD_SHUTDOWN
    import ctypes
    _nvml_lib = None
    try:
        _nvml_lib = ctypes.CDLL('libnvidia-ml.so.1')
    except Exception:
        pass
    nvmlInit()
    device_count = nvmlDeviceGetCount()
    print(f"[MONITOR] Found {device_count} NVIDIA GPU(s)")
except ImportError:
    print("[MONITOR] pynvml not installed, GPU monitoring disabled")
    device_count = 0
except Exception as e:
    print(f"[MONITOR] NVML init error: {e}")
    device_count = 0

# ---------------------------------------------------------------------------
# Configuration (tunable via query params or env)
# ---------------------------------------------------------------------------
CONFIG = {
    'history_max': int(os.environ.get('HISTORY_MAX', '120')),       # keep 120 samples (20min @10s)
    'history_interval': int(os.environ.get('HISTORY_INTERVAL', '10')),   # seconds
    'refresh_interval': int(os.environ.get('REFRESH_INTERVAL', '5000')), # ms for frontend auto-refresh
    'alert_cpu_temp': float(os.environ.get('ALERT_CPU_TEMP', '75')),
    'alert_cpu_usage': float(os.environ.get('ALERT_CPU_USAGE', '85')),
    'alert_ram_percent': float(os.environ.get('ALERT_RAM_PERCENT', '85')),
    'alert_disk_percent': float(os.environ.get('ALERT_DISK_PERCENT', '90')),
    'alert_gpu_temp': float(os.environ.get('ALERT_GPU_TEMP', '80')),
    'alert_gpu_usage': float(os.environ.get('ALERT_GPU_USAGE', '95')),
}

# ---------------------------------------------------------------------------
# Process cache (to avoid repeated iteration)
# ---------------------------------------------------------------------------
_last_process_snapshot = []
_last_process_time = 0
_process_cache_ttl = 5  # seconds

# ---------------------------------------------------------------------------
# History persistence path
# ---------------------------------------------------------------------------
HISTORY_DIR = os.path.expanduser('~/.hermes/data')
HISTORY_FILE = os.path.join(HISTORY_DIR, 'hardware-monitor-history.json')
os.makedirs(HISTORY_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Deque-based metric history (compact, auto-trim)
# ---------------------------------------------------------------------------
H = {
    'cpu_temp': deque(maxlen=CONFIG['history_max']),
    'cpu_freq': deque(maxlen=CONFIG['history_max']),
    'cpu_percent': deque(maxlen=CONFIG['history_max']),
    'ram': deque(maxlen=CONFIG['history_max']),
    'disk': deque(maxlen=CONFIG['history_max']),
    'network': deque(maxlen=CONFIG['history_max']),
    'gpu': deque(maxlen=CONFIG['history_max']),
    'gpu_freq': deque(maxlen=CONFIG['history_max']),
    'gpu_vram': deque(maxlen=CONFIG['history_max']),
    'gpu_power': deque(maxlen=CONFIG['history_max']),
    'disk_io': deque(maxlen=CONFIG['history_max']),
    'swap': deque(maxlen=CONFIG['history_max']),
    'net_errors': deque(maxlen=CONFIG['history_max']),
    'process_snapshot': deque(maxlen=CONFIG['history_max']),
    'llama': deque(maxlen=CONFIG['history_max']),
}

# Try loading persisted history from disk
def _load_history():
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, 'r') as f:
            data = json.load(f)
        for key in H:
            if key in data and isinstance(data[key], list):
                H[key].extend(data[key])
        print(f"[MONITOR] Loaded {len(H['cpu_temp'])} history samples from {HISTORY_FILE}")
    except Exception as e:
        print(f"[MONITOR] Failed to load history: {e}")

_load_history()

# Save counter — persist to disk every N collections
_save_counter = 0

def _save_history():
    global _save_counter
    _save_counter += 1
    if _save_counter < 10:   # save every 10 collections (~100s)
        return
    _save_counter = 0
    try:
        with _collection_lock:
            data = {k: list(H[k]) for k in H}
        tmp = HISTORY_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, HISTORY_FILE)
        print(f"[MONITOR] Saved {len(H['cpu_temp'])} history samples to {HISTORY_FILE}")
    except Exception as e:
        print(f"[MONITOR] Failed to save history: {e}")

# Previous counters for delta calculation
_prev_net = psutil.net_io_counters()
_prev_disk_io = psutil.disk_io_counters()
_prev_collect_time = time.time()
_collection_lock = threading.Lock()

# Server-side status cache (updated by background collector, served to clients)
_cached_status = None
_cached_status_lock = threading.RLock()

# Per-interface network tracking
_prev_net_perif = {}

# ---------------------------------------------------------------------------
# llama-server monitoring cache
# ---------------------------------------------------------------------------
_llama_cache = {'alive': False, 'model': None, 'context_used': 0, 'context_max': 0,
                'prompt_tps': None, 'gen_tps': None,
                'total_prompt_tokens': 0, 'total_predicted_tokens': 0, 'total_tokens': 0,
                'requests_processing': 0, 'requests_deferred': 0,
                'n_decode_total': 0, 'generation_time_seconds': 0}
_llama_cache_time = 0
_llama_tps_time = 0
_llama_tps_data = {'prompt_tps': None, 'gen_tps': None,
                   'total_prompt_tokens': 0, 'total_predicted_tokens': 0, 'total_tokens': 0,
                   'requests_processing': 0, 'requests_deferred': 0,
                   'n_decode_total': 0, 'generation_time_seconds': 0}

# ---------------------------------------------------------------------------
# Alert rules
# ---------------------------------------------------------------------------
def _check_alerts(data):
    """Return list of active alerts."""
    alerts = []
    cpu = data.get('cpu', {})
    ram = data.get('ram', {})
    disk = data.get('disk', {})
    gpus = data.get('gpu', [])

    # CPU temp
    temps = cpu.get('temps', {})
    for sensor, val in temps.items():
        if val > CONFIG['alert_cpu_temp']:
            alerts.append({
                'severity': 'warning' if val < 85 else 'critical',
                'source': 'CPU',
                'message': f"{sensor} at {val:.1f}°C (threshold {CONFIG['alert_cpu_temp']}°C)"
            })
    if cpu.get('percent', 0) > CONFIG['alert_cpu_usage']:
        alerts.append({
            'severity': 'warning',
            'source': 'CPU',
            'message': f"CPU usage {cpu['percent']:.0f}% (threshold {CONFIG['alert_cpu_usage']}%)"
        })
    if ram.get('percent', 0) > CONFIG['alert_ram_percent']:
        alerts.append({
            'severity': 'warning' if ram['percent'] < 92 else 'critical',
            'source': 'RAM',
            'message': f"RAM at {ram['percent']:.0f}% (threshold {CONFIG['alert_ram_percent']}%)"
        })
    if disk.get('percent', 0) > CONFIG['alert_disk_percent']:
        alerts.append({
            'severity': 'warning' if disk['percent'] < 95 else 'critical',
            'source': 'Disk',
            'message': f"Disk at {disk['percent']:.0f}% (threshold {CONFIG['alert_disk_percent']}%)"
        })
    for gpu in gpus:
        if gpu.get('temperature', 0) > CONFIG['alert_gpu_temp']:
            alerts.append({
                'severity': 'warning' if gpu['temperature'] < 85 else 'critical',
                'source': f'GPU {gpu["index"]}',
                'message': f"{gpu['name']} at {gpu['temperature']}°C (threshold {CONFIG['alert_gpu_temp']}°C)"
            })
        if gpu.get('utilization_gpu', 0) > CONFIG['alert_gpu_usage']:
            alerts.append({
                'severity': 'warning',
                'source': f'GPU {gpu["index"]}',
                'message': f"{gpu['name']} util {gpu['utilization_gpu']}% (threshold {CONFIG['alert_gpu_usage']}%)"
            })
    return alerts


def _compute_health(data):
    """Compute 0-100 health score from current metrics."""
    score = 100
    cpu = data.get('cpu', {})
    ram = data.get('ram', {})
    disk = data.get('disk', {})
    gpus = data.get('gpu', [])

    cpu_pct = cpu.get('percent', 0)
    ram_pct = ram.get('percent', 0)
    disk_pct = disk.get('percent', 0)

    if cpu_pct > 80: score -= 10
    if cpu_pct > 90: score -= 10
    if ram_pct > 80: score -= 10
    if ram_pct > 90: score -= 10
    if disk_pct > 85: score -= 10
    if disk_pct > 95: score -= 15

    temps = cpu.get('temps', {})
    for v in temps.values():
        if v > 70: score -= 5
        if v > 80: score -= 10

    for gpu in gpus:
        if gpu.get('temperature', 0) > 75: score -= 10
        if gpu.get('temperature', 0) > 85: score -= 10

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Metric collectors
# ---------------------------------------------------------------------------
def _collect_cpu():
    temps = {}
    try:
        sensors = psutil.sensors_temperatures()
        if sensors:
            for sensor, readings in sensors.items():
                for reading in readings:
                    temps[sensor] = reading.current
    except Exception:
        pass
    freq = psutil.cpu_freq()
    # Fallback for max freq when psutil returns 0 (common on AMD/virtualized)
    max_freq = round(freq.max, 1) if freq and freq.max else 0
    if max_freq == 0:
        # Try common sysfs paths
        for path in ['/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq',
                     '/sys/devices/system/cpu/cpu0/acpi_cpufreq/cpuinfo_max_freq']:
            try:
                with open(path) as f:
                    val = int(f.read().strip())
                    if val > 0:
                        max_freq = round(val / 1000, 1)
                        break
            except Exception:
                pass
    return {
        'temps': temps,
        'freq': {
            'current': round(freq.current, 1) if freq else 0,
            'max': max_freq,
        },
        'percent': psutil.cpu_percent(interval=None),
        'per_core': [round(p, 1) for p in psutil.cpu_percent(percpu=True)],
        'load_avg': [round(x, 2) for x in psutil.getloadavg()],
        'count_logical': psutil.cpu_count(logical=True),
        'count_physical': psutil.cpu_count(logical=False),
    }


def _collect_gpu():
    gpus = []
    if device_count > 0:
        for i in range(device_count):
            try:
                handle = nvmlDeviceGetHandleByIndex(i)
                temp = nvmlDeviceGetTemperature(handle, 0)
                mem = nvmlDeviceGetMemoryInfo(handle)
                util = nvmlDeviceGetUtilizationRates(handle)
                try:
                    fan = nvmlDeviceGetFanSpeed(handle)
                except Exception:
                    fan = -1
                # Fan RPM (NVML may not support this on all GPUs)
                fan_rpm = -1
                fan_min = 0
                fan_max = 100
                try:
                    if _nvml_lib is not None:
                        # RPM
                        _nvml_lib.nvmlDeviceGetFanSpeedRPM.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                        _nvml_lib.nvmlDeviceGetFanSpeedRPM.restype = ctypes.c_uint
                        rpm_val = ctypes.c_uint()
                        if _nvml_lib.nvmlDeviceGetFanSpeedRPM(handle, ctypes.byref(rpm_val)) == 0:
                            fan_rpm = rpm_val.value
                        # Min/Max
                        _nvml_lib.nvmlDeviceGetMinMaxFanSpeed.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
                        _nvml_lib.nvmlDeviceGetMinMaxFanSpeed.restype = ctypes.c_uint
                        mn = ctypes.c_uint()
                        mx = ctypes.c_uint()
                        if _nvml_lib.nvmlDeviceGetMinMaxFanSpeed(handle, ctypes.byref(mn), ctypes.byref(mx)) == 0:
                            fan_min = mn.value
                            fan_max = mx.value
                except Exception:
                    pass
                # Clock speeds (MHz)
                try:
                    core_clock = nvmlDeviceGetClockInfo(handle, 1)  # NVML_CLOCK_GRAPHICS
                except Exception:
                    core_clock = 0
                try:
                    max_clock = nvmlDeviceGetMaxClockInfo(handle, 1)
                except Exception:
                    max_clock = 0
                # Memory clock
                try:
                    mem_clock = nvmlDeviceGetClockInfo(handle, 0)  # NVML_CLOCK_MEM
                except Exception:
                    mem_clock = 0
                try:
                    max_mem_clock = nvmlDeviceGetMaxClockInfo(handle, 0)
                except Exception:
                    max_mem_clock = 0
                # Throttling detection
                throttle = {'throttling': False, 'reasons': []}
                try:
                    slowdown_temp = nvmlDeviceGetTemperatureThreshold(handle, NVML_TEMPERATURE_THRESHOLD_SLOWDOWN)
                    shutdown_temp = nvmlDeviceGetTemperatureThreshold(handle, NVML_TEMPERATURE_THRESHOLD_SHUTDOWN)
                    if temp >= slowdown_temp:
                        throttle['throttling'] = True
                        throttle['reasons'].append(f'Temp ({temp}°C) near slowdown threshold ({slowdown_temp}°C)')
                except Exception:
                    slowdown_temp = 0
                    shutdown_temp = 0
                try:
                    power_mw = nvmlDeviceGetPowerUsage(handle)
                    power_w = power_mw / 1000.0
                except Exception:
                    power_w = 0
                gpus.append({
                    'index': i,
                    'name': nvmlDeviceGetName(handle).decode() if isinstance(nvmlDeviceGetName(handle), bytes) else nvmlDeviceGetName(handle),
                    'temperature': temp,
                    'fan_speed': fan,
                    'fan_rpm': fan_rpm,
                    'fan_min': fan_min,
                    'fan_max': fan_max,
                    'core_clock': core_clock,
                    'max_clock': max_clock,
                    'mem_clock': mem_clock,
                    'max_mem_clock': max_mem_clock,
                    'power_w': round(power_w, 1),
                    'slowdown_temp': slowdown_temp,
                    'shutdown_temp': shutdown_temp,
                    'throttle': throttle,
                    'memory_used': mem.used,
                    'memory_total': mem.total,
                    'memory_used_gb': round(mem.used / (1024**3), 1),
                    'memory_total_gb': round(mem.total / (1024**3), 1),
                    'utilization_gpu': util.gpu,
                    'utilization_memory': util.memory,
                })
            except Exception:
                pass
    return gpus


def _collect_thermal():
    """Collect all available thermal sensor data: fans, temperatures, hwmon."""
    fans = {}
    try:
        pfans = psutil.sensors_fans()
        if pfans:
            for label, entries in pfans.items():
                fans[label] = [{'label': e.label or label, 'current': e.current} for e in entries]
    except Exception:
        pass

    temps = {}
    try:
        ptemps = psutil.sensors_temperatures()
        if ptemps:
            for sensor, readings in ptemps.items():
                temps[sensor] = [{
                    'label': r.label or sensor,
                    'current': round(r.current, 1),
                    'high': r.high,
                    'critical': r.critical,
                } for r in readings]
    except Exception:
        pass

    # Direct hwmon reads for extra detail
    hwmon_zones = []
    try:
        for d in sorted(os.listdir('/sys/class/hwmon/')):
            path = os.path.join('/sys/class/hwmon/', d)
            name_file = os.path.join(path, 'name')
            if not os.path.exists(name_file):
                continue
            with open(name_file) as f:
                name = f.read().strip()
            zones = []
            for fname in sorted(os.listdir(path)):
                if fname.startswith('temp') and fname.endswith('_input'):
                    idx = fname.split('_')[0]  # temp1, temp2, etc.
                    try:
                        with open(os.path.join(path, idx + '_input')) as f:
                            raw = int(f.read().strip())
                        label = idx + '_label'
                        lbl = name
                        if os.path.exists(os.path.join(path, label)):
                            with open(os.path.join(path, label)) as f:
                                lbl = f.read().strip()
                        zones.append({
                            'sensor': lbl,
                            'value_c': round(raw / 1000, 1),
                        })
                    except Exception:
                        pass
            if zones:
                hwmon_zones.append({'name': name, 'zones': zones})
    except Exception:
        pass

    return {
        'fans': fans if fans else {},
        'temperatures': temps if temps else {},
        'hwmon': hwmon_zones,
    }


def _collect_network_delta():
    global _prev_net, _prev_collect_time
    now = time.time()
    dt = now - _prev_collect_time
    if dt < 0.001:
        dt = 1
    net = psutil.net_io_counters()
    rx_bps = (net.bytes_recv - _prev_net.bytes_recv) / dt
    tx_bps = (net.bytes_sent - _prev_net.bytes_sent) / dt
    _prev_net = net
    _prev_collect_time = now

    # Per-interface tracking
    global _prev_net_perif
    interfaces = {}
    try:
        per_if = psutil.net_io_counters(pernic=True)
        for iface, cnt in per_if.items():
            prev = _prev_net_perif.get(iface, cnt)
            iface_rx = max(0, (cnt.bytes_recv - prev.bytes_recv) / dt) if prev is not cnt else 0
            iface_tx = max(0, (cnt.bytes_sent - prev.bytes_sent) / dt) if prev is not cnt else 0
            interfaces[iface] = {
                'rx_speed_bps': iface_rx,
                'tx_speed_bps': iface_tx,
                'rx_bytes': cnt.bytes_recv,
                'tx_bytes': cnt.bytes_sent,
            }
            _prev_net_perif[iface] = cnt
    except Exception:
        pass

    return {
        'rx_bytes': net.bytes_recv,
        'tx_bytes': net.bytes_sent,
        'rx_speed_bps': max(0, rx_bps),
        'tx_speed_bps': max(0, tx_bps),
        'packets_sent': net.packets_sent,
        'packets_recv': net.packets_recv,
        'errors_in': net.errin,
        'errors_out': net.errout,
        'drop_in': net.dropin,
        'drop_out': net.dropout,
        'interfaces': interfaces,
    }


def _collect_disk_io():
    global _prev_disk_io
    disk_io = psutil.disk_io_counters()
    delta_read = disk_io.read_bytes - _prev_disk_io.read_bytes
    delta_write = disk_io.write_bytes - _prev_disk_io.write_bytes
    _prev_disk_io = disk_io
    return {
        'read_bytes': disk_io.read_bytes,
        'write_bytes': disk_io.write_bytes,
        'read_speed_bs': max(0, delta_read / CONFIG['history_interval']),
        'write_speed_bs': max(0, delta_write / CONFIG['history_interval']),
        'read_count': disk_io.read_count,
        'write_count': disk_io.write_count,
        'read_time': disk_io.read_time,
        'write_time': disk_io.write_time,
    }


def _collect_disks():
    """Collect usage for all physical partitions."""
    partitions = []
    seen = set()
    for p in psutil.disk_partitions():
        if p.device in seen:
            continue
        # Skip pseudo filesystems
        if p.fstype in ('proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'fusectl',
                        'cgroup', 'cgroup2', 'pstore', 'bpf', 'securityfs',
                        'debugfs', 'tracefs', 'efivarfs', 'autofs', 'mqueue',
                        'hugetlbfs', 'configfs', 'ramfs', 'overlay'):
            continue
        if p.mountpoint.startswith('/sys') or p.mountpoint.startswith('/proc') or \
           p.mountpoint.startswith('/dev') or p.mountpoint.startswith('/run'):
            continue
        seen.add(p.device)
        try:
            usage = psutil.disk_usage(p.mountpoint)
            partitions.append({
                'device': p.device,
                'mount': p.mountpoint,
                'fstype': p.fstype,
                'total_gb': round(usage.total / (1024**3), 1),
                'used_gb': round(usage.used / (1024**3), 1),
                'free_gb': round(usage.free / (1024**3), 1),
                'percent': usage.percent,
            })
        except Exception:
            pass
    return partitions


def _collect_docker():
    """Collect running container stats using docker CLI."""
    try:
        import subprocess
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return None
        containers = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            try:
                c = json.loads(line)
                containers.append({
                    'name': c.get('Name', '?'),
                    'cpu_percent': c.get('CPUPerc', '0%'),
                    'mem_percent': c.get('MemPerc', '0%'),
                    'mem_usage': c.get('MemUsage', '0B / 0B'),
                    'net_io': c.get('NetIO', '0B / 0B'),
                    'block_io': c.get('BlockIO', '0B / 0B'),
                    'pids': c.get('PIDs', '0'),
                })
            except Exception:
                pass
        return containers if containers else None
    except Exception:
        return None


def _collect_processes():
    """Return top processes sorted by CPU usage (cached)."""
    global _last_process_snapshot, _last_process_time
    now = time.time()
    if now - _last_process_time < _process_cache_ttl and _last_process_snapshot:
        return _last_process_snapshot
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info', 'status']):
        try:
            info = p.info
            mem_info = info.get('memory_info')
            rss = mem_info.rss if mem_info is not None else 0
            procs.append({
                'pid': info['pid'],
                'name': info['name'] or '?',
                'cpu_percent': round(info.get('cpu_percent') or 0, 1),
                'memory_percent': round(info.get('memory_percent') or 0, 1),
                'memory_mb': round(rss / (1024**2), 1),
                'status': info.get('status', '?'),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
    _last_process_snapshot = procs[:25]
    _last_process_time = now
    return _last_process_snapshot


# ---------------------------------------------------------------------------
# llama-server monitoring
# ---------------------------------------------------------------------------
def _query_llama():
    """Query llama-server for status, model, token speeds, context usage.
    Uses caching: health/models/slots fetched every call, TPS measured every ~60s.
    """
    global _llama_cache, _llama_cache_time, _llama_tps_time, _llama_tps_data
    now = time.time()
    result = dict(_llama_cache)  # start with cached values
    try:
        # Health check (always fresh)
        req = urllib.request.Request('http://localhost:8080/health')
        r = urllib.request.urlopen(req, timeout=3)
        result['alive'] = (r.status == 200)

        # Model name (always fresh)
        try:
            r = urllib.request.urlopen('http://localhost:8080/v1/models', timeout=3)
            models_data = json.loads(r.read())
            if models_data.get('models'):
                result['model'] = models_data['models'][0].get('name', 'unknown')
        except Exception:
            pass

        # Slots (always fresh — context usage)
        try:
            r = urllib.request.urlopen('http://localhost:8080/slots', timeout=3)
            slots_data = json.loads(r.read())
            if slots_data:
                result['context_used'] = slots_data[0].get('n_prompt_tokens', 0)
                result['context_max'] = slots_data[0].get('n_ctx', 131072)
        except Exception:
            pass

        # Prometheus metrics (passive — no synthetic completions)
        if now - _llama_tps_time > 10:
            try:
                r = urllib.request.urlopen('http://localhost:8080/metrics', timeout=3)
                metrics_text = r.read().decode()
                metrics = {}
                for line in metrics_text.splitlines():
                    if line.startswith('#') or not line.strip():
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2:
                        metrics[parts[0]] = float(parts[1])
                _llama_tps_data = {
                    'prompt_tps': round(metrics.get('llamacpp:prompt_tokens_seconds', 0), 1),
                    'gen_tps': round(metrics.get('llamacpp:predicted_tokens_seconds', 0), 1),
                    'total_prompt_tokens': int(metrics.get('llamacpp:prompt_tokens_total', 0)),
                    'total_predicted_tokens': int(metrics.get('llamacpp:tokens_predicted_total', 0)),
                    'requests_processing': int(metrics.get('llamacpp:requests_processing', 0)),
                    'requests_deferred': int(metrics.get('llamacpp:requests_deferred', 0)),
                    'n_decode_total': int(metrics.get('llamacpp:n_decode_total', 0)),
                    'generation_time_seconds': round(metrics.get('llamacpp:tokens_predicted_seconds_total', 0), 1),
                    'total_tokens': int(metrics.get('llamacpp:prompt_tokens_total', 0) +
                                         metrics.get('llamacpp:tokens_predicted_total', 0)),
                }
            except Exception:
                pass  # keep last known values
            _llama_tps_time = now
        result.update(_llama_tps_data)
        _llama_cache = dict(result)
        _llama_cache_time = now
    except Exception as e:
        result['alive'] = False
    return result

# ---------------------------------------------------------------------------
# Trading engine monitoring
# ---------------------------------------------------------------------------
_trading_cache = {'alive': False, 'model': None, 'uptime': 0, 'open_trades': 0, 'strategies': [], 'errors': []}
_trading_cache_time = 0

def _collect_trading_engine():
    """Query Hermes trading engine on :5100."""
    global _trading_cache, _trading_cache_time
    now = time.time()
    if now - _trading_cache_time < 10:
        return _trading_cache
    result = {'alive': False, 'model': 'paper', 'uptime': 0, 'open_trades': 0,
              'strategies': [], 'errors': [], 'pairs': [], 'nav': 0,
              'balance': 0, 'daily_pnl': 0, 'drawdown': 0, 'equity': 0,
              'data_quality': 0, 'degraded_pairs': 0}
    try:
        r = urllib.request.urlopen('http://localhost:5100/api/health', timeout=3)
        if r.status == 200:
            result['alive'] = True
            health = json.loads(r.read())
            result['uptime'] = health.get('uptime', 0)
            result['model'] = health.get('mode', 'paper')
            result['pairs'] = health.get('pairs', [])
            dq = health.get('data_quality', {})
            result['data_quality'] = dq.get('overall_score', 0)
            result['degraded_pairs'] = dq.get('degraded_pairs', 0)
        # Engine status
        try:
            r = urllib.request.urlopen('http://localhost:5100/api/status', timeout=3)
            eng_status = json.loads(r.read())
            result['balance'] = eng_status.get('balance', 0)
            result['equity'] = eng_status.get('equity', 0)
            result['daily_pnl'] = eng_status.get('daily_pnl', 0)
            result['drawdown'] = eng_status.get('drawdown_pct', 0)
        except Exception:
            pass
        # Open trades
        try:
            r = urllib.request.urlopen('http://localhost:5100/api/trades/open', timeout=3)
            trades = json.loads(r.read())
            if isinstance(trades, dict) and 'trades' in trades:
                result['open_trades'] = len(trades['trades'])
            elif isinstance(trades, list):
                result['open_trades'] = len(trades)
            else:
                result['open_trades'] = 0
        except Exception:
            pass
        # Strategies
        try:
            r = urllib.request.urlopen('http://localhost:5100/api/strategies', timeout=3)
            strats = json.loads(r.read())
            result['strategies'] = strats if isinstance(strats, list) else []
        except Exception:
            pass
        # Pairs & NAV
        try:
            r = urllib.request.urlopen('http://localhost:5100/api/playbook/orchestrator/decisions', timeout=3)
            decisions = json.loads(r.read())
            if isinstance(decisions, dict) and 'pairs' in decisions:
                result['pairs'] = decisions['pairs']
            result['nav'] = decisions.get('nav', 0) if isinstance(decisions, dict) else 0
        except Exception:
            pass
    except Exception:
        result['alive'] = False
    _trading_cache = result
    _trading_cache_time = now
    return result

# ---------------------------------------------------------------------------
# Systemd service health
# ---------------------------------------------------------------------------
SERVICES_TO_MONITOR = ['llama-server@gemma-4-E4B-it-UD-Q4_K_XL', 'hermes-engine',
                       'hermes-gateway', 'hermes-dashboard', 'hermes-webui',
                       'hardware-monitor', 'frigate']
_systemd_cache = []
_systemd_cache_time = 0

def _collect_systemd():
    """Query systemd for key service statuses."""
    global _systemd_cache, _systemd_cache_time
    now = time.time()
    if now - _systemd_cache_time < 15:
        return _systemd_cache
    services = []
    for name in SERVICES_TO_MONITOR:
        try:
            r = subprocess.run(['systemctl', '--user', 'is-active', name],
                               capture_output=True, text=True, timeout=3)
            is_active = r.stdout.strip() == 'active'
            # Get load state
            r2 = subprocess.run(['systemctl', '--user', 'is-enabled', name],
                                capture_output=True, text=True, timeout=2)
            enabled = r2.stdout.strip() not in ('disabled', 'static')
            services.append({'name': name, 'active': is_active, 'enabled': enabled})
        except Exception:
            # Try root services (frigate)
            try:
                r = subprocess.run(['systemctl', 'is-active', name],
                                   capture_output=True, text=True, timeout=3)
                is_active = r.stdout.strip() == 'active'
                services.append({'name': name, 'active': is_active, 'enabled': True})
            except Exception:
                services.append({'name': name, 'active': False, 'enabled': False})
    _systemd_cache = services
    _systemd_cache_time = now
    return services

# ---------------------------------------------------------------------------
# Frigate monitoring
# ---------------------------------------------------------------------------
_frigate_cache = None
_frigate_cache_time = 0

def _collect_frigate():
    """Query Frigate NVR for camera status and detection stats (Frigate 0.17+)."""
    global _frigate_cache, _frigate_cache_time
    now = time.time()
    if now - _frigate_cache_time < 30:
        return _frigate_cache
    result = {'alive': False, 'cameras': [], 'detection_count': 0}
    # Frigate 0.17: /api/stats requires auth (login -> cookie -> stats)
    frigate_pass = os.environ.get('FRIGATE_PASSWORD', '')
    for url_base in ['https://localhost:8971', 'http://localhost:5000']:
        try:
            import http.cookiejar
            import ssl
            import urllib.request as _ur
            jar = http.cookiejar.CookieJar()
            opener = _ur.build_opener(_ur.HTTPCookieProcessor(jar))
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            if url_base.startswith('https'):
                opener = _ur.build_opener(
                    _ur.HTTPCookieProcessor(jar),
                    _ur.HTTPSHandler(context=ctx),
                )
            # login first (Frigate 0.17 requires auth for /api/*, incl. /api/version)
            if frigate_pass:
                try:
                    body = json.dumps({'user': 'admin', 'password': frigate_pass}).encode()
                    req = _ur.Request(
                        url_base + '/api/login',
                        data=body,
                        headers={'Content-Type': 'application/json', 'X-CSRF-TOKEN': '1'},
                        method='POST',
                    )
                    opener.open(req, timeout=5)
                except Exception:
                    pass  # cookie may still work / stats may be open
            # version probe
            r = opener.open(url_base + '/api/version', timeout=3)
            result['alive'] = (r.status == 200)
            if not result['alive']:
                continue
            r2 = opener.open(url_base + '/api/stats', timeout=5)
            stats = json.loads(r2.read())
            if isinstance(stats, dict) and isinstance(stats.get('cameras'), dict):
                # Frigate 0.17 format: {"cameras": {"garage": {...}}, "detectors": {...}}
                cameras = []
                for name, data in stats['cameras'].items():
                    cameras.append({
                        'name': name,
                        'fps': data.get('camera_fps', 0),
                        'detection_fps': data.get('detection_fps', 0),
                        'detection_enabled': data.get('detection_enabled', True),
                        'audio_rms': data.get('audio_rms', 0),
                        'audio_db': data.get('audio_dBFS', 0),
                    })
                    if data.get('detection_fps', 0) > 0:
                        result['detection_count'] += 1
                result['cameras'] = cameras
                break  # success on this URL
            elif isinstance(stats, dict):
                # legacy format (pre-0.13): camera_* keys at top level
                cameras = []
                for name, data in stats.items():
                    if name.startswith('camera_') or (isinstance(data, dict) and 'camera_fps' in data):
                        cam_name = name.replace('camera_', '')
                        cameras.append({
                            'name': cam_name,
                            'fps': data.get('camera_fps', 0),
                            'detection_fps': data.get('detection_fps', 0),
                            'detection_enabled': data.get('detection_enabled', True),
                            'audio_rms': data.get('audio_rms', 0),
                            'audio_db': data.get('audio_db', 0),
                        })
                        result['detection_count'] += data.get('detection_fps', 0) > 0 and 1 or 0
                result['cameras'] = cameras
                break  # success on this URL
        except Exception:
            continue  # try next URL
    _frigate_cache = result
    _frigate_cache_time = now
    return result

# ---------------------------------------------------------------------------
# Alert notification via Hermes Gateway
# ---------------------------------------------------------------------------
_last_alert_notification = {}  # {alert_message_key: last_sent_time}

def _send_alert_notification(alerts):
    """Send critical alerts via Hermes gateway Telegram webhook."""
    now = time.time()
    for alert in alerts:
        if alert.get('severity') != 'critical':
            continue
        key = f"{alert['source']}:{alert['message']}"
        if key in _last_alert_notification and now - _last_alert_notification[key] < 900:
            continue  # don't spam — once per 15min
        try:
            msg = f"🚨 *{alert['source']} CRITICAL*: {alert['message']}"
            import urllib.parse
            payload = json.dumps({
                'text': msg,
                'chat_id': None,  # gateway routes to home
                'parse_mode': 'Markdown'
            }).encode()
            req = urllib.request.Request(
                'http://localhost:8642/api/telegram/send',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            urllib.request.urlopen(req, timeout=3)
            _last_alert_notification[key] = now
            print(f"[MONITOR] Notified: {msg}")
        except Exception as e:
            print(f"[MONITOR] Alert notification failed: {e}")

# ---------------------------------------------------------------------------
# Process CPU history tracking
# ---------------------------------------------------------------------------
_last_process_snapshot_detail = []
_last_process_detail_time = 0
_process_detail_ttl = 10

def _collect_process_snapshot():
    """Track top processes over time for CPU history."""
    global _last_process_snapshot_detail, _last_process_detail_time
    now = time.time()
    if now - _last_process_detail_time < _process_detail_ttl and _last_process_snapshot_detail:
        return _last_process_snapshot_detail
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'memory_info']):
        try:
            info = p.info
            mem_info = info.get('memory_info')
            rss = mem_info.rss if mem_info is not None else 0
            procs.append({
                'pid': info['pid'],
                'name': info['name'] or '?',
                'cpu_percent': round(info.get('cpu_percent') or 0, 1),
                'memory_mb': round(rss / (1024**2), 1),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
    _last_process_snapshot_detail = procs[:10]
    _last_process_detail_time = now
    return _last_process_snapshot_detail


def _background_collector():
    global _save_counter, _cached_status
    print("[MONITOR] Background collector started")
    while True:
        time.sleep(CONFIG['history_interval'])
        with _collection_lock:
            ts = time.time()
            cpu = _collect_cpu()
            ram = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            net = _collect_network_delta()
            gpus = _collect_gpu()
            dio = _collect_disk_io()
            swap = psutil.swap_memory()
            partitions = _collect_disks()

            # CPU temp history (use first non-zero sensor temp, fallback to max)
            temps_list = list(cpu['temps'].values())
            non_zero = [t for t in temps_list if t > 0]
            _first_temp = non_zero[0] if non_zero else (temps_list[0] if temps_list else 0)
            H['cpu_temp'].append({'t': ts, 'v': _first_temp})
            H['cpu_freq'].append({'t': ts, 'v': cpu['freq']['current']})
            H['cpu_percent'].append({'t': ts, 'v': cpu['percent']})
            H['ram'].append({'t': ts, 'percent': ram.percent, 'used_gb': round(ram.used / (1024**3), 1),
                             'total_gb': round(ram.total / (1024**3), 1)})
            H['disk'].append({'t': ts, 'percent': disk.percent,
                              'free_gb': round(disk.free / (1024**3), 1),
                              'total_gb': round(disk.total / (1024**3), 1)})
            H['network'].append({'t': ts, 'rx_speed_mbps': round(net['rx_speed_bps'] / (1024**2), 2),
                                 'tx_speed_mbps': round(net['tx_speed_bps'] / (1024**2), 2),
                                 'rx_bytes': net['rx_bytes'], 'tx_bytes': net['tx_bytes']})
            H['disk_io'].append({'t': ts,
                                 'read_speed_mbps': round(dio['read_speed_bs'] / (1024**2), 2),
                                 'write_speed_mbps': round(dio['write_speed_bs'] / (1024**2), 2)})
            if gpus:
                H['gpu'].append({'t': ts, 'gpus': gpus})
                H['gpu_freq'].append({'t': ts, 'v': gpus[0].get('core_clock', 0)})
                vram_pct = round(gpus[0]['memory_used_gb'] / gpus[0]['memory_total_gb'] * 100, 1) if gpus[0]['memory_total_gb'] > 0 else 0
                H['gpu_vram'].append({'t': ts, 'v': vram_pct})
                H['gpu_power'].append({'t': ts, 'v': gpus[0].get('power_w', 0)})

            # Swap history
            H['swap'].append({'t': ts, 'percent': swap.percent, 'used_gb': round(swap.used / (1024**3), 3),
                              'total_gb': round(swap.total / (1024**3), 1) if swap.total > 0 else 0,
                              'sin_gb': round(swap.sin / (1024**3), 3),
                              'sout_gb': round(swap.sout / (1024**3), 3)})

            # Network errors/drops history
            H['net_errors'].append({'t': ts, 'errin': net['errors_in'], 'errout': net['errors_out'],
                                    'dropin': net['drop_in'], 'dropout': net['drop_out']})

            # Process snapshot history (top processes CPU)
            procs_snap = _collect_process_snapshot()
            H['process_snapshot'].append({'t': ts, 'procs': procs_snap[:5]})

            # Llama server history (for time-series graphs)
            try:
                llama_now = _query_llama()
                c_used = llama_now.get('context_used', 0)
                c_max = llama_now.get('context_max', 0)
                H['llama'].append({
                    't': ts,
                    'alive': bool(llama_now.get('alive')),
                    'prompt_tps': llama_now.get('prompt_tps'),
                    'gen_tps': llama_now.get('gen_tps'),
                    'kv_usage_pct': round(c_used / c_max * 100, 1) if c_max else 0,
                    'context_used': c_used,
                    'context_max': c_max,
                    'prompt_tokens_total': llama_now.get('total_prompt_tokens', 0),
                    'predicted_tokens_total': llama_now.get('total_predicted_tokens', 0),
                    'requests_processing': llama_now.get('requests_processing', 0),
                    'requests_deferred': llama_now.get('requests_deferred', 0),
                    'n_decode_total': llama_now.get('n_decode_total', 0),
                })
            except Exception as e:
                print(f"[MONITOR] llama history collect error: {e}")

            # Build cached status
            ram_data = {
                'percent': ram.percent,
                'used_gb': round(ram.used / (1024**3), 1),
                'total_gb': round(ram.total / (1024**3), 1),
                'available_gb': round(ram.available / (1024**3), 1),
                'swap': {
                    'total_gb': round(swap.total / (1024**3), 1) if swap.total > 0 else 0,
                    'used_gb': round(swap.used / (1024**3), 1) if swap.total > 0 else 0,
                    'percent': swap.percent if swap.total > 0 else 0,
                    'sin_gb': round(swap.sin / (1024**3), 3) if swap.total > 0 else 0,
                    'sout_gb': round(swap.sout / (1024**3), 3) if swap.total > 0 else 0,
                },
            }
            status_data = {
                'cpu': cpu,
                'ram': ram_data,
                'disk': {
                    'percent': disk.percent,
                    'free_gb': round(disk.free / (1024**3), 1),
                    'total_gb': round(disk.total / (1024**3), 1),
                    'used_gb': round(disk.used / (1024**3), 1),
                    'partitions': partitions,
                },
                'network': net,
                'disk_io': dio,
                'gpu': gpus,
                'thermal': _collect_thermal(),
                'docker': _collect_docker(),
                'uptime': time.time() - app_start_time,
                'hostname': os.uname().nodename,
                'platform': f"{os.uname().sysname} {os.uname().release}",
            }
            status_data['alerts'] = _check_alerts(status_data)
            status_data['health_score'] = _compute_health(status_data)
            # Send notifications for critical alerts (non-blocking)
            try:
                _send_alert_notification(status_data['alerts'])
            except Exception:
                pass
            with _cached_status_lock:
                _cached_status = status_data

        # Persist to disk periodically (outside the lock)
        _save_history()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html',
                           refresh_interval=CONFIG['refresh_interval'],
                           config=json.dumps(CONFIG),
                           api_key=API_KEY)


@app.route('/review')
def review_page():
    """Review hub — gate proposals + kanban cards needing operator input."""
    return render_template('review.html',
                           refresh_interval=30,
                           api_key=API_KEY)


@app.route('/api/status')
def get_status():
    live = request.args.get('live', '0') == '1'
    if not live:
        with _cached_status_lock:
            if _cached_status is not None:
                return jsonify(_cached_status)
    # Fallback: collect fresh
    cpu = _collect_cpu()
    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net = _collect_network_delta()
    gpus = _collect_gpu()
    dio = _collect_disk_io()
    swap = psutil.swap_memory()
    partitions = _collect_disks()

    data = {
        'cpu': cpu,
        'ram': {
            'percent': ram.percent,
            'used_gb': round(ram.used / (1024**3), 1),
            'total_gb': round(ram.total / (1024**3), 1),
            'available_gb': round(ram.available / (1024**3), 1),
            'used': ram.used,
            'total': ram.total,
            'available': ram.available,
            'swap': {
                'total_gb': round(swap.total / (1024**3), 1) if swap.total > 0 else 0,
                'used_gb': round(swap.used / (1024**3), 1) if swap.total > 0 else 0,
                'percent': swap.percent if swap.total > 0 else 0,
                'sin_gb': round(swap.sin / (1024**3), 3) if swap.total > 0 else 0,
                'sout_gb': round(swap.sout / (1024**3), 3) if swap.total > 0 else 0,
            },
        },
        'disk': {
            'percent': disk.percent,
            'free_gb': round(disk.free / (1024**3), 1),
            'total_gb': round(disk.total / (1024**3), 1),
            'used_gb': round(disk.used / (1024**3), 1),
            'free': disk.free,
            'total': disk.total,
            'partitions': partitions,
        },
        'network': net,
        'disk_io': dio,
        'gpu': gpus,
        'thermal': _collect_thermal(),
        'uptime': time.time() - app_start_time,
        'hostname': os.uname().nodename,
        'platform': f"{os.uname().sysname} {os.uname().release}",
    }

    data['alerts'] = _check_alerts(data)
    data['health_score'] = _compute_health(data)
    return jsonify(data)


@app.route('/api/processes')
def get_processes():
    sort_by = request.args.get('sort', 'cpu')
    procs = _collect_processes()
    if sort_by == 'memory':
        procs.sort(key=lambda x: x['memory_percent'], reverse=True)
    elif sort_by == 'ram_mb':
        procs.sort(key=lambda x: x['memory_mb'], reverse=True)
    else:
        procs.sort(key=lambda x: x['cpu_percent'], reverse=True)
    return jsonify({'processes': procs, 'count': len(procs)})


@app.route('/api/history')
def get_history():
    with _collection_lock:
        # Convert deques to lists for JSON
        h = {}
        for k in H:
            h[k] = list(H[k])
        h['total_records'] = len(H['cpu_temp'])
    return jsonify(h)


@app.route('/api/history/<metric>')
def get_history_metric(metric):
    """Get history for a single metric by name."""
    if metric in H:
        with _collection_lock:
            return jsonify(list(H[metric]))
    return jsonify({'error': f'Unknown metric: {metric}'}), 404


@app.route('/api/config')
def get_config():
    return jsonify(CONFIG)


@app.route('/api/health')
def api_health():
    """Quick health endpoint for load balancers / watchdog."""
    return jsonify({'status': 'ok', 'uptime': round(time.time() - app_start_time, 1)})


@app.route('/api/export')
def export_data():
    """Full snapshot of status + history as JSON."""
    h = get_history().get_json()
    s = get_status().get_json()
    return jsonify({'timestamp': time.time(), 'export': 'full', 'status': s, 'history': h})


@app.route('/api/llama')
def get_llama():
    """llama-server status + metrics."""
    return jsonify(_query_llama())


@app.route('/api/trading')
def get_trading():
    """Hermes trading engine status."""
    return jsonify(_collect_trading_engine())


@app.route('/api/systemd')
def get_systemd():
    """Systemd service health."""
    return jsonify({'services': _collect_systemd()})


@app.route('/api/frigate')
def get_frigate():
    """Frigate NVR status."""
    return jsonify(_collect_frigate())


@app.route('/api/export/csv')
def export_csv():
    """Export current history as CSV."""
    import csv
    import io
    with _collection_lock:
        hist = {k: list(H[k]) for k in H}
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['timestamp', 'metric', 'value', 'units'])
    for key, points in hist.items():
        for p in points:
            if isinstance(p, dict) and 't' in p:
                for k2, v2 in p.items():
                    if k2 == 't':
                        continue
                    if isinstance(v2, (int, float)):
                        writer.writerow([p['t'], f"{key}.{k2}", v2, ''])
                    elif isinstance(v2, list):
                        for item in v2:
                            if isinstance(item, dict):
                                for sk, sv in item.items():
                                    if isinstance(sv, (int, float)):
                                        writer.writerow([p['t'], f"{key}.{k2}.{sk}", sv, ''])
    resp = app.response_class(
        response=si.getvalue(),
        status=200,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=hardware-monitor.csv'}
    )
    return resp


@app.route('/api/process-history')
def get_process_history():
    """Historical top-process snapshots."""
    with _collection_lock:
        return jsonify(list(H['process_snapshot']))


# ---------------------------------------------------------------------------
# Review hub routes (gate proposals + kanban cards)
# ---------------------------------------------------------------------------
try:
    from review_routes import register_review_routes
    register_review_routes(app)
except Exception as _rev_exc:  # pragma: no cover — never take the monitor down
    print(f"[MONITOR] review routes unavailable: {_rev_exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    collector = threading.Thread(target=_background_collector, daemon=True)
    collector.start()
    print(f"[MONITOR] Starting on http://0.0.0.0:5001 (refresh {CONFIG['refresh_interval']}ms)")
    app.run(host='0.0.0.0', port=5001, debug=False, threaded=True)
