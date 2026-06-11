"""Convert FeatureBench HuggingFace instances to loopd eval task JSONs."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_HF_DATASET = "LiberCoders/FeatureBench"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_instances(split: str = "lite", limit: Optional[int] = None, repo: Optional[str] = None) -> list[dict]:
    try:
        from datasets import load_dataset
    except ImportError:
        raise ImportError("pip install datasets")

    logger.info(f"Loading {_HF_DATASET} split={split} ...")
    ds = load_dataset(_HF_DATASET, split=split)
    instances = list(ds)
    if repo:
        instances = [i for i in instances if i["repo"] == repo]
    if limit:
        instances = instances[:limit]
    logger.info(f"Loaded {len(instances)} instances")
    return instances


def instance_to_task(instance: dict, task_id: str) -> dict:
    repo = instance["repo"]
    base_commit = instance["base_commit"]
    problem = instance["problem_statement"]

    fail_to_pass = list(instance.get("FAIL_TO_PASS", []))
    pass_to_pass = list(instance.get("PASS_TO_PASS", []))

    # parse repo_settings for install command
    repo_settings = instance.get("repo_settings", "{}")
    if isinstance(repo_settings, str):
        try:
            repo_settings = json.loads(repo_settings)
        except Exception:
            repo_settings = {}
    install_cmd = repo_settings.get("install", "pip install -e .")

    now = _now_utc()
    return {
        "id": task_id,
        "task_type": "feature_bench",
        "status": "pending",
        "prompt": problem,
        "title": f"FeatureBench: {instance.get('instance_id', task_id)}",
        "level": 0,
        "priority": 3,
        "workspace": {
            "repo": repo,
            "branch": "main",
            "base_commit": base_commit,
            "path": None,
        },
        "metadata": {
            "feature_bench": {
                "instance_id": instance["instance_id"],
                "fail_to_pass": fail_to_pass,
                "pass_to_pass": pass_to_pass,
                "test_patch": instance.get("test_patch", ""),
                "install_cmd": install_cmd,
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


def iter_tasks(instances: list[dict], id_prefix: str = "fb") -> Iterator[tuple[str, dict]]:
    for i, instance in enumerate(instances, start=1):
        instance_id = instance.get("instance_id", f"inst-{i}")
        safe_id = instance_id.replace("/", "-").replace(".", "-")
        task_id = f"{id_prefix}-{safe_id}"
        yield task_id, instance_to_task(instance, task_id)


def save_tasks(tasks: list[tuple[str, dict]], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for task_id, task_json in tasks:
        path = output_dir / f"{task_id}.json"
        if not path.exists():
            path.write_text(json.dumps(task_json, indent=2, ensure_ascii=False), encoding="utf-8")
            written.append(path)
        else:
            logger.info(f"Already exists, skipping: {task_id}")
    logger.info(f"Saved {len(written)} task files to {output_dir}")
    return written
