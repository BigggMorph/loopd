"""
Event logging for loopd.

Logs events to JSONL files (one per day) for debugging and auditing.
Compatible with the shell script events.sh format.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from loopd_core.config import Config, get_config


class EventLogger:
    """
    JSONL event logger compatible with lib/events.sh.

    Events are logged to _events/YYYY-MM-DD.jsonl files.

    Event types:
        orchestrator.* - Orchestrator lifecycle
        task.* - Task state changes
        agent.* - Agent handoff/decisions
        self_check.* - Quality gate results
        human.* - Human escalation/response
        rate_limit.* - Token usage events
        github.* - GitHub integration events
        slack.* - Slack communication events
        self_heal.* - Self-heal diagnostics (status_corrected, etc.)
    """

    def __init__(self, config: Optional[Config] = None):
        self.config = config or get_config()
        self.events_dir = self.config.events_path
        self.events_dir.mkdir(parents=True, exist_ok=True)

    def _get_events_file(self, date: Optional[datetime] = None) -> Path:
        """Get events file path for given date (default: today)."""
        if date is None:
            date = datetime.now(timezone.utc)
        date_str = date.strftime("%Y-%m-%d")
        return self.events_dir / f"{date_str}.jsonl"

    def _format_timestamp(self) -> str:
        """Get current UTC timestamp in ISO format."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def log(
        self,
        event_type: str,
        task_id: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """
        Log an event.

        Args:
            event_type: Event type (e.g., "task.created", "agent.core.start")
            task_id: Optional task ID
            data: Optional event data dictionary
        """
        event = {
            "ts": self._format_timestamp(),
            "type": event_type,
        }

        if task_id:
            event["task_id"] = task_id

        if data:
            event["data"] = data

        events_file = self._get_events_file()

        # Append to file with atomic write
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(line)

    # Convenience methods for common event types

    def task_created(self, task_id: str, source: str, ref: Optional[str] = None) -> None:
        """Log task creation event."""
        data = {"source": source}
        if ref:
            data["ref"] = ref
        self.log("task.created", task_id, data)

    def task_moved(
        self,
        task_id: str,
        from_status: str,
        to_status: str
    ) -> None:
        """Log task status transition."""
        self.log("task.moved", task_id, {"from": from_status, "to": to_status})

    def task_activated(self, task_id: str) -> None:
        """Log task activation."""
        self.log("task.activated", task_id)

    def task_completed(self, task_id: str, pr_url: Optional[str] = None) -> None:
        """Log task completion."""
        data = {}
        if pr_url:
            data["pr_url"] = pr_url
        self.log("task.completed", task_id, data if data else None)

    def task_failed(self, task_id: str, reason: str) -> None:
        """Log task failure."""
        self.log("task.failed", task_id, {"reason": reason})

    def task_orphan_recovered(
        self,
        task_id: str,
        target: str,
        reason: str
    ) -> None:
        """Log orphan task recovery."""
        self.log("task.orphan_recovered", task_id, {"target": target, "reason": reason})

    def task_resume_saved(
        self,
        task_id: str,
        agent: str,
        next_step: str
    ) -> None:
        """Log resume point save."""
        self.log("task.resume_saved", task_id, {"agent": agent, "next_step": next_step})

    def turn_start(
        self,
        task_id: str,
        turn_id: int,
        agent: str,
        subagent: Optional[str] = None,
    ) -> None:
        """Log turn start event."""
        data: dict[str, Any] = {"turn_id": turn_id, "agent": agent}
        if subagent:
            data["subagent"] = subagent
        self.log("turn.start", task_id, data)

    def turn_end(
        self,
        task_id: str,
        turn_id: int,
        agent: str,
        state: str,
        result: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Log turn end event."""
        data: dict[str, Any] = {"turn_id": turn_id, "agent": agent, "state": state}
        if result:
            data["result"] = result
        if error:
            data["error"] = error
        self.log("turn.end", task_id, data)

    def agent_start(self, task_id: str, agent: str) -> None:
        """Log agent start."""
        self.log(f"agent.{agent}.start", task_id)

    def agent_complete(
        self,
        task_id: str,
        agent: str,
        next_agent: Optional[str] = None
    ) -> None:
        """Log agent completion."""
        data = {}
        if next_agent:
            data["next_agent"] = next_agent
        self.log(f"agent.{agent}.complete", task_id, data if data else None)

    def agent_error(self, task_id: str, agent: str, error: str) -> None:
        """Log agent error."""
        self.log(f"agent.{agent}.error", task_id, {"error": error})

    def agent_waiting_human(self, task_id: str, agent: str, questions_count: int) -> None:
        """Log agent waiting for human input."""
        self.log(f"agent.{agent}.waiting_human", task_id, {"questions_count": questions_count})

    def handoff(
        self,
        task_id: str,
        from_agent: str,
        to_agent: str
    ) -> None:
        """Log agent handoff."""
        self.log("agent.handoff", task_id, {"from": from_agent, "to": to_agent})

    def agent_review(
        self,
        task_id: str,
        from_agent: str,
        to_agent: str,
        result: dict
    ) -> None:
        """Log inter-agent review result."""
        self.log("agent.review", task_id, {
            "from": from_agent,
            "to": to_agent,
            "passed": result.get("passed"),
            "severity": result.get("severity"),
            "issues_count": len(result.get("issues", [])),
        })

    def human_escalation(self, task_id: str, reason: str) -> None:
        """Log human escalation."""
        self.log("human.escalation", task_id, {"reason": reason})

    def human_response(self, task_id: str, user_id: str) -> None:
        """Log human response received."""
        self.log("human.response", task_id, {"user_id": user_id})

    def rate_limit_warning(self, usage_percent: float) -> None:
        """Log rate limit warning."""
        self.log("rate_limit.warning", data={"usage_percent": usage_percent})

    def rate_limit_stop(self, usage_percent: float, cooldown_until: str) -> None:
        """Log rate limit stop."""
        self.log(
            "rate_limit.stop",
            data={"usage_percent": usage_percent, "cooldown_until": cooldown_until}
        )

    def orchestrator_start(self) -> None:
        """Log orchestrator start."""
        self.log("orchestrator.start")

    def orchestrator_cycle(self, pending: int, active: int) -> None:
        """Log orchestrator cycle."""
        self.log("orchestrator.cycle", data={"pending": pending, "active": active})

    def orchestrator_stop(self, reason: str) -> None:
        """Log orchestrator stop."""
        self.log("orchestrator.stop", data={"reason": reason})

    def self_check_result(
        self,
        task_id: str,
        gate: str,
        passed: bool,
        details: Optional[dict[str, Any]] = None
    ) -> None:
        """Log quality gate check result."""
        data = {"gate": gate, "passed": passed}
        if details:
            data["details"] = details
        self.log("self_check.result", task_id, data)

    def submission_policy_blocked(self, task_id: str, reason: str, branch: str) -> None:
        """Log submission policy violation."""
        self.log("submission.policy_blocked", task_id, {"reason": reason, "branch": branch})

    def submission_policy_passed(self, task_id: str, branch: str) -> None:
        """Log submission policy check passed."""
        self.log("submission.policy_passed", task_id, {"branch": branch})

    def github_pr_created(self, task_id: str, pr_url: str) -> None:
        """Log GitHub PR creation."""
        self.log("github.pr_created", task_id, {"pr_url": pr_url})

    def github_pr_updated(self, task_id: str, pr_url: str) -> None:
        """Log GitHub PR update."""
        self.log("github.pr_updated", task_id, {"pr_url": pr_url})

    def slack_message_sent(
        self,
        task_id: str,
        channel: str,
        message_type: str
    ) -> None:
        """Log Slack message sent."""
        self.log(
            "slack.message_sent",
            task_id,
            {"channel": channel, "message_type": message_type}
        )

    def critic_result(
        self,
        task_id: str,
        data: dict[str, Any],
    ) -> None:
        """Log critic loop evaluation result.

        Args:
            task_id: Task ID
            data: Critic result data from build_critic_event_data()
                  Keys: agent, subagent, critic, verdict, iteration,
                        max_iterations, issues_count, issues, scores
        """
        self.log("critic.result", task_id, data)

    def log_stage_completion(
        self,
        task_id: str,
        stage: str,
        agent: str,
        success: bool,
        tokens_in: int,
        tokens_out: int,
        duration_s: float,
        critic_score: Optional[float] = None,
    ) -> None:
        """Log stage completion metrics event.

        Args:
            task_id: Task ID
            stage: Pipeline stage name (core/analysis/planning/solutioning/impl)
            agent: Agent name that ran this stage
            success: Whether the stage completed successfully
            tokens_in: Total input tokens consumed in this stage
            tokens_out: Total output tokens produced in this stage
            duration_s: Stage wall-clock duration in seconds
            critic_score: Normalized 0.0–1.0 critic score (null if no critic ran)
        """
        data: dict[str, Any] = {
            "schema_version": 1,
            "stage": stage,
            "agent": agent,
            "success": success,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "duration_s": duration_s,
            "critic_score": critic_score,
            "timestamp": self._format_timestamp(),
        }
        self.log("stage.complete", task_id, data)

    def dep_resolved(self, task_id: str, dep_id: str) -> None:
        """Log single dependency link resolved."""
        self.log("dep.resolved", task_id, {"dep_id": dep_id})

    def dep_escalated(self, task_id: str, failed_deps: list[str]) -> None:
        """Log dependency failure escalation to human."""
        self.log("dep.escalated", task_id, {"failed_deps": failed_deps})

    # Query methods

    def read_events(
        self,
        date: Optional[datetime] = None,
        event_type: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """
        Read events from a day's log file.

        Args:
            date: Date to read events from (default: today)
            event_type: Filter by event type prefix (e.g., "task.")
            task_id: Filter by task ID

        Returns:
            List of event dictionaries
        """
        events_file = self._get_events_file(date)
        if not events_file.exists():
            return []

        events = []
        with open(events_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)

                    # Apply filters
                    if event_type and not event.get("type", "").startswith(event_type):
                        continue
                    if task_id and event.get("task_id") != task_id:
                        continue

                    events.append(event)
                except json.JSONDecodeError:
                    continue

        return events

    def get_task_events(self, task_id: str, days: int = 7) -> list[dict[str, Any]]:
        """
        Get all events for a task from the last N days.

        Args:
            task_id: Task ID
            days: Number of days to search (default: 7)

        Returns:
            List of event dictionaries, oldest first
        """
        from datetime import timedelta

        all_events = []
        today = datetime.now(timezone.utc)

        for i in range(days):
            date = today - timedelta(days=i)
            events = self.read_events(date=date, task_id=task_id)
            all_events.extend(events)

        # Sort by timestamp
        all_events.sort(key=lambda e: e.get("ts", ""))
        return all_events
