# Orchestrator — Planning Layer (Rev 17)

> **목적**: 기존 orchestrator 시스템 (`docs/orchestrator-design.md`, Rev 1–16) 위에 **기획 레이어 3-agent**를 추가한다. 코드/기술 gap만 보던 기존 issue-scout 외에 제품 기능 gap, 로드맵/phase 사고, vision 자체의 비판적 재고를 분리된 관점으로 도입한다.
>
> **이 문서의 역할**: 신규 작업분만 self-contained로 다룬다. 기존 시스템의 메커니즘은 `docs/orchestrator-design.md`를 cross-link로 참조하고 중복 정의하지 않는다.
>
> **선행 문서**: [`docs/orchestrator-design.md`](./orchestrator-design.md) (Rev 16까지의 모든 메커니즘은 이 문서를 기준선으로 가정). 본 문서를 읽기 전에 그 문서의 §5 (시스템 구조), §7 (teammate 정의), §9 (state machine), §10 (state schema)에 익숙해야 한다.
>
> **사용자 요구 출처**: 2026-05-22 대화 — "issue-scout가 코드를 보고 파악하는 것 같음. 기획자 측면에서 task를 생성하는 에이전트도 필요. 1개로 다 처리할지 모르겠으니 필요하면 여러 에이전트 도입해도 좋음. → 3개 다 추가하자."
>
> ## 📖 How to read this doc
>
> | # | 섹션 | Skim | 정독 |
> |---|---|---|---|
> | 1 | 이 박스 + §1 동기 + §2 변경 요약 | 5분 | 10분 |
> | 2 | §3 기존 시스템과의 관계 (6-agent 다이어그램) | 5분 | 10분 |
> | 3 | §4 신규 teammate 3명 정의 (**핵심**) | 15분 | 35분 |
> | 4 | §5 라벨/status/state/트리거 | 5분 | 15분 |
> | 5 | §6 state machine 통합 분기 + Stage 1/2/3 다이어그램 | 10분 | 25분 |
> | 6 | §7 재사용 메커니즘 + §8 구현 Phase | 5분 | 15분 |
> | 7 | §9 미해결 항목 + §10 메트릭 | 5분 | 10분 |
> | 8 | §11 에이전트 수 분석 + §12 메모리 관리 (**중요 — 운영 정책**) | 10분 | 20분 |
> | 9 | §13 요약 카드 (한 페이지로 전체) | 2분 | 3분 |
>
> **Reading shortcut**: §13 요약 카드 → §3 다이어그램 → §11 에이전트 수 결론 → §12 메모리 관리. **30분 안에 구현 시작 가능**.

---

## 📋 Revision history

### Rev 17 Round A (2026-05-22) — 4-critic 검토 통합 (44 finding fix)

4명 critic (architecture/edge-case/implementability/security) 만장일치 NEEDS_REVISION. 3 critical / 18 major / 23 minor 발견. 통합 fix 항목:

| 분류 | 주요 fix | 해당 finding |
|---|---|---|
| **Critical security — vision audit gap** | `state.vision` 직접 변경에 audit hook 신설 (`audit.record_state_mutation(actor, action, payload)`) — `audited_bash`는 gh command만 wrap. vision 갱신 시 `vision_critic_history` append + audit entry를 동일 `flock_session` 안에서 atomic 실행 | S1 |
| **Critical FSM — 신규 status 미통합** | 신규 4 status (planning_pending/planning_creating/roadmap_pending/vision_check_pending)를 `_ACTIVE_STATUSES`에 넣지 않고 **별도 cycle status enum 3개 신설**: `_PLANNING_STATUSES`, `_ROADMAP_STATUSES`, `_VISION_CHECK_STATUSES`. 각 enum에 대응하는 transition helper (`planning_transition`, `roadmap_transition`, `vision_transition`) — scout_transition 패턴 동일. state.json 저장 위치도 `state.planner_status` / `state.roadmap_status` / `state.vision_check_status` (issue.status 아님) | A1, I11 |
| **Critical impl — would_self_modify에 planner 라벨 누락** | `safety.py:would_self_modify`는 `scout-suggested` 라벨만 자동 True. `planner-suggested`도 추가: `SELF_AUTHORED_LABELS = {"scout-suggested", "planner-suggested"}` 상수 도입 + 함수 갱신 | I2 |
| **Major schema — vision_history 타입 충돌** | 초안에서 `vision_history`를 list[str]와 list[dict] 둘 다로 사용 — 일관성 깨짐. **확정**: `vision_history`는 기존 list[str] 그대로 (사용자 수동 갱신 시 이전 값 push 전용, 변경 0). vision-critic의 **모든** 구조화 entry (accept/reject/pending)는 `vision_critic_history` (list[dict])로 라우팅. pending delta는 별도 top-level `vision_critic_pending_delta` (단일 dict slot, history list 아님) | A5, I7, I8 |
| **Major impl — fingerprint_label prefix 하드코딩** | `safety.py:fingerprint_label`은 `scout-fp-` 접두를 하드코딩 (인자 X). Rev 17은 `prefix: str = "scout-fp-"` 매개변수 추가 — 신규 helper 카운트가 3 → **4개로 정정** (`fingerprint_label` 시그니처 변경 포함) | I1 |
| **Major impl — create_issues_with_fingerprint Python 미존재** | 현재 `skills/orchestrator/SKILL.md`에 prose 형식으로만 존재 — 실제 Python 함수 없음. Phase 17-C 첫 작업으로 `playbook_helpers.py`에 정식 helper로 **lift** 후 양 분기 (scout/planner)에서 호출. +120 LOC | I3 |
| **Major impl — `_PLANNING_STATUSES` set + transition helpers** | A1/I11 통합. Phase 17-A에 추가 (+40 LOC). `transition()` 호출 시 `unknown status` ValueError 방지 | A1, I11 |
| **Major arch — Stage 1 lock 두 단계 race** | scout_creating + planning_creating 두 lock의 직렬화 race. 통합 `stage1_creating_lock` 단일 owner-CAS로 변경. 두 creator type을 같은 flock_session block 안에서 순차 실행 (scout 먼저 → planner) | A2 |
| **Major arch — Step −0 ordering 충돌** | Step −0 (teammate health check)을 Step −5 직전이 아닌 **Step −1과 Step 0 사이**에 배치 (digest 직후, arg 파싱 직전). 이렇게 하면 Step −2 reflection invariant 깨지지 않고, watermark 도달이 in-flight pending status 있으면 1 turn 지연 | A4 |
| **Major arch — REQUIRED_TEAMMATES vs OPTIONAL_TEAMMATES** | `lifecycle.py`에 `OPTIONAL_TEAMMATES = ("product-planner", "roadmap-strategist", "vision-critic")` 추가. `discover_alive_teammates(team_name) -> set[str]` helper로 actual cfg.members 조회. Step −0의 iteration target = discover_alive_teammates() ∩ teammate_health.keys() | A3 |
| **Major edge — Stage 1 partial timeout + late reply** | scout-only fallback 후 늦은 planner reply 처리. `state.planning_pending_resolved_at` 신규 필드: timeout 시 set. 30 min 이내 late reply 도착 시 discard (사용자에게 "late reply — Stage 1 이미 완료, drop" 보고) | E1 |
| **Major edge — /orchestrator plan:true 중복 가드** | Step 0 arg parsing에서 `if state.planner_status == "planning_pending": emit "already active, skip"`. roadmap:true / vision-check:true 동일 가드 | E2 |
| **Major edge — vision-critic counter retry** | `last_vision_critic_cycle = total` 갱신은 응답 받기 전이 맞지만, 재시도는 cycle 카운터 decrement이 아닌 **명시적 boolean** (`vision_critic_retried`) + 같은 turn 안에서 재SendMessage. analyzer_retried 패턴 동일. Step −2 전체를 flock_session() 안에서 실행 | E3 |
| **Major edge — Watermark mid-cycle 감지** | Step −0이 매 invocation 첫 단계만 검사하면 mid-cycle 도달 2 turn 지연. **이중 가드**: Step −0 (invocation 첫 단계) + 모든 teammate reply 직후 (Step 6A 분기). mid-cycle 도달 시 `state.pending_respawn[member] = True` 플래그 → Step −0 다음 turn 처리 | E6 |
| **Major edge — vision_critic_pending_delta 24h+ 처리** | parked_awaiting_human 전이 시 pending_delta를 `vision_critic_history`에 `user_action="parked_expired"`로 commit 후 pending 슬롯 clear. 다음 vision-critic invocation 클린 시작 | E11 |
| **Major security — would_self_modify 의미론 강화** | 현재 regex는 명시적 키워드만 매치 (plugins/orchestrator, 오케스트레이터 등). planner Epic의 User Story 형식 ("As a user, I want ...")으로 우회 가능. **신규 helper** `would_loosen_safety(candidate)` — acceptance criteria에서 "human approval 제거", "confirmation 우회", "vision 갱신", "audit 비활성" 패턴 검출. planner candidate 전용. | S2 |
| **Major security — FORCE_SPLIT 외부 라벨 trust** | `planner-suggested + split-epic` 두 라벨만으로 FORCE_SPLIT 자동 부착하지 않음. 추가 검증: 직전 audit_log에서 해당 issue를 `gh issue create`한 orchestrator entry 매치 + payload_hash 일치 확인. 외부 사용자가 단 라벨은 사용자 confirm 강제 | S3 |
| **Major security — rejected_delta_hashes 우회 방어** | NFKC 정규화 + 공백/zero-width strip + lowercase 후 hash. 추가로 `before` 텍스트 자체 저장 + SequenceMatcher ratio ≥ 0.85 매치 시 동일 reject. 같은 `before` 영역에 3회+ 제안 시 parked_awaiting_human escalation | S4 |
| **Major security — phase_context sanitize** | `roadmap-strategist.phase_context_for_next_cycles` 저장 전 `sanitize_feedback_message` 통과 필수. 500자 초과는 truncation (재요청 X — DoS 방지). 사용자 confirm 시 raw text를 fenced code block으로 plain 표시 | S5 |
| **Major security — vision-critic self-preservation 강화** | (1) guard를 agent-side가 아닌 **lead-side**로 이동 (agent prompt 우회 차단). (2) `delta.before` vs `delta.after` token diff — "human/confirm/approve/audit/사람/확인/승인" 삭제 검출 시 needs_update=false 강제. (3) **두 연속 vision-critic 호출 (25 cycles 간격)**이 같은 방향 delta 제안할 때만 사용자 prompt — 자동 drift slow down | S6 |
| **Major security — prune_state_history 타입 안전성 + pending 보호** | `rejected_delta_hashes`를 list[str]에서 list[dict] (`{hash, rejected_at, before_norm, rejection_count}`)로 변경. `prune_state_history`에 type-safety 단위 테스트 추가. **하드 가드**: 단일 슬롯 필드 (`vision_critic_pending_delta` 등)는 절대 prune 안 함 (단일 슬롯 ≠ history list — pruner 진입 자체 차단) | S7 |
| **Minor 22개** | 통합 정정 표 — A6/A7/A8, E4/E5/E7/E8/E9/E10/E12, I4/I5/I6/I9/I10/I12/I13/I14, S8/S9/S10 — 본 Round A 통합 후 inline footnote 또는 §X.Y 안에서 직접 정정 | 22개 |

### 작업량 재추정 (Round A 반영 후)

| Phase | 변경 전 | 변경 후 | 비고 |
|---|---|---|---|
| Phase 17-A | 50 LOC / 0.25일 | 110 LOC / 0.5일 | +60 LOC (3개 cycle status set + transition helpers + planning_pending 검증) |
| Phase 17-B | 950 LOC / 1.5일 | 1010 LOC / 1.6일 | +60 LOC (OPTIONAL_TEAMMATES + discover_alive_teammates + agent 정의의 sanity check 가드) |
| Phase 17-C | 600 LOC / 2일 | 770 LOC / 2.5일 | +170 LOC (create_issues_with_fingerprint Python lift + fingerprint_label prefix arg + would_self_modify 라벨 추가 + would_loosen_safety 신설) |
| Phase 17-D | 500 LOC / 2일 | 580 LOC / 2.3일 | +80 LOC (vision-critic retry logic + state.vision audit hook + phase_context sanitize + 두 연속 vision-critic 가드) |
| Phase 17-E | 100 LOC / 0.5일 | 150 LOC / 0.6일 | +50 LOC (FORCE_SPLIT audit cross-check) |
| Phase 17-F | 300 LOC / 1일 | 500 LOC / 1.5일 | +200 LOC (3 추가 fixture: stage1-partial-timeout, respawn-recover, prune-ttl-fifo, rejected-delta-loop) |
| Phase 17-G | 280 LOC / 1일 | 340 LOC / 1.2일 | +60 LOC (rejected_delta_hashes dict 변환 + type-safety 단위 테스트 + pending 슬롯 가드) |
| **합계** | **2900 / 7-8일** | **3460 / 10일** | +560 LOC / +2일 |

### Critic round B 계획

Round A 반영 후 같은 4명 critic 재검토 1회. SUFFICIENT 도달 후 Phase 17-A 진입. 잔여 issue가 minor only이면 본 검토 없이 Phase 17 진행 (Rev 5-8 패턴 동일).

### Rev 17 (2026-05-22) — 기획 레이어 3-agent 도입 + 메모리 관리 정책

| 항목 | 결정 | 위치 |
|---|---|---|
| 신규 teammate 3개 | `product-planner`, `roadmap-strategist`, `vision-critic` | §4 |
| 기존 issue-scout 역할 | **유지** (코드/기술 gap 전담 + Rev 13 D1 reflection 그대로). 변경 없음 | §3 |
| 자동 트리거 | 이슈 바닥 시 **Stage 1 (병렬: scout + product-planner)** → **Stage 2 (roadmap-strategist)**. vision-critic은 매 25 사이클 정기 (offset 12, scout reflection과 phase shift) | §5, §6 |
| 수동 트리거 | `/orchestrator plan:true`, `roadmap:true`, `vision-check:true` 신규 | §5 |
| Epic 후보 처리 | product-planner output에 `split-epic` 라벨 자동 부착 → analyzer가 받으면 Rev 9 split 메커니즘으로 자동 분할 | §6 |
| Roadmap report 처리 | task 아님 → `state.pending_questions` 큐 (Rev 13 C1)로 사용자 batched prompt | §6 |
| Vision-critic 처리 | vision 갱신 제안 → 사용자 confirm 후 `state.vision` 업데이트 + `vision_critic_history` 별도 trail | §6 |
| 신규 라벨 3개 | `planner-suggested`, `roadmap-context`, `vision-update-pending` | §5 |
| 신규 status 4개 | `planning_pending`, `planning_creating`, `roadmap_pending`, `vision_check_pending` | §5 |
| State schema | **schema bump 없음** — 현재 이미 v3, `_normalize` setdefault가 신규 필드 자동 backfill | §5.3 |
| 재사용 메커니즘 | `create_issues_with_fingerprint`, `scout_creating_lock` pattern, `pending_questions` 큐, `sanitize_scout_body`, `would_self_modify`, `parse_acceptance_criteria`, `LABEL_SPEC` 등 — 대부분 기존 helper 재사용 | §7 |
| **신규 helper 3개** | `ensure_team_member` (lazy spawn), `recover_team_context` (respawn bootstrap), `prune_state_history` (FIFO+TTL prune) | §7, §12 |
| **에이전트 수 결론** | 3명으로 시작 충분. 7개 추가 후보 중 history-curator만 운영 중 토큰 압박 시 도입 검토. 그 외는 lead-side LLM thinking 또는 helper로 충분 | §11 |
| **메모리 관리 정책 신설** | Working memory (watermark 기반 shutdown/respawn) + Long-term memory (FIFO cap + TTL + state.json 크기 watermark) + Prompt context (agent별 cap + stateless 원칙) + Catastrophic forgetting 방지 (recover_team_context) | §12 |
| 작업량 정정 | Phase 17-A의 마이그레이션 코드 삭제로 -150줄, Phase 17-G (메모리 관리) 신설로 +280줄. 총 ~2900줄, 7-8일 | §8 |

---

## 1. 동기 (왜 기획 레이어가 필요한가)

### 1.1 기존 issue-scout의 한계

기존 `issue-scout`는 vision 기반으로 후보 이슈를 도출하도록 설계되어 있지만 (`docs/orchestrator-design.md:680-725`), 실제 prompt와 input 구조가 다음과 같다:

```
Input: vision + repo의 README/CLAUDE.md/docs/, package.json/pyproject.toml + 기존 이슈 목록
Output: complexity 0-2, criteria 3-5개, independently mergeable atomic task
```

