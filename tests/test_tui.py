"""Tests for the nbchat terminal UI (nbchat.tui).

These tests do NOT require a running llama-server: they exercise the
construction path, session bookkeeping, the colour palette and the
streaming output hooks (which write to stdout / capture state).
"""
from __future__ import annotations

import pytest
import httpx

from nbchat.tui import TerminalAgent, Palette, run  # noqa: F401  (run importable)
from nbchat.tui.agent import short_arg, _arg_hint
from nbchat.tui.colors import Palette as _Palette  # same object
from nbchat.tui.app import handle_command, read_line, last_turn_stats


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


# ── Session reasoning effort (/effort) ────────────────────────────────────────

class _Delta:
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _Choice:
    def __init__(self, content, finish_reason=None):
        self.finish_reason = finish_reason
        self.delta = _Delta(content)


class _TCFunc:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _TCCall:
    def __init__(self, name, arguments):
        self.index = 0
        self.id = f"call_{name}"
        self.type = "function"
        self.function = _TCFunc(name, arguments)


class _ChoiceTC:
    """A choice whose delta carries one tool-call (SDK shape)."""
    def __init__(self, name, arguments, finish_reason=None):
        self.finish_reason = finish_reason
        self.delta = _Delta(content=None, tool_calls=[
            _TCCall(name, arguments)])


class _ChunkTC:
    """A stream chunk carrying one tool-call delta."""
    def __init__(self, name, arguments, finish_reason=None):
        self.choices = [_ChoiceTC(name, arguments, finish_reason)]

class _Chunk:
    """Mimics an OpenAI stream chunk (empty choices == final usage chunk)."""
    def __init__(self, content=None, finish_reason=None):
        self.choices = ([_Choice(content, finish_reason)]
                        if content is not None else [])


class _FakeCompletions:
    def __init__(self, captured):
        self._captured = captured

    def create(self, **kwargs):
        self._captured.update(kwargs)
        # last content chunk carries the finish_reason; the trailing chunk
        # has empty choices (the stream_options include_usage shape).
        return [_Chunk("hello", finish_reason="stop"), _Chunk()]


class _FakeChat:
    def __init__(self, captured):
        self.completions = _FakeCompletions(captured)


class _FakeClient:
    def __init__(self, captured):
        self.chat = _FakeChat(captured)


def test_reasoning_effort_defaults_to_empty():
    agent = TerminalAgent(color=False)
    assert agent.reasoning_effort == ""


def test_effort_command_sets_level(capsys):
    agent = TerminalAgent(color=False)
    for level in ("none", "low", "medium", "xhigh"):
        assert handle_command(agent, f"/effort {level}") is False
        assert agent.reasoning_effort == level
    out = capsys.readouterr().out
    assert "set" in out and "medium" in out


def test_effort_command_no_arg_resets_to_default(capsys):
    agent = TerminalAgent(color=False)
    agent.reasoning_effort = "xhigh"
    assert handle_command(agent, "/effort") is False
    assert agent.reasoning_effort == ""
    out = capsys.readouterr().out
    assert "reset" in out


def test_effort_command_invalid_keeps_current(capsys):
    agent = TerminalAgent(color=False)
    agent.reasoning_effort = "low"
    assert handle_command(agent, "/effort max") is False
    assert agent.reasoning_effort == "low"  # unchanged
    out = capsys.readouterr().out
    assert "usage: /effort none|low|medium|xhigh" in out


def test_model_command_shows_current_effort(capsys):
    agent = TerminalAgent(color=False)
    handle_command(agent, "/model")
    out = capsys.readouterr().out
    assert "effort  (model default)" in out
    agent.reasoning_effort = "medium"
    handle_command(agent, "/model")
    out = capsys.readouterr().out
    assert "effort  medium" in out


def test_stream_response_sends_effort_when_set():
    agent = TerminalAgent(color=False)
    agent.reasoning_effort = "medium"
    captured = {}
    reasoning, content, tool_calls, finish = agent._stream_response(
        _FakeClient(captured), [{"role": "user", "content": "hi"}])
    assert captured.get("reasoning_effort") == "medium"
    assert content == "hello" and tool_calls is None and finish == "stop"


def test_stream_response_omits_effort_when_default():
    agent = TerminalAgent(color=False)
    captured = {}
    agent._stream_response(_FakeClient(captured),
                           [{"role": "user", "content": "hi"}])
    assert "reasoning_effort" not in captured


# ─── mid-stream transport errors: retry + continue nudge ───
# Regression (2026-09-02): the inference backend dropped the HTTP connection
# mid-stream ("peer closed connection without sending complete message
# body"); the exception escaped the turn and the user saw a reply cut off
# mid-sentence with no recovery.  The loop must instead route the partial
# content through the continuation path and retry the LLM call.


