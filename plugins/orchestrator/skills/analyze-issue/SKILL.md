---
name: analyze-issue
description: Toolkit and rubrics for analyzing a single GitHub issue — gh commands, human-needed checks, complexity mapping.
---

# Issue Analysis Toolkit

This skill auto-loads for the `issue-analyzer` teammate.

## gh CLI shortcuts

```bash
gh issue view <N> --repo <owner/repo> \
  --json title,body,labels,comments,assignees,milestone,reactions

gh issue list --repo <owner/repo> --state open \
  --json number,title,labels,reactions,createdAt \
  --jq 'sort_by(-.reactions.totalCount, .createdAt)'

gh issue view <N> --comments  # comments only
```

## should_process decision table

| reject_category | Signal |
|---|---|
| `spam` | body = ad / link salad / bot-generated noise |
| `duplicate` | matches an existing issue (open or closed) by core ask; **must** include `duplicate_of` URL |
| `invalid` | no reproduction steps, no expected behavior, no env info |
| `out_of_scope` | unrelated to the project's vision/domain |
| `already_resolved` | a `Fixed in #N` comment or recent merged PR resolves it |
| `question_only` | discussion/question — no code change requested |
| `wontfix_candidate` | business-rule "never" (e.g. opposite of declared vision) |

Confidence < 70% → leave `should_process=true`. For `duplicate`, the
`duplicate_of` URL is mandatory.

## human_needed rubric

### Always set `human_needed=true`
1. Body is a single sentence + no reproduction steps.
2. UX / copy decision ("which text on the button?").
3. Business rule decision ("should we allow X?").
4. New external dependency decision ("can we use this library?").
5. Breaking change or migration potential.
6. Auth / permissions / secrets touched.
7. Labels include `needs-discussion`, `question`, `help-wanted`.

### `human_needed=false` is OK when
1. `good-first-issue` label + body has clear reproduction.
2. Simple typo / docs fix.
3. Clear bug reproduction in body + explicit expected behavior.
4. A pattern matching a previously processed issue.

### When in doubt → `human_needed=true` (conservative).

## complexity_level mapping (passed as `level:` to `/dev-task`)

| Level | Meaning |
|---|---|
| 0 | One-line fix / typo |
| 1 | Single file, small change |
| 2 | Multiple files, follows existing patterns |
| 3 | New module / new feature |
| 4 | Architecture change |

## dev_task_prompt format

- ≤ 300 characters.
- 1-3 sentences. State the requirement, not the implementation.
- Reference specific files only if obvious from the issue.
- Don't paste the whole issue body — distill it.

Example (good):
> Fix the login form so the submit button stays enabled when the email
> field has a valid format but the password field is empty. Add a unit
> test in `tests/forms/`.

Example (bad — too long and prescriptive):
> Open src/components/LoginForm.tsx, find the useEffect on line 42,
> change the disabled prop calculation to also check the password... [200 more chars]
