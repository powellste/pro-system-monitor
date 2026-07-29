#!/usr/bin/env python3
"""Test script to verify the API returns correct data structure"""

import requests
import json

base_url = "http://localhost:5001"

print("=" * 60)
print("Testing Hardware Monitor API")
print("=" * 60)

# Test status endpoint
print("\n1. Testing /api/status...")
try:
    r = requests.get(f"{base_url}/api/status")
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
    r = requests.get(f"{base_url}/api/history?hours=1&limit=5")
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
