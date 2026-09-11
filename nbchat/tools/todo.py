"""Maintain a short, live task list for the current working session.

The agent calls this tool to create or update the task list it is working
toward.  The list is rendered as a live progress pill in the TUI status bar
and can be viewed in full with the ``/todos`` command.  The list is persisted
to a small JSON file (default ``~/.nbchat/todos.json``; override with
``NBCHAT_TODO_FILE``) so the UI and the tool share one source of truth.

The tool is intentionally stateless with respect to the session: it reads
and writes only the file, so it never blocks or crashes the agent loop on
an I/O hiccup.
"""
from __future__ import annotations

import json
import os

_MAX_ITEMS = 25


def todo_path() -> str:
    """The task-list file path (env-overridable for tests)."""
    return os.environ.get("NBCHAT_TODO_FILE") or os.path.join(
        os.path.expanduser("~"), ".nbchat", "todos.json")


def load_todos() -> list:
    """Load the current task list.  Never raises; returns ``[]`` on error."""
    try:
        with open(todo_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
    except Exception:
        pass
    return []


def save_todos(items: list) -> bool:
    """Persist the task list.  Returns True on success (best-effort)."""
    try:
        p = todo_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2)
        return True
    except Exception:
        return False


def _todo(items: list) -> str:
    """Set the task list.

    ``items`` is the FULL list: an array of objects ``{text, done}`` (bare
    strings are accepted and treated as not-done).  ``done: true`` marks a
    task complete.  An empty array ``[]`` clears the list.  (A client may
    also pass a JSON-encoded string, which is parsed defensively.)
    """
    data = items
    if not isinstance(data, list):
        try:
            data = json.loads(data)
        except Exception as exc:  # malformed -> tell the model to fix it
            return f"Failed to parse items: {exc}"
    if not isinstance(data, list):
        return "items must be an array of {text, done} objects."
    if len(data) > _MAX_ITEMS:
        return f"Too many items ({len(data)}); keep the list under {_MAX_ITEMS}."
    norm: list = []
    for it in data:
        if isinstance(it, str):
            norm.append({"text": it, "done": False})
        elif isinstance(it, dict):
            norm.append({"text": str(it.get("text", "")),
                         "done": bool(it.get("done", False))})
    if not save_todos(norm):
        return "Failed to save the task list (I/O error)."
    done = sum(1 for t in norm if t["done"])
    if not norm:
        return "Task list cleared."
    return (f"Task list updated: {done}/{len(norm)} done.\n"
            + json.dumps(norm, indent=2))


func = _todo
name = "todo"
description = (
    "Maintain the short task list you are working toward. Pass the FULL list "
    "each time as a JSON array of task objects, each with a text (str) field "
    "and a done (bool) field; done true marks a task complete. Keep it under "
    "~8 items and update it as you go so the user can watch progress in the "
    "status bar. Pass an empty array when the work is done."
)
__all__ = ["func", "name", "description", "todo_path", "load_todos", "save_todos"]