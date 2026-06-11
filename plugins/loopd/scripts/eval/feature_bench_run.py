#!/usr/bin/env python3
"""Run FeatureBench tasks via the loopd implementation agent (headless claude -p).

Examples:
    python scripts/eval/feature_bench_run.py
    python scripts/eval/feature_bench_run.py --workers 2 --timeout 1800
    python scripts/eval/feature_bench_run.py --task-id fb-mwaskom__seaborn-...
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

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_DEFAULT_EVAL_ROOT = Path.home() / ".loopd" / "feature-bench"


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


def _recover_active(dirs: dict[str, Path]) -> int:
    stuck = list(dirs["active"].glob("fb-*.json"))
    for p in stuck:
        task = _load_task(p)
        task["status"] = "pending"
        task["updated_at"] = _now_utc()
        dst = dirs["pending"] / p.name
        _write_task(task, dst)
        p.unlink(missing_ok=True)
    if stuck:
        logger.info(f"Recovered {len(stuck)} interrupted task(s) back to pending/")
    return len(stuck)


def _find_pending(dirs: dict[str, Path], task_id: str | None) -> list[Path]:
    if task_id:
        candidate = dirs["pending"] / f"{task_id}.json"
        return [candidate] if candidate.exists() else []
    return sorted(dirs["pending"].glob("fb-*.json"))


def _validate_task(task: dict) -> str | None:
    ws = task.get("workspace") or {}
    path = ws.get("path")
    if not path or not Path(path).is_dir():
        return f"workspace.path missing or not a directory: {path!r}"
    return None


def _status_summary(dirs: dict[str, Path]) -> dict[str, int]:
    return {
        name: len(list(d.glob("fb-*.json")))
        for name, d in dirs.items()
        if name != "results"
    }


def _run_one(task_file: str, eval_root: str, timeout: int, model: str | None, max_turns: int, claude_bin: str) -> dict:
    import json
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "python_core"))
    from loopd_core.eval.feature_bench.runner import run_task

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

    active_path = active_dir / task_path.name
    task["status"] = "active"
    task["updated_at"] = now()
    active_path.write_text(json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")
    task_path.unlink(missing_ok=True)

    result = run_task(task, timeout=timeout, model=model, max_turns=max_turns, claude_bin=claude_bin)

    task["updated_at"] = now()
    if result.success:
        task["status"] = "completed"
        dst = completed_dir / active_path.name
    else:
        task["status"] = "failed"
        task["error_message"] = result.error or (result.stderr[-500:] if result.stderr else result.error)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run FeatureBench tasks via loopd implementation agent")
    parser.add_argument("--eval-root", default=str(_DEFAULT_EVAL_ROOT))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-turns", type=int, default=50)
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--task-id", default=None)
    parser.add_argument("--no-recover", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    eval_root = Path(args.eval_root)
    dirs = _dirs(eval_root)

    if args.status:
        summary = _status_summary(dirs)
        results_count = len(list(dirs["results"].glob("*.fb_verify.json")))
        resolved = sum(1 for p in dirs["results"].glob("*.fb_verify.json") if json.loads(p.read_text()).get("resolved"))
        print(f"\nFeatureBench eval state: {eval_root}")
        for k, v in summary.items():
            print(f"  {k:10s}: {v:4d}")
        if results_count:
            print(f"\n  verified: {results_count}  ({resolved} resolved = {100*resolved/results_count:.1f}%)")
        return

    if not args.no_recover and not args.task_id:
        recovered = _recover_active(dirs)
        if recovered:
            print(f"↺  Recovered {recovered} interrupted task(s) → pending/")

    pending = _find_pending(dirs, args.task_id)
    if not pending:
        summary = _status_summary(dirs)
        print(f"No pending tasks. completed={summary['completed']}, failed={summary['failed']}")
        return

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
    print(f"\nState — pending:{summary['pending']}  active:{summary['active']}  completed:{summary['completed']}  failed:{summary['failed']}")
    print(f"This run — {len(runnable)} tasks ({skipped} skipped, {args.workers} workers)\n")

    if not runnable:
        print("No runnable tasks. Run feature_bench_import.py --setup first.")
        return

    if args.dry_run:
        for p in runnable:
            task = _load_task(p)
            print(f"  {task['id']}")
        return

    ok = failed_count = 0
    start = time.monotonic()

    worker_kwargs = [
        {"task_file": str(p), "eval_root": str(eval_root), "timeout": args.timeout,
         "model": args.model, "max_turns": args.max_turns, "claude_bin": args.claude_bin}
        for p in runnable
    ]

    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_run_one, **kw): kw["task_file"] for kw in worker_kwargs}
        done = 0
        for fut in as_completed(futures):
            done += 1
            try:
                res = fut.result()
                icon = "✅" if res["success"] else "❌"
                status_str = f"rc={res['returncode']}" if res["success"] else (res.get("error") or "")[:60]
                print(f"[{done:3d}/{len(runnable)}] {icon} {res['task_id']:60s}  {res['duration_s']:.0f}s  {status_str}")
                if res["success"]:
                    ok += 1
                else:
                    failed_count += 1
            except Exception as e:
                logger.error(f"Worker exception: {e}")
                failed_count += 1

    elapsed = time.monotonic() - start
    print(f"\n{'='*65}")
    print(f"This run: ✅ {ok}  ❌ {failed_count}  ({elapsed:.0f}s)")
    print(f"\nVerify results:")
    print(f"  python scripts/eval/feature_bench_verify.py")


if __name__ == "__main__":
    main()
