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


def test_optional_teammates():
    assert set(lifecycle.OPTIONAL_TEAMMATES) == {
        "product-planner",
        "roadmap-strategist",
        "vision-critic",
        "system-doctor",
    }


def test_label_spec_includes_rev17_labels():
    names = {l["name"] for l in lifecycle.LABEL_SPEC}
    assert "planner-suggested" in names
    assert "roadmap-context" in names
    assert "vision-update-pending" in names


def test_label_spec_includes_self_modify_labels():
    # Feature 1 — these must exist so `gh issue create --label self-modify`
    # works and would_self_modify fires on doctor-filed fixes.
    names = {l["name"] for l in lifecycle.LABEL_SPEC}
    assert "self-modify" in names
    assert "infrastructure" in names


def test_system_doctor_agent_frontmatter_name_matches():
    # ensure_team_member's S9 guard requires the frontmatter name to match.
    assert lifecycle._agent_frontmatter_name("system-doctor") == "system-doctor"


def test_discover_alive_teammates_empty_when_no_team(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    assert lifecycle.discover_alive_teammates("orch-x-y") == set()
    assert lifecycle.discover_alive_teammates("") == set()


def test_discover_alive_teammates_returns_member_names(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps(
            {
                "members": [
                    {"name": "issue-analyzer"},
                    {"name": "tester"},
                    "issue-scout",
                    {"name": "product-planner"},
                ]
            }
        )
    )
    alive = lifecycle.discover_alive_teammates("orch-x-y")
    assert alive == {
        "issue-analyzer", "tester", "issue-scout", "product-planner"
    }


def test_ensure_team_member_returns_true_when_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(
        json.dumps({"members": [{"name": "product-planner"}]})
    )
    state = {"pending_team_spawns": []}
    assert lifecycle.ensure_team_member("orch-x-y", "product-planner", state) is True
    assert state["pending_team_spawns"] == []


def test_ensure_team_member_marks_pending_when_not_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    # Empty team config — member is not alive.
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"members": []}))
    state = {"pending_team_spawns": []}
    out = lifecycle.ensure_team_member("orch-x-y", "product-planner", state)
    assert out is False
    assert "product-planner" in state["pending_team_spawns"]


def test_ensure_team_member_rejects_unknown_member(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    state = {"pending_team_spawns": []}
    assert lifecycle.ensure_team_member("orch-x-y", "evil-spawner", state) is False
    assert state["pending_team_spawns"] == []


def test_ensure_team_member_idempotent_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"members": []}))
    state = {"pending_team_spawns": []}
    lifecycle.ensure_team_member("orch-x-y", "vision-critic", state)
    lifecycle.ensure_team_member("orch-x-y", "vision-critic", state)
    assert state["pending_team_spawns"].count("vision-critic") == 1


def test_ensure_team_member_rejects_when_definition_missing(tmp_path, monkeypatch):
    """If agents/<member>.md is missing, ensure_team_member must return False.

    Simulated by repointing AGENTS_DIR to a tmp dir with no files.
    """
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    monkeypatch.setattr(lifecycle, "AGENTS_DIR", tmp_path / "agents-fake")
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"members": []}))
    state = {"pending_team_spawns": []}
    assert lifecycle.ensure_team_member("orch-x-y", "product-planner", state) is False
    assert state["pending_team_spawns"] == []


def test_ensure_team_member_rejects_when_frontmatter_name_mismatches(tmp_path, monkeypatch):
    """Attacker-planted definition with wrong frontmatter name must be rejected."""
    monkeypatch.setattr(lifecycle, "TEAMS_DIR", tmp_path / "teams")
    agents_dir = tmp_path / "agents-fake"
    agents_dir.mkdir()
    (agents_dir / "product-planner.md").write_text(
        "---\nname: evil-impersonator\n---\n\nbody\n"
    )
    monkeypatch.setattr(lifecycle, "AGENTS_DIR", agents_dir)
    cfg_dir = tmp_path / "teams" / "orch-x-y"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.json").write_text(json.dumps({"members": []}))
    state = {"pending_team_spawns": []}
    assert lifecycle.ensure_team_member("orch-x-y", "product-planner", state) is False
    assert state["pending_team_spawns"] == []


# =====================================================================
# Rev 17 Phase 17-G — working-memory health / recover_team_context
# =====================================================================

import datetime as _dt
from unittest import mock


def test_watermark_constants_cover_all_teammates():
    expected = {
        "issue-analyzer", "tester", "issue-scout",
        "product-planner", "roadmap-strategist", "vision-critic",
        "system-doctor",
    }
    assert set(lifecycle.WATERMARK_CALLS.keys()) == expected
    assert set(lifecycle.WATERMARK_TOKENS.keys()) == expected
    for v in lifecycle.WATERMARK_CALLS.values():
        assert v >= 1
    for v in lifecycle.WATERMARK_TOKENS.values():
        assert v >= 1000


