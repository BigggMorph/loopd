"""Headless claude -p runner for FeatureBench tasks."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FB_STRICT_RULES = """\
STRICT RULES:
1. Implement ONLY source/library code. NEVER modify, add, or delete any file
   inside a `tests/` directory, or any file whose name starts with `test_`.
2. Do NOT write new test functions or test classes.
3. Do NOT run the full test suite.
4. Do NOT create a pull request or push to remote.
5. Make a clean, complete implementation — the feature should fully work.
6. Work only inside the provided repository directory.
7. After implementing, verify with `git diff` that changes look correct.
8. Commit: `git add <source-files-only> && git commit -m "feat: <description>"`.
"""

# v1: the original fixed-role prompt (mirrors the staged-pipeline framing).
_FB_SYSTEM_PROMPT = (
    "You are an expert software engineer implementing a new feature in a "
    "repository.\n\n" + _FB_STRICT_RULES
)

# v2: the dev_v2 playbook framing — the agent owns the whole task and judges
# which steps it needs. Used for A/B comparison against v1.
_FB_SYSTEM_PROMPT_V2 = """\
You are a senior software engineer who owns this task end to end. There is no
prescribed procedure — judge for yourself which steps this task needs
(understanding the codebase, sketching a plan, implementing, self-verifying)
and spend effort proportional to the task's actual complexity.

Tendencies that serve you well:
- Understand the relevant code before editing it.
- For a bug-shaped task, reproduce the behavior first. For a refactor-shaped
  task, behavior preservation is the bar. For a feature, define acceptance
  criteria for yourself before implementing, and map your final diff to them.
- Confidence in one's own work is systematically overestimated — re-read your
  full diff with fresh eyes before declaring done.

""" + _FB_STRICT_RULES

_FB_SYSTEM_PROMPTS = {"v1": _FB_SYSTEM_PROMPT, "v2": _FB_SYSTEM_PROMPT_V2}

_FB_USER_TEMPLATE = """\
Repository: {repo}
Working directory: {workspace_path}

Feature request:
{problem_statement}

Implement the feature described above.
"""


@dataclass
class RunResult:
    task_id: str
    success: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    duration_s: float = 0.0


def _hide_ftp_test_files(worktree_path: Path, fail_to_pass: list[str]) -> list[Path]:
    """Delete FAIL_TO_PASS test files so agent can't see them. Returns list of deleted paths."""
    hidden = []
    for rel_path in fail_to_pass:
        target = worktree_path / rel_path
        if target.exists():
            target.unlink()
            hidden.append(target)
            logger.info(f"Hidden test file: {rel_path}")
        else:
            logger.warning(f"FTP test file not found (already absent?): {rel_path}")
    return hidden


def run_task(
    task: dict,
    timeout: int = 1800,
    model: Optional[str] = None,
    max_turns: int = 50,
    claude_bin: str = "claude",
    pipeline: str = "v1",
) -> RunResult:
    ws = task.get("workspace") or {}
    workspace_path_str = ws.get("path")
    task_id = task.get("id", "unknown")
    meta = (task.get("metadata") or {}).get("feature_bench") or {}

    if not workspace_path_str or not Path(workspace_path_str).is_dir():
        return RunResult(
            task_id=task_id,
            success=False,
            error=f"workspace.path not set or not a directory: {workspace_path_str}",
        )

    if not shutil.which(claude_bin):
        return RunResult(task_id=task_id, success=False, error=f"Claude CLI not found: {claude_bin}")

    workspace_path = Path(workspace_path_str)

    # Hide FTP test files before agent runs
    fail_to_pass = meta.get("fail_to_pass", [])
    _hide_ftp_test_files(workspace_path, fail_to_pass)

    prompt = _FB_USER_TEMPLATE.format(
        repo=ws.get("repo", ""),
        workspace_path=workspace_path_str,
        problem_statement=task.get("prompt", ""),
    )

    system_prompt = _FB_SYSTEM_PROMPTS.get(pipeline, _FB_SYSTEM_PROMPT)
    cmd = [
        claude_bin,
        "-p",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Edit", "Write", "Bash", "Read", "Glob", "Grep", "MultiEdit",
        "--output-format", "json",
        "--max-turns", str(max_turns),
        "--append-system-prompt", system_prompt,
    ]
    if model:
        cmd.extend(["--model", model])

    env = {
        k: v for k, v in os.environ.items()
        if k not in ("CLAUDECODE", "CLAUDE_PLUGIN_ROOT")
    }

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=prompt,
            cwd=str(workspace_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        duration = time.monotonic() - start
        return RunResult(
            task_id=task_id,
            success=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=duration,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return RunResult(task_id=task_id, success=False, error=f"claude -p timed out after {timeout}s", duration_s=duration)
    except Exception as e:
        duration = time.monotonic() - start
        return RunResult(task_id=task_id, success=False, error=str(e), duration_s=duration)
