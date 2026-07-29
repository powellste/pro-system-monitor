# Hardware Monitor Professional

A professional hardware monitoring dashboard with real-time metrics, health scoring, and historical data visualization.

## Features

- **Real-time Metrics**: CPU, GPU, RAM, Disk, Network monitoring
- **Health Score**: Automated system health calculation (0-100)
- **Persistent History**: SQLite database stores all readings for trend analysis
- **Alert System**: Color-coded warnings for threshold breaches
- **Professional UI**: Dark-mode dashboard with Chart.js visualizations
- **5-second polling**: Real-time updates without performance impact

## Quick Start

### Install Dependencies
```bash
cd /home/ste/hardware-monitor
pip install -r requirements.txt
```

### Run the Monitor
```bash
pip install -e skillclaw[evolve,sharing,server]  # Optional: collective skill evolution
cd /home/ste/hardware-monitor
bash start.sh
```

### Access Dashboard
Open your browser to: `http://localhost:5001`

## API Endpoints

- `GET /api/stats` - Current system statistics
- `GET /api/history` - Historical data (JSON format)
- `GET /api/health` - System health status (0-100 score)

## Configuration

Edit `config.py` to customize:
- Alert thresholds
- Monitoring intervals
- Display preferences
- Historical data retention

## Database

- **File**: `monitor_history.db` (SQLite)
- **Table**: `readings`
- **Schema**: `id`, `timestamp`, `cpu_percent`, `gpu_percent`, `ram_used`, `ram_total`, `disk_read`, `disk_write`, `network_in`, `network_out`, `health_score`

## Technical Details

### Stack
- **Backend**: Python 3.11 + Flask
- **Database**: SQLite3
- **Frontend**: HTML5 + Chart.js
- **Monitoring**: psutil, nvidia-ml-py

### Architecture
```
┌─────────────────────────────────────┐
│        HTML Dashboard (5001)        │
└─────────────────┬───────────────────┘
                  │
        ┌─────────▼─────────┐
        │  Flask Backend    │
        │  (hardware_monitor_pro.py)
        └─────────┬─────────┘
                  │
        ┌─────────▼─────────┐
        │  Data Collector   │
        │  (5s interval)    │
        └─────────┬─────────┘
                  │
    ┌─────────────┼─────────────┐
    │             │             │
┌───▼───┐   ┌────▼────┐   ┌────▼────┐
│ CPU    │   │  GPU    │   │  RAM/Disk│
│       │   │         │   │   Stats  │
│psutil │   │  pynvml │   │         │
└───────┘   └─────────┘   └─────────┘
```

### Database Schema
```sql
CREATE TABLE readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    cpu_percent REAL,
    gpu_percent REAL,
    ram_used REAL,
    ram_total REAL,
    disk_read BIGINT,
    disk_write BIGINT,
    network_in BIGINT,
    network_out BIGINT,
    health_score INTEGER
);

CREATE INDEX idx_readings_timestamp ON readings(timestamp);
```

## Recent Improvements

- ✅ Fixed SQLite cursor errors in `/api/stats` and `/api/history`
- ✅ Added professional dark-mode UI
- ✅ Implemented health score algorithm
- ✅ Added persistent history storage
- ✅ Color-coded alert system

## Future Enhancements

- [ ] Loading indicators during data collection
- [ ] Error handling for API failures
- [ ] Animated chart transitions
- [ ] Data export (CSV/JSON)
- [ ] System health summary dashboard
- [ ] Real-time alert notifications
- [ ] Customizable dashboard widgets
- [ ] Mobile-responsive design

## Troubleshooting

### API Returns 500 Error
```bash
# Check the database connection
sqlite3 monitor_history.db ".tables"

# Verify table schema
sqlite3 monitor_history.db ".schema readings"

# Check application logs
tail -f hardware_monitor_pro.py.log
```

### Database Lock Error
```bash
# Close any other connections to the database
pkill -f monitor_history.db

# Or check for locked files
lsof monitor_history.db
```

### GPU Metrics Not Working
```bash
# Verify NVML is installed
nvidia-smi

# Check package name
pip list | grep -i nvidia
# Should show: nvidia-ml-py (not pynvml)
```

## License

MIT License - Feel free to modify and distribute.

## Author

Created for local hardware monitoring and system health tracking.
