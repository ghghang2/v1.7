"""Tests for Phase 1b adaptive reasoning effort (nbchat.core.adaptive_effort).

Covers the pure decision functions — effort ladder stepping,
escalation triggers (stall / error streak / truncation), the
clean-run relaxation, and the pinned-effort override — plus the
conversation-loop integration points (``_turn_effort`` reading the
session override, and the escalation hooks existing on the loop).
"""
from nbchat.core import adaptive_effort


# ---------------------------------------------------------------------------
# Ladder
# ---------------------------------------------------------------------------

def test_ladder_order():
    assert adaptive_effort.EFFORT_LADDER[0] == "low"
    assert adaptive_effort.EFFORT_LADDER[-1] == "xhigh"
    # The default sits in the middle of the ladder: room to escalate
    # AND room to de-escalate.
    assert 0 < adaptive_effort.EFFORT_LADDER.index("medium") < len(
        adaptive_effort.EFFORT_LADDER) - 1


def test_next_effort_steps_up_one():
    assert adaptive_effort.next_effort("low", "stall") == "medium"
    assert adaptive_effort.next_effort("medium", "stall") == "high"
    assert adaptive_effort.next_effort("high", "stall") == "xhigh"


def test_next_effort_caps_at_top():
    assert adaptive_effort.next_effort("xhigh", "stall") is None
    # Unknown effort names never escalate (fail-safe: keep the status quo).
    assert adaptive_effort.next_effort("weird", "stall") is None
    assert adaptive_effort.next_effort(None, "stall") is None


def test_escalate_after_errors_threshold():
    assert adaptive_effort.ESCALATE_AFTER_ERRORS >= 2
    assert adaptive_effort.ESCALATE_AFTER_ERRORS <= 5


# ---------------------------------------------------------------------------
# Relaxation (clean-run reset)
# ---------------------------------------------------------------------------

def test_should_reset_only_above_default():
    # Off-ladder efforts ("none"/""/unknown) never reset — there is
    # nothing to relax back down.
    assert not adaptive_effort.should_reset(99, "none")
    assert not adaptive_effort.should_reset(99, "")
    assert not adaptive_effort.should_reset(99, None)
    # Escalated efforts relax back after enough clean turns...
    assert adaptive_effort.should_reset(
        adaptive_effort.CLEAN_TURNS_TO_RESET, "high")
    assert adaptive_effort.should_reset(99, "xhigh")
    # ...but not before the clean-turn count is reached.
    assert not adaptive_effort.should_reset(
        adaptive_effort.CLEAN_TURNS_TO_RESET - 1, "high")


def test_reset_threshold_is_bounded():
    assert adaptive_effort.CLEAN_TURNS_TO_RESET >= 2
    # Relaxation must not happen mid-conversation on a single clean turn.
    assert adaptive_effort.CLEAN_TURNS_TO_RESET <= 10


def test_default_effort_is_on_ladder():
    assert adaptive_effort.DEFAULT_EFFORT in adaptive_effort.EFFORT_LADDER


# ---------------------------------------------------------------------------
# Conversation-loop integration
# ---------------------------------------------------------------------------

def test_loop_wires_turn_effort_and_escalation():
    import inspect

    import nbchat.core.conversation as conv

    loop = conv.ConversationMixin
    src = inspect.getsource(loop._run_conversation_loop)
    # The per-call effort is resolved through the adaptive layer, which
    # honours a user-pinned session effort.
    assert "adaptive_effort" in src
    assert "self._turn_effort" in src  # consumed via self._turn_effort()
    # Both escalation triggers are present in the loop body.
    assert '_maybe_escalate("stall")' in src
    assert "_error_streak" in src
    # The relaxation path is checked on clean turns.
    assert "_note_turn_clean()" in src

    # The effort resolution itself lives in the _turn_effort method.
    loop_src = inspect.getsource(loop._turn_effort)
    assert "reasoning_effort" in loop_src
    assert "_auto_effort" in loop_src
    # The base effort comes from config so no session silently runs at
    # the model template default.
    assert "DEFAULT_REASONING_EFFORT" in loop_src


def test_stream_response_uses_turn_effort():
    import inspect

    import nbchat.core.conversation as conv

    src = inspect.getsource(conv.ConversationMixin._stream_response)
    # The per-call reasoning_effort kwarg is only sent when the resolved
    # effort is non-empty — the adaptive layer feeds this in.
    assert '_create_kwargs["reasoning_effort"]' in src
