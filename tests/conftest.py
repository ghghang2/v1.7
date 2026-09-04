"""Shared test fixtures.

The environment ships the ``httpx`` compatibility layer under the name
``httpx2``.  Tests import ``httpx`` directly, so alias it before any test
module is collected.
"""
import sys

try:
    import httpx  # noqa: F401
except ImportError:
    import httpx2

    sys.modules["httpx"] = httpx2
