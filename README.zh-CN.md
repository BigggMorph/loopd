<p align="center">
  <img src="image.png" width="120" alt="loopd"/>
</p>

<h1 align="center">loopd</h1>
<p align="center">
  为 Claude Code 提供同步式多阶段开发任务
</p>

<p align="center">
  <a href="README.md">English</a> |
  <b>简体中文</b> |
  <a href="README.ko.md">한국어</a>
</p>

<p align="center">
  <a href="https://bigggmorph.com/"><b>bigggmorph.com</b></a>
</p>

---

**loopd** 是一个 Claude Code 插件，在同一个 Claude Code 窗口内将 **planning（规划）→ implementation（实现）→ review（评审）** 作为一条自动化流水线串起来运行，每个任务都隔离在独立的 git worktree 中。

它直接利用 Claude Code 内置的 subagent（通过 `Task` 工具）来驱动每一阶段，而不是通过 `claude -p` 子进程调用，因此**无需守护进程、无需 Slack 桥接、无需 gateway**。

## 特性

- **单窗口流水线。** Planning → implementation → review 在同一个 Claude Code 窗口内自动跑到完成。没有后台守护进程、没有 Slack 桥接、没有 gateway 服务。
- **每个任务一个 worktree。** 任务有独立的 git worktree（`~/.loopd/workspaces/<task_id>--<owner>__<repo>`）和独立的分支 `loopd/<task_id>`。
- **确定性 FSM 驱动器。** `tick.py` 决定下一个 subagent + prompt；主 LLM 只是把它原样复制到一次 `Task` 调用里的"薄泵"。prompt 不会被悄悄改写。
- **内置 critic 阶段。** plan-critic、solution-critic、research-critic 在每个阶段之间把关。
- **两套流水线。** Dev（planning → impl → review → PR）和 Research（STORM 4 阶段 → 可选 GitHub issue 评论）。
- **可恢复。** 离开后用 `/resume-task` 在任意窗口里继续。
- **多窗口安全。** 同时在多个窗口里跑 `/dev-task` ——task ID、worktree、分支都在 single-writer 锁下分配。

## 安装

```
/plugin marketplace add BigggMorph/loopd
/plugin install loopd@loopd
```

`/plugin install` 的格式为 `<插件名>@<市场名>`，必须先添加 marketplace。这里两个名字恰好都是 `loopd`。

依赖要求：`python3.11+`、`git`、`gh` CLI。

## 使用

启动一个新的开发任务：

```
/dev-task "在落地页加上注册表单" repo:BigggMorph/landing-site
```

loopd 会分配一个新的 task ID，在 `~/.loopd/workspaces/<task_id>--<owner>__<repo>` 创建 worktree，然后自动推进 planning → implementation → review 流水线，直到打开一个 PR。

要在另一个窗口里继续之前未完成的任务：

```
/resume-task task-2026-05-19-001
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `/dev-task "<目标>" repo:<owner>/<repo>` | 启动一个新的 dev 任务，跑 planning → implementation → review → PR。 |
| `/research-task "<主题>"` | 启动一个新的 research 任务，跑 STORM 4 阶段，可选把结果发到 GitHub issue。 |
| `/resume-task <task_id>` | 在当前窗口继续一个中断的任务。 |
| `/list-tasks` | 按状态列出已知的任务（只读）。 |

## 架构

```
   /dev-task
       │
       ▼
┌──────────────┐    next_action      ┌──────────────┐
│   tick.py    │ ──────────────────► │   Main LLM   │
│  (FSM driver)│       (JSON)        │ (thin pump)  │
└──────▲───────┘                     └──────┬───────┘
       │                                    │ 把 prompt 原样
       │  tick --record                     │ 拷贝进 Task tool
       │  (PostToolUse hook)                ▼
       │                             ┌──────────────┐
       │  哈希校验 (PreToolUse) ─────┤  Task tool   │
       │                             └──────┬───────┘
       │                                    │
       │                                    ▼
       │                             ┌──────────────┐
       └─────────────────────────────┤   Subagent   │
                                     │  planning /  │
   Stop hook 重新调用 tick    ◄──────│  impl /      │
   （LLM 停止时触发）                │  review /... │
                                     └──────────────┘

   磁盘状态:   ~/.loopd/
              ├── tasks/<task_id>.json
              ├── sessions/<session_uuid>.json
              └── workspaces/<task_id>--<owner>__<repo>/
                             (git worktree, 分支 loopd/<task_id>)