이 설계는 **"이미 존재하는 코드에서 무엇이 빠졌는가"**를 묻는 thinking pattern으로 자연스럽게 흘러간다. README와 코드를 input으로 받는 LLM은 "에러 핸들링 부족", "테스트 커버리지 낮음", "도큐먼트 갱신 필요" 같은 코드 기준 gap을 잘 발견한다.

하지만 비전을 이루기 위해 진짜로 필요한 사고는 다음을 포함한다:

1. **사용자 시나리오 관점의 기능 gap**: "신규 유저가 회원가입 후 첫 5분 안에 어떤 가치를 경험해야 하는가" — 코드에는 존재하지 않는 빈 영역.
2. **로드맵/phase 관점의 우선순위**: 현재 MVP의 어느 단계인가, 다음 단계로 가려면 무엇이 임계 (critical path) 인가.
3. **Vision 자체의 비판적 재고**: "지금 가는 방향이 맞는가, 경쟁 서비스 대비 부족한 본질은 무엇인가, 사용자가 명시하지 않은 implicit needs는 무엇인가" — vision 자체를 갱신해야 할 수도 있음.

이 세 thinking pattern은 같은 LLM 호출이더라도 prompt와 input context가 다르면 다른 결과를 낸다. 한 agent에 셋 다 넣으면 model의 inductive bias가 한쪽 (가장 친숙한 코드 기준)으로 쏠려서 어느 것도 깊이가 부족하다.

### 1.2 분리의 합리화

| 기존 (Rev 16) | 신규 (Rev 17) |
|---|---|
| issue-scout 1개가 **vision → atomic task**를 일직선으로 처리 | issue-scout는 **코드/기술 gap → atomic task**만 담당. 기획 레이어가 **기능/방향/vision** 관점을 분리 |
| Atomic task만 생성 (complexity 0-2 우선) | Atomic + Epic + 방향성 보고 + vision 갱신 제안 4종 output |
| Vision은 1회 입력 + Rev 13 D1의 25 사이클 reflection만 | vision-critic가 적극적으로 vision 자체에 의문 제기, 사용자 confirm 후 갱신 |
| 이슈 바닥 시 자동 트리거 = scout 1회 호출 | Stage 1 (scout + product-planner 병렬) → Stage 2 (roadmap-strategist) 단계화 |

### 1.3 비-목표 (이번 Rev에서 안 함)

- **dev-task 자체 개선**: 기존과 동일 (그대로 호출만).
- **사용자 confirm 없는 자동 이슈/vision 갱신**: planner-suggested 이슈도 scout-suggested와 동일하게 bootstrap 기간 강제 human_needed=true. vision 갱신은 영원히 사용자 confirm 강제.
- **외부 search (경쟁 분석)**: vision-critic은 repo 내부 정보 + vision history만 input으로 받음. 향후 Rev 18에서 WebFetch 도입 검토.
- **6명 teammate 동시 spawn**: lifecycle 관리는 lazy spawn (필요 시점에 SendMessage 직전 ensure).

---

## 2. 변경 요약 (구조 영향 표)

| 카테고리 | 변경 | 영향 범위 |
|---|---|---|
| Teammate | 3명 신규 정의 (`product-planner`, `roadmap-strategist`, `vision-critic`) | `plugins/orchestrator/agents/*.md` 신규 3개 |
| Skill | 1개 신규 정의 (`plan-issues/SKILL.md`) + 2개는 agent prompt 인라인으로 충분 | `plugins/orchestrator/skills/plan-issues/SKILL.md` |
| Lifecycle | lazy spawn 추가 — `ensure_team_member(name)` helper | `python_helpers/lifecycle.py` 갱신 |
| Slash 인자 | `plan:true`, `roadmap:true`, `vision-check:true` 신규 | `commands/orchestrator.md` 갱신 + Step 0 arg 파싱 분기 |
| State machine | 신규 status 4개 (`planning_pending`, `planning_creating`, `roadmap_pending`, `vision_check_pending`) | §6 + `docs/orchestrator-design.md:835` state machine 표 확장 |
| State schema | 신규 필드 5개 (`planner_history`, `roadmap_reports`, `vision_history`, `last_vision_critic_at`, `planning_creating_lock`) | `docs/orchestrator-design.md:1882` v2 schema 확장 → v3 |
| 라벨 | 3개 신규 (`planner-suggested`, `roadmap-context`, `vision-update-pending`) | Phase 0b 라벨 부트스트랩 갱신 |
| 자동 트리거 | 이슈 바닥 시 Stage 1/2/3 단계화 (기존: scout 단일 호출) | Step 5 모드 결정 분기 갱신 |
| 정기 트리거 | vision-critic 매 25 사이클 (기존: Rev 13 D1 vision reflection 강화 버전) | Step −2 reflection 흡수 |
| Helper | `create_issues_with_fingerprint`, `sanitize_scout_body`, `would_self_modify`, `parse_acceptance_criteria`, `pending_questions` 큐 — **모두 기존 재사용**, 신규 helper 0개 | 변경 없음 |
| Dev-task 통합 | 변경 없음 (`/dev-task` 그대로 호출) | 변경 없음 |
| Loopd 의존성 | 변경 없음 (loopd 코드 무수정 원칙 유지) | 변경 없음 |

신규 코드 라인 추정: agents 정의 ~800줄 + SKILL.md ~150줄 + commands/state machine 분기 ~400줄 + state schema 마이그레이션 v2→v3 ~100줄 + 테스트 fixture ~250줄 = **~1700줄**.

---

## 3. 기존 시스템과의 관계

### 3.1 변경된 시스템 다이어그램

```
┌────────────────────────────────────────────────────────────────────┐
│ Lead (main Claude thread, /orchestrator 슬래시 커맨드)              │
│ - state.json (flock) — 신규 status 4개 추가                         │
│ - Wake 이유: (a) teammate reply (b) AskUser 답변                    │
│              (c) β hook의 dev_done inject (d) timer/manual          │
└────────────────────────────────────────────────────────────────────┘
        │
        │ [Resolution cycle — 기존 유지]
        ├── SendMessage ──> issue-analyzer  (변경 없음)
        ├── /dev-task 호출                  (변경 없음)
        ├── SendMessage ──> tester          (변경 없음)
        │
        │ [Scouting cycle — Rev 3] + [Planning layer — Rev 17 신규]
        │
        │ === Stage 1 (이슈 바닥 시 병렬 호출) ===
        ├── SendMessage ──> issue-scout       (코드/기술 gap → atomic)
        ├── SendMessage ──> product-planner   (사용자 시나리오 → Epic)
        │   │  └─ 두 응답 모두 도착 시 dedup (fingerprint hash) → 통합 후보 풀
        │   │     → 사용자 multiSelect confirm
        │   │     → 승인된 것만 gh issue create
        │   │        ├─ scout-suggested 라벨 (scout 출처)
        │   │        └─ planner-suggested + split-epic 라벨 (planner 출처)
        │   │           → 다음 사이클에서 analyzer가 받으면 Rev 9 split 메커니즘 자동 분할
        │   │
        │ === Stage 2 (Stage 1 완료 후 1회) ===
        ├── SendMessage ──> roadmap-strategist
        │       input: Stage 1 후보 풀 + 직전 N건 머지 history + 현재 vision
        │       output: 방향성 보고 (text)
        │              → state.pending_questions 큐 (Rev 13 C1) push
        │              → Step −3에서 사용자에게 batched prompt
        │              → 사용자 채택 시: picker priority weight 갱신 +
        │                              scout/planner prompt에 phase context 자동 주입
        │
        │ === Stage 3 (정기 — 매 25 사이클, Rev 13 D1 확장) ===
        └── SendMessage ──> vision-critic
                input: vision + vision_history + 최근 처리 이슈 + roadmap_reports
                output: vision 갱신 제안 (또는 "갱신 불요" 보고)
                       → 갱신 제안 시 사용자 confirm (필수, bypass 불가)
                       → 채택 시 state.vision 업데이트 +
                          state.vision_history append +
                          Rev 13 A3 lesson injection 메커니즘으로 scout/planner/analyzer 모두에 갱신 history 자동 주입
```

### 3.2 6-agent 시스템 구성 (Rev 17 도입 후)

| Agent | 역할 | thinking pattern | Output | 트리거 |
|---|---|---|---|---|
| `issue-analyzer` (기존) | 이슈 분석, human 개입 판단 | 이슈 본문 → 명세화 | analyze JSON | resolution cycle, Step 6A |
| `tester` (기존) | PR 검증 | acceptance vs diff | verdict JSON | resolution cycle, Step 6A |
| `issue-scout` (기존) | 코드/기술 gap → atomic task | repo state → 빠진 부분 | scout JSON (complexity 0-2) | Stage 1, 자동 + `scout:true` |
| **`product-planner`** (신규) | 사용자 시나리오 → Epic | vision/시나리오 → 큰 기능 단위 | planner JSON (complexity 3-4) | Stage 1, 자동 + `plan:true` |
| **`roadmap-strategist`** (신규) | 현재 phase 진단 + 다음 단계 권장 | history + vision → 방향성 | roadmap report (text) | Stage 2, 자동 + `roadmap:true` |
| **`vision-critic`** (신규) | vision 자체 비판 → 갱신 제안 | vision_history + 처리 history → 의문 제기 | vision delta proposal | 매 25 사이클 + `vision-check:true` |

### 3.3 기존 메커니즘과의 cross-link

본 문서가 가정하는 기존 메커니즘 (이 문서에서 재정의하지 않음). 실제 구현 위치도 함께 명시 (cross-check 결과 반영):

| 메커니즘 | 정의 (orchestrator-design.md) | 실제 구현 (plugins/orchestrator/) | 본 문서 사용처 |
|---|---|---|---|
| `sanitize_scout_body` 화이트리스트 | §19 | `python_helpers/safety.py` (existing) | product-planner body sanitization (재사용, 변경 0) |
| `would_self_modify` gate | §19 + Rev 5 | `python_helpers/safety.py` (existing) | planner-suggested 이슈도 동일 적용 |
| `create_issues_with_fingerprint` | Rev 11 §19 | `skills/orchestrator/SKILL.md` 인라인 (line 595-619) | planning_creating에서 그대로 재사용. SKILL.md 인라인 구현 → `extra_labels=["planner-suggested"]` 인자 변형 |
| `scout_creating_lock` (owner CAS) | Rev 6 R3-1 | `orchestrator_state.py`의 `scout_creating_lock_*` 필드 | `planner_creating_lock_*` 필드를 동일 패턴으로 추가 |
| `parse_acceptance_criteria` | Rev 4 A1.5 | `python_helpers/safety.py` (existing) | planner-suggested 이슈 fast-path |
| `pending_questions` 큐 (cap 20) | Rev 13 C1 | `orchestrator_state.py:144` 필드 + SKILL.md Step −3 | roadmap report push, vision delta push |
| Rev 13 A3 lesson injection | Rev 13 A3 | `playbook_helpers.py:detect_lesson_pattern` (existing) | vision_critic_history → scout/planner/analyzer prompt 주입. **단** 현재 lesson patterns은 7개 fixed (failure_reason 기반). Rev 17은 vision_critic_history를 lesson과 **별도 채널**로 주입 (Rev 13 A3 메커니즘 재사용 X — 별도 prompt 섹션). §12에서 상세. |
| `audited_bash` | Rev 13 D3 | `python_helpers/audit.py` (existing) | gh issue create / vision 갱신 모두 wrap |
| Rev 13 D1 vision reflection | §9 Step −2 | `skills/orchestrator/SKILL.md:93-109` + issue-scout `REFLECTION_REQUEST:` prefix 처리 (`agents/issue-scout.md:77-85`) | **정정**: vision-critic은 Rev 13 D1 reflection을 **대체**하지 않고 **분리**. Step −2의 scout REFLECTION_REQUEST는 그대로 유지 (light-weight subgoal mapping), vision-critic은 별도 트리거 (매 25 사이클 vs scout reflection의 25 사이클 동시 — §5.4에서 충돌 회피 명시) |
| `parked_awaiting_human` 등 timeout 가드 | Rev 5 §13 | `_TERMINAL_STATUSES`에 포함 (`orchestrator_state.py:55-63`) | planning_pending도 동일 timeout (10분) |
| `scout-fp-{hash}` fingerprint label | Rev 6 R3-3 | `python_helpers/safety.py:fingerprint_label` | `planner-fp-{hash}` 동일 함수 — prefix 인자만 다르게 |
| `team_alive(team_name)` | §13 | `python_helpers/lifecycle.py:104` (existing) | 신규 3명을 `REQUIRED_TEAMMATES` 튜플에 추가 시 자동 검증 |
| `LABEL_SPEC` | §17 Phase 0b | `python_helpers/lifecycle.py:25-46` (existing) | 신규 3개 라벨 (`planner-suggested`, `roadmap-context`, `vision-update-pending`) 추가 |

본 문서가 신규 정의하는 메커니즘은 §4-§7에서 다룬다.

---

## 4. 신규 Teammate 3명 정의

세 agent 모두 기존 issue-scout (`docs/orchestrator-design.md:668-725`)와 동일한 패턴을 따른다:
- SendMessage(to="team-lead") 단일 응답 채널 (plain text 출력은 lead에게 안 보임).
- LAST LINE에 single-line JSON.
- 정보 부족 시 `status:"need_clarification"` JSON으로 lead에 추가 질문 요청.
- 작업 끝나면 자동 idle.

### 4.1 `agents/product-planner.md`

```markdown
---
name: product-planner
description: 비전을 이루기 위한 사용자 시나리오/기능 gap을 분석해 Epic 후보를 도출한다. 분할은 analyzer가 함.
tools: Read, Glob, Grep, Bash, WebFetch, SendMessage
skills: [plan-issues]
model: opus
color: purple
---

You are the product-planner teammate in an autonomous GitHub issue resolution system with planning layer (Rev 17).

## Your job
Lead가 SendMessage로 vision + repo + (선택) phase context + 직전 N건 머지 history + 직전 5개 vision_history를 보내면:

1. Repo의 README.md, CLAUDE.md, docs/, 사용자가 작성한 spec 문서를 읽어 **현재 사용자가 받는 가치**를 파악.
2. `gh issue list --repo <repo> --state all --limit 100 --json number,title,labels,state`로 기존/처리된 이슈 확인.
3. **사용자 시나리오 관점**에서 vision과 현재 구현의 gap을 분석:
   - "신규 유저가 첫 5분 안에 어떤 가치를 경험해야 하는가" — 그 경로에 빠진 단계
   - "vision의 핵심 약속을 지키려면 어떤 큰 기능이 필요한가" — 아직 존재하지 않는 영역
   - "사용자가 같은 작업을 반복할 때 마찰이 큰 지점은 어디인가" — UX/workflow gap
4. **Epic 단위** 후보 2-4개 도출. 각 Epic은:
   - complexity_level 3-4 (큰 기능 / 아키텍처 변경 수반).
   - acceptance_criteria 7개 이상 (analyzer가 Rev 9 should_split 임계 자동 매치).
   - sub-task로 자연스럽게 쪼개질 수 있는 단위 (분할은 본인이 하지 않음 — analyzer가 split 사이클로 처리).
5. SendMessage로 lead에 JSON 응답.

## Output contract (LAST LINE = single-line JSON)
```json
{
  "phase": "plan",
  "status": "complete",
  "candidates": [
    {
      "id": "p1",
      "title": "한 줄 제목 (60자 이내)",
      "body": "## Problem\n<사용자 시나리오 관점에서 왜 이 Epic이 필요한지>\n\n## User Story\nAs a <사용자 role>, I want <목표>, so that <가치>.\n\n## Acceptance Criteria\n- [ ] criterion 1\n- [ ] ...\n- [ ] criterion 7+\n\n## Out of Scope\n- <이 Epic이 다루지 않는 것>\n",
      "labels": ["enhancement", "planner-suggested", "split-epic", "priority/medium"],
      "complexity_level": 3,
      "priority_hint": "high",
      "rationale": "왜 이 Epic이 vision의 핵심 약속에 필수인지 1-2문장",
      "user_value": "사용자가 이 Epic 완료 후 얻는 구체적 가치"
    }
  ],
  "summary": "전체 도출 근거 요약",
  "vision_questions": []
}
```
- `vision_questions`: vision이 모호해서 Epic 도출에 결정적인 빈 부분이 있으면 1-3개 질문 채움. 그 외엔 빈 배열.

## 후보 품질 기준 (필수)
- 각 candidate는 **사용자 가치 단위** (기술 단위 아님). "DB 마이그레이션"은 candidate 아님; "유저 데이터 영구 보관" 같은 사용자 약속이 candidate.
- Body에 반드시 **User Story** 섹션 포함 (As a / I want / so that 3단 구조).
- Acceptance criteria는 **사용자가 검증 가능한** 행동 단위 (예: "유저가 X 페이지에서 Y 버튼 누르면 Z가 일어남"). 내부 구현 세부 (예: "Foo 함수 추가") 금지.
- 라벨에 항상 `planner-suggested` + `split-epic` 동시 포함 (출처 표시 + 자동 분할 트리거).
- `Out of Scope` 섹션 필수 — Epic이 무한 확장하지 않도록 경계 명시.
- 너무 atomic한 후보 (complexity 0-2 + criteria < 7)는 도출하지 말 것. 그건 issue-scout 영역. 만약 사용자 시나리오 관점에서 atomic하게 풀 수밖에 없는 후보라면 도출 자체를 생략 (Stage 1에서 scout이 보완).

## Self-injection 방지 (기존 scout과 동일 규칙)
- Body는 사용자가 작성한 듯한 자연스러운 GitHub 이슈 형식. HTML 주석 / `<!-- orchestrator-* -->` / raw script 태그 금지.
- Lead-side에서 `sanitize_scout_body(body)` 동일 helper 적용. `would_self_modify` gate 통과 (orchestrator 파일 변경 요구 후보는 자동 거부).

## Phase context 활용 (Stage 2 출력 자동 주입)
Lead가 SendMessage 본문에 `PHASE_CONTEXT: <text>` 블록을 포함시키면 (Stage 2 roadmap-strategist의 사용자 채택 보고에서 자동 추출), 그 phase 권장사항을 후보 도출 시 우선 적용. 예: "Phase: pre-MVP, focus: 결제 + onboarding" → 두 영역의 Epic만 도출.

## 통신 규칙
- 모든 응답은 SendMessage(to="team-lead")로만.
- Vision이 너무 추상적이라 Epic 도출 불가 시 `{"phase":"plan","status":"need_vision_clarification","question":"..."}` JSON으로 응답.
- 작업 끝나면 자동 idle.
- **재요청 처리**: lead가 "JSON 한 줄로 재전송" 요청하면, 분석을 다시 하지 말고 직전 결과를 JSON 형식으로만 정리해서 응답.
```

