from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import os as _os
DB_PATH: Path = Path(_os.environ.get("DB_PATH") or Path(__file__).parent / "escrow.db")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.row_factory = sqlite3.Row
    return _conn


# Backward-compatible alias expected by the initial design document.
def get_db() -> sqlite3.Connection:
    return get_conn()


def ensure_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS calls (
                call_id       TEXT PRIMARY KEY,
                agent_id      TEXT NOT NULL,
                tool_name     TEXT NOT NULL,
                tool_input    TEXT NOT NULL,
                status        TEXT NOT NULL DEFAULT 'pending',
                is_mutating   INTEGER NOT NULL DEFAULT 0,
                policy_reason TEXT,
                created_at    TEXT NOT NULL,
                resolved_at   TEXT,
                resolved_by   TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_calls_status ON calls(status);
            CREATE INDEX IF NOT EXISTS idx_calls_agent  ON calls(agent_id);

            CREATE TABLE IF NOT EXISTS pending_actions (
                id               TEXT PRIMARY KEY,
                rpc_id           TEXT NOT NULL,
                tool_name        TEXT NOT NULL,
                category         TEXT NOT NULL,
                risk_level       TEXT NOT NULL,
                reason           TEXT,
                raw_arguments    TEXT NOT NULL,
                status           TEXT NOT NULL,
                rejection_reason TEXT,
                submitted_by     TEXT,
                resolved_by      TEXT,
                created_at       TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pending_actions_status ON pending_actions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_actions_created_at ON pending_actions(created_at);

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type  TEXT NOT NULL,
                action_id   TEXT,
                actor       TEXT,
                ip_address  TEXT,
                detail      TEXT,
                created_at  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_action_id  ON audit_log(action_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
        """)
        conn.commit()
        for migration in (
            "ALTER TABLE pending_actions ADD COLUMN resolved_by TEXT",
            "ALTER TABLE pending_actions ADD COLUMN submitted_by TEXT",
        ):
            try:
                conn.execute(migration)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists


def init_db() -> None:
    global _conn
    _conn = None  # force reconnect so DB_PATH changes take effect in tests
    ensure_db()


def insert_call(
    *,
    call_id: str,
    agent_id: str,
    tool_name: str,
    tool_input: Dict[str, Any],
    is_mutating: bool,
    policy_reason: Optional[str],
) -> None:
    conn = get_conn()
    status = "pending" if is_mutating else "allowed"
    with _lock:
        conn.execute(
            """INSERT INTO calls
               (call_id, agent_id, tool_name, tool_input, status,
                is_mutating, policy_reason, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                call_id,
                agent_id,
                tool_name,
                json.dumps(tool_input),
                status,
                1 if is_mutating else 0,
                policy_reason,
                _utcnow(),
            ),
        )
        conn.commit()


def insert_action(
    *,
    action_id: str,
    rpc_id: str,
    tool_name: str,
    category: str,
    risk_level: str,
    reason: str,
    args: Dict[str, Any],
    submitted_by: Optional[str] = None,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO pending_actions (
                id, rpc_id, tool_name, category, risk_level, reason,
                raw_arguments, status, submitted_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                action_id,
                str(rpc_id),
                tool_name,
                category,
                risk_level,
                reason,
                json.dumps(args),
                submitted_by,
                _utcnow(),
            ),
        )
        conn.commit()


def update_action_status(
    action_id: str,
    status: str,
    rejection_reason: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> bool:
    """Update status only if currently PENDING. Returns True if updated."""
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            """
            UPDATE pending_actions
            SET status = ?, rejection_reason = ?, resolved_by = ?, resolved_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (status, rejection_reason, resolved_by, _utcnow(), action_id),
        )
        conn.commit()
    return cur.rowcount > 0


def count_pending() -> int:
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) FROM pending_actions WHERE status='PENDING'"
    ).fetchone()
    return row[0] if row else 0


def reject_stale_pending(reason: str) -> int:
    """Mark all PENDING rows as REJECTED. Called on startup when in-memory waiters are gone."""
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            "UPDATE pending_actions SET status='REJECTED', rejection_reason=?, resolved_at=? WHERE status='PENDING'",
            (reason, _utcnow()),
        )
        conn.commit()
    return cur.rowcount


def get_pending_actions() -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM pending_actions WHERE status = 'PENDING' ORDER BY created_at ASC"
    ).fetchall()
    items: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["raw_arguments"] = json.loads(item["raw_arguments"])
        except (TypeError, json.JSONDecodeError):
            item["raw_arguments"] = {}
        items.append(item)
    return items


def get_resolved_history(limit: int = 20) -> List[Dict[str, Any]]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM pending_actions WHERE status != 'PENDING' ORDER BY resolved_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_call(call_id: str) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM calls WHERE call_id = ?", (call_id,)
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["tool_input"] = json.loads(d["tool_input"])
    return d


def update_call_status(
    call_id: str,
    status: str,
    resolved_by: Optional[str] = None,
) -> bool:
    """Update status only if currently 'pending'. Returns True if updated."""
    conn = get_conn()
    with _lock:
        cur = conn.execute(
            """UPDATE calls
               SET status = ?, resolved_at = ?, resolved_by = ?
               WHERE call_id = ? AND status = 'pending'""",
            (status, _utcnow(), resolved_by, call_id),
        )
        conn.commit()
    return cur.rowcount > 0


def list_calls(status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    if status:
        rows = conn.execute(
            "SELECT * FROM calls WHERE status = ? ORDER BY created_at DESC",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM calls ORDER BY created_at DESC"
        ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        d["tool_input"] = json.loads(d["tool_input"])
        result.append(d)
    return result


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def insert_audit_log(
    event_type: str,
    *,
    action_id: Optional[str] = None,
    actor: Optional[str] = None,
    ip_address: Optional[str] = None,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    conn = get_conn()
    with _lock:
        conn.execute(
            """INSERT INTO audit_log
               (event_type, action_id, actor, ip_address, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_type,
                action_id,
                actor,
                ip_address,
                json.dumps(detail) if detail is not None else None,
                _utcnow(),
            ),
        )
        conn.commit()


def get_audit_log(
    limit: int = 50,
    action_id: Optional[str] = None,
    event_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = get_conn()
    clauses: List[str] = []
    params: List[Any] = []
    if action_id:
        clauses.append("action_id = ?")
        params.append(action_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("detail"):
            try:
                d["detail"] = json.loads(d["detail"])
            except (TypeError, json.JSONDecodeError):
                pass
        result.append(d)
    return result
