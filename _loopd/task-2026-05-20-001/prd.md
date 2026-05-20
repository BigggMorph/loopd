# PRD — Auto-bootstrap Python deps in `loopd` plugin tick shim

GitHub issue: BigggMorph/loopd#2

## Problem

`/plugin install loopd@loopd` lands the plugin files on disk but Claude Code
plugin marketplaces have no post-install hook, so the declared deps in
`python_core/pyproject.toml` (`pydantic>=2.0`, `pydantic-settings>=2.0`,
`PyYAML>=6.0`) are never installed. The very first `/loopd:*` invocation goes
through `python_core/scripts/tick`, which just execs the system `python3 -m
loopd_core.tick` with `PYTHONPATH=python_core/`. That raises
`ModuleNotFoundError: No module named 'pydantic'` and the plugin appears
broken to a new user.

On some hosts `python3` and `pip3` resolve to **different** interpreters
(e.g. brew `python@3.12` vs `python@3.14`), so naive `pip3 install` does not
fix the problem. Any auto-install logic must therefore use `"$PY" -m pip`
against the same interpreter that will exec the FSM.

## Goal

Make `/plugin install loopd@loopd` truly turnkey: the first `tick` invocation
on a fresh machine self-bootstraps its Python dependencies (silently on
success, with one progress line on first install) and then proceeds normally.

## Non-goals

- Shipping or vendoring Pydantic / PyYAML wheels.
- Replacing the user's system Python or forcing a specific interpreter.
- Adding a daemon / background install step.
- Reworking the FSM, hooks, or session layout.

## Functional Requirements

- **FR-1**: `python_core/scripts/tick` MUST verify on every invocation that
  `pydantic`, `pydantic_settings`, and `yaml` import successfully under the
  interpreter resolved by `${LOOPD_PYTHON:-python3}` before exec'ing
  `loopd_core.tick`.

- **FR-2**: When the import check fails, `tick` MUST install the deps using
  `"$PY" -m pip` (never `pip3`) — guaranteeing the install lands on the same
  interpreter the FSM will run under. Install strategy:
    1. `"$PY" -m pip install --user --quiet ...` first (least invasive).
    2. On failure (PEP 668 "externally managed" or otherwise),
       retry with `"$PY" -m pip install --break-system-packages --quiet ...`.
    3. If both fail, exit non-zero with a clear message that includes the
       exact `pip install` command the user can run manually, and a pointer
       to README "Troubleshooting".

- **FR-3**: The bootstrap MUST be idempotent and fast on the happy path: when
  imports already succeed (i.e. on every run after the first), the check is
  one `python3 -c '...'` invocation and no `pip` call is made.

- **FR-4**: The bootstrap MUST print exactly **one** human-readable line to
  stderr on the install path (`[loopd] Installing Python dependencies for
  Python X.Y …`) and stay silent on the no-op path, so it does not pollute
  `tick`'s stdout JSON contract.

- **FR-5**: The bootstrap MUST honour a `LOOPD_SKIP_BOOTSTRAP=1` environment
  variable that short-circuits the check entirely. This is for power users
  who manage their own venv / poetry env and for our own CI.

- **FR-6**: A best-effort marker file (`~/.loopd/.bootstrap_ok`) MUST be
  written after a successful import check so subsequent runs can skip even
  the import probe. The marker MUST include the absolute path of the resolved
  Python interpreter; if the interpreter path changes (user reinstalls
  Homebrew, swaps `LOOPD_PYTHON`, etc.) the probe re-runs.

- **FR-7**: `README.md` MUST gain a short "Troubleshooting → ModuleNotFoundError"
  section documenting the manual fallback (`python3 -m pip install
  --break-system-packages 'pydantic>=2.0' 'pydantic-settings>=2.0'
  'PyYAML>=6.0'`) and the `LOOPD_PYTHON` / `LOOPD_SKIP_BOOTSTRAP` env vars.

- **FR-8**: Existing call sites (`scripts/tick_wrapper.sh`, hooks calling
  `python_core/scripts/tick`) MUST keep working without modification — all
  changes are localised to `python_core/scripts/tick`.

## Non-Functional Requirements

