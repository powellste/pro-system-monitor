#!/usr/bin/env python3
"""
Hardware Monitor with detailed metrics
Collects CPU temps/speed, RAM, Disk, Network, and GPU data
"""

import os
import time
import threading
import psutil
import json

# Import NVML at module level (before any threading)
from pynvml import *


def create_app():
    """Create and configure the Flask app."""
    from flask import Flask, jsonify, render_template, request
    from flask_cors import CORS

    global app
    app = Flask(__name__)
    CORS(app, resources={r"/*": {"origins": "*"}})

    # Initialize NVML
    try:
        nvmlInit()
        device_count = nvmlDeviceGetCount()
        print(f"Found {device_count} NVIDIA GPU(s)")
    except Exception as e:
        print(f"Failed to initialize NVML: {e}")
        device_count = 0

    # Store metrics for history
    metrics_history = {
        'cpu_temps': [],
        'cpu_freqs': [],
        'ram': [],
        'disk': [],
        'network': [],
        'gpu': []
    }
    history_interval = 10  # seconds
    last_collection = time.time()
    collection_lock = threading.Lock()

    return app


def background_collector():
    """Background thread to collect metrics."""
    global app
    app = create_app()  # Initialize Flask app in background thread
    print("DEBUG: background_collector started")

    while True:
        time.sleep(app.history_interval if hasattr(app, 'history_interval') else 10)
        with app.collection_lock:
            now = time.time()
            # Collect CPU metrics
            temps = {}
            try:
                sensors = psutil.sensors_temperatures()
                if sensors:
                    for sensor, readings in sensors.items():
                        for reading in readings:
                            temps[sensor] = reading.current
                if temps:
                    print(f"DEBUG: Collected temps: {temps}")
                else:
                    print("DEBUG: No temps collected")
            except Exception as e:
                print(f"Error collecting temps: {e}")
            cpu_freq = psutil.cpu_freq()

            # Collect RAM
            ram = psutil.virtual_memory()

            # Collect Disk
            disk = psutil.disk_usage('/')

            # Collect Network
            net_io = psutil.net_io_counters()

            # Collect GPU metrics
            gpu_data = []
            if device_count > 0:
                for i in range(device_count):
                    try:
                        handle = nvmlDeviceGetHandleByIndex(i)
                        temp = nvmlDeviceGetTemperature(handle, 0)
                        mem_info = nvmlDeviceGetMemoryInfo(handle)
                        util = nvmlDeviceGetUtilizationRates(handle)
                        # Convert C structs to native Python types
                        gpu_data.append({
                            'index': i,
                            'name': nvmlDeviceGetName(handle),
                            'temperature': temp,
                            'memory_used': mem_info.used,
                            'memory_total': mem_info.total,
                            'utilization_gpu': util.gpu,
                            'utilization_memory': util.memory
                        })
                    except Exception as e:
                        print(f"Error collecting GPU data for device {i}: {e}")

            # Update history
            app.metrics_history['cpu_temps'].append(temps)
            app.metrics_history['cpu_freqs'].append(cpu_freq)
            app.metrics_history['ram'].append(ram)
            app.metrics_history['disk'].append(disk)
            app.metrics_history['network'].append(net_io)
            app.metrics_history['gpu'].append(gpu_data)


def update_history(history_key, data, max_history=50):
    """Update the history with new data."""
    if history_key not in metrics_history:
        metrics_history[history_key] = []
    metrics_history[history_key].append(data)
    if len(metrics_history[history_key]) > max_history:
        metrics_history[history_key] = metrics_history[history_key][-max_history:]


