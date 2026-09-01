#!/usr/bin/env python3
"""Alfred laptop client — runs on the Fedora laptop, not the server.

This is the *other end* of the voice channel.  It:

1. Captures a voice command on push-to-talk from the laptop mic.
2. Transcribes it **locally** with Whisper (no audio crosses the tunnel).
3. POSTs the transcript to the server's voice bridge over an SSH tunnel.
4. Holds the server's SSE event stream open and speaks each verified
   voice event aloud with Piper.

The laptop reaches the server's bridge through an SSH tunnel::

    ssh -L 8765:127.0.0.1:8765 user@server

so from the laptop's point of view everything is on ``127.0.0.1:8765``.

Design notes
------------
* STT is local and latency-first: a small Whisper model (``small`` by
  default) runs on the laptop CPU.  Only the resulting text crosses the
  tunnel — never the audio.
* TTS is Piper, run as a subprocess per utterance (simplest, no server to
  babysit).  Voice is configurable.
* Every function that touches hardware (``capture_until_stop``,
  ``transcribe``, ``speak``) is module-level so tests can monkeypatch them
  without a mic, GPU, or Piper binary present.

Laptop dependencies (Fedora)::

    sudo dnf install portaudio-devel espeak-ng
    pip install sounddevice faster-whisper requests
    # Piper:  https://github.com/rhasspy/piper  (download a .onnx voice)

Run::

    python -m nbchat.voice.laptop_client --port 8765

Push-to-talk: press **Enter** to start recording, **Enter** again to stop
and send.  (A real keybinding such as F9 via ``xbindkey``/``pynput`` is a
drop-in upgrade; the Enter fallback keeps the first run dependency-free.)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Configuration defaults (overridable via CLI / env)
# ---------------------------------------------------------------------------
DEFAULT_PORT = 8765
SAMPLE_RATE = 16000          # Whisper's native rate
WHISPER_MODEL = os.getenv("ALFRED_WHISPER_MODEL", "small")
PIPER_VOICE = os.getenv(
    "ALFRED_PIPER_VOICE",
    "en_GB-alan-medium.onnx",   # a composed, dry-witted British voice
)
PIPER_BIN = os.getenv("ALFRED_PIPER_BIN", "piper")

# ---------------------------------------------------------------------------
# Audio capture (sounddevice) — push-to-talk
# ---------------------------------------------------------------------------


def capture_until_stop(stop: threading.Event, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Record from the default mic until *stop* is set; return WAV bytes.

    Uses :mod:`sounddevice` (PortAudio).  The caller owns *stop* and sets
    it to end the recording.  Isolated so tests can monkeypatch it.
    """
    import sounddevice as sd
    import wave

    chunks: list[bytes] = []

    def cb(indata, frames, t, status):  # noqa: ANN001
        if status:
            pass  # overflow/underrun — keep going
        chunks.append(indata.copy())

    with sd.InputStream(
        samplerate=sample_rate, channels=1, dtype="int16", callback=cb,
    ):
        stop.wait()

    if not chunks:
        return b""
    import numpy as np
    data = np.concatenate(chunks)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------
# STT (faster-whisper) — local
# ---------------------------------------------------------------------------

_WHISPER = None


def _get_whisper(model: str = WHISPER_MODEL):
    """Lazily load a faster-whisper model (cached)."""
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel
        _WHISPER = WhisperModel(model, device="cpu", compute_type="int8")
    return _WHISPER


