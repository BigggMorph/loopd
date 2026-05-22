"""State management for the orchestrator plugin.

State lives at ~/.loopd/orchestrator/state.json, protected by fcntl.flock,
written atomically (tmp + fsync + rename). The schema is documented in
docs/orchestrator-design.md §10.

This module is intentionally dependency-free (stdlib only). It is imported
by both the lead playbook helpers and the Stop hook (orch_stop.py), so it
must not require packages that aren't guaranteed to be on PYTHONPATH.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA_VERSION = 3
ORCHESTRATOR_DIR = Path(os.path.expanduser("~/.loopd/orchestrator"))
STATE_PATH = ORCHESTRATOR_DIR / "state.json"
LOCK_PATH = ORCHESTRATOR_DIR / "state.lock"
AUDIT_ARCHIVE_DIR = ORCHESTRATOR_DIR / "audit_archive"
SENTINEL_DEV_DONE = ORCHESTRATOR_DIR / "dev_done_pending.flag"

_ACTIVE_STATUSES = {
    "new",
    "analyze_pending",
    "analyze_received",
    "human_qa_pending",
    "ready_for_dev",
    "dev_running",
    "dev_done",
    "test_pending",
    "test_received",
    "merge_pending",
    "split_confirm_pending",
    "split_creating",
    "split_done",
    "split_failed",
    "reject_confirm_pending",
    "merged_observing",
    "regression_detected",
    "pr_audit_pending",
    "waiting_on_dep",
}

_TERMINAL_STATUSES = {
    "done",
    "needs_human",
    "skipped_by_human",
    "parked_awaiting_human",
    "rejected",
    "done_final",
    "reverted",
}


def now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _iso(dt: Optional[_dt.datetime]) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    return dt.isoformat()


def _ensure_dirs() -> None:
    ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    # chmod 700 — sensitive (vision text, audit log).
    try:
        os.chmod(ORCHESTRATOR_DIR, 0o700)
    except OSError:
        pass


def _empty_state() -> Dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "updated_at": _iso(now()),
        "vision": "",
        "vision_history": [],
        "repo": "",
        "team_name": "",
        "mode": "resolution",
        "current_issue": None,
        "dev_session_id": None,
        "dev_done_injected": False,
        "dev_started_at": None,
        "auto_merge_consecutive_safe": 0,
        "last_protection_check": None,
        "last_picked_at": {},
        "scout_status": None,
        "scout_bootstrap_done": False,
        "scout_creating_done": [],
        "scout_creating_lock_started_at": None,
        "scout_creating_lock_owner": None,
        "scout_confirm_started_at": None,
        "scout_question_emitted": False,
        "scout_message": None,
        "scout_started_at": None,
        "scout_candidates": [],
        "scout_decisions": {},
        "scout_confirm_idx": 0,
        "scout_clarify_question": None,
        "scout_created_urls": [],
        "scout_failed_creations": [],
        "consecutive_empty_scouts": 0,
        "last_empty_scout_at": None,
        "scout_history": [],
        "issues": {},
        "completed_count": 0,
        "rejected_count": 0,
        "started_at": _iso(now()),
        "lessons_learned": [],
        "feedback_log": [],
        "last_digest_at": None,
        "last_reflection_count": 0,
        "reflection_pending": False,
        "last_main_health_check": None,
        "main_branch_red": False,
        "pending_questions": [],
        "audit_log": [],
        "watch_list": [],
        "last_pr_audit_at": None,
    }


def _atomic_write(path: Path, payload: str) -> None:
    """tmp + fsync + rename → atomic on POSIX."""
    parent = path.parent
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _serialize(state: Dict[str, Any]) -> str:
    state["updated_at"] = _iso(now())
    state["version"] = SCHEMA_VERSION
    return json.dumps(state, indent=2, sort_keys=True, default=_iso)


def read() -> Dict[str, Any]:
    """Read state.json; create empty state if missing.

    File-level read lock prevents partial-write reads when another writer
    is mid-rename (very small window, but cheap to be safe).
    """
    _ensure_dirs()
    if not STATE_PATH.exists():
        return _empty_state()
    # Shared lock for read.
    lock_fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            raw = STATE_PATH.read_text()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"orchestrator state.json corrupt: {exc}") from exc
    return _normalize(state)


def _normalize(state: Dict[str, Any]) -> Dict[str, Any]:
    """Backfill missing fields from new schema versions."""
    defaults = _empty_state()
    for k, v in defaults.items():
        state.setdefault(k, v)
    state["version"] = SCHEMA_VERSION
    return state


def write(state: Dict[str, Any]) -> None:
    _ensure_dirs()
    lock_fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            _atomic_write(STATE_PATH, _serialize(state))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


@contextlib.contextmanager
def flock_session(timeout_s: float = 30.0) -> Iterator[Dict[str, Any]]:
    """Exclusive flock around a read-decide-write transaction.

    Caller MUST invoke `write_in_lock(state)` inside the block to persist
    changes; otherwise edits are discarded.
    """
    _ensure_dirs()
    lock_fd = os.open(str(LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout_s
    acquired = False
    last_err: Optional[OSError] = None
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                last_err = exc
                time.sleep(0.1)
        if not acquired:
            raise TimeoutError(
                f"flock_session: could not acquire lock within {timeout_s}s "
                f"(last error: {last_err})"
            )
        if STATE_PATH.exists():
            state = json.loads(STATE_PATH.read_text())
            state = _normalize(state)
        else:
            state = _empty_state()
        state["__in_lock__"] = True
        try:
            yield state
        finally:
            state.pop("__in_lock__", None)
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def write_in_lock(state: Dict[str, Any]) -> None:
    """Persist state from inside a flock_session() block.

    Does not re-acquire the lock; relies on the caller still holding it.
    Raises if called outside a flock_session().
    """
    if not state.pop("__in_lock__", False):
        raise RuntimeError("write_in_lock called outside flock_session()")
    _atomic_write(STATE_PATH, _serialize(state))
    state["__in_lock__"] = True  # restore so caller can keep mutating


def transition(issue: Dict[str, Any], new_status: str) -> None:
    """Record a status transition in issue.history.

    Same-status transitions are idempotent (no-op, no history entry).
    """
    if new_status not in _ACTIVE_STATUSES and new_status not in _TERMINAL_STATUSES:
        raise ValueError(f"unknown status: {new_status}")
    old = issue.get("status")
    if old == new_status:
        return
    issue.setdefault("history", []).append(
        {"at": _iso(now()), "from": old, "to": new_status}
    )
    issue["status"] = new_status


def mark_dev_started(state: Dict[str, Any], session_id: str) -> None:
    state["dev_session_id"] = session_id
    state["dev_done_injected"] = False
    state["dev_started_at"] = _iso(now())


def mark_dev_done_injected(state: Dict[str, Any]) -> None:
    state["dev_done_injected"] = True


def update_issue(num: int, **fields: Any) -> Dict[str, Any]:
    """Read → modify → write atomically."""
    with flock_session() as state:
        issues = state.setdefault("issues", {})
        key = str(num)
        issue = issues.setdefault(key, {"number": num, "status": "new", "history": []})
        for k, v in fields.items():
            issue[k] = v
        write_in_lock(state)
        return issue


def get_issue(state: Dict[str, Any], num: Any) -> Optional[Dict[str, Any]]:
    if num is None:
        return None
    return state.get("issues", {}).get(str(num))


def reset_to_empty() -> None:
    """Test helper — delete state.json so the next read() yields a fresh state."""
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    if LOCK_PATH.exists():
        with contextlib.suppress(FileNotFoundError):
            LOCK_PATH.unlink()


__all__ = [
    "SCHEMA_VERSION",
    "ORCHESTRATOR_DIR",
    "STATE_PATH",
    "LOCK_PATH",
    "SENTINEL_DEV_DONE",
    "now",
    "read",
    "write",
    "flock_session",
    "write_in_lock",
    "transition",
    "mark_dev_started",
    "mark_dev_done_injected",
    "update_issue",
    "get_issue",
    "reset_to_empty",
]
