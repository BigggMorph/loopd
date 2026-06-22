#!/usr/bin/env python3
"""Import SWE-bench Verified instances as loopd eval task JSONs.

Two modes:
  import-only   Write task JSON files to the eval pending dir.
                No git cloning yet — workspace.path stays null.

  setup         After importing, clone each repo at base_commit and
                create a Python venv inside the worktree.
                This is the slow step; run once and reuse.

Default eval directory: ~/.loopd/swe-bench/

Examples:
    # Import all 500 instances
    python scripts/eval/swe_bench_import.py

    # Import 20 instances only
    python scripts/eval/swe_bench_import.py --limit 20

    # Import + full workspace setup (clone + venv)
    python scripts/eval/swe_bench_import.py --limit 5 --setup

    # Resume workspace setup for already-imported tasks
    python scripts/eval/swe_bench_import.py --setup-only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))

from loopd_core.eval.swe_bench.harness import setup_env, venv_exists
from loopd_core.eval.swe_bench.importer import load_instances, iter_tasks, save_tasks

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "swe-bench"


def _pending_dir(eval_root: Path) -> Path:
    return eval_root / "pending"


def cmd_import(args: argparse.Namespace) -> None:
    pending = _pending_dir(Path(args.eval_root))
    instances = load_instances(limit=args.limit)
    tasks = list(iter_tasks(instances, id_prefix=args.prefix))
    save_tasks(tasks, pending)
    print(f"\n✅ {len(tasks)} task JSONs written to {pending}")
    print("Next — run with --setup to clone repos and create venvs:")
    print(f"  python {__file__} --setup-only\n")


def cmd_setup(args: argparse.Namespace) -> None:
    """Clone repos at base_commit and set up isolated Python venvs."""
    pending = _pending_dir(Path(args.eval_root))
    task_files = sorted(pending.glob("swe-*.json"))

    if not task_files:
        print(f"No swe-*.json task files found in {pending}")
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

        if workspace_path_str and Path(workspace_path_str).exists():
            if venv_exists(Path(workspace_path_str)):
                logger.info(f"Already set up: {task_id}")
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
            task_file.write_text(
                json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8"
            )

            print(f"   ✅ Worktree: {worktree_path}")
            print(f"   ✅ Venv:     {python_exec}")
            ok += 1

        except Exception as e:
            logger.error(f"Failed to set up {task_id}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Setup complete — ✅ {ok} succeeded, ❌ {failed} failed")
    if ok > 0:
        print(f"\nRun tasks:")
        print(f"  python scripts/eval/swe_bench_run.py")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Import SWE-bench Verified instances as loopd eval task JSONs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max instances to import (default: all 500)",
    )
    parser.add_argument(
        "--prefix", default="swe",
        help="Task ID prefix (default: swe)",
    )
    parser.add_argument(
        "--eval-root", default=str(_DEFAULT_EVAL_ROOT),
        help=f"Eval state root directory (default: {_DEFAULT_EVAL_ROOT})",
    )
    parser.add_argument(
        "--setup", action="store_true",
        help="After importing, also clone repos and create venvs",
    )
    parser.add_argument(
        "--setup-only", action="store_true",
        help="Skip import; only run workspace+venv setup for existing task JSONs",
    )

    args = parser.parse_args()

    if args.setup_only:
        cmd_setup(args)
    else:
        cmd_import(args)
        if args.setup:
            cmd_setup(args)


if __name__ == "__main__":
    main()
