"""Unit tests for audit.py"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

import audit
import orchestrator_state


def _fake_run_ok(*args, **kwargs):
    return subprocess.CompletedProcess(args=args, returncode=0, stdout="created", stderr="")


def _fake_run_fail(*args, **kwargs):
    return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="rate limited")


def test_audited_bash_appends_entry_on_success(isolated_home):
    with mock.patch.object(subprocess, "run", side_effect=_fake_run_ok):
        rc, out, err = audit.audited_bash(
            ["gh", "issue", "close", "42", "--repo", "x/y"],
            actor="orchestrator",
            action="gh issue close",
            target="#42",
        )
    assert rc == 0
    state = orchestrator_state.read()
    assert len(state["audit_log"]) == 1
    entry = state["audit_log"][0]
    assert entry["actor"] == "orchestrator"
    assert entry["action"] == "gh issue close"
    assert entry["target"] == "#42"
    assert "payload_hash" in entry
    assert entry["argv"] == ["gh", "issue", "close", "42", "--repo", "x/y"]


def test_audited_bash_skips_log_on_failure(isolated_home):
    with mock.patch.object(subprocess, "run", side_effect=_fake_run_fail):
        rc, _, _ = audit.audited_bash(
            ["gh", "issue", "close", "42", "--repo", "x/y"],
            actor="orchestrator",
            action="gh issue close",
            target="#42",
        )
    assert rc == 1
    state = orchestrator_state.read()
    assert state["audit_log"] == []


def test_audited_bash_empty_argv_raises():
    with pytest.raises(ValueError):
        audit.audited_bash([], actor="x", action="y", target="z")


def test_maybe_rotate_archives_overflow_then_trims(isolated_home):
    state = orchestrator_state.read()
    state["audit_log"] = [
        {
            "at": f"2026-05-22T00:{i:02d}:00+00:00",
            "actor": "orch",
            "action": "gh issue close",
            "target": f"#{i}",
            "payload": {},
            "payload_hash": f"h{i:04x}",
            "argv": ["gh", "issue", "close", str(i)],
        }
        for i in range(audit.AUDIT_ROTATE_THRESHOLD + 5)
    ]
    archived = audit.maybe_rotate(state)
    assert archived == 5
    assert len(state["audit_log"]) == audit.AUDIT_ROTATE_THRESHOLD
    # First 5 should now be in the archive file.
    arc_dir = orchestrator_state.ORCHESTRATOR_DIR / "audit_archive"
    files = list(arc_dir.glob("*.jsonl"))
    assert len(files) == 1
    contents = files[0].read_text().splitlines()
    assert len(contents) == 5


def test_maybe_rotate_no_op_under_threshold(isolated_home):
    state = orchestrator_state.read()
    state["audit_log"] = [{"payload_hash": "x"}]
    n = audit.maybe_rotate(state)
    assert n == 0
    assert len(state["audit_log"]) == 1


def test_maybe_rotate_dedups_archive_on_recovery(isolated_home):
    state = orchestrator_state.read()
    # Simulate a prior crashed run: an archive file already contains entry h0001.
    archive_dir = orchestrator_state.ORCHESTRATOR_DIR / "audit_archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    arc_file = archive_dir / f"{today}.jsonl"
    arc_file.write_text(
        json.dumps({"payload_hash": "dup", "at": "old"}) + "\n"
    )
    state["audit_log"] = [
        {"payload_hash": "dup", "at": "old"},
        {"payload_hash": "new", "at": "now"},
    ] + [
        {"payload_hash": f"h{i:04x}"}
        for i in range(audit.AUDIT_ROTATE_THRESHOLD)
    ]
    n = audit.maybe_rotate(state)
    assert n == 2
    # Archive should now have 2 unique entries (dup + new), not 3 (dup wasn't appended again).
    contents = arc_file.read_text().splitlines()
    contents = [c for c in contents if c.strip()]
    parsed = [json.loads(c) for c in contents]
    hashes = [p.get("payload_hash") for p in parsed]
    assert hashes.count("dup") == 1
    assert "new" in hashes


def test_compute_undo_plan_for_known_actions(isolated_home):
    state = orchestrator_state.read()
    state["repo"] = "x/y"
    state["audit_log"] = [
        {"action": "gh issue close", "target": "10", "payload": {}},
        {"action": "gh pr edit --add-label", "target": "20", "payload": {"label": "scout-suggested"}},
        {"action": "gh pr merge", "target": "30", "payload": {}},
    ]
    plans = audit.compute_undo_plan(state, n=3)
    assert len(plans) == 3
    # Latest first: pr merge → no inverse
    assert plans[0]["entry"]["action"] == "gh pr merge"
    assert plans[0]["inverse_argv"] is None
    assert "rollback" in plans[0]["reason"]
    # Then label remove
    assert plans[1]["entry"]["action"] == "gh pr edit --add-label"
    assert plans[1]["inverse_argv"] == [
        "gh", "pr", "edit", "20", "--repo", "x/y",
        "--remove-label", "scout-suggested",
    ]
    # Then issue close → reopen
    assert plans[2]["entry"]["action"] == "gh issue close"
    assert plans[2]["inverse_argv"] == [
        "gh", "issue", "reopen", "10", "--repo", "x/y",
    ]


def test_compute_undo_plan_empty():
    assert audit.compute_undo_plan({}, 5) == []
    assert audit.compute_undo_plan({"audit_log": []}, 5) == []


# ---------- record_state_mutation (Rev 17 Phase 17-D) ----------

def test_record_state_mutation_appends_entry():
    state = {"audit_log": []}
    entry = audit.record_state_mutation(
        state, actor="vision-critic", action="state.vision update (accepted)",
        payload={"before": "old", "after": "new", "rationale": "drift"},
    )
    assert len(state["audit_log"]) == 1
    assert entry["actor"] == "vision-critic"
    assert entry["action"] == "state.vision update (accepted)"
    assert entry["kind"] == "state_mutation"
    assert entry["argv"] == []
    assert entry["payload"]["after"] == "new"


def test_record_state_mutation_handles_empty_payload():
    state = {"audit_log": []}
    entry = audit.record_state_mutation(
        state, actor="orchestrator", action="test action"
    )
    assert entry["payload"] == {}
    assert entry["target"] == ""


def test_record_state_mutation_payload_hash_is_stable():
    state1 = {"audit_log": []}
    state2 = {"audit_log": []}
    audit.record_state_mutation(state1, "a", "act", {"k": "v"})
    audit.record_state_mutation(state2, "b", "act", {"k": "v"})
    # Same payload → same hash even with different actor.
    assert state1["audit_log"][0]["payload_hash"] == state2["audit_log"][0]["payload_hash"]
