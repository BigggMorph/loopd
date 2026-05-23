"""Unit tests for wake_inference.py"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import wake_inference


def _write_transcript(path: Path, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for ev in events:
            f.write(json.dumps(ev) + "\n")


def test_infer_fresh_when_no_transcript(tmp_path):
    reason = wake_inference.infer(str(tmp_path / "nope.jsonl"), {})
    assert reason == ("fresh", None)


def test_infer_fresh_when_empty_file(tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text("")
    reason = wake_inference.infer(str(p), {})
    assert reason == ("fresh", None)


def test_infer_detects_orch_inject_marker(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {"role": "assistant", "message": {"role": "assistant", "content": "ok"}},
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "ORCH_INJECT:dev_done\n\nproceed",
                },
            },
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("orch_hook_inject", "dev_done")


def test_infer_detects_teammate_sender_prefix(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "[issue-analyzer]: here is my analysis\n\n{...}",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "issue-analyzer")


def test_infer_detects_tester_sender(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "[from:tester] verdict pass",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "tester")


def test_infer_detects_scout_sender(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "## issue-scout\n\nFound 3 candidates",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "issue-scout")


def test_infer_handles_malformed_lines(tmp_path):
    p = tmp_path / "transcript.jsonl"
    p.write_text(
        "this is not json\n"
        + json.dumps({"role": "user", "message": {"role": "user", "content": "hi"}})
        + "\n"
        "another broken line {"
    )
    reason = wake_inference.infer(str(p), {})
    # Should still get "fresh" (one valid user event, no special markers).
    assert reason == ("fresh", None)


def test_infer_handles_content_blocks(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "[issue-analyzer]: done"},
                    ],
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "issue-analyzer")


def test_read_last_task_result_returns_none_when_absent(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_transcript(p, [{"role": "user", "message": {"role": "user", "content": "hi"}}])
    assert wake_inference.read_last_task_result(str(p)) is None


def test_infer_detects_product_planner_sender(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "[product-planner]: 4 epics proposed",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "product-planner")


def test_infer_detects_roadmap_strategist_sender(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "[roadmap-strategist]: pre-mvp diagnosis",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "roadmap-strategist")


def test_infer_detects_vision_critic_sender(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": "## vision-critic\n\nalignment_score 0.4",
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "vision-critic")


def test_infer_detects_sender_via_json_phase_without_any_prefix(tmp_path):
    """No [name]: prefix at all — the JSON-tail phase field must still
    let wake_inference classify the wake as ('teammate_reply', sender).

    Without this fallback, every teammate reply misclassified as 'fresh'
    and the lead silently stalled until the 10-min timeout fired.
    """
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "Here is my analysis.\n\n"
                        '{"phase":"analyze","status":"complete","human_needed":false}'
                    ),
                },
            }
        ],
    )
    reason = wake_inference.infer(str(p), {})
    assert reason == ("teammate_reply", "issue-analyzer")


@pytest.mark.parametrize(
    "phase,expected_sender",
    [
        ("analyze", "issue-analyzer"),
        ("test", "tester"),
        ("scout", "issue-scout"),
        ("reflection", "issue-scout"),
        ("plan", "product-planner"),
        ("roadmap", "roadmap-strategist"),
        ("vision_check", "vision-critic"),
    ],
)
def test_infer_detects_every_phase_value(tmp_path, phase, expected_sender):
    p = tmp_path / f"transcript-{phase}.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": f'{{"phase":"{phase}","status":"complete"}}',
                },
            }
        ],
    )
    assert wake_inference.infer(str(p), {}) == ("teammate_reply", expected_sender)


def test_infer_json_phase_handles_fenced_block(tmp_path):
    """LLMs sometimes wrap JSON in ```json fences; the tail scan must skip
    fence delimiters and still find the inner JSON line.
    """
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "Result below.\n\n```json\n"
                        '{"phase":"vision_check","status":"complete","alignment_score":0.4}\n'
                        "```"
                    ),
                },
            }
        ],
    )
    assert wake_inference.infer(str(p), {}) == ("teammate_reply", "vision-critic")


def test_infer_json_phase_unknown_value_is_fresh(tmp_path):
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": '{"phase":"made_up_phase","status":"complete"}',
                },
            }
        ],
    )
    # Unknown phase — treat as not a teammate reply.
    assert wake_inference.infer(str(p), {}) == ("fresh", None)


def test_infer_prefix_takes_precedence_over_phase(tmp_path):
    """If both [name]: prefix and JSON phase are present, the explicit
    prefix wins. Prevents an attacker who controls JSON content from
    spoofing a different sender than the harness-attributed one.
    """
    p = tmp_path / "transcript.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "user",
                "message": {
                    "role": "user",
                    "content": (
                        "[tester] my verdict\n\n"
                        '{"phase":"analyze","status":"complete"}'
                    ),
                },
            }
        ],
    )
    assert wake_inference.infer(str(p), {}) == ("teammate_reply", "tester")


def test_read_last_task_result_extracts_output(tmp_path):
    p = tmp_path / "t.jsonl"
    _write_transcript(
        p,
        [
            {
                "role": "assistant",
                "tool": "Task",
                "toolUseResult": {"output": "PR approved at https://github.com/x/y/pull/3"},
            }
        ],
    )
    out = wake_inference.read_last_task_result(str(p))
    assert out is not None and "approved" in out
