# app/tools/create_file.py
"""
Tool that creates a new file under the repository root.

This module exposes a **single callable** named ``func`` – the
``tools/__init__`` loader looks for that attribute (or falls back to the
first callable in the module).  The module also supplies ``name`` and
``description`` attributes so that the tool can be discovered
automatically and the OpenAI function‑calling schema can be built.

The public API of this module is intentionally tiny:
* ``func`` – the function that implements the tool
* ``name`` – the name the model will use to refer to the tool
* ``description`` – a short human‑readable description

The function returns a **JSON string**.  On success it contains a
``result`` key; on failure it contains an ``error`` key.  The format
matches the expectations of the OpenAI function‑calling workflow
present in :mod:`app.chat`.

The module is deliberately free of side‑effects and does not depend
on any external configuration – it only needs the repository root,
which is derived from the location of this file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #

def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """
    Resolve ``rel_path`` against ``repo_root`` and ensure the result
    does **not** escape the repository root (prevents directory traversal).
    """
    # Resolve the target path and ensure it stays within the repository
    target = (repo_root / rel_path).resolve()
    try:
        # Path.is_relative_to is available on Python 3.9+
        if not target.is_relative_to(repo_root):
            raise ValueError("Path escapes repository root")
    except AttributeError:
        # Fallback for older Python versions
        if not str(target).startswith(str(repo_root)):
            raise ValueError("Path escapes repository root")
    return target


# --------------------------------------------------------------------------- #
#  The actual tool implementation
# --------------------------------------------------------------------------- #

def _create_file(path: str, content: str) -> str:
    """
    Create a new file at ``path`` (relative to the repository root)
    with the supplied ``content``.

    Parameters
    ----------
    path
        File path relative to the repo root.  ``path`` may contain
        directory separators but **must not** escape the root.
    content
        Raw text to write into the file.

    Returns
    -------
    str
        JSON string.  On success:

        .. code-block:: json

            { "result": "File created: <path>" }

        On failure:

        .. code-block:: json

            { "error": "<exception message>" }
    """
    try:
        # ``app/tools`` -> ``app`` -> repository root
        # parents[0] = file, [1] = tools, [2] = nbchat, [3] = repo root
        repo_root = Path(__file__).resolve().parents[2]
        # Resolve target path and guard against directory traversal
        target = _safe_resolve(repo_root, path)

        # Ensure the parent directory exists
        target.parent.mkdir(parents=True, exist_ok=True)

        # Write the file
        target.write_text(content, encoding="utf-8")

        return json.dumps({"result": f"File created: {path}"})
    except Exception as exc:
        # Any exception is surfaced as an error JSON
        return json.dumps({"error": str(exc)})


# --------------------------------------------------------------------------- #
#  Public attributes for auto‑discovery
# --------------------------------------------------------------------------- #

# ``tools/__init__`` expects the module to expose a ``func`` attribute.
func = _create_file

# Optional, but helpful for humans and for the OpenAI schema
name = "create_file"
description = (
    "Create a new file under the repository root.  Returns a JSON string "
    "with either a `result` key on success or an `error` key on failure."
)

# The module's ``__all__`` is intentionally tiny – we only export what
# is needed for the tool discovery logic.
__all__ = ["func", "name", "description"]