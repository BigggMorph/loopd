"""SWE-bench environment harness: isolated Python venv inside the worktree.

Each SWE-bench task worktree gets its own `.swe_venv/` containing a Python
venv with the target repo installed. This gives code isolation (base_commit)
and environment isolation (repo-specific Python + deps).

Requires `uv` on PATH for best results; falls back to system `python3 -m venv`.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

VENV_DIR_NAME = ".swe_venv"
SETUP_TIMEOUT = 300
VENV_TIMEOUT = 60


class HarnessError(Exception):
    pass


def _has_uv() -> bool:
    return shutil.which("uv") is not None


def _run(
    cmd: list[str],
    cwd: Path,
    timeout: int = SETUP_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise HarnessError(
            f"{' '.join(cmd)} failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    return result


def get_venv_python(worktree_path: Path) -> Path:
    return worktree_path / VENV_DIR_NAME / "bin" / "python"


def venv_exists(worktree_path: Path) -> bool:
    return get_venv_python(worktree_path).exists()


def setup_env(
    worktree_path: Path,
    python_version: Optional[str] = None,
    install_repo: bool = True,
) -> Path:
    """Create isolated Python venv inside worktree and install the repo.

    Returns: Path to Python executable inside the venv.
    """
    venv_path = worktree_path / VENV_DIR_NAME
    python_exec = venv_path / "bin" / "python"

    if python_exec.exists():
        logger.info(f"Venv already exists: {venv_path}")
        return python_exec

    logger.info(f"Creating venv in {venv_path}")
    _create_venv(worktree_path, venv_path, python_version)

    if install_repo:
        _install_repo(worktree_path, python_exec)

    return python_exec


def _detect_python_version(worktree_path: Path) -> Optional[str]:
    """Infer required Python version from repo files.

    Old repos (Django < 4.x, astropy < 5.x) use distutils which was removed
    in Python 3.12. Default to 3.10 so those repos can still import distutils.
    """
    for fname in (".python-version", "pyproject.toml", "setup.cfg", "tox.ini"):
        p = worktree_path / fname
        if not p.exists():
            continue
        text = p.read_text(errors="ignore")
        import re
        m = re.search(r"python_requires\s*=\s*['\"]>=?\s*(\d+\.\d+)", text)
        if m:
            return m.group(1)
        m = re.search(r"python\s*=\s*['\"](\d+\.\d+)", text)
        if m:
            return m.group(1)
    # Default: 3.10 is the last release with distutils in stdlib
    return "3.10"


def _create_venv(
    worktree_path: Path,
    venv_path: Path,
    python_version: Optional[str],
) -> None:
    if python_version is None:
        python_version = _detect_python_version(worktree_path)

    if _has_uv():
        cmd = ["uv", "venv", str(venv_path), "--python", python_version]
        _run(cmd, cwd=worktree_path, timeout=VENV_TIMEOUT)
    else:
        py = shutil.which(f"python{python_version}")
        if not py:
            import sys
            py = sys.executable
        _run([py, "-m", "venv", str(venv_path)], cwd=worktree_path, timeout=VENV_TIMEOUT)


def _install_repo(worktree_path: Path, python_exec: Path) -> None:
    if _has_uv():
        cmd = ["uv", "pip", "install", "-e", ".", "--python", str(python_exec)]
    else:
        pip = python_exec.parent / "pip"
        cmd = [str(pip), "install", "-e", "."]

    logger.info("Installing repo into venv ...")
    result = _run(cmd, cwd=worktree_path, timeout=SETUP_TIMEOUT, check=False)
    if result.returncode != 0:
        logger.warning(
            f"Repo install returned exit {result.returncode} — "
            f"verifier may still work if tests are importable.\n{result.stderr[-500:]}"
        )


def _get_test_extras(worktree_path: Path) -> list[str]:
    """Extract test extra packages from setup.cfg if available."""
    import re
    cfg = worktree_path / "setup.cfg"
    if not cfg.exists():
        return []
    text = cfg.read_text(errors="ignore")
    m = re.search(r"\[options\.extras_require\].*?(?=\[|\Z)", text, re.DOTALL)
    if not m:
        return []
    block = m.group(0)
    # Find test = ... block (up to next key or end)
    tm = re.search(r"^test\s*=\s*(.+?)(?=^\S|\Z)", block, re.MULTILINE | re.DOTALL)
    if not tm:
        return []
    lines = [l.strip() for l in tm.group(1).splitlines() if l.strip() and not l.strip().startswith("#")]
    return [l for l in lines if l]


def _get_package_name(worktree_path: Path) -> Optional[str]:
    """Get the main package name from setup.cfg, pyproject.toml, or setup.py."""
    import re
    cfg = worktree_path / "setup.cfg"
    if cfg.exists():
        text = cfg.read_text(errors="ignore")
        m = re.search(r"^\[metadata\].*?^name\s*=\s*(\S+)", text, re.MULTILINE | re.DOTALL)
        if m:
            return m.group(1).strip()
    pyproject = worktree_path / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(errors="ignore")
        m = re.search(r'^name\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
    # Old-style repos that only have setup.py
    setup_py = worktree_path / "setup.py"
    if setup_py.exists():
        text = setup_py.read_text(errors="ignore")
        # Top-level NAME = 'X' variable
        m = re.search(r'^NAME\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if m:
            return m.group(1).strip()
        # Direct name='X' in setup() call
        m = re.search(r'setup\s*\([^)]*?name\s*=\s*["\']([^"\']+)["\']', text, re.DOTALL)
        if m:
            return m.group(1).strip()
    return None


def _get_package_version_from_git(worktree_path: Path) -> Optional[str]:
    """Get the nearest release version from setup.py / setup.cfg / git describe.

    Prefers setup.py VERSION or setup.cfg version (more accurate for dev
    branches whose nearest ancestor tag is far away), falls back to git describe.
    """
    import re

    # Try setup.py first — old repos declare VERSION = '3.1.dev' at top level
    setup_py = worktree_path / "setup.py"
    if setup_py.exists():
        text = setup_py.read_text(errors="ignore")
        m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
        if m:
            raw = m.group(1)
            mv = re.match(r"(\d+\.\d+(?:\.\d+)?)", raw)
            if mv:
                return mv.group(1)

    # Try setup.cfg — repos using setuptools_scm declare 'version = X.Y.dev'
    cfg = worktree_path / "setup.cfg"
    if cfg.exists():
        text = cfg.read_text(errors="ignore")
        m = re.search(r'^\[metadata\].*?^version\s*=\s*(\S+)', text, re.MULTILINE | re.DOTALL)
        if m:
            raw = m.group(1)
            mv = re.match(r"(\d+\.\d+(?:\.\d+)?)", raw)
            if mv:
                return mv.group(1)

    result = subprocess.run(
        ["git", "describe", "--tags", "--abbrev=0"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        return None
    tag = result.stdout.strip().lstrip("v")
    m = re.match(r"(\d+\.\d+(?:\.\d+)?)", tag)
    return m.group(1) if m else None


def _is_package_importable(package_name: str, python_exec: Path) -> bool:
    """Check if a package can be imported successfully."""
    result = subprocess.run(
        [str(python_exec), "-c", f"import {package_name}"],
        capture_output=True,
        timeout=15,
    )
    return result.returncode == 0


def _copy_compiled_extensions(src_pkg_dir: Path, dst_pkg_dir: Path) -> int:
    """Copy .so compiled extension files from src to dst (mirroring directory structure)."""
    count = 0
    for so_file in src_pkg_dir.rglob("*.so"):
        rel = so_file.relative_to(src_pkg_dir)
        target = dst_pkg_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        import shutil as _shutil
        _shutil.copy2(so_file, target)
        count += 1
    return count


def _get_site_packages(python_exec: Path) -> Optional[Path]:
    result = subprocess.run(
        [str(python_exec), "-c", "import site; print(site.getsitepackages()[0])"],
        capture_output=True, text=True, timeout=10,
    )
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def _try_install_pkg(
    package_name: str,
    pkg_spec: str,
    python_exec: Path,
    worktree_path: Path,
    extra_constraints: Optional[list[str]] = None,
) -> bool:
    """Attempt to install pkg_spec and return True if the package becomes importable."""
    extra = extra_constraints or []
    if _has_uv():
        cmd = ["uv", "pip", "install", pkg_spec] + extra + ["--python", str(python_exec)]
    else:
        pip = python_exec.parent / "pip"
        cmd = [str(pip), "install", pkg_spec] + extra
    _run(cmd, cwd=worktree_path, timeout=SETUP_TIMEOUT, check=False)
    return _is_package_importable(package_name, python_exec)


def install_binary_fallback(worktree_path: Path, python_exec: Path) -> bool:
    """If editable install failed (C extensions missing), install binary wheel and
    copy compiled extensions into the worktree so it becomes importable via PYTHONPATH.

    Tries exact version first; if no binary wheel exists, bumps major version by
    one until a binary wheel is found (e.g. astropy 4.0 → 5.0).

    Returns True if the fallback was applied successfully.
    """
    package_name = _get_package_name(worktree_path)
    if not package_name:
        return False

    # Check if already importable
    if _is_package_importable(package_name, python_exec):
        return False

    version = _get_package_version_from_git(worktree_path)
    numpy_constraint = ["numpy<2.0"]  # old repos need numpy<2.0

    # Try exact version first, then bump major version (up to +2) to find a wheel
    import re as _re
    version_candidates: list[Optional[str]] = [version]
    if version:
        m = _re.match(r"(\d+)", version)
        if m:
            major = int(m.group(1))
            version_candidates += [f"{major + 1}.0", f"{major + 2}.0"]
    version_candidates.append(None)  # unconstrained last resort

    installed = False
    for v in version_candidates:
        pkg_spec = f"{package_name}=={v}" if v else package_name
        logger.info(f"Editable install failed; installing binary wheel {pkg_spec} ...")
        if _try_install_pkg(package_name, pkg_spec, python_exec, worktree_path, numpy_constraint):
            installed = True
            break

    if not installed:
        logger.warning(f"Binary wheel install failed for {package_name}")
        return False

    # Copy .so files from site-packages to worktree so PYTHONPATH works
    try:
        site_pkg = _get_site_packages(python_exec)
        if site_pkg:
            site_pkg_dir = site_pkg / package_name
            worktree_pkg_dir = worktree_path / package_name
            if site_pkg_dir.exists() and worktree_pkg_dir.exists():
                count = _copy_compiled_extensions(site_pkg_dir, worktree_pkg_dir)
                logger.info(f"Copied {count} compiled extensions to worktree")
    except Exception as e:
        logger.warning(f"Failed to copy extensions: {e}")

    return True


_COLLECTIONS_COMPAT_DIR = Path.home() / ".loopd" / "py_compat"

_COLLECTIONS_COMPAT_CONTENT = (
    "import collections, collections.abc\n"
    "for _n in ('Callable', 'Mapping', 'MutableMapping', 'MutableSequence',\n"
    "           'MutableSet', 'Sequence', 'Set', 'Iterator', 'Iterable',\n"
    "           'Generator', 'Hashable', 'Sized', 'Container'):\n"
    "    if not hasattr(collections, _n) and hasattr(collections.abc, _n):\n"
    "        setattr(collections, _n, getattr(collections.abc, _n))\n"
)


def get_collections_compat_dir() -> Path:
    """Return a directory containing sitecustomize.py that restores collections shims.

    Python 3.10 removed collections.Mapping, collections.MutableMapping, etc. that many
    old repos use. Prepending this directory to PYTHONPATH ensures our sitecustomize.py
    is found before the system one, restoring the removed aliases.
    """
    _COLLECTIONS_COMPAT_DIR.mkdir(parents=True, exist_ok=True)
    sc = _COLLECTIONS_COMPAT_DIR / "sitecustomize.py"
    if not sc.exists() or sc.read_text() != _COLLECTIONS_COMPAT_CONTENT:
        sc.write_text(_COLLECTIONS_COMPAT_CONTENT)
    return _COLLECTIONS_COMPAT_DIR


def _install_c_extension_stubs(worktree_path: Path, python_exec: Path) -> None:
    """Create stub Python modules for missing C extensions so old repos can import.

    Some old repos (e.g. astropy < 4.x) have an __init__.py that probes for a
    specific C extension module (astropy.utils._compiler) and raises ImportError
    if it's absent — blocking all imports even for pure-Python tests.
    We detect this pattern and create a minimal stub so the import proceeds.

    Only creates stubs when PYTHONPATH includes the worktree (the verifier does this).
    """
    import re as _re

    # Check common stub patterns
    stub_candidates = [
        # (package_subdir, module_name, probe_pattern_in_init)
        ("astropy/utils", "_compiler", r"from \.utils import _compiler"),
    ]

    for subdir, mod_name, probe_pattern in stub_candidates:
        stub_path = worktree_path / subdir / f"{mod_name}.py"
        if stub_path.exists():
            continue  # already exists (real or stub)

        # Check if the repo's __init__.py probes for this extension
        pkg_init = worktree_path / subdir.split("/")[0] / "__init__.py"
        if not pkg_init.exists():
            continue
        init_text = pkg_init.read_text(errors="ignore")
        if not _re.search(probe_pattern, init_text):
            continue

        logger.info(f"Creating stub for missing C extension: {subdir}/{mod_name}.py")
        stub_path.write_text(
            f"# Auto-generated stub for SWE-bench verification.\n"
            f"# The real {mod_name} is a C extension that couldn't be built.\n"
        )

    # astropy < 4.x has astropy/_erfa/ufunc as a Cython extension that isn't in
    # PyPI binary wheels. Modern pyerfa (installed alongside astropy 5.x) has a
    # compatible ufunc module. Create a redirect stub if the worktree needs it.
    erfa_ufunc_stub = worktree_path / "astropy" / "_erfa" / "ufunc.py"
    erfa_qh = worktree_path / "astropy" / "units" / "quantity_helper.py"
    if (
        not erfa_ufunc_stub.exists()
        and erfa_qh.exists()
        and "from .._erfa import ufunc" in erfa_qh.read_text(errors="ignore")
    ):
        logger.info("Creating astropy/_erfa/ufunc.py redirect stub (pyerfa compat)")
        erfa_ufunc_stub.write_text(
            "# Auto-generated compat stub for SWE-bench verification.\n"
            "# astropy 3.x had _erfa/ufunc as a Cython extension; redirect to pyerfa.\n"
            "try:\n"
            "    from erfa.ufunc import *  # noqa: F401, F403\n"
            "    from erfa import ufunc as _u\n"
            "    import sys as _sys\n"
            "    _sys.modules[__name__].__dict__.update(\n"
            "        {k: getattr(_u, k) for k in dir(_u) if not k.startswith('_')})\n"
            "except ImportError:\n"
            "    pass\n"
        )


def _needs_old_numpy(worktree_path: Path) -> bool:
    """Return True if this repo's quantity.py uses numpy 1.x copy semantics."""
    quantity_py = worktree_path / "astropy" / "units" / "quantity.py"
    if not quantity_py.exists():
        return False
    text = quantity_py.read_text(errors="ignore")
    return "np.array(obj, copy=False)" in text


