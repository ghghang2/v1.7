"""OpenAI-compatible client with streaming metrics logging."""
from __future__ import annotations

import logging
import time

from openai import OpenAI
from .config import SERVER_URL

logger = logging.getLogger("Inference_Metrics")
if not logger.handlers:
    _h = logging.FileHandler("inference_metrics.log")
    _h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class _InstrumentedStream:
    """Proxies an OpenAI stream, logging TTFT and token usage."""

    def __init__(self, stream, t0: float):
        self._stream = stream
        self._t0 = t0

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
            if usage:
                logger.info("Latency: %.2fs | P:%d C:%d T:%d",
                            total, usage.prompt_tokens, usage.completion_tokens, usage.total_tokens)
            else:
                logger.warning("Latency: %.2fs | no usage data", total)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, *args):
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
        kwargs.setdefault("extra_body", {})["cache_prompt"] = True
        t0 = time.time()
        try:
            response = self._client.chat.completions.create(*args, **kwargs)
        except Exception as e:
            logger.error("Request failed after %.2fs: %s", time.time() - t0, e)
            raise
        if kwargs.get("stream"):
            return _InstrumentedStream(response, t0)
        u = getattr(response, "usage", None)
        if u:
            logger.info("Latency: %.2fs | P:%d C:%d T:%d",
                        time.time() - t0, u.prompt_tokens, u.completion_tokens, u.total_tokens)
        return response


def get_client() -> MetricsLoggingClient:
    return MetricsLoggingClient(OpenAI(base_url=f"{SERVER_URL}/v1", api_key="sk-il7yefsdb1uzd1"))
