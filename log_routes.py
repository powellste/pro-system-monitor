"""
Live log streaming routes for the system monitor dashboard.

Each monitored service maps to a whitelisted tail source (systemd journal,
docker, or canonical log file). Logs stream over SSE (text/event-stream);
the frontend connects with an EventSource. Auth is enforced by the global
before_request guard in hardware_monitor_pro.py (?key= or X-API-Key).

Safety: service names are looked up in LOG_SOURCES only — arbitrary argv is
never constructed from user input.
"""
import json
import os
import subprocess

from flask import Response, jsonify

HOME = os.path.expanduser('~')

# service name (matches SERVICES_TO_MONITOR in hardware_monitor_pro.py) -> argv
LOG_SOURCES = {
    'llama-server@gemma-4-12b-it-UD-Q5_K_XL': [
        'journalctl', '--user', '-u', 'llama-server@gemma-4-12b-it-UD-Q5_K_XL',
        '-f', '-n', '200', '--no-pager', '-o', 'short-iso',
    ],
    'hermes-engine': [
        'tail', '-n', '200', '-f', f'{HOME}/hermes-ai-trading-agent/logs/engine_api.log',
    ],
    'hermes-gateway': [
        'journalctl', '--user', '-u', 'hermes-gateway',
        '-f', '-n', '200', '--no-pager', '-o', 'short-iso',
    ],
    'hermes-dashboard': [
        'journalctl', '--user', '-u', 'hermes-dashboard',
        '-f', '-n', '200', '--no-pager', '-o', 'short-iso',
    ],
    'hermes-webui': [
        'journalctl', '--user', '-u', 'hermes-webui',
        '-f', '-n', '200', '--no-pager', '-o', 'short-iso',
    ],
    'hermes-sysmon': [
        'journalctl', '--user', '-u', 'hermes-sysmon',
        '-f', '-n', '200', '--no-pager', '-o', 'short-iso',
    ],
    'frigate': [
        'docker', 'logs', '-f', '--tail', '200', 'frigate',
    ],
}

# Human-friendly short labels for the log list endpoint
LOG_LABELS = {
    'llama-server@gemma-4-12b-it-UD-Q5_K_XL': 'llama-server',
    'hermes-engine': 'hermes-engine',
    'hermes-gateway': 'hermes-gateway',
    'hermes-dashboard': 'hermes-dashboard',
    'hermes-webui': 'hermes-webui',
    'hermes-sysmon': 'hermes-sysmon',
    'frigate': 'frigate',
}


def _stream_logs(cmd):
    """Generator: tail -f style, yields SSE frames. Kills subprocess on disconnect."""
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, errors='replace',
        )
        for line in proc.stdout:
            yield f"data: {json.dumps({'line': line.rstrip(chr(10))})}\n\n"
    finally:
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=3)
            except Exception:
                pass


def register_log_routes(app):
    @app.route('/api/logs')
    def api_logs_list():
        """List services with a log source (drives the frontend picker)."""
        return jsonify({
            'services': [
                {'name': name, 'label': LOG_LABELS.get(name, name),
                 'source': cmd[0]}
                for name, cmd in LOG_SOURCES.items()
            ]
        })

    @app.route('/api/logs/<service>')
    def api_logs_stream(service):
        cmd = LOG_SOURCES.get(service)
        if cmd is None:
            return jsonify({'error': f'unknown service: {service}'}), 404
        resp = Response(_stream_logs(cmd), mimetype='text/event-stream')
        resp.headers['Cache-Control'] = 'no-cache'
        resp.headers['X-Accel-Buffering'] = 'no'
        resp.headers['Connection'] = 'keep-alive'
        return resp
