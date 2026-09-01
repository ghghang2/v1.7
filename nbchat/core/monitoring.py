"""Lightweight monitoring for compression quality and prefix cache alignment.

Two concerns are tracked independently and then correlated:

1. Prefix cache alignment
   Parsed from llama_server.log after each LLM call.  Key signals:
   - sim_best: LCP similarity the server found for this request.  < 0.70
     means the volatile context block (messages[1]) changed enough to bust
     the cache past the system prompt anchor.
   - new_tokens: batch.n_tokens from "prompt processing done".  Low values
     relative to total_tokens confirm cache reuse.
   - cache_hit_rate: (total_tokens - new_tokens) / total_tokens
   - checkpoints_erased: how many intermediate checkpoints were invalidated
     before the server found a stable anchor.

2. Compression quality
   Instrumented per tool call in conversation.py:
   - was_compressed: whether the raw output was modified before sending to model
   - had_error: whether the raw output contained error signals (error_flag=1)
   - strategy: which compression path was taken
   Derived metrics:
   - reread_rate: fraction of compressed calls that triggered lossless learning
     (the model re-requested the same content → compression was lossy)
   - error_after_compression_rate: fraction of compressed calls where the raw
     result had an error flag → compressor may have stripped the error signal
   - llm_failure_rate: fraction of LLM-path calls that fell back to head+tail
   - no_output_rate: fraction of calls returning NO_RELEVANT_OUTPUT

Cross-session stats are persisted to session_meta under the sentinel session_id
"__global__" so no new tables are required.  Each session merges its data into
the global aggregate on completion.

Usage
-----
In conversation.py:

    monitor = get_session_monitor(self.session_id)

    # After build_messages, once per LLM call:
    volatile_len = (
        len(messages[1]["content"])
        if len(messages) > 2 and messages[1].get("role") == "user"
        else 0
    )
    monitor.record_llm_call(volatile_len)

    # After each tool call:
    monitor.record_tool_call(
        tool_name=tool_name,
        was_compressed=(compressed != raw_result),
        had_error=bool(error_flag),
        strategy=comp.get_last_strategy(tool_name),  # optional
    )

    # On session end / new session:
    flush_session_monitor(session_id, db_module)
"""
from __future__ import annotations

import json
import logging
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger("nbchat.monitoring")

# Path to llama.cpp server log — same as chatui.py metrics updater.
_LOG_PATH = Path("llama_server.log")
_LOG_TAIL_BYTES = 8_000   # bytes to read from log tail per parse

# Thresholds for config suggestions.
_REREAD_RATE_THRESHOLD = 0.15       # > 15% reread → add to ALWAYS_KEEP_TOOLS
_ERROR_COMPRESSION_THRESHOLD = 0.10 # > 10% errors after compression → concerning
_LLM_FAILURE_THRESHOLD = 0.20       # > 20% LLM fallback → LLM path unreliable
_NO_OUTPUT_THRESHOLD = 0.40         # > 40% NO_RELEVANT_OUTPUT → tool is noisy
_POOR_RATIO_THRESHOLD = 0.85        # avg_ratio > 0.85 → LLM barely compressing
_LOW_SIM_THRESHOLD = 0.70           # sim_best below this = significant cache bust
_HIGH_INVALIDATION_THRESHOLD = 0.30 # > 30% turns with checkpoint erasure

# DB sentinel for global (cross-session) stats.
_GLOBAL_SESSION_ID = "__global__"
_GLOBAL_META_KEY = "monitoring_global_v1"


# ── Log parsing ───────────────────────────────────────────────────────────────

@dataclass
class _CacheMetrics:
    sim_best: float = 0.0
    f_keep: float = 0.0
    new_tokens: int = 0
    total_tokens: int = 0
    cache_hit_rate: float = 0.0
    checkpoint_restored: bool = False
    checkpoints_erased: int = 0
    prompt_ms_per_token: float = 0.0
    valid: bool = False   # False = log was absent or parse failed


