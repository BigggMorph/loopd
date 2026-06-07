"""Feature 3 — per-repo parallel instances + reset.

Covers slug derivation, per-instance path isolation, the session↔instance
binding map, the flat→per-repo migration, reset_instance, and the orch_stop
multi-instance scan (find_instance_by_dev_session).
"""

from __future__ import annotations

import json

import pytest

import orchestrator_state as st


# --- repo_to_slug ---------------------------------------------------------

@pytest.mark.parametrize(
    "repo,expected",
    [
        ("owner/repo", "owner-repo"),
        ("BigggMorph/loopd", "BigggMorph-loopd"),
        ("  owner/repo  ", "owner-repo"),
        ("a/b/c", "a-b-c"),
        ("owner/repo.git", "owner-repo.git"),
        ("", ""),
        (None, ""),
        ("weird name!/x", "weird-name-x"),
    ],
)
def test_repo_to_slug(isolated_home, repo, expected):
    assert st.repo_to_slug(repo) == expected


def test_repo_to_slug_is_idempotent(isolated_home):
    once = st.repo_to_slug("owner/repo")
    assert st.repo_to_slug(once) == once


# --- active instance path isolation --------------------------------------

def test_unbound_uses_flat_layout(isolated_home):
    st.clear_active_instance()
    assert st.active_instance_slug() == ""
    assert st.state_path() == st.ORCHESTRATOR_DIR / "state.json"
    # __getattr__ legacy alias resolves to the same flat path.
    assert st.STATE_PATH == st.ORCHESTRATOR_DIR / "state.json"


def test_set_active_instance_namespaces_paths(isolated_home):
    st.set_active_instance("owner/repo")
    assert st.active_instance_slug() == "owner-repo"
    assert st.state_path() == st.ORCHESTRATOR_DIR / "owner-repo" / "state.json"
    assert st.lock_path() == st.ORCHESTRATOR_DIR / "owner-repo" / "state.lock"
    assert st.sentinel_dev_done() == st.ORCHESTRATOR_DIR / "owner-repo" / "dev_done_pending.flag"
    assert st.audit_archive_dir() == st.ORCHESTRATOR_DIR / "owner-repo" / "audit_archive"
    # Legacy aliases follow the active instance too.
    assert st.STATE_PATH == st.ORCHESTRATOR_DIR / "owner-repo" / "state.json"


def test_two_instances_are_isolated(isolated_home):
    st.set_active_instance("a/x")
    with st.flock_session() as state:
        state["vision"] = "vision-a"
        state["current_issue"] = 1
        st.write_in_lock(state)

    st.set_active_instance("b/y")
    with st.flock_session() as state:
        state["vision"] = "vision-b"
        state["current_issue"] = 2
        st.write_in_lock(state)

    st.set_active_instance("a/x")
    a = st.read()
    st.set_active_instance("b/y")
    b = st.read()
    assert a["vision"] == "vision-a" and a["current_issue"] == 1
    assert b["vision"] == "vision-b" and b["current_issue"] == 2
    # Distinct files on disk.
    assert (st.ORCHESTRATOR_DIR / "a-x" / "state.json").exists()
    assert (st.ORCHESTRATOR_DIR / "b-y" / "state.json").exists()
    # No flat file was created.
    assert not (st.ORCHESTRATOR_DIR / "state.json").exists()


# --- session ↔ instance binding ------------------------------------------

def test_bind_and_resolve_session(isolated_home):
    st.clear_active_instance()
    st.bind_session_to_instance("sess-123", "owner/repo")
    assert st.resolve_instance_for_session("sess-123") == "owner-repo"
    assert st.resolve_instance_for_session("nope") is None
    # bind also pins the process.
    assert st.active_instance_slug() == "owner-repo"


def test_auto_resolve_via_env(isolated_home, monkeypatch):
    st.clear_active_instance()
    monkeypatch.setenv("ORCHESTRATOR_INSTANCE", "env/repo")
    assert st.active_instance_slug() == "env-repo"


