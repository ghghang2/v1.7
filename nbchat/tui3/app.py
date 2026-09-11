"""The tui3 application: the tui2 base extended with the tui3 feature wave.

This module defines the tui3 ``ChatApp`` (a subclass of the tui2 ``ChatApp``)
and the tui3 ``run`` entry point.  tui3 features are added here as
overrides/extensions of the tui2 ``ChatApp`` methods, so the tui2 base is
left untouched and the version-to-feature-set boundary is explicit.

tui3 feature wave (implemented in phases, each tested + committed + pushed):

* **Phase 1 - ``/trace``** (live task-trace / observability view): a
  structured, scrollable view of the recent agent activity for the current
  session (user turns, assistant turns, tool calls, errors).  Reads the
  existing conversation history - no new data plumbing.  Inspired by CrewAI
  Tracing & Observability + LangGraph durable execution.
"""

from __future__ import annotations

import sys

from typing import List

from nbchat.tui2.app import ChatApp as _Tui2ChatApp, _to_lines, run as _tui2_run
from nbchat.tui2.components import Box
from nbchat.tui2.frame import Line

#: The tui3 version label (wave 2 on top of the tui2 base).
VERSION = "tui3"


class ChatApp(_Tui2ChatApp):
    """The tui3 application.

    Inherits the entire tui2 feature set and adds the tui3 feature wave.
    tui3-native slash commands are intercepted in :meth:`_run_command`
    BEFORE the tui2 dispatch, so they are clearly delineated from the tui2
    command surface.
    """

    #: tui3-native slash commands (intercepted before the tui2 dispatch).
    _TUI3_NATIVE = ("/trace", "/budget")

    def __init__(self, term, events, *args, **kwargs):
        super().__init__(term, events, *args, **kwargs)
        # tui3 state (initialized after the tui2 base is up).
        self._tui3_ready = True

    # -- tui3 command dispatch -------------------------------------------
    def _run_command(self, line: str) -> None:
        """Route a slash command, intercepting the tui3-native ones first."""
        parts = line.strip().split(None, 1)
        cmd = parts[0].lower()
        if cmd in self._TUI3_NATIVE:
            arg = parts[1].strip() if len(parts) > 1 else ""
            self._run_tui3_command(cmd, arg)
            return
        # Fall through to the tui2 dispatch (which also handles v1).
        super()._run_command(line)

    def _run_tui3_command(self, cmd: str, arg: str) -> None:
        """Dispatch a tui3-native slash command to its handler."""
        handlers = {
            "/trace": self._cmd_trace,
            # "/budget": self._cmd_budget,  # Phase 3
        }
        fn = handlers.get(cmd)
        if fn is None:
            self._note("unknown tui3 command: " + cmd)
            return
        try:
            out = fn(arg)
        except Exception as exc:
            out = "command error: %s: %s" % (type(exc).__name__, exc)
        if out:
            self._note(out)

    # -- Phase 1: /trace --------------------------------------------------
    def _cmd_trace(self, arg: str) -> str:
        """Show a trace of the recent agent activity for the current session.

        ``/trace``           - last 40 steps + a summary.
        ``/trace N``         - last N steps (1..500).
        ``/trace errors``    - only the steps that errored.
        ``/trace tools``     - only the tool calls.
        """
        import nbchat.core.db as db

        sid = self.session_id
        steps = (arg or "").strip()
        n = 40
        only = ""
        if steps.isdigit():
            n = max(1, min(500, int(steps)))
        elif steps in ("errors", "tools", "all"):
            only = steps
        try:
            rows = db.load_history(sid)
        except Exception as exc:
            return "trace: failed to load history: %s" % exc
        if not rows:
            return "trace: no history for this session yet"

        n_user = sum(1 for r in rows if r[0] == "user")
        n_assist = sum(1 for r in rows if r[0] == "assistant")
        n_tool = sum(1 for r in rows if r[0] == "tool")
        # The error flag is only meaningful for tool rows (it is a structured
        # outcome check); user/assistant rows use a keyword heuristic, so they
        # are not counted as errors here.
        n_err = sum(1 for r in rows if r[0] == "tool" and r[5])

        # Build the per-step trace (tag, text, errored).
        trace = []
        for role, content, _tid, tool_name, tool_args, err in rows:
            c = (content or "").strip().replace("\n", " ")
            if role == "user":
                trace.append(("U", c[:90], False))
            elif role == "assistant":
                # assistant rows: err is a keyword heuristic, not a real error
                trace.append(("A", c[:90], False))
            elif role == "tool":
                args_b = (tool_args or "").strip().replace("\n", " ")
                if len(args_b) > 40:
                    args_b = args_b[:37] + "..."
                label = tool_name
                if args_b:
                    label = label + " " + args_b
                res = c[:44] + ("..." if len(c) > 44 else "")
                trace.append(("T", label + "  ->  " + res, bool(err)))

        if only == "errors":
            trace = [t for t in trace if t[2]]
        elif only == "tools":
            trace = [t for t in trace if t[0] == "T"]
        trace = trace[-n:]

        lines = []
        for tag, text, err in trace:
            prefix = {"U": "you", "A": "nbchat", "T": "tool"}[tag]
            marker = " [ERR]" if err else ""
            lines.append(prefix + marker + " " + text)

        summary = ("trace  " + str(n_user) + " you  " + str(n_assist) +
                   " nbchat  " + str(n_tool) + " tools  " + str(n_err) +
                   " err")
        if only and only != "all":
            summary = summary + "  (filter: " + only + ")"
        if not lines:
            return summary + "  (nothing matched the filter)"
        return summary + "\n" + "\n".join(lines)

    # -- Phase 2: approval diff-preview (HITL upgrade) --------------------
    def _tool_description(self, tool: str) -> str:
        """Look up a human description of a tool from the tool registry."""
        try:
            import nbchat.tools as _tools
            for t in _tools.TOOLS:
                if t.name == tool:
                    return (t.description or "").strip()
        except Exception:
            pass
        return ""

    def _tool_preview(self, tool: str, args: str) -> str:
        """Build a short human preview of the intended tool change.

        For the common tool shapes (a file path, a command), this surfaces the
        key detail a reviewer actually needs to approve/deny, instead of a raw
        arg blob.
        """
        a = (args or "").strip()
        if not a:
            return ""
        obj = None
        try:
            import json as _json
            obj = _json.loads(a)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            for key in ("path", "file", "file_path", "command", "cmd", "url"):
                if key in obj and obj[key] is not None:
                    val = str(obj[key]).strip()
                    if val:
                        return val[:120]
        return a[:120]

    def _approval_lines(self, w: int):
        """Render the pending tool-approval prompt with a richer preview.

        Phase 2 (HITL upgrade): show the tool name + a short description (from
        the tool registry) + a preview of the intended change (the key detail
        a reviewer needs), instead of just the tool name + a raw arg blob.
        """
        a = self._approval
        tool = (a or {}).get("tool", "?")
        args = (a or {}).get("args", "")
        inner = w - 4
        lines: List[Line] = []
        desc = self._tool_description(tool)
        if desc:
            head = "\u25b8 " + tool + "  -  " + desc[:56]
        else:
            head = "\u25b8 " + tool
        lines.extend(_to_lines(head, inner))
        preview = self._tool_preview(tool, args)
        lines.extend(_to_lines(preview if preview else "(no args)", inner))
        lines.extend(_to_lines("y approve \u00b7 n deny \u00b7 a always", inner))
        lines = lines[:3]  # keep the prompt to 3 inner rows
        return Box(title="approve tool", lines=lines, clip=True).render(w)



def run(argv: list | None = None) -> int:
    """``python -m nbchat.tui3`` entry point.

    Strips the ``--v3`` flag (if present) and launches the tui3 ``ChatApp``
    through the tui2 entry plumbing (so ``--new``/``--session``/``--bg``/
    ``--supervisor``/``--voice`` all work unchanged).
    """
    argv = list(argv if argv is not None else sys.argv[1:])
    argv = [a for a in argv if a not in ("--v3",)]
    return _tui2_run(argv, chat_app_cls=ChatApp)


if __name__ == "__main__":
    raise SystemExit(run())
