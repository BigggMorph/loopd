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
    # Modules cache absolute paths at import time → reload after HOME change.
    import orchestrator_state
    importlib.reload(orchestrator_state)
    yield home
    # Re-reload so other tests see a clean module each round.
    importlib.reload(orchestrator_state)
