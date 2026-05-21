"""Unit tests for loopd_core.session_store.

Covers the new UUID-keyed session file API, the pending-claim bootstrap
path, and the stale-pending GC. These tests together guard against any
regression toward the cwd-hash fallback that caused issue #4.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


def _read_json(p: Path) -> dict:
    return json.loads(p.read_text())


# ─────────────────────────────────────────────────────────────────────────────
# session_path_for
# ─────────────────────────────────────────────────────────────────────────────


def test_session_path_for_rejects_empty_sid():
    from loopd_core import session_store

    with pytest.raises(ValueError):
        session_store.session_path_for("")


def test_session_path_for_rejects_cwd_prefix():
    from loopd_core import session_store

    with pytest.raises(ValueError):
        session_store.session_path_for("cwd-abcdef0123456789")


def test_session_path_for_returns_uuid_path():
    from loopd_core import session_store

    p = session_store.session_path_for("fake-uuid-xyz")
    assert p.name == "fake-uuid-xyz.json"
    assert p.parent.name == "sessions"


# ─────────────────────────────────────────────────────────────────────────────
# write / read / delete session
# ─────────────────────────────────────────────────────────────────────────────


def test_write_and_read_session_round_trip():
    from loopd_core import session_store

    sid = "uuid-1234"
    payload = {"task_id": "task-test-001", "last_next_action": {"kind": "complete"}}
    session_store.write_session(sid, payload)

    got = session_store.read_session(sid)
    assert got == payload


def test_read_session_missing_returns_empty_dict():
    from loopd_core import session_store

    assert session_store.read_session("uuid-doesnt-exist") == {}


def test_delete_session_removes_file():
    from loopd_core import session_store

    sid = "uuid-to-delete"
    session_store.write_session(sid, {"task_id": "t"})
    assert session_store.session_path_for(sid).exists()
    session_store.delete_session(sid)
    assert not session_store.session_path_for(sid).exists()
    # idempotent
    session_store.delete_session(sid)


# ─────────────────────────────────────────────────────────────────────────────
# write_pending / claim_pending
# ─────────────────────────────────────────────────────────────────────────────


def test_write_pending_creates_file_with_created_at():
    from loopd_core import session_store

    task_id = "task-test-002"
    p = session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "v1.tok",
            "next_action": {"kind": "invoke_subagent", "prompt_sha256": "deadbeef"},
        },
    )
    data = _read_json(p)
    assert data["task_id"] == task_id
    assert data["validation_token"] == "v1.tok"
    assert "created_at" in data and data["created_at"].endswith("Z")


def test_claim_pending_moves_to_session_file():
    from loopd_core import session_store

    task_id = "task-test-003"
    sid = "uuid-3"
    session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "tok-3",
            "next_action": {"kind": "invoke_subagent", "subagent_type": "planning"},
        },
    )

    target = session_store.claim_pending(task_id, "tok-3", sid)
    assert target is not None
    assert target == session_store.session_path_for(sid)
    assert target.exists()
    # pending file is consumed
    assert not session_store.pending_path_for(task_id).exists()
    # canonical session shape
    data = _read_json(target)
    assert data["task_id"] == task_id
    assert data["last_next_action"]["subagent_type"] == "planning"


def test_claim_pending_returns_none_for_wrong_token():
    from loopd_core import session_store

    task_id = "task-test-004"
    sid = "uuid-4"
    session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "correct-token",
            "next_action": {"kind": "invoke_subagent"},
        },
    )

    result = session_store.claim_pending(task_id, "wrong-token", sid)
    assert result is None
    # pending file is preserved on failed claim
    assert session_store.pending_path_for(task_id).exists()
    # no session file created
    assert not session_store.session_path_for(sid).exists()


def test_claim_pending_returns_none_when_no_pending_exists():
    from loopd_core import session_store

    result = session_store.claim_pending("task-doesnt-exist", "any-token", "uuid-5")
    assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# claim_pending_by_prompt_hash
# ─────────────────────────────────────────────────────────────────────────────


def test_claim_pending_by_prompt_hash_matches_by_sha():
    from loopd_core import session_store

    task_id = "task-test-005"
    sid = "uuid-5"
    prompt_hash = "abcdef0123456789"
    session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "tok-5",
            "next_action": {
                "kind": "invoke_subagent",
                "subagent_type": "planning",
                "prompt_sha256": prompt_hash,
            },
        },
    )

    target = session_store.claim_pending_by_prompt_hash(prompt_hash, sid)
    assert target is not None
    assert target.exists()
    assert not session_store.pending_path_for(task_id).exists()


def test_claim_pending_by_prompt_hash_no_match_returns_none():
    from loopd_core import session_store

    task_id = "task-test-006"
    session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "tok-6",
            "next_action": {"kind": "invoke_subagent", "prompt_sha256": "real-hash"},
        },
    )

    result = session_store.claim_pending_by_prompt_hash("other-hash", "uuid-6")
    assert result is None
    # pending preserved
    assert session_store.pending_path_for(task_id).exists()


def test_claim_pending_by_prompt_hash_empty_inputs_returns_none():
    from loopd_core import session_store

    assert session_store.claim_pending_by_prompt_hash("", "uuid") is None
    assert session_store.claim_pending_by_prompt_hash("hash", "") is None


# ─────────────────────────────────────────────────────────────────────────────
# cleanup_stale_pending
# ─────────────────────────────────────────────────────────────────────────────


def test_cleanup_stale_pending_deletes_old_files(monkeypatch):
    from loopd_core import session_store

    task_id = "task-test-007"
    p = session_store.pending_path_for(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    p.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "validation_token": "old",
                "next_action": {},
                "created_at": old_ts,
            }
        )
    )

    deleted = session_store.cleanup_stale_pending(ttl_seconds=86400)
    assert deleted == 1
    assert not p.exists()


def test_cleanup_stale_pending_skips_fresh_files():
    from loopd_core import session_store

    task_id = "task-test-008"
    session_store.write_pending(
        task_id,
        {
            "task_id": task_id,
            "validation_token": "fresh",
            "next_action": {},
        },
    )
    p = session_store.pending_path_for(task_id)
    assert p.exists()

    deleted = session_store.cleanup_stale_pending(ttl_seconds=86400)
    assert deleted == 0
    assert p.exists()


def test_cleanup_stale_pending_skips_malformed_timestamp():
    from loopd_core import session_store

    task_id = "task-test-009"
    p = session_store.pending_path_for(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "validation_token": "x",
                "next_action": {},
                "created_at": "not-a-real-timestamp",
            }
        )
    )

    deleted = session_store.cleanup_stale_pending(ttl_seconds=0)
    assert deleted == 0
    # malformed timestamp → preserved, never silently nuked
    assert p.exists()


def test_cleanup_stale_pending_handles_missing_dir(monkeypatch, tmp_path):
    from loopd_core import session_store

    # Pending dir doesn't exist on a fresh install. cleanup must not crash.
    # (Note: _pending_dir() actually mkdirs it; this verifies idempotency.)
    deleted = session_store.cleanup_stale_pending(ttl_seconds=1)
    assert deleted == 0
