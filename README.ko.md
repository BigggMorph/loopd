<p align="center">
  <img src="image.png" width="120" alt="loopd"/>
</p>

<h1 align="center">loopd</h1>
<p align="center">
  Claude Code를 위한 동기식 멀티페이즈 개발 태스크
</p>

<p align="center">
  <a href="README.md">English</a> |
  <a href="README.zh-CN.md">简体中文</a> |
  <b>한국어</b>
</p>

<p align="center">
  <a href="https://bigggmorph.com/"><b>bigggmorph.com</b></a>
</p>

---

**loopd**는 하나의 Claude Code 창 안에서 **planning → implementation → review**를 자동 파이프라인으로 실행하는 Claude Code 플러그인입니다. 각 태스크는 별도의 git worktree에 격리됩니다.

`claude -p` 서브프로세스 호출 대신 Claude Code 내장 subagent(`Task` 도구)를 활용하기 때문에, **데몬, Slack 브릿지, 게이트웨이 없이** 동작합니다.

## 특징

- **단일 창 파이프라인.** Planning → implementation → review가 하나의 Claude Code 창에서 자동으로 끝까지 진행됩니다. 백그라운드 데몬, Slack 브릿지, 게이트웨이 서비스가 모두 필요 없습니다.
- **태스크별 worktree 격리.** 각 태스크는 자기만의 git worktree(`~/.loopd/workspaces/<task_id>--<owner>__<repo>`)와 브랜치 `loopd/<task_id>`를 가집니다.
- **결정론적 FSM 드라이버.** `tick.py`가 다음 subagent + prompt를 결정하고, 메인 LLM은 그걸 `Task` 호출에 그대로 복사만 하는 "thin pump"입니다. prompt가 슬쩍 바뀌는 일이 없습니다.
- **critic 단계 내장.** plan-critic, solution-critic, research-critic이 각 단계를 검수한 뒤 다음으로 넘어갑니다.
- **두 가지 파이프라인.** Dev (planning → impl → review → PR), Research (STORM 4-phase → 선택적으로 GitHub issue 코멘트).
- **재개 가능.** 자리를 비웠다가 다른 창에서 `/resume-task`로 이어 작업합니다.
- **멀티 윈도우 안전.** 여러 창에서 동시에 `/dev-task`를 돌려도 안전 — task ID, worktree, 브랜치가 single-writer 락 아래 할당됩니다.

## 설치

```
/plugin marketplace add BigggMorph/loopd
/plugin install loopd@loopd
```

`/plugin install`은 `<플러그인 이름>@<마켓플레이스 이름>` 형식을 받습니다. 마켓플레이스를 먼저 추가해야 하며, 여기서는 두 이름이 모두 `loopd`로 같습니다.

요구사항: `python3.11+`, `git`, `gh` CLI.

## 사용법

새 개발 태스크 시작:

```
/dev-task "랜딩 페이지에 가입 폼 추가" repo:BigggMorph/landing-site
```

새 task ID가 할당되고 `~/.loopd/workspaces/<task_id>--<owner>__<repo>`에 worktree가 만들어진 뒤, planning → implementation → review 파이프라인이 PR 생성까지 자동으로 진행됩니다.

다른 창에서 이어서 작업하려면:

```
/resume-task task-2026-05-19-001
```

## 커맨드

| 커맨드 | 동작 |
| --- | --- |
| `/dev-task "<목표>" repo:<owner>/<repo>` | 새 dev 태스크 시작. planning → implementation → review → PR을 실행. |
| `/research-task "<주제>"` | 새 research 태스크 시작. STORM 4-phase 리서치를 실행하고, 선택적으로 GitHub issue에 결과를 게시. |
| `/resume-task <task_id>` | 중단된 태스크를 현재 창에서 재개. |
| `/list-tasks` | 알고 있는 태스크를 상태별로 나열 (read-only). |

## 아키텍처

```
   /dev-task
       │
       ▼
┌──────────────┐    next_action      ┌──────────────┐
│   tick.py    │ ──────────────────► │   Main LLM   │
│  (FSM driver)│       (JSON)        │ (thin pump)  │
└──────▲───────┘                     └──────┬───────┘
       │                                    │ prompt를
       │  tick --record                     │ Task 도구에 그대로 복사
       │  (PostToolUse hook)                ▼
       │                             ┌──────────────┐
       │  hash 검증 (PreToolUse) ────┤  Task tool   │
       │                             └──────┬───────┘
       │                                    │
       │                                    ▼
       │                             ┌──────────────┐
       └─────────────────────────────┤   Subagent   │
                                     │  planning /  │
   Stop hook이 tick 재호출  ◄────────│  impl /      │
   (LLM이 멈추면 발화)               │  review /... │
                                     └──────────────┘

   디스크 상태:  ~/.loopd/
                ├── tasks/<task_id>.json
                ├── sessions/<session_uuid>.json
                └── workspaces/<task_id>--<owner>__<repo>/
                               (git worktree, 브랜치 loopd/<task_id>)
```

**파이프라인**

- **Dev:** `planning → plan-critic → implementation → solution-critic → review → PR`
- **Research:** `research → research-critic → (선택) gh-post가 GitHub issue에 결과 코멘트`

**핵심 구성 요소**

