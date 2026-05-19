"""
Artifact management for oh-my-agents.

Replaces artifact functions from lib/handoff.sh (L186-257).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from loopd_core.config import Config, get_config
from loopd_core.state.task_manager import TaskManager

logger = logging.getLogger(__name__)


class ArtifactManager:
    """
    Manages task artifacts (files produced by agents).

    Artifacts are stored in _artifacts/{task_id}/ directory.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.artifacts_dir = self.config.project_root / "_artifacts"
        self.task_manager = TaskManager(self.config)

    def _task_dir(self, task_id: str) -> Path:
        return self.artifacts_dir / task_id

    def get_artifact_path(self, task_id: str, filename: str) -> Path:
        """Get filesystem path for a task artifact."""
        return self._task_dir(task_id) / filename

    def save_artifact(self, task_id: str, filename: str, content: str) -> Path:
        """
        Save artifact content to file and register in task.

        Returns the artifact path.
        """
        artifact_dir = self._task_dir(task_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        artifact_path = artifact_dir / filename
        artifact_path.write_text(content, encoding="utf-8")

        self.register_existing_artifact(task_id, filename)

        logger.info(f"Saved artifact: {artifact_path}")
        return artifact_path

    def register_existing_artifact(self, task_id: str, filename: str) -> None:
        """Register an already-on-disk file into task.artifacts (idempotent).

        Use this when an agent has written a file directly to the artifacts
        directory without going through save_artifact().
        """
        task = self.task_manager.read_task(task_id)
        existing_paths = [a.path for a in task.artifacts]
        if filename not in existing_paths:
            from loopd_core.types import Artifact

            artifact_type = filename.rsplit(".", 1)[0] if "." in filename else filename
            task.artifacts.append(Artifact(type=artifact_type, path=filename))
            task_path = self.task_manager.get_task_path(task_id)
            if task_path:
                task.save_to_file(str(task_path))

    def read_artifact(self, task_id: str, filename: str) -> Optional[str]:
        """Read artifact content. Returns None if not found."""
        artifact_path = self.get_artifact_path(task_id, filename)
        if artifact_path.exists():
            return artifact_path.read_text(encoding="utf-8")
        return None

    def list_artifacts(self, task_id: str) -> list[str]:
        """List artifact filenames for a task."""
        artifact_dir = self._task_dir(task_id)
        if not artifact_dir.exists():
            return []
        return sorted(f.name for f in artifact_dir.iterdir() if f.is_file())

    def has_artifact(self, task_id: str, filename: str) -> bool:
        """Check if artifact exists on disk."""
        return self.get_artifact_path(task_id, filename).exists()
