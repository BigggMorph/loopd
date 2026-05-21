"""Headless claude -p runner for SWE-bench tasks.

Runs `claude -p` in the task worktree with a focused implementation prompt.
No loopd hooks — this is a standalone eval subprocess runner.

Usage:
    result = run_task(task_json, timeout=600)
    print(result.success, result.error)
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SWE_BENCH_SYSTEM_PROMPT = """\
You are an expert software engineer solving a bug report. Your job is to make
the minimal source code change that fixes the described issue.

STRICT RULES — violating any of these will cause the evaluation to fail:
1. Fix ONLY source/library code. NEVER modify, add, or delete any file inside
   a `tests/` or `test_*/` directory, or any file whose name starts with `test_`.
2. Do NOT write new test functions or test classes anywhere.
3. Do NOT run the full test suite (it is very slow).
4. Do NOT create a pull request or push to remote.
5. Make the smallest correct change — no refactoring of unrelated code.
6. Work only inside the provided repository directory.
7. After making the fix, verify with `git diff` that changes look correct.
8. Commit: `git add <source-files-only> && git commit -m "fix: <description>"`.
"""

_SWE_BENCH_USER_TEMPLATE = """\
Repository: {repo}
Working directory: {workspace_path}

Bug report:
{problem_statement}

Fix the bug described above. Make a minimal, targeted change.
"""


def _build_prompt(task: dict) -> str:
    ws = task.get("workspace") or {}
    meta = (task.get("metadata") or {}).get("swe_bench") or {}
    return _SWE_BENCH_USER_TEMPLATE.format(
        repo=ws.get("repo", ""),
        workspace_path=ws.get("path", ""),
        problem_statement=task.get("prompt", ""),
    )


@dataclass
class RunResult:
    task_id: str
    success: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    duration_s: float = 0.0


def run_task(
    task: dict,
    timeout: int = 900,
    model: Optional[str] = None,
    max_turns: int = 50,
    claude_bin: str = "claude",
) -> RunResult:
    """Run the loopd implementation agent on a single SWE-bench task.

    Args:
        task: Task JSON dict (must have workspace.path set).
        timeout: Max seconds to wait for claude -p to finish.
        model: Optional model override (e.g. "claude-opus-4-7").
        max_turns: Max turns claude -p may take.
        claude_bin: Path to the claude CLI binary.

    Returns:
        RunResult with success flag and captured stdout/stderr.
    """
    ws = task.get("workspace") or {}
    workspace_path_str = ws.get("path")
    task_id = task.get("id", "unknown")

    if not workspace_path_str or not Path(workspace_path_str).is_dir():
        return RunResult(
            task_id=task_id,
            success=False,
            error=f"workspace.path not set or not a directory: {workspace_path_str}",
        )

    if not shutil.which(claude_bin):
        return RunResult(
            task_id=task_id,
            success=False,
            error=f"Claude CLI not found: {claude_bin}",
        )

    workspace_path = Path(workspace_path_str)
    prompt = _build_prompt(task)

    cmd = [
        claude_bin,
        "-p",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Edit", "Write", "Bash", "Read", "Glob", "Grep", "MultiEdit",
        "--output-format", "json",
        "--max-turns", str(max_turns),
    ]
    if model:
        cmd.extend(["--model", model])

    # Strip CLAUDECODE so nested session guard doesn't block us.
    # Strip CLAUDE_PLUGIN_ROOT so loopd hooks don't fire.
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
        success = proc.returncode == 0
        return RunResult(
            task_id=task_id,
            success=success,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_s=duration,
        )
    except subprocess.TimeoutExpired:
        duration = time.monotonic() - start
        return RunResult(
            task_id=task_id,
            success=False,
            error=f"claude -p timed out after {timeout}s",
            duration_s=duration,
        )
    except Exception as e:
        duration = time.monotonic() - start
        return RunResult(
            task_id=task_id,
            success=False,
            error=str(e),
            duration_s=duration,
        )
