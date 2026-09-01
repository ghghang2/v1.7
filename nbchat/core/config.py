"""Applicationâ€wide configuration.

All runtime configuration is now loaded from :file:`repo_config.yaml` located in the
repository root. The file is parsed once at import time and the resulting values
populate a set of constants that other modules import.

The module keeps a small fallback dictionary for unit tests that may run in an
environment where the YAML file is absent.  The defaults match the historic
hardâ€coded values from the original code base.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Tuple

# ---------------------------------------------------------------------------
#  Load configuration from YAML
# ---------------------------------------------------------------------------
_LOGGER = logging.getLogger(__name__)

# Path to repo_config.yaml â€” three levels up from this file
_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "repo_config.yaml"

try:
    import yaml
except Exception:  # pragma: no cover â€” yaml is a normal dependency
    _LOGGER.warning("PyYAML not available â€” using empty config")
    yaml = None


def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as exc:  # pragma: no cover
        _LOGGER.warning("Failed to load %s: %s", path, exc)
        return {}

_cfg: Dict[str, Any] = _load_yaml(_CONFIG_PATH)

SERVER_URL: str = str(_cfg["SERVER_URL"])
MODEL_NAME: str = str(_cfg["MODEL_NAME"])
DEFAULT_SYSTEM_PROMPT: str = str(_cfg["DEFAULT_SYSTEM_PROMPT"])

USER_NAME: str = str(_cfg["user_name"])
REPO_NAME: str = str(_cfg["repo_name"])
TAIL_MESSAGES: int = int(_cfg["tail_len"])
MAX_TOOL_OUTPUT_CHARS: int = int(_cfg["max_tool_output_chars"])
MAX_HISTORY_TURNS: int = int(_cfg["max_history_turns"])
# Real token budget for LLM completions (not a char count).
MAX_LLM_OUTPUT_TOKENS: int = int(_cfg.get("max_llm_output_tokens", 8192))

# Conversation loop constants
MAX_TOOL_TURNS: int = int(_cfg["max_tool_turns"])
STALL_TURNS: int = int(_cfg["stall_turns"])

PORT: int = int(_cfg["port"])
N_PARALLEL: int = int(_cfg["n_parallel"])
CTX_SIZE: int = int(_cfg["ctx_size"])
N_GPU_LAYERS: int = int(_cfg["n_gpu_layers"])
SERVICE_INFO_PATH: str = str(_cfg["service_info_path"])
LLAMA_LOG_PATH: str = str(_cfg["llama_log_path"])

IGNORED_ITEMS: list[str] = list(_cfg["IGNORED_ITEMS"])

SUMMARY_PROMPT: str = str(_cfg["SUMMARY_PROMPT"])

# L2 retrieval and context management
L2_RETRIEVAL_LIMIT: int = int(_cfg["l2_retrieval_limit"])
CORE_MEMORY_ACTIVE_ENTITIES_LIMIT: int = int(_cfg["core_memory_active_entities_limit"])
CORE_MEMORY_ERROR_HISTORY_LIMIT: int = int(_cfg["core_memory_error_history_limit"])
SUMMARIZER_TOOL_CHARS: int = int(_cfg["summarizer_tool_chars"])

# Compression parameters
LOSSLESS_WINDOW: int = int(_cfg["lossless_window"])

# Context management parameters
CONTEXT_BUDGET: int = CTX_SIZE//N_PARALLEL
CONTEXT_HEADROOM: float = float(_cfg["context_headroom_ratio"]) 
PREFIX_TOKEN_RESERVE: int = int(_cfg["prefix_token_reserve"])
PERSIST_FRACTION: float = float(_cfg["persist_fraction"]) 

# Retry parameters
DEFAULT_MAX_RETRIES: int = int(_cfg["max_retries"])
DEFAULT_INITIAL_DELAY: float = float(_cfg["initial_delay"])
DEFAULT_MAX_DELAY: float = float(_cfg["max_delay"])
DEFAULT_BACKOFF_MULTIPLIER: float = float(_cfg["backoff_multiplier"])

# Monitoring thresholds
_LOG_TAIL_BYTES: int = int(_cfg["log_tail_bytes"])
_REREAD_RATE_THRESHOLD: float = float(_cfg["reread_rate_threshold"])
_ERROR_COMPRESSION_THRESHOLD: float = float(_cfg["error_compression_threshold"])
_LLM_FAILURE_THRESHOLD: float = float(_cfg["llm_failure_threshold"])
_NO_OUTPUT_THRESHOLD: float = float(_cfg["no_output_threshold"])
_POOR_RATIO_THRESHOLD: float = float(_cfg["poor_ratio_threshold"])
_LOW_SIM_THRESHOLD: float = float(_cfg["low_sim_threshold"])
_HIGH_INVALIDATION_THRESHOLD: float = float(_cfg["high_invalidation_threshold"])

# Email parameters
SMTP_PORT: int = int(_cfg["smtp_port"])
EMAIL_POLL_INTERVAL: int = int(_cfg.get("email_poll_interval", 3))
EMAIL_AUTO_REPLY: bool = bool(_cfg.get("email_auto_reply", True))

# Supervisor parameters
# The supervisor is a second, always-on LLM instance that runs on the
# server's second parallel slot (n_parallel >= 2).  It answers state
# questions and periodically reviews the assistant's work, interjecting
# when it believes the assistant is off track.
SUPERVISOR_ENABLED: bool = bool(_cfg.get("supervisor_enabled", False))
SUPERVISOR_INTERVAL: int = int(_cfg.get("supervisor_interval", 60))
SUPERVISOR_COOLDOWN: int = int(_cfg.get("supervisor_cooldown", 300))
SUPERVISOR_MAX_OUTPUT_TOKENS: int = int(_cfg.get("supervisor_max_output_tokens", 512))

# Voice channel (Alfred) — laptop bridge over an SSH tunnel.
VOICE_ENABLED: bool = bool(_cfg.get("voice_enabled", False))
VOICE_PORT: int = int(_cfg.get("voice_port", 8765))
VOICE_STATUS_MIN_INTERVAL: int = int(_cfg.get("voice_status_min_interval", 300))

# UI parameters
MAX_VISIBLE_WIDGETS: int = int(_cfg["max_visible_widgets"])

# Timeout parameters (in seconds)
BROWSER_TIMEOUT: int = int(_cfg["browser_timeout"])
TESTS_TIMEOUT: int = int(_cfg["tests_timeout"])
OTHER_TOOLS_TIMEOUT: int = int(_cfg["other_tools_timeout"])

# Browser default parameters (in milliseconds)
DEFAULT_NAVIGATION_TIMEOUT: int = int(_cfg["default_navigation_timeout"])
DEFAULT_ACTION_TIMEOUT: int = int(_cfg["default_action_timeout"])
DEFAULT_MAX_CONTENT_LENGTH: int = int(_cfg["default_max_content_length"])

__all__ = [
    # Existing exports
    "SERVER_URL",
    "MODEL_NAME",
    "DEFAULT_SYSTEM_PROMPT",
    "USER_NAME",
    "REPO_NAME",
    "CONTEXT_TOKEN_THRESHOLD",
    "TAIL_MESSAGES",
    "MAX_TOOL_OUTPUT_CHARS",
    "MAX_LLM_OUTPUT_TOKENS",
    "MAX_HISTORY_TURNS",
    # "WINDOW_TURNS",
    # "MAX_WINDOW_ROWS",
    # "MAX_EXCHANGES",
    # "KEEP_RECENT_EXCHANGES",
    "MAX_TOOL_TURNS",
    "STALL_TURNS",
    "PORT",
    "N_PARALLEL",
    "CTX_SIZE",
    "N_GPU_LAYERS",
    "SERVICE_INFO_PATH",
    "LLAMA_LOG_PATH",
    "IGNORED_ITEMS",
    "SUMMARY_PROMPT",
    # L2 retrieval and context management
    # "L2_WRITE_THRESHOLD",
    "L2_RETRIEVAL_LIMIT",
    # "L2_MIN_IMPORTANCE_FOR_RETRIEVAL",
    "CORE_MEMORY_ACTIVE_ENTITIES_LIMIT",
    "CORE_MEMORY_ERROR_HISTORY_LIMIT",
    "SUMMARIZER_TOOL_CHARS",
    # Compression parameters
    "LOSSLESS_WINDOW",
    # NEW context management parameters
    "CONTEXT_HEADROOM",
    "PREFIX_TOKEN_RESERVE",
    "PERSIST_FRACTION",
    # Retry parameters
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_INITIAL_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_BACKOFF_MULTIPLIER",
    # Monitoring thresholds
    "_LOG_TAIL_BYTES",
    "_REREAD_RATE_THRESHOLD",
    "_ERROR_COMPRESSION_THRESHOLD",
    "_LLM_FAILURE_THRESHOLD",
    "_NO_OUTPUT_THRESHOLD",
    "_POOR_RATIO_THRESHOLD",
    "_LOW_SIM_THRESHOLD",
    "_HIGH_INVALIDATION_THRESHOLD",
    # Email parameters
    "SMTP_PORT",
    "EMAIL_POLL_INTERVAL",
    "EMAIL_AUTO_REPLY",
    "SUPERVISOR_ENABLED",
    "SUPERVISOR_INTERVAL",
    "SUPERVISOR_COOLDOWN",
    "SUPERVISOR_MAX_OUTPUT_TOKENS",
    # Voice channel (Alfred)
    "VOICE_ENABLED",
    "VOICE_PORT",
    "VOICE_STATUS_MIN_INTERVAL",
    # UI parameters
    "MAX_VISIBLE_WIDGETS",
    # Timeout parameters
    "BROWSER_TIMEOUT",
    "TESTS_TIMEOUT",
    "OTHER_TOOLS_TIMEOUT",
    # Browser default parameters
    "DEFAULT_NAVIGATION_TIMEOUT",
    "DEFAULT_ACTION_TIMEOUT",
    "DEFAULT_MAX_CONTENT_LENGTH",
]