"""Unit tests for nbchat.core.team (multi-agent team execution).

Covers, without a live LLM server:
  * plan parsing (fenced / bare JSON, truncation, missing keys)
  * TaskQueue claim semantics (one owner per task, stop() halts claiming)
  * run_plan dispatch (success, failure, timeout, interrupted, cancelled,
    concurrent execution, worker-count cap)
  * TeamCoordinator.run() end-to-end with a mocked _coordinator_call
    (plan -> tasks -> synthesis -> persisted summary)
  * TeamAgent output hooks (per-worker prefixing, DB persistence)
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from nbchat.core import db
from nbchat.core.team import (
    PLAN_PARSE_FAILED,
    PlanParseError,
    Task,
    TaskQueue,
    TeamAgent,
    TeamCoordinator,
    _coordinator_system_prompt,
    _default_worker_prompt,
    _extract_json,
    _parse_tasks,
    _sanitize_plan,
)

TIMEOUT = 5.0  # generous for CI; the logic under test is near-instant


# ---------------------------------------------------------------------------
# Plan parsing
# ---------------------------------------------------------------------------

def test_parse_tasks_fenced_json():
    text = (
        "Here is the plan:\n"
        "```json\n"
        "[{\"title\": \"fix tests\", \"objective\": \"make the suite green\"}]\n"
        "```\n"
        "That's all."
    )
    tasks = _parse_tasks(text)
    assert len(tasks) == 1
    assert tasks[0].title == "fix tests"
    assert tasks[0].objective == "make the suite green"
    assert tasks[0].task_id == "T1"


def test_parse_tasks_bare_json_array():
    text = '[{"title": "a"}, {"title": "b", "objective": "B"}]'
    tasks = _parse_tasks(text)
    assert [t.title for t in tasks] == ["a", "b"]
    assert tasks[1].objective == "B"
    assert tasks[0].objective == "a"  # title used as fallback


def test_parse_tasks_bad_json_raises():
    with pytest.raises(PlanParseError):
        _parse_tasks("no json here at all")


def test_parse_tasks_truncated_json_raises():
    # Simulates a max_tokens truncation: an array cut mid-object.
    with pytest.raises(PlanParseError):
        _parse_tasks('[{"title": "a", "objective": "A"}, {"title": "b"')


def test_parse_tasks_empty_list_raises():
    with pytest.raises(PlanParseError):
        _parse_tasks("[]")


def test_extract_json_nested_arrays():
    inner = "[[[1, 2], 3]]"
    text = f'noise\n```json\n{inner}\n```\nmore noise'
    assert json.loads(_extract_json(text)) == [[[1, 2], 3]]


def test_sanitize_plan_drops_unusable_entries_and_caps_count():
    raw = [
        {"title": "ok", "objective": "do it"},
        {"title": ""},                     # no objective usable -> dropped
        "not-a-dict",                      # dropped
        {"objective": "obj only"},         # title falls back to objective
        {"title": "cap test"},
        {"title": "should be dropped"},
    ]
    plan = _sanitize_plan(raw, max_tasks=3)
    assert [t.title for t in plan] == ["ok", "obj only", "cap test"]
    assert all(t.task_id for t in plan)


def test_parse_tasks_empty_string_raises_plan_parse_failed_marker():
    with pytest.raises(PlanParseError) as exc:
        _parse_tasks("")
    assert exc.value.reason == PLAN_PARSE_FAILED


# ---------------------------------------------------------------------------
# TaskQueue
# ---------------------------------------------------------------------------

def test_task_queue_single_owner_and_stop():
    q = TaskQueue([Task("a", "A"), Task("b", "B")])
    assert q.claim() == "a"
    assert q.claim() == "b"
    assert q.claim() is None      # nothing left pending
    q.release("b")
    assert q.claim() == "b"       # released tasks can be claimed again
    assert q.claim() is None
    q.stop()
    assert q.claim() is None
    assert q.join()


def test_task_queue_is_done():
    q = TaskQueue([Task("a", "A")])
    assert not q.is_done()
    q.claim()
    assert q.is_done()


# ---------------------------------------------------------------------------
# run_plan
# ---------------------------------------------------------------------------

class _FastWorker:
    """Worker stand-in whose run() succeeds immediately."""

    model_name = "test-model"

    def __init__(self, results=None):
        self.results = results if results is not None else {}
        self.calls = []

    def run(self, objective):
        self.calls.append(objective)
        return self.results.get(objective, f"done {objective}")


class _FailingWorker:
    model_name = "test-model"

    def __init__(self):
        self.calls = []

    def run(self, objective):
        self.calls.append(objective)
        raise RuntimeError("boom")


class _SlowWorker:
    model_name = "test-model"

    def __init__(self, sleep: float):
        self.sleep = sleep
        self.calls = []

    def run(self, objective):
        self.calls.append(objective)
        time.sleep(self.sleep)
        return f"done {objective}"


def test_run_plan_success():
    tasks = [Task("a", "A"), Task("b", "B")]
    res = TeamCoordinator.run_plan(tasks, _FastWorker(),
                                   max_workers=2, timeout=TIMEOUT)
    assert {t.task_id: t.status for t in tasks} == {"a": "done", "b": "done"}
    assert tasks[0].summary == "done A"
    assert tasks[1].summary == "done B"
    assert res == "done"


def test_run_plan_failure():
    tasks = [Task("a", "A")]
    res = TeamCoordinator.run_plan(tasks, _FailingWorker(),
                                   max_workers=1, timeout=TIMEOUT)
    assert res == "failed"
    assert tasks[0].status == "failed"
    assert "boom" in tasks[0].summary


def test_run_plan_timeout_marks_failed():
    tasks = [Task("a", "A")]
    t0 = time.monotonic()
    res = TeamCoordinator.run_plan(tasks, _SlowWorker(3.0),
                                   max_workers=1, timeout=0.2)
    elapsed = time.monotonic() - t0
    assert res == "failed"
    assert tasks[0].status == "failed"
    assert "timed out" in tasks[0].summary
    assert elapsed < 2.5          # coordinator gave up, did not wait 3s


def test_run_plan_concurrent_execution():
    # Two tasks each sleeping 0.3s run in ~0.3s total only if concurrent.
    tasks = [Task("a", "A"), Task("b", "B")]
    t0 = time.monotonic()
    res = TeamCoordinator.run_plan(tasks, _SlowWorker(0.3),
                                   max_workers=2, timeout=TIMEOUT)
    elapsed = time.monotonic() - t0
    assert res == "done"
    assert elapsed < 0.55


def test_run_plan_respects_worker_cap():
    tasks = [Task(chr(97 + i), f"T{chr(97 + i)}") for i in range(5)]
    t0 = time.monotonic()
    # Cap of 1 worker + 0.15s sleeps: 5 tasks take >= 0.75s serialized.
    res = TeamCoordinator.run_plan(tasks, _SlowWorker(0.15),
                                   max_workers=1, timeout=TIMEOUT)
    elapsed = time.monotonic() - t0
    assert res == "done"
    assert all(t.status == "done" for t in tasks)
    assert elapsed >= 0.7


def test_run_plan_stop_marks_pending_failed():
    q = TaskQueue([Task("a", "A"), Task("b", "B"), Task("c", "C")])

    class _StallingWorker:
        model_name = "test-model"

        def __init__(self):
            self.claims = 0

        def run(self, objective):
            self.claims += 1
            time.sleep(3.0)     # hang: forces the coordinator timeout
            return f"done {objective}"

    w = _StallingWorker()
    t0 = time.monotonic()
    res = TeamCoordinator.run_plan(None, w, queue=q, max_workers=3, timeout=0.3)
    elapsed = time.monotonic() - t0
    assert res in ("failed", "interrupted")
    assert elapsed < 2.5
    by_id = {t.task_id: t.status for t in q._tasks.values()}
    assert by_id["a"] in ("failed", "interrupted")
    # Never-claimed tasks must not be left 'pending' forever.
    assert "done" not in by_id.values()


def test_run_plan_interrupted_worker():
    q = TaskQueue([Task("a", "A"), Task("b", "B")])

    class _InterruptedWorker:
        model_name = "test-model"

        def __init__(self):
            self.calls = 0

        def run(self, objective):
            self.calls += 1
            raise KeyboardInterrupt()

    res = TeamCoordinator.run_plan(None, _InterruptedWorker(), queue=q,
                                   max_workers=2, timeout=TIMEOUT)
    assert res == "interrupted"
    assert all(t.status in ("interrupted", "pending", "failed")
               for t in q._tasks.values())


# ---------------------------------------------------------------------------
# TeamCoordinator.run() end-to-end (mocked LLM)
# ---------------------------------------------------------------------------

class _MockClient:
    """Stands in for the OpenAI-compatible client used by the coordinator."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat_completions_create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.responses.pop(0)
        resp = type("R", (), {})()
        choice = type("C", (), {})()
        msg = type("M", (), {})()
        msg.content = text
        choice.message = msg
        resp.choices = [choice]
        return resp


