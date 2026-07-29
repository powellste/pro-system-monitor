#!/bin/bash
cd /home/ste/hardware-monitor
pkill -f "hardware_monitor" 2>/dev/null || true
sleep 1
python3 hardware_monitor_pro.py > /tmp/hw_monitor.log 2>&1 &
echo "Monitor started. PID: $!"
sleep 2
curl -s http://localhost:5001/api/status | head -1
