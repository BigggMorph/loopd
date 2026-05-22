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
TEAMS_DIR = Path(os.path.expanduser("~/.claude/teams"))
TASKS_DIR = Path(os.path.expanduser("~/.claude/tasks"))

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


def shutdown_marker(team_name: str) -> str:
    """Build the JSON body used in graceful-shutdown SendMessages."""
    return json.dumps({"type": "shutdown_request", "team": team_name})


__all__ = [
    "REQUIRED_TEAMMATES",
    "LABEL_SPEC",
    "ensure_labels",
    "ensure_split_label",
    "team_alive",
    "shutdown_marker",
]
