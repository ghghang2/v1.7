"""Tests for the Alfred voice channel (events, parser, bridge, laptop SSE).

Hardware-touching functions (capture / STT / TTS) are not exercised here —
they are isolated and monkeypatched in the laptop-client path.  These tests
cover the pure logic: the ``<voice>`` tag parser, the event bus, the bridge
inbound queue, and the SSE frame parser.
"""
from __future__ import annotations

import queue
import threading

import pytest

from nbchat.voice.events import (
    ALFRED,
    VoiceEventBus,
    VoiceTagParser,
)
from nbchat.voice.laptop_client import iter_sse_events
from nbchat.voice.server import VoiceBridge


# ---------------------------------------------------------------------------
# VoiceTagParser
# ---------------------------------------------------------------------------


class TestVoiceTagParser:
    def test_no_tags(self):
        p = VoiceTagParser()
        display, blocks = p.process("hello world")
        assert display == "hello world"
        assert blocks == []

    def test_single_complete_block(self):
        p = VoiceTagParser()
        display, blocks = p.process("before <voice>speak this</voice> after")
        assert display == "before  after"
        assert blocks == ["speak this"]

    def test_block_split_across_chunks(self):
        p = VoiceTagParser()
        d1, b1 = p.process("x <voic")
        d2, b2 = p.process("e>hello</voic")
        d3, b3 = p.process("e>tail")
        assert (d1, b1) == ("x ", [])
        assert (d2, b2) == ("", [])
        assert (d3, b3) == ("tail", ["hello"])

    def test_open_tag_at_boundary(self):
        p = VoiceTagParser()
        d1, b1 = p.process("ab<")
        d2, b2 = p.process("voice>hi</voice>")
        assert d1 == "ab"
        assert b1 == []
        assert d2 == ""
        assert b2 == ["hi"]

    def test_multiple_blocks(self):
        p = VoiceTagParser()
        display, blocks = p.process(
            "<voice>one</voice> mid <voice>two</voice>"
        )
        assert blocks == ["one", "two"]
        assert "mid" in display
        assert "one" not in display
        assert "two" not in display

    def test_empty_block_dropped(self):
        p = VoiceTagParser()
        display, blocks = p.process("a <voice></voice> b")
        assert blocks == []
        assert display == "a  b"

    def test_whitespace_stripped(self):
        p = VoiceTagParser()
        display, blocks = p.process("<voice>   spaced   </voice>")
        assert blocks == ["spaced"]

    def test_strip_static(self):
        out = VoiceTagParser.strip("A <voice>hidden</voice> B")
        assert out == "A  B"

    def test_partial_open_tag_held(self):
        # A trailing "<voi" must not leak into display (may complete next).
        p = VoiceTagParser()
        display, blocks = p.process("end <voi")
        assert display == "end "
        assert blocks == []

    def test_realistic_stream(self):
        p = VoiceTagParser()
        full = "Very well, sir. <voice>I shall begin now.</voice> Working..."
        # Feed one char at a time.
        display_parts, all_blocks = [], []
        for ch in full:
            d, b = p.process(ch)
            display_parts.append(d)
            all_blocks.extend(b)
        assert all_blocks == ["I shall begin now."]
        joined = "".join(display_parts)
        assert "Very well, sir." in joined
        assert "Working..." in joined
        assert "<voice>" not in joined
        assert "</voice>" not in joined


# ---------------------------------------------------------------------------
# VoiceEventBus
# ---------------------------------------------------------------------------


class TestVoiceEventBus:
    def test_subscribe_publish(self):
        bus = VoiceEventBus()
        q = bus.subscribe()
        bus.publish("complete", "All done, sir.")
        ev = q.get_nowait()
        assert ev["kind"] == "complete"
        assert ev["text"] == "All done, sir."
        assert "ts" in ev

    def test_fanout_to_multiple(self):
        bus = VoiceEventBus()
        q1, q2 = bus.subscribe(), bus.subscribe()
        assert bus.subscriber_count == 2
        bus.publish("received", "Very well, sir.")
        assert q1.get_nowait()["kind"] == "received"
        assert q2.get_nowait()["kind"] == "received"

    def test_unsubscribe(self):
        bus = VoiceEventBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        assert bus.subscriber_count == 0
        bus.publish("complete", "x")
        assert q.empty()

    def test_disabled_bus_silence(self):
        bus = VoiceEventBus()
        q = bus.subscribe()
        bus.enabled = False
        bus.publish("complete", "x")
        assert q.empty()

    def test_empty_text_ignored(self):
        bus = VoiceEventBus()
        q = bus.subscribe()
        bus.publish("complete", "")
        assert q.empty()

    def test_slow_subscriber_dropped(self):
        bus = VoiceEventBus()
        q = bus.subscribe()  # maxsize=64
        # Flood past the bound; the queue should be dropped, not blocked.
        for i in range(200):
            bus.publish("status", f"update {i}")
        assert bus.subscriber_count == 0


