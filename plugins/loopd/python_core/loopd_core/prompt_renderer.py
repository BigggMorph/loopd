"""Minimal Jinja-like variable substitution for subagent prompts.

The plugin's ``agents/*.md`` files contain a system prompt with ``{{VAR}}``
placeholders. ``tick.py`` calls ``render()`` with a context dict to produce the
final string passed verbatim to the ``Task`` tool.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

_VAR_PATTERN = re.compile(r"\{\{\s*([A-Z][A-Z0-9_]*)\s*\}\}")


def render(template: str, context: Mapping[str, Any]) -> str:
    """Substitute ``{{VAR}}`` tokens. Unknown vars become an empty string."""
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        value = context.get(key, "")
        return "" if value is None else str(value)

    return _VAR_PATTERN.sub(repl, template)


def load_phase_prompt(agents_data_root: Path, phase: str) -> str:
    """Read and concatenate the main + subagent prompts for one phase.

    ``agents_data_root`` is the loopd ``_agents_data/`` directory.
    """
    phase_dir = agents_data_root / phase
    parts: list[str] = []

    main = phase_dir / "prompts" / "main.md"
    if main.exists():
        parts.append(main.read_text())

    sub_dir = phase_dir / "subagents"
    if sub_dir.is_dir():
        for sub in sorted(sub_dir.iterdir()):
            p = sub / "prompt.md"
            if p.exists():
                parts.append(f"\n\n## Subagent: {sub.name}\n\n{p.read_text()}")

    return "\n".join(parts) if parts else f"# {phase} phase\n\n(No prompt content found.)"
