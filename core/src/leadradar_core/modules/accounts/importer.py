"""CSV → company rows (CO-07): column mappings, value normalization, in-file deduplication.

Pure functions without I/O; the service layer (`service.py`) applies the parsed rows to the database.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import cache
from typing import Any, Literal
from urllib.parse import urlsplit

import leadradar_parser as parser
from leadradar_core.utils.domain import normalize_domain
from rapidfuzz import fuzz, process

MappingName = Literal["default", "crunchbase", "custom"]
DuplicatePolicy = Literal["merge", "skip"]
INDUSTRY_MATCH_THRESHOLD = 85

# Target fields an import can fill (the same names are used as keys of a custom column map)
IMPORT_FIELDS: tuple[str, ...] = (
    "name",
    "domain",
    "country",
    "industry",
    "employees",
    "revenue",
    "hq_city",
    "homepage_url",
    "linkedin_url",
    "careers_url",
    "newsroom_url",
    "crunchbase_url",
    "notes",
    "tags",
)

# Default template: target field → accepted header names (compared case- and punctuation-insensitively)
DEFAULT_MAPPING: dict[str, tuple[str, ...]] = {
    "name": ("name", "company", "company name", "organization name", "account name"),
    "domain": ("domain", "website", "url", "homepage", "company domain"),
    "country": ("country", "country code", "country_code", "hq country"),
    "industry": ("industry", "industries", "industry id", "industry_ids", "sector"),
    "employees": ("employees", "employee count", "number of employees", "headcount", "size"),
    "revenue": ("revenue", "revenue eur", "revenue_eur", "annual revenue"),
    "hq_city": ("hq city", "hq_city", "city", "headquarters"),
    "homepage_url": ("homepage url", "homepage_url"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin_url"),
    "careers_url": ("careers", "careers url", "careers_url", "jobs url"),
    "newsroom_url": ("newsroom", "newsroom url", "newsroom_url", "press url"),
    "crunchbase_url": ("crunchbase url", "crunchbase_url"),
    "notes": ("notes", "note", "description"),
    "tags": ("tags", "tag", "labels"),
}

# Crunchbase CSV export (Pro "Export to CSV" of an organization search)
CRUNCHBASE_MAPPING: dict[str, tuple[str, ...]] = {
    "name": ("organization name",),
    "domain": ("website",),
    "country": ("headquarters location",),  # "Bonn, Nordrhein-Westfalen, Germany" → last part
    "industry": ("industries", "industry groups"),
    "employees": ("number of employees",),  # ranges: "10001+", "1001-5000"
    "revenue": ("estimated revenue range",),  # "$10B+", "$1B to $10B"
    "hq_city": ("headquarters location",),  # first part
    "linkedin_url": ("linkedin",),
    "crunchbase_url": ("organization name url",),
    "notes": ("description",),
}

# Country names and common aliases that the parser catalog does not list as labels
_COUNTRY_ALIASES = {
    "UK": "GB",
    "UNITED KINGDOM": "GB",
    "GREAT BRITAIN": "GB",
    "ENGLAND": "GB",
    "USA": "US",
    "U.S.": "US",
    "U.S.A.": "US",
    "UNITED STATES OF AMERICA": "US",
    "DEUTSCHLAND": "DE",
    "SCHWEIZ": "CH",
    "SUISSE": "CH",
    "ÖSTERREICH": "AT",
    "OSTERREICH": "AT",
    "NEDERLAND": "NL",
    "HOLLAND": "NL",
    "THE NETHERLANDS": "NL",
    "ESPAÑA": "ES",
    "ESPANA": "ES",
    "ITALIA": "IT",
    "DANMARK": "DK",
    "SVERIGE": "SE",
    "NORGE": "NO",
    "SUOMI": "FI",
    "POLSKA": "PL",
    "BELGIË": "BE",
    "BELGIQUE": "BE",
    "CZECHIA": "CZ",
    "CZECH REPUBLIC": "CZ",
    "REPUBLIC OF MOLDOVA": "MD",
}


@dataclass
class ParsedRow:
    row: int  # 1-based data row number (header excluded) — used in the report
    name: str
    domain: str
    fields: dict[str, Any]  # company column → value, only for non-empty values


@dataclass
class DuplicateInFile:
    row: int
    first_row: int
    domain: str
    action: DuplicatePolicy


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    total_rows: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicates: list[DuplicateInFile] = field(default_factory=list)


class ImportFormatError(ValueError):
    """The file or the mapping cannot be used at all (bad encoding, missing required columns...)."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class TooManyRowsError(ValueError):
    def __init__(self, limit: int) -> None:
        super().__init__(f"CSV has more than {limit} data rows")
        self.limit = limit


