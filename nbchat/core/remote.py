# app/remote.py
"""
Adapter that knows how to talk to:
  * a local Git repository (via gitpython)
  * GitHub (via PyGithub)

This module is intentionally low-level.  High-level, safety-checked
workflows (test-gate, staged-only commits, active-branch pushes,
dry-run reporting) live in :mod:`nbchat.tools.push_to_github`.
"""

from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import List, Optional

from git import Repo, GitCommandError, InvalidGitRepositoryError
from github import Github
from github.Auth import Token
from github.Repository import Repository

from .config import USER_NAME, REPO_NAME, IGNORED_ITEMS

log = logging.getLogger(__name__)

def _token() -> str:
    """Return the GitHub PAT from the environment."""
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN env variable not set")
    return token

def _remote_url(repo_name: str | None = None) -> str:
    """Return an HTTPS URL that contains the PAT.

    Parameters
    ----------
    repo_name:
        The repository name to use in the URL.  If ``None`` the default
        :data:`~nbchat.core.config.REPO_NAME` is used.
    """
    repo_name = repo_name or REPO_NAME
    return f"https://{USER_NAME}:{_token()}@github.com/{USER_NAME}/{repo_name}.git"


class RemoteClient:
    """Thin wrapper around gitpython + PyGithub."""

    def __init__(self, local_path: Path | str):
        self.local_path = Path(local_path).resolve()
        try:
            self.repo = Repo(self.local_path)
            if self.repo.bare:
                raise InvalidGitRepositoryError(self.local_path)
        except (InvalidGitRepositoryError, GitCommandError):
            log.info("Initializing a fresh git repo at %s", self.local_path)
            self.repo = Repo.init(self.local_path)

        self.github = Github(auth=Token(_token()))
        self.user = self.github.get_user()

    # ------------------------------------------------------------------ #
    #  Branch helpers
    # ------------------------------------------------------------------ #
    @property
    def branch_name(self) -> str:
        """Name of the branch HEAD is currently on (or ``HEAD`` if detached)."""
        try:
            return self.repo.active_branch.name
        except Exception:  # detached HEAD
            return "HEAD"

    # ------------------------------------------------------------------ #
    #  Local-repo helpers
    # ------------------------------------------------------------------ #
    def is_clean(self) -> bool:
        return not self.repo.is_dirty(untracked_files=True)

    def fetch(self) -> None:
        if "origin" in self.repo.remotes:
            log.info("Fetching from origin...")
            self.repo.remotes.origin.fetch()
        else:
            log.info("No remote configured - skipping fetch")

    def pull(self, rebase: bool = True) -> None:
        if "origin" not in self.repo.remotes:
            raise RuntimeError("No remote named 'origin' configured")

        branch = "main"

        # Check if the remote has the branch
        try:
            remote_branch = self.repo.remotes.origin.refs[branch]
        except IndexError:
            log.warning("Remote branch %s does not exist - skipping pull", branch)
            return

        log.info("Pulling %s%s...", branch, " (rebase)" if rebase else "")
        try:
            if rebase:
                self.repo.remotes.origin.pull(refspec=branch, rebase=True)
            else:
                self.repo.remotes.origin.pull(branch)
        except GitCommandError as exc:
            log.warning("Rebase failed: %s - falling back to merge", exc)
            self.repo.git.merge(f"origin/{branch}")

    def push(self, remote: str = "origin") -> None:
        """Push the local ``main`` branch (legacy behaviour).

        Prefer :meth:`push_branch`, which targets the active branch.
        """
        if remote not in self.repo.remotes:
            raise RuntimeError(f"No remote named '{remote}'")
        log.info("Pushing to %s...", remote)
        self.repo.remotes[remote].push("main")

    def reset_hard(self) -> None:
        self.repo.git.reset("--hard")

    # ------------------------------------------------------------------ #
    #  Working-tree inspection
    # ------------------------------------------------------------------ #
    def dirty_files(self, include_untracked: bool = True) -> List[str]:
        """Sorted relative paths of dirty tracked files (staged or unstaged,
        i.e. anything differing from HEAD) plus, by default, untracked files."""
        files = {p for p in self.repo.git.diff("HEAD", "--name-only").splitlines() if p}
        if include_untracked:
            files |= {
                p for p in self.repo.git.ls_files(
                    "--others", "--exclude-standard").splitlines() if p
            }
        return sorted(files)

    def has_staged(self) -> bool:
        """True if the index contains any staged changes."""
        return bool(self.repo.git.diff("--cached", "--name-only").strip())

    def staged_files(self) -> List[str]:
        """Relative paths of files staged for the next commit."""
        return sorted(p for p in self.repo.git.diff("--cached", "--name-only").splitlines() if p)

    # ------------------------------------------------------------------ #
    #  Commit helpers
    # ------------------------------------------------------------------ #
    def commit_staged(self, message: str = "Commit staged changes") -> str:
        """Commit ONLY the staged index and return the new commit sha.

        Raises :class:`GitCommandError` if nothing is staged.  Callers must
        stage explicitly before invoking this (see :meth:`commit_all`).
        """
        commit = self.repo.index.commit(message)
        log.info("Committed: %s (%s)", message, commit.hexsha[:10])
        return commit.hexsha

    def commit_all(self, message: str = "Initial commit") -> None:
        """Stage EVERYTHING (tracked changes, deletions and untracked files) and commit.

        Use with care: this sweeps uncommitted work into the commit.  Prefer
        :meth:`commit_staged` for scoped commits.
        """
        self.repo.git.add(A=True)
        self.commit_staged(message)

    # ------------------------------------------------------------------ #
    #  Remote inspection
    # ------------------------------------------------------------------ #
    def remote_repo_name(self) -> Optional[str]:
        """Repository name encoded in the current origin URL, or ``None``.

        Handles ``https://user:token@github.com/user/name.git`` as well as
        ``git@github.com:user/name.git`` and plain ``https://github.com/...``.
        """
        if "origin" not in self.repo.remotes:
            return None
        url = self.repo.remotes.origin.url or ""
        rest = url.split("://", 1)[-1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[-1]
        rest = rest.rstrip("/").removesuffix(".git")
        parts = rest.split("/")
        return parts[-1] if len(parts) >= 2 else None

    def remote_branch_exists(self, branch: str = "main") -> bool:
        """True if ``origin/<branch>`` resolves (fetches on demand)."""
        if "origin" not in self.repo.remotes:
            return False
        try:
            self.repo.git.fetch(
                "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
            )
        except GitCommandError:
            return False
        try:
            return bool(self.repo.git.rev_parse(f"origin/{branch}").strip())
        except GitCommandError:
            return False

    def sync_from_remote(self, branch: str) -> None:
        """Rebase local work onto ``origin/<branch>`` (no-op if remote is empty)."""
        if not self.remote_branch_exists(branch):
            log.info("Remote branch '%s' does not exist yet - skipping sync", branch)
            return
        log.info("Syncing with origin/%s (rebase)...", branch)
        self.repo.git.rebase(f"origin/{branch}")

    # ------------------------------------------------------------------ #
    #  Push helpers
    # ------------------------------------------------------------------ #
    def push_branch(self, branch: Optional[str] = None,
                    remote: str = "origin",
                    remote_branch: Optional[str] = None,
                    set_upstream: bool = True) -> None:
        """Push the given local branch (default: active) to ``remote``.

        ``remote_branch`` names the destination ref on the remote when it
        differs from the local branch name (e.g. local ``v1.7`` -> remote
        ``main``).  Defaults to the local branch name.
        """
        branch = branch or self.branch_name
        if remote not in self.repo.remotes:
            raise RuntimeError(f"No remote named '{remote}'")
        dest = remote_branch or branch
        args = [remote, f"{branch}:{dest}"]
        if set_upstream:
            args.append("-u")
        log.info("Pushing branch '%s' to %s/%s", branch, remote, dest)
        self.repo.git.push(*args)

    # ------------------------------------------------------------------ #
    #  GitHub helpers
    # ------------------------------------------------------------------ #
    def ensure_repo(self, name: str = REPO_NAME) -> Repository:
        try:
            repo = self.user.get_repo(name)
            log.info("Repo '%s' already exists on GitHub", name)
        except Exception:
            log.info("Creating new repo '%s' on GitHub", name)
            repo = self.user.create_repo(name, private=False)
        return repo

    def attach_remote(self, repo_name: str | None = None, url: Optional[str] = None) -> None:
        """Create or replace the ``origin`` remote.

        Parameters
        ----------
        repo_name:
            Repository name to construct the default URL.  If ``url`` is
            supplied it will take precedence.
        url:
            Explicit remote URL.  Useful when the remote URL does not follow
            the conventional ``github.com/{user}/{repo}.git`` pattern.
        """
        if url is None:
            url = _remote_url(repo_name)
        if "origin" in self.repo.remotes:
            log.info("Removing old origin remote")
            self.repo.delete_remote("origin")
        log.info("Adding new origin remote: %s", url)
        self.repo.create_remote("origin", url)

    def ensure_main_branch(self) -> None:
        """
        Make sure the local repository has a `main` branch.
        If it does not exist, create it pointing at HEAD and set upstream.
        """
        if "main" not in self.repo.branches:
            # Create a new branch named main pointing at the current HEAD
            self.repo.git.branch("main")
            log.info("Created local branch 'main'")

        # Make sure main tracks origin/main
        try:
            self.repo.git.push("--set-upstream", "origin", "main")
            log.info("Set upstream of local main to origin/main")
        except GitCommandError:
            # If the remote branch does not exist yet, just push normally
            log.info("Remote main does not exist yet - will push normally")

    # ------------------------------------------------------------------ #
    #  Convenience helpers
    # ------------------------------------------------------------------ #
    def write_gitignore(self) -> None:
        path = self.local_path / ".gitignore"
        content = "\n".join(IGNORED_ITEMS) + "\n"
        path.write_text(content, encoding="utf-8")
        log.info("Wrote %s", path)
