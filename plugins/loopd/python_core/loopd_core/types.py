"""
Core type definitions for loopd.

Pydantic models that match the task.json schema used by the shell scripts,
ensuring 100% compatibility with existing functionality.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


# Source type marker for idle-generated tasks — used across state and idle layers
IDLE_SOURCE_TYPE = "idle"


class TaskStatus(str, Enum):
    """Task queue status - maps to queue directories."""
    PENDING = "pending"
    ACTIVE = "active"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    PREEMPTED = "preempted"  # idle task normally preempted by user_work_detected


class Phase(str, Enum):
    """Pipeline internal execution phase (Layer 2 FSM).

    Controls when task state can be mutated and persisted:
    - READY: between turns; persist allowed
    - EXECUTING: agent running; disk I/O forbidden
    - HANDING_OFF: building handoff context
    - VALIDATING: checking agent output quality
    """
    READY = "ready"
    EXECUTING = "executing"
    HANDING_OFF = "handing_off"
    VALIDATING = "validating"


class TransitionDecision(str, Enum):
    """Decision from validation of agent output quality."""
    PROCEED = "proceed"    # Output accepted; advance to next agent
    RETRY = "retry"        # Output rejected; re-run same agent
    ESCALATE = "escalate"  # Severe failure; move to waiting_human


class AgentType(str, Enum):
    """Main agent types in the pipeline."""
    CORE = "core"
    ANALYSIS = "analysis"
    PLANNING = "planning"
    SOLUTIONING = "solutioning"
    IMPL = "impl"


class SubagentType(str, Enum):
    """Subagent types within main agents."""
    # Analysis subagents
    RESEARCH = "research"
    BRAINSTORM = "brainstorm"
    PRODUCT_BRIEF = "product_brief"

    # Planning subagents
    PRD = "prd"
    UX_DESIGN = "ux_design"
    PLAN_CRITIC = "plan_critic"

    # Solutioning subagents
    ARCHITECTURE = "architecture"
    EPIC_STORY = "epic_story"
    TECH_SPEC = "tech_spec"
    SOLUTION_CRITIC = "solution_critic"

    # Implementation subagents
    DEV = "dev"
    TEST = "test"
    REVIEW = "review"
    PR = "pr"
    SELF_REFINE = "self_refine"


class TurnState(str, Enum):
    """Turn lifecycle states."""
    IDLE = "idle"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Turn(BaseModel):
    """A single agent execution turn within a task."""
    model_config = ConfigDict(extra="allow")

    turn_id: int = Field(description="Sequential turn number (1-based)")
    agent: str = Field(description="Agent that executed this turn")
    subagent: Optional[str] = Field(default=None, description="Subagent if applicable")
    state: TurnState = Field(default=TurnState.IDLE)
    started_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    result: Optional[str] = Field(default=None, description="Turn outcome summary")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    tokens: Optional[int] = Field(default=None, description="Approximate token count for this turn")
    input_tokens: Optional[int] = Field(default=None, description="Real input token count (from CLI JSON)")
    output_tokens: Optional[int] = Field(default=None, description="Real output token count (from CLI JSON)")
    cost_usd: Optional[float] = Field(default=None, description="Estimated cost in USD")
    model: Optional[str] = Field(default=None, description="Model used for this turn")
    duration_ms: Optional[int] = Field(default=None, description="Turn execution time in milliseconds")

    @field_serializer("started_at", "ended_at")
    @classmethod
    def serialize_turn_datetime(cls, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")


class TaskSource(BaseModel):
    """Source of task creation."""
    model_config = ConfigDict(extra="allow")

    type: str = Field(default="manual", description="Source type: manual, cli, github, slack, webhook")
    ref: Optional[str] = Field(default=None, description="Reference ID (issue number, thread ID, etc.)")


class TaskState(BaseModel):
    """Current execution state of the task."""
    model_config = ConfigDict(extra="allow")

    phase: Optional[str] = Field(default=None, description="Current pipeline phase")
    current_agent: Optional[str] = Field(default=None, description="Currently executing agent")
    current_subagent: Optional[str] = Field(default=None, description="Currently executing subagent")
    previous_agent: Optional[str] = Field(default=None, description="Previous agent (set on handoff)")
    story: Optional[str] = Field(default=None, description="Current story ID if in implementation")
    session_key: Optional[str] = Field(default=None, description="Current deterministic session key")
    session_history: list[str] = Field(default_factory=list, description="Chain of previous session keys from handoffs")


class SlackThread(BaseModel):
    """Slack thread information for task communication."""
    model_config = ConfigDict(extra="allow")

    channel_id: Optional[str] = Field(default=None)
    root_ts: Optional[str] = Field(default=None)


class WorkspaceInfo(BaseModel):
    """Git workspace information for the task."""
    model_config = ConfigDict(extra="allow")

    repo: Optional[str] = Field(default=None, description="GitHub repo in owner/repo format")
    branch: Optional[str] = Field(default="main", description="Base branch")
    base_branch: Optional[str] = Field(default=None, description="Base branch preserved after workspace creation (for PR target and sync)")
    base_commit: Optional[str] = Field(default=None, description="Specific commit SHA to checkout instead of branch tip (used for SWE-bench)")
    path: Optional[str] = Field(default=None, description="Local worktree path")


class HistoryEntry(BaseModel):
    """History entry recording agent actions."""
    model_config = ConfigDict(extra="allow")

    agent: str
    action: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    target: Optional[str] = Field(default=None)
    data: dict[str, Any] = Field(default_factory=dict)


class ResumePoint(BaseModel):
    """Resume point for rate-limit recovery."""
    model_config = ConfigDict(extra="allow")

    agent: str
    subagent: Optional[str] = None
    progress: str
    next_step: str
    last_completed_turn_id: Optional[int] = Field(
        default=None,
        description="Turn ID of the last completed turn before rate limit"
    )
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AutonomyLog(BaseModel):
    """Autonomy tracking for self-checks and escalations."""
    model_config = ConfigDict(extra="allow")

    self_checks: list[dict[str, Any]] = Field(default_factory=list)
    human_escalations: list[dict[str, Any]] = Field(default_factory=list)
    auto_continues: int = 0
    self_refines: int = 0


class Artifact(BaseModel):
    """Output artifact from agent execution."""
    model_config = ConfigDict(extra="allow")

    type: str = Field(description="Artifact type: prd, architecture, epic, etc.")
    path: str = Field(description="Path to artifact file")


class FailureType(str, Enum):
    """Failure classification for retry decisions."""
    TRANSIENT = "transient"          # Rate limit, network error → auto-retry
    DETERMINISTIC = "deterministic"  # Bad output, validation fail → retry wastes tokens
    HUMAN_REQUIRED = "human_required"  # Merge conflict, ambiguous requirements → escalate


class TransitionDecision(str, Enum):
    """Pipeline transition decision from _validate_agent_output."""
    PROCEED = "proceed"    # Validation passed — continue to next stage
    RETRY = "retry"        # Validation failed — retry the same agent (up to limit)
    ESCALATE = "escalate"  # Retry limit exceeded or critical failure — escalate to human


class CheckpointPolicy(str, Enum):
    """Configurable human checkpoint policy for the pipeline."""
    NONE = "none"          # No checkpoints (default, original behavior)
    KEY_STAGES = "key_stages"  # Checkpoint after analysis and solutioning
    ALL = "all"            # Checkpoint after every agent (except impl)
    CUSTOM = "custom"      # Per-agent checkpoint configuration
    ADAPTIVE = "adaptive"  # Checkpoint only when agent confidence is low


class CheckpointConfig(BaseModel):
    """Per-agent checkpoint configuration for CUSTOM policy."""
    model_config = ConfigDict(extra="allow")

    policy: CheckpointPolicy = Field(default=CheckpointPolicy.NONE)
    checkpoint_agents: list[str] = Field(
        default_factory=list,
        description="Agents after which to insert a checkpoint (used with CUSTOM policy)"
    )
    escalate_on_review_warnings: bool = Field(
        default=False,
        description="Auto-checkpoint when handoff review produces warnings (any policy)"
    )
    auto_approve_timeout_seconds: Optional[int] = Field(
        default=None,
        description="If set, auto-approve checkpoint after this many seconds (0 = no auto-approve)"
    )
    require_explicit_approval: bool = Field(
        default=False,
        description="If True, checkpoint blocks until explicit human '진행' response"
    )


class AttachedFile(BaseModel):
    """A single downloaded Slack file attachment with optional extracted content."""
    model_config = ConfigDict(extra="allow")

    path: Optional[str] = Field(default=None, description="Absolute local path to downloaded file")
    name: str = Field(description="Original filename")
    category: str = Field(description="File category: image | text | document | unknown")
    content: Optional[str] = Field(default=None, description="Extracted text content (text/document only)")
    error: Optional[str] = Field(default=None, description="Error message if download/extraction failed")


class TaskContext(BaseModel):
    """Task execution context with blockers, analysis results, etc."""
    model_config = ConfigDict(extra="allow", validate_assignment=True)

    blockers: list[str] = Field(default_factory=list)
    last_self_check: Optional[datetime] = None
    artifacts_ready: list[str] = Field(default_factory=list)
    core_analysis: Optional[dict[str, Any]] = None
    core_parse_failures: int = 0
    validation_warnings: list[str] = Field(default_factory=list)
    human_response: Optional[str] = None
    human_response_user: Optional[str] = None
    human_response_at: Optional[datetime] = None
    human_response_intent: Optional[str] = None
    handoff: Optional[dict[str, Any]] = None
    skip_subagents: Optional[dict[str, list[str]]] = None
    last_orphan_recovery: Optional[datetime] = None
    cooldown_until: Optional[datetime] = Field(
        default=None,
        description="Task cannot be activated before this timestamp (set after orphan recovery to prevent immediate re-activation)"
    )
    activation_started: Optional[datetime] = None
    consecutive_short_lived_count: int = Field(
        default=0,
        description="Consecutive worker deaths under SHORT_LIFETIME_SECONDS — triggers fast-fail circuit breaker"
    )
    retry_count_impl: int = 0
    global_retry_count: int = Field(default=0, description="Total retries across all orchestrator cycles")
    last_failure_type: Optional[str] = Field(default=None, description="FailureType of the last failure")
    checkpoint_policy: CheckpointPolicy = Field(default=CheckpointPolicy.NONE)
    checkpoint_config: Optional[CheckpointConfig] = Field(
        default=None,
        description="Per-agent checkpoint configuration (used with CUSTOM policy)"
    )
    review_results: list[dict[str, Any]] = Field(default_factory=list)
    backward_transition_reason: Optional[str] = None
    backward_transition_count: int = 0
    backward_context: Optional[dict[str, Any]] = Field(
        default=None,
        description="Focused context for backward transition re-execution"
    )
    backward_reason_history: list[str] = Field(
        default_factory=list,
        description="History of backward transition reasons for repeat detection"
    )
    subagent_recommendations: Optional[dict[str, list[str]]] = Field(
        default=None,
        description="Agent-recommended subagents to run (from previous agent output)"
    )
    inter_agent_review_results: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Results from receiving-agent review of previous agent output"
    )
    thread_history: Optional[list[dict[str, Any]]] = Field(
        default=None,
        description=(
            "Slack thread reply history (recent 20). Each entry: {text, user_id, ts}. "
            "None = not yet fetched (uninitialized); [] = fetched but no replies."
        )
    )
    # HITL type discriminator — distinguishes narrowing vs checkpoint
    hitl_type: Optional[str] = Field(
        default=None,
        description="Active HITL interaction type: 'narrowing' | None (regular checkpoint)",
    )
    # Narrowing state
    narrowing_state: Optional[str] = Field(
        default=None,
        description="Narrowing conversation state: 'waiting' | 'completed' | None",
    )
    narrowing_rounds: int = Field(
        default=0,
        description="Number of narrowing conversation rounds completed",
    )
    enriched_prompt: Optional[str] = Field(
        default=None,
        description="User-clarified enrichment for task prompt (set after narrowing completion)",
    )
    # Adaptive checkpoint — confidence tracking
    confidence_scores: dict[str, float] = Field(
        default_factory=dict,
        description="Per-agent confidence scores derived from review results (0.0–1.0)",
    )
    attached_images: list[str] = Field(
        default_factory=list,
        description=(
            "Local absolute paths to downloaded Slack image attachments. "
            "Populated when user attaches images to a Slack message or thread reply."
        )
    )
    attached_files: list[AttachedFile] = Field(
        default_factory=list,
        description=(
            "All downloaded Slack file attachments (images, text, documents) with "
            "extracted content for text/PDF files. Use this for new code; "
            "attached_images is kept for backward compatibility."
        )
    )


def _coerce_task_field_types(data: dict) -> None:
    """Auto-correct known type mismatches in raw task JSON data.

    Some external writers (gateway, manual edits) may produce wrong default
    types for certain fields.  Rather than crashing the entire orchestrator
    cycle, silently fix them before Pydantic validation.
    """
    if "autonomy_log" in data and not isinstance(data["autonomy_log"], dict):
        data["autonomy_log"] = {}
    if "turns" in data and not isinstance(data["turns"], list):
        data["turns"] = []
    if "current_turn" in data and not isinstance(data.get("current_turn"), (dict, type(None))):
        data["current_turn"] = None
    if "artifacts" in data and not isinstance(data["artifacts"], list):
        data["artifacts"] = []


class Task(BaseModel):
    """
    Main task model - matches task JSON schema exactly for shell compatibility.

    This is the core data structure used throughout the system. It must stay
    compatible with the shell scripts' task.json format.
    """
    model_config = ConfigDict(extra="allow", use_enum_values=True)

    id: str = Field(description="Unique task ID: task-YYYY-MM-DD-NNN")
    prompt: str = Field(description="User request/task description")
    title: Optional[str] = Field(default=None, description="Optional short title")
    level: int = Field(default=1, ge=0, le=4, description="Complexity level 0-4")
    priority: int = Field(default=3, ge=1, le=5, description="Priority 1-5 (1=urgent)")
    status: TaskStatus = Field(default=TaskStatus.PENDING)
    task_type: str = Field(default="dev", description="'dev' for full pipeline, 'query' for quick query, 'research' for standalone research workflow")

    source: TaskSource = Field(default_factory=TaskSource)
    state: TaskState = Field(default_factory=TaskState)
    workspace: Optional[WorkspaceInfo] = None
    slack_thread: Optional[SlackThread] = None

    requester_slack_id: Optional[str] = Field(
        default=None,
        description="Slack user ID of the task requester (for @mention in HITL)"
    )

    available_actions: list[str] = Field(default_factory=list)
    context: TaskContext = Field(default_factory=TaskContext)
    autonomy_log: AutonomyLog = Field(default_factory=AutonomyLog)

    resume_point: Optional[ResumePoint] = None
    turns: list[Turn] = Field(default_factory=list, description="Turn lifecycle records")
    current_turn: Optional[Turn] = Field(default=None, description="Currently active turn")
    history: list[HistoryEntry] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list,
        description="Task IDs that must complete before this task can start"
    )

    pr_url: Optional[str] = None
    execution_summary: Optional[dict[str, Any]] = Field(
        default=None,
        description="Pipeline execution summary: total_tokens, turn_count, total_duration_ms, etc."
    )

    @field_validator("execution_summary", mode="before")
    @classmethod
    def _coerce_execution_summary(cls, v: Any) -> Any:
        # Legacy JSON files may store execution_summary as a plain string (e.g. cancellation
        # reason). Wrap it in a dict so Pydantic validation does not crash the orchestrator.
        if isinstance(v, str):
            return {"reason": v}
        return v

    cli_session_id: Optional[str] = Field(
        default=None,
        description="Claude CLI session UUID from the last LLMResponse, used for --resume on followup tasks"
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None

    @field_serializer("created_at", "updated_at", "completed_at")
    @classmethod
    def serialize_datetime(cls, v: Optional[datetime]) -> Optional[str]:
        if v is None:
            return None
        return v.strftime("%Y-%m-%dT%H:%M:%SZ")

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict with datetime serialization for JSON compatibility."""
        import json as _json
        # Use model_dump_json → json.loads round-trip to ensure all values
        # are plain JSON-serializable types (avoids Pydantic SerializationIterator).
        data = _json.loads(self.model_dump_json())
        # Ensure datetime fields are properly formatted
        for key in ["created_at", "updated_at", "completed_at"]:
            if data.get(key):
                if isinstance(data[key], datetime):
                    data[key] = data[key].strftime("%Y-%m-%dT%H:%M:%SZ")
        # Handle nested datetime in history
        for entry in data.get("history", []):
            if entry.get("ts") and isinstance(entry["ts"], datetime):
                entry["ts"] = entry["ts"].strftime("%Y-%m-%dT%H:%M:%SZ")
        # Handle nested datetime in turns
        for turn in data.get("turns", []):
            for key in ["started_at", "ended_at"]:
                if turn.get(key) and isinstance(turn[key], datetime):
                    turn[key] = turn[key].strftime("%Y-%m-%dT%H:%M:%SZ")
        # Handle current_turn datetime
        if data.get("current_turn"):
            for key in ["started_at", "ended_at"]:
                if data["current_turn"].get(key) and isinstance(data["current_turn"][key], datetime):
                    data["current_turn"][key] = data["current_turn"][key].strftime("%Y-%m-%dT%H:%M:%SZ")
        # Handle resume_point datetime
        if data.get("resume_point") and data["resume_point"].get("saved_at"):
            if isinstance(data["resume_point"]["saved_at"], datetime):
                data["resume_point"]["saved_at"] = data["resume_point"]["saved_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
        # Handle context nested datetimes
        if data.get("context"):
            for key in ["last_self_check", "human_response_at", "last_orphan_recovery", "activation_started"]:
                if data["context"].get(key) and isinstance(data["context"][key], datetime):
                    data["context"][key] = data["context"][key].strftime("%Y-%m-%dT%H:%M:%SZ")
        return data

    @classmethod
    def from_json_file(cls, path: str) -> "Task":
        """Load task from JSON file."""
        import json
        from pydantic import ValidationError
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(
                f"Task file must contain a JSON object, got {type(data).__name__}: {path}"
            )
        missing = {"id", "prompt"} - data.keys()
        if missing:
            raise ValueError(f"Task JSON missing required fields {missing}: {path}")
        # Auto-correct known type mismatches (e.g. gateway/external writes)
        _coerce_task_field_types(data)
        try:
            return cls.model_validate(data)
        except ValidationError as e:
            raise ValueError(f"Invalid task JSON schema at {path}: {e}") from e

    def save_to_file(self, path: str) -> None:
        """Save task to JSON file."""
        import json
        import logging
        import traceback
        # Guard against MagicMock-stringified paths from tests
        if "<" in path or ">" in path:
            raise ValueError(f"Invalid file path (likely a MagicMock): {path[:80]}")
        # Debug: trace who saves with current_turn=null (helps identify race condition)
        if self.current_turn is None:
            _logger = logging.getLogger("loopd_core.types")
            stack = "".join(traceback.format_stack(limit=8))
            _logger.debug(f"[save_to_file] current_turn=null at {path}\n{stack}")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)


