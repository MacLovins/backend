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

# Every AtsKind has a collector; careers_html runs only when no ATS (or an unknown one) was detected.
SUPPORTED_ATS = {
    "greenhouse", "lever", "workday", "personio", "ashby", "smartrecruiters", "workable", "recruitee",
}  # fmt: skip
WORKDAY_MAX_SEARCHES = 5
WORKDAY_MAX_DETAILS = 30
SMARTRECRUITERS_MAX_DETAILS = 20


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
            "ashby": self._ashby,
            "smartrecruiters": self._smartrecruiters,
            "workable": self._workable,
            "recruitee": self._recruitee,
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

    async def _ashby(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        url = f"https://api.ashbyhq.com/posting-api/job-board/{company.ats.token}"
        response = await http.get(url, check_robots=False)
        jobs = [
            job
            for job in _json_object(response.json(), "ashby").get("jobs") or []
            if job.get("isListed", True)
        ]
        for job in _prioritize(jobs, plan.job_keywords, "title"):
            title = str(job.get("title") or "").strip()
            body = str(job.get("descriptionPlain") or "") or _plain_html(
                str(job.get("descriptionHtml") or "")
            )
            yield make_document(
                source_type="jobs",
                source_name="ashby",
                url=str(job.get("jobUrl") or f"https://jobs.ashbyhq.com/{company.ats.token}"),
                title=title,
                text=f"{title}\n{body}",
                published_at=job.get("publishedAt"),
                meta={
                    "location": job.get("location"),
                    "department": job.get("department") or job.get("team"),
                    "employment_type": job.get("employmentType"),
                    "ats": "ashby",
                },
                max_chars=MAX_JOB_CHARS,
            )

    async def _smartrecruiters(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        base = f"https://api.smartrecruiters.com/v1/companies/{company.ats.token}/postings"
        response = await http.get(base, check_robots=False, params={"limit": 100})
        postings = _json_object(response.json(), "smartrecruiters").get("content") or []
        for index, posting in enumerate(_prioritize(postings, plan.job_keywords, "name")):
            posting_id = str(posting.get("id") or "")
            title = str(posting.get("name") or "").strip()
            if not posting_id or not title:
                continue
            body = ""
            url = f"https://jobs.smartrecruiters.com/{company.ats.token}/{posting_id}"
            # The list has no descriptions: fetch details for the first (keyword-matching) postings only.
            if index < SMARTRECRUITERS_MAX_DETAILS:
                try:
                    detail = _json_object(
                        (await http.get(f"{base}/{posting_id}", check_robots=False)).json(), "smartrecruiters"
                    )
                except ParserError:
                    detail = {}
                sections = (detail.get("jobAd") or {}).get("sections") or {}
                body = "\n".join(
                    _plain_html(str((sections.get(key) or {}).get("text") or ""))
                    for key in ("jobDescription", "qualifications", "additionalInformation")
                ).strip()
                url = str(detail.get("postingUrl") or url)
            location = posting.get("location") or {}
            yield make_document(
                source_type="jobs",
                source_name="smartrecruiters",
                url=url,
                title=title,
                text=f"{title}\n{body}",
                published_at=posting.get("releasedDate"),
                meta={
                    "location": location.get("fullLocation") or location.get("city"),
                    "department": (posting.get("department") or {}).get("label")
                    or (posting.get("function") or {}).get("label"),
                    "employment_type": (posting.get("typeOfEmployment") or {}).get("label"),
                    "headline_only": not body,
                    "ats": "smartrecruiters",
                },
                max_chars=MAX_JOB_CHARS,
            )

    async def _workable(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        url = f"https://apply.workable.com/api/v1/widget/accounts/{company.ats.token}"
        response = await http.get(url, check_robots=False, params={"details": "true"})
        jobs = _json_object(response.json(), "workable").get("jobs") or []
        for job in _prioritize(jobs, plan.job_keywords, "title"):
            title = str(job.get("title") or "").strip()
            location = ", ".join(str(job[key]) for key in ("city", "country") if job.get(key))
            yield make_document(
                source_type="jobs",
                source_name="workable",
                url=str(
                    job.get("url")
                    or job.get("shortlink")
                    or f"https://apply.workable.com/{company.ats.token}"
                ),
                title=title,
                text=f"{title}\n{_plain_html(str(job.get('description') or ''))}",
                published_at=job.get("published_on") or job.get("created_at"),
                meta={
                    "location": location or None,
                    "department": job.get("department") or job.get("function"),
                    "employment_type": job.get("employment_type"),
                    "ats": "workable",
                },
                max_chars=MAX_JOB_CHARS,
            )

    async def _recruitee(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        assert company.ats is not None
        host = company.ats.host or f"{company.ats.token}.recruitee.com"
        response = await http.get(f"https://{host}/api/offers/", check_robots=False)
        offers = [
            offer
            for offer in _json_object(response.json(), "recruitee").get("offers") or []
            if offer.get("status", "published") == "published"
        ]
        for offer in _prioritize(offers, plan.job_keywords, "title"):
            title = str(offer.get("title") or "").strip()
            body = "\n".join(
                _plain_html(str(offer.get(key) or "")) for key in ("description", "requirements")
            ).strip()
            yield make_document(
                source_type="jobs",
                source_name="recruitee",
                url=str(offer.get("careers_url") or f"https://{host}/o/{offer.get('slug') or ''}"),
                title=title,
                text=f"{title}\n{body}",
                # "2026-09-25 15:46:07 UTC" is neither ISO 8601 nor RFC 2822.
                published_at=_strip_utc(offer.get("published_at") or offer.get("created_at")),
                meta={
                    "location": offer.get("location"),
                    "department": offer.get("department"),
                    "employment_type": offer.get("employment_type_code"),
                    "ats": "recruitee",
                },
                max_chars=MAX_JOB_CHARS,
            )


def _prioritize(jobs: list[Any], keywords: list[str], title_key: str) -> list[dict[str, Any]]:
    """Jobs whose title mentions a plan keyword first (stable), so `max_items_per_source` keeps them."""
    lowered = [keyword.casefold() for keyword in keywords if keyword.strip()]
    valid = [job for job in jobs if isinstance(job, dict)]
    if not lowered:
        return valid
    return sorted(
        valid, key=lambda job: not any(k in str(job.get(title_key) or "").casefold() for k in lowered)
    )


def _strip_utc(value: object) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return f"{text.removesuffix(' UTC')}+00:00" if text.endswith(" UTC") else text


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
