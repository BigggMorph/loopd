"""
Status digest state persistence.

Tracks backoff phase, last sent time, and current Slack message reference
for the periodic waiting_human/failed task digest.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Backoff intervals by phase (seconds)
PHASE_INTERVALS: dict[str, int] = {
    "immediate": 0,      # send right away
    "reminder": 1800,    # 30 minutes
    "hourly": 7200,      # 2 hours
}

# Quiet hours in UTC (KST 23:00–08:00 = UTC 14:00–23:00)
QUIET_HOURS_START_UTC = 14
QUIET_HOURS_END_UTC = 23

# Escalation threshold: waiting > 2 hours triggers :rotating_light:
ESCALATION_THRESHOLD_SECONDS = 7200


@dataclass
class DigestState:
    """다이제스트 발송 상태. _queue/.status_digest_state.json 에 영속."""

    last_sent_at: Optional[str] = None       # ISO8601
    phase: str = "immediate"                  # immediate | reminder | hourly
    phase_started_at: Optional[str] = None   # ISO8601
    message_ts: Optional[str] = None         # Slack message timestamp (chat.update용)
    message_channel: Optional[str] = None    # Slack channel ID
    task_ids: list[str] = field(default_factory=list)
    send_count: int = 0

    # ── Persistence ──────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "DigestState":
        """Load from JSON file. Returns default state if file missing or corrupt."""
        if not path.exists():
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
            state = cls()
            state.last_sent_at = data.get("last_sent_at")
            state.phase = data.get("phase", "immediate")
            state.phase_started_at = data.get("phase_started_at")
            state.message_ts = data.get("message_ts")
            state.message_channel = data.get("message_channel")
            state.task_ids = data.get("task_ids", [])
            state.send_count = data.get("send_count", 0)
            return state
        except Exception:
            return cls()

    def save(self, path: Path) -> None:
        """Atomic write via temp file + rename."""
        tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                json.dump(asdict(self), f, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ── State Helpers ─────────────────────────────────────────────────

    def reset(self) -> None:
        """Clear all state (no pending digest)."""
        self.last_sent_at = None
        self.phase = "immediate"
        self.phase_started_at = None
        self.message_ts = None
        self.message_channel = None
        self.task_ids = []
        self.send_count = 0

    def task_set_changed(self, current_ids: list[str]) -> bool:
        """True if the actionable task set differs from the last digest."""
        return set(self.task_ids) != set(current_ids)

    def seconds_since_last_sent(self) -> Optional[float]:
        """Seconds elapsed since last digest send, or None if never sent."""
        if not self.last_sent_at:
            return None
        try:
            last = datetime.fromisoformat(self.last_sent_at.rstrip("Z")).replace(
                tzinfo=timezone.utc
            )
            return (datetime.now(timezone.utc) - last).total_seconds()
        except ValueError:
            return None

    def advance_phase(self) -> None:
        """Progress to the next backoff phase."""
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if self.phase == "immediate":
            self.phase = "reminder"
            self.phase_started_at = now_str
        elif self.phase == "reminder":
            self.phase = "hourly"
            self.phase_started_at = now_str
        # "hourly" stays as-is (already at maximum interval)
