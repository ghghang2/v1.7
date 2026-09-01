"""Single executor for all tool calls."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict
import json

from nbchat.tools import TOOLS
from nbchat.core.retry import (
    retry_with_backoff,
    DEFAULT_MAX_RETRIES,
)
import nbchat.core.config as config

_executor = ThreadPoolExecutor(max_workers=4)


def run_tool(tool_name: str, args_json: str, timeout: int | None = None) -> str:
    """Execute a tool with arguments and return the (trimmed) string result.

    Retry policy (see nbchat.core.retry): only *transient* failures
    (timeouts, network/connection errors, server 5xx) are retried with
    exponential backoff and jitter.  Deterministic tool errors — a
    non-zero exit code, an unknown selector, a git push rejection — are
    returned to the model immediately without wasting wall-clock time on
    retries that cannot succeed.
    """
    try:
        args = json.loads(args_json)
    except Exception as e:
        return f"Failed to parse tool arguments: {e}"

    func = next((t.func for t in TOOLS if t.name == tool_name), None)
    if not func:
        return f"Unknown tool '{tool_name}'"

    if timeout is None:
        # Per-tool wall-clock budget (seconds) from repo_config.yaml.
        # The old hard-coded values (browser=10s, run_tests=10s, others=5s)
        # were far below the browser's own 30s navigation timeout and below
        # a real pytest run, so those tools time out on nearly every call.
        timeout = (
            config.BROWSER_TIMEOUT
            if tool_name == "browser"
            else config.TESTS_TIMEOUT
            if tool_name == "run_tests"
            else config.OTHER_TOOLS_TIMEOUT
        )

    def execute_with_retry() -> str:
        """Execute one attempt with a hard wall-clock timeout."""
        # Submit a fresh task on every attempt so a task left over from a
        # timed-out attempt cannot leak into the next attempt's future.
        future = _executor.submit(func, **args)
        try:
            return str(future.result(timeout=timeout))
        except TimeoutError:
            # Keep a done-callback so the pool doesn't silently drop it.
            future.add_done_callback(lambda _f: None)
            raise TimeoutError(f"Tool '{tool_name}' timed out after {timeout} seconds.")
        # NOTE: tool exceptions are deliberately NOT re-wrapped here.
        # Wrapping in a generic Exception("Tool execution error: ...")
        # stripped the original message, which made retry classification
        # unreliable and turned deterministic failures into "retryable" ones.

    # Execute with retry policy
    try:
        result = retry_with_backoff(
            execute_with_retry,
            max_retries=DEFAULT_MAX_RETRIES,
            initial_delay=config.DEFAULT_INITIAL_DELAY,
            max_delay=config.DEFAULT_MAX_DELAY,
            backoff_multiplier=config.DEFAULT_BACKOFF_MULTIPLIER,
        )
        return result
    except Exception as e:
        return f"Tool '{tool_name}' failed after {DEFAULT_MAX_RETRIES} retries: {e}"


__all__ = ["run_tool", "trim_tool_output"]