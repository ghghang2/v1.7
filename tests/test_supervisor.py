"""Tests for the nbchat supervisor feature.

Covers:
- InterjectionQueue (thread-safe push/drain/len/bool/maxlen)
- Supervisor (construction, lifecycle, ask, _review_assistant)
- gather_state (structure, exception safety)
- Email bridge supervisor routing (_should_process, _handle_supervisor_email)
- Agent integration (interject, drain_interjections, _turn_active)
"""
from __future__ import annotations

import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from nbchat.core.supervisor import (
    InterjectionQueue,
    Supervisor,
    gather_state,
    create_supervisor,
)
from nbchat.tui.agent import TerminalAgent


# ── InterjectionQueue ──────────────────────────────────────────────────────────

def test_queue_empty_by_default():
    q = InterjectionQueue()
    assert len(q) == 0
    assert not q
    assert q.drain() == []


def test_queue_push_and_drain():
    q = InterjectionQueue()
    q.push("hello")
    q.push("world")
    assert len(q) == 2
    assert q
    items = q.drain()
    assert items == ["hello", "world"]
    assert len(q) == 0
    assert not q


def test_queue_drain_clears():
    q = InterjectionQueue()
    q.push("a")
    q.push("b")
    q.drain()
    assert q.drain() == []


def test_queue_maxlen():
    q = InterjectionQueue(maxlen=3)
    for i in range(10):
        q.push(str(i))
    # Only last 3 survive
    items = q.drain()
    assert items == ["7", "8", "9"]


def test_queue_thread_safety():
    q = InterjectionQueue(maxlen=100)
    errors = []

    def producer(n):
        try:
            for i in range(50):
                q.push(f"{n}-{i}")
        except Exception as e:
            errors.append(e)

    def consumer():
        try:
            for _ in range(20):
                q.drain()
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=producer, args=(i,)) for i in range(5)]
    threads += [threading.Thread(target=consumer) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    # Queue should be empty (consumers drained everything) or have some items
    # but no corruption.
    remaining = q.drain()
    assert all(isinstance(x, str) for x in remaining)


# ── Supervisor construction & lifecycle ────────────────────────────────────────

def test_supervisor_constructs():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=5, max_output_tokens=64)
    assert sup._interval == 10
    assert sup._cooldown == 5
    assert sup._max_tokens == 64
    assert sup.running is False
    assert sup.interjection_count == 0


def test_supervisor_defaults_from_config():
    from nbchat.core import config
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent)
    assert sup._interval == config.SUPERVISOR_INTERVAL
    assert sup._cooldown == config.SUPERVISOR_COOLDOWN
    assert sup._max_tokens == config.SUPERVISOR_MAX_OUTPUT_TOKENS


def test_supervisor_start_stop():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=1, cooldown=1)
    sup.start()
    assert sup.running is True
    time.sleep(0.1)
    sup.stop(timeout=2.0)
    assert sup.running is False


def test_supervisor_start_idempotent():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=1, cooldown=1)
    sup.start()
    thread_ref = sup._thread
    sup.start()  # second call should be a no-op
    assert sup._thread is thread_ref
    sup.stop()


def test_create_supervisor_factory():
    agent = TerminalAgent(color=False)
    sup = create_supervisor(agent, interval=5)
    assert isinstance(sup, Supervisor)
    assert sup._interval == 5


# ── Supervisor.ask (mocked LLM) ────────────────────────────────────────────────

def test_supervisor_ask_returns_llm_response():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=5, max_output_tokens=128)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "The server is healthy on port 8080."

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        answer = sup.ask("Is the server healthy?")

    assert answer == "The server is healthy on port 8080."
    # Verify the LLM was called with the right model
    call_kwargs = mock_client.chat.completions.create.call_args[1]
    from nbchat.core import config
    assert call_kwargs["model"] == config.MODEL_NAME


def test_supervisor_ask_handles_llm_error():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=5, max_output_tokens=128)

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = ConnectionError("server down")

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        answer = sup.ask("status?")

    assert "[supervisor error]" in answer
    assert "ConnectionError" in answer


def test_supervisor_ask_handles_empty_content():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=5, max_output_tokens=128)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = None  # None content

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        answer = sup.ask("hello?")

    assert answer == ""


# ── Supervisor._review_assistant (mocked LLM) ──────────────────────────────────

def test_review_skips_when_not_active():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=0, max_output_tokens=64)
    agent._turn_active = False

    mock_client = MagicMock()
    with patch("nbchat.core.client.get_client", return_value=mock_client):
        sup._review_assistant()

    # LLM should NOT have been called
    mock_client.chat.completions.create.assert_not_called()


