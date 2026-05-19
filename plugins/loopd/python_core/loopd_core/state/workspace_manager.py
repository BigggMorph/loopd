"""
Workspace management for loopd.

Bare repo + worktree based workspace management.
Replaces lib/workspace.sh (813 LOC).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import yaml

from loopd_core.config import Config, get_config
from loopd_core.state.event_logger import EventLogger
from loopd_core.state.lock_manager import LockManager

logger = logging.getLogger(__name__)


class WorkspaceError(Exception):
    """Raised when workspace operations fail."""
    pass


class WorkspaceConfig:
    """Workspace configuration loaded from _config/workspaces.yaml."""

    def __init__(self, config: Config):
        self._config = config
        self._yaml: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        config_file = self._config.config_path / "workspaces.yaml"
        if config_file.exists():
            with open(config_file) as f:
                self._yaml = yaml.safe_load(f) or {}

    @property
    def base_dir(self) -> Path:
        raw = self._yaml.get("base_dir")
        if raw:
            return Path(raw).expanduser()
        return self._config.loopd_root

    @property
    def repos_dir(self) -> Path:
        raw = self._yaml.get("repos_dir")
        if raw:
            return Path(raw).expanduser()
        return self._config.repos_path

    @property
    def workspaces_dir(self) -> Path:
        raw = self._yaml.get("workspaces_dir")
        if raw:
            return Path(raw).expanduser()
        return self._config.workspaces_path

    @property
    def branch_pattern(self) -> str:
        return (
            self._yaml.get("worktree", {})
            .get("branch_pattern", "loopd/{task_id}")
        )

    @property
    def auto_cleanup(self) -> bool:
        return self._yaml.get("worktree", {}).get("auto_cleanup", True)

    @property
    def keep_failed(self) -> bool:
        return self._yaml.get("worktree", {}).get("keep_failed", True)

    @property
    def orphan_max_age_hours(self) -> int:
        return self._yaml.get("worktree", {}).get("orphan_max_age_hours", 24)

    @property
    def fetch_before_worktree(self) -> bool:
        return self._yaml.get("git", {}).get("fetch_before_worktree", True)

    @property
    def create_pr(self) -> bool:
        return self._yaml.get("git", {}).get("create_pr", True)

    @property
    def push_enabled(self) -> bool:
        return self._yaml.get("git", {}).get("push_enabled", True)

    def get_alias(self, name: str) -> Optional[str]:
        """Get repo URL from alias name."""
        aliases = self._yaml.get("aliases", {})
        if isinstance(aliases, dict) and name in aliases:
            alias = aliases[name]
            if isinstance(alias, dict):
                return alias.get("remote")
        return None


class WorkspaceManager:
    """
    Manages task workspaces using bare repos + git worktrees.

    Each task gets an isolated worktree with its own branch,
    created from a shared bare repository clone.
    """

    # Timeout for worktree lock (seconds). Worktree operations include
    # network I/O (git fetch), so this must be generous.
    # _run_git subprocess timeout is 120s, and _create_task_worktree_impl
    # makes 5+ sequential git calls, so waiting processes need at least 3x
    # the per-call timeout to avoid timing out before the lock holder finishes.
    WORKTREE_LOCK_TIMEOUT = 360

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.ws_config = WorkspaceConfig(self.config)
        self.event_logger = EventLogger(self.config)
        self.lock_manager = LockManager(self.config)

    def _run_git(
        self,
        args: list[str],
        cwd: Optional[Path] = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        """Run a git command, capturing output."""
        cmd = ["git"] + args
        result = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if check and result.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(self._mask_url_token(a) for a in args)} failed: {result.stderr.strip()}"
            )
        return result

    # ─── Repo parsing ───

    @staticmethod
    def parse_repo_to_url(repo: str) -> str:
        """
        Parse repo identifier to git URL.

        Accepts: "user/repo", "github.com/user/repo",
                 "git@github.com:user/repo.git", "https://..."
        """
        if repo.startswith("git@") or repo.startswith("https://"):
            return repo

        repo = repo.removeprefix("github.com/")

        if re.match(r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_.-]+$", repo):
            return f"https://github.com/{repo}.git"

        raise WorkspaceError(f"Invalid repo format: {repo}")

    @staticmethod
    def get_repo_name(url: str) -> str:
        """
        Extract filesystem-safe repo name from URL.

        "https://github.com/user/repo.git" -> "user__repo"
        """
        if url.startswith("git@"):
            repo_part = url.split(":", 1)[1].removesuffix(".git")
        elif url.startswith("https://"):
            # Remove scheme + host
            repo_part = "/".join(url.split("/")[3:]).removesuffix(".git")
        else:
            repo_part = url

        return repo_part.replace("/", "__")

    # ─── Bare repo management ───

    def get_bare_repo_path(self, repo_url: str) -> Path:
        repo_name = self.get_repo_name(repo_url)
        return self.ws_config.repos_dir / f"{repo_name}.git"

    def has_bare_repo(self, repo_url: str) -> bool:
        return self.get_bare_repo_path(repo_url).is_dir()

    def _inject_token_into_url(self, url: str) -> str:
        """Inject GITHUB_TOKEN into https://github.com URLs for authentication."""
        token = os.environ.get("GITHUB_TOKEN", "")
        if token and url.startswith("https://github.com/"):
            return url.replace("https://github.com/", f"https://{token}@github.com/", 1)
        return url

    @staticmethod
    def _mask_url_token(s: str) -> str:
        """Mask GITHUB_TOKEN embedded in URLs to prevent leaking in error logs."""
        return re.sub(r"https://[^@]+@github\.com/", "https://***@github.com/", s)

    def clone_bare_repo(self, repo_url: str) -> Path:
        """Clone a bare repository. Returns bare repo path."""
        bare_path = self.get_bare_repo_path(repo_url)

        if bare_path.is_dir():
            logger.info(f"Bare repo already exists: {bare_path}")
            self._ensure_fetch_refspec(bare_path)
            return bare_path

        logger.info(f"Cloning bare repo: {repo_url} -> {bare_path}")
        bare_path.parent.mkdir(parents=True, exist_ok=True)

        auth_url = self._inject_token_into_url(repo_url)
        self._run_git(["clone", "--bare", auth_url, str(bare_path)])
        self._ensure_fetch_refspec(bare_path)
        return bare_path

    def _ensure_fetch_refspec(self, bare_path: Path) -> None:
        """Ensure bare repo has fetch refspec so 'git fetch' updates origin/* refs.

        git clone --bare does NOT set remote.origin.fetch, so subsequent
        git fetch --all silently fetches nothing. This fixes that.
        """
        result = self._run_git(
            ["config", "--get", "remote.origin.fetch"],
            cwd=bare_path, check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.info(f"Setting fetch refspec on bare repo: {bare_path}")
            self._run_git(
                ["config", "remote.origin.fetch",
                 "+refs/heads/*:refs/remotes/origin/*"],
                cwd=bare_path,
            )

    def fetch_bare_repo(self, repo_url: str) -> None:
        """Fetch updates for a bare repository."""
        bare_path = self.get_bare_repo_path(repo_url)
        if not bare_path.is_dir():
            raise WorkspaceError(f"Bare repo not found: {bare_path}")

        self._ensure_fetch_refspec(bare_path)
        logger.info(f"Fetching updates for: {bare_path}")
        self._run_git(["fetch", "--all", "--prune"], cwd=bare_path, check=False)

    # ─── Worktree management ───

    def get_worktree_path(self, task_id: str, repo_url: str) -> Path:
        repo_name = self.get_repo_name(repo_url)
        return self.ws_config.workspaces_dir / f"{task_id}--{repo_name}"

    def _worktree_lock_id(self, repo_url: str) -> str:
        """Return lock ID for bare-repo-level worktree serialization."""
        return f"worktree-{self.get_repo_name(repo_url)}"

    def create_task_worktree(
        self,
        task_id: str,
        repo_url: str,
        base_branch: str = "main",
        base_commit: Optional[str] = None,
    ) -> Path:
        """
        Create a git worktree for a task.

        Acquires a bare-repo-level lock to prevent concurrent worktree
        creation from causing git config.lock conflicts.

        If base_commit is provided, the worktree is checked out at that specific
        commit instead of the tip of base_branch (used for SWE-bench evaluation).

        Returns the worktree path.
        """
        lock_id = self._worktree_lock_id(repo_url)
        with self.lock_manager.lock(lock_id, timeout=self.WORKTREE_LOCK_TIMEOUT):
            return self._create_task_worktree_impl(
                task_id, repo_url, base_branch, base_commit,
            )

    def _create_task_worktree_impl(
        self,
        task_id: str,
        repo_url: str,
        base_branch: str = "main",
        base_commit: Optional[str] = None,
    ) -> Path:
        """Internal worktree creation — caller must already hold the lock."""
        # Ensure bare repo exists
        if not self.has_bare_repo(repo_url):
            self.clone_bare_repo(repo_url)

        # Fetch latest — always, regardless of config flag
        self.fetch_bare_repo(repo_url)

        bare_path = self.get_bare_repo_path(repo_url)
        worktree_path = self.get_worktree_path(task_id, repo_url)
        branch_name = self.ws_config.branch_pattern.replace("{task_id}", task_id)

        if worktree_path.is_dir():
            logger.info(f"Stale worktree found, removing for fresh creation: {worktree_path}")
            self.remove_task_worktree(task_id, repo_url)
            # Fall through to fresh creation below

        logger.info(f"Creating worktree: {worktree_path} (branch: {branch_name})")
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Prune stale worktree references
        self._run_git(["worktree", "prune"], cwd=bare_path, check=False)

        # Delete existing branch if present (from a previous run)
        result = self._run_git(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=bare_path,
            check=False,
        )
        if result.returncode == 0:
            logger.info(f"Branch already exists, deleting: {branch_name}")
            self._run_git(["branch", "-D", branch_name], cwd=bare_path, check=False)

        # Determine the starting ref: specific commit (SWE-bench) or branch tip
        if base_commit:
            start_ref = base_commit
            logger.info(f"Creating worktree at commit: {base_commit[:12]}")
        else:
            start_ref = f"origin/{base_branch}"

        # Create worktree with new branch from base
        result = self._run_git(
            ["worktree", "add", str(worktree_path), "-b", branch_name, start_ref],
            cwd=bare_path,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Worktree created successfully")
            return worktree_path

        raise WorkspaceError(
            f"Failed to create worktree from {start_ref}: "
            f"{result.stderr.strip()}"
        )

    def remove_task_worktree(self, task_id: str, repo_url: str) -> None:
        """Remove worktree for a task."""
        bare_path = self.get_bare_repo_path(repo_url)
        worktree_path = self.get_worktree_path(task_id, repo_url)

        if not worktree_path.is_dir():
            return

        logger.info(f"Removing worktree: {worktree_path}")

        if bare_path.is_dir():
            self._run_git(
                ["worktree", "remove", str(worktree_path), "--force"],
                cwd=bare_path,
                check=False,
            )

        # Ensure directory is removed
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)

        if bare_path.is_dir():
            self._run_git(["worktree", "prune"], cwd=bare_path, check=False)

    # ─── Task workspace operations ───

    def setup_task_workspace(
        self,
        task_id: str,
        repo: str,
        base_branch: str = "main",
        base_commit: Optional[str] = None,
    ) -> Path:
        """
        Setup complete workspace for a task.

        Parses repo, ensures bare clone, creates worktree.
        If base_commit is given, the worktree is checked out at that commit
        instead of the branch tip (SWE-bench evaluation use case).
        Returns worktree path.
        """
        # Resolve repo alias
        alias_url = self.ws_config.get_alias(repo)
        if alias_url:
            repo_url = alias_url
        else:
            repo_url = self.parse_repo_to_url(repo)

        # Initialize directories
        self.ws_config.base_dir.mkdir(parents=True, exist_ok=True)
        self.ws_config.repos_dir.mkdir(parents=True, exist_ok=True)
        self.ws_config.workspaces_dir.mkdir(parents=True, exist_ok=True)

        return self.create_task_worktree(task_id, repo_url, base_branch, base_commit)

    def cleanup_task_workspace(
        self,
        task_id: str,
        repo: str,
        keep: bool = False,
    ) -> None:
        """Cleanup workspace after task completion."""
        if keep:
            logger.info(f"Keeping worktree for task: {task_id}")
            return

        alias_url = self.ws_config.get_alias(repo)
        repo_url = alias_url or self.parse_repo_to_url(repo)
        self.remove_task_worktree(task_id, repo_url)

    def get_task_workspace(self, task_id: str, repo: str) -> Optional[Path]:
        """Get existing workspace path for a task, or None."""
        alias_url = self.ws_config.get_alias(repo)
        repo_url = alias_url or self.parse_repo_to_url(repo)
        worktree_path = self.get_worktree_path(task_id, repo_url)
        return worktree_path if worktree_path.is_dir() else None

    def force_recreate_workspace(
        self,
        task_id: str,
        repo: str,
        base_branch: str = "main",
    ) -> Path:
        """
        Force-recreate workspace by removing stale worktree and creating fresh one.

        Unlike setup_task_workspace, this always removes the existing worktree first.
        Acquires a bare-repo-level lock covering the entire operation
        (fetch + remove + create) to prevent git config.lock conflicts.

        Returns new worktree path.
        """
        # Resolve repo alias
        alias_url = self.ws_config.get_alias(repo)
        repo_url = alias_url or self.parse_repo_to_url(repo)

        # Initialize directories
        self.ws_config.base_dir.mkdir(parents=True, exist_ok=True)
        self.ws_config.repos_dir.mkdir(parents=True, exist_ok=True)
        self.ws_config.workspaces_dir.mkdir(parents=True, exist_ok=True)

        lock_id = self._worktree_lock_id(repo_url)
        with self.lock_manager.lock(lock_id, timeout=self.WORKTREE_LOCK_TIMEOUT):
            # Fetch latest before removing
            if self.has_bare_repo(repo_url):
                self.fetch_bare_repo(repo_url)

            # Explicitly remove existing worktree
            self.remove_task_worktree(task_id, repo_url)

            # Create fresh worktree (use _impl to avoid re-acquiring lock)
            return self._create_task_worktree_impl(
                task_id, repo_url, base_branch,
            )

    # ─── Git operations in worktree ───

    def commit_in_worktree(self, worktree_path: Path, message: str) -> bool:
        """Stage all and commit in worktree. Returns True if committed."""
        if not worktree_path.is_dir():
            raise WorkspaceError(f"Worktree not found: {worktree_path}")

        self._run_git(["add", "-A"], cwd=worktree_path, check=False)
        result = self._run_git(
            ["commit", "-m", message], cwd=worktree_path, check=False
        )
        return result.returncode == 0

    def push_worktree_branch(self, worktree_path: Path) -> bool:
        """Push current branch from worktree. Returns True on success."""
        if not worktree_path.is_dir():
            raise WorkspaceError(f"Worktree not found: {worktree_path}")

        result = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
        )
        branch = result.stdout.strip()

        result = self._run_git(
            ["push", "-u", "origin", branch],
            cwd=worktree_path,
            check=False,
        )
        return result.returncode == 0

    def create_pr_from_worktree(
        self,
        task_id: str,
        worktree_path: Path,
        title: Optional[str] = None,
        body: Optional[str] = None,
        base_branch: str = "main",
    ) -> Optional[str]:
        """
        Create a PR from worktree branch.

        Returns PR URL on success, None on failure.
        """
        if not worktree_path.is_dir():
            raise WorkspaceError(f"Worktree not found: {worktree_path}")

        # Get branch
        result = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree_path
        )
        branch = result.stdout.strip()
        if not branch or branch == "HEAD":
            logger.error("Could not determine branch")
            return None

        # Check commits ahead of base branch
        self._run_git(["fetch", "origin", base_branch], cwd=worktree_path, check=False)

        result = self._run_git(
            ["rev-list", "--count", f"origin/{base_branch}..{branch}"],
            cwd=worktree_path,
            check=False,
        )
        commits_ahead = int(result.stdout.strip() or "0")

        if commits_ahead == 0:
            logger.info(f"No commits ahead of {base_branch}, skipping PR creation")
            return None

        # Ensure pushed
        self.push_worktree_branch(worktree_path)

        # Create PR via gh CLI
        pr_title = title or f"Task: {task_id}"
        pr_body = body or f"## Task: {task_id}\n\n---\nGenerated by loopd"

        result = subprocess.run(
            ["gh", "pr", "create",
             "--title", pr_title,
             "--body", pr_body,
             "--base", base_branch,
             "--head", branch],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )

        if result.returncode == 0 and result.stdout.strip().startswith("https://"):
            pr_url = result.stdout.strip()
            logger.info(f"PR created: {pr_url}")
            self.event_logger.github_pr_created(task_id, pr_url)
            return pr_url

        # Check if PR already exists
        result = subprocess.run(
            ["gh", "pr", "view", branch, "--json", "url", "-q", ".url"],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            pr_url = result.stdout.strip()
            logger.info(f"PR already exists: {pr_url}")
            return pr_url

        logger.error(f"Failed to create or find PR for branch: {branch}")
        return None

    # ─── Maintenance ───

    def clean_orphaned_worktrees(self, max_age_hours: Optional[int] = None) -> int:
        """Remove orphaned worktrees older than max_age_hours. Returns count."""
        import time

        max_hours = max_age_hours or self.ws_config.orphan_max_age_hours
        max_age_seconds = max_hours * 3600
        now = time.time()
        cleaned = 0

        ws_dir = self.ws_config.workspaces_dir
        if not ws_dir.exists():
            return 0

        for entry in ws_dir.iterdir():
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
            if (now - mtime) > max_age_seconds:
                logger.info(f"Cleaning orphaned worktree: {entry}")
                shutil.rmtree(entry, ignore_errors=True)
                cleaned += 1

        # Prune all bare repos
        repos_dir = self.ws_config.repos_dir
        if repos_dir.exists():
            for bare in repos_dir.glob("*.git"):
                if bare.is_dir():
                    self._run_git(["worktree", "prune"], cwd=bare, check=False)

        return cleaned

    def cleanup_old_workspaces(self, keep_count: Optional[int] = None) -> int:
        """Remove old completed workspaces, keeping N most recent. Returns count."""
        keep = keep_count or self.config.workspace_retention_count
        cleaned = 0

        from loopd_core.state.task_manager import TaskManager
        tm = TaskManager(self.config)

        # Get completed task IDs
        completed_ids = tm.list_tasks(status=__import__("loopd_core.types", fromlist=["TaskStatus"]).TaskStatus.COMPLETED)

        ws_dir = self.ws_config.workspaces_dir
        if not ws_dir.exists():
            return 0

        # Find workspace dirs for completed tasks with their mtime
        ws_entries: list[tuple[float, Path]] = []
        for task_id in completed_ids:
            for entry in ws_dir.iterdir():
                if entry.is_dir() and entry.name.startswith(f"{task_id}--"):
                    ws_entries.append((entry.stat().st_mtime, entry))

        # Sort oldest first
        ws_entries.sort(key=lambda x: x[0])

        if len(ws_entries) <= keep:
            return 0

        to_remove = len(ws_entries) - keep
        for _, ws_path in ws_entries[:to_remove]:
            dirname = ws_path.name
            task_id = dirname.split("--", 1)[0]

            logger.info(f"Removing workspace: {dirname}")
            shutil.rmtree(ws_path, ignore_errors=True)

            # Prune bare repo
            repo_part = dirname.split("--", 1)[1] if "--" in dirname else ""
            bare_repo = self.ws_config.repos_dir / f"{repo_part}.git"
            if bare_repo.is_dir():
                self._run_git(["worktree", "prune"], cwd=bare_repo, check=False)

            self.event_logger.log(
                "workspace.cleaned", task_id, {"path": str(ws_path)}
            )
            cleaned += 1

        return cleaned

    def get_workspace_stats(self) -> dict[str, Any]:
        """Get workspace usage statistics."""
        ws_dir = self.ws_config.workspaces_dir
        total = 0
        if ws_dir.exists():
            total = sum(1 for e in ws_dir.iterdir() if e.is_dir())

        return {"total_workspaces": total}