```

**流水线**

- **Dev：** `planning → plan-critic → implementation → solution-critic → review → PR`
- **Research：** `research → research-critic → （可选）gh-post 把结果评论到 GitHub issue`

**核心组件**

- `tick.py` — 确定性 FSM 驱动器。读取任务状态，计算下一个 subagent + prompt，把 JSON 形式的 `next_action` 写到 stdout。
- **主 Claude Code LLM** — "薄泵（thin pump）"：把 `next_action.prompt` 原样复制到一次 `Task` 工具调用。它不挑选下一步，也不改写 prompt。
- **`PreToolUse` hook** — 校验 prompt 哈希值与 `tick` 输出一致，阻止 LLM 侧的任何修改。
- **`PostToolUse` hook** — 把 subagent 的结果通过 `tick --record` 写回，FSM 随之前进。
- **`Stop` hook** — 在流水线未结束时重新调用 `tick` 并注入下一步动作，让窗口自动继续。
- **Subagent**（`plugins/loopd/agents/*.md`）— `planning`、`plan-critic`、`implementation`、`solution-critic`、`review`、`research`、`research-critic`、`gh-post`。

所有状态都在 `~/.loopd/` 下。

### 多窗口安全

支持同时打开多个 Claude Code 窗口、在每个窗口里运行 `/dev-task`。每个任务都在单写者锁下分配到唯一的 `task_id`，并拥有独立的 worktree（`~/.loopd/workspaces/<task_id>--<owner>__<repo>`）和独立的分支 `loopd/<task_id>`。如果发现同名 worktree 或分支已存在，loopd 会拒绝覆盖，而不是悄悄销毁正在进行中的工作。

## 故障排查

**首次运行时报 `ModuleNotFoundError: No module named 'pydantic'`**

`/plugin install` 只会拷贝插件文件，而 Claude Code marketplace 没有 post-install hook，所以 loopd 的 Python 依赖（`pydantic`、`pydantic-settings`、`PyYAML`）需要一次性安装。插件的 `tick` shim 会在首次调用时尝试自动处理；如果失败，请用 `tick` **实际使用的同一个 Python 解释器**手动安装：

```bash
python3 -m pip install --break-system-packages \
  'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'
```

如果你的系统装了 Homebrew Python，并且 `pip3` 指向的解释器和 `python3` 不一致，请始终用 `python3 -m pip ...`，不要用 `pip3`。

**环境变量**

- `LOOPD_PYTHON` — Python 3.11+ 解释器的绝对路径（例如指向 venv），默认 `python3`。
- `LOOPD_SKIP_BOOTSTRAP=1` — 完全禁用自动安装探测。当你用 poetry / venv / conda 自己管理依赖时使用。
- `LOOPD_ROOT` — loopd 的状态目录（默认 `~/.loopd`）。

**从修复前的版本升级、且还有未完成的 session？** 旧版本会用工作目录哈希作为键，写出 `~/.loopd/sessions/cwd-<hash>.json` 这种 session 文件，结果导致跨窗口任务串号（详见 issue #4）。新版本严格使用 Claude Code 的 session UUID 作为键，并忽略所有遗留的 `cwd-*.json`。升级后可以安全删除：

```bash
rm ~/.loopd/sessions/cwd-*.json
```

## 更新

终端用户：

```
/plugin update loopd
```

Claude Code 判断"是否有新版本"的依据是 `plugins/loopd/.claude-plugin/plugin.json` 里的 `version` 字段——**不是** git commit 或 tag。所以只推 commit 但不升 version 字段是没有效果的。如果需要强制刷新当前版本：

```
/plugin uninstall loopd
/plugin install loopd@loopd
```

## 发布（维护者）

版本号由 GitHub Releases 通过 [`.github/workflows/sync-plugin-version.yml`](.github/workflows/sync-plugin-version.yml) 自动推进：

1. 用 semver tag（例如 `v0.2.0` 或 `0.2.0`）发布一个 GitHub Release。
2. workflow 会去掉前导 `v`，把版本号写进 `plugins/loopd/.claude-plugin/plugin.json`，然后以 `github-actions[bot]` 身份提交回 `main`。
3. 终端用户下次执行 `/plugin update loopd` 时会拿到新版本。

注意：

- release tag 必须符合 semver（`X.Y.Z`，可带 `-pre` / `+build` 后缀），不符合的 tag 会让 workflow 失败。
- release 事件只在 release 创建时触发，bot 推回的 commit 不会再次触发 workflow——不会进入死循环。
- 如果 `plugin.json` 已经是目标版本（例如你在同一个 PR 里手动改过），workflow 会自动 no-op。

## License

[MIT](LICENSE)