- `tick.py` — 결정론적 FSM 드라이버. 태스크 상태를 읽고 다음 subagent + prompt를 계산해 JSON `next_action`을 stdout으로 내보냅니다.
- **메인 Claude Code LLM** — "thin pump": `next_action.prompt`를 `Task` 도구 호출에 그대로 복사합니다. 다음 단계를 고르거나 prompt를 다시 쓰지 않습니다.
- **`PreToolUse` 훅** — prompt 해시가 `tick`이 내보낸 값과 일치하는지 검증해, LLM 측의 어떤 수정도 차단합니다.
- **`PostToolUse` 훅** — subagent 결과를 `tick --record`로 기록하고, FSM을 한 칸 전진시킵니다.
- **`Stop` 훅** — 파이프라인이 끝나지 않았다면 `tick`을 다시 호출해 다음 액션을 주입, 창이 자동으로 이어 돌아갑니다.
- **Subagent**(`plugins/loopd/agents/*.md`) — `planning`, `plan-critic`, `implementation`, `solution-critic`, `review`, `research`, `research-critic`, `gh-post`.

모든 상태는 `~/.loopd/` 아래에 저장됩니다.

### 멀티 윈도우 안전성

여러 Claude Code 창을 열어 각각에서 `/dev-task`를 동시에 돌리는 것을 지원합니다. 각 태스크는 single-writer 락 아래에서 고유한 `task_id`를 할당받고, 별도의 worktree(`~/.loopd/workspaces/<task_id>--<owner>__<repo>`)와 별도의 브랜치(`loopd/<task_id>`)를 갖습니다. 동일한 ID의 worktree나 브랜치가 이미 존재하면, loopd는 진행 중인 작업을 조용히 날려버리는 대신 덮어쓰기를 거부합니다.

## 트러블슈팅

**첫 실행에서 `ModuleNotFoundError: No module named 'pydantic'` 오류**

`/plugin install`은 플러그인 파일만 배치하고, Claude Code 마켓플레이스에는 post-install 훅이 없기 때문에 loopd의 Python 의존성(`pydantic`, `pydantic-settings`, `PyYAML`)을 한 번은 직접 설치해야 합니다. 플러그인의 `tick` shim이 첫 호출에서 자동 처리를 시도하지만, 실패할 경우 `tick`이 실제로 사용할 **같은 인터프리터**에 수동으로 설치해 주세요:

```bash
python3 -m pip install --break-system-packages \
  'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'
```

Homebrew Python이 설치되어 있고 `pip3`가 `python3`과 다른 인터프리터를 가리키는 경우, 항상 `pip3`가 아니라 `python3 -m pip ...`을 사용하세요.

**환경 변수**

- `LOOPD_PYTHON` — Python 3.11+ 인터프리터의 절대 경로(예: venv를 가리키도록). 기본값은 `python3`.
- `LOOPD_SKIP_BOOTSTRAP=1` — 자동 설치 시도를 완전히 끔. poetry / venv / conda 등으로 의존성을 직접 관리할 때 사용.
- `LOOPD_ROOT` — loopd 상태 저장 위치(기본값 `~/.loopd`).

**버그 수정 이전 빌드에서 업그레이드, 아직 살아 있는 세션이 있다면?** 예전 빌드는 작업 디렉터리 해시를 키로 `~/.loopd/sessions/cwd-<hash>.json` 형태의 세션 파일을 썼는데, 이게 윈도우 간 태스크 하이재킹의 원인이었습니다(issue #4 참고). 새 빌드는 Claude Code 세션 UUID만 키로 쓰고, 남아 있는 `cwd-*.json`은 무시합니다. 업그레이드 후 안전하게 삭제 가능합니다:

```bash
rm ~/.loopd/sessions/cwd-*.json
```

## 업데이트

엔드 유저:

```
/plugin update loopd
```

Claude Code는 "새 버전이 있는가?"를 git commit이나 tag가 아니라 `plugins/loopd/.claude-plugin/plugin.json`의 `version` 필드로 판단합니다. 그래서 그 필드를 올리지 않고 commit만 push하면 아무 효과도 없습니다. 같은 버전을 강제로 다시 받아야 한다면:

```
/plugin uninstall loopd
/plugin install loopd@loopd
```

## 릴리즈 (메인테이너용)

버전 번호는 GitHub Releases와 [`.github/workflows/sync-plugin-version.yml`](.github/workflows/sync-plugin-version.yml) 워크플로로 관리됩니다:

1. semver tag(예: `v0.2.0` 또는 `0.2.0`)로 GitHub Release를 발행합니다.
2. 워크플로가 앞의 `v`를 떼고 `plugins/loopd/.claude-plugin/plugin.json`에 버전을 써넣은 뒤, `github-actions[bot]` 명의로 `main`에 다시 커밋합니다.
3. 엔드 유저는 다음 `/plugin update loopd` 시점에 새 버전을 받습니다.

참고:

- 릴리즈 태그는 semver(`X.Y.Z`, 선택적으로 `-pre` / `+build` 접미사)에 맞아야 합니다. 그렇지 않으면 워크플로가 실패합니다.
- release 이벤트는 release 생성 시에만 발화하므로, 봇이 push한 커밋이 워크플로를 재트리거하지 않습니다 — 무한 루프 없음.
- `plugin.json`이 이미 목표 버전이라면(예: 같은 PR에서 수동으로 올렸을 경우) 워크플로는 no-op으로 끝납니다.

## License

[MIT](LICENSE)
