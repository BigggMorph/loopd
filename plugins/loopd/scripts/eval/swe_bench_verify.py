#!/usr/bin/env python3
"""Verify completed SWE-bench tasks by applying test_patch and running pytest.

For each completed task, this script:
  1. Reads workspace.path + metadata.swe_bench from the task JSON
  2. Applies test_patch on top of the agent's changes (git apply)
  3. Runs pytest for FAIL_TO_PASS + PASS_TO_PASS tests in the worktree venv
  4. Writes a .swe_verify.json result file to ~/.loopd/swe-bench/results/

SWE-bench scoring: resolved = True iff
  ALL FAIL_TO_PASS tests now pass AND ALL PASS_TO_PASS tests still pass.

Examples:
    # Verify all completed tasks
    python scripts/eval/swe_bench_verify.py

    # Verify a single task
    python scripts/eval/swe_bench_verify.py --task-id swe-django__django-11099

    # Verbose: print per-test outcomes
    python scripts/eval/swe_bench_verify.py --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))

from loopd_core.eval.swe_bench.verifier import SWEBenchResult, verify

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "swe-bench"


def _find_completed(eval_root: Path, task_id: Optional[str]) -> list[Path]:
    completed_dir = eval_root / "completed"
    if task_id:
        for subdir in ("completed", "active", "pending", "failed"):
            candidate = eval_root / subdir / f"{task_id}.json"
            if candidate.exists():
                return [candidate]
        flat = eval_root / f"{task_id}.json"
        if flat.exists():
            return [flat]
        print(f"Task not found: {task_id}")
        return []
    return sorted(completed_dir.glob("swe-*.json"))


def verify_task(task_path: Path, results_dir: Path, verbose: bool = False) -> Optional[SWEBenchResult]:
    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_id = task.get("id", task_path.stem)

    result_path = results_dir / f"{task_id}.swe_verify.json"
    if result_path.exists():
        logger.info(f"{task_id}: already verified, skipping")
        cached = json.loads(result_path.read_text(encoding="utf-8"))
        return SWEBenchResult(**cached)

    ws = task.get("workspace") or {}
    workspace_path_str = ws.get("path")
    if not workspace_path_str:
        logger.warning(f"{task_id}: workspace.path not set")
        return None

    workspace_path = Path(workspace_path_str)
    if not workspace_path.is_dir():
        logger.warning(f"{task_id}: worktree not found at {workspace_path}")
        return None

    meta = (task.get("metadata") or {}).get("swe_bench")
    if not meta:
        logger.warning(f"{task_id}: no metadata.swe_bench")
        return None

    test_patch = meta.get("test_patch", "")
    fail_to_pass = meta.get("fail_to_pass", [])
    pass_to_pass = meta.get("pass_to_pass", [])

    print(f"\n🔍 Verifying {task_id}")
    print(f"   FAIL_TO_PASS: {len(fail_to_pass)} tests")
    print(f"   PASS_TO_PASS: {len(pass_to_pass)} tests")

    result = verify(
        worktree_path=workspace_path,
        test_patch=test_patch,
        fail_to_pass=fail_to_pass,
        pass_to_pass=pass_to_pass,
    )

    icon = "✅" if result.resolved else "❌"
    print(f"   {icon} resolved={result.resolved}")
    if result.error:
        print(f"   ⚠️  error: {result.error}")

    if verbose:
        if result.fail_to_pass_results:
            print("   FAIL_TO_PASS results:")
            for t, passed in result.fail_to_pass_results.items():
                print(f"     {'✅' if passed else '❌'} {t}")
        if result.pass_to_pass_results:
            print("   PASS_TO_PASS results:")
            for t, passed in result.pass_to_pass_results.items():
                print(f"     {'✅' if passed else '❌'} {t}")

    results_dir.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify completed SWE-bench tasks via test_patch + pytest",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--eval-root", default=str(_DEFAULT_EVAL_ROOT),
        help=f"Eval state root directory (default: {_DEFAULT_EVAL_ROOT})",
    )
    parser.add_argument(
        "--task-id", default=None,
        help="Verify a single task by ID",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print per-test results",
    )

    args = parser.parse_args()
    eval_root = Path(args.eval_root)
    results_dir = eval_root / "results"

    task_files = _find_completed(eval_root, args.task_id)
    if not task_files:
        print(f"No completed SWE-bench tasks found in {eval_root / 'completed'}")
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
        print(f"SWE-bench result: {resolved_count}/{total} resolved ({pct:.1f}%)")
        print(f"(Baseline — Claude Sonnet 4.5: 49.0%, o3: 71.7%)")


if __name__ == "__main__":
    main()
