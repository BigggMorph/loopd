"""Concurrency regression tests for TaskManager.create_task.

These tests guard the fix for BigggMorph/loopd#8: when N Claude Code
windows run /dev-task in parallel they must each receive a distinct
task_id. Prior to the per-day LockManager bucket inside create_task,
two callers could compute the same NNN sequence between
_generate_task_id() and the pending JSON write.

The stress test uses multiprocessing.Pool with "spawn" start method
because the contending /dev-task callers are independent Claude Code
processes, not threads inside a shared parent. monkeypatch.setenv from
the isolated_loopd_root autouse fixture does NOT propagate to spawned
subprocesses, so we pass LOOPD_ROOT through the worker args and set
os.environ inside the worker before importing loopd_core.
"""

from __future__ import annotations

import multiprocessing as mp
import re
from pathlib import Path

import pytest


# Module-level so the spawn-mode pool can pickle it.
def _create_one(args: tuple[str, str]) -> str:
    loopd_root, prompt = args
    import os

    os.environ["LOOPD_ROOT"] = loopd_root
    # Clear any cached Config inside the worker process.
    from loopd_core import config as cfg_mod

    cfg_mod.get_config.cache_clear()
    from loopd_core.state.task_manager import TaskManager

    tm = TaskManager()
    task = tm.create_task(prompt=prompt, source_type="test")
    return task.id


def test_concurrent_create_task_yields_unique_ids(tmp_path):
    """100 concurrent create_task calls must yield 100 distinct IDs."""
    loopd_root = str(tmp_path)

    ctx = mp.get_context("spawn")
    n = 100
    args = [(loopd_root, f"prompt-{i}") for i in range(n)]

    with ctx.Pool(processes=8) as pool:
        results = pool.map(_create_one, args)

    # All N IDs must be unique.
    assert len(set(results)) == n, (
        f"expected {n} unique task_ids, got {len(set(results))} "
        f"(duplicates indicate a race condition in _generate_task_id)"
    )

    # All IDs must match the canonical format.
    pattern = re.compile(r"^task-\d{4}-\d{2}-\d{2}-\d{3}$")
    for tid in results:
        assert pattern.match(tid), f"malformed task_id: {tid}"

    # And N pending JSON files must exist on disk.
    pending_dir = Path(loopd_root) / "state" / "pending"
    json_files = sorted(p.name for p in pending_dir.glob("task-*.json"))
    assert len(json_files) == n, (
        f"expected {n} pending JSON files, found {len(json_files)}"
    )


def test_sequential_create_task_still_works():
    """Smoke test: sequential creates produce -001, -002, -003 monotonically."""
    from loopd_core.state.task_manager import TaskManager

    tm = TaskManager()
    t1 = tm.create_task(prompt="one", source_type="test")
    t2 = tm.create_task(prompt="two", source_type="test")
    t3 = tm.create_task(prompt="three", source_type="test")

    # Sequence numbers should be contiguous and increasing.
    seqs = [int(t.id.rsplit("-", 1)[1]) for t in (t1, t2, t3)]
    assert seqs == [seqs[0], seqs[0] + 1, seqs[0] + 2], (
        f"non-monotonic sequence: {seqs}"
    )


def test_lock_release_on_success():
    """After create_task returns, the per-day lock must be released so the
    next call can acquire it immediately (no orphan lock dir left behind)."""
    from datetime import datetime

    from loopd_core.state.lock_manager import LockManager
    from loopd_core.state.task_manager import TaskManager

    tm = TaskManager()
    tm.create_task(prompt="release-check", source_type="test")

    lm = LockManager()
    lock_key = f"task-create-{datetime.now().strftime('%Y-%m-%d')}"
    assert not lm.is_locked(lock_key), (
        "per-day create-task lock must be released when create_task returns"
    )
