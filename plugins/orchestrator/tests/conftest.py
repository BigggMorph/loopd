"""pytest fixtures: isolate every test inside a fresh tmp ~/.loopd dir."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parent.parent / "python_helpers"
if str(HELPERS) not in sys.path:
    sys.path.insert(0, str(HELPERS))


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Redirect ~/.loopd to a per-test tmp dir."""
    home = tmp_path / "home"
    (home / ".loopd" / "sessions").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # The suite runs inside a real Claude Code session, so CLAUDE_CODE_SESSION_ID
    # is set in the ambient env. Clear all session vars so current_session_id()
    # tests are hermetic and only see what each test explicitly sets.
    for _var in ("CLAUDE_CODE_SESSION_ID", "LOOPD_SESSION_ID", "CLAUDE_SESSION_ID"):
        monkeypatch.delenv(_var, raising=False)
    # Modules cache absolute paths at import time → reload after HOME change.
    import orchestrator_state
    importlib.reload(orchestrator_state)
    yield home
    # Re-reload so other tests see a clean module each round.
    importlib.reload(orchestrator_state)
