"""Shared test fixtures.

The environment ships the ``httpx`` compatibility layer under the name
``httpx2``.  Tests import ``httpx`` directly, so alias it before any test
module is collected.
"""
import sys
import os
import time

try:
    import httpx  # noqa: F401
except ImportError:
    import httpx2

    sys.modules["httpx"] = httpx2


_SESSION_START = time.monotonic()


def _session_budget() -> float:
    """Whole-session wall-clock budget in seconds (default 24, min 1).

    The ``run_command`` tool that executes a full ``pytest`` verification
    caps each command at 30 s (``other_tools_timeout`` in repo_config.yaml).
    A warm run takes ~10 s; a cold start, load, or a wedging test can push
    toward that ceiling, where the harness kills the run mid-suite with no
    result.  The guard below aborts deterministically just before that.
    """
    try:
        return max(1.0, float(os.environ.get("TEST_WALL_BUDGET", "24")))
    except ValueError:
        return 24.0


def pytest_runtest_protocol(item, nextitem):
    """Check the session clock before each test item and abort if the
    wall-clock budget is exceeded.

    Called in the main thread (unlike a daemon watchdog), so ``pytest.exit``
    propagates cleanly.  Because individual tests are short, checking before
    each item gives sub-second granularity against the 24 s budget.  A
    healthy run (~10 s total) never trips this; it only fires when the run
    is genuinely heading for the 30 s tool ceiling.
    """
    import pytest

    elapsed = time.monotonic() - _SESSION_START
    budget = _session_budget()
    if elapsed >= budget:
        pytest.exit(
            f"session aborted before {item.nodeid}: exceeded wall-clock "
            f"budget of {budget:.0f}s (ran {elapsed:.1f}s); this would "
            f"otherwise hit the 30 s tool timeout",
            returncode=1,
        )
    return None
