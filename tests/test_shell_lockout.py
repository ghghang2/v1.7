"""Regression tests for the shell lockout (see issues.md, top entry).

A command that never terminates must not be able to hang the agent:
``run_command`` enforces a wall-clock timeout and reports it as a
structured failure, and every SQLite connection is given a bounded
``busy_timeout`` so the database can never block a tool thread forever.
"""
import json
import time

from nbchat.core import db
import nbchat.tools.run_command as rc


def _run(cmd: str, timeout_env: str | None = None) -> str:
    import os
    prev = os.environ.get("NBCAT_TOOL_TIMEOUT")
    if timeout_env is not None:
        os.environ["NBCAT_TOOL_TIMEOUT"] = timeout_env
    try:
        return rc._run_command(cmd)
    finally:
        if prev is None:
            os.environ.pop("NBCAT_TOOL_TIMEOUT", None)
        else:
            os.environ["NBCAT_TOOL_TIMEOUT"] = prev


def test_hanging_command_times_out_and_reports():
    """`sleep 30` with a 1 s budget must return fast, with a structured
    failure - it must NOT hang the calling thread (the failure mode that
    previously wedged the agent process)."""
    t0 = time.monotonic()
    result = _run("sleep 30", timeout_env="1")
    elapsed = time.monotonic() - t0
    assert elapsed < 8.0, "run_command hung: wall-clock timeout did not fire (%.1fs)" % elapsed
    assert result.lstrip().startswith("{"), "expected a structured JSON result, got: %r" % result[:120]
    parsed = json.loads(result)
    blob = json.dumps(parsed).lower()
    assert "timeout" in blob or "timed out" in blob, "timeout not reported: %s" % parsed


def test_fast_command_still_succeeds():
    result = _run("echo alive")
    assert "alive" in result


def test_kill_group_timeout_is_short():
    """The child group is killed within a bounded window after the timeout."""
    t0 = time.monotonic()
    _run("sleep 30", timeout_env="1")
    assert time.monotonic() - t0 < 8.0


def test_db_connections_have_bounded_busy_timeout():
    """Every fresh connection must reject SQLite lock waits after a bounded
    delay instead of blocking forever (the second half of the lockout)."""
    conn = db._connect()
    try:
        val = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
    finally:
        conn.close()
    assert 0 < val <= 5000, "busy_timeout should be bounded, got %r" % val


def test_db_wal_mode_after_init():
    """init_db should leave the database in WAL mode so readers never
    block the writer."""
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    if mode != "wal":
        db.init_db()
        conn = sqlite3.connect(db.DB_PATH)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
    assert mode == "wal", "journal_mode=%r, expected wal" % mode
