"""Tests for the Phase 0 scorecard (nbchat.core.scorecard).

Covers: LLM-log parsing, task_log aggregation (preferred redundancy
source), chat_log tool metrics, per-session redundancy, and the text
report.  All DB access goes through an explicit ``db_path`` fixture on a
throwaway SQLite file with the real schema.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

import nbchat.core.scorecard as sc


# ── Fixture: a throwaway database with the real schema --------------------

@pytest.fixture
def scorecard_db(tmp_path):
    db_file = tmp_path / "scorecard_test.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript("""
            CREATE TABLE chat_log (
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
            CREATE TABLE task_log (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id        TEXT,
                status            TEXT DEFAULT '',
                completion        TEXT,
                started_at        TEXT,
                ended_at          TEXT,
                duration_s        REAL,
                num_llm_calls     INTEGER DEFAULT 0,
                num_tool_turns    INTEGER DEFAULT 0,
                tool_calls_total  INTEGER DEFAULT 0,
                tool_calls_by_name TEXT DEFAULT '{}',
                tool_calls_failed INTEGER DEFAULT 0,
                redundant_tool_calls INTEGER DEFAULT 0,
                stall_events      INTEGER DEFAULT 0,
                truncation_events INTEGER DEFAULT 0,
                llm_latency_s     REAL DEFAULT 0,
                prompt_chars      INTEGER DEFAULT 0,
                completion_chars  INTEGER DEFAULT 0,
                max_context_chars INTEGER DEFAULT 0,
                error_count       INTEGER DEFAULT 0,
                final_response_chars INTEGER DEFAULT 0
            );
        """)
        _seed_chat_log(conn)
        _seed_task_log(conn)
        conn.commit()
    finally:
        conn.close()
    return db_file


def _seed_chat_log(conn: sqlite3.Connection) -> None:
    rows = [
        # session A: 6 tool rows, 1 error, 2 redundant reads of the same file.
        ("sessA", "tool", None, "read_file", json.dumps({"path": "a.py"}), 0),
        ("sessA", "tool", None, "read_file", json.dumps({"path": "a.py"}), 0),
        # Whitespace differs but canonicalizes identically -> redundant.
        ("sessA", "tool", None, "read_file",
         json.dumps({"path": "a.py"}, separators=(", ", ": ")), 0),
        ("sessA", "tool", "boom", "run_command", json.dumps({"command": "make build"}), 1),
        ("sessA", "tool", None, "make_change_to_file", json.dumps({"path": "x.py"}), 0),
        ("sessA", "tool", None, "run_tests", json.dumps({}), 0),
        # session B: one unique call.
        ("sessB", "tool", None, "browser", json.dumps({"url": "https://x"}), 0),
        # Non-tool rows must be ignored by tool metrics.
        ("sessA", "user", "hi", None, None, 0),
        ("sessA", "assistant", "there", None, None, 0),
    ]
    conn.executemany(
        "INSERT INTO chat_log (session_id, role, content, tool_name,"
        " tool_args, error_flag) VALUES (?,?,?,?,?,?)",
        rows,
    )


def _seed_task_log(conn: sqlite3.Connection) -> None:
    cols = [
        "session_id", "status", "completion", "started_at", "ended_at",
        "duration_s", "num_llm_calls", "num_tool_turns", "tool_calls_total",
        "tool_calls_by_name", "tool_calls_failed", "redundant_tool_calls",
        "stall_events", "truncation_events", "llm_latency_s",
        "prompt_chars", "completion_chars", "max_context_chars",
        "error_count", "final_response_chars",
    ]
    a = dict(session_id="sessA", status="complete", completion="complete",
             started_at="2026-09-10 10:00:00", ended_at="2026-09-10 10:05:00",
             duration_s=300.0, num_llm_calls=20, num_tool_turns=10,
             tool_calls_total=6, tool_calls_by_name='{"read_file": 3}',
             tool_calls_failed=1, redundant_tool_calls=2,
             stall_events=1, truncation_events=0, llm_latency_s=60.0,
             prompt_chars=100000, completion_chars=9000,
             max_context_chars=12000, error_count=1, final_response_chars=800)
    b = dict(a)
    b.update(session_id="sessB", status="in_progress", completion="partial",
             duration_s=None, num_llm_calls=5, num_tool_turns=2,
             tool_calls_total=1, tool_calls_by_name='{"browser": 1}',
             tool_calls_failed=0, redundant_tool_calls=0,
             stall_events=0, truncation_events=1, llm_latency_s=10.0,
             prompt_chars=10000, completion_chars=900,
             max_context_chars=8000, error_count=0, final_response_chars=0)
    for row in (a, b):
        cols_sql = ", ".join(cols)
        vals_sql = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO task_log ({cols_sql}) VALUES ({vals_sql})",
            tuple(row[c] for c in cols),
        )


# ── LLM log parsing --------------------------------------------------------

def test_parse_llm_metrics_log(tmp_path):
    log = tmp_path / "m.log"
    log.write_text(
        "2026-09-10 15:17:03,212 [INFO] Inference_Metrics: "
        "Latency: 0.82s | P:2345 C:46 T:2391 | stop=stop\n"
        "2026-09-10 15:17:05,461 [INFO] Inference_Metrics: "
        "Latency: 1.39s | P:439 C:217 T:656\n"
        "2026-09-10 15:17:05,900 [INFO] unrelated line\n",
        encoding="utf-8",
    )
    agg = sc.parse_llm_metrics_log(log)
    assert agg["llm_calls"] == 2
    assert agg["prompt_tokens"] == 2784
    assert agg["completion_tokens"] == 263
    assert agg["total_tokens"] == 3047
    assert agg["avg_latency_s"] == pytest.approx(1.105, abs=1e-3)
    assert agg["max_latency_s"] == 1.39
    assert agg["stop_reasons"] == {"stop": 1}


def test_parse_llm_metrics_log_missing_file(tmp_path):
    agg = sc.parse_llm_metrics_log(tmp_path / "nope.log")
    assert agg["llm_calls"] == 0
    assert agg["total_tokens"] == 0
    assert agg["stop_reasons"] == {}


def test_parse_llm_metrics_log_garbage(tmp_path):
    log = tmp_path / "m.log"
    log.write_text("garbage\nLatency: xx | P:bad C:x\n", encoding="utf-8")
    agg = sc.parse_llm_metrics_log(log)
    assert agg["llm_calls"] == 0


# ── Aggregation -----------------------------------------------------------

def test_aggregate_tool_metrics(scorecard_db):
    card = sc.aggregate(db_path=scorecard_db, llm_log=scorecard_db)
    # 7 tool rows (6 in sessA + 1 in sessB); user/assistant rows excluded.
    assert card["tool_calls"] == 7
    assert card["tool_errors"] == 1
    assert card["tool_error_rate"] == pytest.approx(1 / 7, abs=1e-3)
    # by_tool ordering: read_file 3 > run_command 1 > make_change_to_file 1.
    assert card["by_tool"][0]["tool"] == "read_file"
    assert card["by_tool"][0]["n"] == 3
    run_cmd = next(t for t in card["by_tool"] if t["tool"] == "run_command")
    assert run_cmd["errors"] == 1
    assert card["sessions_n"] == 2


def test_aggregate_task_metrics(scorecard_db):
    card = sc.aggregate(db_path=scorecard_db, llm_log=scorecard_db)
    assert card["tasks"] == 2
    assert card["tasks_done"] == 1          # only sessA is 'complete'
    assert card["avg_task_duration_s"] == pytest.approx(300.0)
    assert card["task_prompt_chars"] == 110000
    assert card["task_completion_chars"] == 9900
    assert card["stall_events"] == 1
    assert card["truncation_events"] == 1
    assert card["max_context_chars"] == 12000
    assert card["error_count"] == 1
    assert card["avg_tool_calls_per_task"] == pytest.approx(3.5)
    # Task-level redundancy is the preferred source.
    assert card["redundant_calls"] == 2
    assert card["redundancy_source"] == "task_log"


def test_redundancy_falls_back_to_chat_log(tmp_path):
    """Without task_log data, redundancy is estimated from chat_log."""
    db_file = tmp_path / "fallback.db"
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute("""
            CREATE TABLE chat_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, role TEXT NOT NULL,
                content TEXT, error_flag INTEGER DEFAULT 0,
                tool_id TEXT, tool_name TEXT, tool_args TEXT,
                ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        rows = [
            ("s1", "tool", None, "read_file", json.dumps({"path": "a.py"})),
            ("s1", "tool", None, "read_file", json.dumps({"path": "a.py"})),
            # Different JSON whitespace, same value -> canonicalizes identically.
            ("s1", "tool", None, "read_file",
             json.dumps({"path": "a.py"}, separators=(", ", ": "))),
            ("s1", "tool", None, "read_file", json.dumps({"path": "c.py"})),
            # Different session, same call -> NOT redundant.
            ("s2", "tool", None, "read_file", json.dumps({"path": "a.py"})),
        ]
        conn.executemany(
            "INSERT INTO chat_log (session_id, role, content, tool_name,"
            " tool_args) VALUES (?,?,?,?,?)", rows)
        conn.commit()
    finally:
        conn.close()
    card = sc.aggregate(db_path=db_file, llm_log=db_file)
    assert card["tool_calls"] == 5
    assert card["redundancy_source"] == "chat_log"
    assert card["redundant_calls"] == 2
    assert card["redundant_rate"] == pytest.approx(2 / 5, abs=1e-3)


def test_redundant_by_session(scorecard_db):
    import nbchat.core.db as db
    import nbchat.core.scorecard as sc2
    # Point the module-level _connect at our fixture DB.
    orig = sc2.DB_PATH
    sc2.DB_PATH = scorecard_db
    try:
        rows = sc2.redundant_by_session()
    finally:
        sc2.DB_PATH = orig
    assert len(rows) == 2
    worst = rows[0]
    assert worst["session_id"] == "sessA"
    assert worst["tool_calls"] == 6
    assert worst["redundant_calls"] == 2
    assert worst["redundant_rate"] == pytest.approx(1 / 3, abs=1e-3)


def test_format_scorecard(scorecard_db):
    card = sc.aggregate(db_path=scorecard_db, llm_log=scorecard_db)
    text = sc.format_scorecard(card)
    assert "nbchat scorecard" in text
    assert "LLM calls:" in text
    assert "Tasks:" in text
    assert "read_file" in text
    assert "sessA" in text
    assert "source=task_log" in text


def test_aggregate_missing_db(tmp_path):
    card = sc.aggregate(db_path=tmp_path / "missing.db",
                        llm_log=tmp_path / "missing.log")
    assert card["llm_calls"] == 0
    assert card["tasks"] == 0
    assert card["tool_calls"] == 0
    assert card["redundancy_source"] == "none"


def test_normalize_args_stable_across_key_order_and_whitespace():
    a = sc._normalize_args('{"b": 2, "a": 1}')
    b = sc._normalize_args('{"a":1,"b":2}')
    c = sc._normalize_args('{"b": 3, "a": 1}')
    d = sc._normalize_args('not json at all')
    e = sc._normalize_args('not  json\t at all')
    assert a == b
    assert a != c
    assert d == e
    assert sc._normalize_args(None) == ""
    assert sc._normalize_args("") == ""
