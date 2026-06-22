"""Event logging and LLM provider for the post-hoc judge system."""
from __future__ import annotations
import logging, subprocess
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

@dataclass
class _CLIResponse:
    content: str
    exit_code: int
    stderr: str
    is_error: bool
    is_rate_limited: bool = False
    is_timeout: bool = False

class SubprocessLLMProvider:
    def run(self, prompt: str, task_id: str, timeout: int, model: str) -> _CLIResponse:
        try:
            r = subprocess.run(["claude", "-p", prompt, "--model", model], capture_output=True, text=True, timeout=timeout)
            stderr = r.stderr.strip()
            return _CLIResponse(
                content=r.stdout.strip(), exit_code=r.returncode, stderr=stderr,
                is_error=r.returncode != 0,
                is_rate_limited="rate limit" in stderr.lower() or "overloaded" in stderr.lower() or r.returncode == 2,
            )
        except subprocess.TimeoutExpired:
            return _CLIResponse(content="", exit_code=-1, stderr="timeout", is_error=True, is_timeout=True)
        except FileNotFoundError:
            return _CLIResponse(content="", exit_code=-1, stderr="claude CLI not found", is_error=True)

def make_isolated_llm_provider(model: str) -> SubprocessLLMProvider:
    return SubprocessLLMProvider()

def log_judge_event(event_logger: Any, event_type: str, task_id: Optional[str], data: Optional[dict[str, Any]] = None) -> None:
    try:
        event_logger.log(f"posthoc_judge.{event_type}", task_id, data)
    except Exception as e:
        logger.warning(f"Failed to log judge event {event_type}: {e}")
