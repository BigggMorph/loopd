"""End-to-end tests for the β Stop hook.

Invokes hooks/orch_stop.py as a subprocess (mimicking the harness), wiring
HOME so it sees a per-test ~/.loopd. Verifies gate behavior across §11A
scenarios.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import orchestrator_state

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


def _seed_dev_running(session_id: str, current_issue: int = 42) -> None:
    state = orchestrator_state.read()
    state["current_issue"] = current_issue
    state["dev_session_id"] = session_id
    state["dev_done_injected"] = False
    state["dev_started_at"] = orchestrator_state.now().isoformat()
    state["issues"][str(current_issue)] = {
        "number": current_issue,
        "status": "dev_running",
        "history": [],
    }
    orchestrator_state.write(state)


def test_hook_noop_when_no_current_issue(isolated_home, tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    proc = _run_hook("any-session", transcript, isolated_home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""  # no block


def test_hook_noop_when_loopd_session_still_exists(isolated_home, tmp_path):
    session_id = "live-session-1"
    _seed_dev_running(session_id)
    # loopd session file present → dev pipeline still running
    loopd_dir = isolated_home / ".loopd" / "sessions"
    loopd_dir.mkdir(parents=True, exist_ok=True)
    (loopd_dir / f"{session_id}.json").write_text("{}")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    proc = _run_hook(session_id, transcript, isolated_home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


def test_hook_blocks_when_session_file_gone(isolated_home, tmp_path):
    session_id = "ended-session-2"
    _seed_dev_running(session_id)
    # Note: no loopd session file → dev finished
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    proc = _run_hook(session_id, transcript, isolated_home)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["decision"] == "block"
    assert "ORCH_INJECT:dev_done" in payload["systemMessage"]
    # idempotency flag flipped
    state = orchestrator_state.read()
    assert state["dev_done_injected"] is True


def test_hook_idempotent_second_call_is_noop(isolated_home, tmp_path):
    session_id = "ended-session-3"
    _seed_dev_running(session_id)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    p1 = _run_hook(session_id, transcript, isolated_home)
    assert json.loads(p1.stdout)["decision"] == "block"
    p2 = _run_hook(session_id, transcript, isolated_home)
    assert p2.stdout.strip() == ""  # idempotent — no second block


def test_hook_noop_for_different_session_id(isolated_home, tmp_path):
    _seed_dev_running("orch-session-A")
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    proc = _run_hook("user-other-session-B", transcript, isolated_home)
    assert proc.stdout.strip() == ""


def test_hook_noop_when_dev_session_id_is_none(isolated_home, tmp_path):
    # status=dev_running but dev_session_id missing → stale state, don't fire
    state = orchestrator_state.read()
    state["current_issue"] = 99
    state["dev_session_id"] = None
    state["issues"]["99"] = {"number": 99, "status": "dev_running", "history": []}
    orchestrator_state.write(state)
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    proc = _run_hook("any", transcript, isolated_home)
    assert proc.stdout.strip() == ""
