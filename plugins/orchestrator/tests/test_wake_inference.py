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