class CoreAgentResponse(BaseModel):
    """Response structure from Core Agent analysis."""
    model_config = ConfigDict(extra="allow")

    level: int = Field(ge=0, le=4, description="Task complexity level")
    reasoning: str = Field(description="Explanation of level determination")
    next_agent: str = Field(description="Agent to route task to")
    initial_context: dict[str, Any] = Field(default_factory=dict)
    questions_for_human: list[str] = Field(default_factory=list)


class AgentResponse(BaseModel):
    """Generic agent response structure."""
    model_config = ConfigDict(extra="allow")

    status: Optional[str] = None
    next_agent: Optional[str] = None
    next_action: Optional[str] = None
    phase: Optional[str] = None
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    handoff_context: dict[str, Any] = Field(default_factory=dict)


# ─── Pure text classification helpers ─────────────────────────────

def interpret_human_response(text: str) -> str:
    """
    Interpret human response content to determine routing action.

    Returns:
        "close"    - Human says task is done/already implemented
        "cancel"   - Human wants to cancel the task
        "feedback" - Specific feedback to incorporate (default)
    """
    lower = text.strip().lower()

    close_exact = {
        "close", "done", "complete", "finished",
        "already done", "already implemented", "already fixed", "already resolved",
        "완료", "닫아", "끝", "종료",
    }
    if lower in close_exact:
        return "close"

    close_patterns = [
        r"close (this|the task|it)",
        r"mark.*(as )?(done|complete|closed)",
        r"already (been )?(done|implemented|fixed|resolved)",
        r"이미 구현.*(됨|완료|했)",
        r"close 처리",
        r"완료 처리",
        r"닫아 ?줘",
    ]
    for pattern in close_patterns:
        if re.search(pattern, lower):
            return "close"

    cancel_exact = {"cancel", "abort", "stop", "nevermind", "never mind", "취소", "중단", "그만"}
    if lower in cancel_exact:
        return "cancel"

    cancel_patterns = [
        r"cancel (this|the task|it)",
        r"abort (this|the task)",
        r"stop (this|the task)",
        r"취소.*(해|줘)",
        r"중단.*(해|줘)",
    ]
    for pattern in cancel_patterns:
        if re.search(pattern, lower):
            return "cancel"

    return "feedback"
