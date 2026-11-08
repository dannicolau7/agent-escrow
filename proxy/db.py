from __future__ import annotations

import hashlib
import hmac
import json
import os as _os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DB_PATH: Path = Path(_os.environ.get("DB_PATH") or Path(__file__).parent / "escrow.db")

# One shared connection; every statement (reads included) goes through _lock
# because sync routes run in FastAPI's threadpool while async routes run on
# the event loop thread.
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

_MAX_RESULT_CHARS = 20_000


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


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


def _query(sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
    conn = get_conn()
    with _lock:
        return conn.execute(sql, params).fetchall()


def ensure_db() -> None:
    conn = get_conn()
    with _lock:
        conn.executescript("""
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
                resolved_at      TEXT,
                execution_status TEXT,
                execution_result TEXT,
                executed_at      TEXT,
                hold_mode        TEXT,
                rpc_payload      TEXT,
                session_id       TEXT
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
                created_at  TEXT NOT NULL,
                prev_hash   TEXT,
                hash        TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_audit_log_action_id  ON audit_log(action_id);
            CREATE INDEX IF NOT EXISTS idx_audit_log_event_type ON audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
        """)
        conn.commit()
        for migration in (
            "ALTER TABLE pending_actions ADD COLUMN resolved_by TEXT",
            "ALTER TABLE pending_actions ADD COLUMN submitted_by TEXT",
            "ALTER TABLE pending_actions ADD COLUMN execution_status TEXT",
            "ALTER TABLE pending_actions ADD COLUMN execution_result TEXT",
            "ALTER TABLE pending_actions ADD COLUMN executed_at TEXT",
            "ALTER TABLE pending_actions ADD COLUMN hold_mode TEXT",
            "ALTER TABLE pending_actions ADD COLUMN rpc_payload TEXT",
            "ALTER TABLE pending_actions ADD COLUMN session_id TEXT",
            "ALTER TABLE audit_log ADD COLUMN prev_hash TEXT",
            "ALTER TABLE audit_log ADD COLUMN hash TEXT",
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


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def insert_action(
    *,
    action_id: str,
    rpc_id: str,
    tool_name: str,
    category: str,
    risk_level: str,
    reason: str,
    args: dict[str, Any],
    submitted_by: str | None = None,
    hold_mode: str = "wait",
    rpc_payload: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> None:
    """Store a held action.  rpc_payload and session_id are kept so an
    async-mode action can be sent upstream later, after approval."""
    conn = get_conn()
    with _lock:
        conn.execute(
            """
            INSERT INTO pending_actions (
                id, rpc_id, tool_name, category, risk_level, reason,
                raw_arguments, status, submitted_by, created_at,
                hold_mode, rpc_payload, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?)
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
                hold_mode,
                json.dumps(rpc_payload) if rpc_payload is not None else None,
                session_id,
            ),
        )
        conn.commit()


def list_expired_async(older_than_seconds: float) -> list[dict[str, Any]]:
    """PENDING async-mode actions created more than `older_than_seconds` ago."""
    rows = _query(
        "SELECT id, tool_name FROM pending_actions WHERE status = 'PENDING' AND hold_mode = 'async' "
        "AND (julianday('now') - julianday(created_at)) * 86400.0 > ?",
        (older_than_seconds,),
    )
    return [dict(r) for r in rows]


def update_action_status(
    action_id: str,
    status: str,
    rejection_reason: str | None = None,
    resolved_by: str | None = None,
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


def record_execution(action_id: str, status: str, result: Any) -> None:
    """Store what happened when an approved action was sent upstream.

    status is SUCCEEDED or FAILED.  The upstream response is stored as JSON,
    truncated to keep the table small.
    """
    text = json.dumps(result, default=str)
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "…[truncated]"
    conn = get_conn()
    with _lock:
        conn.execute(
            "UPDATE pending_actions SET execution_status=?, execution_result=?, executed_at=? WHERE id=?",
            (status, text, _utcnow(), action_id),
        )
        conn.commit()


def get_action(action_id: str) -> dict[str, Any] | None:
    rows = _query("SELECT * FROM pending_actions WHERE id = ?", (action_id,))
    return dict(rows[0]) if rows else None


def count_pending() -> int:
    rows = _query("SELECT COUNT(*) FROM pending_actions WHERE status='PENDING'")
    return rows[0][0] if rows else 0


def reject_stale_pending(reason: str) -> list[str]:
    """Mark all PENDING rows as REJECTED. Called on startup when in-memory
    waiters are gone.  Returns the ids that were rejected.

    NOTE: this assumes a single proxy process owns the database.  Running
    several instances against one database is not supported yet.
    """
    conn = get_conn()
    with _lock:
        ids = [r[0] for r in conn.execute("SELECT id FROM pending_actions WHERE status='PENDING'")]
        conn.execute(
            "UPDATE pending_actions SET status='REJECTED', rejection_reason=?, resolved_by='system', resolved_at=? WHERE status='PENDING'",
            (reason, _utcnow()),
        )
        conn.commit()
    return ids


# Internal columns never returned by the reviewer API.
_PRIVATE_COLUMNS = ("rpc_payload", "session_id")


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for col in _PRIVATE_COLUMNS:
        item.pop(col, None)
    try:
        item["raw_arguments"] = json.loads(item["raw_arguments"])
    except (TypeError, json.JSONDecodeError):
        item["raw_arguments"] = {}
    return item


def get_pending_actions() -> list[dict[str, Any]]:
    rows = _query("SELECT * FROM pending_actions WHERE status = 'PENDING' ORDER BY created_at ASC")
    return [_decode_row(r) for r in rows]


def get_resolved_history(limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    rows = _query(
        "SELECT * FROM pending_actions WHERE status != 'PENDING' ORDER BY resolved_at DESC LIMIT ?",
        (limit,),
    )
    return [_decode_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Audit log — append-only, hash-chained
# ---------------------------------------------------------------------------
# Each row stores prev_hash (the previous row's hash) and
#   hash = HMAC-SHA256(key, prev_hash + canonical JSON of the row)
# with key = ESCROW_AUDIT_KEY (plain SHA-256 if unset).  Editing or deleting
# any row breaks every hash after it, which verify_audit_chain() detects.
# With ESCROW_AUDIT_KEY set, someone with write access to the database but not
# the key cannot rebuild a valid chain.

_AUDIT_FIELDS = ("event_type", "action_id", "actor", "ip_address", "detail", "created_at")


def _audit_digest(prev_hash: str, fields: dict[str, Any]) -> str:
    canonical = json.dumps({k: fields.get(k) for k in _AUDIT_FIELDS}, sort_keys=True, separators=(",", ":"))
    msg = (prev_hash + canonical).encode()
    key = _os.environ.get("ESCROW_AUDIT_KEY", "")
    if key:
        return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()
    return hashlib.sha256(msg).hexdigest()


def insert_audit_log(
    event_type: str,
    *,
    action_id: str | None = None,
    actor: str | None = None,
    ip_address: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn = get_conn()
    fields = {
        "event_type": event_type,
        "action_id": action_id,
        "actor": actor,
        "ip_address": ip_address,
        "detail": json.dumps(detail, sort_keys=True, default=str) if detail is not None else None,
        "created_at": _utcnow(),
    }
    with _lock:
        last = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = last[0] if last and last[0] else ""
        digest = _audit_digest(prev_hash, fields)
        conn.execute(
            """INSERT INTO audit_log
               (event_type, action_id, actor, ip_address, detail, created_at, prev_hash, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (*(fields[k] for k in _AUDIT_FIELDS), prev_hash, digest),
        )
        conn.commit()


def verify_audit_chain() -> dict[str, Any]:
    """Recompute the chain.  Returns {"ok", "checked", "first_bad_id"}."""
    rows = _query("SELECT * FROM audit_log ORDER BY id ASC")
    prev = ""
    checked = 0
    for row in rows:
        r = dict(row)
        if r.get("hash") is None:  # rows written before hashing existed
            continue
        if r.get("prev_hash") != prev or _audit_digest(prev, r) != r["hash"]:
            return {"ok": False, "checked": checked, "first_bad_id": r["id"]}
        prev = r["hash"]
        checked += 1
    return {"ok": True, "checked": checked, "first_bad_id": None}


def get_audit_log(
    limit: int = 50,
    action_id: str | None = None,
    event_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if action_id:
        clauses.append("action_id = ?")
        params.append(action_id)
    if event_type:
        clauses.append("event_type = ?")
        params.append(event_type)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 100_000)))
    # `where` only joins the fixed clauses above; every value is a bound parameter.
    rows = _query(f"SELECT * FROM audit_log {where} ORDER BY id DESC LIMIT ?", params)  # noqa: S608
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
