---
name: loopd
description: |
  loopd — synchronous multi-phase dev/research task runner. Runs the
  planning → implementation → review pipeline inside a single Claude Code
  window with workspace-isolated git worktrees. Trigger with /dev-task or
  /research-task; resume interrupted work with /resume-task.
---

# loopd

`loopd` is a Claude Code Plugin that runs a multi-phase dev/research pipeline
**inside Claude Code's own window** using the `Task` tool and FSM-driven hooks
— no separate daemon or subprocess invocation of `claude -p`.

## When to invoke

- `/dev-task "<request>" repo:<owner>/<repo>` — start a new dev task. Spawns a
  worktree at `~/.loopd/workspaces/<task_id>--<owner>__<repo>`, runs
  planning → plan-critic → implementation → solution-critic → review, opens a
  PR.
- `/research-task "<question>" repo:<owner>/<repo> [issue:<n>]` — STORM-style
  4-phase research. Runs research → research-critic (max 2 attempts); posts
  results back to the GitHub issue when `issue:` is supplied.
- `/resume-task <task_id>` — bind the current window to an existing task and
  pick up at the saved next-action.
- `/list-tasks` — show pending / active / waiting_human / completed / failed
  tasks under `~/.loopd/`.

## How it works (one paragraph)

`tick.py` is the deterministic FSM driver. The main LLM in the Claude Code
window is a "thin pump": it copies `next_action.prompt` verbatim into a `Task`
tool call. A `PreToolUse` hook (`pretool-validate.sh`) verifies the prompt
SHA-256 matches what tick emitted (blocks any LLM-side modification). A
`PostToolUse` hook (`posttool-record.sh`) feeds the subagent's result back via
`tick --record`, which advances the FSM. A `Stop` hook (`stop-continue.sh`)
re-invokes `tick`; if the pipeline isn't complete it injects the next action
back into the session so the window continues automatically.

## Operating N windows in parallel

Each task lives in its own worktree and its own session JSON
(`~/.loopd/sessions/<session_id>.json`), so multiple windows can run
independent tasks simultaneously. Limits:

- Each window counts against your Claude plan's concurrent session quota.
- A long-running task occupies that window until completion. Close the window
  to release the seat; resume later with `/resume-task <task_id>`.
- Workspaces under `~/.loopd/workspaces/` are not auto-deleted yet — clean up
  merged ones manually until the cleanup policy lands.

## Verdict contract for critics

`plan-critic`, `solution-critic`, and `research-critic` emit a single-line
JSON tail with `verdict: "approve"` or `verdict: "rework"`. A `rework` verdict
loops the pipeline back to the predecessor (planning / implementation /
research). The hard backward limit is **3 transitions per task**; when hit,
the FSM emits a `checkpoint_human` action with the latest critic feedback so
you can decide manually.

## Recovery

- Window closed mid-task → next window: `/resume-task <task_id>`.
- Stuck on a critic loop → 4th backward transition triggers human checkpoint
  automatically.
- Look at `~/.loopd/sessions/<session_id>.json` for the last `next_action`
  emitted and `~/.loopd/workspaces/<task_id>--*/` for in-flight artifacts.
