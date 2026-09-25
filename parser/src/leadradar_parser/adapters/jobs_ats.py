from collections.abc import AsyncIterator
from datetime import UTC, datetime
from html import unescape
from typing import Any

from lxml import etree, html

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError
from ..http import HttpClient
from ..normalize import MAX_JOB_CHARS
from .common import make_document

SUPPORTED_ATS = {"greenhouse", "lever", "workday", "personio"}
WORKDAY_MAX_SEARCHES = 5
WORKDAY_MAX_DETAILS = 30


class JobsAtsAdapter:
    id = "jobs_ats"
    source_type = "jobs"
    requires_env = None
    rate_limit = RateLimit(requests=1, per_seconds=1, scope="host")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        if company.ats is None:
            return
        handlers = {
            "greenhouse": self._greenhouse,
            "lever": self._lever,
            "workday": self._workday,
            "personio": self._personio,
        }
        handler = handlers.get(company.ats.kind)
        if handler is None:
            return
        count = 0
        async for document in handler(company, plan, http):
            yield document
            count += 1
            if count >= plan.max_items_per_source:
                return

    async def _greenhouse(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        url = f"https://boards-api.greenhouse.io/v1/boards/{company.ats.token}/jobs"
        response = await http.get(url, check_robots=False, params={"content": "true"})
        for job in _json_object(response.json(), "greenhouse").get("jobs") or []:
            title = str(job.get("title") or "")
            body = _plain_html(str(job.get("content") or ""))
            location = (job.get("location") or {}).get("name")
            yield make_document(
                source_type="jobs",
                source_name="greenhouse",
                url=str(job.get("absolute_url") or url),
                title=title,
                text=f"{title}\n{body}",
                published_at=job.get("first_published") or job.get("updated_at"),
                meta={"location": location, "department": _greenhouse_department(job), "ats": "greenhouse"},
                max_chars=MAX_JOB_CHARS,
            )

    async def _lever(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        api_host = "api.eu.lever.co" if company.ats.host and ".eu." in company.ats.host else "api.lever.co"
        url = f"https://{api_host}/v0/postings/{company.ats.token}"
        response = await http.get(url, check_robots=False, params={"mode": "json"})
        payload = response.json()
        if not isinstance(payload, list):
            raise ParserError(f"Lever returned an unexpected payload for {company.ats.token}")
        for job in payload:
            categories = job.get("categories") or {}
            title = str(job.get("text") or "")
            body = str(job.get("descriptionPlain") or job.get("description") or "")
            yield make_document(
                source_type="jobs",
                source_name="lever",
                url=str(job.get("hostedUrl") or job.get("applyUrl") or url),
                title=title,
                text=f"{title}\n{body}",
                published_at=_epoch_ms(job.get("createdAt")),
                meta={
                    "location": categories.get("location"),
                    "department": categories.get("team"),
                    "employment_type": categories.get("commitment"),
                    "ats": "lever",
                },
                max_chars=MAX_JOB_CHARS,
            )

    async def _workday(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None and company.ats.host and company.ats.site
        base = f"https://{company.ats.host}/wday/cxs/{company.ats.token}/{company.ats.site}"
        keywords = list(dict.fromkeys(plan.job_keywords))[:WORKDAY_MAX_SEARCHES] or [""]
        seen: set[str] = set()
        for keyword in keywords:
            response = await http.post(
                f"{base}/jobs",
                json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": keyword},
                headers={"Accept": "application/json"},
            )
            for summary in _json_object(response.json(), "workday").get("jobPostings") or []:
                path = str(summary.get("externalPath") or "")
                if not path or path in seen or len(seen) >= WORKDAY_MAX_DETAILS:
                    continue
                seen.add(path)
                detail = await http.get(
                    f"{base}{path}", check_robots=False, headers={"Accept": "application/json"}
                )
                info = _json_object(detail.json(), "workday").get("jobPostingInfo") or {}
                title = str(info.get("title") or summary.get("title") or "")
                yield make_document(
                    source_type="jobs",
                    source_name="workday",
                    url=str(
                        info.get("externalUrl") or f"https://{company.ats.host}/{company.ats.site}{path}"
                    ),
                    title=title,
                    text=f"{title}\n{_plain_html(str(info.get('jobDescription') or ''))}",
                    published_at=info.get("startDate"),
                    meta={
                        "location": info.get("location") or summary.get("locationsText"),
                        "employment_type": info.get("timeType"),
                        "search": keyword or None,
                        "ats": "workday",
                    },
                    max_chars=MAX_JOB_CHARS,
                )

    async def _personio(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        host = company.ats.host or f"{company.ats.token}.jobs.personio.de"
        response = await http.get(f"https://{host}/xml", check_robots=False)
        root = etree.fromstring(
            response.content, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
        for position in root.xpath("//*[local-name()='position']"):
            title = _xml_text(position, "name") or ""
            job_id = _xml_text(position, "id") or ""
            descriptions = position.xpath(
                ".//*[local-name()='jobDescription']/*[local-name()='value']/text()"
            )
            body = "\n".join(_plain_html(str(value)) for value in descriptions)
            yield make_document(
                source_type="jobs",
                source_name="personio",
                url=f"https://{host}/job/{job_id}",
                title=title,
                text=f"{title}\n{body}",
                published_at=_xml_text(position, "createdAt"),
                meta={
                    "location": _xml_text(position, "office"),
                    "department": _xml_text(position, "department"),
                    "employment_type": _xml_text(position, "employmentType"),
                    "ats": "personio",
                },
                max_chars=MAX_JOB_CHARS,
            )


def _plain_html(value: str) -> str:
    # Greenhouse returns entity-escaped HTML ("&lt;p&gt;"), so unescape before parsing.
    value = unescape(value).strip()
    if not value:
        return ""
    try:
        tree = html.fromstring(value)
    except (etree.ParserError, ValueError):
        return value
    for block in tree.iter("p", "li", "br", "div", "h1", "h2", "h3", "h4", "tr"):
        block.tail = f"\n{block.tail or ''}"
    return "\n".join(line.strip() for line in "".join(tree.itertext()).splitlines() if line.strip())


def _json_object(payload: object, ats: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ParserError(f"{ats} returned an unexpected payload")
    return payload


def _greenhouse_department(job: dict[str, Any]) -> str | None:
    departments = job.get("departments") or []
    return str(departments[0].get("name")) if departments else None


def _epoch_ms(value: object) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _xml_text(node: etree._Element, name: str) -> str | None:
    values = node.xpath(f"./*[local-name()='{name}']/text()")
    return str(values[0]).strip() if values else None