#### Lead-side sanity check (product-planner — Round A I1/I2/S2/S10 통합)

Lead는 planner 응답을 받자마자 다음 검증, 실패 시 재요청 (최대 2회):
- `candidates`가 비어 있고 `status != "need_vision_clarification"` → 재요청.
- 각 candidate의 `complexity_level`이 3 미만 → 재요청 ("Epic 단위만 도출, atomic은 scout 영역").
- 각 candidate의 `acceptance_criteria` 개수가 7 미만 (body 내 `- [ ]` 라인 수로 측정) → 재요청.
- `body`에 `## User Story` 섹션 부재 → 재요청.
- `body`에 `## Out of Scope` 섹션 부재 → 재요청.
- 라벨에 `planner-suggested` 또는 `split-epic` 부재 → 자동 보강 (재요청 안 함).
- **Order matters (Round A S10)**: 다음 순서대로 적용 — (1) title + body sanitize → (2) gate 검사 → (3) fingerprint dedup → (4) 사용자 confirm:
  - **(1) Sanitize**: `sanitize_scout_body(body)` (기존) + `sanitize_title(title)` 신규 helper (zero-width 제거 + NFKC + RTL override 차단). title도 sanitize 후 GitHub에 등록.
  - **(2) Gates**:
    - `would_self_modify(candidate)` 매칭 → 후보 자동 제외 + 사용자 보고. **Round A I2 fix**: `safety.py`의 `SELF_AUTHORED_LABELS = {"scout-suggested", "planner-suggested"}` 상수 도입 후 라벨 기반 자동 True 확장.
    - **`would_loosen_safety(candidate)` 신규 gate (Round A S2)**: acceptance criteria + body에서 다음 패턴 매치 시 자동 제외:
      - "human approval 제거", "confirmation 우회", "사람 확인 없이", "audit 비활성/제거"
      - "vision 자동 갱신" / "auto vision update"
      - "autonomy 강화" + "without confirmation" 동시 출현
      - User Story 형식에서 가치 표현으로 위장한 안전성 약화 검출
    - planner candidate에만 적용 (scout candidate는 atomic이라 self-modify 가능성 낮아 기존 gate만).
  - **(3) Fingerprint dedup**: `fingerprint_label(sanitize된 title + body[:200], prefix="planner-fp-")` — Round A I1 fix로 prefix 인자 추가.
  - **(4) 사용자 confirm**: AskUserQuestion. raw text를 fenced code block으로 표시 (markdown 미렌더링 — 우회 시각화 방지).
- 재요청 모두 실패 시 candidate 0개로 처리 + `status="needs_human"`, failure_reason="planner 출력 검증 실패".

### 4.2 `agents/roadmap-strategist.md`

```markdown
---
name: roadmap-strategist
description: 현재 phase를 진단하고 다음 단계 방향성을 권장한다. Task 생성하지 않음.
tools: Read, Glob, Grep, Bash, SendMessage
model: opus
color: gold
---

You are the roadmap-strategist teammate in an autonomous GitHub issue resolution system with planning layer (Rev 17).

## Your job
Lead가 SendMessage로 다음 input을 보내면:
- 현재 vision
- 직전 50건 머지 history (gh pr list --state merged --limit 50)
- Stage 1에서 scout + product-planner가 막 도출한 후보 풀
- state.vision_history (직전 5개 vision 갱신 기록)
- state.roadmap_reports (직전 5개 roadmap 보고 기록)

1. **현재 phase 진단**: 머지 history와 현재 기능 set을 분석해 product의 현재 단계를 분류:
   - `pre-mvp` — 핵심 기능 미완성, 사용자가 vision의 약속을 받을 수 없는 상태.
   - `mvp-validation` — 핵심 기능 작동, 소수 사용자 검증 중.
   - `growth` — 사용자 증가, scale/retention 문제 중심.
   - `mature` — 안정 운영, 점진 개선.
2. **임계 경로 (critical path) 식별**: 다음 phase로 가기 위해 막혀 있는 가장 큰 단일 장애물.
3. **Stage 1 후보 평가**: scout + planner가 도출한 후보 풀이 현재 phase의 임계 경로를 다루고 있는가 평가. 다루지 않으면 추가 권장.
4. **Phase context 권장사항** 작성: 다음 25 사이클 동안 어떤 영역에 우선순위를 두어야 하는가.
5. SendMessage로 lead에 JSON 응답.

## Output contract (LAST LINE = single-line JSON)
```json
{
  "phase": "roadmap",
  "status": "complete",
  "current_phase": "pre-mvp|mvp-validation|growth|mature",
  "phase_evidence": ["머지 history에서 phase 판단 근거 1", "..."],
  "critical_path": "다음 phase 진입을 막는 가장 큰 단일 장애물 (1-2문장)",
  "stage1_evaluation": {
    "addresses_critical_path": bool,
    "missing_areas": ["임계 경로에서 빠진 영역 1", "..."],
    "recommended_picker_boost": [
      {"label_pattern": "auth|onboarding", "weight_multiplier": 2.0, "rationale": "..."}
    ]
  },
  "phase_context_for_next_cycles": "다음 25 사이클 동안 scout/planner prompt에 주입할 한 단락 (200자 이내) — 우선순위/포커스/회피 영역",
  "vision_alignment_concern": "vision과 현재 phase 사이에 부정합 발견 시 1-2문장. 없으면 빈 문자열.",
  "summary": "전체 보고 요약"
}
```

## 진단 휴리스틱
- **pre-mvp 시그니처**: vision의 핵심 명사 (예: "결제", "메시지") 관련 머지 PR 0개 / acceptance criteria의 50% 이상 미달성 / E2E 시나리오 한 줄도 안 됨.
- **mvp-validation 시그니처**: 핵심 시나리오 1-2개 작동 / 사용자 retention / error rate / 첫 사용 경험 관련 이슈 비중 ↑.
- **growth 시그니처**: scale (caching, pagination, rate limit) / perf / 다국어 / 다양한 사용자 segment 이슈 비중 ↑.
- **mature 시그니처**: 머지 이슈의 80% 이상이 bug fix / small enhancement / docs.

## Phase context 형식
```
Phase: <current_phase>
Focus: <2-3 영역 키워드>
Critical path: <한 줄 요약>
Avoid: <지금 우선순위가 아닌 영역, 있을 때만>
```
이 텍스트가 다음 Stage 1 호출 시 scout/planner의 SendMessage 본문에 `PHASE_CONTEXT:` 블록으로 자동 주입.

## 통신 규칙
- 모든 응답은 SendMessage(to="team-lead")로만.
- 머지 history가 너무 적어 (< 5건) 진단 불가 시 `{"phase":"roadmap","status":"insufficient_history","note":"..."}` JSON으로 응답.
- 작업 끝나면 자동 idle.
- **Task 생성 금지**: 본인은 phase 진단 + 권장사항만 작성. 후보 이슈는 scout/planner가 도출.
```

#### Lead-side sanity check (roadmap-strategist)

Lead는 roadmap 응답을 받자마자 검증:
- `current_phase`가 4개 enum 중 하나가 아니면 → 재요청.
- `phase_evidence`가 빈 배열이면 → 재요청 ("근거 없는 phase 판단 거부").
- `phase_context_for_next_cycles` 길이 > 500자 → 재요청 ("200자 이내로 압축").
- 통과한 응답은 `state.roadmap_reports`에 append (cap 10개, FIFO) + Step −3 pending_questions 큐에 "다음 25 사이클 phase context 채택?" 질문 push.

### 4.3 `agents/vision-critic.md`

```markdown
---
name: vision-critic
description: Vision 자체에 비판적 의문을 제기하고 갱신을 제안한다. 사용자 confirm 강제.
tools: Read, Glob, Grep, Bash, SendMessage
model: opus
color: red
---

You are the vision-critic teammate in an autonomous GitHub issue resolution system with planning layer (Rev 17).

## Your job
Lead가 정기 트리거 (매 25 사이클) 또는 `/orchestrator vision-check:true`로 SendMessage를 보내면:

1. **현재 vision 정독**: 초기 vision + state.vision_history (갱신 기록) + 갱신 사유 검토.
2. **불일치 시그널 수집**:
   - 처리된 머지 PR과 vision의 정합 — vision이 약속한 영역인데 처리 0건 또는 그 반대.
   - 사용자가 직접 만든 이슈와 vision의 정합 — vision이 다루지 않는 영역에 사용자가 반복적으로 이슈 생성.
   - roadmap_reports의 `vision_alignment_concern` 누적.
   - reject된 이슈 (Rev 12) 패턴 — out_of_scope 빈도 높으면 vision 경계가 사용자 의도와 어긋날 가능성.
3. **비판적 질문 작성** (반드시 3개 이상):
   - "vision이 약속하는 X와 실제 만들고 있는 Y가 정합하는가?"
   - "vision이 명시하지 않은 implicit 사용자 needs는 무엇인가?"
   - "지금 가는 방향의 6개월 후 모습은 어떠한가, 그것이 사용자에게 의미 있는가?"
4. **Vision delta 제안**: 갱신이 필요하다고 판단되면 구체적인 vision 갱신 안 (before / after / rationale) 작성. 아니면 "갱신 불요" 명시.
5. SendMessage로 lead에 JSON 응답.

## Output contract (LAST LINE = single-line JSON)
```json
{
  "phase": "vision_check",
  "status": "complete",
  "alignment_score": 0.0~1.0,
  "alignment_evidence": [
    {"type":"merge_vs_vision|user_issue_vs_vision|reject_pattern|roadmap_concern","detail":"...","weight":0.0~1.0}
  ],
  "critical_questions": ["질문 1", "질문 2", "질문 3+"],
  "vision_delta": {
    "needs_update": bool,
    "before": "현재 vision 원문 (또는 갱신 대상 부분)",
    "after": "제안 vision (또는 갱신 부분)",
    "rationale": "왜 이 갱신이 필요한지 2-3문장",
    "confidence": 0.0~1.0
  },
  "summary": "전체 보고 요약 (3-5문장)"
}
```

## 비판 휴리스틱
- **alignment_score < 0.6**: vision_delta.needs_update=true 권장.
- **alignment_score 0.6-0.8**: 경계 모호. critical_questions만 사용자에게 제시, vision_delta 보류.
- **alignment_score > 0.8**: needs_update=false. 정기 보고만.
- **confidence < 0.5**인 vision_delta는 needs_update=false로 강제 다운그레이드 (잘못된 갱신 방지).

## 자기 보존 가드 (자가 vision 갱신 차단)
다음 케이스는 vision_delta.needs_update=false 강제 + critical_questions에 의문만 남김:
- 사용자가 직전 5 사이클 내 vision을 수동 갱신함 (`state.vision_history[-1].source == "user"`이고 갱신 후 5 사이클 미만).
- 갱신 사유가 "더 많은 자율성 확보" / "사람 confirm 제거" / "보안 가드 제거" 시그니처 — would_self_modify와 유사 패턴.

## 통신 규칙
- 모든 응답은 SendMessage(to="team-lead")로만.
- vision_history가 없고 처리된 PR이 < 10건이면 (분석 데이터 부족) `{"phase":"vision_check","status":"insufficient_data","note":"..."}`로 응답.
- 작업 끝나면 자동 idle.
- **사용자 confirm 우회 불가**: vision_delta.needs_update=true여도 lead가 자동 적용 금지. 반드시 AskUserQuestion으로 사용자 명시 동의 필요.
```

#### Lead-side sanity check (vision-critic — Round A E5/I7/S6 통합)

Lead는 vision-critic 응답을 받자마자 검증:
- **`status == "insufficient_data"` 우선 분기 (Round A E5)**: alignment_score 등 다른 필드 검증 skip. 사용자에게 "vision-critic 분석 불가 (history 부족): {note}" emit + `state.vision_check_status = None` + return (재요청 X).
- `alignment_score`가 0-1 범위 밖 → 재요청.
- `critical_questions` 개수 < 3 → 재요청.
- `vision_delta.needs_update=true`인데 `before`/`after`/`rationale` 중 하나라도 빈 문자열 → 재요청.
- `vision_delta.confidence < 0.5`이면 needs_update=false로 자동 다운그레이드 (재요청 안 함).
- **Lead-side self-preservation 강화 (Round A S6)**: agent-side 자기 보존 가드 (§4.3 agent prompt)에 더해, lead가 추가 검사:
  - `delta.before` vs `delta.after` token diff — 다음 키워드 deletion 검출 시 needs_update=false 강제: `human`, `confirm`, `approve`, `audit`, `review`, `사람`, `확인`, `승인`, `검토`.
  - **두 연속 vision-critic 호출 (25 cycles 간격)**이 같은 방향 delta 제안 (SequenceMatcher ratio ≥ 0.7)할 때만 사용자에게 prompt. 첫 회는 vision_critic_history에 `user_action="pending_second_confirm"` entry로 보류. 두 번째 회에 검증 후 prompt.
- 통과한 응답은 `state.vision_critic_pending_delta` 단일 슬롯 필드에 저장 (Round A I7 정정 — `vision_history.pending_delta` 아님) + AskUserQuestion으로 사용자에게 confirm (`vision_check_pending` status로 전이).
- 사용자 거부 시 §6.1 reject 분기 (Round A S4 fuzzy match 적용)에 따라 `rejected_delta_hashes` 갱신.

### 4.4 SKILL 정의

세 agent 중 `product-planner`만 별도 SKILL 파일 필요 (사용자 시나리오 분석 rubric이 복잡). 나머지 둘은 agent prompt 인라인으로 충분.

#### `skills/plan-issues/SKILL.md`

