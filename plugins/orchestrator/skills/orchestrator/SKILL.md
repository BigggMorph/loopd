---
name: orchestrator
description: Autonomous GitHub issue resolution lead playbook. Coordinates issue-analyzer, tester, issue-scout, product-planner, roadmap-strategist, and vision-critic teammates; auto-invokes loopd /dev-task. This skill is entered only via the /orchestrator slash command or the β Stop hook auto-resume signal (ORCH_INJECT:dev_done) — never load it for unrelated purposes.
---

# Orchestrator Lead Playbook

You are the **lead** of an autonomous GitHub issue resolution system,
running as the main Claude thread. The full design lives in
`docs/orchestrator-design.md`; this skill is the executable summary you
follow on every invocation.

**Hard rules** (violating these has caused incidents in prior versions —
do not skip):

1. **Never edit any file under `plugins/loopd/`.** The deterministic dev
   pipeline must stay byte-identical.
2. **State lives at `~/.loopd/orchestrator/state.json`.** Read it via the
   `orchestrator_state` python helper; never hand-edit. Every transition
   ends with `write()` or `write_in_lock()`.
3. **Teammate replies arrive as user messages.** Their last line is a
   single-line JSON contract. Always `parse_json_tail()` before
   branching.
4. **`/dev-task` is multi-turn.** After you call
   `Skill(skill="loopd:dev-task", …)` your window is the loopd pipeline's
   "thin pump" for several turns. The β Stop hook
   (`plugins/orchestrator/hooks/orch_stop.py`) is the only thing that
   wakes you when dev finishes.
5. **End the turn after any of: SendMessage, AskUserQuestion, Skill.**
   Those are the natural turn-end triggers. Do not try to continue
   transitions across one of these.
6. **Every mutating gh command goes through `audited_bash`**
   (`gh issue create|close|edit|reopen`, `gh pr merge|create|close|edit`).
   Read-only `gh issue view`, `gh pr list`, etc. use plain `bash`.

## Helper modules you use

All under `plugins/orchestrator/python_helpers/` (call them inline from
Bash with `python3 -c`):

| Module | What it gives you |
|---|---|
| `orchestrator_state` | `read()`, `write()`, `flock_session()`, `write_in_lock()`, `transition(issue, status)`, `current_session_id()`, `mark_dev_started()`, `get_issue()`, `now()` |
| `wake_inference` | `infer(transcript_path, state) → (reason, sender)`, `read_last_user_message()`, `read_last_task_result()` |
| `issue_picker` | `pick(state) → [≤5 issues]`, `resume_waiting_on_dep(state)`, `remember_pick()` |
| `lifecycle` | `ensure_labels(repo)`, `ensure_split_label(repo, parent_num)`, `team_alive(team_name)`, `LABEL_SPEC` |
| `safety` | `has_dangerous_label()`, `would_self_modify()`, `sanitize_scout_body()`, `parse_acceptance_criteria()`, `sanitize_feedback_message()`, `fingerprint_label()`, `push_pending_question()` |

Set `PYTHONPATH` for these quick calls:

```bash
export PYTHONPATH="${CLAUDE_PLUGIN_ROOT:-plugins/orchestrator}/python_helpers"
```

`CLAUDE_PLUGIN_ROOT` is set by the harness when the plugin is enabled;
the fallback path is for testing/dev.

---

## The lead loop — what you do on every invocation

### Step −5: Watch-list expiry (Rev 13 A2)

For every entry in `state.watch_list` whose `expires_at` has passed AND
whose issue is still in `merged_observing`, transition the issue to
`done_final`, bump `completed_count`, and drop the entry.
(This runs regardless of whether the issue is the current_issue.)

### Step −4: Stale-PR audit (Rev 13 B3)

If `state.last_pr_audit_at` is None or > 12h old, run
`gh pr list --repo ${repo} --state open --label orchestrator-managed
--json number,url,createdAt,updatedAt`. For each PR:

- Age > 14d → `audited_bash gh pr close … --comment 'Auto-closed after
  14d inactivity'` and add label `orchestrator-abandoned`.
- Age > 7d → push to `state.pending_questions` (target key
  `stale_pr:<num>`, so duplicates dedupe automatically). If a local issue
  matches the PR's URL, transition it to `pr_audit_pending`.

Set `state.last_pr_audit_at = now()` and `write()`.

### Step −3: Pending-questions flush (Rev 13 C1)

If `state.pending_questions` is non-empty, take the **first 4** (the
AskUserQuestion limit), call AskUserQuestion, apply the answers to
state, drop those 4 from the queue, `write()`, and **return** (AskUser
ends the turn).

Critical branches (regression confirmation, dangerous merge) skip the
queue and go through AskUserQuestion immediately.

### Step −2: Vision reflection (Rev 13 D1)

Let `total = completed_count + rejected_count + sum(len(h.created_urls)
for h in scout_history)`. If `total > 0` and `total % 25 == 0` and
`last_reflection_count != total`:

- SendMessage to `issue-scout`:
  `"REFLECTION_REQUEST: vision='<vision>'. Review the last 25 issues and
  report mapped_subgoals / gap_areas / vision_update_suggestion as JSON
  with phase='reflection'."`
- Set `state.last_reflection_count = total`, `state.reflection_pending = True`,
  `write()`, **return**.

When `reflection_pending` is True and wake_reason is
`("teammate_reply","issue-scout")` with `phase=="reflection"`, emit the
results to the user, push a `vision_reflection` pending question, clear
the flag, and continue the normal flow.

### Step −1: Daily digest (Rev 13 C2) + history prune (Rev 17)

If `state.last_digest_at` is None or > 24h old, emit a short markdown
digest (yesterday's merges/rejects/scouts, in-flight statuses, attention
items).

**Rev 17 prune (run every invocation, not just daily):**
`orchestrator_state.prune_state_history(state)` — applies FIFO cap +
TTL to lessons_learned, scout_history, planner_history,
roadmap_reports, vision_critic_history, feedback_log, and
rejected_delta_hashes. Single-slot fields (`vision_critic_pending_delta`,
`active_phase_context`) are blocklisted — never touched.

**State-file size watermark**: read
`orchestrator_state.state_file_size_bytes()` after prune.
- `> STATE_SIZE_WARN_BYTES` (2 MB) → emit warning notice to the user.
- `> STATE_SIZE_HARD_BYTES` (5 MB) → archive long-term history to
  `~/.loopd/orchestrator/state_archive/<UTC-date>.json` and retain only
  the most-recent N entries per field.

