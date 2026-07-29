#!/bin/bash
cd /home/ste/hardware-monitor
/home/ste/.hermes/hermes-agent/venv/bin/python3 hardware_monitor.py > /tmp/hw_monitor.log 2>&1 &
echo "Hardware monitor started with PID: $!"
exec
