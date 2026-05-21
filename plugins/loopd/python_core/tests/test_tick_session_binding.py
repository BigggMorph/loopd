"""Tests for tick.py's session binding behaviour.

The bootstrap path (no LOOPD_SESSION_ID at ``tick init`` time → pending
claim file) and the UUID-present path (write directly to UUID session file)
are the central correctness properties for issue #4.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


@pytest.fixture
def stubbed_workspace(monkeypatch, tmp_path):
    """Stub WorkspaceManager.setup_task_workspace so cmd_init doesn't try
    to clone repos or create real git worktrees during unit tests.
    """
    from loopd_core.state import workspace_manager as wm_mod

    fake_ws = tmp_path / "fake-workspace"
    fake_ws.mkdir()

    def _stub_setup(self, *, task_id, repo, base_branch):
        return fake_ws

    monkeypatch.setattr(
        wm_mod.WorkspaceManager, "setup_task_workspace", _stub_setup
    )
    return fake_ws


def _run_init(args_str: str = '"do a thing" repo:BigggMorph/loopd'):
    from loopd_core import tick

    ns = argparse.Namespace(args=args_str, type="dev")
    return tick.cmd_init(ns)


def _read_pending_dir():
    from loopd_core import session_store

    return list(session_store._pending_dir().glob("*.json"))


def _read_sessions_dir():
    from loopd_core import session_store

    return list(session_store._sessions_dir().glob("*.json"))


def test_init_writes_pending_when_no_session_id(stubbed_workspace, capsys):
    """When no LOOPD_SESSION_ID is set, tick init must write a pending
    claim file (not a session file). This is the core defence against the
    issue-#4 cross-window leak.
    """
    rc = _run_init()
    assert rc == 0

    pending = _read_pending_dir()
    sessions = _read_sessions_dir()

    assert len(pending) == 1, f"expected 1 pending file, got {pending}"
    assert len(sessions) == 0, f"expected 0 session files, got {sessions}"

    # No file named cwd-*.json should ever be produced
    from loopd_core import session_store

    for p in session_store._sessions_dir().iterdir():
        assert not p.name.startswith("cwd-"), f"cwd-prefixed file leaked: {p}"

    # pending file has validation_token, next_action, created_at
    data = json.loads(pending[0].read_text())
    assert data.get("validation_token")
    assert data.get("next_action", {}).get("kind") == "invoke_subagent"
    assert "created_at" in data


def test_init_writes_session_when_session_id_present(
    stubbed_workspace, monkeypatch, capsys
):
    """When LOOPD_SESSION_ID is set (e.g. tick init re-run from a hook
    context that already knows the UUID), the session file is written
    directly under that UUID and no pending file is created.
    """
    monkeypatch.setenv("LOOPD_SESSION_ID", "test-uuid-1")

    rc = _run_init()
    assert rc == 0

    pending = _read_pending_dir()
    sessions = _read_sessions_dir()

    assert len(pending) == 0, f"unexpected pending files: {pending}"
    assert len(sessions) == 1, f"expected 1 session file, got {sessions}"
    assert sessions[0].name == "test-uuid-1.json"


def test_tick_command_errors_without_session_id(monkeypatch, capsys):
    """`tick` (no subcommand) must refuse to run when no CC UUID is
    available — falling back silently is what produced the cross-window
    bug. The error message tells the user to run /dev-task first.
    """
    from loopd_core import tick

    # No env vars set (conftest scrubs them)
    ns = argparse.Namespace()
    rc = tick.cmd_tick(ns)
    assert rc == 2

    captured = capsys.readouterr()
    assert "no active loopd task" in captured.out or "no active loopd task" in captured.err


def test_record_errors_without_session_id(monkeypatch, capsys):
    """`tick --record` must hard-error when LOOPD_SESSION_ID is missing
    rather than fall back to cwd-derived state.
    """
    from loopd_core import tick

    monkeypatch.setattr("sys.stdin", _StdinStub(json.dumps({"validation_token": "x"})))

    ns = argparse.Namespace()
    rc = tick.cmd_record(ns)
    assert rc == 2

    captured = capsys.readouterr()
    # Some message about needing a session id
    combined = captured.out + captured.err
    assert "session id" in combined.lower() or "no active" in combined.lower()


class _StdinStub:
    """Minimal stdin stand-in that returns a fixed string from read()."""

    def __init__(self, content: str):
        self._content = content

    def read(self) -> str:
        return self._content


def test_init_then_bootstrap_via_session_store(stubbed_workspace, monkeypatch):
    """End-to-end happy path: tick init writes pending, then a hook-like
    call to claim_pending_by_prompt_hash promotes it to a UUID session
    file. After this, read_session returns the canonical shape.
    """
    rc = _run_init()
    assert rc == 0

    pending = _read_pending_dir()
    assert len(pending) == 1

    pending_data = json.loads(pending[0].read_text())
    prompt_hash = pending_data["next_action"]["prompt_sha256"]

    from loopd_core import session_store

    sid = "originator-window-uuid"
    target = session_store.claim_pending_by_prompt_hash(prompt_hash, sid)
    assert target is not None

    session = session_store.read_session(sid)
    assert session["task_id"] == pending_data["task_id"]
    assert session["last_next_action"]["prompt_sha256"] == prompt_hash

    # pending consumed, sessions has exactly one file under originator UUID
    assert len(_read_pending_dir()) == 0
    assert len(_read_sessions_dir()) == 1
    assert _read_sessions_dir()[0].name == "originator-window-uuid.json"


def test_window_b_cannot_claim_pending_with_wrong_prompt(stubbed_workspace):
    """Window B sees the pending file (it lives under the shared
    ~/.loopd) but its main LLM's Task invocation has a *different* prompt,
    so claim_pending_by_prompt_hash returns None for B. This is the
    core cross-window isolation guarantee.
    """
    rc = _run_init()
    assert rc == 0

    from loopd_core import session_store

    # Window B's PreToolUse fires with some unrelated Task prompt hash.
    target = session_store.claim_pending_by_prompt_hash(
        "deadbeef-unrelated-prompt-hash", "window-b-uuid"
    )
    assert target is None

    # Window B has no session file. Window A's pending is preserved.
    assert not session_store.session_path_for("window-b-uuid").exists()
    assert len(_read_pending_dir()) == 1
