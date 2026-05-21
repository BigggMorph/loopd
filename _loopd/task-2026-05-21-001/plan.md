# Implementation plan — UUID-keyed session files

Step list is ordered for direct execution by the `implementation` subagent.
All paths are relative to the workspace root:
`/home/sungjin/.loopd/workspaces/task-2026-05-21-001--BigggMorph__loopd`.

## Step 0 — Sanity check

```bash
git status
ls plugins/loopd/python_core/loopd_core
```

Expected: clean tree on `main`, package contains `tick.py`, `config.py`,
`types.py`, `agents/`, `state/`.

## Step 1 — Add `pending_sessions_path` to config

File: `plugins/loopd/python_core/loopd_core/config.py`.

- Add a property on `Config`:

  ```python
  @property
  def pending_sessions_path(self) -> Path:
      return self.sessions_path / ".pending"
  ```

- In `ensure_directories()`, after `self.sessions_path.mkdir(...)`, also
  `self.pending_sessions_path.mkdir(parents=True, exist_ok=True)`.

## Step 2 — Create `session_store.py`

New file: `plugins/loopd/python_core/loopd_core/session_store.py`.

Required functions (small, pure, no global state):

```python
def session_path_for(sid: str) -> Path
def pending_path_for(task_id: str) -> Path
def read_session(sid: str) -> dict
def write_session(sid: str, data: dict) -> None
def delete_session(sid: str) -> None
def write_pending(task_id: str, payload: dict) -> Path
def claim_pending(task_id: str, validation_token: str, sid: str) -> Optional[Path]
def cleanup_stale_pending(ttl_seconds: int = 86400) -> int
```

Behaviour:

- `session_path_for(sid)` raises `ValueError` if `sid` is empty or starts
  with `"cwd-"` (defensive — the new code never produces those, but legacy
  callers must blow up loudly rather than silently re-creating the bug).
- `write_session` / `write_pending` use the `.tmp` + `os.replace` pattern
  already in `tick.py`.
- `claim_pending` reads `.pending/<task_id>.json`, verifies
  `payload["validation_token"] == validation_token` with `hmac.compare_digest`,
  and `os.replace`s it to `<sid>.json`. Returns the new path on success,
  `None` if the pending file does not exist or token mismatches.
- `cleanup_stale_pending` iterates `.pending/*.json`, parses
  `created_at` (ISO 8601), unlinks files older than `ttl_seconds`. Returns
  the count of files deleted. Robust to malformed/missing timestamps
  (just skip).

Module-private helper:

```python
def _sessions_dir() -> Path:
    from loopd_core.config import get_config
    cfg = get_config()
    cfg.sessions_path.mkdir(parents=True, exist_ok=True)
    cfg.pending_sessions_path.mkdir(parents=True, exist_ok=True)
    return cfg.sessions_path
```

## Step 3 — Refactor `tick.py` to use `session_store`

File: `plugins/loopd/python_core/loopd_core/tick.py`.

Changes:

1. Replace the body of `_session_id()` with:

   ```python
   def _session_id() -> Optional[str]:
       return os.environ.get("LOOPD_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID") or None
   ```

   (Returns `None` instead of `"cwd-…"`. Callers must handle `None`.)

2. Remove `_session_file()`, `_read_session()`, `_write_session()`,
   `_delete_session()` — replace internal call sites with
   `session_store.read_session(_session_id())` etc., guarded so that a
   missing `_session_id()` is a typed error in commands that *require* a
   session (record, tick, resume), and is a no-op for commands that don't
   need one.

3. `_persist_session_for_action(task_id, next_action)` gains two code paths:

   ```python
   sid = _session_id()
   if sid:
       session_store.write_session(sid, {
           "task_id": task_id,
           "last_next_action": next_action,
       })
   else:
       # No CC UUID available — happens during `tick init` from a slash-cmd
       # bash sub-shell. Defer claim to the first hook event.
       session_store.write_pending(task_id, {
           "task_id": task_id,
           "validation_token": next_action.get("validation_token"),
           "next_action": next_action,
           "created_at": _now_iso(),
       })
   ```

   Add a small `_now_iso()` helper if not already present (reuse one from
   `state.task_manager` would import too much — just inline it).