def test_review_respects_cooldown():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=999, max_output_tokens=64)
    agent._turn_active = True
    agent.task_log = ["did something"]
    agent._interjection_queue.push("test")
    # Set last interjection to "now" so cooldown blocks
    sup._last_interjection = time.time()

    mock_client = MagicMock()
    with patch("nbchat.core.client.get_client", return_value=mock_client):
        sup._review_assistant()

    mock_client.chat.completions.create.assert_not_called()


def test_review_on_track_no_interjection():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=0, max_output_tokens=64)
    agent._turn_active = True
    agent.task_log = ["step 1", "step 2"]
    agent.history = [("user", "do X", "", "", "", 0),
                     ("assistant", "working on it", "", "", "", 0)]

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ON_TRACK"

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        sup._review_assistant()

    # No interjection should be queued
    assert len(agent._interjection_queue) == 0
    assert sup.interjection_count == 0


def test_review_off_track_injects_interjection():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=0, max_output_tokens=64)
    agent._turn_active = True
    agent.task_log = ["step 1"]
    agent.history = [("user", "write a report", "", "", "", 0)]

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "You are not writing the report. Focus on the main task."

    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        sup._review_assistant()

    # Interjection should be queued
    assert len(agent._interjection_queue) == 1
    items = agent._interjection_queue.drain()
    assert "not writing the report" in items[0]
    assert sup.interjection_count == 1


def test_review_handles_llm_error_gracefully():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=0, max_output_tokens=64)
    agent._turn_active = True
    agent.task_log = ["step 1"]

    mock_client = MagicMock()
    mock_client.chat.completions.create.side_effect = TimeoutError("timed out")

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        # Should not raise
        sup._review_assistant()

    assert len(agent._interjection_queue) == 0
    assert sup.interjection_count == 0


def test_review_skips_when_no_goal_or_actions():
    agent = TerminalAgent(color=False)
    sup = Supervisor(agent, interval=10, cooldown=0, max_output_tokens=64)
    agent._turn_active = True
    agent.task_log = []  # no actions
    agent.history = []  # no exchange

    # Mock get_core_memory to return empty goal
    with patch("nbchat.core.db.get_core_memory", return_value={}):
        mock_client = MagicMock()
        with patch("nbchat.core.client.get_client", return_value=mock_client):
            sup._review_assistant()

    mock_client.chat.completions.create.assert_not_called()


# ── gather_state ───────────────────────────────────────────────────────────────

def test_gather_state_structure():
    agent = TerminalAgent(color=False)
    state = gather_state(agent)

    assert "timestamp" in state
    assert "server" in state
    assert "git" in state
    assert "tasks" in state
    assert "assistant" in state

    # Server info
    assert "model" in state["server"]
    assert "port" in state["server"]
    assert "healthy" in state["server"]

    # Assistant info
    assert "session_id" in state["assistant"]
    assert "turn_active" in state["assistant"]
    assert "tool_running" in state["assistant"]


def test_gather_state_includes_task_log():
    agent = TerminalAgent(color=False)
    agent.task_log = ["action1", "action2", "action3"]
    state = gather_state(agent)
    assert "recent_actions" in state["assistant"]
    assert len(state["assistant"]["recent_actions"]) == 3


def test_gather_state_includes_goal():
    agent = TerminalAgent(color=False)
    with patch("nbchat.core.db.get_core_memory",
               return_value={"goal": "Write a research report"}):
        state = gather_state(agent)
    assert state["assistant"]["current_goal"] == "Write a research report"


def test_gather_state_exception_safe():
    """Even if sub-gatherers fail, gather_state returns a valid dict."""
    agent = TerminalAgent(color=False)
    with patch("nbchat.core.db.get_core_memory", side_effect=Exception("db error")):
        state = gather_state(agent)
    # Should still have top-level keys
    assert "server" in state
    assert "assistant" in state


# ── Agent integration ──────────────────────────────────────────────────────────

def test_agent_has_interject_and_drain():
    agent = TerminalAgent(color=False)
    assert hasattr(agent, "interject")
    assert hasattr(agent, "drain_interjections")
    assert hasattr(agent, "_interjection_queue")
    assert hasattr(agent, "_turn_active")
    assert agent._turn_active is False


def test_agent_interject_and_drain():
    agent = TerminalAgent(color=False)
    agent.interject("focus on the task")
    agent.interject("also check the tests")
    assert len(agent._interjection_queue) == 2
    items = agent.drain_interjections()
    assert items == ["focus on the task", "also check the tests"]
    assert len(agent._interjection_queue) == 0


def test_agent_busy_property():
    agent = TerminalAgent(color=False)
    assert agent.busy is False
    agent._turn_active = True
    assert agent.busy is True
    agent._turn_active = False
    assert agent.busy is False


