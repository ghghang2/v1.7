"""Terminal REPL for nbchat.

Run with a single command:

    python -m nbchat.tui          # or: python nbchat_tui.py

Options:
    --new         Force a brand-new session (skip resuming the last one).
    --session S   Resume a specific session id (see /sessions).
    --no-color    Disable ANSI colours.
    --check       Only check the llama-server is reachable, then exit.

The REPL reuses the full agent stack (memory, context windowing, tools,
streaming).  It talks to the local llama-server configured in
``repo_config.yaml`` (``SERVER_URL``).  If the server is not running it will
still start, but LLM calls will fail until you run ``python run.py``.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path
import sys
import threading
import urllib.request

from nbchat.core import config
from nbchat.core import db
from nbchat.tui.agent import TerminalAgent

_BANNER = """
  ┌──────────────────────────────────────────────┐
  │  n b c h a t  ·  terminal  chat               │
  └──────────────────────────────────────────────┘
"""

_HELP = """Commands
  /help              Show this help.
  /new               Start a new session.
  /sessions          List terminal sessions (name + id).
  /load <id|name>    Load one of the sessions from /sessions (id or name).
  /title [name]      Show or set a short name for the current session.
  /history           Print the current session's message history.
  /model             Show the active model, server, reasoning effort and
                     average tokens/sec for the last 50 turns.
  /effort [E]        Show session reasoning effort; with an argument set it
                     (none, low, medium, xhigh); "default" resets to the
                     configured default (medium).
  /stats [N] [sess]  Task-completion statistics (default: all tasks,
                     last N, or a specific session id): completion rate,
                     durations, tool/redundancy counts.
  /clear             Clear the screen.
  /quit              Exit (Ctrl+C / Ctrl+D also work).
  /sup <question>    Ask the supervisor about system state (requires --supervisor).
  /team <goal>     Run the goal as a team of parallel agents (plan,
                     dispatch, verify, report). Runs in the background;
                     labelled [Wn] worker output streams in here.
  /team            Show the status of the last or current team run.
  /team stop       Request the current team run to stop.

Tips
  • Type a normal message and press Enter to chat; the reply streams in live.
  • End a line with a backslash ( \\ ) to continue on the next line.
  • Type a new message while a reply is streaming to interrupt it and
    redirect the agent immediately (no need to wait for the stream to finish).
  • Press Ctrl+C while a reply is streaming to interrupt it too.
  • Start with --email to also receive Gmail replies in this chat.
