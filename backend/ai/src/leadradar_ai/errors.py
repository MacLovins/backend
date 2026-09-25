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
