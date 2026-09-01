# app/push_to_github.py
"""Push the local repository to GitHub -- with safety checks.

Improvements over the original implementation (which blindly committed
*everything* and always pushed to a hard-wired ``main`` branch of the
config's default repository):

* **Scoped commits.**  By default only *staged* changes are committed.
  Unstaged work is left untouched and reported back as ``unstaged`` so a
  caller can never silently sweep unrelated edits into a commit.  Pass
  ``stage_all=True`` to restore the old commit-everything behaviour.
* **Active branch.**  Pushes the branch HEAD is currently on instead of
  always ``main``.  ``rebase=True`` rebases onto the remote first.
* **Repo override.**  ``repo_name`` targets any repository the token can
  write to, which makes it a first-class way to create + seed a fresh
  repository without editing ``repo_config.yaml``.  When omitted, the
  repository the current ``origin`` already points at wins over the config
  default, so the tool stops dragging work into a stale default repo.
* **Test gate.**  Unless ``skip_tests`` is set, the pytest suite runs first
  and the push is refused on any failure/error.
* **Dry run.**  ``dry_run`` performs every read-only step (repo resolution,
  test run, dirty inspection) and reports the exact commit/push plan
  without mutating the working tree or the remote.

The function returns a JSON string for OpenAI function-calling:
``{"status": "success"|"dry_run"|"error", ...}``.
"""

import json
import logging
import re
import os
import subprocess
from pathlib import Path
from typing import Optional

from nbchat.core.remote import RemoteClient
# Import the config module lazily so we can reload it on each invocation.
import importlib
import nbchat.core.config as cfg

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_pytest(timeout: int = 300) -> dict:
    """Run ``pytest -q`` and return ``{"passed": n, "failed": n, "errors": n}``.

    Counts are taken from the ``-q`` summary line (e.g.
    ``134 passed, 2 failed, 1 error in 3.2s``).  Returns all-zero counts with
    ``failed=1`` if pytest itself crashes or times out, so the caller
    treats "cannot verify" as a gate failure rather than a green light.

    A nested-run guard (``NBCHAT_INNER_PYTEST``) refuses to recurse when this
    function is invoked from inside a pytest process it itself spawned.
    """
    if os.environ.get("NBCHAT_INNER_PYTEST"):
        return {"passed": 0, "failed": 1, "errors": 0,
                "note": "nested pytest run refused"}
    try:
        proc = subprocess.run(
            ["pytest", "-q"],
            capture_output=True, text=True, timeout=timeout, cwd=_REPO_ROOT,
            env={**os.environ, "NBCHAT_INNER_PYTEST": "1"},
        )
    except Exception as exc:  # pragma: no cover - defensive
        return {"passed": 0, "failed": 1, "errors": 0, "note": str(exc)}

    # The real summary line is the LAST line with counts; earlier occurrences
    # may be echoed inside failure tracebacks, so take the last match.
    tail = (proc.stdout or "")[-2000:]
    def _count(pattern: str) -> int:
        matches = re.findall(pattern, tail)
        return int(matches[-1]) if matches else 0

    return {
        "passed": _count(r"(\d+) passed"),
        "failed": _count(r"(\d+) failed"),
        "errors": _count(r"(\d+) error"),
    }


def _resolve_target(cfg_repo: str, remote_name: Optional[str],
                    repo_name: Optional[str]) -> tuple:
    """Decide which GitHub repo to push to.

    Returns ``(target, source)`` where ``source`` is one of
    ``"argument"``, ``"current_remote"`` or ``"config"``.
    """
    if repo_name:
        return repo_name, "argument"
    if remote_name and remote_name != cfg_repo:
        return remote_name, "current_remote"
    return cfg_repo, "config"


