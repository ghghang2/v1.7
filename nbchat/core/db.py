"""SQLite persistence layer for chat history, memory, and episodic store.

Tables: chat_log, session_meta, episodic_store, core_memory,
context_events, task_log.
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


# Tools whose output is a JSON object carrying a machine-readable outcome.
# For these, error_flag must be derived from the structured outcome, not from
# keyword scanning of the text (a successful `grep` that merely prints the word
# "error" was previously mislabelled as a failure, driving needless reruns).
_STRUCTURED_TOOLS = frozenset({
    "run_command", "run_tests", "push_to_github", "browser",
    "get_weather", "create_file", "make_change_to_file", "send_email",
})


def _structured_error(parsed, tool_name: str) -> bool:
    """Return True if a parsed structured tool payload indicates failure."""
    if isinstance(parsed, dict):
        # Explicit error key: {"error": "..."}
        if "error" in parsed:
            return True
        res = parsed.get("result")
        if isinstance(res, dict):
            status = str(res.get("status", "")).lower()
            if status and status not in ("success", "ok", "dry_run"):
                return True
            return bool(res.get("error"))
        if isinstance(res, str):
            return res.strip().lower().startswith("error")
        # push_to_github reports its outcome at the top level.
        if tool_name == "push_to_github":
            status = str(parsed.get("status", "")).lower()
            return bool(status) and status not in ("success", "ok", "dry_run")
        return False
    return False


def is_tool_error(tool_name: str, content: str) -> bool:
    """Derive the error flag for a *tool* result from its structured outcome.

    ``error_flag`` is a telemetry signal (monitoring, supervisor stats, history
    rendering) — it never changes the string handed back to the model, so this
    only ever makes the books more honest.

    Structured tools are judged by their payload (exit code / failed count /
    status).  A non-zero ``exit_code`` on ``run_command`` is a failure even when
    the text looks fine; a green ``run_tests`` run is a success even when its
    summary happens to print the word "error".

    Tools without a recognisable structured payload fall back to the keyword
    heuristic so genuinely unstructured errors are still flagged.
    """
    if not tool_name or tool_name not in _STRUCTURED_TOOLS:
        # Non-structured tools: if the payload is a JSON object with a
        # machine-readable outcome, trust it (a successful call that merely
        # prints the word "error" in its text was previously mislabelled as
        # a failure). Keyword match stays as the non-JSON fallback.
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return _structured_error(parsed, tool_name)
        except Exception:
            pass
        return is_error_content(content)
    try:
        parsed = json.loads(content)
    except Exception:
        # Not JSON (e.g. "Tool 'x' failed after N retries: ..." or an empty
        # string).  Preserve the old behaviour for those.
        return is_error_content(content)
    if tool_name == "run_command":
        if isinstance(parsed, dict) and "exit_code" in parsed:
            try:
                return int(parsed["exit_code"]) != 0
            except (TypeError, ValueError):
                return True
    if tool_name == "run_tests" and isinstance(parsed, dict) and (
        "failed" in parsed or "errors" in parsed
    ):
        return int(parsed.get("failed", 0) or 0) != 0 or \
            int(parsed.get("errors", 0) or 0) != 0
    if tool_name == "browser":
        if isinstance(parsed, dict):
            status = str(parsed.get("status", "")).lower()
            if status == "error" or "exception" in str(parsed.get("error", "")):
                return True
            if "error" in parsed and parsed["error"]:
                return True
            return False
    # Parsed JSON payload: judge by the structured outcome keys only,
    # never by keyword-scan of the text.  Non-dict JSON (list/scalar)
    # carries no error marker.
    if isinstance(parsed, dict):
        return _structured_error(parsed, tool_name)
    return False


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    """Open a DB connection with a busy_timeout.

    A contended database then raises a catchable ``OperationalError``
    after ~2 s instead of blocking the calling thread indefinitely
    (a wedge that froze the agent — see issues.md).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=2000")
    return conn


