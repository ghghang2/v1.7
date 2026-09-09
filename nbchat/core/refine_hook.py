"""Conversation-hook wiring for the refinement engine (phase 2).

After each user turn, :func:`on_task_finished` decides (via the
pure :func:`nbchat.core.refinement.should_refine_task` trigger) whether
the task is worth a refinement review and, if so, schedules a single
background thread that runs one full refine round against the local
server.

Design invariants (see docs/supervisor_v2_design.md section 2.3):
  * The conversation path is never blocked — the hook only appends a
    thread and returns immediately.
  * The LLM call runs with a bounded timeout; any failure is a no-op
    round (the engine itself converts exceptions to skipped results).
  * One in-flight round per agent at a time; a later trigger while a
    round runs is dropped (not queued).
  * Every run is audited via ``_on_agent_message`` (a one-line report)
    and a ``refine:hook`` event row, so the channel never goes silent.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import nbchat.core.config as config
import nbchat.core.db as db
import nbchat.core.refinement as refinement
import nbchat.core.task_tracker as task_tracker

_log = logging.getLogger("nbchat.refine_hook")

REFINE_HOOK_ENABLED: bool = bool(config._cfg.get("refine_hook_enabled", True))
REFINE_LLM_TIMEOUT: float = float(config._cfg.get("refine_llm_timeout", 120.0))
MAX_TASK_SUMMARY_CHARS = 500


# ---------------------------------------------------------------------------
# Trajectory digest — deterministic, no LLM
# ---------------------------------------------------------------------------

def _tool_line(name: str, args_raw, error: bool) -> str:
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except Exception:
        args = args_raw
    try:
        brief = json.dumps(args, sort_keys=True)[:120]
    except Exception:
        brief = str(args)[:120]
    mark = "!" if error else " "
    return f"  {mark} {name}({brief})"


def digest_trajectory(agent, user_text: str) -> str:
    """Build a compact text digest of the last task (last K tool turns,
    error signatures, final response tail) for the review prompt.

    Reads ``agent.task_log`` (ContextManager's per-turn entries) and the
    active :class:`TaskRecord` counters.  Bounded at ~3 KB.
    """
    parts: list[str] = []
    parts.append(f"User request: {user_text[:300]}")

    # Tool-turn lines from the task record's fingerprints + chat log.
    try:
        rec = getattr(agent, "_active_task", None) or getattr(
            agent, "_last_task_rec", None)
        if rec is not None:
            parts.append(
                f"Counters: llm_calls={rec.num_llm_calls} "
                f"tool_turns={rec.num_tool_turns} "
                f"failed_tools={rec.tool_calls_failed} "
                f"stream_retries={rec.stream_retries} "
                f"llm_latency={rec.llm_latency_s:.1f}s")
    except Exception:
        pass

    # Last K tool rows from chat_log for this session.
    try:
        rows = db.query_tool_rows(getattr(agent, "session_id", ""), limit=20)
    except Exception:
        rows = []
    if rows:
        lines = []
        for r in rows[-20:]:
            lines.append(_tool_line(
                r.get("tool_name") or "?", r.get("args_raw"),
                bool(r.get("is_error"))))
        parts.append("Tool turns (last up to 20, ! = error):\n"
                     + "\n".join(lines))

    # Final assistant response tail.
    text = "\n\n".join(parts)
    return text[:3000]


def _build_llm_call(agent):
    """Return a ``prompt -> response`` callable bound to the local
    server with a bounded timeout."""
    from nbchat.core.client import get_client

    client = get_client()
    model = getattr(agent, "model_name", None) or config.MODEL_NAME
    timeout = REFINE_LLM_TIMEOUT

    def llm_call(prompt: str) -> str:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=refinement.REFINE_MAX_OUTPUT_TOKENS,
            timeout=timeout,
        )
        return (resp.choices[0].message.content or "").strip()

    return llm_call


def on_task_finished(agent, rec) -> bool:
    """Hook point — call after a user turn's task record has been
    finished (status ``complete``/``failed``/``interrupted``).

    Returns True when a refine round was scheduled, False otherwise.
    Never raises: a broken hook must not break the conversation path.
    """
    try:
        if rec is None or rec._finished is False:
            return False
        if not REFINE_HOOK_ENABLED:
            return False

        status = getattr(rec, "status", "complete") or "complete"
        ok, _reason = refinement.should_refine_task(
            status=status,
            failed_tools=rec.tool_calls_failed,
            num_tool_turns=rec.num_tool_turns,
        )
        if not ok:
            return False

        # One in-flight round per agent: drop if one is already running.
        hook = getattr(agent, "_refine_hook_state", None)
        if hook is None:
            hook = {"running": False}
            object.__setattr__(agent, "_refine_hook_state", hook) \
                if hasattr(agent, "__slots__") else setattr(agent,
                                                            "_refine_hook_state",
                                                            hook)
        if hook["running"]:
            return False
        hook["running"] = True

        session_id = getattr(agent, "session_id", "") or ""
        user_text = rec.request_text or ""
        trajectory = digest_trajectory(agent, user_text)
        report = getattr(agent, "_on_agent_message", None)

        def _run() -> None:
            started = time.time()
            try:
                llm_call = _build_llm_call(agent)
                result = refinement.run_refine_round(
                    session_id, user_text, trajectory, llm_call)
                n = len(result.applied)
                if n:
                    msg = (f"[refine] round {result.round_id}: "
                           f"{n} lesson(s) updated "
                           f"(took {time.time()-started:.0f}s)")
                else:
                    why = (result.skipped[0]
                           if result.skipped else "no edits needed")
                    msg = f"[refine] reviewed, no changes ({why})"
                if report is not None:
                    try:
                        report(msg)
                    except Exception:
                        pass
                db.insert_refine_event(
                    session_id, "refine:hook",
                    {"applied": n, "elapsed_s": round(time.time()-started, 1)},
                    action="hook", round_id=result.round_id)
            except Exception as exc:
                _log.warning("refine hook round failed: %s: %s",
                             type(exc).__name__, exc)
                try:
                    db.insert_refine_event(
                        session_id, "refine:hook",
                        {"error": f"{type(exc).__name__}: {exc}"[:300]},
                        action="hook:failed")
                except Exception:
                    pass
            finally:
                hook["running"] = False

        t = threading.Thread(
            target=_run, name="refine-hook", daemon=True)
        t.start()
        return True
    except Exception:
        _log.debug("refine hook failed to schedule", exc_info=True)
        return False
