"""Tests for _strip_tool_blocks_reasoning — the thinking-token guard.

Markup is built at runtime via hex escapes to avoid tool-infrastructure
interference with literal close-tag sequences.
"""
import sys
sys.path.insert(0, "/v1.7")
from nbchat.ui.conversation import _strip_tool_blocks_reasoning as f

TC_OPEN = "\x3ctool_call>"
TC_CLOSE = "\x3c/tool_call>"
PARAM_CLOSE = "\x3c/parameter>"


def _block(name, params):
    p = "".join(
        f"\x3cparameter={k}>{v}{PARAM_CLOSE}\n" for k, v in params.items()
    )
    return TC_OPEN + f"\n\x3cfunction={name}>\n{p}\n\x3c/function>\n" + TC_CLOSE


def test_plain_unchanged():
    r = "Let me think about this step by step."
    assert f(r) == r


def test_complete_block_stripped():
    r = "Plan:\n" + _block("read_file", {"path": "a.py"}) + "\nNext step."
    out = f(r)
    assert "tool_call" not in out.lower()
    assert "Plan" in out
    assert "Next step" in out


def test_truncated_block_stripped():
    r = "Let me check.\n" + TC_OPEN + "\n\x3cfunction=read_file>\n\x3cparameter=path>\na.py"
    out = f(r)
    assert "tool_call" not in out.lower()
    assert "Let me check" in out


def test_empty_and_none():
    assert f("") == ""
    assert f(None) == ""


def test_multiple_blocks():
    r = "A " + _block("run_command", {"command": "echo hi"}) + " B " + _block("create_file", {"path": "b.py"}) + " C"
    out = f(r)
    assert "tool_call" not in out.lower()
    assert "A" in out and "B" in out and "C" in out