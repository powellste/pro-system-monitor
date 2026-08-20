"""t_247a5fb9 (ram-94pct-oom-risk) — alert delivery tests.

Proves Candidate A in hardware_monitor_pro.py: the in-process Telegram Bot
API path replaces the `hermes send` CLI subprocess, with window-based dedup
(once per 15 min per source/severity) and failure-key recording with a 60s
retry backoff (no per-tick re-spawn storm under memory pressure).

Run from the repo root (needs hardware-monitor on sys.path so history_archive
resolves):
  /home/ste/hermes-ai-trading-agent/venv/bin/python -m pytest test_alert_delivery.py -v

The real token/channel load from ~/.hermes/.env at import, but every network
call here is stubbed — no test sends a real Telegram message.
"""

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, __file__.rsplit('/', 1)[0] if '/' in __file__ else '.')
import hardware_monitor_pro as h  # noqa: E402


class _FakeResp:
    """Minimal requests.Response stand-in: ok=True + raise_for_status no-op."""

    def __init__(self, ok=True):
        self._ok = ok
        self.status_code = 200 if ok else 400

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError('HTTP 400')

    def json(self):
        return {'ok': self._ok, 'description': 'ok' if self._ok else 'bad request'}


class _FakeRequests:
    """Stand-in for the module-level `requests` object.

    Records every post() call; `fail` makes the next call raise before any
    response is returned (simulates a network/API error).
    """

    def __init__(self):
        self.calls = []
        self.fail = False

    def post(self, url, **kwargs):
        self.calls.append({'url': url, **kwargs})
        if self.fail:
            self.fail = False
            raise RuntimeError('network error')
        return _FakeResp(ok=True)


class _Clock:
    def __init__(self, start=1_000_000.0):
        self.now = start

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    """Fresh dedup state + stubbed network for every test."""
    h._last_alert_notification = {}
    _fake = _FakeRequests()
    monkeypatch.setattr(h, 'requests', _fake)
    return _fake


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(h.time, 'time', c)
    return c


def _crit_ram(pct=94.0):
    return {'severity': 'critical', 'source': 'RAM',
            'message': f'RAM at {pct:.0f}% (threshold 85%)'}


# --- send path -------------------------------------------------------------

def test_send_payload_hits_bot_api(fake):
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call['url'].startswith('https://api.telegram.org/bot')
    assert call['url'].endswith('/sendMessage')
    assert call['timeout'] == 10
    assert call['json']['chat_id'] == h.TELEGRAM_HOME_CHANNEL
    assert call['json']['parse_mode'] == 'Markdown'
    assert '🚨 *RAM CRITICAL*: RAM at 94% (threshold 85%)' == call['json']['text']


def test_no_credentials_raises_without_network(fake, monkeypatch):
    monkeypatch.setattr(h, 'TELEGRAM_BOT_TOKEN', '')
    monkeypatch.setattr(h, 'TELEGRAM_HOME_CHANNEL', '')
    with pytest.raises(RuntimeError):
        h._telegram_send('hello')
    assert fake.calls == []  # never reached the network


def test_proxy_passed_when_configured(fake, monkeypatch):
    monkeypatch.setattr(h, 'TELEGRAM_PROXY', 'http://proxy.local:3128')
    h._telegram_send('x')
    assert fake.calls[0]['proxies'] == {'http': 'http://proxy.local:3128',
                                        'https': 'http://proxy.local:3128'}


def test_message_body_escaped_for_markdown(fake, clock):
    # Underscores in dynamic text (task ids, mount paths) must be escaped so
    # Telegram legacy Markdown does not 400 'can't parse entities'. The static
    # *SOURCE CRITICAL* prefix stays intentional markdown.
    alert = {'severity': 'critical', 'source': 'RAM',
             'message': 'RAM at 95% t_247a5fb9 (threshold 85%)'}
    h._send_alert_notification([alert])
    text = fake.calls[0]['json']['text']
    assert text.startswith('🚨 *RAM CRITICAL*: ')
    assert 't\\_247a5fb9' in text
    assert '*RAM CRITICAL*' in text