"""

# ── Team (multi-agent) state ────────────────────────
# /team <goal> runs a TeamCoordinator on a background thread so the
# input prompt stays live; /team and /team stop read from here.
_team_state: dict = {"thread": None, "last": None, "coordinator": None}


# ── Server health ──────────────────────────────────────────────────────────

def server_ok() -> bool:
    try:
        with urllib.request.urlopen(f"{config.SERVER_URL}/health", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


# ── Banner / startup ───────────────────────────────────────────────────────

def print_banner(agent: TerminalAgent, server_up: bool) -> None:
    p = agent.palette
    print(p.cyan(_BANNER.strip()))
    print(f"  {p.gray('model   ')} {agent.model_name}")
    print(f"  {p.gray('server  ')} {config.SERVER_URL} "
          f"{p.green('[up]') if server_up else p.red('[down]')}")
    if agent.session_title:
        print(f"  {p.gray('session ')} {agent.session_title} "
              f"{p.gray(agent.session_id)}")
    else:
        print(f"  {p.gray('session ')} {agent.session_id}")
    print(f"  {p.gray('help    ')} type /help for commands")
    if not server_up:
        print(p.yellow("  ! llama-server is not reachable — LLM calls will fail "
                       "until you run: python run.py"))
    print()


# ── Slash commands ─────────────────────────────────────────────────────────

def handle_command(agent: TerminalAgent, line: str, supervisor=None) -> bool:
    """Handle a slash command.  Returns True if the caller should exit."""
    parts = line.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("/quit", "/exit"):
        return True
    if cmd == "/help":
        print(_HELP)
    elif cmd == "/new":
        sid = agent.new_session()
        agent.remember_session(sid)
        print(f"Started new session: {sid}")
    elif cmd == "/sessions":
        sessions = agent.list_sessions()
        if not sessions:
            print("No terminal sessions yet.")
        else:
            for sid, title in sessions:
                marker = "  (current)" if sid == agent.session_id else ""
                label = title or sid
                print(f"  {label}  {agent.palette.gray(sid)}{marker}")
    elif cmd == "/title":
        if not arg:
            current = agent.session_title or "(unnamed)"
            print(f"current: {current}  usage: /title <name>")
        else:
            agent.set_title(arg)
            print(f"Session named: {agent.session_title}")
    elif cmd == "/load":
        if not arg:
            print("usage: /load <session-id or name>")
        else:
            canonical = agent.resolve_session(arg)
            if canonical is None:
                # Substring hits but not unique?  Point the user at the list.
                hits = [sid for sid, title in agent.list_sessions()
                        if arg.lower() in (title or "").lower()]
                if len(hits) > 1:
                    print(f"{arg} matches {len(hits)} sessions "
                          f"(see /sessions)")
                else:
                    print(f"No such session: {arg}  (see /sessions)")
            else:
                agent._switch_session(canonical)
                agent.remember_session(agent.session_id)
                if not agent.history:
                    print(f"Loaded session {agent.session_id} (no history rows).")
                else:
                    print(f"Loaded session {agent.session_id} "
                          f"({len(agent.history)} rows).")
    elif cmd == "/history":
        rows = agent.history
        if not rows:
            print("History is empty.")
        else:
            for role, content, _tid, tname, _targs, _ef in rows:
                if role == "analysis":
                    continue
                label = {"user": "You", "assistant": "Agent",
                         "tool": f"tool:{tname}"}.get(role, role)
                text = (content or "").strip()
                if len(text) > 200:
                    text = text[:197] + "..."
                print(f"  {label}: {text}")
    elif cmd == "/effort":
        effort = arg.lower()
        if not effort:
            current = agent.reasoning_effort or config.DEFAULT_REASONING_EFFORT
            print(f"effort  {current}")
            return False
        if effort == "default":
            agent.reasoning_effort = ""
            print(f"reasoning effort reset to default "
                  f"({config.DEFAULT_REASONING_EFFORT}).")
        elif effort in ("none", "low", "medium", "xhigh"):
            agent.reasoning_effort = effort
            print(f"reasoning effort set to {agent.palette.bold(effort)} for this session.")
        else:
            current = agent.reasoning_effort or config.DEFAULT_REASONING_EFFORT
            print(f"current: {current}  " + agent.palette.gray("usage: /effort none|low|medium|xhigh|default"))
    elif cmd == "/stats":
        # Usage: /stats [N] — all tasks, or the last N.  An explicit
        # session id (e.g. /stats tui:s1) restricts to that session.
        from nbchat.core.task_tracker import summarize_tasks
        parts = arg.split() if arg else []
        n = None
        session = None
        for tok in parts:
            if tok.isdigit():
                n = int(tok)
            elif ":" in tok:  # a session id (e.g. tui:s1, whatsapp:...)
                session = tok
        if n is None:
            n = 500
        rows = db.task_summary_rows(session, limit=n)
        if not rows:
            print("No task records yet (tasks are logged as they complete).")
        else:
            summary = summarize_tasks(rows)
            p = agent.palette
            dur = summary["duration_s"]
            scope = f"  (session {session})" if session else "  (all sessions)"
            print(f"{p.bold('tasks   ')} {summary['tasks']}{scope}   status: {summary['by_status']}")
            print(f"{p.bold('completion')} {summary['by_completion']}"
                  f"   failure rate {summary['failure_rate']:.1%}")
            print(f"{p.bold('duration')}  mean {dur['mean']}s  median {dur['median']}s  "
                  f"max {dur['max']}s")
            t = summary["totals"]
            print(f"{p.bold('totals  ')} llm {t['llm_calls']}  tool turns {t['tool_turns']}  "
                  f"tools {t['tool_calls']}  failed {t['tool_failed']}")
            print(f"{p.bold('waste   ')} redundant {t['redundant']} "
                  f"({t['redundant_reads']} reads / {t['redundant_writes']} writes)  "
                  f"stalls {t['stalls']}  truncations {t['truncations']}  "
                  f"stream retries {t['stream_retries']}  "
                  f"interventions {t['interventions']}")
    elif cmd in ("/model", "/about"):
        print(f"model   {agent.model_name}")
        print(f"server  {config.SERVER_URL}")
        print(f"session {agent.session_id}")
        print("effort  " + (agent.reasoning_effort or config.DEFAULT_REASONING_EFFORT))
        stats = last_turn_stats()
        if stats is None:
            print("speed   - (no inference data yet)")
        else:
            print(stats)
    elif cmd == "/clear":
        sys.stdout.write("\033[2J\033[H")
    elif cmd == "/team":
        if arg and arg != "stop":
            if (_team_state["thread"] is not None
                    and _team_state["thread"].is_alive()):
                print(agent.palette.yellow(
                    "  [team] a team run is already in progress; "
                    "wait for it or type /team stop."))
            else:
                from nbchat.core.team import (
                    TeamAgent, TeamCoordinator, ToolArbiter,
                )
                team_agent = TeamAgent(color=True)
                coordinator = TeamCoordinator(team_agent)
                arbiter = ToolArbiter()
                arbiter.install()
                _team_state["coordinator"] = coordinator
                p = agent.palette
                print(p.magenta(f"  [team] starting run for: {arg[:100]}"))

                def _team_run():
                    try:
                        result = coordinator.run(arg)
                        _team_state["last"] = result
                    except Exception as exc:
                        _team_state["last"] = {
                            "status": "failed",
                            "summary": f"team run crashed: {exc}",
                            "tasks": [],
                        }
                    finally:
                        arbiter.remove()

                thread = threading.Thread(
                    target=_team_run, daemon=True, name="nbchat-team-run")
                _team_state["thread"] = thread
                thread.start()
        elif arg == "stop":
            thread = _team_state["thread"]
            if thread is not None and thread.is_alive():
                print(agent.palette.yellow(
                    "  [team] stop requested; the run will wind down."))
            else:
                print("  [team] no team run in progress.")
        else:
            last = _team_state.get("last")
            thread = _team_state["thread"]
            if thread is not None and thread.is_alive():
                print(agent.palette.magenta(
                    "  [team] run in progress \u2014 live output is streaming "
                    "above; type /team when it finishes."))
            elif last is None:
                print("  [team] no team run yet.  Usage: /team <goal>")
            else:
                p = agent.palette
                print(p.magenta(
                    f"  [team] last run status: {last.get('status', 'unknown')}"))
                for t in last.get("tasks", []):
                    print(f"    [{t.get('status', '?')}] "
                          f"{t.get('title') or t.get('task_id', '?')}")
                summary = last.get("summary", "")
                for line_ in summary.splitlines()[:10]:
                    print(f"    {line_}")
    elif cmd == "/sup":
        if supervisor is None:
            print(agent.palette.yellow(
                "  ! Supervisor is not running (start with --supervisor)."))
        elif not arg:
            print("usage: /sup <question>")
        else:
            p = agent.palette
            print(p.magenta(f"  [supervisor] asking: {arg}"))
            # Run the LLM call on a background thread so the main loop
            # immediately returns to reading input.  The supervisor uses
            # its own parallel slot (n_parallel=2) and never touches the
            # assistant's send lock or history.
            def _ask_and_print():
                try:
                    answer = supervisor.ask(arg)
                except Exception as exc:
                    answer = f"[supervisor error] {exc}"
                for line_ in answer.splitlines() or [""]:
                    print(p.magenta(f"  [supervisor] ") + line_)
                print()
            threading.Thread(target=_ask_and_print, daemon=True,
                             name="nbchat-sup-ask").start()
    else:
        print(f"Unknown command: {cmd}  (try /help)")
    return False


# ── Input reading (with backslash continuation) ────────────────────────────

# ── Inference stats (tokens per second, last N turns) ──────────────────────

# Matches one LLM call's metrics line, e.g.
# "2026-09-02 18:46:25,369 [INFO] Inference_Metrics: Latency: 1.22s | P:2447 C:95 T:2542"
_RE_METRIC = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ \[INFO\] Inference_Metrics:"
    r" Latency: ([0-9.]+)s \| P:\d+ C:(\d+) T:\d+$"
)

# Two metric entries this close (seconds) are treated as the same
# conversation turn (a turn may span several LLM calls via tool-calling).
_TURN_GAP_SECONDS = 60.0


def _metric_log_path() -> Path | None:
    """Locate inference_metrics.log (CWD, then this file's repo root)."""
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
        cand = base / "inference_metrics.log"
        if cand.exists():
            return cand
    return None


def last_turn_stats(n_turns: int = 50) -> str | None:
    """Average tokens/second over the last *n_turns* turns, or None.

    Each LLM call is logged by ``nbchat.core.client`` as one
    ``Latency: Xs | P:p C:c T:t`` line; a conversation turn may contain
    several such calls (tool-calling loop), so entries are grouped into
    turns when their timestamps are within ``_TURN_GAP_SECONDS``.  A turn's
    speed is its total completion tokens divided by the elapsed time
    (first to last call); single-call turns use that call's latency.
    """
    path = _metric_log_path()
    if path is None:
        return None
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except OSError:
        return None

    entries = []  # (timestamp, latency_s, completion_tokens)
    for line in lines:
        m = _RE_METRIC.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
        latency, completion = float(m.group(2)), int(m.group(3))
        if latency > 0 and completion > 0:
            entries.append((ts, latency, completion))

    if not entries:
        return None

    # Group consecutive calls into turns: a new call starts a new turn only
    # when it lands _TURN_GAP_SECONDS or more after the previous call.
    turns = []  # list of [start_ts, end_ts, latency_sum, completion_sum]
    last_ts = None
    for ts, latency, completion in entries:
        if last_ts is not None and ts - last_ts < _TURN_GAP_SECONDS:
            t = turns[-1]
            t[1] = max(t[1], ts)
            t[2] += latency
            t[3] += completion
        else:
            turns.append([ts, ts, latency, completion])
        last_ts = ts

    samples = []
    for start, end, latency_sum, completion_sum in turns[-n_turns:]:
        span = max(end - start, 1e-3)
        tps = completion_sum / max(latency_sum, span)
        if tps > 0:
            samples.append(tps)
    if not samples:
        return None
    avg = sum(samples) / len(samples)
    label = (f"last {len(samples)} turn(s)" if len(samples) < n_turns
             else "last 50 turns")
    return f"speed   avg {avg:.1f} tok/s ({label})"


def read_line(prompt: str) -> str:
    line = input(prompt)
    if line.rstrip().endswith("\\"):
        buf = line.rstrip()[:-1]
        while True:
            cont = input("  …")
            if cont.rstrip().endswith("\\"):
                buf += "\n" + cont.rstrip()[:-1]
            else:
                buf += "\n" + cont
                break
        return buf
    return line


def wait_for_turn(agent: TerminalAgent, thread: threading.Thread) -> None:
    """Block until the in-flight turn thread finishes.

    Runs on the main thread *only* when there is no active input prompt (the
    user has not typed anything to redirect), so it never competes with the
    input thread for the terminal.  If the user presses Ctrl+C while we wait,
    we ask the turn to stop and wait for it to wind down so the agent is left
    in a clean state (history consistent, no orphaned LLM call).
    """
    while thread.is_alive():
        try:
            thread.join(timeout=0.25)
        except KeyboardInterrupt:
            agent.interrupt()
            thread.join(timeout=5.0)
            if thread.is_alive():
                # The turn could not be stopped in time; abandon the wait and
                # let the daemon thread finish on its own.
                break

# ── Main loop ──────────────────────────────────────────────────────────────

def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nbchat-tui",
        description="Minimal terminal chat UI for nbchat.",
    )
    parser.add_argument("--new", action="store_true",
                        help="force a new session")
    parser.add_argument("--session", metavar="ID",
                        help="resume a specific session id")
    parser.add_argument("--no-color", action="store_true",
                        help="disable ANSI colours")
    parser.add_argument("--check", action="store_true",
                        help="check the llama-server is reachable, then exit")
    parser.add_argument("--email", action="store_true",
                        help="poll the Gmail inbox and inject replies into "
                             "the chat (extends the input box to email)")
    parser.add_argument("--no-auto-reply", action="store_true",
                        help="with --email: do NOT email the agent's reply "
                             "back to the sender")
    parser.add_argument("--supervisor", action="store_true",
                        help="start the always-on supervisor watchdog "
                             "(uses the second parallel slot)")
    parser.add_argument("--voice", action="store_true",
                        help="start the Alfred voice bridge (localhost "
                             f"port {config.VOICE_PORT}); reach it from "
                             "your laptop via: ssh -L "
                             f"{config.VOICE_PORT}:127.0.0.1:{config.VOICE_PORT} "
                             "user@server")
    args = parser.parse_args(argv)

    up = server_ok()
    if args.check:
        print("llama-server reachable." if up
              else f"llama-server NOT reachable at {config.SERVER_URL}")
        return 0 if up else 1

    agent = TerminalAgent(color=not args.no_color)

    if args.session:
        sid = agent.resolve_session(args.session)
        if sid is None:
            print(f"No such session: {args.session}  (see /sessions)",
                  file=sys.stderr)
            return 1
        agent._switch_session(sid)
    elif not args.new:
        last = TerminalAgent.last_session()
        if last:
            agent._switch_session(last)
    agent.remember_session(agent.session_id)

    print_banner(agent, up)

    # Voice bridge: localhost FastAPI that the laptop's Alfred client
    # reaches over an SSH tunnel.  Created FIRST so both the agent and the
    # supervisor can attach to the same event bus.
    voice_bus = None
    voice_bridge = None
    if args.voice or config.VOICE_ENABLED:
        from nbchat.voice.events import ALFRED_VOICE_PROMPT, VoiceEventBus
        from nbchat.voice.server import VoiceBridge
        voice_bus = VoiceEventBus()
        agent._voice_bus = voice_bus
        agent.system_prompt += ALFRED_VOICE_PROMPT
        voice_bridge = VoiceBridge(voice_bus, port=config.VOICE_PORT)
        ok = voice_bridge.start()
        p = agent.palette
        if ok:
            print(p.magenta("  voice   ")
                  + f"Alfred bridge ACTIVE on 127.0.0.1:{config.VOICE_PORT} "
                    + p.gray(f"(ssh -L {config.VOICE_PORT}:127.0.0.1:"
                             f"{config.VOICE_PORT} user@server)"))
        else:
            print(p.red("  voice   bridge FAILED to start on port "
                        f"{config.VOICE_PORT} \u2014 voice disabled"))
            agent._voice_bus = None
            voice_bridge = None
            voice_bus = None
        if voice_bridge is not None:
            def _voice_inbound_loop():
                """Daemon: auto-submit voice transcripts as user turns.

                Blocks on the bridge's inbound queue; when a transcript
                arrives it fires the verified 'received' ack and hands the
                text to the agent via ``send_async`` (which serialises on
                the send lock, so it never races a terminal turn).
                """
                while True:
                    transcript = voice_bridge.get_inbound(timeout=1.0)
                    if transcript is None:
                        continue
                    agent._voice_fire("received")
                    p = agent.palette
                    print(p.dim(f"  \u266a [voice] {transcript}"))
                    agent.send_async(transcript)
                    agent.remember_session(agent.session_id)
            threading.Thread(target=_voice_inbound_loop, daemon=True,
                             name="nbchat-voice-in").start()

    # Supervisor: always-on watchdog on the second parallel slot.
    supervisor = None
    if args.supervisor or config.SUPERVISOR_ENABLED:
        from nbchat.core.supervisor import create_supervisor
        supervisor = create_supervisor(agent, voice_bus=voice_bus)
        supervisor.start()
        p = agent.palette
        print(p.magenta("  supervisor ")
              + f"ACTIVE (review every {supervisor._interval}s, "
                f"cooldown {supervisor._cooldown}s)")
        if config.N_PARALLEL < 2:
            print(p.yellow(
                "  ! supervisor is sharing the assistant's single slot "
                "(n_parallel=1): its calls will queue behind in-flight "
                f"turns and time out after {int(config.SUPERVISOR_LLM_TIMEOUT)}s. "
                "Set n_parallel: 2 in repo_config.yaml for a dedicated slot."))

    # Email bridge: pipe the Gmail inbox into the chat stream.
    bridge = None
    if args.email:
        import os
        if not os.getenv("GHG_APP_PASSWORD"):
            print(agent.palette.yellow(
                "  ! --email requested but GHG_APP_PASSWORD is not set; "
                "email bridge disabled."))
        else:
            from nbchat.tui.email_bridge import EmailBridge
            bridge = EmailBridge(
                agent,
                auto_reply=not args.no_auto_reply,
                supervisor=supervisor,
            )
            bridge.start()
            p = agent.palette
            print(p.magenta("  email   ") + "inbox bridge ACTIVE "
                  f"(poll every {bridge._poll_interval}s, "
                  f"auto-reply: {bridge._auto_reply})")

    prompt = agent.palette.cyan("\u276f ")
    # The turn thread runs the agentic loop in the background.  The main
    # thread *always* keeps reading input so the user can interject and
    # redirect the stream at any time.  We only join the turn thread on exit
    # (clean shutdown) — never while a prompt is live, which would block the
    # user from typing.
    turn_thread = None
    while True:
        try:
            line = read_line(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            # Ctrl+C / EOF at the prompt: stop any in-flight turn, exit.
            if turn_thread is not None and turn_thread.is_alive():
                agent.interrupt()
                wait_for_turn(agent, turn_thread)
            print("\nBye.")
            break

        if not line:
            continue

        # Slash commands are ALWAYS handled as commands, even mid-stream.
        # This is how the user talks to the supervisor (/sup) or manages
        # sessions (/new, /sessions, etc.) without interrupting the
        # assistant's in-flight work.
        if line.startswith("/"):
            if handle_command(agent, line, supervisor=supervisor):
                print("Bye.")
                break
            continue

        # Mid-stream interjection: a turn is still running and the user typed
        # plain text (not a command).  Stop the current turn and start a fresh
        # one with the new message.  ``send_async`` serialises on the agent's
        # send lock, so the new turn waits for the (now interrupted) turn to
        # wind down, then runs the user's redirect — no message is lost.
        if turn_thread is not None and turn_thread.is_alive():
            agent.interrupt()
            print("\n" + agent.palette.yellow(
                "[redirecting — stopping current response]"))
            turn_thread = agent.send_async(line)
            agent.remember_session(agent.session_id)
            # Do NOT block: keep reading so the user can interject again.
            continue

        turn_thread = agent.send_async(line)
        agent.remember_session(agent.session_id)
        # Do NOT block here: the loop returns to read_line immediately so the
        # user can interject mid-stream.  The turn thread streams in the
        # background.
    if bridge is not None:
        bridge.stop()
    if voice_bridge is not None:
        voice_bridge.stop()
    if supervisor is not None:
        supervisor.stop()
    return 0
