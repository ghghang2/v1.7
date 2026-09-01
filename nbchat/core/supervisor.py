"""Supervisor — an always-on LLM instance on the second parallel slot.

The supervisor has two capabilities:

1. **State queries** (synchronous): the user asks a question about the
   server, the assistant's progress, git status, task stats, etc.  The
   supervisor gathers a live state snapshot and answers with one
   non-streaming LLM call.

2. **Watchdog** (autonomous, periodic): a daemon thread periodically
   reviews the assistant's in-flight work.  If the assistant appears
   off-track or stuck, the supervisor produces a short corrective
   instruction that is placed on the agent's interjection queue.  The
   assistant's conversation loop drains the queue at safe points
   (top of each tool-turn) and injects it as a user message.

Design invariants
-----------------
* The supervisor NEVER writes into the assistant's ``messages`` list.
  It communicates exclusively through the ``InterjectionQueue``.
* All state-gathering is read-only and exception-guarded.
* A supervisor failure (network, timeout, bad model output) is logged
  and never propagates to the assistant's conversation loop.
* The watchdog only fires while the assistant is actively working
  (``agent._turn_active is True``) to avoid wasting tokens.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from nbchat.core import config
from nbchat.core import db

_log = logging.getLogger("nbchat.supervisor")

# ---------------------------------------------------------------------------
# Interjection queue
# ---------------------------------------------------------------------------

class InterjectionQueue:
    """Thread-safe queue for supervisor→assistant interjections.

    The supervisor pushes corrective instructions; the assistant's
    conversation loop drains them at safe points (top of each tool-turn,
    before the next LLM call).
    """

    def __init__(self, maxlen: int = 5) -> None:
        self._q: deque[str] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, text: str) -> None:
        """Enqueue an interjection (called by the supervisor thread)."""
        with self._lock:
            self._q.append(text)

    def drain(self) -> list[str]:
        """Remove and return all pending interjections (called by the
        assistant's conversation loop at a safe point)."""
        with self._lock:
            items = list(self._q)
            self._q.clear()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    def __bool__(self) -> bool:
        return len(self) > 0


# ---------------------------------------------------------------------------
# State gathering (read-only, exception-guarded)
# ---------------------------------------------------------------------------

def _git_status() -> dict:
    """Return a compact git status summary."""
    try:
        repo_root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            ["git", "status", "--porcelain=v1", "--branch"],
            capture_output=True, text=True, timeout=5, cwd=str(repo_root),
        )
        lines = out.stdout.strip().splitlines()
        branch = lines[0].replace("## ", "") if lines else "unknown"
        dirty = [l for l in lines[1:] if l.strip()]
        return {
            "branch": branch,
            "dirty_files": len(dirty),
            "ahead": _parse_ahead(lines[0]) if lines else 0,
            "behind": _parse_behind(lines[0]) if lines else 0,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _parse_ahead(branch_line: str) -> int:
    for part in branch_line.split():
        if part.startswith("ahead"):
            return int(part.split()[1].split(",")[0]) if "," in part else int(part.split()[1])
    return 0


def _parse_behind(branch_line: str) -> int:
    for part in branch_line.split():
        if part.startswith("behind"):
            return int(part.split()[1])
    return 0


def _server_info() -> dict:
    """Return server configuration and health."""
    info = {
        "model": config.MODEL_NAME,
        "server_url": config.SERVER_URL,
        "port": config.PORT,
        "n_parallel": config.N_PARALLEL,
        "ctx_size": config.CTX_SIZE,
        "n_gpu_layers": config.N_GPU_LAYERS,
        "context_budget": config.CONTEXT_BUDGET,
    }
    # Check if server is alive
    try:
        import urllib.request
        with urllib.request.urlopen(f"{config.SERVER_URL}/health", timeout=3) as r:
            info["healthy"] = r.status == 200
    except Exception:
        info["healthy"] = False
    return info


def _task_stats() -> dict:
    """Return task completion statistics from the chat_log table."""
    try:
        with sqlite3.connect(db.DB_PATH) as conn:
            # Count user turns (proxy for "tasks")
            rows = conn.execute(
                "SELECT session_id, COUNT(*) as n, "
                "MIN(ts) as first_ts, MAX(ts) as last_ts "
                "FROM chat_log WHERE role='user' "
                "GROUP BY session_id ORDER BY last_ts DESC LIMIT 20"
            ).fetchall()
            # Count errors
            err_rows = conn.execute(
                "SELECT COUNT(*) FROM chat_log WHERE error_flag=1"
            ).fetchone()
            # Count tool calls
            tool_rows = conn.execute(
                "SELECT tool_name, COUNT(*) as n FROM chat_log "
                "WHERE role='tool' GROUP BY tool_name"
            ).fetchall()

        tasks = []
        for sid, n, first_ts, last_ts in rows:
            duration = None
            try:
                t0 = datetime.fromisoformat(first_ts)
                t1 = datetime.fromisoformat(last_ts)
                duration = round((t1 - t0).total_seconds(), 1)
            except Exception:
                pass
            tasks.append({
                "session": sid,
                "user_turns": n,
                "first_ts": first_ts,
                "last_ts": last_ts,
                "duration_s": duration,
            })

        return {
            "total_tasks": len(tasks),
            "recent_tasks": tasks[:10],
            "total_errors": err_rows[0] if err_rows else 0,
            "tool_calls": {r[0]: r[1] for r in tool_rows},
        }
    except Exception as exc:
        return {"error": str(exc)}


def _assistant_state(agent) -> dict:
    """Return a snapshot of the assistant's current state."""
    state = {
        "session_id": agent.session_id,
        "history_len": len(agent.history),
        "task_log_len": len(agent.task_log),
        "turn_active": getattr(agent, "_turn_active", False),
        "tool_running": getattr(agent, "_tool_running", False),
    }
    # L1 goal (current objective)
    try:
        goal = db.get_core_memory(agent.session_id).get("goal", "")
        if goal:
            state["current_goal"] = goal[:300]
    except Exception:
        pass
    # Recent action log (last 10 entries)
    if agent.task_log:
        state["recent_actions"] = agent.task_log[-10:]
    return state


def gather_state(agent) -> dict:
    """Gather a full state snapshot for the supervisor.

    All sub-gatherers are exception-guarded; a failure in one
    does not prevent the others from reporting.
    """
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "server": _server_info(),
        "git": _git_status(),
        "tasks": _task_stats(),
        "assistant": _assistant_state(agent),
    }


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------