4. `cmd_init()`: at the start, call
   `session_store.cleanup_stale_pending(86400)` defensively. Otherwise its
   only contact with sessions remains the call to
   `_persist_session_for_action`, which now picks pending vs uuid
   automatically.

5. `cmd_tick()`: if `_session_id()` is None, return
   `_emit_error("no active loopd task — run /dev-task first", exit_code=2)`
   (same message as today's "session file empty" path).

6. `cmd_record()`: requires a UUID. If `_session_id()` is None, error
   out: `"--record requires a Claude Code session id; ensure the hook
   exports LOOPD_SESSION_ID"`.

7. `cmd_resume()`: requires a UUID. Same error message.

8. After `cmd_record()` finishes the pipeline and the next_action is
   `"complete"`, call `session_store.delete_session(sid)` (replacing
   today's `_delete_session()`).

## Step 4 — Centralised `_resolve_session` in the three hooks

The three hook files currently each contain the same `_resolve_session`
function with the cwd-hash fallback. Refactor:

1. `plugins/loopd/hooks/_loopd_hook_lib.py` (new file). Tiny helper:

   ```python
   from __future__ import annotations
   import os
   import sys
   from pathlib import Path

   # The hooks ship under plugins/loopd/hooks/ but import paths to
   # loopd_core need python_core/ on sys.path. CLAUDE_PLUGIN_ROOT is set
   # by Claude Code; fall back to walking from __file__.
   def ensure_loopd_core_importable() -> None:
       env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
       if env_root:
           candidate = Path(env_root) / "python_core"
       else:
           candidate = Path(__file__).resolve().parent.parent / "python_core"
       p = str(candidate)
       if p not in sys.path:
           sys.path.insert(0, p)
   ```

   This avoids each hook re-implementing import-path gymnastics.

2. In each of `pretool_validate.py`, `posttool_record.py`,
   `stop_continue.py`:

   - At the top of the file, after stdlib imports:

     ```python
     sys.path.insert(0, str(Path(__file__).parent))
     from _loopd_hook_lib import ensure_loopd_core_importable
     ensure_loopd_core_importable()
     from loopd_core import session_store
     ```

     (Hooks already run under the bundled Python; `session_store` resolves
     fine after `PYTHONPATH` is fixed.)

   - Replace each `_resolve_session(payload_session_id)` with:

     ```python
     def _resolve_session(payload_session_id: str):
         if not payload_session_id:
             return None, None
         path = session_store.session_path_for(payload_session_id)
         if path.exists():
             return payload_session_id, path
         return None, None
     ```

     **Crucially: no cwd-hash fallback.**

## Step 5 — Add claim step to `pretool_validate.py`

File: `plugins/loopd/hooks/pretool_validate.py`.

After `_resolve_session()` is called in `main()`:

```python
resolved_sid, session_file = _resolve_session(payload.get("session_id") or "")

# Bootstrap: first Task invocation after `tick init`. The session file
# doesn't exist yet but a pending claim may. Try to claim it using the
# validation_token the caller is about to use.
if session_file is None and payload.get("tool_name") in ("Task", "Agent"):
    tool_input = payload.get("tool_input") or {}
    actual_prompt = tool_input.get("prompt", "")
    # validation_token isn't a Task-tool field, so we recover it from the
    # pending claim file via the task_id-less heuristic: scan .pending/
    # and try every claim whose prompt_sha256 matches the actual prompt.
    import hashlib
    actual_hash = hashlib.sha256(actual_prompt.encode()).hexdigest()
    sid = payload.get("session_id") or ""
    claimed = session_store.claim_pending_by_prompt_hash(actual_hash, sid)
    if claimed is not None:
        session_file = claimed
        resolved_sid = sid

if session_file is None:
    return 0
```

This requires one extra helper in `session_store`:

```python
def claim_pending_by_prompt_hash(prompt_sha256: str, sid: str) -> Optional[Path]:
    """Find a pending claim whose next_action.prompt_sha256 matches and
    atomically rename it to sessions/<sid>.json. Returns the new path or
    None.
    """
    for p in _pending_dir().glob("*.json"):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        na = data.get("next_action") or {}
        if na.get("prompt_sha256") != prompt_sha256:
            continue
        target = session_path_for(sid)
        try:
            # Re-write target with the full session payload, then unlink
            # the pending claim. Atomic via tmp+rename.
            session_payload = {
                "task_id": data["task_id"],
                "last_next_action": na,
            }
            write_session(sid, session_payload)
            p.unlink()
        except FileNotFoundError:
            return None
        return target
    return None
```

Note: we don't use `os.replace(pending, sessions/<sid>)` directly because
the file *contents* differ (pending file has extra fields). Instead we
write the canonical session shape and unlink the pending. If a second
window's PreToolUse races and tries to claim the same pending, the first
to `unlink` wins; the second sees `FileNotFoundError` and is a no-op.
Acceptable because both windows would be invoking the same Task with the
same validation_token — only one will be the originator anyway.

Race-safety hardening: guard the whole claim with a per-task lock file
under `~/.loopd/sessions/.pending/<task_id>.lock` via `fcntl.flock` on
Linux. Skip on platforms without flock (best-effort).

## Step 6 — `posttool_record.py` and `stop_continue.py` — no bootstrap

These two hooks do **not** bootstrap a session — bootstrap only happens on
the first PreToolUse, which always precedes them. If they fire and find no
session file, they must early-return.

Concretely: after the centralised `_resolve_session` change in Step 4, no
further changes are needed in these two hooks; the cwd-hash fallback is
gone, and `_resolve_session` returns `(None, None)` whenever the current
window is not the originator. Verify both `main()` functions early-return
in that case (today they already do, just via a different code path).

## Step 7 — Migration note in README

File: `README.md`. Add to the Troubleshooting section:

> **Upgrading from a pre-0.1.1 build with active sessions?** Files named
> `~/.loopd/sessions/cwd-*.json` left behind by older builds are ignored
> by the new hooks. You can safely delete them with
> `rm ~/.loopd/sessions/cwd-*.json`.

(One paragraph; respects the "no proactive .md files" rule because
README already exists.)

## Step 8 — Tests

Project has no pytest harness yet. Add minimal scaffolding:

1. `plugins/loopd/python_core/pyproject.toml`: under `[project.optional-dependencies]`
   add `dev = ["pytest>=7"]`. (Do not touch core deps.)

2. `plugins/loopd/python_core/tests/__init__.py` — empty file.

3. `plugins/loopd/python_core/tests/conftest.py`:

   ```python
   import os, tempfile, pathlib, pytest

   @pytest.fixture(autouse=True)
   def isolated_loopd_root(monkeypatch, tmp_path):
       monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))
       # Drop the lru_cache so config is re-read per test
       from loopd_core import config as cfg_mod
       cfg_mod.get_config.cache_clear()
       yield
       cfg_mod.get_config.cache_clear()
   ```

4. `plugins/loopd/python_core/tests/test_session_store.py`. Cases:

   - `test_session_path_for_rejects_empty_sid`
   - `test_session_path_for_rejects_cwd_prefix`
   - `test_write_and_read_session_round_trip`
   - `test_write_pending_creates_file_with_created_at`
   - `test_claim_pending_moves_to_session_file`
   - `test_claim_pending_returns_none_for_wrong_token` (uses
     `claim_pending(task_id, token, sid)` direct token path)
   - `test_claim_pending_by_prompt_hash_matches_by_sha`
   - `test_claim_pending_by_prompt_hash_no_match_returns_none`
   - `test_cleanup_stale_pending_deletes_old_files`
   - `test_cleanup_stale_pending_skips_fresh_files`

5. `plugins/loopd/python_core/tests/test_tick_session_binding.py`. Cases:

   - `test_init_writes_pending_when_no_session_id` (unset
     `LOOPD_SESSION_ID` / `CLAUDE_SESSION_ID`, run `cmd_init` for a fake
     prompt + repo, assert `.pending/<task_id>.json` exists and no
     `sessions/<sid>.json` exists).
   - `test_init_writes_session_when_session_id_present` (set
     `LOOPD_SESSION_ID=fake-uuid`, run `cmd_init`, assert
     `sessions/fake-uuid.json` exists and `.pending/` is empty).
   - `test_tick_command_errors_without_session_id`.

   For tests that require `cmd_init` to actually create a worktree, stub
   `WorkspaceManager.setup_task_workspace` via monkeypatch to return a
   temp dir rather than performing real git work.

6. `plugins/loopd/python_core/tests/test_hooks_isolation.py`. Cases use
   `subprocess.run(["python3", "<hook>.py"], input=json.dumps(payload))`:

   - `test_stop_hook_noop_when_no_session_file`: payload has
     `session_id="window-B-uuid"`, no session file exists → stdout empty,
     exit 0.
   - `test_stop_hook_blocks_when_session_file_exists`: pre-create
     `sessions/window-A-uuid.json` referencing a real task, payload has
     `session_id="window-A-uuid"` → stdout JSON contains
     `"decision":"block"`.
   - `test_pretool_hook_noop_when_no_session_and_no_pending`: payload has
     `session_id="window-B-uuid"`, no pending → exit 0, no stderr.
   - `test_pretool_hook_claims_pending`: write a `.pending/<task_id>.json`,
     fire PreToolUse with `session_id="window-A-uuid"` and a matching
     prompt, assert `sessions/window-A-uuid.json` now exists, `.pending/`
     is empty, exit 0.
   - `test_pretool_hook_rejects_when_subagent_mismatch_after_claim`:
     claim succeeds but `subagent_type` differs from claim's value → exit 2.

   These tests must set `CLAUDE_PLUGIN_ROOT` so the import-path helper
   resolves correctly, and must set `LOOPD_ROOT` to a temp path.

## Step 9 — Smoke-test the slash command flow

Manual repro (document in commit message body, not a separate file):

1. `cd <a real workspace>`
2. Window A: `claude` → `/dev-task "echo hi" repo:BigggMorph/loopd`.
   Wait until planning subagent runs.
3. Window B: `claude` in the same cwd. Ask "what's 2+2?". Verify Stop hook
   does **not** inject the loopd block message.
4. In window B, run an unrelated `Task`. Verify no
   `subagent_type mismatch` exit.
5. Wait for window A to complete. Verify
   `~/.loopd/sessions/` is empty (or contains only stale `cwd-*.json` from
   the previous bug, which the new build ignores).

## Step 10 — Commit

```bash
git add plugins/loopd/python_core/loopd_core/session_store.py \
        plugins/loopd/python_core/loopd_core/tick.py \
        plugins/loopd/python_core/loopd_core/config.py \
        plugins/loopd/hooks/pretool_validate.py \
        plugins/loopd/hooks/posttool_record.py \
        plugins/loopd/hooks/stop_continue.py \
        plugins/loopd/hooks/_loopd_hook_lib.py \
        plugins/loopd/python_core/pyproject.toml \
        plugins/loopd/python_core/tests/ \
        README.md
git commit -m "fix: bind loopd sessions to CC UUID, remove cwd-hash fallback"
```

Commit message body should reference issue #4 and summarise the
suggested-fix-#1 approach.

## Step 11 — Hand-off to review

Verify with `git log -1 --stat` that all artifacts are committed and the
diff is bounded to the files listed in `architecture.md` § 3.