def parse_last_completion_metrics(log_path: Path = None) -> _CacheMetrics:
    """Parse the most recent completed LLM call from the llama.cpp server log.

    Reads the last _LOG_TAIL_BYTES from the file and extracts cache metrics
    for the final completion block.  Returns an invalid _CacheMetrics if the
    log is absent or the block cannot be parsed.
    """
    m = _CacheMetrics()
    log_path = log_path or _LOG_PATH
    if not log_path.exists():
        return m
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _LOG_TAIL_BYTES))
            raw = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return m

    lines = raw.splitlines()

    # Walk backwards to find the most recent "prompt processing done" line,
    # which marks the end of a completion's prompt phase.
    done_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if "prompt processing done" in lines[i]:
            done_idx = i
            break
    if done_idx is None:
        return m

    # Collect the block from the "selected slot by LCP similarity" line that
    # precedes this completion back to done_idx.
    block_start = done_idx
    for i in range(done_idx, -1, -1):
        if "selected slot by" in lines[i]:
            block_start = i
            break

    block = lines[block_start : done_idx + 1]
    text = "\n".join(block)

    # sim_best and f_keep
    sim_m = re.search(r"sim_best\s*=\s*([\d.]+)", text)
    fk_m = re.search(r"f_keep\s*=\s*([\d.]+)", text)
    if sim_m:
        m.sim_best = float(sim_m.group(1))
    if fk_m:
        m.f_keep = float(fk_m.group(1))

    # Total and new tokens from "prompt processing done"
    done_m = re.search(
        r"prompt processing done,\s*n_tokens\s*=\s*(\d+),\s*batch\.n_tokens\s*=\s*(\d+)",
        text,
    )
    if done_m:
        m.total_tokens = int(done_m.group(1))
        m.new_tokens = int(done_m.group(2))
        if m.total_tokens > 0:
            m.cache_hit_rate = (m.total_tokens - m.new_tokens) / m.total_tokens

    # Checkpoint events
    m.checkpoint_restored = "restored context checkpoint" in text
    m.checkpoints_erased = text.count("erased invalidated context checkpoint")

    # Prompt eval ms/token from timing block (may be in next few lines after done_idx)
    timing_search = "\n".join(lines[done_idx : done_idx + 10])
    timing_m = re.search(r"prompt eval time\s*=.*?(\d+\.\d+)\s*ms per token", timing_search)
    if timing_m:
        m.prompt_ms_per_token = float(timing_m.group(1))

    m.valid = bool(sim_m or done_m)
    return m


# ── Per-tool compression accumulator ─────────────────────────────────────────

@dataclass
class _ToolAccum:
    calls: int = 0
    compressed_calls: int = 0
    reread_triggers: int = 0    # lossless_learned strategy fires
    error_after_compression: int = 0
    no_output_count: int = 0
    llm_failure_count: int = 0
    total_input_chars: int = 0
    total_output_chars: int = 0


# ── Session monitor ───────────────────────────────────────────────────────────

@dataclass
class _CacheAccum:
    turn_count: int = 0
    sum_sim_best: float = 0.0
    low_sim_turns: int = 0         # sim_best < _LOW_SIM_THRESHOLD
    cache_invalidations: int = 0   # turns where checkpoints_erased > 0
    sum_volatile_len: int = 0
    sum_volatile_delta: int = 0    # |len(t) - len(t-1)| summed
    _prev_volatile_len: int = field(default=0, repr=False)


