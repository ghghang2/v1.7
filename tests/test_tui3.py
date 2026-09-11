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


# -- Phase 3: /budget (cost / token tracking) ------------------------------
def test_budget_actual_tokens(monkeypatch, tmp_path):
    from nbchat.core import team_metrics as _tm
    _tm.reset_main_tokens()
    _tm.record_tokens(1000)
    _tm.record_tokens(250)
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:budget"
    out = app._cmd_budget("")
    assert "1250" in out
    assert "2 completions" in out
    assert "this session" in out
    assert "conversation size" in out


def test_budget_reset(monkeypatch, tmp_path):
    from nbchat.core import team_metrics as _tm
    _tm.reset_main_tokens()
    _tm.record_tokens(777)
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:budget_reset"
    out = app._cmd_budget("reset")
    assert "reset" in out
    # After reset, the actual tokens are zero
    out2 = app._cmd_budget("")
    assert "actual LLM tokens: 0" in out2


def test_budget_dispatch(monkeypatch, tmp_path):
    from nbchat.core import team_metrics as _tm
    _tm.reset_main_tokens()
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:budget_dispatch"
    notes = []
    app._note = lambda text: notes.append(text)
    app._ui_refresh = lambda: None
    app._run_command("/budget")
    assert len(notes) == 1
    assert "budget" in notes[0]
    assert "/budget" in Tui3ChatApp._TUI3_NATIVE


