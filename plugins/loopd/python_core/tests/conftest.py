"""Pytest scaffolding for loopd_core tests.

Every test runs against an isolated ``LOOPD_ROOT`` under ``tmp_path`` so
real ``~/.loopd`` state on the developer's machine is never touched. The
``Config`` lru_cache is cleared before and after each test.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_loopd_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LOOPD_ROOT", str(tmp_path))
    # Make sure stale env doesn't leak between tests
    monkeypatch.delenv("LOOPD_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)

    from loopd_core import config as cfg_mod

    cfg_mod.get_config.cache_clear()
    yield
    cfg_mod.get_config.cache_clear()
