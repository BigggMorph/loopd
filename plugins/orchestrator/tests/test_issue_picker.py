"""Unit tests for issue_picker.py (mocking gh CLI)."""

from __future__ import annotations

import datetime as _dt
import json
from unittest import mock

import pytest

import issue_picker


SAMPLE_ISSUES = [
    {
        "number": 1,
        "title": "Old issue",
        "labels": [{"name": "priority/medium"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-01-01T00:00:00Z",
        "updatedAt": "2025-01-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "alice"},
        "body": "...",
    },
    {
        "number": 2,
        "title": "Hot issue",
        "labels": [{"name": "priority/high"}, {"name": "bug"}],
        "reactions": {"totalCount": 10},
        "createdAt": "2025-03-01T00:00:00Z",
        "updatedAt": "2025-03-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "bob"},
        "body": "...",
    },
    {
        "number": 3,
        "title": "Scout suggested",
        "labels": [{"name": "scout-suggested"}, {"name": "priority/high"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-04-01T00:00:00Z",
        "updatedAt": "2025-04-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "bot"},
        "body": "...",
    },
    {
        "number": 4,
        "title": "Has human assignee",
        "labels": [{"name": "priority/high"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-05-01T00:00:00Z",
        "updatedAt": "2025-05-01T00:00:00Z",
        "assignees": [{"login": "carol"}],
        "author": {"login": "carol"},
        "body": "...",
    },
    {
        "number": 5,
        "title": "Split epic (already split)",
        "labels": [{"name": "split-epic"}, {"name": "priority/high"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-06-01T00:00:00Z",
        "updatedAt": "2025-06-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "bot"},
        # Rev 17 — marker is now the authoritative skip signal.
        "body": "...\n<!-- split-epic-marker -->\nchildren",
    },
    {
        "number": 6,
        "title": "Rejected",
        "labels": [{"name": "orchestrator-rejected"}, {"name": "priority/high"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-07-01T00:00:00Z",
        "updatedAt": "2025-07-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "bot"},
        "body": "...",
    },
]


@pytest.fixture
def gh_returns_samples():
    with mock.patch.object(
        issue_picker, "_list_open_issues", return_value=list(SAMPLE_ISSUES)
    ) as patched:
        yield patched


def _state_with_repo(local_issues=None):
    return {
        "repo": "owner/repo",
        "issues": local_issues or {},
        "last_picked_at": {},
    }


def test_pick_excludes_human_assigned(gh_returns_samples):
    picks = issue_picker.pick(_state_with_repo())
    nums = [p["number"] for p in picks]
    assert 4 not in nums  # carol assigned


def test_pick_excludes_split_epic(gh_returns_samples):
    picks = issue_picker.pick(_state_with_repo())
    nums = [p["number"] for p in picks]
    assert 5 not in nums


def test_pick_excludes_orchestrator_rejected(gh_returns_samples):
    picks = issue_picker.pick(_state_with_repo())
    nums = [p["number"] for p in picks]
    assert 6 not in nums


def test_pick_excludes_terminal_local_status(gh_returns_samples):
    local = {"2": {"number": 2, "status": "done"}}
    picks = issue_picker.pick(_state_with_repo(local))
    nums = [p["number"] for p in picks]
    assert 2 not in nums


def test_pick_scoring_human_priority_beats_scout(gh_returns_samples):
    picks = issue_picker.pick(_state_with_repo())
    # Issue 2 (human priority/high + 10 reactions) should beat issue 3 (scout priority/high)
    nums = [p["number"] for p in picks]
    assert nums.index(2) < nums.index(3)


def test_pick_returns_max_five(gh_returns_samples):
    picks = issue_picker.pick(_state_with_repo())
    assert len(picks) <= 5


def test_pick_dedup_by_last_picked_at(gh_returns_samples):
    state = _state_with_repo()
    # Mark issue 2 as picked 1 minute ago → should be excluded.
    state["last_picked_at"]["2"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=1)
    ).isoformat()
    picks = issue_picker.pick(state)
    nums = [p["number"] for p in picks]
    assert 2 not in nums


def test_pick_no_dedup_after_window(gh_returns_samples):
    state = _state_with_repo()
    state["last_picked_at"]["2"] = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=30)
    ).isoformat()
    picks = issue_picker.pick(state)
    nums = [p["number"] for p in picks]
    assert 2 in nums


def test_pick_empty_when_no_repo():
    assert issue_picker.pick({"repo": ""}) == []


def test_remember_pick_records_timestamp():
    state = {"last_picked_at": {}}
    issue_picker.remember_pick(state, 7)
    assert "7" in state["last_picked_at"]


# =====================================================================
# Rev 17 Phase 17-E — FORCE_SPLIT / Epic skip
# =====================================================================

def test_split_epic_marker_in_body_excludes(monkeypatch):
    issue = {
        "number": 99,
        "title": "Parent Epic (already split)",
        "labels": [],
        "body": "Some body\n\n<!-- split-epic-marker -->\n\nchildren...",
        "createdAt": "2026-05-22T10:00:00Z",
    }
    assert issue_picker._is_excluded(issue) is True


def test_split_epic_label_alone_does_not_exclude_anymore(monkeypatch):
    """Rev 17: split-epic label without marker stays pickable (Stage 1 newly registered)."""
    issue = {
        "number": 99,
        "title": "Planner Epic (unsplit)",
        "labels": [{"name": "planner-suggested"}, {"name": "split-epic"}],
        "body": "## User Story\n...",
        "createdAt": "2026-05-22T10:00:00Z",
    }
    assert issue_picker._is_excluded(issue) is False


def test_orchestrator_rejected_label_still_excludes():
    issue = {
        "number": 5,
        "title": "x",
        "labels": [{"name": "orchestrator-rejected"}],
        "body": "",
        "createdAt": "2026-05-22T10:00:00Z",
    }
    assert issue_picker._is_excluded(issue) is True


def test_is_orchestrator_authored_finds_audit_entry():
    state = {
        "audit_log": [
            {"action": "gh issue create", "target": "42", "argv": ["gh", "issue", "create", "..."]}
        ]
    }
    issue = {"number": 42, "labels": []}
    assert issue_picker.is_orchestrator_authored(issue, state) is True


def test_is_orchestrator_authored_matches_url_target():
    state = {
        "audit_log": [
            {
                "action": "gh issue create",
                "target": "https://github.com/x/y/issues/77",
                "argv": [],
            }
        ]
    }
    issue = {"number": 77, "labels": []}
    assert issue_picker.is_orchestrator_authored(issue, state) is True


def test_is_orchestrator_authored_false_when_no_entry():
    state = {"audit_log": []}
    issue = {"number": 11, "labels": []}
    assert issue_picker.is_orchestrator_authored(issue, state) is False


def test_is_orchestrator_authored_ignores_non_create_actions():
    state = {
        "audit_log": [
            {"action": "gh issue close", "target": "42", "argv": []}
        ]
    }
    issue = {"number": 42, "labels": []}
    assert issue_picker.is_orchestrator_authored(issue, state) is False


def test_needs_force_split_true_for_orchestrator_planner_epic():
    state = {
        "audit_log": [
            {"action": "gh issue create", "target": "42", "argv": []}
        ]
    }
    issue = {
        "number": 42,
        "labels": [
            {"name": "planner-suggested"},
            {"name": "split-epic"},
            {"name": "enhancement"},
        ],
        "body": "## User Story\n...",
    }
    assert issue_picker.needs_force_split(issue, state) is True


def test_needs_force_split_false_when_external_labels():
    """Round A S3 — external user can't coerce FORCE_SPLIT by adding labels."""
    state = {"audit_log": []}  # no orchestrator-create entry
    issue = {
        "number": 99,
        "labels": [{"name": "planner-suggested"}, {"name": "split-epic"}],
        "body": "## User Story\n...",
    }
    assert issue_picker.needs_force_split(issue, state) is False


def test_needs_force_split_false_when_already_split():
    state = {
        "audit_log": [{"action": "gh issue create", "target": "42", "argv": []}]
    }
    issue = {
        "number": 42,
        "labels": [{"name": "planner-suggested"}, {"name": "split-epic"}],
        "body": "## User Story\n\n<!-- split-epic-marker -->\nchildren\n",
    }
    assert issue_picker.needs_force_split(issue, state) is False


def test_needs_force_split_requires_both_labels():
    state = {
        "audit_log": [{"action": "gh issue create", "target": "42", "argv": []}]
    }
    issue = {
        "number": 42,
        "labels": [{"name": "planner-suggested"}],  # missing split-epic
        "body": "...",
    }
    assert issue_picker.needs_force_split(issue, state) is False
    issue["labels"] = [{"name": "split-epic"}]  # missing planner-suggested
    assert issue_picker.needs_force_split(issue, state) is False
