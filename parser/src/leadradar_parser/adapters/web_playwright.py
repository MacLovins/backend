"""Playwright-based dynamic web crawler adapter.

Renders JavaScript-heavy single page applications (SPAs), dynamic career portals,
and protected pages where static HTML fetch is insufficient.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

import structlog

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient
from ..normalize import canonicalize_url
from .base import SourceAdapter
from .common import extract_page, make_document

log = structlog.get_logger(__name__)


class PlaywrightAdapter(SourceAdapter):
    """Dynamic headless browser crawler using Playwright."""

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
            log.warning("playwright_not_installed")
            return

        urls_to_visit = [company.homepage_url]
        if company.careers_url:
            urls_to_visit.append(company.careers_url)
        if company.newsroom_url:
            urls_to_visit.append(company.newsroom_url)

        visited: set[str] = set()

        try:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                    )
                except Exception as exc:
                    log.warning("playwright_browser_launch_failed", error=str(exc))
                    return

                try:
                    context = await browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                        ),
                        viewport={"width": 1280, "height": 800},
                    )
                    page = await context.new_page()

                    for target_url in urls_to_visit:
                        canonical = canonicalize_url(target_url)
                        if canonical in visited or len(visited) >= plan.max_website_pages:
                            continue
                        visited.add(canonical)

                        try:
                            response = await page.goto(
                                target_url,
                                wait_until="domcontentloaded",
                                timeout=15000,
                            )
                            if not response or response.status >= 400:
                                continue

                            # Wait briefly for dynamic JS frameworks (React/Vue/Angular)
                            try:
                                await page.wait_for_load_state("networkidle", timeout=3000)
                            except Exception:
                                pass

                            html_content = await page.content()
                            final_url = page.url
                            title = await page.title()

                            extracted = await extract_page(html_content, final_url)
                            text = extracted.text if extracted else await page.inner_text("body")

                            if not text or len(text.strip()) < 100:
                                continue

                            yield make_document(
                                source_type="website",
                                source_name=self.id,
                                url=final_url,
                                title=title or (extracted.title if extracted else None),
                                text=text,
                                published_at=datetime.now(UTC),
                                meta={
                                    "dynamic": True,
                                    "rendered_by": "playwright",
                                    "hostname": urlsplit(final_url).hostname or "",
                                },
                            )
                        except Exception as exc:
                            log.debug("playwright_fetch_page_failed", url=target_url, error=str(exc))
                            continue
                finally:
                    await browser.close()

        except Exception as exc:
            log.warning("playwright_session_error", error=str(exc))
