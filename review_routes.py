"""Review Hub backend — gate proposals, kanban cards needing review, instructions.

Extra page for the hardware monitor (:5001) so the operator can review and
approve cards / proposals and post instructions without Telegram.

Data sources (read-only):
  - pain-point kanban board: ~/.hermes/kanban/boards/pain-point/kanban.db
  - gate proposals: ~/hermes-multi-agent-workflow/work/vault/items/*.md
    with frontmatter status=awaiting_approval

Mutations (shell out to the sanctioned paths, never direct DB writes):
  - gate proposals -> proposal_actions.py (approve / shelve / modify)
  - card actions   -> `hermes kanban` CLI (comment / unblock / complete)

Auth: all /api/review/* routes require MONITOR_API_KEY (the same key guard
every other /api/ route uses). The page template embeds the key for its JS
fetches, exactly like index.html does.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from flask import jsonify, request, send_file


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
_BOARDS: Dict[str, Dict[str, Path]] = {
    # slug -> {"db": kanban db, "vault": item vault dir}
    "pain-point": {
        "db": HERMES_HOME / "kanban" / "boards" / "pain-point" / "kanban.db",
        "vault": Path.home() / "hermes-multi-agent-workflow" / "work" / "vault" / "items",
    },
    "sp-photo": {
        "db": HERMES_HOME / "kanban" / "boards" / "sp-photo" / "kanban.db",
        "vault": Path.home() / "hermes-multi-agent-workflow" / "work-sp-photo" / "vault" / "items",
    },
    "system-monitor": {
        "db": HERMES_HOME / "kanban" / "boards" / "system-monitor" / "kanban.db",
        "vault": Path.home() / "hermes-multi-agent-workflow" / "work-system-monitor" / "vault" / "items",
    },
}
WORKFLOW_DIR = Path.home() / "hermes-multi-agent-workflow"
PROPOSAL_ACTIONS = WORKFLOW_DIR / "proposal_actions.py"

# Back-compat defaults (routes resolve per-request; these are the fallbacks).
KANBAN_DB = _BOARDS["pain-point"]["db"]
VAULT_DIR = _BOARDS["pain-point"]["vault"]

BOARD_SLUG = "pain-point"

# Judge-ranking ledger + planner accuracy — read-only view of the trading
# repo's data dir. The ledger is written by the engine
# (engine/services/judge_ranking.py recompute + engine/signal_decision_log.py
# event mirroring); the monitor never writes it.
TRADING_REPO_DIR = Path.home() / "hermes-ai-trading-agent"
JUDGE_LEDGER_PATH = TRADING_REPO_DIR / "data" / "judge_ranking.json"
PLAN_ACCURACY_PATH = TRADING_REPO_DIR / "data" / "session_plans" / "plan_accuracy.json"


def _resolve_board() -> str:
    """Resolve the active board slug from ?board= (or X-Board header)."""
    slug = (
        request.args.get("board")
        or request.headers.get("X-Board")
        or ""
    ).strip()
    if slug not in _BOARDS:
        return BOARD_SLUG
    return slug


def _board_paths(slug: str) -> Dict[str, Path]:
    return _BOARDS.get(slug, _BOARDS[BOARD_SLUG])

_GATE_STATUSES = ("awaiting_approval",)
_BLOCK_KINDS = ("needs_input", "review-required")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_seconds(ts: Optional[Any], now_ts: float) -> Optional[int]:
    """Age in seconds of a unix-epoch timestamp (or None when absent/invalid)."""
    if ts is None:
        return None
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return max(0, int(now_ts - v))


def _read_json_file(path: Path) -> Optional[Any]:
    """Read a JSON file; None when missing or malformed (never raises)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None


