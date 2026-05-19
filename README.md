# loopd

Claude Code Plugin for synchronous multi-phase dev tasks. Run planning → implementation → review in a single Claude Code window with workspace-isolated git worktrees.

Supersedes `oh-my-agents`'s daemon + Slack + Gateway architecture. Removes ~14k LoC of infrastructure by relying on Claude Code's built-in subagents (via the `Task` tool) instead of `claude -p` subprocess calls.

## Install

**Local (development)**:
```
/plugin marketplace add /home/sungjin/Development/loopd
/plugin install loopd@loopd
```

**From GitHub** (once published):
```
/plugin marketplace add BigggMorph/loopd
/plugin install loopd@loopd
```

`/plugin install` expects the form `<plugin-name>@<marketplace-name>`. The marketplace must be added first; both names happen to be `loopd` here.

Requirements: `python3.11+`, `git`, `gh` CLI.

## Usage

```
/dev-task "랜딩 페이지에 가입 폼 추가" repo:BigggMorph/landing-site
```

A new task ID is allocated, a worktree is created at `~/.loopd/workspaces/<task_id>--<owner>__<repo>`, and the pipeline progresses automatically through planning → implementation → review until a PR is opened.

To pick up where a previous window left off:

```
/resume-task task-2026-05-19-001
```

## How it works

- `tick.py` is the deterministic FSM driver. It reads task state, computes the next subagent + prompt, and emits a JSON `next_action` to stdout.
- The main Claude Code LLM is a "thin pump": it copies `next_action.prompt` verbatim into a `Task` tool call.
- `PreToolUse` hook verifies the prompt hash matches what `tick` emitted (blocks any LLM-side modification).
- `PostToolUse` hook records the subagent's result back into `tick --record`, which advances the FSM.
- `Stop` hook re-invokes `tick` and injects the next action if the pipeline hasn't finished, so the window automatically continues.

Storage lives under `~/.loopd/` (separate from `~/.oh-my-agents/`).
