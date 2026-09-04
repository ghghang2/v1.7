"""Task completion statistics — per-task recording and aggregation.

One *task* is one user-initiated turn of the agentic loop: from the user
message (or an injected email/voice transcript) until the loop terminates.
``ConversationMixin`` drives this module:

    rec = task_tracker.start_task(agent, user_text)     # loop start
    ... hooks: record_llm_call / record_tool_call / record_event ...
    task_tracker.finish_task(rec, final_response)       # loop end

A single ``task_log`` row per task is written at loop end (plus one cheap
``in_progress`` row at loop start so a killed process still leaves a
traceable record — see ``_sweep_orphan_tasks`` in :mod:`nbchat.core.db`).

Design notes live in ``docs/task_tracking.md``.
"""
from __future__ import annotations

import json
import statistics
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from nbchat.core import db

# ── Classification sets ─────────────────────────────────────────────
# Read vs write for the redundancy breakdown: reads are cheap, idempotent
# and the dominant source of observed waste (re-reading files); everything
# else executed is counted as a write.
READ_TOOLS = frozenset({"read_file", "get_weather", "repo_overview"})
WRITE_TOOLS = frozenset({
    "run_command", "run_tests", "create_file", "make_change_to_file",
    "push_to_github", "browser", "send_email",
})

_STATUS_TO_COMPLETION = {
    "complete": "complete",
    "interrupted": "partial",
    "failed": "not_completed",
    "in_progress": "unknown",
}


