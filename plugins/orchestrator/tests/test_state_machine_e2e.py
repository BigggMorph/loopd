"""End-to-end-ish state-machine tests.

These tests drive an issue through a sequence of (status, wake_reason)
transitions to verify the FSM stays consistent. We don't run the lead
LLM — we just simulate the transitions the playbook would perform and
check that state is preserved/written correctly across each step.

This is the closest analog to a real `/orchestrator` invocation without
spawning Claude.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest

import audit
import issue_picker
import orchestrator_state
import playbook_helpers as ph


def _new_state(repo: str = "x/y") -> dict:
    state = orchestrator_state.read()
    state["vision"] = "test vision"
    state["repo"] = repo
    state["team_name"] = "orch-test"
    orchestrator_state.write(state)
    return state


def test_happy_path_new_to_done(isolated_home):
    """new → analyze_pending → analyze_received → ready_for_dev →
    dev_running → dev_done → test_pending → test_received → done (auto-merge)
    """
    state = _new_state()
    # 1. Picker selects issue 42 → status=new.
    issue = {
        "number": 42,
        "status": "new",
        "history": [],
        "title": "fix login bug",
        "labels": [{"name": "bug"}],
    }
    state["issues"]["42"] = issue
    state["current_issue"] = 42
    # Auto-merge trust chain at 3 already, so risky gates won't trip.
    state["auto_merge_consecutive_safe"] = 3
    orchestrator_state.write(state)

    # 2. Lead sends analyzer request → status=analyze_pending.
    orchestrator_state.transition(issue, "analyze_pending")
    issue["analyze_pending_started_at"] = orchestrator_state.now().isoformat()

    # 3. Analyzer reply arrives. parse JSON.
    analyzer_reply = (
        "[issue-analyzer]: done\n\n"
        '{"phase":"analyze","status":"complete","should_process":true,'
        '"should_split":false,"human_needed":false,"acceptance_criteria":'
        '["login submits","valid email accepted"],"dev_task_prompt":"Fix login submit.",'
        '"complexity_level":1,"depends_on":[],"touched_paths":["src/auth.py"]}'
    )
    parsed = ph.parse_json_tail(analyzer_reply)
    assert parsed is not None
    issue["acceptance_criteria"] = parsed["acceptance_criteria"]
    issue["dev_task_prompt"] = parsed["dev_task_prompt"]
    issue["complexity_level"] = parsed["complexity_level"]
    orchestrator_state.transition(issue, "analyze_received")
    orchestrator_state.transition(issue, "ready_for_dev")

    # 4. Lead calls /dev-task → mark_dev_started + transition dev_running.
    orchestrator_state.mark_dev_started(state, session_id="cc-session-xyz")
    orchestrator_state.transition(issue, "dev_running")
    orchestrator_state.write(state)

    assert state["dev_session_id"] == "cc-session-xyz"
    assert state["dev_done_injected"] is False

    # 5. β hook fires: dev finished, inject ORCH_INJECT.
    orchestrator_state.mark_dev_done_injected(state)
    orchestrator_state.transition(issue, "dev_done")

    # 6. Lead extracts PR URL from transcript.
    transcript = "Wrapping up.\n[review]: Created https://github.com/x/y/pull/100 ready."
    pr_url = ph.extract_pr_url_from_text(transcript, state["repo"])
    assert pr_url == "https://github.com/x/y/pull/100"
    issue["pr_url"] = pr_url
    issue["test_pending_started_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.transition(issue, "test_pending")

    # 7. Tester reply.
    tester_reply = (
        "[tester]: verdict\n\n"
        '{"phase":"test","status":"complete","verdict":"pass","tests_run":["pytest"],'
        '"test_failures":[],"criteria_match":{"login submits":true,"valid email accepted":true},'
        '"summary":"all green","recommend_human_review":false,"diff_lines":42,'
        '"permission_elevation":{"detected":false,"reasons":[]}}'
    )
    verdict = ph.parse_json_tail(tester_reply)
    assert verdict is not None
    issue["test_verdict"] = verdict
    orchestrator_state.transition(issue, "test_received")

    # 8. Auto-merge gates: all clear.
    assert verdict["verdict"] == "pass"
    assert verdict.get("diff_lines", 10**9) <= 200
    assert not verdict.get("permission_elevation", {}).get("detected")
    assert state["auto_merge_consecutive_safe"] >= 3
    orchestrator_state.transition(issue, "done")
    state["completed_count"] += 1
    state["current_issue"] = None
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    final_issue = final["issues"]["42"]
    assert final_issue["status"] == "done"
    assert final["completed_count"] == 1
    assert final["current_issue"] is None

    # Verify full history chain.
    statuses = [h["to"] for h in final_issue["history"]]
    assert statuses == [
        "analyze_pending",
        "analyze_received",
        "ready_for_dev",
        "dev_running",
        "dev_done",
        "test_pending",
        "test_received",
        "done",
    ]


def test_rework_path_increments_count(isolated_home):
    state = _new_state()
    issue = {
        "number": 7,
        "status": "test_received",
        "history": [],
        "dev_task_prompt": "Fix it",
        "rework_count": 0,
        "test_verdict": {
            "verdict": "fail",
            "test_failures": ["pytest: AssertionError on test_login"],
            "diff_lines": 10,
        },
    }
    state["issues"]["7"] = issue
    state["current_issue"] = 7
    orchestrator_state.write(state)

    # Simulate the test_received-fail branch (rework_count < 2 → ready_for_dev).
    issue["rework_count"] += 1
    issue["dev_task_prompt"] += "\n\n## 이전 시도 실패\n" + str(issue["test_verdict"]["test_failures"])
    issue["last_verdict_signature"] = None
    issue["tester_retried"] = False
    issue["test_pending_started_at"] = None
    orchestrator_state.transition(issue, "ready_for_dev")
    orchestrator_state.write(state)

    final = orchestrator_state.read()["issues"]["7"]
    assert final["status"] == "ready_for_dev"
    assert final["rework_count"] == 1
    assert "이전 시도 실패" in final["dev_task_prompt"]


def test_two_rework_failures_park_to_needs_human(isolated_home):
    state = _new_state()
    issue = {
        "number": 8,
        "status": "test_received",
        "history": [],
        "rework_count": 2,  # already failed twice
        "test_verdict": {"verdict": "fail", "test_failures": ["x"]},
    }
    state["issues"]["8"] = issue
    state["current_issue"] = 8
    orchestrator_state.write(state)

    # Simulate the rework_count == 2 branch → needs_human + detect_lesson.
    issue["failure_reason"] = "dev rework 2회 후에도 tester 거부"
    ph.detect_lesson_pattern(issue["failure_reason"], state)
    orchestrator_state.transition(issue, "needs_human")
    state["current_issue"] = None
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    assert final["issues"]["8"]["status"] == "needs_human"
    assert final["current_issue"] is None
    assert any(
        l["pattern"] == "tester가 PR 2회 연속 거부"
        for l in final["lessons_learned"]
    )


def test_should_process_false_routes_to_reject_confirm(isolated_home):
    state = _new_state()
    issue = {
        "number": 9,
        "status": "analyze_received",
        "history": [],
        "parsed_should_process": False,
    }
    state["issues"]["9"] = issue
    state["current_issue"] = 9

    # Apply the analyze_received → reject_confirm_pending transition.
    issue["reject_category"] = "duplicate"
    issue["reject_reason"] = "Same as #5"
    issue["duplicate_of_url"] = "https://github.com/x/y/issues/5"
    orchestrator_state.transition(issue, "reject_confirm_pending")
    orchestrator_state.write(state)

    final = orchestrator_state.read()["issues"]["9"]
    assert final["status"] == "reject_confirm_pending"
    assert final["reject_category"] == "duplicate"


def test_scout_cycle_transitions(isolated_home):
    state = _new_state()
    # Scout cycle: scout_new → pending → received → confirm_pending → creating → done.
    orchestrator_state.scout_transition(state, "scout_new")
    orchestrator_state.scout_transition(state, "scout_pending")

    scout_reply = (
        "[issue-scout]: candidates\n\n"
        '{"phase":"scout","status":"complete","candidates":['
        '{"id":"c1","title":"Add dark mode","body":"## Problem\\n...\\n",'
        '"labels":["scout-suggested","enhancement"],"complexity_level":1,'
        '"priority_hint":"medium","rationale":"users keep asking"}],'
        '"summary":"1 candidate"}'
    )
    parsed = ph.parse_json_tail(scout_reply)
    assert parsed is not None
    state["scout_candidates"] = parsed["candidates"]
    orchestrator_state.scout_transition(state, "scout_received")
    orchestrator_state.scout_transition(state, "scout_confirm_pending")

    # User confirms c1.
    selected = ph.parse_selected_candidate_ids(["c1"], state["scout_candidates"])
    assert selected == ["c1"]
    state["scout_decisions"] = {"c1": True}
    orchestrator_state.scout_transition(state, "scout_creating")

    # Simulate creation success.
    state["scout_created_urls"] = ["https://github.com/x/y/issues/100"]
    state["scout_creating_done"] = ["c1"]
    orchestrator_state.scout_transition(state, "scout_done")
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    assert final["scout_status"] == "scout_done"
    log = final["scout_history_log"]
    statuses = [h["to"] for h in log]
    assert statuses == [
        "scout_new",
        "scout_pending",
        "scout_received",
        "scout_confirm_pending",
        "scout_creating",
        "scout_done",
    ]

    # clear_scout_fields between cycles.
    ph.clear_scout_fields(final)
    assert final["scout_candidates"] == []
    assert final["scout_decisions"] == {}
    assert final["scout_creating_done"] == []


def test_audit_log_undo_workflow(isolated_home):
    """An audited gh close followed by undo:1 plans the inverse correctly."""
    state = orchestrator_state.read()
    state["repo"] = "x/y"
    orchestrator_state.write(state)

    # Pretend we ran a gh issue close (mocked).
    import subprocess
    fake = subprocess.CompletedProcess(args=[], returncode=0, stdout="closed", stderr="")
    with mock.patch.object(subprocess, "run", return_value=fake):
        rc, _, _ = audit.audited_bash(
            ["gh", "issue", "close", "42", "--repo", "x/y"],
            actor="orchestrator",
            action="gh issue close",
            target="42",
        )
    assert rc == 0

    # /orchestrator undo:1 -> compute_undo_plan should yield "reopen".
    state = orchestrator_state.read()
    plans = audit.compute_undo_plan(state, n=1)
    assert len(plans) == 1
    assert plans[0]["inverse_argv"] == [
        "gh", "issue", "reopen", "42", "--repo", "x/y",
    ]
