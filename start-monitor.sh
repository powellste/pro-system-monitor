#!/bin/bash
# Start the hardware monitor

cd /home/ste/hardware-monitor

# Kill any existing instances
pkill -f "hardware_monitor_pro.py" 2>/dev/null || true

# Start fresh
python3 hardware_monitor_pro.py > /tmp/hw_monitor.log 2>&1 &
PID=$!

echo "======================================"
echo "Hardware Monitor Started"
echo "======================================"
echo "PID: $PID"
echo "Log: /tmp/hw_monitor.log"
echo "Access: http://localhost:5001"
echo ""
echo "Checking if process started..."
sleep 2

if kill -0 $PID 2>/dev/null; then
    echo "✓ Monitor is running (PID: $PID)"
    echo ""
    echo "View logs with: tail -f /tmp/hw_monitor.log"
    echo "Test with: python3 test_monitor.py"
else
    echo "✗ Failed to start monitor"
    echo "Check logs: cat /tmp/hw_monitor.log"
    exit 1
fi
