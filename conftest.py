"""Pytest bootstrap.

The ``nbchat`` package is not installed (there is no pyproject/setup), so
pytest's default ``prepend`` import mode only puts ``tests/`` on ``sys.path``
and ``import nbchat`` would fail.  This root-level conftest makes the repo
root importable so the test suite can import ``nbchat``.
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# -- opt-in slow/interactive tests -----------------------------------------
#
# Tests marked ``@pytest.mark.pty`` spawn a real child over a pty.  They are
# valuable (they prove the raw-mode path end to end) but slow and
# interactive, so they NEVER run in the default full suite.  The suite must
# stay fast; opt-in instead with:
#   python3 -m pytest --run-pty -v


def pytest_addoption(parser):
    parser.addoption(
        "--run-pty",
        action="store_true",
        default=False,
        help="run opt-in pty end-to-end tests (slow; off by default)",
    )


import pytest


def pytest_runtest_setup(item):
    if "pty" in item.keywords and not item.config.getoption("--run-pty"):
        pytest.skip("pty test is opt-in; rerun with --run-pty")