def collect_all_metrics():
    """Collect all metrics."""
    with app.collection_lock:
        now = time.time()
        # Collect CPU metrics
        temps = {}
        try:
            sensors = psutil.sensors_temperatures()
            if sensors:
                for sensor, readings in sensors.items():
                    for reading in readings:
                        temps[sensor] = reading.current
            if temps:
                print(f"DEBUG: Collected temps: {temps}")
            else:
                print("DEBUG: No temps collected")
        except Exception as e:
            print(f"Error collecting temps: {e}")
        cpu_freq = psutil.cpu_freq()

        # Collect RAM
        ram = psutil.virtual_memory()

        # Collect Disk
        disk = psutil.disk_usage('/')

        # Collect Network
        net_io = psutil.net_io_counters()

        # Collect GPU metrics
        gpu_data = []
        if device_count > 0:
            for i in range(device_count):
                try:
                    handle = nvmlDeviceGetHandleByIndex(i)
                    temp = nvmlDeviceGetTemperature(handle, 0)
                    mem_info = nvmlDeviceGetMemoryInfo(handle)
                    util = nvmlDeviceGetUtilizationRates(handle)
                    # Convert C structs to native Python types
                    gpu_data.append({
                        'index': i,
                        'name': nvmlDeviceGetName(handle),
                        'temperature': temp,
                        'memory_used': mem_info.used,
                        'memory_total': mem_info.total,
                        'utilization_gpu': util.gpu,
                        'utilization_memory': util.memory
                    })
                except Exception as e:
                    print(f"Error collecting GPU data for device {i}: {e}")

        # Update history
        metrics_history['cpu_temps'].append(temps)
        metrics_history['cpu_freqs'].append(cpu_freq)
        metrics_history['ram'].append(ram)
        metrics_history['disk'].append(disk)
        metrics_history['network'].append(net_io)
        metrics_history['gpu'].append(gpu_data)


@app.route('/')
def index():
    """Serve the web interface."""
    return render_template('index.html')


@app.route('/api/status')
def get_status():
    """Get current hardware status."""
    temps = {}
    try:
        sensors = psutil.sensors_temperatures()
        if sensors:
            for sensor, readings in sensors.items():
                for reading in readings:
                    temps[sensor] = reading.current
    except Exception as e:
        print(f"Error collecting temps: {e}")

    cpu_freq = psutil.cpu_freq()

    ram = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    net_io = psutil.net_io_counters()

    # Get GPU data
    gpu_data = []
    if device_count > 0:
        try:
            for i in range(device_count):
                handle = nvmlDeviceGetHandleByIndex(i)
                util = nvmlDeviceGetUtilizationRates(handle)
                mem_info = nvmlDeviceGetMemoryInfo(handle)
                gpu_data.append({
                    'index': i,
                    'name': nvmlDeviceGetName(handle),
                    'temperature': nvmlDeviceGetTemperature(handle, 0),
                    'memory_used': mem_info.used,
                    'memory_total': mem_info.total,
                    'utilization_gpu': util.gpu,
                    'utilization_memory': util.memory
                })
        except Exception as e:
            print(f"Error collecting GPU data: {e}")

    return jsonify({
        'cpu_temps': temps,
        'cpu_freq': {
            'current': cpu_freq.current if cpu_freq else 0,
            'max': cpu_freq.max if cpu_freq else 0,
            'min': cpu_freq.min if cpu_freq else 0
        },
        'cpu_percent': psutil.cpu_percent(interval=None),
        'cpu_count': psutil.cpu_count(logical=True),
        'ram_percent': ram.percent,
        'ram_used': ram.used,
        'ram_total': ram.total,
        'disk_usage': disk.percent,
        'disk_free': disk.free,
        'disk_total': disk.total,
        'network_rx': net_io.bytes_recv,
        'network_tx': net_io.bytes_sent,
        'gpu': gpu_data
    })


@app.route('/api/history')
def get_history():
    """Get historical metrics for charts."""
    with app.collection_lock:
        return jsonify({
            'cpu_temps': metrics_history['cpu_temps'][-50:],
            'cpu_freqs': metrics_history['cpu_freqs'][-50:],
            'ram': metrics_history['ram'][-50:],
            'disk': metrics_history['disk'][-50:],
            'network': metrics_history['network'][-50:],
            'gpu': metrics_history['gpu'][-50:]
        })


if __name__ == '__main__':
    # Create the app
    create_app()

    # Start the background collector
    collector_thread = threading.Thread(target=background_collector, daemon=True)
    collector_thread.start()

    print(f"Starting Hardware Monitor on http://0.0.0.0:5001")
    app.run(host='0.0.0.0', port=5001, debug=False)
