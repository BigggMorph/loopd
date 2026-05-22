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
  - dedup_candidates(scout_list, planner_list)  (Rev 17)
  - candidate_create_plan(candidate, repo, fp_prefix, extra_labels)  (Rev 17)
  - clear_planner_fields(state)  (Rev 17)
"""

from __future__ import annotations

import datetime as _dt
import difflib
import hashlib
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
    # Rev 17 — Planning-layer counters (§7.2 digest extension).
    planner_hist = state.get("planner_history") or []
    if planner_hist:
        last3 = planner_hist[-3:]
        proposed = sum(int(h.get("candidates_proposed") or 0) for h in last3)
        accepted = sum(int(h.get("candidates_accepted") or 0) for h in last3)
        lines.append("")
        lines.append("### Planner (Rev 17)")
        lines.append(f"- 최근 3회 제안: {proposed} / 채택: {accepted}")
    roadmap_reports = state.get("roadmap_reports") or []
    if roadmap_reports:
        accepted_ctx = sum(1 for r in roadmap_reports if isinstance(r, dict)
                           and r.get("user_action") == "accepted")
        total = len(roadmap_reports)
        active = state.get("active_phase_context")
        lines.append("")
        lines.append("### Roadmap (Rev 17)")
        lines.append(f"- 보고 누계: {total}, 채택: {accepted_ctx}")
        if active:
            lines.append(f"- active phase context: {active[:80]}")
    vch = state.get("vision_critic_history") or []
    if vch:
        accepted = sum(1 for e in vch if isinstance(e, dict)
                       and e.get("user_action") == "accepted")
        rejected = sum(1 for e in vch if isinstance(e, dict)
                       and e.get("user_action") == "rejected")
        skipped = sum(1 for e in vch if isinstance(e, dict)
                      and e.get("user_action") == "auto_skipped")
        lines.append("")
        lines.append("### Vision-critic (Rev 17)")
        lines.append(
            f"- 누계: {len(vch)} (accept={accepted}, reject={rejected}, auto_skip={skipped})"
        )
        if state.get("vision_critic_pending_delta"):
            lines.append("- ⚠ pending vision delta 사용자 확인 대기")
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


# =====================================================================
# Rev 17 — Stage 1 (scout + planner) integration helpers
# =====================================================================

_DEDUP_TITLE_SIMILARITY = 0.85
_DEDUP_BODY_SIMILARITY = 0.80
DEDUP_METHOD_DEFAULT = "auto"
_SBERT_CACHE: Dict[str, Any] = {}


def _normalize_title(text: str) -> str:
    """Lightweight title canonicalization for dedup."""
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _sequencematcher_ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def _sbert_model():
    """Lazy-load the sentence-transformers model, cached on first use.

    Returns the model object or None if sentence-transformers is not
    installed. Callers should fall back to SequenceMatcher if None.
    """
    if "model" in _SBERT_CACHE:
        return _SBERT_CACHE["model"]
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        _SBERT_CACHE["model"] = None
        return None
    name = os.environ.get(
        "ORCHESTRATOR_DEDUP_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )
    try:
        _SBERT_CACHE["model"] = SentenceTransformer(name)
    except Exception:
        _SBERT_CACHE["model"] = None
    return _SBERT_CACHE["model"]


def _sbert_similarity(a: str, b: str) -> Optional[float]:
    """Cosine similarity via sentence-transformers. None if unavailable."""
    model = _sbert_model()
    if model is None:
        return None
    try:
        embeddings = model.encode([a, b], convert_to_tensor=False)
    except Exception:
        return None
    try:
        import numpy as _np  # type: ignore
        v1, v2 = embeddings[0], embeddings[1]
        denom = float(_np.linalg.norm(v1) * _np.linalg.norm(v2))
        if denom == 0:
            return 0.0
        return float(_np.dot(v1, v2) / denom)
    except Exception:
        return None


def _similarity(a: str, b: str, *, method: str) -> float:
    """Dispatch similarity by configured method.

    method:
      - "auto"             — try sentence-transformers, fallback to SequenceMatcher
      - "sentence"         — require sentence-transformers, else ImportError
      - "sequence_matcher" — always use SequenceMatcher (deterministic, no deps)
    """
    if not a or not b:
        return 0.0
    if method == "sequence_matcher":
        return _sequencematcher_ratio(a, b)
    if method == "sentence":
        score = _sbert_similarity(a, b)
        if score is None:
            raise ImportError(
                "dedup method='sentence' requested but sentence-transformers "
                "is not installed; install it or set "
                "ORCHESTRATOR_DEDUP_METHOD=sequence_matcher"
            )
        return score
    # "auto"
    score = _sbert_similarity(a, b)
    if score is not None:
        return score
    return _sequencematcher_ratio(a, b)


def _candidate_text(candidate: Dict[str, Any]) -> Tuple[str, str]:
    """Return (normalized_title, body_excerpt) for dedup."""
    title = _normalize_title(candidate.get("title") or "")
    body = candidate.get("body") or ""
    if isinstance(body, str):
        body_excerpt = body[:600].lower()
    else:
        body_excerpt = ""
    return title, body_excerpt


def dedup_candidates(
    scout_list: List[Dict[str, Any]],
    planner_list: List[Dict[str, Any]],
    *,
    method: Optional[str] = None,
    title_threshold: float = _DEDUP_TITLE_SIMILARITY,
    body_threshold: float = _DEDUP_BODY_SIMILARITY,
) -> Dict[str, Any]:
    """Merge scout + planner candidates into a deduplicated pool.

    When two candidates are similar (title sim >= title_threshold OR
    title sim >= 0.7 AND body sim >= body_threshold), the scout
    candidate (lower priority) is dropped in favor of the planner Epic.
    The planner candidate keeps its slot; the scout one is recorded in
    `drops` for the user-facing dedup summary.

    method (or env ORCHESTRATOR_DEDUP_METHOD, default "auto"):
      - "auto"             — sentence-transformers if available, else SequenceMatcher
      - "sentence"         — sentence-transformers required
      - "sequence_matcher" — SequenceMatcher only

    Returns: {"merged": [...combined candidates...], "drops": [{dropped_id, dropped_source, kept_id, kept_source, score}]}
    """
    chosen_method = (
        method
        or os.environ.get("ORCHESTRATOR_DEDUP_METHOD")
        or DEDUP_METHOD_DEFAULT
    )

    scout_clean = [c for c in (scout_list or []) if isinstance(c, dict)]
    planner_clean = [c for c in (planner_list or []) if isinstance(c, dict)]

    drops: List[Dict[str, Any]] = []
    dropped_scout_ids: Set[str] = set()

    for p in planner_clean:
        pt, pb = _candidate_text(p)
        for s in scout_clean:
            sid = str(s.get("id") or "")
            if not sid or sid in dropped_scout_ids:
                continue
            st, sb = _candidate_text(s)
            try:
                title_sim = _similarity(pt, st, method=chosen_method)
            except ImportError:
                raise
            body_sim = 0.0
            if title_sim >= 0.7:
                body_sim = _similarity(pb, sb, method=chosen_method)
            is_dup = title_sim >= title_threshold or (
                title_sim >= 0.7 and body_sim >= body_threshold
            )
            if is_dup:
                dropped_scout_ids.add(sid)
                drops.append(
                    {
                        "dropped_id": sid,
                        "dropped_source": "scout",
                        "dropped_title": s.get("title") or "",
                        "kept_id": str(p.get("id") or ""),
                        "kept_source": "planner",
                        "kept_title": p.get("title") or "",
                        "title_similarity": round(title_sim, 3),
                        "body_similarity": round(body_sim, 3),
                    }
                )

    merged: List[Dict[str, Any]] = []
    for s in scout_clean:
        sid = str(s.get("id") or "")
        if sid not in dropped_scout_ids:
            entry = dict(s)
            entry.setdefault("source", "scout")
            merged.append(entry)
    for p in planner_clean:
        entry = dict(p)
        entry.setdefault("source", "planner")
        merged.append(entry)

    return {
        "merged": merged,
        "drops": drops,
        "method_used": chosen_method,
    }


def candidate_create_plan(
    candidate: Dict[str, Any],
    *,
    fp_prefix: str = "scout-fp-",
    extra_labels: Optional[List[str]] = None,
    body_prefix: str = "",
    title_cap: int = 200,
    body_excerpt_chars: int = 200,
) -> Dict[str, Any]:
    """Build the inputs for `gh issue create` for one candidate.

    Pure function — no side effects. The lead is responsible for the
    actual `audited_bash gh issue create` call (Python helpers do not
    have audit hooks).

    Returns: {
      title, body, fingerprint, fingerprint_label,
      labels (deduped, ordered), source
    }
    """
    # Lazy import to avoid circular reference at module load.
    from safety import sanitize_scout_body, sanitize_title, fingerprint_label

    title_raw = candidate.get("title") or ""
    body_raw = candidate.get("body") or ""
    title = sanitize_title(title_raw)
    if len(title) > title_cap:
        title = title[:title_cap].rstrip()
    safe_body = sanitize_scout_body((body_prefix or "") + body_raw)

    fp_input = title + (body_raw or "")[:body_excerpt_chars]
    fp_label = fingerprint_label(fp_input, prefix=fp_prefix)
    fingerprint_hash = fp_label[len(fp_prefix):]

    candidate_labels = list(candidate.get("labels") or [])
    all_labels: List[str] = []
    for lbl in candidate_labels + list(extra_labels or []) + [fp_label]:
        if isinstance(lbl, str) and lbl and lbl not in all_labels:
            all_labels.append(lbl)
    return {
        "id": str(candidate.get("id") or ""),
        "title": title,
        "body": safe_body,
        "fingerprint_label": fp_label,
        "fingerprint": fingerprint_hash,
        "labels": all_labels,
        "source": candidate.get("source") or "scout",
    }


# =====================================================================
# Rev 17 — Vision-critic helpers (Phase 17-D)
# =====================================================================

# Tokens whose deletion in `delta.before → delta.after` indicates an
# attempt to weaken human oversight. Lead refuses such deltas.
_VISION_GUARD_TOKENS = (
    "human", "confirm", "approve", "audit", "review",
    "사람", "확인", "승인", "검토",
)

_VISION_DELTA_REJECT_TTL_DAYS = 30
_VISION_DELTA_REPEAT_THRESHOLD = 3
_VISION_DELTA_SIMILARITY = 0.85
_VISION_TWO_CALL_SIMILARITY = 0.7
_VISION_ALIGNMENT_SKIP_THRESHOLD = 0.8


def _norm_for_vision(text: str) -> str:
    """Local helper to avoid the safety→playbook_helpers circular import."""
    from safety import normalize_for_dedup
    return normalize_for_dedup(text)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def vision_delta_violates_guard(delta: Dict[str, Any]) -> List[str]:
    """Detect deletions of oversight tokens in a vision delta.

    Returns the list of guard tokens that exist in `delta.before` but
    are missing in `delta.after`. Empty list → safe to proceed.
    """
    before = (delta.get("before") or "").lower()
    after = (delta.get("after") or "").lower()
    removed: List[str] = []
    for token in _VISION_GUARD_TOKENS:
        if token in before and token not in after:
            removed.append(token)
    return removed


def find_rejected_delta_match(
    state: Dict[str, Any], before_text: str, after_text: str
) -> Optional[Dict[str, Any]]:
    """Find a prior rejected delta that matches the proposed one.

    Match criteria (any of):
      1. Exact hash match on `sha256(norm(before) + norm(after))`.
      2. SequenceMatcher ratio on normalized `before` >= 0.85.

    Returns the matching entry from `state.rejected_delta_hashes`, or
    None.
    """
    norm_before = _norm_for_vision(before_text)
    norm_after = _norm_for_vision(after_text)
    proposed_hash = hashlib.sha256(
        (norm_before + "|" + norm_after).encode("utf-8")
    ).hexdigest()
    for entry in state.get("rejected_delta_hashes") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("hash") == proposed_hash:
            return entry
        existing_before = entry.get("before_norm") or ""
        if existing_before:
            ratio = difflib.SequenceMatcher(
                None, norm_before, existing_before
            ).ratio()
            if ratio >= _VISION_DELTA_SIMILARITY:
                return entry
    return None


def record_rejected_delta(
    state: Dict[str, Any], before_text: str, after_text: str
) -> Dict[str, Any]:
    """Update state.rejected_delta_hashes for a freshly-rejected delta.

    If a matching entry exists (exact hash or fuzzy ratio), increment
    `rejection_count` and update `last_seen_at`. Otherwise append a new
    entry.

    Returns the entry (new or existing).
    """
    norm_before = _norm_for_vision(before_text)
    norm_after = _norm_for_vision(after_text)
    h = hashlib.sha256(
        (norm_before + "|" + norm_after).encode("utf-8")
    ).hexdigest()
    existing = find_rejected_delta_match(state, before_text, after_text)
    now_iso = _now_iso()
    if existing is not None:
        existing["last_seen_at"] = now_iso
        existing["rejection_count"] = (
            existing.get("rejection_count") or 0
        ) + 1
        return existing
    entry = {
        "hash": h,
        "before_norm": norm_before,
        "after_norm": norm_after,
        "rejected_at": now_iso,
        "last_seen_at": now_iso,
        "rejection_count": 1,
    }
    state.setdefault("rejected_delta_hashes", []).append(entry)
    return entry


def count_same_before_rejections(
    state: Dict[str, Any], before_text: str
) -> int:
    """Count total rejections sharing this normalized `before`.

    Sums `rejection_count` across entries with the same normalized
    `before`, so a single entry whose count has been bumped (via fuzzy
    re-match in `record_rejected_delta`) contributes the full tally.
    Used by the Round A S4 escalation gate (`>= 3` parks vision-critic).
    """
    norm = _norm_for_vision(before_text)
    return sum(
        int(e.get("rejection_count") or 0)
        for e in (state.get("rejected_delta_hashes") or [])
        if isinstance(e, dict) and e.get("before_norm") == norm
    )


def expire_vision_critic_pending_delta(
    state: Dict[str, Any], *, hours: int = 24
) -> bool:
    """Round A E11 — auto-commit an unanswered vision delta to history.

    If `state.vision_critic_pending_delta.proposed_at` is older than
    `hours`, append the delta to `vision_critic_history` with
    `user_action="parked_expired"` and clear the pending slot.
    Returns True if a delta was expired.
    """
    delta = state.get("vision_critic_pending_delta")
    if not isinstance(delta, dict):
        return False
    proposed_at_str = delta.get("proposed_at")
    if not isinstance(proposed_at_str, str) or not proposed_at_str:
        return False
    try:
        proposed_dt = _dt.datetime.fromisoformat(
            proposed_at_str.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    now = _dt.datetime.now(_dt.timezone.utc)
    if (now - proposed_dt).total_seconds() < hours * 3600:
        return False
    state.setdefault("vision_critic_history", []).append({
        "ts": now.isoformat(),
        "source": "vision_critic",
        "before": delta.get("before"),
        "after": delta.get("after"),
        "rationale": delta.get("rationale"),
        "user_action": "parked_expired",
        "alignment_score": delta.get("alignment_score"),
    })
    state["vision_critic_pending_delta"] = None
    return True


def truncate_phase_context(text: str, *, cap: int = 500) -> str:
    """Round A S5 — truncate roadmap phase_context to `cap` chars.

    No re-request loop (DoS defense). Lead displays the truncated text
    as a code block. Returns the truncated string; raises only on
    non-string input.
    """
    if not isinstance(text, str):
        raise ValueError("truncate_phase_context: text must be a string")
    if len(text) <= cap:
        return text
    return text[:cap].rstrip()


def expire_rejected_deltas(state: Dict[str, Any]) -> int:
    """Drop rejected-delta entries older than TTL. Returns dropped count.

    Called from Step −1 prune (Phase 17-G). Standalone helper here so
    Phase 17-D tests can verify the TTL gate.
    """
    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff = (
        now - _dt.timedelta(days=_VISION_DELTA_REJECT_TTL_DAYS)
    ).isoformat()
    lst = state.get("rejected_delta_hashes") or []
    kept = [
        e for e in lst
        if isinstance(e, dict)
        and (e.get("last_seen_at") or e.get("rejected_at") or "9999") >= cutoff
    ]
    dropped = len(lst) - len(kept)
    if dropped > 0:
        state["rejected_delta_hashes"] = kept
    return dropped


def vision_alignment_skip(alignment_score: Optional[float]) -> bool:
    """Return True if the alignment_score is high enough to skip user prompt.

    Per §9.1 user decision: alignment_score > 0.8 → no user prompt; just
    record the report internally.
    """
    if alignment_score is None:
        return False
    try:
        return float(alignment_score) > _VISION_ALIGNMENT_SKIP_THRESHOLD
    except (TypeError, ValueError):
        return False


def two_call_confirm_required(
    state: Dict[str, Any], delta: Dict[str, Any]
) -> bool:
    """Round A S6: two-call confirmation guard.

    Return True if the lead must wait for a *second* vision-critic call
    to propose the same delta before surfacing it to the user (slow-drift
    defense). False means the lead may prompt the user now.

    Logic:
      - If `vision_critic_history` has no prior entry with
        `user_action="pending_second_confirm"` matching this delta, this
        is the first proposal → return True (require second call).
      - If a matching pending entry exists, return False (prompt now).
    """
    norm_before = _norm_for_vision(delta.get("before") or "")
    norm_after = _norm_for_vision(delta.get("after") or "")
    for entry in reversed(state.get("vision_critic_history") or []):
        if not isinstance(entry, dict):
            continue
        if entry.get("user_action") != "pending_second_confirm":
            continue
        prev_before = _norm_for_vision(entry.get("before") or "")
        prev_after = _norm_for_vision(entry.get("after") or "")
        if difflib.SequenceMatcher(None, norm_before, prev_before).ratio() >= _VISION_TWO_CALL_SIMILARITY \
           and difflib.SequenceMatcher(None, norm_after, prev_after).ratio() >= _VISION_TWO_CALL_SIMILARITY:
            return False
    return True


def vision_critic_due(
    state: Dict[str, Any], current_cycle: int, *, offset: int = 12, period: int = 25
) -> bool:
    """Stage 3 trigger predicate.

    Phase-shifted by `offset` cycles from the scout D1 reflection so the
    two heavy reflections never fire in the same invocation.
    """
    if current_cycle <= offset:
        return False
    if current_cycle % period != offset:
        return False
    return (state.get("last_vision_critic_cycle") or 0) != current_cycle


PLANNER_FIELDS_TO_CLEAR = (
    "planner_candidates",
    "planner_candidates_buffer",
    "planner_decisions",
    "planner_creating_done",
    "planner_creating_lock_started_at",
    "planner_creating_lock_owner",
    "planner_confirm_idx",
    "planner_created_urls",
    "planner_failed_creations",
    "planner_message",
    "planning_retried",
)


def clear_planner_fields(state: Dict[str, Any]) -> None:
    """Reset transient planner fields between cycles. Keeps planner_history."""
    for field in PLANNER_FIELDS_TO_CLEAR:
        if field in state:
            v = state[field]
            if isinstance(v, list):
                state[field] = []
            elif isinstance(v, dict):
                state[field] = {}
            elif isinstance(v, bool):
                state[field] = False
            elif isinstance(v, int):
                state[field] = 0
            else:
                state[field] = None


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
    "dedup_candidates",
    "candidate_create_plan",
    "clear_planner_fields",
    "DEDUP_METHOD_DEFAULT",
    # Phase 17-D — vision-critic helpers
    "vision_delta_violates_guard",
    "find_rejected_delta_match",
    "record_rejected_delta",
    "count_same_before_rejections",
    "expire_rejected_deltas",
    "vision_alignment_skip",
    "two_call_confirm_required",
    "vision_critic_due",
    "expire_vision_critic_pending_delta",
    "truncate_phase_context",
]
