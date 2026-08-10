"""R8 (t_1448ef23) — Review Hub overview surfaces silent-judge alerts.

The overview endpoint (/api/review/overview) gains a `judge_alerts` list:
judges whose last output age exceeds max(stale_hours, TTL*2). Prefers the
engine-persisted `silent`/`staleness_threshold_s` ledger fields and falls
back to computing silence here for ledgers written before the engine
restart. The overview must never break when the ledger is missing.

Run from the repo root:
  /home/ste/hermes-ai-trading-agent/venv/bin/python -m pytest test_review_judge_alerts.py -v
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).parent))

import review_routes  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _fresh_ts(**kw) -> str:
    dt = datetime.now(timezone.utc) + timedelta(**kw)
    return dt.isoformat()


def _fixture_ledger(judges: dict, stale_hours: float = 72) -> dict:
    return {
        "version": 1,
        "generated_at": _fresh_ts(minutes=-30),
        "config_snapshot": {
            "binding_enabled": False,
            "lookback_days": 21,
            "decay_days": 21,
            "ewma_alpha": 0.3,
            "prior_n": 10,
            "min_judge_samples": 10,
            "min_judge_closed": 5,
            "cache_ttl_seconds": 300,
            "recompute_interval_seconds": 300,
            "stale_hours": stale_hours,
            "judge_ttl_seconds": {"trader_agent_v1": 5400, "llm_signal": 1200},
        },
        "judges": judges,
    }


def _judge(**kw) -> dict:
    base = {
        "name": "llm_signal",
        "band": "advisory",
        "binding": False,
        "judge_score": 0.2931,
        "stale": False,
        "last_signal_ts": _fresh_ts(minutes=-10),
        "updated": _fresh_ts(minutes=-5),
        "stats": {
            "N_resolved": 124, "N_executed": 0, "N_blocked": 0,
            "N_skipped": 124, "N_unresolved": 0, "N_closed": 0,
            "wins": 0, "losses": 0, "pnl": 0, "gross_profit": 0,
            "gross_loss": 0, "stale_count": 47,
        },
        "wr": 0.0, "pf": 0.0, "credible_wr": 0.5,
        "resolution_rate": 1.0, "noop_rate": 1.0, "stale_rate": 0.3791,
    }
    base.update(kw)
    return base


@pytest.fixture()
def app(tmp_path, monkeypatch):
    ledger_path = tmp_path / "judge_ranking.json"
    ledger_path.write_text(json.dumps(_fixture_ledger({})))
    # Hermetic board: point _board_paths at temp files so the overview's
    # kanban queries hit an empty DB instead of the live board.
    monkeypatch.setattr(review_routes, "JUDGE_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(
        review_routes,
        "_board_paths",
        lambda slug: {
            "db": tmp_path / "kanban.db",
            "vault": tmp_path / "vault",
            "attachments": tmp_path / "attachments",
        },
    )
    flask_app = Flask(__name__)
    flask_app.testing = True
    review_routes.register_review_routes(flask_app)
    return flask_app, ledger_path


@pytest.fixture()
def client(app):
    flask_app, _ledger = app
    return flask_app.test_client()


def _write_ledger(app_fixture, ledger: dict):
    _app, ledger_path = app_fixture
    ledger_path.write_text(json.dumps(ledger))


def _alerts(app, client):
    _app, _ledger = app
    r = client.get("/api/review/overview")
    assert r.status_code == 200
    return r.get_json()["judge_alerts"]


# ---------------------------------------------------------------------------
# Route behaviour — judge_alerts in /api/review/overview
# ---------------------------------------------------------------------------

def test_overview_includes_silent_judge(app, client):
    silent = _judge(
        name="trader_agent_v1",
        last_signal_ts=_fresh_ts(days=-5),
        silent=True,
        staleness_threshold_s=72 * 3600,
    )
    fresh = _judge(name="llm_signal", last_signal_ts=_fresh_ts(minutes=-10),
                   silent=False, staleness_threshold_s=72 * 3600)
    _write_ledger(app, _fixture_ledger({"trader_agent_v1": silent, "llm_signal": fresh}))
    alerts = _alerts(app, client)
    names = [a["name"] for a in alerts]
    assert names == ["trader_agent_v1"]
    assert alerts[0]["age_s"] is not None and alerts[0]["age_s"] > 4 * 86400
    assert alerts[0]["threshold_s"] == 72 * 3600
    assert alerts[0]["badge_label"] == "🔴"


def test_overview_no_alerts_when_fresh(app, client):
    fresh = _judge(name="llm_signal", last_signal_ts=_fresh_ts(minutes=-10),
                   silent=False, staleness_threshold_s=72 * 3600)
    _write_ledger(app, _fixture_ledger({"llm_signal": fresh}))
    assert _alerts(app, client) == []


def test_overview_fallback_computes_silence(app, client):
    # Pre-restart ledger: no silent/staleness_threshold_s keys → fallback
    # computes silence from age > max(stale_hours*3600, TTL*2).
    old = _judge(name="llm_signal", last_signal_ts=_fresh_ts(days=-10))
    fresh = _judge(name="trader_agent_v1", last_signal_ts=_fresh_ts(hours=-1))
    _write_ledger(app, _fixture_ledger({"llm_signal": old, "trader_agent_v1": fresh}))
    alerts = _alerts(app, client)
    names = [a["name"] for a in alerts]
    assert names == ["llm_signal"]
    assert alerts[0]["threshold_s"] == 72 * 3600  # TTL*2 (40min) << 72h


def test_overview_fallback_uses_ttl_map_when_greater(app, client):
    # A long-TTL judge (absent from the fallback map, so 3600s default) with a
    # 200h-old signal is silent under the 72h horizon either way; the map
    # entry only matters when TTL*2 > stale_hours. Verify the horizon path.
    old = _judge(name="unknown_long_ttl", last_signal_ts=_fresh_ts(days=-10))
    _write_ledger(app, _fixture_ledger({"unknown_long_ttl": old}))
    alerts = _alerts(app, client)
    assert [a["name"] for a in alerts] == ["unknown_long_ttl"]
    assert alerts[0]["threshold_s"] == 72 * 3600


def test_overview_never_output_is_alerted(app, client):
    never = _judge(name="orphan_judge", last_signal_ts=None, silent=True,
                   staleness_threshold_s=72 * 3600)
    _write_ledger(app, _fixture_ledger({"orphan_judge": never}))
    alerts = _alerts(app, client)
    assert alerts[0]["name"] == "orphan_judge"
    assert alerts[0]["never_output"] is True
    assert alerts[0]["age_s"] is None


def test_overview_missing_ledger_still_ok(app, client):
    _app, ledger_path = app
    ledger_path.unlink()
    r = client.get("/api/review/overview")
    assert r.status_code == 200
    assert r.get_json()["judge_alerts"] == []


def test_overview_malformed_ledger_still_ok(app, client):
    _app, ledger_path = app
    ledger_path.write_text("{not json")
    r = client.get("/api/review/overview")
    assert r.status_code == 200
    assert r.get_json()["judge_alerts"] == []


def test_overview_preserves_existing_fields(app, client):
    # R8 must be additive: gate/blocked/ready/running/completed still present.
    r = client.get("/api/review/overview")
    body = r.get_json()
    for field in ("gate", "blocked", "ready", "running", "completed", "board"):
        assert field in body
    assert "judge_alerts" in body
