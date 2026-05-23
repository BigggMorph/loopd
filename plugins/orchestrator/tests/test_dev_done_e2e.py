"""End-to-end integration test for the dev_done auto-resume wire.

Exercises the full chain that the lead playbook + β Stop hook + wake
inference depend on, in a single test, so future refactors can't silently
break either of the two root causes that previously stalled the
orchestrator after PR review:

  Bug #1: lead made up a placeholder UUID for ``dev_session_id`` because
          ``current_session_id()`` didn't exist → Stop hook Gate 1 mismatched
          forever.

  Bug #2: Stop hook's systemMessage was written in a user-facing tone, so
          the lead relayed it to the user and stopped instead of
          auto-invoking the orchestrator skill.

The chain under test (one test, one scenario):

  1. Harness sets CLAUDE_SESSION_ID env var (simulated via monkeypatch).
  2. Lead resolves it via orchestrator_state.current_session_id().
  3. Lead persists it via mark_dev_started().
  4. loopd creates ~/.loopd/sessions/<sid>.json (dev pipeline starts).
  5. β Stop hook fires mid-pipeline → must be no-op (Gate 2).
  6. loopd removes the session file (dev pipeline ends).
  7. β Stop hook fires again → must emit
        decision=block + systemMessage containing ORCH_INJECT:dev_done,
     and flip dev_done_injected=True.
  8. A synthetic transcript replays that systemMessage; wake_inference
     must classify the wake as ('orch_hook_inject', 'dev_done').
  9. A second hook fire (same dev_session) is idempotent no-op.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import orchestrator_state
import wake_inference

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = PLUGIN_ROOT / "hooks" / "orch_stop.py"


def _run_hook(session_id: str, transcript_path: Path, env_home: Path) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(env_home)
    env["PYTHONPATH"] = str(PLUGIN_ROOT / "python_helpers")
    payload = json.dumps({"session_id": session_id, "transcript_path": str(transcript_path)})
    return subprocess.run(
        [sys.executable, "-I", str(HOOK_SCRIPT)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def test_dev_done_full_wire_end_to_end(isolated_home, tmp_path, monkeypatch):
    # ── 1. Harness env: the live Claude Code session UUID ────────────────
    real_sid = "harness-session-uuid-abc123"
    monkeypatch.setenv("CLAUDE_SESSION_ID", real_sid)
    monkeypatch.delenv("LOOPD_SESSION_ID", raising=False)

    # ── 2. Lead resolves the session id via the helper (NOT a placeholder)
    resolved = orchestrator_state.current_session_id()
    assert resolved == real_sid, "helper must return the harness env value"

    # ── 3. Lead persists it; guard rejects placeholders/empty values ─────
    state = orchestrator_state.read()
    state["current_issue"] = 3
    state["issues"]["3"] = {
        "number": 3,
        "status": "dev_running",
        "history": [],
    }
    orchestrator_state.mark_dev_started(state, resolved)
    orchestrator_state.write(state)

    persisted = orchestrator_state.read()
    assert persisted["dev_session_id"] == real_sid
    assert persisted["dev_done_injected"] is False

    # ── 4. loopd creates the session file (pipeline running) ─────────────
    loopd_sessions = isolated_home / ".loopd" / "sessions"
    loopd_sessions.mkdir(parents=True, exist_ok=True)
    session_file = loopd_sessions / f"{real_sid}.json"
    session_file.write_text("{}")
    assert session_file.exists()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("")  # empty is fine; Gate 3 is best-effort

    # ── 5. Mid-pipeline Stop hook → no-op (Gate 2: file still exists) ────
    mid = _run_hook(real_sid, transcript, isolated_home)
    assert mid.returncode == 0
    assert mid.stdout.strip() == "", (
        "hook must be silent while the loopd session file exists, got: %r" % mid.stdout
    )
    mid_state = orchestrator_state.read()
    assert mid_state["dev_done_injected"] is False, (
        "idempotency flag must not flip while dev pipeline is still running"
    )

    # ── 6. loopd finishes; the session file disappears ───────────────────
    session_file.unlink()
    assert not session_file.exists()

    # ── 7. Stop hook fires → block + ORCH_INJECT + flag flips ────────────
    end = _run_hook(real_sid, transcript, isolated_home)
    assert end.returncode == 0
    assert end.stderr == "", "hook should not emit stderr on the happy path"
    payload = json.loads(end.stdout)
    assert payload["decision"] == "block"
    assert payload["systemMessage"].startswith("ORCH_INJECT:dev_done")

    # Lead-facing tone — these substrings prove the message is not the
    # old human-facing one ("슬래시 커맨드를 호출해 ... 진행하세요").
    msg = payload["systemMessage"]
    assert "NOT a user-facing message" in msg
    assert "Do not relay" in msg
    assert 'Skill(skill="orchestrator")' in msg
    assert "슬래시 커맨드를 호출" not in msg, "stale human-facing phrasing leaked back in"

    end_state = orchestrator_state.read()
    assert end_state["dev_done_injected"] is True

    # ── 8. Replay the systemMessage through wake_inference ───────────────
    # Synthetic transcript: the harness writes the block's systemMessage
    # as a user-role message on the lead's next wake.
    transcript_with_marker = tmp_path / "transcript_after_inject.jsonl"
    transcript_with_marker.write_text(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "running dev"}]}})
        + "\n"
        + json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": msg}]}})
        + "\n"
    )
    wake = wake_inference.infer(str(transcript_with_marker), end_state)
    assert wake == ("orch_hook_inject", "dev_done"), (
        "lead must classify the next wake as the dev_done hook injection, got: %r" % (wake,)
    )

    # ── 9. Second hook fire → idempotent no-op (flag already set) ────────
    repeat = _run_hook(real_sid, transcript, isolated_home)
    assert repeat.returncode == 0
    assert repeat.stdout.strip() == "", (
        "hook must be silent on the second fire (already injected); got: %r" % repeat.stdout
    )


def test_lead_refuses_to_start_dev_without_session_env(isolated_home, monkeypatch):
    """If the harness env var is missing, the helper raises and the lead
    must NOT fall back to any placeholder. This guards against Bug #1
    coming back via a different code path.
    """
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("LOOPD_SESSION_ID", raising=False)

    import pytest

    with pytest.raises(RuntimeError, match="current_session_id"):
        orchestrator_state.current_session_id()

    # And the lead's mark_dev_started guard catches any other path
    # (e.g. a fallback to "" or whitespace).
    state = orchestrator_state.read()
    with pytest.raises(ValueError, match="non-empty string"):
        orchestrator_state.mark_dev_started(state, "")
    with pytest.raises(ValueError, match="non-empty string"):
        orchestrator_state.mark_dev_started(state, "   ")

    # State must still be pristine — no dev_session_id should have been written.
    assert orchestrator_state.read()["dev_session_id"] is None
