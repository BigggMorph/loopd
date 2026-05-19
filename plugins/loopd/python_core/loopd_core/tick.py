"""loopd tick — deterministic FSM driver for the loopd Claude Code Plugin.

The main Claude Code LLM stays "thin": it calls this script via Bash, copies the
returned ``next_action`` into a Task tool invocation, and lets hooks loop the
process. All routing, skip, and turn-recording decisions happen here.

Subcommands
-----------
``tick init --args "<raw $ARGUMENTS string>"``
    Parse a /dev-task argument string, create a Task + worktree, return the first
    ``next_action``.

``tick``
    Read the current session's task and return the next ``next_action`` without
    advancing state (idempotent).

``tick --record -``  (stdin JSON)
    Apply a completed Task result to the FSM (start_turn + end_turn + persist)
    and return the next ``next_action``.

``tick resume <task_id>``
    Bind this session to an existing task and return its current ``next_action``.

``tick state <task_id>``
    Read-only — print the task's current phase, agent, iteration as JSON.

Output is a single JSON line on stdout. Human logs go to stderr.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("loopd.tick")


# ─────────────────────────────────────────────────────────────────────────────
# Session file — per-Claude-session state under ~/.loopd/sessions/<id>.json
# ─────────────────────────────────────────────────────────────────────────────


def _session_id() -> str:
    """Determine the session identifier.

    Priority:
    1. ``LOOPD_SESSION_ID`` env (set by hooks from the Claude Code session_id)
    2. ``CLAUDE_SESSION_ID`` env (older variants)
    3. Fall back to the cwd hash so single-window dev still works.
    """
    sid = os.environ.get("LOOPD_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
    if sid:
        return sid
    return "cwd-" + hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:16]


def _session_file() -> Path:
    from loopd_core.config import get_config

    cfg = get_config()
    cfg.sessions_path.mkdir(parents=True, exist_ok=True)
    return cfg.sessions_path / f"{_session_id()}.json"


def _read_session() -> dict[str, Any]:
    f = _session_file()
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {}


def _write_session(data: dict[str, Any]) -> None:
    f = _session_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    os.replace(tmp, f)


def _delete_session() -> None:
    f = _session_file()
    if f.exists():
        f.unlink()


# ─────────────────────────────────────────────────────────────────────────────
# Validation tokens (PreToolUse hook checks these)
# ─────────────────────────────────────────────────────────────────────────────


def _secret_path() -> Path:
    from loopd_core.config import get_config

    return get_config().loopd_root / ".secret"


def _load_or_create_secret() -> bytes:
    p = _secret_path()
    if p.exists():
        return p.read_bytes()
    p.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_bytes(32)
    p.write_bytes(raw)
    try:
        p.chmod(0o600)
    except OSError:
        pass
    return raw


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()


def mint_token(task_id: str, iteration: int, subagent: str, prompt: str) -> str:
    secret = _load_or_create_secret()
    payload = json.dumps(
        {"task_id": task_id, "iter": iteration, "subagent": subagent, "p": _prompt_hash(prompt)},
        sort_keys=True,
    ).encode()
    sig = hmac.new(secret, payload, hashlib.sha256).hexdigest()[:8]
    return f"v1.{task_id}.iter-{iteration}.{subagent}.{sig}"


def verify_token(token: str, session: dict[str, Any]) -> bool:
    expected = (session.get("last_next_action") or {}).get("validation_token")
    return bool(expected) and hmac.compare_digest(token, expected)


# ─────────────────────────────────────────────────────────────────────────────
# Argument parsing for `/dev-task`
# ─────────────────────────────────────────────────────────────────────────────

_REPO_RE = re.compile(r"\brepo:([^\s]+)")
_LEVEL_RE = re.compile(r"\blevel:(\d+)")
_BRANCH_RE = re.compile(r"\bbranch:([^\s]+)")
_ISSUE_RE = re.compile(r"\bissue:(\d+)")
_PRIORITY_RE = re.compile(r"\bpriority:(\d+)")


def parse_dev_task_args(raw: str) -> dict[str, Any]:
    """Extract prompt / repo / level / branch from a free-form /dev-task string.

    The user typically writes::

        /dev-task "랜딩 페이지 만들어줘" repo:BigggMorph/landing-site level:2

    ``raw`` is everything after ``/dev-task``.
    """
    raw = raw.strip()
    repo_m = _REPO_RE.search(raw)
    level_m = _LEVEL_RE.search(raw)
    branch_m = _BRANCH_RE.search(raw)

    repo = repo_m.group(1) if repo_m else None
    level = int(level_m.group(1)) if level_m else 1
    branch = branch_m.group(1) if branch_m else "main"

    cleaned = raw
    for m in (repo_m, level_m, branch_m):
        if m:
            cleaned = cleaned.replace(m.group(0), "")
    prompt = cleaned.strip().strip('"').strip("'").strip()

    if not prompt:
        raise ValueError("dev-task: prompt text is required")
    if not repo:
        raise ValueError("dev-task: repo:<owner/repo> is required")

    return {"prompt": prompt, "repo": repo, "level": level, "branch": branch}


def parse_research_task_args(raw: str) -> dict[str, Any]:
    """Extract topic / repo / issue / priority from a /research-task string.

    Example::

        /research-task "AI agent frameworks 비교" repo:BigggMorph/oh-my-agents issue:1234

    ``repo:`` is optional unless ``issue:N`` is given (a GitHub Issue comment
    needs both owner and repo to address the issue).
    """
    raw = raw.strip()
    repo_m = _REPO_RE.search(raw)
    issue_m = _ISSUE_RE.search(raw)
    priority_m = _PRIORITY_RE.search(raw)

    repo = repo_m.group(1) if repo_m else None
    issue = int(issue_m.group(1)) if issue_m else None
    priority = int(priority_m.group(1)) if priority_m else 3

    cleaned = raw
    for m in (repo_m, issue_m, priority_m):
        if m:
            cleaned = cleaned.replace(m.group(0), "")
    topic = cleaned.strip().strip('"').strip("'").strip()

    if not topic:
        raise ValueError("research-task: topic text is required")
    if issue is not None and not repo:
        raise ValueError("research-task: issue:N requires repo:<owner/repo>")

    return {"topic": topic, "repo": repo, "issue": issue, "priority": priority}


# ─────────────────────────────────────────────────────────────────────────────
# Routing — picks the next agent based on FSM + task state
# ─────────────────────────────────────────────────────────────────────────────

# Phase 1 maps oh-my-agents' 5-agent pipeline (CORE → ANALYSIS → PLANNING →
# SOLUTIONING → IMPL) onto Issue #1174's 3-agent contract by collapsing
# ANALYSIS+PLANNING+SOLUTIONING into a single "planning" subagent.
_AGENT_PIPELINE = ["planning", "implementation", "review"]
_CRITICS_AFTER = {
    "planning": "plan-critic",
    "implementation": "solution-critic",  # critique-as-second-look on impl
}

# Hard cap on how many times research can be re-run after a critic FAIL.
# Without this, a chronically-failing critic would loop forever.
_RESEARCH_MAX_ATTEMPTS = 2


def _next_agent(task_dict: dict[str, Any]) -> Optional[str]:
    """Decide which subagent runs next.

    Pipeline: planning → plan-critic → implementation → solution-critic → review.
    On critic FAIL verdict, loops back to the predecessor:
    - plan-critic FAIL → planning
    - solution-critic FAIL → implementation
    - review request_changes → implementation
    Counts turns rather than presence so each backward loop runs the full
    sub-pipeline again (e.g. impl → solution-critic → review). Backward hard
    limit is enforced in :func:`_build_next_action`.
    """
    turns = task_dict.get("turns") or []
    completed = [
        t.get("subagent") or t.get("agent")
        for t in turns
        if t.get("state") in ("completed", "COMPLETED")
    ]

    last_completed_turn = next(
        (t for t in reversed(turns) if t.get("state") in ("completed", "COMPLETED")),
        None,
    )
    if last_completed_turn and _critic_verdict_fail(last_completed_turn):
        last_sub = last_completed_turn.get("subagent") or last_completed_turn.get("agent")
        if last_sub == "plan-critic":
            return "planning"
        if last_sub == "solution-critic":
            return "implementation"
        if last_sub == "review":
            return "implementation"

    planning_count = completed.count("planning")
    plan_critic_count = completed.count("plan-critic")
    if planning_count > plan_critic_count:
        return "plan-critic"

    impl_count = completed.count("implementation")
    solution_critic_count = completed.count("solution-critic")
    if impl_count > solution_critic_count:
        return "solution-critic"

    review_count = completed.count("review")
    if solution_critic_count > review_count:
        return "review"

    if planning_count == 0:
        return "planning"
    if impl_count == 0:
        return "implementation"
    if review_count == 0:
        return "review"

    return None


def _is_backward_step(task_dict: dict[str, Any], agent: str) -> bool:
    """True if running `agent` would be a backward transition.

    A backward step is when we're about to re-run planning or implementation
    (after plan-critic / solution-critic / review FAIL) — detectable by the
    fact that the agent already has one or more completed turns.
    """
    if agent not in {"planning", "implementation"}:
        return False
    turns = task_dict.get("turns") or []
    for t in turns:
        if t.get("state") not in ("completed", "COMPLETED"):
            continue
        sub = t.get("subagent") or t.get("agent")
        if sub == agent:
            return True
    return False


def _critic_verdict_fail(turn: dict[str, Any]) -> bool:
    """Heuristic: scan turn.result text for a FAIL/rework verdict.

    Recognises three verdict vocabularies:
    - plan-critic / solution-critic: ``"approve" | "rework"``
    - research-critic: ``"PASS" | "FAIL"``
    - review: ``"approve" | "request_changes"``
    """
    result = (turn.get("result") or "").lower()
    if not result:
        return False
    if '"verdict"' not in result and "verdict:" not in result:
        return False
    return (
        '"fail"' in result or "fail," in result
        or '"rework"' in result or "rework," in result
        or '"request_changes"' in result or "request_changes," in result
    )


def _next_agent_research(task_dict: dict[str, Any]) -> Optional[str]:
    """Decide the next subagent for a research task.

    Pipeline: research → research-critic → (gh-post if issue is set).
    On critic FAIL, loop back to research up to ``_RESEARCH_MAX_ATTEMPTS`` times.
    """
    turns = task_dict.get("turns") or []
    completed = [
        t.get("subagent") or t.get("agent")
        for t in turns
        if t.get("state") in ("completed", "COMPLETED")
    ]

    research_attempts = sum(1 for s in completed if s == "research")
    last_completed = next(
        (
            t for t in reversed(turns)
            if t.get("state") in ("completed", "COMPLETED")
        ),
        None,
    )

    # critic FAIL → backward to research, if attempts remain
    if (
        last_completed
        and last_completed.get("subagent") == "research-critic"
        and _critic_verdict_fail(last_completed)
        and research_attempts < _RESEARCH_MAX_ATTEMPTS
    ):
        return "research"

    if "research" not in completed:
        return "research"

    # Only run the critic once per fresh research turn. Counting completions:
    # we want exactly one critic per research, so if completed.count("research-critic")
    # < research_attempts, run critic.
    if completed.count("research-critic") < research_attempts:
        return "research-critic"

    # Both done. If task has a GitHub issue, post the result.
    metadata = task_dict.get("metadata") or {}
    if metadata.get("github_issue") and "gh-post" not in completed:
        return "gh-post"

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Prompt building
# ─────────────────────────────────────────────────────────────────────────────


def _is_research_task(task_dict: dict[str, Any]) -> bool:
    return (task_dict.get("task_type") or "dev") == "research"


def _research_dir_for(task_id: str) -> Path:
    from loopd_core.config import get_config

    return get_config().loopd_root / "research-tasks" / task_id


def _build_prompt(agent: str, task_dict: dict[str, Any], workspace_path: Path) -> str:
    from loopd_core.config import get_config
    from loopd_core.prompt_renderer import render

    cfg = get_config()
    agent_md = cfg.plugin_root / "agents" / f"{agent}.md"
    base = agent_md.read_text() if agent_md.exists() else f"# {agent} subagent\n"

    # Strip the YAML frontmatter for the inline prompt body — the Task tool's
    # subagent_type already routes to the agent definition, so we want only the
    # rendered instructions to land in the prompt.
    if base.startswith("---"):
        end = base.find("---", 3)
        if end != -1:
            base = base[end + 3 :].lstrip()

    metadata = task_dict.get("metadata") or {}
    is_research = _is_research_task(task_dict)
    artifact_names = [a.get("name") or a.get("path", "") for a in task_dict.get("artifacts", [])]
    ctx = {
        "TASK_ID": task_dict.get("id", ""),
        "TASK_PROMPT": task_dict.get("prompt", ""),
        "TASK_LEVEL": str(task_dict.get("level", 1)),
        "WORKSPACE_PATH": str(workspace_path),
        "BRANCH": (task_dict.get("workspace") or {}).get("branch", "main"),
        "REPO": (task_dict.get("workspace") or {}).get("repo", "") or (metadata.get("github_repo") or ""),
        "EXISTING_ARTIFACTS": ", ".join(artifact_names) if artifact_names else "(none)",
        "ITERATION": str(len(task_dict.get("turns") or []) + 1),
        # Research-specific
        "RESEARCH_TOPIC": task_dict.get("prompt", "") if is_research else "",
        "GITHUB_ISSUE": str(metadata.get("github_issue") or ""),
        "GITHUB_REPO": metadata.get("github_repo") or "",
    }

    rendered = render(base, ctx)

    if is_research:
        header = (
            f"You are the **{agent}** subagent in the loopd research pipeline for "
            f"task `{ctx['TASK_ID']}`.\n\n"
            f"Workspace (output directory): `{ctx['WORKSPACE_PATH']}`.\n"
            f"Research topic: {ctx['TASK_PROMPT']}\n"
            + (
                f"GitHub Issue target: {ctx['GITHUB_REPO']} #{ctx['GITHUB_ISSUE']}\n"
                if ctx["GITHUB_ISSUE"]
                else ""
            )
            + f"\nAll file output **must** land inside the workspace path above. "
            f"You are not modifying any source repository.\n\n"
            f"---\n\n"
        )
    else:
        header = (
            f"You are the **{agent}** subagent in the loopd pipeline for task "
            f"`{ctx['TASK_ID']}`.\n\n"
            f"Workspace: `{ctx['WORKSPACE_PATH']}` on branch `{ctx['BRANCH']}`.\n"
            f"User request: {ctx['TASK_PROMPT']}\n\n"
            f"All file edits, commits, and command execution **must** happen inside "
            f"the workspace path above. Do not modify files anywhere else.\n\n"
            f"---\n\n"
        )
    return header + rendered


# ─────────────────────────────────────────────────────────────────────────────
# Subcommand implementations
# ─────────────────────────────────────────────────────────────────────────────


def _emit(payload: dict[str, Any]) -> int:
    print(json.dumps(payload, default=str), flush=True)
    return 0


def _emit_error(message: str, exit_code: int = 1) -> int:
    print(json.dumps({"error": message}, default=str), flush=True)
    print(f"loopd tick: {message}", file=sys.stderr)
    return exit_code


def _build_next_action(task_dict: dict[str, Any], workspace_path: Path) -> dict[str, Any]:
    """Compute next_action JSON for a given task state."""
    status = task_dict.get("status")
    if status in ("completed", "COMPLETED"):
        return {"kind": "complete", "task_id": task_dict.get("id")}
    if status in ("failed", "FAILED"):
        return {"kind": "failed", "task_id": task_dict.get("id"),
                "error": task_dict.get("error_message", "unspecified failure")}
    if status in ("waiting_human", "WAITING_HUMAN"):
        return {"kind": "checkpoint_human", "task_id": task_dict.get("id"),
                "question": task_dict.get("checkpoint_question",
                                          "loopd: human input required.")}

    if _is_research_task(task_dict):
        agent = _next_agent_research(task_dict)
        # research uses a flat dir, not a git worktree
        workspace_path = _research_dir_for(task_dict["id"])

        if agent is None:
            # Distinguish "exhausted retries" from "naturally finished".
            turns = task_dict.get("turns") or []
            completed_subs = [
                (t.get("subagent") or t.get("agent"))
                for t in turns
                if t.get("state") in ("completed", "COMPLETED")
            ]
            research_attempts = sum(1 for s in completed_subs if s == "research")
            last_completed = next(
                (t for t in reversed(turns)
                 if t.get("state") in ("completed", "COMPLETED")),
                None,
            )
            if (
                last_completed
                and (last_completed.get("subagent") or last_completed.get("agent"))
                    == "research-critic"
                and _critic_verdict_fail(last_completed)
                and research_attempts >= _RESEARCH_MAX_ATTEMPTS
            ):
                last_critic_result = (last_completed.get("result") or "")[:1500]
                return {
                    "kind": "checkpoint_human",
                    "task_id": task_dict.get("id"),
                    "question": (
                        f"loopd research: critic FAIL but max attempts "
                        f"({_RESEARCH_MAX_ATTEMPTS}) reached. Inspect the latest "
                        f"critic feedback below and decide manually whether to "
                        f"accept the research output, edit it, or abort.\n\n"
                        f"Last critic feedback:\n{last_critic_result}"
                    ),
                }
            return {"kind": "complete", "task_id": task_dict.get("id")}
    else:
        agent = _next_agent(task_dict)

    if not agent:
        return {"kind": "complete", "task_id": task_dict.get("id")}

    if not _is_research_task(task_dict) and _is_backward_step(task_dict, agent):
        from loopd_core.agents.pipeline import _MAX_BACKWARD_TRANSITIONS

        ctx = task_dict.get("context") or {}
        backward_count = int(ctx.get("backward_transition_count") or 0)
        if backward_count >= _MAX_BACKWARD_TRANSITIONS:
            last_critic_result = ""
            for t in reversed(task_dict.get("turns") or []):
                if t.get("state") in ("completed", "COMPLETED") and _critic_verdict_fail(t):
                    last_critic_result = (t.get("result") or "")[:1500]
                    break
            return {
                "kind": "checkpoint_human",
                "task_id": task_dict.get("id"),
                "question": (
                    f"loopd: critic FAIL but backward transition hard limit "
                    f"({_MAX_BACKWARD_TRANSITIONS}) reached. Inspect the latest "
                    f"critic feedback below and decide manually whether to "
                    f"override, edit the artifacts, or abort.\n\n"
                    f"Last critic feedback:\n{last_critic_result}"
                ),
            }

    iteration = len(task_dict.get("turns") or []) + 1
    prompt = _build_prompt(agent, task_dict, workspace_path)
    token = mint_token(task_dict["id"], iteration, agent, prompt)

    return {
        "kind": "invoke_subagent",
        "task_id": task_dict["id"],
        "subagent_type": agent,
        "prompt": prompt,
        "validation_token": token,
        "prompt_sha256": _prompt_hash(prompt),
        "cwd": str(workspace_path),
        "iteration": iteration,
    }


def _persist_session_for_action(task_id: str, next_action: dict[str, Any]) -> None:
    session = _read_session()
    session["task_id"] = task_id
    session["last_next_action"] = next_action
    _write_session(session)


def cmd_init(args: argparse.Namespace) -> int:
    task_type = getattr(args, "type", "dev") or "dev"
    if task_type == "research":
        return _cmd_init_research(args)
    return _cmd_init_dev(args)


def _cmd_init_dev(args: argparse.Namespace) -> int:
    from loopd_core.config import get_config
    from loopd_core.state.task_manager import TaskManager
    from loopd_core.state.workspace_manager import WorkspaceManager

    cfg = get_config()
    cfg.ensure_directories()

    try:
        parsed = parse_dev_task_args(args.args or "")
    except ValueError as e:
        return _emit_error(str(e), exit_code=2)

    tm = TaskManager(cfg)
    task = tm.create_task(
        prompt=parsed["prompt"],
        source_type="loopd",
        source_ref="dev-task",
        level=parsed["level"],
        workspace_repo=parsed["repo"],
        workspace_branch=parsed["branch"],
        task_type="dev",
    )

    wm = WorkspaceManager(cfg)
    try:
        workspace_path = wm.setup_task_workspace(
            task_id=task.id, repo=parsed["repo"], base_branch=parsed["branch"]
        )
    except Exception as e:
        return _emit_error(f"failed to set up workspace: {e}", exit_code=3)

    # Move to ACTIVE so subsequent reads find the task
    from loopd_core.types import TaskStatus

    tm.move_task(task.id, TaskStatus.ACTIVE)

    task_dict = task.to_dict() if hasattr(task, "to_dict") else json.loads(task.model_dump_json())
    next_action = _build_next_action(task_dict, workspace_path)
    _persist_session_for_action(task.id, next_action)

    return _emit({
        "task_id": task.id,
        "workspace_path": str(workspace_path),
        "branch": parsed["branch"],
        "next_action": next_action,
        "fsm": {"iteration": 1, "phase": "ready"},
    })


def _cmd_init_research(args: argparse.Namespace) -> int:
    from loopd_core.config import get_config
    from loopd_core.state.task_manager import TaskManager
    from loopd_core.types import TaskStatus

    cfg = get_config()
    cfg.ensure_directories()

    try:
        parsed = parse_research_task_args(args.args or "")
    except ValueError as e:
        return _emit_error(str(e), exit_code=2)

    metadata: dict[str, Any] = {}
    if parsed["repo"]:
        metadata["github_repo"] = parsed["repo"]
    if parsed["issue"] is not None:
        metadata["github_issue"] = parsed["issue"]

    tm = TaskManager(cfg)
    task = tm.create_task(
        prompt=parsed["topic"],
        source_type="loopd",
        source_ref="research-task",
        priority=parsed["priority"],
        level=0,
        workspace_repo=None,
        task_type="research",
        metadata=metadata or None,
    )

    research_dir = _research_dir_for(task.id)
    research_dir.mkdir(parents=True, exist_ok=True)

    tm.move_task(task.id, TaskStatus.ACTIVE)

    task_dict = task.to_dict() if hasattr(task, "to_dict") else json.loads(task.model_dump_json())
    next_action = _build_next_action(task_dict, research_dir)
    _persist_session_for_action(task.id, next_action)

    return _emit({
        "task_id": task.id,
        "workspace_path": str(research_dir),
        "task_type": "research",
        "github_issue": parsed["issue"],
        "github_repo": parsed["repo"],
        "next_action": next_action,
        "fsm": {"iteration": 1, "phase": "ready"},
    })


def _resolve_task_and_workspace(task_id: str) -> tuple[dict[str, Any], Path]:
    from loopd_core.config import get_config
    from loopd_core.state.task_manager import TaskManager
    from loopd_core.state.workspace_manager import WorkspaceManager

    cfg = get_config()
    tm = TaskManager(cfg)
    task = tm.read_task(task_id)
    task_dict = task.to_dict() if hasattr(task, "to_dict") else json.loads(task.model_dump_json())

    if _is_research_task(task_dict):
        return task_dict, _research_dir_for(task_id)

    wm = WorkspaceManager(cfg)
    repo = (task_dict.get("workspace") or {}).get("repo", "")
    workspace_path = wm.get_worktree_path(task_id, wm.parse_repo_to_url(repo)) if repo else cfg.workspaces_path

    return task_dict, workspace_path


def cmd_tick(args: argparse.Namespace) -> int:
    session = _read_session()
    task_id = session.get("task_id")
    if not task_id:
        return _emit_error("no active loopd task — run /dev-task first", exit_code=2)

    try:
        task_dict, workspace_path = _resolve_task_and_workspace(task_id)
    except Exception as e:
        return _emit_error(f"failed to read task {task_id}: {e}", exit_code=2)

    next_action = _build_next_action(task_dict, workspace_path)
    _persist_session_for_action(task_id, next_action)
    return _emit({
        "task_id": task_id,
        "workspace_path": str(workspace_path),
        "next_action": next_action,
    })


def cmd_record(args: argparse.Namespace) -> int:
    payload_text = sys.stdin.read()
    if not payload_text.strip():
        return _emit_error("--record requires JSON on stdin", exit_code=2)

    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as e:
        return _emit_error(f"invalid JSON: {e}", exit_code=2)

    session = _read_session()
    if not verify_token(payload.get("validation_token", ""), session):
        return _emit_error("validation token mismatch — refusing to record", exit_code=2)

    task_id = session.get("task_id")
    if not task_id:
        return _emit_error("no active task", exit_code=2)

    from loopd_core.config import get_config
    from loopd_core.state.event_logger import EventLogger
    from loopd_core.state.pipeline_state_machine import PipelineStateMachine
    from loopd_core.state.task_manager import TaskManager
    from loopd_core.types import TurnState

    cfg = get_config()
    tm = TaskManager(cfg)
    ev = EventLogger(cfg)
    sm = PipelineStateMachine.load(task_id, tm, ev, max_iterations=cfg.max_pipeline_iterations)

    agent = payload.get("agent") or payload.get("subagent") or "planning"
    subagent = payload.get("subagent") or agent

    sm.tick()
    sm.start_turn(agent=agent, subagent=subagent)

    state_str = (payload.get("state") or "completed").lower()
    state_enum = TurnState.FAILED if state_str in ("failed", "error") else TurnState.COMPLETED

    tokens_in = int((payload.get("tokens") or {}).get("input") or 0)
    tokens_out = int((payload.get("tokens") or {}).get("output") or 0)
    cost_usd = float((payload.get("tokens") or {}).get("cost_usd") or 0.0)
    duration_ms = int(payload.get("duration_ms") or 0)
    model = payload.get("model") or "unknown"
    result = (payload.get("result") or "")[:4000]
    error = payload.get("error")

    sm.end_turn(
        state=state_enum,
        result=result,
        error=error,
        tokens=(tokens_in + tokens_out) or None,
        duration_ms=duration_ms or None,
        input_tokens=tokens_in or None,
        output_tokens=tokens_out or None,
        cost_usd=cost_usd or None,
        model=model,
    )
    sm.persist()

    # Re-read after persist to capture new state
    task_dict = sm.task.to_dict() if hasattr(sm.task, "to_dict") else json.loads(sm.task.model_dump_json())

    # If a dev critic (or review) just landed a FAIL/request_changes verdict,
    # the next routing will go backward to planning/implementation — record the
    # transition so the hard limit in _build_next_action can fire when exhausted.
    if (
        not _is_research_task(task_dict)
        and subagent in ("plan-critic", "solution-critic", "review")
        and state_enum == TurnState.COMPLETED
    ):
        last_turn = (task_dict.get("turns") or [{}])[-1]
        if _critic_verdict_fail(last_turn):
            sm.record_backward_transition()
            sm.persist()
            task_dict = sm.task.to_dict() if hasattr(sm.task, "to_dict") else json.loads(sm.task.model_dump_json())

    from loopd_core.state.workspace_manager import WorkspaceManager

    wm = WorkspaceManager(cfg)
    repo = (task_dict.get("workspace") or {}).get("repo", "")
    workspace_path = wm.get_worktree_path(task_id, wm.parse_repo_to_url(repo)) if repo else cfg.workspaces_path

    next_action = _build_next_action(task_dict, workspace_path)
    if next_action["kind"] == "complete":
        sm.complete_pipeline()
        _delete_session()
    else:
        _persist_session_for_action(task_id, next_action)

    return _emit({
        "task_id": task_id,
        "next_action": next_action,
        "fsm": {"iteration": sm.iteration, "phase": sm.phase.value if hasattr(sm.phase, "value") else str(sm.phase)},
    })


def cmd_resume(args: argparse.Namespace) -> int:
    task_id = args.task_id
    try:
        task_dict, workspace_path = _resolve_task_and_workspace(task_id)
    except Exception as e:
        return _emit_error(f"task {task_id} not found: {e}", exit_code=2)

    next_action = _build_next_action(task_dict, workspace_path)
    _persist_session_for_action(task_id, next_action)
    return _emit({"task_id": task_id, "workspace_path": str(workspace_path),
                  "next_action": next_action})


def cmd_state(args: argparse.Namespace) -> int:
    task_id = args.task_id
    try:
        task_dict, workspace_path = _resolve_task_and_workspace(task_id)
    except Exception as e:
        return _emit_error(f"task {task_id} not found: {e}", exit_code=2)

    return _emit({
        "task_id": task_id,
        "status": task_dict.get("status"),
        "level": task_dict.get("level"),
        "turns": len(task_dict.get("turns") or []),
        "artifacts": [a.get("name") or a.get("path") for a in task_dict.get("artifacts", [])],
        "workspace_path": str(workspace_path),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="loopd-tick", description="loopd FSM driver")
    parser.add_argument("--record", action="store_true",
                        help="Record a completed Task result (reads JSON from stdin).")
    sub = parser.add_subparsers(dest="cmd")

    init_p = sub.add_parser("init", help="Initialize a new dev or research task.")
    init_p.add_argument("--args", required=False, default="",
                        help="Raw $ARGUMENTS string from the slash command.")
    init_p.add_argument("--type", choices=["dev", "research"], default="dev",
                        help="Task type. Defaults to 'dev'.")

    resume_p = sub.add_parser("resume", help="Bind session to an existing task.")
    resume_p.add_argument("task_id")

    state_p = sub.add_parser("state", help="Read-only task state lookup.")
    state_p.add_argument("task_id")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOOPD_LOG", "WARNING").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    try:
        if args.record:
            return cmd_record(args)
        if args.cmd == "init":
            return cmd_init(args)
        if args.cmd == "resume":
            return cmd_resume(args)
        if args.cmd == "state":
            return cmd_state(args)
        # default (no subcommand) → tick
        return cmd_tick(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        logger.exception("loopd tick crashed")
        return _emit_error(f"internal error: {e}", exit_code=1)


if __name__ == "__main__":
    sys.exit(main())
