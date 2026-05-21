#!/usr/bin/env python3
"""Loopd PostToolUse hook.

Reads the Claude Code PostToolUse JSON from stdin. If this session has an
active loopd task and the tool was ``Task``, extract the assistant result and
token usage, then pipe them as JSON into ``tick --record`` so the FSM advances.

This script tolerates several known shapes of ``tool_response``:
- ``content`` as a top-level string
- ``result`` / ``text`` as fallbacks
- ``content`` as a list of content blocks (``[{"type":"text","text":"…"}]``)

Resolution: strict CC UUID match — no cwd-hash fallback. If this window did
not originate the task (session file doesn't exist under its UUID), the hook
is a no-op.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loopd_hook_lib import ensure_loopd_core_importable  # noqa: E402

ensure_loopd_core_importable()
from loopd_core import session_store  # noqa: E402


def _resolve_session(payload_session_id: str):
    """Locate the loopd session file by exact CC UUID match.

    Returns ``(None, None)`` when no matching session exists for this window
    — strictly no cwd-hash fallback.
    """
    if not payload_session_id:
        return None, None
    try:
        path = session_store.session_path_for(payload_session_id)
    except ValueError:
        return None, None
    if path.exists():
        return payload_session_id, path
    return None, None


def _extract_text(tool_response: dict) -> str:
    for key in ("content", "result", "text"):
        v = tool_response.get(key)
        if isinstance(v, str) and v:
            return v
    content = tool_response.get("content")
    if isinstance(content, list):
        for blk in content:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = blk.get("text")
                if isinstance(t, str) and t:
                    return t
    return ""


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0

    resolved_sid, session_file = _resolve_session(payload.get("session_id") or "")
    if resolved_sid is None or session_file is None:
        return 0

    if payload.get("tool_name") not in ("Task", "Agent"):
        return 0

    try:
        session = json.loads(session_file.read_text())
    except Exception:
        return 0

    last = session.get("last_next_action") or {}
    token = last.get("validation_token")
    subagent = last.get("subagent_type", "planning")
    if not token:
        return 0

    tool_response = payload.get("tool_response") or {}
    result_text = _extract_text(tool_response)[:10000]
    usage = tool_response.get("usage") or {}

    record = {
        "validation_token": token,
        "agent": subagent,
        "subagent": subagent,
        "result": result_text,
        "duration_ms": int(tool_response.get("totalDurationMs") or 0),
        "tokens": {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cost_usd": 0,
        },
    }

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    tick_path = Path(plugin_root) / "python_core" / "scripts" / "tick"
    if not tick_path.exists():
        print(f"loopd posttool: tick script not found at {tick_path}", file=sys.stderr)
        return 0

    env = os.environ.copy()
    env["LOOPD_SESSION_ID"] = resolved_sid
    try:
        subprocess.run(
            [str(tick_path), "--record"],
            input=json.dumps(record),
            text=True,
            env=env,
            timeout=25,
            capture_output=True,
            check=False,
        )
    except Exception as e:
        print(f"loopd posttool: tick --record failed: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
