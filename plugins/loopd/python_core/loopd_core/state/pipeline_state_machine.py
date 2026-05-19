"""
Pipeline State Machine (Layer 2 FSM) for oh-my-agents.

Implements strict phase control to prevent the dual-write race conditions
described in PR #62:
  - task_manager.*() (disk read-modify-write) vs task.save_to_file()
    (stale in-memory → disk overwrite) causing current_turn destruction.

Design principles:
  - READY: transition boundary; persist() allowed
  - EXECUTING: agent running; disk I/O FORBIDDEN (prevents stale writes)
  - HANDING_OFF: building handoff context (read-only for most mutations)
  - VALIDATING: checking agent output quality

The SM owns the in-memory Task copy. All state mutations go through SM
methods. Disk persistence happens ONLY via persist() in READY phase.

Heartbeat (mark_task_processing) writes only a .pids marker file,
not the task JSON — so it's outside SM scope and does not conflict.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loopd_core.types import (
    Artifact,
    HistoryEntry,
    Phase,
    Task,
    TaskStatus,
    Turn,
    TurnState,
)

if TYPE_CHECKING:
    from loopd_core.state.event_logger import EventLogger
    from loopd_core.state.task_manager import TaskManager

logger = logging.getLogger(__name__)


# ─── Exceptions ───────────────────────────────────────────────────


class InvalidTransitionError(Exception):
    """Raised when an invalid phase transition is attempted.

    Encodes the attempted operation and the current phase so callers
    can surface meaningful error messages.
    """


# ─── Guard state ──────────────────────────────────────────────────


@dataclass
class GuardState:
    """Tracks pipeline guard conditions that limit cycling behaviour.

    Fields are deliberately separate scalars (not embedded in Task) so
    that the SM can reset them precisely without touching disk state.
    """

    consecutive_same_agent: int = 0
    """How many iterations in a row the same agent has run."""

    review_backward_pair: Optional[tuple[str, str]] = None
    """(from_agent, to_agent) pair that already triggered a review-backward.
    Prevents infinite review-backward loops for the same pair."""

    val_retry_count: int = 0
    """Number of RETRY decisions emitted by _validate_agent_output for the
    *current* agent without a successful PROCEED in between."""

    backward_count: int = 0
    """Total backward transitions taken so far in this pipeline run."""

    last_agent: Optional[str] = None
    """The most recently run agent (used to detect same-agent repeat)."""


# ─── PipelineStateMachine ─────────────────────────────────────────


class PipelineStateMachine:
    """Layer 2 FSM that controls internal pipeline execution phases.

    The SM wraps a Task (data carrier) and gates all state mutations and
    disk I/O through explicit phase transitions.

    Transition table (see plan for full spec):

    | From         | Event               | To           | Side effects            |
    |--------------|---------------------|--------------|-------------------------|
    | READY        | start_turn()        | EXECUTING    | Turn created, disk write|
    | EXECUTING    | end_turn()          | READY        | Turn finalised, disk write |
    | READY        | start_handoff()     | HANDING_OFF  | handoff_context init    |
    | HANDING_OFF  | complete_handoff()  | READY        | persist()               |
    | HANDING_OFF  | reject_handoff()    | READY        | rollback                |
    | READY        | start_validation()  | VALIDATING   | —                       |
    | VALIDATING   | validation_passed() | READY        | —                       |
    | VALIDATING   | validation_retry()  | READY        | val_retry_count++       |
    | VALIDATING   | validation_escalate()| (Layer1 WH) | persist + move          |
    | READY        | checkpoint()        | (Layer1 WH)  | persist + move          |
    | READY        | rate_limit_pause()  | (Layer1 FL)  | resume + move           |
    | READY        | complete_pipeline() | (Layer1 CO)  | persist + move          |
    | Any          | fail()              | (Layer1 FL)  | persist + move          |

    Invalid transitions raise InvalidTransitionError.
    """

    def __init__(
        self,
        task: Task,
        task_manager: "TaskManager",
        event_logger: Optional["EventLogger"] = None,
        max_iterations: int = 10,
    ) -> None:
        self._task = task
        self._task_manager = task_manager
        self._event_logger = event_logger
        self.max_iterations = max_iterations

        self.phase: Phase = Phase.READY
        self.guards = GuardState()
        self._dirty: bool = False
        self._iteration: int = 0

        # Turn reference kept for fallback recovery (passed to end_turn)
        self._active_turn: Optional[Turn] = None
        # Staging area for handoff context being built
        self._pending_handoff_context: Optional[dict[str, Any]] = None

        # Restore guard state from persisted task context when possible
        bc = getattr(task.context, "backward_transition_count", 0)
        if isinstance(bc, int):
            self.guards.backward_count = bc

    # ─── Factory / classmethod ────────────────────────────────────

    @classmethod
    def load(
        cls,
        task_id: str,
        task_manager: "TaskManager",
        event_logger: Optional["EventLogger"] = None,
        max_iterations: int = 10,
    ) -> "PipelineStateMachine":
        """Load SM by reading task from disk.

        Restores guard state from task context so backward_count and
        other counters survive a cron-restart.
        """
        task = task_manager.read_task(task_id)
        return cls(task, task_manager, event_logger, max_iterations)

    # ─── Public properties ────────────────────────────────────────

    @property
    def task(self) -> Task:
        """The in-memory task (data carrier)."""
        return self._task

    @property
    def task_id(self) -> str:
        return self._task.id

    @property
    def iteration(self) -> int:
        """Current main-loop iteration count (0-based before first tick)."""
        return self._iteration

    @property
    def can_continue(self) -> bool:
        """True while the pipeline has not yet exhausted max_iterations."""
        return self._iteration < self.max_iterations

    def tick(self) -> None:
        """Increment the iteration counter at the start of each loop cycle."""
        self._iteration += 1

    # ─── Phase assertion helpers ──────────────────────────────────

    def _assert_phase(self, *allowed: Phase) -> None:
        """Raise InvalidTransitionError if current phase is not in *allowed*."""
        if self.phase not in allowed:
            raise InvalidTransitionError(
                f"Operation not allowed in phase {self.phase!r}. "
                f"Allowed phases: {[p.value for p in allowed]}"
            )

    def _assert_not_phase(self, *forbidden: Phase) -> None:
        """Raise InvalidTransitionError if current phase is in *forbidden*."""
        if self.phase in forbidden:
            raise InvalidTransitionError(
                f"Operation forbidden in phase {self.phase!r}."
            )

    # ─── Phase transitions: Turn lifecycle ───────────────────────

    def start_turn(self, agent: str, subagent: Optional[str] = None) -> Turn:
        """READY → EXECUTING

        Creates a turn record in-memory, persists to disk (so crash-recovery
        can see the started turn), then locks the phase to EXECUTING.

        Returns the new Turn for caller reference.
        """
        self._assert_phase(Phase.READY)

        # Cancel any lingering active turn left by a crash
        if self._task.current_turn and self._task.current_turn.state == TurnState.ACTIVE:
            self._task.current_turn.state = TurnState.CANCELLED
            self._task.current_turn.ended_at = datetime.now(timezone.utc)
            self._task.turns.append(self._task.current_turn)

        next_id = len(self._task.turns) + 1
        turn = Turn(
            turn_id=next_id,
            agent=agent,
            subagent=subagent,
            state=TurnState.ACTIVE,
            started_at=datetime.now(timezone.utc),
        )
        self._task.current_turn = turn
        self._task.updated_at = datetime.now(timezone.utc)
        self._active_turn = turn
        self._dirty = True

        # Persist BEFORE entering EXECUTING so disk shows the active turn.
        # (If the agent process crashes, orchestrator can see the started turn.)
        self.persist()
        self.phase = Phase.EXECUTING

        # Log event (separate events file — no race concern)
        if self._event_logger:
            self._event_logger.turn_start(self._task.id, next_id, agent, subagent)

        return turn

    def end_turn(
        self,
        state: TurnState,
        result: Optional[str] = None,
        error: Optional[str] = None,
        tokens: Optional[int] = None,
        duration_ms: Optional[int] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
        cost_usd: Optional[float] = None,
        model: Optional[str] = None,
    ) -> None:
        """EXECUTING → READY

        Finalises the turn in-memory only — no disk write here.
        The caller MUST call persist() after end_turn() to atomically
        write the completed turn + any artifacts registered during EXECUTING.

        This design prevents the stale-overwrite race: artifact sync happens
        in-memory during EXECUTING, then end_turn + persist() write everything
        in a single atomic operation from READY.
        """
        self._assert_phase(Phase.EXECUTING)

        turn = self._active_turn
        if turn:
            turn.state = state
            turn.ended_at = datetime.now(timezone.utc)
            turn.result = result
            turn.error = error
            if tokens is not None:
                turn.tokens = tokens
            if duration_ms is not None:
                turn.duration_ms = duration_ms
            if input_tokens is not None:
                turn.input_tokens = input_tokens
            if output_tokens is not None:
                turn.output_tokens = output_tokens
            if cost_usd is not None:
                turn.cost_usd = cost_usd
            if model is not None:
                turn.model = model
            self._task.turns.append(turn)
            self._active_turn = None

        self._task.current_turn = None
        self._task.updated_at = datetime.now(timezone.utc)
        self._dirty = True
        self.phase = Phase.READY

        # Log event (separate events file — no race concern)
        if turn and self._event_logger:
            self._event_logger.turn_end(
                self._task.id,
                turn.turn_id,
                turn.agent,
                state.value if hasattr(state, "value") else str(state),
                result=result,
                error=error,
            )

    # ─── Phase transitions: Handoff lifecycle ────────────────────

    def start_handoff(self, to_agent: str) -> None:
        """READY → HANDING_OFF

        Opens a staging area for the handoff context being built.
        No disk I/O yet — the context is accumulated via set_handoff_context()
        and append_review_result() before being committed on complete_handoff().
        """
        self._assert_phase(Phase.READY)
        self._pending_handoff_context = {"to_agent": to_agent, "timestamp": _now_iso()}
        self.phase = Phase.HANDING_OFF

    def complete_handoff(self) -> None:
        """HANDING_OFF → READY

        Commits the pending handoff context and persists task to disk.
        """
        self._assert_phase(Phase.HANDING_OFF)
        self._pending_handoff_context = None
        self._dirty = True
        self.phase = Phase.READY
        self.persist()

    def reject_handoff(self) -> None:
        """HANDING_OFF → READY

        Rolls back the pending handoff context without persisting.
        """
        self._assert_phase(Phase.HANDING_OFF)
        # Roll back any context already written to the in-memory task
        self._pending_handoff_context = None
        self.phase = Phase.READY

    # ─── Phase transitions: Validation ───────────────────────────

    def start_validation(self, response: str = "") -> None:
        """READY → VALIDATING

        Opens the validation phase. The actual decision is made by the
        caller; the SM only tracks the phase gate.
        """
        self._assert_phase(Phase.READY)
        self.phase = Phase.VALIDATING

    def validation_passed(self) -> None:
        """VALIDATING → READY

        Validation succeeded. Resets the per-agent retry counter.
        """
        self._assert_phase(Phase.VALIDATING)
        self.guards.val_retry_count = 0
        self.phase = Phase.READY

    def validation_retry(self) -> None:
        """VALIDATING → READY

        Validation failed but is retryable. Increments val_retry_count.
        """
        self._assert_phase(Phase.VALIDATING)
        self.guards.val_retry_count += 1
        self.phase = Phase.READY

    def validation_escalate(self) -> None:
        """VALIDATING → (Layer 1: WAITING_HUMAN)

        Severe validation failure; escalates task to human review.
        Persists current state before moving to waiting_human queue.
        """
        self._assert_phase(Phase.VALIDATING)
        self.phase = Phase.READY  # Must be READY for persist() to work
        self.persist()
        self._task_manager.move_task(self._task.id, TaskStatus.WAITING_HUMAN)

    # ─── Phase transitions: Pipeline terminal events ──────────────

    def checkpoint(self, next_agent: str) -> None:
        """READY → (Layer 1: WAITING_HUMAN)

        Human checkpoint: saves state and moves task to waiting_human queue.
        """
        self._assert_phase(Phase.READY)
        # Save where to resume
        if not self._task.context.core_analysis:
            self._task.context.core_analysis = {}
        self._task.context.core_analysis["pending_next_agent"] = next_agent
        self._dirty = True
        self.persist()
        self._task_manager.move_task(self._task.id, TaskStatus.WAITING_HUMAN)

    def rate_limit_pause(
        self,
        agent: str,
        subagent: Optional[str] = None,
    ) -> None:
        """READY → (Layer 1: FAILED for auto-retry)

        Saves resume point for the current agent and moves task to failed
        (the cron-orchestrator will pick it up for retry).
        """
        self._assert_phase(Phase.READY)
        self._task_manager.save_resume_point(
            self._task.id, agent, subagent, "rate_limited", "continue"
        )
        self.persist()
        self._task_manager.move_task(self._task.id, TaskStatus.FAILED)

    def complete_pipeline(self) -> None:
        """READY → (Layer 1: COMPLETED)

        Final completion: persists and moves to completed queue.
        """
        self._assert_phase(Phase.READY)
        self.persist()
        self._task_manager.move_task(self._task.id, TaskStatus.COMPLETED)

    def fail(
        self,
        error: str,
        agent: str = "",
        subagent: Optional[str] = None,
    ) -> None:
        """Any → (Layer 1: FAILED)

        Records the error and moves task to failed queue. Works from any
        phase — resets to READY internally so persist() is allowed.
        """
        # Reset to READY so persist() gate allows the write
        self.phase = Phase.READY
        self._active_turn = None  # Don't try to end an in-flight turn
        self._task.current_turn = None  # Clear stale in-memory turn

        if error:
            self._task.context.blockers.append(error)
            self._dirty = True

        self.persist()
        self._task_manager.move_task(self._task.id, TaskStatus.FAILED)

    def move_to(self, status: TaskStatus) -> None:
        """Persist then move task to the given Layer 1 status.

        Convenience wrapper for persist() + move_task(); not allowed
        during EXECUTING phase.
        """
        self._assert_not_phase(Phase.EXECUTING)
        self.persist()
        self._task_manager.move_task(self._task.id, status)

    # ─── Data mutation gates ──────────────────────────────────────

    def register_artifact(self, artifact: Artifact) -> None:
        """Register a new artifact on the in-memory task.

        Allowed in EXECUTING (end-of-turn sync) or READY (between turns).
        NOT allowed in HANDING_OFF or VALIDATING.
        """
        self._assert_phase(Phase.EXECUTING, Phase.READY)
        if artifact not in self._task.artifacts:
            self._task.artifacts.append(artifact)
            self._dirty = True

    def update_context(self, **kwargs: Any) -> None:
        """Update task context fields on the in-memory task.

        Allowed from any phase (data is staged in memory; disk write happens
        via persist() at the next READY boundary).
        """
        for key, value in kwargs.items():
            setattr(self._task.context, key, value)
        self._dirty = True

    def update_state(self, **kwargs: Any) -> None:
        """Update task.state fields.

        NOT allowed during EXECUTING (prevents stale overwrites).
        """
        self._assert_not_phase(Phase.EXECUTING)
        for key, value in kwargs.items():
            setattr(self._task.state, key, value)
        self._dirty = True

    def set_handoff_context(self, context: dict[str, Any]) -> None:
        """Write handoff context to the in-memory task.

        Only allowed in HANDING_OFF phase so the staging area cannot be
        contaminated by other code paths.
        """
        self._assert_phase(Phase.HANDING_OFF)
        self._task.context.handoff = context
        self._dirty = True

    def append_review_result(self, result: dict[str, Any]) -> None:
        """Append a review result.

        Only allowed in HANDING_OFF phase (review happens during handoff).
        """
        self._assert_phase(Phase.HANDING_OFF)
        self._task.context.review_results.append(result)
        self._dirty = True

    def append_history(
        self,
        agent: str,
        action: str,
        target: Optional[str] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> None:
        """Append a history entry in-memory. Allowed from any phase.

        persist() will write the entry to disk atomically.
        """
        entry = HistoryEntry(
            agent=agent,
            action=action,
            target=target,
            data=data or {},
        )
        self._task.history.append(entry)
        self._dirty = True

    def save_resume_point(
        self,
        agent: str,
        subagent: Optional[str],
        progress: str,
        next_step: str,
    ) -> None:
        """Persist a resume point. NOT allowed during EXECUTING."""
        self._assert_not_phase(Phase.EXECUTING)
        self._task_manager.save_resume_point(
            self._task.id, agent, subagent, progress, next_step
        )

    # ─── Disk I/O ─────────────────────────────────────────────────

    def persist(self) -> None:
        """Persist in-memory task to disk (atomic write).

        FORBIDDEN during EXECUTING phase — this is the primary race-condition
        prevention gate. Any code that tries to persist during execution will
        get an InvalidTransitionError instead of silently clobbering disk state.

        Uses tmpfile + os.replace for atomicity (no partial writes visible
        to concurrent readers such as the cron-orchestrator).
        """
        self._assert_not_phase(Phase.EXECUTING)

        if not self._dirty:
            return

        task_path = self._task_manager.get_task_path(self._task.id)
        if not task_path:
            logger.warning(
                f"No task path found for {self._task.id}, skipping persist"
            )
            return

        self._atomic_write(str(task_path))
        self._dirty = False
        logger.debug(f"SM persisted task {self._task.id} (phase={self.phase.value})")

    def _atomic_write(self, path: str) -> None:
        """Write task JSON atomically using tmpfile + os.replace.

        Guarantees that concurrent readers always see a complete file —
        never a partially-written one.
        """
        data = self._task.to_dict()
        dir_path = os.path.dirname(path)

        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ─── Guard properties ─────────────────────────────────────────

    @property
    def can_go_backward(self) -> bool:
        """True if backward transitions have not yet hit the hard limit."""
        # Import here to avoid circular import at module load time
        from loopd_core.agents.pipeline import _MAX_BACKWARD_TRANSITIONS
        return self.guards.backward_count < _MAX_BACKWARD_TRANSITIONS

    @property
    def review_backward_guard_active(self) -> bool:
        """True if the one-shot review-backward guard is currently set."""
        return self.guards.review_backward_pair is not None

    def record_agent_run(self, agent: str) -> None:
        """Update consecutive_same_agent counter after each iteration."""
        if agent == self.guards.last_agent:
            self.guards.consecutive_same_agent += 1
        else:
            self.guards.consecutive_same_agent = 0
            self.guards.last_agent = agent

    def record_backward_transition(self) -> None:
        """Increment the backward_count guard and sync to task context."""
        self.guards.backward_count += 1
        self._task.context.backward_transition_count = self.guards.backward_count
        self._dirty = True

    def set_review_backward_pair(self, pair: Optional[tuple[str, str]]) -> None:
        """Set or clear the one-shot review-backward guard."""
        self.guards.review_backward_pair = pair

    def reset_val_retry_count(self) -> None:
        """Reset per-agent validation retry counter when agent changes."""
        self.guards.val_retry_count = 0


# ─── Internal helpers ─────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
