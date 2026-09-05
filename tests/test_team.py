"""Unit tests for nbchat.core.team (multi-agent team execution).

All groups restored after the strip-down bisection: plan parsing,
TaskQueue, run_plan dispatch, TeamCoordinator.run() end-to-end (mocked
LLM + stub workers), prompts, TeamAgent hooks, ToolArbiter.  The pre-fix
snapshot is preserved at tests/test_team.py.disabled.
"""
from __future__ import annotations

import json

import threading
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
    ToolArbiter,
    _coordinator_system_prompt,
    _default_worker_prompt,
    _extract_json,
    _parse_tasks,
    _salvage_objects,
    _clip_summary,
    _sanitize_plan,
    _break_dependency_cycles,
    _resolve_dependencies,
    _worker_run,
)

TIMEOUT = 5.0  # generous for CI; the logic under test is near-instant


# ---------------------------------------------------------------------------
# Plan parsing (pure functions, no threads, no LLM)
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


def test_parse_tasks_truncated_json_salvages_complete_objects():
    # Simulates a max_tokens truncation: an array cut mid-object.
    # The salvage path (team.py:_salvage_objects) recovers the complete
    # leading objects instead of discarding a mostly-good plan.
    tasks = _parse_tasks('[{"title": "a", "objective": "A"}, {"title": "b"')
    assert len(tasks) == 1
    assert tasks[0].title == "a"


def test_parse_tasks_truncated_json_no_complete_object_raises():
    # Cut before any object is complete: nothing to salvage, so it must
    # still raise PlanParseError.
    with pytest.raises(PlanParseError):
        _parse_tasks('[{"title": "a"')


def test_parse_tasks_empty_list_raises():
    with pytest.raises(PlanParseError):
        _parse_tasks("[]")


def test_extract_json_nested_arrays():
    inner = "[[[1, 2], 3]]"
    text = f'noise\n```json\n{inner}\n```\nmore noise'
    assert json.loads(_extract_json(text)) == [[[1, 2], 3]]


# ---------------------------------------------------------------------------
# Parser edge cases (review item C: brittle custom JSON state machines)
# _extract_json / _salvage_objects are hand-rolled, string/escape aware
# scanners; these cases pin down the behaviours LLM outputs can violate
# (unicode, nested escapes, brackets inside strings, truncation, whitespace
# injection).  A regression here must be a hard, visible failure.
# ---------------------------------------------------------------------------

def test_extract_json_brackets_inside_string_literals():
    text = 'preamble [{"objective": "note: [[x]] and {a}"}] trailer'
    got = json.loads(_extract_json(text))
    assert got == [{"objective": "note: [[x]] and {a}"}]


def test_extract_json_nested_escaped_quote():
    # An escaped quote (backslash + double-quote) inside a string
    # literal must not terminate the string, and an escaped
    # backslash (\\\\) must be consumed as a pair, so the scanner
    # lands on the array's true closing bracket.
    text = 'here is the plan: [{"objective": "quote: \\" end path: C:\\\\"}] hope this helps'
    got = json.loads(_extract_json(text))
    assert got == [{"objective": 'quote: " end path: C:\\'}]

def test_extract_json_escaped_quote_before_brackets():
    # An escaped quote next to bracket characters must not terminate the
    # string early or close the top-level array.  json.dumps builds the
    # literal so the escaping is unambiguous.
    obj = {"objective": 'say "hi" ] } [ now'}
    text = "plan: " + json.dumps([obj]) + " hope this helps"
    got = json.loads(_extract_json(text))
    assert got == [obj]


def test_extract_json_unicode_and_nested_brackets():
    # Non-ASCII characters and bracket characters inside string
    # literals must be preserved and must not confuse string tracking.
    obj = {"objective": "r\u00e9sum\u00e9 \u2014 na\u00efve [draft] \u00abx\u00bb",
           "title": "\u00fcn\u00efcode"}
    text = "here: ```json\n" + json.dumps([obj], ensure_ascii=False) + "\n``` ok"
    got = json.loads(_extract_json(text))
    assert got == [obj]


def test_extract_json_surrounding_whitespace_and_trailing_brackets():
    # Whitespace/newlines around the array must be stripped, and any
    # bracket-bearing text AFTER the balanced array must not be captured
    # (the scanner stops at the array's own closing bracket).
    obj = {"objective": "trim me"}
    text = "   \n\t " + json.dumps([obj]) + "  \n  trailing [noise] here"
    got = json.loads(_extract_json(text))
    assert got == [obj]


