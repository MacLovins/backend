import asyncio
import re
from collections import deque
from collections.abc import AsyncIterator
from time import monotonic

from ..contracts import CollectPlan, Document, RateLimit, ResolvedCompany
from ..errors import ParserError, SourceDisabled, SourceRateLimited
from ..gleif import name_key
from ..http import HttpClient, shared_lock
from ..normalize import MAX_JOB_CHARS
from .common import make_document, plain_text, search_name

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"
# Adzuna country endpoints; EU markets first. Companies elsewhere are skipped (no silent fallback market).
COUNTRIES = {
    "at", "be", "de", "es", "fr", "it", "nl", "pl", "gb", "ch",
    "us", "ca", "au", "nz", "br", "in", "mx", "sg", "za",
}  # fmt: skip
PER_MINUTE = 25
PER_DAY = 250
MAX_WHAT_OR_WORDS = 20

# Process-wide request timestamps (monotonic seconds) for the free-plan limits: 25/min and 250/day.
_calls: deque[float] = deque()


class AdzunaAdapter:
    """Adzuna job search (SPEC PR-19): company filter + `what_or` from the plan's job keywords.

    Needs ADZUNA_APP_ID and ADZUNA_APP_KEY. Adzuna only accepts credentials as query parameters; the HTTP
    layer redacts them from error messages.
    """

    id = "adzuna"
    source_type = "jobs"
    requires_env = "ADZUNA_APP_KEY"
    rate_limit = RateLimit(requests=PER_MINUTE, per_seconds=60, scope="global")

    async def fetch(
        self, company: ResolvedCompany, plan: CollectPlan, http: HttpClient
    ) -> AsyncIterator[Document]:
        app_id = http.settings.env("ADZUNA_APP_ID")
        app_key = http.settings.env(self.requires_env)
        if not app_key:
            return
        if not app_id:
            raise SourceDisabled("ADZUNA_APP_ID is not set")
        country = (
            company.country_code
            or (company.firmographics.country_code if company.firmographics else None)
            or ""
        ).lower()
        if country not in COUNTRIES:
            return
        name = search_name(company)
        params: dict[str, str | int] = {
            "app_id": app_id,
            "app_key": app_key,
            "company": name,
            "results_per_page": min(plan.max_items_per_source, 50),
            "sort_by": "date",
            "content-type": "application/json",
        }
        if words := what_or(plan.job_keywords):
            params["what_or"] = words
        await _acquire()
        response = await http.get(
            API_URL.format(country=country), check_robots=False, attempts=1, params=params
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ParserError("Adzuna returned an unexpected payload")
        wanted = set(name_key(name).split())
        for job in payload.get("results") or []:
            employer = str((job.get("company") or {}).get("display_name") or "")
            # The `company` filter is fuzzy on Adzuna's side; keep only postings of this employer.
            if not wanted <= set(name_key(employer).split()):
                continue
            title = plain_text(str(job.get("title") or ""))
            body = plain_text(str(job.get("description") or ""))
            url = str(job.get("redirect_url") or "")
            if not title or not url:
                continue
            yield make_document(
                source_type="jobs",
                source_name=self.id,
                url=url,
                title=title,
                text=f"{title}\n{body}",
                published_at=job.get("created"),
                meta={
                    "location": (job.get("location") or {}).get("display_name"),
                    "department": (job.get("category") or {}).get("label"),
                    "employment_type": job.get("contract_time") or job.get("contract_type"),
                    "employer": employer,
                    "truncated": True,  # Adzuna returns a snippet of the description
                    "country": country,
                },
                max_chars=MAX_JOB_CHARS,
            )


def what_or(keywords: list[str]) -> str:
    """Adzuna's `what_or` is a space-separated list of words; multi-word keywords contribute each word."""
    words = [word for keyword in keywords for word in re.split(r"\s+", keyword.strip()) if len(word) > 1]
    return " ".join(list(dict.fromkeys(words))[:MAX_WHAT_OR_WORDS])


async def _acquire() -> None:
    async with shared_lock("adzuna"):
        now = monotonic()
        while _calls and now - _calls[0] > 86_400:
            _calls.popleft()
        if len(_calls) >= PER_DAY:
            retry_after = int(86_400 - (now - _calls[0])) + 1
            raise SourceRateLimited("Adzuna daily limit of 250 requests reached", retry_after)
        recent = [stamp for stamp in _calls if now - stamp < 60]
        if len(recent) >= PER_MINUTE:
            await asyncio.sleep(60 - (now - recent[0]))
        _calls.append(monotonic())
