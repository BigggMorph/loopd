"""Small helpers shared by loopd's PreToolUse / PostToolUse / Stop hooks.

The hook scripts ship under ``plugins/loopd/hooks/`` but need to import
``loopd_core`` which lives under ``plugins/loopd/python_core/``. Each hook
previously re-implemented its own ``sys.path`` gymnastics and its own
``_resolve_session`` — both have drifted, both had the cwd-hash fallback
that caused issue #4. This module centralises the import-path fix so all
three hooks delegate to ``loopd_core.session_store`` for session lookup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_loopd_core_importable() -> None:
    """Add ``plugins/loopd/python_core/`` to ``sys.path`` so ``loopd_core``
    imports work regardless of which interpreter / cwd the hook runs in.

    Prefers ``CLAUDE_PLUGIN_ROOT`` (set by Claude Code), falls back to a
    relative walk from this file's location for local development.
    """
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        candidate = Path(env_root) / "python_core"
    else:
        # plugins/loopd/hooks/_loopd_hook_lib.py → plugins/loopd/python_core/
        candidate = Path(__file__).resolve().parent.parent / "python_core"
    p = str(candidate)
    if p not in sys.path:
        sys.path.insert(0, p)
