#!/usr/bin/env python3
"""Import FeatureBench instances as loopd eval task JSONs and set up workspaces.

Examples:
    # Import + setup all lite tasks
    python scripts/eval/feature_bench_import.py --split lite

    # Only seaborn tasks
    python scripts/eval/feature_bench_import.py --split lite --repo mwaskom/seaborn

    # Resume workspace setup
    python scripts/eval/feature_bench_import.py --setup-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))

from loopd_core.eval.feature_bench.importer import load_instances, iter_tasks, save_tasks
from loopd_core.eval.swe_bench.harness import setup_env, venv_exists

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "feature-bench"


def cmd_import(args: argparse.Namespace) -> None:
    pending = Path(args.eval_root) / "pending"
    instances = load_instances(split=args.split, limit=args.limit, repo=args.repo)
    tasks = list(iter_tasks(instances, id_prefix=args.prefix))
    save_tasks(tasks, pending)
    print(f"\n✅ {len(tasks)} task JSONs written to {pending}")


def cmd_setup(args: argparse.Namespace) -> None:
    pending = Path(args.eval_root) / "pending"
    task_files = sorted(pending.glob("fb-*.json"))

    if not task_files:
        print(f"No fb-*.json task files found in {pending}")
        sys.exit(1)

    try:
        from loopd_core.config import get_config
        from loopd_core.state.workspace_manager import WorkspaceManager
    except ImportError as e:
        print(f"loopd_core not importable: {e}")
        sys.exit(1)

    cfg = get_config()
    wm = WorkspaceManager(cfg)
    ok = failed = 0

    for task_file in task_files:
        task = json.loads(task_file.read_text(encoding="utf-8"))
        task_id = task["id"]
        ws = task.get("workspace") or {}
        repo = ws.get("repo")
        base_commit = ws.get("base_commit")
        workspace_path_str = ws.get("path")

        if not repo or not base_commit:
            logger.warning(f"Skipping {task_id}: missing repo or base_commit")
            continue

        # Skip if worktree already exists (e.g. task already completed/ran)
        if workspace_path_str and Path(workspace_path_str).exists():
            if venv_exists(Path(workspace_path_str)):
                logger.info(f"Already set up: {task_id}")
                ok += 1
                continue

        # Also skip if worktree path is null but workspace dir already exists by convention
        from loopd_core.config import get_config as _gc
        _cfg = _gc()
        expected_path = Path(_cfg.workspaces_path) / f"{task_id}--{repo.replace('/', '__')}"
        if expected_path.exists() and venv_exists(expected_path):
            logger.info(f"Worktree already exists, updating path: {task_id}")
            task["workspace"]["path"] = str(expected_path)
            task_file.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
            ok += 1
            continue

        print(f"\n⚙️  Setting up {task_id} ({repo} @ {base_commit[:10]}...)")

        try:
            worktree_path = wm.setup_task_workspace(
                task_id=task_id,
                repo=repo,
                base_branch=ws.get("branch", "main"),
                base_commit=base_commit,
            )

            python_exec = setup_env(worktree_path)

            task["workspace"]["path"] = str(worktree_path)
            task_file.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

            print(f"   ✅ Worktree: {worktree_path}")
            print(f"   ✅ Venv:     {python_exec}")
            ok += 1

        except Exception as e:
            logger.error(f"Failed to set up {task_id}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Setup complete — ✅ {ok} succeeded, ❌ {failed} failed")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import FeatureBench instances as loopd eval tasks")
    parser.add_argument("--split", default="lite", help="Dataset split: lite or full (default: lite)")
    parser.add_argument("--repo", default=None, help="Filter by repo (e.g. mwaskom/seaborn)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--prefix", default="fb")
    parser.add_argument("--eval-root", default=str(_DEFAULT_EVAL_ROOT))
    parser.add_argument("--setup", action="store_true", help="Also clone repos and create venvs after import")
    parser.add_argument("--setup-only", action="store_true", help="Only run workspace setup for existing task JSONs")

    args = parser.parse_args()

    if args.setup_only:
        cmd_setup(args)
    else:
        cmd_import(args)
        if args.setup:
            cmd_setup(args)


if __name__ == "__main__":
    main()
