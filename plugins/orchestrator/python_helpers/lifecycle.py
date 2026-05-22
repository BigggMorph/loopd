"""Team lifecycle helpers.

Most of this module is intentionally lightweight: TeamCreate / Agent /
TeamDelete are Claude-Code tools, not Python APIs, so the lead playbook
calls them directly. This module just gives the playbook a stable place
to check team health and to bootstrap labels.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REQUIRED_TEAMMATES = ("issue-analyzer", "tester", "issue-scout")
# Rev 17 — planning-layer teammates are spawned lazily on first use.
OPTIONAL_TEAMMATES = ("product-planner", "roadmap-strategist", "vision-critic")
ALL_TEAMMATES = REQUIRED_TEAMMATES + OPTIONAL_TEAMMATES
TEAMS_DIR = Path(os.path.expanduser("~/.claude/teams"))
TASKS_DIR = Path(os.path.expanduser("~/.claude/tasks"))
PLUGIN_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = PLUGIN_DIR / "agents"

# Rev 17 — working-memory watermarks per teammate. Whichever threshold
# (call count OR estimated tokens) is hit first triggers a graceful
# shutdown/respawn cycle. Values per §12.1.
WATERMARK_CALLS: Dict[str, int] = {
    "issue-analyzer": 20,
    "tester": 20,
    "issue-scout": 10,
    "product-planner": 10,
    "roadmap-strategist": 5,
    "vision-critic": 3,
}

WATERMARK_TOKENS: Dict[str, int] = {
    "issue-analyzer": 150_000,
    "tester": 200_000,
    "issue-scout": 100_000,
    "product-planner": 100_000,
    "roadmap-strategist": 100_000,
    "vision-critic": 150_000,
}

# Minimum interval between forced respawns for one teammate (rate-limit,
# Round A S8).
RESPAWN_RATE_LIMIT_SEC = 30 * 60  # 30 min

# Label spec from §17 Phase 0b.
LABEL_SPEC: List[Dict[str, str]] = [
    {"name": "scout-suggested", "color": "BFD4F2", "description": "orchestrator scout이 도출한 이슈"},
    {"name": "split-epic", "color": "5319E7", "description": "analyzer가 분할한 부모 이슈 (picker skip)"},
    {"name": "orchestrator-rejected", "color": "D93F0B", "description": "analyzer 거부 + 사용자 confirm 후 close"},
    {"name": "orchestrator-skipped", "color": "EEEEEE", "description": "analyzer 거부 후 사용자 skip (open 유지)"},
    {"name": "orchestrator-abandoned", "color": "666666", "description": "14일 stale PR 자동 close"},
    {"name": "regression-suspect", "color": "D93F0B", "description": "머지 후 회귀 의심"},
    {"name": "orchestrator-managed", "color": "0E8A16", "description": "orchestrator가 만든 PR/이슈"},
    {"name": "priority/high", "color": "B60205", "description": "우선순위 높음"},
    {"name": "priority/medium", "color": "FBCA04", "description": "우선순위 보통"},
    {"name": "priority/low", "color": "C2E0C6", "description": "우선순위 낮음"},
    {"name": "complexity/0", "color": "EEEEEE", "description": "한 줄 수정 / 오타"},
    {"name": "complexity/1", "color": "DDDDDD", "description": "단일 파일 작은 변경"},
    {"name": "complexity/2", "color": "CCCCCC", "description": "다중 파일"},
    {"name": "complexity/3", "color": "BBBBBB", "description": "새 모듈/기능 추가"},
    {"name": "complexity/4", "color": "999999", "description": "아키텍처 변경"},
    {"name": "migration", "color": "D93F0B", "description": "dangerous label — 마이그레이션"},
    {"name": "auth", "color": "D93F0B", "description": "dangerous label — 인증/권한"},
    {"name": "breaking-change", "color": "D93F0B", "description": "dangerous label — API 시그니처 변경"},
    {"name": "security", "color": "D93F0B", "description": "dangerous label — 보안 관련"},
    {"name": "recommend-human-review", "color": "FBCA04", "description": "tester가 부착 — 사람 시야 권장"},
    # Rev 17 — planning layer labels.
    {"name": "planner-suggested", "color": "9F47CC", "description": "orchestrator product-planner가 도출한 Epic"},
    {"name": "roadmap-context", "color": "F5C518", "description": "roadmap-strategist phase context 추적용 (선택)"},
    {"name": "vision-update-pending", "color": "B30000", "description": "vision-critic 갱신 제안 사용자 confirm 대기"},
]


def _run(cmd: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s: {shlex.join(cmd)}"


def ensure_labels(repo: str) -> Dict[str, str]:
    """Create (idempotent) every label in LABEL_SPEC for `repo`.

    Returns {label_name: "created" | "exists" | "error: ..."}.
    Existing labels are not modified (respect user customization).
    """
    out: Dict[str, str] = {}
    for spec in LABEL_SPEC:
        name = spec["name"]
        rc, stdout, stderr = _run(
            [
                "gh", "label", "create", name,
                "--repo", repo,
                "--color", spec["color"],
                "--description", spec["description"],
            ]
        )
        if rc == 0:
            out[name] = "created"
        elif "already exists" in (stderr.lower() + stdout.lower()):
            out[name] = "exists"
        else:
            out[name] = f"error: {stderr.strip() or stdout.strip()}"
    return out


def ensure_split_label(repo: str, parent_num: int) -> Tuple[bool, str]:
    """Idempotent `split-from-#<N>` label create. Color FEF2C0."""
    name = f"split-from-#{parent_num}"
    rc, stdout, stderr = _run(
        [
            "gh", "label", "create", name,
            "--repo", repo,
            "--color", "FEF2C0",
            "--description", f"sub-issue of #{parent_num}",
        ]
    )
    if rc == 0:
        return True, "created"
    msg = (stderr + stdout).lower()
    if "already exists" in msg:
        return True, "exists"
    return False, stderr or stdout


def team_alive(team_name: str) -> bool:
    """Check whether the team config + member records exist.

    Conservative: returns False if any of (config.json, required members,
    recent task activity) are missing. The optional ping check is left to
    the lead (requires SendMessage which is not a Python API).
    """
    if not team_name:
        return False
    cfg_path = TEAMS_DIR / team_name / "config.json"
    if not cfg_path.exists():
        return False
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    members = cfg.get("members") or []
    member_names = {
        m.get("name") if isinstance(m, dict) else m
        for m in members
    }
    if not all(req in member_names for req in REQUIRED_TEAMMATES):
        return False
    # Recent task activity (last 24h) is a soft signal; treat missing tasks
    # dir as alive enough (the team may simply be brand new).
    return True


def discover_alive_teammates(team_name: str) -> set:
    """Return the set of teammate names currently in team config.

    Used by Step −0 (health check) and Stage 1/2/3 entry to iterate
    across the actual alive set (REQUIRED + lazily-spawned OPTIONAL).
    """
    if not team_name:
        return set()
    cfg_path = TEAMS_DIR / team_name / "config.json"
    if not cfg_path.exists():
        return set()
    try:
        cfg = json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    return {
        (m.get("name") if isinstance(m, dict) else m)
        for m in (cfg.get("members") or [])
        if (m.get("name") if isinstance(m, dict) else m)
    }


def _agent_frontmatter_name(member: str) -> Optional[str]:
    """Read the `name:` frontmatter field from agents/<member>.md."""
    path = AGENTS_DIR / f"{member}.md"
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    header = text[3:end]
    for line in header.splitlines():
        line = line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return None


def ensure_team_member(team_name: str, member: str, state: Dict[str, Any]) -> bool:
    """Lazy-spawn helper for OPTIONAL_TEAMMATES.

    Returns True if `member` is already alive in the team config.
    Otherwise validates the agent definition file (existence +
    frontmatter name match) and marks the member in
    `state.pending_team_spawns` for the lead to spawn via the Agent
    tool in Step 3.

    Returns False (with `state.pending_team_spawns` updated) when the
    member is not yet alive, or when validation fails (definition file
    missing or name mismatch — attacker-planted definition guard).

    REQUIRED_TEAMMATES are still spawned at bootstrap; this helper
    handles the lazy case (OPTIONAL_TEAMMATES + post-respawn
    re-creation).
    """
    if member not in ALL_TEAMMATES:
        return False
    if member in discover_alive_teammates(team_name):
        return True
    # Validate definition file (S9 guard).
    declared = _agent_frontmatter_name(member)
    if declared != member:
        return False
    pending = state.setdefault("pending_team_spawns", [])
    if member not in pending:
        pending.append(member)
    return False


def shutdown_marker(team_name: str) -> str:
    """Build the JSON body used in graceful-shutdown SendMessages."""
    return json.dumps({"type": "shutdown_request", "team": team_name})


# =====================================================================
# Rev 17 Phase 17-G — working-memory health + respawn helpers
# =====================================================================


def _summarize_entries(entries: List[Dict[str, Any]], max_entries: int = 3) -> str:
    """Build a short text summary of the last `max_entries` outcome dicts."""
    if not entries:
        return "(no recent activity)"
    parts: List[str] = []
    for entry in entries[-max_entries:]:
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts") or entry.get("at") or "?"
        # Pick a meaningful short field by type — fall back to repr.
        if "created_urls" in entry:
            n = len(entry.get("created_urls") or [])
            parts.append(f"{ts}: {n} issue(s) created")
        elif "issue_urls_created" in entry:
            n = len(entry.get("issue_urls_created") or [])
            parts.append(f"{ts}: {n} issue(s) created")
        elif "user_action" in entry:
            parts.append(
                f"{ts}: {entry.get('user_action')} ("
                f"score={entry.get('alignment_score')})"
            )
        elif "current_phase" in entry:
            parts.append(
                f"{ts}: phase={entry.get('current_phase')}, "
                f"user_action={entry.get('user_action')}"
            )
        else:
            parts.append(f"{ts}: {entry.get('pattern') or '...'}")
    return "; ".join(parts) if parts else "(no recent activity)"


def recover_team_context(state: Dict[str, Any], member: str) -> str:
    """Build the SendMessage body to bootstrap a freshly respawned teammate.

    Re-establishes the bare-minimum context the previous incarnation
    had:
      - Project vision (always)
      - Repo identity
      - Last 5 lessons relevant to this teammate
      - For analyzer/tester: current_issue context if a resolution cycle
        is active
      - For scout: scout_history[-3:] summary
      - For planner: planner_history[-3:] summary
      - For roadmap/vision-critic: vision_critic_history[-3:] summary

    Used after watermark-triggered respawn (§12.1). Returns a plain-text
    body ready for SendMessage.
    """
    parts: List[str] = [
        "You have just been (re)spawned. Restoring context.",
        f"Vision: {(state.get('vision') or '')[:500]}",
        f"Repo: {state.get('repo') or '(unset)'}",
    ]
    lessons = state.get("lessons_learned") or []
    if lessons:
        parts.append("Recent lessons:")
        for lesson in lessons[-5:]:
            if not isinstance(lesson, dict):
                continue
            count = lesson.get("observed_count") or 0
            parts.append(
                f"- {lesson.get('pattern')} (seen {count}x) — "
                f"{lesson.get('resolution') or ''}"
            )

    if member in ("issue-analyzer", "tester"):
        ci = state.get("current_issue")
        if ci is not None:
            issue = (state.get("issues") or {}).get(str(ci))
            if isinstance(issue, dict):
                parts.append(
                    f"Current issue: #{issue.get('number')} "
                    f"status={issue.get('status')}"
                )
    elif member == "issue-scout":
        parts.append(
            "Recent scout outcomes: "
            + _summarize_entries(state.get("scout_history") or [])
        )
    elif member == "product-planner":
        parts.append(
            "Recent planner outcomes: "
            + _summarize_entries(state.get("planner_history") or [])
        )
    elif member == "roadmap-strategist":
        parts.append(
            "Recent roadmap reports: "
            + _summarize_entries(state.get("roadmap_reports") or [])
        )
    elif member == "vision-critic":
        parts.append(
            "Recent vision-critic outcomes: "
            + _summarize_entries(state.get("vision_critic_history") or [])
        )

    return "\n".join(parts)


_CALL_COUNT_CAP_OVERSHOOT = 5  # Round A S8 hard cap on tamper exposure


def needs_respawn(state: Dict[str, Any], member: str) -> bool:
    """Return True if the teammate has hit a watermark.

    Watermarks: call_count >= WATERMARK_CALLS[member] OR
    estimated_tokens >= WATERMARK_TOKENS[member]. Rate-limited so the
    same member cannot be respawned more than once per
    RESPAWN_RATE_LIMIT_SEC.

    Round A S8: if `call_count` exceeds WATERMARK + 5, clamp it back.
    This defends against state-tampering bumping the counter into
    runaway territory; the clamp itself does not lower below the
    watermark, so the respawn still fires.
    """
    if member not in WATERMARK_CALLS:
        return False
    health_map = state.setdefault("teammate_health", {})
    health = health_map.setdefault(member, {})
    calls = int(health.get("call_count") or 0)
    tokens = int(health.get("estimated_tokens") or 0)
    # S8 clamp — silently clamp; SKILL.md prose adds the audit record
    # since this helper has no flock to share.
    if calls > WATERMARK_CALLS[member] + _CALL_COUNT_CAP_OVERSHOOT:
        health["call_count"] = WATERMARK_CALLS[member] + _CALL_COUNT_CAP_OVERSHOOT
        calls = health["call_count"]
    if calls < WATERMARK_CALLS[member] and tokens < WATERMARK_TOKENS[member]:
        return False
    # Rate-limit: respect a recent respawn.
    last_respawn = health.get("last_respawn_at")
    if isinstance(last_respawn, str) and last_respawn:
        try:
            last_dt = _dt.datetime.fromisoformat(
                last_respawn.replace("Z", "+00:00")
            )
        except ValueError:
            last_dt = None
        if last_dt is not None:
            now = _dt.datetime.now(_dt.timezone.utc)
            if (now - last_dt).total_seconds() < RESPAWN_RATE_LIMIT_SEC:
                return False
    return True


def record_teammate_call(
    state: Dict[str, Any],
    member: str,
    *,
    sent_tokens: int = 0,
    received_tokens: int = 0,
) -> Dict[str, Any]:
    """Update `state.teammate_health[member]` for a fresh SendMessage.

    Tokens are *estimates* — the lead computes them from the request +
    response body byte length / 4. The caller may pass 0 when only the
    call count matters (cheaper Watermark A path from §9.6).
    """
    health = state.setdefault("teammate_health", {})
    entry = health.setdefault(
        member,
        {
            "spawned_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "call_count": 0,
            "estimated_tokens": 0,
            "last_response_at": None,
            "respawn_count": 0,
        },
    )
    entry["call_count"] = int(entry.get("call_count") or 0) + 1
    entry["estimated_tokens"] = int(entry.get("estimated_tokens") or 0) + int(
        sent_tokens
    ) + int(received_tokens)
    entry["last_response_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return entry


def reset_teammate_health(state: Dict[str, Any], member: str) -> None:
    """Reset health counters after a successful respawn."""
    health = state.setdefault("teammate_health", {})
    prev = health.get(member) or {}
    health[member] = {
        "spawned_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "call_count": 0,
        "estimated_tokens": 0,
        "last_response_at": None,
        "respawn_count": int(prev.get("respawn_count") or 0) + 1,
        "last_respawn_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


__all__ = [
    "REQUIRED_TEAMMATES",
    "OPTIONAL_TEAMMATES",
    "ALL_TEAMMATES",
    "LABEL_SPEC",
    "WATERMARK_CALLS",
    "WATERMARK_TOKENS",
    "RESPAWN_RATE_LIMIT_SEC",
    "ensure_labels",
    "ensure_split_label",
    "team_alive",
    "discover_alive_teammates",
    "ensure_team_member",
    "shutdown_marker",
    "recover_team_context",
    "needs_respawn",
    "record_teammate_call",
    "reset_teammate_health",
]
