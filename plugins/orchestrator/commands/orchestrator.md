---
description: Autonomous GitHub issue resolution — start, continue, scout, split, force, resume, undo, or stop the orchestrator loop.
argument-hint: "[vision:'<text>'] [repo:owner/repo] [scout:true] [split:N] [resume:N] [force:N] [feedback:N:'<msg>'] [undo:N] [scout_bootstrap_done:true] [stop:true]"
allowed-tools: [Bash, Task, AskUserQuestion, Read, Write, Skill, Agent]
---

Invoke the `orchestrator` skill.

Most invocations take **no args** — the playbook reads `state.json`,
figures out where the FSM is, and advances one or two transitions.

## Argument cheatsheet

| Args | Effect |
|---|---|
| (none) | Continue from current state. Wake reason inferred from transcript. |
| `vision:"<text>"` | Set or overwrite the vision. Prior versions go to `vision_history`. Does not affect the in-flight issue. |
| `repo:owner/repo` | Set the target repo. Required on first call. |
| `scout:true` | Force entry into the scouting cycle even if the backlog is non-empty. |
| `split:N` | Force-split issue #N into sub-issues (analyzer is invoked with `FORCE_SPLIT=true`). |
| `resume:N` | Restore a `parked_awaiting_human` issue to its prior active state. |
| `force:N` | Override an analyzer `should_process=false` decision for issue #N. |
| `feedback:N:"<msg>"` | Append user feedback about PR #N or issue #N. Stored in `feedback_log`; auto-quoted into future analyzer/tester prompts. |
| `undo:N` | Reverse the last N audited mutations (best-effort; merges can't be auto-reverted). |
| `scout_bootstrap_done:true` | Ends the scout-suggested bootstrap window. After this, scout issues skip the human-confirm fast-path (dangerous variants still confirm). |
| `stop:true` | Graceful shutdown: notify teammates, TeamDelete, but leave in-flight PRs alone. |

The playbook itself lives in the `orchestrator` skill — see
`plugins/orchestrator/skills/orchestrator/SKILL.md`.

After running, hand back to the user only if a transition emitted an
AskUserQuestion or SendMessage (those are the natural turn-end triggers).
