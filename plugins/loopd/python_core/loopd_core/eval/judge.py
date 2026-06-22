"""JudgeRunner: single-task LLM-as-Judge evaluation."""
from __future__ import annotations
import json, logging, re, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from loopd_core.eval.errors import RETRY_BACKOFF_SECONDS, JudgeError, JudgeErrorType, MAX_RETRIES
from loopd_core.eval.schema import make_judgement
from loopd_core.eval.stats import mean, std

logger = logging.getLogger(__name__)
DEFAULT_JUDGE_MODEL = "claude-sonnet-4-6"
JUDGE_TIMEOUT_SECONDS = 300
_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_BARE_JSON_RE = re.compile(r"\{", re.DOTALL)

def _extract_json(text: str) -> Optional[dict]:
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try: return json.loads(m.group(1))
        except json.JSONDecodeError: pass
    for m in _BARE_JSON_RE.finditer(text):
        depth, start = 0, m.start()
        for i, ch in enumerate(text[start:], start):
            if ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try: return json.loads(text[start:i+1])
                    except json.JSONDecodeError: break
    return None

class JudgeConfig:
    def __init__(self, judge_model=DEFAULT_JUDGE_MODEL, timeout_seconds=JUDGE_TIMEOUT_SECONDS, project_root=None):
        self.judge_model = judge_model; self.timeout_seconds = timeout_seconds; self.project_root = project_root

class JudgeRunner:
    def __init__(self, config: JudgeConfig, llm_provider: Any):
        self.config = config; self.llm = llm_provider

    def judge_one(self, task_id, task_type, judge_input, rubric_prompt, prompt_hash, runs=1) -> dict:
        runs = max(1, runs)
        results = [self._judge_with_retry(task_id, task_type, judge_input, rubric_prompt, prompt_hash) for _ in range(runs)]
        if runs == 1: return results[0]
        scores = [r["overall_score"] for r in results]
        avg = mean(scores) or 0.0
        all_dims = set(k for r in results for k in r["dimension_scores"])
        avg_dims = {d: mean([r["dimension_scores"][d] for r in results if d in r["dimension_scores"]]) or 0.0 for d in all_dims}
        best = min(range(len(results)), key=lambda i: abs(results[i]["overall_score"] - avg))
        return make_judgement(task_id, avg, avg_dims, results[best]["reasoning"], self.config.judge_model, prompt_hash, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), task_type, runs, std(scores))

    def _judge_with_retry(self, task_id, task_type, judge_input, rubric_prompt, prompt_hash) -> dict:
        last = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                time.sleep(RETRY_BACKOFF_SECONDS[min(attempt-1, len(RETRY_BACKOFF_SECONDS)-1)])
            try:
                return self._call_judge(task_id, task_type, judge_input, rubric_prompt, prompt_hash)
            except JudgeError as e:
                last = e
                if e.error_type == JudgeErrorType.SCHEMA_ERROR: raise
                logger.warning(f"Judge attempt {attempt} failed for {task_id}: {e}")
        raise last or JudgeError("max retries exceeded", JudgeErrorType.UNKNOWN)

    def _call_judge(self, task_id, task_type, judge_input, rubric_prompt, prompt_hash) -> dict:
        resp = self.llm.run(prompt=f"{rubric_prompt}\n\n---\n\n## Task to Evaluate\n\n{judge_input}", task_id=f"{task_id}__judge", timeout=self.config.timeout_seconds, model=self.config.judge_model)
        if resp.is_rate_limited: raise JudgeError("rate limited", JudgeErrorType.RATE_LIMITED)
        if resp.is_timeout: raise JudgeError("timeout", JudgeErrorType.TIMEOUT)
        if resp.is_error or resp.exit_code != 0: raise JudgeError(f"LLM error exit={resp.exit_code}: {resp.stderr[:200]}", JudgeErrorType.LLM_ERROR)
        parsed = _extract_json(resp.content)
        if parsed is None: raise JudgeError(f"Could not extract JSON: {resp.content[:300]}", JudgeErrorType.PARSE_ERROR)
        try:
            return make_judgement(task_id, parsed.get("overall_score", 0), parsed.get("dimension_scores", {}), parsed.get("reasoning", ""), self.config.judge_model, prompt_hash, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), task_type)
        except (ValueError, TypeError) as e:
            raise JudgeError(str(e), JudgeErrorType.SCHEMA_ERROR) from e