def transcribe(wav_bytes: bytes, model: str = WHISPER_MODEL) -> str:
    """Transcribe WAV bytes to text using a local Whisper model.

    Isolated so tests can monkeypatch it.
    """
    model_obj = _get_whisper(model)
    segments, _info = model_obj.transcribe(
        io.BytesIO(wav_bytes), beam_size=1, vad_filter=True,
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


# ---------------------------------------------------------------------------
# TTS (Piper) — subprocess
# ---------------------------------------------------------------------------


def speak(text: str, voice: str = PIPER_VOICE, piper_bin: str = PIPER_BIN) -> None:
    """Speak *text* aloud with Piper.

    Pipes the text into Piper's stdin and plays the resulting raw PCM with
    ``aplay`` (ALSA).  Silently no-ops if Piper or aplay is missing so the
    client still works headless.  Isolated so tests can monkeypatch it.
    """
    if not text:
        return
    try:
        piper = subprocess.Popen(
            [piper_bin, "--model", voice, "--output-raw"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        raw, _ = piper.communicate(input=text.encode("utf-8"))
        if piper.returncode != 0 or not raw:
            return
        aplay = subprocess.Popen(
            ["aplay", "-q", "-r", "22050", "-f", "S16_LE", "-c", "1"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        aplay.communicate(input=raw)
    except (FileNotFoundError, OSError):
        # No Piper / aplay on this box — degrade to silence.
        return


# ---------------------------------------------------------------------------
# Server bridge I/O
# ---------------------------------------------------------------------------


def post_transcript(base_url: str, text: str, timeout: float = 5.0) -> dict:
    """POST a transcript to the server's ``/voice`` endpoint."""
    import requests
    resp = requests.post(
        f"{base_url}/voice", json={"text": text}, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def iter_sse_events(line_iter):
    """Parse an SSE line stream into ``(kind, text)`` tuples.

    *line_iter* is any iterable yielding lines (a ``requests`` response
    body iterator in production, a plain list in tests).  Yields one tuple
    per ``event: voice`` frame.  ``event: connected`` and keepalive
    comments are skipped.  Robust to lines split across reads and to a
    missing trailing newline.
    """
    event = None
    data_buf: list[str] = []

    def _flush():
        nonlocal event, data_buf
        if event == "voice" and data_buf:
            try:
                payload = json.loads("".join(data_buf))
                yield (payload.get("kind", "tag"), payload.get("text", ""))
            except json.JSONDecodeError:
                pass
        event, data_buf = None, []

    for raw in line_iter:
        line = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        line = line.rstrip("\n").rstrip("\r")
        if line == "":
            yield from _flush()
            continue
        if line.startswith(":"):
            continue  # comment / keepalive
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_buf.append(line[len("data:"):].strip())
    yield from _flush()


def open_event_stream(base_url: str):
    """Open the server's SSE stream; return a line iterator (or raise)."""
    import requests
    resp = requests.get(
        f"{base_url}/voice/stream", stream=True, timeout=None,
    )
    resp.raise_for_status()
    return resp.iter_lines()


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _speak_loop(base_url: str, stop: threading.Event) -> None:
    """Daemon: hold the SSE stream open and speak each event.

    Reconnects with backoff if the stream drops (server restart, tunnel
    flap).
    """
    backoff = 1.0
    while not stop.is_set():
        try:
            lines = open_event_stream(base_url)
            backoff = 1.0
            for _kind, text in iter_sse_events(lines):
                if stop.is_set():
                    return
                if text:
                    speak(text)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            if stop.is_set():
                return
            sys.stderr.write(f"[alfred] stream error: {exc}; retry in {backoff:.0f}s\n")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


def _record_once(capture, stop: threading.Event) -> bytes:
    """Record a single push-to-talk clip in a worker thread.

    *capture* is ``capture_until_stop`` (or a test double); it blocks until
    *stop* is set.
    """
    return capture(stop)


def run(port: int = DEFAULT_PORT, host: str = "127.0.0.1",
        capture=capture_until_stop, stt=transcribe) -> int:
    """Run the Alfred laptop client.

    *capture* and *stt* are injectable for tests.  In production they are
    the real ``capture_until_stop`` / ``transcribe``.
    """
    base_url = f"http://{host}:{port}"
    stop = threading.Event()

    threading.Thread(
        target=_speak_loop, args=(base_url, stop),
        name="alfred-speak", daemon=True,
    ).start()

    sys.stderr.write(
        f"[alfred] connected to {base_url}\n"
        "[alfred] Enter to start recording, Enter again to stop & send, "
        "Ctrl-C to quit\n"
    )
    try:
        while not stop.is_set():
            sys.stderr.write("[alfred] > ")
            try:
                sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                break
            if stop.is_set():
                break

            rec_stop = threading.Event()
            result: dict = {}

            def _worker():
                try:
                    result["wav"] = _record_once(capture, rec_stop)
                except Exception as exc:  # noqa: BLE001
                    result["err"] = exc

            rec_thread = threading.Thread(target=_worker, daemon=True)
            rec_thread.start()
            try:
                sys.stdin.readline()
            except (EOFError, KeyboardInterrupt):
                break
            rec_stop.set()
            rec_thread.join(timeout=30)

            if "err" in result:
                sys.stderr.write(f"[alfred] capture failed: {result['err']}\n")
                continue
            wav = result.get("wav", b"")
            if not wav:
                continue
            try:
                text = stt(wav)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[alfred] STT failed: {exc}\n")
                continue
            if not text:
                sys.stderr.write("[alfred] (no speech detected)\n")
                continue
            sys.stderr.write(f"[alfred] sent: {text!r}\n")
            try:
                post_transcript(base_url, text)
            except Exception as exc:  # noqa: BLE001
                sys.stderr.write(f"[alfred] POST failed: {exc}\n")
        return 0
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alfred laptop voice client")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"voice bridge port (default {DEFAULT_PORT})")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bridge host (default 127.0.0.1, i.e. the SSH tunnel)")
    args = parser.parse_args(argv)
    return run(port=args.port, host=args.host)


if __name__ == "__main__":
    sys.exit(main())
