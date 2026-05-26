"""Feature 1 — system-doctor stall classifier + auto-resume guards."""

from __future__ import annotations

import datetime as _dt

import pytest

import doctor
import orchestrator_state as st


def _now():
    return _dt.datetime.now(_dt.timezone.utc)


def _state_with_issue(num=42, status="needs_human", failure_reason="", **issue_extra):
    state = st.read()
    state["current_issue"] = num
    issue = {"number": num, "status": status, "history": [], "failure_reason": failure_reason}
    issue.update(issue_extra)
    state["issues"][str(num)] = issue
    return state


FRESH = ("fresh", None)


# --- failure_signature ----------------------------------------------------

def test_failure_signature_collapses_cosmetic_wording(isolated_home):
    a = doctor.failure_signature("analyzer 10분 무응답 + 1회 재시도 실패", "needs_human")
    b = doctor.failure_signature("analyzer 10분 무응답 — 재시도 실패함", "needs_human")
    assert a == b  # both map to the "analyzer 무응답" canonical
    assert len(a) == 16


def test_failure_signature_differs_by_status(isolated_home):
    a = doctor.failure_signature("PR URL 추출 실패", "needs_human")
    b = doctor.failure_signature("PR URL 추출 실패", "parked_awaiting_human")
    assert a != b


# --- classify_stall: gating ----------------------------------------------

def test_non_fresh_wake_is_never_a_stall(isolated_home):
    state = _state_with_issue(status="needs_human", failure_reason="PR URL 추출 실패")
    verdict, _ = doctor.classify_stall(state, ("teammate_reply", "tester"))
    assert verdict == "none"


def test_active_cycle_is_never_a_stall(isolated_home):
    state = _state_with_issue()
    state["scout_status"] = "scout_pending"
    assert doctor.classify_stall(state, FRESH)[0] == "none"


def test_healthy_dev_running_with_live_session_is_none(isolated_home):
    sid = "live-sess"
    (isolated_home / ".loopd" / "sessions" / f"{sid}.json").write_text("{}")
    state = _state_with_issue(status="dev_running")
    state["dev_session_id"] = sid
    state["dev_started_at"] = (_now() - _dt.timedelta(minutes=10)).isoformat()
    assert doctor.classify_stall(state, FRESH)[0] == "none"


# --- classify_stall: transient -------------------------------------------

def test_lost_dev_done_is_transient(isolated_home):
    state = _state_with_issue(status="dev_running")
    state["dev_session_id"] = "gone-sess"  # no session file → dev ended
    state["dev_done_injected"] = True
    state["dev_started_at"] = (_now() - _dt.timedelta(minutes=5)).isoformat()
    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "transient"
    assert payload["action"] == "process_dev_done"


def test_analyzer_no_response_first_time_is_transient(isolated_home):
    state = _state_with_issue(
        status="parked_awaiting_human",
        failure_reason="analyzer 10분 무응답 + 1회 재시도 실패",
        history=[{"from": "analyze_pending", "to": "parked_awaiting_human"}],
    )
    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "transient"
    assert payload["action"] == "restore_parked"


def test_transient_cooldown_is_noop_then_escalates(isolated_home):
    state = _state_with_issue(
        status="needs_human",
        failure_reason="tester 20분 무응답",
        history=[{"from": "test_pending", "to": "needs_human"}],
    )
    sig = doctor.failure_signature("tester 20분 무응답", "needs_human")

    # First classify → transient; record an attempt.
    assert doctor.classify_stall(state, FRESH)[0] == "transient"
    doctor.record_resume_attempt(state, sig)
    # Immediately after → within cooldown → none.
    assert doctor.classify_stall(state, FRESH)[0] == "none"
    # Force the cap: another attempt + age the cooldown out.
    doctor.record_resume_attempt(state, sig)
    state["last_resume_at"] = (_now() - _dt.timedelta(hours=1)).isoformat()
    assert doctor.classify_stall(state, FRESH)[0] == "structural"


