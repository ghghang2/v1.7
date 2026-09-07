"""Tests for the tool-call hang guard.

A tool that never returns (an infinite loop, a blocking call) used to be
treated as a transient failure and re-run up to ``max_retries`` times with
exponential backoff -- multiplying a hang into a very long stall.  The guard:

1. shortens the retry budget (max_retries=2, small delays) so the *non-hang*
   case also fails fast; and
2. classifies a wall-clock timeout as a ``HangError`` (a deterministic,
   non-retryable failure) so it aborts immediately with a truthful,
   continuity-preserving message instead of re-running the hang.

The session-continuity half is verified at the ``run_tool`` boundary: the
result string must say the call *aborted (no retry)* and must not claim it
"failed after N retries", which would mislead the model into retrying the
same hanging call.
"""
import json
import time

import pytest

import nbchat.core.retry as retry
from nbchat.core import config
from nbchat.ui import tool_executor


# -- Configuration -----------------------------------------------------------

def test_retry_budget_is_shortened():
    """The user asked to cap retries at 2 and shorten the wait.  These come
    from repo_config.yaml via nbchat.core.config."""
    assert config.DEFAULT_MAX_RETRIES == 2
    assert config.DEFAULT_INITIAL_DELAY <= 1.0
    assert config.DEFAULT_MAX_DELAY <= 10.0


def test_retry_defaults_match_config():
    assert retry.DEFAULT_MAX_RETRIES == config.DEFAULT_MAX_RETRIES
    assert retry.DEFAULT_MAX_RETRIES == 2


# -- Hang classification -----------------------------------------------------

@pytest.mark.parametrize("msg", [
    "Tool 'run_command' timed out after 30 s (hung). Do not repeat.",
    "Tool 'browser' aborted (hung, no retry): navigation timed out.",
    "timeout: HANG (exceeded 30s budget)",
    "Tool 'x' timed out after 60 seconds.",
])
def test_is_hang_positive(msg):
    assert retry.is_hang(msg), msg


@pytest.mark.parametrize("msg", [
    "File not found: /tmp/nope",
    "Permission denied",
    "git push rejected (pre-receive hook declined)",
    "Connection reset by peer",
])
def test_is_hang_negative(msg):
    assert not retry.is_hang(msg), msg


def test_hang_is_not_retryable():
    assert not retry._is_retryable("tool 'x' timed out after 30 s (hung)")


def test_plain_timeout_is_still_retryable():
    # (the repo's constant name has a pre-existing typo)
    assert "timed out" in retry.RETRIFIABLE_ERRORS
    assert retry.is_hang("timed out after 5 s (hung)")
    assert not retry.is_hang("timed out after 5 s")


# -- HangError bypasses the retry loop ---------------------------------------

def test_hang_error_is_not_retried():
    calls = []

    def hang():
        calls.append(1)
        raise retry.HangError("tool hung")

    with pytest.raises(retry.HangError):
        retry.retry_with_backoff(hang, max_retries=3)
    assert len(calls) == 1, "HangError must not be retried"


def test_non_hang_still_retried_twice():
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("connection reset")

    with pytest.raises(RuntimeError):
        retry.retry_with_backoff(
            flaky, max_retries=2, initial_delay=0.01, max_delay=0.02
        )
    assert len(calls) == 3  # 1 initial + 2 retries


# -- run_tool boundary -------------------------------------------------------

def test_run_tool_hang_aborts_fast_and_truthful():
    # NOTE: the command must be *short* (sleep 3, not sleep 30).  run_tool
    # aborts the hung attempt after `timeout=1`, but the worker thread keeps
    # running the subprocess; ThreadPoolExecutor's atexit hook then blocks
    # interpreter shutdown until it finishes.  A 3s leak keeps the whole
    # pytest process well under the agent's own run_command timeout.
    t0 = time.monotonic()
    result = tool_executor.run_tool(
        "run_command", json.dumps({"command": "sleep 3"}), timeout=1
    )
    elapsed = time.monotonic() - t0

    assert elapsed < 6.0, "hang guard did not abort fast (%.1fs)" % elapsed
    assert "aborted (hung, no retry)" in result, result
    assert "failed after" not in result.lower(), result


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))