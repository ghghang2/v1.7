"""Tests for Phase 1c — bounded parallel execution of consecutive read-only
tool calls within a single model turn (nbchat.core.conversation).

The design is conservative on purpose:

* Only ``run_command`` calls with no ``cwd`` whose command the idempotent
  read-cache classifier certifies as a *read* are eligible for the parallel
  batch.  Everything else (writes, browser/test/delegate calls, ``cwd``-scoped
  reads, unrecognised commands) stays on the serial path so mutations and
  ordering are never disturbed.
* Only *consecutive* eligible reads form a batch; a single non-read call
  between two reads breaks the run.  A lone read is executed inline (no pool).
* Results are always returned in the ORIGINAL tool-call order — identical to
  what a serial ``for tc in tool_calls`` loop would have produced — so the
  model's next turn is byte-for-byte unaffected by whether reads ran in
  parallel.  The win is wall-clock: a batch's latency is the slowest read,
  not the sum.

These tests exercise the eligibility predicate, the ordering guarantee, the
parallel wall-clock behaviour (via a patched executor that records call order
and sleeps), and the serial-fallback for non-read calls — no network, no LLM.
"""
from __future__ import annotations

import time

from nbchat.core import conversation
from nbchat.core import tool_executor as executor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tc(name: str, args: dict) -> dict:
    """Build a tool_call dict in the exact shape the loop produces."""
    import json as _json
    return {
        "id": "call_test",
        "type": "function",
        "function": {"name": name, "arguments": _json.dumps(args)},
    }


def _clear_read_cache() -> None:
    executor._READ_CACHE.clear()


def _mix() -> conversation.ConversationMixin:
    """A bare mixin instance — enough surface for the two Phase 1c methods
    (they use only ``self`` for the eligibility predicate)."""
    return conversation.ConversationMixin.__new__(conversation.ConversationMixin)


# ---------------------------------------------------------------------------
# Eligibility predicate
# ---------------------------------------------------------------------------

def test_parallel_readable_run_command_read():
    m = _mix()
    assert m._is_parallel_readable(_tc("run_command", {"command": "ls -la"})) is True
    assert m._is_parallel_readable(_tc("run_command", {"command": "cat docs/x.md"})) is True
    assert m._is_parallel_readable(_tc("run_command", {"command": "git status"})) is True


def test_parallel_readable_rejects_cwd_scoped_read():
    m = _mix()
    # A read, but scoped to a working directory — the classifier cannot
    # vouch for it, so it must stay serial.
    tc = _tc("run_command", {"command": "ls -la", "cwd": "/tmp"})
    assert m._is_parallel_readable(tc) is False


def test_parallel_readable_rejects_non_read_command():
    m = _mix()
    assert m._is_parallel_readable(_tc("run_command", {"command": "rm -rf /"})) is False
    assert m._is_parallel_readable(_tc("run_command", {"command": "echo hi > f"})) is False


def test_parallel_readable_rejects_other_tools():
    m = _mix()
    assert m._is_parallel_readable(_tc("browser", {"url": "https://x"})) is False
    assert m._is_parallel_readable(_tc("read_file", {"path": "a"})) is False
    assert m._is_parallel_readable(_tc("delegate_task", {"objective": "x"})) is False


def test_parallel_readable_rejects_malformed_args():
    m = _mix()
    # Arguments that are not valid JSON, or non-dict payloads, must fail safe.
    assert m._is_parallel_readable(_tc("run_command", {})) is False
    assert m._is_parallel_readable(_tc("run_command", {"command": 123})) is False


# ---------------------------------------------------------------------------
# Ordering guarantee
# ---------------------------------------------------------------------------

def test_batch_preserves_original_call_order(monkeypatch):
    _clear_read_cache()
    calls = []

    def fake_run_tool(name, args_json, timeout=None):
        import json as _json
        calls.append(_json.loads(args_json)["command"])
        return f"OK:{_json.loads(args_json)['command']}"

    monkeypatch.setattr(executor, "run_tool", fake_run_tool)
    m = _mix()
    tcs = [
        _tc("run_command", {"command": "ls -la /a"}),
        _tc("run_command", {"command": "cat /b"}),
        _tc("run_command", {"command": "head -n1 /c"}),
    ]
    results = m._batch_tool_results(tcs)

    # Same number of results as calls, in the same order, each tagged with the
    # matching call and its result.
    assert [r[3] for r in results] == ["OK:ls -la /a", "OK:cat /b", "OK:head -n1 /c"]
    assert [r[1] for r in results] == ["run_command"] * 3
    assert [r[0] is tc for r, tc in zip(results, tcs)] == [True, True, True]
    assert [r[4] for r in results] == [False, False, False]  # no errors


