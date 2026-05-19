#!/usr/bin/env bash
# loopd tick_wrapper — plugin-root level entry point for the FSM driver.
# Delegates to python_core/scripts/tick so the actual logic stays beside the
# Python package. This file exists to satisfy issue #1174's published shape
# (scripts/tick_wrapper.sh) and to give hooks / docs a stable path that does
# not reach into python_core/.
set -euo pipefail

if [[ -n "${CLAUDE_PLUGIN_ROOT:-}" ]]; then
  TICK_BIN="${CLAUDE_PLUGIN_ROOT}/python_core/scripts/tick"
else
  TICK_BIN="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/python_core/scripts/tick"
fi

exec "${TICK_BIN}" "$@"
