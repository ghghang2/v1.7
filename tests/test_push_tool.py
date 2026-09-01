"""Tests for the ``push_to_github`` tool and its ``RemoteClient`` helpers.

These tests never touch GitHub or the real repository:
* git-level helpers run against a throwaway repo created in ``tmp_path``;
* the tool's decision logic (repo resolution, pytest summary parsing,
  test-gate refusal) is exercised with a stub client / monkeypatching.
"""
import json
import os

import pytest

from nbchat.core.remote import RemoteClient
from nbchat.tools.push_to_github import _resolve_target, _run_pytest, push_to_github
import nbchat.tools.push_to_github as push_tool


# --------------------------------------------------------------------------- #
#  Helpers on a real (throwaway) git repo
# --------------------------------------------------------------------------- #
@pytest.fixture
def tmp_git_repo(tmp_path, monkeypatch):
    """A scratch git repo with a client whose GitHub session is stubbed out."""
    import subprocess
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True,
                       capture_output=True, env=env)

    git("init", "-q", "-b", "main", ".")
    (tmp_path / "f.txt").write_text("one\n")
    git("add", "f.txt")
    git("commit", "-q", "-m", "c1")

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    import nbchat.core.config as cfg
    monkeypatch.setattr(cfg, "USER_NAME", "testuser")
    monkeypatch.setattr(cfg, "REPO_NAME", "cfg-repo")

    client = RemoteClient(tmp_path)
    # Stub the GitHub network session; only local helpers are under test.
    class _User:
        login = "testuser"
    client.github = object()
    client.user = _User()
    client._tmp = tmp_path
    return client


def test_branch_name_is_main(tmp_git_repo):
    assert tmp_git_repo.branch_name == "main"


def test_dirty_files_and_staged(tmp_git_repo):
    assert tmp_git_repo.dirty_files(include_untracked=False) == []
    assert tmp_git_repo.has_staged() is False

    (tmp_git_repo._tmp / "f.txt").write_text("two\n")
    (tmp_git_repo._tmp / "new.txt").write_text("x\n")
    dirty = tmp_git_repo.dirty_files()
    assert "f.txt" in dirty and "new.txt" in dirty
    tracked_only = tmp_git_repo.dirty_files(include_untracked=False)
    assert "new.txt" not in tracked_only and "f.txt" in tracked_only

    tmp_git_repo.repo.git.add("f.txt")
    assert tmp_git_repo.has_staged() is True
    assert tmp_git_repo.staged_files() == ["f.txt"]


def test_commit_staged_only_touches_staged(tmp_git_repo):
    (tmp_git_repo._tmp / "f.txt").write_text("two\n")
    (tmp_git_repo._tmp / "loose.txt").write_text("nope\n")
    tmp_git_repo.repo.git.add("f.txt")          # stage f.txt only
    sha = tmp_git_repo.commit_staged("scoped")
    assert sha

    status = tmp_git_repo.repo.git.status("--porcelain").splitlines()
    # f.txt committed; loose.txt still untracked; nothing staged
    assert status == ["?? loose.txt"]


def test_commit_all_sweeps_everything(tmp_git_repo):
    (tmp_git_repo._tmp / "f.txt").write_text("two\n")
    (tmp_git_repo._tmp / "loose.txt").write_text("nope\n")
    tmp_git_repo.commit_all("sweep")
    assert tmp_git_repo.repo.git.status("--porcelain") == ""
    assert tmp_git_repo.is_clean()


def test_remote_repo_name_parses_urls():
    class _Remote:
        def __init__(self, url):
            self.url = url

    class _Remotes:
        def __init__(self, url):
            self.origin = _Remote(url)

        def __contains__(self, name):
            return name == "origin"

    class _Client:
        def __init__(self, url):
            self.repo = type("R", (), {"remotes": _Remotes(url)})()

    def parse(url):
        return RemoteClient.remote_repo_name(_Client(url))

    assert parse("https://user:tok@github.com/ghghang2/v1.6.git") == "v1.6"
    assert parse("https://github.com/ghghang2/v1.6") == "v1.6"
    assert parse("git@github.com:ghghang2/v1.6.git") == "v1.6"


def test_remote_repo_name_no_remote(tmp_git_repo):
    # origin does not exist yet in the scratch repo
    assert tmp_git_repo.remote_repo_name() is None


def test_push_branch_rejects_missing_remote(tmp_git_repo):
    with pytest.raises(RuntimeError):
        tmp_git_repo.push_branch(remote="nope")


# --------------------------------------------------------------------------- #
#  Decision logic of the tool (no network)
# --------------------------------------------------------------------------- #
def test_resolve_target_precedence():
    assert _resolve_target("cfg", "remote1", "arg") == ("arg", "argument")
    assert _resolve_target("cfg", "remote1", None) == ("remote1", "current_remote")
    assert _resolve_target("cfg", "cfg", None) == ("cfg", "config")
    assert _resolve_target("cfg", None, None) == ("cfg", "config")


class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def test_pytest_summary_parsing(monkeypatch):
    # Drop the nested-run guard so the (fake) subprocess path is exercised even
    # when this module runs inside the push tool's own spawned pytest.
    monkeypatch.delenv("NBCHAT_INNER_PYTEST", raising=False)
    monkeypatch.setattr(push_tool.subprocess, "run",
                        lambda *a, **k: _FakeProc(
                            "\n134 passed, 2 failed, 1 error in 3.21s\n"))
    stats = _run_pytest()
    assert stats == {"passed": 134, "failed": 2, "errors": 1}, stats


