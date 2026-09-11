"""Tests for the tui3 version (the tui2 base extended with the tui3 wave).

The tui3 feature wave is tested here, separately from the tui2 suite, so the
version-to-feature-set boundary is explicit.  Phase 1: ``/trace`` (the live
task-trace / observability view).
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from nbchat.tui2 import EventQueue, RawTerminal
from nbchat.tui2.app import ChatApp as Tui2ChatApp
from nbchat.tui3 import VERSION
from nbchat.tui3.app import ChatApp as Tui3ChatApp


def _make_tui3_app(monkeypatch, tmp_path):
    """A tui3 ChatApp on a passthrough terminal with a private DB path."""
    import nbchat.core.db as db
    db_path = str(tmp_path / "tui3_test.db")
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db()
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True
    term.width, term.height = 80, 24
    events = EventQueue()
    app = Tui3ChatApp(term, events, resume_last=False)
    return app, term, events


def _seed(db, sid):
    db.log_message(sid, "user", "please run the test suite")
    db.log_tool_msg(sid, "t1", "run_command", "cmd=pytest", "exit 0, 12 passed")
    db.log_tool_msg(sid, "t2", "run_tests", "cmd=pytest", "FAILED=3 ERRORS=1")
    db.log_message(sid, "assistant", "three tests failed; here is the summary")
    db.log_tool_msg(sid, "t3", "run_command", "cmd=pytest", "exit 0, 15 passed")
    db.log_message(sid, "assistant", "all 15 tests now pass")


# -- version + delineation -------------------------------------------------
def test_tui3_version():
    assert VERSION == "tui3"


def test_tui3_chatapp_subclasses_tui2():
    assert issubclass(Tui3ChatApp, Tui2ChatApp)
    assert Tui3ChatApp is not Tui2ChatApp


def test_tui3_native_commands_present():
    assert "/trace" in Tui3ChatApp._TUI3_NATIVE
    # /trace is NOT a tui2-native command (it is a tui3 addition)
    assert "/trace" not in Tui2ChatApp._TUI2_NATIVE


# -- Phase 1: /trace -------------------------------------------------------
def test_cmd_trace_empty(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:empty"
    out = app._cmd_trace("")
    assert "no history" in out


def test_cmd_trace_basic_summary(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:basic"
    _seed(db, sid)
    app.session_id = sid
    out = app._cmd_trace("")
    # Summary counts: 1 user, 2 assistant, 3 tool, 1 error
    assert "1 you" in out
    assert "2 nbchat" in out
    assert "3 tools" in out
    assert "1 err" in out
    # Tool rows appear with their names
    assert "run_command" in out
    assert "run_tests" in out
    # The errored tool is marked
    assert "[ERR]" in out


def test_cmd_trace_errors_filter(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:errfilter"
    _seed(db, sid)
    app.session_id = sid
    out = app._cmd_trace("errors")
    assert "(filter: errors)" in out
    # Only the errored tool is shown (the run_tests failure)
    lines = out.split("\n")
    err_lines = [ln for ln in lines if "[ERR]" in ln]
    assert len(err_lines) == 1
    assert "run_tests" in err_lines[0]
    # The successful run_command must NOT be in the filtered view
    assert "exit 0, 15 passed" not in out


def test_cmd_trace_tools_filter(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:toolsfilter"
    _seed(db, sid)
    app.session_id = sid
    out = app._cmd_trace("tools")
    assert "(filter: tools)" in out
    rows = [ln for ln in out.split("\n") if ln and not ln.startswith("trace ")]
    # All 3 tool calls appear, and every row is a tool row
    assert len(rows) == 3
    assert all(ln.startswith("tool ") for ln in rows)


def test_cmd_trace_limit(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:limit"
    _seed(db, sid)
    app.session_id = sid
    out = app._cmd_trace("2")
    lines = [ln for ln in out.split("\n") if ln and not ln.startswith("trace ")]
    # Exactly the last 2 steps
    assert len(lines) == 2


# -- command dispatch interception ----------------------------------------
def test_run_command_intercepts_trace(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:intercept"
    _seed(db, sid)
    app.session_id = sid
    # _run_command should route /trace to the tui3 handler (a note is added
    # to the log); it must NOT fall through to the v1 handle_command.
    notes = []
    app._note = lambda text: notes.append(text)
    app._ui_refresh = lambda: None
    app._run_command("/trace")
    assert len(notes) == 1
    assert "trace" in notes[0]
    assert "1 you" in notes[0]


def test_run_command_passes_through_other(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:passthrough"
    called = {"super": False}

    def fake_super(self, line):
        called["super"] = True

    # Monkeypatch the parent _run_command to detect fall-through.
    Tui3ChatApp._run_command_orig = Tui2ChatApp._run_command
    try:
        # Replace the parent dispatch with a sentinel to prove /help is NOT
        # intercepted by tui3 (it must fall through to tui2/v1).
        orig = Tui2ChatApp._run_command
        Tui2ChatApp._run_command = fake_super
        try:
            app._run_command("/help")
        finally:
            Tui2ChatApp._run_command = orig
        assert called["super"]
    finally:
        Tui3ChatApp._run_command_orig = None


# -- Phase 2: approval diff-preview (HITL upgrade) ------------------------
def _approval_text(app, width=80):
    """Render the approval modal and return its joined plain text."""
    fr = app._approval_lines(width)
    return "\n".join(ln.text for ln in fr)


def test_approval_lines_with_description(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app._approval = {"tool": "run_command", "args": "{\"command\": \"pytest tests/ -q\"}"}
    txt = _approval_text(app)
    # The registry description of run_command is surfaced
    assert "Execute a shell command" in txt
    # The preview surfaces the command
    assert "pytest tests/ -q" in txt
    # The answer prompt is present
    assert "y approve" in txt


def test_approval_lines_preview_path(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app._approval = {"tool": "create_file", "args": "{\"path\": \"src/new.py\"}"}
    txt = _approval_text(app)
    # The preview surfaces the file path
    assert "src/new.py" in txt


def test_approval_lines_unknown_tool_fallback(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app._approval = {"tool": "mystery_tool", "args": "{\"x\": 1}"}
    txt = _approval_text(app)
    # The raw tool name is shown (no registry description), args as a blob
    assert "mystery_tool" in txt
    assert "x" in txt
    assert "y approve" in txt


def test_approval_lines_no_args(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app._approval = {"tool": "run_command", "args": ""}
    txt = _approval_text(app)
    assert "(no args)" in txt


def test_tool_description_lookup(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    assert "shell command" in app._tool_description("run_command").lower()
    # Unknown tool returns empty
    assert app._tool_description("no_such_tool") == ""


def test_tool_preview_command(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    assert app._tool_preview("run_command", "{\"command\": \"ls -la\"}") == "ls -la"


def test_tool_preview_path(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    assert app._tool_preview("create_file", "{\"path\": \"a/b.py\"}") == "a/b.py"


def test_tool_preview_falls_back_to_raw(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    # A dict with none of the known keys falls back to the raw (truncated) string
    out = app._tool_preview("send_email", "{\"to\": \"a@b.c\", \"subject\": \"hi\"}")
    assert "a@b.c" in out
    # Non-JSON args fall back to the raw string
    assert app._tool_preview("x", "plain text arg") == "plain text arg"


def test_approval_lines_is_tui3_override(monkeypatch, tmp_path):
    from nbchat.tui2.app import ChatApp as Tui2ChatApp
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    # The tui3 override is a distinct method (not the tui2 base method)
    assert Tui3ChatApp._approval_lines is not Tui2ChatApp._approval_lines
