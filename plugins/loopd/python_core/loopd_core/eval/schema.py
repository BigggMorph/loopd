"""Judgement JSON schema definition and validator."""
from __future__ import annotations
from typing import Any

JUDGEMENT_SCHEMA_VERSION = 1
SCORE_MIN, SCORE_MAX = 1, 5

class JudgementValidationError(ValueError):
    pass

def validate_judgement(data: Any) -> dict:
    if not isinstance(data, dict):
        raise JudgementValidationError(f"judgement must be a dict, got {type(data).__name__}")
    for field in ("task_id", "overall_score", "dimension_scores", "reasoning", "judge_model", "judge_prompt_hash", "judged_at"):
        if field not in data:
            raise JudgementValidationError(f"missing required field: {field!r}")
    overall = data["overall_score"]
    if not isinstance(overall, (int, float)) or not (SCORE_MIN <= overall <= SCORE_MAX):
        raise JudgementValidationError(f"overall_score {overall} invalid")
    dim = data["dimension_scores"]
    if not isinstance(dim, dict):
        raise JudgementValidationError("dimension_scores must be a dict")
    for k, v in dim.items():
        if not isinstance(v, (int, float)) or not (SCORE_MIN <= v <= SCORE_MAX):
            raise JudgementValidationError(f"dimension_scores[{k!r}]={v} invalid")
    if not isinstance(data["reasoning"], str) or not data["reasoning"].strip():
        raise JudgementValidationError("reasoning must be non-empty string")
    if not isinstance(data["judge_model"], str) or not data["judge_model"].strip():
        raise JudgementValidationError("judge_model must be non-empty string")
    ph = data["judge_prompt_hash"]
    if not isinstance(ph, str) or len(ph) != 16:
        raise JudgementValidationError(f"judge_prompt_hash must be 16-char hex, got {ph!r}")
    if not isinstance(data["judged_at"], str) or not data["judged_at"]:
        raise JudgementValidationError("judged_at must be non-empty string")
    return data

def make_judgement(task_id, overall_score, dimension_scores, reasoning, judge_model, judge_prompt_hash, judged_at, task_type="unknown", self_consistency_runs=1, self_consistency_std=None) -> dict:
    obj = {
        "schema_version": JUDGEMENT_SCHEMA_VERSION,
        "task_id": task_id,
        "task_type": task_type,
        "overall_score": round(float(overall_score), 3),
        "dimension_scores": {k: round(float(v), 3) for k, v in dimension_scores.items()},
        "reasoning": reasoning,
        "judge_model": judge_model,
        "judge_prompt_hash": judge_prompt_hash,
        "judged_at": judged_at,
        "self_consistency_runs": self_consistency_runs,
        "self_consistency_std": self_consistency_std,
    }
    validate_judgement(obj)
    return obj
