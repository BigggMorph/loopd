"""Atomic file I/O for eval judgements."""
from __future__ import annotations
import json, os, random, string, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _rand_suffix(n=8): return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))

def atomic_write_json(path: Path, obj: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp.{os.getpid()}.{_rand_suffix()}"
    try:
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

def archive_to_history(path: Path, history_dir: Path) -> Path | None:
    path = Path(path)
    if not path.exists(): return None
    history_dir = Path(history_dir)
    history_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = history_dir / f"{path.stem}_{ts}.json"
    if archive_path.exists(): archive_path = history_dir / f"{path.stem}_{ts}_{_rand_suffix(4)}.json"
    os.replace(path, archive_path)
    return archive_path

def cleanup_stale_tempfiles(directory: Path, max_age_s=3600) -> int:
    directory = Path(directory)
    if not directory.exists(): return 0
    now, removed = time.time(), 0
    for p in directory.iterdir():
        if ".tmp." not in p.name: continue
        try:
            if now - p.stat().st_mtime > max_age_s:
                p.unlink(missing_ok=True); removed += 1
        except OSError: pass
    return removed
