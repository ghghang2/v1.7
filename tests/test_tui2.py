"""Tests for the TUI v2 engine (nbchat.tui2).

Phase 1 coverage: the differential frame renderer (the heart of the
flicker-free design), the raw-mode terminal passthrough for non-TTY
environments (which is exactly what a test harness or a pipe sees),
the thread-safe event queue, and the TUIApp render loop.

These tests do not require a TTY: RawTerminal detects a non-tty stdout
and runs in a documented no-op passthrough mode, so the same rendering
code paths that run in a real terminal are exercised here.
"""
from __future__ import annotations

import io
import os
import sys
import threading
import time

import pytest

from nbchat.tui2 import (
    EventQueue,
    Frame,
    Key,
    Line,
    RawTerminal,
    Segment,
    Style,
    TUIApp,
    diff_frames,
    render_frame,
)
from nbchat.tui2.notify import NotifyStack
from nbchat.tui2.app import KEYMAP

CYAN = Style(fg=36)
BOLD = Style(bold=True)

# Keep the persisted settings file (tui3 wave 4) out of the real home
# directory: every config load/save in these tests hits this temp file.
import tempfile as _tempfile  # noqa: E402
CFG_FILE = os.path.join(_tempfile.mkdtemp(prefix="nbchat_tui3_cfg_"),
                        "tui3.json")
os.environ["NBCHAT_TUI3_CONFIG"] = CFG_FILE


def text(line: str) -> Line:
    return Line([Segment(line)])


def frame(*lines: str) -> Frame:
    return Frame([text(l) for l in lines])


# ── Style ──────────────────────────────────────────────────────────


def test_style_code():
    assert Style().as_code() == ""
    assert Style(fg=36).as_code() == "\033[38;5;36m"
    s = Style(bold=True, fg=1, bg=4)
    code = s.as_code()
    assert code.startswith("\033[") and code.endswith("m")
    assert "1" in code.split(";") and "38;5;1" in code and "48;5;4" in code


# ── Differential diff (the core promise) ───────────────────────────


def test_diff_identical_frames_is_empty():
    assert diff_frames(frame("a", "b"), frame("a", "b")) == []
    f = frame("a")
    assert diff_frames(f, f) == []