def test_batch_serialises_writes_between_read_batches(monkeypatch):
    _clear_read_cache()
    calls = []

    def fake_run_tool(name, args_json, timeout=None):
        import json as _json
        args = _json.loads(args_json)
        calls.append(name if name != "run_command" else args.get("command"))
        return f"R:{calls[-1]}"

    monkeypatch.setattr(executor, "run_tool", fake_run_tool)
    m = _mix()
    tcs = [
        _tc("run_command", {"command": "ls -la /a"}),   # read  -> batch
        _tc("run_command", {"command": "cat /b"}),      # read  -> batch
        _tc("write_file", {"path": "f"}),               # write -> serial
        _tc("run_command", {"command": "head -n1 /c"}), # read  -> batch
        _tc("run_command", {"command": "wc -l /d"}),    # read  -> batch
    ]
    results = m._batch_tool_results(tcs)

    # The write must run serially and appear between the two read batches.
    assert calls.index("write_file") == 2
    assert [r[3] for r in results] == ["R:ls -la /a", "R:cat /b",
                                       "R:write_file", "R:head -n1 /c", "R:wc -l /d"]
    # The write result carries no error keyword, so it is not flagged.
    assert results[2][4] is False


# ---------------------------------------------------------------------------
# Parallel wall-clock behaviour
# ---------------------------------------------------------------------------

def test_consecutive_reads_run_in_parallel(monkeypatch):
    _clear_read_cache()
    sleeps = []

    def fake_run_tool(name, args_json, timeout=None):
        import json as _json
        cmd = _json.loads(args_json)["command"]
        sleeps.append(cmd)
        time.sleep(0.25)
        return f"OK:{cmd}"

    monkeypatch.setattr(executor, "run_tool", fake_run_tool)
    m = _mix()
    tcs = [
        _tc("run_command", {"command": "ls -la /a"}),
        _tc("run_command", {"command": "cat /b"}),
        _tc("run_command", {"command": "head -n1 /c"}),
    ]
    t0 = time.perf_counter()
    results = m._batch_tool_results(tcs)
    elapsed = time.perf_counter() - t0

    assert len(sleeps) == 3
    # Parallel: total wall-clock is near the slowest read (0.25s), NOT the
    # sum (~0.75s).  Allow generous headroom for thread-pool overhead.
    assert elapsed < 0.6, f"reads ran serially (elapsed={elapsed:.2f}s)"
    # And they are still returned in call order.
    assert [r[3] for r in results] == ["OK:ls -la /a", "OK:cat /b", "OK:head -n1 /c"]


def test_single_read_runs_inline_serially(monkeypatch):
    _clear_read_cache()
    invocations = []

    def fake_run_tool(name, args_json, timeout=None):
        invocations.append(args_json)
        return "OK:single"

    monkeypatch.setattr(executor, "run_tool", fake_run_tool)
    m = _mix()
    tcs = [_tc("run_command", {"command": "ls -la /only"})]
    results = m._batch_tool_results(tcs)

    # A lone read is executed inline (no pool) with the same result shape.
    assert len(invocations) == 1
    assert results[0][3] == "OK:single"
    assert results[0][4] is False


# ---------------------------------------------------------------------------
# Conversation-loop integration
# ---------------------------------------------------------------------------

def test_loop_wires_parallel_batching():
    import inspect

    loop_src = inspect.getsource(conversation.ConversationMixin._run_conversation_loop)
    # The loop consumes a turn's results produced up front by the batcher,
    # rather than running each call inline in the loop body.
    assert "self._batch_tool_results" in loop_src
    assert "_turn_results" in loop_src

    # The pool runs each call in a copied context so contextvars (session id,
    # active task) propagate into the worker threads, and results come back in
    # the original call order.
    batch_src = inspect.getsource(conversation.ConversationMixin._batch_tool_results)
    assert "contextvars.copy_context().run" in batch_src
    assert "ThreadPoolExecutor" in batch_src
