# Architecture — Self-bootstrap in `python_core/scripts/tick`

## Surface area

| File                                                          | Change   |
|---------------------------------------------------------------|----------|
| `plugins/loopd/python_core/scripts/tick`                      | **MODIFY** — insert bootstrap block before the existing `exec python3 -m loopd_core.tick`. |
| `README.md`                                                   | **MODIFY** — add a Troubleshooting subsection. |
| `plugins/loopd/scripts/tick_wrapper.sh`                       | no change — already delegates to `python_core/scripts/tick`. |
| `plugins/loopd/python_core/pyproject.toml`                    | no change — declared deps stay authoritative. |
| `plugins/loopd/hooks/{posttool_record.py,stop_continue.py,pretool_validate.py}` | no change. |

Everything is funneled through `python_core/scripts/tick` (see
`hooks/stop_continue.py:51` and `hooks/posttool_record.py:98`, both of which
build `tick_path = plugin_root/python_core/scripts/tick`), so a single edit
covers all entry points.

## Data flow (after the change)

```
                /loopd:research-task "<args>"                  (slash command)
                            │
                            ▼
        scripts/tick_wrapper.sh   ──exec──▶  python_core/scripts/tick
                                                       │
                                                       ▼
                              ┌───────────────────────────────────────────┐
                              │  NEW bootstrap block (lines ~5-50)        │
                              │  1. resolve $PY                           │
                              │  2. if LOOPD_SKIP_BOOTSTRAP → skip        │
                              │  3. if ~/.loopd/.bootstrap_ok matches $PY │
                              │     → skip probe                          │
                              │  4. "$PY" -c 'import pydantic,            │
                              │              pydantic_settings, yaml'     │
                              │     → on success: write marker, fall thru │
                              │     → on failure: install (see below),    │
                              │                   re-probe, write marker  │
                              └───────────────────────────────────────────┘
                                                       │
                                                       ▼
                  exec "$PY" -m loopd_core.tick "$@"   (PYTHONPATH set)
                                                       │
                                                       ▼
                                       stdout: JSON next_action   ──▶ hooks
```

### Install cascade

```
"$PY" -m pip install --user --quiet 'pydantic>=2.0' \
                                    'pydantic-settings>=2.0' \
                                    'PyYAML>=6.0'
    │
    │ fail (exit != 0)
    ▼
"$PY" -m pip install --break-system-packages --quiet ...
    │
    │ fail
    ▼
print manual fix to stderr; exit 1
```

## Why a bash bootstrap (not Python)

- The current `tick` is bash; touching only bash keeps the diff tiny and
  reviewable.
- The Python interpreter itself may not be able to import its own deps yet —
  any Python-side bootstrap would have to run in a separate, dep-free
  Python module first. Adding such a module is more code than the bash
  variant.
- `bash` is already a hard prerequisite (hooks invoke `bash`).

## Marker file design

Path: `${LOOPD_ROOT:-$HOME/.loopd}/.bootstrap_ok`

Contents (single JSON line):
```json
{"python":"/opt/homebrew/opt/python@3.12/bin/python3.12","checked_at":"2026-05-20T07:14:00Z","version":"0.1.0"}
```

Why JSON: future-proof for additional checks (e.g. dep version pinning). The
shell parses with `grep -o` rather than pulling in `jq`.

Why include the interpreter path: handles the user upgrading Homebrew (path
changes), swapping `LOOPD_PYTHON`, or moving to a venv. If the resolved
`$PY -c 'import sys;print(sys.executable)'` differs from the marker's
`python`, the probe re-runs.

Why the file is best-effort: if the write fails (e.g. read-only home) we log
a warning to stderr but continue — the probe will just rerun next time.

## Stdout / stderr discipline

- `tick`'s stdout is consumed as JSON by `stop_continue.py` (line 60-67) and
  the slash command (`commands/dev-task.md:25-44`). **Nothing** the
  bootstrap prints may go to stdout.
- All bootstrap output (the one `[loopd] Installing …` line, pip's stderr if
  any, the manual-fix message on failure) goes to stderr only.
- `set -euo pipefail` is preserved. The install commands use `||` to fall
  through to the next strategy without aborting.

## Failure modes

| Symptom                              | Behaviour                                                        |
|--------------------------------------|------------------------------------------------------------------|
| `$PY` not on PATH                    | bash `command -v` fails → print "loopd: python3 not found"       |
| Network unreachable during pip       | `--user` fails, `--break-system-packages` fails → manual-fix msg |
| Marker file write fails              | Warn to stderr, continue                                         |
| User has venv but didn't activate    | Probe under venv's `python` succeeds — marker reflects venv path |
| User flipped `LOOPD_PYTHON` to a new interpreter | Marker mismatch → re-probe with new interpreter        |

## Dependencies / risk

- No new runtime dependencies. Uses only `bash` builtins + `python3`.
- Risk: `pip install --user` on a system where `~/.local/lib/python3.X/site-packages`
  is **not** on `sys.path` would re-install on every run. Mitigated by the
  re-probe after install — if it still fails after `--user`, we escalate to
  `--break-system-packages`, which always lands in the interpreter's
  site-packages.
- Risk: `--break-system-packages` is harmless on systems without PEP 668
  (older Python ignores the flag). Confirmed via `python3 -m pip install
  --help` on bash 3.2 / Python 3.11+ — flag is recognised since pip 23.0.

## Test plan summary

- Manual: blow away pydantic, run `tick --help`, verify the install path.
- Manual: with marker present, run `tick --help` twice; confirm second
  invocation makes zero pip calls (use `PIP_DISABLE_PIP_VERSION_CHECK=1
  PIP_INDEX_URL=http://0.0.0.0/ tick --help` — would fail on install path).
- Manual: `LOOPD_SKIP_BOOTSTRAP=1 tick --help` with deps absent → original
  `ModuleNotFoundError`.
- Manual: `tick init …` stdout pipes cleanly into `json.loads`.

(Implementation phase will own automating these into a smoke test if the
codebase grows one; currently the repo has no test suite.)
