---
name: scout-issues
description: Heuristics + tools for deriving candidate issues from a project vision.
---

# Issue Scouting Toolkit

This skill auto-loads for the `issue-scout` teammate.

## Information gathering order (highest signal first)

1. **Repo docs** — `README.md`, `CLAUDE.md`, `docs/`, `CONTRIBUTING.md`,
   `ROADMAP.md`. (Cap each at 5 KB.)
2. **Manifest files** — `package.json`, `pyproject.toml`, `Cargo.toml`
   tell you the stack/domain.
3. **All issues (open + closed)** — patterns you can learn from
   closed ones tell you what the maintainers actually merge.
   ```bash
   gh issue list --state all --limit 100 \
     --json number,title,labels,state,closedAt
   ```
4. **Recent merged PRs** — direction of recent work:
   ```bash
   gh pr list --state merged --limit 30 --json title,body,labels
   ```
5. **(Optional) external context** — vision-domain WebFetch, ≤ 2 URLs,
   10 KB each, time-boxed to 5 minutes total.

## Candidate-derivation heuristics

- **Vision-gap mapping**: break vision into 3-7 sub-goals; for each
  sub-goal, ask "what's missing?" — pick one concrete candidate.
- **Atomic first**: aim for complexity 0-2. Allow at most one
  complexity 3.
- **Duplicate avoidance**: skip candidates whose title cosine-similarity
  vs. an existing issue is ≥ 0.7. Skip candidates resembling a closed
  issue (especially `wontfix`).

## Label conventions (every candidate)

- Always include: `scout-suggested`.
- Add: `priority/<high|medium|low>`, `complexity/<0-4>`.
- Pick one category label: `enhancement`, `bug`, `docs`, `refactor`,
  `test`.

## Vision-too-abstract escape hatch

If the vision is so high-level you cannot meaningfully derive concrete
candidates, reply:

```json
{"phase":"scout","status":"need_vision_clarification","question":"<one specific question>"}
```

The lead will relay your question via AskUserQuestion and then re-send
the prompt with the clarification appended.

## Token guard

If approaching token limits, abort cleanly:

> SendMessage(to="team-lead",
>   "토큰 한도 — 부분 결과로 진행할지?")

and wait for instructions rather than producing a half-baked candidate list.
