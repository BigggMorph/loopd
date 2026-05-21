#!/usr/bin/env python3
"""
Post-hoc LLM-as-Judge: batch quality scorer for archived loopd tasks.

Scans ~/.loopd/state/archive/ and ~/.loopd/state/completed/ for task JSONs,
judges each task that hasn't been judged yet, and writes results to
~/.loopd/artifacts/eval/judgements/<task_id>.json.

Usage:
    python scripts/eval/posthoc_judge_batch.py [options]

    python scripts/eval/posthoc_judge_batch.py --dry-run
    python scripts/eval/posthoc_judge_batch.py --window 7d --concurrency 3
    python scripts/eval/posthoc_judge_batch.py --rescore-on-prompt-change
    python scripts/eval/posthoc_judge_batch.py --task-type dev
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)

# Ensure loopd_core is importable when run from scripts/eval/
_PYTHON_CORE = Path(__file__).resolve().parent.parent.parent / "python_core"
if str(_PYTHON_CORE) not in sys.path:
    sys.path.insert(0, str(_PYTHON_CORE))

# Rubrics live at plugins/loopd/prompts/
_PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None


def _scan_dirs(dirs: list[Path], cutoff: Optional[datetime], task_type_filter: Optional[str]) -> list[dict]:
    tasks = []
    for d in dirs:
        if not d.exists():
            continue
        for path in sorted(d.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if cutoff is not None:
                created_at = _parse_dt(data.get("created_at"))
                if created_at is None or created_at < cutoff:
                    continue
            if task_type_filter and data.get("task_type") != task_type_filter:
                continue
            tasks.append(data)
    return tasks


def _build_events_index(events_dir: Path) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    if not events_dir.exists():
        return index
    for jsonl_path in sorted(events_dir.glob("*.jsonl")):
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        tid = ev.get("task_id")
                        if tid:
                            index.setdefault(tid, []).append(ev)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return index


def run(
    window: str = "all",
    task_type_filter: Optional[str] = None,
    model: str = "claude-sonnet-4-6",
    concurrency: int = 3,
    consistency_runs: int = 1,
    rescore_on_prompt_change: bool = False,
    max_cost_usd: Optional[float] = None,
    dry_run: bool = False,
) -> dict:
    from loopd_core.config import get_config
    from loopd_core.eval.batch import BatchRunner, estimate_cost
    from loopd_core.eval.events import log_judge_event, make_isolated_llm_provider
    from loopd_core.eval.input_assembler import InputAssembler
    from loopd_core.eval.judge import JudgeConfig, JudgeRunner
    from loopd_core.eval.rubric import RubricLoader
    from loopd_core.state.event_logger import EventLogger

    config = get_config()

    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=int(window.rstrip("d")))) if window != "all" else None

    # Scan both archive and completed directories
    scan_dirs = [config.queue_paths.archive, config.queue_paths.completed]
    tasks = _scan_dirs(scan_dirs, cutoff, task_type_filter)
    logger.info(f"Found {len(tasks)} tasks (window={window})")

    judgements_dir = config.artifacts_path / "eval" / "judgements"
    history_dir    = config.artifacts_path / "eval" / "_history"
    quarantine_dir = config.artifacts_path / "eval" / "_quarantine"

    rubric_loader = RubricLoader(_PROMPTS_DIR)
    assembler = InputAssembler(config.loopd_root)
    events_index = _build_events_index(config.events_path)

    llm = make_isolated_llm_provider(model)
    judge_config = JudgeConfig(judge_model=model)
    runner = JudgeRunner(config=judge_config, llm_provider=llm)
    event_logger = EventLogger(config)

    def judge_task(task: dict) -> dict:
        tid = task.get("id", "")
        task_type = task.get("task_type", "dev")
        events = events_index.get(tid, [])
        judge_input = assembler.assemble(task, events)
        rubric_prompt, prompt_hash = rubric_loader.load(task_type)
        judgement = runner.judge_one(
            task_id=tid, task_type=task_type,
            judge_input=judge_input, rubric_prompt=rubric_prompt,
            prompt_hash=prompt_hash, runs=consistency_runs,
        )
        log_judge_event(event_logger, "judged", tid, {
            "overall_score": judgement["overall_score"],
            "judge_model": model,
            "judge_prompt_hash": prompt_hash,
        })
        return judgement

    batch_runner = BatchRunner(
        project_root=config.loopd_root,
        judge_fn=judge_task,
        rubric_loader=rubric_loader,
        judgements_dir=judgements_dir,
        history_dir=history_dir,
        quarantine_dir=quarantine_dir,
        event_logger=event_logger,
        max_workers=concurrency,
        rescore_on_prompt_change=rescore_on_prompt_change,
        max_cost_usd=max_cost_usd,
        dry_run=dry_run,
        consistency_runs=consistency_runs,
        judge_model=model,
    )

    summary = batch_runner.run(tasks)
    return {
        "total": summary.total,
        "judged": summary.judged,
        "rescored": summary.rescored,
        "skipped": summary.skipped,
        "errors": summary.errors,
        "quarantined": summary.quarantined,
        "cost_usd_estimated": round(summary.cost_usd_estimated, 4),
        "dry_run": dry_run,
        "window": window,
        "model": model,
    }


def main() -> None:
    if os.environ.get("LOOPD_JUDGE_ENABLED", "true").lower() in ("0", "false", "no"):
        logger.info("LOOPD_JUDGE_ENABLED=false — judge batch skipped")
        sys.exit(0)

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", default="all", metavar="WINDOW", help="Time window: all | 7d | 30d | Nd (default: all)")
    parser.add_argument("--task-type", default=None, metavar="TYPE", help="Filter by task_type: dev|research|query|followup")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Judge model ID (default: claude-sonnet-4-6)")
    parser.add_argument("--concurrency", type=int, default=3, help="Parallel workers (default: 3)")
    parser.add_argument("--runs", type=int, default=1, dest="consistency_runs", help="Self-consistency runs per task (default: 1)")
    parser.add_argument("--rescore-on-prompt-change", action="store_true", help="Re-judge tasks whose rubric prompt hash has changed")
    parser.add_argument("--max-cost", type=float, default=None, dest="max_cost_usd", metavar="USD", help="Abort if estimated cost exceeds this USD amount")
    parser.add_argument("--dry-run", action="store_true", help="Plan the batch and estimate cost without calling the judge LLM")
    args = parser.parse_args()

    try:
        result = run(
            window=args.window,
            task_type_filter=args.task_type,
            model=args.model,
            concurrency=args.concurrency,
            consistency_runs=args.consistency_runs,
            rescore_on_prompt_change=args.rescore_on_prompt_change,
            max_cost_usd=args.max_cost_usd,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2))
    except Exception as e:
        logger.error(f"Batch failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