_SUPERVISOR_SYSTEM_PROMPT = """\
You are the Supervisor for an AI assistant system. You run on a dedicated
parallel slot of the llama-server and have read-only access to the system's
state. Your job is to:

1. Answer the user's questions about the system state (server config, model,
   git status, task progress, errors, etc.) concisely and accurately.
2. When reviewing the assistant's work, identify if it is off-track, stuck,
   or missing a key requirement. If so, produce ONE short corrective
   instruction (max 2 sentences) that the assistant will receive as a user
   message. If the assistant is on track, respond with exactly: ON_TRACK

Always base your answers on the provided state data. Be concise.
"""

_REVIEW_PROMPT = """\
Review the assistant's current work. The user's current request is:
{goal}

Recent actions taken by the assistant:
{actions}

Recent conversation exchange (last few messages):
{exchange}

Is the assistant making good progress toward the current request? If the
recent exchange clearly shows the assistant is working on a DIFFERENT task
than the stated request (e.g. the request was from a prior, already-completed
turn), trust the exchange — the assistant is on track. Only interject if the
assistant is genuinely stuck, off-track, or missing a key requirement of the
CURRENT task. If on track, respond with exactly: ON_TRACK
"""


_VOICE_STATUS_PROMPT = """\
You are Alfred, the user's butler, speaking aloud over a voice channel.
Give the user ONE short spoken status update on the assistant's current task.
Base it ONLY on the state data below. 1-2 sentences, under 15 seconds.
Address the user as "sir". State progress honestly — never claim a step is
done unless the data shows it. If there is nothing meaningful to report,
respond with exactly: SILENT
"""