# ---------------------------------------------------------------------------
# ALFRED templates
# ---------------------------------------------------------------------------


class TestAlfredTemplates:
    def test_all_kinds_present(self):
        for kind in ("received", "started", "complete", "failed", "interrupted"):
            assert kind in ALFRED
            assert ALFRED[kind]

    def test_addresses_sir(self):
        # Persona: Alfred addresses the user as "sir".
        for kind in ("received", "started", "complete", "interrupted"):
            assert "sir" in ALFRED[kind].lower()


# ---------------------------------------------------------------------------
# VoiceBridge (inbound queue, no server start)
# ---------------------------------------------------------------------------


class TestVoiceBridge:
    def test_enqueue_drain(self):
        bus = VoiceEventBus()
        bridge = VoiceBridge(bus, port=0)
        bridge.enqueue_inbound("first")
        bridge.enqueue_inbound("second")
        assert bridge.drain_inbound() == ["first", "second"]
        assert bridge.drain_inbound() == []

    def test_get_inbound_timeout(self):
        bus = VoiceEventBus()
        bridge = VoiceBridge(bus, port=0)
        assert bridge.get_inbound(timeout=0.05) is None
        bridge.enqueue_inbound("hi")
        assert bridge.get_inbound(timeout=1.0) == "hi"

    def test_get_inbound_blocking(self):
        bus = VoiceEventBus()
        bridge = VoiceBridge(bus, port=0)
        result = {}

        def producer():
            import time
            time.sleep(0.05)
            bridge.enqueue_inbound("late")

        threading.Thread(target=producer, daemon=True).start()
        assert bridge.get_inbound(timeout=2.0) == "late"


# ---------------------------------------------------------------------------
# iter_sse_events (laptop-side SSE parser)
# ---------------------------------------------------------------------------


class TestIterSSEEvents:
    def test_single_voice_event(self):
        lines = [
            "event: voice",
            'data: {"kind": "complete", "text": "All done, sir."}',
            "",
        ]
        out = list(iter_sse_events(lines))
        assert out == [("complete", "All done, sir.")]

    def test_skips_connected_and_keepalive(self):
        lines = [
            "event: connected",
            "data: {}",
            "",
            ": keepalive",
            "event: voice",
            'data: {"kind": "received", "text": "Very well, sir."}',
            "",
        ]
        out = list(iter_sse_events(lines))
        assert out == [("received", "Very well, sir.")]

    def test_multiple_events(self):
        lines = [
            "event: voice",
            'data: {"kind": "started", "text": "Underway, sir."}',
            "",
            "event: voice",
            'data: {"kind": "complete", "text": "All done, sir."}',
            "",
        ]
        out = list(iter_sse_events(lines))
        assert out == [("started", "Underway, sir."), ("complete", "All done, sir.")]

    def test_bytes_lines(self):
        lines = [
            b"event: voice",
            b'data: {"kind": "tag", "text": "spoken"}',
            b"",
        ]
        out = list(iter_sse_events(lines))
        assert out == [("tag", "spoken")]

    def test_unterminated_final_event(self):
        # No trailing blank line — a real stream may end mid-frame.
        lines = [
            "event: voice",
            'data: {"kind": "failed", "text": "Apologies, sir."}',
        ]
        out = list(iter_sse_events(lines))
        assert out == [("failed", "Apologies, sir.")]

    def test_malformed_data_skipped(self):
        lines = [
            "event: voice",
            "data: {not json",
            "",
        ]
        out = list(iter_sse_events(lines))
        assert out == []

    def test_empty_stream(self):
        assert list(iter_sse_events([])) == []
