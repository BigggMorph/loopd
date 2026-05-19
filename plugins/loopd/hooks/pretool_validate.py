#!/usr/bin/env python3
"""Loopd PreToolUse hook.

Reads the Claude Code PreToolUse JSON payload from stdin. If the current
Claude session has an active loopd task, and the tool being invoked is
``Task``, verify that ``subagent_type`` and the SHA256 of ``prompt`` match
what ``tick.py`` last emitted. Mismatch → exit 2 with stderr explanation
(Claude Code surfaces this to the LLM as a self-correction prompt).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _resolve_session(payload_session_id: str):
    """Locate the loopd session file regardless of which session_id scheme is in
    play: prefer Claude Code's UUID (if tick was launched with LOOPD_SESSION_ID
    set), then fall back to tick.py's cwd-hash naming.
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


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0  # malformed input — let it pass

    _, session_file = _resolve_session(payload.get("session_id") or "")
    if session_file is None:
        return 0

    if payload.get("tool_name") not in ("Task", "Agent"):
        return 0

    try:
        session = json.loads(session_file.read_text())
    except Exception:
        return 0

    last = session.get("last_next_action") or {}
    if last.get("kind") != "invoke_subagent":
        # Not currently expecting a Task — let manual calls through
        return 0

    expected_sub = last.get("subagent_type", "")
    expected_hash = last.get("prompt_sha256", "")

    tool_input = payload.get("tool_input") or {}
    actual_sub = tool_input.get("subagent_type", "")
    actual_prompt = tool_input.get("prompt", "")
    actual_hash = hashlib.sha256(actual_prompt.encode()).hexdigest()

    def _norm(s: str) -> str:
        return s.split(":", 1)[1] if s.startswith("loopd:") else s

    if _norm(actual_sub) != _norm(expected_sub):
        print(
            f"Loopd: subagent_type mismatch.\n"
            f"  expected: {expected_sub}\n"
            f"  got:      {actual_sub}\n"
            f"Use the subagent_type from the most recent `tick` next_action verbatim.",
            file=sys.stderr,
        )
        return 2

    if actual_hash != expected_hash:
        print(
            f"Loopd: prompt has been modified between tick output and Task invocation.\n"
            f"Copy next_action.prompt into the Task tool exactly — no edits, no extra wrapping.\n"
            f"(expected SHA256 {expected_hash[:12]}…, got {actual_hash[:12]}…)",
            file=sys.stderr,
        )
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
