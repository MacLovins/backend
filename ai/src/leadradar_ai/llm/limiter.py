"""Per-model RPM (sliding window, in process — one worker) and RPD (UsageSink, survives restarts).

Gemini daily quotas reset at midnight Pacific time; a model that answered 429 "per day" is skipped until then.
"""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import date, datetime
from zoneinfo import ZoneInfo

from leadradar_ai.ports import UsageSink
from leadradar_ai.settings import LLMSettings

PACIFIC = ZoneInfo("America/Los_Angeles")
WINDOW_S = 60.0


def pacific_today() -> date:
    return datetime.now(PACIFIC).date()


class NoDailyBudget(Exception):
    """None of the requested models has RPD left today."""


class RateLimiter:
    def __init__(
        self,
        settings: LLMSettings,
        usage: UsageSink,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        today: Callable[[], date] = pacific_today,
    ) -> None:
        self._settings = settings
        self._usage = usage
        self._clock = clock
        self._sleep = sleep
        self._today = today
        self._calls: dict[str, deque[float]] = {}
        self._exhausted: dict[str, date] = {}

    def mark_exhausted(self, model: str) -> None:
        self._exhausted[model] = self._today()

    async def has_daily_budget(self, model: str) -> bool:
        if self._exhausted.get(model) == self._today():
            return False
        rpd = self._settings.limits(model).rpd
        return rpd is None or await self._usage.used_today(model) < rpd

    def _rpm_wait(self, model: str) -> float:
        """Seconds until the model has a free slot in the last-minute window."""
        rpm = self._settings.limits(model).rpm
        if rpm is None:
            return 0.0
        calls = self._calls.setdefault(model, deque())
        now = self._clock()
        while calls and now - calls[0] >= WINDOW_S:
            calls.popleft()
        return 0.0 if len(calls) < rpm else WINDOW_S - (now - calls[0])

    def _take(self, model: str) -> None:
        self._calls.setdefault(model, deque()).append(self._clock())

    async def acquire(self, models: list[str]) -> str:
        """First model with RPD left and a free RPM slot; waits for a slot if all are busy this minute."""
        while True:
            candidates = [m for m in models if await self.has_daily_budget(m)]
            if not candidates:
                raise NoDailyBudget(models)
            waits = [(self._rpm_wait(m), m) for m in candidates]
            for wait, model in waits:
                if wait <= 0:
                    self._take(model)
                    return model
            await self._sleep(min(w for w, _ in waits))
