"""Infer why the lead Claude thread was woken up.

The lead has no native API for "wake reason," so we reconstruct it from the
transcript JSONL + the orchestrator state file. Output is consumed by the
playbook's `match (issue.status, wake_reason)` dispatcher (see
docs/orchestrator-design.md §9).

Possible reasons (per §9 Step 4):

  ("teammate_reply",  "issue-analyzer" | "tester" | "issue-scout")
  ("orch_hook_inject", "dev_done")
  ("user_input",       None)   # AskUserQuestion answer
  ("fresh",            None)   # manual /orchestrator, /loop timer, first call

The transcript path is a JSONL file (one event per line). Different Claude
Code versions tag events differently, so this parser is intentionally
forgiving — when in doubt, fall back to ("fresh", None).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ORCH_INJECT_MARKER = "ORCH_INJECT:dev_done"
TEAMMATE_NAMES = (
    "issue-analyzer",
    "tester",
    "issue-scout",
    # Rev 17 planning layer.
    "product-planner",
    "roadmap-strategist",
    "vision-critic",
)

# Maps the `phase` field of a teammate's JSON-tail reply to its sender slug.
# This is the primary, contract-grounded detector — every agent prompt
# (`agents/*.md`) already mandates a single-line JSON tail with a `phase`
# field, so detection works even when no `[name]:` prefix is present.
#
# Keep in sync with the `phase:` values in each agent's output contract.
PHASE_TO_SENDER: Dict[str, str] = {
    "analyze": "issue-analyzer",
    "test": "tester",
    "scout": "issue-scout",
    "reflection": "issue-scout",         # REFLECTION_REQUEST reply (Rev 13 D1)
    "plan": "product-planner",
    "roadmap": "roadmap-strategist",
    "vision_check": "vision-critic",
}

WakeReason = Tuple[str, Optional[str]]


def _read_tail(path: Path, max_lines: int = 50) -> List[Dict[str, Any]]:
    """Read up to `max_lines` JSONL events from the end of the transcript.

    Malformed lines are skipped silently — transcripts can be truncated
    mid-write (the Stop hook runs before the harness fully flushes).
    """
    if not path.exists():
        return []
    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return []
    lines = raw.splitlines()
    tail = lines[-max_lines:]
    events: List[Dict[str, Any]] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _message_text(msg: Any) -> str:
    """Best-effort extraction of a string body from a transcript message.

    Transcript schemas vary between Claude Code versions. We try the most
    common shapes:
      - {"content": "..."}                          (string content)
      - {"content": [{"type":"text","text":"..."}]} (Anthropic-style blocks)
      - {"text": "..."}
    """
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg
    if isinstance(msg, dict):
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts: List[str] = []
            for block in c:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    t = block.get("text") or block.get("content") or ""
                    if isinstance(t, str):
                        parts.append(t)
            return "\n".join(parts)
        t = msg.get("text") or msg.get("body") or ""
        if isinstance(t, str):
            return t
    return ""


def _event_text(event: Dict[str, Any]) -> str:
    if "message" in event:
        return _message_text(event["message"])
    return _message_text(event)


def _event_role(event: Dict[str, Any]) -> str:
    msg = event.get("message")
    if isinstance(msg, dict):
        r = msg.get("role")
        if isinstance(r, str):
            return r
    r = event.get("role") or event.get("type") or ""
    return r if isinstance(r, str) else ""


def _last_event(events: List[Dict[str, Any]], pred) -> Optional[Dict[str, Any]]:
    for ev in reversed(events):
        if pred(ev):
            return ev
    return None


def _parse_teammate_sender(text: str) -> Optional[str]:
    """Look for a sender marker in the message body.

    Agent Teams' sender-name prefix format is not yet stable across versions,
    so we accept several patterns:

      [issue-analyzer]: ...
      [from:tester] ...
      (sender=issue-scout) ...
      ## issue-analyzer
    """
    if not text:
        return None
    head = text.strip().splitlines()[0] if text.strip() else ""
    head_lower = head.lower()
    for name in TEAMMATE_NAMES:
        candidates = (
            f"[{name}]",
            f"[from:{name}]",
            f"(sender={name})",
            f"## {name}",
            f"# {name}",
            f"<{name}>",
            f"sender: {name}",
            f"from {name}",
        )
        for c in candidates:
            if c in head_lower:
                return name
    return None


def _parse_phase_from_json_tail(text: str) -> Optional[str]:
    """Extract sender from the JSON tail's `phase` field.

    Every agent prompt mandates that the LAST non-empty line of the reply
    is a single-line JSON object containing a `phase` field. We parse that
    structurally and map phase → sender via PHASE_TO_SENDER. This works
    even when the teammate forgets to prepend a `[name]:` prefix — which
    they currently always do, since no agent prompt instructs them
    otherwise (see fix history: 2026-05-23).

    Robust to:
      - leading prose ("Here is my analysis: {...}")
      - fenced code blocks (```json ... ```)
      - trailing whitespace / blank lines after the JSON
      - very long messages (we only scan the last few non-empty lines)
    """
    if not text:
        return None
    # Scan from the end backwards through non-empty lines for a single-line
    # JSON object. Cap at 8 lines so an enormous prose body doesn't bog us.
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for line in reversed(lines[-8:]):
        # Skip code-fence delimiters.
        if line.startswith("```"):
            continue
        # Quick reject: only consider candidate JSON objects.
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        phase = obj.get("phase")
        if isinstance(phase, str):
            sender = PHASE_TO_SENDER.get(phase)
            if sender:
                return sender
        # First valid JSON dict we find — even if no recognized phase, stop
        # looking; the tail is not a teammate reply.
        return None
    return None


def infer(transcript_path: str, state: Dict[str, Any]) -> WakeReason:
    if not transcript_path:
        return ("fresh", None)
    events = _read_tail(Path(transcript_path), max_lines=50)
    if not events:
        return ("fresh", None)

    # Latest user-facing message (the one the lead is "responding" to).
    last_user = _last_event(events, lambda e: _event_role(e) == "user")
    if last_user is None:
        return ("fresh", None)

    body = _event_text(last_user)

    # β-hook injected systemMessage marker.
    if ORCH_INJECT_MARKER in body:
        return ("orch_hook_inject", "dev_done")

    sender = _parse_teammate_sender(body)
    if sender:
        return ("teammate_reply", sender)

    # Structural fallback: when no [name]: prefix is present (the agent
    # prompts don't actually mandate one), look at the JSON tail's `phase`
    # field. Every teammate's output contract requires this field.
    sender = _parse_phase_from_json_tail(body)
    if sender:
        return ("teammate_reply", sender)

    # AskUserQuestion answers are tagged by the harness as a tool_result for
    # the AskUserQuestion tool. Best-effort detection.
    if last_user.get("toolUseResult") or last_user.get("tool_use_id"):
        # Tool result is from the user — usually means AskUser answered.
        return ("user_input", None)
    if "<ask_user_answer" in body.lower():
        return ("user_input", None)

    return ("fresh", None)


def read_last_user_message(transcript_path: str) -> Optional[Dict[str, Any]]:
    """Return a normalized dict for the latest user-role event."""
    events = _read_tail(Path(transcript_path), max_lines=50)
    last_user = _last_event(events, lambda e: _event_role(e) == "user")
    if last_user is None:
        return None
    body = _event_text(last_user)
    return {
        "role": "user",
        "body": body,
        "system_message_body": body,
        "is_ask_user_answer": (
            "<ask_user_answer" in body.lower()
            or bool(last_user.get("toolUseResult"))
        ),
    }


def read_last_task_result(transcript_path: str) -> Optional[str]:
    """Return the body of the most recent Task-tool result, or None.

    Used by the β Stop hook's optional Gate 3 (review approve signature).
    Missing data → None, and Gate 3 silently degrades.
    """
    events = _read_tail(Path(transcript_path), max_lines=50)
    for ev in reversed(events):
        tur = ev.get("toolUseResult") or ev.get("tool_use_result")
        if not tur:
            continue
        tool_name = ev.get("tool") or ev.get("toolName") or ""
        # Some harnesses include the name in toolUseResult.
        if isinstance(tur, dict):
            tool_name = tool_name or tur.get("tool") or tur.get("name") or ""
        if "task" not in str(tool_name).lower():
            continue
        if isinstance(tur, dict):
            for key in ("output", "stdout", "result", "content"):
                v = tur.get(key)
                if isinstance(v, str) and v:
                    return v
        if isinstance(tur, str):
            return tur
    return None


__all__ = [
    "infer",
    "read_last_user_message",
    "read_last_task_result",
    "ORCH_INJECT_MARKER",
    "TEAMMATE_NAMES",
    "PHASE_TO_SENDER",
]