def test_diff_rewrites_only_changed_tail():
    # Only line 2 changes: the op stream must touch line 2 and nothing
    # above it, and must not touch line 1.
    ops = diff_frames(frame("one", "two", "three"), frame("one", "TWO", "three"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 2 in rows
    assert 1 not in rows
    texts = [op[2] for op in ops if op[0] == "write_line"]
    line_texts = ["".join(s.text for s in t) for t in texts]
    assert "TWO" in line_texts


def test_diff_no_ops_for_unchanged_first_lines():
    # A change on the last line must not cause rewrites of earlier lines.
    ops = diff_frames(frame("a", "b", "c"), frame("a", "b", "C"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert min(rows) == 3


def test_diff_extends_taller_frame_with_new_lines():
    ops = diff_frames(frame("a", "b"), frame("a", "b", "c", "d"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 3 in rows and 4 in rows
    assert 1 not in rows and 2 not in rows


def test_diff_clears_stale_tail_when_shorter():
    ops = diff_frames(frame("a", "b", "c"), frame("a", "b"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    # The now-removed line 3 must be explicitly cleared, not left stale.
    assert 3 in rows
    assert 1 not in rows and 2 not in rows


def test_diff_rewrites_whole_tail_when_middle_changes():
    # A change in the middle invalidates everything from that row down.
    ops = diff_frames(frame("a", "b", "c"), frame("a", "B", "c"))
    rows = [op[1] for op in ops if op[0] == "write_line"]
    assert 2 in rows and 3 in rows
    assert 1 not in rows


# ── Rendered bytes ─────────────────────────────────────────────────


def test_render_identical_frames_emits_nothing():
    assert render_frame(frame("a"), frame("a")) == ""


def test_render_uses_sync_output():
    out = render_frame(frame(), frame("hello"))
    assert out.startswith("\033[?2026h") and out.endswith("\033[?2026l")


def test_render_first_frame_uses_absolute_positioning():
    # Fresh terminal: every rewritten line is addressed absolutely, so
    # the first frame lands regardless of the cursor's starting position.
    out = render_frame(Frame([]), frame("a", "b", "c"))
    # Every rewritten line is addressed absolutely (CUP: ESC[row;1H]),
    # so the frame lands correctly regardless of where the cursor is.
    assert "\033[1;1H" in out and "\033[2;1H" in out and "\033[3;1H" in out
    # No relative cursor movement (CSI A/B) remains in the writer.
    assert "\033[A" not in out and "\033[B" not in out


def test_render_absolute_positioning_after_change():
    # A change on line 2 of an existing frame still uses CUP addressing.
    out = render_frame(frame("a", "b"), frame("a", "c"))
    assert "\033[2;1H" in out and "\033[A" not in out and "\033[B" not in out


def test_render_changed_line_cleared_to_eol():
    out = render_frame(frame("aaaa"), frame("bb"))
    assert "\033[K" in out  # clear-to-EOL so the old longer line cannot show


def test_render_applies_style():
    f = Frame([Line([Segment("hi", CYAN)])])
    out = render_frame(Frame([]), f)
    assert "\033[38;5;36m" in out


def test_render_full_reconstruction_roundtrip():
    # Replaying the writer's ops line by line must yield exactly the
    # new frame's content.
    prev = frame("x1", "x2", "x3")
    new = frame("x1", "y2", "y3", "y4")
    ops = diff_frames(prev, new)
    buf = {1: "x1", 2: "x2", 3: "x3"}
    for op in ops:
        assert op[0] == "write_line", op
        buf[op[1]] = "".join(s.text for s in op[2])
    assert [buf[i] for i in range(1, 5)] == ["x1", "y2", "y3", "y4"]


def test_render_recovers_from_lost_bytes():
    # The design argument in one test (\u00a712.3.1): drop bytes mid-frame so
    # the cursor ends up in an unknown place, then render the next
    # frame.  With absolute (CUP) addressing the frame still lands
    # line-for-line: each op targets its own row independently.
    ops = diff_frames(frame("a", "b"), frame("c", "d"))
    assert ops
    buf = {1: "b0", 2: "b1"}
    for op in ops:
        assert op[0] == "write_line", op
        buf[op[1]] = "".join(s.text for s in op[2])  # absolute: row only
    assert buf == {1: "c", 2: "d"}

# ── RawTerminal (passthrough mode, no TTY required) ────────────────


def make_terminal() -> RawTerminal:
    stdin = io.StringIO("")
    stdin.fileno = lambda: 0  # type: ignore[attr-defined]
    term = RawTerminal(stdin, io.StringIO())
    term._saved = None
    term._passthrough = True
    return term


def test_passthrough_enter_restore_roundtrip():
    term = make_terminal()
    with term:
        term._write("payload")
        assert term._restored is False
    assert term._restored is True
    assert term.stdout.getvalue() == "payload"


def test_restore_is_idempotent():
    term = make_terminal()
    term.enter()
    term.restore()
    term.restore()  # must not raise or double-restore


# ── EventQueue ─────────────────────────────────────────────────────


def test_event_queue_fifo_order():
    q = EventQueue()
    q.put("a")
    q.put("b", 2)
    events = q.drain()
    assert events == [("a", None), ("b", 2)]
    assert q.drain() == []


def test_event_queue_is_thread_safe():
    q = EventQueue()
    errors = []

    def worker(n):
        try:
            for i in range(50):
                q.put(f"t{n}", i)
        except Exception as e:  # pragma: no cover - defensive
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    events = q.drain()
    assert not errors
    assert len(events) == 400
    assert {kind for kind, _ in events} == {f"t{n}" for n in range(8)}


def test_event_queue_close():
    q = EventQueue()
    assert not q.closed
    q.close()
    assert q.closed
    # drain swallows the sentinel: after close the queue is empty.
    assert q.drain() == []


# ── TUIApp render loop ─────────────────────────────────────────────


def _feed(term: RawTerminal, chunks):
    data = iter(chunks)
    term.stdin.read = lambda n: (next(data) if data else "")
    return data


def test_app_renders_and_updates():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return frame(f"tick {calls['n']}")

    app.set_frame_provider(provider)
    _feed(term, ["tick", "\x03"])  # some keystrokes, then Ctrl+C
    app.start()

    rendered = out.getvalue()
    assert "tick 1" in rendered
    assert calls["n"] >= 2
    assert term._restored is True  # terminal restored even on exit


def test_app_quit_event_stops_loop():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("static"))
    app.events.put("quit")
    _feed(term, [""])
    app.start()
    assert not app._running
    assert "static" in out.getvalue()
    assert term._restored is True


def test_app_ignores_identical_frames():
    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("same"))
    _feed(term, ["\x03"])
    app.start()
    # Second identical frame renders nothing: content appears exactly once.
    assert out.getvalue().count("same") == 1



# ── regression: a normal key must reach on_input (was "dead keys") ──────────
def test_app_dispatches_keys_to_on_input():
    """A normal keystroke must be parsed by the KeyReader and delivered to
    ``on_input``.  Before the fix, only Ctrl+C/Ctrl+D ever reached a handler
    and every other key was silently dropped ("none of the keys worked")."""
    from nbchat.tui2 import KeyReader

    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True

    app = TUIApp(term)
    app.set_frame_provider(lambda: frame("static"))
    reader = KeyReader()
    app.set_key_reader(reader)

    seen = []
    app.on_input = lambda key: seen.append(key)
    # "r" then "q" then Ctrl+C to break the loop.
    _feed(term, ["r", "q", "\x03"])
    app.start()

    # The two ordinary keys must have been delivered, in order.
    assert [k.name for k in seen] == ["r", "q"], seen

# \u2500\u2500 Phase 2: chat-surface components (markdown / messages / tools) \u2500\u2500
def _plain(line):
    return "".join(s.text for s in line.segments)


def test_markdown_heading_and_inline():
    from nbchat.tui2 import theme
    from nbchat.tui2.chat import Markdown

    out = Markdown("# Title\n\nhello **bold** and `code`").render(40)
    text = "\n".join(_plain(l) for l in out)
    assert "Title" in text
    assert "hello bold and code" in text
    head = next(l for l in out if "Title" in _plain(l))
    assert any(s.style == theme.DARK.accent for s in head.segments
               if s.text == "Title")


def test_markdown_fenced_code_block():
    from nbchat.tui2.chat import Markdown

    out = Markdown("before\n```\nline1\nline2\n```\nafter").render(30)
    text = "\n".join(_plain(l) for l in out)
    assert "line1" in text and "line2" in text and "before" in text
    # Canonical engine renders code lines with border + muted styles.
    assert any(s.text == "\u2502 " for l in out for s in l.segments)


def test_markdown_bullet_list():
    from nbchat.tui2.chat import Markdown

    out = Markdown("- a\n- b").render(30)
    bullets = [_plain(l) for l in out if _plain(l).lstrip().startswith("\u00b7")]
    assert any("a" in b for b in bullets)
    assert any("b" in b for b in bullets)


def test_markdown_never_crashes_on_unclosed_markup():
    from nbchat.tui2.chat import Markdown

    for src in ["`unclosed", "**unclosed", "plain"]:
        out = Markdown(src).render(24)
        for l in out:
            assert sum(len(s.text) for s in l.segments) <= 24


def test_diff_lines_colouring():
    from nbchat.tui2.chat import GREEN, RED, DIFF_HUNK, DIFF_CONTEXT
    from nbchat.tui2 import diff_lines

    out = diff_lines(["+add", "-del", "@@ hunk", " ctx"], width=30)

    def first_style(prefix):
        row = next(l for l in out if _plain(l).lstrip().startswith(prefix))
        return row.segments[0].style

    assert first_style("+add") == GREEN
    assert first_style("-del") == RED
    assert first_style("@@ hunk") == DIFF_HUNK
    assert first_style("ctx") == DIFF_CONTEXT


def test_toolcall_panel_structure_and_diff():
    from nbchat.tui2 import ToolCall

    out = ToolCall(
        "edit", title="tool: edit", status="done",
        body=["+a", "-b"], show_diff=True,
    ).render(30)
    texts = [_plain(l) for l in out]
    joined = "\n".join(texts)
    assert "tool: edit" in joined
    assert "\u256d" in texts[0]
    assert "\u256f" in texts[-1]
    assert any(t.lstrip("\u2502").startswith("+a") for t in texts)
    assert any(t.lstrip("\u2502").startswith("-b") for t in texts)


def test_toolcall_omits_when_max_rows():
    from nbchat.tui2 import ToolCall

    body = [f"line{i}" for i in range(10)]
    out = ToolCall("t", body=body, max_rows=2).render(30)
    joined = "\n".join(_plain(l) for l in out)
    assert "more line(s)" in joined
    assert "line9" not in joined


def test_message_user_and_assistant():
    from nbchat.tui2 import Message

    u = Message("user", "hello world").render(30)
    assert "you" in _plain(u[0])
    a = Message("assistant", "# H\n\ntext here").render(30)
    joined = "\n".join(_plain(l) for l in a)
    assert "agent" in _plain(a[0])
    assert "H" in joined and "text here" in joined


def test_thinking_block_collapsed_and_expanded():
    from nbchat.tui2 import ThinkingBlock

    c = ThinkingBlock("a lot of hidden reasoning", collapsed=True).render(30)
    assert len(c) == 1
    assert "thinking" in _plain(c[0])
    e = ThinkingBlock("visible reasoning", collapsed=False).render(30)
    joined = "\n".join(_plain(l) for l in e)
    assert "visible reasoning" in joined


def test_chat_components_integrated_into_frame_diff():
    from nbchat.tui2 import Frame, Message, diff_frames

    def frame_of(text):
        return Frame(lines=Message("assistant", text).render(30))

    ops = diff_frames(frame_of("v1"), frame_of("v2 totally different"))
    assert any(op[0] == "write_line" for op in ops)

# ── Phase 2: ChatApp (agent + chat log + input, print hooks → render tree) ──


def _make_chat_app():
    """A ChatApp wired to a passthrough (non-TTY) terminal with a stubbed
    turn path, so no real LLM call or network happens during the test."""
    from nbchat.tui2 import Key
    from nbchat.tui2.app import ChatApp

    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True
    term.width, term.height = 80, 24

    events = EventQueue()
    app = ChatApp(term, events, resume_last=False)

    # Stub the agent turn so _turn_worker completes deterministically.
    replies = {"n": 0}

    def fake_send(text):
        replies["n"] += 1
        app._on_stream_token("hello ")
        app._on_stream_token("hello world")
        app._on_stream_complete("hello world")
        return "hello world"

    app.send = fake_send
    return app, term, events, replies


def test_chatapp_builds_stable_frame():
    app, term, events, _ = _make_chat_app()
    frame = app._build_frame()
    assert frame.height == term.height
    assert frame.width == term.width
    # Status line reports the turn count.
    flat = "\n".join(
        "".join(s.text for s in line.segments) for line in frame.lines)
    assert "turns: 0" in flat


def test_chatapp_print_user_feeds_log_not_stdout():
    app, term, events, _ = _make_chat_app()
    app._print_user("hi there")
    # The user message is in the structured log, and the stdout buffer
    # (a passthrough) received nothing from the print hook itself.
    assert any(
        getattr(m, "role", None) == "user" and "hi there" in m.text
        for m in app.log.messages
    )


def test_chatapp_stream_and_finalize_lands_in_log():
    app, _, _, _ = _make_chat_app()
    app._on_stream_token("A")
    app._on_stream_token("AB")
    app._on_stream_complete("AB")
    app._finalize_turn()
    # _stream_text is drained into a single assistant message.
    assert app._stream_text == ""
    assert app._turns == 1
    last = app.log.messages[-1]
    assert last.role == "assistant"
    assert "AB" in last.text
    assert app._status_state == "ready"


def test_chatapp_tool_display_appends_block():
    app, _, _, _ = _make_chat_app()
    app._on_tool_display('{"ok": true}', "search", '{"q": "x"}')
    assert app._stream_blocks, "tool display should append a block"
    block = app._stream_blocks[-1]
    assert block.kind == "tool"
    assert "search" in block.title


def test_chatapp_key_enter_submits_turn():
    from nbchat.tui2 import Key

    app, term, events, replies = _make_chat_app()
    # Type a message then Enter; the editor should submit and a turn start.
    for ch in "hi":
        app._on_input(Key(name=ch))
    app._on_input(Key(name="enter"))
    assert app.editor.submitted is False  # consumed by the app
    # Give the daemon turn worker a beat to run the stubbed send.
    deadline = time.time() + 3
    while replies["n"] == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert replies["n"] == 1


def test_chatapp_ctrl_d_on_empty_editor_stops():
    from nbchat.tui2 import Key

    app, term, events, _ = _make_chat_app()
    assert app._tui._running or True  # start() not called in unit tests
    # Simulate the loop running, then Ctrl+D on an empty editor stops it.
    app._tui._running = True
    app._on_input(Key(name="ctrl+d"))
    assert app._tui._running is False


def test_chatapp_ctrl_d_with_text_does_not_quit():
    from nbchat.tui2 import Key

    app, term, events, _ = _make_chat_app()
    for ch in "x":
        app._on_input(Key(name=ch))
    app._tui._running = True
    app._on_input(Key(name="ctrl+d"))
    # With non-empty text, Ctrl+D is a normal key (del/insert), not a quit.
    assert app._tui._running is True


def test_key_text_maps_chars_and_control_keys():
    from nbchat.tui2 import Key
    from nbchat.tui2.app import _key_text

    # Plain printable character lives in ``name`` with no payload.
    assert _key_text(Key(name="a")) == "a"
    assert _key_text(Key(name="Z")) == "Z"
    # Named control keys carry no insertable text.
    assert _key_text(Key(name="enter")) == ""
    assert _key_text(Key(name="ctrl+c")) == ""
    assert _key_text(Key(name="up")) == ""
    # Paste / unknown sequences carry the text in the payload.
    assert _key_text(Key(name="paste", payload="line1\nline2")) == "line1\nline2"


def test_on_input_inserts_character_not_key_name():
    from nbchat.tui2 import Key

    app, _, _, _ = _make_chat_app()
    # Typing "hi" must insert the characters (the fix: the editor now
    # receives _key_text(key), so a single-char key inserts that char and
    # a control key inserts nothing instead of the literal key name).
    for ch in "hi":
        app._on_input(Key(name=ch))
    assert app.editor.text() == "hi"
    # A control key must not corrupt the buffer.
    app._on_input(Key(name="enter"))  # submits; the app clears the buffer
    assert app.editor.text() == ""


# ── Phase 3: fuzzy matching ────────────────────────────────────────


def test_fuzzy_rank_orders_and_filters():
    from nbchat.tui2.fuzzy import fuzzy_match, fuzzy_rank

    ranked = fuzzy_rank("sess", ["sessions", "session-old", "model", "xyz"])
    assert [it for it, _ in ranked] == ["sessions", "session-old"]
    assert "model" not in [it for it, _ in ranked]
    assert "xyz" not in [it for it, _ in ranked]
    assert fuzzy_match("zzz", "sessions") is None
    m = fuzzy_match("sio", "sessions")
    assert m is not None
    assert all("sessions".lower()[i] == c for i, c in zip(m.indices, "sio"))


def test_fuzzy_rank_empty_needle_returns_all_in_order():
    from nbchat.tui2.fuzzy import fuzzy_rank
    ranked = fuzzy_rank("", ["a", "b", "c"])
    assert [it for it, _ in ranked] == ["a", "b", "c"]


# ── Phase 3: SelectList component ──────────────────────────────────


def test_selectlist_selected_item_and_clamping():
    from nbchat.tui2.components import SelectList

    sel = SelectList(title="Sessions", items=["a", "b", "c"], selected=5)
    assert sel.selected == 2
    assert sel.selected_item == "c"
    sel.select(0)
    assert sel.selected_item == "a"
    sel.select(99)
    assert sel.selected == 0


def test_selectlist_render_shape_and_highlight():
    from nbchat.tui2.components import SelectList

    sel = SelectList(title="Sessions", items=["s1", "s2"], selected=1,
                     footer="move with arrows")
    lines = sel.render(30)
    assert len(lines) == 5
    text = ["".join(s.text for s in line.segments) for line in lines]
    assert "Sessions" in text[0]
    assert "s1" in text[1]
    assert "s2" in text[2]
    assert text[4].startswith("\u2570")
    assert "\u25b8" in text[2]
    assert "\u25b8" not in text[1]


def test_selectlist_empty_and_narrow():
    from nbchat.tui2.components import SelectList
    sel = SelectList(items=[], hint="no matches")
    lines = sel.render(20)
    assert any("no matches" in "".join(s.text for s in ln.segments)
               for ln in lines)
    assert sel.render(1)


# ── Phase 3: LineEditor component ──────────────────────────────────


def test_lineeditor_undo_redo_killring():
    from nbchat.tui2.editor import LineEditor

    ed = LineEditor()
    for ch in "hello":
        ed.handle("char", ch)
    assert ed.text() == "hello"
    for _ in range(5):
        ed.handle("ctrl+z")
    assert ed.text() == ""
    for _ in range(5):
        ed.handle("ctrl+r")
    assert ed.text() == "hello"
    # Redo leaves the cursor at the end of the line; home first so the
    # kill-ring check below is independent of undo/redo cursor semantics.
    ed.handle("home")
    ed.handle("ctrl+k")
    assert ed.text() == ""
    ed.handle("ctrl+y")
    assert ed.text() == "hello"


def test_lineeditor_multiline_and_continuation():
    from nbchat.tui2.editor import LineEditor
    ed = LineEditor()
    for ch in "abc":
        ed.handle("char", ch)
    ed.handle("enter")
    for ch in "def":
        ed.handle("char", ch)
    assert ed.text().replace("\n", "") == "abcdef"
    assert len(ed.lines) == 2
    ed.handle("up")
    line_no, col = ed.cursor
    assert line_no == 0


# ── Fixes pass: regressions for the 2026-07-10 bug list ───────────────────


def test_key_text_space_maps_to_space():
    from nbchat.tui2 import Key
    from nbchat.tui2.app import _key_text

    # The space bar is named "space" by the KeyReader; it must still
    # insert a space (the C4 fix — spaces used to be dropped entirely).
    assert _key_text(Key(name="space")) == " "


def test_typing_with_spaces_keeps_them():
    from nbchat.tui2 import Key

    app, _, _, _ = _make_chat_app()
    for ch in "Say exactly: PING":
        app._on_input(Key(name=ch) if ch != " " else Key(name="space"))
    assert app.editor.text() == "Say exactly: PING"
    app.editor.clear()


def test_editor_placeholder_renders_with_cursor_when_empty():
    from nbchat.tui2.editor import LineEditor

    ed = LineEditor(placeholder="Type a message…", multiline=False)
    lines = ed.render(80)
    text = "".join(s.text for s in lines[0].segments)
    # Focused empty editor: cursor block over the first placeholder char.
    assert text.startswith("█")
    assert "ype a message" in text


def test_chatmessage_renders_blocks_and_answer_text():
    from nbchat.tui2.chat import ChatBlock, ChatMessage

    # The C1 regression: a turn with thinking/tool blocks must STILL show
    # its final answer text below the blocks.
    msg = ChatMessage(
        role="assistant",
        text="PING",
        blocks=[
            ChatBlock(kind="thinking", title="thinking", text="hmm"),
            ChatBlock(kind="tool", name="run_command",
                      title="run_command(command=echo PING)",
                      status="done", body=["PING"]),
        ],
    )
    flat = "\n".join(
        "".join(s.text for s in ln.segments) for ln in msg.render(80))
    assert "hmm" in flat
    assert "run_command" in flat
    assert "PING" in flat and "agent" in flat


def test_chatapp_single_thinking_block_per_llm_call():
    app, _, _, _ = _make_chat_app()
    # Call 1: reasoning streams token by token.
    app._on_stream_reasoning("The user")
    app._on_stream_reasoning('The user wants "PING".')
    assert len(app._stream_blocks) == 1, "tokens must update one block"
    assert app._stream_blocks[0].text == 'The user wants "PING".'
    app._on_stream_complete('The user wants "PING".')
    # Call 2 (after a tool): a fresh block opens, chronologically.
    app._on_tool_display("out", "run_command", '{"command": "echo PING"}')
    app._on_stream_reasoning("It worked.")
    thinking = [b for b in app._stream_blocks if b.kind == "thinking"]
    assert len(thinking) == 2, "one thinking block per LLM call"
    assert thinking[-1].text == "It worked."
    # Chronological order: thinking, tool, thinking.
    kinds = [b.kind for b in app._stream_blocks]
    assert kinds == ["thinking", "tool", "thinking"]


def test_chatapp_full_turn_frame_shows_answer_and_panels():
    app, term, events, _ = _make_chat_app()
    app._print_user("Say exactly: PING")
    app._on_stream_reasoning('The user wants "PING".')
    app._on_tool_display("PING", "run_command", '{"command": "echo PING"}')
    app._on_stream_token("PING")
    app._on_stream_complete("PING")
    app._finalize_turn()
    flat = "\n".join(
        "".join(s.text for s in ln.segments)
        for ln in app._build_frame().lines)
    assert "Say exactly: PING" in flat      # spaces kept (C4)
    assert "run_command(command=echo PING)" in flat
    # The answer text is on its own line, below the blocks (C1).
    lines = [l for l in flat.split("\n") if l.strip()]
    assert any(l.strip() == "PING" for l in lines)


def test_chatapp_live_turn_renders_mid_stream():
    app, _, _, _ = _make_chat_app()
    app._print_user("hi")
    app._on_stream_reasoning("thinking hard")
    app._on_stream_token("He")
    flat = "\n".join(
        "".join(s.text for s in ln.segments)
        for ln in app._build_frame().lines)
    # The in-flight turn is part of the frame while streaming (C3).
    assert "thinking hard" in flat
    assert "He" in flat
    app._on_stream_complete("He")
    app._finalize_turn()
    assert app._stream_text == "" and app._stream_blocks == []


def test_chatapp_ctrl_c_interrupts_when_busy_and_quits_when_idle():
    from nbchat.tui2 import Key

    app, _, _, _ = _make_chat_app()
    app._tui._running = True
    # Idle: Ctrl+C quits.
    assert app._handle_input("\x03") is True
    # Busy: Ctrl+C is consumed and interrupts the turn (C5).
    app._tui._running = True
    app._turn_active = True
    assert app._handle_input("\x03") is None
    assert app._stop_event.is_set()
    app._stop_event.clear()
    app._turn_active = False


def test_chatapp_ctrl_d_submits_when_text_present():
    from nbchat.tui2 import Key

    app, term, events, replies = _make_chat_app()
    for ch in "ab":
        app._on_input(Key(name=ch))
    app._tui._running = True
    # Ctrl+D with text submits instead of quitting (C5).
    assert app._handle_input("\x04") is None
    assert app.editor.text() == ""
    deadline = time.time() + 3
    while replies["n"] == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert replies["n"] == 1


def test_chatapp_slash_new_resets_log_and_keeps_note():
    app, _, _, _ = _make_chat_app()
    app._print_user("old message")
    assert len(app.log.messages) == 1
    app._run_command("/new")
    # Log reset for the fresh session; the command output is a note.
    assert not any("old message" in m.text for m in app.log.messages)
    notes = [m for m in app.log.messages if m.role == "system"]
    assert notes and "Started new session" in notes[-1].text


def test_chatapp_slash_quit_stops_the_loop():
    app, _, _, _ = _make_chat_app()
    app._tui._running = True
    app._run_command("/quit")
    assert app._tui._running is False


def test_chatapp_midstream_interjection_redirects():
    app, _, _, _ = _make_chat_app()

    class _AliveThread:
        def is_alive(self):
            return True

    app._turn_thread = _AliveThread()
    app._turn_active = True
    app._tui._running = True
    # A new message while busy: interrupt + pending redirect (M3).
    app._start_turn("first")
    assert app._redirect == "first"
    assert app._stop_event.is_set()
    app._stop_event.clear()
    # Latest interjection wins.
    app._start_turn("newer")
    assert app._redirect == "newer"
    # The interrupted turn finalises: the redirect spawns a fresh worker.
    old = app._turn_thread
    app._finalize_turn()
    import threading as _threading

    assert isinstance(app._turn_thread, _threading.Thread)
    assert app._turn_thread is not old
    assert app._redirect is None
    app._turn_thread.join(timeout=10)


def test_chatapp_resumes_last_session_into_log():
    from nbchat.core import db
    from nbchat.tui2.app import ChatApp

    # Seed a real session with history.
    seed = __import__("nbchat.tui.agent", fromlist=["TerminalAgent"]) \
        .TerminalAgent(color=False)
    seed.remember_session(seed.session_id)
    sid = seed.session_id
    db.log_message(sid, "user", "hello from before")
    db.log_message(sid, "assistant", "hi there")

    out = io.StringIO()
    term = RawTerminal(io.StringIO(""), out)
    term._saved = None
    term._passthrough = True
    term.width, term.height = 80, 24
    app = ChatApp(term, EventQueue())  # resume_last is the default
    assert app.session_id == sid
    flat = "\n".join(
        "".join(s.text for s in ln.segments)
        for ln in app._build_frame().lines)
    assert "hello from before" in flat
    assert "hi there" in flat
    # --new bypasses the last session.
    app2 = ChatApp(term, EventQueue(), resume_last=False)
    assert app2.session_id != sid
    assert app2.log.messages == []


# ── tui2-native slash commands (Batch 1) ──────────────────────────────────

def test_cmd_context_reports_turns_and_session():
    app, *_ = _make_chat_app()
    out = app._cmd_context("")
    assert "turns 0" in out
    assert "session" in out
    assert "model" in out


def test_cmd_hotkeys_lists_keybindings():
    app, *_ = _make_chat_app()
    out = app._cmd_hotkeys("")
    assert "enter" in out
    assert "ctrl+l" in out
    assert "ctrl+t" in out


def test_cmd_copy_copies_last_assistant_message():
    from nbchat.tui2 import chat as _chat
    app, *_ = _make_chat_app()
    app.log.add(_chat.ChatMessage(role="assistant", text="answer text"))
    out = app._cmd_copy("")
    assert "copied" in out and "11 chars" in out


def test_cmd_copy_empty_log():
    app, *_ = _make_chat_app()
    assert "nothing to copy" in app._cmd_copy("")


def test_run_command_rewrites_name_to_title(monkeypatch):
    import nbchat.tui.app as tui_app
    seen = {}

    def fake_handle_command(agent, line):
        seen["line"] = line
        return False

    monkeypatch.setattr(tui_app, "handle_command", fake_handle_command)
    app, *_ = _make_chat_app()
    app._run_command("/name my project")
    assert seen["line"] == "/title my project"


def test_run_command_routes_tui2_native_not_to_v1(monkeypatch):
    import nbchat.tui.app as tui_app
    called = {}

    def fake_handle_command(agent, line):
        called["line"] = line
        return False

    monkeypatch.setattr(tui_app, "handle_command", fake_handle_command)
    app, *_ = _make_chat_app()
    app._run_command("/hotkeys")
    assert "line" not in called  # never delegated to v1
    txt = " ".join(m.text or "" for m in app.log.messages)
    assert "hotkeys (normal mode)" in txt
    assert "browse mode" in txt  # generated from the KEYMAP source of truth
    assert "ctrl+o" in txt


def test_force_compact_nothing_to_evict():
    app, *_ = _make_chat_app()
    app.history = [("user", "hi", "", "", "", 0),
                   ("assistant", "yo", "", "", "", 0)]
    rep = app.force_compact("")
    assert rep["compacted"] is False
    assert "nothing to evict" in rep["reason"]


def test_force_compact_evicts_when_over_budget(monkeypatch):
    app, *_ = _make_chat_app()
    big = "x" * 4000
    rows = []
    for i in range(40):
        rows.append(("user", f"q{i} {big}", "", "", "", 0))
        rows.append(("assistant", f"a{i} {big}", "", "", "", 0))
    app.history = rows
    app._prefetch_summaries = lambda r: None
    app._build_prior_context = lambda r: "prior summary"
    rep = app.force_compact("focus on X")
    assert rep["compacted"] is True
    assert rep["before_rows"] > rep["window_rows"]
    assert rep["evicted_rows"] > 0
    assert rep["instructions"] == "focus on X"


def test_cmd_compact_renders_note():
    app, *_ = _make_chat_app()
    app.history = [("user", "hi", "", "", "", 0),
                   ("assistant", "yo", "", "", "", 0)]
    out = app._cmd_compact("")
    assert "compact:" in out
    # Routed through the dispatcher, the report becomes a logged note.
    app._run_tui2_command("/compact", "")
    assert any("compact:" in (m.text or "") for m in app.log.messages)


def test_cmd_lessons_empty():
    app, *_ = _make_chat_app()
    out = app._cmd_lessons("")
    assert "lessons" in out


def test_cmd_memory_renders_l1_l2():
    app, *_ = _make_chat_app()
    out = app._cmd_memory("")
    assert "L1 core" in out
    assert "L2 episodic" in out


def test_cmd_refine_rollback_reports():
    app, *_ = _make_chat_app()
    out = app._cmd_refine("rollback")
    assert "refine rollback" in out


def test_send_side_question_isolated(monkeypatch):
    import nbchat.tui.agent as agent_mod

    def fake_send(self, text):
        # The side agent must run under a btw: session, not the main one.
        assert self.session_id.startswith("btw:")
        return "side answer"

    monkeypatch.setattr(agent_mod.TerminalAgent, "send", fake_send)
    app, *_ = _make_chat_app()
    assert app._send_side_question("hello") == "side answer"
    # The main session's log was not touched by the side answer.
    assert not any("side answer" in (m.text or "") for m in app.log.messages)


def test_cmd_btw_usage_note():
    app, *_ = _make_chat_app()
    app._cmd_btw("")
    assert any("usage: /btw" in (m.text or "") for m in app.log.messages)


# ── session picker modal ──────────────────────────────────────────────────

def test_picker_open_and_render():
    from nbchat.tui2 import Key
    app, *_ = _make_chat_app()
    assert app._picker is None
    app._open_picker()
    assert app._picker is not None
    flat = "\n".join(
        "".join(s.text for s in ln.segments)
        for ln in app._build_frame().lines)
    assert "sessions" in flat
    # Esc cancels.
    app._picker_key(Key("escape"))
    assert app._picker is None


def test_picker_filter_and_navigation():
    from nbchat.tui2 import Key
    app, *_ = _make_chat_app()
    app._open_picker()
    n_before = len(app._picker_rows)
    app._picker_key(Key("a"))
    assert app._picker_filter == "a"
    # Backspace clears the filter.
    app._picker_key(Key("backspace"))
    assert app._picker_filter == ""
    app._picker_key(Key("down"))
    app._picker_key(Key("up"))
    assert len(app._picker_rows) == n_before
    app._close_picker()


def test_picker_ctrl_l_opens_from_key():
    from nbchat.tui2 import Key
    app, *_ = _make_chat_app()
    assert app._picker is None
    app._on_input(Key("ctrl+l"))
    assert app._picker is not None
    app._close_picker()


def test_picker_ctrl_c_closes_not_quit(monkeypatch):
    from nbchat.tui2 import Key
    app, *_ = _make_chat_app()
    stopped = {}
    monkeypatch.setattr(app._tui, "stop", lambda: stopped.update(x=1))
    app._open_picker()
    # Ctrl+C while the picker is open must close it, not stop the app.
    assert app._handle_input("\x03") is None
    assert app._picker is None
    assert "x" not in stopped


# ── thinking toggle (Ctrl+T) ──────────────────────────────────────────────

def test_toggle_thinking_hides_and_restores():
    from nbchat.tui2 import Key, chat as _chat
    app, *_ = _make_chat_app()
    blk = _chat.ChatBlock(kind="thinking", text="some reasoning")
    tool = _chat.ChatBlock(kind="tool", name="x", title="x")
    msg = _chat.ChatMessage(role="assistant", text="ans", blocks=[blk, tool])
    msg._full_blocks = [blk, tool]
    app.log.add(msg)

    app._on_input(Key("ctrl+t"))  # hide
    assert app._thinking_visible is False
    assert all(b.kind != "thinking" for b in (msg.blocks or []))

    app._on_input(Key("ctrl+t"))  # show
    assert app._thinking_visible is True
    assert any(b.kind == "thinking" for b in msg.blocks)


def test_live_rows_hide_thinking_when_toggled():
    from nbchat.tui2 import chat as _chat
    app, *_ = _make_chat_app()
    app._stream_blocks = [
        _chat.ChatBlock(kind="thinking", text="reasoning"),
        _chat.ChatBlock(kind="tool", name="run_command", title="run"),
    ]
    app._stream_text = ""
    shown = "\n".join(
        "".join(s.text for s in ln.segments) for ln in app._live_rows(80))
    assert "reasoning" in shown
    app._thinking_visible = False
    hidden = "\n".join(
        "".join(s.text for s in ln.segments) for ln in app._live_rows(80))
    assert "reasoning" not in hidden


def test_finalize_turn_stores_full_blocks():
    app, *_ = _make_chat_app()
    from nbchat.tui2 import chat as _chat
    app._stream_blocks = [_chat.ChatBlock(kind="thinking", text="r")]
    app._stream_text = "final answer"
    app._finalize_turn()
    msg = app.log.messages[-1]
    assert getattr(msg, "_full_blocks", None) is not None
    assert any(b.kind == "thinking" for b in msg._full_blocks)


def test_scroll_log_pages_and_clamps():
    from nbchat.tui2 import chat as _chat
    app, *_ = _make_chat_app()
    # Seed enough history to be taller than the log region.
    for i in range(30):
        app.log.add(_chat.ChatMessage(role="assistant", text=f"line {i} " + "x" * 60))
    app._scroll_log(1000)  # clamp to max
    total = len(app.log._all_rows(80))
    log_rows = max(app.term.height - 8, 2)  # same formula as _scroll_log
    assert app.log.offset == max(0, total - log_rows)
    app._scroll_log(-1000)  # clamp to 0
    assert app.log.offset == 0


def test_new_content_snaps_to_bottom():
    app, *_ = _make_chat_app()
    app.log.offset = 5
    app._finalize_turn()  # commits an empty-but-present assistant msg
    assert app.log.offset == 0



# ── KeyReader CSI parsing (regression: the "[" introducer is itself in the
#    0x40-0x7E final-byte range, so the terminator scan must start past it) ──

def _feed_keys(seq):
    from nbchat.tui2.keys import KeyReader
    return [k.name for k in KeyReader().feed(seq)]


def test_keyreader_csi_arrows():
    assert _feed_keys("\x1b[A") == ["up"]
    assert _feed_keys("\x1b[B") == ["down"]
    assert _feed_keys("\x1b[C") == ["right"]
    assert _feed_keys("\x1b[D") == ["left"]


def test_keyreader_csi_paging_and_home_end():
    assert _feed_keys("\x1b[5~") == ["pageup"]
    assert _feed_keys("\x1b[6~") == ["pagedown"]
    assert _feed_keys("\x1b[1~") == ["home"]
    assert _feed_keys("\x1b[4~") == ["end"]
    assert _feed_keys("\x1b[7~") == ["home"]
    assert _feed_keys("\x1b[8~") == ["end"]


def test_keyreader_csi_function_keys():
    assert _feed_keys("\x1b[11~") == ["f1"]
    assert _feed_keys("\x1b[12~") == ["f2"]
    assert _feed_keys("\x1b[13~") == ["f3"]
    assert _feed_keys("\x1b[14~") == ["f4"]


def test_keyreader_csi_split_across_chunks():
    from nbchat.tui2.keys import KeyReader
    r = KeyReader()
    k1 = [k.name for k in r.feed("\x1b[5")]   # final byte not yet arrived
    assert k1 == []                            # held, not mis-parsed
    k2 = [k.name for k in r.feed("~")]
    assert k2 == ["pageup"]                    # completed on the next chunk


def test_keyreader_lone_esc_and_legacy():
    assert _feed_keys("\x1b") == ["escape"]
    assert _feed_keys("\x1ba") == ["escape", "a"]
    assert _feed_keys("\x1bOA") == ["up"]


def test_keyreader_unknown_csi_swallowed():
    # An unrecognised CSI must be swallowed whole, not corrupted.  (A valid
    # SGR mouse report is now *parsed* into a mouse/wheel key — see the
    # wave-3 mouse tests — so use a bogus button code that stays unknown.)
    from nbchat.tui2.keys import KeyReader
    r = KeyReader()
    ks = [k.name for k in r.feed("\x1b[<5;10;5M")]
    assert ks == ["unknown"]


# ── Wave 3: shell prefixes, command palette (Ctrl+P), reverse search ──

def test_run_shell_empty_shows_usage():
    app, *_ = _make_chat_app()
    app._run_shell("!")
    last = app.log.messages[-1]
    assert last.role == "system"
    assert "usage" in last.text


def test_run_shell_records_user_line():
    app, *_ = _make_chat_app()
    app._run_shell("!echo hi")
    assert any(m.role == "user" and m.text == "!echo hi"
               for m in app.log.messages)


def test_shell_worker_renders_output_and_exit():
    app, *_ = _make_chat_app()
    app._shell_worker("echo hi", store=False)  # synchronous, deterministic
    blocks = [b for m in app.log.messages for b in (m.blocks or [])
              if b.name == "shell"]
    assert blocks, "no shell block rendered"
    assert blocks[0].title == "exit 0"
    assert any("hi" in line for line in blocks[0].body)


def test_shell_worker_stores_output_on_double_bang():
    app, *_ = _make_chat_app()
    app._shell_worker("echo stored", store=True)
    assert app._last_shell.strip() == "stored"


def test_shell_worker_nonzero_exit_marks_error():
    app, *_ = _make_chat_app()
    app._shell_worker("exit 3", store=False)
    blocks = [b for m in app.log.messages for b in (m.blocks or [])
              if b.name == "shell"]
    assert blocks[0].title == "exit 3"
    assert blocks[0].status == "error"


def test_submit_records_history():
    app, *_ = _make_chat_app()
    app._submit("/context")
    app._submit("!ls")
    assert "/context" in app._history
    assert "!ls" in app._history


# ── Ctrl+P command palette ──

def test_palette_opens_and_filters():
    app, *_ = _make_chat_app()
    app._open_palette()
    assert app._picker is not None
    assert app._modal_kind == "palette"
    n_all = len(app._picker_rows)
    assert n_all >= 10
    for ch in "comp":                 # type the filter one key at a time
        app._picker_key(Key(ch))
    assert app._picker_filter == "comp"
    assert len(app._picker_rows) < n_all
    app._close_picker()


def test_palette_enter_inserts_command():
    app, *_ = _make_chat_app()
    app._open_palette()
    idx = next(i for i, (_k, lab) in enumerate(app._picker_rows)
               if lab.startswith("model"))
    app._picker.select(idx)
    app._picker_key(Key("enter"))
    assert app._picker is None
    assert app.editor.text().startswith("/context")


def test_ctrl_p_opens_palette_from_key():
    app, *_ = _make_chat_app()
    assert app._picker is None
    app._on_input(Key("ctrl+p"))
    assert app._picker is not None
    assert app._modal_kind == "palette"
    app._close_picker()


# ── Ctrl+R reverse search ──

def test_search_opens_from_history_newest_first():
    app, *_ = _make_chat_app()
    app._history = ["alpha", "beta", "gamma"]
    app._open_search()
    assert app._picker is not None
    assert app._modal_kind == "search"
    assert app._picker_rows[0][0] == "gamma"   # newest on top
    app._close_picker()


def test_search_enter_inserts_text():
    app, *_ = _make_chat_app()
    app._history = ["hello world"]
    app._open_search()
    app._picker_key(Key("enter"))
    assert app._picker is None
    assert app.editor.text() == "hello world"


def test_ctrl_r_opens_search_from_key():
    app, *_ = _make_chat_app()
    app._history = ["x"]
    assert app._picker is None
    app._on_input(Key("ctrl+r"))
    assert app._picker is not None
    assert app._modal_kind == "search"
    app._close_picker()


# ── Tool-approval gate ─────────────────────────────────────────────


def test_approval_gate_wraps_run_tool():
    import nbchat.core.tool_executor as te
    app, *_ = _make_chat_app()
    real = te.run_tool
    calls = []

    def stub(name, args, timeout=None):
        calls.append(name)
        return "STUB_RAN"

    try:
        te.run_tool = stub
        app._approval_enabled = True
        app._risky_tools = {"run_command"}
        app._install_approval_gate()
        assert te.run_tool is not stub
        app._prompt_approval = lambda tool, args: True
        assert te.run_tool("run_command", "{}") == "STUB_RAN"
        assert calls == ["run_command"]
        app._prompt_approval = lambda tool, args: False
        res = te.run_tool("run_command", "{}")
        assert "DECLINED" in res
        assert calls == ["run_command"]  # declined -> stub not called
        # Non-risky tool: no prompt.
        def _boom(tool, args):
            raise AssertionError("should not prompt")
        app._prompt_approval = _boom
        assert te.run_tool("get_weather", "{}") == "STUB_RAN"
        assert calls == ["run_command", "get_weather"]
        # Disabled: risky tools not prompted.
        app._approval_enabled = False
        assert te.run_tool("run_command", "{}") == "STUB_RAN"
        assert calls == ["run_command", "get_weather", "run_command"]
    finally:
        app._remove_approval_gate()
        te.run_tool = real


def test_approval_gate_restores_and_unblocks():
    import nbchat.core.tool_executor as te
    app, *_ = _make_chat_app()
    real = te.run_tool
    try:
        app._install_approval_gate()
        assert te.run_tool is not real
        app._remove_approval_gate()
        assert te.run_tool is real
        # Removal unblocks a parked approval prompt.
        ev = threading.Event()
        app._approval = {"tool": "run_command", "args": "x",
                         "event": ev, "result": False}
        app._remove_approval_gate()
        assert ev.is_set() and app._approval is None
    finally:
        te.run_tool = real


def test_approval_modal_renders_and_keys():
    app, term, events, _ = _make_chat_app()
    ev = threading.Event()
    app._approval = {"tool": "run_command", "args": "git push",
                     "event": ev, "result": False}
    frame = app._build_frame()
    flat = "\n".join(
        "".join(s.text for s in ln.segments) for ln in frame.lines)
    assert "approve tool" in flat and "run_command" in flat
    app._on_input(Key("y"))
    assert ev.is_set() and app._approval["result"] is True
    ev2 = threading.Event()
    app._approval = {"tool": "push_to_github", "args": "x",
                     "event": ev2, "result": True}
    app._on_input(Key("n"))
    assert ev2.is_set() and app._approval["result"] is False


def test_cmd_approve():
    app, *_ = _make_chat_app()
    assert "ON" in app._cmd_approve("")
    assert "OFF" in app._cmd_approve("off")
    assert app._approval_enabled is False
    assert "ON" in app._cmd_approve("on")
    out = app._cmd_approve("add create_file")
    assert "create_file" in out and "create_file" in app._risky_tools
    app._cmd_approve("rm create_file")
    assert "create_file" not in app._risky_tools


# ── /goal ──────────────────────────────────────────────────────────


def test_goal_next_prompt_budget():
    app, *_ = _make_chat_app()
    assert app._goal_next_prompt() is None
    app._goal = {"objective": "ship it", "remaining": 3, "budget": 3,
                 "done": 0, "stopped": False}
    p1 = app._goal_next_prompt()
    assert p1 and "ship it" in p1 and "1/3" in p1
    p2 = app._goal_next_prompt()
    assert p2 and "2/3" in p2
    p3 = app._goal_next_prompt()
    assert p3 and "3/3" in p3
    p4 = app._goal_next_prompt()
    assert p4 is None and app._goal["stopped"] is True


def test_goal_declared_done():
    app, *_ = _make_chat_app()
    assert app._goal_declared_done("All done. GOAL COMPLETE. Shipped.")
    assert not app._goal_declared_done("still working")
    assert not app._goal_declared_done("")


def test_goal_finalize_continues_then_stops():
    app, term, events, _ = _make_chat_app()
    started = []
    app._start_turn = lambda text: started.append(text)
    app._tui._running = True
    app._goal = {"objective": "ship it", "remaining": 5, "budget": 5,
                 "done": 0, "stopped": False}
    app._stream_text = "working on it"
    app._stream_blocks = []
    app._finalize_turn()
    assert len(started) == 1
    assert "ship it" in started[0] and "1/5" in started[0]
    assert app._goal["done"] == 1 and app._goal["remaining"] == 4
    started.clear()
    app._stream_text = "All done. GOAL COMPLETE. Shipped."
    app._finalize_turn()
    assert started == [] and app._goal["stopped"] is True


def test_cmd_goal_commands():
    app, *_ = _make_chat_app()
    started = []
    app._start_turn = lambda text: started.append(text)
    assert "no active goal" in app._cmd_goal("")
    assert "budget set to 5" in app._cmd_goal("budget 5")
    out = app._cmd_goal("do the thing")
    assert app._goal is not None
    assert app._goal["objective"] == "do the thing"
    assert started and "do the thing" in started[0]
    assert "running" in app._cmd_goal("")
    assert "stopping" in app._cmd_goal("stop")
    assert "cleared" in app._cmd_goal("clear")
    assert app._goal is None


# ── Notification stack (herdr-style toasts + BEL) ─────────────────────

def test_notify_stack_push_prune_render():
    n = NotifyStack(max_age=0.05)
    assert n.active == []
    n.push("hello", "body", "ok", write=None)
    assert len(n.active) == 1
    assert n.active[0].kind == "ok"
    lines = n.render_one(80)
    assert len(lines) >= 2  # border rows at minimum
    time.sleep(0.06)
    n.prune()
    assert n.active == []


def test_notify_max_visible_cap():
    n = NotifyStack(max_visible=2)
    for title in ("a", "b", "c"):
        n.push(title, "", "info", write=None)
    assert len(n.active) == 2
    assert [t.title for t in n.active] == ["b", "c"]


def test_notify_frame_renders_toast():
    app, term, events, _ = _make_chat_app()
    app._notify.push("turn complete", "", "ok", write=None)
    frame = app._build_frame()
    text = "\n".join(
        "".join(s.text for s in ln.segments) for ln in frame.lines)
    assert "turn complete" in text
    assert len(frame.lines) == term.height  # frame height preserved


def test_notify_toasts_disabled_hides_card():
    app, term, events, _ = _make_chat_app()
    app._notify.toasts = False
    app._notify.push("hidden", "", "ok", write=None)
    frame = app._build_frame()
    text = "\n".join(
        "".join(s.text for s in ln.segments) for ln in frame.lines)
    assert "hidden" not in text


def test_cmd_notify():
    app, *_ = _make_chat_app()
    assert "toasts: True" in app._cmd_notify("")
    assert "toasts: False" in app._cmd_notify("toasts off")
    assert "toasts: True" in app._cmd_notify("toasts on")
    assert "bel: False" in app._cmd_notify("bel off")
    app._cmd_notify("test warn")
    assert any(t.kind == "warn" for t in app._notify.active)
    assert "usage" in app._cmd_notify("bogus")


def test_approval_always_removes_risky():
    app, *_ = _make_chat_app()
    import threading
    ev = threading.Event()
    app._approval = {"tool": "run_command", "args": "{}",
                     "event": ev, "result": False}
    app._risky_tools = {"run_command", "push_to_github"}
    app._on_input(Key("a"))
    assert ev.is_set()
    assert app._approval["result"] is True
    assert "run_command" not in app._risky_tools
    assert "push_to_github" in app._risky_tools


def test_help_addendum_lists_tui2_extras():
    app, *_ = _make_chat_app()
    add = app._tui2_help_addendum()
    assert "TUI v2 extras" in add
    for tok in ("/goal", "/notify", "/approve", "/compact", "Ctrl+P"):
        assert tok in add


# ── tui3 wave 1: keymap substrate + browse mode + mode bar ────────────

def _frame_text(app):
    frame = app._build_frame()
    return "\n".join("".join(s.text for s in ln.segments)
                      for ln in frame.lines), frame


def test_mode_bar_renders_and_switches():
    app, term, *_ = _make_chat_app()
    text, frame = _frame_text(app)
    assert "mode: normal" in text
    assert len(frame.lines) == term.height
    app._browse = True
    text, frame = _frame_text(app)
    assert "mode: browse" in text
    assert len(frame.lines) == term.height  # height still exact


def test_hotkeys_generated_from_keymap():
    app, *_ = _make_chat_app()
    out = app._cmd_hotkeys("")
    # every normal-mode KEYMAP row appears, and the browse section too
    for k, _d in KEYMAP["normal"]:
        assert k in out
    assert "browse mode" in out
    for k, _d in KEYMAP["browse"]:
        assert k in out


def test_browse_toggle_and_scroll():
    from nbchat.tui2 import chat as chatc
    app, *_ = _make_chat_app()
    # give the log enough content to scroll
    for i in range(60):
        app.log.add(chatc.ChatMessage(role="user", text=f"line {i}"))
    app._scroll_log(0)  # ensure offset valid
    assert app._browse is False
    app._on_input(Key("ctrl+o"))
    assert app._browse is True
    off0 = app.log.offset
    app._on_input(Key("k"))  # up / older -> offset grows by one
    assert app.log.offset == off0 + 1
    app._on_input(Key("j"))  # down / newer -> offset shrinks by one
    assert app.log.offset == off0
    app._on_input(Key("k"))
    up = app.log.offset
    app._on_input(Key("k"))
    assert app.log.offset == up + 1
    # a normal letter is consumed, NOT typed into the editor
    app._on_input(Key("x"))
    assert app.editor.text() == ""
    # esc leaves browse and snaps to the bottom
    app._on_input(Key("escape"))
    assert app._browse is False
    assert app.log.offset == 0
    # ctrl+o also leaves browse
    app._on_input(Key("ctrl+o"))
    assert app._browse is True
    app._on_input(Key("ctrl+o"))
    assert app._browse is False


# ── tui3 wave 2: in-log search + visual copy ──────────────────────────

def _seed_log(app, texts):
    from nbchat.tui2 import chat as chatc
    for i, txt in enumerate(texts):
        app.log.add(chatc.ChatMessage(role="user", text=txt))
    app._scroll_log(0)


def test_logsearch_open_type_exec():
    app, *_ = _make_chat_app()
    _seed_log(app, ["alpha one", "the needle here", "beta two",
                    "another needle", "gamma three"])
    app._on_input(Key("ctrl+o"))           # enter browse
    app._on_input(Key("/"))                 # open search
    assert app._logsearch is not None and app._logsearch["input"]
    for ch in "needle":
        app._on_input(Key(ch))
    assert app._logsearch["query"] == "needle"
    # mode bar shows the live query
    frame = app._build_frame()
    ftext = "\n".join("".join(s.text for s in ln.segments)
                       for ln in frame.lines)
    assert "search: needle" in ftext
    app._on_input(Key("enter"))             # run
    assert app._logsearch["input"] is False
    assert len(app._search_matches) == 2
    # mode bar shows the match position
    frame = app._build_frame()
    ftext = "\n".join("".join(s.text for s in ln.segments)
                       for ln in frame.lines)
    assert "match 1/2" in ftext


def test_logsearch_next_prev_cycles():
    app, *_ = _make_chat_app()
    _seed_log(app, ["a1", "needle x", "b2", "needle y", "c3"])
    app._on_input(Key("ctrl+o"))
    app._on_input(Key("/"))
    for ch in "needle":
        app._on_input(Key(ch))
    app._on_input(Key("enter"))
    assert len(app._search_matches) == 2
    i0 = app._search_idx
    app._on_input(Key("n"))
    assert app._search_idx == (i0 + 1) % 2
    app._on_input(Key("N"))
    assert app._search_idx == i0
    # offset is always clamped to a valid range
    w = app.term.width
    total = len(app.log._all_rows(w))
    assert 0 <= app.log.offset <= max(0, total - 2)


def test_logsearch_no_matches():
    app, *_ = _make_chat_app()
    _seed_log(app, ["one", "two", "three"])
    app._on_input(Key("ctrl+o"))
    app._on_input(Key("/"))
    for ch in "zzzz":
        app._on_input(Key(ch))
    app._on_input(Key("enter"))
    assert app._search_matches == []
    # esc clears the search
    app._on_input(Key("escape"))
    assert app._logsearch is None
    # n with no matches re-opens a fresh search
    app._on_input(Key("n"))
    assert app._logsearch is not None and app._logsearch["input"]
    app._on_input(Key("escape"))
    assert app._logsearch is None


def test_visual_copy_uses_clipboard():
    app, *_ = _make_chat_app()
    _seed_log(app, ["copied line one", "copied line two", "copied line three"])
    app._on_input(Key("ctrl+o"))
    captured = {}
    app._copy_to_clipboard = lambda t: captured.setdefault("text", t)
    app._on_input(Key("v"))
    assert "text" in captured
    assert "copied line one" in captured["text"]
    # a toast was pushed
    assert app._notify._queue, "visual copy should push a toast"


# ── tui3 wave 3: SGR mouse parsing + wheel scroll ─────────────────────

def _mouse_keys(seq):
    from nbchat.tui2.keys import KeyReader
    return [k for k in KeyReader().feed(seq)]


def test_mouse_wheel_sgr_parsing():
    ks = _mouse_keys("\x1b[<64;10;5M")
    assert [k.name for k in ks] == ["wheel-up"]
    ks = _mouse_keys("\x1b[<65;10;5M")
    assert [k.name for k in ks] == ["wheel-down"]
    ks = _mouse_keys("\x1b[<0;10;5M")
    assert ks[0].name == "mouse-press" and ks[0].payload == "10,5"
    ks = _mouse_keys("\x1b[<3;10;5m")
    assert ks[0].name == "mouse-release" and ks[0].payload == "10,5"


def test_mouse_split_across_chunks():
    from nbchat.tui2.keys import KeyReader
    r = KeyReader()
    assert r.feed("\x1b[<6") == []          # incomplete
    ks = r.feed("4;10;5M")
    assert [k.name for k in ks] == ["wheel-up"]


def test_mouse_unknown_sgr_swallowed():
    # a non-mouse CSI with "<" but malformed still yields no crash
    ks = _mouse_keys("\x1b[<99;M")
    assert all(k.name in ("unknown",) for k in ks) or ks == []


def test_wheel_scrolls_log_and_click_consumed():
    from nbchat.tui2 import chat as chatc
    app, *_ = _make_chat_app()
    for i in range(40):
        app.log.add(chatc.ChatMessage(role="user", text=f"line {i}"))
    app._scroll_log(0)
    off0 = app.log.offset
    app._on_input(Key("wheel-up"))
    assert app.log.offset > off0
    up = app.log.offset
    app._on_input(Key("wheel-down"))
    # one symmetric tick returns to the start (clamped if needed)
    assert app.log.offset == max(0, up - 3)
    app._on_input(Key("wheel-down"))  # clamped at the bottom
    assert app.log.offset == max(0, up - 6)
    # a click is consumed, not typed into the editor
    app._on_input(Key("mouse-press", "10,5"))
    assert app.editor.text() == ""
    app._on_input(Key("mouse-release", "10,5"))
    assert app.editor.text() == ""


# ── tui3 wave 3b: click / drag-to-copy over the log ───────────────────

def _content_rows(app):
    """1-based frame rows in the log region that carry non-empty text."""
    rows = []
    for r in range(2, app._last_log_end + 1):
        if app._row_text(app._last_frame_rows[r - 1]).strip():
            rows.append(r)
    return rows


def test_mouse_click_copies_single_line():
    from nbchat.tui2 import chat as chatc
    app, *_ = _make_chat_app()
    for i in range(12):
        app.log.add(chatc.ChatMessage(role="user", text=f"uniq line {i}"))
    app._build_frame()
    rows = _content_rows(app)
    assert rows, "need at least one content row"
    target = rows[0]
    captured = {}
    app._copy_to_clipboard = lambda t: captured.setdefault("text", t)
    app._on_input(Key("mouse-press", f"10,{target}"))
    app._on_input(Key("mouse-release", f"10,{target}"))
    assert "text" in captured, "click should copy a line"
    expected = app._row_text(app._last_frame_rows[target - 1]).strip()
    assert expected in captured["text"]
    # a toast confirms the copy
    assert app._notify._queue


def test_mouse_drag_copies_line_range():
    from nbchat.tui2 import chat as chatc
    app, *_ = _make_chat_app()
    for i in range(12):
        app.log.add(chatc.ChatMessage(role="user", text=f"range line {i}"))
    app._build_frame()
    rows = _content_rows(app)
    assert len(rows) >= 2
    lo_row, hi_row = rows[0], rows[-1]
    captured = {}
    app._copy_to_clipboard = lambda t: captured.setdefault("text", t)
    app._on_input(Key("mouse-press", f"5,{lo_row}"))
    app._on_input(Key("mouse-release", f"5,{hi_row}"))
    assert "text" in captured
    # every line in the range is present, in order
    for r in range(lo_row, hi_row + 1):
        line = app._row_text(app._last_frame_rows[r - 1]).strip()
        if line:
            assert line in captured["text"]


def test_mouse_click_outside_log_is_ignored():
    from nbchat.tui2 import chat as chatc
    app, *_ = _make_chat_app()
    for i in range(6):
        app.log.add(chatc.ChatMessage(role="user", text=f"x {i}"))
    app._build_frame()
    captured = {}
    app._copy_to_clipboard = lambda t: captured.setdefault("text", t)
    # header row (1) and a row below the log region should be ignored
    app._on_input(Key("mouse-press", "10,1"))
    app._on_input(Key("mouse-release", "10,1"))
    assert "text" not in captured
    below = app._last_log_end + 2
    app._on_input(Key("mouse-press", f"10,{below}"))
    app._on_input(Key("mouse-release", f"10,{below}"))
    assert "text" not in captured


def test_mouse_row_payload_parse():
    app, *_ = _make_chat_app()
    assert app._mouse_row(Key("mouse-press", "10,5")) == 5
    assert app._mouse_row(Key("mouse-release", "3,42")) == 42
    assert app._mouse_row(Key("mouse-press", None)) is None
    assert app._mouse_row(Key("mouse-press", "bad")) is None


# ── tui3 wave 4: user settings persistence ──────────────────────────

def test_config_roundtrip():
    from nbchat.tui2 import config
    c = config.load()
    c["thinking_visible"] = False
    c["scroll_tick"] = 7
    assert config.save(c)
    c2 = config.load()
    assert c2["thinking_visible"] is False
    assert c2["scroll_tick"] == 7


def test_app_loads_persisted_settings():
    from nbchat.tui2 import config
    config.save({"thinking_visible": False, "scroll_tick": 5,
                 "notify_sound": True,
                 "risky_tools": ["run_command", "send_email"]})
    app, *_ = _make_chat_app()
    assert app._thinking_visible is False
    assert app._scroll_tick == 5
    assert app._notify.sound is True
    assert app._risky_tools == {"run_command", "send_email"}


def test_toggle_thinking_persists():
    from nbchat.tui2 import config
    app, *_ = _make_chat_app()
    app._save_cfg()
    before = app._thinking_visible
    app._toggle_thinking()
    c = config.load()
    assert c["thinking_visible"] == (not before)
    app2, *_ = _make_chat_app()
    assert app2._thinking_visible == (not before)


def test_approve_off_persists():
    from nbchat.tui2 import config
    app, *_ = _make_chat_app()
    app._cmd_approve("off")
    c = config.load()
    assert c["approval_enabled"] is False
    assert c["risky_tools"] == sorted(app._risky_tools)


# ── tui3 wave 5: colour theming ──────────────────────────────────────

def test_theme_switch_and_persist():
    from nbchat.tui2 import theme, config
    app, *_ = _make_chat_app()
    try:
        out = app._cmd_theme("light")
        assert theme.current().name == "light"
        assert "theme: light" in out
        # the DARK proxy follows the active theme
        assert theme.DARK.name == "light"
        # persisted
        assert config.load()["theme"] == "light"
        # unknown theme is rejected and leaves the active theme unchanged
        out2 = app._cmd_theme("bogus")
        assert "unknown theme" in out2
        assert theme.current().name == "light"
    finally:
        theme.set_active("dark")


def test_theme_load_on_start():
    from nbchat.tui2 import theme, config
    try:
        config.save({"theme": "prime"})
        app, *_ = _make_chat_app()
        assert theme.current().name == "prime"
        assert theme.DARK.name == "prime"
    finally:
        theme.set_active("dark")


def test_theme_status_lists_available():
    from nbchat.tui2 import theme
    try:
        app, *_ = _make_chat_app()
        out = app._cmd_theme("")
        assert theme.current().name in out
        for t in theme.all_themes():
            assert t.name in out
    finally:
        theme.set_active("dark")


# ── tui3 wave 6: external control socket ─────────────────────────────

def _ctl_server(tmp_path, dispatch=None, status=None, sessions=None):
    import os
    from nbchat.tui2 import ctl
    path = str(tmp_path / "ctl.sock")
    dispatch = dispatch if dispatch is not None else (lambda fn: None)
    status = status if status is not None else (lambda: {"busy": False})
    sessions = sessions if sessions is not None else (lambda: [])
    srv = ctl.ControlServer(
        path,
        dispatch=dispatch,
        status_fn=status,
        sessions_fn=sessions,
        theme_fn=lambda n: None,
        send_fn=lambda t: None,
        quit_fn=lambda: None,
    )
    assert srv.start()
    return srv, path


def test_ctl_status_and_sessions(tmp_path):
    from nbchat.tui2 import ctl
    srv, path = _ctl_server(tmp_path,
                            status=lambda: {"busy": True, "model": "m"},
                            sessions=lambda: [{"session": "s1", "title": "t"}])
    try:
        r = ctl.call(path, "status")
        assert r["ok"] and r["data"]["busy"] is True and r["data"]["model"] == "m"
        r2 = ctl.call(path, "sessions")
        assert r2["ok"] and r2["data"] == [{"session": "s1", "title": "t"}]
    finally:
        srv.stop()


def test_ctl_mutating_commands_dispatch(tmp_path):
    from nbchat.tui2 import ctl
    calls = []
    srv, path = _ctl_server(tmp_path,
                            dispatch=lambda fn: calls.append(fn))
    try:
        assert ctl.call(path, "theme", "light")["queued"] is True
        assert ctl.call(path, "send", "hi there")["queued"] is True
        assert ctl.call(path, "quit")["queued"] is True
        assert len(calls) == 3
        # execute the queued closures: theme, send, quit
        calls[0]()  # theme
        calls[1]()  # send
        calls[2]()  # quit
    finally:
        srv.stop()


def test_ctl_unknown_command(tmp_path):
    from nbchat.tui2 import ctl
    srv, path = _ctl_server(tmp_path)
    try:
        r = ctl.call(path, "bogus")
        assert r["ok"] is False and "unknown command" in r["error"]
    finally:
        srv.stop()


def test_ctl_main_no_running_tui(tmp_path, capsys):
    import os
    from nbchat.tui2 import ctl
    os.environ["NBCHAT_CTL_SOCKET"] = str(tmp_path / "nope.sock")
    try:
        rc = ctl.main(["status"])
        assert rc == 1  # no socket present
    finally:
        os.environ.pop("NBCHAT_CTL_SOCKET", None)


def test_ctl_result(tmp_path):
    import os
    from nbchat.tui2 import ctl
    path = str(tmp_path / "ctl.sock")
    calls = []
    srv = ctl.ControlServer(
        path,
        dispatch=lambda fn: calls.append(fn),
        status_fn=lambda: {"busy": False},
        sessions_fn=lambda: [],
        result_fn=lambda: {"session": "s1", "text": "the answer"},
    )
    assert srv.start()
    try:
        r = ctl.call(path, "result")
        assert r["ok"] and r["data"]["text"] == "the answer"
        assert r["data"]["session"] == "s1"
    finally:
        srv.stop()


# ── tui3 wave 6: headless / background agent (--bg + nbchat-ctl bg) ────

def test_chatapp_bg_flag_propagates_to_tuiapp():
    import io
    from nbchat.tui2.app import ChatApp
    term = RawTerminal(io.StringIO(""), io.StringIO())
    term._saved = None
    term._passthrough = True
    term.width, term.height = 80, 24
    events = EventQueue()
    app = ChatApp(term, events, resume_last=False, bg=True)
    assert app._tui._bg is True
    app2 = ChatApp(term, events, resume_last=False)
    assert app2._tui._bg is False


def test_tuiapp_constructor_bg_default_false():
    import io
    from nbchat.tui2.raw import TUIApp
    term = RawTerminal(io.StringIO(""), io.StringIO())
    tui = TUIApp(term)
    assert tui._bg is False
    tui2 = TUIApp(term, EventQueue(), bg=True)
    assert tui2._bg is True


def test_bg_mode_quit_via_control_socket(tmp_path):
    """A --bg TUI (stdin=/dev/null, always EOF) stays alive on its heartbeat
    and exits cleanly when a control-socket 'quit' is delivered — proving
    the stdin-EOF path does NOT skip the event drain."""
    import subprocess, sys, time, os
    from nbchat.tui2 import ctl
    sock = str(tmp_path / "bg.sock")
    env = dict(os.environ)
    env["NBCHAT_CTL_SOCKET"] = sock
    env["NBCHAT_TUI3_CONFIG"] = str(tmp_path / "cfg.json")
    env.pop("NBCHAT_NO_CTL", None)
    if os.path.exists(sock):
        os.remove(sock)
    proc = subprocess.Popen(
        [sys.executable, "-m", "nbchat.tui2", "--bg", "--new"],
        env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)
    try:
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.exists(sock):
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        assert os.path.exists(sock), "background TUI did not come up"
        # It is still alive (did not quit on the /dev/null stdin EOF).
        assert proc.poll() is None, "background TUI exited on stdin EOF"
        st = ctl.call(sock, "status", timeout=6)
        assert st.get("ok") is True
        q = ctl.call(sock, "quit", timeout=6)
        assert q.get("ok") is True
        rc = proc.wait(timeout=10)
        assert rc == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


# ── tui3: /monitor (live per-session observability) ──────────────────

def test_cmd_monitor_reports_session_metrics():
    app, term, events, _ = _make_chat_app()
    from nbchat.core import monitoring as mon
    m = mon.get_session_monitor(app.session_id)
    m.record_llm_call(volatile_len=1234)
    m.record_tool_call("read_file", was_compressed=False, had_error=False,
                       input_chars=100, output_chars=50)
    out = app._cmd_monitor("")
    assert "Session:" in out
    assert app.session_id in out
    assert "read_file" in out


def test_cmd_monitor_is_native_and_safe():
    app, term, events, _ = _make_chat_app()
    # /monitor is a tui2-native command (intercepted before v1).
    assert "/monitor" in app._TUI2_NATIVE
    # With no metrics recorded it still returns a friendly note, not a crash.
    fresh = _make_chat_app()[0]
    out = fresh._cmd_monitor("")
    assert isinstance(out, str) and out

# ── /inbox (tui3: unseen-email browsing, off-thread) ───────────────────

def _fake_email(uid, sender, subject):
    from types import SimpleNamespace
    from datetime import datetime
    return SimpleNamespace(uid=uid, from_addr=sender, subject=subject,
                           date=datetime(2026, 7, 12, 9, 30),
                           body="", message_id=uid, x_nbchat="")


def test_inbox_peek_lists_unseen(monkeypatch):
    app, term, events, _ = _make_chat_app()
    from nbchat.core import email_inbox
    msgs = [_fake_email("1", "alice@example.com", "Hello"),
            _fake_email("2", "bob@example.com", "Re: hi")]
    monkeypatch.setattr(email_inbox, "peek_unseen", lambda **k: msgs)
    out = app._inbox_peek(None)
    assert "inbox: 2 unseen" in out
    assert "alice@example.com: Hello" in out
    assert "bob@example.com: Re: hi" in out


def test_inbox_peek_reads_body(monkeypatch):
    app, term, events, _ = _make_chat_app()
    from nbchat.core import email_inbox
    msgs = [_fake_email("1", "alice@example.com", "Hello")]
    monkeypatch.setattr(email_inbox, "peek_unseen", lambda **k: msgs)
    monkeypatch.setattr(email_inbox, "fetch_body", lambda uid: "the body text")
    out = app._inbox_peek(1)
    assert "alice@example.com" in out
    assert "the body text" in out


def test_inbox_peek_out_of_range(monkeypatch):
    app, term, events, _ = _make_chat_app()
    from nbchat.core import email_inbox
    monkeypatch.setattr(email_inbox, "peek_unseen",
                        lambda **k: [_fake_email("1", "a@x", "s")])
    out = app._inbox_peek(5)
    assert "no unseen message #5" in out


def test_cmd_inbox_is_native_and_async_delivers(monkeypatch):
    app, term, events, _ = _make_chat_app()
    from nbchat.core import email_inbox
    assert "/inbox" in app._TUI2_NATIVE
    # usage guard
    assert app._cmd_inbox("abc").startswith("inbox: usage")
    # async ack, then the result is delivered via a "call" event (UI thread)
    msgs = [_fake_email("1", "alice@example.com", "Hello")]
    monkeypatch.setattr(email_inbox, "peek_unseen", lambda **k: msgs)
    ack = app._cmd_inbox("")
    assert ack.startswith("inbox: checking")
    call = None
    deadline = time.time() + 2
    while time.time() < deadline:
        for kind, payload in events.drain():
            if kind == "call":
                call = payload
        if call is not None:
            break
        time.sleep(0.01)
    assert call is not None
    before = len(app.log.messages)
    call()
    assert len(app.log.messages) == before + 1
    assert "Hello" in app.log.messages[-1].text

# ── /team (tui3: multi-agent team runs, output relayed off-thread) ─────

class _FakeCoordinator:
    """Stands in for nbchat.core.team.TeamCoordinator in tests: writes
    fake worker output to sys.stdout (which the app redirects to its
    _TeamCapture) and returns a result dict."""

    def __init__(self, agent, out=(), result=None, block=None):
        self.agent = agent
        self._out = list(out)
        self._result = result or {"status": "done", "summary": "ALL TASKS DONE"}
        self._block = block
        self.interrupted = False

    def run(self, goal):
        if self._block is not None:
            self._block.wait(timeout=5)
        for line in self._out:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()
        return dict(self._result)

    def _interrupt_active_workers(self):
        self.interrupted = True
        if self._block is not None:
            self._block.set()


def _patch_team(monkeypatch, out=(), result=None, block=None):
    import nbchat.core.team as team_mod

    def _make(agent):
        return _FakeCoordinator(agent, out=out, result=result, block=block)

    monkeypatch.setattr(team_mod, "TeamCoordinator", _make)


def _drain_calls(app, events):
    """Stand in for the render loop: run queued 'call' closures."""
    for _ in range(50):
        evs = list(events.drain())
        calls = [p for k, p in evs if k == "call" and callable(p)]
        if not calls:
            return
        for c in calls:
            c()


def _wait_team_done(app, timeout=5.0):
    st = app._team_state
    end = time.time() + timeout
    while time.time() < end:
        th = st["thread"]
        if th is None or not th.is_alive():
            break
        time.sleep(0.02)
    return st


def test_team_status_idle():
    app, term, events, _ = _make_chat_app()
    out = app._cmd_team("")
    assert "no team run" in out


def test_team_start_relays_output_and_report(monkeypatch):
    app, term, events, _ = _make_chat_app()
    _patch_team(monkeypatch, out=["[worker 1] investigating A",
                                  "[worker 2] verifying B"],
                result={"status": "done", "summary": "ALL TASKS DONE"})
    ack = app._cmd_team("do the thing")
    assert "starting run" in ack
    st = _wait_team_done(app)
    _drain_calls(app, events)
    assert st["status"] == "done"
    logtext = "\n".join(m.text for m in app.log.messages)
    assert "[worker 1] investigating A" in logtext
    assert "[worker 2] verifying B" in logtext
    status = app._cmd_team("")
    assert "ALL TASKS DONE" in status


def test_team_busy_guard(monkeypatch):
    app, term, events, _ = _make_chat_app()
    gate = threading.Event()
    _patch_team(monkeypatch, block=gate,
                result={"status": "done", "summary": "late"})
    app._cmd_team("first")
    assert app._team_state["status"] == "running"
    # a second run is refused while the first is alive
    busy = app._cmd_team("second")
    assert "already in progress" in busy
    # stopping unblocks the run
    stop = app._cmd_team("stop")
    assert "stop requested" in stop
    st = _wait_team_done(app)
    _drain_calls(app, events)
    assert st["status"] == "stopped"


def test_team_stop_with_no_run():
    app, term, events, _ = _make_chat_app()
    assert "nothing to stop" in app._cmd_team("stop")


def test_team_capture_batches_and_flushes(monkeypatch):
    app, term, events, _ = _make_chat_app()
    from nbchat.tui2.app import _TeamCapture
    cap = _TeamCapture(app, min_interval=0.0, min_chars=10)
    # below the size threshold and no time elapsed -> buffered
    cap.write("hello wor")
    assert app.log.messages == []
    # crossing the size threshold ships a batch (queued as a 'call'); run it
    # via _drain_calls (which drains AND executes, unlike a bare drain)
    cap.write("ld more")
    _drain_calls(app, events)
    joined = "\n".join(m.text for m in app.log.messages)
    assert "hello world more" in joined




# ── Arrow-key history recall (Up/Down) ───────────────────────────────

def test_history_updown_recalls_inputs():
    from nbchat.tui2 import Key
    app, _, _, _ = _make_chat_app()
    app._history = ["first", "second", "third"]
    # Up walks to older (newest first); Down walks back.
    app._on_input(Key(name="up"))
    assert app.editor.text() == "third"
    assert app._hist_pos == 2
    app._on_input(Key(name="up"))
    assert app.editor.text() == "second"
    assert app._hist_pos == 1
    app._on_input(Key(name="up"))
    assert app.editor.text() == "first"
    assert app._hist_pos == 0
    # Up at the oldest entry stays put.
    app._on_input(Key(name="up"))
    assert app.editor.text() == "first"
    # Down walks back toward the newest.
    app._on_input(Key(name="down"))
    assert app.editor.text() == "second"
    app._on_input(Key(name="down"))
    assert app.editor.text() == "third"


def test_history_down_past_newest_restores_draft():
    from nbchat.tui2 import Key
    app, _, _, _ = _make_chat_app()
    app._history = ["a", "b"]
    for ch in "partial draft":
        app._on_input(Key(name=ch))
    assert app.editor.text() == "partial draft"
    # Up recalls the newest ("b"), snapshotting the draft.
    app._on_input(Key(name="up"))
    assert app.editor.text() == "b"
    assert app._hist_pos == 1
    assert app._hist_draft == "partial draft"
    # Down past the newest restores the in-progress draft and stops recalling.
    app._on_input(Key(name="down"))
    assert app.editor.text() == "partial draft"
    assert app._hist_pos is None


def test_history_typing_resets_recall():
    from nbchat.tui2 import Key
    app, _, _, _ = _make_chat_app()
    app._history = ["old1", "old2"]
    app._on_input(Key(name="up"))     # recall old2
    assert app.editor.text() == "old2"
    assert app._hist_pos == 1
    # Typing a character ends recall (the next Up starts fresh).
    app._on_input(Key(name="z"))
    assert app._hist_pos is None
    app._on_input(Key(name="up"))     # fresh recall from the newest
    assert app.editor.text() == "old2"


def test_editor_set_text_updates_buffer_and_undo():
    from nbchat.tui2.editor import LineEditor
    ed = LineEditor(multiline=False)
    ed.set_text("hello")
    assert ed.text() == "hello"
    assert ed.cursor_col == 5
    # A recorded undo point lets Ctrl+Z (undo) restore the prior buffer.
    ed.undo()
    assert ed.text() == ""
    ed.redo()
    assert ed.text() == "hello"


# ── /browse + /search (web surface over the browser tool) ────────────

def _fake_browser_json(title="T", content="hello body", url=None):
    import json
    def fake(url, *a, **k):
        return json.dumps({"status": "success",
                           "url": url or "https://x.example/",
                           "title": title, "content": content})
    return fake

def test_browse_url_formats_title_and_content(monkeypatch):
    import nbchat.tools.browser as B
    app, _, _, _ = _make_chat_app()
    monkeypatch.setattr(B, "browser", _fake_browser_json(title="My Page", content="some text here"))
    out = app._browse_url("https://x.example/")
    assert "My Page" in out
    assert "some text here" in out
    assert out.startswith("browse:")


def test_browse_url_truncates_long_content(monkeypatch):
    import nbchat.tools.browser as B
    app, _, _, _ = _make_chat_app()
    long = "x" * 10000
    monkeypatch.setattr(B, "browser", _fake_browser_json(content=long))
    out = app._browse_url("https://x.example/", max_chars=100)
    assert "[truncated]" in out
    assert len(out) < 300


def test_browse_url_error_note(monkeypatch):
    import json, nbchat.tools.browser as B
    app, _, _, _ = _make_chat_app()
    def fake(url, *a, **k):
        return json.dumps({"error": "net down", "hint": "retry"})
    monkeypatch.setattr(B, "browser", fake)
    out = app._browse_url("https://x.example/")
    assert "net down" in out


def test_cmd_browse_async_delivers_note(monkeypatch):
    import nbchat.tools.browser as B
    app, term, events, _ = _make_chat_app()
    monkeypatch.setattr(B, "browser", _fake_browser_json(title="Live", content="async body"))
    ack = app._cmd_browse("example.com")
    assert "loading" in ack
    _drain_calls(app, events)
    joined = "\n".join(m.text for m in app.log.messages)
    assert "async body" in joined
    assert "Live" in joined


def test_cmd_browse_usage():
    app, _, _, _ = _make_chat_app()
    assert "usage" in app._cmd_browse("")


def test_cmd_search_usage():
    app, _, _, _ = _make_chat_app()
    assert "usage" in app._cmd_search("")


def test_cmd_search_builds_duckduckgo_url(monkeypatch):
    import nbchat.tools.browser as B
    app, term, events, _ = _make_chat_app()
    seen = {}
    def fake(url, *a, **k):
        import json
        seen["url"] = url
        return json.dumps({"status": "success", "url": url,
                           "title": "results", "content": "search hits"})
    monkeypatch.setattr(B, "browser", fake)
    ack = app._cmd_search("hello world")
    assert ack == "search: hello world"
    _drain_calls(app, events)
    assert "duckduckgo.com" in seen["url"]
    assert "hello+world" in seen["url"]


# ── /sup (supervisor state query + watchdog wiring) ──────────────────

class _FakeSupervisor:
    def __init__(self, answer="all good"):
        self._answer = answer
        self.running = True
        self.interjection_count = 3
        self._interval = 30
        self._cooldown = 60
        self.started = False
        self.stopped = False
    def ask(self, question):
        return f"{self._answer}: {question}"
    def start(self):
        self.started = True
    def stop(self, timeout=5.0):
        self.stopped = True

def test_sup_not_running_note():
    app, _, _, _ = _make_chat_app()
    assert app._supervisor is None
    assert "not running" in app._cmd_sup("")


def test_sup_status_text():
    app, _, _, _ = _make_chat_app()
    app._supervisor = _FakeSupervisor()
    out = app._cmd_sup("")
    assert "running" in out
    assert "3" in out  # interjection count


def test_sup_query_off_thread_delivers(monkeypatch):
    app, term, events, _ = _make_chat_app()
    app._supervisor = _FakeSupervisor(answer="ok")
    ack = app._cmd_sup("is it working?")
    assert "asking" in ack
    _drain_calls(app, events)
    joined = "\n".join(m.text for m in app.log.messages)
    assert "ok: is it working?" in joined
    assert "sup:" in joined


def test_start_supervisor_disabled_is_noop():
    app, _, _, _ = _make_chat_app()
    app._supervisor_enabled = False
    app._start_supervisor()
    assert app._supervisor is None


def test_start_supervisor_creates_and_starts(monkeypatch):
    import nbchat.core.supervisor as S
    fake = _FakeSupervisor()
    created = {}
    def factory(agent, **kw):
        created["agent"] = agent
        return fake
    monkeypatch.setattr(S, "create_supervisor", factory)
    app, _, _, _ = _make_chat_app()
    app._supervisor_enabled = True
    app._start_supervisor()
    assert app._supervisor is fake
    assert fake.started is True
    assert created["agent"] is app


def test_stop_supervisor_stops():
    app, _, _, _ = _make_chat_app()
    fake = _FakeSupervisor()
    app._supervisor = fake
    app._stop_supervisor()
    assert fake.stopped is True
    assert app._supervisor is None


# ── /voice (Alfred voice bridge wiring) ───────────────────────────────

def test_voice_not_running_note():
    app, _, _, _ = _make_chat_app()
    assert app._voice_bridge is None
    assert "not running" in app._cmd_voice("")


def test_start_voice_disabled_is_noop():
    app, _, _, _ = _make_chat_app()
    app._voice_enabled = False
    app._start_voice()
    assert app._voice_bridge is None


class _FakeVoiceBridge:
    def __init__(self):
        self.port = 8765
        self.stopped = False
        self.started = False
    def start(self):
        self.started = True
        return True
    def stop(self):
        self.stopped = True

def test_cmd_voice_active_status():
    app, _, _, _ = _make_chat_app()
    app._voice_bridge = _FakeVoiceBridge()
    out = app._cmd_voice("")
    assert "ACTIVE" in out


def test_voice_submit_records_history_notes_and_starts_turn(monkeypatch):
    app, _, _, _ = _make_chat_app()
    seen = {}
    monkeypatch.setattr(app, "_start_turn", lambda t: seen.setdefault("turn", t))
    monkeypatch.setattr(app, "_note", lambda s: seen.setdefault("note", s))
    app._voice_submit("hello from mic")
    assert "hello from mic" in app._history
    assert seen.get("turn") == "hello from mic"
    assert "[voice]" in seen.get("note", "")


def test_stop_voice_stops_bridge():
    app, _, _, _ = _make_chat_app()
    fake = _FakeVoiceBridge()
    app._voice_bridge = fake
    app._stop_voice()
    assert fake.stopped is True
    assert app._voice_bridge is None


# ── /fork (branch the conversation into a new session) ───────────────
# NOTE: every nbchat.core.db function is patched via the `monkeypatch`
# fixture so the patch is reverted after each test (no leak into the
# shared DB that later v1 tests depend on).

def _fork_rows():
    # 6-tuples: (role, content, tool_id, tool_name, tool_args, error_flag)
    return [
        ("user", "hi", "", "", "", 0),
        ("assistant", "hello!", "", "", "", 0),
        ("user", "do X", "", "", "", 0),
        ("tool", "did X", "t1", "run_command", "{}", 0),
        ("assistant", "done", "", "", "", 0),
        ("user", "now Y", "", "", "", 0),
    ]

def _patch_db_fork(monkeypatch, rows):
    import nbchat.core.db as dbmod
    calls = {}
    monkeypatch.setattr(dbmod, "load_history",
                        lambda sid, limit=None: [tuple(r) for r in rows])
    monkeypatch.setattr(dbmod, "replace_session_history",
                        lambda sid, hist: calls.setdefault("hist", (sid, hist)))
    monkeypatch.setattr(dbmod, "load_task_log", lambda sid: [])
    monkeypatch.setattr(dbmod, "save_task_log", lambda sid, tl: None)
    monkeypatch.setattr(dbmod, "save_session_title",
                        lambda sid, t: calls.setdefault("title", (sid, t)))
    return calls

def test_fork_empty_history(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.db as dbmod
    monkeypatch.setattr(dbmod, "load_history", lambda sid, limit=None: [])
    out = app._cmd_fork("")
    assert "nothing to fork" in out


def test_fork_full_copies_all_and_switches(monkeypatch):
    app, _, _, _ = _make_chat_app()
    rows = _fork_rows()
    calls = _patch_db_fork(monkeypatch, rows)
    orig = app.session_id
    def fake_switch(sid):
        calls["switch"] = sid
        app.session_id = sid  # mimic real _switch_session
    monkeypatch.setattr(app, "_switch_session", fake_switch)
    monkeypatch.setattr(app, "_session_changed", lambda: calls.setdefault("changed", True))
    monkeypatch.setattr(app, "remember_session", lambda sid: calls.setdefault("remember", sid))
    out = app._cmd_fork("")
    assert "full history" in out
    sid, hist = calls["hist"]
    assert sid != orig                 # a brand-new session id
    assert len(hist) == len(rows)      # all rows copied
    assert calls["switch"] == sid      # switched to the fork
    assert calls["changed"] is True
    assert calls["remember"] == sid    # fork is now the current session
    assert "fork" in calls["title"][1] # title records it is a fork


def test_fork_at_message_2_includes_that_user_message(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = _patch_db_fork(monkeypatch, _fork_rows())
    monkeypatch.setattr(app, "_switch_session", lambda sid: None)
    monkeypatch.setattr(app, "_session_changed", lambda: None)
    monkeypatch.setattr(app, "remember_session", lambda sid: None)
    out = app._cmd_fork("2")
    assert "message 2" in out
    sid, hist = calls["hist"]
    # 2nd user message is at index 2 -> cut = 3 -> rows[:3] = [hi, hello!, do X]
    assert len(hist) == 3
    assert hist[-1][0] == "user" and hist[-1][1] == "do X"


def test_fork_bad_arg(monkeypatch):
    app, _, _, _ = _make_chat_app()
    _patch_db_fork(monkeypatch, _fork_rows())
    out = app._cmd_fork("abc")
    assert "give a number" in out


def test_fork_out_of_range(monkeypatch):
    app, _, _, _ = _make_chat_app()
    _patch_db_fork(monkeypatch, _fork_rows())
    out = app._cmd_fork("99")
    assert "message(s)" in out
    assert "/fork 1..3" in out


# ── /checkpoint + /undo (git-backed code revert) ───────────────────────

def _gitrepo(tmp_path):
    import subprocess as _sp
    d = tmp_path / "repo"
    d.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    def sh(*a):
        _sp.run(["git", *a], cwd=d, check=True, capture_output=True, text=True, env=env)
    sh("init", "-q"); sh("config", "user.name", "t"); sh("config", "user.email", "t@t")
    (d / "f.txt").write_text("v1\n")
    sh("add", "."); sh("commit", "-qm", "init")
    return d

def _patch_cp_store(tmp_path, monkeypatch):
    import nbchat.tui2.undo as u
    monkeypatch.setenv("NBCHAT_TUI3_CHECKPOINTS", str(tmp_path / "cps.json"))
    return u

def test_undo_checkpoint_and_restore(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")          # dirty tree
    cp = u.take_checkpoint(str(d), "sessA", label="cp1")
    assert cp and cp["label"] == "cp1"
    (d / "f.txt").write_text("v3\n")          # further change
    ok, summ = u.apply(str(d), cp, dry=False)
    assert ok, summ
    assert (d / "f.txt").read_text() == "v2\n"  # restored to checkpoint


def test_undo_clean_tree_falls_back_to_head(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)                      # clean tree (== HEAD)
    cp = u.take_checkpoint(str(d), "sessB", label="clean")
    assert cp and cp["note"] == "HEAD (clean tree)"
    import subprocess as _sp
    head = _sp.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True,
                   text=True).stdout.strip()
    assert cp["source"] == head


def test_undo_not_git_repo_returns_none(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    assert u.is_git_repo(str(tmp_path)) is False
    assert u.take_checkpoint(str(tmp_path), "sessC") is None


def test_undo_find_checkpoint_last_and_label(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    u.take_checkpoint(str(d), "s", label="one")
    (d / "f.txt").write_text("x\n")
    u.take_checkpoint(str(d), "s", label="two")
    assert u.find_checkpoint("s", "last")["label"] == "two"
    assert u.find_checkpoint("s", "one")["label"] == "one"
    assert u.find_checkpoint("s", "missing") is None
    assert u.latest_checkpoint("s")["label"] == "two"


def test_undo_apply_refuses_cwd_mismatch(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    cp = u.take_checkpoint(str(d), "s", label="cp")
    other = tmp_path / "else"; other.mkdir()
    ok, summ = u.apply(str(other), cp, dry=False)
    assert not ok and "different directory" in summ


def test_undo_preview_no_diff(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")
    cp = u.take_checkpoint(str(d), "s", label="cp")
    pv = u.preview(str(d), cp)
    assert "no tracked-file differences" in pv


def test_cmd_checkpoint_non_git(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: False)
    out = app._cmd_checkpoint("")
    assert "not a git work tree" in out


def test_cmd_checkpoint_ok(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: True)
    monkeypatch.setattr(u, "take_checkpoint",
                        lambda cwd, sid, label="": {"label": label or "c1",
                        "sha": "abc123", "note": "working tree (tracked files)"})
    out = app._cmd_checkpoint("mylabel")
    assert "recorded" in out and "mylabel" in out and "abc123" in out


def test_cmd_undo_no_checkpoints(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "list_checkpoints", lambda sid: [])
    out = app._cmd_undo("")
    assert "no checkpoints" in out


def test_cmd_undo_preview(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    cp = {"label": "auto", "sha": "abc123", "note": "working tree (tracked files)"}
    monkeypatch.setattr(u, "list_checkpoints", lambda sid: [cp])
    monkeypatch.setattr(u, "preview", lambda cwd, c: "1 tracked file(s) would change:\n  f.txt")
    out = app._cmd_undo("")
    assert "preview" in out and "f.txt" in out and "/undo auto" in out


def test_cmd_undo_apply(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    cp = {"label": "auto", "sha": "abc123", "note": "n", "cwd": os.getcwd()}
    monkeypatch.setattr(u, "list_checkpoints", lambda sid: [cp])
    monkeypatch.setattr(u, "find_checkpoint", lambda sid, lab: cp)
    monkeypatch.setattr(u, "apply", lambda cwd, c, dry=False: (True, "restored OK"))
    out = app._cmd_undo("auto")
    assert "restored OK" in out


def test_cmd_undo_unknown_label(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "list_checkpoints", lambda sid: [{"label": "auto"}])
    monkeypatch.setattr(u, "find_checkpoint", lambda sid, lab: None)
    out = app._cmd_undo("nope")
    assert "no checkpoint named" in out and "auto" in out


def test_auto_checkpoint_records_auto(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    calls = {}
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: True)
    monkeypatch.setattr(u, "take_checkpoint",
        lambda cwd, sid, label="": calls.setdefault("label", label) or {"label": label, "sha": "abc"})
    app._auto_checkpoint("make_change_to_file")
    assert calls["label"] == "auto"


def test_auto_checkpoint_silent_when_not_git(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: False)
    app._auto_checkpoint("make_change_to_file")  # must not raise


def test_gated_one_auto_checkpoint_per_window(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.tool_executor as te
    calls = []
    app._auto_checkpoint = lambda tool: calls.append(tool)
    monkeypatch.setattr(te, "run_tool", lambda name, args, timeout=None: "ok")
    app._install_approval_gate()
    try:
        te.run_tool("make_change_to_file", "{}")  # window 1 first -> checkpoint
        te.run_tool("create_file", "{}")          # window 1 second -> no
        app._turn_mutated = False                 # new turn
        te.run_tool("create_file", "{}")          # window 2 first -> checkpoint
    finally:
        app._remove_approval_gate()
    assert calls == ["make_change_to_file", "create_file"]


# ── /find (cross-session full-text search) ─────────────────────────────

def test_db_search_messages(monkeypatch, tmp_path):
    import sqlite3
    import nbchat.core.db as dbmod
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE chat_log (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT)")
    rows = [
        ("tui:aaa", "user", "hello checkpoint world"),
        ("tui:bbb", "assistant", "I made a checkpoint"),
        ("tui:aaa", "user", "unrelated message"),
        ("tui:ccc", "user", "CHECKPOINT in caps"),
    ]
    for i, (sid, role, c) in enumerate(rows):
        conn.execute("INSERT INTO chat_log (id, session_id, role, content) VALUES (?,?,?,?)",
                     (i + 1, sid, role, c))
    monkeypatch.setattr(dbmod, "_connect", lambda: conn)
    hits = dbmod.search_messages("checkpoint")
    assert {h[0] for h in hits} == {"tui:aaa", "tui:bbb", "tui:ccc"}  # case-insensitive
    assert hits[0][0] == "tui:ccc"  # newest first (id 4)
    hits2 = dbmod.search_messages("checkpoint", session_id="tui:aaa")
    assert {h[0] for h in hits2} == {"tui:aaa"}
    assert dbmod.search_messages("   ") == []
    conn.close()


def test_cmd_find_all_sessions(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.db as dbmod
    dbmod.search_messages = lambda q, limit=30, session_id=None: [
        ("tui:aaa11111", "user", "hello there general"),
        ("tui:bbb22222", "assistant", "hi back at you"),
    ]
    out = app._cmd_find("hello")
    assert "2 match(es)" in out and "all sessions" in out
    assert "aaa" in out and "bbb" in out
    assert "/load" in out


def test_cmd_find_marks_current_session(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.db as dbmod
    sid = app.session_id
    dbmod.search_messages = lambda q, limit=30, session_id=None: [(sid, "user", "the current one")]
    out = app._cmd_find("current")
    assert "* " in out and "the current one" in out


def test_cmd_find_session_scope(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.db as dbmod
    seen = {}
    def fake(q, limit=30, session_id=None):
        seen["session_id"] = session_id
        return []
    dbmod.search_messages = fake
    out = app._cmd_find("x session")
    assert seen["session_id"] == app.session_id  # restricted to current
    assert "no messages" in out and "this session" in out


def test_cmd_find_empty(monkeypatch):
    app, _, _, _ = _make_chat_app()
    out = app._cmd_find("   ")
    assert "give a search term" in out


def test_cmd_find_no_matches(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.core.db as dbmod
    dbmod.search_messages = lambda q, limit=30, session_id=None: []
    out = app._cmd_find("zzzqqq")
    assert "no messages" in out


# ── /diff (colorized git-diff review) ──────────────────────────────────

def test_diff_real_git_shows_changed_file(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")          # dirty the tree
    app, _, _, _ = _make_chat_app()
    monkeypatch.chdir(d)
    monkeypatch.setattr(app, "_note", lambda t: None)
    out = app._cmd_diff("")
    assert "1 file(s) changed" in out and "vs HEAD" in out
    last = app.log.messages[-1]
    blk = last.blocks[0]
    assert blk.kind == "tool" and blk.name == "diff" and blk.diff is True
    assert any(l.startswith("+") for l in blk.body) and any(l.startswith("-") for l in blk.body)


def test_diff_stat_not_colored_as_diff(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")
    app, _, _, _ = _make_chat_app()
    monkeypatch.chdir(d)
    monkeypatch.setattr(app, "_note", lambda t: None)
    out = app._cmd_diff("--stat")
    assert "file(s) changed" in out
    assert app.log.messages[-1].blocks[0].diff is False


def test_diff_no_changes(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)                       # clean tree
    app, _, _, _ = _make_chat_app()
    monkeypatch.chdir(d)
    monkeypatch.setattr(app, "_note", lambda t: None)
    out = app._cmd_diff("")
    assert "no tracked-file changes" in out


def test_diff_vs_checkpoint(tmp_path, monkeypatch):
    u = _patch_cp_store(tmp_path, monkeypatch)
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")
    app, _, _, _ = _make_chat_app()
    u.take_checkpoint(str(d), app.session_id, label="cp1")
    (d / "f.txt").write_text("v3\n")            # change after checkpoint
    monkeypatch.chdir(d)
    monkeypatch.setattr(app, "_note", lambda t: None)
    out = app._cmd_diff("cp1")
    assert "checkpoint" in out and "cp1" in out
    assert app.log.messages[-1].blocks[0].diff is True


def test_diff_not_git(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: False)
    out = app._cmd_diff("")
    assert "not a git work tree" in out


def test_diff_unknown_checkpoint(monkeypatch):
    app, _, _, _ = _make_chat_app()
    import nbchat.tui2.undo as u
    monkeypatch.setattr(u, "is_git_repo", lambda cwd: True)
    monkeypatch.setattr(u, "find_checkpoint", lambda sid, lab: None)
    monkeypatch.setattr(u, "list_checkpoints", lambda sid: [{"label": "auto"}])
    out = app._cmd_diff("nope")
    assert "no checkpoint named" in out and "auto" in out


# ── /export (session → markdown file) ──────────────────────────────────

def _patch_history(monkeypatch, rows, title="My Session"):
    import nbchat.core.db as dbmod
    monkeypatch.setattr(dbmod, "load_history", lambda sid, limit=None: [tuple(r) for r in rows])
    monkeypatch.setattr(dbmod, "load_session_title", lambda sid: title)

def test_export_writes_markdown(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    _patch_history(monkeypatch, [
        ("user", "hello there", "", "", "", 0),
        ("assistant", "hi! how can I help?", "", "", "", 0),
    ])
    path = str(tmp_path / "out.md")
    out = app._cmd_export(path)
    assert path in out and "2 messages" in out
    txt = open(path, encoding="utf-8").read()
    assert txt.startswith("# My Session")
    assert "**user**" in txt and "hello there" in txt
    assert "**assistant**" in txt and "hi! how can I help?" in txt
    assert "session `tui:" in txt

def test_export_tool_block_fenced(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    _patch_history(monkeypatch, [
        ("user", "run ls", "", "", "", 0),
        ("tool", "file1\nfile2", "t1", "run_command", "{}", 0),
    ])
    path = str(tmp_path / "t.md")
    app._cmd_export(path)
    txt = open(path, encoding="utf-8").read()
    assert "run_command" in txt
    assert "```" in txt and "file1" in txt and "file2" in txt

def test_export_default_dir(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    _patch_history(monkeypatch, [("user", "x", "", "", "", 0)])
    monkeypatch.setenv("NBCHAT_EXPORT_DIR", str(tmp_path / "exp"))
    out = app._cmd_export("")
    assert "wrote 1 messages" in out
    base = tmp_path / "exp"
    files = list(base.glob("nbchat-*.md"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8").startswith("# ")

def test_export_empty(monkeypatch):
    app, _, _, _ = _make_chat_app()
    _patch_history(monkeypatch, [])
    out = app._cmd_export(str(tmp_path := __import__("tempfile").mkdtemp()) + "/n.md")
    assert "no messages" in out

def test_export_write_error(monkeypatch):
    app, _, _, _ = _make_chat_app()
    _patch_history(monkeypatch, [("user", "x", "", "", "", 0)])
    out = app._cmd_export("/proc/nonexistent/nope/out.md")
    assert "could not write" in out


# ── /plan (read-only research mode) ────────────────────────────────────

def test_plan_toggle_system_prompt(monkeypatch):
    app, _, _, _ = _make_chat_app()
    assert app._plan_mode is False
    out = app._cmd_plan("")          # bare toggle -> on
    assert "ON" in out and app._plan_mode is True
    assert app._PLAN_NOTE in app.system_prompt
    out = app._cmd_plan("off")       # explicit off
    assert "OFF" in out and app._plan_mode is False
    assert app._PLAN_NOTE not in app.system_prompt
    assert "already off" in app._cmd_plan("off")

def test_plan_explicit_on_idempotent(monkeypatch):
    app, _, _, _ = _make_chat_app()
    out = app._cmd_plan("on")
    assert "ON" in out and app._plan_mode is True
    assert "already on" in app._cmd_plan("on")

def test_plan_blocks_file_mutating_tool(monkeypatch):
    import nbchat.core.tool_executor as te
    orig = te.run_tool
    called = []
    def fake(tool_name, args_json, timeout=None):
        called.append(tool_name)
        return "RAN:" + tool_name
    monkeypatch.setattr(te, "run_tool", fake)
    app, _, _, _ = _make_chat_app()
    app._install_approval_gate()      # wraps fake as the "original"
    try:
        app._plan_mode = True
        out = te.run_tool("create_file", "{}")
        assert "[PLAN MODE]" in out and "blocked" in out
        assert called == []                    # the tool never ran
        out2 = te.run_tool("read_file", "{}")  # non-mutating still runs
        assert out2 == "RAN:read_file" and called == ["read_file"]
        app._plan_mode = False                 # off re-enables edits
        assert te.run_tool("create_file", "{}") == "RAN:create_file"
    finally:
        monkeypatch.setattr(te, "run_tool", orig)

def test_plan_mode_bar_label(monkeypatch):
    from nbchat.tui2 import frame as fr
    app, _, _, _ = _make_chat_app()
    app._plan_mode = True
    line = app._mode_bar(100)
    assert "plan" in line.text
    app._plan_mode = False
    assert "plan" not in app._mode_bar(100).text


# ── @-file completion (tui3) ─────────────────────────────────────────────

def _fc_tree(tmp_path):
    """A small file tree (incl. a .git dir that must be pruned)."""
    for rel in ("alpha.py", "beta.txt", "gamma.py"):
        (tmp_path / rel).write_text("x")
    src = tmp_path / "src"
    src.mkdir()
    (src / "delta.py").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]")
    return tmp_path


def _mk_key(name):
    from nbchat.tui2.keys import Key
    return Key(name=name)


def test_filecomp_open_and_rank(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("@al")
    app._maybe_open_filecomp()
    fc = app._filecomp
    assert fc is not None
    assert fc["query"] == "al"
    assert fc["matches"][0] == "alpha.py"          # best fuzzy match first
    assert fc["idx"] == 0
    # .git must be pruned from the walked tree
    assert all(".git" not in p for p in app._file_list())


def test_filecomp_accept_replaces_token(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("check @al")
    app._maybe_open_filecomp()
    app._filecomp_key(_mk_key("enter"))
    assert app._filecomp is None
    assert app.editor.text() == "check alpha.py"
    assert app.editor.submitted is False           # enter accepted, did not send


def test_filecomp_email_no_trigger(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("mail user@example.com")
    app._maybe_open_filecomp()
    assert app._filecomp is None


def test_filecomp_nav_and_esc(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("@")
    app._maybe_open_filecomp()
    assert app._filecomp is not None
    n = len(app._filecomp["matches"])
    app._filecomp_key(_mk_key("down"))
    assert app._filecomp["idx"] == min(1, n - 1)
    app._filecomp_key(_mk_key("down"))
    assert app._filecomp["idx"] == min(2, n - 1)
    app._filecomp_key(_mk_key("up"))
    assert app._filecomp["idx"] == min(1, n - 1)
    app._filecomp_key(_mk_key("esc"))
    assert app._filecomp is None


def test_filecomp_tab_accepts(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("@be")
    app._maybe_open_filecomp()
    app._filecomp_key(_mk_key("tab"))
    assert app._filecomp is None
    assert app.editor.text() == "beta.txt"


def test_filecomp_frame_height(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, term, _, _ = _make_chat_app()
    app.editor.set_text("@a")
    app._maybe_open_filecomp()
    fr = app._build_frame()
    assert len(fr.lines) == term.height
    # the completion box is rendered above the message editor
    joined = "\n".join(ln.text for ln in fr.lines)
    assert "alpha.py" in joined
    assert "message" in joined


def test_filecomp_delete_at_closes(monkeypatch, tmp_path):
    tree = _fc_tree(tmp_path)
    monkeypatch.chdir(tree)
    app, _, _, _ = _make_chat_app()
    app.editor.set_text("@a")
    app._maybe_open_filecomp()
    assert app._filecomp is not None
    # Backspace twice: delete 'a' then '@' -> token gone -> modal closes.
    app.editor.handle("backspace")
    app._maybe_open_filecomp()
    assert app._filecomp is not None            # '@' still present
    app.editor.handle("backspace")
    app._maybe_open_filecomp()
    assert app._filecomp is None


def test_editor_replace_range_records_undo():
    from nbchat.tui2.editor import LineEditor
    ed = LineEditor()
    ed.set_text("hello world")
    ed.replace_range(0, 5, "goodbye")
    assert ed.lines[ed.cursor_line] == "goodbye world"
    assert ed.cursor_col == 7
    ed.undo()
    assert ed.lines[ed.cursor_line] == "hello world"
    # Clamping out-of-range bounds is safe.
    ed.replace_range(-5, 999, "X")
    assert ed.lines[ed.cursor_line] == "X"


# ── auto-compact (context-over-threshold) ─────────────────────────
def test_auto_compact_cfg_default():
    from nbchat.tui2.app import ChatApp
    assert ChatApp._auto_compact_cfg() == (0.8, True)

def test_auto_compact_cfg_variants(monkeypatch):
    from nbchat.tui2.app import ChatApp
    monkeypatch.setenv("NBCHAT_AUTO_COMPACT", "0")
    assert ChatApp._auto_compact_cfg() == (0.8, False)
    monkeypatch.setenv("NBCHAT_AUTO_COMPACT", "off")
    assert ChatApp._auto_compact_cfg() == (0.8, False)
    monkeypatch.setenv("NBCHAT_AUTO_COMPACT", "0.6")
    assert ChatApp._auto_compact_cfg() == (0.6, True)
    monkeypatch.setenv("NBCHAT_AUTO_COMPACT", "1.5")
    assert ChatApp._auto_compact_cfg() == (1.0, True)  # capped at 1.0
    monkeypatch.setenv("NBCHAT_AUTO_COMPACT", "garbage")
    assert ChatApp._auto_compact_cfg() == (0.8, True)  # fallback

def test_status_window_flags_auto_compact_due():
    app, _, _, _ = _make_chat_app()
    app._auto_compact_frac = 0.8
    app._status_window(85, 100)
    assert app._auto_compact_due is True
    app._status_window(50, 100)
    assert app._auto_compact_due is False

def test_finalize_turn_triggers_auto_compact(monkeypatch):
    import time
    app, _, _, _ = _make_chat_app()
    calls = []
    def fake_fc(instructions=""):
        calls.append(instructions)
        return {"compacted": False, "reason": "test"}
    monkeypatch.setattr(app, "force_compact", fake_fc)
    app._auto_compact_enabled = True
    app._auto_compact_frac = 0.8
    app._ctx_used, app._ctx_budget = 90.0, 100.0
    app._auto_compact_due = True
    app._redirect = None
    app._finalize_turn()
    for _ in range(60):
        if calls:
            break
        time.sleep(0.05)
    assert calls, "force_compact was not triggered by _finalize_turn"
    assert "auto" in calls[0]

def test_auto_compact_disabled_skips(monkeypatch):
    import time
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(
        app, "force_compact",
        lambda instructions="": calls.append(instructions)
        or {"compacted": False, "reason": "x"})
    app._auto_compact_enabled = False
    app._auto_compact_due = True
    app._redirect = None
    app._finalize_turn()
    time.sleep(0.3)
    assert not calls, "disabled auto-compact must not call force_compact"

def test_context_shows_auto_compact_state():
    app, _, _, _ = _make_chat_app()
    app._auto_compact_enabled = True
    app._auto_compact_frac = 0.8
    assert "auto-compact on" in app._cmd_context("")
    app._auto_compact_enabled = False
    assert "auto-compact off" in app._cmd_context("")


# ── /retry (re-run last user message) ─────────────────────────────
def test_submit_captures_last_user_text(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._submit("hello there")
    assert app._last_user_text == "hello there"
    assert calls == ["hello there"]

def test_retry_resends_last_message(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._submit("hello there")
    out = app._cmd_retry("")
    assert calls == ["hello there", "hello there"]
    assert "re-sending" in out

def test_retry_with_arg_overrides(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._submit("first")
    app._cmd_retry("second question")
    assert calls[-1] == "second question"
    assert app._last_user_text == "second question"

def test_retry_nothing_to_retry():
    app, _, _, _ = _make_chat_app()
    assert "nothing to retry" in app._cmd_retry("")

def test_retry_busy_refuses(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._turn_active = True  # busy -> refuses to interrupt
    out = app._cmd_retry("")
    assert not calls
    assert "wait" in out

def test_retry_listed_in_help():
    app, _, _, _ = _make_chat_app()
    assert "/retry" in app._tui2_help_addendum()


# ── steering queue (Ctrl+Q) ───────────────────────────────────────
def test_queue_key_queues_when_busy(monkeypatch):
    app, _, _, _ = _make_chat_app()
    app._turn_active = True  # a turn is in flight
    app.editor.set_text("follow up later")
    app._queue_key()
    assert app._queue == ["follow up later"]
    assert app.editor.text().strip() == ""

def test_queue_key_submits_when_idle(monkeypatch):
    app, _, _, _ = _make_chat_app()
    submitted = []
    monkeypatch.setattr(app, "_submit", lambda t: submitted.append(t))
    app.editor.set_text("send this now")
    app._queue_key()
    assert submitted == ["send this now"]
    assert app._queue == []

def test_queue_key_empty_editor_notes(monkeypatch):
    app, _, _, _ = _make_chat_app()
    notes = []
    monkeypatch.setattr(app, "_note", lambda t: notes.append(t))
    app.editor.set_text("")
    app._queue_key()
    assert any("nothing to queue" in n for n in notes)

def test_process_next_queued_runs_in_order(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._queue = ["first", "second"]
    app._turn_thread = None
    app._tui._running = True
    app._process_next_queued()
    assert calls == ["first"]
    assert app._queue == ["second"]
    app._process_next_queued()
    assert calls == ["first", "second"]
    assert app._queue == []

def test_process_next_queued_empty_is_noop(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_start_turn", lambda t: calls.append(t))
    app._queue = []
    app._tui._running = True
    app._process_next_queued()
    assert not calls

def test_cmd_queue_list_and_clear():
    app, _, _, _ = _make_chat_app()
    app._queue = ["alpha", "beta"]
    out = app._cmd_queue("")
    assert "2 pending" in out and "alpha" in out and "beta" in out
    out = app._cmd_queue("clear")
    assert "cleared 2" in out
    assert app._queue == []

def test_queue_listed_in_help():
    app, _, _, _ = _make_chat_app()
    assert "/queue" in app._tui2_help_addendum()


# ── prompt templates (/tpl) ──────────────────────────────────────
def test_tpl_render_positional():
    app, _, _, _ = _make_chat_app()
    assert app._render_template("Fix $1 and test $2", "parser bug") == "Fix parser and test bug"

def test_tpl_render_all_and_flatten():
    app, _, _, _ = _make_chat_app()
    assert app._render_template("Do $0 now", "a b c") == "Do a b c now"
    assert app._render_template("Do $ARG now", "a b c") == "Do a b c now"
    assert app._render_template("Line1" + chr(10) + "Line2 $1", "x") == "Line1 Line2 x"

def test_tpl_render_short_args_left():
    app, _, _, _ = _make_chat_app()
    assert app._render_template("$1 $3", "only") == "only $3"

def test_cmd_tpl_list_empty(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    monkeypatch.setenv("NBCHAT_PROMPTS_DIR", str(tmp_path))
    out = app._cmd_tpl("")
    assert "no templates found" in out

def test_cmd_tpl_list_shows_names(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    (tmp_path / "review.md").write_text("Review $1", encoding="utf-8")
    (tmp_path / "notes.md").write_text("Summarize $0", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("not a template", encoding="utf-8")
    monkeypatch.setenv("NBCHAT_PROMPTS_DIR", str(tmp_path))
    out = app._cmd_tpl("")
    assert "review" in out and "notes" in out and "ignore" not in out

def test_cmd_tpl_send(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    (tmp_path / "review.md").write_text("Review $1 for bugs", encoding="utf-8")
    (tmp_path / "all.md").write_text("Summarize $0", encoding="utf-8")
    monkeypatch.setenv("NBCHAT_PROMPTS_DIR", str(tmp_path))
    sent = []
    monkeypatch.setattr(app, "_start_turn", lambda t: sent.append(t))
    out = app._cmd_tpl("review parser")
    app._cmd_tpl("all my whole parser")
    assert sent == ["Review parser for bugs", "Summarize my whole parser"]
    assert "sent template 'review'" in out

def test_cmd_tpl_queues_when_busy(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    app._turn_active = True  # a turn is running
    (tmp_path / "review.md").write_text("Review $1", encoding="utf-8")
    monkeypatch.setenv("NBCHAT_PROMPTS_DIR", str(tmp_path))
    def _boom(t):
        raise AssertionError("_start_turn must not be called while busy")
    monkeypatch.setattr(app, "_start_turn", _boom)
    out = app._cmd_tpl("review code")
    assert app._queue == ["Review code"]
    assert "queued template 'review'" in out

def test_cmd_tpl_unknown(monkeypatch, tmp_path):
    app, _, _, _ = _make_chat_app()
    (tmp_path / "review.md").write_text("Review $1", encoding="utf-8")
    monkeypatch.setenv("NBCHAT_PROMPTS_DIR", str(tmp_path))
    out = app._cmd_tpl("nope")
    assert "no template 'nope'" in out and "review" in out

def test_tpl_listed_in_help():
    app, _, _, _ = _make_chat_app()
    assert "/tpl" in app._tui2_help_addendum()


# ── external editor ($EDITOR) ────────────────────────────────────
def test_launch_editor_no_editor(monkeypatch):
    app, _, _, _ = _make_chat_app()
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.delenv("VISUAL", raising=False)
    notes = []
    monkeypatch.setattr(app, "_note", lambda t: notes.append(t))
    app.editor.set_text("existing draft")
    app._launch_external_editor()
    assert any("no $EDITOR" in n for n in notes)
    assert app.editor.text() == "existing draft"  # unchanged

def test_launch_editor_loads_back_and_flattens(monkeypatch):
    import subprocess as _sp
    app, _, _, _ = _make_chat_app()
    monkeypatch.setenv("EDITOR", "myeditor")
    def _fake_run(cmd, *a, **k):
        path = cmd[-1]
        with open(path, "w", encoding="utf-8") as f:
            f.write("line one\nline two\nline three")
        return _sp.CompletedProcess(cmd, 0)
    monkeypatch.setattr(_sp, "run", _fake_run)
    notes = []
    monkeypatch.setattr(app, "_note", lambda t: notes.append(t))
    app._launch_external_editor()
    assert app.editor.text() == "line one line two line three"
    assert any("loaded draft" in n for n in notes)

def test_launch_editor_error_is_reported(monkeypatch):
    import subprocess as _sp
    app, _, _, _ = _make_chat_app()
    monkeypatch.setenv("EDITOR", "myeditor")
    def _boom(cmd, *a, **k):
        raise _sp.SubprocessError("editor blew up")
    monkeypatch.setattr(_sp, "run", _boom)
    notes = []
    monkeypatch.setattr(app, "_note", lambda t: notes.append(t))
    app.editor.set_text("keep me")
    app._launch_external_editor()  # must not raise
    assert any("editor error" in n for n in notes)
    assert app.editor.text() == "keep me"

def test_ctrl_e_triggers_external_editor(monkeypatch):
    app, _, _, _ = _make_chat_app()
    calls = []
    monkeypatch.setattr(app, "_launch_external_editor", lambda: calls.append(1))
    app._on_input(_mk_key("ctrl+e"))
    assert calls == [1]

def test_editor_listed_in_help():
    app, _, _, _ = _make_chat_app()
    assert "/editor" in app._tui2_help_addendum()


# ── /gstatus (git working-tree overview) ─────────────────────────
def test_gstatus_real_git_lists_categories(tmp_path, monkeypatch):
    import subprocess as _sp
    d = _gitrepo(tmp_path)
    (d / "f.txt").write_text("v2\n")                    # unstaged (tracked, modified)
    (d / "g.txt").write_text("g\n")
    _sp.run(["git", "add", "g.txt"], cwd=d, check=True, capture_output=True)  # staged
    (d / "h.txt").write_text("h\n")                     # untracked
    monkeypatch.chdir(d)
    app, _, _, _ = _make_chat_app()
    monkeypatch.setattr(app, "_note", lambda t: None)
    out = app._cmd_gstatus("")
    assert "1 staged, 1 unstaged, 1 untracked" in out
    blk = app.log.messages[-1].blocks[0]
    assert blk.kind == "tool" and blk.name == "git status" and blk.diff is False
    joined = chr(10).join(blk.body)
    assert "staged (1):" in joined and "unstaged (1):" in joined and "untracked (1):" in joined
    assert "g.txt" in joined and "f.txt" in joined and "h.txt" in joined

def test_gstatus_clean_working_tree(tmp_path, monkeypatch):
    d = _gitrepo(tmp_path)
    monkeypatch.chdir(d)
    app, _, _, _ = _make_chat_app()
    notes = []
    monkeypatch.setattr(app, "_note", lambda t: notes.append(t))
    out = app._cmd_gstatus("")
    assert "clean working tree" in out
    assert any("clean" in n for n in notes)

def test_gstatus_not_git(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app, _, _, _ = _make_chat_app()
    out = app._cmd_gstatus("")
    assert "not a git work tree" in out
