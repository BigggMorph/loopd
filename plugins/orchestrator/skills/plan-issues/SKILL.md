---
name: plan-issues
description: Rubric for deriving Epic-level issue candidates from a user-scenario perspective.
---

# Plan-Issues Toolkit

This skill auto-loads for the `product-planner` teammate.

## Information gathering order (highest signal first)

1. **User specification docs** — `SPEC.md`, `docs/product/`,
   `docs/user-stories/`. If present, these outrank everything else.
2. **README.md** — "Features" / "What it does" / "Use cases" sections
   articulate the explicit user promise.
3. **CLAUDE.md** — `## Vision` / `## Non-goals` sections set the
   boundary.
4. Recent 25 cycles of merged PR titles — simple keyword tally tells
   you where momentum is.
5. User-authored open issues (filter out `orchestrator-managed` and
   `scout-suggested` labels) — these are implicit needs.
6. Manifest `description` fields as a fallback.

## Epic-derivation heuristics

- **User-journey mapping (4 stages)**: discover → onboard → use →
  retain. For each stage, ask: does the vision promise a value here
  that the codebase does not yet deliver? Each gap is a candidate.
- **Implicit-needs extraction**: needs the user did not state but that
  the vision must obviously cover. Example: vision "AI conversation
  simulator" implicitly needs conversation history, replay, sharing.
- **Out-of-Scope discipline**: every Epic must name 1-2 things it does
  not cover, to prevent unbounded expansion.

## User-value framing (mandatory)

- ❌ Wrong: "Database migration", "API refactor", "Add test".
  → These belong to issue-scout.
- ✅ Right: "User can share their conversation simulation with others",
  "Enterprise customer can run staff training with a custom persona".

## complexity_level mapping (per Epic, before split)

- Epic itself is always 3 or 4.
- `3` — splits into 5-7 sub-issues, ~1-2 weeks.
- `4` — splits into 8+ sub-issues, architectural change, 2+ weeks.

## Required Epic-body sections

Every candidate body MUST include, in this order:

1. `## Problem` — user-scenario framing of why this Epic exists.
2. `## User Story` — `As a <role>, I want <goal>, so that <value>.`
3. `## Acceptance Criteria` — 7+ checklist items, user-observable.
4. `## Out of Scope` — 1-2 explicit non-goals.

## Token / cost guard

- Each candidate body ≤ 2 KB.
- Total candidates 2-4. ≥ 5 triggers user confirm fatigue.
- WebFetch disabled in this Rev (per agent prompt).

## Vision-too-abstract escape

If the vision is so abstract that no Epic can be derived, reply with:

```json
{"phase":"plan","status":"need_vision_clarification","question":"<one specific question>"}
```

The lead surfaces this via AskUserQuestion and may re-send with
clarification.