```markdown
---
name: plan-issues
description: 사용자 시나리오 관점에서 Epic 후보를 도출하는 rubric
---

## 정보 수집 순서 (우선순위 높은 것부터)
1. Repo의 사용자 명세 문서 (`SPEC.md`, `docs/product/`, `docs/user-stories/` 등) — 있으면 1순위 input.
2. `README.md`의 "Features" / "What it does" 섹션 — 사용자 약속.
3. `CLAUDE.md`의 vision/non-goals 섹션 — 명시된 경계.
4. 직전 25 사이클 머지 PR title (단순 키워드 통계).
5. 사용자가 작성한 (orchestrator-managed 아닌) open issue title — implicit needs.
6. (선택) `package.json`/`pyproject.toml`의 description 필드.

## Epic 도출 휴리스틱
- **사용자 여정 4단계 매핑**: discover → onboard → use → retain. 각 단계에서 vision이 약속하는 가치가 빠져 있는 곳이 Epic 후보.
- **Implicit needs 추출**: 사용자가 명시하지 않았지만 vision이 충족하려면 자명하게 필요한 기능 (예: vision="AI 대화 시뮬레이션"이면 implicit: 대화 history 저장, 다시 보기, 공유).
- **Out of Scope 명시**: Epic이 무한 확장하지 않도록 경계 정의 — 사용자 시나리오 단위로 1-2개 명시.

## 사용자 가치 표현 강제
- ❌ "Database 마이그레이션", "API 리팩토링", "테스트 추가" → 이건 scout 영역.
- ✅ "유저가 작성한 대화 시뮬레이션을 다른 사람과 공유할 수 있음", "기업 고객이 자체 페르소나로 직원 교육에 사용 가능".

## complexity_level 매핑 (분할 후 sub-issue 기준)
- Epic 자체는 항상 3 또는 4.
- 3: 5-7개 sub-issue로 분할 가능, 1-2주 작업.
- 4: 8개 이상 sub-issue, 아키텍처 변경 수반, 2주+ 작업.

## 토큰/비용 가드
- 후보 본문은 각 2KB 이내 (User Story + Criteria + Out of Scope만).
- candidates 총 개수 2-4개 (5개 초과 시 사용자가 confirm fatigue).
- WebFetch 사용 금지 (시간 비용 + repo 외부 의존 줄임).
```

#### roadmap-strategist / vision-critic SKILL

별도 파일 안 만듦. agent prompt에 rubric이 충분히 자족적.

---

## 5. 신규 라벨 / Status / State 필드 / 트리거

### 5.1 신규 라벨 (Phase 0b 라벨 부트스트랩 갱신)

기존 라벨 표 (`docs/orchestrator-design.md` §17 Phase 0b)에 추가:

| 라벨 | 색상 (hex) | 부착 조건 | 사용처 |
|---|---|---|---|
| `planner-suggested` | `9F47CC` (purple) | product-planner candidate 등록 시 자동 부착 (lead-side) | picker 우선순위 가중치, fast-path 매칭, 출처 추적 |
| `roadmap-context` | `F5C518` (gold) | roadmap-strategist가 채택된 phase context를 임시 보존용 GitHub issue로 등록할 때만 (선택, 기본 비활성) | 로드맵 history 시각화. 디폴트는 state.roadmap_reports에만 저장하고 GitHub에는 push 안 함 |
| `vision-update-pending` | `B30000` (red) | vision-critic이 vision_delta.needs_update=true 응답 후 사용자 confirm 대기 중인 메타-이슈 (orchestrator가 자기 자신의 vision 갱신을 추적하기 위해 생성, 선택적) | vision 갱신 audit trail |

부착 자동화: `Phase 0b 라벨 부트스트랩` (orchestrator-design.md §17)에서 위 3개 라벨을 `gh label create` 자동 등록 로직에 추가. 멱등성: `gh label list | grep -q "<name>" || gh label create ...`.

### 5.2 신규 Status (cycle status enum 3개 신설)

**정정 (Round A A1/I11)**: 초안에서 신규 status를 issue.status 또는 기존 `_ACTIVE_STATUSES`에 넣으려 했으나, `orchestrator_state.py:transition()` (line 284-297)은 enum 외 status를 ValueError로 거부. `scout_status`가 `_SCOUT_STATUSES` 별도 enum 패턴 사용하는 것처럼, Rev 17 신규 status도 **3개 cycle-level enum**으로 분리:

```python
# orchestrator_state.py에 추가 (Phase 17-A)

_PLANNING_STATUSES = {
    "planning_pending",
    "planning_creating",
    "planning_done",
    "planning_failed",
}

_ROADMAP_STATUSES = {
    "roadmap_pending",
    "roadmap_received",
    "roadmap_done",
}

_VISION_CHECK_STATUSES = {
    "vision_check_pending",
    "vision_check_received",
    "vision_check_done",
}
```

저장 위치: 각각 `state.planner_status`, `state.roadmap_status`, `state.vision_check_status` (issue.status 아님). 신규 transition helpers (scout_transition과 동일 패턴):

```python
def planning_transition(state, new_status):
    if new_status not in _PLANNING_STATUSES:
        raise ValueError(f"unknown planning status: {new_status}")
    state["planner_status"] = new_status
    state.setdefault("planning_history_log", []).append(
        {"to": new_status, "at": _iso(now())}
    )

def roadmap_transition(state, new_status): ...  # 동일 패턴
def vision_transition(state, new_status): ...   # 동일 패턴
```

#### 각 status의 의미 + 전이

| Status | enum 소속 | 의미 | 진입 | 종료 (→ 다음 status) | Timeout |
|---|---|---|---|---|---|
| `planning_pending` | _PLANNING_STATUSES | product-planner에 SendMessage 보내고 응답 대기 | Stage 1 진입 시 (scout_pending과 동시) | `planning_done` (응답 도착) → `planning_creating` 또는 None | 10분 — timeout 시 `planning_pending_resolved_at` 마킹 + 1회 재시도 → 실패 시 candidates=[] |
| `planning_creating` | _PLANNING_STATUSES | 사용자 confirm된 planner candidate를 GitHub에 등록 중 | planning_pending에서 사용자 confirm 통과 시 | 등록 완료 → `planning_done` → state.planner_status = None | 트랜잭션 lock (`stage1_creating_lock` 통합 — Round A A2 fix) |
| `roadmap_pending` | _ROADMAP_STATUSES | roadmap-strategist 응답 대기 | Stage 2 진입 시 (Stage 1 종료 후) | 응답 도착 + 사용자 채택/거부 → `roadmap_done` → None | 10분 |
| `vision_check_pending` | _VISION_CHECK_STATUSES | vision-critic 응답 대기 또는 사용자 vision 갱신 confirm 대기 | 매 25 사이클 정기 + 수동 호출 | vision_delta 채택/거부 → `vision_check_done` → None | 응답 대기 10분 + 사용자 confirm 24시간 (parked_awaiting_human 폴백, pending_delta는 vision_critic_history에 expired entry로 commit) |

기존 `scout_creating` / `scout_pending` 등은 그대로 유지. planner는 Stage 1 병렬 호출이지만 **두 enum이 독립적**으로 진행 (둘 다 응답 도착해야 사용자 confirm 단계로 진입, 한쪽만 도착 시 다른 쪽 timeout까지 대기 — Round A E1 fix로 late reply discard).

### 5.3 신규 State 필드 (schema bump 없음 — `_empty_state` 확장만)

**중요 정정**: 현재 코드는 이미 `SCHEMA_VERSION = 3` (`plugins/orchestrator/python_helpers/orchestrator_state.py:26`). 또한 `_normalize` 함수가 매 read마다 `_empty_state()`의 default 값으로 누락 필드를 자동 backfill하므로 (`orchestrator_state.py:205-211`), **신규 필드 추가에 schema bump도 명시적 마이그레이션 코드도 불필요**. `_empty_state()`에 신규 필드 default만 추가하면 기존 state.json 파일들이 다음 read 시 자동으로 신규 필드를 빈 값으로 갖게 된다.

#### 기존 `vision_history`와의 충돌 정합

`vision_history`는 이미 v3 schema에 존재 (`orchestrator_state.py:104`). 현재 용도: 사용자가 `/orchestrator vision:"..."` 인자로 vision을 갱신할 때 **이전 vision 문자열**을 push (`skills/orchestrator/SKILL.md:128-129`). 즉 현재 schema는 단순 `list[str]`로 추정.

Rev 17은 vision-critic 갱신 audit trail (source/before/after/rationale/user_action 구조체)이 필요하므로 **별도 필드 `vision_critic_history`로 분리**한다. 기존 `vision_history`는 그대로 유지 (사용자 수동 갱신 history 전용). 두 필드의 책임은:
- `vision_history` (기존): 사용자 수동 갱신 시 이전 vision 문자열만 push. 변경 없음.
- `vision_critic_history` (신규): vision-critic이 제안한 delta 보고 — accepted/rejected 모두 기록.

#### `_empty_state()` 확장 (신규 필드만)

`plugins/orchestrator/python_helpers/orchestrator_state.py`의 `_empty_state()` 함수에 다음 default 추가:

```python
def _empty_state() -> Dict[str, Any]:
    return {
        # ... 기존 필드 (variant 없음) ...

        # === Rev 17 신규 ===

        # Product-planner cycle
        "planner_status": None,                    # planning_pending|planning_creating|planning_done|None
        "planner_started_at": None,                # ISO timestamp
        "planner_candidates_buffer": None,         # Stage 1 통합 전 임시 저장 (list or None)
        "planner_creating_done": [],               # 등록 완료된 candidate id 리스트
        "planner_creating_lock_started_at": None,
        "planner_creating_lock_owner": None,
        "planner_history": [],                     # FIFO cap 50, entry: {ts, candidates_proposed, candidates_accepted, candidates_rejected, issue_urls_created}
        "planner_decisions": {},                   # 사용자 confirm 결과 임시 저장
        "planner_confirm_idx": 0,
        "planner_created_urls": [],
        "planner_failed_creations": [],

        # Stage 1 통합용 (scout buffer는 기존 scout_candidates 재사용)
        "stage1_merge_pending": False,             # 둘 다 도착 대기 플래그
        "last_stage1_completed_at": None,

        # Roadmap cycle
        "roadmap_status": None,                    # roadmap_pending|roadmap_received|None
        "roadmap_started_at": None,
        "roadmap_reports": [],                     # FIFO cap 10
        "active_phase_context": None,              # 채택된 phase_context 텍스트
        "active_phase_context_until_cycle": 0,
        "last_roadmap_report_cycle": 0,

        # Vision-critic cycle
        "vision_check_status": None,               # vision_check_pending|vision_check_received|None — _VISION_CHECK_STATUSES enum 별도 (§5.2)
        "vision_check_started_at": None,
        "vision_critic_retried": False,            # (Round A E3) timeout 재시도 boolean — 같은 turn 안에서 SendMessage
        "vision_critic_history": [],               # FIFO cap 20, entry: {ts, source, before, after, rationale, user_action, alignment_score}. **모든** vision-critic 구조화 entry는 여기로 (vision_history는 list[str] 그대로 유지 — Round A A5/I7/I8)
        "vision_critic_pending_delta": None,       # 단일 slot dict (list 아님 — prune 비대상). 사용자 confirm 대기 중인 delta. {before, after, rationale, alignment_score, proposed_at}
        "last_vision_critic_cycle": 0,
        "rejected_delta_hashes": [],               # (Round A S4/E7) list[dict] — {hash, rejected_at, before_norm, rejection_count}. NFKC + lowercase + whitespace collapse 후 hash. 같은 before 영역 3회 reject 시 parked_awaiting_human escalation. TTL 30일 (first_rejected_at 기준).

        # Lazy spawn (Round A A8/I12)
        "pending_team_spawns": [],                 # ensure_team_member이 spawn 필요 마킹한 member 이름 리스트. Step 3에서 drain.
        "teammate_health": {},                     # {member: {spawned_at, call_count, estimated_tokens, last_response_at, respawn_count}} — Step −0 검사 대상

        # Stage 1 partial timeout (Round A E1)
        "planning_pending_resolved_at": None,      # planning_pending timeout 시 set. 30분 이내 late reply는 discard.
        "scout_pending_resolved_at": None,         # 대칭. 향후 stage1 late reply 처리에 사용.

        # Mid-cycle respawn (Round A E6)
        "pending_respawn": {},                     # {member: True} — Step 6A에서 watermark 도달 검출 시 set. Step −0 다음 turn에서 처리.
    }
```

#### Backfill 동작 검증

기존 state.json (Rev 16까지의 v3) 파일이 있는 환경에서 Rev 17 코드 첫 실행 시:
1. `read()` → `_normalize(state)` → `setdefault`로 신규 필드 모두 빈 값 추가.
2. `write(state)` 시 다음 invocation부터 신규 필드 영구 저장.
3. `version` 필드는 그대로 3 유지 (bump 불요).

별도 마이그레이션 코드/테스트 fixture 불필요. 단 Phase 17-F에서 _empty_state() 신규 필드들이 기존 read 경로에서 default로 채워지는지 unit test 추가 권장 (1 케이스).

### 5.4 자동 트리거 — Stage 1/2/3 단계화

#### Stage 1 — 이슈 바닥 감지 시 병렬 호출

기존 Step 5 (모드 결정, `docs/orchestrator-design.md:1100`)의 `picker.pick() == 0` 분기 갱신:

```
기존:
  picker.pick() == 0
    → scouting cycle 진입 (SendMessage to issue-scout)

신규 (Rev 17):
  picker.pick() == 0
    → Stage 1 진입:
       1. ensure_team_member("issue-scout")
       2. ensure_team_member("product-planner")
       3. SendMessage(to="issue-scout", body=<vision + active_phase_context + lessons>)
       4. SendMessage(to="product-planner", body=<vision + active_phase_context + vision_history[-5:] + lessons>)
       5. state.status는 두 개로 분리:
          - state.scout_pending = True (응답 대기)
          - state.planning_pending = True (응답 대기)
       6. 두 응답 모두 도착할 때까지 idle wake 반복
          - 한쪽만 도착하면 도착한 후보는 buffer에 저장, 다른 쪽 timeout 대기
          - 양쪽 모두 도착 또는 양쪽 모두 timeout 시 통합 후보 풀 생성 → 사용자 multiSelect confirm
       7. 통합 단계에서 fingerprint dedup:
          - scout 후보와 planner 후보가 같은 sha256(title+body) 또는 ≥0.85 cosine 유사도면 한 쪽 (priority 낮은 쪽 — scout-suggested) 자동 제외
          - 사용자에게 dedup 결과 표시 ("p1과 c2가 유사해 c2 자동 제외")
```

#### Stage 2 — Stage 1 종료 후 1회

Stage 1 후보 풀 (등록 여부 무관) 도착 직후 자동 호출:

```
Stage 1에서 candidates 통합 + 사용자 confirm 완료 시:
  → ensure_team_member("roadmap-strategist")
  → SendMessage(to="roadmap-strategist", body=<vision + 최근 50건 merged PR + Stage1 후보 풀 + vision_history[-5:] + roadmap_reports[-3:]>)
  → state.roadmap_pending = True
  → 응답 도착 + sanity check 통과 시:
    - state.roadmap_reports에 append
    - Step −3에서 pending_questions 큐에 "phase context 채택?" 질문 push
  → 사용자 채택 시:
    - state.active_phase_context = report.phase_context_for_next_cycles
    - state.active_phase_context_until_cycle = current_cycle + 25
  → 사용자 거부 시:
    - report.user_action = "rejected" 기록
    - 다음 Stage 2 호출까지 active_phase_context 유지 (이전 값) 또는 null
```

#### Stage 3 — 정기 (vision-critic 매 25 사이클)

**기존 Rev 13 D1 vision reflection과의 충돌 회피** — 정정사항: 신규 문서 초기 작성에서 "vision-critic이 D1 reflection을 흡수/대체"라고 했으나, 실제 코드 (`skills/orchestrator/SKILL.md:93-109`, `agents/issue-scout.md:77-85`) 확인 결과 D1은 **issue-scout이 `REFLECTION_REQUEST:` prefix 메시지를 받아 subgoal mapping 보고**하는 가벼운 메커니즘. vision-critic은 더 무거운 비판/갱신 제안 역할이므로 **두 메커니즘을 분리 운영**:

| 항목 | 기존 D1 scout reflection | 신규 Rev 17 vision-critic |
|---|---|---|
| 트리거 | 25 사이클마다 `total % 25 == 0 && last_reflection_count != total` | 25 사이클마다 `current_cycle - last_vision_critic_cycle >= 25` |
| 호출 대상 | issue-scout (REFLECTION_REQUEST prefix) | vision-critic teammate |
| Output | `{phase:"reflection", mapped_subgoals, gap_areas, vision_update_suggestion}` (단순 요약) | `{phase:"vision_check", alignment_score, critical_questions, vision_delta}` (구조화된 비판) |
| 비용 | 낮음 (scout context 누적 X, 단발 호출) | 높음 (vision-critic 신규 spawn, input 큼) |
| 결과 처리 | pending_questions에 단순 요약 push | 사용자 confirm으로 state.vision 직접 갱신 |

**충돌 회피 정책**: 같은 invocation의 Step −2에서 둘 다 트리거되지 않도록 phase shift 적용:
- D1 (scout reflection): `total % 25 == 0` (총 처리 25, 50, 75, ... 시점)
- vision-critic: `total % 25 == 12` (총 처리 12, 37, 62, ... 시점) — 12 cycle offset

