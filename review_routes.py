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
            "SELECT id, title, assignee, priority, status, created_at FROM tasks "
            "WHERE status IN ('ready','todo') ORDER BY priority DESC LIMIT 20",
            board=board,
        )
        now_ts = datetime.now(timezone.utc).timestamp()
        for c in ready:
            c["created_age_s"] = _age_seconds(c.get("created_at"), now_ts)
        running = _kanban_query(
            "SELECT id, title, assignee, priority, status, created_at, started_at, "
            "last_heartbeat_at, worker_pid, current_run_id, max_runtime_seconds "
            "FROM tasks WHERE status='running' ORDER BY priority DESC LIMIT 10",
            board=board,
        )
        for c in running:
            c["last_comment"] = _card_last_comment(c["id"], board)
            c["started_age_s"] = _age_seconds(c.get("started_at"), now_ts)
            c["beat_age_s"] = _age_seconds(c.get("last_heartbeat_at"), now_ts)
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
        return jsonify({"gate": gate, "blocked": blocked,
                        "ready": ready, "running": running,
                        "completed": completed,
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
            "worker_pid, current_run_id, max_runtime_seconds, result "
            "FROM tasks WHERE id=?",
            (card_id,),
            board,
        )
        if not rows:
            return jsonify({"ok": False, "error": "card not found"}), 404
        c = rows[0]
        c["last_comment"] = _card_last_comment(c["id"], board)
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
            "outcome, summary, error FROM task_runs WHERE task_id=? "
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
