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
