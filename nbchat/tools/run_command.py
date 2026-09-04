"""Tool that executes a shell command and returns its output.

This module exposes a single callable named ``func`` – the tools/__init__ loader looks for that attribute. The module also supplies name and description attributes so that the tool can be discovered automatically and the OpenAI function‑calling schema can be built.

The public API of this module is intentionally tiny:
* ``func`` – the function that implements the tool
* ``name`` – the name the model will use to refer to the tool
* ``description`` – a short human‑readable description

The function returns a **JSON string**.  On success it contains a
``stdout``, ``stderr`` and ``exit_code`` key; on failure it contains an
``error`` key.  The format matches the expectations of the OpenAI
function‑calling workflow present in :mod:`app.chat`.

The module is deliberately free of side‑effects and does not depend
on any external configuration – it only needs the repository root,
which is derived from the location of this file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path
from typing import Dict, Optional

# ---------------------------------------------------------------------------

def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``repo_root`` and ensure the result
    does **not** escape the repository root (prevents directory traversal).
    """
    target = (repo_root / rel_path).resolve()
    if not str(target).startswith(str(repo_root)):
        raise ValueError("Path escapes repository root")
    return target

# ---------------------------------------------------------------------------

def _run_command(command: str, cwd: Optional[str] = None) -> str:
    """Execute ``command`` in the repository root and return a JSON string with:
        * ``stdout``
        * ``stderr``
        * ``exit_code``
    Any exception is converted to an error JSON.

    The ``cwd`` argument is accepted for backward compatibility but
    ignored; the command is always executed in the repository root.
    """
    try:
        repo_root = Path(__file__).resolve().parents[2]
        target_dir = repo_root

        # Hard wall-clock ceiling (see issues.md): a hung child used to
        # block the tool-execution thread forever, wedging the agent.
        # start_new_session puts the shell in its own process group so
        # the timeout path can kill the shell AND everything it spawned.
        timeout = int(os.environ.get("NBCAT_TOOL_TIMEOUT", 60))
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(target_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            out, err = proc.communicate()
            return json.dumps({
                "error": (
                    f"command timed out after {timeout}s and was killed; "
                    f"the command is probably waiting on input or a lock"
                ),
                "stdout": (out or "")[-4000:],
                "stderr": (err or "")[-2000:],
            })
        result: Dict[str, str | int] = {
            "stdout": out,
            "stderr": err,
            "exit_code": proc.returncode,
        }
        return json.dumps(result)

    except Exception as exc:
        return json.dumps({"error": str(exc)})

# ---------------------------------------------------------------------------

func = _run_command
name = "run_command"
description = (
    "Execute a shell command within the repository root and return the stdout, stderr and exit code. Returns a JSON string with either the result keys or an ``error`` key on failure."
)
__all__ = ["func", "name", "description"]
