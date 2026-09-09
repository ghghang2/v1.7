"""OpenAI-compatible client with streaming metrics logging."""
from __future__ import annotations

import logging
import contextvars
import time

from openai import OpenAI
from .config import SERVER_URL

logger = logging.getLogger("Inference_Metrics")

# Team-mode LLM call budget.  Set (per thread/context) by the team
# coordinator before a worker turn runs; ``MetricsLoggingClient.create``
# picks it up so no single worker LLM request can outlive the team's
# task deadline.  A parked worker was the root cause of hung /team
# runs: the SDK default read timeout is 600s and it retries twice.
team_llm_timeout: contextvars.ContextVar = contextvars.ContextVar(
    "nbchat_team_llm_timeout", default=None)

if not logger.handlers:
    _h = logging.FileHandler("inference_metrics.log")
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class _InstrumentedStream:
    """Proxies an OpenAI stream, logging TTFT and token usage.

    Optionally carries a decode-lane :class:`nbchat.core.lane_gate.GateHandle`
    that is held for the *entire* decode (released only once, when the
    stream is exhausted, closed, errors, or the context manager exits).
    """

    def __init__(self, stream, t0: float, gate=None):
        self._stream = stream
        self._t0 = t0
        self._gate = gate

    def _release_gate(self) -> None:
        if self._gate is not None:
            self._gate.release()
            self._gate = None

    def __iter__(self):
        ttft = None
        usage = None
        try:
            for chunk in self._stream:
                if ttft is None and chunk.choices and chunk.choices[0].delta.content:
                    ttft = time.time() - self._t0
                    logger.info("TTFT: %.3fs", ttft)
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                yield chunk
        except Exception as e:
            logger.error("Stream error after %.2fs: %s", time.time() - self._t0, e)
            raise
        finally:
            total = time.time() - self._t0
            self._release_gate()
            if usage:
                try:
                    from nbchat.core.team_metrics import record_tokens
                    record_tokens(usage.total_tokens)
                except Exception:
                    pass
            if usage:
                logger.info("Latency: %.2fs | P:%d C:%d T:%d",
                            total, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            else:
                logger.warning("Latency: %.2fs | no usage data", total)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
        self._release_gate()
        return self._stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._stream, name)


class MetricsLoggingClient:
    """Thin wrapper around OpenAI that logs latency and token usage."""

    def __init__(self, client: OpenAI):
        self._client = client

    def __getattr__(self, name):
        return getattr(self._client, name)

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    def create(self, *args, **kwargs):
        if kwargs.get("stream"):
            kwargs.setdefault("stream_options", {})["include_usage"] = True
        team_timeout = team_llm_timeout.get()
        if team_timeout is not None:
            # Bound the in-flight HTTP request inside team runs.  (The
            # SDK's ``max_retries`` is a client-constructor option in
            # openai 3.x -- passing it per request raises TypeError and
            # killed every worker LLM call, see incident 2026-09-04.)
            kwargs.setdefault("timeout", float(team_timeout))
        kwargs.setdefault("extra_body", {})["cache_prompt"] = True
        # Decode-lane gate: cap concurrent in-flight generations to the
        # saturation ceiling so bursts don't overshoot the KV pool.  The
        # permit is held for the whole decode (streams release it only on
        # exhaustion/closing).  acquire() is idempotent-safe and never
        # blocks indefinitely.
        from . import lane_gate
        permit = lane_gate.acquire()
        t0 = time.time()
        try:
            response = self._client.chat.completions.create(*args, **kwargs)
        except Exception as e:
            if permit is not None:
                permit.release()
            logger.error("Request failed after %.2fs: %s", time.time() - t0, e)
            raise
        if kwargs.get("stream"):
            # Transfer the permit to the stream so it stays held until the
            # last token is consumed (or the stream is closed early).
            return _InstrumentedStream(response, t0, gate=permit)
        if permit is not None:
            permit.release()
        u = getattr(response, "usage", None)
        if u:
            try:
                from nbchat.core.team_metrics import record_tokens
                record_tokens(u.total_tokens)
            except Exception:
                pass
            logger.info("Latency: %.2fs | P:%d C:%d T:%d",
                        time.time() - t0, u.prompt_tokens, u.completion_tokens, u.total_tokens)
        return response


def get_client() -> MetricsLoggingClient:
    return MetricsLoggingClient(OpenAI(base_url=f"{SERVER_URL}/v1", api_key="sk-il7yefsdb1uzd1"))
