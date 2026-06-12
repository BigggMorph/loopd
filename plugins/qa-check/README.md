# qa-check (experimental)

PR-레벨 QA 에이전트. PR diff + 티켓 의도에서 **L3(브라우저) QA 시나리오**를 생성해
Playwright spec으로 컴파일하고, 실행 결과를 **사람의 머지 판단을 위한 증거 리포트**로 종합한다.

> 머지 판단은 사람이 한다. 에이전트의 일은 증거를 만드는 것이다 — auto-merge는 비목표.

## 파이프라인

```
/qa-check <PR번호|커밋>
 ├─ A 분석     diff + 티켓 → 영향 사용자 플로우, risk, 회귀 선택      → qa/<id>/impact.md
 ├─ B 시나리오  의도(티켓) > diff > 코드 순으로 근거 → spec 컴파일      → qa/<id>/scenarios.yaml + tests/e2e/qa/*.pw.ts
 ├─ D 실행     실패 → 재실행 → BUG/FLAKE/BLOCKED 분류 (expect 수정 금지)
 └─ E 리포트   custom reporter가 머지 판단 요약 자동 생성              → qa/<id>/report.auto.md
```

## 핵심 설계 원칙

- **expected는 의도에서**: 코드에서 기대값을 만들면 버그까지 스펙이 된다 (코드 기반 LLM 테스트의 99%+가 옛 행동에 정렬 — arxiv 2603.23443. intent-aware 생성은 회귀 검출 2배 — Meta, arxiv 2601.22832).
- **SPEC-UNCLEAR는 `test.fixme` 플레이스홀더로**: 실행 못 한 가장 중요한 발견이 리포트에서 누락되지 않게.
- **expect 수정 금지**: spec 수리는 셀렉터·타이밍까지만. 증거의 신뢰가 제품이다.
- **리팩토링 가드**: 행동 불변 PR은 회귀 라이브러리가 곧 의도. 새 코드에서 기대값 생성 금지.

## 첫 실전 결과 (레퍼런스)

Next.js 앱의 실제 PR(복지사 대시보드 UX)에 대해:

```
✅ 9 · ❌ 0 · 🚧 1 (BLOCKED — seed 부재) · ❓ 1 (SPEC-UNCLEAR)
머지 전 확인 필요: S-07 — 타 복지사의 PATCH가 silent no-op + 200 반환 (권한 처리 의도 미확정)
```

- L1/L2를 통과한 커밋에서 **권한 처리 버그 후보를 사람 질문으로 격상** (S-07)
- 첫 실행 실패 1건을 spec 결함(strict mode 셀렉터 모호)으로 분류·수리 — expect 불변
- 회귀 4종 2회 연속 그린 (결정성)

## 상태

v0.1 — A/B/E 검증 완료, D(재실행·자동 분류)와 C(ephemeral 환경 자동 기동)는 수동/부분 구현.
