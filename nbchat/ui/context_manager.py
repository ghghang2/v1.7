"""Context management for ChatUI.

Keeps messages within the model's context limit via:
  1. Token-budget sliding window  — walks back through history until CONTEXT_BUDGET is reached
  2. L1 Core Memory               — typed persistent slots injected as a system block each call
  3. L2 Episodic store            — importance-scored tool exchanges persisted across context boundaries
  4. Structured prior summaries   — evicted turns summarised async in GOAL/ENTITIES/RATIONALE format
  5. Hard trim                    — least-important exchanges dropped with optional L2 persistence
"""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import nbchat.core.db as db
import nbchat.core.config as config
from nbchat.ui import chat_builder

_log = logging.getLogger("nbchat.compaction")

_Row = Tuple[str, str, str, str, str, int]

L2_RETRIEVAL_LIMIT = config.L2_RETRIEVAL_LIMIT
CORE_MEMORY_ACTIVE_ENTITIES_LIMIT = config.CORE_MEMORY_ACTIVE_ENTITIES_LIMIT
CORE_MEMORY_ERROR_HISTORY_LIMIT = config.CORE_MEMORY_ERROR_HISTORY_LIMIT
_SUMMARIZER_TOOL_CHARS = config.SUMMARIZER_TOOL_CHARS
_PREFIX_TOKEN_RESERVE = getattr(config, "PREFIX_TOKEN_RESERVE", 2000)

_CORRECTION_KEYWORDS = (
    "actually", "wait,", "no,", "wrong", "instead", "correct",
    "not that", "stop,", "that's not", "don't do", "undo",
)
_STRUCTURED_SUMMARY_PROMPT = (
    "Analyse this conversation segment and output EXACTLY three labelled lines.\n"
    "GOAL: <one sentence — what the user was trying to accomplish>\n"
    "ENTITIES: <pipe-separated entity state changes, e.g. 'file:report.py created | api:/users → 404'. "
    "Use 'none' if none.>\n"
    "RATIONALE: <one sentence — key action and whether it achieved the expected outcome>\n"
    "Be factual. Output exactly three lines with labels GOAL:, ENTITIES:, RATIONALE: — no preamble."
)

_summarizer_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="nbchat-summarizer")


# ---------------------------------------------------------------------------
# Adaptive importance tracker
# ---------------------------------------------------------------------------

class ImportanceTracker:
    """Rolling percentile tracker for importance scores; derives L2 write/retrieval thresholds."""

    _COLD_WRITE = 2.5
    _COLD_RETRIEVAL = 3.0
    _COLD_MIN = 10

    def __init__(self, persist_fraction: float = 0.40, window: int = 200):
        self._sorted: List[float] = []
        self._fifo: List[float] = []
        self._maxlen = window
        self._persist_fraction = persist_fraction

    def record(self, score: float) -> None:
        if len(self._fifo) >= self._maxlen:
            oldest = self._fifo.pop(0)
            idx = bisect.bisect_left(self._sorted, oldest)
            if idx < len(self._sorted) and self._sorted[idx] == oldest:
                self._sorted.pop(idx)
        bisect.insort(self._sorted, score)
        self._fifo.append(score)

    def _q(self, q: float) -> float:
        n = len(self._sorted)
        return self._sorted[max(0, min(n - 1, int(q * n)))] if n else self._COLD_WRITE

    @property
    def write_threshold(self) -> float:
        if len(self._sorted) < self._COLD_MIN:
            return self._COLD_WRITE
        return max(self._COLD_WRITE, self._q(1.0 - self._persist_fraction))

    @property
    def retrieval_threshold(self) -> float:
        if len(self._sorted) < self._COLD_MIN:
            return self._COLD_RETRIEVAL
        return self._q(0.70)

    def state_dict(self) -> dict:
        return {
            "n": len(self._sorted),
            "write_threshold": round(self.write_threshold, 2),
            "retrieval_threshold": round(self.retrieval_threshold, 2),
            "min": round(self._sorted[0], 2) if self._sorted else None,
            "max": round(self._sorted[-1], 2) if self._sorted else None,
        }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _est_tokens(row: _Row) -> int:
    role, content, _, _, tool_args, _ = row
    chars = len(content or "") + len(tool_args or "")
    return max(1, int(chars / (2.5 if role == "tool" else 4.0)))


