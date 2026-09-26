class ParserError(Exception):
    """Base exception for the parser package."""


class SourceBlocked(ParserError):
    """The target host is forbidden by policy."""


class RobotsDenied(ParserError):
    """robots.txt disallows the requested URL."""


class SourceRateLimited(ParserError):
    def __init__(self, message: str, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class SourceRequestFailed(ParserError):
    """A source failed after retries."""


class SourceTimeout(SourceRequestFailed):
    """A source did not answer within the request timeout."""


class SourceTooLarge(SourceRequestFailed):
    """A download exceeded its size limit."""


class SourceDisabled(ParserError):
    """The adapter is not configured (a credential is missing)."""