class _StreamingClient:
    """Client whose ``chat.completions.create`` yields scripted streams.

    Each scenario is a list of events: content strings (the final one
    carries ``finish_reason="stop"``), ``TC:<text>`` (a tool-call delta),
    or Exception instances (the stream dies there, mimicking an httpx
    transport error mid-body).
    """

    def __init__(self, scenarios):
        self.scenarios = list(scenarios)
        self.captured_calls = 0

    @property
    def chat(self):
        return self

    def __getattr__(self, name):
        if name == "completions":
            return self
        raise AttributeError(name)

    def create(self, **kwargs):
        self.captured_calls += 1
        if not self.scenarios:
            raise AssertionError("stream requested more times than scripted")
        return iter(_scripted_chunks(self.scenarios.pop(0)))


def _scripted_chunks(scenario):
    for i, ev in enumerate(scenario):
        if isinstance(ev, BaseException):
            raise ev
        if ev.startswith("TC:"):
            # "TC:<name>:<arguments>" -> a tool-call delta chunk.
            _name, _args = ev[3:].split(":", 1)
            yield _ChunkTC(_name, _args)
        else:
            yield _Chunk(content=ev,
                         finish_reason=("stop"
                                         if i == len(scenario) - 1
                                         else None))


def _stream_agent(client):
    from nbchat.tui.agent import TerminalAgent
    agent = TerminalAgent(color=False)
    # Capture agent notices without rendering them.
    agent._agent_notices = []
    agent._on_agent_message = lambda t: agent._agent_notices.append(t)
    return agent


def test_stream_drop_with_partial_content_is_retried():
    """A mid-stream transport error after content was rendered must not
    kill the turn: the partial reply is logged, a continue nudge is
    injected, and the LLM call is retried."""
    client = _StreamingClient([
        ["Partial reply ", "sent but the ", "connection died"],
        ["Recovered and finished."],
    ])
    client.scenarios[0].append(httpx.RemoteProtocolError("peer closed"))
    agent = _stream_agent(client)
    agent._run_conversation_loop(client)
    assert client.captured_calls == 2
    asst = [r for r in agent.history if r[0] == "assistant"]
    assert asst[0][1] == "Partial reply sent but the connection died"
    nudges = [r for r in agent.history if r[0] == "user"
              and "cut off mid-sentence" in r[1]]
    assert len(nudges) == 1
    assert any("stream interrupted" in n for n in agent._agent_notices)


def test_stream_drop_retries_bounded():
    """A stream that dies mid-content every time must not loop forever:
    after MAX_STREAM_RETRIES continue attempts the error propagates."""
    from nbchat.core import config as _config
    MAX_STREAM_RETRIES = _config.MAX_STREAM_RETRIES
    client = _StreamingClient([
        ["partial one"],
        ["partial two"],
        ["partial three"],
    ])
    for s in client.scenarios:
        s.append(httpx.RemoteProtocolError("peer closed"))
    agent = _stream_agent(client)
    with pytest.raises(Exception):
        agent._run_conversation_loop(client)
    assert client.captured_calls == MAX_STREAM_RETRIES + 1


def test_stream_drop_with_no_content_propagates():
    """No content rendered yet (the drop happened before any token):
    nothing to continue, so the error must propagate as before."""
    client = _StreamingClient([
        [httpx.RemoteProtocolError("peer closed")],
    ])
    agent = _stream_agent(client)
    with pytest.raises(Exception):
        agent._run_conversation_loop(client)
    assert client.captured_calls == 1


def test_stream_drop_after_tool_call_propagates():
    """A partial tool call cannot be trusted (it may be half-written):
    the error must propagate rather than be 'continued'."""
    client = _StreamingClient([
        ["TC:run_command:partial-args", httpx.RemoteProtocolError("peer closed")],
    ])
    agent = _stream_agent(client)
    with pytest.raises(Exception):
        agent._run_conversation_loop(client)
    assert client.captured_calls == 1

# ── /model speed stats (last 50 turns) ─────────────────────────────────────

_LINE = ("2026-09-02 10:{m:02d}:{s:02d},000 [INFO] Inference_Metrics: "
         "Latency: {lat:.2f}s | P:100 C:{c} T:{t}")


def _write_metrics(tmp_path, rows):
    path = tmp_path / "inference_metrics.log"
    path.write_text("\n".join(
        _LINE.format(m=m, s=s, lat=lat, c=c, t=100 + c)
        for m, s, lat, c in rows))
    return path


def _patch_log(tmp_path, monkeypatch):
    """Point the stats reader at the tmp log (chdir beats CWD lookup).

    When the tmp dir holds no metrics log, also make the lookup return
    None so the repo-root fallback can't leak the real log in.
    """
    monkeypatch.chdir(tmp_path)
    if not (tmp_path / "inference_metrics.log").exists():
        monkeypatch.setattr("nbchat.tui.app._metric_log_path", lambda: None)


