"""Regression tests for legacy XML tool-call drift recovery (2026-09-02 incident).

The model occasionally emits tool calls as legacy <tool_call> XML
text in the assistant content instead of the structured tool_calls
channel.  Part 1 recovers and executes complete blocks at stream time;
Part 2 scrubs the markup when message lists are rebuilt from history.
"""
from __future__ import annotations

import json
import re

from nbchat.ui import chat_builder
from nbchat.ui.conversation import (
    _recover_text_tool_calls,
    _strip_tool_blocks,
)

_ID_SHAPE = re.compile(r"^recovered_[0-9a-f]{12}$")

TWO_BLOCK = """
Let me look.
<tool_call>
<function=read_file>
<parameter=path>
nbchat/core/db.py
</parameter>
</function>
</tool_call>
<tool_call>
<function=run_command>
<parameter=command>
echo hi
</parameter>
</function>
</tool_call>
"""

HEREDOC_BLOCK = """
<tool_call>
<function=run_command>
<parameter=command>
python3 - <<'PY'
print("hello")
PY
</parameter>
</function>
</tool_call>
"""

TRUNCATED_BLOCK = """Let me check.
<tool_call>
<function=read_file>
<parameter=path>
nbchat/core/db.py
</parameter>
"""


# ── _recover_text_tool_calls ────────────────────────────────────────────────

def test_recover_two_blocks_shape_and_args():
    calls = _recover_text_tool_calls(TWO_BLOCK)
    assert calls is not None
    assert len(calls) == 2
    for c in calls:
        assert _ID_SHAPE.match(c["id"]), c["id"]
        assert c["type"] == "function"
        assert "name" in c["function"]
        json.loads(c["function"]["arguments"])  # valid JSON
    assert calls[0]["function"]["name"] == "read_file"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "path": "nbchat/core/db.py"
    }
    assert calls[1]["function"]["name"] == "run_command"
    assert json.loads(calls[1]["function"]["arguments"]) == {"command": "echo hi"}


def test_recover_multiline_heredoc_value():
    calls = _recover_text_tool_calls(HEREDOC_BLOCK)
    assert calls is not None
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "run_command"
    args = json.loads(calls[0]["function"]["arguments"])
    assert 'print("hello")' in args["command"]
    assert "<<'PY'" in args["command"]


def test_recover_truncated_block_returns_none():
    assert _recover_text_tool_calls(TRUNCATED_BLOCK) is None


def test_recover_plain_text_returns_none():
    assert _recover_text_tool_calls("Just a normal reply with no calls.") is None


def test_recover_empty_and_none():
    assert _recover_text_tool_calls("") is None
    assert _recover_text_tool_calls(None) is None


# ── _strip_tool_blocks ──────────────────────────────────────────────────────

def test_strip_preserves_surrounding_prose():
    out = _strip_tool_blocks(TWO_BLOCK)
    assert "<tool_call" not in out
    assert "Let me look." in out


def test_strip_removes_truncated_trailing_block():
    assert _strip_tool_blocks(TRUNCATED_BLOCK) == "Let me check."


def test_strip_empty_inputs():
    assert _strip_tool_blocks("") == ""
    assert _strip_tool_blocks(None) == ""


# ── build_messages scrubbing ────────────────────────────────────────────────

def _build(history):
    return chat_builder.build_messages(history, "sys prompt")


def _last_assistant(msgs):
    return [m for m in msgs if m.get("role") == "assistant"][-1]


def test_build_plain_assistant_row_is_scrubbed():
    hist = [
        ("user", "Do it.", "", "", "", 0),
        ("assistant", "I checked.\n" + TWO_BLOCK, "", "", "", 0),
    ]
    assistant = _last_assistant(_build(hist))
    assert "<tool_call" not in assistant["content"]
    assert "I checked." in assistant["content"]


def test_build_pure_markup_row_gets_not_executed_marker():
    hist = [
        ("user", "Do it.", "", "", "", 0),
        ("assistant", TWO_BLOCK.strip().split("\n", 1)[1], "", "", "", 0),
    ]
    assistant = _last_assistant(_build(hist))
    assert assistant["content"] == (
        "[Your previous reply contained only a tool call written as "
        "text markup; it was not executed.]"
    )


def test_build_assistant_full_keeps_tool_calls_and_scrubs_content():
    full = {
        "role": "assistant",
        "content": "Working on it. " + TWO_BLOCK,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "run_command",
                    "arguments": json.dumps({"command": "ls"}),
                },
            },
        ],
    }
    hist = [
        ("user", "Do it.", "", "", "", 0),
        ("assistant_full", "", "full", "full", json.dumps(full), 0),
    ]
    assistant = _last_assistant(_build(hist))
    assert assistant["tool_calls"] == full["tool_calls"]
    assert "<tool_call" not in (assistant["content"] or "")
    assert "Working on it." in assistant["content"]


def test_build_assistant_full_pure_markup_content_becomes_none():
    full = {
        "role": "assistant",
        "content": "<tool_call>\n<function=read_file>\n"
                   "<parameter=path>x</parameter>\n"
                   "</function>\n</tool_call>",
        "tool_calls": [
            {
                "id": "call_2",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "x"}),
                },
            },
        ],
    }
    hist = [
        ("user", "Do it.", "", "", "", 0),
        ("assistant_full", "", "full", "full", json.dumps(full), 0),
    ]
    assistant = _last_assistant(_build(hist))
    assert assistant["tool_calls"][0]["id"] == "call_2"
    assert not assistant.get("content")
