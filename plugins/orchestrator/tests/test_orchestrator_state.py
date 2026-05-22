"""Unit tests for orchestrator_state.py"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import orchestrator_state


def test_read_creates_empty_when_missing(isolated_home):
    state = orchestrator_state.read()
    assert state["version"] == orchestrator_state.SCHEMA_VERSION
    assert state["mode"] == "resolution"
    assert state["current_issue"] is None
    assert state["issues"] == {}


def test_write_then_read_roundtrip(isolated_home):
    state = orchestrator_state.read()
    state["vision"] = "test vision"
    state["repo"] = "owner/repo"
    state["issues"]["123"] = {"number": 123, "status": "new", "history": []}
    orchestrator_state.write(state)

    state2 = orchestrator_state.read()
    assert state2["vision"] == "test vision"
    assert state2["repo"] == "owner/repo"
    assert state2["issues"]["123"]["status"] == "new"


def test_atomic_write_creates_no_tmp_residue(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.write(state)
    residue = list(orchestrator_state.ORCHESTRATOR_DIR.glob("*.tmp"))
    assert residue == []


def test_state_file_permissions_are_600(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.write(state)
    mode = orchestrator_state.STATE_PATH.stat().st_mode & 0o777
    assert mode == 0o600


def test_orchestrator_dir_is_700(isolated_home):
    orchestrator_state.read()
    mode = orchestrator_state.ORCHESTRATOR_DIR.stat().st_mode & 0o777
    assert mode == 0o700


def test_transition_records_history(isolated_home):
    issue = {"number": 1, "status": "new", "history": []}
    orchestrator_state.transition(issue, "analyze_pending")
    assert issue["status"] == "analyze_pending"
    assert len(issue["history"]) == 1
    assert issue["history"][0]["from"] == "new"
    assert issue["history"][0]["to"] == "analyze_pending"


def test_transition_to_same_status_is_idempotent(isolated_home):
    issue = {"number": 1, "status": "new", "history": []}
    orchestrator_state.transition(issue, "new")
    assert issue["history"] == []


def test_transition_rejects_unknown_status(isolated_home):
    issue = {"number": 1, "status": "new", "history": []}
    with pytest.raises(ValueError):
        orchestrator_state.transition(issue, "totally_made_up")


def test_flock_session_persists_via_write_in_lock(isolated_home):
    with orchestrator_state.flock_session() as state:
        state["repo"] = "x/y"
        orchestrator_state.write_in_lock(state)
    state = orchestrator_state.read()
    assert state["repo"] == "x/y"


def test_flock_session_discards_without_write_in_lock(isolated_home):
    with orchestrator_state.flock_session() as state:
        state["repo"] = "x/y"  # never persisted
    state = orchestrator_state.read()
    assert state["repo"] == ""


def test_write_in_lock_outside_session_raises(isolated_home):
    state = orchestrator_state.read()
    with pytest.raises(RuntimeError):
        orchestrator_state.write_in_lock(state)


def test_mark_dev_started_sets_all_fields(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.mark_dev_started(state, "session-xyz")
    assert state["dev_session_id"] == "session-xyz"
    assert state["dev_done_injected"] is False
    assert state["dev_started_at"] is not None


def test_update_issue_atomic(isolated_home):
    orchestrator_state.update_issue(42, dev_task_prompt="fix the thing", complexity_level=1)
    state = orchestrator_state.read()
    assert state["issues"]["42"]["dev_task_prompt"] == "fix the thing"
    assert state["issues"]["42"]["complexity_level"] == 1


def test_normalize_backfills_missing_fields(isolated_home):
    # Simulate an old-schema state.json missing several fields.
    orchestrator_state.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    legacy = {"version": 1, "vision": "old", "repo": "x/y", "issues": {}}
    orchestrator_state.STATE_PATH.write_text(json.dumps(legacy))
    state = orchestrator_state.read()
    # New fields backfilled to defaults.
    assert state["mode"] == "resolution"
    assert state["pending_questions"] == []
    assert state["audit_log"] == []
    assert state["watch_list"] == []
    assert state["version"] == orchestrator_state.SCHEMA_VERSION
