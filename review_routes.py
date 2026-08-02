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
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import jsonify, request


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

HERMES_HOME = Path.home() / ".hermes"
KANBAN_DB = HERMES_HOME / "kanban" / "boards" / "pain-point" / "kanban.db"
WORKFLOW_DIR = Path.home() / "hermes-multi-agent-workflow"
VAULT_DIR = WORKFLOW_DIR / "work" / "vault" / "items"
PROPOSAL_ACTIONS = WORKFLOW_DIR / "proposal_actions.py"

BOARD_SLUG = "pain-point"

_GATE_STATUSES = ("awaiting_approval",)
_BLOCK_KINDS = ("needs_input", "review-required")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _iter_proposals() -> List[Dict[str, Any]]:
    if not VAULT_DIR.is_dir():
        return []
    out = []
    for p in sorted(VAULT_DIR.glob("*.md")):
        item = _read_proposal_md(p)
        if item:
            out.append(item)
    return out


def _kanban_query(sql: str, params: tuple = ()) -> List[Dict[str, Any]]:
    """Read-only kanban board query."""
    if not KANBAN_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql, params).fetchall()]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def _card_last_comment(card_id: str) -> str:
    rows = _kanban_query(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='comment' "
        "ORDER BY id DESC LIMIT 1",
        (card_id,),
    )
    if not rows:
        return ""
    try:
        p = json.loads(rows[0]["payload"])
        return (p.get("body") or "")[:300]
    except (json.JSONDecodeError, AttributeError):
        return str(rows[0]["payload"])[:300]


def _card_comments(card_id: str, limit: int = 15) -> List[Dict[str, str]]:
    rows = _kanban_query(
        "SELECT payload, created_at FROM task_events WHERE task_id=? AND kind='comment' "
        "ORDER BY id DESC LIMIT ?",
        (card_id, limit),
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


def _card_block_reason(card_id: str) -> str:
    rows = _kanban_query(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' "
        "ORDER BY id DESC LIMIT 1",
        (card_id,),
    )
    if not rows:
        return ""
    try:
        p = json.loads(rows[0]["payload"])
        return (p.get("reason") or "")[:2000]
    except (json.JSONDecodeError, AttributeError):
        return str(rows[0]["payload"])[:2000]


def _run_workflow(verb: str, args: List[str]) -> Dict[str, Any]:
    cmd = ["python3", str(PROPOSAL_ACTIONS), verb] + args
    try:
        proc = subprocess.run(cmd, cwd=str(WORKFLOW_DIR),
                              capture_output=True, text=True, timeout=60)
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[:800],
                "stderr": (proc.stderr or "")[:400]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def _run_kanban(args: List[str]) -> Dict[str, Any]:
    cmd = ["hermes", "kanban", "--board", BOARD_SLUG] + args
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
        gate = [i for i in _iter_proposals() if i["status"] in _GATE_STATUSES]
        gate.sort(key=lambda x: x.get("mtime", 0), reverse=True)

        blocked = _kanban_query(
            "SELECT id, title, assignee, priority, status, block_kind, "
            "last_failure_error, body, created_at FROM tasks WHERE status='blocked' "
            "ORDER BY priority DESC LIMIT 40",
        )
        for c in blocked:
            c["last_comment"] = _card_last_comment(c["id"])
            c["block_reason"] = (
                _card_block_reason(c["id"])
                or (c.pop("last_failure_error", "") or "")
            )

        ready = _kanban_query(
            "SELECT id, title, assignee, priority, status FROM tasks "
            "WHERE status IN ('ready','todo') ORDER BY priority DESC LIMIT 20",
        )
        running = _kanban_query(
            "SELECT id, title, assignee, priority, status FROM tasks "
            "WHERE status='running' ORDER BY priority DESC LIMIT 10",
        )
        return jsonify({"gate": gate, "blocked": blocked,
                        "ready": ready, "running": running,
                        "generated_at": _now_iso()})

    @app.post("/api/review/gate/approve")
    def review_gate_approve():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        if not slug:
            return jsonify({"ok": False, "error": "missing slug"}), 400
        return jsonify(_run_workflow("approve", [slug]))

    @app.post("/api/review/gate/shelve")
    def review_gate_shelve():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not slug:
            return jsonify({"ok": False, "error": "missing slug"}), 400
        args = [slug] + (["--reason", reason] if reason else [])
        return jsonify(_run_workflow("shelve", args))

    @app.post("/api/review/gate/modify")
    def review_gate_modify():
        data = request.get_json(silent=True) or {}
        slug = (data.get("slug") or "").strip()
        change = (data.get("change") or "").strip()
        if not slug or not change:
            return jsonify({"ok": False, "error": "slug and change required"}), 400
        return jsonify(_run_workflow("modify", [slug, "--change", change]))

    @app.post("/api/review/cards/comment")
    def review_card_comment():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        text = (data.get("text") or "").strip()
        if not card_id or not text:
            return jsonify({"ok": False, "error": "task_id and text required"}), 400
        return jsonify(_run_kanban(["comment", card_id, "--author", "operator", text]))

    @app.post("/api/review/cards/unblock")
    def review_card_unblock():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        reason = (data.get("reason") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        args = [card_id] + (["--reason", reason] if reason else [])
        return jsonify(_run_kanban(["unblock"] + args))

    @app.post("/api/review/cards/complete")
    def review_card_complete():
        data = request.get_json(silent=True) or {}
        card_id = (data.get("task_id") or data.get("card_id") or "").strip()
        summary = (data.get("summary") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        args = [card_id] + (["--summary", summary] if summary else [])
        return jsonify(_run_kanban(["complete"] + args))

    @app.get("/api/review/cards/detail")
    def review_card_detail():
        card_id = (request.args.get("task_id") or request.args.get("card_id") or "").strip()
        if not card_id:
            return jsonify({"ok": False, "error": "task_id required"}), 400
        rows = _kanban_query(
            "SELECT id, title, assignee, priority, status, block_kind, "
            "last_failure_error, body, created_at FROM tasks WHERE id=?",
            (card_id,),
        )
        if not rows:
            return jsonify({"ok": False, "error": "card not found"}), 404
        c = rows[0]
        c["last_comment"] = _card_last_comment(c["id"])
        c["block_reason"] = (
            _card_block_reason(c["id"])
            or (c.pop("last_failure_error", "") or "")
        )
        c["comments"] = _card_comments(card_id)
        return jsonify({"ok": True, "card": c})