def _parse_iso_ts(iso: Optional[Any]) -> Optional[float]:
    """Epoch seconds from an ISO-8601 timestamp (tz-aware or naive), else None.

    Handles the ledger's `2026-08-10T02:59:38.410160+00:00` style and the
    trailing-Z shorthand; naive timestamps are treated as UTC.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# R8 per-judge TTL fallback map — mirrors
# engine/services/judge_ranking.py DEFAULT_CONFIG.judge_ttl_seconds for
# ledgers written before the engine persisted staleness_threshold_s/silent.
# Judges absent here use 3600s (2h*2 < 72h, so the 72h horizon still wins).
_JUDGE_TTL_FALLBACK = {
    "trader_agent_v1": 5400,
    "llm_signal": 1200,
}


def _judge_silence_alerts() -> list:
    """R8: judges whose last output age exceeds max(stale_hours, TTL*2).

    Reads the engine's judge-ranking ledger. Prefers the engine-persisted
    ``silent``/``staleness_threshold_s`` fields; falls back to computing
    silence here (age > max(stale_hours*3600, TTL*2)) for ledgers written
    before the engine restart. Returns [] when the ledger is missing — the
    overview must never break because the advisory ledger is absent.
    """
    ledger = _read_json_file(JUDGE_LEDGER_PATH)
    if not isinstance(ledger, dict) or not isinstance(ledger.get("judges"), dict):
        return []
    cfg = ledger.get("config_snapshot") or {}
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        stale_hours = float(cfg.get("stale_hours", 72))
    except (TypeError, ValueError):
        stale_hours = 72.0
    now_ts = datetime.now(timezone.utc).timestamp()
    alerts = []
    for name, j in (ledger.get("judges") or {}).items():
        if not isinstance(j, dict):
            continue
        last_ts = _parse_iso_ts(j.get("last_signal_ts"))
        age_s = max(0, int(now_ts - last_ts)) if last_ts is not None else None
        silent = bool(j.get("silent"))
        thr_s = j.get("staleness_threshold_s")
        try:
            thr_s = int(thr_s) if thr_s is not None else None
        except (TypeError, ValueError):
            thr_s = None
        if thr_s is None:
            thr_s = int(max(stale_hours * 3600.0, 2 * _JUDGE_TTL_FALLBACK.get(name, 3600)))
        if not silent and age_s is not None:
            silent = age_s > thr_s
        if not silent:
            continue
        alerts.append({
            "name": j.get("name") or name,
            "age_s": age_s,
            "threshold_s": thr_s,
            "never_output": last_ts is None,
            "last_signal_ts": j.get("last_signal_ts"),
            "badge": "stale",
            "badge_label": "🔴",
        })
    alerts.sort(key=lambda a: (a["age_s"] is None, -(a["age_s"] or 0)))
    return alerts


# ---------------------------------------------------------------------------
# Profile -> LLM resolution (which model a worker profile runs on)
# ---------------------------------------------------------------------------

_PROFILE_MODELS_CACHE: Dict[str, Dict[str, str]] = {}
_PROFILE_MODELS_TS = 0.0


def _load_profile_models() -> Dict[str, Dict[str, str]]:
    """Map profile name -> {model, provider, local} by reading profile config.yaml.

    Effective model rules (mirror the dispatcher's semantics):
      - a card's model_override/provider_override wins when set (handled by caller)
      - otherwise the assignee profile's model.default + model.provider
      - a provider of 'custom' (or a fallback_providers entry with base_url on
        127.0.0.1 / localhost) is flagged local=true so the UI can show 🧠 vs 💻
    Cached 30s; profiles change rarely and the page polls every 30s anyway.
    """
    global _PROFILE_MODELS_CACHE, _PROFILE_MODELS_TS
    now = datetime.now(timezone.utc).timestamp()
    if _PROFILE_MODELS_CACHE and now - _PROFILE_MODELS_TS < 30:
        return _PROFILE_MODELS_CACHE
    out: Dict[str, Dict[str, str]] = {}
    profiles_dir = HERMES_HOME / "profiles"
    if profiles_dir.is_dir():
        for pdir in sorted(profiles_dir.iterdir()):
            cfg = pdir / "config.yaml"
            if not cfg.is_file():
                continue
            try:
                text = cfg.read_text(encoding="utf-8")
            except OSError:
                continue
            model = provider = ""
            local = False
            # naive YAML-lite parse: model: / default: / provider: under model:
            in_model = False
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("model:"):
                    in_model = True
                    continue
                if in_model:
                    if stripped and not stripped.startswith(("#", "-")):
                        if stripped.startswith("provider:"):
                            provider = stripped.split(":", 1)[1].strip().strip("'\"")
                            if provider == "custom":
                                local = True
                        elif stripped.startswith("default:"):
                            model = stripped.split(":", 1)[1].strip().strip("'\"")
                        elif stripped.startswith("fallback") or stripped.startswith("auxiliary"):
                            break
                    if ":" not in stripped and stripped:
                        break
            # fallback_providers exists but is NOT the effective model — the badge
            # must show what the worker is configured to run on, not the fallback.
            # (A fallback only becomes effective when the primary provider is down,
            # which this read-only view cannot know; local is only true when the
            # effective provider is 'custom' i.e. the local llama-server.)
            if model:
                out[pdir.name] = {
                    "model": model,
                    "provider": provider or "?",
                    "local": "local" if local else "",
                }
    _PROFILE_MODELS_CACHE = out
    _PROFILE_MODELS_TS = now
    return out


def _card_llm(card: Dict[str, Any], profile_models: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """Resolve which LLM a card runs on: override > profile default.

    Returns {model, provider, local} ready for display.
    """
    ov_model = (card.get("model_override") or "").strip()
    ov_provider = (card.get("provider_override") or "").strip()
    if ov_model and ov_model != "none":
        return {
            "model": ov_model,
            "provider": ov_provider or "?",
            "local": "local" if ov_provider == "custom" else "",
        }
    pm = profile_models.get(card.get("assignee") or "", {})
    return {
        "model": pm.get("model", ""),
        "provider": pm.get("provider", ""),
        "local": pm.get("local", ""),
    }


# ---------------------------------------------------------------------------
# Actual model usage per worker session (from each profile's state.db)
#
# Hermes records real LLM usage in <profile>/state.db -> session_model_usage:
#   session_id | model | billing_provider | billing_base_url | api_call_count |
#   input_tokens | output_tokens | estimated_cost_usd | first_seen | last_seen
# The kanban task_runs.metadata carries worker_session_id (stamped at
# completion), so a finished card can be joined to the exact session that ran
# it. For a RUNNING card the session id is not stamped yet, so we match the
# profile's most recently-active session (last_seen within the last 5 min) —
# that is the live worker. This also surfaces real fallbacks: if the primary
# provider was down and the worker fell back to the local llama-server, the
# actual model/provider/base_url differ from the configured card LLM.
# ---------------------------------------------------------------------------

_SESSION_USAGE_CACHE: Dict[str, Dict[str, Dict[str, Any]]] = {}
_SESSION_USAGE_TS: Dict[str, float] = {}


def _load_session_usage(profile: str) -> Dict[str, Dict[str, Any]]:
    """Map session_id -> aggregated actual usage for one worker profile.

    Aggregates all rows per session (main + approval + compression tasks);
    the dominant model row (by api_call_count) provides model/provider/base_url.
    Cached 15s per profile — short enough to track a live worker's tokens.
    """
    global _SESSION_USAGE_CACHE, _SESSION_USAGE_TS
    now = datetime.now(timezone.utc).timestamp()
    if profile in _SESSION_USAGE_CACHE and now - _SESSION_USAGE_TS.get(profile, 0.0) < 15:
        return _SESSION_USAGE_CACHE[profile]
    out: Dict[str, Dict[str, Any]] = {}
    state_db = HERMES_HOME / "profiles" / profile / "state.db"
    if not state_db.is_file():
        state_db = HERMES_HOME / "state.db"  # default profile
    if not state_db.is_file():
        _SESSION_USAGE_CACHE[profile] = out
        _SESSION_USAGE_TS[profile] = now
        return out
    try:
        con = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT session_id, model, billing_provider, billing_base_url, "
            "api_call_count, input_tokens, output_tokens, estimated_cost_usd, "
            "first_seen, last_seen FROM session_model_usage"
        ).fetchall()
        con.close()
    except sqlite3.Error:
        _SESSION_USAGE_CACHE[profile] = out
        _SESSION_USAGE_TS[profile] = now
        return out
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        sid = r["session_id"] or ""
        if not sid:
            continue
        a = agg.setdefault(
            sid,
            {
                "model": "",
                "provider": "",
                "base_url": "",
                "api_call_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": 0.0,
                "first_seen": None,
                "last_seen": None,
                "_start_ts": _session_id_start_ts(sid),
            },
        )
        a["api_call_count"] += r["api_call_count"] or 0
        a["input_tokens"] += r["input_tokens"] or 0
        a["output_tokens"] += r["output_tokens"] or 0
        a["cost"] += r["estimated_cost_usd"] or 0.0
        if r["first_seen"] and (a["first_seen"] is None or r["first_seen"] < a["first_seen"]):
            a["first_seen"] = r["first_seen"]
        if r["last_seen"] and (a["last_seen"] is None or r["last_seen"] > a["last_seen"]):
            a["last_seen"] = r["last_seen"]
        # dominant model row: most api calls in this session
        if r["api_call_count"] and r["api_call_count"] >= a.get("_dom_calls", 0):
            a["_dom_calls"] = r["api_call_count"]
            a["model"] = r["model"]
            a["provider"] = r["billing_provider"] or ""
            a["base_url"] = r["billing_base_url"] or ""
    for a in agg.values():
        a.pop("_dom_calls", None)
        a["local"] = (
            "local"
            if (a["provider"] == "custom" or "127.0.0.1" in (a["base_url"] or "") or "localhost" in (a["base_url"] or ""))
            else ""
        )
    out = agg
    _SESSION_USAGE_CACHE[profile] = out
    _SESSION_USAGE_TS[profile] = now
    return out


def _session_id_start_ts(session_id: str) -> Optional[float]:
    """Local-time start of a Hermes session id (`YYYYMMDD_HHMMSS_xxxx`).

    Hermes session ids embed the session start time (local). A kanban worker
    session starts ~1-2s after the dispatcher claims the run, so this lets us
    disambiguate which of several concurrent same-profile sessions belongs to
    a given running card. Returns epoch seconds or None if unparseable.
    """
    try:
        m = re.match(r"^(\d{8})_(\d{6})_", session_id or "")
        if not m:
            return None
        dt = datetime.strptime(m.group(1) + "_" + m.group(2), "%Y%m%d_%H%M%S")
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _active_worker_usage(profile: str, now_ts: float, max_idle_s: float = 300.0,
                         run_started_at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Actual usage of the live session for a running worker profile.

    With several concurrent workers on the same profile, pick the session whose
    id-encoded start time is closest to the run's started_at (they match within
    ~1-2s). Falls back to the most recently-active session when no session id
    timestamp matches the run start (or none given) — a running worker
    heartbeats LLM calls every few seconds; 5 min covers dispatch +
    prompt-build gaps. None if idle.
    """
    usage = _load_session_usage(profile)
    if not usage:
        return None
    if run_started_at is not None:
        # candidate sessions: active within max_idle_s AND start-timestamped
        # within a plausible window of the run start (dispatch can lag a few
        # seconds; never more than a few minutes).
        cands = []
        for u in usage.values():
            if not u.get("last_seen"):
                continue
            if now_ts - u["last_seen"] > max_idle_s:
                continue
            st = u.get("_start_ts")
            if st is None:
                continue
            delta = abs(st - run_started_at)
            if delta <= 300.0:  # 5 min: covers slow dispatch + session boot
                cands.append((delta, u))
        if cands:
            cands.sort(key=lambda t: t[0])
            return cands[0][1]
    best: Optional[Dict[str, Any]] = None
    for u in usage.values():
        if not u.get("last_seen"):
            continue
        if now_ts - u["last_seen"] > max_idle_s:
            continue
        if best is None or u["last_seen"] > best["last_seen"]:
            best = u
    return best


def _attach_actual_llm(card: Dict[str, Any], profile_models: Dict[str, Dict[str, str]]) -> None:
    """Merge llm.actual into a card dict (running or completed).

    - Running card: active session usage for the run profile.
    - Completed card: exact worker_session_id from run metadata (caller sets
      card["_worker_session_id"]).
    Falls back to configured llm when no usage row exists yet. Also sets
    llm.fell_back = true when actual model != configured model.
    """
    cfg = card.get("llm") or {}
    actual: Optional[Dict[str, Any]] = None
    run = card.get("run") or {}
    profile = run.get("profile") or card.get("assignee") or ""
    now_ts = datetime.now(timezone.utc).timestamp()
    sid = card.get("_worker_session_id") or ""
    if sid:
        actual = (_load_session_usage(profile) or {}).get(sid)
    if actual is None and profile:
        actual = _active_worker_usage(profile, now_ts,
                                      run_started_at=run.get("started_at"))
    if actual:
        cfg = dict(cfg)
        cfg["actual"] = {
            "model": actual.get("model", ""),
            "provider": actual.get("provider", ""),
            "local": actual.get("local", ""),
            "api_call_count": actual.get("api_call_count", 0),
            "input_tokens": actual.get("input_tokens", 0),
            "output_tokens": actual.get("output_tokens", 0),
            "cost": actual.get("cost", 0.0),
        }
        if actual.get("model") and cfg.get("model") and actual["model"] != cfg["model"]:
            cfg["fell_back"] = True
    card["llm"] = cfg


def _read_proposal_md(path: Path) -> Optional[Dict[str, Any]]:
    """Parse a vault item markdown file's frontmatter + body."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm: Dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            raw_fm = text[3:end]
            for line in raw_fm.splitlines():
                if ":" not in line:
                    continue
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip().strip("'\"")
            body = text[end + 4:].strip()
    slug = fm.get("slug") or path.stem
    return {
        "slug": slug,
        "title": fm.get("title") or slug,
        "status": fm.get("status", ""),
        "path": fm.get("path", ""),
        "score": fm.get("score", ""),
        "first_seen": fm.get("first_seen", ""),
        "body": body,
        "mtime": path.stat().st_mtime,
        "file": path.name,
    }


def _iter_proposals(board: str = BOARD_SLUG) -> List[Dict[str, Any]]:
    vault = _board_paths(board)["vault"]
    if not vault.is_dir():
        return []
    out = []
    for p in sorted(vault.glob("*.md")):
        item = _read_proposal_md(p)
        if item:
            out.append(item)
    return out


def _kanban_query(sql: str, params: tuple = (), board: str = BOARD_SLUG) -> List[Dict[str, Any]]:
    """Read-only kanban board query."""
    db = _board_paths(board)["db"]
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _card_last_comment(card_id: str, board: str = BOARD_SLUG) -> str:
    rows = _kanban_query(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='comment' "
        "ORDER BY id DESC LIMIT 1",
        (card_id,),
        board,
    )
    if not rows:
        return ""
    try:
        p = json.loads(rows[0]["payload"])
        return (p.get("body") or "")[:300]
    except (json.JSONDecodeError, AttributeError):
        return str(rows[0]["payload"])[:300]


def _card_comments(card_id: str, limit: int = 15, board: str = BOARD_SLUG) -> List[Dict[str, str]]:
    rows = _kanban_query(
        "SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='comment' "
        "ORDER BY id DESC LIMIT ?",
        (card_id, limit),
        board,
    )
    out = []
    for r in rows:
        try:
            p = json.loads(r["payload"])
            body = p.get("body") or ""
        except (json.JSONDecodeError, AttributeError):
            body = str(r["payload"])
        author = p.get("author") if isinstance(p, dict) else None
        out.append({"author": author or "?", "body": body,
                    "created_at": r.get("created_at") or ""})
    return out


def _card_block_reason(card_id: str, board: str = BOARD_SLUG) -> str:
    rows = _kanban_query(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
        "ORDER BY id DESC LIMIT 1",
        (card_id,),
        board,
    )
    if not rows:
        return ""
    try:
        p = json.loads(rows[0]["payload"])
        return (p.get("reason") or "")[:2000]
    except (json.JSONDecodeError, AttributeError):
        return str(rows[0]["payload"])[:2000]


def _card_completed_event(card_id: str, board: str = BOARD_SLUG) -> Dict[str, Any]:
    """Latest 'completed' task_event payload (summary / result / metadata)."""
    rows = _kanban_query(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='completed' "
        "ORDER BY id DESC LIMIT 1",
        (card_id,),
        board,
    )
    if not rows:
        return {}
    try:
        p = json.loads(rows[0]["payload"])
        return p if isinstance(p, dict) else {}
    except (json.JSONDecodeError, AttributeError):
        return {}


def _card_latest_run(card_id: str, board: str = BOARD_SLUG) -> Dict[str, Any]:
    """Latest task_runs row for a card (profile, outcome, summary, metadata)."""
    rows = _kanban_query(
        "SELECT profile, outcome, summary, metadata, error, ended_at "
        "FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (card_id,),
        board,
    )
    if not rows:
        return {}
    out = dict(rows[0])
    try:
        out["metadata"] = json.loads(out["metadata"]) if out.get("metadata") else {}
    except (json.JSONDecodeError, TypeError):
        out["metadata"] = {}
    return out


def _card_attachments(card_id: str, board: str = BOARD_SLUG) -> List[Dict[str, Any]]:
    rows = _kanban_query(
        "SELECT id, filename, stored_path, content_type, size, uploaded_by, "
        "created_at FROM task_attachments WHERE task_id=? ORDER BY id DESC LIMIT 10",
        (card_id,),
        board,
    )
    _EXT_CT = {
        ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
        ".json": "application/json", ".csv": "text/csv", ".html": "text/html",
        ".htm": "text/html", ".pdf": "application/pdf", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml", ".py": "text/x-python",
        ".yaml": "text/yaml", ".yml": "text/yaml", ".log": "text/plain",
    }
    for r in rows:
        if not r.get("content_type"):
            r["content_type"] = _EXT_CT.get(
                Path(r["filename"] or "").suffix.lower(), "application/octet-stream"
            )
        r["url"] = (
            f"/api/review/attachments/{card_id}/"
            + quote(r["filename"])
        )
    return rows


def _run_workflow(verb: str, args: List[str], board: str = BOARD_SLUG) -> Dict[str, Any]:
    cmd = ["python3", str(PROPOSAL_ACTIONS), verb] + args
    env = {**os.environ, "HERMES_KANBAN_BOARD": board}
    try:
        proc = subprocess.run(cmd, cwd=str(WORKFLOW_DIR),
                              capture_output=True, text=True, timeout=60,
                              env=env)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:800],
                "stderr": (proc.stderr or "")[:400]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _run_kanban(args: List[str], board: str = BOARD_SLUG) -> Dict[str, Any]:
    cmd = ["hermes", "kanban", "--board", board] + args
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:800],
                "stderr": (proc.stderr or "")[:400]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def register_review_routes(app) -> None:
    """Register all /api/review/* routes on the monitor Flask app."""

    @app.get("/api/review/overview")
    def review_overview():
        board = _resolve_board()
        paths = _board_paths(board)
        gate = [i for i in _iter_proposals(board) if i["status"] in _GATE_STATUSES]
        gate.sort(key=lambda x: x.get("mtime", 0), reverse=True)

        blocked = _kanban_query(
            "SELECT id, title, assignee, priority, status, block_kind, "
            "last_failure_error, body, created_at FROM tasks WHERE status='blocked' "
            "ORDER BY priority DESC LIMIT 40",
            board=board,
        )
        for c in blocked:
            c["last_comment"] = _card_last_comment(c["id"], board)
            c["block_reason"] = (
                _card_block_reason(c["id"], board)
                or (c.pop("last_failure_error", "") or "")
            )

        ready = _kanban_query(
            "SELECT id, title, assignee, priority, status, created_at, "
            "model_override, provider_override FROM tasks "
            "WHERE status IN ('ready','todo') ORDER BY priority DESC LIMIT 20",
            board=board,
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        profile_models = _load_profile_models()
        for c in ready:
            c["created_age_s"] = _age_seconds(c.get("created_at"), now_ts)
            c["llm"] = _card_llm(c, profile_models)
        running = _kanban_query(
            "SELECT id, title, assignee, priority, status, created_at, started_at, "
            "last_heartbeat_at, worker_pid, current_run_id, max_runtime_seconds, "
            "model_override, provider_override "
            "FROM tasks WHERE status='running' ORDER BY priority DESC LIMIT 10",
            board=board,
        )
        for c in running:
            c["last_comment"] = _card_last_comment(c["id"], board)
            c["started_age_s"] = _age_seconds(c.get("started_at"), now_ts)
            c["beat_age_s"] = _age_seconds(c.get("last_heartbeat_at"), now_ts)
            c["llm"] = _card_llm(c, profile_models)
            run = _kanban_query(
                "SELECT profile, step_key, status, started_at, last_heartbeat_at "
                "FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (c["id"],),
                board,
            )
            c["run"] = run[0] if run else {}
            if c["run"].get("last_heartbeat_at"):
                c["run"]["beat_age_s"] = _age_seconds(
                    c["run"]["last_heartbeat_at"], now_ts
                )
            else:
                c["run"]["beat_age_s"] = None
            _attach_actual_llm(c, profile_models)
        completed = _kanban_query(
            "SELECT id, title, assignee, priority, status, created_at, "
            "completed_at, result FROM tasks WHERE status='done' "
            "ORDER BY completed_at DESC LIMIT 30",
            board=board,
        )
        for c in completed:
            ev = _card_completed_event(c["id"], board)
            run = _card_latest_run(c["id"], board)
            c["summary"] = (
                ev.get("summary")
                or run.get("summary")
                or ""
            )
            c["run_profile"] = run.get("profile") or ""
            c["run_outcome"] = run.get("outcome") or ""
            meta = ev.get("metadata") or run.get("metadata") or {}
            if not isinstance(meta, dict):
                try:
                    meta = json.loads(meta)
                except (json.JSONDecodeError, TypeError):
                    meta = {}
            c["metadata"] = meta
            c["attachments"] = _card_attachments(c["id"], board)
            c["created_at"] = c.get("created_at") or ""
            c["completed_at"] = c.get("completed_at") or ""
            c["_worker_session_id"] = str(meta.get("worker_session_id") or "")
            c["llm"] = _card_llm(c, profile_models)
            _attach_actual_llm(c, profile_models)
        return jsonify({"gate": gate, "blocked": blocked,
                        "ready": ready, "running": running,
                        "completed": completed,
                        # R8: silent judges surface in the overview alert list
                        "judge_alerts": _judge_silence_alerts(),
                        "board": board,
                        "vault_dir": str(paths["vault"]),
                        "generated_at": _now_iso()})

    @app.post("/api/review/gate/approve")
    def review_gate_approve():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        if not slug:
            return jsonify({"ok": False, "error": "missing slug"}), 400
        return jsonify(_run_workflow("approve", [slug], _resolve_board()))

    @app.post("/api/review/gate/shelve")
    def review_gate_shelve():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not slug:
            return jsonify({"ok": False, "error": "missing slug"}), 400
        args = [slug] + (["--reason", reason] if reason else [])
        return jsonify(_run_workflow("shelve", args, _resolve_board()))

    @app.post("/api/review/gate/modify")
    def review_gate_modify():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        change = (data.get("change") or "").strip()
        if not slug or not change:
            return jsonify({"ok": False, "error": "slug and change required"}), 400
        return jsonify(_run_workflow("modify", [slug, "--change", change], _resolve_board()))

    @app.post("/api/review/cards/comment")
    def review_card_comment():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        text = (data.get("text") or "").strip()
        if not card_id or not text:
            return jsonify({"ok": False, "error": "task_id and text required"}), 400
        return jsonify(_run_kanban(["comment", card_id, "--author", "operator", text], _resolve_board()))

    @app.post("/api/review/cards/unblock")
    def review_card_unblock():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        args = [card_id] + (["--reason", reason] if reason else [])
        return jsonify(_run_kanban(["unblock"] + args, _resolve_board()))

    @app.post("/api/review/cards/complete")
    def review_card_complete():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        summary = (data.get("summary") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        args = [card_id] + (["--summary", summary] if summary else [])
        return jsonify(_run_kanban(["complete"] + args, _resolve_board()))

    @app.get("/api/review/cards/detail")
    def review_card_detail():
        card_id = (request.args.get("task_id") or request.args.get("card_id") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        board = _resolve_board()
        rows = _kanban_query(
            "SELECT id, title, assignee, priority, status, block_kind, "
            "last_failure_error, body, created_at, started_at, last_heartbeat_at, "
            "worker_pid, current_run_id, max_runtime_seconds, result, "
            "model_override, provider_override "
            "FROM tasks WHERE id=?",
            (card_id,),
            board,
        )
        if not rows:
            return jsonify({"ok": False, "error": "card not found"}), 404
        c = rows[0]
        c["last_comment"] = _card_last_comment(c["id"], board)
        c["llm"] = _card_llm(c, _load_profile_models())
        c["block_reason"] = (
            _card_block_reason(c["id"], board)
            or (c.pop("last_failure_error", "") or "")
        )
        c["comments"] = _card_comments(card_id, board=board)
        now_ts = datetime.now(timezone.utc).timestamp()
        c["started_age_s"] = _age_seconds(c.get("started_at"), now_ts)
        c["beat_age_s"] = _age_seconds(c.get("last_heartbeat_at"), now_ts)
        run = _kanban_query(
            "SELECT profile, step_key, status, started_at, last_heartbeat_at, "
            "outcome, summary, error, metadata FROM task_runs WHERE task_id=? "
            "ORDER BY id DESC LIMIT 1",
            (card_id,),
            board,
        )
        c["run"] = run[0] if run else {}
        if c["run"].get("last_heartbeat_at"):
            c["run"]["beat_age_s"] = _age_seconds(
                c["run"]["last_heartbeat_at"], now_ts
            )
        else:
            c["run"]["beat_age_s"] = None
        run_meta = c["run"].get("metadata") or {}
        if isinstance(run_meta, str):
            try:
                run_meta = json.loads(run_meta)
            except (json.JSONDecodeError, TypeError):
                run_meta = {}
        c["_worker_session_id"] = str(run_meta.get("worker_session_id") or "")
        _attach_actual_llm(c, _load_profile_models())
        return jsonify({"ok": True, "card": c})

    @app.get("/api/review/attachments/<task_id>/<path:filename>")
    def review_attachment(task_id: str, filename: str):
        """Serve a stored task attachment (images inline, others as download)."""
        if not filename:
            return jsonify({"ok": False, "error": "filename required"}), 400
        board = _resolve_board()
        rows = _kanban_query(
            "SELECT stored_path, content_type, size, filename "
            "FROM task_attachments WHERE task_id=? AND filename=?",
            (task_id, filename),
            board,
        )
        if not rows:
            return jsonify({"ok": False, "error": "attachment not found"}), 404
        stored = Path(rows[0]["stored_path"])
        if not stored.is_file():
            return jsonify({"ok": False, "error": "attachment missing on disk"}), 404
        ct = rows[0]["content_type"] or ""
        if not ct:
            ct = {
                ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
                ".json": "application/json", ".csv": "text/csv", ".html": "text/html",
                ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
                ".svg": "image/svg+xml", ".py": "text/x-python", ".log": "text/plain",
            }.get(Path(filename).suffix.lower(), "application/octet-stream")
        inline = ct.startswith("image/") or ct.startswith("text/")
        return send_file(
            str(stored),
            mimetype=ct,
            as_attachment=not inline,
            download_name=rows[0]["filename"],
            max_age=0,
        )

    @app.get("/api/review/judges")
    def review_judges():
        """Judge-ranking ledger (R3): read-only per-judge scores + planner accuracy.

        Returns the engine's judge_ranking.json ledger enriched with live
        last-output age (now - last signal timestamp) and staleness, plus the
        session-planner accuracy per pair (plan_accuracy.json). Same
        MONITOR_API_KEY auth as every other /api route.
        """
        ledger = _read_json_file(JUDGE_LEDGER_PATH)
        if not isinstance(ledger, dict) or not isinstance(ledger.get("judges"), dict):
            return jsonify({
                "ok": False,
                "error": "judge ledger not found",
                "ledger_path": str(JUDGE_LEDGER_PATH),
            }), 404
        cfg = ledger.get("config_snapshot") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        try:
            stale_hours = float(cfg.get("stale_hours", 72))
        except (TypeError, ValueError):
            stale_hours = 72.0
        now_ts = datetime.now(timezone.utc).timestamp()
        judges = []
        for name, j in (ledger.get("judges") or {}).items():
            if not isinstance(j, dict):
                continue
            last_ts = _parse_iso_ts(j.get("last_signal_ts"))
            age_s = max(0.0, now_ts - last_ts) if last_ts is not None else None
            stale = bool(j.get("stale"))
            if not stale and age_s is not None:
                stale = age_s > stale_hours * 3600.0
            try:
                score = float(j.get("judge_score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            band = str(j.get("band") or "advisory")
            if stale:
                badge, badge_label = "stale", "🔴"
            elif score >= 0.6:
                badge, badge_label = "healthy", "🟢"
            elif band == "advisory":
                badge, badge_label = "low-n", "🟡"
            else:
                badge, badge_label = "gated", "⚪"
            judges.append({
                "name": j.get("name") or name,
                "key": name,
                "band": band,
                "binding": bool(j.get("binding")),
                "judge_score": score,
                "stale": stale,
                "badge": badge,
                "badge_label": badge_label,
                "last_output_age_s": age_s,
                "last_signal_ts": j.get("last_signal_ts"),
                "updated": j.get("updated"),
                "stats": j.get("stats") or {},
                "wr": j.get("wr"),
                "pf": j.get("pf"),
                "credible_wr": j.get("credible_wr"),
                "resolution_rate": j.get("resolution_rate"),
                "noop_rate": j.get("noop_rate"),
                "stale_rate": j.get("stale_rate"),
            })
        judges.sort(key=lambda x: -x["judge_score"])
        plan_acc = _read_json_file(PLAN_ACCURACY_PATH)
        return jsonify({
            "ok": True,
            "judges": judges,
            "config": cfg,
            "planner_accuracy": plan_acc if isinstance(plan_acc, dict) else {},
            "ledger_generated_at": ledger.get("generated_at") or "",
            "generated_at": _now_iso(),
        })
