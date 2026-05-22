"""Unit tests for lifecycle.py"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import lifecycle


def test_label_spec_includes_all_designed_labels():
    names = {l["name"] for l in lifecycle.LABEL_SPEC}
    required = {
        "scout-suggested",
        "split-epic",
        "orchestrator-rejected",
        "orchestrator-skipped",
        "orchestrator-abandoned",
        "regression-suspect",
        "orchestrator-managed",
        "priority/high",
        "priority/medium",
        "priority/low",
        "complexity/0",
        "complexity/1",
        "complexity/2",
        "complexity/3",
        "complexity/4",
        "migration",
        "auth",
        "breaking-change",
        "security",
        "recommend-human-review",
    }
    missing = required - names
    assert not missing, f"missing labels: {missing}"


def test_label_spec_colors_are_valid_hex():
    for spec in lifecycle.LABEL_SPEC:
        color = spec["color"]
        assert len(color) == 6
        int(color, 16)  # raises on invalid hex


def test_required_teammates_are_three():
    assert set(lifecycle.REQUIRED_TEAMMATES) == {
        "issue-analyzer",
        "tester",
        "issue-scout",
    }


def test_ensure_labels_classifies_results():
    def fake_run(cmd, timeout=30):
        # First label "exists", second "created", third errors.
        name = cmd[3]
        if name == "scout-suggested":
            return 1, "", "Label 'scout-suggested' already exists\n"
        if name == "split-epic":
            return 0, "https://github.com/x/y/labels/split-epic", ""
        return 1, "", "rate limited\n"

    with mock.patch.object(lifecycle, "_run", side_effect=fake_run):
        out = lifecycle.ensure_labels("x/y")
    assert out["scout-suggested"] == "exists"
    assert out["split-epic"] == "created"
    # 18 other labels also processed (error or other).
    assert len(out) == len(lifecycle.LABEL_SPEC)


def test_ensure_split_label_idempotent():
    with mock.patch.object(
        lifecycle, "_run",
        return_value=(1, "", "Label 'split-from-#42' already exists\n"),
    ):
        ok, msg = lifecycle.ensure_split_label("x/y", 42)
    assert ok is True
    assert msg == "exists"


def test_team_alive_false_when_no_config(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    assert lifecycle.team_alive("orch-x-y") is False


def test_team_alive_false_when_members_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"members": [{"name": "issue-analyzer"}, {"name": "tester"}]})
    )
    assert lifecycle.team_alive("orch-x-y") is False  # missing issue-scout


def test_team_alive_true_when_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "members": [
                    {"name": "issue-analyzer"},
                    {"name": "tester"},
                    {"name": "issue-scout"},
                ]
            }
        )
    )
    assert lifecycle.team_alive("orch-x-y") is True


def test_team_alive_false_for_corrupted_config(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text("not json {")
    assert lifecycle.team_alive("orch-x-y") is False


def test_team_alive_false_on_empty_name():
    assert lifecycle.team_alive("") is False


def test_shutdown_marker_is_valid_json():
    raw = lifecycle.shutdown_marker("orch-x-y")
    parsed = json.loads(raw)
    assert parsed["type"] == "shutdown_request"
    assert parsed["team"] == "orch-x-y"
