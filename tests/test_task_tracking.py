"""Tests for task completion statistics (nbchat.core.task_tracker).

Covers:
  * the task_log schema and insert/update lifecycle in nbchat.core.db
  * TaskRecord hook accounting (LLM calls, tool calls, failures, events,
    stream retries, context high-water mark)
  * redundancy detection (same tool+args within a task, read/write split)
  * status -> completion mapping
  * user-intervention counting from chat_log
  * orphan-task sweep (process died mid-turn)
  * /stats plumbing (task_summary_rows + summarize_tasks)

The db module resolves its database at import time, so each test that
touches the store points nbchat.core.db.DB_PATH at a temp file and
re-initialises.
"""
import json
import os
import tempfile

import nbchat.core.db as db
import nbchat.core.task_tracker as tt
import pytest


@pytest.fixture()
def tmpdb(monkeypatch):
    path = tempfile.mktemp(prefix="task_track_", suffix=".db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


class _FakeAgent:
    def __init__(self, session_id):
        self.session_id = session_id
        self.model_name = "test-model"


# ---------------------------------------------------------------- lifecycle

def test_start_and_finish_write_row(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:s1"), "implement the stats feature")
    assert rec.task_id is not None

    rec.record_llm_call(latency_s=1.5, prompt_chars=2000, completion_chars=120)
    rec.record_tool_call("read_file", '{"path": "a.py"}', error=False,
                         is_tool_turn=False)
    rec.record_llm_call(latency_s=0.8, prompt_chars=2600, completion_chars=80)
    rec.note_tool_turn()
    rec.record_tool_call("run_command", '{"command": "ls"}', error=True,
                         is_tool_turn=False)

    fields = tt.finish_task(rec, final_response="Done, sir.", status="complete")

    row = db.query_tasks(session_id="tui:s1")[0]
    assert fields is not None
    assert row["status"] == "complete"
    assert row["completion"] == "complete"
    assert row["num_llm_calls"] == 2
    assert row["num_tool_turns"] == 1
    assert row["tool_calls_total"] == 2
    assert json.loads(row["tool_calls_by_name"]) == {"read_file": 1, "run_command": 1}
    assert row["tool_calls_failed"] == 1
    assert row["error_count"] == 1
    assert row["final_response_chars"] == len("Done, sir.")
    assert row["llm_latency_s"] == pytest.approx(2.3, abs=0.01)
    assert row["prompt_chars"] == 4600
    assert row["request_chars"] == len("implement the stats feature")
    assert row["duration_s"] >= 0


def test_finish_is_idempotent(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:s2"), "hi")
    tt.finish_task(rec, "ok", status="complete")
    assert tt.finish_task(rec, "again", status="complete") is None
    assert len(db.query_tasks(session_id="tui:s2")) == 1


def test_status_to_completion_mapping(tmpdb):
    for status, completion in (("complete", "complete"),
                               ("interrupted", "partial"),
                               ("failed", "not_completed")):
        rec = tt.start_task(_FakeAgent("tui:s3"), "x")
        tt.finish_task(rec, "y", status=status)
    rows = db.query_tasks(session_id="tui:s3")
    assert [r["completion"] for r in rows] == [
        "not_completed", "partial", "complete",
    ]  # newest first: failed, interrupted, complete


# ----------------------------------------------------------- event counters

def test_event_counters_and_context(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:e1"), "x")
    rec.record_event("stall")
    rec.record_event("stall")
    rec.record_event("truncation")
    rec.record_event("stream_retry")
    rec.record_event("text_toolcall")
    rec.record_stream_error()
    rec.record_context(500)
    rec.record_context(9000)
    rec.record_context(300)
    fields = tt.finish_task(rec, "done", status="complete")
    assert fields["stall_events"] == 2
    assert fields["truncation_events"] == 1
    assert fields["stream_retries"] == 1
    assert fields["text_toolcall_recovery"] == 1
    assert fields["max_context_chars"] == 9000
    assert fields["error_count"] == 1  # the surfaced stream error


# ------------------------------------------------------------- redundancy

def test_redundancy_counts_read_and_write(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:r1"), "x")
    rec.record_tool_call("read_file", '{"path": "a.py"}', error=False)
    rec.record_tool_call("read_file", '{"path": "b.py"}', error=False)
    rec.record_tool_call("read_file", '{"path": "a.py"}', error=False)  # dup read
    rec.record_tool_call("run_command", '{"command": "ls"}', error=False)
    rec.record_tool_call("run_command", '{"command": "ls"}', error=False)  # dup write
    fields = tt.finish_task(rec, "done", status="complete")
    assert fields["redundant_tool_calls"] == 2
    assert fields["redundant_reads"] == 1
    assert fields["redundant_writes"] == 1
    assert fields["tool_calls_total"] == 5


def test_redundancy_normalises_arg_order(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:r2"), "x")
    rec.record_tool_call("read_file", '{"path": "a.py", "limit": 10}', error=False)
    rec.record_tool_call("read_file", '{"limit": 10, "path": "a.py"}', error=False)
    fields = tt.finish_task(rec, "done", status="complete")
    assert fields["redundant_tool_calls"] == 1
    assert fields["redundant_reads"] == 1


def test_redundancy_not_across_tasks(tmpdb):
    r1 = tt.start_task(_FakeAgent("tui:r3"), "x")
    r1.record_tool_call("read_file", '{"path": "a.py"}', error=False)
    tt.finish_task(r1, "done", status="complete")
    r2 = tt.start_task(_FakeAgent("tui:r3"), "y")
    r2.record_tool_call("read_file", '{"path": "a.py"}', error=False)
    fields = tt.finish_task(r2, "done", status="complete")
    assert fields["redundant_tool_calls"] == 0


# ------------------------------------------------------- user interventions

def test_user_interventions_counted(tmpdb):
    # The trigger message is the session's last user row at task start;
    # a redirect logged mid-turn gets a higher row id and must count.
    db.log_message("tui:u1", "user", "start the job")
    rec = tt.start_task(_FakeAgent("tui:u1"), "start the job")
    db.log_message("tui:u1", "user", "wait, also do X")
    db.log_message("tui:u1", "user", "and also Y")
    db.log_message("tui:u1", "assistant", "understood")
    fields = tt.finish_task(rec, "done", status="complete")
    assert fields["user_interventions"] == 2
    assert fields["final_response_chars"] == len("done")


def test_no_interventions_when_quiet(tmpdb):
    db.log_message("tui:u2", "user", "start the job")
    rec = tt.start_task(_FakeAgent("tui:u2"), "start the job")
    db.log_message("tui:u2", "assistant", "working")
    fields = tt.finish_task(rec, "done", status="complete")
    assert fields["user_interventions"] == 0


# ------------------------------------------------------------ orphan sweep

def test_orphan_swept_to_in_progress(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:orphan"), "dying turn")
    rows = db.query_tasks(session_id="tui:orphan")
    assert rows[0]["status"] == "in_progress"
    # Force the row to look old, then re-init: sweep should label it.
    conn = db._connect()
    conn.execute(
        "UPDATE task_log SET status='complete' WHERE id=?", (rows[0]["id"],)
    )
    conn.execute(
        "UPDATE task_log SET started_at=datetime('now','-2 hours') WHERE id=?",
        (rows[0]["id"],),
    )
    conn.commit()
    conn.close()
    # A terminal status ('complete') must NOT be overwritten by the sweep.
    db.init_db()
    row = db.query_tasks(session_id="tui:orphan")[0]
    assert row["status"] == "complete"

    # A non-terminal stale row IS swept to in_progress.
    conn = db._connect()
    conn.execute(
        "UPDATE task_log SET status='stale_open' WHERE id=?", (row["id"],)
    )
    conn.commit()
    conn.close()
    db.init_db()
    row = db.query_tasks(session_id="tui:orphan")[0]
    assert row["status"] == "in_progress"
    assert row["completion"] == "unknown"


# ------------------------------------------------------------ stats plumbing

def test_summary_rows_and_summarize(tmpdb):
    rec = tt.start_task(_FakeAgent("tui:st1"), "x")
    rec.record_llm_call(latency_s=1.0)
    rec.record_tool_call("read_file", '{"path": "a"}', error=False,
                         is_tool_turn=False)
    rec.record_tool_call("read_file", '{"path": "a"}', error=False,
                         is_tool_turn=False)  # redundant duplicate
    tt.finish_task(rec, "ok", status="complete")

    rows = db.task_summary_rows("tui:st1", limit=10)
    assert len(rows) == 1
    assert rows[0]["tool_calls_total"] == 2  # duplicate is a second call
    assert rows[0]["redundant_tool_calls"] == 1

    summary = tt.summarize_tasks(rows)
    assert summary["tasks"] == 1
    assert summary["by_completion"] == {"complete": 1}
    assert summary["failure_rate"] == 0.0
    assert summary["totals"]["llm_calls"] == 1
    assert summary["totals"]["tool_calls"] == 2
    assert summary["totals"]["redundant"] == 1
    assert "duration_s" in summary and "mean" in summary["duration_s"]


def test_summarize_empty():
    summary = tt.summarize_tasks([])
    assert summary["tasks"] == 0
    assert summary["failure_rate"] == 0.0
