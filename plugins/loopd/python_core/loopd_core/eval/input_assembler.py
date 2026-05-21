"""InputAssembler: build LLM judge input from task JSON + events + artifacts."""
from __future__ import annotations
import json, logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)
INPUT_BUDGET_BYTES = 120_000
PR_BUDGET = 8_000
ARTIFACT_BUDGET = 12_000
EVENTS_BUDGET = 6_000

def _truncate(text, max_bytes, label=""):
    enc = text.encode("utf-8")
    if len(enc) <= max_bytes: return text
    return enc[:max_bytes].decode("utf-8", errors="ignore") + f"\n[TRUNCATED — {label} exceeded {max_bytes} bytes]"

def _events_summary(events):
    if not events: return "(no events found)"
    return "\n".join(f"  [{ev.get('ts','')}] {ev.get('type','?')} {json.dumps(ev.get('data') or {}, ensure_ascii=False) if ev.get('data') else ''}".rstrip() for ev in events)

class InputAssembler:
    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def assemble(self, task: dict[str, Any], events: list[dict[str, Any]], pr_body: Optional[str] = None) -> str:
        parts = [
            f"# Task Quality Evaluation\n\n"
            f"**Task ID**: {task.get('id','unknown')}\n"
            f"**Task Type**: {task.get('task_type','unknown')}\n"
            f"**Status**: {task.get('status','unknown')}\n"
            f"**Level**: {task.get('level','?')}\n"
            f"**Created**: {task.get('created_at','')}\n"
            f"**Completed**: {task.get('completed_at','')}\n\n"
            f"## Original Request\n\n{task.get('prompt','')}\n"
        ]
        body = pr_body or self._find_pr_body(task)
        if body:
            parts.append(f"\n## PR / Handoff Summary\n\n{_truncate(body, PR_BUDGET, 'pr_body')}\n")
        art = self._load_artifacts(task)
        if art:
            parts.append(f"\n## Key Artifacts\n\n{art}\n")
        parts.append(f"\n## Execution Events\n\n{_truncate(_events_summary(events), EVENTS_BUDGET, 'events')}\n")
        full = "".join(parts)
        return _truncate(full, INPUT_BUDGET_BYTES, "total_input") if len(full.encode("utf-8")) > INPUT_BUDGET_BYTES else full

    def _find_pr_body(self, task):
        ws = task.get("workspace", {})
        wp = ws.get("path") if isinstance(ws, dict) else None
        if not wp: return None
        for f in ["handoff.md", "pr_body.md", "SUMMARY.md"]:
            p = Path(wp) / f
            if p.exists(): return p.read_text(encoding="utf-8", errors="replace")
        return None

    def _load_artifacts(self, task):
        artifacts = task.get("artifacts") or []
        sections, remaining = [], ARTIFACT_BUDGET * 3
        for a in artifacts[:5]:
            if remaining <= 0: break
            path = a.get("path", "") if isinstance(a, dict) else a
            if not path: continue
            p = Path(path)
            if not p.is_absolute(): p = self.project_root / path
            if not p.exists(): continue
            try:
                content = _truncate(p.read_text(encoding="utf-8", errors="replace"), min(ARTIFACT_BUDGET, remaining), p.name)
                sections.append(f"### {p.name}\n\n```\n{content}\n```")
                remaining -= len(content.encode("utf-8"))
            except OSError: pass
        return "\n\n".join(sections)
