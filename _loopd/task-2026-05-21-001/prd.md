# PRD — Fix concurrent CC sessions hijacking each other's loopd task

GitHub issue: BigggMorph/loopd#4
Task: `task-2026-05-21-001`
Status: planning

## 1. Problem statement

When a loopd task is in flight inside one Claude Code (CC) window (window A),
opening **any** other CC window in the same project directory (window B)
causes window B to:

1. Be told by the loopd `Stop` hook that a pipeline is in progress and that it
   must call the next `Task` *verbatim*.
2. Concurrently advance the same FSM through `PostToolUse → tick --record`,
   producing a race against window A.
3. Be blocked from launching unrelated `Task` invocations because
   `PreToolUse` rejects them with `subagent_type mismatch` /
   `prompt_sha256 mismatch`.

The root cause is the **session id fallback** shared by `tick.py` and the
three hooks:

```
sid = LOOPD_SESSION_ID or CLAUDE_SESSION_ID
      or "cwd-" + sha256(cwd)[:16]
```

`/dev-task` invokes `tick init` from a slash-command Bash sub-shell that
**does not propagate** CC's session UUID, so the session file is created as
`~/.loopd/sessions/cwd-<hash>.json` from the very start. Every CC window
sharing the same cwd then resolves to the same loopd session file, since the
hooks fall back to the same cwd-hash when the payload's `session_id` does not
match an existing file.

## 2. Goals

- **G-1.** A loopd pipeline started in window A must never be triggered,
  advanced, or read by hooks in any other CC window — even when both share
  the same project cwd.
- **G-2.** Non-originator windows must remain fully usable for unrelated work
  (manual `Task` calls, normal Stop, normal PreToolUse) without false
  rejections from loopd hooks.
- **G-3.** A single-window dev workflow must continue to work exactly as
  today.
- **G-4.** No new environment-variable plumbing is required from the user
  (no manual `CLAUDE_SESSION_ID` exports, no wrapper script).

## 3. Non-goals

- Cross-window *collaboration* on the same pipeline (e.g. window B picking up
  where window A left off mid-task). Resumption stays opt-in via
  `/resume-task <task_id>`.
- Reworking the FSM, agent matrix, or hook entry-point contracts.
- Migrating in-flight session files written by older builds.

## 4. Functional requirements

### FR-1 — Originator-bound session files

Loopd session state must be keyed by an identifier that is **unique per CC
window**, not by cwd. Concretely:

- Each loopd session file under `~/.loopd/sessions/` must be linked 1:1 with
  the CC session that started the task.
- Hooks must only resolve a session file when the payload's `session_id`
  unambiguously identifies it. There must be no implicit fallback that lets
  any other window find that file.

### FR-2 — Deferred session-file creation when session id is unknown

`tick init` is invoked from a slash-command Bash sub-shell that may not see
CC's UUID. In that case:

- `tick init` must still create the task / worktree and compute the first
  `next_action`.
- `tick init` must **not** write a `cwd-<hash>.json` session file that any
  other window could match.
- The first hook event in the originator window (PreToolUse on the first
  `Task` call, which always carries a real `payload.session_id`) is
  responsible for materialising the loopd session file under that UUID.

### FR-3 — Hooks reject non-originator sessions

Once a session file is bound to a CC UUID:

- All three hooks (`pretool_validate`, `posttool_record`, `stop_continue`)
  must resolve the loopd session **only** by exact match on
  `payload.session_id`.
- If a hook fires in a window whose `payload.session_id` does not match any
  existing session file, it must return success without touching loopd state
  (no `decision: "block"`, no `tick --record`, no `exit 2`).
- In particular `stop_continue` must not emit a `decision: "block"` in any
  window that did not originate the task.

### FR-4 — `tick init` returns the bootstrap signal

`tick init` keeps returning the same JSON shape (`task_id`, `workspace_path`,
`next_action`, `fsm`). When the first PreToolUse fires, the hook must be
able to *re-mint* the session file deterministically from `next_action.task_id`
+ `next_action.validation_token` — i.e. the originator window must be able to
prove "I am the window that just ran `tick init` for this task". The
`validation_token` carried in the first `Task` invocation is sufficient
provided the hook can look up the task and re-derive expected values.

### FR-5 — Backwards-compatible `tick`, `tick --record`, `tick resume`

- `tick` (default subcommand) must continue to find the active task for the
  current CC session.
- `tick --record` must continue to advance the FSM for the active task.
- `tick resume <task_id>` must continue to bind the current CC session to an
  existing task.

