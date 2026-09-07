"""TUI v2 — a prime-agent-style fullscreen terminal UI for nbchat.

This is a *new* sub-package (``nbchat.tui2``) and is completely
independent from the existing print-based REPL in :mod:`nbchat.tui`.
The old entry point (``python -m nbchat.tui`` / ``nbchat_tui.py``) is
untouched and keeps working exactly as before.

Phase 1 modules:

* :mod:`nbchat.tui2.frame` — screen model and differential renderer
* :mod:`nbchat.tui2.raw`   — raw terminal, event queue, app loop

See ``docs/prime_tui_port_tracker.md`` for the phased plan and status.
"""
from .frame import Frame, Line, Segment, Style, diff_frames, render_frame, sync_out
from .raw import EventQueue, RawTerminal, TUIApp
from .keys import Key, KeyReader
from .chat import (
    Markdown,
    Message,
    ToolCall,
    ThinkingBlock,
    diff_lines,
)

__all__ = [
    "Frame",
    "Line",
    "Segment",
    "Style",
    "diff_frames",
    "render_frame",
    "sync_out",
    "EventQueue",
    "RawTerminal",
    "TUIApp",
    "Key",
    "KeyReader",
    "Markdown",
    "Message",
    "ToolCall",
    "ThinkingBlock",
    "diff_lines",
]
