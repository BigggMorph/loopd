# orchestrator (experimental)

Autonomous GitHub issue resolution plugin. Coexists with `loopd`; the lead
Claude thread runs an `/orchestrator` playbook that:

1. Picks the highest-priority open issue (or runs `issue-scout` to propose
   candidates when the backlog is empty).
2. Sends it to the `issue-analyzer` teammate, which decides whether the
   issue should be processed / split / handed to a human.
3. Invokes `loopd:dev-task` directly (no human in the loop) to produce a PR.
4. Sends the PR to the `tester` teammate for sandboxed verification.
5. Merges (or requests human confirmation for risky / uncertain verdicts).
6. Watches the merged PR for a 6h regression window, then moves on.

`loopd`'s deterministic FSM is preserved 100% — orchestrator never edits
loopd files. The two plugins talk only through Skill invocation and a Stop
hook (`hooks/orch_stop.py`, the "β mechanism") that detects when `/dev-task`
finishes and re-enters the playbook.

See `docs/orchestrator-design.md` for the full spec (state machine, helper
contracts, prerequisites, evaluation plan).

## Status

Experimental. Living on `experimental/orchestrator-v1`; will not be merged
to `main` until §15 evaluation passes.

## Activation

```jsonc
// ~/.claude/settings.json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

Both `loopd` and `orchestrator` plugins must be enabled.

## Usage

```
> /orchestrator vision:"..." repo:owner/repo
> /loop 5m /orchestrator        # auto-wake fallback
> /orchestrator scout:true      # force scouting
> /orchestrator split:1234      # force-split a too-large issue
> /orchestrator resume:1234     # resume parked issue
> /orchestrator force:1234      # override analyzer reject
> /orchestrator stop:true       # graceful shutdown
```