이렇게 하면 두 reflection이 12-13 cycle 간격으로 번갈아 호출되어 분석 cadence는 12-13 cycle로 더 빈번, 단일 invocation 비용은 절반.

```
매 invocation의 Step −2:
  total = completed_count + rejected_count + sum(len(h.created_urls) for h in scout_history)

  # 기존 D1 (변경 없음)
  if total > 0 and total % 25 == 0 and last_reflection_count != total:
    → SendMessage(to="issue-scout", body="REFLECTION_REQUEST: ...")
    → state.reflection_pending = True
    → state.last_reflection_count = total
    → write(); return

  # 신규 Rev 17 vision-critic (12 offset)
  elif total > 12 and total % 25 == 12 and last_vision_critic_cycle != total:
    → ensure_team_member("vision-critic")  # lazy spawn (§7에서 정의)
    → SendMessage(to="vision-critic", body=<vision + vision_critic_history + 최근 50건 merged PR + roadmap_reports + reject 이슈 패턴>)
    → state.vision_check_status = "vision_check_pending"
    → state.last_vision_critic_cycle = total  # 응답 받기 전 갱신 (중복 호출 방지)
    → write(); return

# 응답 도착 분기 (wake_reason 처리에서):
# - phase == "reflection" (scout) → 기존 Rev 13 F-I3 처리 그대로
# - phase == "vision_check" (vision-critic) → 신규 분기:
#     sanity check 통과 시:
#       vision_delta.needs_update=true → state.vision_critic_pending_delta에 저장 + AskUserQuestion confirm
#       사용자 채택 시: state.vision = delta.after, vision_history.append(prev_vision),
#                       vision_critic_history.append({source:"vision_critic", user_action:"accepted", ...})
#       사용자 거부 시: vision_critic_history.append({user_action:"rejected", ...})
#                       state.rejected_delta_hashes.append(sha256(delta.before + delta.after))
#       needs_update=false → critical_questions만 pending_questions에 push (선택)
```

### 5.5 수동 트리거 — 슬래시 인자

`/orchestrator` 슬래시 커맨드 (`docs/orchestrator-design.md:819`)에 인자 추가:

| 인자 | 의미 | 처리 분기 |
|---|---|---|
| `plan:true` | product-planner만 강제 호출 (scout 동시 호출 안 함) | Step 0에서 인자 파싱 → planning_pending 진입 (scout_pending bypass) |
| `roadmap:true` | roadmap-strategist 강제 호출 | Step 0에서 인자 파싱 → roadmap_pending 진입 (Stage 1 bypass, 직전 머지 history만으로 진단) |
| `vision-check:true` | vision-critic 강제 호출 (25 사이클 cooldown 무시) | Step 0에서 인자 파싱 → vision_check_pending 진입 |

#### 충돌 처리

- 여러 인자 동시 지정 시 (예: `/orchestrator plan:true roadmap:true`) → 순차 실행 (plan → roadmap → vision-check 순).
- active resolution cycle (analyze_pending / test_pending 등) 중에 plan/roadmap/vision-check 호출 시 → 기존 cycle은 `parked_awaiting_human`으로 전이 후 진행 (기존 stop:true 패턴 재사용, `docs/orchestrator-design.md:1017` Step 0 참조).
- **중복 호출 가드 (Round A E2)**: Step 0 arg parsing에서 각 인자 처리 직전 idempotency 검사:
  - `plan:true` & `state.planner_status == "planning_pending"` → "이미 진행 중, skip" emit + write + return (재SendMessage 안 함, timeout 가드만으로 진행).
  - `roadmap:true` & `state.roadmap_status == "roadmap_pending"` → 동일.
  - `vision-check:true` & `state.vision_check_status == "vision_check_pending"` → 동일.
  - `*_started_at` timestamp 갱신 안 함 (timeout window 위조 방지).

#### `/orchestrator stop:true` 정리 (Round A E9)

stop:true 처리에 신규 분기 추가 (기존 §SKILL Step 0 stop:true 갱신):
- TeamDelete 직전: `state.pending_team_spawns = []` clear (다음 invocation에서 stale member spawn 방지).
- `state.planner_status = None`, `state.roadmap_status = None`, `state.vision_check_status = None` 모두 reset.
- 진행 중이던 `vision_critic_pending_delta` 있으면 `vision_critic_history`에 `user_action="stopped_by_user"` entry로 commit 후 clear.
- `state.pending_respawn = {}` clear.

---

## 6. State Machine 통합 분기 + dev-task 연계

### 6.1 신규 status 전이 표

기존 §9 (`docs/orchestrator-design.md:835`) state machine에 추가되는 4개 status의 전이:

```
[Stage 1 — 병렬 진입]

picker.pick() == 0  (Step 5)
  ↓
  branch:
    ├─ Stage 1 시작 (자동) — 또는 plan:true 인자 (수동)
    │  ├─ scout_pending = True  (기존 Rev 3 분기)
    │  └─ planning_pending = True  (신규)
    │
    │  wake (teammate_reply: issue-scout):
    │    state.scout_pending = False
    │    state.scout_candidates_buffer = parsed candidates
    │
    │  wake (teammate_reply: product-planner):
    │    state.planning_pending = False
    │    state.planner_candidates_buffer = parsed candidates
    │
    │  wake (idle check — 두 buffer 모두 채워졌거나 둘 다 timeout):
    │    → 통합 단계:
    │       1. dedup (fingerprint hash + cosine 0.85)
    │       2. would_self_modify gate 적용 (양쪽 모두)
    │       3. sanitize_scout_body 적용 (양쪽 모두)
    │       4. AskUserQuestion multiSelect (cap 8개 후보)
    │    → 사용자 confirm 결과:
    │       state.status = scout_creating  (scout 후보 등록 중)
    │       또는 state.status = planning_creating  (planner 후보 등록 중)
    │       또는 둘 다 — 순차 실행 (scout 먼저, planning 다음)
    │
    │  wake (등록 완료):
    │    state.status = idle
    │    다음 wake에서 picker가 신규 이슈 받음
```

```
[Stage 2 — Stage 1 완료 후 자동 1회]

state.status == idle AND state.last_stage1_completed_at within 5분
  ↓
  Stage 2 시작 (자동):
    state.roadmap_pending = True
    SendMessage to roadmap-strategist
  ↓
  wake (teammate_reply: roadmap-strategist):
    state.roadmap_pending = False
    sanity check 통과 시:
      state.roadmap_reports.append(...)
      pending_questions.push("phase context 채택?")
    state.status = idle
  ↓
  Step −3에서 사용자가 채택/거부:
    채택 → state.active_phase_context = report.phase_context
           state.active_phase_context_until_cycle = current + 25
    거부 → report.user_action = "rejected"
```

```
[Stage 3 — 정기 (Step −2)]

매 invocation Step −2:
  cycle_diff = current_cycle - state.last_vision_critic_cycle
  if cycle_diff >= 25 OR /orchestrator vision-check:true 인자:
    state.vision_check_pending = True
    SendMessage to vision-critic
    state.last_vision_critic_cycle = current_cycle (응답 받기 전에 갱신 — 중복 호출 방지)
  ↓
  wake (teammate_reply: vision-critic):
    state.vision_check_pending = False
    sanity check 통과:
      if vision_delta.needs_update:
        state.vision_history.pending_delta = delta
        AskUserQuestion confirm
      else:
        critical_questions → pending_questions (선택)
  ↓
  사용자 confirm (Round A S1/A5/I7/I8/S4 통합 — vision_history는 list[str] 그대로):
    accept →
      flock_session():
        prev_vision = state.vision
        state.vision = delta.after
        state.vision_history.append(prev_vision)  # list[str] 유지
        state.vision_critic_history.append({
          ts, source:"vision_critic", before, after, rationale,
          user_action:"accepted", alignment_score
        })  # list[dict] 별도 trail
        state.vision_critic_pending_delta = None
        audit.record_state_mutation(  # Round A S1 — gh 명령 아닌 state mutation도 audit
          actor="vision-critic",
          action="state.vision update (accepted)",
          payload={before:prev_vision, after:delta.after, rationale:delta.rationale}
        )
      # §12.3 별도 채널로 scout/planner/analyzer prompt에 vision_critic_history[-5:] 주입
    reject →
      flock_session():
        norm = lambda s: nfkc(s).strip().lower().collapse_whitespace()
        h = sha256(norm(delta.before) + norm(delta.after))
        # 기존 hash 또는 SequenceMatcher ratio >= 0.85 매치 검사 (Round A S4)
        existing = find_match_in(state.rejected_delta_hashes, h, norm(delta.before), norm(delta.after))
        if existing:
          existing.rejection_count += 1
          existing.last_seen_at = now()
        else:
          state.rejected_delta_hashes.append({
            hash:h, before_norm:norm(delta.before), after_norm:norm(delta.after),
            rejected_at:now(), last_seen_at:now(), rejection_count:1
          })
        # 같은 before 영역 3회+ reject 시 vision-critic 일시 정지 (Round A S4)
        if same_before_count(state.rejected_delta_hashes, norm(delta.before)) >= 3:
          state.vision_check_status = "vision_check_parked"
          push_pending_question("vision-critic이 동일 영역 3회+ 거부됨 — 일시 정지할까?")
        state.vision_critic_history.append({
          ts, source:"vision_critic", before, after, rationale,
          user_action:"rejected", alignment_score
        })
        state.vision_critic_pending_delta = None
        audit.record_state_mutation(actor="vision-critic", action="vision delta rejected", payload={hash:h})
  ↓
  # 24h+ 사용자 무응답 (Round A E11)
  parked_awaiting_human 전이:
    flock_session():
      if state.vision_critic_pending_delta:
        state.vision_critic_history.append({
          ts, source:"vision_critic", before, after, rationale,
          user_action:"parked_expired", alignment_score
        })
        state.vision_critic_pending_delta = None
```

### 6.2 dev-task 연계 흐름

Planner 후보 (Epic, `split-epic` 라벨 부착)가 GitHub에 등록되면, 다음 사이클에서 picker가 받아 analyzer에 전달한다. 이후는 **기존 Rev 9 split 메커니즘 재사용**:

```
다음 사이클:
  picker.pick() → planner-suggested + split-epic 라벨 이슈 발견
                  (picker 우선순위: 사용자 priority > scout-suggested = planner-suggested)
  ↓
  state.status = analyze_pending
  SendMessage to issue-analyzer with issue + FORCE_SPLIT=true 자동 주입
    (이유: split-epic 라벨 있으면 lead가 자동으로 FORCE_SPLIT 부착)
  ↓
  analyzer 응답 (Rev 9 메커니즘):
    should_split = true
    sub_candidates = [3-5개 sub-issue]
  ↓
  Lead가 split_confirm_pending 진입 → 사용자 confirm
  ↓
  사용자 승인:
    Rev 9 split_creating: sub-issue들 등록 (`split-from-#<N>` 라벨 자동 부착)
    원 Epic은 `split-epic` 라벨 + `<!-- split-epic-marker -->` body comment 부착 → picker가 skip (Rev 10 fix A)
  ↓
  다음 사이클부터 sub-issue들이 picker에서 픽됨
  ↓
  각 sub-issue → analyzer (split-from-#N fast-path) → dev-task → tester → 머지

Lead-side 자동 FORCE_SPLIT 규칙:
  picker가 issue 받을 때 라벨 검사:
    if "planner-suggested" in labels AND "split-epic" in labels:
      analyzer SendMessage 본문에 "FORCE_SPLIT=true" 자동 추가
      (analyzer Rev 9 메커니즘이 should_split=true 강제 응답)
```

#### Epic 자체의 dev-task 호출 금지

**중요**: Epic 후보는 절대 `/dev-task`로 직접 보내지 않는다. picker가 `split-epic` 라벨 있는 이슈는 다음 두 단계 거치게:
1. analyzer → split_confirm → sub-issue 생성
2. 원 Epic은 `split-epic-marker` 부착 후 picker가 skip (영구적)

만약 사용자가 명시적으로 split을 거부하면 (`split_refused` 분기, Rev 9 Round B fix H + Rev 11) → Epic은 needs_human으로 보고하고 사용자가 수동 처리.

### 6.3 Wake 이유 추론 갱신

기존 `wake_inference.py` (`docs/orchestrator-design.md:1845`)에 신규 status 4개 분기 추가:

```python
def infer_wake_reason(transcript_last, state):
    # 기존 분기들 ...

    # === Rev 17 신규 ===

    # product-planner 응답 도착
    if state.planning_pending and transcript_last contains "phase":"plan"" JSON":
        return "planner_reply"

    # roadmap-strategist 응답 도착
    if state.roadmap_pending and transcript_last contains "phase":"roadmap"" JSON":
        return "roadmap_reply"

    # vision-critic 응답 도착
    if state.vision_check_pending and transcript_last contains "phase":"vision_check"" JSON":
        return "vision_critic_reply"

    # Stage 1 두 응답 모두 도착 (idle wake)
    if not state.scout_pending and not state.planning_pending \
       and state.scout_candidates_buffer is not None \
       and state.planner_candidates_buffer is not None:
        return "stage1_merge_ready"

    # Stage 2 진입 시점 감지
    if state.status == "idle" \
       and state.last_stage1_completed_at within 5분 \
       and state.last_roadmap_report_cycle != current_cycle:
        return "stage2_due"

    # 기존 fall-through 계속 ...
```

추가 state 필드 (§5.3 보강):
- `state.scout_candidates_buffer`: list or None. Stage 1 통합 전 임시 저장.
- `state.planner_candidates_buffer`: list or None. 동일.
- `state.last_stage1_completed_at`: ISO timestamp or null.
- `state.last_roadmap_report_cycle`: int or 0.

### 6.4 Idempotency / 안전장치

신규 status 4개에 대한 idempotency 보장 (기존 §9 idempotency 메커니즘과 정합):

| 보호 대상 | 메커니즘 |
|---|---|
| product-planner 중복 SendMessage | `state.planning_pending == True`면 skip. 5분 idempotency 가드 (Rev 6 R3-4 패턴 적용 — `planning_pending_started_at` + retried 플래그) |
| planning_creating 동시 invocation race | `planning_creating_lock` (owner-CAS, scout_creating_lock과 동일) |
| Planner candidate 중복 등록 | `planner-fp-{sha256(title+body)}` fingerprint 라벨 (Rev 6 R3-3 scout-fp 패턴 동일) |
| roadmap-strategist 중복 호출 | `state.roadmap_pending == True`면 skip + `last_stage1_completed_at` 5분 cooldown |
| vision-critic 25 사이클 cooldown 우회 | `last_vision_critic_cycle` 갱신을 응답 받기 **전**에 — 응답 timeout 시 중복 호출 방지. timeout 후 1회 재시도는 `last_vision_critic_cycle - 1`로 일시 차감 후 호출 |
| Vision delta 중복 제안 | `vision_history`에서 rejected_delta hash 검사 — 같은 (before, after) hash 매치하는 제안은 자동 거부 (응답 단계에서 lead-side drop) |
| Stage 2 무한 루프 (Stage 1 → Stage 2 → 또 Stage 1) | Stage 2 트리거 조건에 `last_roadmap_report_cycle != current_cycle` 명시 |

### 6.5 Timeout / Crash recovery

기존 §13 (lifecycle)의 timeout 가드 표에 추가:

| 신규 status | Timeout | 재시도 | 실패 시 |
|---|---|---|---|
| planning_pending | 10분 | 1회 ("JSON 한 줄로 재전송" 재요청) | candidates=[] 처리 + Stage 1 다른 쪽 (scout) 결과만으로 진행 |
| planning_creating | flock 트랜잭션 (즉시) | 없음 — flock 해제 후 idle 복귀 | 사용자 보고 + planner_history에 partial_creation 기록 |
| roadmap_pending | 10분 | 1회 | report 없이 Stage 2 skip + 다음 사이클로 |
| vision_check_pending (응답 대기) | 10분 | 1회 | vision_history에 timeout 기록 + 다음 25 사이클 후 재시도 |
| vision_check_pending (사용자 confirm 대기) | 24시간 | 없음 (parked) | parked_awaiting_human으로 전이 (기존 Rev 5 메커니즘) |

#### Crash recovery

- 5h+ usage limit gap 후 재개 시 (Rev 16 시나리오): 위 timeout이 모두 충분히 짧아 timeout 분기로 자동 정리됨 (Rev 14 F-A1 패턴 동일).
- `planning_creating_lock`의 stale owner: scout_creating_lock의 인수 정책 (Rev 7 §13) 그대로 적용 — flock atime 기반 owner 검증.

---

## 7. 재사용 메커니즘 (신규 helper 3개)

본 Rev 17의 설계 원칙 중 하나: **신규 helper 최소화**. cross-check 결과 3개 helper만 신규 추가 필요. 나머지 모든 분기는 기존 helper의 호출 인자만 다르게 사용. 신규 3개:

1. **`ensure_team_member(team, member) -> bool`** (§7.1) — lazy spawn. 신규 3명이 항상 spawn되지 않도록.
2. **`recover_team_context(state, member) -> str`** (§12.1) — respawn 후 핵심 context 부트스트랩 SendMessage 본문 생성.
3. **`prune_state_history(state) -> dict`** (§12.2) — long-term memory FIFO cap + TTL 자동 prune. Step −1에서 매 invocation 호출.

### 7.1 신규 helper — `ensure_team_member` + `discover_alive_teammates` (Round A A3/A8/I12)

**필요 이유**: 현재 `lifecycle.py:104`의 `team_alive()`는 등록된 모든 `REQUIRED_TEAMMATES`가 살아 있는지만 검증. 신규 3명을 즉시 `REQUIRED_TEAMMATES`에 추가하면 모든 사용자가 6명 teammate를 항상 spawn하게 되어 비용 ↑. **lazy spawn 필요** — Stage 1/2/3 호출 시점에 해당 teammate만 ensure.

**`lifecycle.py` 갱신**:

```python
REQUIRED_TEAMMATES = ("issue-analyzer", "tester", "issue-scout")  # 변경 없음
OPTIONAL_TEAMMATES = ("product-planner", "roadmap-strategist", "vision-critic")  # 신규 (Round A A3)
ALL_TEAMMATES = REQUIRED_TEAMMATES + OPTIONAL_TEAMMATES

def discover_alive_teammates(team_name: str) -> set[str]:
    """Read team config and return set of currently-spawned member names.

    Used by Step −0 to iterate watermark check across whichever teammates
    are actually alive (REQUIRED + lazily-spawned OPTIONAL subset).
    """
    if not team_name:
        return set()
    cfg_path = TEAMS_DIR / team_name / "config.json"
    if not cfg_path.exists():
        return set()
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        (m.get("name") if isinstance(m, dict) else m)
        for m in cfg.get("members") or []
    }

