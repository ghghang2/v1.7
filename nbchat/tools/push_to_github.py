# app/push_to_github.py
"""A minimal OpenAI function‑calling tool that pushes the local
repository to GitHub.

The tool is deliberately lightweight: it relies on the existing
``RemoteClient`` and the configuration found in ``repo_config.yaml``.
It performs the following steps:

1. Load the target repository name from ``config.py`` (or from the
   optional ``repo_name`` argument).
2. Ensure the repository exists on GitHub.
3. Attach the HTTPS remote.
4. Ensure a local ``main`` branch exists and is set to track
   ``origin/main``.
5. Pull the latest changes (optionally rebasing).
6. Commit all staged changes with the supplied ``commit_message``.
7. Push the local ``main`` branch to GitHub.

The function returns a JSON string suitable for use with OpenAI
function‑calling: ``{"status": "success", "repo": "<user>/<repo>"}``.
On error a JSON object with an ``error`` key is returned.
"""

from pathlib import Path
import json
from typing import Optional

from nbchat.core.remote import RemoteClient
# Import the config module lazily so we can reload it on each invocation.
import importlib
import nbchat.core.config as cfg


def push_to_github(
    commit_message: str = "Auto commit",
    rebase: bool = False,
) -> str:
    """Push the current repository to GitHub.

    Parameters
    ----------
    commit_message:
        Commit message for the auto commit.  Defaults to ``"Auto commit"``.
    rebase:
        Whether to rebase during pull.  Defaults to ``False`` to mirror
        the original behaviour.
    """
    try:
        # Reload the config module each call to pick up any changes in repo_config.yaml
        importlib.reload(cfg)
        target_repo = cfg.REPO_NAME
        client = RemoteClient(Path("."))
        # 1. Ensure the GitHub repo exists
        client.ensure_repo(target_repo)
        # 2. Attach the HTTPS remote
        client.attach_remote(target_repo)
        # 3. Ensure local main branch exists and tracks remote
        client.ensure_main_branch()
        # 4. Fetch & pull
        client.fetch()
        client.pull(rebase=rebase)
        # 5. Commit all changes
        client.commit_all(commit_message)
        # 6. Push to GitHub
        client.push()
        return json.dumps({"status": "success", "repo": f"{client.user.login}/{target_repo}"})
    except Exception as exc:  # pragma: no cover - defensive
        return json.dumps({"error": str(exc)})

# ---------------------------------------------------------------------------
# Tool Definition for OpenAI function‑calling
# ---------------------------------------------------------------------------
func = push_to_github
name = "push_to_github"
description = "Push the current local repository to GitHub with a commit message and optional rebase flag."

schema = {
    "parameters": {
        "type": "object",
        "properties": {
            "commit_message": {"type": "string"},
            "rebase": {"type": "boolean"},
        },
        "required": [],
    }
}

__all__ = ["push_to_github", "func", "name", "description", "schema"]
