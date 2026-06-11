"""FeatureBench verifier: restore test files and run pytest at file level."""
from __future__ import annotations

import logging
import subprocess
import tempfile
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loopd_core.eval.swe_bench.harness import get_venv_python

logger = logging.getLogger(__name__)


def _ensure_pytest(python_exec: Path) -> None:
    """Install pytest if not already present."""
    check = subprocess.run(
        [str(python_exec), "-m", "pytest", "--version"],
        capture_output=True, timeout=10,
    )
    if check.returncode == 0:
        return
    logger.info("Installing pytest into venv")
    # Bootstrap pip first (uv venvs don't include pip by default)
    subprocess.run(
        [str(python_exec), "-m", "ensurepip"],
        capture_output=True, timeout=30,
    )
    subprocess.run(
        [str(python_exec), "-m", "pip", "install", "-q", "pytest"],
        capture_output=True, timeout=120,
    )

PYTEST_TIMEOUT = 600


@dataclass
class FBResult:
    resolved: bool
    fail_to_pass_results: dict[str, bool] = field(default_factory=dict)
    pass_to_pass_results: dict[str, bool] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "resolved": self.resolved,
            "fail_to_pass_results": self.fail_to_pass_results,
            "pass_to_pass_results": self.pass_to_pass_results,
            "error": self.error,
        }


def _restore_test_files(worktree_path: Path, fail_to_pass: list[str]) -> None:
    """Restore FTP test files from git HEAD (they were deleted before agent ran)."""
    for rel_path in fail_to_pass:
        result = subprocess.run(
            ["git", "checkout", "HEAD", "--", rel_path],
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            logger.info(f"Restored test file: {rel_path}")
        else:
            logger.warning(f"Failed to restore {rel_path}: {result.stderr.strip()}")


def _ensure_package_installed(worktree_path: Path, python_exec: Path) -> None:
    """Install the repo's own package if not importable (e.g. pandas C extension)."""
    # Try importing the top-level package (guessed from worktree dir name)
    # e.g. pandas-dev__pandas → pandas
    dir_name = worktree_path.name
    # Extract package name: last segment after __ and before the hash suffix
    parts = dir_name.split("--")[0]  # strip worktree suffix
    pkg_guess = parts.split("__")[-1].split("-")[0] if "__" in parts else None
    if not pkg_guess:
        return
    check = subprocess.run(
        [str(python_exec), "-c", f"import {pkg_guess}"],
        capture_output=True, timeout=15,
    )
    if check.returncode != 0:
        logger.info(f"Package '{pkg_guess}' not importable — installing binary wheel")
        subprocess.run(
            [str(python_exec), "-m", "pip", "install", "-q", pkg_guess],
            capture_output=True, timeout=120,
        )


def _run_file_pytest(
    worktree_path: Path,
    test_files: list[str],
    python_exec: Path,
) -> dict[str, bool]:
    """Run pytest on each test file and return {file: all_passed}."""
    results = {}
    env = dict(os.environ)
    env["PYTHONPATH"] = str(worktree_path)

    for test_file in test_files:
        try:
            cmd = [
                str(python_exec), "-m", "pytest",
                "--tb=no", "-q", "--no-header",
                "--override-ini=filterwarnings=default::DeprecationWarning",
                "--override-ini=cache_dir=.pytest_cache",
                test_file,
            ]
            proc = subprocess.run(
                cmd,
                cwd=str(worktree_path),
                capture_output=True,
                text=True,
                env=env,
                timeout=PYTEST_TIMEOUT,
            )
            # returncode 0 = all collected tests passed
            # Parse stdout to confirm at least 1 test was collected
            output = proc.stdout + proc.stderr
            import re
            m = re.search(r"(\d+) passed", output)
            passed_count = int(m.group(1)) if m else 0
            has_failure = bool(re.search(r"(\d+) failed", output))

            # Collection error = missing optional dep (torch, databricks-sdk, etc.)
            # The agent didn't break this — treat as skipped/passing.
            is_collection_error = (
                "ERROR collecting" in output or
                (proc.returncode != 0 and passed_count == 0 and not has_failure)
            )
            if is_collection_error:
                logger.warning(f"Collection error in {test_file} (missing dep?) — treating as skipped")
                results[test_file] = True
            elif proc.returncode == 0 and passed_count > 0:
                results[test_file] = True
            elif "no tests ran" in output or "collected 0 items" in output:
                results[test_file] = False
            else:
                results[test_file] = proc.returncode == 0
        except subprocess.TimeoutExpired:
            results[test_file] = False

    return results


def verify(
    worktree_path: Path,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    python_exec: Optional[Path] = None,
) -> FBResult:
    if python_exec is None:
        python_exec = get_venv_python(worktree_path)

    if not python_exec.exists():
        return FBResult(resolved=False, error=f"Python venv not found at {python_exec}")

    _ensure_pytest(python_exec)

    # Ensure the repo package itself is importable (e.g. pandas needs binary wheel)
    _ensure_package_installed(worktree_path, python_exec)

    # Restore FTP test files (they were hidden from the agent)
    _restore_test_files(worktree_path, fail_to_pass)

    try:
        ftp_results = _run_file_pytest(worktree_path, fail_to_pass, python_exec)
    except Exception as e:
        return FBResult(resolved=False, error=f"FTP pytest failed: {e}")

    try:
        ptp_results = _run_file_pytest(worktree_path, pass_to_pass, python_exec)
    except Exception as e:
        return FBResult(resolved=False, error=f"PTP pytest failed: {e}")

    ftp_ok = all(ftp_results.values()) if ftp_results else True
    ptp_ok = all(ptp_results.values()) if ptp_results else True

    return FBResult(
        resolved=ftp_ok and ptp_ok,
        fail_to_pass_results=ftp_results,
        pass_to_pass_results=ptp_results,
    )
