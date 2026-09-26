"""GLEIF LEI records (https://api.gleif.org/api/v1, no key, CC0): legal name, LEI, jurisdiction, parents."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .contracts import CompanyRef, Firmographics
from .errors import SourceRequestFailed
from .http import HttpClient

API_URL = "https://api.gleif.org/api/v1"
HEADERS = {"Accept": "application/vnd.api+json"}
LEI_PATTERN = re.compile(r"^[A-Z0-9]{18}[0-9]{2}$")
# Legal-form suffixes ignored when a GLEIF legal name is compared with the company name.
LEGAL_FORMS = {
    "ag", "aktiengesellschaft", "se", "gmbh", "mbh", "kg", "kgaa", "co", "ohg", "ug", "sa", "sas", "sarl",
    "spa", "srl", "nv", "bv", "plc", "ltd", "limited", "inc", "incorporated", "corp", "corporation", "llc",
    "ab", "asa", "as", "oyj", "oy", "a/s", "group", "holding", "holdings", "the",
}  # fmt: skip


@dataclass(frozen=True)
class LeiRecord:
    lei: str
    legal_name: str
    country_code: str | None
    jurisdiction: str | None
    city: str | None
    entity_status: str | None
    registration_status: str | None
    registered_as: str | None
    legal_form: str | None


async def lei_record(lei: str, http: HttpClient) -> LeiRecord | None:
    if not LEI_PATTERN.match(lei):
        return None
    try:
        response = await http.get(f"{API_URL}/lei-records/{lei}", check_robots=False, headers=HEADERS)
    except SourceRequestFailed:  # 404: unknown or retired LEI
        return None
    return _record(response.json().get("data"))


async def ultimate_parent(lei: str, http: HttpClient) -> LeiRecord | None:
    """The ultimate accounting parent; None for top-level entities (GLEIF answers 404)."""
    try:
        response = await http.get(
            f"{API_URL}/lei-records/{lei}/ultimate-parent", check_robots=False, headers=HEADERS
        )
    except SourceRequestFailed:
        return None
    parent = _record(response.json().get("data"))
    return parent if parent and parent.lei != lei else None


async def search_lei(company: CompanyRef, http: HttpClient, names: list[str]) -> LeiRecord | None:
    """Full-text search; accept only an unambiguous exact name match (legal forms ignored).

    GLEIF full text returns subsidiaries and foundations first ("Lufthansa" → Lufthansa Systems GmbH, …), so
    a fuzzy pick would attach the wrong legal entity. No match is better than a wrong LEI.
    """
    wanted = {key for name in names if (key := name_key(name))}
    if not wanted:
        return None
    query = max(names, key=len)
    params: dict[str, str | int] = {"filter[fulltext]": query, "page[size]": 20}
    if company.country_code:
        params["filter[entity.legalAddress.country]"] = company.country_code
    response = await http.get(f"{API_URL}/lei-records", check_robots=False, headers=HEADERS, params=params)
    records = [record for item in response.json().get("data") or [] if (record := _record(item))]
    matches = {record.lei: record for record in records if name_key(record.legal_name) in wanted}
    active = [
        r for r in matches.values() if r.entity_status == "ACTIVE" and r.registration_status != "RETIRED"
    ]
    candidates = active or list(matches.values())
    return candidates[0] if len(candidates) == 1 else None


def firmographics_from(record: LeiRecord) -> Firmographics:
    return Firmographics(
        legal_name=record.legal_name,
        country_code=record.country_code,
        hq_city=record.city,
        lei=record.lei,
        source="gleif",
    )


def name_key(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().casefold()
    words = [word for word in re.split(r"[^a-z0-9/]+", folded.replace(".", "")) if word]
    while words and words[-1] in LEGAL_FORMS:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def _record(item: Any) -> LeiRecord | None:
    if not isinstance(item, dict):
        return None
    attributes = item.get("attributes") or {}
    entity = attributes.get("entity") or {}
    registration = attributes.get("registration") or {}
    legal_name = (entity.get("legalName") or {}).get("name")
    lei = attributes.get("lei") or item.get("id")
    if not lei or not legal_name:
        return None
    headquarters = entity.get("headquartersAddress") or {}
    legal_address = entity.get("legalAddress") or {}
    return LeiRecord(
        lei=str(lei),
        legal_name=str(legal_name),
        country_code=legal_address.get("country") or headquarters.get("country"),
        jurisdiction=entity.get("jurisdiction"),
        city=headquarters.get("city") or legal_address.get("city"),
        entity_status=entity.get("status"),
        registration_status=registration.get("status"),
        registered_as=entity.get("registeredAs"),
        legal_form=(entity.get("legalForm") or {}).get("id"),
    )
