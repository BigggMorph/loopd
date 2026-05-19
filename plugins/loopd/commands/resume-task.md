---
description: 이전에 중단된 loopd task를 현재 창에서 재개
argument-hint: '<task_id>'
allowed-tools:
  - Bash(${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick:*)
  - Task
---

# loopd /resume-task

기존 `task-YYYY-MM-DD-NNN`을 이 창에 바인딩하고 마지막으로 멈춘 지점부터 진행합니다.

## 절차

```!
"${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick" resume "$ARGUMENTS"
```

응답의 `next_action.kind`에 따라:

- `"invoke_subagent"`: 즉시 `Task` 도구로 호출 (dev-task의 Step 2와 동일 규칙 — 인자를 한 글자도 수정하지 마세요).
- `"complete"`: task가 이미 완료된 상태. 사용자에게 보고하고 종료.
- `"checkpoint_human"`: 사용자 입력 대기 중. `question` 필드를 사용자에게 출력하고 종료.
- `"failed"`: 이미 실패 상태. 사용자에게 보고하고 종료.

hooks가 이어지는 phase 전환을 자동 처리합니다.
