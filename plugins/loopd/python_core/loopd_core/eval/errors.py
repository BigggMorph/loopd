"""Error taxonomy for the post-hoc judge system."""
from __future__ import annotations
from enum import Enum

class JudgeErrorType(str, Enum):
    PARSE_ERROR = "parse_error"
    SCHEMA_ERROR = "schema_error"
    LLM_ERROR = "llm_error"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    INPUT_ERROR = "input_error"
    RUBRIC_ERROR = "rubric_error"
    IO_ERROR = "io_error"
    UNKNOWN = "unknown"

class JudgeError(Exception):
    def __init__(self, message: str, error_type: JudgeErrorType = JudgeErrorType.UNKNOWN):
        super().__init__(message)
        self.error_type = error_type

RETRY_BACKOFF_SECONDS = (5, 15, 60)
MAX_RETRIES = 3
QUARANTINE_AFTER_N_ERRORS = 3
