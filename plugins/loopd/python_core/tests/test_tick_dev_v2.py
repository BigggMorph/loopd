"""dev_v2 pipeline routing tests.

dev_v2 replaces the deterministic 5-phase pipeline with a single developer
agent that holds the workflow as a playbook and declares its judgment in a
structured exit report. tick only honours that judgment and closes the
review loop. These tests exercise the routing table:

    (no turns)                          → developer
    developer + review:"skipped"        → complete
    developer + review:"requested"      → review
    developer + unparseable report      → review   (conservative)
    developer + status:"failed"         → checkpoint_human
    review approve                      → complete
    review request_changes              → developer (with feedback in prompt)
    developer turns >= backstop         → checkpoint_human
"""

from __future__ import annotations

import json

from loopd_core.tick import (
    _DEV_V2_MAX_DEVELOPER_TURNS,
    _build_next_action,
    _next_agent_dev_v2,
    _truncate_keep_tail,
    parse_dev_task_args,
    parse_exit_report,
)


def _turn(subagent: str, result: str, state: str = "completed") -> dict:
    return {"subagent": subagent, "state": state, "result": result}


def _report(**overrides) -> str:
    base = {
        "phase": "developer",
        "status": "complete",
        "pr_url": "https://github.com/o/r/pull/1",
        "tests": {"command": "pytest", "result": "passed", "summary": "3 passed"},
        "review": "skipped",
        "skipped": [],
        "decisions": [],
        "summary": "done",
    }
    base.update(overrides)
    return json.dumps(base)


def _task(turns: list[dict], **extra) -> dict:
    return {
        "id": "task-t",
        "status": "active",
        "task_type": "dev_v2",
        "prompt": "do the thing",
        "workspace": {"repo": "o/r", "branch": "main"},
        "turns": turns,
        **extra,
    }


# ── arg parsing ──────────────────────────────────────────────────────────────


def test_parse_dev_task_args_pipeline_flag():
    parsed = parse_dev_task_args('"fix it" repo:o/r pipeline:v2')
    assert parsed["pipeline"] == "v2"
    assert parsed["prompt"] == "fix it"


def test_parse_dev_task_args_pipeline_defaults_v1():
    parsed = parse_dev_task_args('"fix it" repo:o/r')
    assert parsed["pipeline"] == "v1"


# ── exit report parser ───────────────────────────────────────────────────────


def test_parse_exit_report_last_line():
    result = "I did stuff.\n\n" + _report()
    report = parse_exit_report(result)
    assert report is not None and report["status"] == "complete"


def test_parse_exit_report_tolerates_code_fence():
    result = "prose\n```json\n" + _report() + "\n```"
    report = parse_exit_report(result)
    assert report is not None and report["phase"] == "developer"


def test_parse_exit_report_ignores_non_report_json():
    result = '{"unrelated": true}\n' + _report() + "\ntrailing prose"
    report = parse_exit_report(result)
    assert report is not None and report["phase"] == "developer"


def test_parse_exit_report_missing():
    assert parse_exit_report("no json here") is None
    assert parse_exit_report("") is None


# ── routing ──────────────────────────────────────────────────────────────────


def test_fresh_task_routes_to_developer():
    assert _next_agent_dev_v2(_task([])) == "developer"


def test_review_skipped_completes():
    task = _task([_turn("developer", _report(review="skipped"))])
    assert _next_agent_dev_v2(task) is None
    action = _build_next_action(task, None)
    assert action["kind"] == "complete"


def test_review_requested_routes_to_review():
    task = _task([_turn("developer", _report(review="requested"))])
    assert _next_agent_dev_v2(task) == "review"


def test_unparseable_report_routes_to_review():
    task = _task([_turn("developer", "I think I'm done, no JSON though")])
    assert _next_agent_dev_v2(task) == "review"


def test_failed_report_checkpoints_human():
    task = _task([
        _turn("developer", _report(status="failed", error="cannot reach origin"))
    ])
    assert _next_agent_dev_v2(task) is None
    action = _build_next_action(task, None)
    assert action["kind"] == "checkpoint_human"
    assert "cannot reach origin" in action["question"]


def test_review_approve_completes():
    task = _task([
        _turn("developer", _report(review="requested")),
        _turn("review", '{"phase": "review", "verdict": "approve"}'),
    ])
    assert _next_agent_dev_v2(task) is None
    assert _build_next_action(task, None)["kind"] == "complete"


def test_review_request_changes_routes_back_to_developer():
    task = _task([
        _turn("developer", _report(review="requested")),
        _turn("review", '{"phase": "review", "verdict": "request_changes", '
                        '"issues": ["foo.py:42 NPE"]}'),
    ])
    assert _next_agent_dev_v2(task) == "developer"


def test_developer_backstop_checkpoints_human(tmp_path, monkeypatch):
    turns = []
    for _ in range(_DEV_V2_MAX_DEVELOPER_TURNS):
        turns.append(_turn("developer", _report(review="requested")))
        turns.append(_turn("review", '{"phase": "review", '
                                     '"verdict": "request_changes"}'))
    task = _task(turns)
    action = _build_next_action(task, None)
    assert action["kind"] == "checkpoint_human"
    assert "backstop" in action["question"]


def test_rework_prompt_carries_review_feedback(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "")  # fall back to repo layout
    task = _task([
        _turn("developer", _report(review="requested")),
        _turn("review", 'review notes\n{"phase": "review", '
                        '"verdict": "request_changes", '
                        '"issues": ["foo.py:42 NPE possible"]}'),
    ])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "developer"
    assert "foo.py:42 NPE possible" in action["prompt"]


# ── truncation keeps the verdict tail ────────────────────────────────────────


def test_truncate_keep_tail_preserves_exit_report():
    long_result = ("x" * 20000) + "\n" + _report(review="requested")
    truncated = _truncate_keep_tail(long_result, 4000)
    assert len(truncated) <= 4000 + 10
    report = parse_exit_report(truncated)
    assert report is not None and report["review"] == "requested"


def test_truncate_keep_tail_noop_when_short():
    assert _truncate_keep_tail("short", 4000) == "short"