# --- classify_stall: structural ------------------------------------------

def test_structural_canonical_below_threshold_is_human(isolated_home):
    state = _state_with_issue(status="needs_human", failure_reason="PR URL 추출 실패")
    # observed_count not seeded (0) → below threshold → leave for human.
    assert doctor.classify_stall(state, FRESH)[0] == "human"


def test_structural_canonical_at_threshold_dispatches_doctor(isolated_home):
    state = _state_with_issue(status="needs_human", failure_reason="PR URL 추출 실패")
    state["lessons_learned"] = [{"pattern": "PR URL 추출 실패", "observed_count": 3}]
    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "structural"
    assert payload["failure_signature"] == doctor.failure_signature("PR URL 추출 실패", "needs_human")


def test_stale_dev_session_state_is_structural(isolated_home):
    state = _state_with_issue(
        status="parked_awaiting_human",
        failure_reason="dev_running 인데 dev_session_id is None — stale state",
    )
    assert doctor.classify_stall(state, FRESH)[0] == "structural"


# --- classify_stall: human -----------------------------------------------

def test_dev_rework_twice_is_human(isolated_home):
    state = _state_with_issue(
        status="needs_human", failure_reason="dev rework 2회 후에도 tester 거부"
    )
    assert doctor.classify_stall(state, FRESH)[0] == "human"


def test_unknown_reason_is_human(isolated_home):
    state = _state_with_issue(status="needs_human", failure_reason="뭔가 알 수 없는 이유")
    assert doctor.classify_stall(state, FRESH)[0] == "human"


def test_no_current_issue_is_none(isolated_home):
    state = st.read()
    assert doctor.classify_stall(state, FRESH)[0] == "none"


# --- restore_parked_issue -------------------------------------------------

def test_restore_parked_issue_restores_prior_active_status(isolated_home):
    state = _state_with_issue(
        status="parked_awaiting_human",
        failure_reason="analyzer 무응답",
        history=[
            {"from": "new", "to": "analyze_pending"},
            {"from": "analyze_pending", "to": "parked_awaiting_human"},
        ],
        analyze_pending_started_at="2020-01-01T00:00:00+00:00",
        analyzer_retried=True,
    )
    assert doctor.restore_parked_issue(state, 42) is True
    issue = state["issues"]["42"]
    assert issue["status"] == "analyze_pending"
    assert "analyze_pending_started_at" not in issue
    assert "analyzer_retried" not in issue
    assert state["current_issue"] == 42


def test_restore_parked_issue_missing_returns_false(isolated_home):
    state = st.read()
    assert doctor.restore_parked_issue(state, 999) is False


# --- runaway guards -------------------------------------------------------

def test_daily_cap_and_prune(isolated_home):
    state = st.read()
    now = _now()
    # 2 recent + 1 stale (>24h).
    state["doctor_issues_today"] = [
        now.isoformat(),
        (now - _dt.timedelta(hours=1)).isoformat(),
        (now - _dt.timedelta(hours=30)).isoformat(),
    ]
    assert doctor.doctor_daily_cap_reached(state) is False  # stale pruned → 2 < 3
    assert len(state["doctor_issues_today"]) == 2
    state["doctor_issues_today"].append(now.isoformat())
    assert doctor.doctor_daily_cap_reached(state) is True  # now 3


def test_doctor_signature_active_window(isolated_home):
    state = st.read()
    sig = "abc123"
    assert doctor.doctor_signature_active(state, sig) is False
    doctor.record_doctor_signature(state, sig, fix_issue_url="http://x/1")
    assert doctor.doctor_signature_active(state, sig) is True
    # Age it out of the window.
    state["doctor_signatures_seen"][sig]["last_at"] = (
        _now() - _dt.timedelta(hours=30)
    ).isoformat()
    assert doctor.doctor_signature_active(state, sig) is False


# --- loopd-untouchable target guard --------------------------------------