Set `state.last_digest_at = now()`, `write()`. **Do not return —
continue.**

### Step −0: Teammate health check (Rev 17 Phase 17-G)

For each alive teammate in
`discover_alive_teammates(team_name) ∩ state.teammate_health.keys()`:

1. If `state.pending_respawn.get(member)` is True → respawn this turn
   (mid-cycle watermark hit, deferred from a prior teammate-reply).
   `state.pending_respawn.pop(member)`. Proceed to step 4.
2. Else if `lifecycle.needs_respawn(state, member)` → continue.
   Otherwise skip this member.
3. **In-flight guard (Round A A4)**: if any pending status references
   this member (`analyze_pending` for analyzer, `planning_pending` for
   planner, etc.), set `state.pending_respawn[member] = True`, `write()`,
   skip — wait until the next turn so the reply isn't lost.
4. **Graceful shutdown**: SendMessage(to=member,
   body=`lifecycle.shutdown_marker(team_name)`).
5. On next turn (when ack arrives): Agent tool re-spawn the same
   definition. Then SendMessage with
   `lifecycle.recover_team_context(state, member)` as the bootstrap body.
6. `lifecycle.reset_teammate_health(state, member)`. `write()`.
   Return — turn ends.

Only one respawn per invocation (the iteration short-circuits on the
first matching member). `write()`. Continue if no respawn was needed.

### Step 0: Parse args

