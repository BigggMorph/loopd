"""Unit tests for playbook_helpers.py"""

from __future__ import annotations

import pytest

import playbook_helpers as ph


# ---------- parse_json_tail ----------

def test_parse_json_tail_last_line():
    text = "some chatter\nmore chatter\n{\"phase\":\"analyze\",\"x\":1}"
    out = ph.parse_json_tail(text)
    assert out == {"phase": "analyze", "x": 1}


def test_parse_json_tail_handles_pre_text():
    text = "[issue-analyzer]: here is my answer\n\n{\"verdict\":\"pass\"}"
    out = ph.parse_json_tail(text)
    assert out == {"verdict": "pass"}


def test_parse_json_tail_finds_embedded_block():
    text = "Here's the result:\n{\"a\":1,\"b\":[1,2,3]}\n(end)"
    out = ph.parse_json_tail(text)
    assert out == {"a": 1, "b": [1, 2, 3]}


def test_parse_json_tail_handles_nested_braces():
    text = "{\"outer\":{\"inner\":42}}"
    assert ph.parse_json_tail(text) == {"outer": {"inner": 42}}


def test_parse_json_tail_returns_none_on_no_json():
    assert ph.parse_json_tail("no json here") is None
    assert ph.parse_json_tail("") is None


def test_parse_json_tail_handles_escaped_quotes():
    text = '{"msg":"she said \\"hello\\""}'
    out = ph.parse_json_tail(text)
    assert out == {"msg": 'she said "hello"'}


# ---------- extract_pr_url_from_text ----------

def test_extract_pr_url_no_repo_filter():
    text = "Created PR https://github.com/foo/bar/pull/42 ready for review"
    assert ph.extract_pr_url_from_text(text) == "https://github.com/foo/bar/pull/42"


def test_extract_pr_url_picks_last_match():
    text = (
        "earlier https://github.com/foo/bar/pull/1\n"
        "later https://github.com/foo/bar/pull/99"
    )
    assert ph.extract_pr_url_from_text(text) == "https://github.com/foo/bar/pull/99"


def test_extract_pr_url_repo_filter():
    text = (
        "https://github.com/wrong/repo/pull/1 "
        "https://github.com/foo/bar/pull/2"
    )
    assert ph.extract_pr_url_from_text(text, "foo/bar") == "https://github.com/foo/bar/pull/2"


def test_extract_pr_url_none_when_repo_mismatch():
    text = "https://github.com/wrong/repo/pull/1"
    assert ph.extract_pr_url_from_text(text, "foo/bar") is None


def test_extract_pr_url_empty():
    assert ph.extract_pr_url_from_text("") is None
    assert ph.extract_pr_url_from_text(None) is None  # type: ignore[arg-type]


# ---------- format_recent_history ----------

def test_format_recent_history_orders_newest_first():
    state = {
        "issues": {
            "1": {
                "number": 1,
                "title": "old issue",
                "status": "done",
                "history": [{"at": "2025-01-01T00:00:00+00:00", "from": "new", "to": "done"}],
            },
            "2": {
                "number": 2,
                "title": "newer issue",
                "status": "done",
                "history": [{"at": "2025-06-01T00:00:00+00:00", "from": "new", "to": "done"}],
            },
        }
    }
    out = ph.format_recent_history(state, n=10)
    assert "#2" in out
    assert "#1" in out
    assert out.index("#2") < out.index("#1")


def test_format_recent_history_empty():
    out = ph.format_recent_history({"issues": {}})
    assert "history empty" in out


# ---------- parse_selected_candidate_ids ----------

CANDIDATES = [
    {"id": "c1", "title": "first cand", "complexity_level": 1, "priority_hint": "medium"},
    {"id": "c2", "title": "second cand", "complexity_level": 2, "priority_hint": "high"},
]


