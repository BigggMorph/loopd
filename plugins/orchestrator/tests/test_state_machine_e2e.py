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
import safety


def _new_state(repo: str = "x/y") -> dict:
    state = orchestrator_state.read()
    state["vision"] = "test vision"
    state["repo"] = repo
    state["team_name"] = "orch-test"
    orchestrator_state.write(state)
    return state


# --- Rev 19 post-merge observation: Python simulations of the prose playbook ---
# These mirror the SKILL.md / design.md merge path + Step −5 sweep using the
# real helpers (the gh calls are external, so the CI classification is supplied
# directly — classify_ci_completion itself is unit-tested in
# test_playbook_helpers.py).

def _enter_observation(state: dict, issue: dict, *, merged_at=None) -> dict:
    """Mirror the test_received auto-merge success path (Rev 19)."""
    cap = ph.observe_cap_minutes()
    merged = merged_at or orchestrator_state.now()
    w = {
        "pr_url": issue.get("pr_url") or f"https://github.com/x/y/pull/{issue['number']}",
        "issue_num": issue["number"],
        "pr_branch": issue.get("pr_branch") or f"loopd-task-{issue['number']}",
        "touched_paths": issue.get("touched_paths") or [],
        "merged_at": merged.isoformat(),
        "merge_commit_sha": None,
        "last_checked_at": None,
        "expires_at": (merged + timedelta(minutes=cap)).isoformat(),
    }
    state["watch_list"].append(w)
    orchestrator_state.transition(issue, "merged_observing")
    state["current_issue"] = None
    return w


def _route_regression(state: dict, issue: dict, w: dict) -> None:
    orchestrator_state.transition(issue, "regression_detected")
    safety.push_pending_question(state, {
        "question": f"#{issue['number']} regression suspected. revert/keep/manual?",
        "options": [{"label": "revert"}, {"label": "keep"}, {"label": "manual"}],
        "target": f"regression:{w['pr_url']}",
    })


def _run_sweep_entry(state, issue, w, *, cls="running", reverted=False, xref=False):
    """Mirror Step −5's per-entry branch for one watch entry, in-place.

    Drops or keeps the entry in state['watch_list'] exactly as the sweep would.
    `cls` is the classify_ci_completion verdict the lead would compute.
    """
    def _keep():
        if w not in state["watch_list"]:
            state["watch_list"].append(w)

    def _drop():
        state["watch_list"] = [x for x in state["watch_list"] if x is not w]

    # Process against a fresh list each call (sweep rebuilds survivors).
    state["watch_list"] = [x for x in state["watch_list"] if x is not w]

    status = issue.get("status")
    if status not in ("merged_observing", "regression_detected"):
        return  # orphan / terminal → pruned

    # 0. regression_detected: apply the user decision, or keep waiting.
    if status == "regression_detected":
        decision = issue.get("regression_decision")
        if decision == "revert":
            orchestrator_state.transition(issue, "reverted"); return
        if decision == "keep":
            orchestrator_state.transition(issue, "done_final"); state["completed_count"] += 1; return
        if decision == "manual":
            issue["failure_reason"] = "regression suspected — user chose manual review"
            orchestrator_state.transition(issue, "needs_human"); return
        _keep(); return  # no decision yet → wait

    # status == merged_observing → CI-completion gate.
    if cls == "pending_merge":
        _keep(); return
    if cls == "closed_unmerged":
        issue["regression_evidence"] = {"reason": "pr_closed_unmerged"}
        _route_regression(state, issue, w); _keep(); return
    if cls == "failed" or reverted or xref:
        issue["regression_evidence"] = {"ci": cls, "reverted_externally": reverted}
        _route_regression(state, issue, w); _keep(); return
    if cls == "passed":
        orchestrator_state.transition(issue, "done_final"); state["completed_count"] += 1; return
    # no_checks / running → safety cap
    cap = ph.observe_cap_minutes()
    from datetime import datetime
    if orchestrator_state.now() - datetime.fromisoformat(w["merged_at"]) >= timedelta(minutes=cap):
        orchestrator_state.transition(issue, "done_final"); state["completed_count"] += 1; return
    _keep()


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

    # 8. Auto-merge gates: all clear → merge enters non-blocking observation
    #    (Rev 19): merged_observing + current_issue freed immediately, NOT done.
    assert verdict["verdict"] == "pass"
    assert verdict.get("diff_lines", 10**9) <= 200
    assert not verdict.get("permission_elevation", {}).get("detected")
    assert state["auto_merge_consecutive_safe"] >= 3
    _enter_observation(state, issue)
    orchestrator_state.write(state)

    observing = orchestrator_state.read()
    assert observing["issues"]["42"]["status"] == "merged_observing"
    assert observing["current_issue"] is None          # freed → throughput unblocked
    assert observing["completed_count"] == 0           # NOT counted yet
    assert len(observing["watch_list"]) == 1

    # 9. Background Step −5 sweep: merge-commit CI on main passes → done_final.
    _run_sweep_entry(state, issue, state["watch_list"][0], cls="passed")
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    final_issue = final["issues"]["42"]
    assert final_issue["status"] == "done_final"
    assert final["completed_count"] == 1
    assert final["current_issue"] is None
    assert final["watch_list"] == []

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
        "merged_observing",
        "done_final",
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


