"""loopd-core — deterministic FSM driver for the loopd Claude Code Plugin."""

__version__ = "0.1.0"

from loopd_core.config import Config, get_config
from loopd_core.types import (
    Task,
    TaskStatus,
    TaskSource,
    TaskState,
    HistoryEntry,
    ResumePoint,
    AgentType,
    SubagentType,
)

__all__ = [
    "Config",
    "get_config",
    "Task",
    "TaskStatus",
    "TaskSource",
    "TaskState",
    "HistoryEntry",
    "ResumePoint",
    "AgentType",
    "SubagentType",
]
