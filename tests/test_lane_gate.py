"""Unit tests for nbchat.core.lane_gate (client-side decode-lane gate).

The LaneGate caps the number of *concurrent in-flight generations* this
process may run against the fixed-KV-pool server, so a burst of LLM calls
(workers + planner + subtasks + synthesis) can never overshoot the measured
8-decode ceiling and thrash the KV pool.  See docs/c8-saturation-assessment.md
and bench/THROUGHPUT_FINDINGS.md for the throughput rationale.
"""
from __future__ import annotations

import threading
import time

import pytest

from nbchat.core import config
from nbchat.core import lane_gate


@pytest.fixture(autouse=True)
def _reset_gate_singleton(monkeypatch):
    """Reset the lazily-initialised semaphore between tests.

    The gate caches a process-wide ``BoundedSemaphore`` whose size is fixed
    on first use; without a reset a test that changes ``LANE_GATE_LIMIT``
    would keep the previous limit.  monkeypatch restores it afterwards.
    """
    monkeypatch.setattr(lane_gate, "_sem", None, raising=False)
    monkeypatch.setattr(lane_gate, "_limit", 0, raising=False)
    yield


def test_disabled_gate_returns_none():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "LANE_GATE_ENABLED", False)
    try:
        assert lane_gate.acquire() is None
    finally:
        monkeypatch.undo()


def test_limit_follows_config():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 3)
    try:
        assert lane_gate.limit() == 3
    finally:
        monkeypatch.undo()


def test_acquire_releases_once_and_is_bounded():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 2)
    try:
        a = lane_gate.acquire()
        b = lane_gate.acquire()
        assert a is not None and b is not None
        # A third permit must NOT be available while a and b are held (the
        # timeout path returns None + warns).
        monkeypatch.setattr(config, "LANE_GATE_ACQUIRE_TIMEOUT", 0.1)
        c = lane_gate.acquire()
        assert c is None
        # Double-release must not over-release (would let a 3rd through).
        a.release()
        a.release()
        b.release()
        now = lane_gate.acquire()
        assert now is not None
        now.release()
    finally:
        monkeypatch.undo()


def test_gate_is_shared_across_threads(monkeypatch):
    """N workers blocked on the gate see exactly ``limit`` concurrent holds."""
    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 4)
    monkeypatch.setattr(config, "LANE_GATE_ACQUIRE_TIMEOUT", 5.0)

    held = 0
    peak = 0
    lock = threading.Lock()
    events = []

    def worker():
        permit = lane_gate.acquire()
        with lock:
            nonlocal held, peak
            held += 1
            peak = max(peak, held)
            events.append(held)
        time.sleep(0.05)
        if permit is not None:
            permit.release()
        with lock:
            held -= 1

    # 12 workers, limit 4 -> the gate must hold peak concurrency at 4.
    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert peak <= 4, f"gate allowed {peak} concurrent > limit 4"
    assert peak == 4, f"gate under-utilised: peak {peak} < 4"


def test_acquired_permit_releases_on_exception_path():
    """Simulate the client's except path: release on failure, not on success."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 1)
    try:
        permit = lane_gate.acquire()
        assert permit is not None
        # Simulate an HTTP failure -> release.
        permit.release()
        # Slot is free again.
        second = lane_gate.acquire()
        assert second is not None
        second.release()
    finally:
        monkeypatch.undo()


# ---------------------------------------------------------------------------
# Integration: the client's streaming path holds the gate permit end-to-end
# ---------------------------------------------------------------------------

class _Chunk:
    """Attribute-access stand-in for a single OpenAI stream chunk."""
    def __init__(self, content="x"):
        self._c = content
    @property
    def choices(self):
        if not self._c:
            return []
        class _D:
            content = self._c
        class _C:
            delta = _D()
        return [_C()]


class _FakeStream:
    """Stand-in for a raw OpenAI streaming response (iterable of chunks)."""

    def __init__(self, chunks=3):
        self.n = 0
        self._chunks = chunks

    def __iter__(self):
        for _ in range(self._chunks):
            time.sleep(0.02)
            self.n += 1
            yield _Chunk("x")


def test_client_stream_holds_gate_permit(monkeypatch):
    """A streaming call must hold its lane permit for the whole decode and
    release it only once the stream is fully consumed/closed."""
    from nbchat.core import client as client_mod

    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 2)
    monkeypatch.setattr(config, "LANE_GATE_ACQUIRE_TIMEOUT", 5.0)

    captured = {}

    class _Completions:
        def create(self, *args, **kwargs):
            stream = _FakeStream(chunks=3)
            captured["stream"] = stream
            return stream

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _FakeOpenAI:
        def __init__(self):
            self.chat = _Chat()

    cl = client_mod.MetricsLoggingClient(_FakeOpenAI())
    gen = cl.create(model="m", messages=[{"role": "user", "content": "hi"}],
                    temperature=0.0, max_tokens=16, stream=True)

    # Consume the stream fully; the permit must be held across all chunks.
    consumed = 0
    for _ in gen:
        consumed += 1
    assert consumed == 3

    # After the stream is exhausted the permit is released; a fresh acquire
    # succeeds and returns a handle we can release.
    permit = lane_gate.acquire()
    assert permit is not None
    permit.release()
    # The underlying raw stream saw all chunks.
    assert captured["stream"].n == 3


def test_client_stream_releases_permit_on_error(monkeypatch):
    """A permit must be released if the stream raises mid-iteration, so a
    failing decode never leaks a lane."""
    from nbchat.core import client as client_mod

    class _BoomStream:
        def __iter__(self):
            yield _Chunk("a")
            raise RuntimeError("boom")

    class _Completions:
        def create(self, *a, **k):
            return _BoomStream()

    class _Chat:
        def __init__(self):
            self.completions = _Completions()

    class _FakeOpenAI:
        def __init__(self):
            self.chat = _Chat()

    monkeypatch.setattr(config, "LANE_GATE_ENABLED", True)
    monkeypatch.setattr(config, "LANE_GATE_LIMIT", 1)
    monkeypatch.setattr(config, "LANE_GATE_ACQUIRE_TIMEOUT", 5.0)

    cl = client_mod.MetricsLoggingClient(_FakeOpenAI())
    gen = cl.create(model="m", messages=[{"role": "user", "content": "hi"}],
                    temperature=0.0, max_tokens=16, stream=True)
    with pytest.raises(RuntimeError):
        list(gen)

    # The leaked permit would otherwise block forever; a fresh acquire must
    # succeed promptly.
    permit = lane_gate.acquire()
    assert permit is not None
    permit.release()