def test_parse_selected_by_id_list():
    out = ph.parse_selected_candidate_ids(["c1", "c2"], CANDIDATES)
    assert out == ["c1", "c2"]


def test_parse_selected_by_title():
    out = ph.parse_selected_candidate_ids(["first cand"], CANDIDATES)
    assert out == ["c1"]


def test_parse_selected_by_full_label():
    out = ph.parse_selected_candidate_ids(["second cand (2, high)"], CANDIDATES)
    assert out == ["c2"]


def test_parse_selected_by_split_label():
    out = ph.parse_selected_candidate_ids(["second cand (level 2)"], CANDIDATES)
    assert out == ["c2"]


def test_parse_selected_by_dict_answer():
    out = ph.parse_selected_candidate_ids({"c1": True, "c2": False}, CANDIDATES)
    assert out == ["c1"]


def test_parse_selected_empty():
    assert ph.parse_selected_candidate_ids([], CANDIDATES) == []
    assert ph.parse_selected_candidate_ids(None, CANDIDATES) == []  # type: ignore[arg-type]


# ---------- find_path_intersections ----------

def test_find_path_intersections_basic():
    open_prs = [
        {"number": 5, "files": [{"path": "src/foo.py"}]},
        {"number": 7, "files": [{"path": "lib/bar.ts"}, {"path": "README.md"}]},
        {"number": 8, "files": [{"path": "tests/baz.py"}]},
    ]
    out = ph.find_path_intersections(open_prs, ["src/foo.py", "lib/bar.ts"])
    assert sorted(out) == [5, 7]


def test_find_path_intersections_no_touched_paths():
    assert ph.find_path_intersections([{"number": 1, "files": ["x"]}], []) == []


def test_find_path_intersections_accepts_string_files():
    open_prs = [{"number": 5, "files": ["src/foo.py"]}]
    assert ph.find_path_intersections(open_prs, ["src/foo.py"]) == [5]


# ---------- detect_lesson_pattern ----------

def test_detect_lesson_pattern_creates_new():
    state = {}
    entry = ph.detect_lesson_pattern(
        "dev-task 시작 후 5m내 loopd session 미생성", state
    )
    assert entry is not None
    assert entry["pattern"] == "/dev-task 시작 후 loopd session 파일 미생성"
    assert entry["observed_count"] == 1
    assert len(state["lessons_learned"]) == 1


def test_detect_lesson_pattern_increments_existing():
    state = {}
    ph.detect_lesson_pattern("tester 20분 무응답 + 1회 재시도 실패", state)
    ph.detect_lesson_pattern("tester 20분 무응답 + 1회 재시도 실패", state)
    assert state["lessons_learned"][0]["observed_count"] == 2


def test_detect_lesson_pattern_unmatched_returns_none():
    state = {}
    entry = ph.detect_lesson_pattern("some unique failure not in patterns", state)
    assert entry is None
    assert state.get("lessons_learned", []) == []


def test_detect_lesson_pattern_handles_none_reason():
    state = {}
    assert ph.detect_lesson_pattern("", state) is None
    assert ph.detect_lesson_pattern(None, state) is None  # type: ignore[arg-type]


# ---------- compose_daily_digest ----------

def test_compose_daily_digest_has_required_sections():
    state = {
        "completed_count": 3,
        "rejected_count": 1,
        "scout_history": [{"created_urls": ["a", "b"]}, {"created_urls": ["c"]}],
        "issues": {
            "1": {"status": "ready_for_dev", "number": 1},
            "2": {"status": "parked_awaiting_human", "number": 2},
            "3": {"status": "done", "number": 3},
        },
        "main_branch_red": True,
    }
    digest = ph.compose_daily_digest(state)
    assert "Daily digest" in digest
    assert "3" in digest  # completed_count
    assert "ready_for_dev" in digest  # in-flight
    assert "주의 필요" in digest  # attention section
    assert "main 브랜치 CI red" in digest


