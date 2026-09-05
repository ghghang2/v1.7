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
import re
import time
import uuid

import nbchat.core.db as db
import nbchat.core.config as config
import nbchat.core.compressor as comp
import nbchat.core.monitoring as mon
import nbchat.core.task_tracker as task_tracker
from nbchat.core.db import is_error_content, is_tool_error
from nbchat.ui import chat_builder, tool_executor as executor
import nbchat.tools as tools_mod

_log = logging.getLogger("nbchat.compaction")


def _normalise_args(args_str: str) -> str:
    try:
        return json.dumps(json.loads(args_str), sort_keys=True)
    except Exception:
        return args_str


_TOOL_BLOCK_RE = re.compile(
    r"<tool_call>\s*<function=([A-Za-z_]\w*)>\s*(.*?)</function>\s*</tool_call>",
    re.DOTALL,
)
_PARAM_RE = re.compile(
    r"<parameter=([A-Za-z_]\w*)>\s*(.*?)\s*</parameter>", re.DOTALL
)

_TOOL_OPEN_RE = re.compile(r"<tool_call>")

# How many times a single turn will retry an LLM call that died mid-stream
# on a transport error (the peer closed the connection before the message
# body was complete).  The retry is only attempted when partial content was
# already rendered: that text stays in the transcript, so the model is
# nudged to continue from the break point rather than restarting.

class MidStreamError(Exception):
    """An LLM stream died mid-call before the message body was complete.

    Carries whatever the interrupted call had already produced (partial
    content, reasoning, and any partial tool calls) so the conversation
    loop can continue from the break point — or ask the model to re-issue
    a half-streamed tool call — instead of losing the partial reply and
    killing the whole turn.
    """

    def __init__(self, cause, content: str, reasoning: str,
                 tool_calls: list | None):
        super().__init__(str(cause))
        self.cause = cause
        self.content = content or ""
        self.reasoning = reasoning or ""
        self.tool_calls = tool_calls or None

def _recover_text_tool_calls(content: str) -> list[dict] | None:
    """Parse legacy XML <tool_call> blocks out of assistant *text*.

    Some models emit tool calls as <tool_call><function=name>
    <parameter=k>v</parameter></function></tool_call> text instead of the
    structured tool_calls channel.  Returns tool-call dicts in the same
    shape as the structured channel, or None if no complete block parsed.
    """
    calls: list[dict] = []
    for m in _TOOL_BLOCK_RE.finditer(content or ""):
        name, body = m.group(1), m.group(2)
        args = {pm.group(1): pm.group(2) for pm in _PARAM_RE.finditer(body)}
        if not args:
            continue
        try:
            args_json = json.dumps(args)
        except (TypeError, ValueError):
            continue
        calls.append({
            "id": f"recovered_{uuid.uuid4().hex[:12]}",
            "type": "function",
            "function": {"name": name, "arguments": args_json},
        })
    return calls or None


