import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from weakref import WeakKeyDictionary

import httpx
from aiolimiter import AsyncLimiter
from hishel import AsyncSqliteStorage, BaseFilter, FilterPolicy, Request, Response
from hishel.httpx import AsyncCacheTransport
from protego import Protego
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .errors import RobotsDenied, SourceBlocked, SourceRateLimited, SourceRequestFailed, SourceTimeout
from .settings import ParserSettings

BLOCKED_HOSTS = {"linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com"}
BLOCKED_LABELS = {"indeed", "glassdoor"}
RETRYABLE_ERRORS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)

_LOOP_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"HTTP {response.status_code}: {response.request.url}")


class _CacheableRequest(BaseFilter[Request]):
    def needs_body(self) -> bool:
        return False

    def apply(self, item: Request, body: bytes | None) -> bool:
        return item.method.upper() == "GET"


class _CacheableResponse(BaseFilter[Response]):
    def needs_body(self) -> bool:
        return False

    def apply(self, item: Response, body: bytes | None) -> bool:
        return item.status_code == 200


def is_blocked_host(host: str) -> bool:
    normalized = host.lower().rstrip(".")
    if any(normalized == item or normalized.endswith(f".{item}") for item in BLOCKED_HOSTS):
        return True
    labels = normalized.split(".")
    return any(label in BLOCKED_LABELS for label in labels)


def ensure_allowed_host(host: str) -> None:
    if is_blocked_host(host):
        raise SourceBlocked(f"Requests to {host} are forbidden by source policy")


def shared_lock(name: str) -> asyncio.Lock:
    """A process-wide lock per event loop (module-level asyncio locks break across loops)."""
    loop = asyncio.get_running_loop()
    return _LOOP_LOCKS.setdefault(loop, {}).setdefault(name, asyncio.Lock())


class _PoliteTransport(httpx.AsyncBaseTransport):
    """Network-only layer below the cache: block-list (also for redirects), host rate limit, concurrency."""

    def __init__(self, inner: httpx.AsyncBaseTransport, settings: ParserSettings) -> None:
        self._inner = inner
        rps = max(settings.host_rps, 0.001)
        self._limiters: defaultdict[str, AsyncLimiter] = defaultdict(
            lambda: AsyncLimiter(1, 1 / rps) if rps < 1 else AsyncLimiter(rps, 1)
        )
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrency))
        self.requests_sent = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        ensure_allowed_host(host)
        async with self._limiters[host], self._slots:
            self.requests_sent += 1
            response = await self._inner.handle_async_request(request)
            await response.aread()
            return response

    async def aclose(self) -> None:
        await self._inner.aclose()


class HttpClient:
    def __init__(
        self,
        settings: ParserSettings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        cache: bool = True,
    ) -> None:
        self.settings = settings or ParserSettings()
        self._network = _PoliteTransport(
            transport
            or httpx.AsyncHTTPTransport(
                limits=httpx.Limits(max_connections=max(1, self.settings.max_concurrency))
            ),
            self.settings,
        )
        outer: httpx.AsyncBaseTransport = self._network
        if cache:
            self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
            outer = AsyncCacheTransport(
                next_transport=self._network,
                storage=AsyncSqliteStorage(
                    database_path=self.settings.cache_dir / "http.sqlite3",
                    default_ttl=self.settings.cache_ttl_s,
                ),
                policy=FilterPolicy(
                    request_filters=[_CacheableRequest()], response_filters=[_CacheableResponse()]
                ),
            )
        self._client = httpx.AsyncClient(
            transport=outer,
            headers={"User-Agent": self.settings.user_agent, "Accept": "*/*"},
            timeout=self.settings.request_timeout_s,
            follow_redirects=True,
        )
        self._robots: dict[str, tuple[datetime, Protego | None]] = {}
        self._robots_lock = asyncio.Lock()

    @property
    def network_requests(self) -> int:
        """Requests that actually left the process (cache hits are not counted)."""
        return self._network.requests_sent

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def allowed(self, url: str) -> bool:
        parser = await self._robots_for(url)
        return parser is None or parser.can_fetch(url, self.settings.user_agent)

    async def robots_sitemaps(self, url: str) -> list[str]:
        parser = await self._robots_for(url)
        return list(parser.sitemaps) if parser is not None else []

    async def _robots_for(self, url: str) -> Protego | None:
        parts = urlsplit(url)
        ensure_allowed_host(parts.hostname or "")
        origin = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
        cached = self._robots.get(origin)
        if cached and cached[0] > datetime.now(UTC):
            return cached[1]
        async with self._robots_lock:
            cached = self._robots.get(origin)
            if cached and cached[0] > datetime.now(UTC):
                return cached[1]
            try:
                response = await self._request("GET", f"{origin}/robots.txt")
                parser = Protego.parse(response.text) if response.status_code == 200 else None
            except (SourceRequestFailed, SourceRateLimited):
                parser = None
            expires = datetime.now(UTC) + timedelta(seconds=self.settings.cache_ttl_s)
            self._robots[origin] = (expires, parser)
            return parser

    async def request(
        self,
        method: str,
        url: str,
        *,
        check_robots: bool = True,
        attempts: int | None = None,
        **kwargs: object,
    ) -> httpx.Response:
        ensure_allowed_host(urlsplit(url).hostname or "")
        if check_robots and method.upper() in {"GET", "HEAD"} and not await self.allowed(url):
            raise RobotsDenied(f"robots.txt disallows {url}")
        return await self._request(method, url, attempts=attempts, **kwargs)

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: object) -> httpx.Response:
        kwargs.setdefault("check_robots", False)
        return await self.request("POST", url, **kwargs)

    async def _request(
        self, method: str, url: str, *, attempts: int | None = None, **kwargs: object
    ) -> httpx.Response:
        host = urlsplit(url).hostname or ""
        ensure_allowed_host(host)
        last_response: httpx.Response | None = None
        retrying = AsyncRetrying(
            stop=stop_after_attempt(max(1, attempts or self.settings.retry_attempts)),
            wait=self._wait,
            retry=retry_if_exception_type((*RETRYABLE_ERRORS, _RetryableStatus)),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    response = await self._client.request(method, url, **kwargs)
                    if response.status_code == 429 or response.status_code >= 500:
                        raise _RetryableStatus(response)
                    response.raise_for_status()
                    return response
        except _RetryableStatus as exc:
            last_response = exc.response
        except httpx.TimeoutException as exc:
            raise SourceTimeout(f"Timeout requesting {url}") from exc
        except httpx.TransportError as exc:
            raise SourceRequestFailed(f"Network error requesting {url}: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise SourceRequestFailed(f"HTTP {exc.response.status_code}: {url}") from exc

        if last_response is not None and last_response.status_code == 429:
            retry_after = _retry_after_seconds(last_response.headers.get("Retry-After"))
            raise SourceRateLimited(f"Rate limited by {host}", retry_after)
        status = last_response.status_code if last_response is not None else "unknown"
        raise SourceRequestFailed(f"HTTP {status}: {url}")

    def _wait(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, _RetryableStatus):
            retry_after = _retry_after_seconds(exc.response.headers.get("Retry-After"))
            if retry_after is not None:
                return min(float(retry_after), self.settings.retry_max_wait_s)
        backoff = wait_exponential(
            multiplier=self.settings.retry_min_wait_s,
            min=self.settings.retry_min_wait_s,
            max=self.settings.retry_max_wait_s,
        )
        return backoff(state)


def _retry_after_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()))
        except (TypeError, ValueError):
            return None


def create_http_client(settings: ParserSettings | None = None) -> HttpClient:
    return HttpClient(settings)