- `stop:true` → graceful shutdown:
  - If `current_issue.status == "dev_running"` transition it to
    `parked_awaiting_human` (failure_reason = "stop:true while dev
    pipeline running").
  - `state.dev_session_id = None`, `state.dev_done_injected = False`,
    `state.current_issue = None`.
  - SendMessage shutdown to each teammate, then TeamDelete.
  - `write()`, emit summary, return.
- `vision:"…"` / `repo:…` → set on state, append prior vision to
  `vision_history`. Doesn't disrupt in-flight issue.
- `scout:true` → if `current_issue.status == "dev_running"`, transition
  it to parked first; set `mode="scouting"`, `scout_status="scout_new"`,
  `scout_started_at=now()`.
- `undo:N` → walk `state.audit_log[-N:]` in reverse; for each entry
  apply its inverse via `audited_bash`. Notes:
  - `gh pr merge` cannot be auto-reverted — emit a guide and recommend
    the user use the `regression_detected` flow.
  - `gh issue close` → `gh issue reopen`.
  - `gh issue create` → `gh issue close --comment "undone"`.
  - `gh pr edit --add-label X` → `--remove-label X`.
  - Cache `gh auth status` (24h) for permission verification.
- `feedback:N:"<msg>"` → `state.feedback_log.append({at: now(), target_num: N, target_type, message})`. If `"revert"` is in msg, AskUser whether to draft a revert PR. Emit acknowledgement, return.
- `force:N` → set `issue.force_process = True`, transition to `new` (or
  pick #N if not in state). The next analyzer call will include
  `FORCE_PROCESS=true`.
- `resume:N` → require status == `parked_awaiting_human`. Restore the
  most recent active status from `issue.history` (fallback `new`); reset
  all `*_started_at` and `*_retried` flags; set `state.current_issue = N`.
  If another issue is currently active, park it first.
- `split:N` → see §9 `split:N` in the design (similar to resume but
  forces `issue.force_split = True`, status `new`). Guard against
  `is_split_epic == True` (refuse double-split).
- `scout_bootstrap_done:true` → `state.scout_bootstrap_done = True`,
  emit confirmation, return.

### Step 1: Load state

```bash
STATE=$(python3 -c "import orchestrator_state, json; \
  print(json.dumps(orchestrator_state.read()))")
```

### Step 2: First-run init

If `state.vision == ""` and no `vision:` arg, AskUserQuestion for the
vision text and (if needed) the repo. Save to state and return.

### Step 3: Ensure team

If `state.team_name` is empty or `lifecycle.team_alive(team_name) == False`:

- TeamCreate with `team_name = "orchestrator-<repo-slug>"`.
- Agent(subagent_type="issue-analyzer", name="issue-analyzer", …).
- Agent(subagent_type="tester", name="tester", …).
- Agent(subagent_type="issue-scout", name="issue-scout", …).
- Save `team_name`. `lifecycle.ensure_labels(repo)`.

**Drain `state.pending_team_spawns` (Rev 17 lazy spawn):**
For each member in `state.pending_team_spawns[:]`:
- Agent tool spawn (subagent_type=member, name=member). The Agent tool
  call ends the turn — only spawn one per invocation.
- After the agent returns idle, `state.pending_team_spawns.remove(member)`,
  initialize `state.teammate_health[member]` if missing, `write()`,
  return.

This pattern means lazy-spawning a teammate takes 2 turns (mark pending
+ spawn). The Stage 1/2/3 entry branches handle this by re-checking on
the next wake.

### Step 3.5: Record outgoing SendMessage (Rev 17 Phase 17-G)

Whenever a branch is about to call `SendMessage(to=<teammate>, body=<text>)`,
**before** the actual send, run:

```python
lifecycle.record_teammate_call(
    state,
    member=<teammate>,
    sent_tokens=len(body) // 4,           # rough char/token approximation
    received_tokens=0,                    # filled in on the next wake from the reply length
)
write_in_lock(state)
```

And whenever a `teammate_reply` wake handler parses a reply (Step 6A /
6B / 6C / 6D), it additionally updates the receiver's
`estimated_tokens` with `len(reply_body) // 4` via the same helper
(`sent_tokens=0`). This is what feeds `lifecycle.needs_respawn` in
Step −0 — without these calls the watermark cannot fire.

### Step 4: Infer wake reason

```python
wake_reason = wake_inference.infer(transcript_path, state)
# → ("teammate_reply", "issue-analyzer"|"tester"|"issue-scout")
#   ("orch_hook_inject", "dev_done")
#   ("user_input", None)
#   ("fresh", None)
```

Handle the `reflection_pending` analytic branch here (Step −2).

### Step 4.5: Baseline health check (Rev 13 D2)

If `last_main_health_check` is None or > 24h old:

```bash
gh run list --repo <repo> --branch main --limit 1 --json conclusion,status
```

If conclusion == failure → set `state.main_branch_red = True`, emit a
warning. The picker will boost main-fix candidates by +200.

### Step 5: Mode decision

If `state.mode == "scouting"` → go to **Step 6B** (Stage 1: scout + planner).

If `state.roadmap_status` is set → go to **Step 6C** (Stage 2 dispatch).

If `state.vision_check_status` is set → go to **Step 6D** (Stage 3 dispatch).

Else if `state.current_issue` is set → go to **Step 6A** with the in-flight issue.

Else:

1. First try `picker.resume_waiting_on_dep(state)`. If it returns an
   issue, that issue's deps are now closed — set `current_issue` to it,
   ensure `state.issues[N]` is preserved (don't overwrite analysis data),
   go to Step 6A.
2. `candidates = issue_picker.pick(state)`.
3. If `len(candidates) == 0`:
   - If `state.consecutive_empty_scouts >= 3` and the last empty scout
     was < 24h ago, emit a cooldown message and return.
   - Otherwise set `mode="scouting"`, `scout_status="scout_new"`,
     `planner_status="planning_pending"` (Rev 17 Stage 1 parallel),
     `scout_started_at=now()`, `planner_started_at=now()`,
     `write()`, go to Step 6B.
4. `picked = pick_best_by_vision(candidates, state.vision)` — use
   LLM thinking, see "pick_best_by_vision" below.
5. If `safety.would_self_modify(picked, state)`, AskUserQuestion for
   confirmation. On "no", mark skipped and return.
6. `state.current_issue = picked.number`. If `state.issues[N]` already
   exists (from a previous parked attempt), keep its analysis data and
   just set `status="new"`; otherwise create a fresh entry.
   `issue_picker.remember_pick(state, picked.number)`. `write()`. Go to
   Step 6A.

### Step 6A: Resolution-cycle dispatch

Match on `(issue.status, wake_reason)` and execute the matching branch
below. **Stop the turn** after the first SendMessage / AskUserQuestion /
Skill.

(Statuses not listed = unreachable terminal/legacy.)

#### `("new", _)`
Compose `directives` from lessons (last 5) + sanitized feedback (last 5);
append `FORCE_SPLIT=true` / `FORCE_PROCESS=true` markers if set.

**Rev 17 auto-FORCE_SPLIT**: before sending to analyzer, call
`issue_picker.needs_force_split(issue, state)`. If True, append
`FORCE_SPLIT=true` to directives automatically. This triggers analyzer's
Rev 9 split mechanism for planner-authored Epics. Skipped when the label
combination came from an external user (audit cross-check fails — Round
A S3) — those still go through the regular analyze pass and the user can
manually split.

```
SendMessage(to="issue-analyzer",
  message=f"Analyze issue #{issue.number} in {state.repo}. "
          f"Vision: {state.vision}.{directives}")
```

Set `issue.analyze_pending_started_at=now()`, `issue.analyzer_retried=False`.
`transition(issue, "analyze_pending")`. `write()`. Return.

#### `("analyze_pending", ("teammate_reply","issue-analyzer"))`
`parsed = parse_json_tail(last_user_message_body())`. If None, ask the
teammate to resend JSON and return.

Run lead-side sanity checks (length, criteria/complexity consistency,
should_split requires sub_candidates ≥ 2, should_process=false requires
reject_category + reject_reason, duplicate requires duplicate_of URL).
Each failure → re-request (max 2 times) → on third failure transition to
needs_human with failure_reason "analyzer 출력 검증 실패".

Stash the parsed result on the issue (`issue.parsed = parsed`, plus
flatten the fields you need into `issue.analysis`,
`issue.acceptance_criteria`, `issue.dev_task_prompt`,
`issue.complexity_level`, `issue.depends_on`, `issue.touched_paths`).
`transition(issue, "analyze_received")` and **fall through** in the same
turn.

#### `("analyze_pending", _)` (any other wake)
Timeout guard:
- If `analyze_pending_started_at` is None, set it now and return.
- `elapsed = now() - analyze_pending_started_at`.
- If `analyzer_retried` and elapsed < 5 min → return (idempotency).
- If elapsed > 10 min:
  - If not yet retried → resend the analyzer prompt, set
    `analyzer_retried=True`, reset `analyze_pending_started_at=now()`,
    return.
  - Else `issue.failure_reason = "analyzer 10분 무응답 + 1회 재시도 실패"`,
    `detect_lesson_pattern(...)`, transition needs_human.

#### `("analyze_received", _)` (one fall-through pass)
Resolve **in this order** (matches §9 case analyze_received):

1. `should_process == false` and not `force_process`: copy reject fields
   onto issue; `transition(issue, "reject_confirm_pending")`. Continue.
2. `should_process == false` and `force_process`: emit override notice;
   fall through with normal-issue treatment, but if `dev_task_prompt` is
   empty AND `should_split` is False → needs_human (failure_reason
   "force:N이지만 analyzer가 dev_task_prompt 안 채움").
3. `parsed.status == "split_refused"`: clear `force_split`, fall through
   to normal branch (human_qa if human_needed, else ready_for_dev). If
   neither path applies → needs_human.
4. `should_split == true`:
   - If any label starts with `split-from-#` (already a sub-issue) →
     needs_human (failure_reason: "이미 sub-issue인데 또 split 시도").
   - Else copy candidates + reason, transition `split_confirm_pending`.
5. `human_needed == true`: copy `questions`, transition `human_qa_pending`.
6. Else: `transition(issue, "ready_for_dev")`. Continue.

#### `("human_qa_pending", _)`
- 24h timeout → park.
- `wake_reason == ("user_input", None)`: read the answer, append a
  "## 사용자 답변" section to `dev_task_prompt`, transition
  `ready_for_dev`, **continue**.
- Else (first entry): set `human_qa_started_at = now()`, call
  AskUserQuestion(`issue.questions`), return.

#### `("ready_for_dev", _)`
Dependency gate (Rev 13 A1): for each n in `issue.depends_on`, check
local state and (if needed) `gh issue view n --json state`. Any
unresolved → `issue.unresolved_dependencies = [...]`, transition
`waiting_on_dep`, `current_issue = None`, write, **goto Step 5**.

Conflict prediction (Rev 13 B1): if `issue.touched_paths`, fetch
`gh pr list --state open --json number,files`, intersect file paths. If
any conflict and `conflict_warned` is unset → set `conflict_warned=True`,
AskUserQuestion (continue Y/N), return.

Then:

```python
# Source of truth: CLAUDE_CODE_SESSION_ID (canonical; falls back to
# LOOPD_SESSION_ID / CLAUDE_SESSION_ID) env var injected by the Claude Code
# harness. NEVER make up a placeholder UUID — the β Stop hook Gate 1 compares
# this against the live payload.session_id (== CLAUDE_CODE_SESSION_ID);
# a mismatch silently breaks dev-done auto-resume forever.
try:
    session_id = orchestrator_state.current_session_id()
except RuntimeError as exc:
    # Env var missing — refuse to start dev. Park the issue so the user
    # can investigate; do NOT proceed with a synthetic id.
    issue.failure_reason = f"current_session_id unavailable: {exc}"
    transition(issue, "needs_human")
    write(state)
    return

orchestrator_state.mark_dev_started(state, session_id)
transition(issue, "dev_running")
write(state)                              # MUST flush before Skill call
Skill(skill="loopd:dev-task",
      args=f'"{issue.dev_task_prompt}" '
           f'repo:{state.repo} '
           f'level:{issue.complexity_level} '
           f'branch:main')
return                                    # window now belongs to loopd
```

#### `("dev_running", ("orch_hook_inject","dev_done"))`
`transition(issue, "dev_done")` and **fall through** in the same turn.

#### `("dev_running", "fresh")`
PoC-4 stale-state guard:

- `state.dev_session_id is None` → park (failure_reason: "dev_running 인데
  dev_session_id is None — stale state"); `current_issue=None`; write;
  goto Step 5.
- `loopd_session_exists = Path(~/.loopd/sessions/<id>.json).exists()`.
- Exists → still running, return.
- Not exists AND age < 2 min → still booting, return.
- Not exists AND age >= 2 min → needs_human (failure_reason
  "dev-task 시작 후 N분 내 loopd session 미생성"). Reset
  dev_session_id=None, dev_done_injected=False. Return.

#### `("dev_running", _)`
Unexpected wake — return without changes.

#### `("dev_done", _)`
- `pr = extract_pr_url(state)` (§11 3-step fallback).
- If None → needs_human (failure_reason "dev-task 완료했으나 PR URL 추출
  실패"), `detect_lesson_pattern`, continue.
- Validate PR ownership (§11 R2-18 gate): `gh pr view <num> --json
  author,headRepositoryOwner,headRefName,createdAt`. Reject if external
  fork, if `headRefName` doesn't match `loopd-task-YYYY-MM-DD`, or if
  `createdAt < state.dev_started_at`. Each failure → needs_human.
- On accept: `audited_bash gh pr edit … --add-label orchestrator-managed
  issue/<issue.number>`; `audited_bash gh pr comment …
  --body '<!-- orchestrator-task-id: <num>-<rework> -->'`.
- `issue.pr_url = pr`, `issue.test_pending_started_at = now()`,
  `issue.tester_retried = False`, `transition(issue, "test_pending")`.
- SendMessage to tester: `"Verify PR {pr_url} against acceptance:
  {criteria}. Repo: {state.repo}."`. Return.

#### `("test_pending", ("teammate_reply","tester"))`
`parsed = parse_json_tail(...)`. None → ask resend, return.

Tester sanity check (Round 5 R5-1): `diff_lines >= 0` (int), `verdict in
{"pass","fail","uncertain"}`, `recommend_human_review` is bool. Each
failure → re-request (max 2). On third failure, force
`verdict="uncertain"`, summary suffix "tester 응답 검증 실패",
`recommend_human_review=true`.

`issue.test_verdict = parsed`. Transition `test_received`. Fall through.

#### `("test_pending", _)` (any other wake)
Same timeout pattern as analyzer, but 20-minute window (test takes
longer). Reuse `tester_retried` and `test_pending_started_at`.

#### `("test_received", _)`
- `verdict_signature = sha256(json.dumps(verdict, sort_keys=True))[:16]`.
- If `issue.last_verdict_signature == verdict_signature` → return
  (idempotent — same verdict reprocessed).
- Save signature.
- Compute gates:
  - `large_diff = verdict.get("diff_lines", 10**9) > 200` (fail-safe
    default for missing field).
  - `permission_escalated = verdict.get("permission_elevation",
    {"detected":True}).get("detected", True)`.
  - `force_human_check = state.auto_merge_consecutive_safe < 3`.
- If `verdict == "pass"`:
  - If `recommend_human_review`, `has_dangerous_label(issue)`,
    `large_diff`, `permission_escalated`, or `force_human_check` →
    transition `merge_pending` (save `issue.escalation_details` if
    permission_escalated).
  - Else idempotent merge:
    ```
    state = bash("gh pr view <pr> --json state --jq .state")
    if state != "MERGED":
      audited_bash gh pr merge <pr> --squash --auto
    state.auto_merge_consecutive_safe += 1
    transition done  (then merged_observing path below if used)
    ```
    On merge success append a 6h watch_list entry and transition
    `merged_observing` (Rev 13 A2) instead of straight `done`.
- If `verdict == "fail"`:
  - `rework_count < 2`: bump rework_count; append failures to
    `dev_task_prompt`; reset `last_verdict_signature`, `tester_retried`,
    `test_pending_started_at`, `analyzer_retried`,
    `analyze_pending_started_at`; transition `ready_for_dev`. Continue.
  - Else needs_human (failure_reason "dev rework 2회 후에도 tester 거부"),
    `detect_lesson_pattern`.
- `verdict == "uncertain"` → transition `merge_pending`.

#### `("merge_pending", _)`
- 24h timeout → park.
- Wake `user_input` → read answer:
  - "yes": idempotent merge (state-check first); if merge succeeds and
    risky factors absent, bump `auto_merge_consecutive_safe`; add a
    watch_list entry; transition `merged_observing`.
  - "no": transition `skipped_by_human`.
- First entry (no `merge_question_emitted`): AskUserQuestion with PR URL
  + verdict summary; set `merge_question_emitted=True`,
  `merge_pending_started_at=now()`. Return.

#### `("reject_confirm_pending", _)`
- 24h timeout → park.
- Wake user_input:
  - "close": `audited_bash gh issue close <N> --comment "Closed by
    orchestrator: <cat> — <reason>"` and add label
    `orchestrator-rejected`. Transition `rejected`.
  - "skip": just add label `orchestrator-skipped`. Transition
    `skipped_by_human`.
  - "force": set `force_process=True`, reset retries, transition `new`.
- First entry: AskUserQuestion with close/skip/force options.

#### `("rejected", _)`
`current_issue=None`, `rejected_count += 1`, write, goto Step 5.

#### `("split_confirm_pending", _)`
- 24h timeout → park.
- Wake user_input: parse selected_ids from multi-select answer, set
  `issue.split_decisions`, transition `split_creating`. Continue.
- First entry: AskUserQuestion (multiSelect=True) listing each
  sub_candidate (title + complexity + body excerpt). Return.

#### `("split_creating", _)`
Use the common helper `create_issues_with_fingerprint`. Make sure each
sub-issue gets both `split-from-#<parent>` (via `ensure_split_label`) and
`scout-suggested` labels.

After the helper:
- If `len(issue.split_created_urls) == 0` → needs_human (failure_reason
  "split sub-issue 0건 등록").
- Else transition `split_done` (or `split_failed` if any failed); fall
  through.

#### `("split_done"|"split_failed", _)`
`mark_as_epic(state, issue, issue.split_created_urls)` (idempotent — uses
`<!-- split-epic-marker -->` sentinel). Transition `done`,
current_issue=None, write, goto Step 5.

#### `("waiting_on_dep", _)`

This issue is dormant — Step 5's `picker.resume_waiting_on_dep(state)`
re-tests dependencies on every wake. Don't process it inline here; just
`current_issue = None`, `write()`, goto Step 5.

#### `("merged_observing", _)` (Rev 13 A2)

Active monitoring of a merged PR for 6h. On every wake:

1. Find the matching `state.watch_list` entry by `pr_url`. If missing,
   transition `done_final` and continue.
2. Run regression probes:
   - **CI**: `gh pr checks {pr_url} --json conclusion --jq '.[].conclusion'`.
     Any `"failure"` → `ci_red = True`.
   - **External revert**: `gh pr list --repo {repo} --search 'in:title
     "Revert" head:{issue.pr_branch}' --json number --jq 'length'`.
     `> 0` → `reverted_externally = True`.
   - **Cross-reference**: `gh issue list --repo {repo} --state open
     --label bug --search 'created:>{watch.merged_at}' --json
     title,body --jq '.[] | select(.body | contains("<first touched path>"))' | head -3`.
     Any output → `regression_suspect = True`.
3. If any probe is true: save evidence on `issue.regression_evidence`,
   transition `regression_detected`, continue.
4. Else if `now() >= watch.expires_at`: drop watch_list entry,
   transition `done_final`, `current_issue=None`, `write()`, goto Step 5.
5. Else: return (wait for next wake).

#### `("regression_detected", _)` (Rev 13 A2)

- If `wake_reason == ("user_input", None)` — apply the AskUser answer:
  - **`revert`**: `audited_bash gh pr create --repo {repo} --title 'Revert: {issue.title}' --body 'Auto-revert by orchestrator: regression suspected on PR {pr_url}\n\nEvidence: {regression_evidence}' --head 'revert-{pr_branch}' --base main`. The actual `git revert <sha>` step needs a worktree — emit a guide to the user; transition `reverted`.
  - **`keep`**: transition `done_final`.
  - **`manual`**: transition `needs_human` with failure_reason "regression suspected — user chose manual review".
  - Drop the watch_list entry; `current_issue=None`; `write()`; goto Step 5.
- Else (first entry):
  - If `regression_q_emitted` is already true, return (still waiting for answer).
  - Else AskUserQuestion with three options
    (revert/keep/manual) and a summary of `regression_evidence`.
  - Set `regression_q_emitted=True`, return.

#### `("pr_audit_pending", _)` (Rev 13 B3)

A `Step −4` push routed this issue here. The user answer (close /
rebase / keep) arrives via the `pending_questions` flush in Step −3 and
sets a flag on the issue. On wake:

- If `issue.audit_decision == "close"`: `audited_bash gh pr close <num>
  --repo {repo}` + add label `orchestrator-abandoned`; transition
  `skipped_by_human`.
- If `"rebase"`: emit guidance (the lead can't rebase remotely);
  transition `parked_awaiting_human`.
- If `"keep"`: just transition back to the issue's prior status (use
  `history`); reset `last_pr_audit_at` for this PR so it doesn't
  re-trigger immediately.
- Else: return (decision not in yet).

#### `("done_final", _)` / `("reverted", _)`

Both are terminal; ensure `state.current_issue = None`, write, goto
Step 5.

#### `("done"|"needs_human"|"skipped_by_human"|"parked_awaiting_human", _)`
Terminal. `current_issue=None`, `completed_count += 1` (for `done` only),
write, goto Step 5.

### Step 6B: Stage 1 dispatch — scout + product-planner (parallel)

Rev 17 supersedes the scout-only branch. Stage 1 runs **issue-scout** and
**product-planner** in parallel from the same `scouting` mode entry, merges
their candidate pools (dedup), and surfaces a single user confirm.

Match on `(state.scout_status, state.planner_status, wake_reason)`:

#### `("scout_new", "planning_pending", _)` — initial fan-out
1. `lifecycle.ensure_team_member(team_name, "product-planner", state)`. If
   it returns False (`state.pending_team_spawns` was just appended) →
   `write()`; return. Step 3 next turn will spawn the agent.
2. SendMessage to **issue-scout** with vision + repo + format_recent_history
   + `active_phase_context` (if set and `current_cycle <
   active_phase_context_until_cycle`).
3. SendMessage to **product-planner** with vision + repo +
   `active_phase_context` + `vision_history[-5:]` +
   `planner_history[-3:]` (title + accepted-count only).
4. `scout_transition(state, "scout_pending")`. (No-op if already
   `scout_pending`.)
5. `planning_transition(state, "planning_pending")`.
6. `write()`. Return.

#### `("scout_pending", _, ("teammate_reply","issue-scout"))`
parse_json_tail → store in `state.scout_candidates_buffer` (NOT
`scout_candidates` yet — that's the merged pool). Set
`scout_pending_resolved_at = now()` and `scout_transition(state,
"scout_received")`. **Fall through** to merge check below.

#### `(_, "planning_pending", ("teammate_reply","product-planner"))`
parse_json_tail → run lead-side sanity checks (§4.1 contract):
1. `status == "need_vision_clarification"` → push vision question to
   pending_questions, `planning_transition(None)`, return.
2. Each candidate must have `complexity_level >= 3` and ≥ 7 acceptance
   criteria (count `- [ ]` lines in body); body must contain
   `## User Story` and `## Out of Scope` sections; labels must include
   `planner-suggested` + `split-epic` (auto-add if missing).
3. On any structural failure: re-request (`planning_retried=True`,
   `last_user_message body = "JSON 한 줄로 재전송"`) up to 2 times. Then
   discard with `candidates=[]`.
4. Run `safety.would_self_modify` AND `safety.would_loosen_safety` on
   each candidate. Drop matches; emit a user notice naming the dropped
   candidates and why.
5. Sanitize: `safety.sanitize_title(c.title)` + `safety.sanitize_scout_body(c.body)`
   on every survivor.
6. Store in `state.planner_candidates_buffer`. Set
   `planning_pending_resolved_at = now()`.
7. `planning_transition(state, "planning_done")`. **Fall through.**

#### Stage 1 merge check (after either reply, or idle wake)
After either teammate replies, check if both have reported (or both have
timed out per 5.4 timeout rules):

```python
scout_done = state.scout_candidates_buffer is not None or state.scout_pending_resolved_at
planner_done = state.planner_candidates_buffer is not None or state.planning_pending_resolved_at
both_resolved = scout_done and planner_done
```

If only one is done and the other is within its 10-min timeout window →
`write()`; return (wait for the other).

If `both_resolved`:
1. `merged = playbook_helpers.dedup_candidates(state.scout_candidates_buffer or [], state.planner_candidates_buffer or [])`.
2. Emit a user-visible note for each entry in `merged.drops` (e.g.
   "scout c2 (Add wizard) auto-deduped against planner p1 (Onboarding
   Epic) — similarity 0.93").
3. `state.scout_candidates = [c for c in merged.merged if c.get("source")=="scout"]`
   and `state.planner_candidates = [c for c in merged.merged if c.get("source")=="planner"]`.
4. Reset `scout_candidates_buffer = None`, `planner_candidates_buffer = None`.
5. If both lists are empty: `scout_transition("scout_done")` +
   `planning_transition(None)` with reason "no candidates" → goto Step 5.
6. Else build a single multiSelect (cap 8). Each option label is
   `f"[{source}] {title} (complexity={complexity_level}, priority={priority_hint})"`.
   `scout_transition("scout_confirm_pending")`,
   `planning_transition("planning_pending")` (re-use the status as
   "awaiting confirm" — `planner_status="planning_creating"` arrives on
   user confirm). AskUserQuestion, return.

#### `("scout_confirm_pending", _, ("user_input", _))` — confirm received
Parse selected IDs. Split into scout-selected + planner-selected lists.
Set `state.scout_decisions = {cid: True for cid in scout_selected}` and
`state.planner_decisions = {pid: True for pid in planner_selected}`.

If any scout selected → `scout_transition("scout_creating")`.
If any planner selected → `planning_transition("planning_creating")`.
If neither → both go straight to `*_done`/`None`.

`write()`. **Fall through** to creation.

#### `("scout_creating", _, _)` — register scout candidates
Acquire `stage1_creating_lock_*` (owner-CAS, 10-min stale window).
For each scout candidate with `decisions[c.id]=True`:
- `plan = playbook_helpers.candidate_create_plan(c, fp_prefix="scout-fp-",
  extra_labels=["scout-suggested"])`.
- `gh issue list --repo <repo> --label <plan.fingerprint_label> --state all
  --json number`. If any exist → record URL, skip create.
- `lifecycle.ensure_split_label` not needed here. `audited_bash` →
  `gh label create <plan.fingerprint_label>` (idempotent).
- `audited_bash gh issue create --repo <repo> --title <plan.title>
  --body <plan.body> --label <plan.labels>`.
- Append URL to `state.scout_created_urls`; mark
  `state.scout_creating_done.append(c.id)`. On error, append to
  `state.scout_failed_creations`.
- `write_in_lock(state)` after each candidate.

On exit: `scout_transition("scout_done")` (or `"scout_failed"` if any
failure). **Fall through** to planner creating.

#### `(_, "planning_creating", _)` — register planner candidates
Mirror the scout block with these substitutions:
- `fp_prefix="planner-fp-"`, `extra_labels=["planner-suggested", "split-epic"]`.
- Append to `state.planner_created_urls`,
  `state.planner_creating_done`, `state.planner_failed_creations`.
- All inside the same `stage1_creating_lock_*` — both creators share the
  one lock.

On exit: `planning_transition("planning_done")` (or `"planning_failed"`).

#### `("scout_done"|"scout_failed", "planning_done"|"planning_failed"|None, _)` — Stage 1 finalize
Both creators are finished:
- Push entries:
  - `state.scout_history.append({"ts": now(), "candidates_proposed": N,
    "candidates_accepted": M, "issue_urls_created": state.scout_created_urls})`.
  - `state.planner_history.append({"ts": now(), "candidates_proposed": N,
    "candidates_accepted": M, "issue_urls_created":
    state.planner_created_urls})`.
- `state.last_stage1_completed_at = now()`.
- Update `consecutive_empty_scouts` / `last_empty_scout_at` if zero
  candidates created across both.
- `mode="resolution"`. `playbook_helpers.clear_scout_fields(state)` and
  `playbook_helpers.clear_planner_fields(state)`.
- Phase 17-D will trigger Stage 2 here. For now: `write()`; goto Step 5.

#### `("scout_pending", _, _)` (other wakes)
10-min timeout (per §6.5):
- First timeout: re-SendMessage with "JSON 한 줄로 재전송", set
  `scout_retried` flag, reset `scout_started_at=now()`, return.
- Second timeout: store `scout_candidates_buffer = []`,
  `scout_pending_resolved_at = now()`, `scout_transition("scout_received")`,
  fall through to merge.

#### `(_, "planning_pending", _)` (other wakes)
Symmetric to scout 10-min timeout. On second timeout:
`planner_candidates_buffer = []`, `planning_pending_resolved_at = now()`,
`planning_transition("planning_done")` (with no candidates),
fall through to merge.

#### Late reply handling (Round A E1)
If a reply for `issue-scout` arrives but
`state.scout_pending_resolved_at` is already set AND the merge already
ran (e.g. `state.scout_candidates_buffer is None` because we moved on),
**discard the reply** with a user-visible note "late scout reply — Stage 1
already finalized, drop". Same for late planner replies.

#### `("scout_clarify_pending", _, _)`
Unchanged from Rev 16 — AskUserQuestion the saved clarify question,
resend prompt to scout, `scout_transition("scout_pending")`, return.

#### Stage 2 auto-entry (after Stage 1 finalize)
If `state.last_stage1_completed_at` is set and `now() -
last_stage1_completed_at < 5 min` AND
`state.roadmap_status is None` AND `state.last_roadmap_report_cycle !=
current_cycle`:

1. `lifecycle.ensure_team_member(team_name, "roadmap-strategist", state)`.
   If False → `write()`; return.
2. SendMessage to **roadmap-strategist** with: vision + 50-merged-PR
   summary (`gh pr list --state merged --limit 50 --json title,labels,mergedAt`)
   + Stage 1 candidate-pool summary (title + complexity only) +
   `vision_history[-5:]` + `roadmap_reports[-3:]`.
3. `roadmap_transition(state, "roadmap_pending")`. Set
   `state.roadmap_started_at = now()`. `write()`. Return.

### Step 6C: Roadmap dispatch (Rev 17)

Match on `(state.roadmap_status, wake_reason)`:

#### `("roadmap_pending", ("teammate_reply","roadmap-strategist"))`
parse_json_tail. Sanity check:
- `status == "insufficient_history"` → push to pending_questions ("최근
  머지 < 5건, roadmap 진단 보류"); `roadmap_transition(None)`; return.
- `current_phase` must be one of `pre-mvp|mvp-validation|growth|mature`;
  on failure re-request once (`roadmap_retried=True`), then drop.
- `phase_evidence` must be non-empty; on failure re-request.
- `phase_context_for_next_cycles` ≤ 500 chars; truncate at 500 + emit
  notice (do NOT re-request — Round A S5).
- Apply `safety.sanitize_feedback_message` to `phase_context_for_next_cycles`
  before storing (Round A S5).

On success:
- Append the report to `state.roadmap_reports` (FIFO cap 10 in
  prune_state_history).
- Push to `state.pending_questions`: `{question: "phase context 채택? (다음
  25 사이클 scout/planner prompt에 주입)", target: f"roadmap:{cycle}", options: [채택, 거부]}`.
- `state.last_roadmap_report_cycle = current_cycle`.
- `roadmap_transition(state, "roadmap_received")` then immediately
  `roadmap_transition(state, "roadmap_done")` and `roadmap_transition(state, None)`.
- `write()`. Return.

#### `("roadmap_pending", _)` (other wake)
10-min timeout → re-request once on first hit; on second timeout treat as
"no report" (`roadmap_transition(None)`), emit notice.

#### Step −3 phase-context acceptance handling
When Step −3 surfaces the "phase context 채택?" question and the user
answers:
- 채택 → set `state.active_phase_context = report.phase_context_for_next_cycles`
  AND `state.active_phase_context_until_cycle = current_cycle + 25`.
- 거부 → leave `active_phase_context` unchanged; mark
  `report.user_action = "rejected"`.

### Step 6D: Vision-critic dispatch (Rev 17)

#### Step −2 Stage 3 trigger
Replace the legacy `total % 25 == 0` reflection clause with:

```python
total = state.completed_count + state.rejected_count \
        + sum(len(h.get("created_urls") or []) for h in (state.scout_history or [])) \
        + sum(len(h.get("issue_urls_created") or []) for h in (state.planner_history or []))

# Existing D1 scout reflection (offset 0)
if total > 0 and total % 25 == 0 and state.last_reflection_count != total:
    SendMessage(to="issue-scout", body="REFLECTION_REQUEST: ...")
    state.reflection_pending = True
    state.last_reflection_count = total
    write(); return

# Rev 17 — vision-critic (offset 12)
elif playbook_helpers.vision_critic_due(state, total, offset=12, period=25):
    lifecycle.ensure_team_member(team_name, "vision-critic", state)
    if "vision-critic" in state.pending_team_spawns:
        write(); return  # next turn, Step 3 spawns it
    SendMessage(to="vision-critic", body=<vision + vision_critic_history[-10:] + 50 merged PR titles + roadmap_reports[-5:] + feedback_log[-10:]>)
    vision_transition(state, "vision_check_pending")
    state.vision_check_started_at = now()
    state.last_vision_critic_cycle = total  # set BEFORE response to prevent duplicate sends
    write(); return
```

Also handle `/orchestrator vision-check:true` in Step 0: if not
already `vision_check_pending`, force the trigger by setting
`last_vision_critic_cycle = -1` and re-entering Step −2.

#### `("vision_check_pending", ("teammate_reply","vision-critic"))`
parse_json_tail. Sanity check:
1. `status == "insufficient_data"` → emit notice to user (`{note}`);
   `vision_transition(None)`; return. **No further checks.**
2. `alignment_score` in [0, 1] else re-request once.
3. `critical_questions` length ≥ 3 else re-request once.
4. If `vision_delta.needs_update is True` then all of
   `before`/`after`/`rationale` must be non-empty.
5. If `vision_delta.confidence < 0.5` → force-downgrade
   `needs_update=False`.

Lead-side **self-preservation guard (Round A S6)**:
- `guard_removed = playbook_helpers.vision_delta_violates_guard(delta)`.
  If non-empty → `needs_update=False`, emit notice "vision delta removes
  oversight tokens {guard_removed} — blocked".
- If proposed delta matches a prior rejected (`find_rejected_delta_match`)
  → `needs_update=False`, emit notice "previously rejected delta".

**Alignment-score skip (User decision §9.1, option B)**:
- If `playbook_helpers.vision_alignment_skip(alignment_score)` (i.e.
  `> 0.8`) → record summary in `vision_critic_history` with
  `user_action="auto_skipped"`; `vision_transition(None)`; return without
  prompting user.

**Two-call confirm gate (Round A S6)**:
- If `playbook_helpers.two_call_confirm_required(state, delta)` returns
  True → append history entry with `user_action="pending_second_confirm"`
  (including `before`/`after`/`rationale`/`alignment_score`); set
  `state.vision_critic_pending_delta = None`; `vision_transition(None)`;
  return. (The next vision-critic call in ~25 cycles either re-confirms,
  causing the prompt, or drops.)

Otherwise, populate `state.vision_critic_pending_delta = {before, after,
rationale, alignment_score, proposed_at: now()}`. `vision_transition(state,
"vision_check_received")`. AskUserQuestion via `push_pending_question`
("vision 갱신 채택?", target=f"vision-delta:{hash[:8]}", options=[채택, 거부]).
`write()`. Return.

#### `("vision_check_pending", _)` (other wake)
10-min timeout. First hit → re-request via SendMessage. Second hit →
`vision_transition(None)` with no delta; emit notice "vision-critic
timeout — skipped this cycle".

#### Vision delta user-confirm handling (in Step −3 flush)
When Step −3 surfaces "vision 갱신 채택?" and the user answers:

```python
with flock_session() as state:
    delta = state.vision_critic_pending_delta or {}
    if user_chose("채택"):
        prev_vision = state.get("vision") or ""
        state["vision"] = delta.get("after") or prev_vision
        state.setdefault("vision_history", []).append(prev_vision)
        state.setdefault("vision_critic_history", []).append({
            "ts": now_iso(),
            "source": "vision_critic",
            "before": delta.get("before"),
            "after": delta.get("after"),
            "rationale": delta.get("rationale"),
            "user_action": "accepted",
            "alignment_score": delta.get("alignment_score"),
        })
        audit.record_state_mutation(
            state,
            actor="vision-critic",
            action="state.vision update (accepted)",
            payload={"before": prev_vision, "after": delta.get("after"), "rationale": delta.get("rationale")},
        )
    else:  # rejected
        ph.record_rejected_delta(state, delta.get("before") or "", delta.get("after") or "")
        state.setdefault("vision_critic_history", []).append({
            "ts": now_iso(),
            "source": "vision_critic",
            "before": delta.get("before"),
            "after": delta.get("after"),
            "rationale": delta.get("rationale"),
            "user_action": "rejected",
            "alignment_score": delta.get("alignment_score"),
        })
        if ph.count_same_before_rejections(state, delta.get("before") or "") >= 3:
            vision_transition(state, "vision_check_parked")
            push_pending_question("vision-critic이 동일 영역 3회+ 거부 — 일시 정지할까?", ...)
        audit.record_state_mutation(state, actor="vision-critic",
            action="vision delta rejected",
            payload={"hash": (state.rejected_delta_hashes[-1] or {}).get("hash")})
    state["vision_critic_pending_delta"] = None
    write_in_lock(state)
```

24h+ no-response (Round A E11) → on Step −3 sweep, if
`vision_critic_pending_delta.proposed_at > 24h ago`, commit it to
history with `user_action="parked_expired"` and clear the slot.

---

## pick_best_by_vision(candidates, vision)

Use **LLM thinking** (you, the lead):

1. Decompose vision into 3-7 sub-goals.
2. For each candidate, decide which sub-goal (if any) it serves.
3. Pick:
   a. Highest-priority sub-goal match first.
   b. Tie-break by `candidate.__score__` (already on the dict from the picker).
   c. Tie-break by `complexity_level` (lower wins — small wins first).
4. If no candidate maps to any sub-goal, pick the highest score and emit
   a warning ("선택된 이슈가 vision과 직접 매핑 안 됨").

Because this is LLM-based and non-deterministic, the picker dedups
recent picks via `last_picked_at` (5-minute window).

## extract_pr_url(state) (§11 fallback chain)

1. **Primary**: regex `https://github.com/<owner>/<repo>/pull/\d+` against
   the transcript tail (use `wake_inference.read_last_user_message()`'s
   body or a wider scan).
2. **Fallback 1**: `gh pr list --repo <repo> --state open --head
   <pr_branch> --json url --jq '.[0].url'`. The `pr_branch` is the
   loopd worktree branch; you may have captured it earlier from the
   loopd session file while it was alive.
3. **Fallback 2**: `gh pr list --repo <repo> --state open --label
   orchestrator-managed --search "in:title issue/#<num>" --json
   url,createdAt --jq 'sort_by(.createdAt) | last | .url'`.
4. All three failed → needs_human.

## Common-helper sketch — `candidate_create_plan` (Python)

Use `playbook_helpers.candidate_create_plan(candidate, fp_prefix, extra_labels)`
to compute the title / body / labels / fingerprint for a single candidate
(pure function — no GitHub side effects). Then the lead calls the gh
commands itself, wrapped in `audited_bash`:

For each candidate where `decisions[c.id]` is true:

1. If `c.id` already in `done_list_field(state_or_issue)` → skip
   (already created).
2. `plan = playbook_helpers.candidate_create_plan(c, fp_prefix=<scout-fp-|planner-fp->, extra_labels=[<source-label>])`.
3. Pre-check: `gh issue list --repo <repo> --label <plan.fingerprint_label>
   --state all --json number`. If any exist → record the existing URL
   and skip create.
4. `lifecycle.ensure_labels(repo)` once at the start; for each candidate
   `audited_bash gh label create <plan.fingerprint_label>` (idempotent).
5. `audited_bash gh issue create --repo <repo> --title <plan.title>
   --body <plan.body> --label <,>.join(plan.labels)`.
6. On success: append URL to `created_field(...)`, mark in `done_list_field(...)`.
7. On failure: append `{candidate_id, error}` to `failed_field(...)`.
8. **After each candidate** → `write_in_lock(state)` so a crash leaves
   recoverable state.

Wrap the whole block in `flock_session()` with an
owner-CAS `stage1_creating_lock_*` (owner = `f"{session_id}-{pid}"`).
Rev 17 uses a single shared lock for both scout-creating and
planning-creating (sequential, scout first then planner) to avoid the
two-step race. If the existing owner is someone else and the lock is
< 10 min old, return early (someone else is working on it). After 10 min,
treat as stale and take it over.

### Dedup method (`dedup_candidates`)

Stage 1 merges scout + planner pools via
`playbook_helpers.dedup_candidates(scout_list, planner_list)`. Method:
- Default (env unset) — `auto`: sentence-transformers if installed,
  SequenceMatcher otherwise.
- `ORCHESTRATOR_DEDUP_METHOD=sequence_matcher` — deterministic,
  dependency-free.
- `ORCHESTRATOR_DEDUP_METHOD=sentence` — required; raises ImportError if
  sentence-transformers not installed (no silent fallback).
- `ORCHESTRATOR_DEDUP_MODEL` — overrides the model name (default
  `sentence-transformers/all-MiniLM-L6-v2`).

Drop entries from `merged.drops` are surfaced to the user before the
multiSelect ("scout c2 auto-deduped against planner p1 (sim 0.93)").

## When in doubt

- Read the corresponding section of `docs/orchestrator-design.md` —
  every branch in this playbook references one.
- Prefer **fewer transitions per turn** over more. If you've called
  SendMessage / AskUser / Skill, stop.
- Save state often. The flock helpers are cheap.
