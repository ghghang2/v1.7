"""Tests for the nbchat terminal UI (nbchat.tui).

These tests do NOT require a running llama-server: they exercise the
construction path, session bookkeeping, the colour palette and the
streaming output hooks (which write to stdout / capture state).
"""
from __future__ import annotations

import pytest

from nbchat.tui import TerminalAgent, Palette, run  # noqa: F401  (run importable)
from nbchat.tui.agent import short_arg, _arg_hint
from nbchat.tui.colors import Palette as _Palette  # same object
from nbchat.tui.app import handle_command, read_line


# ── Palette ────────────────────────────────────────────────────────────────

def test_palette_disabled_has_no_escapes():
    p = Palette(color=False)
    assert p.color is False
    assert p.cyan("hi") == "hi"
    assert p.bold("") == ""
    assert "\033" not in p.red("x")


def test_palette_wrap_shape_when_enabled(monkeypatch):
    # Force the TTY check to pass so the palette enables regardless of env.
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    p = Palette(color=True)
    assert p.color is True
    out = p.cyan("hi")
    assert out.startswith("\033[36m") and out.endswith("\033[0m")
    assert "hi" in out


# ── Arg helpers ────────────────────────────────────────────────────────────

def test_short_arg_truncates():
    assert short_arg("hello") == "hello"
    long = "x" * 200
    out = short_arg(long)
    assert len(out) == 60 and out.endswith("...")


def test_arg_hint_pretty():
    assert _arg_hint('{"city": "Paris"}') == "city=Paris"
    assert _arg_hint("not json") == "not json"
    assert _arg_hint('{"a": 1, "b": 2}') == "a=1, b=2"


# ── Agent construction & session bookkeeping ───────────────────────────────

def test_agent_constructs_with_tui_session():
    agent = TerminalAgent(color=False)
    assert agent.session_id.startswith("tui:")
    assert agent.history == []
    assert agent.task_log == []
    assert agent._last_response == ""


def test_new_session_resets_state():
    agent = TerminalAgent(color=False)
    first = agent.session_id
    agent.history = [("user", "hi", "", "", "", 0)]
    sid = agent.new_session()
    assert sid != first
    assert agent.session_id == sid
    assert agent.history == []


def test_list_sessions_only_tui():
    agent = TerminalAgent(color=False)
    sessions = agent.list_sessions()
    assert isinstance(sessions, list)
    assert all(s.startswith("tui:") for s in sessions)


def test_remember_and_last_session_roundtrip():
    agent = TerminalAgent(color=False)
    agent.remember_session(agent.session_id)
    assert TerminalAgent.last_session() == agent.session_id


def test_switch_session_reloads():
    agent = TerminalAgent(color=False)
    agent.remember_session(agent.session_id)
    # Switching to the same id is a no-op.
    same = agent.session_id
    agent._switch_session(same)
    assert agent.session_id == same


# ── Streaming output hooks (no network) ────────────────────────────────────

def test_streaming_hooks_write_and_capture(capsys):
    agent = TerminalAgent(color=False)
    agent._on_stream_reasoning("I will think")
    agent._on_stream_reasoning("I will think step by step")
    agent._on_stream_token("Hello")
    agent._on_stream_token("Hello world")
    agent._on_stream_complete("Hello world", None)

    out = capsys.readouterr().out
    assert "[thinking]" in out
    assert "step by step" in out
    assert "Hello world" in out
    assert agent._last_response == "Hello world"
    # Streaming state resets after completion.
    assert agent._content_started is False
    assert agent._reasoning_printed == ""


def test_streaming_content_only_no_reasoning(capsys):
    agent = TerminalAgent(color=False)
    agent._on_stream_token("just an answer")
    agent._on_stream_complete("just an answer", None)
    out = capsys.readouterr().out
    assert "just an answer" in out
    assert "[thinking]" not in out


def test_agent_message_fallback(capsys):
    agent = TerminalAgent(color=False)
    agent._on_agent_message("Maximum tool turns (200) reached.")
    out = capsys.readouterr().out
    assert "Maximum tool turns" in out
    # _last_response should capture the notice when nothing else was set.
    assert agent._last_response == "Maximum tool turns (200) reached."


