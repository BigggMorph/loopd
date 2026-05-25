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


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_mark_dev_started_rejects_empty_session_id(isolated_home, bad):
    state = orchestrator_state.read()
    with pytest.raises(ValueError, match="session_id must be a non-empty string"):
        orchestrator_state.mark_dev_started(state, bad)


def test_mark_dev_started_rejects_non_string(isolated_home):
    state = orchestrator_state.read()
    with pytest.raises(ValueError):
        orchestrator_state.mark_dev_started(state, None)  # type: ignore[arg-type]


def test_current_session_id_reads_loopd_env(monkeypatch):
    monkeypatch.setenv("LOOPD_SESSION_ID", "abc-123")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert orchestrator_state.current_session_id() == "abc-123"


def test_current_session_id_falls_back_to_claude_env(monkeypatch):
    monkeypatch.delenv("LOOPD_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "cc-uuid-9")
    assert orchestrator_state.current_session_id() == "cc-uuid-9"


def test_current_session_id_prefers_loopd_over_claude(monkeypatch):
    # Mirrors loopd's _session_id() precedence so the two helpers agree.
    monkeypatch.setenv("LOOPD_SESSION_ID", "loopd-first")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-second")
    assert orchestrator_state.current_session_id() == "loopd-first"


def test_current_session_id_reads_claude_code_env(monkeypatch):
    # CLAUDE_CODE_SESSION_ID is the canonical harness var (== hook payload).
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "ccsid-uuid")
    assert orchestrator_state.current_session_id() == "ccsid-uuid"


def test_current_session_id_prefers_claude_code_over_legacy(monkeypatch):
    # The canonical var must win — it is what orch_stop's payload.session_id
    # carries, so storing it is what makes Gate 1 match.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "canonical")
    monkeypatch.setenv("LOOPD_SESSION_ID", "loopd-legacy")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "claude-legacy")
    assert orchestrator_state.current_session_id() == "canonical"


def test_current_session_id_raises_when_unset(monkeypatch):
    # conftest already clears all three session vars; this asserts the raise.
    with pytest.raises(RuntimeError, match="current_session_id"):
        orchestrator_state.current_session_id()


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


def test_normalize_backfills_rev17_planning_fields(isolated_home):
    """Rev 17: a pre-planning-layer state.json should auto-gain new fields."""
    orchestrator_state.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 3,
        "vision": "v",
        "repo": "x/y",
        "issues": {},
        "scout_history": [],
    }
    orchestrator_state.STATE_PATH.write_text(json.dumps(legacy))
    state = orchestrator_state.read()
    # Product-planner defaults.
    assert state["planner_status"] is None
    assert state["planner_candidates"] == []
    assert state["planner_history"] == []
    # Roadmap defaults.
    assert state["roadmap_status"] is None
    assert state["roadmap_reports"] == []
    assert state["active_phase_context"] is None
    # Vision-critic defaults.
    assert state["vision_check_status"] is None
    assert state["vision_critic_history"] == []
    assert state["vision_critic_pending_delta"] is None
    assert state["rejected_delta_hashes"] == []
    assert state["last_vision_critic_cycle"] == 0
    # Lazy spawn / health defaults.
    assert state["pending_team_spawns"] == []
    assert state["teammate_health"] == {}
    assert state["pending_respawn"] == {}


