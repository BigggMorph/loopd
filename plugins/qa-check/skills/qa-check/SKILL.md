---
name: qa-check
description: PR-레벨 QA 에이전트. PR diff와 티켓 의도에서 L3(브라우저) QA 시나리오를 생성해 Playwright spec으로 컴파일하고, 실행 결과를 머지 판단 증거 리포트로 종합한다. /qa-check <PR번호|커밋범위>로 호출.
---

# /qa-check — PR QA 에이전트

> **머지 판단은 사람이 한다. 너의 일은 사람이 판단할 수 있는 증거를 만드는 것이다.**
> L1(유닛)/L2(라우트)는 개발 중에 이미 수행됐다고 가정한다. 너는 **L3 — 사용자가 브라우저에서 보는 행동** — 만 다룬다.

## 입력

`$ARGUMENTS` = PR 번호 또는 커밋/커밋범위.

- PR 번호: `gh pr view <n> --json title,body,url` + `gh pr diff <n>`
- 커밋(범위): `git show -s --format='%B' <ref>` + `git diff <ref>^ <ref>` (범위면 `git diff A..B`, `git log --format='%B' A..B`)
- gh가 실패하면 커밋 범위로 폴백한다.
- 산출물 디렉토리: `qa/<id>/` (id = `pr-<번호>` 또는 짧은 커밋 해시)

## A0단계 — 대상 repo 관례 파악 (최초 1회)

처음 보는 repo면 먼저 파악하고, 결과를 `qa/conventions.md`에 기록해 다음 실행에서 재사용한다:

1. `playwright.config.*`: testDir, testMatch, baseURL, webServer, 기존 reporter.
2. 기존 E2E spec의 위치·스타일·env 게이트(예: `RUN_*_QA=1` 가드)·seed/auth 헬퍼.
3. dev 로그인/seed 경로 존재 여부 (없으면 사람에게 확인 — L3 실행의 전제조건).
4. 신규 spec 디렉토리: `tests/e2e/qa/` (없으면 생성). 신규 spec env 게이트: `RUN_QA_AGENT=1`.

## A단계 — 임팩트 분석 → `qa/<id>/impact.md`

1. **변경 파일 분류**: UI 컴포넌트 / 페이지·라우트 / lib·가드·DB 접근 / 마이그레이션 / 테스트·설정·문서(QA 불필요).
2. **white-box 추적**: 변경된 lib/컴포넌트를 어떤 페이지가 import하는지 **코드를 실제로 읽어** 따라간다. 추측 금지.
3. **사용자 플로우로 번역**: 파일 목록이 아니라 "어떤 화면에서 사용자가 무엇을 하는 흐름"으로 표현한다.
4. **risk 등급**: `high`(권한·인증·돈·데이터 삭제·개인정보·사용자 안전) / `med`(핵심 플로우 동작 변경) / `low`(문구·스타일·레이아웃).
5. **회귀 선택**: `tests/e2e/qa/` 및 기존 E2E 라이브러리에서 영향 플로우와 겹치는 시나리오를 나열한다. 대상 커밋이 기존 spec을 함께 갱신했다면 **갱신본을 회귀로 재실행**한다 (이전 버전 재실행은 셀렉터가 깨지므로 불가).
6. **리팩토링 가드**: 행동 불변(순수 리팩토링) PR도 실행은 동일하게 한다. 바뀌는 것은 시나리오 출처다 — 회귀 라이브러리 최우선(라이브러리가 곧 의도), 변경 후 코드에서 새 기대값 생성 금지, 커버리지 공백이면 **변경 전 행동**에서 시나리오를 생성한다(옛 행동이 곧 스펙).

`impact.md` 구성: 변경 요약(≤3줄) → 영향 플로우 표(플로우 / 근거 파일 / risk) → 회귀 재실행 목록 → QA 불필요 변경 목록(이유 포함).

## B단계 — 시나리오 생성 → `qa/<id>/scenarios.yaml` + spec 컴파일

원칙 (위반 금지):

- **expected는 의도에서 끌어온다.** PR 본문·티켓·커밋 메시지가 주장하는 행동이 기대값의 근거다. 코드에서 기대값을 만들면 코드의 버그까지 스펙이 된다 (실측: 코드 기반 LLM 생성 테스트의 99%+가 옛 행동에 정렬됨 — arxiv 2603.23443). 의도 문서로 기대 행동을 판단할 수 없으면 `spec_unclear: true`를 표시하고 사람에게 물을 질문을 적는다.
- **코드는 edge 발굴용.** 분기·가드·에러 경로를 읽고 happy path 외 시나리오(권한 없음, 빈 데이터, 중복 제출 등)를 추가한다.
- **시나리오 수는 risk에 비례.** low는 0~1개. 과잉 생성은 실행 비용이다.

