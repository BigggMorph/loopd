---
description: loopd 리서치 task 시작 — STORM 4-Phase research → critic → (선택) GitHub Issue 코멘트
argument-hint: '"<주제>" [repo:owner/repo] [issue:N] [priority:1-5]'
allowed-tools:
  - Bash(${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick:*)
  - Task
hide-from-slash-command-tool: "true"
---

# loopd /research-task

신규 리서치 task를 시작합니다. `tick init --type research`가 `~/.loopd/research-tasks/<task_id>/` 디렉토리를 만들고 첫 sub-agent를 결정하면, hooks가 자동으로 research → research-critic을 진행합니다. `issue:N` 인자가 있으면 critic 통과 후 gh-post가 GitHub Issue에 결과를 코멘트합니다.

## 절대 규칙

1. **`tick init --type research`는 단 한 번만** 호출합니다. 이후 phase 전환은 PreToolUse/PostToolUse/Stop hooks가 자동 처리합니다.
2. `tick init`이 반환한 `next_action.prompt`와 `next_action.subagent_type`을 **한 글자도 수정하지 말고** `Task` 도구에 그대로 복사하세요.
3. `Task` 도구 외 다른 도구는 호출하지 마세요. 사용자에게 추가 질문 금지.
4. `next_action.kind == "checkpoint_human"`이면 `question` 필드를 사용자에게 출력하고 종료.
5. 응답에 `"error"` 키가 있으면 그 메시지를 사용자에게 보고하고 종료.

## Step 1 — tick init 실행

```!
"${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick" init --type research --args "$ARGUMENTS"
```

응답 예시:

```json
{
  "task_id": "task-YYYY-MM-DD-NNN",
  "workspace_path": "/home/.../research-tasks/task-...",
  "task_type": "research",
  "github_issue": 1234,
  "github_repo": "BigggMorph/oh-my-agents",
  "next_action": {
    "kind": "invoke_subagent",
    "subagent_type": "research",
    "prompt": "…전체 system prompt…",
    "validation_token": "v1.…",
    "prompt_sha256": "<hex>",
    "cwd": "/home/.../research-tasks/task-...",
    "iteration": 1
  }
}
```

## Step 2 — 첫 Task 호출

위 응답을 다음 매핑으로 `Task`를 **정확히 1회** 호출:

- `subagent_type` = `next_action.subagent_type` (보통 `"research"`)
- `description` = `"loopd research: " + next_action.subagent_type + " (task " + task_id + ")"`
- `prompt` = `next_action.prompt` (문자 단위로 보존)

## Step 3 이후

Task 완료 → PostToolUse hook이 `tick --record` 호출 → Stop hook이 다음 next_action을 system message로 주입. 당신은 다시 Step 2와 같은 방식으로 `Task`를 호출하면 됩니다.

파이프라인 흐름:
- **issue 없음**: research → research-critic → 완료
- **issue 있음**: research → research-critic → gh-post (Issue 코멘트 게시) → 완료
- **critic FAIL**: research로 backward (최대 2회 재시도)

산출물은 `~/.loopd/research-tasks/<task_id>/`에 보존되므로, gh-post가 실패해도 사용자가 수동으로 게시할 수 있습니다.
