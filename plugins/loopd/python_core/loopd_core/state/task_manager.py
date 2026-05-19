"""
Task management for oh-my-agents.

Replaces lib/task.sh with a type-safe Python implementation.
Maintains 100% compatibility with shell script task.json format.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loopd_core.config import Config, get_config
from loopd_core.state.event_logger import EventLogger
from loopd_core.state.lock_manager import LockError, LockManager
from loopd_core.types import (
    AttachedFile,
    AutonomyLog,
    HistoryEntry,
    ResumePoint,
    SlackThread,
    Task,
    TaskContext,
    TaskSource,
    TaskState,
    TaskStatus,
    Turn,
    TurnState,
    IDLE_SOURCE_TYPE,
)

logger = logging.getLogger(__name__)


class TaskNotFoundError(Exception):
    """Raised when task is not found."""
    pass


class TaskManager:
    """
    Manages task lifecycle and state.

    Replaces lib/task.sh with Python implementation while maintaining
    full compatibility with the JSON task format used by shell scripts.
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.paths = self.config.queue_paths
        self.lock_manager = LockManager(self.config)
        self.event_logger = EventLogger(self.config)

        # Ensure directories exist
        for path in [
            self.paths.pending,
            self.paths.active,
            self.paths.waiting_human,
            self.paths.completed,
            self.paths.failed,
            self.paths.preempted,
            self.paths.archive,
            self.paths.backup,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def _now_iso(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _parse_iso(self, ts: str) -> datetime:
        """Parse ISO timestamp to datetime."""
        ts = ts.rstrip("Z")
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

    def _status_to_dir(self, status: TaskStatus) -> Path:
        """Get directory path for a status."""
        mapping = {
            TaskStatus.PENDING: self.paths.pending,
            TaskStatus.ACTIVE: self.paths.active,
            TaskStatus.WAITING_HUMAN: self.paths.waiting_human,
            TaskStatus.COMPLETED: self.paths.completed,
            TaskStatus.FAILED: self.paths.failed,
            TaskStatus.PREEMPTED: self.paths.preempted,
        }
        return mapping[status]

    @staticmethod
    def _is_task_filename(filename: str) -> bool:
        """Check if filename is a valid task file (task- or gw- prefix)."""
        return filename.endswith(".json") and (
            filename.startswith("task-") or filename.startswith("gw-")
        )

    # ─────────────────────────────────────────────────────────────────
    # ID Generation
    # ─────────────────────────────────────────────────────────────────

    def _generate_task_id(self) -> str:
        """
        Generate unique task ID.

        Format: task-YYYY-MM-DD-NNN
        """
        date_str = datetime.now().strftime("%Y-%m-%d")
        pattern = re.compile(rf"task-{date_str}-(\d+)\.json")

        # Find highest sequence number across all directories
        max_seq = 0
        for status in TaskStatus:
            dir_path = self._status_to_dir(status)
            if not dir_path.exists():
                continue

            for filename in os.listdir(dir_path):
                match = pattern.match(filename)
                if match:
                    seq = int(match.group(1))
                    max_seq = max(max_seq, seq)

        # Also check backup directory
        backup_dir = self.paths.backup
        if backup_dir.exists():
            for filename in os.listdir(backup_dir):
                match = pattern.match(filename)
                if match:
                    seq = int(match.group(1))
                    max_seq = max(max_seq, seq)

        return f"task-{date_str}-{max_seq + 1:03d}"

    # ─────────────────────────────────────────────────────────────────
    # Task Creation
    # ─────────────────────────────────────────────────────────────────

    def create_task(
        self,
        prompt: str,
        source_type: str = "manual",
        source_ref: Optional[str] = None,
        title: Optional[str] = None,
        priority: int = 3,
        level: int = 1,
        workspace_repo: Optional[str] = None,
        workspace_branch: str = "main",
        metadata: Optional[dict[str, Any]] = None,
        depends_on: Optional[list[str]] = None,
        task_type: str = "dev",
    ) -> Task:
        """
        Create a new task.

        Args:
            prompt: Task description/request
            source_type: Source type (manual, cli, github, slack, webhook, idle)
            source_ref: Optional source reference (issue number, thread ID)
            title: Optional short title
            priority: Priority 1-5 (1=urgent)
            level: Complexity level 0-4
            workspace_repo: GitHub repo (owner/repo)
            workspace_branch: Base branch (default: main)
            metadata: Additional metadata

        Returns:
            Created Task object
        """
        if not prompt:
            raise ValueError("prompt is required")

        if depends_on:
            for dep_id in depends_on:
                if not self.task_exists(dep_id):
                    raise ValueError(f"Dependency task not found: {dep_id}")

        task_id = self._generate_task_id()
        now = self._now_iso()

        task = Task(
            id=task_id,
            prompt=prompt,
            title=title,
            level=level,
            priority=priority,
            task_type=task_type,
            status=TaskStatus.PENDING,
            source=TaskSource(type=source_type, ref=source_ref),
            state=TaskState(),
            requester_slack_id=(metadata or {}).get("slack_user_id"),
            context=TaskContext(),
            autonomy_log=AutonomyLog(),
            history=[],
            artifacts=[],
            metadata=metadata or {},
            depends_on=depends_on or [],
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

        if workspace_repo:
            from loopd_core.types import WorkspaceInfo
            task.workspace = WorkspaceInfo(repo=workspace_repo, branch=workspace_branch)

        # Save to pending queue
        task_file = self.paths.pending / f"{task_id}.json"
        task.save_to_file(str(task_file))

        # Log event
        self.event_logger.task_created(task_id, source_type, source_ref)

        return task

    # ─────────────────────────────────────────────────────────────────
    # Task Query
    # ─────────────────────────────────────────────────────────────────

    def get_task_path(self, task_id: str) -> Optional[Path]:
        """
        Find task file path by ID.

        Also checks the archive directory as source of truth.
        Returns None if task not found.
        """
        for status in TaskStatus:
            path = self._status_to_dir(status) / f"{task_id}.json"
            if path.exists():
                return path
        archive_path = self.paths.archive / f"{task_id}.json"
        if archive_path.exists():
            return archive_path
        return None

    def get_task_status(self, task_id: str) -> Optional[TaskStatus]:
        """
        Get task status by checking which directory it's in.

        Also checks the archive directory — archived tasks are treated as COMPLETED.
        Returns None if task not found.
        """
        for status in TaskStatus:
            path = self._status_to_dir(status) / f"{task_id}.json"
            if path.exists():
                return status
        # Archive is the source of truth for completed tasks that were moved out
        if (self.paths.archive / f"{task_id}.json").exists():
            return TaskStatus.COMPLETED
        return None

    def read_task(self, task_id: str) -> Task:
        """
        Read task by ID.

        Raises:
            TaskNotFoundError: If task not found
        """
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")
        return Task.from_json_file(str(path))

    def task_exists(self, task_id: str) -> bool:
        """Check if task exists."""
        return self.get_task_path(task_id) is not None

    def list_tasks(self, status: Optional[TaskStatus] = None) -> list[str]:
        """
        List task IDs by status.

        Args:
            status: Filter by status (None = all)

        Returns:
            List of task IDs, sorted
        """
        task_ids = []

        statuses = [status] if status else list(TaskStatus)

        for s in statuses:
            dir_path = self._status_to_dir(s)
            if not dir_path.exists():
                continue

            for filename in os.listdir(dir_path):
                if self._is_task_filename(filename):
                    task_ids.append(filename[:-5])  # Remove .json

        return sorted(task_ids)

    def count_tasks(self, status: TaskStatus) -> int:
        """Count tasks in a specific status."""
        dir_path = self._status_to_dir(status)
        if not dir_path.exists():
            return 0

        return len([
            f for f in os.listdir(dir_path)
            if self._is_task_filename(f)
        ])

    # ─────────────────────────────────────────────────────────────────
    # Task State Transitions
    # ─────────────────────────────────────────────────────────────────

    def move_task(
        self,
        task_id: str,
        new_status: TaskStatus,
        lock_timeout: int = 10
    ) -> bool:
        """
        Move task to a new status (directory).

        Uses locking to prevent race conditions.

        Args:
            task_id: Task ID
            new_status: Target status
            lock_timeout: Lock timeout in seconds

        Returns:
            True if moved successfully

        Raises:
            TaskNotFoundError: If task not found
            LockError: If lock cannot be acquired
        """
        with self.lock_manager.lock(task_id, lock_timeout):
            current_path = self.get_task_path(task_id)
            if not current_path:
                raise TaskNotFoundError(f"Task not found: {task_id}")

            target_dir = self._status_to_dir(new_status)
            target_path = target_dir / f"{task_id}.json"

            # Skip if already in target
            if current_path == target_path:
                return True

            # Read task
            task = Task.from_json_file(str(current_path))
            # Get old status as string (may be enum or string due to use_enum_values)
            old_status = task.status.value if hasattr(task.status, 'value') else task.status

            # Backup before moving
            try:
                shutil.copy(str(current_path), str(self.paths.backup))
            except Exception:
                pass  # Best effort backup

            # Update task
            task.status = new_status
            task.updated_at = datetime.now(timezone.utc)

            if new_status == TaskStatus.COMPLETED:
                task.completed_at = datetime.now(timezone.utc)

            # Write to target
            task.save_to_file(str(target_path))

            # Remove from source
            current_path.unlink()

            # Log event
            new_status_str = new_status.value if hasattr(new_status, 'value') else new_status
            self.event_logger.task_moved(task_id, old_status, new_status_str)

            return True

    def archive_task(self, task_id: str, lock_timeout: int = 10) -> bool:
        """
        Move a completed/failed task to the archive directory.

        Uses locking and backup, following the same pattern as move_task().

        Args:
            task_id: Task ID to archive
            lock_timeout: Lock timeout in seconds

        Returns:
            True if archived successfully

        Raises:
            TaskNotFoundError: If task not found
            LockError: If lock cannot be acquired
        """
        with self.lock_manager.lock(task_id, lock_timeout):
            current_path = self.get_task_path(task_id)
            if not current_path:
                raise TaskNotFoundError(f"Task not found: {task_id}")

            target_path = self.paths.archive / f"{task_id}.json"

            if current_path == target_path:
                return True

            # Backup before archiving
            try:
                shutil.copy(str(current_path), str(self.paths.backup))
            except Exception:
                pass  # Best effort backup

            # Move file to archive
            shutil.move(str(current_path), str(target_path))

            self.event_logger.log("task.archived", task_id, {
                "from": str(current_path.parent.name),
            })

            return True

    def cleanup_backups(self, max_age_days: int = 7) -> int:
        """
        Remove backup files older than max_age_days.

        Args:
            max_age_days: Maximum age in days for backup files

        Returns:
            Number of backup files removed
        """
        if not self.paths.backup.exists():
            return 0

        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=max_age_days)
        removed = 0

        for backup_file in self.paths.backup.iterdir():
            if not backup_file.is_file():
                continue
            try:
                mtime = datetime.fromtimestamp(
                    backup_file.stat().st_mtime, tz=timezone.utc
                )
                if mtime < cutoff:
                    backup_file.unlink()
                    removed += 1
            except OSError:
                pass  # Best effort cleanup

        if removed:
            self.event_logger.log("backup.cleanup", "system", {
                "removed": removed,
                "max_age_days": max_age_days,
            })

        return removed

    # ─────────────────────────────────────────────────────────────────
    # Task Update
    # ─────────────────────────────────────────────────────────────────

    def update_task(
        self,
        task_id: str,
        updates: dict[str, Any],
        use_lock: bool = True
    ) -> Task:
        """
        Update task fields.

        Args:
            task_id: Task ID
            updates: Dictionary of field updates
            use_lock: Whether to use locking

        Returns:
            Updated Task object
        """
        def do_update() -> Task:
            path = self.get_task_path(task_id)
            if not path:
                raise TaskNotFoundError(f"Task not found: {task_id}")

            # Backup
            try:
                shutil.copy(str(path), str(self.paths.backup))
            except Exception:
                pass

            # Load current task
            task = Task.from_json_file(str(path))

            # Apply updates
            for key, value in updates.items():
                if key == "context" and isinstance(value, dict):
                    # Coerce raw dict → TaskContext to trigger field validation
                    from loopd_core.types import TaskContext
                    value = TaskContext.model_validate({**task.context.model_dump(), **value})
                if hasattr(task, key):
                    setattr(task, key, value)

            task.updated_at = datetime.now(timezone.utc)

            # Save
            task.save_to_file(str(path))
            return task

        if use_lock:
            with self.lock_manager.lock(task_id, timeout=10):
                return do_update()
        else:
            return do_update()

    def update_task_state(
        self,
        task_id: str,
        phase: Optional[str] = None,
        current_agent: Optional[str] = None,
        current_subagent: Optional[str] = None,
    ) -> Task:
        """Update task execution state."""
        updates: dict[str, Any] = {}

        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        task = Task.from_json_file(str(path))

        if phase is not None:
            task.state.phase = phase
        if current_agent is not None:
            task.state.current_agent = current_agent
        if current_subagent is not None:
            task.state.current_subagent = current_subagent

        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        return task

    def append_history(
        self,
        task_id: str,
        agent: str,
        action: str,
        target: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> Task:
        """
        Append entry to task history.

        Args:
            task_id: Task ID
            agent: Agent name
            action: Action performed
            target: Optional target (agent, queue, etc.)
            data: Optional additional data
        """
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        # Backup
        try:
            shutil.copy(str(path), str(self.paths.backup))
        except Exception:
            pass

        task = Task.from_json_file(str(path))

        entry = HistoryEntry(
            agent=agent,
            action=action,
            ts=datetime.now(timezone.utc),
            target=target,
            data=data or {},
        )

        task.history.append(entry)
        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        return task

    # ─────────────────────────────────────────────────────────────────
    # Resume Points
    # ─────────────────────────────────────────────────────────────────

    def save_resume_point(
        self,
        task_id: str,
        agent: str,
        subagent: Optional[str],
        progress: str,
        next_step: str,
    ) -> Task:
        """
        Save resume point for rate-limit recovery.

        Automatically captures the last completed turn_id so that
        rate-limit resume can restart from the correct turn.
        """
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        task = Task.from_json_file(str(path))

        # Find last completed turn_id
        last_completed_turn_id = None
        for turn in reversed(task.turns):
            if turn.state == TurnState.COMPLETED:
                last_completed_turn_id = turn.turn_id
                break

        # If there's an active current_turn, end it as failed (rate limited)
        if task.current_turn and task.current_turn.state == TurnState.ACTIVE:
            task.current_turn.state = TurnState.FAILED
            task.current_turn.ended_at = datetime.now(timezone.utc)
            task.current_turn.error = "rate_limited"
            task.turns.append(task.current_turn)
            self.event_logger.turn_end(
                task_id, task.current_turn.turn_id, task.current_turn.agent,
                TurnState.FAILED.value, error="rate_limited"
            )
            task.current_turn = None

        task.resume_point = ResumePoint(
            agent=agent,
            subagent=subagent,
            progress=progress,
            next_step=next_step,
            last_completed_turn_id=last_completed_turn_id,
            saved_at=datetime.now(timezone.utc),
        )
        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        self.event_logger.task_resume_saved(task_id, agent, next_step)

        return task

    def clear_resume_point(self, task_id: str) -> Task:
        """Clear resume point."""
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        task = Task.from_json_file(str(path))
        task.resume_point = None
        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        return task

    def get_resume_point(self, task_id: str) -> Optional[ResumePoint]:
        """Get resume point if exists."""
        try:
            task = self.read_task(task_id)
            return task.resume_point
        except TaskNotFoundError:
            return None

    def has_resume_point(self, task_id: str) -> bool:
        """Check if task has a resume point."""
        return self.get_resume_point(task_id) is not None

    # ─────────────────────────────────────────────────────────────────
    # Turn Lifecycle
    # ─────────────────────────────────────────────────────────────────

    def start_turn(
        self,
        task_id: str,
        agent: str,
        subagent: Optional[str] = None,
    ) -> Turn:
        """
        Start a new turn for a task.

        Creates a Turn with sequential turn_id, sets it as current_turn,
        and logs a turn-start event.

        Returns:
            The newly created Turn
        """
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        task = Task.from_json_file(str(path))

        # Cancel any lingering active current_turn
        if task.current_turn and task.current_turn.state == TurnState.ACTIVE:
            task.current_turn.state = TurnState.CANCELLED
            task.current_turn.ended_at = datetime.now(timezone.utc)
            task.turns.append(task.current_turn)

        # Determine next turn_id
        next_id = len(task.turns) + 1

        turn = Turn(
            turn_id=next_id,
            agent=agent,
            subagent=subagent,
            state=TurnState.ACTIVE,
            started_at=datetime.now(timezone.utc),
        )

        task.current_turn = turn
        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        # Log event
        self.event_logger.turn_start(task_id, next_id, agent, subagent)

        return turn

    def end_turn(
        self,
        task_id: str,
        state: TurnState,
        result: Optional[str] = None,
        error: Optional[str] = None,
        fallback_turn: Optional[Turn] = None,
        tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
    ) -> Turn:
        """
        End the current turn for a task.

        Moves current_turn to turns[] array with final state and timestamps.

        Args:
            task_id: Task ID
            state: Final turn state (completed, failed, cancelled)
            result: Optional result summary
            error: Optional error message
            fallback_turn: Optional Turn to use if current_turn was wiped from disk
                           (race condition recovery). Caller passes the Turn returned
                           by start_turn() so we can recover gracefully.
            tokens: Approximate token count for this turn
            duration_ms: Turn execution time in milliseconds

        Returns:
            The completed Turn

        Raises:
            TaskNotFoundError: If task not found
            ValueError: If no active turn exists and no fallback provided
        """
        path = self.get_task_path(task_id)
        if not path:
            raise TaskNotFoundError(f"Task not found: {task_id}")

        task = Task.from_json_file(str(path))

        if not task.current_turn:
            if fallback_turn is not None:
                logger.warning(
                    f"[end_turn] current_turn wiped from disk for {task_id} "
                    f"(subagent={getattr(fallback_turn, 'subagent', '?')}), "
                    "recovering with in-memory fallback turn"
                )
                task.current_turn = fallback_turn
            else:
                raise ValueError(f"No active turn for task {task_id}")

        turn = task.current_turn
        turn.state = state
        turn.ended_at = datetime.now(timezone.utc)
        turn.result = result
        turn.error = error
        if tokens is not None:
            turn.tokens = tokens
        if duration_ms is not None:
            turn.duration_ms = duration_ms

        # Move to turns history
        task.turns.append(turn)
        task.current_turn = None
        task.updated_at = datetime.now(timezone.utc)
        task.save_to_file(str(path))

        # Log event
        state_value = state.value if hasattr(state, 'value') else state
        self.event_logger.turn_end(
            task_id, turn.turn_id, turn.agent, state_value, result, error
        )

        return turn

    def get_last_completed_turn(self, task_id: str) -> Optional[Turn]:
        """
        Get the last completed turn for a task.

        Used for rate-limit resume — restart from the turn after the last
        successfully completed one.

        Returns:
            The last completed Turn, or None if no completed turns exist
        """
        try:
            task = self.read_task(task_id)
        except TaskNotFoundError:
            return None

        for turn in reversed(task.turns):
            if turn.state == TurnState.COMPLETED:
                return turn
        return None

    # ─────────────────────────────────────────────────────────────────
    # Heartbeat & Processing Markers
    # ─────────────────────────────────────────────────────────────────

    def task_heartbeat(self, task_id: str) -> bool:
        """
        Update task's updated_at to signal it's alive.

        Returns False if task not found.
        """
        path = self.get_task_path(task_id)
        if not path:
            return False

        try:
            task = Task.from_json_file(str(path))
            task.updated_at = datetime.now(timezone.utc)
            task.save_to_file(str(path))
            return True
        except Exception:
            return False

    def mark_task_processing(self, task_id: str) -> None:
        """Mark task as actively processing (prevents orphan detection)."""
        marker_file = self.paths.pids / f"{task_id}.processing"
        self.paths.pids.mkdir(parents=True, exist_ok=True)
        marker_file.write_text(self._now_iso())

    def unmark_task_processing(self, task_id: str) -> None:
        """Remove task processing marker."""
        marker_file = self.paths.pids / f"{task_id}.processing"
        if marker_file.exists():
            marker_file.unlink()

    # ─────────────────────────────────────────────────────────────────
    # Orphan Detection & Recovery
    # ─────────────────────────────────────────────────────────────────

    def _is_task_pid_running(self, task_id: str) -> bool:
        """Check if task process is running via PID file."""
        pid_file = self.paths.pids / f"{task_id}.pid"
        if not pid_file.exists():
            return False

        try:
            pid = int(pid_file.read_text().strip())
            # Check if process exists (kill 0 doesn't kill, just checks)
            os.kill(pid, 0)
            return True
        except (ValueError, ProcessLookupError, PermissionError):
            return False

    def get_orphaned_tasks(self) -> list[str]:
        """
        Find orphaned tasks in the active queue.

        A task is orphaned if:
        1. No running PID process exists
        2. No startup lock exists
        3. No processing marker exists
        4. Not recently updated (beyond grace period)
        """
        now = datetime.now(timezone.utc)
        orphaned = []

        if not self.paths.active.exists():
            return orphaned

        for task_file in self.paths.active.iterdir():
            if not self._is_task_filename(task_file.name):
                continue
            task_id = task_file.stem

            # Check 1: PID running (most definitive)
            if self._is_task_pid_running(task_id):
                continue

            # Check 2: Startup lock
            lock_file = self.paths.pids / f"{task_id}.starting"
            if lock_file.is_dir():
                time_file = lock_file / "time"
                if time_file.exists():
                    try:
                        lock_time = self._parse_iso(time_file.read_text().strip())
                        if (now - lock_time).total_seconds() < 300:  # 5 min
                            continue
                    except (ValueError, OSError):
                        pass

            # Check 3: Processing marker
            processing_marker = self.paths.pids / f"{task_id}.processing"
            if processing_marker.exists():
                try:
                    marker_time = self._parse_iso(processing_marker.read_text().strip())
                    if (now - marker_time).total_seconds() < 1800:  # 30 min
                        continue
                except (ValueError, OSError):
                    pass

            # Check 4: Load task and check timestamps
            try:
                task = Task.from_json_file(str(task_file))
            except Exception:
                orphaned.append(task_id)
                continue

            # Check for recent activation
            if task.context.activation_started:
                if isinstance(task.context.activation_started, str):
                    activation_started = self._parse_iso(task.context.activation_started)
                else:
                    activation_started = task.context.activation_started

                if (now - activation_started).total_seconds() < self.config.orphan_grace_seconds:
                    continue

            # Check for recent orphan recovery
            if task.context.last_orphan_recovery:
                if isinstance(task.context.last_orphan_recovery, str):
                    last_recovery = self._parse_iso(task.context.last_orphan_recovery)
                else:
                    last_recovery = task.context.last_orphan_recovery

                if (now - last_recovery).total_seconds() < 1500:  # 25 min cooldown
                    continue

            # Check 5: Rate limit cooldown — Claude Code handles its own rate limits;
            # loopd treats every rate_limited resume as eligible for recovery.

            # Check 6: Status mismatch (crashed during transition)
            if task.status in [TaskStatus.FAILED, TaskStatus.COMPLETED, TaskStatus.WAITING_HUMAN, TaskStatus.PREEMPTED]:
                orphaned.append(task_id)
                continue

            # Check 7: Grace period based on task state
            if isinstance(task.updated_at, str):
                updated_at = self._parse_iso(task.updated_at)
            else:
                updated_at = task.updated_at

            age_seconds = (now - updated_at).total_seconds()

            # Determine effective grace period
            has_started = len(task.history) > 0
            effective_grace = (
                self.config.claude_call_grace_seconds
                if has_started
                else self.config.orphan_grace_seconds
            )

            if age_seconds >= effective_grace:
                orphaned.append(task_id)

        return orphaned

    def recover_orphaned_task(self, task_id: str) -> bool:
        """
        Recover an orphaned task.

        Moves task to appropriate queue based on its status.

        Returns:
            True if recovered, False if task not in active or recovery failed
        """
        task_file = self.paths.active / f"{task_id}.json"
        if not task_file.exists():
            return False

        # Double-check PID before recovery
        if self._is_task_pid_running(task_id):
            return False

        try:
            task = Task.from_json_file(str(task_file))
        except Exception:
            return False

        # Count recent recoveries to detect loops
        now = datetime.now(timezone.utc)
        recent_recoveries = 0
        for entry in task.history:
            if entry.action == "orphan_recovery":
                if isinstance(entry.ts, str):
                    entry_time = self._parse_iso(entry.ts)
                else:
                    entry_time = entry.ts
                if (now - entry_time).total_seconds() < 3600:  # 60 min
                    recent_recoveries += 1

        # Short-lived worker circuit breaker:
        # If worker dies within SHORT_LIFETIME_SECONDS 3× in a row → fail fast.
        # Targets the "self-kill restart loop" pattern where a worker triggers
        # daemon restart and SIGTERM kills it within seconds.
        SHORT_LIFETIME_SECONDS = 120
        CONSECUTIVE_SHORT_LIVED_THRESHOLD = 3
        # Minimum cooldown before a recovered-pending task may be activated again.
        # Prevents the same orchestrator cycle from immediately re-activating a
        # just-recovered task (recover→activate infinite loop).
        ORPHAN_RECOVERY_COOLDOWN_SECONDS = 300  # 5 minutes
        if task.context.activation_started and task.status == TaskStatus.ACTIVE:
            if isinstance(task.context.activation_started, str):
                activation_started = self._parse_iso(task.context.activation_started)
            else:
                activation_started = task.context.activation_started
            worker_lifetime = (now - activation_started).total_seconds()
            if worker_lifetime < SHORT_LIFETIME_SECONDS:
                task.context.consecutive_short_lived_count += 1
            else:
                task.context.consecutive_short_lived_count = 0

        # Determine target queue
        if task.source.type == IDLE_SOURCE_TYPE and task.metadata.get("preempted"):
            # Idle task was preempted by user_work_detected — not a failure
            target_status = TaskStatus.PREEMPTED
            recovery_reason = "idle_preempted_by_user_work"
        elif recent_recoveries >= 5:
            # Loop detected - fail the task
            target_status = TaskStatus.FAILED
            recovery_reason = "orphan_loop_breaker"
            task.resume_point = None  # Prevent auto-retry
        elif task.context.consecutive_short_lived_count >= CONSECUTIVE_SHORT_LIVED_THRESHOLD:
            # Fast-crash loop: worker dying within SHORT_LIFETIME_SECONDS repeatedly
            target_status = TaskStatus.FAILED
            recovery_reason = f"short_lived_loop_breaker (lifetime<{SHORT_LIFETIME_SECONDS}s × {task.context.consecutive_short_lived_count})"
            task.resume_point = None  # Prevent auto-retry
            logger.warning(
                f"Task {task_id}: short-lived loop breaker triggered "
                f"(consecutive={task.context.consecutive_short_lived_count}, "
                f"threshold={CONSECUTIVE_SHORT_LIVED_THRESHOLD})"
            )
        elif task.status == TaskStatus.COMPLETED:
            target_status = TaskStatus.COMPLETED
            recovery_reason = "completed_in_active"
        elif task.status == TaskStatus.FAILED:
            target_status = TaskStatus.FAILED
            recovery_reason = "failed_in_active"
        elif task.status == TaskStatus.WAITING_HUMAN:
            target_status = TaskStatus.WAITING_HUMAN
            recovery_reason = "waiting_human_in_active"
        else:
            # Check if pipeline was complete
            last_entry = task.history[-1] if task.history else None
            if last_entry and last_entry.action == "complete" and last_entry.target == "pipeline":
                target_status = TaskStatus.COMPLETED
                recovery_reason = "pipeline_complete_orphaned"
            else:
                target_status = TaskStatus.PENDING
                recovery_reason = "stale_or_crashed"

        # Snapshot mutable fields before we re-read from disk
        updated_consecutive_count = task.context.consecutive_short_lived_count
        updated_resume_point = task.resume_point

        # Record recovery in history
        self.append_history(
            task_id,
            "system",
            "orphan_recovery",
            target_status.value,
            {"reason": recovery_reason, "ts": self._now_iso()}
        )

        # Set last_orphan_recovery timestamp + persist snapshotted fields
        task = self.read_task(task_id)
        now_utc = datetime.now(timezone.utc)
        task.context.last_orphan_recovery = now_utc
        task.context.consecutive_short_lived_count = updated_consecutive_count
        task.resume_point = updated_resume_point
        # Prevent same-cycle recover→activate loop: pending tasks must wait
        # at least ORPHAN_RECOVERY_COOLDOWN_SECONDS before being activated.
        if target_status == TaskStatus.PENDING:
            task.context.cooldown_until = now_utc + timedelta(seconds=ORPHAN_RECOVERY_COOLDOWN_SECONDS)
        task_file_path = self.get_task_path(task_id)
        if task_file_path:
            task.save_to_file(str(task_file_path))

        # Clean up stale files
        for suffix in [".pid", ".heartbeat_ctl", ".processing"]:
            marker = self.paths.pids / f"{task_id}{suffix}"
            if marker.exists():
                marker.unlink()

        startup_lock = self.paths.pids / f"{task_id}.starting"
        if startup_lock.is_dir():
            shutil.rmtree(startup_lock)

        # Move to target queue
        self.move_task(task_id, target_status)

        self.event_logger.task_orphan_recovered(task_id, target_status.value, recovery_reason)

        return True

    def recover_all_orphaned_tasks(self) -> int:
        """
        Recover all orphaned tasks.

        Returns:
            Number of tasks recovered
        """
        count = 0
        for task_id in self.get_orphaned_tasks():
            if self.recover_orphaned_task(task_id):
                count += 1
        return count

    # ─────────────────────────────────────────────────────────────────
    # Migration: Archive stale completed/failed tasks
    # ─────────────────────────────────────────────────────────────────

    def migrate_archive_stale_tasks(self) -> int:
        """
        Migration option B: move completed/failed tasks without
        github_issue_number directly to archive (skip Issue creation).

        Returns:
            Number of tasks archived.
        """
        count = 0

        for status in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PREEMPTED]:
            dir_path = self._status_to_dir(status)
            if not dir_path.exists():
                continue

            for filename in os.listdir(dir_path):
                if not self._is_task_filename(filename):
                    continue

                task_id = filename[:-5]  # Remove .json
                filepath = dir_path / filename

                try:
                    task = Task.from_json_file(str(filepath))
                except Exception:
                    continue

                # Only archive tasks without github_issue_number
                if task.metadata.get("github_issue_number"):
                    continue

                try:
                    self.archive_task(task_id)
                    count += 1
                except Exception:
                    pass  # Best effort

        return count