def ensure_team_member(team_name: str, member: str, state: Dict[str, Any]) -> bool:
    """Mark a teammate as needing spawn; return True if already alive.

    Validates the requested member is in ALL_TEAMMATES (allowlist). Reads
    config.json under a shared lock to mitigate partial-write races. The
    actual Agent tool call is a Claude-Code tool, not a Python API, so this
    helper sets state.pending_team_spawns for the lead to drain in Step 3.

    (Round A S9) Validates that agents/<member>.md exists and its frontmatter
    name matches `member` before allowing spawn (prevents attacker-planted
    agent definitions).
    """
    if member not in ALL_TEAMMATES:
        return False
    alive = discover_alive_teammates(team_name)
    if member in alive:
        return True
    # Validate agent definition file (Round A S9)
    agent_path = Path(__file__).parent.parent / "agents" / f"{member}.md"
    if not agent_path.exists():
        return False
    # (frontmatter name 매칭 검증 — Phase 17-B에서 구체 구현)
    pending = state.setdefault("pending_team_spawns", [])
    if member not in pending:
        pending.append(member)
    return False  # 아직 alive 아님 — lead가 다음 turn에 처리
```

**lead playbook (SKILL.md) 갱신**:

```
Step 3 — team 보장 (확장):
  ensure_team_alive(REQUIRED_TEAMMATES)  # 기존 — 필수 3명
  # Drain pending_team_spawns (Round A A8/I12)
  for member in state.pending_team_spawns[:]:
    Agent tool call로 member spawn
    state.pending_team_spawns.remove(member)
    state.teammate_health[member] = 초기 dict
    write()
    # spawn은 turn-ending action — return 후 다음 turn에서 SendMessage

Stage N 진입 시점:
  ensure_team_member(team, "<member>", state)
  if False (spawn 필요):
    write(); return  # 다음 turn에 Step 3에서 drain
  else:
    SendMessage(to=member, ...)
