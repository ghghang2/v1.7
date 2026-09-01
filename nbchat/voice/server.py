"""Voice bridge — Alfred voice channel for the TUI.

Runs a small FastAPI app *inside the TUI process* (uvicorn on a daemon
thread, localhost only) so the inbound queue and the event bus are shared
directly with the agent — no IPC, no extra process.

Endpoints
---------
``POST /voice``
    ``{"text": ...}`` — an STT transcript from the laptop.  Enqueued for
    auto-submission by the TUI main loop.  This is the *verified receipt*
    point: the "received" voice ack is only fired by the TUI after it has
    actually handed the message to the agent.

``GET /voice/stream``
    Server-Sent Events stream of verified voice events (see
    :mod:`nbchat.voice.events`).  The laptop holds this open and speaks
    each event with Piper.

``GET /health``
    Liveness probe.

The laptop reaches this port through an SSH tunnel::

    ssh -L 8765:127.0.0.1:8765 user@server
"""
from __future__ import annotations

import json
import logging
import queue
import threading

from nbchat.voice.events import VoiceEventBus

_log = logging.getLogger("nbchat.voice")


class VoiceBridge:
    """In-process voice bridge: inbound queue + SSE outbound fan-out."""

    def __init__(self, bus: VoiceEventBus, port: int = 8765) -> None:
        self.bus = bus
        self.port = port
        self._inbound: queue.Queue = queue.Queue()
        self._app = self._build_app()
        self._server = None
        self._thread: threading.Thread | None = None

    # -- inbound ----------------------------------------------------------

    def enqueue_inbound(self, text: str) -> None:
        """Called by the HTTP layer when a transcript arrives."""
        self._inbound.put(text)

    def drain_inbound(self) -> list[str]:
        """Called by the TUI main loop; returns all pending transcripts."""
        out: list[str] = []
        while True:
            try:
                out.append(self._inbound.get_nowait())
            except queue.Empty:
                return out

    def get_inbound(self, timeout: float | None = None) -> str | None:
        """Blocking fetch of one transcript (for the auto-submit thread).

        Returns ``None`` when *timeout* elapses with nothing queued.
        """
        try:
            return self._inbound.get(timeout=timeout)
        except queue.Empty:
            return None

    # -- HTTP app ---------------------------------------------------------

    def _build_app(self):
        from fastapi import FastAPI
        from fastapi.responses import StreamingResponse

        app = FastAPI(title="nbchat-voice", docs_url=None, redoc_url=None)

        @app.get("/health")
        def health():
            return {"ok": True, "subscribers": self.bus.subscriber_count}

        @app.post("/voice")
        def voice_in(payload: dict):
            text = (payload.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "empty text"}
            self.enqueue_inbound(text)
            _log.info("voice inbound queued (%d chars)", len(text))
            return {"ok": True}

        @app.get("/voice/stream")
        def voice_stream():
            q = self.bus.subscribe()

            def gen():
                try:
                    yield "event: connected\ndata: {}\n\n"
                    while True:
                        try:
                            ev = q.get(timeout=15)
                        except queue.Empty:
                            yield ": keepalive\n\n"
                            continue
                        yield (
                            "event: voice\n"
                            f"data: {json.dumps(ev)}\n\n"
                        )
                finally:
                    self.bus.unsubscribe(q)

            return StreamingResponse(
                gen(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                },
            )

        return app

    # -- lifecycle --------------------------------------------------------

    def start(self) -> bool:
        """Start uvicorn on a daemon thread.  Returns True if serving."""
        import uvicorn

        cfg = uvicorn.Config(
            self._app, host="127.0.0.1", port=self.port,
            log_level="warning", lifespan="off",
        )
        self._server = uvicorn.Server(cfg)
        self._thread = threading.Thread(
            target=self._server.run, name="nbchat-voice", daemon=True
        )
        self._thread.start()
        # Wait briefly for the server to bind (or fail).
        import time
        for _ in range(50):
            if self._server.started:
                return True
            if self._server.should_exit:
                return False
            time.sleep(0.1)
        return self._server.started

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=3)


__all__ = ["VoiceBridge"]
