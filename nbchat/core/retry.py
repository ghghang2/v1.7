"""Retry policy for tool calls and API failures.

Inspired by openclaw's retry policy (https://docs.openclaw.ai/concepts/retry).

This module provides retry mechanisms for:
1. Tool calls that fail due to transient errors
2. API calls that experience network issues
3. Operations that may succeed on retry

Retry Strategy:
- Exponential backoff with jitter
- Maximum retry attempts configurable
- Different retry policies for different error types
- Logging for debugging and monitoring
"""

from __future__ import annotations

import logging
import random
import re
import time
from typing import Callable, Any, Optional, TypeVar

_log = logging.getLogger("nbchat.retry")

# Retry configuration
DEFAULT_MAX_RETRIES = 2
DEFAULT_INITIAL_DELAY = 0.5  # seconds
DEFAULT_MAX_DELAY = 5.0  # seconds
DEFAULT_BACKOFF_MULTIPLIER = 2.0

# Hang guard: a tool call that stays silent for this long (wall-clock) is
# assumed hung (e.g. an infinite loop) and is abandoned so the session can
# continue instead of stalling.  Kept generous on purpose — a legitimately
# slow-but-finite tool must be able to complete within this window.
HANG_TIMEOUT = 120.0  # seconds

# Error types that should be retried
RETRIFIABLE_ERRORS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "temporarily",
    "retry",
    "busy",
    "rate limit",
    "503",
    "502",
    "504",
)

# Error types that should NOT be retried (permanent failures)
NON_RETRIFIABLE_ERRORS = (
    "not found",
    "forbidden",
    "permission",
    "invalid",
    "authentication",
    "authorization",
    "404",
    "401",
    "403",
)


class NonRetryableError(Exception):
    """Deterministic failure — skip retry and propagate immediately."""


class HangError(Exception):
    """A tool call exceeded its wall-clock budget and was abandoned.

    A hang is *not* transient — retrying an infinite loop just hangs again.
    The retry layer treats this as non-retryable so the session fails fast
    and continues instead of spending retry attempts on a tool that will
    never return.
    """

    def __init__(self, message: str, tool: str | None = None):
        super().__init__(message)
        self.tool = tool


def _is_retryable(error_message: str) -> bool:
    """Check if an error is retryable based on error message."""
    error_lower = error_message.lower()
    
    # Check for non-retryable errors first
    for pattern in NON_RETRIFIABLE_ERRORS:
        if pattern in error_lower:
            return False
    
    # A wall-clock timeout of a tool call is a HANG, not a transient network
    # condition.  Treating it as retryable made a hung tool spend every retry
    # attempt re-hanging (the markdown.py failure mode).  Fail fast instead.
    if is_hang(error_lower):
        return False

    # Check for retryable errors
    for pattern in RETRIFIABLE_ERRORS:
        if pattern in error_lower:
            return True
    
    # Default: do NOT retry.  Unknown errors are usually deterministic
    # (bad input, missing selector, 4xx); retrying them with exponential
    # backoff only adds seconds to minutes of dead wall time, which this
    # harness is built to avoid.  Only clearly transient failures above
    # are retried.
    return False


# A tool call exhausting its per-tool wall-clock budget is a hang even in the
# bare timeout wording ("Tool 'name' timed out after N seconds.").  The
# "Tool '...' timed out" prefix is the tool executor's signature, unambiguous
# that a *tool* (not the network) never returned.
_TOOL_TIMEOUT_RE = re.compile(r"tool\s+'[^']*'\s+timed out after")


def is_hang(error_message: str) -> bool:
    """True when an error is a wall-clock hang (a call that never returned),
    as opposed to a transient failure that a retry could recover from.

    Used both by :func:`_is_retryable` (to fail fast) and by the session
    layer (to fire the hang-continuity guard).
    """
    m = error_message.lower() if isinstance(error_message, str) else ""
    if "hung" in m or "hang" in m or "wall-clock" in m:
        return True
    # Per-tool wall-clock timeout (tool executor signature).  A bare
    # "timed out" without this prefix may be a transient network timeout that
    # a retry could recover from, so it is NOT treated as a hang.
    return bool(_TOOL_TIMEOUT_RE.search(m))


