"""tui3 checkpoint / undo — safe, git-backed code revert.

A checkpoint is a *record* of the current working-tree state that the user
can later restore to, so an agent's file edits can be reverted.  It is
self-contained and safe by construction:

* A checkpoint is produced with ``git stash create`` — a commit object that
  captures the current working-tree state of **tracked** files.  That call
  does **not** modify the working tree.  If the tree is clean it falls back
  to the current ``HEAD``.  No history is rewritten and nothing is deleted.
* ``/undo`` restores tracked files to a checkpoint with
  ``git restore --source=<sha> --staged --worktree .``.  That touches only
  tracked files: it never deletes untracked files and is itself reversible
  (a fresh checkpoint before the restore is all that is needed to go back).
* Every restore is previewable (``/undo`` with no label is *preview only*;
  applying requires naming a checkpoint), and a checkpoint's recorded cwd
  must match the current cwd before it is applied.

Persistence is best-effort JSON (default ``~/.nbchat/tui3-checkpoints.json``;
override with ``NBCHAT_TUI3_CHECKPOINTS``); any I/O error is swallowed so the
TUI can never be blocked by a checkpoint problem.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple

_MAX_PER_SESSION = 32


def store_path() -> str:
    """Checkpoint store path (env-overridable for tests)."""
    return os.environ.get("NBCHAT_TUI3_CHECKPOINTS") or os.path.join(
        os.path.expanduser("~"), ".nbchat", "tui3-checkpoints.json")


def _load_store() -> Dict[str, List[Dict[str, Any]]]:
    try:
        with open(store_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, list)}
    except Exception:
        pass
    return {}


def _save_store(store: Dict[str, List[Dict[str, Any]]]) -> bool:
    try:
        p = store_path()
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
        return True
    except Exception:
        return False


def _git(cwd: str, *args: str, timeout: int = 20) -> Tuple[int, str]:
    """Run a git command; return (rc, combined_stdout).  Never raises."""
    try:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return 127, f"git error: {exc}"


def is_git_repo(cwd: str) -> bool:
    rc, _ = _git(cwd, "rev-parse", "--is-inside-work-tree")
    return rc == 0


def take_checkpoint(cwd: str, session_id: str, label: str = "") -> Optional[Dict[str, Any]]:
    """Record the current working-tree state as a restorable checkpoint.

    Returns the checkpoint dict (also persisted), or ``None`` if ``cwd`` is
    not a git work tree.  Never raises.
    """
    if not is_git_repo(cwd):
        return None
    rc, out = _git(cwd, "stash", "create")
    source = out.strip().splitlines()[0].strip() if (rc == 0 and out.strip()) else ""
    if source:
        note = "working tree (tracked files)"
    else:
        rc2, head = _git(cwd, "rev-parse", "HEAD")
        source = head.strip()
        note = "HEAD (clean tree)"
    label = (label or "").strip() or f"c{len(_load_store().get(session_id, [])) + 1}"
    cp = {
        "source": source,
        "sha": source[:10],
        "label": label,
        "cwd": os.path.realpath(cwd),
        "session_id": session_id,
        "created_at": time.time(),
        "note": note,
    }
    store = _load_store()
    lst = store.setdefault(session_id, [])
    # de-dupe labels within the session (a re-used label replaces the older one)
    store[session_id] = [c for c in lst if c.get("label") != label]
    store[session_id].append(cp)
    store[session_id] = store[session_id][-_MAX_PER_SESSION:]
    _save_store(store)
    return cp


def latest_checkpoint(session_id: str) -> Optional[Dict[str, Any]]:
    lst = _load_store().get(session_id) or []
    return lst[-1] if lst else None


def find_checkpoint(session_id: str, label: str) -> Optional[Dict[str, Any]]:
    """Resolve a checkpoint by label; 'last'/'latest' → most recent."""
    lst = _load_store().get(session_id) or []
    if not lst:
        return None
    label = (label or "").strip().lower()
    if label in ("", "last", "latest"):
        return lst[-1]
    for c in reversed(lst):
        if (c.get("label") or "").lower() == label:
            return c
    return None


def list_checkpoints(session_id: str) -> List[Dict[str, Any]]:
    return list(_load_store().get(session_id) or [])


def preview(cwd: str, cp: Dict[str, Any]) -> str:
    """Human summary of what restoring ``cp`` would change.  Never raises."""
    rc, out = _git(cwd, "diff", "--name-only", cp.get("source", ""))
    files = [l for l in out.splitlines() if l.strip()]
    rc2, stat = _git(cwd, "diff", "--stat", cp.get("source", ""))
    tail = stat.strip().splitlines()
    summary = tail[-1] if tail else ""
    if not files:
        return f"no tracked-file differences vs checkpoint '{cp.get('label')}' (already at that state)"
    head = f"{len(files)} tracked file(s) would change:"
    listing = chr(10).join("  " + f for f in files[:40])
    more = f"  … +{len(files) - 40} more" if len(files) > 40 else ""
    body = head + chr(10) + listing
    if more:
        body += chr(10) + more
    body += chr(10) + summary
    return body


def apply(cwd: str, cp: Dict[str, Any], dry: bool = False) -> Tuple[bool, str]:
    """Restore tracked files to checkpoint ``cp``.  ``dry=True`` previews only.

    Returns ``(ok, summary)``.  Refuses if the checkpoint's recorded cwd does
    not match ``cwd``.  Never raises.
    """
    try:
        if os.path.realpath(cwd) != os.path.realpath(cp.get("cwd", "")):
            return False, ("undo: checkpoint was taken in a different directory "
                           f"({cp.get('cwd')}); re-run /checkpoint here first")
    except Exception:
        pass
    label = cp.get("label", "?")
    if dry:
        return True, f"preview for checkpoint '{label}':\n" + preview(cwd, cp)
    rc, out = _git(cwd, "restore", "--source=" + cp.get("source", ""),
                   "--staged", "--worktree", ".")
    if rc != 0:
        return False, f"undo: git restore failed: {out.strip()[:300]}"
    rc2, stat = _git(cwd, "diff", "--stat", "HEAD")
    files = [l for l in _git(cwd, "diff", "--name-only", cp.get("source", ""))[1].splitlines() if l.strip()]
    return True, (f"undo: restored tracked files to checkpoint '{label}' "
                  f"(working tree now matches it; untracked files were left alone)")
