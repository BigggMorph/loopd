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