def _make_coordinator(monkeypatch, plan_json, synth="All tasks completed."):
    client = _MockClient([plan_json, synth])
    monkeypatch.setattr(
        "nbchat.core.team.get_client", lambda: client, raising=False)
    # team.py imports get_client inside the call via `from nbchat.core.client
    # import get_client`, so patch the client module attribute instead:
    import nbchat.core.client as client_mod
    monkeypatch.setattr(client_mod, "get_client", lambda: client)
    agent = TeamAgent()
    agent.session_id = f"team:test-{uuid.uuid4().hex[:8]}"
    coord = TeamCoordinator(agent)
    return coord, client, agent


def test_run_end_to_end_success(monkeypatch):
    plan = json.dumps([
        {"title": "read the db module", "objective": "Summarize db.py"},
        {"title": "check tests", "objective": "Run pytest and report"},
    ])
    coord, client, agent = _make_coordinator(monkeypatch, plan)
    results = coord.run("Do the two things in parallel.")

    assert results["status"] == "done"
    assert results["summary"] == "All tasks completed."
    assert results["tasks"] and all(t["status"] == "done" for t in results["tasks"])
    # Both LLM calls happened: plan + synthesis.
    assert len(client.calls) == 2
    # Workers got the worker prompt (not the coordinator system prompt).
    for t in agent.sessions.values():
        assert t.system_prompt == _default_worker_prompt()
        assert t.session_id.startswith("team:")
        break
    # The coordinator persisted a summary row to the shared DB.
    rows = db.get_history(agent.session_id)
    assert rows and rows[0][1] == "assistant" and "All tasks completed." in rows[0][2]