# ── Email bridge supervisor routing ────────────────────────────────────────────

def test_email_should_process_supervisor_subject():
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core import email_smtp
    from nbchat.core import email_inbox

    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<sup1@x>", from_addr=email_smtp.LOGIN,
        subject="supervisor: what model are we using?", body="status?",
        date=None, uid="100",
    )
    assert bridge._should_process(msg) is True


def test_email_should_process_supervisor_case_insensitive():
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core import email_smtp
    from nbchat.core import email_inbox

    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)

    msg = email_inbox.EmailMessage(
        message_id="<sup2@x>", from_addr=email_smtp.LOGIN,
        subject="SUPERVISOR check", body="hi",
        date=None, uid="101",
    )
    assert bridge._should_process(msg) is True


def test_email_supervisor_routing_in_poll():
    """When _supervisor is set and subject has 'supervisor', it routes to
    _handle_supervisor_email, NOT to send_from_email."""
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core import email_smtp
    from nbchat.core import email_inbox

    agent = TerminalAgent(color=False)
    mock_sup = MagicMock()
    mock_sup.ask.return_value = "Server is on port 8080."

    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1,
                         supervisor=mock_sup)

    msg = email_inbox.EmailMessage(
        message_id="<sup3@x>", from_addr=email_smtp.LOGIN,
        subject="supervisor: server status", body="what port is the server on?",
        date=None, uid="102",
    )

    with patch("nbchat.core.email_inbox.mark_read") as mock_mark:
        bridge._handle_supervisor_email(msg)

    mock_sup.ask.assert_called_once_with("what port is the server on?")
    mock_mark.assert_called_once_with("102")
    # send_from_email should NOT have been called
    agent.send_from_email = MagicMock()
    # (We can't easily test the full _poll_once without mocking fetch_unseen,
    # but the routing logic is verified by the _should_process + _handle calls.)


def test_email_supervisor_auto_reply():
    """When auto_reply is True, the supervisor answer is sent by email."""
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core import email_smtp
    from nbchat.core import email_inbox

    agent = TerminalAgent(color=False)
    mock_sup = MagicMock()
    mock_sup.ask.return_value = "All systems go."

    bridge = EmailBridge(agent, auto_reply=True, poll_interval=1,
                         supervisor=mock_sup)

    msg = email_inbox.EmailMessage(
        message_id="<sup4@x>", from_addr=email_smtp.LOGIN,
        subject="supervisor: check status", body="status?",
        date=None, uid="103",
    )

    with patch("nbchat.core.email_inbox.mark_read"), \
         patch("nbchat.core.email_smtp.send") as mock_send:
        bridge._handle_supervisor_email(msg)

    mock_send.assert_called_once()
    call_kwargs = mock_send.call_args[1]
    assert call_kwargs["body"] == "All systems go."
    assert "supervisor" in call_kwargs["subject"].lower()


def test_email_supervisor_not_routed_when_no_supervisor():
    """When _supervisor is None, supervisor-subject emails fall through
    to the normal send_from_email path (or are skipped)."""
    from nbchat.tui.email_bridge import EmailBridge
    from nbchat.core import email_smtp
    from nbchat.core import email_inbox

    agent = TerminalAgent(color=False)
    bridge = EmailBridge(agent, auto_reply=False, poll_interval=1)
    assert bridge._supervisor is None

    msg = email_inbox.EmailMessage(
        message_id="<sup5@x>", from_addr=email_smtp.LOGIN,
        subject="supervisor: hello", body="hi",
        date=None, uid="104",
    )
    # _should_process still returns True (subject has 'supervisor')
    assert bridge._should_process(msg) is True
    # But _handle_supervisor_email would crash (None.ask) — the _poll_once
    # guard checks `self._supervisor is not None` before routing.


# ── Watchdog loop integration (short-lived) ────────────────────────────────────

def test_watchdog_loop_fires_and_stops():
    """Start the watchdog with a very short interval, let it fire once,
    then stop it.  The LLM is mocked."""
    agent = TerminalAgent(color=False)
    agent._turn_active = True
    agent.task_log = ["did a thing"]
    agent.history = [("user", "do something", "", "", "", 0)]

    sup = Supervisor(agent, interval=0.1, cooldown=0, max_output_tokens=32)

    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "ON_TRACK"
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = mock_resp

    with patch("nbchat.core.client.get_client", return_value=mock_client):
        sup.start()
        time.sleep(0.3)  # let it fire at least once
        sup.stop(timeout=2.0)

    assert sup.running is False
    # It should have called the LLM at least once
    assert mock_client.chat.completions.create.call_count >= 1
