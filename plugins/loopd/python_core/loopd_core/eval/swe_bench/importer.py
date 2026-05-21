"""Convert SWE-bench Verified HuggingFace instances to loopd eval task JSONs.

Task JSON format mirrors loopd's Task model but is stored in a separate
eval state directory (~/.loopd/swe-bench/) to avoid polluting the main queue.

Key fields:
  task_type  = "swe_bench"
  workspace.repo        = "owner/repo"
  workspace.base_commit = "<sha>"
  metadata.swe_bench    = {instance_id, fail_to_pass, pass_to_pass, test_patch}
  metadata.swe_bench._patch_reference  ← ground truth, never shown to agent
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_HF_DATASET = "princeton-nlp/SWE-bench_Verified"
_HF_SPLIT = "test"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_instances(limit: Optional[int] = None) -> list[dict]:
    """Load SWE-bench Verified instances from HuggingFace. Requires `datasets`."""
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        raise ImportError("Install the HuggingFace datasets library: pip install datasets")

    logger.info(f"Loading {_HF_DATASET} split={_HF_SPLIT} ...")
    ds = load_dataset(_HF_DATASET, split=_HF_SPLIT)
    instances = list(ds)
    if limit:
        instances = instances[:limit]
    logger.info(f"Loaded {len(instances)} instances")
    return instances


def instance_to_task(instance: dict, task_id: str) -> dict:
    """Convert one HuggingFace instance dict to a loopd eval task JSON dict."""
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    problem = instance["problem_statement"]

    fail_to_pass = instance.get("FAIL_TO_PASS", [])
    pass_to_pass = instance.get("PASS_TO_PASS", [])
    if isinstance(fail_to_pass, str):
        fail_to_pass = json.loads(fail_to_pass)
    if isinstance(pass_to_pass, str):
        pass_to_pass = json.loads(pass_to_pass)

    now = _now_utc()
    return {
        "id": task_id,
        "task_type": "swe_bench",
        "status": "pending",
        "prompt": problem,
        "title": f"SWE-bench: {instance.get('instance_id', task_id)}",
        "level": 0,
        "priority": 3,
        "workspace": {
            "repo": repo,
            "branch": "main",
            "base_commit": base_commit,
            "path": None,
        },
        "metadata": {
            "swe_bench": {
                "instance_id": instance["instance_id"],
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "test_patch": instance.get("test_patch", ""),
                "_patch_reference": instance.get("patch", ""),
            }
        },
        "turns": [],
        "history": [],
        "artifacts": [],
        "depends_on": [],
        "created_at": now,
        "updated_at": now,
    }


def iter_tasks(
    instances: list[dict],
    id_prefix: str = "swe",
) -> Iterator[tuple[str, dict]]:
    """Yield (task_id, task_json) pairs."""
    for i, instance in enumerate(instances, start=1):
        instance_id = instance.get("instance_id", f"inst-{i}")
        safe_id = instance_id.replace("/", "-").replace(".", "-")
        task_id = f"{id_prefix}-{safe_id}"
        yield task_id, instance_to_task(instance, task_id)


def save_tasks(tasks: list[tuple[str, dict]], output_dir: Path) -> list[Path]:
    """Write task JSON files to output_dir. Returns list of written paths."""
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for task_id, task_json in tasks:
        path = output_dir / f"{task_id}.json"
        path.write_text(json.dumps(task_json, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path)
    logger.info(f"Saved {len(written)} task files to {output_dir}")
    return written