```yaml
- id: S-<id>-01
  source: ticket | diff | library     # library = 기존 시나리오 회귀 재실행
  risk: high | med | low
  flow: "한 줄 사용자 플로우"
  preconditions: ["seed: ..."]
  steps: ["사용자 행동 단위 스텝", ...]
  expected: ["관찰 가능한 화면 결과", ...]
  spec: tests/e2e/qa/s-<id>-01.pw.ts  # 신규만. library는 기존 경로
  spec_unclear: false                  # true면 question 필드 필수, spec은 fixme 플레이스홀더로
```

spec 컴파일:

- 위치: `tests/e2e/qa/s-<id>-XX.pw.ts`. 스타일: 대상 repo의 기존 spec을 따른다 (기본형: `test.step` 단위 + 한국어 스텝 설명 + 기존 auth/seed 헬퍼 재사용).
- 가드: `test.skip(process.env.RUN_QA_AGENT !== "1", "set RUN_QA_AGENT=1")`
- **셀렉터는 컴포넌트 코드를 읽고 실제 텍스트/role에서 가져온다.** 추측한 셀렉터 금지. 코드에 없는 문구를 expect에 쓰지 마라.
- **annotations 필수**: 본문 첫 줄에 `test.info().annotations.push({ type: "risk", ... }, { type: "source", ... })` — qa-reporter와 HTML 리포트가 읽는다.
- **SPEC-UNCLEAR도 spec 파일로 만든다**: `test.fixme(제목, { annotation: [verdict/risk/question] }, 빈 본문)` 플레이스홀더. 실행은 안 되지만 리포트에 "❓ 사람 답변 필요"로 노출된다 — 가장 중요한 발견이 리포트에서 누락되는 것을 막는다.
- **seed 규칙**: 공유 seed 엔티티를 변형(mutation)하지 마라 — 다른 테스트의 재사용 조회가 깨진다. 수정·삭제 시나리오는 spec이 데이터를 직접 발급하고 끝에 정리하는 자급 패턴으로 작성한다 (보일러플레이트 중복 시 `tests/e2e/qa/helpers.ts` 허용). seed로 만들 수 없는 데이터 의존 시나리오는 전달용 환경변수 미설정 시 `test.skip`하는 패턴으로 (→ BLOCKED 판정으로 연결).

## D단계 — 실행·판정

- 실행 전 환경 점검: baseURL 포트에 **무관 프로세스가 없는지** 확인 (IPv4/IPv6 양쪽 — 한쪽만 점유돼도 간헐 가짜 실패가 난다).
- 실패 → 1회 재실행 → 분류: `BUG`(재현, 기대와 다름) / `FLAKE`(재실행 통과) / `BLOCKED`(환경) / spec 결함(셀렉터·타이밍).
- **expect 수정 금지.** spec 수리는 셀렉터·타이밍에 한정, 최대 2회, 이후 BLOCKED. 기대값을 통과하도록 고치는 순간 증거 신뢰가 죽는다.

## E단계 — 머지 판단 리포트

`templates/qa-reporter.ts`(이 플러그인 동봉)를 대상 repo `tests/e2e/qa/qa-reporter.ts`로 복사하고 `playwright.config` reporter 배열에 추가한다 (`RUN_QA_AGENT=1`일 때만 동작).

```
QA_RUN_ID=<id> RUN_QA_AGENT=1 <기존 회귀 env=1> npx playwright test --grep "<회귀 글롭>|S-<id>"
→ qa/<id>/report.auto.md + Playwright HTML 리포트(raw 증거: 영상·trace)
```

verdict 매핑: passed→✅ / failed→❌(BUG·FLAKE 분류 필요) / skip→🚧 BLOCKED(사유 포함) / fixme→❓ SPEC-UNCLEAR.
리포트에 **검증하지 않은 것**(영향 플로우 − 실행된 시나리오)을 명시하라 — 사람의 머지 판단은 "무엇을 안 봤나"에도 의존한다.

## 출력 (사람 리뷰용)

```
영향 플로우 N개 · 신규 시나리오 N개 (high X / med Y / low Z) · 회귀 N개 · SPEC-UNCLEAR N개
→ qa/<id>/report.auto.md · 머지 전 확인 필요: <목록>
```
