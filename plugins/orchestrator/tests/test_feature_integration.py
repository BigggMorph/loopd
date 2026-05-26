"""Integration / intent-verification tests for the three Rev-18 features.

These close the gap left by pure unit tests:

- Feature 2 (response language): the runtime behavior is prompt-driven (the LLM
  follows SKILL.md), which can't be exercised in Python — but the *enforcement
  points* (state field, lead directive, per-agent LANG rule, command doc) are
  structural invariants we CAN lock down so a refactor can't silently drop them.
- Feature 1 (self-healing): `/loop` firing is a harness concern, but the
  recovery logic that runs *when* a fresh wake arrives is fully simulatable —
  we drive the whole Step −6 → classify → act sequence in Python.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import doctor
import orchestrator_state as st
import playbook_helpers as ph
import safety

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
AGENTS = PLUGIN_ROOT / "agents"
SKILL = PLUGIN_ROOT / "skills" / "orchestrator" / "SKILL.md"
COMMAND = PLUGIN_ROOT / "commands" / "orchestrator.md"

ALL_AGENTS = [
    "issue-analyzer", "tester", "issue-scout",
    "product-planner", "roadmap-strategist", "vision-critic",
    "system-doctor",
]


# ======================================================================
# Feature 2 — response language enforcement points
# ======================================================================

def test_state_has_response_language_default_ko():
    assert st._empty_state()["response_language"] == "ko"


def test_skill_has_top_level_language_directive():
    text = SKILL.read_text()
    assert "response_language" in text
    # The Hard rule must tie user-facing output to the configured language.
    assert "user-facing output" in text.lower()
    # And the lead must inject LANG into teammate messages.
    assert "LANG=" in text


def test_skill_parses_lang_arg():
    text = SKILL.read_text()
    assert "lang:ko" in text and "lang:en" in text


def test_command_documents_lang_arg():
    text = COMMAND.read_text()
    assert "lang:ko" in text  # argument-hint
    assert "language" in text.lower()


@pytest.mark.parametrize("agent", ALL_AGENTS)
def test_every_agent_has_lang_rule(agent):
    text = (AGENTS / f"{agent}.md").read_text()
    # Each agent must honor the LANG tag for its human-readable fields.
    assert "LANG=" in text, f"{agent} missing LANG instruction"
    assert "language" in text.lower()


# ======================================================================
# Feature 1 — full self-heal loop simulation (what /loop triggers)
# ======================================================================

FRESH = ("fresh", None)


def _seed(status, reason="", **extra):
    st.clear_active_instance()
    st.set_active_instance("acme/heal")
    state = st.read()
    state["current_issue"] = 7
    issue = {"number": 7, "status": status, "history": extra.pop("history", []),
             "failure_reason": reason}
    issue.update(extra)
    state["issues"]["7"] = issue
    st.write(state)
    return st.read()


def _apply_transient(state, payload):
    """Mirror SKILL.md Step −6 transient branch in Python."""
    doctor.record_resume_attempt(state, payload["signature"])
    if payload["action"] == "restore_parked":
        doctor.restore_parked_issue(state, payload["num"])
    elif payload["action"] == "process_dev_done":
        st.transition(state["issues"][str(payload["num"])], "dev_done")
    st.write(state)


def test_loop_tick_auto_resumes_transient_stall_end_to_end():
    # A fresh /loop tick lands on an analyzer-timeout-parked issue.
    state = _seed(
        "parked_awaiting_human",
        "analyzer 10분 무응답 + 1회 재시도 실패",
        history=[{"from": "new", "to": "analyze_pending"},
                 {"from": "analyze_pending", "to": "parked_awaiting_human"}],
        analyze_pending_started_at="2020-01-01T00:00:00+00:00",
        analyzer_retried=True,
    )
    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "transient" and payload["action"] == "restore_parked"

    _apply_transient(state, payload)
    reloaded = st.read()
    # Issue is back on an active status, ready to re-dispatch; timers cleared.
    assert reloaded["issues"]["7"]["status"] == "analyze_pending"
    assert "analyzer_retried" not in reloaded["issues"]["7"]
    assert reloaded["current_issue"] == 7

    # A second immediate tick must NOT double-resume (cooldown).
    assert doctor.classify_stall(reloaded, FRESH)[0] in ("none",)


def test_loop_tick_dispatches_doctor_for_recurring_structural_bug():
    # Same systemic bug seen across 3 issues → structural → doctor cycle.
    state = _seed("needs_human", "PR URL 추출 실패")
    state["lessons_learned"] = [{"pattern": "PR URL 추출 실패", "observed_count": 3}]

    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "structural"
    sig = payload["failure_signature"]

    # Step −6 hands off; runaway guards permit (fresh signature, under cap).
    doctor.prune_doctor_issues_today(state)
    assert doctor.doctor_daily_cap_reached(state) is False
    assert doctor.doctor_signature_active(state, sig) is False
    st.doctor_transition(state, "doctor_pending")

    # Doctor replies with a valid orchestrator-only diagnosis.
    diagnosis = {
        "status": "complete",
        "target_files": ["plugins/orchestrator/python_helpers/playbook_helpers.py"],
        "confidence": 0.82,
        "root_cause": "extract_pr_url regex misses squash-merge URLs",
        "fix_issue_body": "## Problem\n...\n## Acceptance Criteria\n- [ ] handle squash URL",
    }
    assert doctor.targets_are_safe(diagnosis["target_files"]) is True
    assert safety.would_loosen_safety(
        {"title": diagnosis["root_cause"], "body": diagnosis["fix_issue_body"]}
    ) is False
    assert diagnosis["confidence"] >= 0.5
    st.doctor_transition(state, "doctor_received")

    # File the fix issue (the plan the lead would create + label).
    plan = ph.candidate_create_plan(
        {"id": sig, "title": diagnosis["root_cause"], "body": diagnosis["fix_issue_body"]},
        fp_prefix="doctor-fp-",
        extra_labels=["self-modify", "infrastructure", "orchestrator-managed"],
    )
    issue = {"labels": [{"name": l} for l in plan["labels"]],
             "title": plan["title"], "body": plan["body"]}
    # The filed fix can NEVER auto-merge (self-modify gate).
    assert safety.would_self_modify(issue, state) is True

    doctor.record_doctor_signature(state, sig, fix_issue_url="https://github.com/acme/heal/issues/12")
    st.doctor_transition(state, "doctor_filing")
    st.doctor_transition(state, "doctor_done")
    ph.clear_doctor_fields(state)
    st.doctor_transition(state, None)
    st.write(state)

    # Cycle returns to idle; re-detection is suppressed (fix in flight).
    reloaded = st.read()
    assert reloaded["doctor_status"] is None
    assert doctor.doctor_signature_active(reloaded, sig) is True


def test_loop_tick_leaves_genuine_human_wait_untouched():
    # An issue legitimately awaiting the user (merge confirm) must not be
    # auto-touched by stall detection.
    state = _seed("merge_pending")
    assert doctor.classify_stall(state, FRESH)[0] == "none"
    # And a 2x-rework rejection is a real "this change is hard" outcome.
    state2 = _seed("needs_human", "dev rework 2회 후에도 tester 거부")
    assert doctor.classify_stall(state2, FRESH)[0] == "human"


def test_user_stopped_issue_is_not_auto_resumed():
    # After stop:true the issue parks with this reason; never auto-resume it.
    state = _seed("parked_awaiting_human", "stop:true while dev pipeline running")
    assert doctor.classify_stall(state, FRESH)[0] == "human"
