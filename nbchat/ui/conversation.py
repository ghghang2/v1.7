"""Conversation loop mixin — agentic tool-calling loop and streaming.

Output is decoupled via five hooks (no-op by default, overridden by ChatUI):
  _on_stream_token(content)         — each streamed chunk of assistant text
  _on_stream_reasoning(reasoning)   — each streamed chunk of reasoning/thinking
  _on_tool_display(raw, name, args) — after each tool execution
  _on_agent_message(text)           — warnings / error notices
  _on_stream_complete(content, tcs) — once streaming finishes

ChatUI overrides all five. WhatsAppAgent inherits the no-ops.
"""
from __future__ import annotations

import json
import logging

import nbchat.core.db as db
import nbchat.core.config as config
import nbchat.core.compressor as comp
import nbchat.core.monitoring as mon
from nbchat.core.client import get_client
from nbchat.core.db import is_error_content
from nbchat.ui import chat_builder, tool_executor as executor
import nbchat.tools as tools_mod

_log = logging.getLogger("nbchat.compaction")


def _normalise_args(args_str: str) -> str:
    try:
        return json.dumps(json.loads(args_str), sort_keys=True)
    except Exception:
        return args_str


class ConversationMixin:
    """Mixed into ChatUI and headless channel agents.

    Required on host: history, task_log, system_prompt, model_name,
    session_id, _stop_event, _tool_running, _history_lock,
    _hard_trim, _log_action, _window, MAX_TOOL_TURNS.
    """

    # ── Output hooks — override in subclasses ─────────────────────────────

    def _on_stream_token(self, content: str) -> None: pass
    def _on_stream_reasoning(self, reasoning: str) -> None: pass
    def _on_tool_display(self, raw_result: str, tool_name: str, tool_args: str) -> None: pass
    def _on_agent_message(self, text: str) -> None: pass
    def _on_stream_complete(self, content: str, tool_calls: list | None) -> None: pass
    def _append(self, widget) -> None: pass
    def _refresh_monitoring_panel(self) -> None: pass
    def drain_interjections(self) -> list:
        """Return pending supervisor interjections (empty by default).

        Agents wired to a supervisor override this to drain their
        interjection queue.  The conversation loop calls it at the top of
        each tool-turn (a safe point) and injects the results as user
        messages.
        """
        return []

    # ── Entry point ───────────────────────────────────────────────────────

    def _process_conversation_turn(self) -> None:
        try:
            self._run_conversation_loop(get_client())
        except Exception as exc:
            msg = f"Conversation loop stopped unexpectedly: {type(exc).__name__}: {exc}"
            _log.debug(msg, exc_info=True)
            mon.flush_session_monitor(self.session_id, db)
            self._on_agent_message(msg)

    def _run_conversation_loop(self, client) -> None:
        # L1: update goal from latest user message
        try:
            last_user = next((r[1] for r in reversed(self.history) if r[0] == "user"), None)
            if last_user:
                self._update_l1_goal_from_user(last_user)
        except Exception as exc:
            _log.debug("L1 goal update failed: %s", exc)

        window, _cut = self._window()
        messages = chat_builder.build_messages(window, self.system_prompt, self.task_log)
        for msg in messages:
            msg.pop("reasoning_content", None)

        monitor = mon.get_session_monitor(self.session_id)
        STALL_TURNS = config.STALL_TURNS
        _recent_call_sets: list = []

        for turn in range(self.MAX_TOOL_TURNS + 1):
            if self._stop_event.is_set():
                mon.flush_session_monitor(self.session_id, db)
                break

            # ── Drain supervisor interjections (safe point) ──────────
            # The supervisor may have queued corrective instructions while
            # we were streaming / running tools.  We inject them here — at
            # the top of a turn, before the next LLM call — so we never
            # mutate the message list mid-stream.
            _interjections = self.drain_interjections()
            for _ij in _interjections:
                _log.info("injecting supervisor interjection: %.120s", _ij)
                self._on_agent_message(f"[supervisor] {_ij}")
                with self._history_lock:
                    self.history.append(("user", _ij, "", "", "", 0))
                db.log_message(self.session_id, "user", _ij)
                messages.append({"role": "user", "content": _ij})

            volatile_len = (
                len(messages[1]["content"])
                if len(messages) > 2 and messages[1].get("role") == "user" else 0
            )
            reasoning, content, tool_calls, finish_reason = self._stream_response(client, messages)

            try:
                monitor.record_llm_call(volatile_len)
            except Exception:
                pass

            if reasoning:
                with self._history_lock:
                    self.history.append(("analysis", reasoning, "", "", "", 0))
                db.log_message(self.session_id, "analysis", reasoning)

            if not tool_calls or finish_reason != "tool_calls":
                # Truncation guard: if the model ran out of output tokens
                # mid-reply (finish_reason == "length" or content ends with
                # a colon/ellipsis that suggests an unfinished sentence),
                # do NOT treat it as a final answer.  Log the event and
                # inject a nudge so the model continues.
                _tail = (content or "").rstrip()
                # A reply that ends on an "incomplete" marker was almost
                # certainly cut off mid-sentence (or mid-tool-call).  Heuristics
                # below catch the common cases without tripping on real answers.
                _ends_unfinished = bool(_tail) and _tail.endswith((
                    ":", "\u2026", "...", " (", " [", " —",
                    ", and", ", the", ", that", ", it",
                    " then", " now", " let", " i will",
                ))
                _truncated = (finish_reason == "length") or _ends_unfinished
                if _truncated and turn < self.MAX_TOOL_TURNS:
                    _log.warning(
                        "Possible truncation detected (finish_reason=%s, content_tail=%r). "
                        "Injecting continue nudge instead of stopping.",
                        finish_reason, (content or "")[-80:],
                    )
                    nudge = (
                        "Your previous reply appears to have been cut off mid-sentence "
                        "(likely due to the output token limit). Please continue from "
                        "exactly where you left off. Do NOT repeat the content you already "
                        "output. If you were about to call a tool, call it now."
                    )
                    self._on_agent_message(nudge)
                    with self._history_lock:
                        self.history.append(("assistant", content or "", "", "", "", 0))
                    db.log_message(self.session_id, "assistant", content or "")
                    with self._history_lock:
                        self.history.append(("user", nudge, "", "", "", 0))
                    db.log_message(self.session_id, "user", nudge)
                    messages.append({"role": "assistant", "content": content or None})
                    messages.append({"role": "user", "content": nudge})
                    continue  # next turn of the loop

                if content:
                    with self._history_lock:
                        self.history.append(("assistant", content, "", "", "", 0))
                    db.log_message(self.session_id, "assistant", content)
                mon.flush_session_monitor(self.session_id, db)
                self._refresh_monitoring_panel()
                break

            if turn == self.MAX_TOOL_TURNS:
                warning = f"Maximum tool turns ({self.MAX_TOOL_TURNS}) reached."
                self._on_agent_message(warning)
                with self._history_lock:
                    self.history.append(("assistant", warning, "", "", "", 0))
                db.log_message(self.session_id, "assistant", warning)
                mon.flush_session_monitor(self.session_id, db)
                break

            # Stall detection
            turn_calls = frozenset(
                (tc["function"]["name"], _normalise_args(tc["function"]["arguments"]))
                for tc in tool_calls
            )
            _recent_call_sets.append(turn_calls)
            if len(_recent_call_sets) > STALL_TURNS:
                _recent_call_sets.pop(0)
            if len(_recent_call_sets) == STALL_TURNS and len(set(_recent_call_sets)) == 1:
                stall_msg = (
                    f"You appear to be stuck in a loop — same tool calls {STALL_TURNS} turns in a row. "
                    "Review the task log, identify what has already been done, and take a concrete next step."
                )
                _log.debug("stall detected — injecting interrupt")
                with self._history_lock:
                    self.history.append(("user", stall_msg, "", "", "", 0))
                db.log_message(self.session_id, "user", stall_msg)
                messages.append({"role": "user", "content": stall_msg})
                _recent_call_sets.clear()

            msg_for_model = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
            messages.append(msg_for_model)
            full_msg_json = json.dumps(msg_for_model)
            with self._history_lock:
                self.history.append(("assistant_full", "", "full", "full", full_msg_json, 0))
            db.log_row(self.session_id, "assistant_full", "", "full", "full", full_msg_json)

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                tool_args = tc["function"]["arguments"]

                self._tool_running = True
                try:
                    raw_result = executor.run_tool(tool_name, tool_args)
                finally:
                    self._tool_running = False

                self._on_tool_display(raw_result, tool_name, tool_args)

                compressed = comp.compress_tool_output(
                    tool_name, tool_args, raw_result,
                    model=self.model_name, client=client, session_id=self.session_id,
                )
                model_result = (
                    f"[{tool_name}: no relevant output]"
                    if compressed.strip() == "NO_RELEVANT_OUTPUT" else compressed
                )

                self._log_action(tool_name, tool_args, model_result)
                error_flag = int(is_error_content(raw_result))

                with self._history_lock:
                    self.history.append(("tool", raw_result, tc["id"], tool_name, tool_args, error_flag))
                db.log_tool_msg(self.session_id, tc["id"], tool_name, tool_args, raw_result)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": model_result})

                # L1 + L2 update
                try:
                    importance = self._importance_score(
                        [msg_for_model, {"role": "tool", "content": model_result}],
                        raw_result=raw_result,
                    )
                    self._write_exchange_to_episodic(turn, tool_name, tool_args, model_result, importance)
                    self._update_l1_from_exchange(tool_name, tool_args, model_result)
                except Exception as exc:
                    _log.debug("L1/L2 post-tool update failed: %s", exc)

                # Monitoring
                try:
                    comp_stats = comp.get_compression_stats().get(tool_name, {})
                    last_strategy = next(iter(comp_stats.get("strategies", {})), "")
                    monitor.record_tool_call(
                        tool_name=tool_name,
                        was_compressed=(compressed != raw_result),
                        had_error=bool(error_flag),
                        strategy=last_strategy,
                        input_chars=len(raw_result),
                        output_chars=len(compressed),
                    )
                    if compressed.strip() == "NO_RELEVANT_OUTPUT":
                        monitor.record_no_output(tool_name)
                except Exception:
                    pass

                self._refresh_monitoring_panel()

    # ── Streaming ─────────────────────────────────────────────────────────

    def _stream_response(self, client, messages):
        """Stream one LLM completion, firing output hooks per chunk.

        Returns (reasoning_accum, content_accum, tool_calls, finish_reason).
        """
        reasoning_accum = ""
        content_accum = ""
        tool_buffer: dict = {}
        finish_reason = None

        self._hard_trim(messages)
        _sanitize_messages(messages)

        try:
            stream = client.chat.completions.create(
                model=self.model_name, messages=messages, stream=True,
                tools=tools_mod.get_tools(), max_tokens=config.MAX_LLM_OUTPUT_TOKENS,
            )
            for chunk in stream:
                if self._stop_event.is_set():
                    stream.close()
                    break
                # With stream_options.include_usage (set by MetricsLoggingClient),
                # the final chunk carries usage and has an EMPTY choices list.
                # Indexing choices[0] there raised IndexError and killed the
                # whole agentic loop on every turn.
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        entry = tool_buffer.setdefault(tc.index, {
                            "id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": ""},
                        })
                        if tc.function.arguments:
                            entry["function"]["arguments"] += tc.function.arguments
                if getattr(delta, "reasoning_content", None):
                    reasoning_accum += delta.reasoning_content
                    self._on_stream_reasoning(reasoning_accum)
                if delta.content:
                    content_accum += delta.content
                    self._on_stream_token(content_accum)
        except Exception as exc:
            if "now finding less tool calls" in str(exc):
                _log.warning("SDK diff error: tool_buffer=%s finish=%s", json.dumps(tool_buffer), finish_reason)
            raise

        tool_calls = [tool_buffer[i] for i in sorted(tool_buffer)] if tool_buffer else None
        self._on_stream_complete(content_accum, tool_calls)
        return reasoning_accum, content_accum, tool_calls, finish_reason


def _sanitize_messages(messages: list) -> None:
    """Ensure assistant messages with tool_calls have content=None, not content=""."""
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content"):
            m["content"] = None