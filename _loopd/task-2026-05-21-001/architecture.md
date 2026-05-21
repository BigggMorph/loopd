# Architecture — UUID-keyed session files (no cwd-hash fallback)

## 1. Current behaviour (recap)

### Components

- `python_core/loopd_core/tick.py`
  - `_session_id()` — picks `LOOPD_SESSION_ID | CLAUDE_SESSION_ID | cwd-<hash>`.
  - `_session_file()` — returns `~/.loopd/sessions/<sid>.json`.
  - `cmd_init()` — creates task + worktree, calls `_persist_session_for_action()`
    which writes that session file immediately.
  - `cmd_tick()`, `cmd_record()`, `cmd_resume()` — read/write the same file.
- `plugins/loopd/hooks/pretool_validate.py`,
  `posttool_record.py`, `stop_continue.py`
  - Each defines its own `_resolve_session(payload_session_id)` with the same
    cwd-hash fallback when the UUID doesn't match an existing file.

### Failure path

```
window A: /dev-task
  → slash-cmd bash sub-shell runs `tick init`
  → CLAUDE_SESSION_ID NOT exported into sub-shell
  → _session_id() returns "cwd-<hash>"
  → ~/.loopd/sessions/cwd-<hash>.json written

window B: any interaction
  → Stop hook fires with payload.session_id = B's UUID
  → ~/.loopd/sessions/<B-uuid>.json does not exist
  → fallback to cwd-<hash> → matches window A's file
  → tick runs against A's task → emits decision:"block" to B
  → B's LLM is coerced into calling the next Task
  → race on the FSM
```

## 2. Target architecture

### Design choice: suggested fix #1 from issue #4

> **Bind sessions to CC's session UUID, not cwd.** Defer creation of the
> loopd session file until the first hook event (which always carries the
> real `payload.session_id`).

The two-window leak is eliminated by removing the shared key entirely.
Window B simply has no `~/.loopd/sessions/<B-uuid>.json`, and the hooks
no longer fall back to anything cwd-derived.

### Bootstrap flow

```
window A: /dev-task
  1. slash-cmd bash sub-shell runs `tick init --args ...`
  2. tick init:
       - creates Task + workspace as today
       - computes next_action with validation_token (HMAC over task_id, iter,
         subagent, prompt_sha256)
       - writes a *pending claim file*:
           ~/.loopd/sessions/.pending/<task_id>.json
         containing { "task_id", "validation_token", "next_action" }
       - returns the existing JSON shape on stdout (unchanged)
  3. main LLM copies next_action into a Task tool invocation

  4. PreToolUse hook fires (carries payload.session_id = A's UUID):
       - reads payload.tool_input.validation_token
       - finds the matching .pending/<task_id>.json
       - validates that validation_token + prompt SHA match
       - atomically renames .pending/<task_id>.json
           → ~/.loopd/sessions/<A-uuid>.json
       - normal validation proceeds (subagent_type + prompt SHA)

  5. PostToolUse hook fires:
       - resolves session via <A-uuid>.json (now exists)
       - tick --record under env LOOPD_SESSION_ID=<A-uuid>
       - tick writes back to <A-uuid>.json

  6. Stop hook fires:
       - resolves session via <A-uuid>.json
       - tick → invoke_subagent for next phase
       - emits decision:"block" to A only

window B: any interaction
  - all hooks compute payload.session_id = B's UUID
  - ~/.loopd/sessions/<B-uuid>.json does not exist
  - .pending/<task_id>.json exists but only matches by validation_token
    which window B has never seen
  - hooks early-return → no block, no record, no validation
  - B can freely call unrelated Tasks
```

### Atomicity guarantees

- `tick init` writes `.pending/<task_id>.json.tmp` then `os.replace()` →
  atomic on POSIX.
- PreToolUse claim is `os.replace(.pending/<task_id>.json,
  sessions/<uuid>.json)` → atomic, so even if two PreToolUse fired
  simultaneously, only one succeeds and the second sees ENOENT and falls
  through to the no-op path.
- A `tick init` followed by a window crash leaves the `.pending/<task_id>.json`
  orphaned. We add a tolerant cleanup: the pending file carries a
  `created_at` ISO timestamp, and any pending file older than 24 h is
  considered stale and ignored / deletable.

### Backwards compatibility

- Existing `cwd-<hash>.json` files left behind by older builds are
  *ignored* by the new hooks (they look up by `<uuid>.json` only). They are
  not deleted automatically to avoid surprising in-flight pre-upgrade tasks
  on the user's machine, but the new behaviour neither reads nor writes
  them. A one-line note added to README's Troubleshooting suggests
  `rm ~/.loopd/sessions/cwd-*.json` after upgrade if desired.

## 3. Files added / modified

### Modified