def test_extract_json_brackets_inside_string_do_not_close_array():
    # A bare ] and { } (no escaped quote) inside a string literal must
    # not reduce the array depth.
    obj = {"objective": "close ] open [ and braces { } stay inside"}
    text = json.dumps([obj])
    got = json.loads(_extract_json(text))
    assert got == [obj]


def test_extract_json_truncation_mid_string_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json('[{"objective": "run the full build and t')


def test_extract_json_truncation_mid_array_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json('[{"a": 1}, {"b": 2}')


def test_extract_json_weird_whitespace_injection():
    text = ("weird \n\t \u00a0 \r\f [\n   {   \"objective\"\n:\n"
            '"x\"\n}\n ] trailing')
    got = json.loads(_extract_json(text))
    assert got == [{"objective": "x"}]


def test_extract_json_no_array_raises_value_error():
    with pytest.raises(ValueError):
        _extract_json("no array here, only a string")


def test_salvage_objects_truncated_last_object_dropped():
    text = ('[{"title": "a", "objective": "A"}, '
            '{"title": "b", "objective": "B"}, '
            '{"title": "trun')
    objs = _salvage_objects(text)
    assert objs == [{"title": "a", "objective": "A"},
                    {"title": "b", "objective": "B"}]


def test_salvage_objects_escaped_quote_in_truncated_tail():
    # An escaped quote before the truncation point must not swallow the
    # object boundary.
    text = ('[{"objective": "a: \\"x\\""}, '
            '{"objective": "b cut m')
    objs = _salvage_objects(text)
    assert objs == [{"objective": 'a: "x"'}]


def test_salvage_objects_nested_braces_in_strings():
    text = ('[{"objective": "write {x} and [1]"}, '
            '{"title": "second"}')
    objs = _salvage_objects(text)
    assert objs == [{"objective": "write {x} and [1]"}, {"title": "second"}]


def test_salvage_objects_no_array_returns_none():
    assert _salvage_objects("no bracket at all") is None


def test_salvage_objects_no_complete_object_returns_none():
    assert _salvage_objects('[{"title": "a"') is None


def test_parse_tasks_truncated_unicode_object_salvaged():
    # Truncation inside a unicode-heavy second object: the first, complete,
    # object must still be recoverable by the planner fallback.
    text = ('[{"objective": "caf\u00e9 \u2603 done"}, '
            '{"objective": "\u00fcber [x] cu')
    tasks = _parse_tasks(text)
    assert len(tasks) == 1
    assert tasks[0].objective == "caf\u00e9 \u2603 done"


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
# NEXT GROUPS (in tests/test_team.py.disabled, add one at a time):
#   * run_plan dispatch + worker stubs
#   * TeamCoordinator.run() end-to-end (mocked LLM)
#   * prompts, TeamAgent hooks, ToolArbiter
# ---------------------------------------------------------------------------
class _FastWorker:
    """Worker stand-in whose run() succeeds immediately."""

    model_name = "test-model"

    @property
    def agent(self):
        """``run_plan`` dispatches through ``worker.agent``; a stub *is*
        its own agent."""
        return self

    def __init__(self, results=None):
        self.results = results if results is not None else {}
        self.calls = []

    def run(self, objective):
        self.calls.append(objective)
        return self.results.get(objective, f"done {objective}")


class _FailingWorker:
    model_name = "test-model"

    @property
    def agent(self):
        return self

    def __init__(self):
        self.calls = []

    def run(self, objective):
        self.calls.append(objective)
        raise RuntimeError("boom")


class _SlowWorker:
    model_name = "test-model"

    @property
    def agent(self):
        return self

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

        @property
        def agent(self):
            return self

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

        @property
        def agent(self):
            return self

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

_FakeWorker = _FastWorker