In all three cases the lookup must be by CC session UUID, with the loopd
session file under `~/.loopd/sessions/<cc-uuid>.json`.

### FR-6 — Stale / pre-fix session-file tolerance

When the new build encounters a legacy `cwd-<hash>.json` session file in
`~/.loopd/sessions/`, hooks must not adopt it as their own. The file may be
ignored (preferred) or migrated when the originator is unambiguously
identifiable.

### FR-7 — Single-window dev still works

If only one CC window is open in cwd `D`, the user must observe no change in
behaviour:
- `/dev-task` starts a task.
- Hooks loop the pipeline to completion.
- On completion, `stop_continue` deletes the session file.

## 5. Non-functional requirements

### NFR-1 — No new user-visible configuration

The fix must work out of the box with the existing `/dev-task`, `/resume-task`,
`/list-tasks` slash commands. No new env vars required by the user.

### NFR-2 — Determinism / no polling races

The fix must not introduce sleep/poll loops or "wait for hook to claim
session" timing dependencies. The bootstrap path (FR-2 + FR-4) must be a
one-shot, deterministic write triggered by the first hook event.

### NFR-3 — Hook latency budget

The PreToolUse path runs synchronously before every `Task` invocation; total
added latency must remain under ~50 ms in the steady state. The bootstrap
path (first call only) may do slightly more work (one extra file read).

### NFR-4 — Tests

Behavioural changes must be covered by unit tests that exercise:
- Two simulated CC sessions in the same cwd → only the originator advances.
- A bare `cwd-<hash>.json` left behind by an old build → ignored by hooks.
- The single-window happy path → unchanged.

(Project currently has no test harness — see `architecture.md` for the
proposed minimal pytest layout.)

### NFR-5 — No network or external service calls

Pure file-system and process-internal changes only.

## 6. User stories

- **US-1.** As a developer, I open two CC windows in the same project. In
  window A I run `/dev-task`, the loopd pipeline runs to completion in A
  only. Window B is unaffected.
- **US-2.** As a developer, while a loopd task is running in A, I want to
  freely invoke unrelated `Task` sub-agents from window B without
  `subagent_type mismatch` errors.
- **US-3.** As a developer, in window B I want my normal Stop behaviour
  (Claude responds, finishes, stops) without being coerced into running A's
  next pipeline phase.
- **US-4.** As a developer with only one window open, my `/dev-task` flow is
  indistinguishable from before the fix.
- **US-5.** As a developer, I want `/resume-task <task_id>` to continue to
  bind the *current* window to an existing task, even when other windows are
  open in the same cwd.

## 7. Acceptance criteria

- **AC-1.** With windows A and B open in the same cwd: after `/dev-task` in
  A, no Stop hook in B emits `decision: "block"` for the running task.
  `payload.session_id != originator_uuid` → hook is a no-op.
- **AC-2.** With windows A and B as in AC-1: any `Task` call in B (any
  `subagent_type`, any prompt) passes `pretool_validate` without
  `subagent_type mismatch` or `prompt_sha256 mismatch`.
- **AC-3.** With windows A and B as in AC-1: `posttool_record` in B does
  **not** invoke `tick --record` against A's task. A's `turns` list grows
  by exactly one per agent.
- **AC-4.** No session file under `~/.loopd/sessions/` is ever named
  `cwd-*.json` after this change. The directory contains only
  `<cc-uuid>.json` files.
- **AC-5.** Single-window `/dev-task` end-to-end completes (planning →
  plan-critic → implementation → solution-critic → review) without
  regression. Session file is deleted on completion.
- **AC-6.** `/resume-task <task_id>` in a fresh CC window binds the current
  session to the task and produces the expected first `next_action`.
- **AC-7.** Unit tests in `python_core/tests/` covering AC-1..AC-6 pass.
- **AC-8.** Existing tools (`/list-tasks`, `tick state`) continue to work
  read-only without depending on the session file.

## 8. Out-of-scope mitigations considered

- Removing the cwd-hash fallback wholesale and *requiring* `CLAUDE_SESSION_ID`
  to be propagated by the user (suggested fix #3 in the issue). Rejected
  because it violates NFR-1.
- Recording `originator_session_id` inside the existing `cwd-<hash>.json`
  file and gating hooks on it (suggested fix #2). Rejected because the
  cwd-keyed file remains a shared name across windows, so a *future* window
  starting a new task in the same cwd would still collide; and stale
  per-cwd files become a footgun. Going with suggested fix #1 (deferred,
  UUID-keyed session files) per the issue's own recommendation.
