#!/usr/bin/env bash
# Loopd Stop hook — keeps the pipeline running by injecting the next tick
# next_action back to the main LLM when the FSM still has work to do.
exec python3 "${CLAUDE_PLUGIN_ROOT}/hooks/stop_continue.py"
