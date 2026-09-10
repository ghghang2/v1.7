"""Single executor for all tool calls."""
from __future__ import annotations

import contextvars
import re
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict
import json

from nbchat.tools import TOOLS
from nbchat.core.retry import (
    retry_with_backoff,
    DEFAULT_MAX_RETRIES,
    HangError,
    is_hang,
)
import nbchat.core.config as config

_executor = ThreadPoolExecutor(max_workers=4)

# ============================================================================
# Idempotent read cache (optimization roadmap #2b)
# ============================================================================
# The task tracker's redundancy report showed ~48% of tool calls were
# re-reads of unchanged state (same file / same command issued again).
# Reads dominate observed waste and are the cheapest tokens an agent can
# waste.  This cache serves *read-only* run_command calls from a bounded
# LRU when the same (normalized) command is re-issued, and invalidates
# every entry the moment a mutating tool executes.  It deliberately only
# uses a conservative CLOSED allow-list: only well-known read-only verbs
# are ever cached.  Anything the classifier does not certify as a read
# (unknown verb, shell metacharacters, path invocation, sed -i, ...) is
# treated as a potential mutation and FLUSHES the whole cache, so a stale
# entry can never be served and a write can never hide behind a cached
# read.

_READ_VERBS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "rg",
    "sort", "uniq", "diff", "file", "stat", "tree",
    "pwd", "whoami", "date", "df", "du", "which",
})
# git subcommands that are pure reads regardless of their flags.
_GIT_READ_SUBCOMMANDS = frozenset({
    "status", "log", "diff", "show", "ls-files",
    "rev-parse", "describe", "cat-file", "shortlog",
})

_READ_CACHE_MAX = 64
_READ_CACHE: "OrderedDict[str, str]" = OrderedDict()


def _normalize_command(command: str) -> str:
    """Lower-case, collapse whitespace, drop trailing comment/quotes."""
    text = re.sub(r"\s+", " ", command).strip()
    text = re.sub(r"\s*(#.*)?$", "", text).strip()
    if text.endswith(("'", '"')) and text[0] == text[-1]:
        text = text[1:-1]
    return text.lower()


def is_readable_command(command: Any) -> bool:
    """True only when ``command`` is a plain single invocation of a known
    read-only verb (see ``_READ_VERBS`` / ``_GIT_READ_SUBCOMMANDS``).

    Conservative on purpose: shell metacharacters, path/script invocations,
    and any verb outside the allow-list all return False.  The caller
    flushes the read cache whenever a non-certified command runs, so the
    cache can never serve stale state after a write it did not recognise.
    """
    if not isinstance(command, str) or not command:
        return False
    if any(ch in command for ch in ("|", "&", ">", "<", "`", "$", ";")):
        return False
    if command[0] in ("-", "!", ".", "/"):
        return False
    tokens = command.split()
    if not tokens:
        return False
    verb = tokens[0].lower()
    if verb == "git":
        return (
            len(tokens) >= 2
            and tokens[1].lower() in _GIT_READ_SUBCOMMANDS
        )
    if verb == "sed":
        # sed -i edits in place: a mutation in disguise.
        return not any(t.startswith("-i") for t in tokens[1:])
    return verb in _READ_VERBS


def _cache_lookup(key: str) -> str | None:
    value = _READ_CACHE.get(key)
    if value is not None:
        _READ_CACHE.move_to_end(key)
    return value


def _cache_store(key: str, value: str) -> None:
    _READ_CACHE[key] = value
    _READ_CACHE.move_to_end(key)
    while len(_READ_CACHE) > _READ_CACHE_MAX:
        _READ_CACHE.popitem(last=False)


def invalidate_read_cache() -> None:
    """Drop all cached reads.  Called after any mutating tool succeeds:
    once the agent writes to the tree, previously-read state may be stale."""
    _READ_CACHE.clear()


_MUTATING_TOOLS = frozenset({
    "create_file", "make_change_to_file", "push_to_github",
    "browser", "send_email",
})


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

    # --- Idempotent read cache (see header) --------------------------------
    if (tool_name == "run_command"
            and not args.get("cwd")
            and is_readable_command(args.get("command"))):
        cache_key = _normalize_command(args.get("command"))
        cached = _cache_lookup(cache_key)
        if cached is not None:
            return (
                '[cached read-only output, served from the idempotent '
                f'cache; rerun WITHOUT this note if you mutated '
                f'state]\n{cached}'
            )
    elif (
        tool_name == "run_command"
        and not args.get("cwd")
        and not is_readable_command(args.get("command"))
    ):
        # A run_command the classifier did NOT certify as a read may have
        # mutated the tree (mv, python -c, ./script.sh, ...).  Flush every
        # cached read so nothing stale can be served afterwards.
        invalidate_read_cache()

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
        # Context is propagated to the worker thread (it was NOT doing
        # this before): tools that read team-mode ContextVars -- notably
        # delegate_task reading nbchat.core.team._current_delegation,
        # set on the worker pool's claimer thread -- saw the default
        # (None) here and reported "delegation unavailable" during a
        # perfectly live team run, collapsing it to a single worker.
        future = _executor.submit(contextvars.copy_context().run, func, **args)
        try:
            return str(future.result(timeout=timeout))
        except TimeoutError:
            # Keep a done-callback so the pool doesn't silently drop it.
            future.add_done_callback(lambda _f: None)
            # A wall-clock timeout is a HANG, not a transient failure.  Raise
            # HangError so the retry layer abandons it on this attempt instead
            # of re-hanging for every retry (the markdown.py failure mode).
            # The message is kept actionable so the model can change approach.
            raise HangError(
                f"Tool '{tool_name}' timed out after {timeout} seconds (hung — "
                f"likely an infinite loop or blocking call). Do not repeat the "
                f"same call; break the work into a smaller step or use a "
                f"non-blocking alternative.",
                tool=tool_name,
            )
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

        # Store successful reads; invalidate everything on mutating tools.
        if (tool_name == "run_command"
                and not args.get("cwd")
                and is_readable_command(args.get("command"))):
            try:
                parsed = json.loads(result)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("exit_code") == 0 \
                    and not str(parsed.get("error", "")).strip():
                _cache_store(cache_key, result)
        elif tool_name in _MUTATING_TOOLS:
            invalidate_read_cache()

        return result
    except Exception as e:
        from nbchat.core.retry import HangError as _HangError

        if isinstance(e, _HangError):
            # A hang was NOT retried (see retry.HangError) — report that truthfully.
            return f"Tool '{tool_name}' aborted (hung, no retry): {e}"
        return f"Tool '{tool_name}' failed after {DEFAULT_MAX_RETRIES} retries: {e}"


__all__ = [
    "run_tool",
    "trim_tool_output",
    "is_readable_command",
    "invalidate_read_cache",
    "_READ_CACHE",
]