def _strip_tool_blocks(content: str) -> str:
    """Remove legacy <tool_call> markup from text, keeping surrounding prose.

    A truncated trailing block (an unclosed <tool_call> at the end) is
    removed too, so a half-emitted call is never re-fed to the model.
    """
    if not content:
        return ""
    text = _TOOL_BLOCK_RE.sub(" ", content)
    text = re.sub(r"<tool_call>.*$", " ", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _strip_tool_blocks_reasoning(reasoning: str) -> str:
    """Remove legacy <tool_call> markup from a reasoning/thinking trace.

    Mirror of :func:`_strip_tool_blocks` for the thinking channel: a model that
    drifts into emitting tool calls as text markup sometimes does so inside
    ``reasoning_content`` rather than the assistant content.  Reasoning is
    display-only (``build_messages`` drops ``analysis`` rows), so there is
    nothing to recover or re-issue here — the goal is simply to keep the
    half-emitted markup out of the stored/rendered thinking trace.  A
    truncated trailing block (an unclosed <tool_call> at the end) is removed
    too, for the same reason as the content guard.
    """
    if not reasoning:
        return ""
    text = _TOOL_BLOCK_RE.sub(" ", reasoning)
    text = re.sub(r"<tool_call>.*$", " ", text, flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── Task telemetry helpers — thin, never-raising wrappers around
# nbchat.core.task_tracker so a bookkeeping failure can never disturb
# the conversation loop.  See docs/task_tracking.md for the schema.

def _tt_start(agent, user_text: str):
    try:
        return task_tracker.start_task(agent, user_text)
    except Exception:
        _log.debug("task telemetry start failed", exc_info=True)
        return None


def _tt_event(rec, event: str) -> None:
    if rec is None:
        return
    try:
        rec.record_event(event)
    except Exception:
        _log.debug("task telemetry event %s failed", event, exc_info=True)


def _tt_llm(rec, latency: float) -> None:
    if rec is None:
        return
    try:
        rec.record_llm_call(latency_s=latency)
    except Exception:
        _log.debug("task telemetry llm record failed", exc_info=True)


def _tt_tool_turn(rec) -> None:
    if rec is None:
        return
    try:
        rec.note_tool_turn()
    except Exception:
        _log.debug("task telemetry tool-turn record failed", exc_info=True)


def _tt_retry(rec, exc: MidStreamError) -> None:
    if rec is None:
        return
    try:
        rec.record_event("stream_retry")
        rec.record_stream_error()
    except Exception:
        _log.debug("task telemetry stream retry failed", exc_info=True)


def _tt_tool(rec, tool_name: str, tool_args: str) -> None:
    if rec is None:
        return
    try:
        rec.record_tool_call(tool_name, tool_args, error=False, is_tool_turn=False)
    except Exception:
        _log.debug("task telemetry tool record failed", exc_info=True)


def _tt_tool_error(rec, tool_name: str, raw_result: str) -> None:
    if rec is None:
        return
    try:
        if is_tool_error(tool_name, raw_result):
            rec.note_tool_error()
    except Exception:
        _log.debug("task telemetry tool error failed", exc_info=True)


def _tt_user_row(rec) -> None:
    """Mark an agent-injected role='user' row (nudge/stall) so it is not
    later counted as a user intervention at task finish."""
    if rec is None:
        return
    try:
        rec.note_agent_user_row()
    except Exception:
        _log.debug("task telemetry user-row record failed", exc_info=True)


def _tt_finish(rec, content: str | None = None,
               status: str | None = None) -> None:
    if rec is None:
        return
    try:
        task_tracker.finish_task(rec, final_response=content,
                                 status=status or "complete")
    except Exception:
        _log.debug("task telemetry finish failed", exc_info=True)



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
            # Resolve at call time (not import time) so tests can
            # monkeypatch nbchat.core.client.get_client per-session.
            from nbchat.core.client import get_client
            self._run_conversation_loop(get_client())
        except Exception as exc:
            _tt_finish(getattr(self, "_active_task", None), status="failed")
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
        # ── Task telemetry: open one task record per turn ──────────
        try:
            _last_user = next((r[1] for r in reversed(self.history)
                               if r[0] == "user"), None)
            _task_rec = task_tracker.start_task(self, _last_user or "")
        except Exception:
            _task_rec = None
        # Crash path (_process_conversation_turn) finishes the record via
        # this attribute; finish_task is idempotent, so a second finish
        # from a loop exit is a no-op.
        self._active_task = _task_rec
        STALL_TURNS = config.STALL_TURNS
        _recent_call_sets: list = []

        for turn in range(self.MAX_TOOL_TURNS + 1):
            if self._stop_event.is_set():
                mon.flush_session_monitor(self.session_id, db)
                _tt_finish(_task_rec, None, "interrupted")
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
            reasoning, content, tool_calls, finish_reason = (None, "", None, None)
            _stream_exc = None
            # Invariant: assistant + nudge rows are appended to self.history
            # AND the DB *inside* the attempt loop, once per partial stream.
            # If a later attempt fails before any content, the partial rows
            # from earlier attempts remain persisted and rendered -
            # intentional; do not "simplify" the append placement into the
            # loop's success path.
            for _attempt in range(config.MAX_STREAM_RETRIES + 1):
                try:
                    _tt_call_start = time.monotonic()
                    reasoning, content, tool_calls, finish_reason = self._stream_response(
                        client, messages
                    )
                    _stream_exc = None
                    break
                except MidStreamError as exc:
                    # A mid-stream transport error leaves the partial reply
                    # already rendered on screen and about to be lost: the
                    # exception used to escape the whole turn.  Route it
                    # through the same continuation path as the truncation
                    # guard below — the model continues exactly where it
                    # stopped — instead of killing the turn.  A partial
                    # tool call cannot be trusted (it may be half-written)
                    # so it is discarded; the nudge tells the model to
                    # re-issue it.  If nothing was rendered yet there is
                    # nothing to continue, so the error propagates as before.
                    _tt_retry(_task_rec, exc)
                    _stream_exc = exc
                    if not exc.content or exc.tool_calls:
                        raise exc
                    if exc.reasoning:
                        with self._history_lock:
                            self.history.append(
                                ("analysis", exc.reasoning, "", "", "", 0))
                        db.log_message(self.session_id, "analysis", exc.reasoning)
                    # Finalise the interrupted portion on screen: the hook
                    # writes the closing newline, recovers a held unclosed
                    # <voice> payload, and resets per-call render state so
                    # the continuation streams cleanly from an empty buffer.
                    self._on_stream_complete(exc.content, None)
                    self._on_agent_message(
                        f"Reply stream interrupted ({type(exc.cause).__name__}); "
                        "asking the model to continue."
                    )
                    _nudge = (
                        "Your previous reply was cut off mid-sentence because "
                        "the connection to the model dropped. Please continue "
                        "from exactly where you left off. Do NOT repeat the "
                        "content you already output. If you were about to "
                        "call a tool, call it now."
                    )
                    with self._history_lock:
                        self.history.append(("assistant", exc.content, "", "", "", 0))
                    db.log_message(self.session_id, "assistant", exc.content)
                    with self._history_lock:
                        self.history.append(("user", _nudge, "", "", "", 0))
                    db.log_message(self.session_id, "user", _nudge)
                    _tt_user_row(_task_rec)
                    messages.append({"role": "assistant", "content": exc.content})
                    messages.append({"role": "user", "content": _nudge})
                    try:
                        self._write_exchange_to_episodic(
                            turn, "", "", exc.content or "",
                            self._importance_score(
                                [{"role": "assistant", "content": exc.content}],
                                raw_result=exc.content or "",
                            ),
                        )
                        self._update_l1_from_exchange("", "", exc.content or "")
                    except Exception as _e:
                        _log.debug("L1/L2 mid-stream nudge update failed: %s", _e)
            if _stream_exc is not None:
                raise _stream_exc

            _tt_llm(_task_rec, time.monotonic() - _tt_call_start)
            if tool_calls:
                _tt_tool_turn(_task_rec)
            try:
                monitor.record_llm_call(volatile_len)
            except Exception:
                pass

            # Guard: a drifted model can emit tool calls as <tool_call> text
            # inside the reasoning trace rather than the content channel.
            # Reasoning is display-only (build_messages drops "analysis"
            # rows), so there is nothing to recover or re-issue — just keep
            # the markup out of the stored/rendered thinking trace.
            reasoning = _strip_tool_blocks_reasoning(reasoning)
            if reasoning:
                with self._history_lock:
                    self.history.append(("analysis", reasoning, "", "", "", 0))
                db.log_message(self.session_id, "analysis", reasoning)

            # ── Legacy XML tool-call recovery ───────────────────────────────
            # Some models emit tool calls as <tool_call>...</tool_call> text
            # in the content instead of the structured tool_calls channel.
            # Treated as a final answer, nothing executes — and the
            # unexecuted markup stored in history is re-fed on every
            # subsequent turn, reinforcing the drift.  Recover complete,
            # parseable blocks and route them through the normal
            # tool-execution path below; if the markup is malformed or
            # truncated, nudge the model to re-emit via the proper channel.
            # Gate on the drift signature: an OPENING <tool_call> tag.
            # A bare mention in prose (e.g. a backticked word) must not
            # trigger recovery or the re-emit nudge.
            if not tool_calls and content and _TOOL_OPEN_RE.search(content):
                recovered = _recover_text_tool_calls(content)
                if recovered:
                    _log.warning(
                        "Recovered %d tool call(s) emitted as legacy XML text; "
                        "executing via normal path.", len(recovered),
                    )
                    self._on_agent_message(
                        f"Recovered {len(recovered)} tool call(s) emitted as "
                        "text markup; executing them now."
                    )
                    _tt_event(_task_rec, "text_toolcall")
                    tool_calls = recovered
                    finish_reason = "tool_calls"
                    content = _strip_tool_blocks(content)
                elif turn < self.MAX_TOOL_TURNS:
                    _log.warning(
                        "Unparseable <tool_call> text markup in content "
                        "(tail=%r); nudging model to re-emit.",
                        (content or "")[-80:],
                    )
                    _nudge = (
                        "Your previous reply contained a tool call written as text "
                        "markup (e.g. <tool_call>), which was NOT executed. Re-issue "
                        "that tool call using the structured tool-calling mechanism, "
                        "not as text."
                    )
                    self._on_agent_message(
                        "Detected unexecuted tool call in text; asking the "
                        "model to re-emit it."
                    )
                    _clean = _strip_tool_blocks(content) or (
                        "[tool call emitted as text markup; not executed]"
                    )
                    with self._history_lock:
                        self.history.append(("assistant", _clean, "", "", "", 0))
                    db.log_message(self.session_id, "assistant", _clean)
                    with self._history_lock:
                        self.history.append(("user", _nudge, "", "", "", 0))
                    db.log_message(self.session_id, "user", _nudge)
                    _tt_user_row(_task_rec)
                    messages.append({"role": "assistant", "content": _clean})
                    messages.append({"role": "user", "content": _nudge})
                    try:
                        self._write_exchange_to_episodic(
                            turn, "", "", _nudge,
                            self._importance_score(
                                [{"role": "assistant", "content": _clean}],
                                raw_result=_clean,
                            ),
                        )
                        self._update_l1_from_exchange("", "", _clean)
                    except Exception as _e:
                        _log.debug("L1/L2 re-emit nudge update failed: %s", _e)
                    continue

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
                # An unclosed <voice> open tag (a mistyped close such as
                # ``</voice`` can never match the parser's close pattern and
                # holds the tail in the parser buffer) means the stream is
                # malformed — treat it as a truncation and nudge instead of
                # finalising a reply that ends on broken markup.
                _voice_unclosed = bool(_tail) and (
                    _tail.count("<voice>") > _tail.count("</voice>")
                )
                _truncated = (
                    (finish_reason == "length") or _ends_unfinished
                    or _voice_unclosed
                )
                if _truncated and turn < self.MAX_TOOL_TURNS:
                    _tt_event(_task_rec, "truncation")
                    _log.warning(
                        "Possible truncation detected (finish_reason=%s, "
                        "voice_unclosed=%s, content_tail=%r). "
                        "Injecting continue nudge instead of stopping.",
                        finish_reason, _voice_unclosed, (content or "")[-80:],
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
                    _tt_user_row(_task_rec)
                    messages.append({"role": "assistant", "content": content or None})
                    messages.append({"role": "user", "content": nudge})
                    try:
                        self._write_exchange_to_episodic(
                            turn, "", "", nudge,
                            self._importance_score(
                                [{"role": "assistant", "content": content or ""}],
                                raw_result=content or "",
                            ),
                        )
                        self._update_l1_from_exchange("", "", content or "")
                    except Exception as _e:
                        _log.debug("L1/L2 truncation nudge update failed: %s", _e)
                    continue  # next turn of the loop

                if content:
                    with self._history_lock:
                        self.history.append(("assistant", content, "", "", "", 0))
                    db.log_message(self.session_id, "assistant", content)
                mon.flush_session_monitor(self.session_id, db)
                self._refresh_monitoring_panel()
                _tt_finish(_task_rec, content)
                break

            if turn == self.MAX_TOOL_TURNS:
                warning = f"Maximum tool turns ({self.MAX_TOOL_TURNS}) reached."
                self._on_agent_message(warning)
                _tt_event(_task_rec, "truncation")
                with self._history_lock:
                    self.history.append(("assistant", warning, "", "", "", 0))
                db.log_message(self.session_id, "assistant", warning)
                mon.flush_session_monitor(self.session_id, db)
                _tt_finish(_task_rec, warning)
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
                _tt_event(_task_rec, "stall")
                stall_msg = (
                    f"You appear to be stuck in a loop — same tool calls {STALL_TURNS} turns in a row. "
                    "Review the task log, identify what has already been done, and take a concrete next step."
                )
                _log.debug("stall detected — injecting interrupt")
                with self._history_lock:
                    self.history.append(("user", stall_msg, "", "", "", 0))
                db.log_message(self.session_id, "user", stall_msg)
                _tt_user_row(_task_rec)
                messages.append({"role": "user", "content": stall_msg})
                _recent_call_sets.clear()

            msg_for_model = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
            messages.append(msg_for_model)
            full_msg_json = json.dumps(msg_for_model)
            if _task_rec is not None:
                _task_rec.record_context(len(full_msg_json) + len(messages[0]["content"]))
            # Keep the full message JSON in tool_args (the history readers —
            # chat_builder and chatui — parse it) AND store the assistant text
            # in the readable content column so monitoring/analysis can inspect
            # assistant output without JSON parsing (fixes the previously
            # empty assistant_full.content column).
            _af_content = content or ""
            with self._history_lock:
                self.history.append(("assistant_full", _af_content, "full", "full", full_msg_json, 0))
            db.log_row(self.session_id, "assistant_full", _af_content, "full", "full", full_msg_json)

            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                tool_args = tc["function"]["arguments"]

                self._tool_running = True
                try:
                    raw_result = executor.run_tool(tool_name, tool_args)
                finally:
                    self._tool_running = False

                self._on_tool_display(raw_result, tool_name, tool_args)
                _tt_tool(_task_rec, tool_name, tool_args)

                compressed = comp.compress_tool_output(
                    tool_name, tool_args, raw_result,
                    model=self.model_name, client=client, session_id=self.session_id,
                )
                model_result = (
                    f"[{tool_name}: no relevant output]"
                    if compressed.strip() == "NO_RELEVANT_OUTPUT" else compressed
                )

                self._log_action(tool_name, tool_args, model_result)
                _tt_tool_error(_task_rec, tool_name, raw_result)
                # Derived from the structured tool outcome (exit code / status),
                # not keyword matching — see db.is_tool_error.
                error_flag = int(is_tool_error(tool_name, raw_result))

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
        # Session reasoning effort.  Sent on every call so the loaded model
        # uses it this turn and later ones.  Falls back to the configured
        # default (DEFAULT_REASONING_EFFORT) so no session silently runs at
        # the model template default (xhigh) unless /effort none is used.
        _effort = getattr(self, "reasoning_effort", None) or config.DEFAULT_REASONING_EFFORT
        reasoning_accum = ""
        content_accum = ""
        tool_buffer: dict = {}
        finish_reason = None

        self._hard_trim(messages)
        _sanitize_messages(messages)

        try:
            _create_kwargs = dict(
                model=self.model_name, messages=messages, stream=True,
                tools=tools_mod.get_tools(), max_tokens=config.MAX_LLM_OUTPUT_TOKENS)
            if _effort:
                _create_kwargs["reasoning_effort"] = _effort
            stream = client.chat.completions.create(**_create_kwargs)
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
                    # Scrub per-chunk, not just at end of stream: a drifted
                    # model can emit tool calls as legacy <tool_call> text
                    # inside reasoning_content, and the live display would
                    # otherwise render raw XML mid-think.  Display-only
                    # guard — the same scrub runs again on the stored
                    # trace in _process_conversation_turn.
                    self._on_stream_reasoning(
                        _strip_tool_blocks_reasoning(reasoning_accum))
                if delta.content:
                    content_accum += delta.content
                    self._on_stream_token(content_accum)
        except Exception as exc:
            if "now finding less tool calls" in str(exc):
                _log.warning("SDK diff error: tool_buffer=%s finish=%s", json.dumps(tool_buffer), finish_reason)
            # A transport error mid-stream leaves partial output already
            # rendered on screen.  Surface it on the exception so the
            # conversation loop can decide whether to continue from the
            # break point (partial content) or re-run the call (nothing
            # rendered yet / partial tool call that cannot be trusted).
            raise MidStreamError(
                exc, content_accum, reasoning_accum,
                [tool_buffer[i] for i in sorted(tool_buffer)] or None,
            ) from exc

        tool_calls = [tool_buffer[i] for i in sorted(tool_buffer)] if tool_buffer else None
        self._on_stream_complete(content_accum, tool_calls)
        return reasoning_accum, content_accum, tool_calls, finish_reason


def _sanitize_messages(messages: list) -> None:
    """Ensure assistant messages with tool_calls have content=None, not content=""."""
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls") and not m.get("content"):
            m["content"] = None