def test_run_planner_failure_falls_back_to_single_task(monkeypatch):
    coord, client, agent = _make_coordinator(
        monkeypatch, "[broken json {", synth="Fallback run.")
    results = coord.run("Investigate the repo layout.")
    assert results["status"] == "done"
    assert len(results["tasks"]) == 1
    assert results["tasks"][0]["objective"] == "Investigate the repo layout."


def test_run_disabled(monkeypatch):
    import nbchat.core.config as config
    monkeypatch.setattr(config, "TEAM_ENABLED", False)
    coord, _, _ = _make_coordinator(monkeypatch, "[]")
    results = coord.run("Whatever.")
    assert results["status"] == "skipped"


def test_run_server_down(monkeypatch):
    import nbchat.core.client as client_mod

    def _boom():
        raise ConnectionError("no server")

    monkeypatch.setattr(client_mod, "get_client", _boom)
    agent = TeamAgent()
    agent.session_id = f"team:test-{uuid.uuid4().hex[:8]}"
    results = TeamCoordinator(agent).run("Do the thing.")
    assert results["status"] == "failed"
    assert "planner" in results["summary"].lower()


def test_run_worker_exception_is_not_fatal(monkeypatch):
    plan = json.dumps([{"title": "t1", "objective": "O1"},
                       {"title": "t2", "objective": "O2"}])
    tasks = _parse_tasks(plan)

    class _W:
        model_name = "test-model"

        def __init__(self):
            self.n = 0

        def run(self, objective):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("worker crash")
            return "second fine"

    res = TeamCoordinator.run_plan(tasks, _W(), max_workers=2, timeout=TIMEOUT)
    by_id = {t.task_id: t.status for t in tasks}
    assert "failed" in by_id.values()
    assert "done" in by_id.values()
    assert res == "done"   # overall success as long as at least one succeeded


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_coordinator_prompt_forbids_execution():
    p = _coordinator_system_prompt()
    assert "MUST NOT" in p or "must not" in p
    assert "json" in p.lower()


def test_worker_prompt_requires_self_containment():
    p = _default_worker_prompt()
    assert "parallel" in p.lower()
    assert "independent" in p.lower() or "isolat" in p.lower()


# ---------------------------------------------------------------------------
# TeamAgent output hooks
# ---------------------------------------------------------------------------

def test_team_agent_hooks_prefix_and_persist(monkeypatch, capsys):
    written = []
    monkeypatch.setattr(db, "log_message",
                        lambda sid, role, content: written.append((sid, role, content)))
    agent = TeamAgent()
    agent.session_id = "team:hooks-test"
    agent._wrap_worker_hooks("w1")

    agent._on_stream_token("hello ")
    agent._on_stream_token("hello world")
    agent._on_tool_display("toolout", "run_command", '{"command":"ls"}')
    agent._on_agent_message("something broke")
    agent._on_stream_complete("hello world", None)

    out = capsys.readouterr().out
    assert "[w1]" in out
    assert "hello world" in out
    assert "run_command" in out
    # DB rows are persisted under the TEAM session, role preserved.
    assert ("team:hooks-test", "assistant", "hello world") in written
    roles = {r for _, r, _ in written}
    assert "user" in roles and "tool" in roles