def test_auto_resolve_via_session_binding(isolated_home, monkeypatch):
    # No explicit instance, no env → fall back to instances.json keyed by the
    # session id reported by current_session_id().
    st.bind_session_to_instance("sess-xyz", "bound/repo")
    st.clear_active_instance()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-xyz")
    assert st.active_instance_slug() == "bound-repo"


# --- flat → per-repo migration -------------------------------------------

def test_flat_state_migrates_to_matching_instance(isolated_home):
    # Seed a legacy flat state.json for owner/repo.
    st.clear_active_instance()
    st.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    (st.ORCHESTRATOR_DIR / "state.json").write_text(
        json.dumps({"version": 3, "repo": "owner/repo", "vision": "legacy", "issues": {}})
    )
    # Binding to that repo migrates the flat file in on first read.
    st.set_active_instance("owner/repo")
    state = st.read()
    assert state["vision"] == "legacy"
    assert (st.ORCHESTRATOR_DIR / "owner-repo" / "state.json").exists()
    assert not (st.ORCHESTRATOR_DIR / "state.json").exists()


def test_flat_state_not_migrated_for_other_repo(isolated_home):
    st.clear_active_instance()
    st.ORCHESTRATOR_DIR.mkdir(parents=True, exist_ok=True)
    (st.ORCHESTRATOR_DIR / "state.json").write_text(
        json.dumps({"version": 3, "repo": "owner/repo", "vision": "legacy", "issues": {}})
    )
    # A different repo must NOT claim the flat file.
    st.set_active_instance("someone/else")
    state = st.read()
    assert state["vision"] == ""  # fresh empty, not the legacy one
    assert (st.ORCHESTRATOR_DIR / "state.json").exists()  # left for its owner


# --- reset_instance -------------------------------------------------------

def test_reset_instance_backs_up_and_preserves_identity(isolated_home):
    st.set_active_instance("owner/repo")
    with st.flock_session() as state:
        state["vision"] = "keep me"
        state["repo"] = "owner/repo"
        state["response_language"] = "en"
        state["current_issue"] = 42
        state["completed_count"] = 7
        st.write_in_lock(state)

    backup = st.reset_instance()
    assert backup is not None and backup.endswith(".json")

    fresh = st.read()
    # Identity preserved.
    assert fresh["vision"] == "keep me"
    assert fresh["repo"] == "owner/repo"
    assert fresh["response_language"] == "en"
    # FSM state cleared.
    assert fresh["current_issue"] is None
    assert fresh["completed_count"] == 0

    # Backup contains the pre-reset snapshot.
    backup_data = json.loads(open(backup).read())
    assert backup_data["current_issue"] == 42
    assert backup_data["completed_count"] == 7


def test_reset_instance_no_state_returns_none(isolated_home):
    st.set_active_instance("owner/repo")
    assert st.reset_instance() is None


# --- orch_stop multi-instance scan ---------------------------------------

def test_find_instance_by_dev_session_picks_right_instance(isolated_home):
    st.set_active_instance("a/x")
    with st.flock_session() as state:
        state["dev_session_id"] = "sess-AAA"
        st.write_in_lock(state)
    st.set_active_instance("b/y")
    with st.flock_session() as state:
        state["dev_session_id"] = "sess-BBB"
        st.write_in_lock(state)

    assert st.find_instance_by_dev_session("sess-AAA") == "a-x"
    assert st.find_instance_by_dev_session("sess-BBB") == "b-y"
    assert st.find_instance_by_dev_session("sess-unknown") is None
    assert st.find_instance_by_dev_session("") is None


def test_find_instance_by_dev_session_includes_flat(isolated_home):
    st.clear_active_instance()  # flat
    with st.flock_session() as state:
        state["dev_session_id"] = "flat-sess"
        st.write_in_lock(state)
    assert st.find_instance_by_dev_session("flat-sess") == ""


def test_iter_instance_slugs(isolated_home):
    st.set_active_instance("a/x")
    st.write(st.read())
    st.set_active_instance("b/y")
    st.write(st.read())
    slugs = set(st.iter_instance_slugs())
    assert {"a-x", "b-y"} <= slugs
