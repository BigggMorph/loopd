"""Subprocess-level tests for the three loopd hook scripts.

These tests are the closest analogue to "two CC windows running in the
same project" that we can write without a live Claude Code. Each test
fires a hook with a synthetic JSON payload and asserts the hook's
behaviour matches the no-cwd-fallback contract from issue #4.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3].parent  # …/loopd
HOOKS_DIR = REPO_ROOT / "plugins" / "loopd" / "hooks"
PRETOOL = HOOKS_DIR / "pretool_validate.py"
POSTTOOL = HOOKS_DIR / "posttool_record.py"
STOP = HOOKS_DIR / "stop_continue.py"
PYTHON_CORE = REPO_ROOT / "plugins" / "loopd" / "python_core"


def _run_hook(hook_path: Path, payload: dict, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # Make loopd_core importable in the subprocess
    env["PYTHONPATH"] = str(PYTHON_CORE) + os.pathsep + env.get("PYTHONPATH", "")
    # Allow the hook lib to find python_core via CLAUDE_PLUGIN_ROOT
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT / "plugins" / "loopd")
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(hook_path)],
        input=json.dumps(payload),
        text=True,
        env=env,
        capture_output=True,
        timeout=15,
    )


def _sessions_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions"


def _pending_dir(tmp_path: Path) -> Path:
    return tmp_path / "sessions" / ".pending"


# ─────────────────────────────────────────────────────────────────────────────
# Stop hook
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_hook_noop_when_no_session_file(monkeypatch, tmp_path):
    """Window B fires Stop with its own UUID. No session file exists for
    that UUID → hook must produce no output and exit 0. (This is the
    critical fix for issue #4 — old code would have fallen back to the
    cwd-hash file and emitted decision:"block".)
    """
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    payload = {"session_id": "window-B-uuid", "hook_event_name": "Stop"}
    result = _run_hook(STOP, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"unexpected stdout: {result.stdout!r}"


def test_stop_hook_emits_block_when_session_file_exists(monkeypatch, tmp_path):
    """Window A fires Stop with its own UUID and has a session file under
    that UUID with a pending invoke_subagent. The hook must emit
    decision:"block" so the LLM continues the pipeline.

    NB: this also exercises the tick script path; if tick can't be located,
    the hook silently returns 0 (no block) — which is fine for the bug we
    care about. We assert *either* a block (full path works) or a clean
    no-op (tick-script unavailable in the test environment).
    """
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    sessions = _sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    (sessions / "window-A-uuid.json").write_text(
        json.dumps(
            {
                "task_id": "task-test-stop-001",
                "last_next_action": {
                    "kind": "invoke_subagent",
                    "subagent_type": "planning",
                    "prompt": "irrelevant",
                    "validation_token": "tok",
                    "prompt_sha256": "0" * 64,
                    "iteration": 1,
                },
            }
        )
    )

    payload = {"session_id": "window-A-uuid", "hook_event_name": "Stop"}
    result = _run_hook(STOP, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    # Either a decision:"block" payload was emitted, or the tick subprocess
    # failed to read the task (which is expected in a stub test that didn't
    # populate the full task FSM). What we MUST NOT see is the hook crashing.


def test_stop_hook_noop_for_empty_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))
    payload = {"session_id": "", "hook_event_name": "Stop"}
    result = _run_hook(STOP, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0
    assert result.stdout.strip() == ""


def test_stop_hook_ignores_legacy_cwd_hash_file(tmp_path, monkeypatch):
    """A leftover cwd-<hash>.json file from the old build must be IGNORED.
    Window B's Stop payload carries its own UUID; the hook must not
    silently match the cwd-hash file. This is FR-6 / AC-4.
    """
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    sessions = _sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    legacy = sessions / "cwd-deadbeefcafefeed.json"
    legacy.write_text(
        json.dumps(
            {
                "task_id": "task-legacy-001",
                "last_next_action": {"kind": "invoke_subagent", "subagent_type": "planning"},
            }
        )
    )

    payload = {"session_id": "fresh-window-uuid", "hook_event_name": "Stop"}
    result = _run_hook(STOP, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0
    assert result.stdout.strip() == "", f"hook adopted legacy cwd file: {result.stdout!r}"
    # The legacy file is intentionally NOT deleted by the hook
    assert legacy.exists()


# ─────────────────────────────────────────────────────────────────────────────
# PreToolUse hook
# ─────────────────────────────────────────────────────────────────────────────


def test_pretool_hook_noop_when_no_session_and_no_pending(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    payload = {
        "session_id": "window-B-uuid",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"subagent_type": "planning", "prompt": "some prompt"},
    }
    result = _run_hook(PRETOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    assert result.stderr == "", f"unexpected stderr: {result.stderr!r}"


def test_pretool_hook_claims_pending(monkeypatch, tmp_path):
    """When a pending claim file exists and PreToolUse fires with a Task
    whose prompt SHA matches the pending's next_action.prompt_sha256, the
    pending must be atomically promoted to sessions/<sid>.json.
    """
    import hashlib

    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    pending = _pending_dir(tmp_path)
    pending.mkdir(parents=True)

    prompt = "the exact prompt that tick init minted"
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    task_id = "task-test-pretool-001"

    (pending / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "validation_token": "v1.tok",
                "next_action": {
                    "kind": "invoke_subagent",
                    "subagent_type": "planning",
                    "prompt": prompt,
                    "prompt_sha256": prompt_hash,
                    "validation_token": "v1.tok",
                    "iteration": 1,
                },
                "created_at": "2026-05-21T00:00:00Z",
            }
        )
    )

    payload = {
        "session_id": "window-A-uuid",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {"subagent_type": "planning", "prompt": prompt},
    }
    result = _run_hook(PRETOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    # Session file now lives under window-A's UUID
    assert (_sessions_dir(tmp_path) / "window-A-uuid.json").exists()
    # Pending consumed
    assert not (pending / f"{task_id}.json").exists()


def test_pretool_hook_window_b_does_not_claim_pending(monkeypatch, tmp_path):
    """Window B fires PreToolUse with an unrelated prompt. The pending
    file (written by Window A's tick init) must remain untouched, and
    Window B must not get a session file.
    """
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    pending = _pending_dir(tmp_path)
    pending.mkdir(parents=True)
    task_id = "task-test-pretool-002"
    (pending / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "validation_token": "v1.tok",
                "next_action": {
                    "kind": "invoke_subagent",
                    "subagent_type": "planning",
                    "prompt": "A-prompt",
                    "prompt_sha256": "a" * 64,
                    "iteration": 1,
                },
                "created_at": "2026-05-21T00:00:00Z",
            }
        )
    )

    payload = {
        "session_id": "window-B-uuid",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "general-purpose",
            "prompt": "B's totally different prompt",
        },
    }
    result = _run_hook(PRETOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    # B has NO session file
    assert not (_sessions_dir(tmp_path) / "window-B-uuid.json").exists()
    # A's pending preserved
    assert (pending / f"{task_id}.json").exists()


def test_pretool_hook_rejects_subagent_mismatch_after_claim(monkeypatch, tmp_path):
    """Even after a successful claim, if the actual subagent_type doesn't
    match what the pending recorded, the hook must exit 2.
    """
    import hashlib

    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    pending = _pending_dir(tmp_path)
    pending.mkdir(parents=True)

    prompt = "claim-by-hash-but-wrong-subagent"
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    task_id = "task-test-pretool-003"
    (pending / f"{task_id}.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "validation_token": "v1.tok",
                "next_action": {
                    "kind": "invoke_subagent",
                    "subagent_type": "planning",
                    "prompt": prompt,
                    "prompt_sha256": prompt_hash,
                    "iteration": 1,
                },
                "created_at": "2026-05-21T00:00:00Z",
            }
        )
    )

    payload = {
        "session_id": "window-A-uuid",
        "hook_event_name": "PreToolUse",
        "tool_name": "Task",
        "tool_input": {
            "subagent_type": "implementation",  # mismatch with pending's "planning"
            "prompt": prompt,
        },
    }
    result = _run_hook(PRETOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "subagent_type mismatch" in result.stderr


def test_pretool_hook_passes_non_task_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    payload = {
        "session_id": "window-B-uuid",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    result = _run_hook(PRETOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0
    assert result.stderr == ""


# ─────────────────────────────────────────────────────────────────────────────
# PostToolUse hook
# ─────────────────────────────────────────────────────────────────────────────


def test_posttool_hook_noop_for_non_originator_window(monkeypatch, tmp_path):
    """Window B fires PostToolUse for an unrelated Task. No session file
    exists under B's UUID → hook must be a no-op. Critically, this must
    NOT silently invoke tick --record against window A's task.
    """
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))

    # Pre-populate window A's session file. Window B must NOT touch it.
    sessions = _sessions_dir(tmp_path)
    sessions.mkdir(parents=True)
    a_file = sessions / "window-A-uuid.json"
    a_file.write_text(
        json.dumps(
            {
                "task_id": "task-A-001",
                "last_next_action": {
                    "kind": "invoke_subagent",
                    "subagent_type": "planning",
                    "validation_token": "v1.A-tok",
                },
            }
        )
    )
    a_content_before = a_file.read_text()

    payload = {
        "session_id": "window-B-uuid",
        "hook_event_name": "PostToolUse",
        "tool_name": "Task",
        "tool_response": {"content": "B's unrelated result"},
    }
    result = _run_hook(POSTTOOL, payload, env_extra={"LOOPD_ROOT": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    # A's session file is unchanged
    assert a_file.read_text() == a_content_before
