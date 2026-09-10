"""Scorecard — the Phase 0 measurement backbone.

Every optimization phase compounds only if it is *measured* against a
baseline.  This module computes, from the existing data sources (no new
tables, no schema migration):

* **LLM metrics** — parsed from ``inference_metrics.log``
  (tokens per call, latency, stop reasons).
* **Tool metrics** — aggregated from the ``chat_log`` table (the
  conversation loop records one row per tool call; ``role='tool'``).
* **Task metrics** — aggregated from the ``task_log`` table (already
  stores per-task token counts, durations, redundancy counts, stall and
  truncation events).
* **Redundancy** — the headline metric the 2b read cache targets.
  Preferred source: ``task_log.redundant_tool_calls`` (exact, computed at
  turn level by the conversation loop).  Fallback: computed from
  ``chat_log`` by grouping identical (session, tool, normalized-args).

Public API:
    parse_llm_metrics_log(path)      -> dict
    aggregate(db_path=None)          -> dict   (full scorecard)
    redundant_by_session(limit=50)   -> list
    format_scorecard(card)           -> str    (one-page report)

The module is read-only and import-safe: a missing database or log file
yields an empty scorecard, never an exception.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

# ── Paths ────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).resolve().parent.parent / "chat_history.db"
DEFAULT_LLM_LOG = Path(__file__).resolve().parent.parent.parent / "inference_metrics.log"

# One LLM call: "Latency: 12.34s | P:12345 C:678 T:13023 | stop=stop"
_LLM_LINE_RE = re.compile(
    r"Latency:\s*([0-9.]+)s\s*\|\s*P:(\d+)\s*C:(\d+)\s*T:(\d+)"
    r"(?:\s*\|\s*stop=([a-z_]+))?"
)


# ── LLM log ──────────────────────────────────────────────────────────────
def parse_llm_metrics_log(path: str | Path = DEFAULT_LLM_LOG) -> dict:
    """Parse the inference-metrics log.

    Returns a dict with: ``llm_calls``, ``prompt_tokens``,
    ``completion_tokens``, ``total_tokens``, ``avg_latency_s``,
    ``max_latency_s``, ``stop_reasons`` (Counter-like dict).

    Missing file -> all-zero dict (never raises).
    """
    out = {
        "llm_calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "latency_sum": 0.0,
        "avg_latency_s": 0.0,
        "max_latency_s": 0.0,
        "stop_reasons": {},
    }
    p = Path(path)
    if not p.is_file():
        return out
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _LLM_LINE_RE.search(line)
        if not m:
            continue
        lat, p_tok, c_tok, t_tok, stop = m.groups()
        out["llm_calls"] += 1
        out["prompt_tokens"] += int(p_tok)
        out["completion_tokens"] += int(c_tok)
        out["total_tokens"] += int(t_tok)
        lat = float(lat)
        out["latency_sum"] += lat
        if lat > out["max_latency_s"]:
            out["max_latency_s"] = lat
        if stop:
            out["stop_reasons"][stop] = out["stop_reasons"].get(stop, 0) + 1
    if out["llm_calls"]:
        out["avg_latency_s"] = round(out["latency_sum"] / out["llm_calls"], 3)
    return out


# ── Helpers ──────────────────────────────────────────────────────────────
def _connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


def _normalize_args(args_json: str | None) -> str:
    """Stable key for a tool call: canonicalized JSON, else raw text."""
    if not args_json:
        return ""
    try:
        obj = json.loads(args_json)
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except (ValueError, TypeError):
        return re.sub(r"\s+", " ", args_json).strip()


# ── Aggregation ──────────────────────────────────────────────────────────
def aggregate(db_path: str | Path | None = None,
              llm_log: str | Path = DEFAULT_LLM_LOG) -> dict:
    """Build the full scorecard dict.

    Keys:
      llm_calls, prompt_tokens, completion_tokens, total_tokens,
      avg_latency_s, max_latency_s, stop_reasons,
      tasks, tasks_done, avg_task_duration_s, task_tokens (prompt/compl),
      tool_calls, tool_errors, tool_error_rate, avg_tool_calls_per_task,
      by_tool: [{tool, n, errors}],
      redundant_calls, redundant_rate, redundancy_source,
      stall_events, truncation_events, max_context_chars, error_count,
      sessions: [{session_id, tool_calls, errors, first_ts, last_ts}],
      sessions_n
    """
    out: dict[str, Any] = {
        **parse_llm_metrics_log(llm_log),
        "tasks": 0,
        "tasks_done": 0,
        "avg_task_duration_s": 0.0,
        "task_prompt_chars": 0,
        "task_completion_chars": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_error_rate": 0.0,
        "avg_tool_calls_per_task": 0.0,
        "by_tool": [],
        "redundant_calls": 0,
        "redundant_rate": 0.0,
        "redundancy_source": "none",
        "stall_events": 0,
        "truncation_events": 0,
        "max_context_chars": 0,
        "error_count": 0,
        "sessions": [],
        "sessions_n": 0,
    }

    db_file = Path(db_path or DB_PATH)
    if not db_file.is_file():
        return out
    conn = _connect(db_file)
    try:
        _aggregate_tasks(conn, out)
        _aggregate_chat_log(conn, out)
    finally:
        conn.close()

    # Cross metrics.
    if out["tasks"]:
        out["avg_tool_calls_per_task"] = round(
            out["tool_calls"] / out["tasks"], 1)
    return out


def _aggregate_tasks(conn: sqlite3.Connection, out: dict) -> None:
    rows = _query(conn, """
        SELECT COUNT(*)                                        AS n,
               SUM(CASE WHEN status IN ('done', 'completed', 'complete',
                                        'success', 'succeeded')
                        THEN 1 ELSE 0 END)                     AS n_done,
               COALESCE(AVG(CASE WHEN duration_s IS NOT NULL
                                 THEN duration_s END), 0)      AS avg_dur,
               COALESCE(SUM(prompt_chars), 0)                  AS prompt_chars,
               COALESCE(SUM(completion_chars), 0)              AS compl_chars,
               COALESCE(SUM(tool_calls_total), 0)              AS tool_calls,
               COALESCE(SUM(tool_calls_failed), 0)             AS tool_failed,
               COALESCE(SUM(redundant_tool_calls), 0)          AS redundant,
               COALESCE(SUM(stall_events), 0)                  AS stalls,
               COALESCE(SUM(truncation_events), 0)             AS truncs,
               COALESCE(MAX(max_context_chars), 0)             AS max_ctx,
               COALESCE(SUM(error_count), 0)                   AS err_count
        FROM task_log""")
    if not rows:
        return
    r = rows[0]
    out["tasks"] = int(r["n"] or 0)
    out["tasks_done"] = int(r["n_done"] or 0)
    out["avg_task_duration_s"] = round(float(r["avg_dur"] or 0), 1)
    out["task_prompt_chars"] = int(r["prompt_chars"] or 0)
    out["task_completion_chars"] = int(r["compl_chars"] or 0)
    out["stall_events"] = int(r["stalls"] or 0)
    out["truncation_events"] = int(r["truncs"] or 0)
    out["max_context_chars"] = int(r["max_ctx"] or 0)
    out["error_count"] = int(r["err_count"] or 0)
    # Task-level redundancy is the *exact* source (computed turn-by-turn
    # with mutation-awareness by the conversation loop).  Prefer it when
    # it reports data; fall back to the chat_log estimate below.
    t_calls = int(r["tool_calls"] or 0)
    if t_calls:
        out["redundant_calls"] = int(r["redundant"] or 0)
        out["redundant_rate"] = round(
            out["redundant_calls"] / t_calls, 3)
        out["redundancy_source"] = "task_log"


def _aggregate_chat_log(conn: sqlite3.Connection, out: dict) -> None:
    rows = _query(conn, """
        SELECT COUNT(*)                                        AS n,
               SUM(CASE WHEN COALESCE(error_flag, 0) = 1
                        THEN 1 ELSE 0 END)                     AS n_err
        FROM chat_log
        WHERE role = 'tool' AND tool_name IS NOT NULL""")
    if rows:
        r = rows[0]
        out["tool_calls"] = int(r["n"] or 0)
        out["tool_errors"] = int(r["n_err"] or 0)
        if out["tool_calls"]:
            out["tool_error_rate"] = round(
                out["tool_errors"] / out["tool_calls"], 3)

    by_tool = _query(conn, """
        SELECT tool_name,
               COUNT(*)                              AS n,
               SUM(CASE WHEN COALESCE(error_flag, 0) = 1
                        THEN 1 ELSE 0 END)           AS n_err
        FROM chat_log
        WHERE role = 'tool' AND tool_name IS NOT NULL
        GROUP BY tool_name
        ORDER BY n DESC, tool_name""")
    out["by_tool"] = [
        {"tool": r["tool_name"], "n": int(r["n"]),
         "errors": int(r["n_err"] or 0)}
        for r in by_tool
    ]

    # Per-session activity.
    out["sessions"] = _query(conn, """
        SELECT session_id,
               COUNT(*)                                        AS tool_calls,
               SUM(CASE WHEN COALESCE(error_flag, 0) = 1
                        THEN 1 ELSE 0 END)                     AS errors,
               MIN(ts)                                         AS first_ts,
               MAX(ts)                                         AS last_ts
        FROM chat_log
        WHERE role = 'tool' AND tool_name IS NOT NULL
        GROUP BY session_id
        ORDER BY last_ts DESC
        LIMIT 200""")
    out["sessions_n"] = len(out["sessions"])

    # Redundancy fallback: exact duplicate (session, tool, args) pairs.
    if out["redundancy_source"] != "task_log" and out["tool_calls"]:
        n_red = _redundant_from_chat_log(conn)
        if n_red is not None:
            out["redundant_calls"] = n_red
            out["redundant_rate"] = round(n_red / out["tool_calls"], 3)
            out["redundancy_source"] = "chat_log"


def _redundant_from_chat_log(conn: sqlite3.Connection) -> Optional[int]:
    """Count redundant tool calls from chat_log.

    Done in Python (not SQL) so the same normalization the executor cache
    uses applies: identical (session, tool, canonicalized-args).  O(rows)
    with rows bounded by LIMIT 20000; the real store is tiny (10^4 scale).
    """
    rows = _query(conn, """
        SELECT session_id, tool_name, tool_args
        FROM chat_log
        WHERE role = 'tool' AND tool_name IS NOT NULL
        ORDER BY id
        LIMIT 20000""")
    counts: dict[tuple, int] = {}
    for r in rows:
        key = (r["session_id"], r["tool_name"], _normalize_args(r["tool_args"]))
        counts[key] = counts.get(key, 0) + 1
    return sum(c - 1 for c in counts.values() if c > 1)


def redundant_by_session(limit: int = 50) -> list[dict]:
    """Per-session redundant-call rates, worst offenders first.

    Feeds the scorecard's re-read column and lets a later phase confirm
    whether the 2b read cache moved the number.
    """
    conn = _connect()
    try:
        rows = _query(conn, """
            SELECT session_id, tool_name, tool_args
            FROM chat_log
            WHERE role = 'tool' AND tool_name IS NOT NULL
            ORDER BY id
            LIMIT 20000""")
    finally:
        conn.close()
    per: dict[str, dict[str, int]] = {}
    counts: dict[tuple, int] = {}
    for r in rows:
        key = (r["session_id"], r["tool_name"], _normalize_args(r["tool_args"]))
        counts[key] = counts.get(key, 0) + 1
        s = per.setdefault(r["session_id"], {"tool_calls": 0, "redundant_calls": 0})
        s["tool_calls"] += 1
    for (session_id, _tool, _args), c in counts.items():
        if c > 1:
            per[session_id]["redundant_calls"] += c - 1
    out = []
    for session_id, s in per.items():
        if not s["tool_calls"]:
            continue
        s["redundant_rate"] = round(s["redundant_calls"] / s["tool_calls"], 3)
        out.append({"session_id": session_id, **s})
    out.sort(key=lambda d: (d["redundant_calls"], d["redundant_rate"]),
             reverse=True)
    return out[: int(limit)]


# ── Report ───────────────────────────────────────────────────────────────
def format_scorecard(card: dict) -> str:
    """Render the one-page text report."""
    lines: list[str] = []
    push = lines.append
    push("nbchat scorecard")
    push("================")
    push(f"LLM calls:        {card.get('llm_calls', 0)}")
    push(f"Tokens (P/C/T):   {card.get('prompt_tokens', 0):,} / "
         f"{card.get('completion_tokens', 0):,} / {card.get('total_tokens', 0):,}")
    push(f"Latency (avg/max): {card.get('avg_latency_s', 0.0):.3f}s / "
         f"{card.get('max_latency_s', 0.0):.1f}s")
    stops = card.get("stop_reasons") or {}
    if stops:
        push("Stop reasons:     " + ", ".join(
            f"{k}={v}" for k, v in sorted(stops.items())))
    push(f"Tasks:            {card.get('tasks', 0)} "
         f"({card.get('tasks_done', 0)} done), "
         f"avg {card.get('avg_task_duration_s', 0.0):.2f}s, "
         f"task-tokens P={card.get('task_prompt_chars', 0):,} "
         f"C={card.get('task_completion_chars', 0):,}")
    push(f"Tool calls:       {card.get('tool_calls', 0)}, "
         f"{card.get('tool_errors', 0)} errors "
         f"({card.get('tool_error_rate', 0.0):.3f} rate)"
         + (f", {card.get('avg_tool_calls_per_task', 0.0)} per task"
            if card.get("tasks") else ""))
    push(f"Redundant calls:  {card.get('redundant_calls', 0)} "
         f"({card.get('redundant_rate', 0.0):.3f} rate, "
         f"source={card.get('redundancy_source', 'n/a')})")
    push(f"Stalls / truncs:  {card.get('stall_events', 0)} / "
         f"{card.get('truncation_events', 0)}, "
         f"peak context {card.get('max_context_chars', 0):,} chars, "
         f"task errors {card.get('error_count', 0)}")
    if card.get("by_tool"):
        push("")
        push("By tool:")
        for t in card["by_tool"][:10]:
            push(f"  {t['tool']:<22} {t['n']:>5} calls, {t['errors']} errors")
    if card.get("sessions"):
        push("")
        push("Recent sessions (tool activity):")
        for s in card["sessions"][:5]:
            push(f"  {s['session_id']:<32} {s['tool_calls']:>5} calls, "
                 f"{s['errors']} err, {s['last_ts']}")
    return "\n".join(lines)


def _empty_aggregate() -> dict:
    return {
        "llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
        "total_tokens": 0, "avg_latency_s": 0.0, "max_latency_s": 0.0,
        "stop_reasons": {},
        "tasks": 0, "tasks_done": 0, "avg_task_duration_s": 0.0,
        "task_prompt_chars": 0, "task_completion_chars": 0,
        "tool_calls": 0, "tool_errors": 0, "tool_error_rate": 0.0,
        "avg_tool_calls_per_task": 0.0,
        "by_tool": [], "redundant_calls": 0, "redundant_rate": 0.0,
        "redundancy_source": "none",
        "stall_events": 0, "truncation_events": 0, "max_context_chars": 0,
        "error_count": 0, "sessions": [], "sessions_n": 0,
    }