def _utcnow() -> str:
    """UTC timestamp matching SQLite's ``datetime('now')`` format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fingerprint(tool_name: str, args_raw) -> str:
    """Stable identity of a tool call for redundancy detection.

    ``args_raw`` is the raw arguments JSON string (or dict).  A call is
    *redundant* in a task when its fingerprint was already issued earlier
    in the same task — i.e. the same tool with the same effective
    arguments was run again (a re-read, a duplicated command, a repeated
    broken call).  Non-JSON args fall back to the raw string so the
    counter never crashes on malformed input.
    """
    if isinstance(args_raw, dict):
        args_raw = json.dumps(args_raw)
    try:
        norm = json.dumps(json.loads(args_raw), sort_keys=True)
    except Exception:
        norm = str(args_raw or "")
    return f"{tool_name}::{norm}"


# ── Per-task record ──────────────────────────────────────────────────

@dataclass
class TaskRecord:
    """Mutable telemetry state for one in-flight task."""

    task_id: int | None = None          # task_log row id (set at start)
    session_id: str = ""
    request_text: str = ""
    request_chars: int = 0
    started_at: str = ""
    start_ts: float = field(default_factory=time.time, repr=False)
    baseline_user_row_id: int | None = None  # last user row id at task start
    agent_user_rows: int = 0  # agent-injected role='user' rows logged in-loop

    # machine-derived counters (hooks)
    num_llm_calls: int = 0
    num_tool_turns: int = 0
    tool_calls_total: int = 0
    tool_calls_by_name: dict = field(default_factory=dict)
    tool_calls_failed: int = 0
    stall_events: int = 0
    truncation_events: int = 0
    stream_retries: int = 0
    text_toolcall_recovery: int = 0
    llm_latency_s: float = 0.0
    prompt_chars: int = 0
    completion_chars: int = 0
    max_context_chars: int = 0
    error_count: int = 0

    # analytic state (filled at finish)
    fingerprints: list = field(default_factory=list, repr=False)  # in order
    turn_ids: list = field(default_factory=list, repr=False)      # chat_log ids
    _finished: bool = field(default=False, repr=False)

    # ── Hook API ────────────────────────────────────────────────

    def record_llm_call(self, latency_s: float = 0.0, prompt_chars: int = 0,
                        completion_chars: int = 0) -> None:
        self.num_llm_calls += 1
        self.llm_latency_s += max(float(latency_s or 0.0), 0.0)
        self.prompt_chars += int(prompt_chars or 0)
        self.completion_chars += int(completion_chars or 0)

    def record_tool_call(self, tool_name: str, args_raw, error: bool,
                         is_tool_turn: bool = True) -> None:
        self.tool_calls_total += 1
        self.tool_calls_by_name[tool_name] = \
            self.tool_calls_by_name.get(tool_name, 0) + 1
        self.fingerprints.append(_fingerprint(tool_name, args_raw))
        if is_tool_turn:
            self.num_tool_turns += 1
        if error:
            self.tool_calls_failed += 1
            self.error_count += 1

    def record_stream_error(self) -> None:
        self.error_count += 1

    def note_tool_turn(self) -> None:
        """Bump once per LLM response that requested tools."""
        self.num_tool_turns += 1

    def note_tool_error(self) -> None:
        """Count a failed tool call (totals are tracked by record_tool_call)."""
        self.tool_calls_failed += 1
        self.error_count += 1

    def note_agent_user_row(self) -> None:
        """Record one agent-injected role='user' row (nudge/stall) logged
        in-loop.  These rows share the user role in chat_log but are not
        user effort, so they are subtracted from user_interventions."""
        self.agent_user_rows += 1

    def record_event(self, kind: str) -> None:
        """Bump one of the named event counters: 'stall' | 'truncation' |
        'stream_retry' | 'text_toolcall'."""
        attr = {"stall": "stall_events",
                "truncation": "truncation_events",
                "stream_retry": "stream_retries",
                "text_toolcall": "text_toolcall_recovery"}.get(kind)
        if attr:
            setattr(self, attr, getattr(self, attr) + 1)

    def record_context(self, context_chars: int) -> None:
        self.max_context_chars = max(self.max_context_chars,
                                     int(context_chars or 0))

    # ── Analytics ───────────────────────────────────────────────

    def redundancy(self) -> tuple[int, int, int]:
        """(redundant_total, redundant_reads, redundant_writes).

        A call is redundant if its fingerprint occurred earlier *in this
        task* (see module docstring / docs/task_tracking.md for the exact
        semantics and limits).
        """
        seen: set = set()
        total = reads = writes = 0
        for fp in self.fingerprints:
            if fp in seen:
                total += 1
                if fp.split("::", 1)[0] in READ_TOOLS:
                    reads += 1
                else:
                    writes += 1
            else:
                seen.add(fp)
        return total, reads, writes


# ── Lifecycle ────────────────────────────────────────────────────────

def _max_user_row_id(session_id: str) -> int | None:
    """The session's most recent user-row id in chat_log, or None."""
    if not session_id:
        return None
    conn = db._connect()
    try:
        row = conn.execute(
            "SELECT MAX(id) FROM chat_log WHERE session_id=? AND role='user'",
            (session_id,),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def start_task(agent, user_text: str) -> TaskRecord:
    """Open a task record at conversation-loop start.

    *agent* must expose ``session_id`` and (optionally) ``model_name``.
    Never raises: telemetry must not break the conversation loop.
    """
    rec = TaskRecord(
        session_id=getattr(agent, "session_id", "") or "",
        request_text=(user_text or "")[:2000],
        request_chars=len(user_text or ""),
        started_at=_utcnow(),
    )
    try:
        # Baseline for user_interventions: the session's last user row at
        # task start is the turn's own trigger message; any user row with a
        # higher id (persisted mid-turn) is a redirect and counts.
        rec.baseline_user_row_id = _max_user_row_id(rec.session_id)
    except Exception:
        rec.baseline_user_row_id = None
    try:
        rec.task_id = db.record_task(
            None,
            session_id=rec.session_id,
            request_text=rec.request_text,
            request_chars=rec.request_chars,
            status="in_progress",
            started_at=rec.started_at,
        )
        rec.turn_ids.append(uuid.uuid4().hex[:12])  # sentinel for our own row
    except Exception:
        rec.task_id = None  # in-memory-only fallback: hooks still count
    return rec


def finish_task(rec: TaskRecord, final_response: str | None = None,
                status: str = "complete") -> dict | None:
    """Finalise and persist a task record.  Idempotent per record.

    The analytic pass re-reads the session's persisted chat_log rows to
    compute what the in-loop hooks cannot observe: user interventions
    (redirect messages) logged during the task's window, and the final
    assistant reply length.  Returns the persisted field dict (or None on
    failure).
    """
    if rec is None or rec._finished or rec.task_id is None:
        return None
    rec._finished = True

    fields: dict = {
        "status": status,
        "completion": _STATUS_TO_COMPLETION.get(status, "unknown"),
        "ended_at": _utcnow(),
        "duration_s": round(time.time() - rec.start_ts, 3),
        "num_llm_calls": rec.num_llm_calls,
        "num_tool_turns": rec.num_tool_turns,
        "tool_calls_total": rec.tool_calls_total,
        "tool_calls_by_name": json.dumps(rec.tool_calls_by_name),
        "tool_calls_failed": rec.tool_calls_failed,
        "stall_events": rec.stall_events,
        "truncation_events": rec.truncation_events,
        "stream_retries": rec.stream_retries,
        "text_toolcall_recovery": rec.text_toolcall_recovery,
        "llm_latency_s": round(rec.llm_latency_s, 3),
        "prompt_chars": rec.prompt_chars,
        "completion_chars": rec.completion_chars,
        "max_context_chars": rec.max_context_chars,
        "error_count": rec.error_count,
        "final_response_chars": len(final_response or ""),
        "turn_ids": json.dumps(rec.turn_ids),
    }

    # ── Analytic pass (best effort; the row is written regardless) ──
    try:
        interventions = _count_user_interventions(rec)
        fields["user_interventions"] = interventions
        fields["redundant_tool_calls"], fields["redundant_reads"], \
            fields["redundant_writes"] = rec.redundancy()
    except Exception:
        fields.setdefault("user_interventions", 0)
        r, rr, rw = rec.redundancy()
        fields.update(redundant_tool_calls=r, redundant_reads=rr,
                      redundant_writes=rw)

    try:
        db.record_task(rec.task_id, **fields)
        return fields
    except Exception:
        return None


def _count_user_interventions(rec: TaskRecord) -> int:
    """Mid-task user rows for the session, by row-id ordering.

    ``chat_log`` rows carry second-resolution timestamps, so a time-window
    query cannot reliably separate the turn's own trigger message (logged
    moments before the task row opens, usually in the same second) from a
    redirect typed mid-turn.  Row ids, on the other hand, are strict
    ordering: the trigger message is the session's last user row at task
    start, so any user row with a higher id arrived mid-task.  Supervisor
    interjections are deliberately excluded — they are agent-side
    injections, not user effort.
    """
    if not rec.session_id:
        return 0
    # Agent-injected user rows (truncation/mid-stream nudges, stall
    # interrupts) share the user role in chat_log but are not user effort,
    # so they are subtracted here.
    agent_rows = int(rec.agent_user_rows)
    conn = db._connect()
    try:
        if rec.baseline_user_row_id is None:
            return 0
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM chat_log "
            "WHERE session_id=? AND role='user' AND id > ?",
            (rec.session_id, rec.baseline_user_row_id),
        ).fetchone()
        return max(0, int(n or 0) - agent_rows)
    finally:
        conn.close()