@pytest.mark.parametrize(
    "targets,ok",
    [
        (["plugins/orchestrator/python_helpers/doctor.py"], True),
        (["plugins/orchestrator/skills/orchestrator/SKILL.md", "plugins/orchestrator/x.py"], True),
        (["./plugins/orchestrator/foo.py"], True),
        (["plugins/loopd/tick.py"], False),       # loopd is frozen
        (["plugins/orchestrator/x.py", "plugins/loopd/y.py"], False),  # any escape fails all
        (["src/app.py"], False),                  # outside the plugin
        ([], False),                              # must name a file
        ("plugins/orchestrator/x.py", False),     # not a list
    ],
)
def test_targets_are_safe(isolated_home, targets, ok):
    assert doctor.targets_are_safe(targets) is ok


# --- end-to-end: structural stall → fileable self-fix --------------------

def test_e2e_structural_stall_produces_self_modify_fix(isolated_home):
    import playbook_helpers as ph
    import safety

    # A systemic PR-URL-extraction bug recurred across issues (count >= 3).
    state = _state_with_issue(status="needs_human", failure_reason="PR URL 추출 실패")
    state["lessons_learned"] = [{"pattern": "PR URL 추출 실패", "observed_count": 3}]

    verdict, payload = doctor.classify_stall(state, FRESH)
    assert verdict == "structural"
    sig = payload["failure_signature"]

    # Lead enters the doctor cycle.
    st.doctor_transition(state, "doctor_pending")
    st.doctor_transition(state, "doctor_received")

    # A valid doctor diagnosis targeting orchestrator code.
    diagnosis = {
        "target_files": ["plugins/orchestrator/skills/orchestrator/SKILL.md"],
        "confidence": 0.8,
    }
    assert doctor.targets_are_safe(diagnosis["target_files"]) is True

    # The filed fix issue gets the self-modify labels.
    plan = ph.candidate_create_plan(
        {"id": sig, "title": "Fix PR URL extraction", "body": "## Problem\n...\n## Acceptance Criteria\n- [ ] handle missing URL"},
        fp_prefix="doctor-fp-",
        extra_labels=["self-modify", "infrastructure", "orchestrator-managed"],
    )
    assert plan["fingerprint_label"].startswith("doctor-fp-")
    assert {"self-modify", "infrastructure", "orchestrator-managed"} <= set(plan["labels"])

    # would_self_modify fires → the fix can NEVER auto-merge.
    issue = {"labels": [{"name": l} for l in plan["labels"]], "title": plan["title"], "body": plan["body"]}
    assert safety.would_self_modify(issue, state) is True

    # Record + file bookkeeping, then complete the cycle.
    doctor.record_doctor_signature(state, sig, fix_issue_url="https://github.com/o/r/issues/9")
    state["doctor_issues_today"].append(_now().isoformat())
    state.setdefault("doctor_history", []).append(
        {"ts": _now().isoformat(), "severity": "high", "fix_issue_url": "https://github.com/o/r/issues/9", "user_action": "filed"}
    )
    st.doctor_transition(state, "doctor_filing")
    st.doctor_transition(state, "doctor_done")
    ph.clear_doctor_fields(state)
    st.doctor_transition(state, None)

    # History/audit preserved; transient fields cleared.
    assert state["doctor_status"] is None
    assert len(state["doctor_history"]) == 1
    assert state["last_doctor_signature"] is None
    assert sig in state["doctor_signatures_seen"]
    # A re-detection now suppresses (fix in flight).
    assert doctor.doctor_signature_active(state, sig) is True


def test_e2e_doctor_cycle_persists_round_trip(isolated_home):
    st.set_active_instance("o/r")
    state = st.read()
    state["doctor_status"] = "doctor_pending"
    state["last_doctor_signature"] = "deadbeef"
    state["doctor_message"] = {"failure_signature": "deadbeef", "num": 5}
    st.write(state)
    # Reload (fresh process would do the same).
    again = st.read()
    assert again["doctor_status"] == "doctor_pending"
    assert again["doctor_message"]["num"] == 5