def push_to_github(
    commit_message: str = "Auto commit",
    rebase: bool = False,
    repo_name: Optional[str] = None,
    stage_all: bool = False,
    skip_tests: bool = False,
    dry_run: bool = False,
) -> str:
    """Push the current repository to GitHub with a commit message.

    Parameters
    ----------
    commit_message:
        Message for the commit.  If nothing is staged and ``stage_all`` is
        false the push proceeds without committing (nothing to commit).
    rebase:
        Rebase local work onto the remote branch before pushing.
    repo_name:
        Target repository (``user``-scoped, e.g. ``"v1.7"``).  Defaults to
        the repository the current ``origin`` points at, else
        ``repo_config.yaml``'s ``repo_name``.
    stage_all:
        Stage every change (tracked + untracked) before committing.  Default
        ``False`` commits staged changes only.
    skip_tests:
        Skip the pre-push test gate.
    dry_run:
        Run every read-only step and report the plan without committing or
        pushing.
    """
    try:
        # Reload the config module each call to pick up repo_config.yaml changes.
        importlib.reload(cfg)
        client = RemoteClient(Path("."))

        target, source = _resolve_target(cfg.REPO_NAME,
                                         client.remote_repo_name(), repo_name)

        # ---- 1. Test gate -------------------------------------------------
        tests = None if skip_tests else _run_pytest()
        if tests is not None and (tests["failed"] or tests["errors"]):
            return json.dumps({
                "status": "error",
                "error": "test gate failed - push refused",
                "tests": tests,
                "hint": "fix the failures or pass skip_tests=true to override",
            })

        # ---- 2. Resolve the target repository -----------------------------
        current = client.remote_repo_name()
        plan = {
            "repo": f"{client.user.login}/{target}",
            "repo_source": source,
            "branch": client.branch_name,
            "rebase": rebase,
            "tests": tests,
        }
        if current and current != target:
            plan["note"] = (f"origin currently points at {current}; "
                            f"the remote will be re-attached to {target}")

        # ---- 3. Inspect the working tree ----------------------------------
        unstaged = client.dirty_files(include_untracked=False)
        untracked = client.dirty_files(include_untracked=True)
        plan["unstaged"] = unstaged
        plan["untracked"] = untracked

        if dry_run:
            plan["staged"] = client.staged_files()
            plan["would_commit"] = bool(client.has_staged()) or (
                stage_all and (unstaged or untracked))
            plan["commit_message"] = commit_message
            return json.dumps({"status": "dry_run", **plan})

        # ---- 4. Commit (scoped) -------------------------------------------
        if stage_all:
            client.commit_all(commit_message)
            plan["committed"] = True
        elif client.has_staged():
            client.commit_staged(commit_message)
            plan["committed"] = True
        else:
            plan["committed"] = False
            # Note: a clean tree only means "nothing to commit" - the active
            # branch may still be ahead of the remote, so we always proceed to
            # push (a push that is already up to date is a harmless no-op).

        # ---- 5. Ensure the repo + remote + sync ---------------------------
        client.ensure_repo(target)
        client.attach_remote(target)
        if rebase:
            client.sync_from_remote(client.branch_name)

        # ---- 6. Push the active branch -------------------------------------
        client.push_branch(client.branch_name)
        return json.dumps({"status": "success", **plan})
    except Exception as exc:  # pragma: no cover - defensive
        return json.dumps({"error": str(exc), "type": type(exc).__name__})


# ---------------------------------------------------------------------------
# Tool Definition for OpenAI function-calling
# ---------------------------------------------------------------------------
func = push_to_github
name = "push_to_github"
description = (
    "Commit and push the current repository to GitHub. By default commits "
    "only STAGED changes (pass stage_all=true to commit everything), pushes "
    "the active branch, runs the pytest suite first and refuses to push on "
    "failures (override with skip_tests=true). Use repo_name to target a "
    "different repository and dry_run=true to preview without pushing."
)

schema = {
    "parameters": {
        "type": "object",
        "properties": {
            "commit_message": {"type": "string"},
            "rebase": {"type": "boolean"},
            "repo_name": {"type": "string"},
            "stage_all": {"type": "boolean"},
            "skip_tests": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
        },
        "required": [],
    }
}

__all__ = ["push_to_github", "func", "name", "description", "schema"]
