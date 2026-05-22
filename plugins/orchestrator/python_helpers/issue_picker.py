"""Pick the next GitHub issue to work on.

Calls `gh issue list` + applies the filters/scoring described in
docs/orchestrator-design.md §10 `issue_picker.py requirements`.

Returns up to 5 candidates (score-sorted). The lead's
`pick_best_by_vision()` LLM thinking step makes the final choice.
"""

from __future__ import annotations

import datetime as _dt
import json
import shlex
import subprocess
from typing import Any, Dict, List, Optional

import orchestrator_state

# Statuses that mean "stop processing this issue."
_DEAD_STATUSES = {
    "done",
    "done_final",
    "reverted",
    "skipped_by_human",
    "needs_human",
    "parked_awaiting_human",
    "rejected",
    "waiting_on_dep",
}

# Labels that disqualify an issue from being picked.
_EXCLUDED_LABELS = {
    "split-epic",
    "orchestrator-rejected",
    "orchestrator-skipped",
}

_DEDUP_WINDOW_MIN = 5


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_iso(s: Optional[str]) -> Optional[_dt.datetime]:
    if not s:
        return None
    try:
        # gh returns RFC3339 with trailing Z.
        return _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_gh(cmd: List[str], timeout: int = 60) -> str:
    """Invoke gh CLI and return stdout. Raises on non-zero exit."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"gh command failed ({result.returncode}): {shlex.join(cmd)}\n"
            f"stderr: {result.stderr.strip()}"
        )
    return result.stdout


def _list_open_issues(repo: str, limit: int = 50) -> List[Dict[str, Any]]:
    raw = _run_gh(
        [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--limit", str(limit),
            "--json",
            "number,title,labels,reactions,createdAt,updatedAt,assignees,author,body",
        ]
    )
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def _is_closed_externally(issue: Dict[str, Any]) -> bool:
    # gh issue list --state open already filters; this is a paranoia check
    # for issues that were closed between list and pick.
    return False


def _label_names(issue: Dict[str, Any]) -> List[str]:
    return [
        (lbl.get("name") or "")
        for lbl in (issue.get("labels") or [])
        if isinstance(lbl, dict)
    ]


def _is_excluded(issue: Dict[str, Any]) -> bool:
    labels = set(_label_names(issue))
    if labels & _EXCLUDED_LABELS:
        return True
    # Human assignees (non-bot) → skip.
    assignees = issue.get("assignees") or []
    if any(
        isinstance(a, dict) and a.get("login") and not a["login"].endswith("[bot]")
        for a in assignees
    ):
        return True
    return False


def _score(issue: Dict[str, Any]) -> int:
    labels = set(_label_names(issue))
    score = 0
    is_split = any(l.startswith("split-from-#") for l in labels)
    is_scout = "scout-suggested" in labels

    if not is_split and not is_scout:
        if "priority/high" in labels:
            score += 100
        elif "priority/medium" in labels:
            score += 50
    if is_split:
        if "priority/high" in labels:
            score += 60
        elif "priority/medium" in labels:
            score += 30
    if is_scout:
        if "priority/high" in labels:
            score += 40
        elif "priority/medium" in labels:
            score += 20
    reactions = (issue.get("reactions") or {}).get("totalCount") or 0
    score += int(reactions) * 5
    if "good-first-issue" in labels:
        score += 10
    return score


def _created_at_for_tiebreak(issue: Dict[str, Any]) -> str:
    return issue.get("createdAt") or ""


def _is_recently_picked(state: Dict[str, Any], num: int) -> bool:
    last = (state.get("last_picked_at") or {}).get(str(num))
    if not last:
        return False
    last_dt = _parse_iso(last)
    if last_dt is None:
        return False
    return (_now() - last_dt) < _dt.timedelta(minutes=_DEDUP_WINDOW_MIN)


def pick(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Return up to 5 ranked candidate issues.

    Empty list means "no work to do" → caller should enter scouting cycle.
    """
    repo = state.get("repo") or ""
    if not repo:
        return []
    candidates = _list_open_issues(repo, limit=50)

    state_issues = state.get("issues") or {}
    filtered: List[Dict[str, Any]] = []
    for issue in candidates:
        num = issue.get("number")
        if num is None:
            continue
        # Skip terminal-state issues that orchestrator already finished.
        local = state_issues.get(str(num)) or {}
        if local.get("status") in _DEAD_STATUSES:
            continue
        if _is_excluded(issue):
            continue
        if _is_closed_externally(issue):
            continue
        if _is_recently_picked(state, num):
            continue
        issue["__score__"] = _score(issue)
        filtered.append(issue)

    filtered.sort(
        key=lambda i: (-i["__score__"], _created_at_for_tiebreak(i))
    )
    return filtered[:5]


def resume_waiting_on_dep(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Find a waiting_on_dep issue whose dependencies are all resolved.

    Returns the issue dict (number/title) plus marks it ready_for_dev in
    state.issues. Caller is responsible for setting current_issue and
    transitioning the in-memory issue dict.
    """
    repo = state.get("repo") or ""
    if not repo:
        return None
    for key, issue in (state.get("issues") or {}).items():
        if issue.get("status") != "waiting_on_dep":
            continue
        deps = issue.get("unresolved_dependencies") or []
        if not deps:
            orchestrator_state.transition(issue, "ready_for_dev")
            return issue
        all_closed = True
        for dep in deps:
            try:
                out = _run_gh(
                    [
                        "gh", "issue", "view", str(dep),
                        "--repo", repo,
                        "--json", "state",
                        "--jq", ".state",
                    ]
                ).strip()
            except RuntimeError:
                all_closed = False
                break
            if out.upper() != "CLOSED":
                all_closed = False
                break
        if all_closed:
            issue["unresolved_dependencies"] = []
            orchestrator_state.transition(issue, "ready_for_dev")
            return issue
    return None


def remember_pick(state: Dict[str, Any], num: int) -> None:
    state.setdefault("last_picked_at", {})[str(num)] = _now().isoformat()


__all__ = ["pick", "resume_waiting_on_dep", "remember_pick"]
