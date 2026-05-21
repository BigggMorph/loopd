"""Worktree/branch collision regression tests for WorkspaceManager.

Guards the fix for BigggMorph/loopd#8: when create_task_worktree is
invoked twice with the same task_id (which only happens if the
task_id lock in TaskManager ever regresses or some other code path
reuses an in-flight id), it MUST raise WorkspaceError instead of
silently `rmtree`-ing the existing worktree and `branch -D`-ing the
existing branch — that destructive default historically wiped the
other Claude Code window's uncommitted-or-unpushed work without any
error signal.

The tests construct a tiny local bare repo on disk so we do not need
network access; the bare clone path is passed as ``repo_url`` directly
(it is a valid git URL because it is a filesystem path to a bare repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _run_git(args: list[str], cwd: Path) -> None:
    """Run git, raising on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr}"
        )


@pytest.fixture
def fake_remote(tmp_path: Path) -> str:
    """Build a tiny bare repo on disk with a single commit on main.

    Returns a path-string usable as ``repo_url``. Compatible with older
    git versions (no --initial-branch flag) by renaming the default
    branch to ``main`` after the initial commit.
    """
    # 1. Initialize the bare "remote".
    bare = tmp_path / "fake-remote.git"
    bare.mkdir()
    _run_git(["init", "--bare", str(bare)], cwd=tmp_path)

    # 2. Build a scratch worktree, make a commit, push to the bare.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _run_git(["init"], cwd=scratch)
    _run_git(["config", "user.email", "test@example.com"], cwd=scratch)
    _run_git(["config", "user.name", "Test"], cwd=scratch)
    (scratch / "README.md").write_text("hello\n")
    _run_git(["add", "README.md"], cwd=scratch)
    _run_git(["commit", "-m", "init"], cwd=scratch)
    # Force-rename the current branch to "main" (handles old git default
    # "master" and new "main" alike) and push it to the bare.
    _run_git(["branch", "-M", "main"], cwd=scratch)
    _run_git(["remote", "add", "origin", str(bare)], cwd=scratch)
    _run_git(["push", "origin", "main"], cwd=scratch)
    # Set HEAD of the bare repo so subsequent fetches see main as default.
    _run_git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=bare)

    return str(bare)


def _make_wm():
    """Build a fresh WorkspaceManager pointing at the isolated LOOPD_ROOT."""
    from loopd_core.state.workspace_manager import WorkspaceManager

    return WorkspaceManager()


def test_collision_raises_workspace_error(fake_remote: str, tmp_path: Path):
    """Second create_task_worktree for the same task_id must raise
    WorkspaceError, and the first worktree's contents must survive intact."""
    from loopd_core.state.workspace_manager import WorkspaceError

    wm = _make_wm()
    task_id = "task-test-001"

    # First call: should succeed.
    path1 = wm.create_task_worktree(task_id, fake_remote, base_branch="main")
    assert path1.is_dir(), "first worktree must be created"

    # Drop a sentinel file inside the worktree to represent in-flight user work.
    sentinel = path1 / "user-work.txt"
    sentinel.write_text("DO NOT WIPE ME")
    assert sentinel.exists()

    # Second call: same task_id → WorkspaceError, sentinel survives.
    with pytest.raises(WorkspaceError) as excinfo:
        wm.create_task_worktree(task_id, fake_remote, base_branch="main")

    assert "already exists" in str(excinfo.value)
    assert sentinel.exists(), (
        "WorkspaceError must be raised BEFORE the existing worktree is wiped"
    )
    assert sentinel.read_text() == "DO NOT WIPE ME"


def test_collision_with_force_recreates(fake_remote: str, tmp_path: Path):
    """force_recreate_workspace must succeed and wipe the previous worktree
    (legacy behavior preserved for SWE-bench style use cases)."""
    wm = _make_wm()
    task_id = "task-test-002"

    path1 = wm.create_task_worktree(task_id, fake_remote, base_branch="main")
    sentinel = path1 / "stale-work.txt"
    sentinel.write_text("can be wiped on force=True")
    assert sentinel.exists()

    # Note: force_recreate_workspace takes ``repo`` not ``repo_url``. We pass
    # the bare repo path; parse_repo_to_url would reject it, but get_alias
    # returns None for unknown names and parse_repo_to_url accepts paths
    # starting with neither "git@" nor "https://" only via the user/repo
    # regex. So we go through create_task_worktree(force=True) directly,
    # which is the same code path force_recreate_workspace uses internally.
    path2 = wm.create_task_worktree(
        task_id, fake_remote, base_branch="main", force=True,
    )
    assert path2.is_dir(), "force=True must (re)create the worktree"
    assert not sentinel.exists(), (
        "force=True must wipe the stale worktree (legacy semantics)"
    )


def test_branch_collision_raises_workspace_error(fake_remote: str, tmp_path: Path):
    """When the branch exists in the bare repo but the worktree dir is
    already gone (e.g. someone rmtree'd it manually), the branch
    collision check must still raise instead of silently `branch -D`-ing
    the existing branch (which would discard any unpushed commits)."""
    from loopd_core.state.workspace_manager import WorkspaceError

    wm = _make_wm()
    task_id = "task-test-003"
    branch_name = f"loopd/{task_id}"

    # Create the worktree, then remove the worktree directory while
    # leaving the branch alive in the bare repo — this mimics the
    # narrow window between "remove_task_worktree" and "create new".
    path1 = wm.create_task_worktree(task_id, fake_remote, base_branch="main")
    bare_path = wm.get_bare_repo_path(fake_remote)
    # Forcibly remove the worktree directory + clean git's worktree list,
    # but keep the branch ref alive in the bare repo.
    subprocess.run(
        ["git", "worktree", "remove", str(path1), "--force"],
        cwd=str(bare_path), check=False, capture_output=True,
    )
    # Sanity-check: branch still exists in the bare.
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=str(bare_path), check=False,
    )
    assert result.returncode == 0, "test setup error: branch should still exist"

    # Recreate attempt without force must raise on the branch collision.
    with pytest.raises(WorkspaceError) as excinfo:
        wm.create_task_worktree(task_id, fake_remote, base_branch="main")
    assert "branch" in str(excinfo.value).lower()

    # And the branch must still be alive after the raise.
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=str(bare_path), check=False,
    )
    assert result.returncode == 0, (
        "WorkspaceError must be raised BEFORE the existing branch is deleted"
    )