def test_error_never_contains_token(fake, clock, monkeypatch):
    # requests embeds the full request URL (with the bot token) in its
    # exception text; the raised error must stay token-free for journal safety.
    class _FailingPost:
        def __init__(self):
            self.calls = 0

        def post(self, url, **kwargs):
            self.calls += 1
            raise RuntimeError(f'Connection aborted to {url}')

    failing = _FailingPost()
    monkeypatch.setattr(h, 'requests', failing)
    with pytest.raises(RuntimeError) as exc_info:
        h._telegram_send('hello')
    assert h.TELEGRAM_BOT_TOKEN not in str(exc_info.value)


def test_http_error_never_contains_token(fake, clock, monkeypatch):
    class _BadResp:
        def __init__(self, status, ok):
            self.status_code = status
            self._ok = ok

        def json(self):
            return {'ok': self._ok, 'description': 'bad'}

    class _BadRequests:
        def post(self, url, **kwargs):
            return _BadResp(400, False)

    monkeypatch.setattr(h, 'requests', _BadRequests())
    with pytest.raises(RuntimeError) as exc_info:
        h._telegram_send('hello')
    assert 'bot' not in str(exc_info.value)
    assert 'HTTP 400' in str(exc_info.value)


# --- window dedup ----------------------------------------------------------

def test_percent_churn_does_not_resend(fake, clock):
    # Same 15-min window: 94 -> 96 -> 99% must produce exactly ONE send.
    h._send_alert_notification([_crit_ram(94.0)])
    h._send_alert_notification([_crit_ram(96.0)])
    h._send_alert_notification([_crit_ram(99.0)])
    assert len(fake.calls) == 1


def test_new_window_sends_again(fake, clock):
    h._send_alert_notification([_crit_ram()])
    clock.now += 901  # next 15-min window
    h._send_alert_notification([_crit_ram(95.0)])
    assert len(fake.calls) == 2


def test_different_sources_send_independently(fake, clock):
    h._send_alert_notification([_crit_ram(), _crit_ram()])
    h._send_alert_notification([{'severity': 'critical', 'source': 'CPU',
                                 'message': 'k10temp at 96.0°C'}])
    assert len(fake.calls) == 2


# --- failure backoff -------------------------------------------------------

def test_failure_recorded_with_60s_backoff(fake, clock):
    fake.fail = True
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 1  # first attempt failed
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 1  # suppressed by 60s backoff, same window
    clock.now += 61
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 2  # retried after backoff
    key = f"RAM:critical:{int(clock.now // 900)}"
    assert h._last_alert_notification[key]['sent_at'] is not None


def test_failure_then_success_then_silence(fake, clock):
    fake.fail = True
    h._send_alert_notification([_crit_ram()])
    fake.fail = False
    clock.now += 61
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 2
    clock.now += 30  # still inside the window
    h._send_alert_notification([_crit_ram()])
    assert len(fake.calls) == 2  # delivered already; no re-send


# --- gate preservation -----------------------------------------------------

def test_disk_warning_forwarded(fake, clock):
    alert = {'severity': 'warning', 'source': 'Disk',
             'message': 'Disk at 93% (threshold 90%)'}
    h._send_alert_notification([alert])
    assert len(fake.calls) == 1


def test_ram_warning_not_forwarded(fake, clock):
    alert = {'severity': 'warning', 'source': 'RAM',
             'message': 'RAM at 88% (threshold 85%)'}
    h._send_alert_notification([alert])
    assert len(fake.calls) == 0


def test_cpu_warning_not_forwarded(fake, clock):
    alert = {'severity': 'warning', 'source': 'CPU',
             'message': 'CPU usage 90% (threshold 85%)'}
    h._send_alert_notification([alert])
    assert len(fake.calls) == 0


def test_critical_cpu_forwarded(fake, clock):
    alert = {'severity': 'critical', 'source': 'CPU',
             'message': 'k10temp at 97.2°C (threshold 75°C)'}
    h._send_alert_notification([alert])
    assert len(fake.calls) == 1


# --- state bounding --------------------------------------------------------

def test_state_pruned_when_large(fake, clock):
    # 100 distinct windows (source churn) -> prune drops entries >1h old.
    for i in range(100):
        h._last_alert_notification[f'RAM:critical:{i}'] = {
            'sent_at': clock.now - (3600 + i), 'last_attempt': clock.now - (3600 + i)}
    h._send_alert_notification([_crit_ram()])
    assert len(h._last_alert_notification) <= 65