- **NFR-1 (Performance)**: No-op path adds < 200 ms (one `python -c` import
  probe). Once the marker file exists, adds < 5 ms (a `[[ -f … ]]` check).

- **NFR-2 (Safety)**: No `sudo`, no global site-packages writes by default
  (`--user` first). Never modify `pip` config or the system interpreter.

- **NFR-3 (Portability)**: Shell script stays `/usr/bin/env bash` with
  `set -euo pipefail`. Compatible with macOS bash 3.2 and Linux bash 5.x. No
  GNU-only flags (no `mapfile`, no `readarray`).

- **NFR-4 (Determinism)**: stdout MUST remain the tick FSM JSON. All
  bootstrap chatter goes to stderr. Hooks reading `proc.stdout` must not
  break.

- **NFR-5 (Backwards compatibility)**: A user who already manually installed
  the deps sees zero new behaviour — the import probe passes, marker is
  written, exec proceeds.

## User Stories

- **US-1**: As a new loopd user on a fresh laptop, I run
  `/plugin install loopd@loopd` then `/loopd:research-task "…"` and the
  command works on the first try — no `ModuleNotFoundError` traceback.

- **US-2**: As a user with Homebrew Python where `python3` and `pip3` point
  to different interpreters, the bootstrap still works because it uses
  `python3 -m pip` against the resolved interpreter.

- **US-3**: As a user on a PEP 668-locked distro (Debian 12+, recent Ubuntu),
  the `--user` install succeeds and I don't have to think about
  `--break-system-packages`.

- **US-4**: As a CI / dev who already has the deps in a venv, I export
  `LOOPD_SKIP_BOOTSTRAP=1` and `tick` skips the probe entirely.

- **US-5**: As a maintainer, the change is < 30 lines of bash in one file
  plus a README paragraph — easy to review and revert.

## Acceptance Criteria

- **AC-1**: On a host where `python3 -c 'import pydantic'` currently fails,
  running `python_core/scripts/tick --help` (or any subcommand) exits 0 after
  performing the install, prints exactly one `[loopd] Installing …` line to
  stderr, and `python3 -c 'import pydantic, pydantic_settings, yaml'`
  subsequently succeeds.

- **AC-2**: On a host where the imports already succeed, the second-and-later
  `tick` invocations make no `pip` call and produce no extra stderr lines.
  Verified with `strace -e execve` (or by timing — < 200 ms overhead).

- **AC-3**: `~/.loopd/.bootstrap_ok` exists after a successful run and
  contains a JSON object with at least `python` (absolute path) and
  `checked_at` (ISO-8601 UTC).

- **AC-4**: `LOOPD_SKIP_BOOTSTRAP=1 tick --help` runs the FSM **without**
  probing imports (verified by `ltrace` or by deleting `pydantic` and seeing
  the original ModuleNotFoundError surface).

- **AC-5**: When both `--user` and `--break-system-packages` installs fail
  (simulated by `PIP_INDEX_URL=http://0.0.0.0/`), `tick` exits with code
  ≠ 0, the stderr contains the manual `pip install` command and a link to
  README troubleshooting, and `~/.loopd/.bootstrap_ok` is **not** written.

- **AC-6**: stdout from `tick init …` is still a single JSON line — no
  bootstrap text leaks into the FSM contract. Confirmed by piping into
  `python3 -c 'import json,sys;json.loads(sys.stdin.read())'`.

- **AC-7**: README contains an `Install → Troubleshooting` subsection that
  spells out the manual `pip install --break-system-packages` workaround and
  the `LOOPD_PYTHON` / `LOOPD_SKIP_BOOTSTRAP` overrides.

- **AC-8**: `scripts/tick_wrapper.sh`, `hooks/stop_continue.py`, and
  `hooks/posttool_record.py` are **not** modified — bootstrap is fully
  contained in `python_core/scripts/tick`.

## Out of Scope (future)

- A managed venv under `~/.local/share/loopd/venv` (issue mentions this as
  option 2). Lower priority — the `--user` / `--break-system-packages`
  cascade in option 1 is enough to unblock the install. Tracked as a
  follow-up.
