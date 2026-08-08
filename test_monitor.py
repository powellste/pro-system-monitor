#!/usr/bin/env python3
"""Test script for hardware monitor - checks if monitor is running and collecting data"""

import os
import requests
import sqlite3
import time
import sys
from pathlib import Path


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


def test_monitor():
    """Test the hardware monitor API"""
    base_url = "http://localhost:5001"
    headers = {"X-API-Key": _api_key()} if _api_key() else {}
    
    print("=" * 60)
    print("Hardware Monitor Diagnostic Test")
    print("=" * 60)
    
    # Test health endpoint
    print("\n1. Testing health endpoint...")
    try:
        r = requests.get(f"{base_url}/api/health", headers=headers)
        if r.status_code == 200:
            health = r.json()
            print(f"   ✓ Monitor is running (uptime: {health.get('uptime', 'unknown')}s)")
        else:
            print(f"   ✗ Health check failed: {r.status_code}")
            return False
    except Exception as e:
        print(f"   ✗ Cannot reach monitor: {e}")
        return False
    
    # Test status endpoint
    print("\n2. Testing status endpoint...")
    try:
        r = requests.get(f"{base_url}/api/status", headers=headers)
        if r.status_code == 200:
            data = r.json()
            print(f"   ✓ Status retrieved")
            print(f"   Health Score: {data.get('health_score', 'N/A')}")
            print(f"   CPU Temp: {data.get('cpu', {}).get('temps', {}).get('k10temp', 'N/A')}°C")
            print(f"   RAM: {data.get('ram', {}).get('percent', 'N/A')}%")
            print(f"   Disks: {len(data.get('disk', []))} partitions")
            print(f"   Network: {data.get('network', {}).get('rx_bytes', 0):,} bytes recv")
        else:
            print(f"   ✗ Status check failed: {r.status_code}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    # Test history endpoint
    print("\n3. Testing history endpoint...")
    try:
        r = requests.get(f"{base_url}/api/history?hours=1&limit=10", headers=headers)
        if r.status_code == 200:
            history = r.json()
            print(f"   ✓ History retrieved")
            print(f"   Total records: {history.get('total_records', 0)}")
            print(f"   CPU temps points: {len(history.get('cpu_temps', []))}")
            print(f"   RAM points: {len(history.get('ram_percent', []))}")
            print(f"   Disk points: {len(history.get('disk_percent', []))}")
            print(f"   Network rates: {len(history.get('network_rates', []))}")
            
            if history.get('network_rates'):
                last_rate = history['network_rates'][-1]
                print(f"   Last network: {last_rate.get('rx', 0):.2f} MB/s ↓ / {last_rate.get('tx', 0):.2f} MB/s ↑")
        else:
            print(f"   ✗ History check failed: {r.status_code}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
    
    # Check database
    print("\n4. Checking database...")
    try:
        conn = sqlite3.connect('/home/ste/hardware-monitor/monitor_history.db')
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM metrics")
        count = cursor.fetchone()[0]
        print(f"   ✓ Database has {count} records")
        
        # Check last record
        cursor.execute("SELECT * FROM metrics ORDER BY timestamp DESC LIMIT 1")
        row = cursor.fetchone()
        print(f"   Last record timestamp: {row[1][:19] if row[1] else 'N/A'}")
        conn.close()
    except Exception as e:
        print(f"   ✗ Database error: {e}")
    
    print("\n" + "=" * 60)
    print("Diagnostic complete!")
    print("=" * 60)
    
    return True

if __name__ == "__main__":
    test_monitor()
