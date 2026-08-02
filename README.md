# Hardware Monitor Professional

A professional hardware monitoring dashboard with real-time metrics, health scoring, and historical data visualization.

## Features

- **Real-time Metrics**: CPU, GPU, RAM, Disk, Network monitoring
- **Health Score**: Automated system health calculation (0-100)
- **Persistent History**: rolling ~24h JSON store + append-only JSONL archive (14-day retention) for trend analysis
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

## Data Storage

Metrics are stored in two layers:

- **Live store** (rolling ~24h): `~/.hermes/data/hardware-monitor-history.json`
  — in-memory deques (8640 samples @10s) persisted to disk every ~100s.
  Served by `GET /api/history`.
- **Long-term archive** (append-only JSONL, default 14-day retention):
  `~/.hermes/data/hardware-monitor-history.jsonl` — one compact JSON line per
  collection tick, pruned to `HISTORY_ARCHIVE_DAYS` (default 14). Durable
  across restarts; use for >24h trend analysis.

> The old `monitor_history.db` (SQLite) in the repo dir is a **dead artifact**
> — nothing writes it since 2026-07-28. Kept for reference; do not rely on it.

## Technical Details

### Stack
- **Backend**: Python 3.11 + Flask
- **Storage**: rolling JSON store + append-only JSONL archive (SQLite retired)
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

### Data Schema
The live store is a JSON object keyed by metric name, each value a list of
sample dicts `{t: <unix ts>, ...}` (e.g. `cpu_temp: [{t, v}]`, `ram: [{t,
percent, used_gb, total_gb}]`). The JSONL archive is one flat record per tick:
`{t, cpu_temp, cpu_freq, cpu_percent, ram_percent, ram_used_gb, ram_total_gb,
disk_percent, disk_free_gb, disk_total_gb, net_rx_mbps, net_tx_mbps,
swap_percent, gpu_temp, gpu_util, gpu_vram_pct, gpu_power_w, llama_alive,
llama_kv_pct}`.

## Recent Improvements

- ✅ Added append-only JSONL long-term archive (14-day default retention,
  env `HISTORY_ARCHIVE_DAYS`) — durable >24h history for trend analysis
- ✅ Added professional dark-mode UI
- ✅ Implemented health score algorithm
- ✅ Added persistent history storage (rolling JSON store)
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
# Check the live JSON store is fresh
python3 -c "import json,time;d=json.load(open('/home/ste/.hermes/data/hardware-monitor-history.json'));print('last sample age (s):', round(time.time()-d['cpu_temp'][-1]['t'],1))"

# Check application logs
journalctl --user -u hardware-monitor -n 50 --no-pager
tail -f hardware_monitor_pro.py.log
```

### History Missing / Archive Not Growing
```bash
# Live store freshness (last sample should be < ~60s old)
ls -la ~/.hermes/data/hardware-monitor-history.json*

# JSONL archive line count should increase every ~10s
wc -l ~/.hermes/data/hardware-monitor-history.jsonl

# Restart the collector if stale
systemctl --user restart hardware-monitor
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
