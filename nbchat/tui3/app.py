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
import time

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
    _TUI3_NATIVE = ("/trace", "/budget", "/reflect", "/verify", "/health")

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
            "/budget": self._cmd_budget,
            "/reflect": self._cmd_reflect,
            "/verify": self._cmd_verify,
            "/health": self._cmd_health,
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

    # -- Phase 3: /budget (cost / token tracking) -------------------------
    def _cmd_budget(self, arg: str) -> str:
        """Show the token / cost usage for the current session.

        ``/budget``            - the ACTUAL LLM token usage (from the
          main-agent meter) + a per-session conversation summary + an
          estimate of the current conversation's token footprint.
        ``/budget reset``      - reset the main-agent token meter to zero.
        """
        if (arg or "").strip().lower() == "reset":
            try:
                from nbchat.core import team_metrics as _tm
                _tm.reset_main_tokens()
            except Exception as exc:
                return "budget: reset failed: %s" % exc
            return "budget: token meter reset to zero"

        import nbchat.core.db as db
        from nbchat.core import team_metrics as _tm

        sid = self.session_id
        stats = _tm.main_token_stats()
        try:
            rows = db.load_history(sid)
        except Exception:
            rows = []
        n_user = sum(1 for r in rows if r[0] == "user")
        n_assist = sum(1 for r in rows if r[0] == "assistant")
        n_tool = sum(1 for r in rows if r[0] == "tool")
        total_chars = sum(len(r[1] or "") for r in rows)
        # Rough token estimate for the CURRENT conversation (chars / 4).
        est_tokens = total_chars // 4
        lines = []
        lines.append("budget")
        lines.append("  actual LLM tokens: %d  (across %d completion%s)" % (
            stats["total_tokens"], stats["calls"],
            "" if stats["calls"] == 1 else "s"))
        lines.append("  this session:      %d you  %d nbchat  %d tools" % (
            n_user, n_assist, n_tool))
        lines.append("  conversation size: %d chars (~%d tokens est.)" % (
            total_chars, est_tokens))
        lines.append("  note: the token meter is per TUI instance (reset with "
                     "/budget reset)")
        return "\n".join(lines)

    # -- Phase 4: /reflect (deep-agent plan loop - reflect step) -----------
    def _cmd_reflect(self, arg: str) -> str:
        """Reflect on the current session (the reflect step of a plan loop).

        Asks the LLM (on an isolated throwaway agent) to reflect on the recent
        conversation: what has been accomplished, what remains, and the next
        concrete step.  Runs in a background thread so it never blocks the UI;
        the reflection is appended to the log when it arrives.  The side
        question is kept out of the current session history (a throwaway ``refl:``
        session), so it does not pollute the context.
        """
        if self.busy:
            return "reflect: wait for the current turn to finish first"
        import threading
        t = threading.Thread(target=self._reflect_worker,
                             name="reflect", daemon=True)
        t.start()
        return "reflect: analyzing the session..."

    def _reflect_worker(self) -> None:
        """Make the reflection LLM call and append the result to the log."""
        import nbchat.core.db as db
        sid = self.session_id
        try:
            rows = db.load_history(sid)
        except Exception as exc:
            self._note("reflect: failed to load history: %s" % exc)
            return
        if not rows:
            self._note("reflect: no history to reflect on yet")
            return
        # Build a concise transcript of the recent conversation.
        parts = []
        for role, content, _tid, tool_name, _ta, _err in rows[-60:]:
            c = (content or "").strip().replace("\n", " ")
            if len(c) > 200:
                c = c[:197] + "..."
            if role == "user":
                parts.append("user: " + c)
            elif role == "assistant":
                parts.append("assistant: " + c)
            elif role == "tool":
                parts.append("tool(%s): %s" % (tool_name, c[:80]))
        transcript = "\n".join(parts)
        prompt = (
            "Here is the recent conversation for the current task:\n\n"
            + transcript + "\n\n"
            + "Reflect on the progress. State: (1) what has been "
            + "accomplished, (2) what remains, (3) the single next concrete "
            + "step. Be concise (under 200 words)."
        )
        try:
            reply = self._send_side_question(prompt)
        except Exception as exc:
            self._note("reflect: failed: %s" % exc)
            return
        if not reply:
            self._note("reflect: (no reply from the model)")
            return
        self._note("reflect: " + reply)

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



    # -- Candidate A: /verify (verifier-driven process score) --------------
    # Inspired by T1 (Terminal Agent RL: per-task verifiers as dense process
    # rewards), Proof-Carrying Cognition (the verification gap), and
    # LLM-as-a-Judge Is Not an Oracle (gate on deterministic verification,
    # not the LLM judge).
    def _verify_data(self) -> dict:
        """Read the most recent test/build/lint tool results from the DB.

        Returns a dict with keys: ``passed``, ``failed``, ``errors``,
        ``total``, ``clean``, ``source``, ``error``.  ``source`` is the
        tool that produced the result (``run_tests``, ``run_command``, or
        ``none``); ``clean`` is True when the latest verifier shows no
        failures.  Walks the history most-recent-first so the score always
        reflects the LATEST verifier result.
        """
        import json

        import nbchat.core.db as db

        sid = self.session_id
        out = {"passed": 0, "failed": 0, "errors": 0, "total": 0,
               "clean": False, "source": "none", "error": ""}
        try:
            rows = db.load_history(sid)
        except Exception as exc:
            out["error"] = str(exc)
            return out
        for row in reversed(rows):
            try:
                role, content, tool_id, tool_name, tool_args, error_flag = row
            except Exception:
                continue
            if role != "tool":
                continue
            if tool_name == "run_tests":
                try:
                    data = json.loads(content)
                except Exception:
                    data = None
                if isinstance(data, dict) and "passed" in data:
                    passed = int(data.get("passed", 0) or 0)
                    failed = int(data.get("failed", 0) or 0)
                    errors = int(data.get("errors", 0) or 0)
                    out.update({
                        "passed": passed, "failed": failed, "errors": errors,
                        "total": passed + failed + errors,
                        "clean": (failed == 0 and errors == 0 and passed > 0),
                        "source": "run_tests",
                    })
                    return out
            elif tool_name == "run_command":
                # A build/lint shell command: use the exit code / error flag.
                ok = not bool(error_flag)
                try:
                    data = json.loads(content)
                    if isinstance(data, dict) and "exit_code" in data:
                        ok = (int(data.get("exit_code") or 0) == 0)
                except Exception:
                    pass
                out.update({
                    "passed": 1 if ok else 0, "failed": 0 if ok else 1,
                    "errors": 0, "total": 1, "clean": bool(ok),
                    "source": "run_command",
                })
                return out
        return out

    def _cmd_verify(self, arg: str) -> str:
        """Show the verifier-driven process score for the current session.

        ``/verify`` reads the DB for the most recent ``run_tests`` tool
        result (or, failing that, the most recent ``run_command`` result)
        and reports a 0-100 verifier score + the pass/fail breakdown.  This
        is the T1 recipe: the score is driven by MEASURED outcomes (tests
        passing, exit codes), never by the model self-reporting progress.
        """
        d = self._verify_data()
        if d.get("error"):
            return "verify: failed to load history: %s" % d["error"]
        if d["source"] == "none":
            return ("verify: no test/build/lint results yet in this session "
                    "(run the run_tests tool to populate the score)")
        score = int(round(100.0 * d["passed"] / d["total"])) if d["total"] else 0
        lines = []
        lines.append("verifier score  %d%%   (source: %s)" % (score, d["source"]))
        if d["source"] == "run_tests":
            lines.append("  passed %d   failed %d   errors %d   total %d"
                         % (d["passed"], d["failed"], d["errors"], d["total"]))
        else:
            lines.append("  %s   (exit-code based)"
                         % ("OK" if d["clean"] else "FAILED"))
        if d["clean"]:
            lines.append("  clean - all checks passed")
        else:
            lines.append("  NOT clean - fix the failures before proceeding")
        return chr(10).join(lines)

    def _verify_pill(self) -> str:
        """A live status-line pill showing the latest verifier result."""
        now = time.monotonic()
        cache = getattr(self, "_verify_pill_cache", None)
        if cache is not None and now - cache[0] < 0.4:
            return cache[1]
        text = ""
        try:
            d = self._verify_data()
            if d["source"] == "run_tests" and d["total"] > 0:
                if d["failed"] or d["errors"]:
                    text = "tests %dF" % (d["failed"] + d["errors"])
                else:
                    text = "tests %d/%d" % (d["passed"], d["total"])
            elif d["source"] == "run_command":
                text = "check " + ("ok" if d["clean"] else "FAIL")
        except Exception:
            text = ""
        self._verify_pill_cache = (now, text)
        return text

    def _status_right(self) -> str:
        """The tui2 status line with the tui3 verifier + health pills."""
        base = super()._status_right()
        try:
            parts = [p for p in (self._verify_pill(), self._health_pill()) if p]
            if parts:
                extra = "  ".join(parts)
                base = (base + "  " + extra) if base else extra
        except Exception:
            pass
        return base

    # -- Candidate B: /health (objective long-run rot monitor) -------------
    # Inspired by How Fast Do Agents Rot (agents degrade sharply past
    # benchmark horizons) + The Unreliable Progress Bar (self-reported
    # progress is untrustworthy - use objective signals: message count,
    # verifier score, test trend, time since last verified progress).
    def _health_data(self) -> dict:
        """Compute objective long-run health signals for the current session.

        Returns a dict with keys: ``messages``, ``turns``,
        ``verifier_score``, ``verifier_clean``, ``test_trend``
        (up/down/flat/none), ``mins_since_progress`` (float or None),
        ``rot`` (bool), ``rot_reasons`` (list).  Every signal is OBJECTIVE
        (read from the DB / the verifier), never self-reported.
        """
        import json
        from datetime import datetime, timezone

        import nbchat.core.db as db

        sid = self.session_id
        out = {"messages": 0, "turns": 0, "verifier_score": None,
               "verifier_clean": False, "test_trend": "none",
               "mins_since_progress": None, "rot": False, "rot_reasons": []}
        try:
            rows = db.load_history(sid)
        except Exception as exc:
            out["error"] = str(exc)
            return out
        out["messages"] = len(rows)
        out["turns"] = sum(1 for r in rows if r[0] == "user")
        # Verifier score (from Candidate A).
        v = self._verify_data()
        if v.get("source") != "none" and v.get("total", 0) > 0:
            out["verifier_score"] = int(round(100.0 * v["passed"] / v["total"]))
            out["verifier_clean"] = v.get("clean", False)
        # Test trend: the last two run_tests pass-rates.
        rates = []
        for row in rows:
            try:
                role, content, tool_id, tool_name, tool_args, error_flag = row
            except Exception:
                continue
            if role == "tool" and tool_name == "run_tests":
                try:
                    data = json.loads(content)
                except Exception:
                    data = None
                if isinstance(data, dict) and "passed" in data:
                    passed = int(data.get("passed", 0) or 0)
                    failed = int(data.get("failed", 0) or 0)
                    errors = int(data.get("errors", 0) or 0)
                    total = passed + failed + errors
                    rates.append(passed / total if total else 0.0)
        if len(rates) >= 2:
            delta = rates[-1] - rates[-2]
            if delta > 0.01:
                out["test_trend"] = "up"
            elif delta < -0.01:
                out["test_trend"] = "down"
            else:
                out["test_trend"] = "flat"
        # Time since the last verified progress (a passing run_tests row).
        try:
            with db._connect() as conn:
                cur = conn.execute(
                    "SELECT ts FROM chat_log WHERE session_id=? AND role=? "
                    "AND tool_name=? ORDER BY id DESC LIMIT 1",
                    (sid, "tool", "run_tests"))
                r = cur.fetchone()
            if r and r[0]:
                dt = datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S")
                now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
                delta = (now_utc - dt).total_seconds() / 60.0
                if delta >= 0:
                    out["mins_since_progress"] = round(delta, 1)
        except Exception:
            pass
        # Compute the rot flag + reasons (objective signals only).
        if out["messages"] > 200:
            out["rot_reasons"].append("context bloat (%d messages)" % out["messages"])
        if out["verifier_score"] is not None and out["verifier_score"] < 50:
            out["rot_reasons"].append("low verifier score (%d%%)" % out["verifier_score"])
        if out["test_trend"] == "down":
            out["rot_reasons"].append("test trend regressing")
        if out["mins_since_progress"] is not None and out["mins_since_progress"] > 30:
            out["rot_reasons"].append(
                "%dm since last verified progress" % int(out["mins_since_progress"]))
        out["rot"] = bool(out["rot_reasons"])
        return out

    def _cmd_health(self, arg: str) -> str:
        """Show the objective long-run health (rot monitor) for this session.

        ``/health`` computes objective health signals - message count
        (context bloat), turn count, the current verifier score, the
        test-suite trend, and the time since the last verified progress -
        and reports whether the session is at RISK of "rotting".  Every
        signal is objective (never self-reported).
        """
        d = self._health_data()
        if d.get("error"):
            return "health: failed to load: %s" % d["error"]
        lines = []
        lines.append("health  %s" % ("ROT RISK" if d["rot"] else "ok"))
        lines.append("  messages %d   turns %d" % (d["messages"], d["turns"]))
        if d["verifier_score"] is not None:
            lines.append("  verifier score %d%%   (clean: %s)"
                         % (d["verifier_score"], d["verifier_clean"]))
        lines.append("  test trend: %s" % d["test_trend"])
        if d["mins_since_progress"] is not None:
            lines.append("  %dm since last verified progress"
                         % int(d["mins_since_progress"]))
        if d["rot"]:
            lines.append("  ROT reasons:")
            for reason in d["rot_reasons"]:
                lines.append("    - %s" % reason)
            lines.append("  (consider /compact, checkpointing, or a fresh session)")
        return chr(10).join(lines)

    def _health_pill(self) -> str:
        """A live status-line pill: the objective rot indicator (only when at
        risk - the status line is already busy, so a healthy session shows
        nothing)."""
        now = time.monotonic()
        cache = getattr(self, "_health_pill_cache", None)
        if cache is not None and now - cache[0] < 0.5:
            return cache[1]
        text = ""
        try:
            d = self._health_data()
            if d["rot"]:
                if d["mins_since_progress"] is not None and d["mins_since_progress"] > 30:
                    text = "ROT %dm" % int(d["mins_since_progress"])
                elif d["verifier_score"] is not None and d["verifier_score"] < 50:
                    text = "ROT %d%%" % d["verifier_score"]
                else:
                    text = "ROT"
        except Exception:
            text = ""
        self._health_pill_cache = (now, text)
        return text

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
