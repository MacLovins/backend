from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

from lxml import etree, html

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..http import HttpClient, is_blocked_host
from ..normalize import MAX_JOB_CHARS, canonicalize_url
from .common import make_document
from .jobs_ats import SUPPORTED_ATS

JOB_MARKERS = ("/job/", "/jobs/", "/stellen", "/position", "/vacanc", "jobid=", "job_id=", "gh_jid=")
MIN_TITLE_CHARS = 4


class CareersHtmlAdapter:
    """Fallback when no supported ATS was detected: job-like links on the careers page, headline only."""

    id = "careers_html"
    source_type = "jobs"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="host")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        if not company.careers_url or (company.ats is not None and company.ats.kind in SUPPORTED_ATS):
            return
        response = await http.get(company.careers_url)
        try:
            tree = html.fromstring(response.text)
        except (etree.ParserError, ValueError):
            return
        careers = canonicalize_url(str(response.url))
        seen: set[str] = set()
        for anchor in tree.xpath("//a[@href]"):
            href = urljoin(str(response.url), str(anchor.get("href")))
            parts = urlsplit(href)
            if parts.scheme not in {"http", "https"} or is_blocked_host(parts.hostname or ""):
                continue
            if not any(marker in href.lower() for marker in JOB_MARKERS):
                continue
            canonical = canonicalize_url(href)
            title = " ".join(" ".join(anchor.itertext()).split())
            if canonical == careers or canonical in seen or len(title) < MIN_TITLE_CHARS:
                continue
            seen.add(canonical)
            yield make_document(
                source_type="jobs",
                source_name=self.id,
                url=href,
                title=title,
                text=title,
                meta={"headline_only": True, "careers_url": company.careers_url},
                max_chars=MAX_JOB_CHARS,
            )
            if len(seen) >= plan.max_items_per_source:
                return