def _parse_structured_summary(text: str) -> dict:
    result = {"goal": "", "entities": [], "rationale": ""}
    for line in text.strip().splitlines():
        if line.startswith("GOAL:"):
            result["goal"] = line[5:].strip()
        elif line.startswith("ENTITIES:"):
            raw = line[9:].strip()
            if raw.lower() != "none":
                result["entities"] = [e.strip() for e in raw.split("|") if e.strip()]
        elif line.startswith("RATIONALE:"):
            result["rationale"] = line[10:].strip()
    return result


def _extract_entities(text: str) -> List[str]:
    entities: List[str] = []
    for m in re.finditer(
        r'\b[\w\-./]+\.(?:py|js|ts|jsx|tsx|json|yaml|yml|txt|md|html|'
        r'css|sh|env|cfg|ini|toml|sql|csv|lock|log)\b', text
    ):
        entities.append(m.group())
    for m in re.finditer(r'(?<!\w)/[a-z][a-z0-9_/\-]{2,40}', text):
        entities.append("api:" + m.group()[:50])
    for m in re.finditer(r'https?://([^/\s"\']{4,60})', text):
        entities.append("url:" + m.group(1))
    seen: set = set()
    result = []
    for e in entities:
        if e not in seen:
            seen.add(e)
            result.append(e)
        if len(result) >= 10:
            break
    return result


def _group_by_user_turn(rows: List[_Row]) -> List[List[_Row]]:
    units: List[List[_Row]] = []
    current: List[_Row] = []
    for row in rows:
        if row[0] == "user" and current:
            units.append(current)
            current = [row]
        else:
            current.append(row)
    if current:
        units.append(current)
    return units


# ---------------------------------------------------------------------------
# ContextMixin
# ---------------------------------------------------------------------------