class Supervisor:
    """Always-on supervisor instance on the second parallel slot.

    Parameters
    ----------
    agent:
        The TerminalAgent (or any agent-like object) whose state the
        supervisor monitors and whose interjection queue it feeds.
    """

    def __init__(self, agent, *,
                 interval: int | None = None,
                 cooldown: int | None = None,
                 max_output_tokens: int | None = None,
                 voice_bus=None,
                 voice_status_interval: int | None = None) -> None:
        self._agent = agent
        self._interval = interval or config.SUPERVISOR_INTERVAL
        self._cooldown = cooldown or config.SUPERVISOR_COOLDOWN
        self._max_tokens = max_output_tokens or config.SUPERVISOR_MAX_OUTPUT_TOKENS
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_interjection: float = 0.0
        self._interjection_count: int = 0
        self._lock = threading.Lock()
        # Voice channel: periodic spoken status updates (verified — grounded
        # in a live gather_state() snapshot).  Disabled unless a voice_bus is
        # supplied (i.e. the TUI was started with --voice).
        self._voice_bus = voice_bus
        self._voice_status_interval = (
            voice_status_interval or config.VOICE_STATUS_MIN_INTERVAL
        )
        self._last_voice_status: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the watchdog daemon thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watchdog_loop,
            name="nbchat-supervisor",
            daemon=True,
        )
        self._thread.start()
        _log.info("supervisor watchdog started (interval=%ss, cooldown=%ss)",
                  self._interval, self._cooldown)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the watchdog thread."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None
        _log.info("supervisor watchdog stopped (total interjections: %d)",
                  self._interjection_count)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def interjection_count(self) -> int:
        return self._interjection_count

    # ── State query (synchronous) ──────────────────────────────────────────

    def ask(self, question: str) -> str:
        """Answer a user question about system state.

        Gathers the current state snapshot, sends it to the LLM on the
        supervisor's parallel slot, and returns the answer text.
        """
        state = gather_state(self._agent)
        state_text = json.dumps(state, indent=2, default=str)

        messages = [
            {"role": "system", "content": _SUPERVISOR_SYSTEM_PROMPT},
            {"role": "user", "content":
                f"System state:\n{state_text}\n\nQuestion: {question}"},
        ]

        try:
            from nbchat.core.client import get_client
            client = get_client()
            resp = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=messages,
                max_tokens=self._max_tokens,
                temperature=0.3,
            )
            answer = resp.choices[0].message.content or ""
            _log.info("supervisor answered question (%d chars)", len(answer))
            return answer.strip()
        except Exception as exc:
            _log.warning("supervisor ask failed: %s: %s", type(exc).__name__, exc)
            return f"[supervisor error] {type(exc).__name__}: {exc}"

    # ── Watchdog loop ──────────────────────────────────────────────────────

    def _watchdog_loop(self) -> None:
        """Periodically review the assistant's work."""
        while not self._stop_event.is_set():
            try:
                self._review_assistant()
            except Exception as exc:
                _log.warning("supervisor review failed: %s: %s",
                             type(exc).__name__, exc)
            try:
                self._voice_status()
            except Exception as exc:
                _log.warning("supervisor voice status failed: %s: %s",
                             type(exc).__name__, exc)
            self._stop_event.wait(self._interval)

    def _voice_status(self) -> None:
        """Emit a spoken status update to the voice channel.

        Only fires when:
        1. A voice bus is attached (TUI started with --voice).
        2. The assistant is actively working (``_turn_active``).
        3. The voice status interval has elapsed.

        The update is grounded in a live ``gather_state()`` snapshot — the
        LLM may only report what the data shows.  A "SILENT" response is
        dropped (no speech).
        """
        if self._voice_bus is None:
            return
        if not getattr(self._agent, "_turn_active", False):
            return
        if time.time() - self._last_voice_status < self._voice_status_interval:
            return

        state = gather_state(self._agent)
        messages = [
            {"role": "system", "content": _SUPERVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": _VOICE_STATUS_PROMPT + "\n\nState data:\n"
             + json.dumps(state, default=str)[:4000]},
        ]
        try:
            from nbchat.core.client import get_client
            client = get_client()
            resp = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=messages,
                max_tokens=64,
                temperature=0.3,
            )
            response = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            _log.warning("voice status LLM call failed: %s: %s",
                         type(exc).__name__, exc)
            return

        if response.upper().startswith("SILENT"):
            return
        if not response:
            return
        _log.info("voice status: %s", response[:120])
        self._voice_bus.publish("status", response)
        self._last_voice_status = time.time()

    def _review_assistant(self) -> None:
        """One review cycle.

        Only fires when:
        1. The assistant is actively working (``_turn_active``).
        2. The cooldown period has elapsed since the last interjection.
        """
        agent = self._agent
        # Only review if the assistant is actively working.
        if not getattr(agent, "_turn_active", False):
            return
        # Respect cooldown.
        elapsed = time.time() - self._last_interjection
        if elapsed < self._cooldown:
            return

        goal, actions, exchange = self._gather_review_context()
        if not goal and not actions:
            return  # nothing to review

        prompt = _REVIEW_PROMPT.format(
            goal=goal or "(no explicit goal recorded)",
            actions="\n".join(actions) if actions else "(no recent actions)",
            exchange=exchange or "(no recent exchange)",
        )

        messages = [
            {"role": "system", "content": _SUPERVISOR_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            from nbchat.core.client import get_client
            client = get_client()
            resp = client.chat.completions.create(
                model=config.MODEL_NAME,
                messages=messages,
                max_tokens=128,
                temperature=0.2,
            )
            response = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            _log.warning("supervisor LLM call failed: %s: %s",
                         type(exc).__name__, exc)
            return

        if response.upper().startswith("ON_TRACK"):
            _log.debug("supervisor: assistant is on track")
            return

        # The supervisor produced a corrective instruction.
        _log.info("supervisor interjecting: %s", response[:120])
        self._agent.interject(response)
        with self._lock:
            self._last_interjection = time.time()
            self._interjection_count += 1

    def _gather_review_context(self) -> tuple[str, list[str], str]:
        """Gather goal, recent actions, and recent exchange for review."""
        agent = self._agent
        # Goal from core memory
        goal = ""
        try:
            goal = db.get_core_memory(agent.session_id).get("goal", "")
        except Exception:
            pass
        # Recent action log
        actions = list(agent.task_log[-15:]) if agent.task_log else []
        # Recent exchange: last few messages from history
        exchange_parts = []
        try:
            for row in agent.history[-10:]:
                role, content = row[0], row[1]
                if role in ("user", "assistant") and content:
                    label = "User" if role == "user" else "Assistant"
                    exchange_parts.append(f"{label}: {content[:200]}")
        except Exception:
            pass
        return goal, actions, "\n".join(exchange_parts)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def create_supervisor(agent, **kw) -> Supervisor:
    """Factory: create a Supervisor bound to *agent*."""
    return Supervisor(agent, **kw)


__all__ = [
    "Supervisor",
    "InterjectionQueue",
    "gather_state",
    "create_supervisor",
]
