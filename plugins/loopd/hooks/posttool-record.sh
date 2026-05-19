#!/usr/bin/env bash
# Loopd PostToolUse hook — pipes the Task tool result into tick --record.
exec python3 "${CLAUDE_PLUGIN_ROOT}/hooks/posttool_record.py"