def test_pytest_summary_crash_is_gate_failure(monkeypatch):
    monkeypatch.delenv("NBCHAT_INNER_PYTEST", raising=False)
    monkeypatch.setattr(push_tool.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    stats = _run_pytest()
    assert stats["failed"] == 1 and stats["passed"] == 0, stats


def test_pytest_nested_run_refused(monkeypatch):
    monkeypatch.setenv("NBCHAT_INNER_PYTEST", "1")
    stats = _run_pytest()
    assert stats["failed"] == 1 and "nested" in stats["note"], stats


# --------------------------------------------------------------------------- #
#  End-to-end tool behaviour with a stubbed client (no network / no push)
# --------------------------------------------------------------------------- #
class _FakeClient:
    """Records calls; simulates a repo with a dirty but unstaged file."""
    def __init__(self, remote_name="v1.6", branch="main",
                 staged=None, unstaged=("repo_config.yaml",)):
        class _User:
            login = "testuser"

        self.user = _User()
        self.calls = []
        self._remote = remote_name
        self._branch = branch
        self._staged = list(staged or [])
        self._unstaged = list(unstaged)

    def remote_repo_name(self):
        self.calls.append("remote_repo_name")
        return self._remote

    @property
    def branch_name(self):
        return self._branch

    def dirty_files(self, include_untracked=True):
        self.calls.append(f"dirty_files:{include_untracked}")
        return list(self._unstaged)

    def staged_files(self):
        self.calls.append("staged_files")
        return list(self._staged)

    def has_staged(self):
        self.calls.append("has_staged")
        return bool(self._staged)

    def commit_staged(self, message):
        self.calls.append(f"commit_staged:{message}")
        self._staged = []

    def commit_all(self, message):
        self.calls.append(f"commit_all:{message}")
        self._staged = []
        self._unstaged = []

    def ensure_repo(self, target):
        self.calls.append(f"ensure_repo:{target}")

    def attach_remote(self, target):
        self.calls.append(f"attach_remote:{target}")

    def sync_from_remote(self, branch):
        self.calls.append(f"sync_from_remote:{branch}")

    def push_branch(self, branch, remote=None,
                    remote_branch=None, set_upstream=True):
        self.calls.append(f"push_branch:{branch}:{remote_branch or branch}")


def _out(result):
    return json.loads(result)


def test_push_commits_staged_only_and_ignores_unstaged(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(staged=["nbchat/core/remote.py"])
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg"))
    assert out["status"] == "success"
    assert any(c.startswith("commit_staged:") for c in fake.calls)
    assert not any(c.startswith("commit_all") for c in fake.calls)
    assert out["unstaged"] == ["repo_config.yaml"]          # left untouched
    assert out["repo"] == "testuser/v1.6"
    assert out["repo_source"] == "config"                   # remote == config default


def test_push_stage_all_sweeps_unstaged(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient()
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg", stage_all=True))
    assert out["status"] == "success"
    assert any(c.startswith("commit_all:") for c in fake.calls)
    assert not any(c.startswith("commit_staged:") for c in fake.calls)


def test_push_refused_when_tests_fail(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 2, "errors": 0})
    fake = _FakeClient()
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg"))
    assert out["status"] == "error"
    assert "test gate failed" in out["error"]
    assert "commit" not in [c.split(":")[0] for c in fake.calls]
    assert "push_branch" not in fake.calls


def test_push_skip_tests_bypasses_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(push_tool, "_run_pytest",
                        lambda timeout=300: calls.append("pytest"))
    fake = _FakeClient()
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg", skip_tests=True))
    assert out["status"] == "success"
    assert calls == []
    assert out["tests"] is None


def test_push_repo_argument_targets_new_repo(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(remote_name="v1.6")
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("seed", repo_name="v1.7"))
    assert out["status"] == "success"
    assert out["repo"] == "testuser/v1.7"
    assert out["repo_source"] == "argument"
    assert any(c == "attach_remote:v1.7" for c in fake.calls)
    assert any(c == "ensure_repo:v1.7" for c in fake.calls)
    assert out["note"]  # warns that origin currently points at v1.6


def test_push_dry_run_makes_no_mutations(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(staged=["nbchat/core/remote.py"])
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg", dry_run=True))
    assert out["status"] == "dry_run"
    assert out["would_commit"] is True
    assert out["staged"] == ["nbchat/core/remote.py"]
    assert not any(c in ("commit_staged:msg", "commit_all:msg",
                         "push_branch:main", "attach_remote:v1.6",
                         "ensure_repo:v1.6") for c in fake.calls)


def test_push_nothing_to_commit_fast_path(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(staged=None, unstaged=())
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg"))
    assert out["status"] == "success"
    assert out["committed"] is False
    # A clean tree still pushes the active branch (it may be ahead of remote);
    # it just does not create a commit.
    assert "push_branch:main:main" in fake.calls


def test_push_remote_branch_maps_refspec(monkeypatch):
    """remote_branch pushes local branch to a differently-named remote ref."""
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(staged=["a.txt"])
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg", remote_branch="main"))
    assert out["status"] == "success"
    assert out["remote_branch"] == "main"
    assert "push_branch:main:main" in fake.calls


def test_push_rebase_triggers_sync(monkeypatch):
    monkeypatch.setattr(push_tool, "_run_pytest", lambda timeout=300:
                        {"passed": 5, "failed": 0, "errors": 0})
    fake = _FakeClient(staged=["a.txt"])
    monkeypatch.setattr(push_tool, "RemoteClient", lambda p: fake)
    out = _out(push_to_github("msg", rebase=True))
    assert out["status"] == "success"
    assert any(c == "sync_from_remote:main" for c in fake.calls)