def test_planning_transition_records_history(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.planning_transition(state, "planning_pending")
    assert state["planner_status"] == "planning_pending"
    orchestrator_state.planning_transition(state, "planning_creating")
    assert state["planner_status"] == "planning_creating"
    log = state["planning_history_log"]
    assert [e["to"] for e in log] == ["planning_pending", "planning_creating"]


def test_planning_transition_to_none_returns_to_idle(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.planning_transition(state, "planning_pending")
    orchestrator_state.planning_transition(state, None)
    assert state["planner_status"] is None


def test_planning_transition_rejects_unknown(isolated_home):
    state = orchestrator_state.read()
    with pytest.raises(ValueError):
        orchestrator_state.planning_transition(state, "totally_made_up")


def test_planning_transition_idempotent(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.planning_transition(state, "planning_pending")
    orchestrator_state.planning_transition(state, "planning_pending")
    assert len(state["planning_history_log"]) == 1


def test_roadmap_transition_basic(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.roadmap_transition(state, "roadmap_pending")
    assert state["roadmap_status"] == "roadmap_pending"
    orchestrator_state.roadmap_transition(state, "roadmap_received")
    orchestrator_state.roadmap_transition(state, "roadmap_done")
    orchestrator_state.roadmap_transition(state, None)
    assert state["roadmap_status"] is None
    assert len(state["roadmap_history_log"]) == 4


def test_roadmap_transition_rejects_unknown(isolated_home):
    state = orchestrator_state.read()
    with pytest.raises(ValueError):
        orchestrator_state.roadmap_transition(state, "nope")


def test_vision_transition_basic(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.vision_transition(state, "vision_check_pending")
    assert state["vision_check_status"] == "vision_check_pending"
    orchestrator_state.vision_transition(state, "vision_check_parked")
    assert state["vision_check_status"] == "vision_check_parked"


def test_vision_transition_rejects_unknown(isolated_home):
    state = orchestrator_state.read()
    with pytest.raises(ValueError):
        orchestrator_state.vision_transition(state, "vision_check_done_wrong")


def test_rev17_fields_roundtrip(isolated_home):
    """Write Rev 17 fields, read back, verify shape preserved."""
    state = orchestrator_state.read()
    state["planner_history"].append({
        "ts": "2026-05-22T10:00:00Z",
        "candidates_proposed": 3,
        "candidates_accepted": 2,
        "candidates_rejected": 1,
        "issue_urls_created": ["https://github.com/x/y/issues/1"],
    })
    state["vision_critic_history"].append({
        "ts": "2026-05-22T10:00:00Z",
        "source": "vision_critic",
        "before": "old vision",
        "after": "new vision",
        "rationale": "...",
        "user_action": "accepted",
        "alignment_score": 0.4,
    })
    state["vision_critic_pending_delta"] = {
        "before": "x",
        "after": "y",
        "rationale": "...",
        "alignment_score": 0.5,
        "proposed_at": "2026-05-22T10:00:00Z",
    }
    state["roadmap_reports"].append({
        "ts": "2026-05-22T10:00:00Z",
        "current_phase": "pre-mvp",
        "user_action": "accepted",
    })
    state["rejected_delta_hashes"].append({
        "hash": "abc",
        "before_norm": "before",
        "after_norm": "after",
        "rejected_at": "2026-05-22T10:00:00Z",
        "last_seen_at": "2026-05-22T10:00:00Z",
        "rejection_count": 1,
    })
    orchestrator_state.write(state)
    state2 = orchestrator_state.read()
    assert state2["planner_history"][0]["candidates_accepted"] == 2
    assert state2["vision_critic_history"][0]["user_action"] == "accepted"
    assert state2["vision_critic_pending_delta"]["after"] == "y"
    assert state2["roadmap_reports"][0]["current_phase"] == "pre-mvp"
    assert state2["rejected_delta_hashes"][0]["hash"] == "abc"


# =====================================================================
# Rev 17 Phase 17-G — prune_state_history
# =====================================================================

import datetime as _dt


def test_prune_state_history_caps_lessons_learned_fifo(isolated_home):
    state = orchestrator_state.read()
    state["lessons_learned"] = [
        {"pattern": f"p{i}", "observed_count": 1} for i in range(150)
    ]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned["lessons_learned"] == 50  # 150 - 100 cap
    assert len(state["lessons_learned"]) == 100
    # Oldest entries dropped (FIFO).
    assert state["lessons_learned"][0]["pattern"] == "p50"


def test_prune_state_history_caps_scout_history(isolated_home):
    state = orchestrator_state.read()
    state["scout_history"] = [{"ts": "2026-05-22T10:00:00Z", "i": i} for i in range(60)]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned["scout_history"] == 10
    assert len(state["scout_history"]) == 50


def test_prune_state_history_caps_planner_history(isolated_home):
    state = orchestrator_state.read()
    state["planner_history"] = [{"ts": "2026-05-22T10:00:00Z", "i": i} for i in range(80)]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned["planner_history"] == 30
    assert len(state["planner_history"]) == 50


def test_prune_state_history_caps_roadmap_reports(isolated_home):
    state = orchestrator_state.read()
    state["roadmap_reports"] = [{"ts": "2026-05-22T10:00:00Z", "i": i} for i in range(15)]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned["roadmap_reports"] == 5
    assert len(state["roadmap_reports"]) == 10


def test_prune_state_history_ttl_drops_old_entries(isolated_home):
    state = orchestrator_state.read()
    state["lessons_learned"] = [
        {"pattern": "old", "first_at": "2020-01-01T00:00:00+00:00", "last_at": "2020-01-01T00:00:00+00:00"},
        {"pattern": "fresh", "first_at": "9999-01-01T00:00:00+00:00", "last_at": "9999-01-01T00:00:00+00:00"},
    ]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned.get("lessons_learned") == 1
    patterns = [e["pattern"] for e in state["lessons_learned"]]
    assert patterns == ["fresh"]


def test_prune_state_history_does_not_touch_single_slot_fields(isolated_home):
    """Round A S7: single-slot fields are blocklisted from prune."""
    state = orchestrator_state.read()
    state["vision_critic_pending_delta"] = {
        "before": "x", "after": "y", "rationale": "r"
    }
    state["active_phase_context"] = "Phase: mvp-validation"
    orchestrator_state.prune_state_history(state)
    # Single-slot fields preserved verbatim.
    assert state["vision_critic_pending_delta"]["after"] == "y"
    assert state["active_phase_context"] == "Phase: mvp-validation"


def test_prune_state_history_preserves_list_str_vision_history(isolated_home):
    """vision_history is list[str] (sources) — must not be filtered by TTL."""
    state = orchestrator_state.read()
    state["vision_history"] = ["v1", "v2", "v3"]
    orchestrator_state.prune_state_history(state)
    assert state["vision_history"] == ["v1", "v2", "v3"]


def test_prune_state_history_ttl_drops_rejected_delta_hashes(isolated_home):
    state = orchestrator_state.read()
    state["rejected_delta_hashes"] = [
        {
            "hash": "old", "before_norm": "x", "after_norm": "y",
            "rejected_at": "2020-01-01T00:00:00+00:00",
            "last_seen_at": "2020-01-01T00:00:00+00:00",
            "rejection_count": 1,
        },
        {
            "hash": "fresh", "before_norm": "a", "after_norm": "b",
            "rejected_at": "9999-01-01T00:00:00+00:00",
            "last_seen_at": "9999-01-01T00:00:00+00:00",
            "rejection_count": 1,
        },
    ]
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned.get("rejected_delta_hashes") == 1
    assert state["rejected_delta_hashes"][0]["hash"] == "fresh"


def test_prune_state_history_no_op_on_empty(isolated_home):
    state = orchestrator_state.read()
    pruned = orchestrator_state.prune_state_history(state)
    assert pruned == {}


def test_state_file_size_bytes_zero_when_missing(isolated_home):
    # Make sure no state.json exists.
    orchestrator_state.reset_to_empty()
    assert orchestrator_state.state_file_size_bytes() == 0


def test_state_file_size_bytes_positive_after_write(isolated_home):
    state = orchestrator_state.read()
    orchestrator_state.write(state)
    assert orchestrator_state.state_file_size_bytes() > 0


def test_state_size_watermarks_defined():
    assert orchestrator_state.STATE_SIZE_WARN_BYTES > 0
    assert orchestrator_state.STATE_SIZE_HARD_BYTES > orchestrator_state.STATE_SIZE_WARN_BYTES
