"""
Requirement Narrowing — pre-pipeline HITL for ambiguous task prompts.

When CoreAgent detects questions_for_human, NarrowingFormatter builds a rich
Slack message that shows:
  1. What the agent understood from the task
  2. What it plans to do (proposed approach)
  3. Available alternatives / different directions
  4. Specific clarification questions

The user can reply with a choice, free text, or just "진행" to proceed as-is.
This replaces the bare escalation message that previously just listed questions.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from loopd_core.types import Task

logger = logging.getLogger(__name__)

# Keywords indicating the user wants to proceed without changes
_PROCEED_KEYWORDS = {
    "진행", "proceed", "continue", "go", "ok", "ㅇㅋ",
    "lgtm", "approve", "approved", "승인", "다음", "넘어가",
    "a", "A",  # first option = proceed as-is
}


def build_narrowing_message(
    task: Task,
    questions: list[str],
    analysis: Optional[dict[str, Any]] = None,
) -> str:
    """Build a rich Slack narrowing message from CoreAgent's analysis.

    Shows the agent's understanding, proposed plan, and alternatives,
    then lists clarification questions. Designed to replace meaningless
    "I have questions" escalations with substantive decision points.

    Args:
        task: The task being analyzed.
        questions: questions_for_human from CoreAgentResponse.
        analysis: Optional core_analysis dict with reasoning/initial_context.

    Returns:
        Formatted Slack message string.
    """
    parts: list[str] = []

    title = task.title or task.id
    parts.append(f":brain: *요구사항 확인 — {title}*")
    parts.append("")

    # Section 1: What the agent understood
    understanding = _extract_understanding(task, analysis)
    if understanding:
        parts.append("*이해한 내용:*")
        parts.append(understanding)
        parts.append("")

    # Section 2: Proposed approach / plan
    approach = _extract_approach(task, analysis)
    if approach:
        parts.append("*실행 계획:*")
        parts.append(approach)
        parts.append("")

    # Section 3: Alternatives exploration
    alternatives = _extract_alternatives(task, analysis)
    parts.append("*방향 선택:*")
    parts.append(f"A) 위 계획대로 진행 (추천)")
    if alternatives:
        for i, alt in enumerate(alternatives[:2], ord("B")):
            parts.append(f"{chr(i)}) {alt}")
    parts.append("직접 입력) 다른 방향이 있다면 자유롭게 알려주세요")
    parts.append("")

    # Section 4: Specific questions
    if questions:
        parts.append("*확인이 필요한 사항:*")
        for q in questions:
            parts.append(f"- {q}")
        parts.append("")

    # Footer with instructions
    parts.append(
        ":bulb: *A를 선택하거나 `진행`을 입력하면 현재 계획대로 시작합니다.*\n"
        "다른 방향이나 추가 컨텍스트가 있다면 자유롭게 알려주세요."
    )

    return "\n".join(parts)


def _extract_understanding(task: Task, analysis: Optional[dict]) -> str:
    """Extract agent's understanding from core_analysis or task prompt."""
    if analysis:
        initial_ctx = analysis.get("initial_context", {}) or {}
        # Try various fields in initial_context
        for field in ("key_requirements", "scope_summary", "understanding"):
            val = initial_ctx.get(field)
            if isinstance(val, list) and val:
                return "\n".join(f"- {item}" for item in val[:5])
            if isinstance(val, str) and val.strip():
                return val.strip()[:400]

        # Fall back to reasoning
        reasoning = analysis.get("reasoning", "")
        if reasoning:
            # Take first 300 chars, stop at sentence boundary
            text = reasoning.strip()[:300]
            last_period = max(text.rfind("。"), text.rfind("."), text.rfind("\n"))
            if last_period > 100:
                text = text[:last_period + 1]
            return text

    # Fallback: first 200 chars of task prompt
    return task.prompt.strip()[:200]


def _extract_approach(task: Task, analysis: Optional[dict]) -> str:
    """Extract proposed approach from core_analysis."""
    if not analysis:
        return ""
    initial_ctx = analysis.get("initial_context", {}) or {}
    for field in ("suggested_approach", "approach", "implementation_plan"):
        val = initial_ctx.get(field)
        if isinstance(val, str) and val.strip():
            return val.strip()[:400]
        if isinstance(val, list) and val:
            return "\n".join(f"- {item}" for item in val[:5])
    return ""


def _extract_alternatives(task: Task, analysis: Optional[dict]) -> list[str]:
    """Extract alternative approaches from core_analysis."""
    if not analysis:
        return []
    initial_ctx = analysis.get("initial_context", {}) or {}

    # Check for explicit alternatives field
    alts = initial_ctx.get("alternatives") or initial_ctx.get("options")
    if isinstance(alts, list) and alts:
        return [str(a)[:150] for a in alts[:2]]

    # Fallback: generate simple alternative descriptions based on level
    level = analysis.get("level", task.level)
    if level >= 3:
        return ["더 작은 범위로 먼저 MVP 구현 (빠른 검증)"]
    if level == 2:
        return ["더 간단한 접근법으로 최소 구현"]
    return []


def is_proceed_response(text: str) -> bool:
    """Check if human response indicates proceeding without changes."""
    lower = text.strip().lower()
    # Exact keyword match
    if lower in _PROCEED_KEYWORDS:
        return True
    # Contains only proceed keywords
    words = set(lower.split())
    if words and words.issubset({w.lower() for w in _PROCEED_KEYWORDS}):
        return True
    return False