def _ensure_pkg_resources(worktree_path: Path, python_exec: Path) -> None:
    """Ensure pkg_resources is importable — install setuptools<70 if missing.

    Modern setuptools (70+) installed via uv no longer ships pkg_resources as a
    top-level package. Old repos call `from pkg_resources import parse_version`
    and fall back to distutils.LooseVersion when it's absent. That fallback
    raises DeprecationWarning which becomes an error in astropy's conftest.
    """
    result = subprocess.run(
        [str(python_exec), "-c", "import pkg_resources"],
        capture_output=True,
        timeout=10,
    )
    if result.returncode == 0:
        return  # already available
    logger.info("pkg_resources missing; installing setuptools<70 to restore it")
    if _has_uv():
        _run(
            ["uv", "pip", "install", "setuptools<70", "--python", str(python_exec)],
            cwd=worktree_path, timeout=120, check=False,
        )
    else:
        pip = python_exec.parent / "pip"
        _run([str(pip), "install", "setuptools<70"], cwd=worktree_path, timeout=120, check=False)


def install_test_dependencies(worktree_path: Path) -> None:
    """Install pytest, json-report plugin, and repo test extras into the worktree venv."""
    python_exec = get_venv_python(worktree_path)
    if not python_exec.exists():
        raise HarnessError(
            f"Venv not found at {worktree_path / VENV_DIR_NAME}. Run setup_env() first."
        )

    pkgs = ["pytest", "pytest-json-report", "setuptools_scm"] + _get_test_extras(worktree_path)
    if _has_uv():
        cmd = ["uv", "pip", "install"] + pkgs + ["--python", str(python_exec)]
    else:
        pip = python_exec.parent / "pip"
        cmd = [str(pip), "install"] + pkgs

    _run(cmd, cwd=worktree_path, timeout=300, check=False)

    # Ensure the collections compat dir is ready (used by verifier's PYTHONPATH)
    get_collections_compat_dir()

    # Old repos that use np.array(obj, copy=False) break with numpy 2.x.
    # Install numpy<2.0 so they stay compatible.
    if _needs_old_numpy(worktree_path):
        logger.info("Old numpy semantics detected; pinning numpy<2.0")
        if _has_uv():
            _run(
                ["uv", "pip", "install", "numpy<2.0", "--python", str(python_exec)],
                cwd=worktree_path, timeout=120, check=False,
            )
        else:
            pip = python_exec.parent / "pip"
            _run([str(pip), "install", "numpy<2.0"], cwd=worktree_path, timeout=120, check=False)

    # Old repos use `from pkg_resources import parse_version`; modern setuptools
    # installed via uv omits the top-level pkg_resources package.
    _ensure_pkg_resources(worktree_path, python_exec)

    # Fallback: if editable install failed, use binary wheel + copy .so files
    install_binary_fallback(worktree_path, python_exec)

    # Last-resort: create stub C-extension modules for repos (e.g. old astropy)
    # whose __init__.py blocks import when extension stubs are absent.
    _install_c_extension_stubs(worktree_path, python_exec)
