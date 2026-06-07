#!/usr/bin/env bash
# Orchestrator Stop hook entrypoint.
#
# Per docs/orchestrator-design.md §20: overwrite PYTHONPATH (not prepend) to
# block other plugins from injecting a same-name `orchestrator_state` module,
# and use `python -I` (isolated mode) for the same reason.
set -uo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-}"
if [ -z "${PLUGIN_ROOT}" ]; then
    # Fallback: derive from this script's location.
    PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fi

export PYTHONPATH="${PLUGIN_ROOT}/python_helpers"

PYTHON_BIN="${ORCH_PYTHON_BIN:-python3}"

exec "${PYTHON_BIN}" -I "${PLUGIN_ROOT}/hooks/orch_stop.py"
