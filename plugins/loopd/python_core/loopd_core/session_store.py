"""Centralised storage for per-CC-session loopd state.

This module is the single source of truth for the mapping between Claude
Code session UUIDs and on-disk loopd session files. All callers — both
``tick.py`` and the three hook scripts — go through these helpers so the
rule "no cwd-hash fallback, no cross-window leak" lives in one place.

Layout under ``~/.loopd/sessions/``::

    <cc-uuid>.json          # one per active CC window that originated a task
    .pending/<task_id>.json # bootstrap claim written by `tick init` when no
                            # UUID is available; consumed by the first
                            # PreToolUse hook in the originator window.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows / non-POSIX
    _fcntl = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Path helpers
# ─────────────────────────────────────────────────────────────────────────────


def _sessions_dir() -> Path:
    """Return the active sessions directory, ensuring it exists.

    Also ensures ``.pending/`` exists so the two callers (tick + hooks) never
    race on directory creation.
    """
    from loopd_core.config import get_config

    cfg = get_config()
    cfg.sessions_path.mkdir(parents=True, exist_ok=True)
    cfg.pending_sessions_path.mkdir(parents=True, exist_ok=True)
    return cfg.sessions_path


def _pending_dir() -> Path:
    from loopd_core.config import get_config

    cfg = get_config()
    cfg.sessions_path.mkdir(parents=True, exist_ok=True)
    cfg.pending_sessions_path.mkdir(parents=True, exist_ok=True)
    return cfg.pending_sessions_path


def session_path_for(sid: str) -> Path:
    """Return ``~/.loopd/sessions/<sid>.json``.

    Raises ``ValueError`` if ``sid`` is empty or starts with ``"cwd-"`` —
    the new code never produces those, but legacy callers must blow up
    loudly rather than silently re-creating the cross-window-leak bug.
    """
    if not sid:
        raise ValueError("session_path_for: sid must be a non-empty string")
    if sid.startswith("cwd-"):
        raise ValueError(
            "session_path_for: refusing cwd-hashed sid; loopd now keys "
            "sessions by Claude Code UUID only"
        )
    return _sessions_dir() / f"{sid}.json"


def pending_path_for(task_id: str) -> Path:
    if not task_id:
        raise ValueError("pending_path_for: task_id must be a non-empty string")
    return _pending_dir() / f"{task_id}.json"


def _pending_lock_path_for(task_id: str) -> Path:
    return _pending_dir() / f".{task_id}.lock"


@contextlib.contextmanager
def _pending_claim_lock(task_id: str) -> Iterator[bool]:
    """Per-task advisory lock around pending-file claim operations.

    Acquires ``fcntl.flock`` (LOCK_EX | LOCK_NB) on
    ``~/.loopd/sessions/.pending/.<task_id>.lock``. Yields ``True`` when the
    lock was acquired, ``False`` when another process holds it (caller should
    skip this entry). On platforms without ``fcntl`` (e.g. Windows) the
    context is a best-effort no-op that always yields ``True``.
    """
    if _fcntl is None:
        yield True
        return

    lock_path = _pending_lock_path_for(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                _fcntl.flock(fd, _fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            lock_path.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write helper
# ─────────────────────────────────────────────────────────────────────────────


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str))
    os.replace(tmp, target)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─────────────────────────────────────────────────────────────────────────────
# Active session API
# ─────────────────────────────────────────────────────────────────────────────


def read_session(sid: str) -> dict[str, Any]:
    """Return the session JSON, or ``{}`` if missing / malformed."""
    f = session_path_for(sid)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def write_session(sid: str, data: dict[str, Any]) -> None:
    _atomic_write_json(session_path_for(sid), data)


def delete_session(sid: str) -> None:
    f = session_path_for(sid)
    if f.exists():
        f.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Pending claim API (bootstrap path)
# ─────────────────────────────────────────────────────────────────────────────


def write_pending(task_id: str, payload: dict[str, Any]) -> Path:
    """Write a pending claim file. ``payload`` should already contain
    ``task_id``, ``validation_token``, ``next_action``, ``created_at``.
    """
    target = pending_path_for(task_id)
    enriched = dict(payload)
    enriched.setdefault("created_at", _now_iso())
    _atomic_write_json(target, enriched)
    return target


def claim_pending(task_id: str, validation_token: str, sid: str) -> Optional[Path]:
    """Claim a pending file by exact task_id + validation_token match.

    Re-writes the canonical session payload to ``sessions/<sid>.json`` and
    unlinks the pending file. Returns the new session path on success,
    ``None`` on missing file or token mismatch.
    """
    pending = pending_path_for(task_id)
    if not pending.exists():
        return None
    try:
        data = json.loads(pending.read_text())
    except Exception:
        return None

    expected = data.get("validation_token") or ""
    if not expected or not hmac.compare_digest(str(expected), str(validation_token)):
        return None

    next_action = data.get("next_action") or {}
    session_payload = {
        "task_id": data.get("task_id") or task_id,
        "last_next_action": next_action,
    }
    target = session_path_for(sid)
    try:
        write_session(sid, session_payload)
        pending.unlink()
    except FileNotFoundError:
        return None
    return target


def claim_pending_by_prompt_hash(prompt_sha256: str, sid: str) -> Optional[Path]:
    """Locate a pending claim whose ``next_action.prompt_sha256`` matches
    and atomically promote it to ``sessions/<sid>.json``.

    Used by the PreToolUse hook when ``tool_input`` doesn't carry the
    ``validation_token`` (Task tool doesn't expose it as a field), but the
    raw ``prompt`` SHA256 uniquely identifies a freshly minted next_action.
    """
    if not prompt_sha256 or not sid:
        return None

    for p in _pending_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        na = data.get("next_action") or {}
        if na.get("prompt_sha256") != prompt_sha256:
            continue

        task_id = data.get("task_id") or p.stem
        with _pending_claim_lock(task_id) as acquired:
            if not acquired:
                continue
            # Re-check after acquiring the lock — another window may have
            # promoted this pending file between glob() and the lock.
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text())
            except Exception:
                continue
            na = data.get("next_action") or {}
            if na.get("prompt_sha256") != prompt_sha256:
                continue
            session_payload = {
                "task_id": data.get("task_id") or task_id,
                "last_next_action": na,
            }
            try:
                write_session(sid, session_payload)
                p.unlink()
            except FileNotFoundError:
                return None
            return session_path_for(sid)
    return None


def cleanup_stale_pending(ttl_seconds: int = 86400) -> int:
    """Delete any pending file whose ``created_at`` is older than ``ttl_seconds``.

    Robust to malformed / missing timestamps — those entries are skipped (not
    deleted), so that a manual operator-written claim isn't accidentally
    nuked.
    """
    pending_dir = _pending_dir()
    if not pending_dir.exists():
        return 0

    now = datetime.now(timezone.utc)
    deleted = 0
    for p in pending_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        ts = data.get("created_at")
        if not ts:
            continue
        try:
            # Accept "...Z" suffix or full offset string.
            if ts.endswith("Z"):
                parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
            else:
                parsed = datetime.fromisoformat(ts)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
        except Exception:
            continue

        age = (now - parsed).total_seconds()
        if age > ttl_seconds:
            try:
                p.unlink()
                deleted += 1
            except FileNotFoundError:
                pass
    return deleted
