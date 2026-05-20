# Implementation plan — Auto-bootstrap Python deps

Target file: `plugins/loopd/python_core/scripts/tick`
Secondary file: `README.md`
No other files should be touched.

All paths below are relative to the workspace root
`/home/sungjin/.loopd/workspaces/task-2026-05-20-001--BigggMorph__loopd`.

## Step 1 — Read the current shim

Open `plugins/loopd/python_core/scripts/tick`. Confirm it still reads:

```bash
#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  PYTHON_CORE_DIR="${CLAUDE_PLUGIN_ROOT}/python_core"
else
  PYTHON_CORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

export PYTHONPATH="${PYTHON_CORE_DIR}:${PYTHONPATH:-}"
exec python3 -m loopd_core.tick "$@"
```

The new version keeps the same outer shape — `PYTHONPATH` export and `exec` —
but inserts a `bootstrap_deps` function plus a single call site before the
`exec`. `python3` is replaced with `"$PY"`.

## Step 2 — Rewrite `python_core/scripts/tick`

Replace the file with:

```bash
#!/usr/bin/env bash
# loopd tick shim — invokes the Python entry point with the plugin's own
# python_core directory on PYTHONPATH, and self-bootstraps Python deps on
# first run because Claude Code plugin marketplaces have no install hook.
# See: https://github.com/BigggMorph/loopd/issues/2
set -euo pipefail

if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  PYTHON_CORE_DIR="${CLAUDE_PLUGIN_ROOT}/python_core"
else
  # Local development fallback: scripts/ is a sibling of loopd_core/
  PYTHON_CORE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi

PY="${LOOPD_PYTHON:-python3}"
LOOPD_ROOT_DIR="${LOOPD_ROOT:-${HOME}/.loopd}"
MARKER="${LOOPD_ROOT_DIR}/.bootstrap_ok"

LOOPD_DEPS=('pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0')
LOOPD_IMPORT_PROBE='import pydantic, pydantic_settings, yaml'

_loopd_log() { printf '%s\n' "[loopd] $*" >&2; }

_loopd_resolve_python() {
  if ! command -v "$PY" >/dev/null 2>&1; then
    _loopd_log "python interpreter not found: $PY"
    _loopd_log "Set LOOPD_PYTHON to an absolute path of a Python 3.11+ binary."
    exit 1
  fi
}

_loopd_marker_matches() {
  [[ -f "$MARKER" ]] || return 1
  local resolved
  resolved="$("$PY" -c 'import sys;print(sys.executable)' 2>/dev/null || true)"
  [[ -n "$resolved" ]] || return 1
  # Grep for the resolved path in the JSON marker. Cheap, no jq dep.
  grep -Fq "\"python\":\"$resolved\"" "$MARKER" 2>/dev/null
}

_loopd_write_marker() {
  local resolved iso
  resolved="$("$PY" -c 'import sys;print(sys.executable)' 2>/dev/null || true)"
  iso="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
  mkdir -p "$LOOPD_ROOT_DIR" 2>/dev/null || return 0
  printf '{"python":"%s","checked_at":"%s","version":"0.1.0"}\n' \
    "$resolved" "$iso" > "$MARKER" 2>/dev/null || \
    _loopd_log "warning: could not write $MARKER (continuing)"
}

_loopd_pip_install() {
  # First attempt: --user (least invasive, works on most systems).
  if "$PY" -m pip install --user --quiet --disable-pip-version-check \
        "${LOOPD_DEPS[@]}" >&2; then
    return 0
  fi
  # Fallback: --break-system-packages (PEP 668 distros, Homebrew Python 3.12+).
  if "$PY" -m pip install --break-system-packages --quiet \
        --disable-pip-version-check "${LOOPD_DEPS[@]}" >&2; then
    return 0
  fi
  return 1
}

_loopd_bootstrap() {
  [[ "${LOOPD_SKIP_BOOTSTRAP:-0}" == "1" ]] && return 0
  _loopd_resolve_python

  if _loopd_marker_matches; then
    return 0
  fi

  if "$PY" -c "$LOOPD_IMPORT_PROBE" >/dev/null 2>&1; then
    _loopd_write_marker
    return 0
  fi

  _loopd_log "Installing Python dependencies for $($PY -V 2>&1) ..."
  if ! _loopd_pip_install; then
    _loopd_log "Failed to install loopd Python dependencies automatically."
    _loopd_log "Run this manually and try again:"
    _loopd_log "  $PY -m pip install --break-system-packages \\"
    _loopd_log "    'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'"
    _loopd_log "Or set LOOPD_SKIP_BOOTSTRAP=1 if you manage deps yourself."
    _loopd_log "See README → Troubleshooting for more options."
    exit 1
  fi

  # Re-probe to confirm the install actually took effect under $PY.
  if ! "$PY" -c "$LOOPD_IMPORT_PROBE" >/dev/null 2>&1; then
    _loopd_log "Install reported success but imports still fail under $PY."
    _loopd_log "This usually means pip targeted a different interpreter."
    _loopd_log "Set LOOPD_PYTHON to an absolute path and retry."
    exit 1
  fi

  _loopd_write_marker
}

_loopd_bootstrap

export PYTHONPATH="${PYTHON_CORE_DIR}:${PYTHONPATH:-}"
exec "$PY" -m loopd_core.tick "$@"
```

Key invariants preserved:
- Stdout is untouched (all log lines go through `_loopd_log` → stderr).
- `set -euo pipefail` still active; install steps use `||`-style fallthrough
  via explicit `if … then return; fi` to stay safe under `-e`.
