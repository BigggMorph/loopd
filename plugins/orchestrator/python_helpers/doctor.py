"""Feature 1 — self-healing stall classifier + auto-resume helpers.

The lead runs `classify_stall(...)` in Step −6 on every *fresh* wake (timer /
manual, not a teammate reply) to decide whether the orchestrator has stalled
*alive-but-stuck* and, if so, how to recover:

  ("none",       None)                          — healthy / actively progressing
  ("transient",  {action, num, signature, ...}) — re-kick / auto-resume
  ("structural", {failure_signature, ...})       — dispatch system-doctor
  ("human",      {reason})                        — genuinely awaiting the user

Detection relies on the playbook being re-invoked periodically (the user runs
`/loop` or the CronCreate routine — see SKILL.md Part 0). A fully-dead loop is
explicitly out of scope (no external watchdog).

PER-REPO: every signature is derived only from `failure_reason + status`, never
the repo, because Feature 3 already gives each repo its own state file — so the
doctor cycle is independent per instance with no extra work here.

stdlib-only except the two sibling helpers (orchestrator_state, playbook_helpers)
so it stays importable from the lead's inline `python3 -c` calls.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import orchestrator_state
import playbook_helpers

# Re-kick a structural fix issue only after the same systemic failure has been
# observed across this many issues (avoids filing a self-fix for a one-off).
DOCTOR_LESSON_THRESHOLD = 3
# Max system-doctor fix issues filed per rolling 24h (runaway guard).
DOCTOR_DAILY_CAP = 3
# A signature with an OPEN doctor-filed fix is not re-diagnosed within this window.
DOCTOR_SIGNATURE_WINDOW_SEC = 24 * 3600
# Auto-resume idempotency: same signature is not re-kicked within this window…
RESUME_COOLDOWN_SEC = 600
# …and after this many attempts a transient stall is escalated to structural.
RESUME_MAX_ATTEMPTS = 2

def _loopd_sessions_dir() -> Path:
    # Resolved at call time so HOME overrides (tests / sandboxes) are honored.
    return Path(os.path.expanduser("~/.loopd/sessions"))

# Failure canonicals (from playbook_helpers._LESSON_PATTERNS) bucketed by how the
# doctor should react. A *transient* canonical is re-kickable (re-send usually
# works); a *structural* canonical is an orchestrator bug worth a fix issue once
# it recurs; a *human* canonical is a legitimate "this change is hard" outcome.
_TRANSIENT_CANONICALS = {
    "analyzer 무응답",
    "tester 무응답",
}
_STRUCTURAL_CANONICALS = {
    "PR URL 추출 실패",
    "/dev-task 시작 후 loopd session 파일 미생성",
    "auto-merge 실패",
    "split 결과 0개",
}
_HUMAN_CANONICALS = {
    "tester가 PR 2회 연속 거부",
}

# Raw-reason substrings (not lesson canonicals) → bucket. Checked when the
# failure_reason maps to no canonical.
_STRUCTURAL_SUBSTRINGS = (
    "stale state",
    "dev_session_id is none",
)
_TRANSIENT_SUBSTRINGS = (
    "current_session_id unavailable",
)

# Statuses that are inherently waiting on the user — never auto-touch them.
_HUMAN_STATUSES = {
    "human_qa_pending",
    "merge_pending",
    "reject_confirm_pending",
    "split_confirm_pending",
    "regression_detected",
    "pr_audit_pending",
    "parked_awaiting_human",  # only if its reason isn't recoverable (handled below)
}

_STUCK_STATUSES = {"needs_human", "parked_awaiting_human"}


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


_SAFE_TARGET_PREFIX = "plugins/orchestrator/"
_FORBIDDEN_TARGET_PREFIX = "plugins/loopd/"


def targets_are_safe(target_files: Any) -> bool:
    """True only if every target path is under plugins/orchestrator/ and none
    under plugins/loopd/ (the loopd-untouchable guard for doctor diagnoses).

    Empty / non-list input is unsafe (a fix must name at least one file).
    """
    if not isinstance(target_files, list) or not target_files:
        return False
    for t in target_files:
        if not isinstance(t, str):
            return False
        norm = t.strip().lstrip("./").replace("\\", "/")
        if norm.startswith(_FORBIDDEN_TARGET_PREFIX):
            return False
        if not norm.startswith(_SAFE_TARGET_PREFIX):
            return False
    return True


def failure_signature(failure_reason: str, status: Optional[str]) -> str:
    """Stable 16-hex signature for a stall.

    Uses the canonical lesson pattern when available so cosmetic wording
    variations collapse to one signature; otherwise the normalized raw reason.
    """
    match = playbook_helpers.canonical_for_reason(failure_reason or "")
    base = match[0] if match else (failure_reason or "").strip().lower()
    raw = f"{base}:{status or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _loopd_session_exists(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    return (_loopd_sessions_dir() / f"{session_id}.json").exists()


def _observed_count(state: Dict[str, Any], failure_reason: str) -> int:
    """Read (no mutation) the observed_count of the lesson matching this reason."""
    match = playbook_helpers.canonical_for_reason(failure_reason or "")
    if match is None:
        return 0
    canonical = match[0]
    for entry in state.get("lessons_learned") or []:
        if isinstance(entry, dict) and entry.get("pattern") == canonical:
            return int(entry.get("observed_count") or 0)
    return 0


def _bucket_for_reason(
    failure_reason: str, observed_count: int
) -> str:
    """Map a failure_reason to a base bucket: transient | structural | human."""
    match = playbook_helpers.canonical_for_reason(failure_reason or "")
    if match is not None:
        canonical = match[0]
        if canonical in _HUMAN_CANONICALS:
            return "human"
        if canonical in _TRANSIENT_CANONICALS:
            # Re-kick a few times; if it keeps recurring it's a real bug.
            return "structural" if observed_count >= DOCTOR_LESSON_THRESHOLD else "transient"
        if canonical in _STRUCTURAL_CANONICALS:
            return "structural" if observed_count >= DOCTOR_LESSON_THRESHOLD else "human"
        return "human"
    text = (failure_reason or "").lower()
    for sub in _STRUCTURAL_SUBSTRINGS:
        if sub in text:
            return "structural"
    for sub in _TRANSIENT_SUBSTRINGS:
        if sub in text:
            return "transient"
    # Unknown reason: leave it for the user (conservative — never auto-resume
    # something we can't reason about).
    return "human"


# --- auto-resume idempotency guard ---------------------------------------

def should_auto_resume(
    state: Dict[str, Any], signature: str, now_dt: Optional[_dt.datetime] = None
) -> Tuple[bool, bool]:
    """Return (ok_to_resume, escalate_to_structural).

    - ok=False, escalate=False → within cooldown of the last resume of this
      signature; do nothing this turn.
    - ok=False, escalate=True  → attempt cap exceeded; caller should treat the
      stall as structural instead.
    - ok=True,  escalate=False → safe to auto-resume now.
    """
    now_dt = now_dt or _now()
    attempts = (state.get("resume_attempts") or {}).get(signature) or {}
    count = int(attempts.get("count") or 0)
    if count >= RESUME_MAX_ATTEMPTS:
        return (False, True)
    last = state.get("last_resume_at")
    if state.get("last_resume_signature") == signature:
        last_dt = _parse_iso(last)
        if last_dt is not None and (now_dt - last_dt).total_seconds() < RESUME_COOLDOWN_SEC:
            return (False, False)
    return (True, False)


def record_resume_attempt(
    state: Dict[str, Any], signature: str, now_dt: Optional[_dt.datetime] = None
) -> None:
    now_dt = now_dt or _now()
    iso = now_dt.isoformat()
    attempts = state.setdefault("resume_attempts", {})
    entry = attempts.setdefault(signature, {"count": 0, "first_at": iso, "last_at": iso})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_at"] = iso
    state["last_resume_signature"] = signature
    state["last_resume_at"] = iso


def restore_parked_issue(state: Dict[str, Any], num: Any) -> bool:
    """Restore a parked/needs_human issue to its prior active status and clear
    transient timers/flags so the restored status drives a clean re-dispatch.

    Shared by the `resume:N` arg path and the Step −6 auto-resume branch.
    Returns True on success.
    """
    issue = orchestrator_state.get_issue(state, num)
    if not issue:
        return False
    prior = "new"
    for h in reversed(issue.get("history") or []):
        frm = h.get("from")
        if frm in orchestrator_state._ACTIVE_STATUSES:
            prior = frm
            break
    for k in (
        "analyze_pending_started_at",
        "analyzer_retried",
        "test_pending_started_at",
        "tester_retried",
        "merge_pending_started_at",
        "merge_question_emitted",
        "human_qa_started_at",
        "conflict_warned",
        "regression_decision",
        "last_verdict_signature",
    ):
        issue.pop(k, None)
    orchestrator_state.transition(issue, prior)
    state["current_issue"] = int(num) if str(num).isdigit() else num
    return True


# --- system-doctor runaway guards ----------------------------------------

def prune_doctor_issues_today(
    state: Dict[str, Any], now_dt: Optional[_dt.datetime] = None
) -> None:
    """Drop doctor_issues_today timestamps older than 24h (rolling daily cap)."""
    now_dt = now_dt or _now()
    cutoff = now_dt - _dt.timedelta(seconds=DOCTOR_SIGNATURE_WINDOW_SEC)
    kept = []
    for ts in state.get("doctor_issues_today") or []:
        dt = _parse_iso(ts)
        if dt is not None and dt >= cutoff:
            kept.append(ts)
    state["doctor_issues_today"] = kept


def doctor_daily_cap_reached(
    state: Dict[str, Any], now_dt: Optional[_dt.datetime] = None
) -> bool:
    prune_doctor_issues_today(state, now_dt)
    return len(state.get("doctor_issues_today") or []) >= DOCTOR_DAILY_CAP


def doctor_signature_active(
    state: Dict[str, Any], signature: str, now_dt: Optional[_dt.datetime] = None
) -> bool:
    """True if this signature already has a doctor diagnosis within the window
    (suppress re-diagnosis). The lead additionally checks GitHub for an OPEN
    `doctor-fp-<sig>` fix issue before re-filing."""
    now_dt = now_dt or _now()
    seen = (state.get("doctor_signatures_seen") or {}).get(signature)
    if not seen:
        return False
    last = _parse_iso(seen.get("last_at"))
    if last is None:
        return False
    return (now_dt - last).total_seconds() < DOCTOR_SIGNATURE_WINDOW_SEC


def record_doctor_signature(
    state: Dict[str, Any],
    signature: str,
    *,
    fix_issue_url: Optional[str] = None,
    now_dt: Optional[_dt.datetime] = None,
) -> Dict[str, Any]:
    now_dt = now_dt or _now()
    iso = now_dt.isoformat()
    seen = state.setdefault("doctor_signatures_seen", {})
    entry = seen.setdefault(signature, {"count": 0, "first_at": iso, "last_at": iso})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_at"] = iso
    if fix_issue_url:
        entry["fix_issue_url"] = fix_issue_url
    return entry


# --- the classifier -------------------------------------------------------

def classify_stall(
    state: Dict[str, Any],
    wake_reason: Tuple[str, Optional[str]],
    now_dt: Optional[_dt.datetime] = None,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Decide whether the orchestrator has stalled and how to recover.

    Only acts on *fresh* wakes — when a teammate just replied, the user just
    answered, or the dev-done hook just fired, the system is progressing, so we
    return ("none", None) to stay out of the way.
    """
    now_dt = now_dt or _now()

    # Active progress / non-resolution cycle in flight → not a stall.
    if not wake_reason or wake_reason[0] != "fresh":
        return ("none", None)
    for cyc in ("scout_status", "planner_status", "roadmap_status",
                "vision_check_status", "doctor_status"):
        if state.get(cyc):
            return ("none", None)

    ci = state.get("current_issue")
    issue = orchestrator_state.get_issue(state, ci) if ci is not None else None
    if not issue:
        return ("none", None)

    status = issue.get("status")

    # Case A — lost dev-done: the β hook fired (dev_done_injected) and loopd
    # finished (session gone), but the lead never processed the signal (turn /
    # loop didn't advance). Re-kick the dev_done path. We require
    # dev_done_injected so the *not-injected* case (loopd never created its
    # session / crashed) is left to Step 6A's existing ("dev_running","fresh")
    # PoC-4 guard, which parks it as needs_human.
    if status == "dev_running":
        started = _parse_iso(state.get("dev_started_at"))
        old_enough = started is None or (now_dt - started).total_seconds() > 120
        if (
            state.get("dev_done_injected")
            and old_enough
            and not _loopd_session_exists(state.get("dev_session_id"))
        ):
            sig = failure_signature(f"dev_done_missed:{ci}", status)
            ok, escalate = should_auto_resume(state, sig, now_dt)
            if escalate:
                return ("structural", {
                    "failure_signature": sig,
                    "failure_reason": "dev_done 신호 반복 누락 (auto-resume 한계 초과)",
                    "status": status, "num": ci,
                })
            if not ok:
                return ("none", None)
            return ("transient", {
                "action": "process_dev_done", "num": ci, "signature": sig,
            })
        return ("none", None)  # dev still running normally

    # Cases B/C/D — only inspect genuinely stuck (parked / needs_human) issues.
    if status not in _STUCK_STATUSES:
        return ("none", None)

    reason = issue.get("failure_reason") or ""
    sig = failure_signature(reason, status)
    observed = _observed_count(state, reason)
    bucket = _bucket_for_reason(reason, observed)

    if bucket == "human":
        return ("human", {"reason": reason, "status": status, "num": ci})

    if bucket == "transient":
        ok, escalate = should_auto_resume(state, sig, now_dt)
        if escalate:
            bucket = "structural"
        elif not ok:
            return ("none", None)
        else:
            return ("transient", {
                "action": "restore_parked", "num": ci, "signature": sig,
                "failure_reason": reason,
            })

    # structural
    return ("structural", {
        "failure_signature": sig,
        "failure_reason": reason,
        "status": status,
        "num": ci,
        "observed_count": observed,
    })


__all__ = [
    "DOCTOR_LESSON_THRESHOLD",
    "DOCTOR_DAILY_CAP",
    "DOCTOR_SIGNATURE_WINDOW_SEC",
    "RESUME_COOLDOWN_SEC",
    "RESUME_MAX_ATTEMPTS",
    "failure_signature",
    "targets_are_safe",
    "classify_stall",
    "restore_parked_issue",
    "should_auto_resume",
    "record_resume_attempt",
    "prune_doctor_issues_today",
    "doctor_daily_cap_reached",
    "doctor_signature_active",
    "record_doctor_signature",
]