class SessionMonitor:
    """Accumulates per-session compression and cache metrics.

    Call record_llm_call() once after each build_messages/stream call.
    Call record_tool_call() once per tool execution.
    Call get_session_report() to inspect current state.

    Thread-safe via a simple lock — the conversation loop runs on a
    background thread while get_session_report() may be called from the
    UI thread.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._lock = threading.Lock()
        self._cache = _CacheAccum()
        self._tools: dict[str, _ToolAccum] = defaultdict(_ToolAccum)

    def record_llm_call(self, volatile_len: int = 0) -> None:
        """Record one LLM call.  Parses the server log for cache metrics.

        Parameters
        ----------
        volatile_len:
            Character length of messages[1]["content"] (the volatile context
            block) for this call.  Pass 0 if no volatile block was emitted.
        """
        metrics = parse_last_completion_metrics()
        with self._lock:
            c = self._cache
            c.turn_count += 1
            c.sum_volatile_len += volatile_len
            c.sum_volatile_delta += abs(volatile_len - c._prev_volatile_len)
            c._prev_volatile_len = volatile_len

            if metrics.valid:
                c.sum_sim_best += metrics.sim_best
                if metrics.sim_best < _LOW_SIM_THRESHOLD:
                    c.low_sim_turns += 1
                if metrics.checkpoints_erased > 0:
                    c.cache_invalidations += 1

    def record_tool_call(
        self,
        tool_name: str,
        was_compressed: bool,
        had_error: bool,
        strategy: str = "",
        input_chars: int = 0,
        output_chars: int = 0,
    ) -> None:
        """Record one tool execution.

        Parameters
        ----------
        tool_name:
            Name of the tool that was called.
        was_compressed:
            True if the raw output was modified before being sent to the model.
        had_error:
            True if the raw output contained error signals (error_flag=1).
        strategy:
            The compressor strategy string (e.g. "llm", "syntax_py",
            "lossless_learned").  Pass "" if unknown.
        input_chars, output_chars:
            Raw and compressed character counts.  Used for ratio tracking.
        """
        with self._lock:
            t = self._tools[tool_name]
            t.calls += 1
            if was_compressed:
                t.compressed_calls += 1
                if had_error:
                    t.error_after_compression += 1
            if strategy == "lossless_learned":
                t.reread_triggers += 1
            if strategy in ("headtail_llm_fallback",):
                t.llm_failure_count += 1
            # Note: Use record_no_output() explicitly to mark tool calls with no output
            if input_chars:
                t.total_input_chars += input_chars
                t.total_output_chars += output_chars

    def record_no_output(self, tool_name: str) -> None:
        """Convenience: record a NO_RELEVANT_OUTPUT compression outcome."""
        with self._lock:
            self._tools[tool_name].no_output_count += 1

    def get_session_report(self) -> dict:
        """Return a snapshot of current session metrics.

        Structure
        ---------
        {
          "session_id": str,
          "cache": {
            "turn_count": int,
            "avg_sim_best": float,
            "low_sim_rate": float,      # fraction of turns with sim < 0.70
            "cache_invalidation_rate": float,
            "avg_volatile_len": int,
            "avg_volatile_delta": int,
          },
          "tools": {
            "<tool_name>": {
              "calls": int,
              "compressed_calls": int,
              "compression_rate": float,
              "avg_ratio": float,
              "reread_rate": float,
              "error_after_compression_rate": float,
              "llm_failure_rate": float,
              "no_output_rate": float,
            },
            ...
          },
          "warnings": [ str, ... ],   # immediate issues detected this session
        }
        """
        with self._lock:
            c = self._cache
            turns = max(c.turn_count, 1)
            cache_report = {
                "turn_count": c.turn_count,
                "avg_sim_best": round(c.sum_sim_best / turns, 3),
                "low_sim_rate": round(c.low_sim_turns / turns, 3),
                "cache_invalidation_rate": round(c.cache_invalidations / turns, 3),
                "avg_volatile_len": c.sum_volatile_len // turns,
                "avg_volatile_delta": c.sum_volatile_delta // turns,
            }

            tools_report: dict = {}
            for name, t in self._tools.items():
                comp = max(t.compressed_calls, 1)
                ratio = (
                    t.total_output_chars / t.total_input_chars
                    if t.total_input_chars else 1.0
                )
                tools_report[name] = {
                    "calls": t.calls,
                    "compressed_calls": t.compressed_calls,
                    "compression_rate": round(t.compressed_calls / max(t.calls, 1), 3),
                    "avg_ratio": round(ratio, 3),
                    "reread_rate": round(t.reread_triggers / max(t.calls, 1), 3),
                    "error_after_compression_rate": round(
                        t.error_after_compression / comp, 3
                    ),
                    "llm_failure_rate": round(t.llm_failure_count / comp, 3),
                    "no_output_rate": round(t.no_output_count / max(t.calls + t.no_output_count, 1), 3),
                }

            warnings = _detect_warnings(cache_report, tools_report)

        return {
            "session_id": self.session_id,
            "cache": cache_report,
            "tools": tools_report,
            "warnings": warnings,
        }

    def to_mergeable(self) -> dict:
        """Serialise session data for merging into global stats."""
        with self._lock:
            return {
                "cache": {
                    "turn_count": self._cache.turn_count,
                    "sum_sim_best": self._cache.sum_sim_best,
                    "low_sim_turns": self._cache.low_sim_turns,
                    "cache_invalidations": self._cache.cache_invalidations,
                    "sum_volatile_delta": self._cache.sum_volatile_delta,
                },
                "tools": {
                    name: {
                        "calls": t.calls,
                        "compressed_calls": t.compressed_calls,
                        "reread_triggers": t.reread_triggers,
                        "error_after_compression": t.error_after_compression,
                        "no_output_count": t.no_output_count,
                        "llm_failure_count": t.llm_failure_count,
                        "total_input_chars": t.total_input_chars,
                        "total_output_chars": t.total_output_chars,
                    }
                    for name, t in self._tools.items()
                },
            }


# ── Warning detection (immediate, per-session) ────────────────────────────────

def _detect_warnings(cache: dict, tools: dict) -> list[str]:
    warnings: list[str] = []

    if cache["turn_count"] >= 5:
        if cache["avg_sim_best"] < _LOW_SIM_THRESHOLD and cache["avg_sim_best"] > 0:
            warnings.append(
                f"avg_sim_best={cache['avg_sim_best']:.3f} < {_LOW_SIM_THRESHOLD} — "
                f"volatile context block is changing too much between turns; "
                f"cache prefix is being busted frequently."
            )
        if cache["cache_invalidation_rate"] > _HIGH_INVALIDATION_THRESHOLD:
            warnings.append(
                f"cache_invalidation_rate={cache['cache_invalidation_rate']:.1%} — "
                f"checkpoints are being erased on >{_HIGH_INVALIDATION_THRESHOLD:.0%} "
                f"of turns; volatile block size or content instability is the likely cause."
            )

    for name, t in tools.items():
        if t["calls"] < 3:
            continue   # too few samples
        if t["reread_rate"] > _REREAD_RATE_THRESHOLD:
            warnings.append(
                f"{name}: reread_rate={t['reread_rate']:.1%} — model is re-requesting "
                f"compressed content; compression is likely lossy for this tool."
            )
        if t["error_after_compression_rate"] > _ERROR_COMPRESSION_THRESHOLD:
            warnings.append(
                f"{name}: error_after_compression_rate={t['error_after_compression_rate']:.1%} — "
                f"errors are occurring after compressed calls; compressor may be "
                f"stripping error-critical context."
            )
        if t["llm_failure_rate"] > _LLM_FAILURE_THRESHOLD:
            warnings.append(
                f"{name}: llm_failure_rate={t['llm_failure_rate']:.1%} — LLM compressor "
                f"is failing frequently; this tool should use head+tail instead."
            )

    return warnings


# ── Global (cross-session) stats ──────────────────────────────────────────────

def _empty_global() -> dict:
    return {
        "sessions_seen": 0,
        "cache": {
            "turn_count": 0,
            "sum_sim_best": 0.0,
            "low_sim_turns": 0,
            "cache_invalidations": 0,
            "sum_volatile_delta": 0,
        },
        "tools": {},
    }


def merge_into_global(global_stats: dict, session_data: dict) -> dict:
    """Return a new global stats dict with session_data merged in.

    Both dicts follow the shape returned by SessionMonitor.to_mergeable().
    Pure function — does not mutate either argument.
    """
    g = json.loads(json.dumps(global_stats))   # deep copy
    g["sessions_seen"] = g.get("sessions_seen", 0) + 1

    sc = session_data["cache"]
    gc = g["cache"]
    for k in ("turn_count", "sum_sim_best", "low_sim_turns",
              "cache_invalidations", "sum_volatile_delta"):
        gc[k] = gc.get(k, 0) + sc.get(k, 0)

    for tool_name, st in session_data["tools"].items():
        if tool_name not in g["tools"]:
            g["tools"][tool_name] = {
                "calls": 0,
                "compressed_calls": 0,
                "reread_triggers": 0,
                "error_after_compression": 0,
                "no_output_count": 0,
                "llm_failure_count": 0,
                "total_input_chars": 0,
                "total_output_chars": 0,
            }
        gt = g["tools"][tool_name]
        for k in gt:
            gt[k] = gt.get(k, 0) + st.get(k, 0)

    return g


def get_global_report(global_stats: dict) -> dict:
    """Compute derived metrics from raw global stats.

    Returns a report dict with the same structure as SessionMonitor.get_session_report()
    but aggregated across all sessions.
    """
    gc = global_stats.get("cache", {})
    turns = max(gc.get("turn_count", 0), 1)

    cache_report = {
        "turn_count": gc.get("turn_count", 0),
        "sessions_seen": global_stats.get("sessions_seen", 0),
        "avg_sim_best": round(gc.get("sum_sim_best", 0) / turns, 3),
        "low_sim_rate": round(gc.get("low_sim_turns", 0) / turns, 3),
        "cache_invalidation_rate": round(gc.get("cache_invalidations", 0) / turns, 3),
        "avg_volatile_delta": gc.get("sum_volatile_delta", 0) // turns,
    }

    tools_report: dict = {}
    for name, t in global_stats.get("tools", {}).items():
        comp = max(t.get("compressed_calls", 0), 1)
        ratio = (
            t["total_output_chars"] / t["total_input_chars"]
            if t.get("total_input_chars") else 1.0
        )
        tools_report[name] = {
            "calls": t.get("calls", 0),
            "compressed_calls": t.get("compressed_calls", 0),
            "avg_ratio": round(ratio, 3),
            "reread_rate": round(t.get("reread_triggers", 0) / max(t.get("calls", 1), 1), 3),
            "error_after_compression_rate": round(
                t.get("error_after_compression", 0) / comp, 3
            ),
            "llm_failure_rate": round(t.get("llm_failure_count", 0) / comp, 3),
            "no_output_rate": round(
                t.get("no_output_count", 0) / max(t.get("calls", 1), 1), 3
            ),
        }

    suggestions = suggest_config(cache_report, tools_report)

    return {
        "cache": cache_report,
        "tools": tools_report,
        "suggestions": suggestions,
    }


def suggest_config(cache: dict, tools: dict) -> list[dict]:
    """Return a list of concrete config change suggestions.

    Each suggestion is a dict:
    {
        "priority": "high" | "medium" | "low",
        "target": "<config key or tool name>",
        "action": "<what to change>",
        "reason": "<evidence summary>",
    }
    """
    suggestions: list[dict] = []
    turns = max(cache.get("turn_count", 0), 1)

    # ── Cache alignment suggestions ───────────────────────────────────────
    avg_sim = cache.get("avg_sim_best", 1.0)
    if turns >= 20 and 0 < avg_sim < _LOW_SIM_THRESHOLD:
        suggestions.append({
            "priority": "high",
            "target": "TASK_LOG_CAP / L1 entity limit",
            "action": (
                f"Reduce task_log[-20:] cap or CORE_MEMORY_ACTIVE_ENTITIES_LIMIT. "
                f"Consider reducing from 20→10 entries and 20→10 entities."
            ),
            "reason": (
                f"avg_sim_best={avg_sim:.3f} across {turns} turns indicates "
                f"messages[1] (volatile context) is changing too much between turns, "
                f"busting the prefix cache past the system prompt anchor."
            ),
        })

    inv_rate = cache.get("cache_invalidation_rate", 0.0)
    if turns >= 20 and inv_rate > _HIGH_INVALIDATION_THRESHOLD:
        suggestions.append({
            "priority": "medium",
            "target": "WINDOW_TURNS / MAX_WINDOW_ROWS",
            "action": (
                "Reduce WINDOW_TURNS or MAX_WINDOW_ROWS to shrink the per-call "
                "context size and stabilize the token sequence."
            ),
            "reason": (
                f"cache_invalidation_rate={inv_rate:.1%} — checkpoints are "
                f"frequently erased, suggesting the context is growing faster "
                f"than the SWA window can track."
            ),
        })

    # ── Tool compression suggestions ──────────────────────────────────────
    for name, t in tools.items():
        if t["calls"] < 10:
            continue   # insufficient data

        if t["reread_rate"] > _REREAD_RATE_THRESHOLD:
            suggestions.append({
                "priority": "high",
                "target": f"ALWAYS_KEEP_TOOLS (add '{name}')",
                "action": f"Add '{name}' to ALWAYS_KEEP_TOOLS in compressor.py.",
                "reason": (
                    f"reread_rate={t['reread_rate']:.1%} over {t['calls']} calls — "
                    f"the model repeatedly re-requests content from this tool, "
                    f"indicating compression is losing actionable information."
                ),
            })

        if t["error_after_compression_rate"] > _ERROR_COMPRESSION_THRESHOLD:
            suggestions.append({
                "priority": "high",
                "target": f"ALWAYS_KEEP_TOOLS (add '{name}')",
                "action": (
                    f"Add '{name}' to ALWAYS_KEEP_TOOLS, or specifically to "
                    f"FILE_READ_TOOLS if it returns file content."
                ),
                "reason": (
                    f"error_after_compression_rate={t['error_after_compression_rate']:.1%} — "
                    f"errors occur disproportionately after this tool's output is "
                    f"compressed, suggesting error-critical content is being stripped."
                ),
            })

        if t["avg_ratio"] > _POOR_RATIO_THRESHOLD and t.get("llm_failure_rate", 0) < 0.05:
            suggestions.append({
                "priority": "low",
                "target": f"Compression path for '{name}'",
                "action": (
                    f"Consider adding '{name}' to COMMAND_TOOLS to skip LLM "
                    f"compression and use head+tail instead."
                ),
                "reason": (
                    f"avg_ratio={t['avg_ratio']:.2f} — LLM compression is barely "
                    f"reducing output size, spending model tokens for little benefit."
                ),
            })

        if t["no_output_rate"] > _NO_OUTPUT_THRESHOLD:
            suggestions.append({
                "priority": "low",
                "target": f"Tool usage: '{name}'",
                "action": (
                    f"Review whether '{name}' is being called unnecessarily. "
                    f"Consider adding result caching at the tool level."
                ),
                "reason": (
                    f"no_output_rate={t['no_output_rate']:.1%} — this tool is "
                    f"returning empty or confirmation-only results most of the time."
                ),
            })

        if t["llm_failure_rate"] > _LLM_FAILURE_THRESHOLD:
            suggestions.append({
                "priority": "medium",
                "target": f"COMMAND_TOOLS (add '{name}')",
                "action": (
                    f"Add '{name}' to COMMAND_TOOLS in compressor.py to use "
                    f"head+tail instead of the failing LLM path."
                ),
                "reason": (
                    f"llm_failure_rate={t['llm_failure_rate']:.1%} — the LLM "
                    f"compressor is failing frequently for this tool, falling back "
                    f"to head+tail anyway at extra latency cost."
                ),
            })

    # Sort by priority
    _priority_order = {"high": 0, "medium": 1, "low": 2}
    suggestions.sort(key=lambda s: _priority_order.get(s["priority"], 3))
    return suggestions


# ── Session registry ──────────────────────────────────────────────────────────

_monitors: dict[str, SessionMonitor] = {}
_monitors_lock = threading.Lock()


def get_session_monitor(session_id: str) -> SessionMonitor:
    """Return the SessionMonitor for *session_id*, creating it if needed."""
    with _monitors_lock:
        if session_id not in _monitors:
            _monitors[session_id] = SessionMonitor(session_id)
        return _monitors[session_id]


def flush_session_monitor(session_id: str, db) -> None:
    """Merge session monitor data into global stats and persist.

    Call this at the end of a session or when switching sessions.
    After flushing, the session monitor is removed from memory to prevent stale data.

    Parameters
    ----------
    db: the nbchat.core.db module (passed to avoid circular imports)
    """
    with _monitors_lock:
        monitor = _monitors.get(session_id)
    if monitor is None:
        return
    try:
        raw = db.load_global_monitoring_stats()
        global_stats = raw if raw else _empty_global()
        merged = merge_into_global(global_stats, monitor.to_mergeable())
        db.save_global_monitoring_stats(merged)
        _log.debug(
            f"flush_session_monitor: merged session {session_id} into global stats "
            f"({merged['sessions_seen']} sessions total)"
        )
        # Remove the session monitor from memory after flushing
        _monitors.pop(session_id, None)
    except Exception as exc:
        _log.debug(f"flush_session_monitor failed: {exc}")


def format_report(report: dict) -> str:
    """Return a human-readable summary of a session or global report."""
    lines: list[str] = []
    c = report.get("cache", {})

    if "sessions_seen" in c:
        lines.append(f"Global stats — {c['sessions_seen']} sessions, {c['turn_count']} turns")
    else:
        lines.append(f"Session: {report.get('session_id','?')} — {c.get('turn_count',0)} turns")

    lines.append(
        f"  Cache: avg_sim={c.get('avg_sim_best',0):.3f}  "
        f"low_sim_rate={c.get('low_sim_rate',0):.1%}  "
        f"invalidation_rate={c.get('cache_invalidation_rate',0):.1%}  "
        f"avg_volatile_delta={c.get('avg_volatile_delta',0)} chars"
    )

    for name, t in report.get("tools", {}).items():
        lines.append(
            f"  {name}: calls={t['calls']}  ratio={t['avg_ratio']:.2f}  "
            f"reread={t['reread_rate']:.1%}  err_after_comp={t['error_after_compression_rate']:.1%}"
        )

    for w in report.get("warnings", []):
        lines.append(f"  ⚠  {w}")

    for s in report.get("suggestions", []):
        lines.append(
            f"  [{s['priority'].upper()}] {s['target']}: {s['action']}\n"
            f"    reason: {s['reason']}"
        )

    return "\n".join(lines)


def format_monitoring_html(
    session_report: dict,
    global_report: Optional[dict],
    code_color: str = "#006400",
) -> str:
    """Render monitoring data as compact HTML for the sidebar widget.

    Parameters
    ----------
    session_report:
        Output of SessionMonitor.get_session_report().
    global_report:
        Output of get_global_report(), or None if no cross-session data yet.
    code_color:
        Hex color for code/metric values — should match CODE_COLOR in styles.py.

    Layout
    ------
    - Session cache metrics (open by default)
    - Per-tool rows inside a nested collapsible
    - Warnings always visible (not collapsed) — prominent orange text
    - Global stats + suggestions (collapsed by default)
    """
    def _code(val: str) -> str:
        return f'<code style="color:{code_color};font-size:0.9em;">{val}</code>'

    def _pct(v: float) -> str:
        return _code(f"{v:.1%}")

    def _f(v: float, digits: int = 3) -> str:
        return _code(f"{v:.{digits}f}")

    parts: list[str] = ['<div style="font-size:0.82em;line-height:1.5;">']

    # ── Session block ─────────────────────────────────────────────────────
    c = session_report.get("cache", {})
    sid = session_report.get("session_id", "")[:8]
    turns = c.get("turn_count", 0)
    tools_data = session_report.get("tools", {})

    parts.append(
        f'<details open style="margin:0;">'
        f'<summary style="cursor:pointer;font-weight:bold;">'
        f'Session <span style="color:gray;font-weight:normal;">{sid}</span>'
        f' — {turns} turn{"s" if turns != 1 else ""}'
        f'</summary>'
    )

    if turns > 0:
        parts.append(
            f'<div style="padding-left:4px;">'
            f'sim {_f(c.get("avg_sim_best", 0.0))} &nbsp;'
            f'hit {_pct(1.0 - c.get("low_sim_rate", 0.0))} &nbsp;'
            f'inv {_pct(c.get("cache_invalidation_rate", 0.0))}<br>'
            f'Δvol {_code(str(c.get("avg_volatile_delta", 0)))} chars'
            f'</div>'
        )
    else:
        parts.append('<div style="color:gray;padding-left:4px;">no turns yet</div>')

    if tools_data:
        parts.append(
            '<details style="margin-left:4px;margin-top:2px;">'
            '<summary style="cursor:pointer;">Tools</summary>'
            '<div style="padding-left:4px;">'
        )
        for name, t in tools_data.items():
            ratio_color = (
                "#cc4400" if t.get("reread_rate", 0) > _REREAD_RATE_THRESHOLD
                else code_color
            )
            parts.append(
                f'<b>{name}</b> '
                f'×{t["calls"]} '
                f'ratio=<code style="color:{ratio_color};font-size:0.9em;">'
                f'{t["avg_ratio"]:.2f}</code> '
                f'rrd={_pct(t["reread_rate"])}<br>'
            )
        parts.append('</div></details>')

    parts.append('</details>')  # end session block

    # ── Warnings — always visible ─────────────────────────────────────────
    warnings = session_report.get("warnings", [])
    if warnings:
        parts.append('<div style="margin-top:4px;">')
        for w in warnings:
            # Truncate long warning text for sidebar — full text in tooltip
            short = w[:80] + ("…" if len(w) > 80 else "")
            parts.append(
                f'<div style="color:#cc6600;" title="{w}">'
                f'⚠ {short}</div>'
            )
        parts.append('</div>')

    # ── Global block — collapsed by default ───────────────────────────────
    if global_report:
        gc = global_report.get("cache", {})
        n_sessions = gc.get("sessions_seen", 0)
        n_turns = gc.get("turn_count", 0)
        suggestions = global_report.get("suggestions", [])

        parts.append(
            f'<details style="margin-top:4px;">'
            f'<summary style="cursor:pointer;font-weight:bold;">'
            f'Global — {n_sessions} session{"s" if n_sessions != 1 else ""}, '
            f'{n_turns} turn{"s" if n_turns != 1 else ""}'
            f'</summary>'
            f'<div style="padding-left:4px;">'
            f'sim {_f(gc.get("avg_sim_best", 0.0))} &nbsp;'
            f'inv {_pct(gc.get("cache_invalidation_rate", 0.0))}<br>'
        )

        if suggestions:
            _priority_colors = {"high": "#cc0000", "medium": "#cc6600", "low": "#666666"}
            for s in suggestions:
                col = _priority_colors.get(s["priority"], "#333")
                short_action = s["action"][:70] + ("…" if len(s["action"]) > 70 else "")
                parts.append(
                    f'<div style="margin-top:2px;" title="{s["reason"]}">'
                    f'<span style="color:{col};font-weight:bold;">'
                    f'[{s["priority"].upper()}]</span> '
                    f'<b>{s["target"]}</b><br>'
                    f'<span style="color:#444;">{short_action}</span>'
                    f'</div>'
                )
        else:
            parts.append('<div style="color:gray;">No suggestions.</div>')

        parts.append('</div></details>')

    parts.append('</div>')
    return "".join(parts)


__all__ = [
    "SessionMonitor",
    "get_session_monitor",
    "flush_session_monitor",
    "parse_last_completion_metrics",
    "get_global_report",
    "merge_into_global",
    "suggest_config",
    "format_report",
    "format_monitoring_html",
]