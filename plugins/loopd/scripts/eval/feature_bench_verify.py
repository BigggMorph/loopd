#!/usr/bin/env python3
"""Verify completed FeatureBench tasks by restoring test files and running pytest.

Examples:
    python scripts/eval/feature_bench_verify.py
    python scripts/eval/feature_bench_verify.py --task-id fb-mwaskom__seaborn-...
    python scripts/eval/feature_bench_verify.py --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))

from loopd_core.eval.feature_bench.verifier import FBResult, verify

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "feature-bench"


def _find_completed(eval_root: Path, task_id: Optional[str]) -> list[Path]:
    completed_dir = eval_root / "completed"
    if task_id:
        for subdir in ("completed", "active", "pending", "failed"):
            candidate = eval_root / subdir / f"{task_id}.json"
            if candidate.exists():
                return [candidate]
        print(f"Task not found: {task_id}")
        return []
    return sorted(completed_dir.glob("fb-*.json"))


def verify_task(task_path: Path, results_dir: Path, verbose: bool = False) -> Optional[FBResult]:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_id = task.get("id", task_path.stem)

    result_path = results_dir / f"{task_id}.fb_verify.json"
    if result_path.exists():
        logger.info(f"{task_id}: already verified, skipping")
        cached = json.loads(result_path.read_text(encoding="utf-8"))
        return FBResult(**cached)

    ws = task.get("workspace") or {}
    workspace_path_str = ws.get("path")
    if not workspace_path_str:
        logger.warning(f"{task_id}: workspace.path not set")
        return None

    workspace_path = Path(workspace_path_str)
    if not workspace_path.is_dir():
        logger.warning(f"{task_id}: worktree not found at {workspace_path}")
        return None

    meta = (task.get("metadata") or {}).get("feature_bench")
    if not meta:
        logger.warning(f"{task_id}: no metadata.feature_bench")
        return None

    fail_to_pass = meta.get("fail_to_pass", [])
    pass_to_pass = meta.get("pass_to_pass", [])

    print(f"\n🔍 Verifying {task_id}")
    print(f"   FAIL_TO_PASS: {fail_to_pass}")
    print(f"   PASS_TO_PASS: {len(pass_to_pass)} files")

    result = verify(
        worktree_path=workspace_path,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
    )

    icon = "✅" if result.resolved else "❌"
    print(f"   {icon} resolved={result.resolved}")
    if result.error:
        print(f"   ⚠️  error: {result.error}")

    if verbose:
        print("   FTP results:")
        for f, passed in result.fail_to_pass_results.items():
            print(f"     {'✅' if passed else '❌'} {f}")
        print("   PTP results:")
        for f, passed in result.pass_to_pass_results.items():
            print(f"     {'✅' if passed else '❌'} {f}")

    results_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify completed FeatureBench tasks")
    parser.add_argument("--eval-root", default=str(_DEFAULT_EVAL_ROOT))
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()
    eval_root = Path(args.eval_root)
    results_dir = eval_root / "results"

    task_files = _find_completed(eval_root, args.task_id)
    if not task_files:
        print(f"No completed FeatureBench tasks found in {eval_root / 'completed'}")
        sys.exit(1)

    print(f"Verifying {len(task_files)} task(s) ...")

    resolved_count = total = 0
    for task_file in task_files:
        result = verify_task(task_file, results_dir, verbose=args.verbose)
        if result is not None:
            total += 1
            if result.resolved:
                resolved_count += 1

    if total > 0:
        pct = 100 * resolved_count / total
        print(f"\n{'='*60}")
        print(f"FeatureBench result: {resolved_count}/{total} resolved ({pct:.1f}%)")
        print(f"(Baseline — OpenHands + Claude Opus 4.7 Lite: 46.7%)")


if __name__ == "__main__":
    main()
