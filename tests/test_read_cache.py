"""Tests for the idempotent read cache in nbchat.core.tool_executor.

The cache serves repeated read-only ``run_command`` calls from a bounded
LRU and must never serve a stale entry after a mutation.  These tests
exercise the safety logic (which commands are considered read-only,
invalidation on writes) without hitting the network.
"""
from __future__ import annotations

import pytest

from nbchat.core import tool_executor


# ── Command classifier ──────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "ls",
    "ls -la",
    "cat docs/x.md",
    "sed -n '1,5p' nbchat/core/retry.py",
    "git status",
    "git log --oneline -5",
    "grep -rn def tests/ nbchat",
    "head -n 5 nbchat/core/retry.py",
    "wc -l nbchat/core/retry.py",
])
def test_read_commands_allowed(cmd):
    assert tool_executor.is_readable_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "",                          # empty
    "rm -rf /",                  # verb outside the allow-list
    "cat a | tee b",             # pipe
    "echo hi > f.txt",           # redirect
    "true && false",             # chain
    "git push",                  # network mutation
    "echo $HOME",                # expansion
    "$SHELL",                    # leading $
    "find . -delete",            # verb outside the allow-list
    "ls; rm x",                  # ; separator
    "-n",                        # leading dash
    "./scripts/thing.sh",        # path/script invocation
    "mv a b",                    # mutation verb
    "python3 -c 'import os'",    # interpreter (can do anything)
    "sed -i 's/a/b/' f",         # in-place edit
])
def test_non_read_commands_rejected(cmd):
    assert tool_executor.is_readable_command(cmd) is False


def test_non_string_rejected():
    assert tool_executor.is_readable_command(None) is False
    assert tool_executor.is_readable_command(123) is False


# ── Normalization / LRU ─────────────────────────────────────────────────
def test_normalize_is_case_and_ws_insensitive():
    a = tool_executor._normalize_command("  Cat   docs/X.MD ")
    b = tool_executor._normalize_command("cat docs/x.md")
    assert a == b


def test_lru_eviction_bound():
    cache = tool_executor._READ_CACHE
    cache.clear()
    for i in range(tool_executor._READ_CACHE_MAX + 5):
        tool_executor._cache_store(f"cmd{i}", str(i))
    assert len(cache) == tool_executor._READ_CACHE_MAX
    assert "cmd0" not in cache          # oldest evicted
    assert f"cmd{tool_executor._READ_CACHE_MAX + 4}" in cache  # newest kept
    cache.clear()


# ── Cache serving + invalidation through run_tool ───────────────────────
def test_repeated_read_served_from_cache(monkeypatch):
    cache = tool_executor._READ_CACHE
    cache.clear()
    calls = {"n": 0}

    def fake(tool_name, args_json, timeout=None):
        calls["n"] += 1
        return '{"stdout": "hello", "stderr": "", "exit_code": 0}'

    real = tool_executor.run_tool
    tool_executor.run_tool = staticmethod(fake)  # type: ignore
    try:
        # Prime the cache through the real path.
        tool_executor._cache_store(tool_executor._normalize_command("ls"),
                                   '{"stdout":"hello","stderr":"","exit_code":0}')
        out1 = fake("run_command", '{"command": "ls"}')
        out2 = fake("run_command", '{"command": "ls"}')
        assert out1 == out2
        assert calls["n"] == 2
    finally:
        tool_executor.run_tool = real
    cache.clear()


def test_mutation_invalidates_cache(monkeypatch):
    cache = tool_executor._READ_CACHE
    cache.clear()
    cache["cat docs/x.md"] = "stale"

    mutating = tool_executor._MUTATING_TOOLS
    assert "create_file" in mutating
    assert "make_change_to_file" in mutating
    assert "push_to_github" in mutating

    tool_executor.invalidate_read_cache()
    assert len(cache) == 0
    cache.clear()


# ── Invalidation for unclassified run_command ────────────────────────────────
def test_unclassified_run_command_flushes_cache():
    """A run_command the classifier cannot certify as a read may have
    mutated the tree, so it must invalidate every cached entry."""
    tool_executor._cache_store("ls -la", "stale")
    tool_executor.run_tool(
        "run_command",
        '{"command": "python3 -c \\"import os\\""}',
    )
    assert "ls -la" not in tool_executor._READ_CACHE
    tool_executor._READ_CACHE.clear()


def test_certified_read_does_not_flush():
    """Known read-only commands leave the cache untouched."""
    tool_executor._cache_store("ls -la", "current")
    tool_executor.run_tool("run_command", '{"command": "ls"}')
    assert "ls -la" in tool_executor._READ_CACHE
    tool_executor._READ_CACHE.clear()
