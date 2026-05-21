"""Configuration for loopd_core.

Storage root defaults to ``~/.loopd/`` and can be overridden with the
``LOOPD_ROOT`` environment variable. There is no Slack/Gateway/daemon
plumbing — the loopd plugin runs synchronously inside a Claude Code session.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _default_loopd_root() -> Path:
    override = os.environ.get("LOOPD_ROOT")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".loopd"


def _default_plugin_root() -> Path:
    override = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if override:
        return Path(override)
    # Fallback for local development: <repo>/python_core/loopd_core/config.py
    return Path(__file__).resolve().parent.parent.parent


class QueuePaths(BaseModel):
    root: Path
    pending: Path
    active: Path
    waiting_human: Path
    completed: Path
    failed: Path
    preempted: Path
    archive: Path
    backup: Path
    locks: Path
    pids: Path

    @classmethod
    def from_root(cls, queue_root: Path) -> "QueuePaths":
        return cls(
            root=queue_root,
            pending=queue_root / "pending",
            active=queue_root / "active",
            waiting_human=queue_root / "waiting_human",
            completed=queue_root / "completed",
            failed=queue_root / "failed",
            preempted=queue_root / "preempted",
            archive=queue_root / "archive",
            backup=queue_root / ".backup",
            locks=queue_root / ".locks",
            pids=queue_root / ".pids",
        )


class GitHubConfig(BaseModel):
    token: Optional[str] = None
    default_repo: Optional[str] = None


class Config(BaseSettings):
    loopd_root: Path = Field(default_factory=_default_loopd_root)
    plugin_root: Path = Field(default_factory=_default_plugin_root)

    project_root: Path = Field(default_factory=lambda: Path.cwd())

    github_token: Optional[str] = Field(default=None, description="GITHUB_TOKEN")

    max_pipeline_iterations: int = 15
    lock_timeout_seconds: int = 30
    stale_lock_seconds: int = 300
    orphan_grace_seconds: int = 1200
    claude_call_grace_seconds: int = 7200
    heartbeat_interval_seconds: int = 60

    workspace_retention_count: int = 5

    model_config = ConfigDict(
        env_prefix="LOOPD_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def queue_paths(self) -> QueuePaths:
        return QueuePaths.from_root(self.loopd_root / "state")

    @property
    def workspaces_path(self) -> Path:
        return self.loopd_root / "workspaces"

    @property
    def repos_path(self) -> Path:
        return self.loopd_root / "repos"

    @property
    def sessions_path(self) -> Path:
        return self.loopd_root / "sessions"

    @property
    def pending_sessions_path(self) -> Path:
        return self.sessions_path / ".pending"

    @property
    def agents_path(self) -> Path:
        return self.plugin_root / "_agents_data"

    @property
    def state_path(self) -> Path:
        return self.loopd_root / "state"

    @property
    def artifacts_path(self) -> Path:
        return self.loopd_root / "artifacts"

    @property
    def events_path(self) -> Path:
        return self.loopd_root / "events"

    @property
    def execution_records_path(self) -> Path:
        return self.loopd_root / "execution_records"

    @property
    def config_path(self) -> Path:
        return self.plugin_root / "_config"

    @property
    def lib_path(self) -> Path:
        return self.plugin_root / "lib"

    def load_github_config(self) -> GitHubConfig:
        return GitHubConfig(
            token=self.github_token or os.environ.get("GITHUB_TOKEN"),
            default_repo=os.environ.get("LOOPD_DEFAULT_REPO"),
        )

    def ensure_directories(self) -> None:
        self.loopd_root.mkdir(parents=True, exist_ok=True)
        qp = self.queue_paths
        for d in (qp.root, qp.pending, qp.active, qp.waiting_human, qp.completed,
                  qp.failed, qp.preempted, qp.archive, qp.backup, qp.locks, qp.pids):
            d.mkdir(parents=True, exist_ok=True)
        self.workspaces_path.mkdir(parents=True, exist_ok=True)
        self.repos_path.mkdir(parents=True, exist_ok=True)
        self.sessions_path.mkdir(parents=True, exist_ok=True)
        self.pending_sessions_path.mkdir(parents=True, exist_ok=True)
        self.artifacts_path.mkdir(parents=True, exist_ok=True)
        self.events_path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()


def load_env_files() -> None:
    """Load env files from $LOOPD_ROOT/.env (chmod 600 recommended)."""
    root = _default_loopd_root()
    candidates = [root / ".env", Path.cwd() / ".env"]
    try:
        from dotenv import load_dotenv

        for f in candidates:
            if f.exists():
                load_dotenv(f, override=False)
    except ImportError:
        pass


load_env_files()