# =====================================================================
# Rev 17 — Planning layer e2e fixtures
# =====================================================================

def _stage1_state(isolated_home, monkeypatch):
    """Common setup: Stage 1 just entered (scout_pending + planning_pending)."""
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    state = _new_state()
    state["mode"] = "scouting"
    orchestrator_state.scout_transition(state, "scout_new")
    orchestrator_state.scout_transition(state, "scout_pending")
    orchestrator_state.planning_transition(state, "planning_pending")
    state["scout_started_at"] = orchestrator_state.now().isoformat()
    state["planner_started_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.write(state)
    return state


def test_stage1_happy_path_both_reply_and_merge(isolated_home, monkeypatch):
    """Stage 1: both teammates reply → dedup → merged pool → user confirms both."""
    state = _stage1_state(isolated_home, monkeypatch)

    # 1. issue-scout reply arrives.
    scout_reply = (
        "[issue-scout]: candidates\n\n"
        '{"phase":"scout","status":"complete","candidates":['
        '{"id":"c1","title":"Improve test coverage","body":"## Problem\\nlow coverage",'
        '"labels":["scout-suggested","test"],"complexity_level":1,'
        '"priority_hint":"medium","rationale":"raise quality"}],'
        '"summary":"1 candidate"}'
    )
    scout_parsed = ph.parse_json_tail(scout_reply)
    state["scout_candidates_buffer"] = scout_parsed["candidates"]
    state["scout_pending_resolved_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.scout_transition(state, "scout_received")

    # 2. product-planner reply arrives later.
    planner_reply = (
        "[product-planner]: epics\n\n"
        '{"phase":"plan","status":"complete","candidates":['
        '{"id":"p1","title":"Onboarding wizard Epic","body":"## Problem\\n'
        'New users have no guided path.\\n\\n## User Story\\nAs a new user, I want'
        ' a guided wizard so that I can start fast.\\n\\n## Acceptance Criteria\\n'
        '- [ ] step 1\\n- [ ] step 2\\n- [ ] step 3\\n- [ ] step 4\\n- [ ] step 5\\n'
        '- [ ] step 6\\n- [ ] step 7\\n\\n## Out of Scope\\n- legacy app",'
        '"labels":["enhancement","planner-suggested","split-epic","priority/medium"],'
        '"complexity_level":3,"priority_hint":"high","rationale":"vision",'
        '"user_value":"first-time success"}],'
        '"summary":"1 epic"}'
    )
    planner_parsed = ph.parse_json_tail(planner_reply)
    state["planner_candidates_buffer"] = planner_parsed["candidates"]
    state["planning_pending_resolved_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.planning_transition(state, "planning_done")

    # 3. Merge step — both buffers are non-None now.
    assert state["scout_candidates_buffer"] is not None
    assert state["planner_candidates_buffer"] is not None
    merge = ph.dedup_candidates(
        state["scout_candidates_buffer"], state["planner_candidates_buffer"]
    )
    state["scout_candidates"] = [
        c for c in merge["merged"] if c.get("source") == "scout"
    ]
    state["planner_candidates"] = [
        c for c in merge["merged"] if c.get("source") == "planner"
    ]
    state["scout_candidates_buffer"] = None
    state["planner_candidates_buffer"] = None
    # No dedup drops expected here (totally different topics).
    assert merge["drops"] == []

    # 4. User confirms both.
    state["scout_decisions"] = {"c1": True}
    state["planner_decisions"] = {"p1": True}
    orchestrator_state.scout_transition(state, "scout_confirm_pending")
    orchestrator_state.scout_transition(state, "scout_creating")
    orchestrator_state.planning_transition(state, "planning_creating")

    # 5. Simulate creation success.
    plan_scout = ph.candidate_create_plan(
        state["scout_candidates"][0],
        fp_prefix="scout-fp-",
        extra_labels=["scout-suggested"],
    )
    plan_planner = ph.candidate_create_plan(
        state["planner_candidates"][0],
        fp_prefix="planner-fp-",
        extra_labels=["planner-suggested", "split-epic"],
    )
    state["scout_created_urls"] = ["https://github.com/x/y/issues/200"]
    state["scout_creating_done"] = ["c1"]
    state["planner_created_urls"] = ["https://github.com/x/y/issues/201"]
    state["planner_creating_done"] = ["p1"]

    # 6. Finalize Stage 1.
    orchestrator_state.scout_transition(state, "scout_done")
    orchestrator_state.planning_transition(state, "planning_done")
    state["scout_history"].append({
        "ts": orchestrator_state.now().isoformat(),
        "candidates_proposed": 1, "candidates_accepted": 1,
        "created_urls": state["scout_created_urls"],
    })
    state["planner_history"].append({
        "ts": orchestrator_state.now().isoformat(),
        "candidates_proposed": 1, "candidates_accepted": 1,
        "issue_urls_created": state["planner_created_urls"],
    })
    state["last_stage1_completed_at"] = orchestrator_state.now().isoformat()
    state["mode"] = "resolution"
    ph.clear_scout_fields(state)
    ph.clear_planner_fields(state)
    orchestrator_state.scout_transition(state, "scout_done")
    orchestrator_state.planning_transition(state, None)
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    assert final["last_stage1_completed_at"] is not None
    assert len(final["scout_history"]) == 1
    assert len(final["planner_history"]) == 1
    # Confirm plan helper produces planner-fp prefix
    assert plan_planner["fingerprint_label"].startswith("planner-fp-")
    assert plan_scout["fingerprint_label"].startswith("scout-fp-")


def test_stage1_partial_timeout_scout_only(isolated_home, monkeypatch):
    """Round A E1: planner times out, scout proceeds alone."""
    state = _stage1_state(isolated_home, monkeypatch)

    # Only scout reply arrives.
    state["scout_candidates_buffer"] = [{
        "id": "c1", "title": "Quick fix", "body": "x",
        "labels": ["scout-suggested"], "complexity_level": 1,
    }]
    state["scout_pending_resolved_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.scout_transition(state, "scout_received")

    # Planner times out — treat as empty.
    state["planner_candidates_buffer"] = []
    state["planning_pending_resolved_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.planning_transition(state, "planning_done")

    # Merge proceeds with what we have.
    merge = ph.dedup_candidates(
        state["scout_candidates_buffer"], state["planner_candidates_buffer"]
    )
    assert len(merge["merged"]) == 1
    assert merge["merged"][0]["source"] == "scout"

    # Late planner reply arrives — should be discarded.
    state["scout_candidates_buffer"] = None
    state["planner_candidates_buffer"] = None
    orchestrator_state.write(state)

    # Confirm the playbook logic: when buffer is None and resolved_at is set,
    # any new reply is discarded by inspection.
    reloaded = orchestrator_state.read()
    assert reloaded["planning_pending_resolved_at"] is not None
    assert reloaded["planner_candidates_buffer"] is None


def test_stage1_dedup_drops_scout_candidate(isolated_home, monkeypatch):
    """Round A — dedup drops scout if planner Epic covers same area."""
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    state = _new_state()
    scout = [{"id": "c1", "title": "Add onboarding wizard", "body": "wizard"}]
    planner = [{
        "id": "p1",
        "title": "Add onboarding wizard",
        "body": "wizard\n## User Story\nAs a new user...",
        "labels": ["planner-suggested", "split-epic"],
        "complexity_level": 3,
    }]
    merge = ph.dedup_candidates(scout, planner)
    sources = [c["source"] for c in merge["merged"]]
    assert sources == ["planner"]
    assert len(merge["drops"]) == 1
    drop = merge["drops"][0]
    assert drop["dropped_id"] == "c1"
    assert drop["kept_id"] == "p1"


def test_stage2_roadmap_accept_sets_phase_context(isolated_home):
    """Stage 2: roadmap report → user accept → active_phase_context updated."""
    state = _new_state()
    state["last_stage1_completed_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.roadmap_transition(state, "roadmap_pending")
    state["roadmap_started_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.write(state)

    roadmap_reply = (
        "[roadmap-strategist]: report\n\n"
        '{"phase":"roadmap","status":"complete","current_phase":"mvp-validation",'
        '"phase_evidence":["3 onboarding PRs merged","retention metrics added"],'
        '"critical_path":"Increase first-day retention",'
        '"stage1_evaluation":{"addresses_critical_path":true,"missing_areas":[],'
        '"recommended_picker_boost":[]},'
        '"phase_context_for_next_cycles":"Phase: mvp-validation\\nFocus: retention",'
        '"vision_alignment_concern":"",'
        '"summary":"good direction"}'
    )
    parsed = ph.parse_json_tail(roadmap_reply)
    assert parsed["current_phase"] == "mvp-validation"

    report = {
        "ts": orchestrator_state.now().isoformat(),
        "current_phase": parsed["current_phase"],
        "phase_context_for_next_cycles": parsed["phase_context_for_next_cycles"],
        "user_action": "pending",
    }
    state["roadmap_reports"].append(report)
    state["last_roadmap_report_cycle"] = 1
    orchestrator_state.roadmap_transition(state, "roadmap_received")
    orchestrator_state.roadmap_transition(state, "roadmap_done")
    orchestrator_state.roadmap_transition(state, None)

    # User accepts via Step −3 flush.
    state["active_phase_context"] = report["phase_context_for_next_cycles"]
    state["active_phase_context_until_cycle"] = 26  # current_cycle + 25
    state["roadmap_reports"][-1]["user_action"] = "accepted"
    orchestrator_state.write(state)

    final = orchestrator_state.read()
    assert final["roadmap_status"] is None
    assert final["active_phase_context"].startswith("Phase: mvp-validation")
    assert final["active_phase_context_until_cycle"] == 26


def test_stage3_vision_critic_accept_audits_state_mutation(isolated_home):
    """Stage 3: vision delta accept → state.vision updated + audit entry."""
    state = _new_state()
    state["vision"] = "Vision: build AI assistant"
    orchestrator_state.vision_transition(state, "vision_check_pending")
    state["vision_check_started_at"] = orchestrator_state.now().isoformat()
    orchestrator_state.write(state)

    delta = {
        "before": "Vision: build AI assistant",
        "after": "Vision: build AI assistant for software developers",
        "rationale": "narrow scope improves retention",
        "alignment_score": 0.55,
        "proposed_at": orchestrator_state.now().isoformat(),
    }
    state["vision_critic_pending_delta"] = delta
    orchestrator_state.vision_transition(state, "vision_check_received")
    orchestrator_state.write(state)

    # User accepts.
    with orchestrator_state.flock_session() as s:
        prev_vision = s["vision"]
        s["vision"] = delta["after"]
        s["vision_history"].append(prev_vision)
        s["vision_critic_history"].append({
            "ts": orchestrator_state.now().isoformat(),
            "source": "vision_critic",
            "before": delta["before"],
            "after": delta["after"],
            "rationale": delta["rationale"],
            "user_action": "accepted",
            "alignment_score": delta["alignment_score"],
        })
        audit.record_state_mutation(
            s,
            actor="vision-critic",
            action="state.vision update (accepted)",
            payload={
                "before": prev_vision,
                "after": delta["after"],
                "rationale": delta["rationale"],
            },
        )
        s["vision_critic_pending_delta"] = None
        orchestrator_state.write_in_lock(s)

    final = orchestrator_state.read()
    assert final["vision"] == "Vision: build AI assistant for software developers"
    assert final["vision_history"] == ["Vision: build AI assistant"]
    assert len(final["vision_critic_history"]) == 1
    assert final["vision_critic_history"][0]["user_action"] == "accepted"
    assert final["vision_critic_pending_delta"] is None
    # Audit log carries the mutation.
    log = final["audit_log"]
    assert any(
        e.get("action") == "state.vision update (accepted)"
        and e.get("kind") == "state_mutation"
        for e in log
    )


def test_stage3_vision_critic_reject_increments_hash(isolated_home):
    state = _new_state()
    state["vision"] = "old"
    delta = {
        "before": "old vision text",
        "after": "weakened vision text",
        "rationale": "...",
        "alignment_score": 0.3,
    }
    state["vision_critic_pending_delta"] = delta
    orchestrator_state.vision_transition(state, "vision_check_received")
    orchestrator_state.write(state)

    # User rejects.
    with orchestrator_state.flock_session() as s:
        ph.record_rejected_delta(s, delta["before"], delta["after"])
        s["vision_critic_history"].append({
            "ts": orchestrator_state.now().isoformat(),
            "source": "vision_critic",
            "before": delta["before"],
            "after": delta["after"],
            "rationale": delta["rationale"],
            "user_action": "rejected",
            "alignment_score": delta["alignment_score"],
        })
        s["vision_critic_pending_delta"] = None
        orchestrator_state.write_in_lock(s)

    final = orchestrator_state.read()
    assert len(final["rejected_delta_hashes"]) == 1
    assert final["rejected_delta_hashes"][0]["rejection_count"] == 1
    assert final["vision_critic_history"][0]["user_action"] == "rejected"


def test_stage3_three_rejections_same_before_park_vision_critic(isolated_home):
    """Round A S4: 3+ rejections on the same `before` → vision-critic parked."""
    state = _new_state()
    # Three different `after` proposals on the same `before` topic.
    for after in ("after v1", "after v2", "after v3"):
        ph.record_rejected_delta(state, "same before topic", after)
    assert ph.count_same_before_rejections(state, "same before topic") == 3
    # Lead would park vision-critic now.
    orchestrator_state.vision_transition(state, "vision_check_parked")
    orchestrator_state.write(state)
    final = orchestrator_state.read()
    assert final["vision_check_status"] == "vision_check_parked"


def test_stage3_alignment_above_threshold_auto_skips(isolated_home):
    """User decision §9.1 option B: alignment_score > 0.8 → no user prompt."""
    state = _new_state()
    orchestrator_state.vision_transition(state, "vision_check_pending")

    # Vision-critic returns a high alignment_score.
    score = 0.9
    assert ph.vision_alignment_skip(score) is True
    # Lead would record the report and skip prompting.
    state["vision_critic_history"].append({
        "ts": orchestrator_state.now().isoformat(),
        "source": "vision_critic",
        "before": "x", "after": "y", "rationale": "ok",
        "user_action": "auto_skipped",
        "alignment_score": score,
    })
    orchestrator_state.vision_transition(state, None)
    orchestrator_state.write(state)
    final = orchestrator_state.read()
    assert final["vision_check_status"] is None
    assert final["vision_critic_history"][-1]["user_action"] == "auto_skipped"


def test_stage3_guard_token_deletion_blocks_delta(isolated_home):
    delta = {
        "before": "all changes require human confirm and audit log",
        "after": "all changes ship instantly",
    }
    removed = ph.vision_delta_violates_guard(delta)
    assert set(removed) >= {"human", "confirm", "audit"}


def test_force_split_flow_for_planner_epic(isolated_home):
    """Planner-suggested Epic with audit trail triggers FORCE_SPLIT."""
    state = _new_state()
    # Simulate orchestrator creating issue 77.
    state["audit_log"].append({
        "action": "gh issue create",
        "target": "77",
        "argv": ["gh", "issue", "create", "--repo", "x/y", "--title", "Epic"],
    })
    issue = {
        "number": 77,
        "labels": [
            {"name": "planner-suggested"},
            {"name": "split-epic"},
            {"name": "enhancement"},
        ],
        "body": "## User Story\nAs a user...",
    }
    assert issue_picker.needs_force_split(issue, state) is True


def test_force_split_external_labels_blocked(isolated_home):
    """External user-applied labels do NOT trigger FORCE_SPLIT."""
    state = _new_state()  # no audit entry
    issue = {
        "number": 88,
        "labels": [{"name": "planner-suggested"}, {"name": "split-epic"}],
        "body": "user story",
    }
    assert issue_picker.needs_force_split(issue, state) is False


def test_planner_creating_resumes_after_partial_write(isolated_home):
    """planner_creating_done preserves progress across simulated crash."""
    state = _new_state()
    candidates = [
        {"id": "p1", "title": "Epic 1", "body": "a", "labels": ["planner-suggested"]},
        {"id": "p2", "title": "Epic 2", "body": "b", "labels": ["planner-suggested"]},
    ]
    state["planner_candidates"] = candidates
    state["planner_decisions"] = {"p1": True, "p2": True}
    orchestrator_state.planning_transition(state, "planning_creating")
    state["planner_creating_done"] = ["p1"]  # first survived
    state["planner_created_urls"] = ["https://github.com/x/y/issues/300"]
    orchestrator_state.write(state)

    reloaded = orchestrator_state.read()
    # Lead resuming: skip p1 (already in planner_creating_done), do p2.
    pending = [
        c for c in reloaded["planner_candidates"]
        if reloaded["planner_decisions"].get(c["id"])
        and c["id"] not in reloaded["planner_creating_done"]
    ]
    assert [c["id"] for c in pending] == ["p2"]


# ---------- Rev 19: non-blocking post-merge observation ----------

def _observing_state(num=42, merged_at=None):
    state = _new_state()
    issue = {
        "number": num, "status": "test_received", "history": [],
        "pr_url": f"https://github.com/x/y/pull/{num}",
        "pr_branch": f"loopd-task-{num}", "touched_paths": ["src/auth.py"],
    }
    state["issues"][str(num)] = issue
    state["current_issue"] = num
    orchestrator_state.write(state)
    w = _enter_observation(state, issue, merged_at=merged_at)
    orchestrator_state.write(state)
    return state, issue, w


def test_merge_frees_current_issue_and_picks_next(isolated_home):
    """Headline throughput test: merging issue A frees current_issue
    immediately, and a new issue B can proceed while A is still observed."""
    state, issue_a, w = _observing_state(num=42)
    assert state["current_issue"] is None        # freed at merge
    assert issue_a["status"] == "merged_observing"
    assert state["completed_count"] == 0         # not counted while observing
    assert len(state["watch_list"]) == 1

    # Lead's next wake picks issue B while A is still in watch_list.
    issue_b = {"number": 43, "status": "new", "history": []}
    state["issues"]["43"] = issue_b
    state["current_issue"] = 43
    orchestrator_state.transition(issue_b, "analyze_pending")
    orchestrator_state.write(state)

    reloaded = orchestrator_state.read()
    assert reloaded["current_issue"] == 43       # B is in flight
    assert reloaded["issues"]["42"]["status"] == "merged_observing"  # A still observed
    assert len(reloaded["watch_list"]) == 1


def test_watch_entry_issue_num_resolves_only_via_get_issue(isolated_home):
    """Guard: watch entry issue_num is an int but state['issues'] keys are str.
    The sweep MUST resolve via get_issue (str-normalized), not state.issues.get."""
    state, issue, w = _observing_state(num=42)
    assert isinstance(w["issue_num"], int)
    # Direct int lookup misses (this is the bug the sweep must avoid):
    assert state["issues"].get(w["issue_num"]) is None
    # get_issue normalizes int -> str and finds it:
    assert orchestrator_state.get_issue(state, w["issue_num"]) is issue


def test_observe_sweep_ci_passed_done_final_counts_once(isolated_home):
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="passed")
    orchestrator_state.write(state)
    assert issue["status"] == "done_final"
    assert state["completed_count"] == 1
    assert state["watch_list"] == []

    # Re-running the sweep must NOT double-count (entry pruned, status guard).
    _run_sweep_entry(state, issue, w, cls="passed")
    orchestrator_state.write(state)
    assert orchestrator_state.read()["completed_count"] == 1


def test_observe_sweep_ci_failed_routes_pending_question(isolated_home):
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="failed")
    orchestrator_state.write(state)
    assert issue["status"] == "regression_detected"
    assert state["current_issue"] is None            # never hijacked
    assert len(state["watch_list"]) == 1             # entry kept until decision
    targets = [q.get("target") for q in state["pending_questions"]]
    assert f"regression:{w['pr_url']}" in targets
    # Dedup: a second failed sweep must not enqueue a duplicate question.
    _run_sweep_entry(state, issue, w, cls="failed")
    assert sum(1 for t in targets if t == f"regression:{w['pr_url']}") == 1


def test_observe_sweep_external_revert_routes_regression(isolated_home):
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="passed", reverted=True)
    assert issue["status"] == "regression_detected"  # revert signal overrides pass


def test_observe_sweep_no_checks_falls_to_safety_cap(isolated_home):
    # Within the cap → keep waiting.
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="no_checks")
    orchestrator_state.write(state)
    assert issue["status"] == "merged_observing"
    assert len(state["watch_list"]) == 1

    # Backdate the same entry past the cap → done_final.
    old = orchestrator_state.now() - timedelta(minutes=ph.observe_cap_minutes() + 5)
    w["merged_at"] = old.isoformat()
    _run_sweep_entry(state, issue, w, cls="no_checks")
    orchestrator_state.write(state)
    assert issue["status"] == "done_final"
    assert state["completed_count"] == 1
    assert state["watch_list"] == []


def test_observe_sweep_running_respects_safety_cap(isolated_home):
    old = orchestrator_state.now() - timedelta(minutes=ph.observe_cap_minutes() + 1)
    state, issue, w = _observing_state(num=42, merged_at=old)
    _run_sweep_entry(state, issue, w, cls="running")
    assert issue["status"] == "done_final"           # cap reached, no failure → pass


def test_observe_sweep_pending_merge_waits(isolated_home):
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="pending_merge")
    orchestrator_state.write(state)
    assert issue["status"] == "merged_observing"     # auto-merge not landed → wait
    assert len(state["watch_list"]) == 1


def test_observe_sweep_orphan_entry_pruned(isolated_home):
    state, issue, w = _observing_state(num=42)
    orchestrator_state.transition(issue, "done_final")   # already left observation
    _run_sweep_entry(state, issue, w, cls="passed")
    orchestrator_state.write(state)
    assert state["watch_list"] == []                 # orphan dropped
    assert state["completed_count"] == 0             # not re-counted


@pytest.mark.parametrize("decision,expected,counts", [
    ("keep", "done_final", True),
    ("revert", "reverted", False),
    ("manual", "needs_human", False),
])
def test_regression_decision_resolves(isolated_home, decision, expected, counts):
    state, issue, w = _observing_state(num=42)
    _run_sweep_entry(state, issue, w, cls="failed")      # → regression_detected
    issue["regression_decision"] = decision              # Step −3 recorded the answer
    _run_sweep_entry(state, issue, w, cls="failed")      # Step −5 applies it
    orchestrator_state.write(state)
    assert issue["status"] == expected
    assert state["watch_list"] == []
    assert state["completed_count"] == (1 if counts else 0)