def test_last_turn_stats_averages_last_n_turns(tmp_path, monkeypatch):
    # 5 turns, one LLM call each, 60s apart -> each is its own turn.
    rows = [(0, 0, 1.0, 100),   # 100 tok/s
            (1, 0, 2.0, 100),   # 50 tok/s  (60s gap)
            (2, 0, 1.0, 50),    # 50 tok/s
            (3, 0, 1.0, 200),   # 200 tok/s
            (4, 0, 2.0, 200)]   # 100 tok/s
    _write_metrics(tmp_path, rows)
    _patch_log(tmp_path, monkeypatch)
    out = last_turn_stats()
    assert out is not None
    # avg of (100, 50, 50, 200, 100) = 100 tok/s
    assert "100.0 tok/s" in out and "last 5 turn(s)" in out


def test_last_turn_stats_groups_calls_into_one_turn(tmp_path, monkeypatch):
    # Two calls 5s apart = ONE turn: 300 tokens over a 5s wall span
    # (span 5s exceeds the summed 3s of LLM latency).
    rows = [(0, 0, 1.0, 100), (0, 5, 2.0, 200)]
    _write_metrics(tmp_path, rows)
    _patch_log(tmp_path, monkeypatch)
    out = last_turn_stats()
    assert "60.0 tok/s" in out and "last 1 turn(s)" in out


def test_last_turn_stats_caps_at_50_turns(tmp_path, monkeypatch):
    # 60 turns, each exactly 60s apart -> not merged, capped at 50.
    rows = [(i, 0, 1.0, 100) for i in range(60)]
    _write_metrics(tmp_path, rows)
    _patch_log(tmp_path, monkeypatch)
    out = last_turn_stats()
    assert "last 50 turns" in out


def test_last_turn_stats_no_log_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr("nbchat.tui.app._metric_log_path",
                        lambda: tmp_path / "missing.log")
    assert last_turn_stats() is None


def test_model_command_prints_speed(capsys, tmp_path, monkeypatch):
    _write_metrics(tmp_path, [(0, 0, 1.0, 100)])
    _patch_log(tmp_path, monkeypatch)
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/model") is False
    out = capsys.readouterr().out
    assert "speed   avg 100.0 tok/s" in out


def test_model_command_speed_placeholder_when_no_data(capsys, tmp_path,
                                                      monkeypatch):
    _patch_log(tmp_path, monkeypatch)
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/model") is False
    out = capsys.readouterr().out
    assert "speed   - (no inference data yet)" in out


# ── Session-id resolution (bare vs prefixed) ─────────────────────────────
# Regression: the TUI shows and accepts bare session ids (e.g.
# "8ac30abd8aec") but rows are stored under the namespaced id
# ("tui:8ac30abd8aec"), so /load <bare-id> silently loaded zero rows.


@pytest.fixture()
def db_env(tmp_path, monkeypatch):
    """Point nbchat.core.db at a throwaway database."""
    from nbchat.core import db
    db_path = tmp_path / "test_chat_history.db"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    return db


def test_normalize_prefers_prefixed_twin(db_env):
    for _ in range(3):
        db_env.log_message("tui:abc123def456", "user", "hi")
    db_env.log_message("abc123def456", "user", "stray")
    assert db_env.normalize_session_id("abc123def456") == "tui:abc123def456"


def test_normalize_keeps_bare_when_no_twin(db_env):
    db_env.log_message("solo12345678", "user", "hi")
    assert db_env.normalize_session_id("solo12345678") == "solo12345678"


def test_normalize_passes_through_unknown(db_env):
    assert db_env.normalize_session_id("doesnotexist") == "doesnotexist"


def test_switch_session_accepts_bare_id(db_env):
    for i in range(3):
        db_env.log_message("tui:abc123def456", "user", f"msg {i}")
    agent = TerminalAgent(color=False)
    agent._switch_session("abc123def456")
    assert agent.session_id == "tui:abc123def456"
    assert len(agent.history) == 3


def test_load_command_bare_id_loads_history(db_env, capsys):
    for i in range(2):
        db_env.log_message("tui:abc123def456", "user", f"msg {i}")
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/load abc123def456") is False
    out = capsys.readouterr().out
    assert "Loaded session tui:abc123def456 (2 rows)" in out
    handle_command(agent, "/history")
    hist = capsys.readouterr().out
    assert "msg 0" in hist and "msg 1" in hist


def test_load_command_unknown_id_reports(db_env, capsys):
    agent = TerminalAgent(color=False)
    assert handle_command(agent, "/load no-such-id") is False
    out = capsys.readouterr().out
    assert "No such session: no-such-id" in out
