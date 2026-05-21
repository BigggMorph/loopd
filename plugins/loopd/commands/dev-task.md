---
description: loopd 신규 dev task 시작 — planning → implementation → review를 한 창에서 동기 실행
argument-hint: '"<요구사항>" repo:<owner/repo> [level:0-4] [branch:<base>]'
allowed-tools:
  - Bash(${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick:*)
  - Task
hide-from-slash-command-tool: "true"
---

# loopd /dev-task

새 dev task를 시작합니다. `tick init`이 워크스페이스를 만들고 첫 sub-agent를 결정하면, hooks가 자동으로 나머지를 진행합니다.

## 절대 규칙

1. **`tick init`은 단 한 번만** 호출합니다. 그 후의 모든 phase 전환은 PreToolUse/PostToolUse/Stop hooks가 처리합니다.
2. `tick init`이 반환한 `next_action.prompt`와 `next_action.subagent_type`을 **한 글자도 수정하지 말고** `Task` 도구에 그대로 복사하세요. 줄바꿈·공백·JSON 따옴표를 보존하세요.
3. `Task` 도구 외 다른 도구는 호출하지 마세요. 사용자에게 추가 질문 금지.
4. `next_action.kind`가 `"checkpoint_human"`이면 `question` 필드를 사용자에게 출력하고 종료하세요. hooks가 사용자 답변 후 자동으로 재개합니다.
5. `next_action.kind`가 `"failed"`나 응답에 `"error"` 키가 있으면 사용자에게 그 메시지를 보고하고 종료하세요.

## Step 1 — tick init 실행

```!
"${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick" init --args "$ARGUMENTS"
```

> 메모: `tick init`은 슬래시 명령의 bash 서브셸에서 실행되므로 Claude Code의
> 세션 UUID를 직접 보지 못합니다. 이 시점에서는 `~/.loopd/sessions/<uuid>.json`이
> 아직 만들어지지 않고, 대신 `~/.loopd/sessions/.pending/<task_id>.json`에
> "pending claim" 파일이 잠깐 저장됩니다. 다음 단계에서 Step 2의 첫 `Task`
> 호출 직전에 PreToolUse hook이 실제 세션 UUID로 이 파일을 청구(claim)해
> `~/.loopd/sessions/<uuid>.json`으로 옮깁니다 — 그 후의 모든 hook은 이
> UUID로만 세션을 찾습니다. (cwd 해시 fallback은 제거되었습니다.)

응답은 정확히 다음 형태의 JSON 1줄입니다:

```json
{
  "task_id": "task-YYYY-MM-DD-NNN",
  "workspace_path": "/home/.../workspaces/task-...",
  "branch": "main",
  "next_action": {
    "kind": "invoke_subagent",
    "subagent_type": "planning",
    "prompt": "…전체 system prompt 본문 (수천 글자일 수 있음)…",
    "validation_token": "v1.task-….",
    "prompt_sha256": "<hex>",
    "cwd": "/home/.../workspaces/...",
    "iteration": 1
  }
}
```

## Step 2 — 첫 Task 호출

위 응답을 다음 매핑으로 `Task` 도구를 **정확히 1회** 호출하세요:

- `subagent_type` = `next_action.subagent_type`
- `description` = `"loopd phase: " + next_action.subagent_type + " (task " + task_id + ")"`
- `prompt` = `next_action.prompt` (문자 단위로 보존)

## Step 3 이후

Task가 완료되면 PostToolUse hook이 `tick --record`를 자동 실행하고, Stop hook이 다음 next_action을 system message로 주입합니다. 당신은 그 system message를 받으면 Step 2와 같은 방식으로 `Task`를 다시 호출하기만 하면 됩니다 — planning → plan-critic → implementation → solution-critic → review 순서가 자동으로 흘러갑니다.

PR이 만들어지고 review가 `approve`를 내면 loopd가 세션을 정리하고 종료합니다.