| File | Change |
|---|---|
| `plugins/loopd/python_core/loopd_core/tick.py` | New `_session_paths()` helper exposing `sessions_dir`, `pending_dir`. `_session_id()` retained but only as the *write* side for UUID-keyed files (`LOOPD_SESSION_ID` env required). `_session_file()` raises a typed error when no UUID is available. `cmd_init()` writes a pending claim under `.pending/<task_id>.json` instead of a session file. `_persist_session_for_action()` becomes UUID-required for tick/record/resume; init uses a new `_persist_pending_for_action()`. New helper `claim_pending_session(task_id, validation_token, sid)` used by the hook. |
| `plugins/loopd/hooks/pretool_validate.py` | `_resolve_session()` drops the cwd-hash fallback. When the existing UUID session file is absent, attempts to *claim* a pending file using the incoming `validation_token`; on success, continues with normal validation. Otherwise returns 0 (no-op). |
| `plugins/loopd/hooks/posttool_record.py` | `_resolve_session()` drops the cwd-hash fallback. If no UUID session file exists, returns 0. (No claim attempt here — the claim must happen in PreToolUse first.) |
| `plugins/loopd/hooks/stop_continue.py` | `_resolve_session()` drops the cwd-hash fallback. If no UUID session file exists, returns 0 — no `decision: "block"` emitted. |
| `plugins/loopd/python_core/loopd_core/config.py` | New property `pending_sessions_path = sessions_path / ".pending"`. `ensure_directories()` mkdirs it. |
| `plugins/loopd/commands/dev-task.md` | No behavioural change required, but add a short note under "Step 1" explaining that the session file is created on first Task invocation (so users debugging see the timing). |

### Added

| File | Purpose |
|---|---|
| `plugins/loopd/python_core/loopd_core/session_store.py` | New tiny module that centralises the three concepts: `session_path_for(sid)`, `pending_path_for(task_id)`, `claim_pending(task_id, validation_token, sid) -> Optional[Path]`, `read_session(sid)`, `write_session(sid, data)`, `delete_session(sid)`. Imported by `tick.py` and the three hooks so the rule lives in exactly one place. |
| `plugins/loopd/python_core/tests/__init__.py` | Empty — marks pytest package. |
| `plugins/loopd/python_core/tests/test_session_store.py` | Unit tests for: pending creation, claim by validation token, two windows can't both claim, stale cwd-hash file is ignored, normal read/write/delete round-trip. |
| `plugins/loopd/python_core/tests/test_hooks_isolation.py` | Sub-process tests that pipe synthetic `Stop` / `PreToolUse` payloads with different `session_id`s into the hook scripts and assert isolated behaviour. |

### Untouched (intentionally)

- `state/task_manager.py`, `state/pipeline_state_machine.py`,
  `agents/pipeline.py` — task-level state is unaffected; only the
  CC-session-to-task binding changes.
- `commands/list-tasks.md`, `commands/resume-task.md` — they don't touch
  the session file.
- `tick state <task_id>` — already session-agnostic (looks up by task_id).

## 4. Data shapes

### `~/.loopd/sessions/.pending/<task_id>.json`

```json
{
  "task_id": "task-2026-05-21-001",
  "validation_token": "v1.task-2026-05-21-001.iter-1.planning.<sig>",
  "next_action": { ... whole next_action payload ... },
  "created_at": "2026-05-21T01:23:45Z"
}
```

### `~/.loopd/sessions/<cc-uuid>.json`

Unchanged shape from today:

```json
{
  "task_id": "task-2026-05-21-001",
  "last_next_action": { ... }
}
```

Created by the PreToolUse claim, mutated by `tick --record` / `tick resume`.

## 5. Module dependency diagram

```
                tick.py
                   │
            session_store.py
            (sessions/, .pending/)
                   ▲
   ┌───────────────┼───────────────┐
pretool_validate  posttool_record  stop_continue
        (all hooks import session_store)
```

Today each hook re-implements its own `_resolve_session` — after this
change, all four call sites (tick + three hooks) go through
`session_store`, eliminating the three drifted copies that all hard-coded
the cwd-hash fallback.

## 6. Risk analysis

| Risk | Likelihood | Mitigation |
|---|---|---|
| First `Task` invocation racing two PreToolUse claims (e.g. user double-clicks) | low | `os.replace` makes the claim atomic; the loser sees ENOENT and is a no-op. The legitimate winner is whichever CC window actually invokes the Task. |
| Claim file leaks if `tick init` succeeds but user closes the window before invoking Task | medium | New `cleanup_stale_pending(ttl=24h)` called by `tick init` and `tick state` at startup. |
| Old build still running mid-task during upgrade reads new `.pending/` layout | low | The old build only knows about cwd-hash files; it never looks under `.pending/`. New files coexist with old. |
| Hooks fire with empty `payload.session_id` (unknown CC variant) | low | Falls into the no-op path — strictly safer than today's cross-window leak. |
| `validation_token` is exposed to other CC windows somehow (e.g. screen sharing) and they replay it | very low | Token is single-use claim; once claimed, the pending file is gone. Replay against an already-claimed session uses normal `verify_token` which compares against `last_next_action.validation_token` and rejects stale tokens. |
