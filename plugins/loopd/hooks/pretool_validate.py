#!/usr/bin/env python3
"""Loopd PreToolUse hook.

Reads the Claude Code PreToolUse JSON payload from stdin. If the current
Claude session has an active loopd task, and the tool being invoked is
``Task``, verify that ``subagent_type`` and the SHA256 of ``prompt`` match
what ``tick.py`` last emitted. Mismatch → exit 2 with stderr explanation
(Claude Code surfaces this to the LLM as a self-correction prompt).

Bootstrap path: when ``tick init`` runs from a slash-command bash
sub-shell it cannot see Claude Code's session UUID, so it writes a
"pending claim" file under ``~/.loopd/sessions/.pending/<task_id>.json``
instead of a real session file. The first time *this* window's main LLM
invokes ``Task`` after ``tick init`` (PreToolUse fires with the real
``payload.session_id``), we look for a pending file whose prompt SHA256
matches the actual ``tool_input.prompt`` and atomically promote it to
``~/.loopd/sessions/<payload.session_id>.json``. Subsequent hooks then
resolve that file by exact UUID match — no cwd-hash fallback exists.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _loopd_hook_lib import ensure_loopd_core_importable  # noqa: E402

ensure_loopd_core_importable()
from loopd_core import session_store  # noqa: E402


def _resolve_session(payload_session_id: str):
    """Locate the loopd session file by exact CC UUID match.

    No cwd-hash fallback — when the UUID-keyed file does not exist, return
    ``(None, None)`` so the caller can either attempt a pending-claim
    bootstrap (PreToolUse only) or no-op.
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


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return 0  # malformed input — let it pass

    sid = payload.get("session_id") or ""
    resolved_sid, session_file = _resolve_session(sid)

    # Bootstrap: first Task invocation after `tick init`. No UUID-keyed
    # session file exists yet, but a pending claim may. Try to claim it by
    # matching the prompt SHA256 — the Task tool doesn't expose
    # validation_token as a field, but prompt SHA256 uniquely identifies
    # the next_action that tick just minted.
    if (
        session_file is None
        and sid
        and payload.get("tool_name") in ("Task", "Agent")
    ):
        tool_input = payload.get("tool_input") or {}
        actual_prompt = tool_input.get("prompt", "")
        if actual_prompt:
            actual_hash = hashlib.sha256(actual_prompt.encode()).hexdigest()
            claimed = session_store.claim_pending_by_prompt_hash(actual_hash, sid)
            if claimed is not None:
                session_file = claimed
                resolved_sid = sid

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
