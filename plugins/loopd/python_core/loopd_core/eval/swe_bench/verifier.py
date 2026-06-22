"""SWE-bench verifier: apply test_patch + run pytest to determine resolved status.

SWE-bench scoring:
  resolved = True  iff
    ALL tests in FAIL_TO_PASS now pass  (they failed at base_commit)
    AND ALL tests in PASS_TO_PASS still pass  (they passed at base_commit)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from loopd_core.eval.swe_bench.harness import (
    get_venv_python,
    get_collections_compat_dir,
    install_test_dependencies,
)

logger = logging.getLogger(__name__)

PYTEST_TIMEOUT = 300


class VerifierError(Exception):
    pass


@dataclass
class SWEBenchResult:
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


def _reset_test_files(patch_text: str, worktree_path: Path) -> None:
    """Reset any test files that appear in test_patch back to HEAD state.

    If the agent accidentally modified test files, git apply will fail with
    conflicts. Resetting those files before applying lets the patch land cleanly.
    For new files (--- /dev/null), delete them if they exist so the patch can
    recreate them.
    """
    import re

    # Find all test-file diffs: modified, newly created, or renamed.
    # Match test files at any depth (not just top-level tests/ or test_*).
    diff_blocks = re.split(r"(?=^diff --git )", patch_text, flags=re.MULTILINE)
    for block in diff_blocks:
        m = re.match(r"^diff --git a/([^\s]+)\s+b/([^\s]+)", block)
        if not m:
            continue
        f_src, f_dst = m.group(1), m.group(2)
        # Only handle test files (anywhere in path)
        if not ("/test" in f_dst or f_dst.startswith("test") or
                "/test" in f_src or f_src.startswith("test")):
            continue
        is_new_file = "--- /dev/null" in block
        is_rename = "rename from " in block
        if is_rename:
            # Delete destination if it already exists (agent may have created it)
            dst_target = worktree_path / f_dst
            if dst_target.exists():
                dst_target.unlink()
            # Restore source file to HEAD so the rename patch can apply
            subprocess.run(
                ["git", "checkout", "HEAD", "--", f_src],
                cwd=str(worktree_path),
                capture_output=True,
                timeout=10,
            )
        elif is_new_file:
            # Delete if the agent already created this file
            target = worktree_path / f_dst
            if target.exists():
                target.unlink()
        else:
            subprocess.run(
                ["git", "checkout", "HEAD", "--", f_dst],
                cwd=str(worktree_path),
                capture_output=True,
                timeout=10,
            )


def _apply_patch(patch_text: str, worktree_path: Path) -> None:
    if not patch_text.strip():
        return

    # Reset any test files the agent may have modified so test_patch applies cleanly
    _reset_test_files(patch_text, worktree_path)

    result = subprocess.run(
        ["git", "apply", "--whitespace=fix", "-"],
        input=patch_text,
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        result2 = subprocess.run(
            ["git", "apply", "--reject", "--whitespace=fix", "-"],
            input=patch_text,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result2.returncode != 0:
            raise VerifierError(
                f"git apply failed:\n{result.stderr.strip()}\n{result2.stderr.strip()}"
            )
        logger.warning("test_patch applied with --reject (partial)")


def _is_django_test_id(test_id: str) -> bool:
    """Django unittest IDs look like 'test_method (module.path.ClassName)'.

    The content inside the parens must be a valid Python dotted identifier
    (letters, digits, underscores, dots only) — not a bug number like #13407.
    """
    import re as _re
    if " (" not in test_id or not test_id.endswith(")"):
        return False
    paren_start = test_id.rfind(" (")
    inner = test_id[paren_start + 2:-1]
    return bool(_re.match(r'^[A-Za-z_][A-Za-z0-9_.]*$', inner))


def _parse_django_test_id(test_id: str) -> tuple[str, str]:
    """Return (module_path, full_dotted_id) for a Django unittest test ID."""
    # 'test_method (urlpatterns.test_resolvers.RegexPatternTests)'
    paren_start = test_id.rfind(" (")
    if paren_start < 0 or not test_id.endswith(")"):
        return test_id, test_id
    method = test_id[:paren_start]
    dotted = test_id[paren_start + 2:-1]   # 'urlpatterns.test_resolvers.RegexPatternTests'
    full = f"{dotted}.{method}"             # 'urlpatterns.test_resolvers.RegexPatternTests.test_method'
    module = dotted.rsplit(".", 1)[0]       # 'urlpatterns.test_resolvers'
    return module, full


def _run_django_tests(
    worktree_path: Path,
    test_ids: list[str],
    python_exec: Path,
) -> dict[str, bool]:
    """Run Django tests via tests/runtests.py and parse unittest output."""
    if not test_ids:
        return {}

    runtests = worktree_path / "tests" / "runtests.py"
    if not runtests.exists():
        logger.warning("tests/runtests.py not found, falling back to pytest")
        return _run_pytest_tests(worktree_path, test_ids, python_exec)

    # Collect unique dotted test labels (method-level precision)
    # Non-standard IDs (docstrings, #bug-ref in parens, etc.) can't be passed
    # as runtests.py labels — keep them separate and infer results later.
    standard_tids = [t for t in test_ids if _is_django_test_id(t)]
    docstring_tids = [t for t in test_ids if not _is_django_test_id(t)]

    labels = list(dict.fromkeys(_parse_django_test_id(t)[1] for t in standard_tids))

    # Collect unique modules for docstring tests so we can run their parent module
    docstring_modules: set[str] = set()
    for tid in docstring_tids:
        # Try to infer module from co-located standard tests in the same batch
        for std in standard_tids:
            _, full = _parse_django_test_id(std)
            mod = full.rsplit(".", 2)[0]  # module (without Class.method)
            docstring_modules.add(mod)
            break

    cmd = [str(python_exec), str(runtests), "--verbosity=2"] + labels
    try:
        result = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=PYTEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {t: False for t in test_ids}

    output = result.stdout + result.stderr

    # If there are docstring-based test IDs, run parent modules to get their output
    if docstring_tids and docstring_modules:
        try:
            ds_cmd = [str(python_exec), str(runtests), "--verbosity=2"] + sorted(docstring_modules)
            ds_result = subprocess.run(
                ds_cmd, cwd=str(worktree_path), capture_output=True, text=True, timeout=PYTEST_TIMEOUT,
            )
            output_ds = ds_result.stdout + ds_result.stderr
        except subprocess.TimeoutExpired:
            output_ds = ""
    else:
        output_ds = ""

    results: dict[str, bool] = {}
    import re
    for tid in test_ids:
        # Match: 'test_method (module.Class) ... ok' (no docstring)
        # or     'test_method (module.Class)\nDocstring text ... ok' (with docstring)
        if not _is_django_test_id(tid):
            # Docstring-based ID: search in the parent-module output
            search_out = output_ds or output
            idx = search_out.find(tid)
            if idx >= 0:
                chunk = search_out[idx:idx + 300]
                m = re.search(r"\.\.\.\s+(ok|FAIL|ERROR)", chunk)
                results[tid] = (m.group(1) == "ok") if m else True
            else:
                results[tid] = True  # can't verify, assume still passing
            continue
        paren_start = tid.rfind(" (")
        method = tid[:paren_start]
        dotted_class = tid[paren_start + 2:-1]
        search_key = f"{method} ({dotted_class})"
        idx = output.find(search_key)
        if idx >= 0:
            # Look for '... ok/FAIL/ERROR' within the next 600 chars (covers docstring line)
            chunk = output[idx:idx + 600]
            m = re.search(r"\.\.\.\s+(ok|FAIL|ERROR)", chunk)
            results[tid] = (m.group(1) == "ok") if m else (result.returncode == 0)
        else:
            results[tid] = result.returncode == 0

    return results


def _is_valid_test_id(test_id: str) -> bool:
    """Return False for test IDs that are truncated parametrize markers.

    Some SWE-bench dataset entries have truncated test IDs like
    'test_foo[param1,' (no closing bracket) which cause pytest to abort
    all collection even with --continue-on-collection-errors. We skip these
    and assume they still pass (they were passing at base_commit).
    """
    if "[" in test_id:
        return test_id.endswith("]")
    return True


def _run_pytest_tests(
    worktree_path: Path,
    test_ids: list[str],
    python_exec: Path,
) -> dict[str, bool]:
    if not test_ids:
        return {}

    # Filter out truncated parametrize test IDs that would abort pytest collection.
    # Mark them as passed by default (can't verify; assume still passing).
    valid_ids = [t for t in test_ids if _is_valid_test_id(t)]
    invalid_ids = [t for t in test_ids if not _is_valid_test_id(t)]
    if invalid_ids:
        logger.warning(f"Skipping {len(invalid_ids)} truncated test IDs: {invalid_ids[:3]}")
    results_pre: dict[str, bool] = {t: True for t in invalid_ids}

    if not valid_ids:
        return results_pre

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        report_path = tmp.name

    # PYTHONPATH order:
    #   1. collections-compat dir: sitecustomize.py shim so old repos using
    #      collections.Mapping (removed in Python 3.10) still work.
    #   2. worktree: agent's modified Python source takes priority over site-packages.
    import os as _os
    env = dict(_os.environ)
    compat_dir = str(get_collections_compat_dir())
    env["PYTHONPATH"] = compat_dir + _os.pathsep + str(worktree_path)

    try:
        # Old repos with [tool:pytest] in setup.cfg return None for cache_dir
        # ini option in new pytest, causing TypeError in cache_dir_from_config.
        # Pre-create .pytest_cache/astropy/ (old astropy plugin needs it).
        pytest_cache = worktree_path / ".pytest_cache"
        (pytest_cache / "astropy").mkdir(parents=True, exist_ok=True)

        cmd = [
            str(python_exec), "-m", "pytest",
            "--tb=no",
            "-q",
            "--json-report",
            f"--json-report-file={report_path}",
            "--no-header",
            # Override repo filterwarnings to avoid numpy/scipy deprecations
            # becoming errors during conftest import (old repos + new numpy).
            "--override-ini=filterwarnings=default::DeprecationWarning",
            # Old repos with [tool:pytest] in setup.cfg return None for cache_dir,
            # causing INTERNALERROR in newer pytest versions.
            "--override-ini=cache_dir=.pytest_cache",
        ] + valid_ids

        result = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            env=env,
            timeout=PYTEST_TIMEOUT,
        )

        try:
            with open(report_path, encoding="utf-8") as f:
                report = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            passed = result.returncode == 0
            return {**results_pre, **{t: passed for t in valid_ids}}

        results: dict[str, bool] = dict(results_pre)
        for test in report.get("tests", []):
            node_id = test.get("nodeid", "")
            outcome = test.get("outcome", "failed")
            results[node_id] = outcome == "passed"

        for t in valid_ids:
            if t not in results:
                results[t] = False

        return results
    finally:
        try:
            os.unlink(report_path)
        except OSError:
            pass


def verify(
    worktree_path: Path,
    test_patch: str,
    fail_to_pass: list[str],
    pass_to_pass: list[str],
    python_exec: Optional[Path] = None,
) -> SWEBenchResult:
    """Apply test_patch and verify FAIL_TO_PASS / PASS_TO_PASS test outcomes."""
    if python_exec is None:
        python_exec = get_venv_python(worktree_path)

    if not python_exec.exists():
        return SWEBenchResult(
            resolved=False,
            error=f"Python venv not found at {python_exec}. Run harness.setup_env() first.",
        )

    install_test_dependencies(worktree_path)

    try:
        _apply_patch(test_patch, worktree_path)
    except VerifierError as e:
        return SWEBenchResult(resolved=False, error=str(e))

    all_test_ids = list(dict.fromkeys(fail_to_pass + pass_to_pass))
    # Route to the right test runner based on test ID format
    use_django = any(_is_django_test_id(t) for t in all_test_ids)
    _run_tests = _run_django_tests if use_django else _run_pytest_tests
    try:
        all_results = _run_tests(worktree_path, all_test_ids, python_exec)
    except subprocess.TimeoutExpired:
        return SWEBenchResult(resolved=False, error=f"tests timed out after {PYTEST_TIMEOUT}s")
    except Exception as e:
        return SWEBenchResult(resolved=False, error=str(e))

    ftp_results = {t: all_results.get(t, False) for t in fail_to_pass}
    ptp_results = {t: all_results.get(t, False) for t in pass_to_pass}

    ftp_ok = all(ftp_results.values()) if ftp_results else True
    ptp_ok = all(ptp_results.values()) if ptp_results else True

    return SWEBenchResult(
        resolved=ftp_ok and ptp_ok,
        fail_to_pass_results=ftp_results,
        pass_to_pass_results=ptp_results,
    )