def test_record_teammate_call_initializes_and_increments():
    state = {"teammate_health": {}}
    e1 = lifecycle.record_teammate_call(state, "issue-analyzer", sent_tokens=1000, received_tokens=500)
    assert e1["call_count"] == 1
    assert e1["estimated_tokens"] == 1500
    e2 = lifecycle.record_teammate_call(state, "issue-analyzer", sent_tokens=2000)
    assert e2["call_count"] == 2
    assert e2["estimated_tokens"] == 3500


def test_needs_respawn_below_watermark_false():
    state = {
        "teammate_health": {
            "issue-analyzer": {
                "call_count": 5,
                "estimated_tokens": 50000,
            }
        }
    }
    assert lifecycle.needs_respawn(state, "issue-analyzer") is False


def test_needs_respawn_at_call_watermark_true():
    state = {
        "teammate_health": {
            "issue-analyzer": {
                "call_count": lifecycle.WATERMARK_CALLS["issue-analyzer"],
                "estimated_tokens": 0,
            }
        }
    }
    assert lifecycle.needs_respawn(state, "issue-analyzer") is True


def test_needs_respawn_at_token_watermark_true():
    state = {
        "teammate_health": {
            "vision-critic": {
                "call_count": 1,
                "estimated_tokens": lifecycle.WATERMARK_TOKENS["vision-critic"],
            }
        }
    }
    assert lifecycle.needs_respawn(state, "vision-critic") is True


def test_needs_respawn_unknown_member_false():
    assert lifecycle.needs_respawn({}, "unknown-bot") is False


def test_needs_respawn_rate_limit_blocks_recent_respawn():
    recent = (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)
    ).isoformat()
    state = {
        "teammate_health": {
            "issue-analyzer": {
                "call_count": lifecycle.WATERMARK_CALLS["issue-analyzer"] + 5,
                "estimated_tokens": 0,
                "last_respawn_at": recent,
            }
        }
    }
    assert lifecycle.needs_respawn(state, "issue-analyzer") is False


def test_reset_teammate_health_resets_counters_and_bumps_respawn():
    state = {
        "teammate_health": {
            "issue-analyzer": {
                "call_count": 30,
                "estimated_tokens": 200000,
                "respawn_count": 2,
            }
        }
    }
    lifecycle.reset_teammate_health(state, "issue-analyzer")
    h = state["teammate_health"]["issue-analyzer"]
    assert h["call_count"] == 0
    assert h["estimated_tokens"] == 0
    assert h["respawn_count"] == 3
    assert h["last_respawn_at"] is not None


def test_recover_team_context_includes_vision_and_repo():
    state = {
        "vision": "Build a chatbot",
        "repo": "x/y",
        "lessons_learned": [],
        "scout_history": [],
        "current_issue": None,
    }
    body = lifecycle.recover_team_context(state, "issue-scout")
    assert "Build a chatbot" in body
    assert "Repo: x/y" in body
    assert "(re)spawned" in body
    assert "Recent scout outcomes" in body


def test_recover_team_context_planner_uses_planner_history():
    state = {
        "vision": "v",
        "repo": "x/y",
        "planner_history": [
            {"ts": "2026-05-22T10:00:00Z", "issue_urls_created": ["u1", "u2"]}
        ],
    }
    body = lifecycle.recover_team_context(state, "product-planner")
    assert "planner outcomes" in body.lower()
    assert "2 issue(s) created" in body


def test_recover_team_context_vision_critic_uses_history():
    state = {
        "vision": "v",
        "repo": "x/y",
        "vision_critic_history": [
            {
                "ts": "2026-05-22T10:00:00Z",
                "user_action": "rejected",
                "alignment_score": 0.4,
            }
        ],
    }
    body = lifecycle.recover_team_context(state, "vision-critic")
    assert "rejected" in body
    assert "score=0.4" in body


def test_recover_team_context_analyzer_includes_current_issue():
    state = {
        "vision": "v",
        "repo": "x/y",
        "current_issue": 42,
        "issues": {"42": {"number": 42, "status": "analyze_pending"}},
    }
    body = lifecycle.recover_team_context(state, "issue-analyzer")
    assert "#42" in body
    assert "analyze_pending" in body


def test_recover_team_context_lessons_included():
    state = {
        "vision": "v",
        "repo": "x/y",
        "lessons_learned": [
            {"pattern": "PR URL 추출 실패", "observed_count": 3, "resolution": "..."}
        ],
    }
    body = lifecycle.recover_team_context(state, "issue-scout")
    assert "PR URL 추출 실패" in body
    assert "seen 3x" in body
