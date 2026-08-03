#!/usr/bin/env python3
"""LLM usage, cost & speed statistics for the PRO System Monitor.

Extra page (``/stats``) showing Hermes LLM provider usage:

  * per-provider / per-model token + cost totals, read-only from
    ``~/.hermes/state.db`` ``session_model_usage`` (aggregates)
  * daily time-series (last 30 days) grouped by billing provider
  * today's stats per provider group
  * live balances: OpenRouter credits, DeepSeek balance, local llama-server TPS
  * cloud LLM latency parsed from the trading engine log (``LLM timing`` lines)

Auth: all ``/api/*`` routes are guarded by the app's global X-API-Key check
(MONITOR_API_KEY). The page template embeds the key for its JS fetches, exactly
like index.html / review.html do.

Redeploy: edit file, then ``fuser -k 5001/tcp`` (systemd Respawn) — see the
hardware-monitor skill.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import jsonify, render_template

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
STATE_DB = HERMES_HOME / "state.db"
ENV_FILE = HERMES_HOME / ".env"
ENGINE_LOG = Path.home() / "hermes-ai-trading-agent" / "logs" / "engine_api.log"
HISTORY_JSON = HERMES_HOME / "data" / "hardware-monitor-history.json"

# billing_provider -> chart group. Anything custom*/'' is local.
GROUP_OF = {
    "deepseek": "deepseek",
    "openrouter": "openrouter",
    "auto": "auto",
    "moa": "moa",
}

GROUP_META: Dict[str, Dict[str, Any]] = {
    "deepseek": {"label": "DeepSeek", "color": "#3b82f6"},
    "openrouter": {"label": "OpenRouter", "color": "#a855f7"},
    "auto": {"label": "Auto", "color": "#06b6d4"},
    "moa": {"label": "MoA", "color": "#ec4899"},
    "local": {"label": "Local", "color": "#10b981"},
}

MODEL_LABEL_RE = re.compile(r"^custom:local-\((.*)\)$|^custom:(.*)$")

_LLM_TIMING_RE = re.compile(
    r"LLM timing (\S+): fetch=(\d+)ms call=(\d+)ms(?: total=(\d+)ms)?"
)

_CACHE: Dict[str, Any] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 60  # seconds for balance / log-derived data


def _cache_get(key: str) -> Optional[Any]:
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
    return None


def _cache_put(key: str, value: Any, ttl: int = _CACHE_TTL) -> Any:
    with _CACHE_LOCK:
        _CACHE[key] = (time.time() + ttl, value)
    return value


def _group_of(provider: Optional[str] = "", base_url: Optional[str] = "") -> str:
    """Classify a usage row into a display group.

    deepseek / openrouter / moa map to themselves. Everything that talks to a
    local endpoint (``billing_base_url`` contains 127.0.0.1 / localhost) is
    Local regardless of provider label — 'auto' rows frequently route to
    llama-swap on :8080. custom* and empty-provider rows are Local too
    (empty base_url, $0 rows are unmetered/local calls).
    """
    p = (provider or "").strip().lower()
    b = (base_url or "").lower()
    if p in GROUP_OF:  # deepseek / openrouter / auto / moa
        if p == "auto" and ("127.0.0.1" in b or "localhost" in b or "0.0.0.0" in b):
            return "local"
        return p
    return "local"


def _model_label(model: str) -> str:
    m = MODEL_LABEL_RE.match(model or "")
    if m:
        return m.group(1) or m.group(2) or model
    # short vendor/model display for very long identifiers
    return (model or "").split("/")[-1] or model or "?"


def _env(name: str) -> Optional[str]:
    """Read a key from the environment, falling back to the Hermes home .env."""
    val = os.environ.get(name)
    if val:
        return val
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return None


def _fetch_json(url: str, headers: Dict[str, str], timeout: int = 8) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "hardware-monitor/1.0", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (internal LAN/monitor)
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Provider balances
# ---------------------------------------------------------------------------

def _openrouter_balance() -> Dict[str, Any]:
    """Live OpenRouter account state (GET /api/v1/auth/key).

    The current API returns usage/limit fields, not a 'credits' balance —
    ``data.usage`` is USD spent with this key, ``data.limit_remaining`` is
    the remaining limit (None = no limit set).
    """
    key = _env("OPENROUTER_API_KEY")
    if not key:
        return {"ok": False, "error": "OPENROUTER_API_KEY not found in ~/.hermes/.env"}
    try:
        data = _fetch_json("https://openrouter.ai/api/v1/auth/key", {"Authorization": f"Bearer {key}"})
        info = (data or {}).get("data") or {}
        credits = info.get("credits")
        if isinstance(credits, dict):  # defensive: some shapes nest {total: ...}
            credits = credits.get("total")
        return {
            "ok": True,
            "credits": round(float(credits), 4) if isinstance(credits, (int, float)) else None,
            "usage": round(float(info.get("usage") or 0), 4),
            "usage_monthly": round(float(info.get("usage_monthly") or 0), 4),
            "limit_remaining": info.get("limit_remaining"),
            "is_free_tier": bool(info.get("is_free_tier")),
            "label": info.get("label") or "",
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 — report, never crash the page
        return {"ok": False, "error": f"openrouter: {exc}"}


def _deepseek_balance() -> Dict[str, Any]:
    """Live DeepSeek balance (GET /user/balance -> balance_infos[0].total_balance)."""
    key = _env("DEEPSEEK_API_KEY")
    if not key:
        return {"ok": False, "error": "DEEPSEEK_API_KEY not found in ~/.hermes/.env"}
    try:
        data = _fetch_json(
            "https://api.deepseek.com/user/balance",
            {"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )
        infos = (data or {}).get("balance_infos") or []
        if not infos:
            return {"ok": False, "error": "no balance_infos in response"}
        first = infos[0]
        try:
            balance = float(first.get("total_balance"))
        except (TypeError, ValueError):
            balance = None
        return {
            "ok": True,
            "balance": balance,
            "currency": first.get("currency") or "USD",
            "is_available": bool(data.get("is_available", True)),
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _llama_tps() -> Dict[str, Any]:
    """Local llama-server throughput, reusing the monitor's own _query_llama cache."""
    try:
        from hardware_monitor_pro import _query_llama  # lazy: avoid import cycle

        info = _query_llama()
        if not isinstance(info, dict):
            return {"ok": False, "error": "unexpected _query_llama result"}
        alive = bool(info.get("alive"))
        return {
            "ok": alive,
            "alive": alive,
            "model": info.get("model") or "",
            "prompt_tps": info.get("prompt_tps"),
            "gen_tps": info.get("gen_tps"),
            "context": info.get("context"),
            "error": None if alive else info.get("error") or "llama-server not alive",
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _local_speed_history(max_samples: int = 24) -> List[Dict[str, Any]]:
    """Hourly (latest-sample-per-hour) prompt/gen TPS from the history store."""
    try:
        data = json.loads(HISTORY_JSON.read_text())
        llama = data.get("llama") if isinstance(data, dict) else None
        if not isinstance(llama, list) or not llama:
            return []
        buckets: Dict[int, Dict[str, Any]] = {}
        for sample in llama:
            t = sample.get("t")
            if not t or not sample.get("alive"):
                continue
            hour = int(t // 3600) * 3600
            buckets[hour] = sample  # later samples within the hour win
        out = []
        for hour in sorted(buckets)[-max_samples:]:
            s = buckets[hour]
            out.append({
                "t": hour,
                "prompt_tps": s.get("prompt_tps"),
                "gen_tps": s.get("gen_tps"),
            })
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Cloud speeds from the engine log
# ---------------------------------------------------------------------------

def _cloud_speed() -> Dict[str, Any]:
    """Parse ``LLM timing <SYM>: fetch=Xms call=Yms`` lines from engine_api.log.

    Returns avg/median/max call latency today, per-symbol stats and an hourly
    latency history. Token counts are not present in these log lines, so
    tokens/sec is reported as unavailable.
    """
    cached = _cache_get("cloud_speed")
    if cached is not None:
        return cached

    today = datetime.now().date()
    calls: List[int] = []
    per_symbol: Dict[str, List[int]] = {}
    hourly: Dict[int, List[int]] = {}

    try:
        with ENGINE_LOG.open(errors="replace") as fh:
            for line in fh:
                m = _LLM_TIMING_RE.search(line)
                if not m:
                    continue
                ts_part = line[:19]
                try:
                    line_date = datetime.strptime(ts_part, "%Y-%m-%d %H:%M:%S").date()
                except ValueError:
                    continue
                if line_date != today:
                    continue
                sym, call_ms = m.group(1), int(m.group(3))
                calls.append(call_ms)
                per_symbol.setdefault(sym, []).append(call_ms)
                hourly.setdefault(int(line[11:13]), []).append(call_ms)  # HH from the line prefix
    except FileNotFoundError:
        result = {"ok": False, "error": "engine log not found", "count": 0}
        return _cache_put("cloud_speed", result)

    def _stats(vals: List[int]) -> Dict[str, float]:
        if not vals:
            return {"count": 0}
        s = sorted(vals)
        n = len(s)
        median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
        return {
            "count": n,
            "avg_ms": round(sum(s) / n, 1),
            "median_ms": round(median, 1),
            "max_ms": s[-1],
        }

    history = []
    now = datetime.now()
    for h in range(24):
        bucket_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23 - h)
        vals = hourly.get(bucket_start.hour, [])
        history.append({
            "t": int(bucket_start.timestamp()),
            "avg_call_ms": round(sum(vals) / len(vals), 1) if vals else None,
            "count": len(vals),
        })

    result = {
        "ok": True,
        "error": None,
        "today": _stats(calls),
        "by_symbol": {sym: _stats(v) for sym, v in sorted(per_symbol.items())},
        "history": history,
        "tokens_per_sec": None,  # token counts are not logged in LLM timing lines
    }
    return _cache_put("cloud_speed", result)


# ---------------------------------------------------------------------------
# state.db aggregates (read-only)
# ---------------------------------------------------------------------------

_AGG_COLS = [
    "api_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
]


def _row_stats(row, start: int = 1) -> Dict[str, Any]:
    """Read the 7 agg sums + cost from a query row.

    Column layout (0-indexed): key columns..., then at ``start`` the seven
    SUM(api_call_count..reasoning_tokens) values, then SUM(estimated_cost_usd)
    at ``start + 7``.
    """
    return {
        "calls": int(row[start + 0] or 0),
        "input_tokens": int(row[start + 1] or 0),
        "output_tokens": int(row[start + 2] or 0),
        "cache_read_tokens": int(row[start + 3] or 0),
        "cache_write_tokens": int(row[start + 4] or 0),
        "reasoning_tokens": int(row[start + 5] or 0),
        "cost_usd": round(float(row[start + 6] or 0), 6),
    }


def _open_state_db():
    con = sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _db_aggregates() -> Dict[str, Any]:
    """Read-only aggregation of session_model_usage (never writes)."""
    if not STATE_DB.exists():
        return {"state": "missing", "state_error": f"{STATE_DB} not found"}
    try:
        con = _open_state_db()
        try:
            agg = ", ".join(f"SUM(COALESCE({c},0))" for c in _AGG_COLS)
            # consistent layout: <keys...> | agg(7) | SUM(cost) | [COUNT(DISTINCT session)] | [MIN] | [MAX]

            # --- totals ---
            row = con.execute(
                f"SELECT COUNT(*), {agg}, SUM(COALESCE(estimated_cost_usd,0)), "
                "COUNT(DISTINCT session_id), MIN(first_seen), MAX(last_seen) "
                "FROM session_model_usage"
            ).fetchone()
            st = _row_stats(row, start=1)
            totals = {
                **st,
                "sessions": int(row[8] or 0),
                "first_seen": row[9],
                "last_seen": row[10],
            }

            # --- per provider (split by base_url so 'auto' local rows classify as Local) ---
            providers = []
            for r in con.execute(
                f"SELECT billing_provider, billing_base_url, {agg}, SUM(COALESCE(estimated_cost_usd,0)), "
                "COUNT(DISTINCT session_id), MIN(first_seen), MAX(last_seen) "
                "FROM session_model_usage GROUP BY billing_provider, billing_base_url "
                "ORDER BY SUM(COALESCE(estimated_cost_usd,0)) DESC"
            ).fetchall():
                pst = _row_stats(r, start=2)
                prov = r[0] or ""
                burl = r[1] or ""
                g = _group_of(prov, burl)
                providers.append({
                    "provider": prov,
                    "base_url": burl,
                    "label": GROUP_META[g]["label"] if prov == "" else prov,
                    "group": g,
                    **pst,
                    "sessions": int(r[9] or 0),
                    "first_seen": r[10],
                    "last_seen": r[11],
                })

            # --- per model (top 25 by cost) ---
            models = []
            for r in con.execute(
                f"SELECT model, billing_provider, {agg}, SUM(COALESCE(estimated_cost_usd,0)), "
                "COUNT(DISTINCT session_id), MAX(billing_base_url) "
                "FROM session_model_usage GROUP BY model, billing_provider "
                "ORDER BY SUM(COALESCE(estimated_cost_usd,0)) DESC LIMIT 25"
            ).fetchall():
                mst = _row_stats(r, start=2)
                models.append({
                    "model": r[0] or "",
                    "label": _model_label(r[0] or ""),
                    "provider": r[1] or "",
                    "group": _group_of(r[1], r[10]),
                    **mst,
                    "sessions": int(r[9] or 0),
                })

            # --- today (local midnight) ---
            midnight = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
            today = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "by_group": {}}
            for r in con.execute(
                f"SELECT CASE WHEN billing_base_url LIKE '%127.0.0.1%' OR billing_base_url LIKE '%localhost%' "
                "THEN 'local' ELSE COALESCE(billing_provider,'') END AS grp0, "
                f"{agg}, SUM(COALESCE(estimated_cost_usd,0)) "
                "FROM session_model_usage WHERE last_seen >= ? GROUP BY grp0",
                (midnight,),
            ).fetchall():
                tst = _row_stats(r, start=1)
                g = _group_of(r[0])
                today["calls"] += tst["calls"]
                today["input_tokens"] += tst["input_tokens"]
                today["output_tokens"] += tst["output_tokens"]
                today["cost_usd"] = round(today["cost_usd"] + tst["cost_usd"], 6)
                grp = today["by_group"].setdefault(g, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
                grp["calls"] += tst["calls"]
                grp["tokens"] += tst["input_tokens"] + tst["output_tokens"]
                grp["cost_usd"] = round(grp["cost_usd"] + tst["cost_usd"], 6)

            # --- daily series (last 30 days, local day of last_seen) ---
            daily_raw = {}
            for r in con.execute(
                f"SELECT strftime('%Y-%m-%d', last_seen, 'unixepoch', 'localtime') AS day, "
                f"CASE WHEN billing_base_url LIKE '%127.0.0.1%' OR billing_base_url LIKE '%localhost%' "
                "THEN 'local' ELSE COALESCE(billing_provider,'') END AS grp0, "
                f"{agg}, SUM(COALESCE(estimated_cost_usd,0)) "
                "FROM session_model_usage WHERE last_seen >= ? "
                "GROUP BY day, grp0 ORDER BY day",
                (midnight - 29 * 86400,),
            ).fetchall():
                dst = _row_stats(r, start=2)
                g = _group_of(r[1])
                grp = daily_raw.setdefault(r[0], {}).setdefault(g, {"calls": 0, "tokens": 0, "cost_usd": 0.0})
                grp["calls"] += dst["calls"]
                grp["tokens"] += dst["input_tokens"] + dst["output_tokens"]
                grp["cost_usd"] = round(grp["cost_usd"] + dst["cost_usd"], 6)

            daily = []
            for i in range(29, -1, -1):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                daily.append({"date": day, "by_group": daily_raw.get(day, {})})

            # --- folded group totals ---
            groups = {}
            for p in providers:
                g = p["group"]
                grp = groups.setdefault(g, {
                    "group": g,
                    "label": GROUP_META[g]["label"],
                    "color": GROUP_META[g]["color"],
                    "calls": 0, "input_tokens": 0, "output_tokens": 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0,
                    "reasoning_tokens": 0, "cost_usd": 0.0, "sessions": 0,
                })
                for k in ("calls", "input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_write_tokens", "reasoning_tokens"):
                    grp[k] += p[k]
                grp["cost_usd"] = round(grp["cost_usd"] + p["cost_usd"], 6)
                grp["sessions"] += p["sessions"]

            return {
                "state": "ok",
                "state_error": None,
                "window": {"first_seen": totals["first_seen"], "last_seen": totals["last_seen"]},
                "totals": totals,
                "groups": list(groups.values()),
                "providers": providers,
                "models": models,
                "today": today,
                "daily": daily,
            }
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        return {"state": "error", "state_error": str(exc)}


# ---------------------------------------------------------------------------
# Assembled payload + routes
# ---------------------------------------------------------------------------

def _api_payload() -> Dict[str, Any]:
    db = _db_aggregates()
    balances = {
        "openrouter": _cache_get("bal_or") or _cache_put("bal_or", _openrouter_balance()),
        "deepseek": _cache_get("bal_ds") or _cache_put("bal_ds", _deepseek_balance()),
        "local": _llama_tps(),  # cheap — always fresh from the monitor's cache
    }
    speeds = {
        "cloud": _cloud_speed(),
        "local_history": _local_speed_history(),
    }
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "db": {"path": str(STATE_DB), "size_bytes": STATE_DB.stat().st_size if STATE_DB.exists() else None},
        **db,
        "balances": balances,
        "speeds": speeds,
    }


def register_stats_routes(app) -> None:
    """Register the /stats page + /api/stats/llm JSON endpoint."""

    @app.get("/stats")
    def stats_page():
        return render_template(
            "stats.html",
            api_key=app.config.get("API_KEY") or os.environ.get("MONITOR_API_KEY", ""),
            refresh_interval=60,
        )

    @app.get("/api/stats/llm")
    def api_stats_llm():
        return jsonify(_api_payload())
