#!/usr/bin/env bash
# Loopd PreToolUse hook — validates Task tool invocations against the last
# tick.py output. Delegates to a Python helper to avoid jq dependency.
exec python3 "${CLAUDE_PLUGIN_ROOT}/hooks/pretool_validate.py"