def test_compose_daily_digest_idle():
    digest = ph.compose_daily_digest({})
    assert "idle" in digest


# ---------- mark_as_epic_body ----------

def test_mark_as_epic_body_appends_when_no_marker():
    out = ph.mark_as_epic_body(
        "Original body.\n",
        ["https://github.com/x/y/issues/1", "https://github.com/x/y/issues/2"],
    )
    assert ph.SPLIT_EPIC_MARKER in out
    assert "Original body." in out
    assert "## Split into sub-issues (2개)" in out


def test_mark_as_epic_body_idempotent_when_marker_present():
    original = f"body\n{ph.SPLIT_EPIC_MARKER}\nold links"
    out = ph.mark_as_epic_body(original, ["new url"])
    assert out == original


def test_mark_as_epic_body_raises_on_empty_children():
    with pytest.raises(ValueError):
        ph.mark_as_epic_body("orig", [])


# ---------- clear_scout_fields ----------

def test_clear_scout_fields_resets_transient_state():
    state = {
        "scout_candidates": [{"id": "c1"}],
        "scout_decisions": {"c1": True},
        "scout_creating_done": ["c1"],
        "scout_question_emitted": True,
        "scout_message": "something",
        "scout_confirm_idx": 3,
        "scout_history": [{"keep": "me"}],  # not in the clear list
    }
    ph.clear_scout_fields(state)
    assert state["scout_candidates"] == []
    assert state["scout_decisions"] == {}
    assert state["scout_creating_done"] == []
    assert state["scout_question_emitted"] is False
    assert state["scout_message"] is None
    assert state["scout_confirm_idx"] == 0
    assert state["scout_history"] == [{"keep": "me"}]  # preserved


# =====================================================================
# Rev 17 — dedup_candidates / candidate_create_plan / clear_planner_fields
# =====================================================================

import os


def _set_env(monkeypatch, key, value):
    if value is None:
        monkeypatch.delenv(key, raising=False)
    else:
        monkeypatch.setenv(key, value)


