"""Smaller helpers used by the lead playbook.

These are factored out as Python rather than left as inline LLM prose so
they can be unit-tested and called via `python3 -c "import
playbook_helpers; …"` from the Bash tool.

Helpers included:
  - parse_json_tail(text)
  - extract_pr_url_from_text(text, repo)
  - format_recent_history(state, n=10)
  - parse_selected_candidate_ids(answers, candidates)
  - find_path_intersections(open_prs, touched_paths)
  - detect_lesson_pattern(failure_reason, state)
  - compose_daily_digest(state)
  - mark_as_epic_body(orig_body, child_urls)  (pure body builder)
  - clear_scout_fields(state)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

SPLIT_EPIC_MARKER = "<!-- split-epic-marker -->"


def parse_json_tail(text: str) -> Optional[Dict[str, Any]]:
    """Pull the last `{...}` block from the end of a teammate reply.

    Returns None if no parseable JSON is present.
    """
    if not text:
        return None
    # Try the very last non-empty line first (the common contract).
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1].strip()
    try:
        return json.loads(last)
    except json.JSONDecodeError:
        pass
    # Fallback: scan backward for a balanced `{...}` block.
    last_open = text.rfind("{")
    while last_open != -1:
        snippet = text[last_open:]
        # Try to find a balanced close.
        depth = 0
        end = -1
        in_str = False
        esc = False
        for i, ch in enumerate(snippet):
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end != -1:
            candidate = snippet[: end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        last_open = text.rfind("{", 0, last_open)
    return None


_PR_URL_RX = re.compile(
    r"https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)/pull/(?P<num>\d+)"
)


def extract_pr_url_from_text(text: str, repo: Optional[str] = None) -> Optional[str]:
    """Find the most recent matching PR URL in `text`.

    If `repo` (form "owner/repo") is given, only URLs that match the repo
    are accepted — this prevents picking up cross-repo cross-references.
    """
    if not text:
        return None
    last: Optional[str] = None
    want_owner: Optional[str] = None
    want_repo: Optional[str] = None
    if repo and "/" in repo:
        want_owner, want_repo = repo.split("/", 1)
    for m in _PR_URL_RX.finditer(text):
        owner = m.group("owner")
        rname = m.group("repo")
        if want_owner and (owner != want_owner or rname != want_repo):
            continue
        last = m.group(0)
    return last


def format_recent_history(state: Dict[str, Any], n: int = 10) -> str:
    """Format the last n processed issues as markdown for scout context."""
    issues = state.get("issues") or {}
    rows: List[Tuple[str, Dict[str, Any]]] = []
    for key, issue in issues.items():
        if not issue.get("history"):
            continue
        last_at = issue["history"][-1].get("at")
        rows.append((last_at or "", issue))
    rows.sort(key=lambda r: r[0], reverse=True)
    lines = ["## 최근 처리 이슈 (참고용)"]
    for _, issue in rows[:n]:
        num = issue.get("number")
        title = issue.get("title") or issue.get("analysis") or ""
        status = issue.get("status") or "?"
        title = title[:80]
        lines.append(f"- #{num} [{status}] {title}")
    if len(lines) == 1:
        lines.append("- (history empty)")
    return "\n".join(lines)


def parse_selected_candidate_ids(
    answer: Any, candidates: List[Dict[str, Any]]
) -> List[str]:
    """Map an AskUserQuestion multiSelect answer back to candidate ids.

    `answer` may be a list of labels, a dict {label: True/False}, or a
    single string. `candidates` each have `id`, `title`, `complexity_level`,
    `priority_hint` (label is "<title> (<complexity>, <priority>)").
    """
    if not candidates:
        return []
    by_label: Dict[str, str] = {}
    by_title: Dict[str, str] = {}
    by_id: Dict[str, str] = {}
    for c in candidates:
        cid = str(c.get("id") or "")
        if not cid:
            continue
        by_id[cid] = cid
        title = c.get("title") or ""
        by_title[title] = cid
        # Match the playbook's option label format:
        # "{title} ({complexity_level}, {priority_hint})"
        complexity = c.get("complexity_level", "")
        priority = c.get("priority_hint", "")
        label = f"{title} ({complexity}, {priority})"
        by_label[label] = cid
        # split_creating uses a slightly different label:
        # "{title} (level {complexity_level})"
        alt = f"{title} (level {complexity})"
        by_label[alt] = cid

    if isinstance(answer, dict):
        return [
            by_id.get(k) or by_label.get(k) or by_title.get(k)
            for k, v in answer.items()
            if v and (k in by_id or k in by_label or k in by_title)
        ]
    if isinstance(answer, list):
        return [
            cid
            for entry in answer
            if (cid := by_id.get(entry) or by_label.get(entry) or by_title.get(entry))
        ]
    if isinstance(answer, str):
        cid = by_id.get(answer) or by_label.get(answer) or by_title.get(answer)
        return [cid] if cid else []
    return []


def find_path_intersections(
    open_prs: Iterable[Dict[str, Any]], touched_paths: List[str]
) -> List[int]:
    """Return PR numbers whose files intersect with touched_paths."""
    if not touched_paths:
        return []
    want = set(touched_paths)
    conflicts: List[int] = []
    for pr in open_prs:
        if not isinstance(pr, dict):
            continue
        files = pr.get("files") or []
        # gh returns files as [{path:"..."}, ...] or a list of strings depending
        # on the flag — accept both.
        for f in files:
            if isinstance(f, dict):
                p = f.get("path") or ""
            else:
                p = str(f)
            if p in want:
                conflicts.append(int(pr.get("number") or 0))
                break
    return [n for n in conflicts if n]


def detect_lesson_pattern(
    failure_reason: str, state: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Update state.lessons_learned in place; return the matched/created entry.

    A "lesson" is one of a handful of canned patterns. We don't try to
    cluster arbitrary text — we just count occurrences of known reasons
    so the analyzer/tester prompts can be informed about repeat issues.
    """
    if not failure_reason or not isinstance(failure_reason, str):
        return None
    text = failure_reason.lower()
    patterns: List[Tuple[str, str, str]] = [
        # (substring, canonical pattern, suggested resolution)
        (
            "loopd session 미생성",
            "/dev-task 시작 후 loopd session 파일 미생성",
            "PYTHONPATH/CLAUDE_PLUGIN_ROOT 점검, loopd 플러그인 활성화 확인",
        ),
        (
            "tester 20분 무응답",
            "tester 무응답",
            "tester teammate 재spawn 또는 SendMessage 재시도 빈도 점검",
        ),
        (
            "analyzer 10분 무응답",
            "analyzer 무응답",
            "analyzer teammate 재spawn 또는 prompt 단순화",
        ),
        (
            "pr url 추출 실패",
            "PR URL 추출 실패",
            "loopd dev-task의 branch naming + transcript 패턴 점검",
        ),
        (
            "dev rework 2회",
            "tester가 PR 2회 연속 거부",
            "criteria 명확화, dev_task_prompt 보강, 사용자 컨텍스트 추가",
        ),
        (
            "gh pr merge 실패",
            "auto-merge 실패",
            "branch protection / required checks / required reviews 확인",
        ),
        (
            "split sub-issue 0건",
            "split 결과 0개",
            "analyzer가 force_split 시 atomic 판정 — 사용자 확인 필요",
        ),
    ]
    matched_canonical: Optional[str] = None
    resolution: Optional[str] = None
    for needle, canonical, fix in patterns:
        if needle in text:
            matched_canonical = canonical
            resolution = fix
            break
    if matched_canonical is None:
        return None
    lessons = state.setdefault("lessons_learned", [])
    for entry in lessons:
        if entry.get("pattern") == matched_canonical:
            entry["observed_count"] = (entry.get("observed_count") or 0) + 1
            entry["last_at"] = _dt.datetime.now(_dt.timezone.utc).isoformat()
            return entry
    new_entry = {
        "pattern": matched_canonical,
        "observed_count": 1,
        "resolution": resolution,
        "first_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "last_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    lessons.append(new_entry)
    return new_entry


def compose_daily_digest(state: Dict[str, Any]) -> str:
    """Build the markdown digest emitted by Step −1."""
    completed = state.get("completed_count") or 0
    rejected = state.get("rejected_count") or 0
    scout_history = state.get("scout_history") or []
    last_scout_created = sum(len(h.get("created_urls") or []) for h in scout_history[-3:])
    in_flight: Dict[str, int] = {}
    for issue in (state.get("issues") or {}).values():
        s = issue.get("status") or "?"
        if s in ("done", "needs_human", "skipped_by_human", "rejected", "done_final", "reverted"):
            continue
        in_flight[s] = in_flight.get(s, 0) + 1
    attention: List[str] = []
    now = _dt.datetime.now(_dt.timezone.utc)
    for issue in (state.get("issues") or {}).values():
        if issue.get("status") == "parked_awaiting_human":
            attention.append(f"#{issue.get('number')} (parked)")
        if issue.get("status") == "regression_detected":
            attention.append(f"#{issue.get('number')} (regression)")
    if state.get("main_branch_red"):
        attention.append("main 브랜치 CI red")
    lines = [
        "## 📊 Daily digest",
        f"- 총 처리(done): {completed}",
        f"- 거부(rejected): {rejected}",
        f"- 최근 3회 scout 등록 누계: {last_scout_created}",
        "",
        "### In-flight",
    ]
    if in_flight:
        for status, count in sorted(in_flight.items()):
            lines.append(f"- {status}: {count}")
    else:
        lines.append("- (idle)")
    if attention:
        lines.append("")
        lines.append("### 주의 필요")
        for note in attention:
            lines.append(f"- {note}")
    return "\n".join(lines)


def mark_as_epic_body(orig_body: str, child_urls: List[str]) -> str:
    """Build the new body for an epic, idempotently.

    If `orig_body` already contains SPLIT_EPIC_MARKER, returns it
    unchanged. Otherwise appends the marker + a child-link section.
    """
    if SPLIT_EPIC_MARKER in (orig_body or ""):
        return orig_body
    if not child_urls:
        raise ValueError("mark_as_epic_body: child_urls must be non-empty")
    suffix_lines = [
        "",
        SPLIT_EPIC_MARKER,
        f"## Split into sub-issues ({len(child_urls)}개)",
    ]
    for url in child_urls:
        suffix_lines.append(f"- {url}")
    return (orig_body or "") + "\n".join(suffix_lines) + "\n"


SCOUT_FIELDS_TO_CLEAR = (
    "scout_candidates",
    "scout_decisions",
    "scout_creating_done",
    "scout_creating_lock_started_at",
    "scout_creating_lock_owner",
    "scout_clarify_question",
    "scout_confirm_started_at",
    "scout_question_emitted",
    "scout_confirm_idx",
    "scout_message",
    "scout_created_urls",
    "scout_failed_creations",
)


def clear_scout_fields(state: Dict[str, Any]) -> None:
    """Reset transient scout fields between cycles. Keeps scout_history."""
    for field in SCOUT_FIELDS_TO_CLEAR:
        if field in state:
            if isinstance(state[field], list):
                state[field] = []
            elif isinstance(state[field], dict):
                state[field] = {}
            elif isinstance(state[field], bool):
                state[field] = False
            elif isinstance(state[field], int):
                state[field] = 0
            else:
                state[field] = None


__all__ = [
    "SPLIT_EPIC_MARKER",
    "parse_json_tail",
    "extract_pr_url_from_text",
    "format_recent_history",
    "parse_selected_candidate_ids",
    "find_path_intersections",
    "detect_lesson_pattern",
    "compose_daily_digest",
    "mark_as_epic_body",
    "clear_scout_fields",
]
