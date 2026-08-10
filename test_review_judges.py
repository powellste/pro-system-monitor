"""R3 (t_32045c48) — tests for GET /api/review/judges + Judges panel data.

The endpoint is a read-only view of the engine's judge-ranking ledger
(engine/services/judge_ranking.py -> data/judge_ranking.json) plus planner
accuracy (data/session_plans/plan_accuracy.json). We exercise the route with
a bare Flask app and fixture ledgers (same schema as the real one), and run
one smoke test against the real ledger when it exists.

Run from the repo root:
  /home/ste/hermes-ai-trading-agent/venv/bin/python -m pytest test_review_judges.py -v
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
    """ISO-8601 UTC timestamp (ledger style) relative to now."""
    dt = datetime.now(timezone.utc) + timedelta(**kw)
    return dt.isoformat()


def _fixture_ledger(judges: dict) -> dict:
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
            "stale_hours": 72,
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
    if "name" in kw or base["name"] == "llm_signal":
        pass
    return base


@pytest.fixture()
def app(tmp_path, monkeypatch):
    ledger_path = tmp_path / "judge_ranking.json"
    plan_path = tmp_path / "plan_accuracy.json"
    ledger_path.write_text(json.dumps(_fixture_ledger({})))
    plan_path.write_text(json.dumps({}))
    monkeypatch.setattr(review_routes, "JUDGE_LEDGER_PATH", ledger_path)
    monkeypatch.setattr(review_routes, "PLAN_ACCURACY_PATH", plan_path)
    flask_app = Flask(__name__)
    flask_app.testing = True
    review_routes.register_review_routes(flask_app)
    return flask_app, ledger_path, plan_path


@pytest.fixture()
def client(app):
    flask_app, _ledger, _plan = app
    return flask_app.test_client()


def _write_ledger(app_fixture, ledger: dict):
    _app, ledger_path, _plan = app_fixture
    ledger_path.write_text(json.dumps(ledger))


def _write_plan(app_fixture, plan: dict):
    _app, _ledger, plan_path = app_fixture
    plan_path.write_text(json.dumps(plan))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_parse_iso_ts_handles_ledger_format():
    # ledger timestamps: 2026-08-10T02:59:38.410160+00:00
    ts = review_routes._parse_iso_ts("2026-08-10T02:59:38.410160+00:00")
    assert ts is not None
    assert abs(ts - datetime(2026, 8, 10, 2, 59, 38, tzinfo=timezone.utc).timestamp()) < 1.0


def test_parse_iso_ts_z_and_naive():
    assert review_routes._parse_iso_ts("2026-08-10T02:59:38Z") is not None
    assert review_routes._parse_iso_ts("2026-08-10 02:59:38") is not None


def test_parse_iso_ts_invalid_returns_none():
    assert review_routes._parse_iso_ts(None) is None
    assert review_routes._parse_iso_ts("") is None
    assert review_routes._parse_iso_ts("not-a-date") is None
    assert review_routes._parse_iso_ts(12345) is None  # epoch int is not ISO


def test_read_json_file_missing_or_malformed(app):
    _app, _ledger, _plan = app
    assert review_routes._read_json_file(Path("/nonexistent/nope.json")) is None
    bad = _ledger.with_name("bad.json")
    bad.write_text("{not json")
    assert review_routes._read_json_file(bad) is None


# ---------------------------------------------------------------------------
# Route behaviour
# ---------------------------------------------------------------------------

def test_missing_ledger_returns_404(app, client):
    _app, ledger_path, _plan = app
    ledger_path.unlink()  # no ledger at all
    r = client.get("/api/review/judges")
    assert r.status_code == 404
    body = r.get_json()
    assert body["ok"] is False
    assert "ledger" in body["error"]


def test_malformed_ledger_returns_404(app, client):
    _app, ledger_path, _plan = app
    ledger_path.write_text(json.dumps({"version": 1}))  # missing "judges"
    r = client.get("/api/review/judges")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False


def test_returns_ledger_fields_sorted_by_score(app, client):
    judges = {
        "trader_agent_v1": _judge(name="trader_agent_v1", judge_score=0.3085),
        "llm_signal": _judge(name="llm_signal", judge_score=0.2931),
        "fx_session_momentum_v1": _judge(name="fx_session_momentum_v1", judge_score=0.2),
    }
    _write_ledger(app, _fixture_ledger(judges))
    r = client.get("/api/review/judges")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    names = [j["name"] for j in body["judges"]]
    assert names == ["trader_agent_v1", "llm_signal", "fx_session_momentum_v1"]
    j = body["judges"][0]
    assert j["key"] == "trader_agent_v1"
    assert j["band"] == "advisory"
    assert j["binding"] is False
    assert j["judge_score"] == 0.3085
    assert j["badge"] == "low-n"
    assert j["last_output_age_s"] is not None and j["last_output_age_s"] >= 0
    assert j["stale"] is False
    for k in ("N_resolved", "N_executed", "N_blocked", "N_skipped",
              "N_unresolved", "N_closed"):
        assert k in j["stats"]
    for k in ("wr", "pf", "credible_wr", "resolution_rate", "noop_rate",
              "stale_rate", "last_signal_ts", "updated"):
        assert k in j
    assert body["config"]["stale_hours"] == 72
    assert body["ledger_generated_at"]


def test_stale_badge_and_age_for_old_signal(app, client):
    judges = {
        "quiet_judge": _judge(
            name="quiet_judge", judge_score=0.5,
            last_signal_ts=_fresh_ts(hours=-80),  # > 72h stale_hours
        ),
    }
    _write_ledger(app, _fixture_ledger(judges))
    body = client.get("/api/review/judges").get_json()
    j = body["judges"][0]
    assert j["stale"] is True
    assert j["badge"] == "stale"
    assert j["badge_label"] == "🔴"
    assert j["last_output_age_s"] > 72 * 3600


def test_ledger_stale_flag_wins_even_when_fresh(app, client):
    # ledger's own stale flag takes precedence over the computed age
    judges = {"flagged": _judge(name="flagged", judge_score=0.9, stale=True,
                                last_signal_ts=_fresh_ts(minutes=-1))}
    _write_ledger(app, _fixture_ledger(judges))
    j = client.get("/api/review/judges").get_json()["judges"][0]
    assert j["stale"] is True
    assert j["badge"] == "stale"


def test_healthy_badge_fresh_and_high_score(app, client):
    judges = {"ace": _judge(name="ace", judge_score=0.85,
                            last_signal_ts=_fresh_ts(minutes=-5))}
    _write_ledger(app, _fixture_ledger(judges))
    j = client.get("/api/review/judges").get_json()["judges"][0]
    assert j["badge"] == "healthy"
    assert j["badge_label"] == "🟢"


def test_low_n_badge_for_advisory_band(app, client):
    # advisory band with fresh low score -> low-N (advisory)
    judges = {"fresh_adv": _judge(name="fresh_adv", judge_score=0.1)}
    _write_ledger(app, _fixture_ledger(judges))
    j = client.get("/api/review/judges").get_json()["judges"][0]
    assert j["band"] == "advisory"
    assert j["badge"] == "low-n"
    assert j["badge_label"] == "🟡"


def test_gated_badge_for_non_advisory_low_score(app, client):
    # binding band that failed the healthy threshold -> gated
    judges = {"slipping": _judge(name="slipping", band="binding", binding=True,
                                 judge_score=0.4)}
    _write_ledger(app, _fixture_ledger(judges))
    j = client.get("/api/review/judges").get_json()["judges"][0]
    assert j["badge"] == "gated"
    assert j["badge_label"] == "⚪"


def test_no_signal_ts_means_no_age_and_fresh(app, client):
    judges = {"never": _judge(name="never", last_signal_ts=None)}
    _write_ledger(app, _fixture_ledger(judges))
    j = client.get("/api/review/judges").get_json()["judges"][0]
    assert j["last_output_age_s"] is None
    assert j["stale"] is False  # no age to compare -> rely on ledger flag only


def test_planner_accuracy_included(app, client):
    _write_ledger(app, _fixture_ledger({"a": _judge(name="a")}))
    _write_plan(app, {
        "__meta__": {"updated": _fresh_ts()},
        "BTC/USD": {"hits": 22, "total": 77, "hit_rate": 0.591, "ewma": 0.591,
                    "dir_hits": 0, "dir_total": 0, "dir_hit_rate": None,
                    "neutral_hits": 2, "neutral_total": 2,
                    "neutral_avoid_rate": 1.0, "updated": _fresh_ts()},
        "ETH/USD": {"hits": 20, "total": 82, "hit_rate": 0.511, "ewma": 0.511,
                    "dir_hits": 0, "dir_total": 0, "dir_hit_rate": None,
                    "neutral_hits": 2, "neutral_total": 2,
                    "neutral_avoid_rate": 1.0, "updated": _fresh_ts()},
    })
    body = client.get("/api/review/judges").get_json()
    pa = body["planner_accuracy"]
    assert pa["BTC/USD"]["hit_rate"] == 0.591
    assert pa["ETH/USD"]["total"] == 82


def test_missing_plan_accuracy_is_empty_not_error(app, client):
    _write_ledger(app, _fixture_ledger({"a": _judge(name="a")}))
    body = client.get("/api/review/judges").get_json()
    assert body["ok"] is True
    assert body["planner_accuracy"] == {}


# ---------------------------------------------------------------------------
# Real-data smoke test (the incident this card serves: Steve opens Review Hub
# and wants to see the live judges with scores)
# ---------------------------------------------------------------------------

def test_real_ledger_endpoint_smoke(app, client):
    real = Path.home() / "hermes-ai-trading-agent" / "data" / "judge_ranking.json"
    real_plan = Path.home() / "hermes-ai-trading-agent" / "data" / "session_plans" / "plan_accuracy.json"
    if not real.is_file() or not real_plan.is_file():
        pytest.skip("real judge ledger / plan accuracy not present")
    _app, _ledger, _plan = app
    _ledger.write_text(real.read_text(encoding="utf-8"))
    _plan.write_text(real_plan.read_text(encoding="utf-8"))
    body = client.get("/api/review/judges").get_json()
    assert body["ok"] is True
    assert len(body["judges"]) >= 1
    for j in body["judges"]:
        assert 0.0 <= j["judge_score"] <= 1.0
        assert j["name"]
        assert j["badge"] in ("healthy", "low-n", "stale", "gated")
    assert len(body["planner_accuracy"]) >= 1
