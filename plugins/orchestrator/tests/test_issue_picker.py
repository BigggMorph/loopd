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
        "title": "Split epic",
        "labels": [{"name": "split-epic"}, {"name": "priority/high"}],
        "reactions": {"totalCount": 0},
        "createdAt": "2025-06-01T00:00:00Z",
        "updatedAt": "2025-06-01T00:00:00Z",
        "assignees": [],
        "author": {"login": "bot"},
        "body": "...",
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
