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

import pytest

from loopd_core.tick import (
    _DEV_V2_MAX_DEVELOPER_TURNS,
    _build_next_action,
    _dev_v2_token_spend,
    _next_agent_dev_v2,
    _truncate_keep_tail,
    parse_dev_task_args,
    parse_exit_report,
)


@pytest.fixture(autouse=True)
def stub_exit_gate_probes(monkeypatch):
    """Default gate probes to benign values so routing tests never shell out
    to gh/git. Individual tests override to exercise specific gates."""
    monkeypatch.setattr("loopd_core.tick._dev_v2_pr_verified", lambda report: True)
    monkeypatch.setattr(
        "loopd_core.tick._dev_v2_git_diff_stats",
        lambda ws, base: (["src/app.py"], 50),
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


# ── exit gates ───────────────────────────────────────────────────────────────


def test_gate_risky_path_forces_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "loopd_core.tick._dev_v2_git_diff_stats",
        lambda ws, base: (["src/auth/login.py"], 30),
    )
    task = _task([_turn("developer", _report(review="skipped"))])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "review"
    assert "risky paths" in action["prompt"]


def test_gate_large_diff_forces_review(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "loopd_core.tick._dev_v2_git_diff_stats",
        lambda ws, base: (["src/big.py"], 500),
    )
    task = _task([_turn("developer", _report(review="skipped"))])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "review"


def test_gate_tests_not_run_on_code_diff_forces_review(monkeypatch, tmp_path):
    task = _task([
        _turn("developer", _report(review="skipped",
                                   tests={"command": "", "result": "not_run",
                                          "summary": ""}))
    ])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "review"


def test_gate_tests_not_run_on_docs_only_diff_completes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "loopd_core.tick._dev_v2_git_diff_stats",
        lambda ws, base: (["README.md", "docs/guide.rst"], 40),
    )
    task = _task([
        _turn("developer", _report(review="skipped",
                                   tests={"command": "", "result": "not_run",
                                          "summary": ""}))
    ])
    assert _build_next_action(task, tmp_path)["kind"] == "complete"


def test_gate_passed_claim_without_log_artifact_forces_review(tmp_path):
    # tmp_path is a real dir with no _loopd/<task_id>/test_log.txt
    task = _task([_turn("developer", _report(review="skipped"))])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "review"
    assert "test log artifact" in action["prompt"]


def test_gate_passed_claim_with_log_artifact_completes(tmp_path):
    log_dir = tmp_path / "_loopd" / "task-t"
    log_dir.mkdir(parents=True)
    (log_dir / "test_log.txt").write_text("3 passed in 0.1s\n")
    task = _task([_turn("developer", _report(review="skipped"))])
    assert _build_next_action(task, tmp_path)["kind"] == "complete"


def test_gate_missing_pr_forces_developer_rework(monkeypatch, tmp_path):
    monkeypatch.setattr("loopd_core.tick._dev_v2_pr_verified",
                        lambda report: False)
    task = _task([_turn("developer", _report(review="skipped"))])
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "invoke_subagent"
    assert action["subagent_type"] == "developer"
    assert "pr_url" in action["prompt"]


def test_gate_pr_check_unavailable_fails_open(monkeypatch, tmp_path):
    monkeypatch.setattr("loopd_core.tick._dev_v2_pr_verified",
                        lambda report: None)
    log_dir = tmp_path / "_loopd" / "task-t"
    log_dir.mkdir(parents=True)
    (log_dir / "test_log.txt").write_text("ok\n")
    task = _task([_turn("developer", _report(review="skipped"))])
    assert _build_next_action(task, tmp_path)["kind"] == "complete"


def test_gate_review_approve_skips_review_forcing_gates(monkeypatch, tmp_path):
    # Risky diff would force review — but an approve already landed, so only
    # the PR gate applies and the task completes.
    monkeypatch.setattr(
        "loopd_core.tick._dev_v2_git_diff_stats",
        lambda ws, base: (["src/auth/login.py"], 999),
    )
    task = _task([
        _turn("developer", _report(review="requested")),
        _turn("review", '{"phase": "review", "verdict": "approve"}'),
    ])
    assert _build_next_action(task, tmp_path)["kind"] == "complete"


# ── token budget ─────────────────────────────────────────────────────────────


def test_budget_exhaustion_checkpoints_human(tmp_path):
    task = _task(
        [
            _turn("developer", _report(review="requested")),
            {"subagent": "review", "state": "completed",
             "result": '{"phase": "review", "verdict": "request_changes"}',
             "input_tokens": 900, "output_tokens": 200},
        ],
        metadata={"token_budget": 1000},
    )
    action = _build_next_action(task, tmp_path)
    assert action["kind"] == "checkpoint_human"
    assert "token budget" in action["question"]


def test_token_spend_falls_back_to_char_estimate():
    task = _task([_turn("developer", "x" * 4000)])
    assert _dev_v2_token_spend(task) == 1000


def test_parse_dev_task_args_budget():
    assert parse_dev_task_args('"x" repo:o/r budget:400k')["budget"] == 400_000
    assert parse_dev_task_args('"x" repo:o/r budget:5000')["budget"] == 5000
    assert parse_dev_task_args('"x" repo:o/r')["budget"] is None


# ── truncation keeps the verdict tail ────────────────────────────────────────


def test_truncate_keep_tail_preserves_exit_report():
    long_result = ("x" * 20000) + "\n" + _report(review="requested")
    truncated = _truncate_keep_tail(long_result, 4000)
    assert len(truncated) <= 4000 + 10
    report = parse_exit_report(truncated)
    assert report is not None and report["review"] == "requested"


def test_truncate_keep_tail_noop_when_short():
    assert _truncate_keep_tail("short", 4000) == "short"