# ── Aggregation ──────────────────────────────────────────────────────

def summarize_tasks(rows: list[dict]) -> dict:
    """Aggregate task rows (from ``db.task_summary_rows`` / ``query_tasks``)
    into a compact performance summary suitable for printing or storage."""
    n = len(rows)
    by_status: dict = {}
    by_completion: dict = {}
    durations: list = []
    totals = {
        "llm_calls": 0, "tool_turns": 0, "tool_calls": 0, "tool_failed": 0,
        "redundant": 0, "redundant_reads": 0, "redundant_writes": 0,
        "stalls": 0, "truncations": 0, "stream_retries": 0,
        "interventions": 0,
    }
    for r in rows:
        by_status[r.get("status") or "?"] = by_status.get(r.get("status") or "?", 0) + 1
        comp = r.get("completion") or "?"
        by_completion[comp] = by_completion.get(comp, 0) + 1
        d = r.get("duration_s")
        if d is not None:
            durations.append(float(d))
        totals["llm_calls"] += int(r.get("num_llm_calls") or 0)
        totals["tool_turns"] += int(r.get("num_tool_turns") or 0)
        totals["tool_calls"] += int(r.get("tool_calls_total") or 0)
        totals["tool_failed"] += int(r.get("tool_calls_failed") or 0)
        totals["redundant"] += int(r.get("redundant_tool_calls") or 0)
        totals["redundant_reads"] += int(r.get("redundant_reads") or 0)
        totals["redundant_writes"] += int(r.get("redundant_writes") or 0)
        totals["stalls"] += int(r.get("stall_events") or 0)
        totals["truncations"] += int(r.get("truncation_events") or 0)
        totals["stream_retries"] += int(r.get("stream_retries") or 0)
        totals["interventions"] += int(r.get("user_interventions") or 0)

    redundant_ratio = (round(totals["redundant"] / totals["tool_calls"], 3)
                       if totals["tool_calls"] else 0.0)
    return {
        "tasks": n,
        "by_status": by_status,
        "by_completion": by_completion,
        "failure_rate": (round(by_status.get("failed", 0) / n, 3)
                         if n else 0.0),
        "duration_s": {
            "mean": round(statistics.fmean(durations), 1) if durations else 0.0,
            "median": round(statistics.median(durations), 1) if durations else 0.0,
            "max": round(max(durations), 1) if durations else 0.0,
        },
        "totals": totals,
        "redundant_ratio": redundant_ratio,
    }
