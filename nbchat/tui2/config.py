"""Persisted user settings for TUI v2/v3.

A small, best-effort JSON settings file (default
``~/.nbchat/tui3.json``; override with ``NBCHAT_TUI3_CONFIG``) holds a few
user preferences so they survive across sessions.  Loading and saving are
defensive — any I/O error is swallowed so the TUI can never be blocked by
a config problem.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

_DEFAULTS: Dict[str, Any] = {
    "thinking_visible": True,
    "notify_toasts": True,
    "notify_bel": True,
    "notify_sound": False,
    "approval_enabled": True,
    "risky_tools": ["run_command", "push_to_github", "send_email"],
    "scroll_tick": 3,
}


def path() -> str:
    """The settings file path (env-overridable for tests)."""
    return os.environ.get("NBCHAT_TUI3_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".nbchat", "tui3.json")


def load() -> Dict[str, Any]:
    """Load settings, merged over the defaults.  Never raises."""
    cfg = dict(_DEFAULTS)
    try:
        with open(path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in _DEFAULTS:
                if k in data:
                    cfg[k] = data[k]
    except Exception:
        pass
    return cfg


def save(cfg: Dict[str, Any]) -> bool:
    """Persist the known settings keys.  Returns True on success."""
    try:
        p = path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        known = {k: cfg.get(k, _DEFAULTS[k]) for k in _DEFAULTS}
        with open(p, "w", encoding="utf-8") as f:
            json.dump(known, f, indent=2, sort_keys=True)
        return True
    except Exception:
        return False