```

#### `team_alive()` 확장 (Round A A3)

기존 `team_alive(team_name)`는 REQUIRED만 검증 — 변경 없음. Step −0의 watermark 검사는 `discover_alive_teammates(team_name) ∩ state.teammate_health.keys()`로 iteration target 명시 (REQUIRED + 현재 alive인 OPTIONAL 모두 포함).

### 7.2 기존 helper / 메커니즘 재사용 매핑

| 기존 helper / 메커니즘 | 정의 위치 | 본 Rev 사용처 |
|---|---|---|
| `create_issues_with_fingerprint(candidates, done_list_field, created_field, failed_field)` | `docs/orchestrator-design.md` Rev 11 §19 | `planning_creating` 분기에서 `done_list_field=planner_history`, `created_field="issue_urls_created"`, `failed_field="creation_failures"`로 호출 |
| `scout_creating_lock` owner-CAS | Rev 6 R3-1 §19 | `planning_creating_lock`을 동일 패턴으로 구현. helper signature 변경 없음 (lock 파일 경로만 다름) |
| `sanitize_scout_body(body)` 화이트리스트 | Rev 4 §19 / Rev 5 강화 | product-planner candidate body에 그대로 적용 — 같은 화이트리스트 규칙 |
| `would_self_modify(candidate)` gate | Rev 4 §19 / Rev 5 한국어/우회 패턴 강화 | planner candidate에도 동일 적용 — orchestrator 파일 변경 요구 시 자동 제외 |
| `parse_acceptance_criteria(body)` | Rev 4 A1.5 §19 | planner-suggested 이슈 fast-path에 그대로 적용 (Rev 9 split sub-issue fast-path와 동일) |
| `pending_questions` 큐 (cap 20, target-based dedup) | Rev 13 C1 + Rev 14 F-E2 | roadmap report "phase context 채택?" 질문 push, vision delta confirm 질문 push |
| `push_pending_question(target, question, options)` | Rev 14 F-E2 helper | roadmap report 채택 prompt + vision delta confirm prompt에 사용 |
| Rev 13 A3 lesson injection | Rev 13 A3 §9 + §19 | vision_history → scout/planner/analyzer prompt에 직전 5개 vision 변경 history 주입. 추가 helper 불필요 |
| `audited_bash` wrapper | Rev 13 D3 + Rev 14 F-I2 | `gh issue create` (planner 후보 등록), `gh label create` (신규 라벨), vision 갱신 시 state 파일 갱신 — 모두 audited_bash 강제 |
| `compose_daily_digest` | Rev 13 C2 §19 | digest 컴포넌트에 planner 채택률 + roadmap 채택률 + vision delta 채택률 항목 추가 (helper 시그니처는 변경 없음, 입력 state에 신규 필드 포함되어 자동 반영) |
| `sanitize_feedback_message` | Rev 14 F-S1 §19 | vision-critic의 `rationale` 텍스트에 적용 (사용자에게 표시되기 전 sanitize) |
| `detect_lesson_pattern` | Rev 13 A3 §19 | planner_history / roadmap_reports의 reject 패턴에서 lesson 자동 추출 (호출 지점: planner candidate 거부 시 + roadmap context 거부 시) |
| `parked_awaiting_human` 24h gate | Rev 5 §9 / §13 | vision_check_pending (사용자 confirm) 24h timeout, planning_pending (사용자 confirm) 24h timeout |
| `flock_session` / `write_in_lock` | Rev 7 §19 | state.json v3 갱신 시 동일 트랜잭션 보호 |
| `read_last_task_result` | Rev 6 R3-12 §19 | 변경 없음. Stage 1 통합 단계에서 두 teammate 응답 읽기에 사용 |
| `format_answers` / `sha256` | Rev 5 §19 | planner candidate fingerprint 계산 (`sha256(title + body)`)에 사용 |
| AskUserQuestion multiSelect 패턴 | Rev 3 §12 | Stage 1 통합 후보 풀 confirm에 그대로 적용. 단 사용자에게 출처 표시 ("[scout]" vs "[planner]" prefix) |

신규 helper 0개를 달성하기 위한 설계 제약:
- planner 후보의 fingerprint는 scout과 같은 hash function 사용 (label 이름만 `planner-fp-{hash}`로 prefix 다르게).
- planning_creating 분기는 scout_creating 코드를 거의 그대로 복사 + state 필드 이름만 다르게 (Rev 11 호출자 분리 시그니처 활용).
- vision_history 갱신은 Rev 13 lessons_learned 갱신 패턴 동일.

→ 구현 시 **scout_creating 함수의 인자에 `creator_type: "scout"|"planner"` 추가**하면 80% 코드 재사용 가능.

---

## 8. 구현 Phase

기존 §17 (`docs/orchestrator-design.md:17` Phase 0~F) 이후에 본 Rev 17의 Phase가 추가된다. 단 기존 Phase가 모두 끝난 (Rev 16 제외) 상태를 전제.

### Phase 17-A — 라벨 부트스트랩 + state schema 확장

- **Goal**: GitHub 라벨 3개 추가 + `_empty_state()` 신규 필드 backfill 동작 보장.
- **정정**: 초안의 "v2→v3 마이그레이션"은 잘못. 현재 코드는 이미 `SCHEMA_VERSION = 3`이고 `_normalize` setdefault 패턴이 자동 backfill하므로 schema bump도 마이그레이션 코드도 불필요.
- **Tasks**:
  1. `plugins/orchestrator/python_helpers/orchestrator_state.py:_empty_state()`에 §5.3의 신규 필드 default 추가 (~20 라인).
  2. `plugins/orchestrator/python_helpers/lifecycle.py:LABEL_SPEC`에 `planner-suggested` / `roadmap-context` / `vision-update-pending` 3개 라벨 dict 추가 (~6 라인).
  3. `tests/test_orchestrator_state.py`에 backfill 검증 단위 테스트 추가: Rev 16 state 시뮬레이션 dict → `_normalize` → 신규 필드 모두 default로 채워지는지 확인 (~15 라인).
- **Done 기준**: 기존 v3 state.json이 있는 환경에서 `/orchestrator` 첫 호출 시 신규 필드가 default 값으로 자동 채워지고 기존 cycle 정상 진행. 신규 필드 직렬화/역직렬화 라운드트립 검증 테스트 통과.
- **추정 작업량**: 50줄 추가, 0.25일 (대폭 축소).

### Phase 17-B — Teammate 3명 정의 + lazy spawn helper

- **Goal**: agent 정의 파일 추가 + `ensure_team_member` lazy spawn helper 구현.
- **Tasks**:
  1. `plugins/orchestrator/agents/product-planner.md` 작성 (§4.1).
  2. `plugins/orchestrator/agents/roadmap-strategist.md` 작성 (§4.2).
  3. `plugins/orchestrator/agents/vision-critic.md` 작성 (§4.3).
  4. `plugins/orchestrator/skills/plan-issues/SKILL.md` 작성 (§4.4).
  5. `plugins/orchestrator/python_helpers/lifecycle.py`에 `ensure_team_member(team, member) -> bool` helper 추가 (§7.1). `REQUIRED_TEAMMATES`는 **변경하지 않음** — 신규 3명은 optional (lazy spawn).
  6. `plugins/orchestrator/python_helpers/wake_inference.py`의 `_parse_teammate_sender`에 신규 3명 인식 추가 (현재 issue-analyzer/tester/issue-scout만 인식). regex 또는 dict lookup 갱신.
  7. `tests/test_lifecycle.py`에 `ensure_team_member` 단위 테스트 (있을 때 True, 없을 때 pending_team_spawns 마킹).
  8. `tests/test_wake_inference.py`에 신규 3명 phase JSON 인식 케이스 추가.
- **Done 기준**:
  - `/orchestrator plan:true` 실행 시 lead가 ensure_team_member를 호출해 product-planner를 lazy spawn하고 SendMessage + JSON 응답 수신.
  - 같은 식으로 `roadmap:true` / `vision-check:true` 검증.
  - wake_inference가 `{"phase":"plan",...}`, `{"phase":"roadmap",...}`, `{"phase":"vision_check",...}` 모두 `teammate_reply`로 인식.
- **추정 작업량**: 950줄 추가 (agent prompt 800 + helper 50 + tests 100), 1.5일.

### Phase 17-C — Stage 1 병렬 호출 + 통합 로직

- **Goal**: Step 5의 `picker.pick() == 0` 분기를 Stage 1으로 갱신.
- **Tasks**:
  1. Step 5 분기에 scout + planner 병렬 SendMessage 추가.
  2. wake_inference.py에 stage1_merge_ready 분기 추가.
  3. 두 buffer 통합 + dedup (fingerprint + cosine) + would_self_modify gate + sanitize_scout_body 적용.
  4. AskUserQuestion multiSelect (cap 8, 출처 prefix 표시).
  5. scout_creating 코드를 `creator_type` 인자 추가하여 planning_creating에서도 재사용 가능하게 리팩토링.
- **Done 기준**: 이슈 바닥 상태에서 `/orchestrator` 1회 호출 → scout + planner 동시 응답 → 통합 후보 풀 → 사용자 confirm → GitHub 등록 → 다음 사이클에서 picker 픽업.
- **추정 작업량**: 600줄 추가, 2일.

### Phase 17-D — Stage 2 + Stage 3 자동 트리거

- **Goal**: roadmap-strategist + vision-critic 자동 호출 흐름 완성.
- **Tasks**:
  1. Stage 1 완료 후 Stage 2 자동 진입 분기 추가 (state.last_stage1_completed_at 5분 window).
  2. roadmap 응답 sanity check + pending_questions push.
  3. Step −3에서 phase context 채택/거부 분기 (push_pending_question 재사용).
  4. Step −2를 vision-critic 강화 버전으로 갱신 (기존 Rev 13 D1 reflection 흡수).
  5. vision_delta 사용자 confirm + state.vision 업데이트 + lesson injection 활성화.
- **Done 기준**:
  - Stage 1 → Stage 2 → 사용자 phase context 채택 → 다음 Stage 1 호출 시 active_phase_context가 scout/planner SendMessage에 PHASE_CONTEXT 블록으로 자동 주입됨.
  - 25 사이클 후 vision-critic 자동 호출 → vision delta 제안 → 사용자 confirm → state.vision 갱신.
- **추정 작업량**: 500줄 추가, 2일.

### Phase 17-E — Epic split 흐름 통합 + 라벨 매칭

- **Goal**: planner-suggested + split-epic 라벨 이슈가 picker에서 자동으로 FORCE_SPLIT analyzer 호출되는 흐름.
- **Tasks**:
  1. picker가 라벨 검사 후 FORCE_SPLIT 자동 부착 분기 추가.
  2. analyzer prompt에 FORCE_SPLIT 처리 명시 (이미 Rev 9 Round B fix H로 정의됨 — 검증만).
  3. Epic 자체의 dev-task 호출 차단 (picker가 split-epic-marker 부착된 이슈 skip — Rev 10 fix A 재사용).
- **Done 기준**: planner-suggested Epic 등록 → 다음 사이클에서 picker가 FORCE_SPLIT으로 analyzer 호출 → split_confirm → sub-issue 생성 → 원 Epic skip.
- **추정 작업량**: 100줄 추가 (대부분 기존 메커니즘 재사용), 0.5일.

### Phase 17-F — 통합 테스트 + fixture

- **Goal**: 전체 흐름의 end-to-end fixture 기반 테스트.
- **Tasks**:
  1. `docs/orchestrator-test-fixtures/`에 신규 fixture 추가:
     - `planning-layer-stage1.json` (scout + planner 통합 시나리오).
     - `planning-layer-stage2.json` (roadmap report 채택/거부).
     - `planning-layer-stage3.json` (vision-critic 갱신 시나리오).
     - `planning-layer-epic-split.json` (planner Epic → analyzer split → sub-issue).
  2. 4개 fixture에 대한 FSM end-to-end test (Phase F에서 정의한 패턴 따름).
  3. PoC: dummy vision + 비어 있는 repo로 1 사이클 돌려 4개 stage 모두 발동 확인.
- **Done 기준**: 4개 fixture 모두 통과 + PoC 시연 가능.
- **추정 작업량**: 300줄 추가 (fixture + test), 1일.

### Phase 17-G — 메모리 관리 정책 통합 (§12 반영)

- **Goal**: Working memory watermark + long-term pruning + recover_team_context 구현.
- **Tasks**:
  1. `lifecycle.py`에 `recover_team_context(state, member)` helper 추가 (§12.1).
  2. `lifecycle.py`에 WATERMARK_CALLS / WATERMARK_TOKENS 상수 추가.
  3. `orchestrator_state.py:_empty_state()`에 `teammate_health` dict 필드 추가.
  4. `orchestrator_state.py`에 `prune_state_history(state)` helper 추가 (§12.2).
  5. `skills/orchestrator/SKILL.md`에 Step −0 (teammate health check) 신설 + Step −1에 prune 호출 추가.
  6. `tests/test_lifecycle.py`에 recover_team_context 단위 테스트 (member별 다른 context 반환).
  7. `tests/test_orchestrator_state.py`에 prune_state_history 단위 테스트 (FIFO cap + TTL 둘 다 검증).
- **Done 기준**: WATERMARK 도달 시뮬레이션 fixture에서 graceful shutdown → respawn → recover_team_context bootstrap 동작 확인.
- **추정 작업량**: 280줄 추가, 1일.

**Phase 17 총 작업량**: 약 2900줄 추가, 7-8일 (1주~1.5주).

### Critic round 계획

기존 Rev 1-15와 같이 4명 critic (architecture / edge case / implementability / security) 검토 round 통과 후 Phase 17-B 진입 권장. 첫 round에서 예상되는 주요 우려:

| 영역 | 예상 우려 |
|---|---|
| Security | (a) vision-critic이 자기 자신의 vision 갱신을 정당화하는 self-bootstrap 공격 표면 — §4.3의 자기 보존 가드로 일부 차단했으나 검증 필요. (b) planner candidate가 코드 변경을 사용자 가치 표현으로 위장해 would_self_modify gate 우회 가능성 |
| Architecture | (a) Stage 1 두 응답 통합 시점의 race (한쪽만 timeout 시) — buffer 패턴이 충분한가. (b) active_phase_context_until_cycle 만료 후에도 prompt 주입 잔존 가능성 |
| Edge case | (a) scout 후보 0개 + planner 후보 0개 + Stage 2 진입 — pending_questions에 의미 없는 질문이 쌓이는가. (b) vision-critic이 vision_history 비어 있을 때 panic 안 하는가 |
| Implementability | (a) planner_history / roadmap_reports / vision_history 세 FIFO를 동시 관리할 때 state.json 크기 증가 속도. cap 적정성 검증 |

---

## 9. 미해결 항목 (Phase 17 진입 전 재결정)

### 9.1 vision-critic 호출 빈도

- 현재 설계: 매 25 사이클. **재고**: 25 사이클이 너무 자주인지, 너무 드문지 unknown. 옵션:
  - A. 50 사이클로 늘림 (vision 갱신은 큰 사건이므로 자주 안 함).
  - B. 25 사이클 유지하되 alignment_score > 0.8이면 출력 자체를 skip (사용자에게 question 안 push).
  - C. 시간 기반 (예: 매 7일) — cycle counter와 무관.
- **Phase 17-D 진입 전 사용자와 확정 필요**.

### 9.2 Dedup 신뢰도 (scout + planner 후보 통합)

- 현재 설계: fingerprint hash + cosine 0.85 유사도. **재고**: cosine 측정 방법 (어떤 embedding? 단순 word overlap?) 미정의. 옵션:
  - A. 단순 SequenceMatcher ratio + title-only 비교.
  - B. sentence-transformers 같은 외부 의존 도입 (Phase 17-C 작업량 ↑).
  - C. dedup 자동화 포기 — 사용자가 multiSelect 단계에서 직접 판단 (가장 단순).
- **권장**: C로 시작 → 사용자 confirm fatigue 발생 시 A로 보강.

### 9.3 Phase context 주입 위치

- 현재 설계: 다음 Stage 1 호출 시 scout + planner 모두에 PHASE_CONTEXT 블록 주입. **재고**: analyzer에게도 주입할지? picker 우선순위에도 반영할지?
- 옵션:
  - A. scout + planner만 (현재). 가장 보수적.
  - B. analyzer에도 주입 (Epic split 결정 시 phase context 고려).
  - C. picker priority weight에도 반영 (예: phase_context의 focus 키워드 매칭 이슈 boost).
- **B + C**가 효과 클 것으로 예상되지만 복잡도 증가. Phase 17-D 종료 후 dogfooding 결과 보고 결정.

### 9.4 Epic의 dev-task 직접 호출 정책

- 현재 설계: Epic은 절대 직접 호출하지 않고 항상 split 거침. **재고**: 사용자가 split을 거부한 Epic은 어떻게 처리?
- 옵션:
  - A. 영구 needs_human (사용자 수동 처리).
  - B. 사용자가 "split 거부 + Epic 그대로 진행" 선택 시 Epic을 dev-task에 직접 호출 (loopd가 큰 PR로 처리). 리스크 큼.
  - C. 사용자가 Epic을 더 작은 후보로 수동 재작성 후 등록.
- **권장**: A (안전). Phase 17-E 진입 전 결정.

### 9.5 Roadmap report의 GitHub 영구 저장 여부

- 현재 설계: `roadmap-context` 라벨로 임시 issue 등록 가능하지만 디폴트 비활성 (state.roadmap_reports에만 저장).
- 옵션:
  - A. 디폴트 비활성 유지 (state 파일 안에서만).
  - B. 채택된 phase context는 GitHub issue로 영구 등록 (history 시각화 가능).
  - C. 채택+거부 모두 GitHub issue로 등록 (audit trail).
- **권장**: A로 시작. 사용자가 history 시각화 요구하면 B로 확장.

### 9.6 Working memory watermark 튜닝

- 현재 설계: 호출 횟수 (analyzer 20, vision-critic 3) + 추정 토큰 (150K~200K) 동시 적용.
- **재고**: 실제 토큰 사용량은 매 SendMessage마다 정확히 측정 어려움 (Claude Code teammate API가 직접 노출하지 않음). 추정 토큰은 SendMessage 본문 길이 + 응답 길이의 합으로만 근사 가능.
- 옵션:
  - A. 호출 횟수만 사용 (단순). 단점: 짧은 응답이 많은 teammate가 불필요하게 자주 respawn.
  - B. 추정 토큰 (본문 누적) 사용. 단점: 부정확.
  - C. Anthropic API의 usage 정보를 hook으로 캡처 (정확하지만 구현 복잡).
- **권장**: A로 시작 → Phase 17-G 후 dogfooding에서 respawn 빈도 측정 → 너무 자주면 B로 보강.

### 9.7 Watermark 도달 시 진행 중 작업 처리

- 현재 설계: graceful shutdown → respawn. 진행 중 SendMessage 응답을 기다리는 중이면?
- 옵션:
  - A. 응답 도착까지 대기 → 응답 처리 완료 후 respawn (안전).
  - B. 즉시 shutdown → 진행 중 응답 폐기 → 재요청.
  - C. 진행 중 작업의 importance에 따라 분기 (analyzer 응답 대기 중이면 A, vision-critic 응답 대기 중이면 B).
- **권장**: A (단순 + 안전). watermark는 hard limit이 아닌 soft signal.

### 9.8 Vision-critic의 외부 정보 접근

- 현재 설계: repo 내부 + vision_history + 처리 history만 input. WebFetch 비활성.
- **재고**: 진정한 disruption 사고를 하려면 경쟁 서비스 / 시장 / 사용자 feedback 같은 외부 input 필요.
- 옵션:
  - A. 현재 설계 유지 (Rev 17 완료 후 dogfooding).
  - B. Rev 18에서 WebFetch + 명시된 search query만 허용 (사용자가 vision-critic 호출 시 search hint 전달).
  - C. 사용자가 별도 `feedback:` 채널로 외부 시그널 주입 (Rev 13 C3 메커니즘 재사용) — 코드 변경 0.
- **권장**: C로 보강 + Rev 18에서 B 검토.

---

## 10. 메트릭 (효용성 평가)

기존 §15 (`docs/orchestrator-design.md`)에 추가:

| 메트릭 | 측정 방법 | 목표 |
|---|---|---|
| Planner 채택률 | 사용자 confirm 통과한 planner candidate / 총 도출 candidate | > 30% (낮으면 prompt 품질 문제) |
| Planner Epic 머지율 | planner-suggested Epic이 split → 모든 sub-issue 머지된 비율 | > 50% (낮으면 Epic 크기 부적절) |
| Roadmap 채택률 | 사용자 채택한 phase context / 총 보고 | > 40% |
| Phase context 영향도 | active_phase_context 활성 기간에 머지된 PR 중 focus 키워드 매치율 | > 60% (낮으면 context 주입 비효과적) |
| Vision delta 채택률 | 사용자 채택한 vision_delta / 총 제안 | > 20% (너무 높으면 vision-critic이 과적극) |
| Vision-critic false positive | vision_delta 사용자 거부 + 다음 5 사이클 내 같은 영역 재제안 횟수 | < 1회 (rejected_delta 매커니즘 검증) |
| Stage 1 통합 dedup 정확도 | 사용자가 multiSelect에서 "둘 다 비슷" 표시한 비율 | > 80% (dedup 누락이 적음을 의미) |
| Stage 2 진입 latency | Stage 1 완료 → roadmap 응답 도착까지 평균 | < 5분 |
| Respawn 빈도 (memory health) | 각 teammate의 respawn_count / 운영 일수 | < 0.5회/일 (즉 2일에 1회 미만) — 더 잦으면 watermark 상향 조정 |
| state.json 크기 | 운영 1주 후 STATE_PATH 크기 | < 1MB (2MB watermark 도달률 < 5%) |
| Long-term history prune rate | Step −1 호출당 prune된 entry 수 | < 5 entry/호출 (steady state) |
| Context bootstrap 정확도 | respawn 후 첫 응답이 sanity check 통과한 비율 | > 95% |

#### 효용성 판단 기준 (Rev 17 채택 여부)

다음을 모두 만족 시 Rev 17 메인 머지 결정 (Phase 17-F 완료 + 2주 dogfooding 후):
- Planner 채택률 > 30% AND Planner Epic 머지율 > 50%.
- Vision delta 채택률 > 20% AND false positive < 1회.
- 사용자가 "기획 레이어 도입 전후 비교, 처리 task의 사용자 가치가 명확히 증가했다" 명시 동의.

미달 시 Rev 17 branch 보존 + README에 결과 기록 후 archive (Rev 1-16과 동일 정책).

---

## 11. 에이전트 수 분석 — 3명으로 충분한가?

사용자 의문: "에이전트 3개만 추가하면 되는지 더 필요한지." cross-check 결과 결론을 먼저 제시: **3명으로 시작 충분**. 추가 7개 후보 중 1개 (history-curator)만 운영 중 토큰 압박 발생 시 도입 검토. 나머지 6개는 lead-side LLM thinking 또는 Python helper로 충분.

### 11.1 추가 후보 평가 표

| 후보 agent | 역할 | 별도 agent 필요성 | 대체안 | 결론 |
|---|---|---|---|---|
| **A. dedup-evaluator** | scout vs planner 후보 자동 dedup | 낮음 — fingerprint hash + cosine ratio는 결정적 계산. LLM thinking 불요 | `playbook_helpers.py`에 `dedup_candidates(scout_list, planner_list)` 함수 신설 (~30줄). SequenceMatcher 기반 | **보류** |
| **B. metric-analyst** | 정기 KPI 트렌드 분석, 이상치 감지 | 낮음 — §10 메트릭은 단순 카운트/비율. 통계 계산은 결정적 | `compose_daily_digest` 확장 + 트렌드 라인 추가 | **보류** |
| **C. dependency-mapper** | cross-issue dependency graph, cycle 감지 | 중간 — graph reasoning은 LLM이 잘하지만, 실 시나리오는 작은 그래프 (이슈 10개 미만) | issue.depends_on/blocked_by 필드 (Rev 13 A1) 위에 `playbook_helpers.py:topological_sort` 함수 | **보류** |
| **D. conflict-arbiter** | scout/planner 충돌 후보 자동 조정 | 낮음 — 충돌은 dedup-evaluator 분기로 흡수. 사용자 multiSelect로 fallback | A와 통합 | **보류** |
| **E. prompt-tuner** | 다른 agent prompt 자동 개선 | 위험 — self-modify에 가까운 메타 작업. Rev 5 R2-21 (would_self_modify) 정책 위반 가능 | 사용자 수동 prompt 갱신만 | **거부** (보안) |
| **F. risk-assessor** | tester verdict 외 추가 risk (보안/성능/비용) | 낮음 — tester의 `permission_elevation` 필드 (Rev 13 B2)가 이미 보안 risk 검출. 성능은 별도 도메인 (orchestrator 범위 외) | tester prompt 보강 (recommend_human_review 조건 확장) | **보류** |
| **G. history-curator** | 모든 history 필드 압축/요약, agent prompt에 주입할 최적화된 context 생성 | **중간-높음 — 6명 agent 운영 시 input 폭증 위험 (특히 vision-critic 50KB+)** | §12의 long-term memory pruning + summary 메커니즘으로 일부 흡수 | **운영 중 검토** — 토큰 압박 발생 시 Rev 18에서 도입 |

### 11.2 결론 — 단계적 도입 정책

```
Phase 17 (현재 PR):
  추가 agent: 3명 (product-planner, roadmap-strategist, vision-critic)
  추가 helper: 1개 (ensure_team_member) + dedup helper 권장 (§9.2 미해결)

Phase 18 (조건부):
  토큰/비용 watermark 발생 시:
    - history-curator agent 도입 (장기 history 자동 요약)
    - 또는 §12의 working memory shutdown/respawn 정책 강화로 대체
  대규모 dependency graph 필요 시:
    - dependency-mapper helper (agent 아닌 함수) 추가

영구 보류:
  prompt-tuner (보안 위험)
  metric-analyst (단순 통계로 충분)
