<p align="center">
  <img src="image.png" width="120" alt="loopd"/>
</p>

<h1 align="center">loopd</h1>
<p align="center">
  Synchronous multi-phase dev tasks for Claude Code
</p>

<p align="center">
  <b>English</b> |
  <a href="README.zh-CN.md">简体中文</a> |
  <a href="README.ko.md">한국어</a>
</p>

<p align="center">
  <a href="https://bigggmorph.com/"><b>bigggmorph.com</b></a>
</p>

---

**loopd** is a Claude Code plugin that runs **planning → implementation → review** as a single, automatic pipeline inside one Claude Code window, with each task isolated in its own git worktree.

It relies on Claude Code's built-in subagents (via the `Task` tool) instead of `claude -p` subprocess calls — so there is **no daemon, no Slack bridge, no gateway** to operate.

## Features

- **Single-window pipeline.** Planning → implementation → review runs to completion automatically inside one Claude Code window. No background daemons, no Slack bridge, no gateway service.
- **Worktree isolation per task.** Each task gets its own git worktree at `~/.loopd/workspaces/<task_id>--<owner>__<repo>` and its own branch `loopd/<task_id>`.
- **Deterministic FSM driver.** `tick.py` decides the next subagent + prompt; the main LLM is a "thin pump" that just copies it into a `Task` call. Prompts can't silently drift.
- **Critic stages built in.** Plan-critic, solution-critic, and research-critic gate each phase before moving on.
- **Two pipelines.** Dev (planning → impl → review → PR) and Research (STORM 4-phase → optional GitHub issue post).
- **Resumable.** Walk away and pick up later from any window with `/resume-task`.
- **Multi-window safe.** Run `/dev-task` in several windows at once — task IDs, worktrees, and branches are allocated under a single-writer lock.

## Install

```
/plugin marketplace add BigggMorph/loopd
/plugin install loopd@loopd
```

`/plugin install` expects the form `<plugin-name>@<marketplace-name>`. The marketplace must be added first; both names happen to be `loopd` here.

Requirements: `python3.11+`, `git`, `gh` CLI.

## Usage

Start a new dev task:

```
/dev-task "Add a signup form to the landing page" repo:BigggMorph/landing-site
```

A new task ID is allocated, a worktree is created at `~/.loopd/workspaces/<task_id>--<owner>__<repo>`, and the pipeline progresses automatically through planning → implementation → review until a PR is opened.

To pick up where a previous window left off:

```
/resume-task task-2026-05-19-001
```

## Commands

| Command | What it does |
| --- | --- |
| `/dev-task "<goal>" repo:<owner>/<repo>` | Start a new dev task. Runs planning → implementation → review → PR. |
| `/research-task "<topic>"` | Start a new research task. Runs STORM 4-phase research and (optionally) posts the result to a GitHub issue. |
| `/resume-task <task_id>` | Resume an interrupted task in the current window. |
| `/list-tasks` | List known tasks grouped by status (read-only). |

## Architecture

```
   /dev-task
       │
       ▼
┌──────────────┐    next_action      ┌──────────────┐
│   tick.py    │ ──────────────────► │   Main LLM   │
│  (FSM driver)│       (JSON)        │ (thin pump)  │
└──────▲───────┘                     └──────┬───────┘
       │                                    │ copies prompt
       │  tick --record                     │ into Task tool
       │  (PostToolUse hook)                ▼
       │                             ┌──────────────┐
       │  hash check (PreToolUse) ───┤  Task tool   │
       │                             └──────┬───────┘
       │                                    │
       │                                    ▼
       │                             ┌──────────────┐
       └─────────────────────────────┤   Subagent   │
                                     │  planning /  │
   Stop hook re-invokes tick  ◄──────│  impl /      │
   when the LLM stops                │  review /... │
                                     └──────────────┘

   State on disk:  ~/.loopd/
                   ├── tasks/<task_id>.json
                   ├── sessions/<session_uuid>.json
                   └── workspaces/<task_id>--<owner>__<repo>/
                                  (git worktree on branch loopd/<task_id>)
```

**Pipelines**

- **Dev:** `planning → plan-critic → implementation → solution-critic → review → PR`
- **Research:** `research → research-critic → (optional) gh-post to a GitHub issue`

**Key pieces**

- `tick.py` — deterministic FSM driver. Reads task state, computes the next subagent + prompt, and emits a JSON `next_action` to stdout.
- **Main Claude Code LLM** — a "thin pump": copies `next_action.prompt` verbatim into a `Task` tool call. It does not pick the next step or rewrite the prompt.
- **`PreToolUse` hook** — verifies the prompt hash matches what `tick` emitted, blocking any LLM-side modification.
- **`PostToolUse` hook** — records the subagent's result back into `tick --record`, which advances the FSM.
- **`Stop` hook** — re-invokes `tick` and injects the next action if the pipeline hasn't finished, so the window automatically continues.
- **Subagents** (`plugins/loopd/agents/*.md`) — `planning`, `plan-critic`, `implementation`, `solution-critic`, `review`, `research`, `research-critic`, `gh-post`.

All state lives under `~/.loopd/`.

### Multi-window safety

Opening multiple Claude Code windows and running `/dev-task` in each at the same time is supported. Each task is allocated a unique `task_id` under a single-writer lock, a separate worktree at `~/.loopd/workspaces/<task_id>--<owner>__<repo>`, and a separate branch `loopd/<task_id>`. If a worktree or branch with the same ID already exists, loopd refuses to overwrite it rather than silently destroying in-flight work.

## Troubleshooting

**`ModuleNotFoundError: No module named 'pydantic'` on first run**

`/plugin install` lays down the plugin files but Claude Code marketplaces have no post-install hook, so loopd's Python deps (`pydantic`, `pydantic-settings`, `PyYAML`) need a one-time install. The plugin's `tick` shim will try to handle this automatically on first invocation; if it fails, run the install manually against the **same interpreter** that `tick` will use:

```bash
python3 -m pip install --break-system-packages \
  'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'
```

If your system has Homebrew Python and `pip3` resolves to a different interpreter than `python3`, always invoke `python3 -m pip ...`, not `pip3`.

**Environment overrides**

- `LOOPD_PYTHON` — absolute path to a Python 3.11+ interpreter (e.g. pointing at a venv). Defaults to `python3`.
- `LOOPD_SKIP_BOOTSTRAP=1` — disables the auto-install probe entirely. Use this when you manage deps yourself (poetry, venv, conda, ...).
- `LOOPD_ROOT` — where loopd stores its state (default `~/.loopd`).

**Upgrading from a pre-fix build with active sessions?** Older builds wrote session files named `~/.loopd/sessions/cwd-<hash>.json` (keyed by the working-directory hash, which caused cross-window task hijacking — see issue #4). The new build keys sessions strictly by the Claude Code session UUID and ignores any leftover `cwd-*.json` files. You can safely delete them after upgrading:

```bash
rm ~/.loopd/sessions/cwd-*.json
```

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

## License

[MIT](LICENSE)
