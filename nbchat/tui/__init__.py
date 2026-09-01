"""nbchat.tui — minimal terminal UI for nbchat.

Headless terminal REPL that reuses the full agent stack (``ContextMixin`` +
``ConversationMixin``): L1/L2 memory, context windowing, tool execution,
compression and streaming — with a plain-text terminal frontend (no Jupyter,
no ipywidgets).

Start it with a single command::

    python -m nbchat.tui
"""
from nbchat.tui.agent import TerminalAgent
from nbchat.tui.colors import Palette
from nbchat.tui.app import run

__all__ = ["TerminalAgent", "Palette", "run"]