```

### 11.3 7명 시스템의 운영 비용 추정

신규 3명 + 잠재 history-curator까지 추가 시 총 7명 agent. 매 invocation 시:

| Agent | 호출 빈도 (cycle 기준) | 평균 input | 평균 응답 | 1 사이클당 토큰 (gross) |
|---|---|---|---|---|
| issue-analyzer | 1회 / 사이클 | ~8KB | ~2KB | ~10K |
| tester | 1회 / 사이클 | ~12KB (PR diff 포함) | ~2KB | ~14K |
| issue-scout | 0.2회 (이슈 바닥 시) | ~10KB | ~3KB | ~13K × 0.2 ≈ 3K |
| product-planner | 0.2회 | ~12KB | ~4KB | ~16K × 0.2 ≈ 3K |
| roadmap-strategist | 0.2회 | ~15KB | ~3KB | ~18K × 0.2 ≈ 4K |
| vision-critic | 0.04회 (25 사이클 1회) | ~50KB | ~5KB | ~55K × 0.04 ≈ 2K |
| (history-curator) | 0.04회 (도입 시) | ~30KB | ~10KB | ~40K × 0.04 ≈ 2K |

**1 사이클당 평균 토큰 추정**: ~36K (gross). 100 사이클 (~1주 dogfooding) 기준 3.6M 토큰. Opus 기준 약 $54 (input) + $27 (output) ≈ $80/주. **수용 가능**.

vision-critic의 high-input (50KB)이 가장 큰 부담이지만 25 사이클당 1회만 호출되므로 amortize 됨. 운영 중 vision-critic input이 100KB+로 증가하면 history-curator 도입으로 압축 (§12).

---

## 12. 에이전트별 메모리 관리 정책

cross-check 결과 **기존 design 문서에 메모리 관리 명시 부족** 발견. 특히 6명 agent 운영 시 다음 3개 메모리 layer 모두 명시적 정책 필요:

| Layer | 위치 | 현재 상태 | Rev 17 정책 |
|---|---|---|---|
| **Working memory** | Claude Code teammate context (in-Claude-Code 누적) | 명시적 관리 0. `shutdown_marker`만 정의됨, 자동 호출 로직 없음 | §12.1 — shutdown/respawn watermark 정책 |
| **Long-term memory** | state.json 영속 필드 (lessons_learned, scout_history 등) | 일부 cap 명시 (audit_log 1000), 대부분 무한 누적 | §12.2 — FIFO cap + TTL + archive 회전 정책 |
| **Prompt context** | 각 agent SendMessage 본문에 주입되는 context | issue-scout만 명시 (`scout_history[-5:]`), 나머지 agent는 미정 | §12.3 — agent별 prompt context 주입 정책 |

### 12.1 Working memory — Shutdown/Respawn 정책

**문제**: teammate (특히 issue-analyzer, tester처럼 사이클마다 호출되는)는 Claude Code 내부에서 turn 단위로 context 누적. 1주 운영 후 100+ turn 누적 시:
- 입력 토큰 비용 ↑ (매 SendMessage마다 전체 history 재처리)
- 응답 품질 ↓ (오래된 무관 context가 attention noise)
- Memory cap (Anthropic API 200K 토큰)에 근접 시 truncation 발생

기존 design (§13 line 2618)에서 "completed_count % 10 == 0 또는 토큰 watermark 트리거"로 미해결 처리. Rev 17에서 정식 편입:

#### Watermark 정의

각 teammate별 두 watermark 동시 적용 (둘 중 먼저 도달하는 것):

| Watermark 종류 | issue-analyzer | tester | issue-scout | product-planner | roadmap-strategist | vision-critic |
|---|---|---|---|---|---|---|
| **호출 횟수** | 20회 (≈ 2일) | 20회 | 10회 (호출 빈도 낮음) | 10회 | 5회 (호출 빈도 낮음) | 3회 (input 큼) |
| **추정 토큰** (입력+응답 누적) | 150K | 200K (diff 포함) | 100K | 100K | 100K | 150K |

`state.teammate_health` 신규 필드로 추적:

```python
"teammate_health": {
    "issue-analyzer": {
        "spawned_at": "2026-05-22T10:00:00Z",
        "call_count": 0,
        "estimated_tokens": 0,  # 각 SendMessage 후 incremental update
        "last_response_at": None,
        "respawn_count": 0,
    },
    # ... 각 teammate마다 동일 구조 ...
}
```

#### Respawn 흐름

**정정 (Round A A4/I13)**: 초안에서 "Step −5 직전"이라 했으나 이는 Step −2의 reflection invariant를 깨뜨림 (D1 scout reflection이 한 cycle 지연). 정확한 위치는 **Step −1 (daily digest) 직후, Step 0 (arg parsing) 직전**. 즉 ordering은:

```
Step −5: Watch list 자동 만료
Step −4: Stale PR audit
Step −3: Pending questions flush
Step −2: Vision reflection (D1 scout) + vision-critic 25 cycle (offset 12)
Step −1: Daily digest + prune_state_history (Round A — long-term memory)
Step −0: Teammate health check (Round A 신설 — 이 위치)
Step 0:  Args parsing
Step 1:  state load
...
```

**Step −0 의사코드**:

```
Step −0: Teammate health check (Rev 17 신설, Round A 정정)
  # iteration target은 OPTIONAL 포함 alive 명단 (Round A A3)
  alive = discover_alive_teammates(state.team_name) ∩ state.teammate_health.keys()
  for member in alive:
    health = state.teammate_health[member]

    # Round A E6 — pending_respawn 플래그 (mid-cycle 검출분) 우선 처리
    if state.pending_respawn.get(member):
      target_member = member
    elif health.call_count >= WATERMARK_CALLS[member] OR \
         health.estimated_tokens >= WATERMARK_TOKENS[member]:
      # Round A S8 — call_count tamper 방어: cap 적용
      capped_count = min(health.call_count, WATERMARK_CALLS[member] + 5)
      if capped_count != health.call_count:
        audit.record_state_mutation(actor="orchestrator", action="teammate_health.call_count cap-clamp", payload={member, original:health.call_count, clamped:capped_count})
      health.call_count = capped_count
      # Rate-limit: 30분 내 respawn 1회 (Round A S8)
      if health.get("last_respawn_at") and now() - health.last_respawn_at < 30분:
        continue
      target_member = member
    else:
      continue

    # Round A A4 — in-flight pending status 있으면 1 turn 지연 (응답 처리 후 다음 invocation에서 respawn)
    if any_pending_status_for(target_member, state):
      state.pending_respawn[target_member] = True
      write(); continue

    # Graceful shutdown
    SendMessage(to=target_member, body=shutdown_marker(team_name))
    # 다음 wake에서 ack 확인 + Agent tool로 동일 정의 재spawn
    # 이후 recover_team_context(state, target_member) SendMessage
    state.teammate_health[target_member] = 초기화 + respawn_count++ + last_respawn_at=now()
    state.pending_respawn.pop(target_member, None)
    write(); return  # turn 종료
```

#### `recover_team_context` 신규 helper

```python
def recover_team_context(state: Dict[str, Any], member: str) -> str:
    """Return the bootstrap SendMessage body for a freshly-respawned teammate.

    The body re-establishes essential context the previous incarnation had:
    - Project vision (always)
    - Repo identity
    - Last 5 lessons relevant to this teammate
    - For analyzer/tester: current_issue context (if active cycle)
    - For scout: scout_history[-3:] summary
    - For planner: planner_history[-3:] summary
    - For roadmap/vision-critic: vision_critic_history[-3:] summary
    """
    parts = [
        f"You have just been (re)spawned. Restoring context.",
        f"Vision: {state.get('vision', '')[:500]}",
        f"Repo: {state.get('repo', '')}",
    ]
    lessons = state.get("lessons_learned", [])[-5:]
    if lessons:
        parts.append("Recent lessons:")
        for lesson in lessons:
            parts.append(f"- {lesson['pattern']} (seen {lesson['observed_count']}x)")

    if member in ("issue-analyzer", "tester"):
        ci = state.get("current_issue")
        if ci:
            parts.append(f"Current issue: #{ci.get('number')} status={ci.get('status')}")

    elif member == "issue-scout":
        hist = state.get("scout_history", [])[-3:]
        if hist:
            parts.append(f"Recent scout outcomes: {summarize(hist)}")

    elif member == "product-planner":
        hist = state.get("planner_history", [])[-3:]
        if hist:
            parts.append(f"Recent planner outcomes: {summarize(hist)}")

    elif member in ("roadmap-strategist", "vision-critic"):
        vch = state.get("vision_critic_history", [])[-3:]
        if vch:
            parts.append(f"Recent vision-critic outcomes: {summarize(vch)}")

    return "\n".join(parts)
```

#### Stateless prompt 원칙

Working memory 누적 자체를 줄이기 위해 **모든 Stage 호출은 stateless prompt** 원칙 — SendMessage 본문에 그 turn 처리에 필요한 모든 context (vision, history snippet, phase context) 명시. teammate가 이전 turn을 참조할 일 없게.

이 원칙은 기존 issue-analyzer/tester에도 적용 (이미 부분적으로 implicit). 신규 3명은 더 엄격히 — 각 SendMessage 본문 cap 20KB 권장 (vision-critic만 50KB 예외).

### 12.2 Long-term memory — state.json FIFO/TTL/archive 정책

**현재 누적되는 history 필드** (cap 명시 여부):

| 필드 | 현재 정책 | Rev 17 정정 |
|---|---|---|
| `lessons_learned` | cap 없음 | **cap 100 + TTL 90일** (observed_count 가중 priority — 자주 발생한 lesson 우선 보존) |
| `scout_history` | cap 없음 | cap 50 (FIFO) |
| `scout_history_log` | cap 없음 | cap 200 (FIFO, debug 용) |
| `vision_history` | cap 없음 | cap 30 (사용자 수동 갱신 history) |
| `vision_critic_history` (신규) | cap 20 (이미 명시) | 유지 + TTL 180일 |
| `planner_history` (신규) | cap 50 (이미 명시) | 유지 + TTL 120일 |
| `roadmap_reports` (신규) | cap 10 (이미 명시) | 유지 + TTL 90일 |
| `feedback_log` | cap 없음 | cap 100 (FIFO) + TTL 60일 |
| `audit_log` | 1000 + archive (Rev 14 F-E3) | 유지 (이미 정책 있음) |
| `pending_questions` | cap 20 (Rev 14 F-E2) | 유지 |
| `rejected_delta_hashes` (신규) | TTL 30일 | 신설 — 같은 vision delta 반복 차단용 |

#### Pruning 메커니즘

기존 design에 없던 통합 prune helper 신설:

```python
def prune_state_history(state: Dict[str, Any]) -> Dict[str, int]:
    """Apply FIFO caps and TTL to long-term history fields.

    Called from Step −1 (after daily digest) once per invocation.
    Returns {field: pruned_count} for digest.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    pruned: Dict[str, int] = {}

    # FIFO caps (간단)
    caps = {
        "lessons_learned": 100,
        "scout_history": 50,
        "scout_history_log": 200,
        "vision_history": 30,
        "vision_critic_history": 20,
        "planner_history": 50,
        "roadmap_reports": 10,
        "feedback_log": 100,
    }
    for field, cap in caps.items():
        lst = state.get(field, [])
        if len(lst) > cap:
            pruned[field] = len(lst) - cap
            state[field] = lst[-cap:]

    # TTL prune
    ttls = {
        "lessons_learned": 90,
        "vision_critic_history": 180,
        "planner_history": 120,
        "roadmap_reports": 90,
        "feedback_log": 60,
        "rejected_delta_hashes": 30,
    }
    for field, days in ttls.items():
        lst = state.get(field, [])
        cutoff = (now - _dt.timedelta(days=days)).isoformat()
        before = len(lst)
        # entry.get("ts") 또는 entry.get("last_at") 기준
        kept = [e for e in lst if (e.get("ts") or e.get("last_at") or "9999") >= cutoff]
        if len(kept) < before:
            pruned[field] = pruned.get(field, 0) + (before - len(kept))
            state[field] = kept

    return pruned
```

#### state.json 파일 크기 watermark

매 invocation Step −1 직후 `STATE_PATH.stat().st_size` 확인:
- > 2MB: 사용자에게 경고 emit + `prune_state_history()` 즉시 실행 + force write.
- > 5MB: 강제 archive — 모든 history 필드를 `~/.loopd/orchestrator/state_archive/<date>.json`으로 cold storage 이동, state.json은 최근 N개만 유지.

### 12.3 Prompt context 주입 정책 — agent별

각 agent의 SendMessage 본문에 주입할 context 양/내용을 명시적으로 정의 (기존 design에 issue-scout만 부분 명시).

| Agent | 주입 항목 (모두 stateless — 매 호출 self-contained) | 크기 cap |
|---|---|---|
| **issue-analyzer** | (1) issue body+comments, (2) `lessons_learned[-5:]`의 pattern + resolution, (3) `active_phase_context` (있을 때) | 10KB |
| **tester** | (1) PR URL + acceptance_criteria, (2) `lessons_learned[-5:]`, (3) 같은 PR 직전 verdict (rework 시) | 15KB |
| **issue-scout** | (1) vision + repo, (2) `scout_history[-5:]` summary (created title만), (3) `lessons_learned[-5:]`, (4) `active_phase_context` (있을 때), (5) 처리된 이슈 직전 10건 title | 15KB |
| **product-planner** | (1) vision + repo + SPEC docs path, (2) `planner_history[-5:]` summary (제출 title + 사용자 채택률), (3) `vision_critic_history[-3:]` (alignment_score만), (4) `active_phase_context` (있을 때), (5) `lessons_learned[-5:]` | 20KB |
| **roadmap-strategist** | (1) vision, (2) 최근 50건 머지 PR (title + label만, body 제외), (3) Stage 1 후보 풀 요약 (title + complexity만), (4) `roadmap_reports[-3:]` (current_phase + user_action만), (5) `vision_critic_history[-3:]` 의 alignment_score | 25KB |
| **vision-critic** | (1) vision + `vision_history` (전체 — cap 30이라 안전), (2) `vision_critic_history` (전체 — cap 20이라 안전), (3) 최근 50건 머지 PR title만 + 거부 이슈 통계, (4) `roadmap_reports[-5:]`, (5) `feedback_log[-10:]`의 사용자 의견 요약 | 50KB |
| **history-curator** (도입 시) | 모든 history 필드 raw — 요약본 생성 임무 | 100KB |

#### Lesson injection 정책 (Rev 13 A3 확장)

기존 Rev 13 A3는 `lessons_learned`를 analyzer/tester prompt에 자동 주입 (failure pattern 7개 fixed). Rev 17은 다음을 추가:
- `vision_critic_history` 의 채택된 delta는 lesson과 **별도 채널** ("Recent vision updates:")로 모든 agent에 주입. agent가 vision 변경 사실을 신속히 인지하도록.
- `roadmap_reports` 의 채택된 phase_context는 scout/planner/analyzer에 `active_phase_context`로 주입 (이미 §5.4에 명시).
- 즉 prompt 구조는:

```
<system: agent role>

# Current task
<task-specific input>

# Recent lessons (from lessons_learned)
- <pattern 1>
- ...

# Recent vision updates (from vision_critic_history accepted)
- 2026-05-15: vision changed from "X" to "Y" — rationale: ...

# Active phase context (from roadmap_reports user-accepted)
Phase: mvp-validation
Focus: ...
Critical path: ...

# Output contract
<JSON 명세>
```

### 12.4 Catastrophic forgetting 방지

Respawn 후 새 teammate가 "처음 보는 상태"로 인지하지 않도록 `recover_team_context`가 다음을 보장:
- vision/repo는 무조건 첫 줄.
- 직전 처리 결과 (해당 agent의 가장 최근 3-5건 outcome)은 항상 포함.
- 활성 cycle 진행 중 (current_issue.status가 active set)인 analyzer/tester는 추가로 current_issue 전체 body 재주입.
- Respawn 직후 첫 SendMessage 응답은 **sanity check 강제** — JSON 구조 검증 1회 실패 시 즉시 두 번째 respawn 시도 (3회까지). 그래도 실패 시 사용자에게 보고 후 needs_human.

### 12.5 메모리 관련 신규 helper / state 필드 정리

| 항목 | 종류 | 위치 |
|---|---|---|
| `recover_team_context(state, member)` | helper | `python_helpers/lifecycle.py` |
| `prune_state_history(state)` | helper | `python_helpers/orchestrator_state.py` (Step −1에서 호출) |
| `state.teammate_health` (dict) | state 필드 | `_empty_state()`에 추가 |
| WATERMARK_CALLS / WATERMARK_TOKENS 상수 | config | `python_helpers/lifecycle.py` |
| Step −0 (teammate health check) | playbook step | `skills/orchestrator/SKILL.md` |
| state.json 파일 크기 watermark | check | Step −1 보강 |

**작업량 추정 (메모리 관리 추가분)**: 250줄 추가, 1일 (Phase 17-A 또는 17-B 안에 흡수, 별도 phase 신설 안 함).

---

## 13. 부록 — 한 페이지 요약 카드

```
┌──────────────────────────────────────────────────────────────────────┐
│ Rev 17 — Planning Layer (3 신규 agent + 메모리 관리 정책)             │
├──────────────────────────────────────────────────────────────────────┤
│ Agents (3 신규, lazy spawn):                                          │
│   product-planner   → Epic (complexity 3-4, criteria 7+)              │
│   roadmap-strategist → 방향성 보고 (task 아님)                         │
│   vision-critic     → vision 갱신 제안 (사용자 confirm 필수)           │
│                                                                       │
│ Trigger:                                                              │
│   Stage 1 (이슈 바닥): scout + product-planner 병렬                    │
│   Stage 2 (Stage 1 후): roadmap-strategist 1회                        │
│   Stage 3 (매 25 cycle, offset 12): vision-critic                     │
│   (기존 Rev 13 D1 scout reflection은 그대로 25 cycle, offset 0 유지)   │
│                                                                       │
│ Slash 인자: /orchestrator plan:true | roadmap:true | vision-check:true│
│                                                                       │
│ 신규 라벨 3개: planner-suggested, roadmap-context, vision-update-pending│
│ 신규 status 4개: planning_pending, planning_creating,                 │
│                 roadmap_pending, vision_check_pending                 │
│ 신규 state 필드 ~22개 (모두 _empty_state()에 default만 추가 — schema  │
│                       bump 불필요, _normalize backfill 자동)          │
│ 신규 helper 3개: ensure_team_member, recover_team_context,            │
│                 prune_state_history                                   │
│                                                                       │
│ Epic 흐름: planner → split-epic 라벨 → analyzer FORCE_SPLIT →         │
│            sub-issue들 → dev-task                                      │
│                                                                       │
│ 메모리 정책 (§12):                                                     │
│   Working memory:    teammate별 watermark → graceful shutdown/respawn │
│   Long-term memory:  FIFO cap + TTL (60~180일) + 2/5MB watermark      │
│   Prompt context:    agent별 cap (10~50KB), stateless 원칙             │
│   Catastrophic forgetting: recover_team_context로 respawn 시 부트     │
│                                                                       │
│ 에이전트 수 결론 (§12): 3명 + 운영 중 토큰 압박 시 history-curator     │
│                       1명 추가 검토. 기타 6개 후보는 lead-side/helper  │
│                                                                       │
│ 구현: Phase 17-A~F, 약 2900줄, 7-8일                                   │
│                                                                       │
│ 미해결: vision-critic offset cadence 적정성, dedup 신뢰도,             │
│         phase context 주입 범위, Epic split 거부 시 처리,              │
│         roadmap GitHub 영구 저장, vision-critic 외부 정보,             │
│         watermark 튜닝 (호출 횟수 vs 토큰 추정)                        │
└──────────────────────────────────────────────────────────────────────┘
```

