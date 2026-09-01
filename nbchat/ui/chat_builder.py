"""Build OpenAI API message lists from internal chat history.

KV-cache design: messages[0] is always exactly `system_prompt`, unmodified.
Volatile context (L1/L2/prior summaries, task log) goes into a synthetic
user/assistant pair at messages[1-2] so the system prompt prefix is
token-identical on every call and gets full KV cache hits on llama-server.

Layout:
  [0] {"role": "system",    "content": system_prompt}       ← static, cached
  [1] {"role": "user",      "content": "[SESSION CONTEXT]…"} ← volatile, omitted if empty
  [2] {"role": "assistant", "content": "Context received."}  ← volatile, omitted if empty
  [3+] actual conversation (user / assistant / tool)
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List, Tuple

_log = logging.getLogger("nbchat.compaction")

_CTX_LABEL = "[SESSION CONTEXT — updated each turn]"
_CTX_ACK = "Context received."


def build_messages(
    history: List[Tuple[str, str, str, str, str, int]],
    system_prompt: str,
    task_log: List[str] | None = None,
) -> List[Dict]:
    """Convert windowed history into an OpenAI message list.

    Leading ("system", …) rows injected by ContextMixin._window() (L1/L2/prior
    context) are folded into the volatile context turn, never into messages[0].
    ("analysis", …) reasoning-trace rows are dropped — display-only, not sent.
    """
    messages: List[Dict] = [{"role": "system", "content": system_prompt}]

    # Split leading system rows (context blocks) from actual conversation.
    context_parts: List[str] = []
    conv_rows: List[Tuple] = []
    conv_started = False
    for row in history:
        role, content = row[0], row[1]
        if role == "system" and not conv_started:
            context_parts.append(content)
        else:
            if role not in ("system", "analysis"):
                conv_started = True
            if role == "system":
                # Mid-conversation system row (shouldn't occur normally) — demote.
                conv_rows.append(("_context_note", content, *row[2:]))
            elif role != "analysis":
                conv_rows.append(row)

    # Volatile context turn (messages[1-2]) — omitted when empty.
    volatile: List[str] = []
    if task_log:
        entries = "\n".join(f"  {e}" for e in task_log[-20:])
        volatile.append("[RECENT ACTION LOG]\n" + entries + "\n[END ACTION LOG]")
    volatile.extend(context_parts)
    if volatile:
        messages.append({"role": "user", "content": _CTX_LABEL + "\n\n" + "\n\n".join(volatile)})
        messages.append({"role": "assistant", "content": _CTX_ACK})

    # Actual conversation.
    for role, content, tool_id, tool_name, tool_args, _ef in conv_rows:
        if role == "user":
            messages.append({"role": "user", "content": content})

        elif role == "_context_note":
            messages.append({"role": "user", "content": f"[CONTEXT NOTE]\n{content}"})

        elif role == "assistant":
            if tool_id:
                # Legacy single-tool DB row format.
                messages.append({
                    "role": "assistant", "content": content or None,
                    "tool_calls": [{"id": tool_id, "type": "function",
                                    "function": {"name": tool_name, "arguments": tool_args}}],
                })
            else:
                messages.append({"role": "assistant", "content": content})

        elif role == "assistant_full":
            try:
                msg = json.loads(tool_args)
                msg.pop("reasoning_content", None)
                if msg.get("tool_calls") and not msg.get("content"):
                    msg["content"] = None
                messages.append(msg)
            except Exception:
                messages.append({"role": "assistant", "content": content})

        elif role == "tool":
            messages.append({"role": "tool", "tool_call_id": tool_id, "content": content})

    return messages


__all__ = ["build_messages"]