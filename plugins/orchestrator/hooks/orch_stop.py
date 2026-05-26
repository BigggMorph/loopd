"""Orchestrator Stop hook — detects /dev-task completion and re-enters
the playbook by `decision: "block"` + a systemMessage marker.

Spec: docs/orchestrator-design.md §11A.

Lifecycle inside one Stop event (the harness fires loopd + orchestrator
hooks in parallel; time-separated so they never both `block`):

  Gate 0  state.current_issue is None                  → exit 0 (no-op)
  Gate 1  current_issue.status != dev_running          → exit 0
          state.dev_session_id != session_id           → exit 0
          state.dev_done_injected already True         → exit 0
  Gate 2  ~/.loopd/sessions/<session_id>.json exists   → exit 0 (still running)
  Gate 3  (optional) transcript shows review approve   → strengthen confidence
          (failure to find approve does NOT block injection)

On all gates pass:
  - mark state.dev_done_injected = True (idempotent)
  - emit {"decision": "block", "reason": ..., "systemMessage": "ORCH_INJECT:dev_done ..."}
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Python -I (isolated mode) ignores PYTHONPATH for security, so import the
# bundled helpers via the file's own location rather than env vars.
_HELPERS = Path(__file__).resolve().parent.parent / "python_helpers"
if str(_HELPERS) not in sys.path:
    sys.path.insert(0, str(_HELPERS))

import orchestrator_state  # type: ignore[import-not-found]  # noqa: E402
import wake_inference  # type: ignore[import-not-found]  # noqa: E402

LOOPD_SESSIONS_DIR = Path(os.path.expanduser("~/.loopd/sessions"))

ORCH_INJECT_MESSAGE = (
    "ORCH_INJECT:dev_done\n\n"
    "[Automated orchestrator wake — internal signal to the lead Claude thread, "
    "NOT a user-facing message. Do not relay, paraphrase, or quote this to the "
    "user.] The loopd /dev-task pipeline has completed. You are the orchestrator "
    "lead; immediately invoke Skill(skill=\"orchestrator\") to enter the "
    "(\"dev_running\", (\"orch_hook_inject\", \"dev_done\")) branch of the "
    "playbook (extract PR URL → dispatch tester → merge or merge_pending). "
    "Proceed autonomously without prompting the user."
)


def _read_payload() -> Dict[str, Any]:
    raw = sys.stdin.read() or "{}"
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _exit_noop() -> None:
    # Empty body = no block; harness proceeds with normal stop.
    sys.stdout.write("")
    sys.exit(0)


def _exit_block(reason: str) -> None:
    payload = {
        "decision": "block",
        "reason": reason,
        "systemMessage": ORCH_INJECT_MESSAGE,
    }
    sys.stdout.write(json.dumps(payload))
    sys.exit(0)


def _should_inject(state: Dict[str, Any], session_id: str, transcript_path: str) -> Optional[str]:
    """Return a reason string if we should inject, else None."""
    # Gate 0
    if state.get("current_issue") is None:
        return None
    issue = orchestrator_state.get_issue(state, state["current_issue"])
    if not issue:
        return None
    # Gate 1
    if issue.get("status") != "dev_running":
        return None
    dev_session_id = state.get("dev_session_id")
    if dev_session_id is None:
        return None
    if dev_session_id != session_id:
        return None
    if state.get("dev_done_injected"):
        return None
    # Gate 2
    loopd_session_path = LOOPD_SESSIONS_DIR / f"{session_id}.json"
    if loopd_session_path.exists():
        return None
    # Gate 3 (best effort)
    last_task = wake_inference.read_last_task_result(transcript_path) or ""
    approve_signal = "approve" in last_task.lower() or "pr" in last_task.lower()
    return (
        f"orchestrator: dev-task ended (session={session_id[:8]}, "
        f"approve_signal={approve_signal})"
    )


def main() -> None:
    payload = _read_payload()
    session_id = payload.get("session_id") or ""
    transcript_path = payload.get("transcript_path") or ""
    if not session_id:
        _exit_noop()
        return

    # Feature 3 — the payload carries only a session_id, so locate which
    # per-repo instance owns this dev session before touching any state. Session
    # UUIDs are globally unique → at most one instance matches. None means this
    # Stop event belongs to no orchestrator instance (e.g. plain chat) → noop.
    try:
        slug = orchestrator_state.find_instance_by_dev_session(session_id)
    except Exception as exc:  # noqa: BLE001 — never block the harness
        sys.stderr.write(f"orch_stop.py instance scan error: {exc}\n")
        _exit_noop()
        return
    if slug is None:
        _exit_noop()
        return
    orchestrator_state.set_active_instance(slug)

    # Use flock_session so the dev_done_injected flip is atomic vs. concurrent
    # writes from the lead playbook. All path constants below now resolve to
    # this instance (set_active_instance above).
    try:
        with orchestrator_state.flock_session(timeout_s=5.0) as state:
            reason = _should_inject(state, session_id, transcript_path)
            if reason is None:
                # Touch sentinel as a fallback signal so the lead can
                # recover even if injection failed.
                try:
                    orchestrator_state.SENTINEL_DEV_DONE.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                return _exit_noop()
            orchestrator_state.mark_dev_done_injected(state)
            orchestrator_state.write_in_lock(state)
            # Also touch sentinel — defensive.
            try:
                orchestrator_state.SENTINEL_DEV_DONE.parent.mkdir(parents=True, exist_ok=True)
                orchestrator_state.SENTINEL_DEV_DONE.touch()
            except OSError:
                pass
        _exit_block(reason)
    except TimeoutError:
        # If we can't even grab the lock in 5s, don't block the harness.
        _exit_noop()
    except Exception as exc:  # noqa: BLE001  (last-resort safety)
        # Never let a hook crash kill the Stop event. Emit nothing + log to
        # stderr (harness usually shows that to user).
        sys.stderr.write(f"orch_stop.py error: {exc}\n")
        _exit_noop()


if __name__ == "__main__":
    main()