def init_db() -> None:
    with _connect() as conn:
        # Persistent across restarts; also converts a legacy
        # rollback-journal database on first use.  Readers never block
        # the writer, so a slow read cannot wedge the conversation loop.
        conn.execute("PRAGMA journal_mode=WAL")
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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS task_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL,
                request_text  TEXT DEFAULT '',
                request_chars INTEGER DEFAULT 0,
                status        TEXT DEFAULT 'in_progress',
                completion    TEXT DEFAULT 'unknown',
                nature        TEXT DEFAULT 'unknown',
                difficulty    TEXT DEFAULT 'unknown',
                started_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                ended_at      TIMESTAMP,
                duration_s    REAL,
                num_llm_calls INTEGER DEFAULT 0,
                num_tool_turns INTEGER DEFAULT 0,
                tool_calls_total INTEGER DEFAULT 0,
                tool_calls_by_name TEXT DEFAULT '{}',
                tool_calls_failed INTEGER DEFAULT 0,
                redundant_tool_calls INTEGER DEFAULT 0,
                redundant_reads INTEGER DEFAULT 0,
                redundant_writes INTEGER DEFAULT 0,
                stall_events INTEGER DEFAULT 0,
                truncation_events INTEGER DEFAULT 0,
                stream_retries INTEGER DEFAULT 0,
                text_toolcall_recovery INTEGER DEFAULT 0,
                user_interventions INTEGER DEFAULT 0,
                llm_latency_s REAL DEFAULT 0,
                prompt_chars INTEGER DEFAULT 0,
                completion_chars INTEGER DEFAULT 0,
                max_context_chars INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                final_response_chars INTEGER DEFAULT 0,
                turn_ids      TEXT DEFAULT '[]',
                annotations   TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_task_session ON task_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_task_status ON task_log(status);
        """)
        _sweep_orphan_tasks(conn)
        conn.commit()


# Tasks older than this that never reached a terminal state (the process
# died mid-turn) are swept to 'in_progress' at startup.  Generous enough
# that a long background turn in progress when the app restarts is not
# mislabelled; the next finish_task() overrides it via turn id anyway.
_ORPHAN_TASK_MAX_AGE_MINUTES = 15


def _sweep_orphan_tasks(conn) -> None:
    conn.execute(
        "UPDATE task_log SET status='in_progress', completion='unknown' "
        "WHERE status NOT IN ('complete','interrupted','failed') "
        "AND started_at < datetime('now', ?)",
        (f"-{_ORPHAN_TASK_MAX_AGE_MINUTES} minutes",),
    )


# ---------------------------------------------------------------------------
# session_meta
# ---------------------------------------------------------------------------

def _meta_set(session_id: str, key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO session_meta (session_id, key, value, ts) VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (session_id, key, value),
        )
        conn.commit()


def _meta_get(session_id: str, key: str) -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM session_meta WHERE session_id=? AND key=?", (session_id, key)
        ).fetchone()
    return row[0] if row and row[0] else ""


def normalize_session_id(session_id: str) -> str:
    """Return the canonical ``chat_log`` id for a user-supplied session id.

    The TUI shows and accepts the *bare* id (e.g. ``8ac30abd8aec``)
    while the stored rows carry the namespace prefix (``tui:``,
    ``wa:``).  A bare id resolves to its prefixed twin when one
    exists; when both a bare and a prefixed row set exist, the one
    with more history wins (that is the real session).  Unresolvable
    ids pass through unchanged so callers can report them as unknown.
    """
    if not session_id:
        return session_id
    with _connect() as conn:
        counts = {sid: n for sid, n in conn.execute(
            "SELECT session_id, COUNT(*) FROM chat_log GROUP BY session_id")}
    # Prefixed candidates first so a tie prefers the full history.
    cands = [f"{p}{session_id}" for p in ("tui:", "wa:")] + [session_id]
    known = [c for c in cands if c in counts]
    if not known:
        return session_id
    return max(known, key=lambda s: counts[s])


# ---------------------------------------------------------------------------
# Chat log
# ---------------------------------------------------------------------------

def log_message(session_id: str, role: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, error_flag) VALUES (?,?,?,?)",
            (session_id, role, content, int(is_error_content(content))),
        )
        conn.commit()


def log_row(session_id: str, role: str, content: str,
            tool_id: str = "", tool_name: str = "", tool_args: str = "") -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, tool_id, tool_name, tool_args, error_flag) "
            "VALUES (?,?,?,?,?,?,?)",
            (session_id, role, content or "", tool_id or "", tool_name or "", tool_args or "",
             int(is_error_content(content))),
        )
        conn.commit()


def log_tool_msg(session_id: str, tool_id: str, tool_name: str,
                 tool_args: str, content: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_log (session_id, role, content, tool_id, tool_name, tool_args, error_flag) "
            "VALUES (?,'tool',?,?,?,?,?)",
            (session_id, content, tool_id, tool_name, tool_args, int(is_tool_error(tool_name, content))),
        )
        conn.commit()


def backfill_tool_rows() -> int:
    """Recompute ``error_flag`` on existing ``role='tool'`` rows using
    :func:`is_tool_error`.

    Safe to run repeatedly: it only writes when the derived flag differs from
    the stored one.  Returns the number of rows whose flag was corrected.
    """
    fixed = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tool_name, COALESCE(content,'') FROM chat_log WHERE role='tool'"
        ).fetchall()
        for rid, tool_name, content in rows:
            new_flag = int(is_tool_error(tool_name or "", content or ""))
            row = conn.execute(
                "SELECT error_flag FROM chat_log WHERE id=?", (rid,)
            ).fetchone()
            if row is not None and int(row[0]) != new_flag:
                conn.execute("UPDATE chat_log SET error_flag=? WHERE id=?",
                             (new_flag, rid))
                fixed += 1
        conn.commit()
    return fixed


def backfill_assistant_full() -> int:
    """Populate ``content`` on ``assistant_full`` rows that were written with an
    empty content column (the full payload was duplicated into ``tool_args``).

    The readable assistant text is recovered from the ``content`` field of the
    JSON stored in ``tool_args``.  Safe to run repeatedly: rows that already
    have non-empty content are skipped.  Returns the number of rows updated.
    """
    fixed = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, tool_args FROM chat_log "
            "WHERE role='assistant_full' AND (content IS NULL OR content='')"
        ).fetchall()
        for rid, tool_args in rows:
            text = ""
            if tool_args:
                try:
                    msg = json.loads(tool_args)
                    text = msg.get("content") or ""
                except Exception:
                    text = ""
            if text:
                conn.execute(
                    "UPDATE chat_log SET content=? WHERE id=?", (text, rid)
                )
                fixed += 1
        conn.commit()
    return fixed

def load_history(session_id: str, limit: int | None = None) -> list[tuple]:
    with _connect() as conn:
        q = ("SELECT role, content, COALESCE(tool_id,''), COALESCE(tool_name,''), "
             "COALESCE(tool_args,''), error_flag FROM chat_log WHERE session_id=? ORDER BY id ASC")
        params: list = [session_id]
        if limit is not None:
            q += " LIMIT ?"
            params.append(limit)
        return conn.execute(q, params).fetchall()


def get_history(session_id: str) -> list[tuple]:
    """Return ``(session_id, role, content)`` rows for *session_id* in
    insertion order.

    A slimmer view than :func:`load_history` for callers that only need the
    transcript (the team coordinator persists its final report here, and the
    TUI renders it).
    """
    with _connect() as conn:
        return conn.execute(
            "SELECT session_id, role, content FROM chat_log "
            "WHERE session_id=? ORDER BY id ASC",
            (session_id,),
        ).fetchall()


def get_session_ids() -> list[str]:
    with _connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT DISTINCT session_id FROM chat_log ORDER BY ts DESC"
        ).fetchall()]


def replace_session_history(session_id: str, history: list[tuple]) -> None:
    with _connect() as conn:
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
    with _connect() as conn:
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
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT id, turn_id, action_type, entity_refs, outcome_summary, importance_score "
            f"FROM episodic_store WHERE ({clauses}) AND session_id=? "
            f"ORDER BY importance_score DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def query_episodic_top_importance(session_id: str, min_score: float = 3.0, limit: int = 5) -> list[dict]:
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, turn_id, action_type, entity_refs, outcome_summary, importance_score "
            "FROM episodic_store WHERE session_id=? AND importance_score>=? "
            "ORDER BY importance_score DESC LIMIT ?",
            (session_id, min_score, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_episodic_for_session(session_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM episodic_store WHERE session_id=?", (session_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# L1 Core memory
# ---------------------------------------------------------------------------

def get_core_memory(session_id: str) -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT key, value FROM core_memory WHERE session_id=?", (session_id,)
        ).fetchall()
    return {k: v for k, v in rows if v}


def update_core_memory(session_id: str, updates: dict) -> None:
    if not updates:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO core_memory (session_id, key, value, ts) VALUES (?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(session_id, key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            [(session_id, k, str(v)) for k, v in updates.items()],
        )
        conn.commit()


def clear_core_memory(session_id: str) -> None:
    with _connect() as conn:
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
    with _connect() as conn:
        conn.execute(
            "INSERT INTO context_events (session_id, event_type, payload) VALUES (?,?,?)",
            (session_id, event_type, json.dumps(payload)),
        )
        # Retention cap: keep the last 5000 events per session (debugging
        # telemetry only - bounded growth, no consumer reads further back).
        conn.execute(
            "DELETE FROM context_events WHERE session_id=? AND id NOT IN "
            "(SELECT id FROM context_events WHERE session_id=? ORDER BY id DESC LIMIT 5000)",
            (session_id, session_id),
        )
        conn.commit()


def query_context_events(session_id: str, event_type: str | None = None, limit: int = 100) -> list[dict]:
    with _connect() as conn:
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


# ---------------------------------------------------------------------------
# Task completion statistics (task_log)
# ---------------------------------------------------------------------------

_TASK_FIELDS = (
    "session_id", "request_text", "request_chars", "status", "completion",
    "nature", "difficulty", "started_at", "ended_at", "duration_s",
    "num_llm_calls", "num_tool_turns", "tool_calls_total",
    "tool_calls_by_name", "tool_calls_failed", "redundant_tool_calls",
    "redundant_reads", "redundant_writes", "stall_events",
    "truncation_events", "stream_retries", "text_toolcall_recovery",
    "user_interventions", "llm_latency_s", "prompt_chars",
    "completion_chars", "max_context_chars", "error_count",
    "final_response_chars", "turn_ids", "annotations",
)


def record_task(task_id: int | None, **fields: Any) -> int:
    """Insert (task_id=None) or update (by id) one task_log row.

    Only the fields supplied are written (others keep their defaults /
    previous values), so the two-phase lifecycle — insert at loop start,
    finalise at loop end — can each pass a partial field set.  Returns the
    row id.
    """
    keys = [k for k in fields if k in _TASK_FIELDS]
    if not keys:
        raise ValueError("record_task: no known fields supplied")
    if task_id is None:
        ph = ", ".join("?" for _ in keys)
        with _connect() as conn:
            cur = conn.execute(
                f"INSERT INTO task_log ({', '.join(keys)}) VALUES ({ph})",
                [fields[k] for k in keys],
            )
        return int(cur.lastrowid)
    sets = ", ".join(f"{k}=?" for k in keys)
    with _connect() as conn:
        conn.execute(f"UPDATE task_log SET {sets} WHERE id=?",
                     [fields[k] for k in keys] + [task_id])
    return int(task_id)


def query_tasks(session_id: str | None = None, status: str | None = None,
                limit: int = 100) -> list[dict]:
    """Return task_log rows (newest first) as dicts, optionally filtered."""
    sql = "SELECT * FROM task_log"
    where, params = [], []
    if session_id:
        where.append("session_id=?")
        params.append(session_id)
    if status:
        where.append("status=?")
        params.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def task_summary_rows(session_id: str | None = None,
                      limit: int = 500) -> list[dict]:
    """The subset of columns needed to build a stats summary view."""
    sql = (
        "SELECT session_id, status, completion, nature, difficulty, "
        "started_at, ended_at, duration_s, num_llm_calls, num_tool_turns, "
        "tool_calls_total, tool_calls_failed, redundant_tool_calls, "
        "redundant_reads, redundant_writes, stall_events, "
        "truncation_events, stream_retries, user_interventions, "
        "final_response_chars FROM task_log"
    )
    params: list = []
    if session_id:
        sql += " WHERE session_id=?"
        params.append(session_id)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    conn = _connect()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]