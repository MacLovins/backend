"""Playwright rendering for JavaScript-only pages (SPEC PR-20, P2 fallback).

Off by default. Needs the optional extra (`uv sync --extra browser` / `leadradar-parser[browser]`) and
`playwright install chromium`. The same politeness rules as the HTTP layer apply: block-list and robots.txt
are checked before navigation, and sub-requests to block-listed hosts are aborted.
"""

import logging
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient, is_blocked_host
from ..normalize import canonicalize_url, utc_datetime
from .common import extract_page, make_document

log = logging.getLogger(__name__)
MIN_PAGE_CHARS = 300
NAVIGATION_TIMEOUT_MS = 15_000


class PlaywrightAdapter:
    id = "playwright"
    source_type = "website"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=2, scope="host")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            log.warning("playwright is not installed; install leadradar-parser[browser]")
            return

        targets: list[str] = []
        for url in (company.homepage_url, company.newsroom_url, company.careers_url):
            if not url or canonicalize_url(url) in {canonicalize_url(item) for item in targets}:
                continue
            try:
                if await http.allowed(url):  # raises SourceBlocked for block-listed hosts
                    targets.append(url)
            except ParserError:
                continue
        targets = targets[: plan.max_website_pages]
        if not targets:
            return

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(user_agent=http.settings.user_agent)
                await context.route("**/*", _abort_blocked_hosts)
                page = await context.new_page()
                for url in targets:
                    try:
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS
                        )
                        if response is None or response.status >= 400:
                            continue
                        try:
                            await page.wait_for_load_state("networkidle", timeout=3_000)
                        except Exception:  # pages with long-polling never go idle; the DOM is enough
                            pass
                        final_url = page.url
                        extracted = await extract_page(await page.content(), final_url)
                    except Exception as exc:  # one broken page must not stop the others
                        log.debug("playwright page failed: %s: %s", url, exc)
                        continue
                    if extracted is None or len(extracted.text) < MIN_PAGE_CHARS:
                        continue
                    yield make_document(
                        source_type="website",
                        source_name=self.id,
                        url=final_url,
                        title=extracted.title or await page.title(),
                        text=extracted.text,
                        published_at=utc_datetime(extracted.date),
                        meta={
                            "page_kind": "other",
                            "rendered_by": "playwright",
                            "host": urlsplit(final_url).hostname,
                        },
                    )
            finally:
                await browser.close()


async def _abort_blocked_hosts(route, request) -> None:  # type: ignore[no-untyped-def]
    if is_blocked_host(urlsplit(request.url).hostname or ""):
        await route.abort()
    else:
        await route.continue_()
