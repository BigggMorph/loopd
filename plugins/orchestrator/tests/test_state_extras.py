"""Additional state-helper tests covering Phase F fixes:

- scout_transition records history
- resume_waiting_on_dep adds history via transition()
"""

from __future__ import annotations

from unittest import mock

import pytest

import issue_picker
import orchestrator_state


def test_scout_transition_records_history(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.scout_transition(state, "scout_new")
    orchestrator_state.scout_transition(state, "scout_pending")
    assert state["scout_status"] == "scout_pending"
    log = state["scout_history_log"]
    assert len(log) == 2
    assert log[0]["to"] == "scout_new"
    assert log[1]["from"] == "scout_new"
    assert log[1]["to"] == "scout_pending"


def test_scout_transition_idempotent(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.scout_transition(state, "scout_new")
    orchestrator_state.scout_transition(state, "scout_new")
    assert len(state.get("scout_history_log", [])) == 1


def test_scout_transition_rejects_invalid():
    with pytest.raises(ValueError):
        orchestrator_state.scout_transition({}, "scout_made_up")


def test_resume_waiting_on_dep_records_history(isolated_home):
    state = orchestrator_state.read()
    state["repo"] = "x/y"
    state["issues"]["50"] = {
        "number": 50,
        "status": "waiting_on_dep",
        "unresolved_dependencies": [],  # already resolved
        "history": [
            {"at": "old", "from": "ready_for_dev", "to": "waiting_on_dep"},
        ],
    }
    result = issue_picker.resume_waiting_on_dep(state)
    assert result is not None
    assert result["status"] == "ready_for_dev"
    # transition() should have appended a new history entry.
    assert len(result["history"]) == 2
    assert result["history"][-1]["from"] == "waiting_on_dep"
    assert result["history"][-1]["to"] == "ready_for_dev"


def test_resume_waiting_on_dep_uses_gh_check(isolated_home):
    state = orchestrator_state.read()
    state["repo"] = "x/y"
    state["issues"]["77"] = {
        "number": 77,
        "status": "waiting_on_dep",
        "unresolved_dependencies": [99],
        "history": [],
    }

    with mock.patch.object(issue_picker, "_run_gh", return_value="CLOSED\n"):
        result = issue_picker.resume_waiting_on_dep(state)
    assert result is not None
    assert result["status"] == "ready_for_dev"
    assert result["unresolved_dependencies"] == []
    assert result["history"][-1]["to"] == "ready_for_dev"


def test_resume_waiting_on_dep_skips_when_dep_still_open(isolated_home):
    state = orchestrator_state.read()
    state["repo"] = "x/y"
    state["issues"]["77"] = {
        "number": 77,
        "status": "waiting_on_dep",
        "unresolved_dependencies": [99],
        "history": [],
    }

    with mock.patch.object(issue_picker, "_run_gh", return_value="OPEN\n"):
        result = issue_picker.resume_waiting_on_dep(state)
    assert result is None
    # Status preserved.
    assert state["issues"]["77"]["status"] == "waiting_on_dep"
