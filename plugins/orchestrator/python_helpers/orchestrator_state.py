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

# Scout-cycle statuses (lives on state.scout_status, not issue.status).
_SCOUT_STATUSES = {
    "scout_new",
    "scout_pending",
    "scout_received",
    "scout_clarify_pending",
    "scout_confirm_pending",
    "scout_creating",
    "scout_done",
    "scout_failed",
}

# Rev 17 — Planning layer cycle statuses (separate enums, stored on
# state.planner_status / state.roadmap_status / state.vision_check_status).
_PLANNING_STATUSES = {
    "planning_pending",
    "planning_creating",
    "planning_done",
    "planning_failed",
    "planning_parked",
}

_ROADMAP_STATUSES = {
    "roadmap_pending",
    "roadmap_received",
    "roadmap_done",
}

_VISION_CHECK_STATUSES = {
    "vision_check_pending",
    "vision_check_received",
    "vision_check_done",
    "vision_check_parked",
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
        # === Rev 17 (Planning layer) ===
        # Product-planner cycle
        "planner_status": None,
        "planner_started_at": None,
        "planner_candidates_buffer": None,
        "planner_candidates": [],
        "planner_creating_done": [],
        "planner_creating_lock_started_at": None,
        "planner_creating_lock_owner": None,
        "planner_history": [],
        "planner_decisions": {},
        "planner_confirm_idx": 0,
        "planner_created_urls": [],
        "planner_failed_creations": [],
        "planner_message": None,
        "planning_retried": False,
        # Stage 1 integration (scout + planner)
        "stage1_creating_lock_started_at": None,
        "stage1_creating_lock_owner": None,
        "scout_candidates_buffer": None,
        "stage1_merge_pending": False,
        "last_stage1_completed_at": None,
        "planning_pending_resolved_at": None,
        "scout_pending_resolved_at": None,
        # Roadmap cycle
        "roadmap_status": None,
        "roadmap_started_at": None,
        "roadmap_reports": [],
        "active_phase_context": None,
        "active_phase_context_until_cycle": 0,
        "last_roadmap_report_cycle": 0,
        "roadmap_retried": False,
        # Vision-critic cycle
        "vision_check_status": None,
        "vision_check_started_at": None,
        "vision_critic_retried": False,
        "vision_critic_history": [],
        "vision_critic_pending_delta": None,
        "last_vision_critic_cycle": 0,
        "rejected_delta_hashes": [],
        # Lazy spawn / teammate health (Phase 17-G)
        "pending_team_spawns": [],
        "teammate_health": {},
        "pending_respawn": {},
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


def scout_transition(state: Dict[str, Any], new_status: str) -> None:
    """Record a scout-cycle status transition on state.scout_history_log.

    Same-status transitions are idempotent. The scout cycle uses a
    separate enum from issue.status, so we validate against
    `_SCOUT_STATUSES`.
    """
    if new_status not in _SCOUT_STATUSES:
        raise ValueError(f"unknown scout status: {new_status}")
    old = state.get("scout_status")
    if old == new_status:
        return
    state.setdefault("scout_history_log", []).append(
        {"at": _iso(now()), "from": old, "to": new_status}
    )
    state["scout_status"] = new_status


def planning_transition(state: Dict[str, Any], new_status: Optional[str]) -> None:
    """Record a planning-cycle status transition.

    Stored on `state.planner_status`. `None` (cycle end) is accepted to
    return to idle. Otherwise validated against `_PLANNING_STATUSES`.
    """
    if new_status is not None and new_status not in _PLANNING_STATUSES:
        raise ValueError(f"unknown planning status: {new_status}")
    old = state.get("planner_status")
    if old == new_status:
        return
    state.setdefault("planning_history_log", []).append(
        {"at": _iso(now()), "from": old, "to": new_status}
    )
    state["planner_status"] = new_status


def roadmap_transition(state: Dict[str, Any], new_status: Optional[str]) -> None:
    """Record a roadmap-cycle status transition. `None` returns to idle."""
    if new_status is not None and new_status not in _ROADMAP_STATUSES:
        raise ValueError(f"unknown roadmap status: {new_status}")
    old = state.get("roadmap_status")
    if old == new_status:
        return
    state.setdefault("roadmap_history_log", []).append(
        {"at": _iso(now()), "from": old, "to": new_status}
    )
    state["roadmap_status"] = new_status


def vision_transition(state: Dict[str, Any], new_status: Optional[str]) -> None:
    """Record a vision-check-cycle status transition. `None` returns to idle."""
    if new_status is not None and new_status not in _VISION_CHECK_STATUSES:
        raise ValueError(f"unknown vision_check status: {new_status}")
    old = state.get("vision_check_status")
    if old == new_status:
        return
    state.setdefault("vision_check_history_log", []).append(
        {"at": _iso(now()), "from": old, "to": new_status}
    )
    state["vision_check_status"] = new_status


def current_session_id() -> str:
    """Return the live Claude Code session UUID for the lead window.

    Source of truth: env vars injected by the Claude Code harness. Mirrors
    loopd's `_session_id()` precedence (loopd/python_core/loopd_core/tick.py)
    so the value matches what the loopd Stop hook and the orchestrator Stop
    hook both see on their payloads.

    Raises:
        RuntimeError: when neither env var is set. The lead must NOT fall
            back to a placeholder UUID — doing so causes the orch_stop hook
            Gate 1 (`state.dev_session_id != payload.session_id`) to
            mismatch forever and silently break dev-done auto-resume.
    """
    sid = os.environ.get("LOOPD_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if not sid:
        raise RuntimeError(
            "current_session_id: neither LOOPD_SESSION_ID nor CLAUDE_SESSION_ID "
            "is set in the lead's environment. The β Stop hook requires the "
            "real Claude Code session UUID to match dev_session_id; using a "
            "placeholder would silently break dev-done auto-resume."
        )
    return sid


def mark_dev_started(state: Dict[str, Any], session_id: str) -> None:
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError(
            "mark_dev_started: session_id must be a non-empty string from "
            "current_session_id(); refusing to store a placeholder that would "
            "break the orch_stop hook Gate 1 match."
        )
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


# =====================================================================
# Rev 17 Phase 17-G — long-term memory pruning
# =====================================================================

# (field, max_entries) — FIFO caps from §12.2.
_FIFO_CAPS: Dict[str, int] = {
    "lessons_learned": 100,
    "scout_history": 50,
    "scout_history_log": 200,
    "planning_history_log": 200,
    "roadmap_history_log": 200,
    "vision_check_history_log": 200,
    "vision_history": 30,
    "vision_critic_history": 20,
    "planner_history": 50,
    "roadmap_reports": 10,
    "feedback_log": 100,
}

# (field, ttl_days) — TTL prune from §12.2.
_TTL_DAYS: Dict[str, int] = {
    "lessons_learned": 90,
    "vision_critic_history": 180,
    "planner_history": 120,
    "roadmap_reports": 90,
    "feedback_log": 60,
    "rejected_delta_hashes": 30,
}

# Single-slot fields that must never be touched by the pruner (Round A S7).
_PRUNE_BLOCKLIST = frozenset(
    {
        "vision_critic_pending_delta",
        "active_phase_context",
        "current_issue",
        "teammate_health",
    }
)

# Soft / hard watermarks for STATE_PATH file size (§12.2).
STATE_SIZE_WARN_BYTES = 2 * 1024 * 1024
STATE_SIZE_HARD_BYTES = 5 * 1024 * 1024


def prune_state_history(state: Dict[str, Any]) -> Dict[str, int]:
    """Apply FIFO caps + TTL to long-term history fields.

    Called from Step −1 once per invocation. Returns
    `{field: pruned_count}` for digest.

    Single-slot fields in `_PRUNE_BLOCKLIST` are skipped entirely
    (Round A S7 hard guard).
    """
    now_dt = now()
    pruned: Dict[str, int] = {}

    for field, cap in _FIFO_CAPS.items():
        if field in _PRUNE_BLOCKLIST:
            continue
        lst = state.get(field)
        if not isinstance(lst, list):
            continue
        if len(lst) > cap:
            pruned[field] = pruned.get(field, 0) + (len(lst) - cap)
            state[field] = lst[-cap:]

    for field, days in _TTL_DAYS.items():
        if field in _PRUNE_BLOCKLIST:
            continue
        lst = state.get(field)
        if not isinstance(lst, list):
            continue
        cutoff = (now_dt - _dt.timedelta(days=days)).isoformat()
        before = len(lst)
        kept: list = []
        for entry in lst:
            if not isinstance(entry, dict):
                # Preserve non-dict entries (e.g. legacy list[str]
                # vision_history).
                kept.append(entry)
                continue
            ts = (
                entry.get("ts")
                or entry.get("last_at")
                or entry.get("last_seen_at")
                or entry.get("first_at")
                or "9999"
            )
            if ts >= cutoff:
                kept.append(entry)
        if len(kept) < before:
            pruned[field] = pruned.get(field, 0) + (before - len(kept))
            state[field] = kept

    return pruned


def state_file_size_bytes() -> int:
    """Return current STATE_PATH size in bytes (0 if missing)."""
    if not STATE_PATH.exists():
        return 0
    try:
        return STATE_PATH.stat().st_size
    except OSError:
        return 0


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
    "scout_transition",
    "planning_transition",
    "roadmap_transition",
    "vision_transition",
    "mark_dev_started",
    "mark_dev_done_injected",
    "update_issue",
    "get_issue",
    "reset_to_empty",
    "prune_state_history",
    "state_file_size_bytes",
    "STATE_SIZE_WARN_BYTES",
    "STATE_SIZE_HARD_BYTES",
]
