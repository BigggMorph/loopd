# loopd

Claude Code Plugin for synchronous multi-phase dev tasks. Run planning → implementation → review in a single Claude Code window with workspace-isolated git worktrees.

Relies on Claude Code's built-in subagents (via the `Task` tool) instead of `claude -p` subprocess calls, so no daemon, Slack bridge, or gateway is needed.

## Install

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

Storage lives under `~/.loopd/`.

## Updating

End users:

```
/plugin update loopd
```

Claude Code decides "is there a new version?" by reading the `version` field in `plugins/loopd/.claude-plugin/plugin.json` — **not** git commits or tags. So pushing new commits without bumping that field has no effect. If you need to force-refresh the same version:

```
/plugin uninstall loopd
/plugin install loopd@loopd
```

## Releasing (maintainers)

Versioning is driven by GitHub Releases via [`.github/workflows/sync-plugin-version.yml`](.github/workflows/sync-plugin-version.yml):

1. Publish a GitHub Release with a semver tag (e.g. `v0.2.0` or `0.2.0`).
2. The workflow strips the leading `v`, writes the version into `plugins/loopd/.claude-plugin/plugin.json`, and commits back to `main` as `github-actions[bot]`.
3. End users get the new version on their next `/plugin update loopd`.

Notes:

- The release tag must match semver (`X.Y.Z`, optionally with `-pre` / `+build` suffix). Non-semver tags fail the workflow.
- The release event only fires on release creation, so the bot-pushed commit will not retrigger the workflow — no infinite loop.
- If `plugin.json` is already at the target version (e.g. you bumped it manually in the same PR that became the release), the workflow no-ops.