def test_dedup_candidates_empty_returns_empty(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    out = ph.dedup_candidates([], [])
    assert out["merged"] == []
    assert out["drops"] == []


def test_dedup_candidates_no_overlap_keeps_both(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    scout = [{"id": "c1", "title": "Add caching to /api/users", "body": "tech"}]
    planner = [{"id": "p1", "title": "Onboarding flow Epic", "body": "user value"}]
    out = ph.dedup_candidates(scout, planner)
    titles = {c["title"] for c in out["merged"]}
    assert titles == {"Add caching to /api/users", "Onboarding flow Epic"}
    assert out["drops"] == []


def test_dedup_candidates_drops_scout_when_similar(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    scout = [{
        "id": "c1",
        "title": "Add onboarding wizard for new users",
        "body": "new user wizard",
    }]
    planner = [{
        "id": "p1",
        "title": "Add onboarding wizard for new users",
        "body": "Onboarding Epic",
    }]
    out = ph.dedup_candidates(scout, planner)
    # Only the planner one survives.
    sources = [c.get("source") for c in out["merged"]]
    assert sources == ["planner"]
    assert len(out["drops"]) == 1
    drop = out["drops"][0]
    assert drop["dropped_id"] == "c1"
    assert drop["kept_id"] == "p1"
    assert drop["dropped_source"] == "scout"
    assert drop["kept_source"] == "planner"


def test_dedup_candidates_preserves_source_tag(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    scout = [{"id": "c1", "title": "Bug fix in router", "body": "fix"}]
    planner = [{"id": "p1", "title": "Multi-tenant Epic", "body": "..."}]
    out = ph.dedup_candidates(scout, planner)
    by_id = {c["id"]: c for c in out["merged"]}
    assert by_id["c1"]["source"] == "scout"
    assert by_id["p1"]["source"] == "planner"


def test_dedup_candidates_method_explicit_sentence_raises_when_missing(monkeypatch):
    """If method='sentence' and sentence-transformers is unavailable, ImportError."""
    monkeypatch.delenv("ORCHESTRATOR_DEDUP_METHOD", raising=False)
    # Force the cache to None so the lookup runs.
    ph._SBERT_CACHE["model"] = None
    scout = [{"id": "c1", "title": "A", "body": "x"}]
    planner = [{"id": "p1", "title": "A", "body": "y"}]
    with pytest.raises(ImportError):
        ph.dedup_candidates(scout, planner, method="sentence")


def test_dedup_candidates_auto_falls_back_when_sbert_missing(monkeypatch):
    """method='auto' should silently fall back to SequenceMatcher."""
    monkeypatch.delenv("ORCHESTRATOR_DEDUP_METHOD", raising=False)
    ph._SBERT_CACHE["model"] = None
    scout = [{"id": "c1", "title": "Add a wizard", "body": "x"}]
    planner = [{"id": "p1", "title": "Add a wizard", "body": "x"}]
    out = ph.dedup_candidates(scout, planner, method="auto")
    assert out["method_used"] == "auto"
    assert len(out["drops"]) == 1


def test_dedup_candidates_handles_missing_id_field(monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_DEDUP_METHOD", "sequence_matcher")
    scout = [{"title": "X", "body": "y"}]  # no id
    planner = [{"id": "p1", "title": "X", "body": "y"}]
    out = ph.dedup_candidates(scout, planner)
    # Without id, the dedup loop can't dedup — the no-id entry survives.
    # That's acceptable: ids are required for tracking.
    assert any(c.get("title") == "X" for c in out["merged"])


# ---------- candidate_create_plan ----------

def test_candidate_create_plan_uses_default_prefix():
    cand = {"id": "c1", "title": "A title", "body": "body", "labels": ["bug"]}
    plan = ph.candidate_create_plan(cand)
    assert plan["fingerprint_label"].startswith("scout-fp-")
    assert "scout-fp-" in plan["labels"][-1]
    assert "bug" in plan["labels"]


def test_candidate_create_plan_planner_prefix_and_extra_labels():
    cand = {
        "id": "p1",
        "title": "Epic title",
        "body": "## User Story\n...",
        "labels": ["enhancement"],
        "source": "planner",
    }
    plan = ph.candidate_create_plan(
        cand,
        fp_prefix="planner-fp-",
        extra_labels=["planner-suggested", "split-epic"],
    )
    assert plan["fingerprint_label"].startswith("planner-fp-")
    assert "planner-suggested" in plan["labels"]
    assert "split-epic" in plan["labels"]
    assert plan["labels"].count("planner-suggested") == 1  # deduped


def test_candidate_create_plan_sanitizes_title_and_body():
    cand = {
        "id": "c1",
        "title": "Title​with zero-width",
        "body": "<script>alert(1)</script>\nclean text",
        "labels": [],
    }
    plan = ph.candidate_create_plan(cand)
    assert "​" not in plan["title"]
    assert "<script>" not in plan["body"]


def test_candidate_create_plan_returns_source_default():
    plan = ph.candidate_create_plan({"id": "c1", "title": "t", "body": "b", "labels": []})
    assert plan["source"] == "scout"


def test_candidate_create_plan_stable_fingerprint():
    cand = {"id": "c1", "title": "Same", "body": "Same"}
    a = ph.candidate_create_plan(cand)
    b = ph.candidate_create_plan(cand)
    assert a["fingerprint_label"] == b["fingerprint_label"]


# ---------- clear_planner_fields ----------

def test_clear_planner_fields_resets_transients_but_keeps_history():
    state = {
        "planner_candidates": [{"id": "p1"}],
        "planner_candidates_buffer": [{"id": "p1"}],
        "planner_decisions": {"p1": True},
        "planner_creating_done": ["p1"],
        "planner_creating_lock_started_at": "ts",
        "planner_creating_lock_owner": "owner",
        "planner_confirm_idx": 3,
        "planner_created_urls": ["url1"],
        "planner_failed_creations": [{"id": "p2", "error": "..."}],
        "planner_message": "msg",
        "planning_retried": True,
        "planner_history": [{"keep": "me"}],
    }
    ph.clear_planner_fields(state)
    assert state["planner_candidates"] == []
    assert state["planner_decisions"] == {}
    assert state["planner_creating_done"] == []
    assert state["planner_creating_lock_started_at"] is None
    assert state["planner_confirm_idx"] == 0
    assert state["planner_created_urls"] == []
    assert state["planner_message"] is None
    assert state["planning_retried"] is False
    assert state["planner_history"] == [{"keep": "me"}]


# =====================================================================
# Rev 17 Phase 17-D — vision-critic helpers
# =====================================================================

def test_vision_delta_violates_guard_detects_human_deletion():
    delta = {
        "before": "User vision: every change requires human confirm + audit log.",
        "after": "User vision: changes ship automatically.",
    }
    removed = ph.vision_delta_violates_guard(delta)
    assert "human" in removed
    assert "confirm" in removed
    assert "audit" in removed


def test_vision_delta_violates_guard_korean():
    delta = {
        "before": "비전: 사람 승인 후 적용.",
        "after": "비전: 자동 적용.",
    }
    removed = ph.vision_delta_violates_guard(delta)
    assert "사람" in removed
    assert "승인" in removed


def test_vision_delta_violates_guard_safe_delta():
    delta = {
        "before": "vision: build AI assistant",
        "after": "vision: build AI assistant for developers",
    }
    assert ph.vision_delta_violates_guard(delta) == []


def test_vision_delta_violates_guard_handles_empty():
    assert ph.vision_delta_violates_guard({}) == []


def test_record_rejected_delta_appends_new():
    state = {"rejected_delta_hashes": []}
    entry = ph.record_rejected_delta(state, "before text", "after text")
    assert len(state["rejected_delta_hashes"]) == 1
    assert entry["rejection_count"] == 1
    assert entry["before_norm"] == "before text"
    assert entry["after_norm"] == "after text"


def test_record_rejected_delta_increments_on_exact_match():
    state = {"rejected_delta_hashes": []}
    ph.record_rejected_delta(state, "before text", "after text")
    ph.record_rejected_delta(state, "before text", "after text")
    assert len(state["rejected_delta_hashes"]) == 1
    assert state["rejected_delta_hashes"][0]["rejection_count"] == 2


def test_record_rejected_delta_increments_on_fuzzy_before_match():
    state = {"rejected_delta_hashes": []}
    ph.record_rejected_delta(
        state,
        "vision is to deliver value to users every day",
        "vision is to deliver value automatically",
    )
    # Slight cosmetic change to before — should still match via SequenceMatcher.
    ph.record_rejected_delta(
        state,
        "Vision is to Deliver Value to Users every day!",
        "vision is to deliver value automatically",
    )
    # The fuzzy match should fold both into a single entry.
    assert len(state["rejected_delta_hashes"]) == 1
    assert state["rejected_delta_hashes"][0]["rejection_count"] == 2


def test_find_rejected_delta_returns_none_when_no_history():
    state = {"rejected_delta_hashes": []}
    assert ph.find_rejected_delta_match(state, "a", "b") is None


def test_count_same_before_rejections():
    state = {"rejected_delta_hashes": []}
    ph.record_rejected_delta(state, "same before", "after1")
    state["rejected_delta_hashes"].append({
        "hash": "h2",
        "before_norm": "same before",
        "after_norm": "after2",
        "rejected_at": "2026-05-22T10:00:00+00:00",
        "last_seen_at": "2026-05-22T10:00:00+00:00",
        "rejection_count": 1,
    })
    state["rejected_delta_hashes"].append({
        "hash": "h3",
        "before_norm": "different before",
        "after_norm": "after3",
        "rejected_at": "2026-05-22T10:00:00+00:00",
        "last_seen_at": "2026-05-22T10:00:00+00:00",
        "rejection_count": 1,
    })
    assert ph.count_same_before_rejections(state, "same before") == 2


def test_expire_rejected_deltas_drops_old_entries():
    state = {
        "rejected_delta_hashes": [
            {
                "hash": "h1",
                "before_norm": "x",
                "after_norm": "y",
                "rejected_at": "2024-01-01T00:00:00+00:00",
                "last_seen_at": "2024-01-01T00:00:00+00:00",
                "rejection_count": 1,
            },
            {
                "hash": "h2",
                "before_norm": "a",
                "after_norm": "b",
                "rejected_at": "9999-01-01T00:00:00+00:00",
                "last_seen_at": "9999-01-01T00:00:00+00:00",
                "rejection_count": 1,
            },
        ]
    }
    dropped = ph.expire_rejected_deltas(state)
    assert dropped == 1
    assert len(state["rejected_delta_hashes"]) == 1
    assert state["rejected_delta_hashes"][0]["hash"] == "h2"


def test_vision_alignment_skip_above_threshold():
    assert ph.vision_alignment_skip(0.85) is True
    assert ph.vision_alignment_skip(0.9) is True


def test_vision_alignment_skip_below_threshold():
    assert ph.vision_alignment_skip(0.8) is False
    assert ph.vision_alignment_skip(0.6) is False
    assert ph.vision_alignment_skip(0.0) is False


def test_vision_alignment_skip_handles_none():
    assert ph.vision_alignment_skip(None) is False


def test_two_call_confirm_required_first_call():
    state = {"vision_critic_history": []}
    delta = {"before": "X", "after": "Y", "rationale": "r"}
    assert ph.two_call_confirm_required(state, delta) is True


def test_two_call_confirm_required_second_matching_call():
    state = {
        "vision_critic_history": [
            {
                "ts": "2026-05-22T10:00:00+00:00",
                "source": "vision_critic",
                "before": "X is the vision",
                "after": "Y is the vision",
                "rationale": "r",
                "user_action": "pending_second_confirm",
                "alignment_score": 0.5,
            }
        ]
    }
    delta = {"before": "X is the vision", "after": "Y is the vision"}
    assert ph.two_call_confirm_required(state, delta) is False


def test_two_call_confirm_required_unmatching_pending():
    state = {
        "vision_critic_history": [
            {
                "ts": "2026-05-22T10:00:00+00:00",
                "source": "vision_critic",
                "before": "totally different vision",
                "after": "even more different",
                "rationale": "r",
                "user_action": "pending_second_confirm",
                "alignment_score": 0.5,
            }
        ]
    }
    delta = {"before": "X is the vision", "after": "Y is the vision"}
    assert ph.two_call_confirm_required(state, delta) is True


def test_vision_critic_due_offset_12_period_25():
    state = {"last_vision_critic_cycle": 0}
    # First trigger is 37 (offset 12, next phase-aligned cycle > 12).
    assert ph.vision_critic_due(state, 37) is True
    assert ph.vision_critic_due(state, 62) is True
    # Wrong phase
    assert ph.vision_critic_due(state, 25) is False
    assert ph.vision_critic_due(state, 50) is False


def test_vision_critic_due_idempotent():
    state = {"last_vision_critic_cycle": 37}
    assert ph.vision_critic_due(state, 37) is False


def test_vision_critic_due_below_or_at_offset():
    state = {"last_vision_critic_cycle": 0}
    assert ph.vision_critic_due(state, 11) is False
    # cycle == offset is excluded per design ("total > 12").
    assert ph.vision_critic_due(state, 12) is False
    assert ph.vision_critic_due(state, 37) is True
