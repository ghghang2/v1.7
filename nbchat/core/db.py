"""SQLite persistence layer for chat history, memory, and episodic store.

Tables: chat_log, session_meta, episodic_store, core_memory, context_events.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "chat_history.db"

_ERROR_PATTERNS = (
    "error", "exception", "failed", "cannot", "traceback",
    "fatal", "unexpected", "invalid", "permission denied", "not found",
)


def is_error_content(content: str) -> bool:
    low = (content or "").lower()
    return any(p in low for p in _ERROR_PATTERNS)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chat_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                role        TEXT NOT NULL,
                content     TEXT,
                error_flag  INTEGER DEFAULT 0,
                tool_id     TEXT,
                tool_name   TEXT,
                tool_args   TEXT,
                ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_session ON chat_log(session_id);

            CREATE TABLE IF NOT EXISTS session_meta (
                session_id  TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT,
                ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, key)
            );

            CREATE TABLE IF NOT EXISTS episodic_store (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id       TEXT NOT NULL,
                turn_id          INTEGER DEFAULT 0,
                action_type      TEXT DEFAULT '',
                entity_refs      TEXT DEFAULT '[]',
                outcome_summary  TEXT DEFAULT '',
                importance_score REAL DEFAULT 1.0,
                ts               TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ep_session
                ON episodic_store(session_id, importance_score DESC);

            CREATE TABLE IF NOT EXISTS core_memory (
                session_id  TEXT NOT NULL,
                key         TEXT NOT NULL,
                value       TEXT DEFAULT '',
                ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (session_id, key)
            );

            CREATE TABLE IF NOT EXISTS context_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                payload     TEXT DEFAULT '{}',
                ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_ce_session
                ON context_events(session_id, event_type);
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# session_meta
# ---------------------------------------------------------------------------

def _meta_set(session_id: str, key: str, value: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO session_meta (session_id, key, value, ts) VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (session_id, key, value),
        )
        conn.commit()


def _meta_get(session_id: str, key: str) -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM session_meta WHERE session_id=? AND key=?", (session_id, key)
        ).fetchone()
    return row[0] if row and row[0] else ""


# ---------------------------------------------------------------------------
# Chat log
# ---------------------------------------------------------------------------

def log_message(session_id: str, role: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, error_flag) VALUES (?,?,?,?)",
            (session_id, role, content, int(is_error_content(content))),
        )
        conn.commit()


def log_row(session_id: str, role: str, content: str,
            tool_id: str = "", tool_name: str = "", tool_args: str = "") -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, tool_id, tool_name, tool_args, error_flag) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, role, content or "", tool_id or "", tool_name or "", tool_args or "",
             int(is_error_content(content))),
        )
        conn.commit()


def log_tool_msg(session_id: str, tool_id: str, tool_name: str,
                 tool_args: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, tool_id, tool_name, tool_args, error_flag) "
            "VALUES (?,'tool',?,?,?,?,?)",
            (session_id, content, tool_id, tool_name, tool_args, int(is_error_content(content))),
        )
        conn.commit()


def load_history(session_id: str, limit: int | None = None) -> list[tuple]:
    with sqlite3.connect(DB_PATH) as conn:
        q = ("SELECT role, content, COALESCE(tool_id,''), COALESCE(tool_name,''), "
             "COALESCE(tool_args,''), error_flag FROM chat_log WHERE session_id=? ORDER BY id ASC")
        params: list = [session_id]
        if limit is not None:
            q += " LIMIT ?"
            params.append(limit)
        return conn.execute(q, params).fetchall()


def get_session_ids() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat_log ORDER BY ts DESC"
        ).fetchall()]


def replace_session_history(session_id: str, history: list[tuple]) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM chat_log WHERE session_id=?", (session_id,))
        conn.executemany(
            "INSERT INTO chat_log (session_id, role, content, tool_id, tool_name, tool_args, error_flag) "
            "VALUES (?,?,?,?,?,?,?)",
            [(session_id, *row) for row in history],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Typed meta accessors
# ---------------------------------------------------------------------------

def save_context_summary(session_id: str, summary: str) -> None:
    _meta_set(session_id, "context_summary", summary)

def load_context_summary(session_id: str) -> str:
    return _meta_get(session_id, "context_summary")

def save_turn_summaries(session_id: str, cache: dict) -> None:
    _meta_set(session_id, "turn_summaries", json.dumps(cache))

def load_turn_summaries(session_id: str) -> dict:
    raw = _meta_get(session_id, "turn_summaries")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def save_task_log(session_id: str, task_log: list) -> None:
    _meta_set(session_id, "task_log", json.dumps(task_log))

def load_task_log(session_id: str) -> list:
    raw = _meta_get(session_id, "task_log")
    try:
        return json.loads(raw) if raw else []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# L2 Episodic store
# ---------------------------------------------------------------------------

def append_episodic(session_id: str, turn_id: int, action_type: str,
                    entity_refs: str, outcome_summary: str, importance_score: float) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO episodic_store (session_id, turn_id, action_type, entity_refs, "
            "outcome_summary, importance_score) VALUES (?,?,?,?,?,?)",
            (session_id, turn_id, action_type, entity_refs, outcome_summary, importance_score),
        )
        conn.commit()


def query_episodic_by_entities(session_id: str, entity_refs: list[str], limit: int = 5) -> list[dict]:
    if not entity_refs:
        return []
    clauses = " OR ".join("entity_refs LIKE ?" for _ in entity_refs)
    params: list[Any] = [f"%{e}%" for e in entity_refs] + [session_id, limit]
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, turn_id, action_type, entity_refs, outcome_summary, importance_score "
            f"FROM episodic_store WHERE ({clauses}) AND session_id=? "
            f"ORDER BY importance_score DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def query_episodic_top_importance(session_id: str, min_score: float = 3.0, limit: int = 5) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, turn_id, action_type, entity_refs, outcome_summary, importance_score "
            "FROM episodic_store WHERE session_id=? AND importance_score>=? "
            "ORDER BY importance_score DESC LIMIT ?",
            (session_id, min_score, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_episodic_for_session(session_id: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM episodic_store WHERE session_id=?", (session_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# L1 Core memory
# ---------------------------------------------------------------------------

def get_core_memory(session_id: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT key, value FROM core_memory WHERE session_id=?", (session_id,)
        ).fetchall()
    return {k: v for k, v in rows if v}


def update_core_memory(session_id: str, updates: dict) -> None:
    if not updates:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany(
            "INSERT INTO core_memory (session_id, key, value, ts) VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            [(session_id, k, str(v)) for k, v in updates.items()],
        )
        conn.commit()


def clear_core_memory(session_id: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM core_memory WHERE session_id=?", (session_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Monitoring & context events
# ---------------------------------------------------------------------------

_GLOBAL = "__global__"
_GLOBAL_MON_KEY = "monitoring_global_v1"

def save_global_monitoring_stats(stats: dict) -> None:
    _meta_set(_GLOBAL, _GLOBAL_MON_KEY, json.dumps(stats))

def load_global_monitoring_stats() -> dict | None:
    raw = _meta_get(_GLOBAL, _GLOBAL_MON_KEY)
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None


def log_context_event(session_id: str, event_type: str, payload: dict) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO context_events (session_id, event_type, payload) VALUES (?,?,?)",
            (session_id, event_type, json.dumps(payload)),
        )
        conn.commit()


def query_context_events(session_id: str, event_type: str | None = None, limit: int = 100) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if event_type:
            rows = conn.execute(
                "SELECT id, event_type, payload, ts FROM context_events "
                "WHERE session_id=? AND event_type=? ORDER BY id DESC LIMIT ?",
                (session_id, event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, event_type, payload, ts FROM context_events "
                "WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Paste store
# ---------------------------------------------------------------------------

_PASTE_SESSION = "__paste_store__"

def store_paste_content(content: str) -> str:
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    _meta_set(_PASTE_SESSION, content_hash, content)
    return content_hash

def retrieve_paste_content(content_hash: str) -> str | None:
    raw = _meta_get(_PASTE_SESSION, content_hash)
    return raw if raw else None