"""
Lock management for atomic task operations.

Uses mkdir-based locking (atomic on POSIX) for compatibility with shell scripts.
This ensures that both Python and shell code can safely coordinate access.
"""

from __future__ import annotations

import os
import shutil
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Generator, Optional

from loopd_core.config import Config, get_config


class LockError(Exception):
    """Raised when lock acquisition fails."""
    pass


class LockManager:
    """
    Manages task-level locks using atomic mkdir.

    Compatible with the shell script locking mechanism in lib/task.sh.
    Uses mkdir for atomic lock creation (POSIX guarantee).
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.locks_dir = self.config.queue_paths.locks
        self.locks_dir.mkdir(parents=True, exist_ok=True)

    def _lock_path(self, task_id: str) -> Path:
        """Get lock directory path for a task."""
        return self.locks_dir / f"{task_id}.lock"

    def _parse_iso_timestamp(self, ts: str) -> datetime:
        """Parse ISO 8601 timestamp to datetime."""
        ts = ts.rstrip("Z")
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)

    def _format_iso_timestamp(self, dt: Optional[datetime] = None) -> str:
        """Format datetime as ISO 8601 UTC timestamp."""
        if dt is None:
            dt = datetime.now(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _get_lock_age_seconds(self, lock_path: Path) -> int:
        """Get age of lock in seconds."""
        time_file = lock_path / "time"
        if not time_file.exists():
            return 0

        try:
            with open(time_file) as f:
                lock_time = self._parse_iso_timestamp(f.read().strip())
            now = datetime.now(timezone.utc)
            return int((now - lock_time).total_seconds())
        except (ValueError, OSError):
            return 0

    def _is_stale_lock(self, lock_path: Path) -> bool:
        """Check if lock is stale (older than stale_lock_seconds)."""
        age = self._get_lock_age_seconds(lock_path)
        return age > self.config.stale_lock_seconds

    def _remove_stale_lock(self, lock_path: Path) -> bool:
        """Remove a stale lock directory."""
        try:
            shutil.rmtree(lock_path)
            return True
        except OSError:
            return False

    def acquire(self, task_id: str, timeout: Optional[int] = None) -> bool:
        """
        Acquire lock for a task.

        Uses atomic mkdir - if mkdir succeeds, we have the lock.

        Args:
            task_id: Task ID to lock
            timeout: Timeout in seconds (default from config)

        Returns:
            True if lock acquired, False if timeout reached
        """
        if timeout is None:
            timeout = self.config.lock_timeout_seconds

        lock_path = self._lock_path(task_id)
        start_time = time.time()
        pid = os.getpid()

        while True:
            try:
                # Atomic mkdir - if this succeeds, we have the lock
                lock_path.mkdir(parents=True, exist_ok=False)

                # Write metadata
                (lock_path / "pid").write_text(str(pid))
                (lock_path / "time").write_text(self._format_iso_timestamp())

                return True

            except FileExistsError:
                # Lock exists - check if stale
                if self._is_stale_lock(lock_path):
                    if self._remove_stale_lock(lock_path):
                        continue  # Try again

                # Check timeout
                elapsed = time.time() - start_time
                if elapsed >= timeout:
                    return False

                # Wait before retry
                time.sleep(0.5)

    def release(self, task_id: str) -> bool:
        """
        Release lock for a task.

        Only releases if we own the lock (same PID).

        Args:
            task_id: Task ID to unlock

        Returns:
            True if lock released, False if we don't own it
        """
        lock_path = self._lock_path(task_id)
        pid_file = lock_path / "pid"

        if not pid_file.exists():
            return False

        try:
            lock_pid = int(pid_file.read_text().strip())
            if lock_pid != os.getpid():
                return False

            shutil.rmtree(lock_path)
            return True

        except (ValueError, OSError):
            return False

    def is_locked(self, task_id: str) -> bool:
        """Check if task is currently locked."""
        return self._lock_path(task_id).is_dir()

    def get_lock_owner(self, task_id: str) -> Optional[int]:
        """Get PID of lock owner, if locked."""
        lock_path = self._lock_path(task_id)
        pid_file = lock_path / "pid"

        if not pid_file.exists():
            return None

        try:
            return int(pid_file.read_text().strip())
        except (ValueError, OSError):
            return None

    def force_release(self, task_id: str) -> bool:
        """
        Force release a lock regardless of owner.

        Use with caution - only for cleanup of known stale locks.
        """
        lock_path = self._lock_path(task_id)
        if lock_path.is_dir():
            try:
                shutil.rmtree(lock_path)
                return True
            except OSError:
                return False
        return False

    @contextmanager
    def lock(
        self,
        task_id: str,
        timeout: Optional[int] = None
    ) -> Generator[None, None, None]:
        """
        Context manager for task locking.

        Usage:
            with lock_manager.lock("task-001"):
                # Critical section
                update_task(...)

        Raises:
            LockError: If lock cannot be acquired within timeout
        """
        if not self.acquire(task_id, timeout):
            raise LockError(f"Could not acquire lock for {task_id}")

        try:
            yield
        finally:
            self.release(task_id)

    def cleanup_stale_locks(self) -> int:
        """
        Clean up all stale locks.

        Returns:
            Number of locks cleaned up
        """
        count = 0
        if not self.locks_dir.exists():
            return count

        for lock_dir in self.locks_dir.iterdir():
            if lock_dir.is_dir() and lock_dir.suffix == ".lock":
                if self._is_stale_lock(lock_dir):
                    if self._remove_stale_lock(lock_dir):
                        count += 1

        return count
