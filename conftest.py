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