class ContextMixin:
    """Mixed into ChatUI. Requires: history, task_log, system_prompt, model_name,
    session_id, _turn_summary_cache, _summary_futures, _importance_tracker."""

    def _log_context_event(self, event_type: str, payload: dict) -> None:
        try:
            db.log_context_event(self.session_id, event_type, payload)
        except Exception as exc:
            _log.debug("_log_context_event(%s) failed: %s", event_type, exc)

    # ── Importance scoring ────────────────────────────────────────────────────

    @staticmethod
    def _importance_score(exchange_msgs: list, raw_result: str = "") -> float:
        score = 1.0
        if any(k in raw_result.lower() for k in ("error", "exception", "failed", "cannot", "traceback")):
            score += 3.0
        for msg in exchange_msgs:
            content = (msg.get("content") or "").lower()
            role = msg.get("role", "")
            if role == "tool":
                score += 1.0
                if any(k in content for k in ("error", "exception", "failed", "cannot", "traceback")):
                    score += 1.5
                if any(k in content for k in ("success", "completed", "done", "created", "written")):
                    score += 1.5
                if len(content) > 500:
                    score += 0.5
            if role == "user" and any(k in content for k in ("correct", "wrong", "actually", "instead", "don't")):
                score += 2.5
        return min(score, 10.0)

    # ── L1 Core Memory ────────────────────────────────────────────────────────

    def _get_l1_block(self) -> Optional[str]:
        try:
            cm = db.get_core_memory(self.session_id)
        except Exception:
            return None
        if not cm:
            return None
        parts = []
        if cm.get("goal"):
            parts.append(f"Goal: {cm['goal']}")
        if cm.get("constraints"):
            try:
                c = json.loads(cm["constraints"])
                if c:
                    parts.append(f"Constraints: {'; '.join(c)}")
            except Exception:
                if cm["constraints"]:
                    parts.append(f"Constraints: {cm['constraints']}")
        if cm.get("active_entities"):
            try:
                e = json.loads(cm["active_entities"])
                if e:
                    parts.append(f"Active entities: {', '.join(e[:15])}")
            except Exception:
                pass
        if cm.get("error_history"):
            try:
                eh = json.loads(cm["error_history"])
                if eh:
                    parts.append(f"Recent errors: {' | '.join(eh[-3:])}")
            except Exception:
                pass
        if cm.get("last_correction"):
            parts.append(f"Last user correction: {cm['last_correction']}")
        if not parts:
            return None
        return "[CORE MEMORY — persistent task state]\n" + "\n".join(parts) + "\n[END CORE MEMORY]"

    def _update_l1_goal_from_user(self, user_message: str) -> None:
        try:
            lower = user_message.lower()
            updates: dict = {}
            # The goal tracks the user's CURRENT task.  It is updated on
            # every substantive user message so the supervisor always reviews
            # against the right objective.  Very short messages (acknowledg-
            # ments like "yes", "sure", "pick it back up") and correction-
            # keyword messages do NOT update the goal — they are continuations
            # of the current task, not new tasks.
            is_short = len(user_message.strip()) < 20
            is_correction = any(
                lower.startswith(kw) or f" {kw} " in lower or lower.endswith(f" {kw}")
                for kw in _CORRECTION_KEYWORDS
            )
            if not is_short and not is_correction:
                updates["goal"] = user_message[:300]
            if any(
                lower.startswith(kw) or f" {kw} " in lower or lower.endswith(f" {kw}")
                for kw in _CORRECTION_KEYWORDS
            ):
                updates["last_correction"] = user_message[:300]
            if updates:
                db.update_core_memory(self.session_id, updates)
        except Exception as exc:
            _log.debug("_update_l1_goal_from_user failed: %s", exc)

    def _update_l1_from_exchange(self, tool_name: str, tool_args: str, result: str) -> None:
        try:
            cm = db.get_core_memory(self.session_id) or {}
            new_entities = _extract_entities(tool_args + " " + result)
            try:
                existing = json.loads(cm.get("active_entities", "[]"))
            except Exception:
                existing = []
            merged = list(dict.fromkeys(new_entities + existing))[:CORE_MEMORY_ACTIVE_ENTITIES_LIMIT]
            updates = {"active_entities": json.dumps(merged)}
            if any(k in result.lower() for k in ("error", "exception", "failed", "cannot", "traceback")):
                try:
                    errors = json.loads(cm.get("error_history", "[]"))
                except Exception:
                    errors = []
                errors.append(result.split("\n")[0][:120])
                updates["error_history"] = json.dumps(errors[-CORE_MEMORY_ERROR_HISTORY_LIMIT:])
            db.update_core_memory(self.session_id, updates)
        except Exception as exc:
            _log.debug("_update_l1_from_exchange failed: %s", exc)

    # ── L2 Episodic store ─────────────────────────────────────────────────────

    def _get_l2_block(self, active_entities: List[str]) -> Optional[str]:
        try:
            entries: List[dict] = []
            seen_ids: set = set()
            if active_entities:
                for e in db.query_episodic_by_entities(self.session_id, active_entities, limit=L2_RETRIEVAL_LIMIT):
                    seen_ids.add(e["id"])
                    entries.append(e)
            if len(entries) < L2_RETRIEVAL_LIMIT:
                for e in db.query_episodic_top_importance(
                    self.session_id,
                    min_score=self._importance_tracker.retrieval_threshold,
                    limit=L2_RETRIEVAL_LIMIT + len(seen_ids),
                ):
                    if e["id"] not in seen_ids:
                        entries.append(e)
                        seen_ids.add(e["id"])
                        if len(entries) >= L2_RETRIEVAL_LIMIT:
                            break
        except Exception as exc:
            _log.debug("_get_l2_block failed: %s", exc)
            return None
        if not entries:
            return None
        lines = []
        for e in entries:
            try:
                refs = json.loads(e.get("entity_refs", "[]"))
                entity_str = f" [{', '.join(refs[:5])}]" if refs else ""
            except Exception:
                entity_str = ""
            lines.append(f"• {e['action_type']}: {e['outcome_summary']}{entity_str} (importance: {e['importance_score']:.1f})")
        return "[RELEVANT PAST EVENTS — retrieved from episodic memory]\n" + "\n".join(lines) + "\n[END EPISODIC CONTEXT]"

    def _write_exchange_to_episodic(self, turn: int, tool_name: str, tool_args: str,
                                     result: str, importance: float) -> None:
        self._importance_tracker.record(importance)
        threshold = self._importance_tracker.write_threshold
        self._log_context_event("L2_CANDIDATE", {
            "tool": tool_name, "importance": round(importance, 2),
            "threshold": round(threshold, 2), "persisted": importance >= threshold,
            "tracker": self._importance_tracker.state_dict(),
        })
        if importance < threshold:
            return
        try:
            db.append_episodic(
                session_id=self.session_id, turn_id=turn, action_type=tool_name,
                entity_refs=json.dumps(_extract_entities(tool_args + " " + result)),
                outcome_summary=result.split("\n")[0][:200],
                importance_score=importance,
            )
        except Exception as exc:
            _log.debug("_write_exchange_to_episodic failed: %s", exc)

    # ── Sliding window ────────────────────────────────────────────────────────

    def _window(self) -> Tuple[List[_Row], int]:
        """Return (window_rows, effective_cut) using token-budget walkback."""
        budget = int(config.CONTEXT_BUDGET * getattr(config, "CONTEXT_HEADROOM", 0.82)) - _PREFIX_TOKEN_RESERVE
        tokens = 0
        cut = 0
        for i in range(len(self.history) - 1, -1, -1):
            row = self.history[i]
            if row[0] == "analysis":
                continue
            if tokens + _est_tokens(row) > budget:
                j = i + 1
                while j < len(self.history) and self.history[j][0] != "user":
                    j += 1
                cut = j
                break
            tokens += _est_tokens(row)

        last_user = next((i for i in range(len(self.history) - 1, -1, -1) if self.history[i][0] == "user"), 0)
        cut = min(cut, last_user)
        window = list(self.history[cut:])
        effective_cut = cut

        prefix: List[_Row] = []
        if l1 := self._get_l1_block():
            prefix.append(("system", l1, "", "", "", 0))
        try:
            cm = db.get_core_memory(self.session_id) or {}
            active_entities = json.loads(cm.get("active_entities", "[]"))
        except Exception:
            active_entities = []
        if l2 := self._get_l2_block(active_entities):
            prefix.append(("system", l2, "", "", "", 0))
        if effective_cut > 0:
            if prior := self._build_prior_context(self.history[:effective_cut]):
                prefix.append(("system", prior, "", "", "", 0))
            self._prefetch_summaries(self.history[:effective_cut])

        self._log_context_event("WINDOW_COMPUTED", {
            "history_len": len(self.history), "effective_cut": effective_cut,
            "window_rows": len(window), "estimated_tokens": tokens, "budget": budget,
            "prefix_blocks": len(prefix), "tracker": self._importance_tracker.state_dict(),
        })
        return prefix + window, effective_cut

    # ── Summarisation ─────────────────────────────────────────────────────────

    def _prefetch_summaries(self, prior_rows: List[_Row]) -> None:
        for unit in _group_by_user_turn(prior_rows):
            key = hashlib.sha1("".join(r[1] + r[4] for r in unit).encode()).hexdigest()
            if key not in self._turn_summary_cache and key not in self._summary_futures:
                self._summary_futures[key] = _summarizer_executor.submit(self._call_summarizer, unit)

    def _build_prior_context(self, prior_rows: List[_Row]) -> Optional[str]:
        units = _group_by_user_turn(prior_rows)
        if not units:
            return None
        lines = []
        for i, unit in enumerate(units, 1):
            user_row = next((r for r in unit if r[0] == "user"), None)
            user_text = user_row[1][:200] if user_row else "(no user message)"
            parsed = _parse_structured_summary(self._get_turn_summary(unit))
            line = f'Turn {i} — User: "{user_text}"'
            if parsed["goal"]:
                line += f"\n  Goal: {parsed['goal']}"
            if parsed["entities"]:
                line += f"\n  Entities: {' | '.join(parsed['entities'])}"
            if parsed["rationale"]:
                line += f"\n  Outcome: {parsed['rationale']}"
            lines.append(line)
        return "[PRIOR SESSION CONTEXT — earlier turns summarized]\n" + "\n".join(lines) + "\n[END PRIOR CONTEXT]"

    def _get_turn_summary(self, unit: List[_Row]) -> str:
        key = hashlib.sha1("".join(r[1] + r[4] for r in unit).encode()).hexdigest()
        if key in self._turn_summary_cache:
            return self._turn_summary_cache[key]
        future = self._summary_futures.get(key)
        if future is not None:
            if future.done():
                try:
                    summary = future.result()
                except Exception as exc:
                    _log.debug("summary future %s failed: %s", key[:8], exc)
                    summary = self._fallback_summary(unit)
                self._turn_summary_cache[key] = summary
                del self._summary_futures[key]
                self._persist_summary_cache()
                self._log_context_event("SUMMARY_GENERATED", {"key": key[:8], "length": len(summary), "source": "async_future"})
                return summary
            return self._fallback_summary(unit)
        self._summary_futures[key] = _summarizer_executor.submit(self._call_summarizer, unit)
        return self._fallback_summary(unit)

    def _fallback_summary(self, unit: List[_Row]) -> str:
        user_text = next((r[1][:100] for r in unit if r[0] == "user"), "")
        tool_hint = next((r[1].split("\n")[0][:80] for r in unit if r[0] == "tool"), "")
        return f"GOAL: (summary pending) {user_text}\nENTITIES: none\nRATIONALE: {tool_hint}"

    def _persist_summary_cache(self) -> None:
        try:
            db.save_turn_summaries(self.session_id, self._turn_summary_cache)
        except Exception:
            pass

    def _call_summarizer(self, unit: List[_Row]) -> str:
        """Runs in thread pool — no widget access."""
        from nbchat.core.client import get_client
        trimmed = [
            (role, content[:_SUMMARIZER_TOOL_CHARS] if role == "tool" else content, tid, tname, targs, ef)
            for role, content, tid, tname, targs, ef in unit
        ]
        messages = chat_builder.build_messages(trimmed, self.system_prompt)
        for m in messages:
            m.pop("reasoning_content", None)
        if messages and messages[-1].get("role") == "assistant":
            messages[-1].pop("tool_calls", None)
            if not messages[-1].get("content"):
                messages.pop()
        messages.append({"role": "user", "content": _STRUCTURED_SUMMARY_PROMPT})
        try:
            resp = get_client().chat.completions.create(model=self.model_name, messages=messages, max_tokens=512)
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            _log.debug("summarizer failed: %s", exc)
            return self._fallback_summary(unit)

    # ── Hard trim ─────────────────────────────────────────────────────────────

    def _hard_trim(self, messages: list) -> None:
        """Drop least-important tool exchanges until messages fit the budget.

        Exchanges above the importance threshold are persisted to L2 before eviction.
        """
        limit = int(config.CONTEXT_BUDGET * getattr(config, "CONTEXT_HEADROOM", 0.82) * 0.85)
        KEEP_RECENT = getattr(config, "KEEP_RECENT_EXCHANGES", 5)

        def est(msg: dict) -> int:
            chars = len(msg.get("content") or "") + len(
                "".join(tc.get("function", {}).get("arguments", "") for tc in (msg.get("tool_calls") or []))
            )
            return max(1, int(chars / 2.5))

        def total() -> int:
            return sum(est(m) for m in messages)

        def get_exchanges() -> list:
            result, i = [], 2
            while i < len(messages):
                if messages[i].get("role") == "assistant" and messages[i].get("tool_calls"):
                    end = i + 1
                    while end < len(messages) and messages[end].get("role") == "tool":
                        end += 1
                    result.append((i, end))
                    i = end
                else:
                    i += 1
            return result

        def drop_least_important(exchanges: list) -> None:
            scored = [(self._importance_score(messages[s:e]), s, e) for s, e in exchanges]
            scored.sort(key=lambda x: x[0])
            score, s, e = scored[0]
            self._importance_tracker.record(score)
            threshold = self._importance_tracker.write_threshold
            persisted = False
            if score >= threshold:
                tc_list = messages[s].get("tool_calls") or []
                action = tc_list[0].get("function", {}).get("name", "") if tc_list else ""
                tc_args = tc_list[0].get("function", {}).get("arguments", "") if tc_list else ""
                for j in range(s + 1, e):
                    if messages[j].get("role") == "tool":
                        result_text = messages[j].get("content", "")
                        try:
                            db.append_episodic(
                                session_id=self.session_id, turn_id=0,
                                action_type=action or "unknown",
                                entity_refs=json.dumps(_extract_entities(tc_args + " " + result_text)),
                                outcome_summary=result_text.split("\n")[0][:200],
                                importance_score=score,
                            )
                            persisted = True
                        except Exception as exc:
                            _log.debug("_hard_trim L2 write failed: %s", exc)
            dropped = [messages[s + j].get("content", "")[:80] for j in range(1, e - s) if messages[s + j].get("role") == "tool"]
            if dropped and messages[0].get("role") == "system":
                messages[0]["content"] += f"\n[earlier: {' | '.join(dropped)}]"
            self._log_context_event("EXCHANGE_EVICTED", {
                "score": round(score, 2), "threshold": round(threshold, 2),
                "persisted_to_l2": persisted, "msg_range": [s, e],
            })
            del messages[s:e]

        # Pass 1: drop least-important, protect KEEP_RECENT most recent exchanges
        while total() > limit:
            exchanges = get_exchanges()
            droppable = exchanges[:-KEEP_RECENT] if len(exchanges) > KEEP_RECENT else []
            if not droppable:
                break
            drop_least_important(droppable)

        # Pass 2: last resort — truncate the largest tool result
        while total() > limit:
            tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
            if not tool_indices:
                break
            largest = max(tool_indices, key=lambda i: len(messages[i].get("content", "")))
            original = messages[largest].get("content", "")
            if len(original) <= 200:
                break
            messages[largest]["content"] = original[:200] + f"\n[...truncated {len(original) - 200} chars...]"

    # ── Task log ─────────────────────────────────────────────────────────────

    def _log_action(self, tool_name: str, tool_args: str, result: str) -> None:
        try:
            args_obj = json.loads(tool_args)
            hint = next((str(v)[:60] for v in args_obj.values() if isinstance(v, str)), tool_args[:60])
        except Exception:
            hint = tool_args[:60]
        first_line = result.split("\n")[0][:120]
        if first_line.strip() == "NO_RELEVANT_OUTPUT":
            first_line = "(no relevant output)"
        entry = f"{tool_name}({hint}) -> {first_line}"
        self.task_log.append(entry)
        if len(self.task_log) > 30:
            self.task_log = self.task_log[-30:]
        db.save_task_log(self.session_id, self.task_log)