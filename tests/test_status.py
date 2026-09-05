"""Tests for nbchat.tui.status (Phase 1: model + pure renderer)."""

import time

from nbchat.tui import status as st


# -- AgentStatus ---------------------------------------------------------

def test_agent_status_defaults():
    a = st.AgentStatus("w1")
    assert a.id == "w1"
    assert a.label == "w1"
    assert a.state == "idle"
    assert a.detail == ""
    assert a.tokens_seen == 0
    assert a.owner_alive is True

    snap = a.snapshot()
    for key in ("id", "label", "state", "detail", "since",
                "tokens_seen", "last_tool", "owner_alive"):
        assert key in snap


def test_agent_status_label_override():
    a = st.AgentStatus("worker-2", label="w2")
    assert a.label == "w2"


# -- StatusBar -----------------------------------------------------------

def _bar():
    bar = st.StatusBar()
    bar.set_model("qwen3-coder")
    return bar


def test_register_and_snapshot():
    bar = _bar()
    bar.register("assistant")
    bar.register("w2", label="worker-2")
    snap = bar.snapshot()
    assert bar.agent_ids() == ["assistant", "w2"]
    ids = [a["id"] for a in snap["agents"]]
    assert ids == ["assistant", "w2"]
    assert snap["model"] == "qwen3-coder"


def test_set_state_updates_state_and_detail():
    bar = _bar()
    bar.register("assistant")
    bar.set_state("assistant", "thinking")
    bar.set_state("assistant", "tool", "pytest")
    a = [x for x in bar.snapshot()["agents"] if x["id"] == "assistant"][0]
    assert a["state"] == "tool"
    assert a["detail"] == "pytest"
    assert a["last_tool"] == "pytest"


def test_set_state_invalid_state_falls_back_to_idle():
    bar = _bar()
    bar.register("assistant")
    bar.set_state("assistant", "bogus-state")
    a = [x for x in bar.snapshot()["agents"] if x["id"] == "assistant"][0]
    assert a["state"] == "idle"


def test_set_state_unknown_agent_creates_it():
    bar = _bar()
    bar.set_state("ghost", "thinking")  # no explicit register
    assert "ghost" in bar.agent_ids()


def test_set_state_resets_since_on_change_only():
    bar = _bar()
    bar.register("assistant")
    bar.set_state("assistant", "thinking")
    first = bar.snapshot()["agents"][0]["since"]
    bar.set_state("assistant", "thinking", "still")
    second = bar.snapshot()["agents"][0]["since"]
    assert first == second
    bar.set_state("assistant", "tool", "x")
    third = bar.snapshot()["agents"][0]["since"]
    assert third > first


def test_unregister():
    bar = _bar()
    bar.register("assistant")
    bar.register("w2")
    bar.unregister("w2")
    assert bar.agent_ids() == ["assistant"]


def test_set_context_and_turn():
    bar = _bar()
    bar.register("assistant")
    bar.set_context(12345.0, 32000.0)
    bar.set_turn(3)
    snap = bar.snapshot()
    assert snap["context_used"] == 12345.0
    assert snap["context_budget"] == 32000.0
    assert snap["turn"] == 3


# -- render_line ----------------------------------------------------------

def test_render_line_contains_core_fields():
    bar = _bar()
    bar.register("assistant")
    bar.set_state("assistant", "tool", "pytest")
    bar.set_context(13440.0, 32000.0)
    bar.set_turn(2)
    line = st.render_line(bar.snapshot(), term_width=120)
    assert "qwen3-coder" in line
    assert "tool" in line
    assert "pytest" in line
    assert "turn 2" in line

    # exactly one line
    assert "\n" not in line


def test_render_line_truncates_to_width():
    bar = _bar()
    bar.register("assistant")
    bar.register("w2", label="worker-two")
    bar.register("w3", label="worker-three")
    bar.set_state("w3", "thinking")
    bar.set_context(31000.0, 32000.0)
    bar.set_turn(9)
    line = st.render_line(bar.snapshot(), term_width=40)
    assert len(line) <= 40

    # narrow width: model name survives, tail items are dropped
    narrow = st.render_line(bar.snapshot(), term_width=20)
    assert len(narrow) <= 20
    assert "qwen3-coder" in narrow


def test_render_line_idle_agent_omitted_from_detail():
    bar = _bar()
    bar.register("assistant")
    line = st.render_line(bar.snapshot(), term_width=120)
    assert "w2" not in line  # only one agent registered
    assert "idle" in line or "assistant" not in line