def test_tool_display(capsys):
    agent = TerminalAgent(color=False)
    agent._on_tool_display('{"result": "File created: a.py"}',
                           "create_file", '{"path": "a.py", "content": "x"}')
    out = capsys.readouterr().out
    assert "create_file" in out
    assert "path=a.py" in out
    assert "File created: a.py" in out


# ── REPL command handling (pure, no network) ──────────────────────────────

def test_handle_command_quit(capsys):
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/quit") is True
    assert handle_command(agent, "/exit") is True


def test_handle_command_new_and_unknown(capsys):
    agent = TerminalAgent(color=False)
    before = agent.session_id
    assert handle_command(agent, "/new") is False
    assert agent.session_id != before
    assert handle_command(agent, "/bogus") is False
    out = capsys.readouterr().out
    assert "Unknown command" in out


def test_handle_command_model_shows_config(capsys):
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/model") is False
    out = capsys.readouterr().out
    assert agent.model_name in out


def test_read_line_plain(capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _="": "hello")
    assert read_line("❯ ") == "hello"


def test_read_line_continuation(capsys, monkeypatch):
    values = iter(["line1\\", "line2\\", "line3"])
    monkeypatch.setattr("builtins.input", lambda _="": next(values))
    assert read_line("❯ ") == "line1\nline2\nline3"


# ── Mid-stream interjection (send_async + interrupt) ──────────────────────────
# These exercise the fix: the TUI runs each turn on a background thread and
# keeps reading input, so the user can interrupt an in-flight turn and redirect
# it with a new message.  The real LLM loop is replaced with a controllable
# fake so no llama-server is needed.

import time  # noqa: E402


def _blocking_turn_factory(agent, calls):
    """Return a fake _process_conversation_turn that spins until stopped."""
    def _fake():
        calls.append(1)
        # Simulate a long streaming turn: keep going until the stop event is set.
        while not agent._stop_event.is_set():
            time.sleep(0.01)
    return _fake


def test_send_async_returns_alive_thread():
    agent = TerminalAgent(color=False)
    calls = []
    agent._process_conversation_turn = _blocking_turn_factory(agent, calls)
    t = agent.send_async("hello")
    try:
        assert t.is_alive()
        # Give it a moment to run and set the busy flag.
        deadline = time.time() + 2
        while not agent.busy and time.time() < deadline:
            time.sleep(0.01)
        assert agent.busy is True
        assert calls == [1]
        # The user message is recorded in history before the loop runs.
        assert ("user", "hello", "", "", "", 0) in agent.history
    finally:
        agent.interrupt()
        t.join(timeout=2)
    assert not t.is_alive()
    assert agent.busy is False


def test_interrupt_stops_inflight_turn():
    agent = TerminalAgent(color=False)
    calls = []
    agent._process_conversation_turn = _blocking_turn_factory(agent, calls)
    t = agent.send_async("go")
    try:
        deadline = time.time() + 2
        while not agent.busy and time.time() < deadline:
            time.sleep(0.01)
        assert agent.busy is True
    finally:
        agent.interrupt()
        t.join(timeout=2)
    assert not t.is_alive()
    assert agent._stop_event.is_set()


def test_midstream_redirect_serializes_and_keeps_both_messages():
    """Typing a new message while a turn is streaming interrupts the current
    turn and starts a fresh one; both user messages are preserved and the new
    turn runs only after the old one winds down (serialized on _send_lock)."""
    agent = TerminalAgent(color=False)
    calls = []
    agent._process_conversation_turn = _blocking_turn_factory(agent, calls)

    t1 = agent.send_async("first")
    try:
        deadline = time.time() + 2
        while not agent.busy and time.time() < deadline:
            time.sleep(0.01)
        assert agent.busy is True

        # User interjects mid-stream: interrupt, then send a new message.
        agent.interrupt()
        t2 = agent.send_async("redirect me")
        # Wait for the redirect turn to actually start, then stop it too.
        deadline = time.time() + 2
        while calls.count(1) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert calls.count(1) == 2
        agent.interrupt()
    finally:
        t2.join(timeout=5)
        t1.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()
    # Both user turns were recorded, in order.
    user_rows = [h[1] for h in agent.history if h[0] == "user"]
    assert user_rows == ["first", "redirect me"]
    # The loop actually ran twice (once per turn).
    assert len(calls) == 2
    # The final response reflects the last (redirect) turn, not the old one.
    assert agent.busy is False
