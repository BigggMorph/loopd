"""Audit-log + audited_bash wrapper for every mutating gh command.

Per design §19, mutating gh commands (`gh issue create|close|edit|reopen`,
`gh pr merge|create|close|edit`) must go through `audited_bash`, which:

1. Runs the command (via subprocess, like a normal bash() call).
2. On success, appends an entry to `state.audit_log`:
   {at, actor, action, target, payload_hash}.
3. When the audit log exceeds 1000 entries, archives the oldest ones
   atomically to ~/.loopd/orchestrator/audit_archive/<UTC-date>.jsonl
   (tmp + fsync → atomic rename → only then trim state.audit_log).

The wrapper is small, deterministic, and testable. The lead playbook
calls it inline via the same `python3 -c "import audit; audit.audited_bash(...)"`
pattern as the other helpers.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import orchestrator_state

AUDIT_ROTATE_THRESHOLD = 1000

# Inverse-action map for `undo:N`. Each value is a callable
#   (entry: dict, repo: str) -> Optional[List[str]]
# returning the gh command argv that would reverse `entry`, or None when
# auto-reversal isn't possible.
def _inverse_argv(entry: Dict[str, Any], repo: str) -> Optional[List[str]]:
    action = entry.get("action") or ""
    target = entry.get("target") or ""
    if action == "gh issue close":
        return ["gh", "issue", "reopen", target, "--repo", repo]
    if action == "gh issue reopen":
        return ["gh", "issue", "close", target, "--repo", repo, "--comment", "undone"]
    if action == "gh issue create":
        return [
            "gh", "issue", "close", target, "--repo", repo,
            "--comment", "undone (audit_log)",
        ]
    if action == "gh pr close":
        return ["gh", "pr", "reopen", target, "--repo", repo]
    if action == "gh pr edit --add-label":
        payload = entry.get("payload") or {}
        label = payload.get("label") or ""
        if not label:
            return None
        return [
            "gh", "pr", "edit", target, "--repo", repo,
            "--remove-label", label,
        ]
    if action == "gh issue edit --add-label":
        payload = entry.get("payload") or {}
        label = payload.get("label") or ""
        if not label:
            return None
        return [
            "gh", "issue", "edit", target, "--repo", repo,
            "--remove-label", label,
        ]
    if action == "gh pr merge":
        # Merges cannot be auto-reversed: the design tells the lead to use
        # the regression_detected flow + `git revert <sha>` guide.
        return None
    return None


def _archive_dir() -> Path:
    return orchestrator_state.ORCHESTRATOR_DIR / "audit_archive"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _payload_hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _archive_path_for(now: Optional[_dt.datetime] = None) -> Path:
    when = now or _dt.datetime.now(_dt.timezone.utc)
    return _archive_dir() / f"{when.date().isoformat()}.jsonl"


def _atomic_append_archive(entries: List[Dict[str, Any]]) -> None:
    """Atomic-rename archive write (§F-E3 + Round G atomicity).

    1. Write `<date>.jsonl.tmp` with fsync.
    2. Atomic rename into place.
    3. Caller trims state.audit_log only AFTER this returns.
    """
    if not entries:
        return
    archive_dir = _archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(archive_dir, 0o700)
    except OSError:
        pass
    target = _archive_path_for()
    # Append-style archive: read existing content (if any), then write the
    # whole new file atomically. Avoids duplicate-entry race if crash
    # interrupts between rename and trim.
    existing_hashes = set()
    if target.exists():
        for line in target.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            existing_hashes.add(ev.get("payload_hash"))
    body_lines: List[str] = []
    if target.exists():
        body_lines.append(target.read_text())
    for entry in entries:
        if entry.get("payload_hash") in existing_hashes:
            continue  # Already archived in a prior crashed run.
        body_lines.append(json.dumps(entry, sort_keys=True))
    body = "\n".join(line for line in body_lines if line)
    if body and not body.endswith("\n"):
        body += "\n"
    fd, tmp_path = tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(archive_dir),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def _cleanup_stale_tmp() -> None:
    """Clean up any orphan .tmp files (crash-recovery, §F-E3)."""
    archive_dir = _archive_dir()
    if not archive_dir.exists():
        return
    for p in archive_dir.glob("*.tmp"):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def maybe_rotate(state: Dict[str, Any]) -> int:
    """If audit_log > AUDIT_ROTATE_THRESHOLD, archive the overflow and trim.

    Returns the number of entries archived. Crash-safe: the archive write
    completes (atomic rename) BEFORE state.audit_log is trimmed, so a
    crash between the two steps is recoverable (duplicates detected via
    payload_hash in `_atomic_append_archive`).
    """
    _cleanup_stale_tmp()
    log = state.setdefault("audit_log", [])
    if len(log) <= AUDIT_ROTATE_THRESHOLD:
        return 0
    overflow = log[:-AUDIT_ROTATE_THRESHOLD]
    _atomic_append_archive(overflow)
    state["audit_log"] = log[-AUDIT_ROTATE_THRESHOLD:]
    return len(overflow)


def audited_bash(
    argv: List[str],
    actor: str,
    action: str,
    target: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 120,
) -> Tuple[int, str, str]:
    """Run a mutating command and append an audit entry on success.

    Returns (exit_code, stdout, stderr). On non-zero exit, no audit entry
    is written — failed mutations don't need to be undone.

    Caller is responsible for ensuring `argv[0]` is one of the gh
    subcommands the audit log knows how to reverse (see _inverse_argv);
    otherwise `undo:N` will silently skip the entry.
    """
    if not argv:
        raise ValueError("audited_bash: empty argv")
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode == 0:
        with orchestrator_state.flock_session() as state:
            entry = {
                "at": _now_iso(),
                "actor": actor,
                "action": action,
                "target": target,
                "payload": payload or {},
                "payload_hash": _payload_hash(
                    {"argv": argv, "payload": payload or {}}
                ),
                "argv": argv,
            }
            state.setdefault("audit_log", []).append(entry)
            maybe_rotate(state)
            orchestrator_state.write_in_lock(state)
    return result.returncode, result.stdout, result.stderr


def record_state_mutation(
    state: Dict[str, Any],
    actor: str,
    action: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Append an audit entry for a non-gh state mutation (Rev 17 Round A S1).

    Direct state writes — most importantly `state.vision` updates by the
    vision-critic flow — bypass `audited_bash` (no command to run). This
    helper records the mutation in `state.audit_log` with the same shape
    so undo / archive flows treat it uniformly.

    Caller MUST be inside an existing `flock_session()` block and MUST
    call `write_in_lock(state)` afterward to persist the entry. This
    helper does not acquire the lock itself.
    """
    entry = {
        "at": _now_iso(),
        "actor": actor,
        "action": action,
        "target": payload.get("target") if isinstance(payload, dict) else "",
        "payload": payload or {},
        "payload_hash": _payload_hash(payload or {}),
        "argv": [],
        "kind": "state_mutation",
    }
    state.setdefault("audit_log", []).append(entry)
    maybe_rotate(state)
    return entry


def compute_undo_plan(state: Dict[str, Any], n: int) -> List[Dict[str, Any]]:
    """Return up-to-N undo plans (newest entry first).

    Each plan: {"entry": original_entry, "inverse_argv": [...] or None,
    "reason": optional human note}.
    """
    log = state.get("audit_log") or []
    if n <= 0 or not log:
        return []
    repo = state.get("repo") or ""
    plans: List[Dict[str, Any]] = []
    for entry in reversed(log[-n:]):
        inverse = _inverse_argv(entry, repo)
        plan = {"entry": entry, "inverse_argv": inverse}
        if inverse is None:
            plan["reason"] = (
                "이 action은 자동 rollback이 지원되지 않습니다 "
                "(예: gh pr merge — git revert + regression_detected 권장)."
            )
        plans.append(plan)
    return plans


__all__ = [
    "AUDIT_ROTATE_THRESHOLD",
    "audited_bash",
    "record_state_mutation",
    "maybe_rotate",
    "compute_undo_plan",
]
