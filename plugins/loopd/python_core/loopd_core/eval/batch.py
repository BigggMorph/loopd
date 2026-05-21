"""BatchRunner: parallel post-hoc judge evaluation over archived tasks."""
from __future__ import annotations
import json, logging, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from loopd_core.eval.errors import QUARANTINE_AFTER_N_ERRORS
from loopd_core.eval.io import archive_to_history, atomic_write_json, cleanup_stale_tempfiles
from loopd_core.eval.judge import DEFAULT_JUDGE_MODEL

logger = logging.getLogger(__name__)
_MODEL_COST: dict[str, float] = {"claude-opus-4-7": 15e-6, "claude-sonnet-4-6": 3e-6, "claude-haiku-4-5": 0.8e-6}

class SkipReason(str, Enum):
    NONE = "none"; ALREADY_JUDGED = "already_judged"; PROMPT_HASH_CHANGED = "prompt_hash_changed_no_rescore"; QUARANTINED = "quarantined"

@dataclass
class TaskPlan:
    task: dict; action: str; skip_reason: SkipReason = SkipReason.NONE; existing_judgement: Optional[dict] = None

@dataclass
class BatchResult:
    task_id: str; action: str; success: bool; judgement: Optional[dict] = None; error: Optional[str] = None; duration_s: float = 0.0

@dataclass
class BatchSummary:
    total: int = 0; judged: int = 0; skipped: int = 0; rescored: int = 0; errors: int = 0; quarantined: int = 0; cost_usd_estimated: float = 0.0; results: list[BatchResult] = field(default_factory=list)

def _rj(path):
    try: return json.loads(Path(path).read_text(encoding="utf-8"))
    except: return None

def _err_count(qdir, tid):
    d = _rj(Path(qdir) / f"{tid}.errors.json"); return int(d.get("consecutive_errors", 0)) if d else 0

def _save_err(qdir, tid, count, msg):
    Path(qdir).mkdir(parents=True, exist_ok=True)
    atomic_write_json(Path(qdir) / f"{tid}.errors.json", {"task_id": tid, "consecutive_errors": count, "last_error": msg})

def _reset_err(qdir, tid):
    (Path(qdir) / f"{tid}.errors.json").unlink(missing_ok=True)

def estimate_cost(tasks, model="claude-sonnet-4-6", tpt=50_000):
    return len(tasks) * tpt * _MODEL_COST.get(model, 3e-6)

class BatchRunner:
    def __init__(self, project_root, judge_fn, rubric_loader, judgements_dir, history_dir, quarantine_dir, event_logger, max_workers=3, rescore_on_prompt_change=False, max_cost_usd=None, dry_run=False, consistency_runs=1, judge_model=DEFAULT_JUDGE_MODEL):
        self.project_root = project_root; self.judge_fn = judge_fn; self.rubric_loader = rubric_loader
        self.judgements_dir = Path(judgements_dir); self.history_dir = Path(history_dir); self.quarantine_dir = Path(quarantine_dir)
        self.event_logger = event_logger; self.judge_model = judge_model; self.max_workers = max_workers
        self.rescore_on_prompt_change = rescore_on_prompt_change; self.max_cost_usd = max_cost_usd
        self.dry_run = dry_run; self.consistency_runs = consistency_runs
        cleanup_stale_tempfiles(self.judgements_dir)

    def _plan(self, task, prompt_hash):
        tid = task.get("id", ""); jp = self.judgements_dir / f"{tid}.json"
        if _err_count(self.quarantine_dir, tid) >= QUARANTINE_AFTER_N_ERRORS: return TaskPlan(task, "quarantine", SkipReason.QUARANTINED)
        if prompt_hash is None: return TaskPlan(task, "skip", SkipReason.PROMPT_HASH_CHANGED)
        if not jp.exists(): return TaskPlan(task, "run")
        existing = _rj(jp)
        if existing is None: return TaskPlan(task, "run")
        if existing.get("judge_prompt_hash", "") == prompt_hash: return TaskPlan(task, "skip", SkipReason.ALREADY_JUDGED, existing)
        if self.rescore_on_prompt_change: return TaskPlan(task, "rescore", existing_judgement=existing)
        return TaskPlan(task, "skip", SkipReason.PROMPT_HASH_CHANGED, existing)

    def run(self, tasks) -> BatchSummary:
        summary = BatchSummary(total=len(tasks))
        plans = []
        for t in tasks:
            try: _, ph = self.rubric_loader.load(t.get("task_type", "dev"))
            except FileNotFoundError: ph = None
            plans.append(self._plan(t, ph))
        to_run = [p for p in plans if p.action in ("run", "rescore")]
        summary.skipped = sum(1 for p in plans if p.action == "skip")
        summary.quarantined = sum(1 for p in plans if p.action == "quarantine")
        cost = estimate_cost(to_run, self.judge_model)
        summary.cost_usd_estimated = cost
        if self.max_cost_usd and cost > self.max_cost_usd:
            logger.warning(f"Cost ${cost:.2f} exceeds max ${self.max_cost_usd:.2f}, aborting")
            return summary
        if self.dry_run:
            logger.info(f"DRY RUN: would judge {len(to_run)}, skip {summary.skipped}, quarantine {summary.quarantined}. Cost: ${cost:.4f}")
            return summary
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            for fut in as_completed({ex.submit(self._process, p): p for p in to_run}):
                r = fut.result(); summary.results.append(r)
                if r.success: (summary.rescored if r.action == "rescore" else summary.__setattr__("judged", summary.judged + 1))
                else: summary.errors += 1
        return summary

    def _process(self, plan) -> BatchResult:
        tid = plan.task.get("id", "unknown"); start = time.monotonic()
        try:
            j = self.judge_fn(plan.task); jp = self.judgements_dir / f"{tid}.json"
            if plan.action == "rescore" and jp.exists(): archive_to_history(jp, self.history_dir)
            atomic_write_json(jp, j); _reset_err(self.quarantine_dir, tid)
            return BatchResult(tid, plan.action, True, j, duration_s=time.monotonic()-start)
        except Exception as e:
            msg = str(e); count = _err_count(self.quarantine_dir, tid) + 1
            _save_err(self.quarantine_dir, tid, count, msg)
            if count >= QUARANTINE_AFTER_N_ERRORS: logger.error(f"Task {tid} quarantined after {count} errors")
            try: atomic_write_json(self.judgements_dir / f"{tid}.error.json", {"task_id": tid, "error": msg, "consecutive_errors": count})
            except OSError: pass
            return BatchResult(tid, plan.action, False, error=msg, duration_s=time.monotonic()-start)
