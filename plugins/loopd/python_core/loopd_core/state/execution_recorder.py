"""
Task-scoped JSONL execution record writer/reader.

Records pipeline execution history per task to
_state/execution_records/{task_id}.jsonl.

Unlike EventLogger (daily cross-task operational log),
ExecutionRecorder provides task-scoped execution history
for post-analysis (token tracking, agent behavior, failure debugging).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loopd_core.config import Config, get_config

logger = logging.getLogger(__name__)

# Truncation limits for sensitive/large fields
_MAX_PREVIEW_LEN = 200
_MAX_ERROR_MSG_LEN = 500


class ExecutionRecorder:
    """Task-scoped JSONL execution record writer/reader.

    Lifecycle: open(task_id) -> N x record() -> close()
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self._records_dir = self.config.execution_records_path
        self._records_dir.mkdir(parents=True, exist_ok=True)
        self._task_id: Optional[str] = None

    @property
    def is_open(self) -> bool:
        """True if recorder has an active task (open() called, close() not yet)."""
        return self._task_id is not None

    # ── Writer API (open/close lifecycle) ─────────────────────

    def open(
        self,
        task_id: str,
        starting_agent: str,
        level: int,
        priority: int,
        resume: bool = False,
    ) -> None:
        """Start recording for a task.

        Writes pipeline_start event and stores task_id for subsequent calls.
        """
        self._task_id = task_id
        self.record(
            event="pipeline_start",
            detail={
                "starting_agent": starting_agent,
                "level": level,
                "priority": priority,
                "resume": resume,
            },
        )

    def close(
        self,
        status: str,
        final_agent: str,
        iterations: int,
        total_duration_ms: Optional[int] = None,
    ) -> None:
        """End recording. Writes pipeline_end with summary.

        Calculates total_tokens from all turn_end events.
        Resets internal state.
        """
        # Aggregate tokens from turn_end events
        total_tokens = 0
        if self._task_id:
            events = self.read_record(self._task_id)
            for ev in events:
                if ev.get("event") == "turn_end" and ev.get("tokens"):
                    total_tokens += ev["tokens"]

        self.record(
            event="pipeline_end",
            agent=final_agent,
            duration_ms=total_duration_ms,
            detail={
                "status": status,
                "final_agent": final_agent,
                "iterations": iterations,
                "total_tokens": total_tokens,
            },
        )
        self._task_id = None

    # ── Writer API (event recording) ──────────────────────────

    def record(
        self,
        event: str,
        agent: Optional[str] = None,
        subagent: Optional[str] = None,
        turn: Optional[int] = None,
        tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
        detail: Optional[dict[str, Any]] = None,
        **detail_kwargs: Any,
    ) -> None:
        """Write a single event record.

        Core low-level method. All convenience methods delegate here.
        Appends one JSON line to _state/execution_records/{task_id}.jsonl.
        """
        if not self._task_id:
            logger.warning("ExecutionRecorder.record() called without open()")
            return

        merged_detail: dict[str, Any] = {}
        if detail:
            merged_detail.update(detail)
        if detail_kwargs:
            merged_detail.update(detail_kwargs)

        record: dict[str, Any] = {
            "ts": self._now(),
            "event": event,
            "agent": agent,
            "subagent": subagent,
            "turn": turn,
            "tokens": tokens,
            "duration_ms": duration_ms,
            "detail": merged_detail,
        }
        self._append(record)

    # ── Convenience methods ───────────────────────────────────

    def agent_start(
        self, agent: str, subagents: Optional[list[str]] = None
    ) -> None:
        self.record(
            event="agent_start",
            agent=agent,
            detail={"subagents_planned": subagents or []},
        )

    def agent_complete(
        self,
        agent: str,
        artifacts: Optional[list[str]] = None,
        next_agent: Optional[str] = None,
    ) -> None:
        self.record(
            event="agent_complete",
            agent=agent,
            detail={
                "artifacts_created": artifacts or [],
                "next_agent": next_agent,
            },
        )

    def turn_start(
        self,
        turn_id: int,
        agent: str,
        subagent: Optional[str] = None,
    ) -> None:
        self.record(
            event="turn_start",
            agent=agent,
            subagent=subagent,
            turn=turn_id,
        )

    def turn_end(
        self,
        turn_id: int,
        agent: str,
        state: str,
        tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
        result_preview: Optional[str] = None,
    ) -> None:
        detail: dict[str, Any] = {"state": state}
        if result_preview is not None:
            detail["result_preview"] = result_preview[:_MAX_PREVIEW_LEN]
        self.record(
            event="turn_end",
            agent=agent,
            turn=turn_id,
            tokens=tokens,
            duration_ms=duration_ms,
            detail=detail,
        )

    def handoff(
        self,
        from_agent: str,
        to_agent: str,
        review_passed: bool = True,
        backward: bool = False,
    ) -> None:
        self.record(
            event="handoff",
            detail={
                "from_agent": from_agent,
                "to_agent": to_agent,
                "review_passed": review_passed,
                "backward": backward,
            },
        )

    def human_reply(
        self,
        intent: str,
        response_preview: Optional[str] = None,
        wait_duration_ms: Optional[int] = None,
    ) -> None:
        detail: dict[str, Any] = {"intent": intent}
        if response_preview is not None:
            detail["response_preview"] = response_preview[:_MAX_PREVIEW_LEN]
        if wait_duration_ms is not None:
            detail["wait_duration_ms"] = wait_duration_ms
        self.record(event="human_reply", detail=detail)

    def error(
        self,
        agent: str,
        error_type: str,
        message: str,
        recoverable: bool = True,
    ) -> None:
        self.record(
            event="error",
            agent=agent,
            detail={
                "error_type": error_type,
                "message": message[:_MAX_ERROR_MSG_LEN],
                "recoverable": recoverable,
            },
        )

    def rate_limit(
        self,
        agent: str,
        usage_percent: Optional[float] = None,
        cooldown_until: Optional[str] = None,
    ) -> None:
        detail: dict[str, Any] = {}
        if usage_percent is not None:
            detail["usage_percent"] = usage_percent
        if cooldown_until is not None:
            detail["cooldown_until"] = cooldown_until
        self.record(event="rate_limit", agent=agent, detail=detail)

    def checkpoint(self, agent: str, next_agent: str, policy: str) -> None:
        self.record(
            event="checkpoint",
            agent=agent,
            detail={
                "completed_agent": agent,
                "next_agent": next_agent,
                "policy": policy,
            },
        )

    def backward(
        self,
        from_agent: str,
        to_agent: str,
        reason: str,
        backward_count: int,
    ) -> None:
        self.record(
            event="backward",
            agent=from_agent,
            detail={
                "from_agent": from_agent,
                "to_agent": to_agent,
                "reason": reason,
                "backward_count": backward_count,
            },
        )

    def validation(
        self, agent: str, decision: str, retry_count: int = 0
    ) -> None:
        self.record(
            event="validation",
            agent=agent,
            detail={"decision": decision, "retry_count": retry_count},
        )

    # ── Reader API ────────────────────────────────────────────

    def read_record(self, task_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Read all events for a task.

        Uses self._task_id if task_id not given (for active recording).
        Skips malformed JSON lines.
        """
        tid = task_id or self._task_id
        if not tid:
            return []

        record_file = self._record_path(tid)
        if not record_file.exists():
            return []

        events: list[dict[str, Any]] = []
        with open(record_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events

    def get_summary(self, task_id: Optional[str] = None) -> dict[str, Any]:
        """Get execution summary.

        Returns aggregated stats: total_tokens, duration, agent_sequence, etc.
        """
        events = self.read_record(task_id)

        total_tokens = 0
        total_duration_ms = 0
        agent_sequence: list[str] = []
        error_count = 0
        backward_count = 0
        turn_count = 0
        tokens_by_agent: dict[str, int] = {}

        for ev in events:
            event_type = ev.get("event")

            if event_type == "turn_end":
                turn_count += 1
                t = ev.get("tokens") or 0
                total_tokens += t
                agent = ev.get("agent", "unknown")
                tokens_by_agent[agent] = tokens_by_agent.get(agent, 0) + t

            elif event_type == "agent_start":
                agent = ev.get("agent")
                if agent and (not agent_sequence or agent_sequence[-1] != agent):
                    agent_sequence.append(agent)

            elif event_type == "error":
                error_count += 1

            elif event_type == "backward":
                backward_count += 1

            elif event_type == "pipeline_end":
                total_duration_ms = ev.get("duration_ms") or 0

        return {
            "total_tokens": total_tokens,
            "total_duration_ms": total_duration_ms,
            "turn_count": turn_count,
            "agent_sequence": agent_sequence,
            "error_count": error_count,
            "backward_count": backward_count,
            "tokens_by_agent": tokens_by_agent,
        }

    def exists(self, task_id: str) -> bool:
        """Check if execution record file exists."""
        return self._record_path(task_id).exists()

    # ── Internal ──────────────────────────────────────────────

    def _record_path(self, task_id: str) -> Path:
        """Resolve: _state/execution_records/{task_id}.jsonl"""
        return self._records_dir / f"{task_id}.jsonl"

    def _append(self, event: dict[str, Any]) -> None:
        """Append JSON line to file. Best-effort (IOError logged, not raised)."""
        if not self._task_id:
            return
        try:
            record_file = self._record_path(self._task_id)
            line = json.dumps(event, ensure_ascii=False) + "\n"
            with open(record_file, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            logger.warning(
                f"Failed to write execution record for {self._task_id}",
                exc_info=True,
            )

    def _now(self) -> str:
        """UTC ISO 8601 timestamp (second precision)."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