# --- normalization helpers -----------------------------------------------------------------------


def _norm_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).strip()


def _norm_label(value: str) -> str:
    value = value.lower().replace("&", " and ").replace("_", " ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


# Common names that are not close to a taxonomy label (normalized form → taxonomy id)
_INDUSTRY_ALIASES = {
    "energy": "energy_utilities",
    "utilities": "energy_utilities",
    "power": "energy_utilities",
    "oil": "oil_gas",
    "financial services": "banking",
    "fintech": "banking",
    "banks": "banking",
    "automobile": "automotive",
    "automotive industry": "automotive",
    "information technology": "it_services",
    "it": "it_services",
    "saas": "software",
    "enterprise software": "software",
    "transportation": "logistics",
    "supply chain": "logistics",
    "biotechnology": "pharma",
    "biotech": "pharma",
    "life sciences": "pharma",
    "e commerce": "retail",
    "ecommerce": "retail",
    "government": "public_sector",
    "food": "food_beverage",
    "aviation": "airlines",
}


@cache
def _industry_index() -> tuple[set[str], dict[str, str]]:
    ids = set()
    choices: dict[str, str] = {}  # normalized label/id → taxonomy id
    for item in parser.industry_taxonomy():
        ids.add(item.id)
        choices[_norm_label(item.label)] = item.id
        choices[_norm_label(item.id)] = item.id
    for alias, industry_id in _INDUSTRY_ALIASES.items():
        if industry_id in ids:
            choices.setdefault(alias, industry_id)
    return ids, choices


def _industry_similarity(a: str, b: str, **_: object) -> float:
    """Whole-name similarity: word order and spacing ("Health Care" ~ "healthcare") do not matter.

    Partial matches are not used on purpose: "Underwater robotics" must not become "water".
    """
    return max(fuzz.token_sort_ratio(a, b), fuzz.ratio(a, b))


@cache
def _country_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for item in parser.country_catalog():
        index[item.code.upper()] = item.code.upper()
        index[item.label.upper()] = item.code.upper()
    for alias, code in _COUNTRY_ALIASES.items():
        index.setdefault(alias, code)
    return index


def match_industry(value: str) -> str | None:
    """Taxonomy id for a free-text industry: exact id, else fuzzy match on labels (rapidfuzz ≥ 85)."""
    raw = value.strip()
    if not raw:
        return None
    ids, choices = _industry_index()
    if raw.lower() in ids:
        return raw.lower()
    key = _norm_label(raw)
    if not key:
        return None
    if key in choices:
        return choices[key]
    best = process.extractOne(
        key, list(choices), scorer=_industry_similarity, score_cutoff=INDUSTRY_MATCH_THRESHOLD
    )
    return choices[best[0]] if best else None


def match_industries(value: str) -> tuple[list[str], list[str]]:
    """Split a cell into industries ("A; B", "A, B", "A|B"), return (taxonomy ids, unmatched values)."""
    matched: list[str] = []
    unmatched: list[str] = []
    for part in re.split(r"[;,|/]", value):
        part = part.strip()
        if not part:
            continue
        industry_id = match_industry(part)
        if industry_id is None:
            unmatched.append(part)
        elif industry_id not in matched:
            matched.append(industry_id)
    return matched, unmatched


def match_country(value: str) -> str | None:
    """ISO2 code for an ISO2 code or a country name; for "City, Region, Country" the last part is used."""
    raw = value.strip()
    if not raw:
        return None
    index = _country_index()
    candidates = [raw, raw.split(",")[-1]] if "," in raw else [raw]
    for candidate in candidates:
        key = candidate.strip().upper()
        if key in index:
            return index[key]
        if re.fullmatch(r"[A-Z]{2}", key):
            return key  # any ISO2 code, even if not in the parser catalog
    return None


_NUMBER = re.compile(
    r"(\d[\d,.\s']*)\s*(k|m|mn|mm|b|bn|t|thousand|million|billion|trillion)?\b", re.IGNORECASE
)
_MULTIPLIERS = {
    "k": 10**3,
    "thousand": 10**3,
    "m": 10**6,
    "mn": 10**6,
    "mm": 10**6,
    "million": 10**6,
    "b": 10**9,
    "bn": 10**9,
    "billion": 10**9,
    "t": 10**12,
    "trillion": 10**12,
}


def _to_decimal(digits: str) -> Decimal | None:
    s = digits.strip().replace(" ", "").replace("'", "")
    if not s:
        return None
    if "," in s and "." in s:  # 1,234.5 or 1.234,5
        s = s.replace(",", "") if s.rfind(".") > s.rfind(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:  # 1,234,567 (thousands) or 1,5 (decimal)
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")  # 1.234.567 (European thousands)
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def parse_amount(value: str) -> Decimal | None:
    """First (lower-bound) amount in a cell: "590000", "590,000", "$1B to $10B", "€81.8bn", "10001+"."""
    match = _NUMBER.search(value.strip())
    if not match:
        return None
    number = _to_decimal(match.group(1))
    if number is None:
        return None
    suffix = (match.group(2) or "").lower()
    return number * _MULTIPLIERS.get(suffix, 1)


def parse_employees(value: str) -> int | None:
    amount = parse_amount(value)
    if amount is None or amount < 0:
        return None
    return int(amount)


def _url(value: str) -> str | None:
    raw = value.strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    parts = urlsplit(raw)
    return raw if parts.netloc and "." in parts.netloc else None


def _crunchbase_id(value: str) -> str | None:
    match = re.search(r"crunchbase\.com/organization/([^/?#\s]+)", value)
    return match.group(1) if match else None


# --- mapping --------------------------------------------------------------------------------------


def resolve_columns(
    headers: list[str], mapping: MappingName, column_map: dict[str, str] | None
) -> dict[str, str]:
    """Target field → actual CSV header for this file."""
    by_norm = {_norm_header(h): h for h in headers if h}
    resolved: dict[str, str] = {}
    if mapping == "custom":
        if not column_map:
            raise ImportFormatError(
                "missing_column_map", "mapping=custom requires a column_map (target field → CSV column)"
            )
        unknown_fields = sorted(set(column_map) - set(IMPORT_FIELDS))
        if unknown_fields:
            raise ImportFormatError(
                "invalid_column_map",
                f"Unknown target fields in column_map: {', '.join(unknown_fields)}",
                {"allowed_fields": list(IMPORT_FIELDS)},
            )
        missing = [col for col in column_map.values() if _norm_header(col) not in by_norm]
        if missing:
            raise ImportFormatError(
                "unknown_columns",
                f"Columns not found in CSV: {', '.join(missing)}",
                {"columns": headers},
            )
        resolved = {target: by_norm[_norm_header(col)] for target, col in column_map.items()}
    else:
        table = CRUNCHBASE_MAPPING if mapping == "crunchbase" else DEFAULT_MAPPING
        for target, aliases in table.items():
            for alias in aliases:
                if _norm_header(alias) in by_norm:
                    resolved[target] = by_norm[_norm_header(alias)]
                    break
    missing_required = [f for f in ("name", "domain") if f not in resolved]
    if missing_required:
        raise ImportFormatError(
            "missing_columns",
            f"CSV must have columns for: {', '.join(missing_required)} (mapping={mapping})",
            {"columns": headers, "mapping": mapping},
        )
    return resolved


# --- parsing --------------------------------------------------------------------------------------


def _row_fields(
    get: dict[str, str], mapping: MappingName, row_no: int, warnings: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if country_raw := get.get("country"):
        code = match_country(country_raw)
        if code:
            out["country_code"] = code
        else:
            warnings.append(f"Row {row_no}: unknown country '{country_raw}'")
    if industry_raw := get.get("industry"):
        ids, unmatched = match_industries(industry_raw)
        if ids:
            out["industry_ids"] = ids
        if unmatched:
            warnings.append(f"Row {row_no}: industry not in taxonomy: {', '.join(unmatched)}")
    if employees_raw := get.get("employees"):
        employees = parse_employees(employees_raw)
        if employees is None:
            warnings.append(f"Row {row_no}: cannot parse employees '{employees_raw}'")
        else:
            out["employees"] = employees
    if revenue_raw := get.get("revenue"):
        revenue = parse_amount(revenue_raw)
        if revenue is None:
            warnings.append(f"Row {row_no}: cannot parse revenue '{revenue_raw}'")
        else:
            out["revenue_eur"] = revenue.quantize(Decimal("0.01"))
    if city := get.get("hq_city"):
        out["hq_city"] = city.split(",")[0].strip() if mapping == "crunchbase" else city.strip()
    for key in ("homepage_url", "linkedin_url", "careers_url", "newsroom_url"):
        if raw := get.get(key):
            url = _url(raw)
            if url:
                out[key] = url
            else:
                warnings.append(f"Row {row_no}: invalid {key} '{raw}'")
    if cb := get.get("crunchbase_url"):
        cb_id = _crunchbase_id(cb)
        if cb_id:
            out["crunchbase_id"] = cb_id
    if notes := get.get("notes"):
        out["notes"] = notes.strip()
    if tags := get.get("tags"):
        tag_list = [t.strip() for t in re.split(r"[;,|]", tags) if t.strip()]
        if tag_list:
            out["tags"] = tag_list
    return out


def parse_csv(
    content: bytes,
    *,
    mapping: MappingName = "default",
    column_map: dict[str, str] | None = None,
    on_duplicate: DuplicatePolicy = "merge",
    max_rows: int = 5000,
) -> ParseResult:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ImportFormatError("invalid_encoding", "CSV must be UTF-8 encoded") from e
    if not text.strip():
        raise ImportFormatError("empty_file", "CSV file is empty")

    header_line = text.splitlines()[0]
    delimiter = max((",", ";", "\t"), key=header_line.count)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    columns = resolve_columns(headers, mapping, column_map)

    result = ParseResult()
    first_by_domain: dict[str, ParsedRow] = {}
    try:
        for row_no, raw in enumerate(reader, start=1):
            if row_no > max_rows:
                raise TooManyRowsError(max_rows)
            result.total_rows = row_no
            get = {
                target: (raw.get(col) or "").strip()
                for target, col in columns.items()
                if (raw.get(col) or "").strip()
            }
            if not get:
                result.skipped += 1  # blank line
                continue
            name = get.get("name", "")
            domain_raw = get.get("domain", "")
            if not name or not domain_raw:
                result.skipped += 1
                result.errors.append(f"Row {row_no}: missing {'name' if not name else 'domain'}")
                continue
            domain = normalize_domain(domain_raw)
            if not domain or "." not in domain or " " in domain:
                result.skipped += 1
                result.errors.append(f"Row {row_no}: invalid domain '{domain_raw}'")
                continue
            fields = _row_fields(get, mapping, row_no, result.warnings)
            if "homepage_url" not in fields and domain_raw.lower().startswith(("http://", "https://")):
                parts = urlsplit(domain_raw)
                fields["homepage_url"] = f"{parts.scheme.lower()}://{parts.netloc.lower()}"

            parsed = ParsedRow(row=row_no, name=name[:255], domain=domain, fields=fields)
            first = first_by_domain.get(domain)
            if first is None:
                first_by_domain[domain] = parsed
                result.rows.append(parsed)
                continue
            result.duplicates.append(
                DuplicateInFile(row=row_no, first_row=first.row, domain=domain, action=on_duplicate)
            )
            if on_duplicate == "merge":
                for key, value in fields.items():
                    first.fields.setdefault(key, value)
            result.skipped += 1
    except csv.Error as e:
        raise ImportFormatError("invalid_csv", f"Malformed CSV: {e}") from e
    return result