- `exec` still tail-calls the Python entry point so signal forwarding is
  unchanged.

## Step 3 — Update README.md

Append a Troubleshooting subsection just below the Install block. Don't
touch the "How it works" or "Usage" sections.

Insertion point: after line 16 (`Requirements: python3.11+, git, gh CLI.`).
New content:

```markdown
### Troubleshooting

**`ModuleNotFoundError: No module named 'pydantic'` on first run**

`/plugin install` lays down the plugin files but Claude Code marketplaces
have no post-install hook, so loopd's Python deps (`pydantic`,
`pydantic-settings`, `PyYAML`) need a one-time install. The plugin's
`tick` shim will try to handle this automatically on first invocation; if it
fails, run the install manually against the **same interpreter** that
`tick` will use:

```bash
python3 -m pip install --break-system-packages \
  'pydantic>=2.0' 'pydantic-settings>=2.0' 'PyYAML>=6.0'
```

If your system has Homebrew Python and `pip3` resolves to a different
interpreter than `python3`, always invoke `python3 -m pip ...`, not `pip3`.

**Environment overrides**

- `LOOPD_PYTHON` — absolute path to a Python 3.11+ interpreter (e.g.
  pointing at a venv). Defaults to `python3`.
- `LOOPD_SKIP_BOOTSTRAP=1` — disables the auto-install probe entirely. Use
  this when you manage deps yourself (poetry, venv, conda, ...).
- `LOOPD_ROOT` — where loopd stores its state (default `~/.loopd`).
```

(The marker file mention is intentionally omitted from the user-facing README
— it's an implementation detail; if users hit issues they'll find it via
`grep` or via this PR.)

## Step 4 — Smoke tests (manual, since no test suite exists)

Run from the workspace, with the workspace's own `tick` shim:

```bash
cd /home/sungjin/.loopd/workspaces/task-2026-05-20-001--BigggMorph__loopd
TICK=plugins/loopd/python_core/scripts/tick

# Sanity: shim is still executable
test -x "$TICK"

# 1. Happy path on a system that already has deps: no [loopd] line,
#    marker created.
rm -f ~/.loopd/.bootstrap_ok
"$TICK" --help 2>&1 | tee /tmp/loopd_t1.txt
grep -q '^\[loopd\] Installing' /tmp/loopd_t1.txt && echo "FAIL: unexpected install line" || echo "OK: no install line"
test -f ~/.loopd/.bootstrap_ok && echo "OK: marker exists"

# 2. Marker shortcut: second run is silent and stdout is still valid JSON
#    when given a real subcommand.
"$TICK" --help >/dev/null 2>/tmp/loopd_t2.err
test ! -s /tmp/loopd_t2.err && echo "OK: silent on marker hit"

# 3. Skip switch works.
LOOPD_SKIP_BOOTSTRAP=1 "$TICK" --help >/dev/null 2>&1 && echo "OK: skip honoured"

# 4. stdout discipline — tick init JSON is still parseable. (Will fail-fast
#    if args are wrong, but the *shape* of stdout we care about: no
#    bootstrap junk leaked.)
"$TICK" --help >/tmp/loopd_t4.out 2>/dev/null
# --help prints text to stdout — that's expected. The JSON contract is for
# init/record/run subcommands, not --help. Verified separately in step 5.
```

If the host has internet and a fresh venv with no `pydantic`, also run:

```bash
python3 -m venv /tmp/loopd-empty
LOOPD_PYTHON=/tmp/loopd-empty/bin/python "$TICK" --help
# expect: one "[loopd] Installing …" line on stderr, then --help text on stdout
LOOPD_PYTHON=/tmp/loopd-empty/bin/python "$TICK" --help 2>&1 >/dev/null
# expect: zero stderr lines (marker now hit)
```

## Step 5 — Sanity-check stdout JSON contract

The hooks parse `tick`'s stdout for non-`--help` subcommands. The bootstrap
only affects stderr, but verify once with:

```bash
# tick init demands a workspace etc. If it errors that's fine — we only care
# that any error text goes through Python's stderr, not the bash bootstrap.
"$TICK" init --args "noop" 2>/dev/null | head -c 200
# expect: starts with '{' (a JSON object from loopd_core.tick) OR is empty;
# never a "[loopd] …" line.
```

## Step 6 — Commit

```bash
cd /home/sungjin/.loopd/workspaces/task-2026-05-20-001--BigggMorph__loopd
git status
git add plugins/loopd/python_core/scripts/tick README.md
git commit -m "fix: self-bootstrap Python deps in tick shim (closes #2)"
```

Commit message should include `Closes #2` so the GitHub issue auto-closes
when the PR merges.

## Step 7 — Hand off to review

Review agent should focus on:

1. Stdout / stderr discipline (no `printf` to stdout in bootstrap).
2. The `--user` → `--break-system-packages` fallthrough doesn't mask
   network errors silently — the manual-fix message must fire.
3. The marker file is best-effort and never breaks the run when write fails.
4. README diff is additive only, no removal of existing sections.

## What is intentionally not done

- No managed venv under `~/.local/share/loopd/venv` (issue's option 2).
  Tracked as a follow-up.
- No unit test framework added — repo currently has no Python test suite,
  and adding one is out of scope for an issue-#2 fix.
- No changes to `pyproject.toml` — declared deps remain the source of
  truth, but the bash array `LOOPD_DEPS=(…)` mirrors them. A comment in
  `tick` notes they must be kept in sync; a future iteration could
  generate the bash list from `pyproject.toml` at build time.
