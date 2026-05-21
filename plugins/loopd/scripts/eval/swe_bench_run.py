#!/usr/bin/env python3
"""Run SWE-bench tasks via the loopd implementation agent (headless claude -p).

For each pending task, invokes `claude -p` in the worktree with a focused
bug-fix prompt. Tasks run in parallel (default: 3 concurrent).

State directory (~/.loopd/swe-bench/):
  pending/    → tasks waiting to run
  active/     → currently running (or stuck if process died — auto-recovered at startup)
  completed/  → agent finished (go verify with swe_bench_verify.py)
  failed/     → claude -p error or timeout

Resume: Just re-run the script. Completed tasks are skipped automatically.
Stuck active/ tasks from a previous crash are moved back to pending/ at startup.

Examples:
    # Run all pending tasks (3 workers by default)
    python scripts/eval/swe_bench_run.py

    # Run only 50 at a time
    python scripts/eval/swe_bench_run.py --limit 50

    # Resume after interruption (auto-recovers active/, skips completed/)
    python scripts/eval/swe_bench_run.py --limit 50

    # Check current state without running
    python scripts/eval/swe_bench_run.py --status

    # Dry run
    python scripts/eval/swe_bench_run.py --dry-run --limit 10
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))

from loopd_core.eval.swe_bench.runner import run_task  # noqa: F401 (used in worker)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "swe-bench"


# ─────────────────────────────────────────────────────────────────────────────
# State helpers
# ─────────────────────────────────────────────────────────────────────────────


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dirs(eval_root: Path) -> dict[str, Path]:
    d = {
        "pending": eval_root / "pending",
        "active": eval_root / "active",
        "completed": eval_root / "completed",
        "failed": eval_root / "failed",
        "results": eval_root / "results",
    }
    for p in d.values():
        p.mkdir(parents=True, exist_ok=True)
    return d


def _load_task(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_task(task: dict, path: Path) -> None:
    path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")


def _move_to(src: Path, dst_dir: Path, status: str) -> Path:
    """Atomically update status and move task file to dst_dir."""
    task = _load_task(src)
    task["status"] = status
    task["updated_at"] = _now_utc()
    dst = dst_dir / src.name
    _write_task(task, dst)
    src.unlink(missing_ok=True)
    return dst


# ─────────────────────────────────────────────────────────────────────────────
# Recover stuck active/ tasks from a previous interrupted run
# ─────────────────────────────────────────────────────────────────────────────


def _recover_active(dirs: dict[str, Path]) -> int:
    """Move all tasks in active/ back to pending/ (they were interrupted)."""
    stuck = list(dirs["active"].glob("swe-*.json"))
    if not stuck:
        return 0
    for p in stuck:
        _move_to(p, dirs["pending"], "pending")
    logger.info(f"Recovered {len(stuck)} interrupted task(s) back to pending/")
    return len(stuck)


# ─────────────────────────────────────────────────────────────────────────────
# Task discovery & validation
# ─────────────────────────────────────────────────────────────────────────────


def _find_pending(dirs: dict[str, Path], task_id: str | None) -> list[Path]:
    if task_id:
        candidate = dirs["pending"] / f"{task_id}.json"
        return [candidate] if candidate.exists() else []
    return sorted(dirs["pending"].glob("swe-*.json"))


def _validate_task(task: dict) -> str | None:
    ws = task.get("workspace") or {}
    path = ws.get("path")
    if not path or not Path(path).is_dir():
        return f"workspace.path missing or not a directory: {path!r}"
    return None


def _status_summary(dirs: dict[str, Path]) -> dict[str, int]:
    return {
        name: len(list(d.glob("swe-*.json")))
        for name, d in dirs.items()
        if name != "results"
    }


# ─────────────────────────────────────────────────────────────────────────────
# Per-task worker — runs in a subprocess pool (must be a module-level function)
# ─────────────────────────────────────────────────────────────────────────────


def _run_one(
    task_file: str,
    eval_root: str,
    timeout: int,
    model: str | None,
    max_turns: int,
    claude_bin: str,
) -> dict:
    """Execute one SWE-bench task and update its status file."""
    import json
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))
    from loopd_core.eval.swe_bench.runner import run_task

    def now():
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    task_path = Path(task_file)
    root = Path(eval_root)
    active_dir = root / "active"
    completed_dir = root / "completed"
    failed_dir = root / "failed"
    for d in (active_dir, completed_dir, failed_dir):
        d.mkdir(parents=True, exist_ok=True)

    if not task_path.exists():
        return {"task_id": task_path.stem, "success": False, "duration_s": 0, "error": "file gone (race)", "returncode": -1}

    task = json.loads(task_path.read_text(encoding="utf-8"))
    task_id = task["id"]

    # pending → active (write first, then remove from pending)
    active_path = active_dir / task_path.name
    task["status"] = "active"
    task["updated_at"] = now()
    active_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    task_path.unlink(missing_ok=True)

    result = run_task(task, timeout=timeout, model=model, max_turns=max_turns, claude_bin=claude_bin)

    # active → completed / failed
    task["updated_at"] = now()
    if result.success:
        task["status"] = "completed"
        dst = completed_dir / active_path.name
    else:
        task["status"] = "failed"
        task["error_message"] = result.error or result.stderr[-500:] if result.stderr else result.error
        dst = failed_dir / active_path.name

    dst.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    active_path.unlink(missing_ok=True)

    return {
        "task_id": task_id,
        "success": result.success,
        "duration_s": round(result.duration_s, 1),
        "error": result.error,
        "returncode": result.returncode,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run SWE-bench tasks via loopd implementation agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--eval-root", default=str(_DEFAULT_EVAL_ROOT),
        help=f"Eval state root (default: {_DEFAULT_EVAL_ROOT})",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Max tasks to run this session (default: all pending)",
    )
    parser.add_argument(
        "--workers", type=int, default=3,
        help="Parallel claude -p processes (default: 3)",
    )
    parser.add_argument(
        "--timeout", type=int, default=900,
        help="Per-task timeout in seconds (default: 900)",
    )
    parser.add_argument(
        "--model", default=None,
        help="Claude model (e.g. claude-sonnet-4-6, claude-opus-4-7)",
    )
    parser.add_argument(
        "--max-turns", type=int, default=50,
        help="Max turns per claude -p run (default: 50)",
    )
    parser.add_argument(
        "--claude-bin", default="claude",
        help="Path to claude CLI binary (default: claude)",
    )
    parser.add_argument(
        "--task-id", default=None,
        help="Run a single specific task by ID",
    )
    parser.add_argument(
        "--no-recover", action="store_true",
        help="Skip auto-recovery of stuck active/ tasks (default: recover)",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print current state counts and exit",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show tasks that would run without executing",
    )

    args = parser.parse_args()
    eval_root = Path(args.eval_root)
    dirs = _dirs(eval_root)

    # ── status mode ──────────────────────────────────────────────────────────
    if args.status:
        summary = _status_summary(dirs)
        total = sum(summary.values())
        results_count = len(list(dirs["results"].glob("*.swe_verify.json")))
        resolved = sum(
            1 for p in dirs["results"].glob("*.swe_verify.json")
            if json.loads(p.read_text()).get("resolved")
        )
        print(f"\nSWE-bench eval state: {eval_root}")
        print(f"  pending:   {summary['pending']:4d}")
        print(f"  active:    {summary['active']:4d}  (stuck = was running when process died)")
        print(f"  completed: {summary['completed']:4d}")
        print(f"  failed:    {summary['failed']:4d}")
        print(f"  ─────────────────")
        print(f"  total:     {total:4d}")
        if results_count:
            print(f"\n  verified:  {results_count:4d}  ({resolved} resolved = {100*resolved/results_count:.1f}%)")
        return

    # ── auto-recover stuck active/ tasks ─────────────────────────────────────
    if not args.no_recover and not args.task_id:
        recovered = _recover_active(dirs)
        if recovered:
            print(f"↺  Recovered {recovered} interrupted task(s) → pending/")

    # ── discover pending tasks ────────────────────────────────────────────────
    pending = _find_pending(dirs, args.task_id)
    if not pending:
        summary = _status_summary(dirs)
        print(f"No pending tasks. completed={summary['completed']}, failed={summary['failed']}")
        print(f"Import more: python scripts/eval/swe_bench_import.py --limit 50")
        return

    # Filter unrunnable (no workspace) + apply --limit
    runnable: list[Path] = []
    skipped = 0
    for p in pending:
        if args.limit and len(runnable) >= args.limit:
            break
        task = _load_task(p)
        err = _validate_task(task)
        if err:
            logger.warning(f"Skip {task.get('id', p.stem)}: {err}")
            skipped += 1
        else:
            runnable.append(p)

    summary = _status_summary(dirs)
    print(
        f"\nState — pending:{summary['pending']}  active:{summary['active']}  "
        f"completed:{summary['completed']}  failed:{summary['failed']}"
    )
    print(f"This run — {len(runnable)} tasks ({skipped} skipped, {args.workers} workers)\n")

    if not runnable:
        print("No runnable tasks. Run swe_bench_import.py --setup first.")
        return

    # ── dry-run mode ──────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"DRY RUN — would run {len(runnable)} tasks:")
        for p in runnable[:20]:
            task = _load_task(p)
            ws = task.get("workspace") or {}
            print(f"  {task['id']:55s}  {ws.get('repo', '?')}")
        if len(runnable) > 20:
            print(f"  ... and {len(runnable) - 20} more")
        return

    # ── execute ───────────────────────────────────────────────────────────────
    ok = failed_count = 0
    start = time.monotonic()

    worker_kwargs = [
        {
            "task_file": str(p),
            "eval_root": str(eval_root),
            "timeout": args.timeout,
            "model": args.model,
            "max_turns": args.max_turns,
            "claude_bin": args.claude_bin,
        }
        for p in runnable
    ]

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(_run_one, **kw): kw["task_file"]
            for kw in worker_kwargs
        }
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                res = fut.result()
                icon = "✅" if res["success"] else "❌"
                status_str = (
                    f"rc={res['returncode']}"
                    if res["success"]
                    else (res.get("error") or "")[:60]
                )
                print(
                    f"[{done:3d}/{len(runnable)}] {icon} "
                    f"{res['task_id']:50s}  {res['duration_s']:.0f}s  {status_str}"
                )
                if res["success"]:
                    ok += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"Worker exception: {e}")
                failed_count += 1

    elapsed = time.monotonic() - start
    overall_summary = _status_summary(dirs)
    print(f"\n{'='*65}")
    print(f"This run: ✅ {ok}  ❌ {failed_count}  ({elapsed:.0f}s)")
    print(
        f"Overall — pending:{overall_summary['pending']}  "
        f"completed:{overall_summary['completed']}  "
        f"failed:{overall_summary['failed']}"
    )
    if overall_summary["pending"]:
        print(f"\nMore tasks remain. Resume with:")
        print(f"  python scripts/eval/swe_bench_run.py --limit {args.limit or 50}")
    else:
        print(f"\nAll tasks done. Verify results:")
        print(f"  python scripts/eval/swe_bench_verify.py")


if __name__ == "__main__":
    main()
