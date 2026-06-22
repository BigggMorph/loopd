"""RubricLoader: load task_type rubric prompts and compute deterministic prompt hashes."""
from __future__ import annotations
import hashlib, logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
FALLBACK_TASK_TYPE = "dev"
TASK_TYPE_ALIAS: dict[str, str] = {"feature": "dev", "update": "dev"}

_COMMON_PREAMBLE = """\
# Post-hoc LLM-as-Judge Evaluation
You are an expert quality evaluator for autonomous AI agent tasks.
Score each dimension from 1 (very poor) to 5 (excellent).
Return ONLY valid JSON matching the schema below. No prose outside JSON.

Output schema:
{
  "overall_score": <float 1-5>,
  "dimension_scores": { "<dim>": <int 1-5>, ... },
  "reasoning": "<concise explanation, max 300 words>"
}
"""

def compute_prompt_hash(rubric_content: str) -> str:
    text = (_COMMON_PREAMBLE + "\n\n" + rubric_content).replace("\r\n", "\n").replace("\r", "\n")
    lines = [l.rstrip() for l in text.split("\n")]
    while lines and not lines[0]: lines.pop(0)
    while lines and not lines[-1]: lines.pop()
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]

class RubricLoader:
    def __init__(self, prompts_dir: Path):
        self.prompts_dir = Path(prompts_dir)

    def _load_raw(self, task_type: str) -> Optional[str]:
        path = self.prompts_dir / "eval" / f"judge_{task_type}.md"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def load(self, task_type: str) -> tuple[str, str]:
        resolved = TASK_TYPE_ALIAS.get(task_type, task_type)
        content = self._load_raw(resolved)
        if content is None:
            if resolved != FALLBACK_TASK_TYPE:
                logger.warning(f"rubric for {task_type!r} not found, falling back to {FALLBACK_TASK_TYPE!r}")
            content = self._load_raw(FALLBACK_TASK_TYPE)
            if content is None:
                raise FileNotFoundError(f"Rubric not found: {self.prompts_dir}/eval/judge_{FALLBACK_TASK_TYPE}.md")
        return _COMMON_PREAMBLE + "\n\n" + content, compute_prompt_hash(content)
