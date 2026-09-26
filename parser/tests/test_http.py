import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from conftest import make_settings
from leadradar_parser import ParserSettings, create_http_client
from leadradar_parser.errors import RobotsDenied, SourceBlocked, SourceRateLimited
from leadradar_parser.http import HttpClient


async def test_http_blocklist_rejects_before_network(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: pytest.fail(f"network called: {request.url}"))
    async with HttpClient(make_settings(tmp_path), transport=transport) as client:
        for url in (
            "https://www.linkedin.com/company/example",
            "https://de.indeed.com/jobs",
            "https://www.glassdoor.co.uk/Reviews",
            "https://x.com/example",
        ):
            with pytest.raises(SourceBlocked):
                await client.get(url)


async def test_http_redirect_to_blocked_host_is_rejected(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://www.linkedin.com/company/example"})

    async with HttpClient(make_settings(tmp_path), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceBlocked):
            await client.get("https://example.com/social")
    assert "www.linkedin.com" not in seen


@respx.mock
async def test_http_robots_deny_happens_before_page_request(tmp_path: Path) -> None:
    robots = respx.get("https://example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    page = respx.get("https://example.com/private/data").mock(return_value=httpx.Response(200, text="secret"))
    async with HttpClient(make_settings(tmp_path)) as client:
        with pytest.raises(RobotsDenied):
            await client.get("https://example.com/private/data")
    assert robots.call_count == 1
    assert page.call_count == 0


@respx.mock
async def test_http_429_honours_retry_after_and_becomes_domain_error(tmp_path: Path) -> None:
    route = respx.get("https://api.example.com/data").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "42"})
    )
    async with HttpClient(make_settings(tmp_path, retry_attempts=2)) as client:
        with pytest.raises(SourceRateLimited) as raised:
            await client.get("https://api.example.com/data", check_robots=False)
    assert raised.value.retry_after_s == 42
    assert route.call_count == 2  # one retry, wait capped by retry_max_wait_s=0


@respx.mock
async def test_http_repeat_request_is_served_from_hishel_cache_without_network(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    route = respx.get("https://api.example.com/jobs").mock(
        return_value=httpx.Response(200, json={"jobs": [1]})  # no Cache-Control: cached by our policy
    )
    async with HttpClient(settings) as client:
        first = await client.get("https://api.example.com/jobs", check_robots=False)
        second = await client.get("https://api.example.com/jobs", check_robots=False)
        assert client.network_requests == 1
    assert first.json() == second.json() == {"jobs": [1]}
    assert second.extensions.get("hishel_from_cache") is True

    # A new client (next `collect` run) within the TTL must not touch the network at all.
    route.mock(side_effect=httpx.ConnectError("offline"))
    async with HttpClient(settings) as client:
        third = await client.get("https://api.example.com/jobs", check_robots=False)
        assert client.network_requests == 0
    assert third.json() == {"jobs": [1]}
    assert route.call_count == 1


@respx.mock
async def test_http_errors_and_posts_are_not_cached(tmp_path: Path) -> None:
    failing = respx.get("https://api.example.com/flaky").mock(
        side_effect=[httpx.Response(404), httpx.Response(200, json={"ok": True})]
    )
    search = respx.post("https://api.example.com/search").mock(return_value=httpx.Response(200, json={}))
    async with HttpClient(make_settings(tmp_path)) as client:
        with pytest.raises(Exception, match="404"):
            await client.get("https://api.example.com/flaky", check_robots=False)
        assert (await client.get("https://api.example.com/flaky", check_robots=False)).json() == {"ok": True}
        await client.post("https://api.example.com/search", json={"q": 1})
        await client.post("https://api.example.com/search", json={"q": 1})
    assert failing.call_count == 2
    assert search.call_count == 2


@respx.mock
async def test_http_cache_lives_in_parser_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache_dir = tmp_path / "from-env"
    monkeypatch.setenv("PARSER_CACHE_DIR", str(cache_dir))
    assert ParserSettings().cache_dir == cache_dir
    respx.get("https://api.example.com/x").mock(return_value=httpx.Response(200, text="x"))
    async with create_http_client() as client:
        await client.get("https://api.example.com/x", check_robots=False)
    assert (cache_dir / "http.sqlite3").is_file()


async def test_http_max_concurrency_caps_parallel_network_requests(tmp_path: Path) -> None:
    active = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, text="ok")

    settings = make_settings(tmp_path, max_concurrency=2)
    async with HttpClient(settings, transport=httpx.MockTransport(handler), cache=False) as client:
        await asyncio.gather(
            *(client.get(f"https://host{index}.example/", check_robots=False) for index in range(6))
        )
        assert client.network_requests == 6
    assert peak == 2


async def test_http_fractional_host_rps_is_supported(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    settings = make_settings(tmp_path, host_rps=0.5)
    async with HttpClient(settings, transport=transport, cache=False) as client:
        response = await client.get("https://example.com/", check_robots=False)
    assert response.text == "ok"


def test_http_redact_url_hides_api_keys() -> None:
    from leadradar_parser.http import redact_url

    url = "https://serpapi.com/search.json?q=DHL&api_key=secret&apiKey=x&user_key=y"
    assert redact_url(url) == "https://serpapi.com/search.json?q=DHL&api_key=***&apiKey=***&user_key=***"
    assert redact_url("https://example.com/a") == "https://example.com/a"


async def test_http_total_deadline_stops_tarpitting_hosts(tmp_path: Path) -> None:
    """httpx timeouts are per read; a host that never finishes must still fail within request_timeout_s."""

    async def tarpit(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return httpx.Response(200)

    import time

    from leadradar_parser.errors import SourceTimeout

    settings = make_settings(tmp_path, request_timeout_s=0.2, retry_attempts=1)
    async with HttpClient(settings, transport=httpx.MockTransport(tarpit), cache=False) as client:
        started = time.monotonic()
        with pytest.raises(SourceTimeout):
            await client.get("https://slow.example/", check_robots=False)
    assert time.monotonic() - started < 2
