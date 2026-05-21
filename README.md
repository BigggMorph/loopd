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

### Troubleshooting

**`ModuleNotFoundError: No module named 'pydantic'` on first run**

`/plugin install` lays down the plugin files but Claude Code marketplaces
have no post-install hook, so loopd's Python deps (`pydantic`,
`pydantic-settings`, `PyYAML`) need a one-time install. The plugin's
`tick` shim will try to handle this automatically on first invocation; if it
fails, run the install manually against the **same interpreter** that
`tick` will use:

```bash
python3 -m pip install --break-system-packages \
  'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'
```

If your system has Homebrew Python and `pip3` resolves to a different
interpreter than `python3`, always invoke `python3 -m pip ...`, not `pip3`.

**Environment overrides**

- `LOOPD_PYTHON` — absolute path to a Python 3.11+ interpreter (e.g.
  pointing at a venv). Defaults to `python3`.
- `LOOPD_SKIP_BOOTSTRAP=1` — disables the auto-install probe entirely. Use
  this when you manage deps yourself (poetry, venv, conda, ...).
- `LOOPD_ROOT` — where loopd stores its state (default `~/.loopd`).

**Upgrading from a pre-fix build with active sessions?** Older builds wrote
session files named `~/.loopd/sessions/cwd-<hash>.json` (keyed by the
working-directory hash, which caused cross-window task hijacking — see
issue #4). The new build keys sessions strictly by the Claude Code session
UUID and ignores any leftover `cwd-*.json` files. You can safely delete
them after upgrading:

```bash
rm ~/.loopd/sessions/cwd-*.json
```

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