# -- Phase 4: /reflect (deep-agent plan loop - reflect step) ---------------
def test_reflect_busy(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:reflect_busy"
    app._turn_active = True  # busy
    out = app._cmd_reflect("")
    assert "wait" in out


def test_reflect_empty_history(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:reflect_empty"
    notes = []
    app._note = lambda text: notes.append(text)
    app._reflect_worker()
    assert any("no history" in n for n in notes)


def test_reflect_worker_transcript(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:reflect_worker"
    db.log_message(sid, "user", "please fix the failing test")
    db.log_tool_msg(sid, "t1", "run_tests", "cmd=pytest", "FAILED=1")
    db.log_message(sid, "assistant", "I fixed the failing assertion.")
    # Stub the LLM call to capture the prompt.
    captured = {}

    def fake_send(prompt):
        captured["prompt"] = prompt
        return "Reflection: (1) fixed the test, (2) nothing left, (3) done."

    app._send_side_question = fake_send
    app.session_id = sid
    notes = []
    app._note = lambda text: notes.append(text)
    app._reflect_worker()
    # The prompt includes the recent conversation.
    assert "please fix the failing test" in captured["prompt"]
    assert "run_tests" in captured["prompt"]
    # The reflection is appended to the log.
    assert any("Reflection:" in n for n in notes)


def test_reflect_dispatch_starts_thread(monkeypatch, tmp_path):
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:reflect_dispatch"
    # Stub the worker so no real LLM call happens.
    started = {"n": 0}

    def fake_worker():
        started["n"] += 1

    app._reflect_worker = fake_worker
    out = app._cmd_reflect("")
    assert "analyzing" in out
    import time as _time
    for _ in range(50):
        if started["n"] >= 1:
            break
        _time.sleep(0.02)
    assert started["n"] >= 1
    assert "/reflect" in Tui3ChatApp._TUI3_NATIVE


# -- Candidate A: /verify (verifier-driven process score) -----------------
def _seed_verify(db, sid):
    """Seed a session with JSON tool results (the real tool flow)."""
    db.log_message(sid, "user", "please run the test suite and fix failures")
    db.log_tool_msg(sid, "vt1", "run_command", "cmd=pytest",
                    '{"stdout": "collected 5", "stderr": "", "exit_code": 1}')
    db.log_tool_msg(sid, "vt2", "run_tests", "cmd=pytest",
                    '{"passed": 3, "failed": 2, "errors": 0, "output": "3 failed"}')
    db.log_message(sid, "assistant", "two tests are failing; fixing them now")


def test_verify_empty(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:verr"
    out = app._cmd_verify("")
    assert "no test/build/lint results" in out


def test_verify_run_tests_score(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vtests"
    _seed_verify(db, sid)
    app.session_id = sid
    out = app._cmd_verify("")
    # The MOST RECENT verifier is run_tests (3 passed, 2 failed) -> 60%
    assert "verifier score  60%" in out
    assert "source: run_tests" in out
    assert "passed 3" in out
    assert "failed 2" in out
    assert "NOT clean" in out


def test_verify_clean_run_tests(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vclean"
    db.log_message(sid, "user", "run the tests")
    db.log_tool_msg(sid, "vc1", "run_tests", "cmd=pytest",
                    '{"passed": 15, "failed": 0, "errors": 0, "output": "15 passed"}')
    app.session_id = sid
    out = app._cmd_verify("")
    assert "verifier score  100%" in out
    assert "clean - all checks passed" in out


def test_verify_run_command_exit_code(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vcmd"
    db.log_message(sid, "user", "build the project")
    db.log_tool_msg(sid, "vb1", "run_command", "cmd=make",
                    '{"stdout": "done", "stderr": "", "exit_code": 0}')
    app.session_id = sid
    out = app._cmd_verify("")
    assert "source: run_command" in out
    assert "verifier score  100%" in out
    assert "exit-code based" in out


def test_verify_pill_run_tests_failing(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vpill1"
    _seed_verify(db, sid)
    app.session_id = sid
    pill = app._verify_pill()
    # 2 failed + 0 errors -> "tests 2F"
    assert pill == "tests 2F"


def test_verify_pill_run_tests_clean(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vpill2"
    db.log_message(sid, "user", "run the tests")
    db.log_tool_msg(sid, "vp2", "run_tests", "cmd=pytest",
                    '{"passed": 15, "failed": 0, "errors": 0, "output": "15 passed"}')
    app.session_id = sid
    pill = app._verify_pill()
    assert pill == "tests 15/15"


def test_verify_pill_run_command(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vpill3"
    db.log_message(sid, "user", "build")
    db.log_tool_msg(sid, "vp3", "run_command", "cmd=make",
                    '{"stdout": "ok", "stderr": "", "exit_code": 0}')
    app.session_id = sid
    assert app._verify_pill() == "check ok"


def test_status_right_appends_verify_pill(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:vpill4"
    _seed_verify(db, sid)
    app.session_id = sid
    line = app._status_right()
    assert "tests 2F" in line


def test_verify_dispatch_intercepted(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    captured = []
    app._note = lambda text: captured.append(text)
    app._run_command("/verify")
    assert captured
    assert "verifier score" in captured[0] or "no test/build/lint" in captured[0]


# -- Candidate B: /health (objective long-run rot monitor) ----------------
def test_health_empty_ok(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:hempty"
    db.log_message(sid, "user", "hello")
    app.session_id = sid
    d = app._health_data()
    assert d["rot"] is False
    assert d["messages"] == 1
    assert d["turns"] == 1
    out = app._cmd_health("")
    assert "health  ok" in out


def test_health_rot_low_verifier(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:hrot1"
    db.log_message(sid, "user", "run the tests")
    # A run_tests result with a LOW pass rate (2 passed, 8 failed).
    db.log_tool_msg(sid, "hr1", "run_tests", "cmd=pytest",
                    '{"passed": 2, "failed": 8, "errors": 0, "output": "8 failed"}')
    app.session_id = sid
    d = app._health_data()
    assert d["rot"] is True
    assert d["verifier_score"] == 20
    assert any("verifier score" in r for r in d["rot_reasons"])
    out = app._cmd_health("")
    assert "ROT RISK" in out
    assert "verifier score 20%" in out


def test_health_rot_context_bloat(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:hrot2"
    # Seed > 200 messages (context bloat).
    for i in range(210):
        db.log_message(sid, "user" if i % 2 == 0 else "assistant",
                       "turn %d" % i)
    app.session_id = sid
    d = app._health_data()
    assert d["rot"] is True
    assert any("context bloat" in r for r in d["rot_reasons"])
    assert d["messages"] == 210


def test_health_rot_test_trend_down(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:hrot3"
    db.log_message(sid, "user", "run the tests")
    # First run: 80% pass rate. Second run: 40% (regressing).
    db.log_tool_msg(sid, "ht1", "run_tests", "cmd=pytest",
                    '{"passed": 4, "failed": 1, "errors": 0}')
    db.log_tool_msg(sid, "ht2", "run_tests", "cmd=pytest",
                    '{"passed": 2, "failed": 3, "errors": 0}')
    app.session_id = sid
    d = app._health_data()
    assert d["test_trend"] == "down"
    assert d["rot"] is True
    assert any("test trend regressing" in r for r in d["rot_reasons"])


def test_health_pill_only_when_rot(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    # Healthy session: no pill.
    sid = "tui:hpill1"
    db.log_message(sid, "user", "hello")
    app.session_id = sid
    assert app._health_pill() == ""
    # Clear the pill cache before switching sessions (the cache is ~0.5 s).
    app._health_pill_cache = None
    # Rotting session (low verifier): a pill.
    sid2 = "tui:hpill2"
    db.log_message(sid2, "user", "run the tests")
    db.log_tool_msg(sid2, "hp2", "run_tests", "cmd=pytest",
                    '{"passed": 1, "failed": 9, "errors": 0}')
    app.session_id = sid2
    assert app._health_pill() == "ROT 10%"


def test_status_right_appends_health_pill(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:hpill3"
    db.log_message(sid, "user", "run the tests")
    db.log_tool_msg(sid, "hp3", "run_tests", "cmd=pytest",
                    '{"passed": 1, "failed": 9, "errors": 0}')
    app.session_id = sid
    line = app._status_right()
    # Both the verifier pill (tests 9F) AND the health pill (ROT 10%) appear.
    assert "tests 9F" in line
    assert "ROT 10%" in line


# -- Candidate C: /audit (provenance / audit panel) -----------------------
def test_audit_empty(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    app.session_id = "tui:aempty"
    out = app._cmd_audit("")
    assert "no history" in out


def test_audit_classification_counts(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:acounts"
    db.log_message(sid, "user", "run the tests and build")
    # A verified claim (clean tests).
    db.log_tool_msg(sid, "ac1", "run_tests", "cmd=pytest",
                    '{"passed": 10, "failed": 0, "errors": 0}')
    # A relayed claim (a text summary, no objective check).
    db.log_tool_msg(sid, "ac2", "repo_overview", "path=.", "12 files, 3 dirs")
    # An unverified claim (a failed test run).
    db.log_tool_msg(sid, "ac3", "run_tests", "cmd=pytest",
                    '{"passed": 3, "failed": 7, "errors": 0}')
    # An unverified claim (a failing command, non-zero exit code).
    db.log_tool_msg(sid, "ac4", "run_command", "cmd=make",
                    '{"exit_code": 2, "output": "make failed"}')
    app.session_id = sid
    out = app._cmd_audit("")
    assert "provenance  4 tool calls" in out
    assert "verified   1" in out
    assert "relayed    1" in out
    assert "unverified 2" in out


def test_audit_relayed_only_note(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:arelayed"
    db.log_message(sid, "user", "look at the repo")
    db.log_tool_msg(sid, "ar1", "repo_overview", "path=.", "5 files")
    app.session_id = sid
    out = app._cmd_audit("")
    assert "relayed    1" in out
    assert "every claim is relayed" in out


def test_audit_unverified_note(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    sid = "tui:aunv"
    db.log_message(sid, "user", "run the tests")
    db.log_tool_msg(sid, "au1", "run_tests", "cmd=pytest",
                    '{"passed": 1, "failed": 9, "errors": 0}')
    app.session_id = sid
    out = app._cmd_audit("")
    assert "unverified 1" in out
    assert "1 unverified claim" in out


def test_audit_dispatch_intercepted(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    captured = []
    app._note = lambda text: captured.append(text)
    app.session_id = "tui:adispatch"
    app._run_command("/audit")
    assert captured
    assert "audit" in captured[0]


# -- Candidate D: /profile (evolvable harness profile + user gate) --------
def test_profile_show_default(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    out = app._cmd_profile("")
    assert "profile (applied)" in out
    assert "prompt_template" in out
    assert "verification_rules" in out
    assert "memory_policy" in out


def test_profile_set_stages_not_applies(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    # Stage an edit (should NOT apply it).
    out = app._cmd_profile("set memory_policy compact")
    assert "STAGED" in out
    assert "memory_policy" in out
    # The applied profile is unchanged (still the default).
    applied = app._load_profile()
    assert applied["memory_policy"] == "auto"
    # The staged profile has the new value.
    staged = app._load_staged()
    assert staged["memory_policy"] == "compact"
    # The applied file is NOT written (only the staged file).
    import os as _os
    assert not _os.path.exists(p)
    assert _os.path.exists(p + ".staged")


def test_profile_diff_shows_changes(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    app._cmd_profile("set memory_policy compact")
    out = app._cmd_profile("diff")
    assert "staged changes" in out
    assert "memory_policy" in out
    assert "auto" in out
    assert "compact" in out


def test_profile_apply_applies_and_clears(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    app._cmd_profile("set memory_policy compact")
    out = app._cmd_profile("apply")
    assert "APPLIED" in out
    # The applied profile now has the new value.
    applied = app._load_profile()
    assert applied["memory_policy"] == "compact"
    # The staged file is cleared.
    import os as _os
    assert not _os.path.exists(p + ".staged")
    # The applied file now exists.
    assert _os.path.exists(p)


def test_profile_discard_clears_staged(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    app._cmd_profile("set memory_policy compact")
    out = app._cmd_profile("discard")
    assert "discarded" in out
    import os as _os
    assert not _os.path.exists(p + ".staged")
    # The applied profile is unchanged.
    applied = app._load_profile()
    assert applied["memory_policy"] == "auto"


def test_profile_set_rejects_unknown_key(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    out = app._cmd_profile("set bogus_key value")
    assert "unknown key" in out


def test_profile_dispatch_intercepted(monkeypatch, tmp_path):
    import nbchat.core.db as db
    app, term, events = _make_tui3_app(monkeypatch, tmp_path)
    p = str(tmp_path / "profile.json")
    monkeypatch.setenv("NBCHAT_TUI3_PROFILE", p)
    captured = []
    app._note = lambda text: captured.append(text)
    app._run_command("/profile")
    assert captured
    assert "profile (applied)" in captured[0]
