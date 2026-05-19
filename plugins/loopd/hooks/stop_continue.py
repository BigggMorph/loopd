#!/usr/bin/env python3
"""Loopd Stop hook (ralph-loop pattern).

If the FSM has not finished, write a JSON instruction back to stdout telling
Claude Code to block the stop and feed a continuation prompt to the main LLM.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


def _resolve_session(payload_session_id: str):
    """See pretool_validate._resolve_session — fallback to tick.py's cwd-hash
    naming when the slash-command Bash sub-shell did not see CLAUDE_SESSION_ID.
    """
    sessions_dir = Path.home() / ".loopd" / "sessions"
    if payload_session_id:
        f = sessions_dir / f"{payload_session_id}.json"
        if f.exists():
            return payload_session_id, f
    cwd_sid = "cwd-" + hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:16]
    f = sessions_dir / f"{cwd_sid}.json"
    if f.exists():
        return cwd_sid, f
    return None, None


def _emit_block(reason: str, system_msg: str) -> int:
    print(json.dumps({"decision": "block", "reason": reason, "systemMessage": system_msg}))
    return 0


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    resolved_sid, session_file = _resolve_session(payload.get("session_id") or "")
    if resolved_sid is None or session_file is None:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    tick_path = Path(plugin_root) / "python_core" / "scripts" / "tick"
    if not tick_path.exists():
        return 0

    env = os.environ.copy()
    env["LOOPD_SESSION_ID"] = resolved_sid

    try:
        proc = subprocess.run(
            [str(tick_path)], env=env, text=True, capture_output=True, timeout=20
        )
    except Exception as e:
        print(f"loopd stop: tick invocation failed: {e}", file=sys.stderr)
        return 0

    try:
        next_json = json.loads(proc.stdout)
    except Exception:
        return 0

    action = next_json.get("next_action") or {}
    kind = action.get("kind", "")

    if kind in ("complete", "failed", ""):
        if session_file.exists():
            session_file.unlink()
        return 0

    if kind == "checkpoint_human":
        question = action.get("question", "loopd: human input required")
        return _emit_block(f"loopd: 사용자 입력 대기 중 — {question}", "loopd checkpoint")

    if kind == "invoke_subagent":
        subagent = action.get("subagent_type", "")
        prompt = action.get("prompt", "")
        task_id = next_json.get("task_id", "")
        iteration = action.get("iteration", 0)

        reason = (
            f"loopd 파이프라인이 아직 진행 중입니다 (task={task_id}, iteration={iteration}).\n\n"
            f"다음 next_action을 `Task` 도구로 **한 글자도 수정하지 말고** 호출하세요:\n\n"
            f"- subagent_type: {subagent}\n"
            f'- description: "Loopd phase: {subagent} (task {task_id})"\n'
            f"- prompt:\n\n"
            f"<<<LOOPD_PROMPT_BEGIN>>>\n"
            f"{prompt}\n"
            f"<<<LOOPD_PROMPT_END>>>\n\n"
            f"다른 도구는 호출하지 마세요. Task 호출 후 hooks가 자동으로 다음 단계를 처리합니다."
        )
        return _emit_block(reason, f"loopd iteration {iteration} → {subagent}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