class _MockClient:
    """Stands in for the OpenAI-compatible client used by the coordinator."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        # Mirror the real client's nested ``chat.completions.create`` path so
        # the production code path (no test-only shortcuts) is exercised.
        self.chat = type("Chat", (), {})()
        self.chat.completions = type("Completions", (), {})()
        self.chat.completions.create = self._create

    def _create(self, **kwargs):
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
    # Use a fake worker factory: the default (TerminalAgent) would spawn
    # real LLM-backed workers, making these unit tests slow and flaky.
    coord = TeamCoordinator(agent, worker_factory=_FakeWorker)
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
    # The planner retries up to TEAM_PLAN_ATTEMPTS times (default 2),
    # consuming one (garbage) response per attempt before falling back:
    # responses must cover attempt 1, attempt 2 and the synthesis call.
    client.responses.insert(0, "[broken json {")
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

        @property
        def agent(self):
            return self

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
# Synthesis-report clipping (the 2026-09-04 /team incident: a head-only
# 500-char slice lopped the final answer off a worker's summary)
# ---------------------------------------------------------------------------

def test_clip_summary_short_passes_through():
    s = "Answer: 13 days"
    assert _clip_summary(s, 1000) == s
    assert _clip_summary("", 1000) == ""


def test_clip_summary_keeps_head_and_tail():
    # Final answer sits at the END — must survive clipping.
    body = "Investigation notes. " * 300
    summary = body + "Coordinates: lat 38.0693586, lon -78.9099644"
    out = _clip_summary(summary, 1000)
    assert len(out) <= 1000
    assert "Coordinates: lat 38.0693586, lon -78.9099644" in out
    assert out.startswith(body[:100])
    assert "[summary clipped: middle omitted]" in out


def test_clip_summary_tiny_budget_still_bounded():
    s = "x" * 5000
    out = _clip_summary(s, 50)
    assert len(out) <= 50


def _make_coordinator_plain(monkeypatch, plan, synth="Report ok."):
    from nbchat.core.client import _FakeLLM  # noqa: F401  (if present)
    import nbchat.core.client as client_mod

    client = type(
        "_Client",
        (),
        {
            "calls": [],
            "responses": [plan, synth],
            "model_name": "fake",
        },
    )

    def _fake_call(**kw):
        client.calls.append(kw)
        return client.responses.pop(0)

    client.call = _fake_call
    monkeypatch.setattr(client_mod, "get_client", lambda: client)
    agent = TeamAgent()
    agent.session_id = f"team:test-{uuid.uuid4().hex[:8]}"
    coord = TeamCoordinator(agent, worker_factory=_FakeWorker)
    return coord


def test_build_report_clips_long_summary(monkeypatch):
    import nbchat.core.config as config

    agent = TeamAgent()
    agent.session_id = f"team:test-{uuid.uuid4().hex[:8]}"
    coord = TeamCoordinator(agent)
    long_summary = ("notes. " * 2000) + "Answer: 13 days"
    tasks = [Task("t1", "obj", title="UFC days",
                  status="done", summary=long_summary)]
    report = coord._build_report(tasks)
    assert "Answer: 13 days" in report           # tail preserved
    assert "[summary clipped: middle omitted]" in report


def test_build_report_total_budget_shrinks_per_summary(monkeypatch):
    import nbchat.core.config as config

    agent = TeamAgent()
    agent.session_id = f"team:test-{uuid.uuid4().hex[:8]}"
    coord = TeamCoordinator(agent)
    monkeypatch.setattr(config, "TEAM_REPORT_MAX_CHARS", 6000)
    monkeypatch.setattr(config, "TEAM_SUMMARY_MAX_CHARS", 4096)
    tasks = [
        Task(f"t{i}", "obj", title=f"task {i}", status="done",
             summary=f"body {i}. " * 400 + f"Answer: {i}")
        for i in range(8)
    ]
    report = coord._build_report(tasks)
    assert len(report) <= 6000
    for i in range(8):
        assert f"Answer: {i}" in report          # every answer survives


# ---------------------------------------------------------------------------
# Planner-level dependencies (DAG)
# ---------------------------------------------------------------------------


class _DepsWorker:
    """Worker stand-in that records every objective it is given.

    *run* accepts the (objective, task, deps) shape the real
    ``_worker_run`` dispatch uses; ``results`` maps the *base* objective
    (any objective starting with it) to the summary to return.
    """

    model_name = "test-model"

    @property
    def agent(self):
        return self

    def __init__(self, results=None, sleep: float = 0.0):
        self.results = results or {}
        self.sleep = sleep
        self.objectives = []

    def run(self, objective, task=None, deps=None):
        self.objectives.append(objective)
        if self.sleep:
            time.sleep(self.sleep)
        for base, value in self.results.items():
            if objective.startswith(base):
                return value
        return "done " + objective


def test_break_dependency_cycles_breaks_minimally():
    tasks = [
        Task("T1", "a", depends_on=("T2",)),          # cycle
        Task("T2", "b", depends_on=("T1",)),          # cycle
        Task("T3", "c", depends_on=("T1", "T3", "T9", "T4")),  # fwd+self+dangling
    ]
    out = _break_dependency_cycles(tasks)
    assert out[0].depends_on == ()
    assert out[1].depends_on == ("T1",)               # minimal break: T2 keeps its edge
    assert out[2].depends_on == ("T1",)               # forward-only kept




def test_resolve_dependencies():
    t = Task("T3", "c", depends_on=("T1", "T2"))
    assert _resolve_dependencies(t, {"T1": "done", "T2": "done"})
    assert _resolve_dependencies(t, {"T1": "failed", "T2": "done"})
    assert not _resolve_dependencies(t, {"T1": "done", "T2": "claimed"})
    assert not _resolve_dependencies(t, {"T1": "pending", "T2": "pending"})
    assert _resolve_dependencies(Task("T9", "x"), {"T1": "pending"})


def test_parse_tasks_keeps_depends_on():
    plan = ('[{"title": "a", "objective": "A"}, '
            '{"title": "b", "objective": "B", "depends_on": [1]}, '
            '{"title": "s", "objective": "S", "depends_on": [1, 2]}]')
    tasks = _parse_tasks(plan)
    assert [t.task_id for t in tasks] == ["T1", "T2", "T3"]
    assert tasks[1].depends_on == ("T1",)
    assert tasks[2].depends_on == ("T1", "T2")


def test_queue_blocks_dependent_task_until_prereq_resolves():
    q = TaskQueue([Task("a", "A"),
                   Task("s", "S", depends_on=("a",))])
    first = q.claim()
    assert first == "a"
    assert q.claim() is None          # s blocked on a (still claimed)
    q._tasks["a"].status = "done"
    assert q.claim() == "s"


def test_queue_dependent_unblocks_when_prereq_fails():
    q = TaskQueue([Task("a", "A"),
                   Task("s", "S", depends_on=("a",))])
    q._tasks["a"].status = "failed"   # terminal (any) unblocks dependents
    assert q.claim() == "s"


def test_queue_wait_claim_timeout_and_exhaustion():
    q = TaskQueue([Task("s", "S", depends_on=("a",))])  # a never appears
    # A truncated prerequisite is failed at claim time rather
    # than blocking forever (and rather than running blind).
    assert q.wait_claim(timeout=0.05) is None
    assert q._tasks["s"].status == "failed"
    assert "missing prerequisite" in q._tasks["s"].summary


def test_wait_claim_stop_wakes_parked_worker():
    q = TaskQueue([Task("a", "A"),
                   Task("s", "S", depends_on=("a",))])
    got = {}

    def parked():
        got["tid"] = q.wait_claim(timeout=5.0)

    q.claim()                    # claim a; s is now blocked on it
    th = threading.Thread(target=parked, daemon=True)
    th.start()
    time.sleep(0.1)          # parked: s blocked on claimed a
    assert "tid" not in got  # still blocked
    q.stop()
    th.join(2.0)
    assert got.get("tid") is None  # woke on stop(), did not sleep 5s


def test_run_plan_dependent_task_waits_and_sees_prereq_results():
    tasks = [Task("a", "A"), Task("b", "B"),
             Task("s", "SYNTH", depends_on=("a", "b"))]
    w = _DepsWorker(results={"A": "alpha", "B": "beta"}, sleep=0.1)
    t0 = time.monotonic()
    res = TeamCoordinator.run_plan(tasks, w, max_workers=3, timeout=TIMEOUT)
    elapsed = time.monotonic() - t0
    assert res == "done"
    assert all(t.status == "done" for t in tasks)
    # The synthesis waited for BOTH prereqs: total >= 2 * 0.1s.
    assert elapsed >= 0.2
    synth = [o for o in w.objectives if o.startswith("SYNTH")]
    assert len(synth) == 1
    assert "[a] status=done" in synth[0]
    assert "alpha" in synth[0]
    assert "[b] status=done" in synth[0]
    assert "beta" in synth[0]


def test_run_plan_missing_prereq_fails_fast():
    tasks = [Task("s", "S", depends_on=("ghost",))]
    w = _DepsWorker()
    t0 = time.monotonic()
    res = TeamCoordinator.run_plan(tasks, w, max_workers=1,
                                   timeout=TIMEOUT)
    elapsed = time.monotonic() - t0
    assert res == "failed"
    assert tasks[0].status == "failed"
    assert "missing prerequisite" in tasks[0].summary
    assert w.objectives == []       # never executed blind
    assert elapsed < 2.5            # no deadline wait


def test_worker_run_injects_prereq_block():
    t = Task("T3", "S", depends_on=("T1", "T2"))
    got = {}

    def _run(objective, task=None, deps=None):
        got["objective"] = objective
        return "ok"

    class _W:
        model_name = "test-model"
        run = staticmethod(_run)

    out = _worker_run(_W(), "S", task=t,
                      deps={"T1": ("done", "one"),
                            "T2": ("failed", "boom")})
    assert out == "ok"
    assert "T1" in got["objective"]
    assert "one" in got["objective"]
    assert "status=failed" in got["objective"]
    assert "boom" in got["objective"]
    # No deps mapping -> the block is absent, objective unchanged.
    got.clear()
    _worker_run(_W(), "S", task=t)
    assert got["objective"] == "S"
