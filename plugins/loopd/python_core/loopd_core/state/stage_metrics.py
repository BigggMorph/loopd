"""
Stage-level quality metrics collection for the agent pipeline.

Tracks per-stage token usage, duration, and critic scores,
then emits a single stage.complete event via EventLogger.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from loopd_core.state.event_logger import EventLogger

logger = logging.getLogger(__name__)


def normalize_critic_scores(scores: dict[str, str]) -> Optional[float]:
    """Convert PASS/FAIL scores dict to 0.0–1.0 normalized score.

    Returns None if scores is empty.
    """
    if not scores:
        return None
    passed = sum(1 for v in scores.values() if v == "PASS")
    return round(passed / len(scores), 4)


class StageMetricsCollector:
    """
    In-process collector for stage-level pipeline metrics.

    Lifecycle per stage:
        1. stage_begin()       — record start time, reset accumulators
        2. accumulate_tokens() — called per turn during the stage
        3. set_critic_score()  — called after each critic evaluation (last write wins)
        4. stage_end()         — compute duration, emit stage.complete event

    All methods swallow exceptions so metric collection never breaks the pipeline.
    """

    def __init__(self) -> None:
        self._starts: dict[str, float] = {}
        self._tokens_in: dict[str, int] = {}
        self._tokens_out: dict[str, int] = {}
        self._critic_scores: dict[str, Optional[float]] = {}

    @staticmethod
    def _key(task_id: str, stage: str) -> str:
        return f"{task_id}:{stage}"

    def stage_begin(self, task_id: str, stage: str) -> None:
        try:
            key = self._key(task_id, stage)
            self._starts[key] = time.monotonic()
            self._tokens_in[key] = 0
            self._tokens_out[key] = 0
            self._critic_scores.pop(key, None)
        except Exception:
            logger.debug("stage_metrics: stage_begin failed", exc_info=True)

    def accumulate_tokens(
        self, task_id: str, stage: str, tokens_in: int, tokens_out: int
    ) -> None:
        try:
            key = self._key(task_id, stage)
            self._tokens_in[key] = self._tokens_in.get(key, 0) + tokens_in
            self._tokens_out[key] = self._tokens_out.get(key, 0) + tokens_out
        except Exception:
            logger.debug("stage_metrics: accumulate_tokens failed", exc_info=True)

    def set_critic_score(
        self, task_id: str, stage: str, score: Optional[float]
    ) -> None:
        try:
            self._critic_scores[self._key(task_id, stage)] = score
        except Exception:
            logger.debug("stage_metrics: set_critic_score failed", exc_info=True)

    def stage_end(
        self,
        task_id: str,
        stage: str,
        agent: str,
        success: bool,
        event_logger: "EventLogger",
    ) -> None:
        try:
            key = self._key(task_id, stage)
            start = self._starts.pop(key, None)
            duration_s = round(time.monotonic() - start, 3) if start is not None else 0.0
            tokens_in = self._tokens_in.pop(key, 0)
            tokens_out = self._tokens_out.pop(key, 0)
            critic_score = self._critic_scores.pop(key, None)

            event_logger.log_stage_completion(
                task_id=task_id,
                stage=stage,
                agent=agent,
                success=success,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                duration_s=duration_s,
                critic_score=critic_score,
            )
        except Exception:
            logger.debug("stage_metrics: stage_end failed", exc_info=True)
