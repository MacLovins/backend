"""Domain exceptions of leadradar-ai; core translates them to run statuses and HTTP."""


class LeadRadarAIError(Exception):
    """Base class for all leadradar-ai errors."""


class QuotaExhausted(LeadRadarAIError):
    """Every model of the pool is out of RPM/RPD budget: the company is paused and resumed from checkpoint."""

    def __init__(self, pool: str, message: str | None = None) -> None:
        self.pool = pool
        super().__init__(message or f"LLM quota exhausted for pool '{pool}'")


class ExtractionFailed(LeadRadarAIError):
    """The LLM output could not be obtained or validated even after the repair attempt."""


class LLMUnavailable(LeadRadarAIError):
    """Every model of the pool failed with server or network errors after retries."""


class LLMBadRequest(LeadRadarAIError):
    """The API rejected the request itself (4xx other than 429): retrying will not help."""


class LLMInputTooLarge(LeadRadarAIError):
    """The prompt exceeds LLM_MAX_INPUT_TOKENS; the caller must split it (prefilter makes two calls)."""