def _calculate_delay(attempt: int, initial_delay: float, max_delay: float, 
                     backoff_multiplier: float) -> float:
    """Calculate delay with exponential backoff and jitter."""
    delay = min(initial_delay * (backoff_multiplier ** attempt), max_delay)
    # Add jitter to prevent thundering herd
    jitter = random.uniform(0.5, 1.5)
    return delay * jitter


T = TypeVar('T')

def retry(
    func: Callable[..., T],
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
) -> Callable[..., T]:
    """Decorator to add retry logic to a function.
    
    Args:
        func: Function to decorate
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_multiplier: Multiplier for exponential backoff
        on_retry: Optional callback when retry occurs (attempt, error, next_delay)
    
    Returns:
        Decorated function with retry logic
    """
    def wrapper(*args: Any, **kwargs: Any) -> T:
        last_error: Optional[Exception] = None
        
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                last_error = e
                if isinstance(e, NonRetryableError):
                    raise
                if isinstance(e, HangError):
                    # A hung tool call will hang again — no retry can fix it.
                    _log.warning(
                        f"Hang detected in {func.__name__}; not retrying. {e}"
                    )
                    raise
                error_msg = str(e).lower()
                
                if not _is_retryable(error_msg):
                    _log.debug(f"Not retryable: {error_msg}")
                    raise
                
                if attempt < max_retries:
                    delay = _calculate_delay(
                        attempt, initial_delay, max_delay, backoff_multiplier
                    )
                    _log.info(
                        f"Retry {attempt + 1}/{max_retries} for {func.__name__}: "
                        f"{e}. Waiting {delay:.2f}s..."
                    )
                    if on_retry:
                        on_retry(attempt + 1, str(e), delay)
                    time.sleep(delay)
                else:
                    _log.warning(
                        f"Max retries ({max_retries}) exceeded for {func.__name__}"
                    )
        
        raise last_error  # type: ignore
    
    return wrapper


def retry_with_backoff(
    func: Callable[..., T],
    *args: Any,
    max_retries: int = DEFAULT_MAX_RETRIES,
    initial_delay: float = DEFAULT_INITIAL_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    backoff_multiplier: float = DEFAULT_BACKOFF_MULTIPLIER,
    on_retry: Optional[Callable[[int, str, float], None]] = None,
    **kwargs: Any,
) -> T:
    """Execute a function with retry logic and exponential backoff.
    
    Args:
        func: Function to execute
        args: Positional arguments for func
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay in seconds
        max_delay: Maximum delay in seconds
        backoff_multiplier: Multiplier for exponential backoff
        on_retry: Optional callback when retry occurs
        kwargs: Keyword arguments for func
    
    Returns:
        Result of func
    
    Raises:
        Exception: If all retries fail
    """
    last_error: Optional[Exception] = None
    
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if isinstance(e, NonRetryableError):
                raise
            if isinstance(e, HangError):
                # A hung tool call will hang again — no retry can fix it.
                _log.warning(
                    f"Hang detected in {func.__name__}; not retrying. {e}"
                )
                raise
            error_msg = str(e).lower()
            
            if not _is_retryable(error_msg):
                _log.debug(f"Not retryable: {error_msg}")
                raise
            
            if attempt < max_retries:
                delay = _calculate_delay(
                    attempt, initial_delay, max_delay, backoff_multiplier
                )
                _log.info(
                    f"Retry {attempt + 1}/{max_retries} for {func.__name__}: "
                    f"{e}. Waiting {delay:.2f}s..."
                )
                if on_retry:
                    on_retry(attempt + 1, str(e), delay)
                time.sleep(delay)
            else:
                _log.warning(
                    f"Max retries ({max_retries}) exceeded for {func.__name__}"
                )
    
    raise last_error  # type: ignore


__all__ = [
    "retry",
    "retry_with_backoff",
    "NonRetryableError",
    "HangError",
    "is_hang",
    "DEFAULT_MAX_RETRIES",
]