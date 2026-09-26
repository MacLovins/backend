"""CO-07: CSV import — mappings, normalization, in-file duplicates, accurate report, limits."""

import json
from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from leadradar_auth.schemas import Principal
from leadradar_auth.security import create_access_token
from leadradar_core.main import create_app
from leadradar_core.modules.accounts.importer import (
    ImportFormatError,
    TooManyRowsError,
    match_country,
    match_industries,
    match_industry,
    parse_amount,
    parse_csv,
    parse_employees,
)
from leadradar_core.settings import settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings.ENV = "test"
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def headers() -> dict[str, str]:
    principal = Principal(
        user_id=uuid4(), org_id=settings.DEFAULT_ORG_ID, email="admin@leadradar.ai", role="admin"
    )
    return {"Authorization": f"Bearer {create_access_token(principal)}"}


@pytest.fixture
def tag() -> str:
    return uuid4().hex[:10]


def _upload(client: TestClient, headers: dict[str, str], text: str, **params: str):
    data = {}
    if "column_map" in params:
        data["column_map"] = params.pop("column_map")
    return client.post(
        "/api/v1/companies/import",
        params=params,
        data=data,
        files={"file": ("accounts.csv", text.encode(), "text/csv")},
        headers=headers,
    )


def _company(client: TestClient, headers: dict[str, str], domain: str) -> dict:
    res = client.get("/api/v1/companies", params={"q": domain, "page_size": 100}, headers=headers)
    assert res.status_code == 200
    matches = [c for c in res.json()["items"] if c["domain"] == domain]
    assert len(matches) == 1, matches
    return matches[0]


def _cleanup(client: TestClient, headers: dict[str, str], tag: str) -> None:
    res = client.get("/api/v1/companies", params={"q": tag, "page_size": 100}, headers=headers)
    for c in res.json().get("items", []):
        client.delete(f"/api/v1/companies/{c['id']}", headers=headers)


# --- pure parsing ---------------------------------------------------------------------------------


def test_csv_import_industry_fuzzy_match() -> None:
    assert match_industry("energy_utilities") == "energy_utilities"
    assert match_industry("Energy & Utilities") == "energy_utilities"
    assert match_industry("Energy and utilities") == "energy_utilities"
    assert match_industry("Pharmaceutical") == "pharma"
    assert match_industry("Health Care") == "healthcare"
    assert match_industry("Telecommunication") == "telecom"
    assert match_industry("Oil and Gas") == "oil_gas"
    assert match_industry("Underwater basket weaving") is None
    ids, unmatched = match_industries("Software; Banking, Quantum knitting")
    assert ids == ["software", "banking"]
    assert unmatched == ["Quantum knitting"]


def test_csv_import_country_and_amounts() -> None:
    assert match_country("DE") == "DE"
    assert match_country("germany") == "DE"
    assert match_country("Deutschland") == "DE"
    assert match_country("Bonn, Nordrhein-Westfalen, Germany") == "DE"
    assert match_country("United Kingdom") == "GB"
    assert match_country("Atlantis") is None
    assert parse_employees("590,000") == 590000
    assert parse_employees("10001+") == 10001
    assert parse_employees("1001-5000") == 1001
    assert parse_employees("n/a") is None
    assert parse_amount("$1B to $10B") == Decimal(10**9)
    assert parse_amount("81800000000") == Decimal(81800000000)
    assert parse_amount("€81.8bn") == Decimal("81.8") * 10**9


def test_csv_import_duplicates_merge_and_skip() -> None:
    text = "name,domain,country,employees\nAcme,acme.com,DE,\nAcme GmbH,https://www.acme.com/about,,1200\n"
    merged = parse_csv(text.encode(), on_duplicate="merge")
    assert len(merged.rows) == 1
    assert merged.rows[0].fields == {
        "country_code": "DE",
        "employees": 1200,
        "homepage_url": "https://www.acme.com",
    }
    assert [(d.row, d.first_row, d.domain, d.action) for d in merged.duplicates] == [
        (2, 1, "acme.com", "merge")
    ]
    skipped = parse_csv(text.encode(), on_duplicate="skip")
    assert skipped.rows[0].fields == {"country_code": "DE"}
    assert skipped.rows[0].name == "Acme"
    assert skipped.duplicates[0].action == "skip"
    assert skipped.skipped == 1


def test_csv_import_format_errors() -> None:
    with pytest.raises(ImportFormatError) as e:
        parse_csv(b"company,url\nA,a.com\n", mapping="crunchbase")
    assert e.value.code == "missing_columns"
    with pytest.raises(ImportFormatError) as e:
        parse_csv(b"name,domain\nA,a.com\n", mapping="custom")
    assert e.value.code == "missing_column_map"
    with pytest.raises(TooManyRowsError):
        parse_csv(b"name,domain\n" + b"".join(f"A{i},a{i}.com\n".encode() for i in range(6)), max_rows=5)
    with pytest.raises(ImportFormatError) as e:
        parse_csv("name,domain\nA,ä.com\n".encode("latin-1"))
    assert e.value.code == "invalid_encoding"


# --- API --------------------------------------------------------------------------------------------


def test_csv_import_default_template_all_fields(
    client: TestClient, headers: dict[str, str], tag: str
) -> None:
    text = (
        "name,domain,country,industry,employees,revenue,linkedin_url,careers_url,newsroom_url,tags\n"
        f'Alpha {tag},https://www.alpha-{tag}.com/en,Germany,Energy & Utilities,"12,500",1.5bn,'
        f"linkedin.com/company/alpha-{tag},https://careers.alpha-{tag}.com,https://alpha-{tag}.com/news,a;b\n"
        f"Beta {tag},beta-{tag}.io,FR,pharma,800,,,,,\n"
        f"No domain {tag},,DE,,,,,,,\n"
    )
    try:
        res = _upload(client, headers, text)
        assert res.status_code == 200, res.text
        report = res.json()
        assert report["created"] == 2
        assert report["updated"] == 0
        assert report["skipped"] == 1
        assert report["total_rows"] == 3
        assert len(report["errors"]) == 1 and "Row 3" in report["errors"][0]

        alpha = _company(client, headers, f"alpha-{tag}.com")
        assert alpha["country_code"] == "DE"
        assert alpha["industry_ids"] == ["energy_utilities"]
        assert alpha["employees"] == 12500
        assert Decimal(str(alpha["revenue_eur"])) == Decimal("1500000000")
        assert alpha["linkedin_url"] == f"https://linkedin.com/company/alpha-{tag}"
        assert alpha["careers_url"] == f"https://careers.alpha-{tag}.com"
        assert alpha["newsroom_url"] == f"https://alpha-{tag}.com/news"
        assert alpha["tags"] == ["a", "b"]
        assert alpha["origin"] == "csv"
        beta = _company(client, headers, f"beta-{tag}.io")
        assert beta["industry_ids"] == ["pharma"]
        assert beta["country_code"] == "FR"
    finally:
        _cleanup(client, headers, tag)


def test_csv_import_updated_counts_only_real_changes(
    client: TestClient, headers: dict[str, str], tag: str
) -> None:
    domain = f"gamma-{tag}.com"
    try:
        first = _upload(client, headers, f"name,domain\nGamma {tag},{domain}\n").json()
        assert (first["created"], first["updated"], first["skipped"]) == (1, 0, 0)

        same = _upload(client, headers, f"name,domain\nGamma {tag},{domain}\n").json()
        assert (same["created"], same["updated"], same["skipped"]) == (0, 0, 1)

        filled = _upload(
            client, headers, f"name,domain,country,employees\nGamma {tag},{domain},NL,300\n"
        ).json()
        assert (filled["created"], filled["updated"], filled["skipped"]) == (0, 1, 0)
        company = _company(client, headers, domain)
        assert company["country_code"] == "NL" and company["employees"] == 300

        # existing values are never overwritten → nothing changes → skipped
        again = _upload(
            client, headers, f"name,domain,country,employees\nGamma {tag},{domain},DE,999\n"
        ).json()
        assert (again["created"], again["updated"], again["skipped"]) == (0, 0, 1)
        assert _company(client, headers, domain)["country_code"] == "NL"
    finally:
        _cleanup(client, headers, tag)


def test_csv_import_duplicates_reported(client: TestClient, headers: dict[str, str], tag: str) -> None:
    text = f"name,domain,employees\nDelta {tag},delta-{tag}.com,\nDelta dup {tag},www.delta-{tag}.com,4000\n"
    try:
        report = _upload(client, headers, text, on_duplicate="merge").json()
        assert report["created"] == 1
        assert report["skipped"] == 1
        assert report["duplicates"] == [
            {"row": 2, "first_row": 1, "domain": f"delta-{tag}.com", "action": "merge"}
        ]
        assert _company(client, headers, f"delta-{tag}.com")["employees"] == 4000
    finally:
        _cleanup(client, headers, tag)


def test_csv_import_crunchbase_mapping(client: TestClient, headers: dict[str, str], tag: str) -> None:
    text = (
        "Organization Name,Organization Name URL,Industries,Headquarters Location,Description,"
        "CB Rank (Company),Website,LinkedIn,Number of Employees,Estimated Revenue Range\n"
        f'Epsilon {tag},https://www.crunchbase.com/organization/epsilon-{tag},"Software, Information Technology",'
        f'"Walldorf, Baden-Wurttemberg, Germany",ERP vendor,12,https://www.epsilon-{tag}.com,'
        f"https://www.linkedin.com/company/epsilon-{tag},10001+,$10B+\n"
    )
    try:
        res = _upload(client, headers, text, mapping="crunchbase")
        assert res.status_code == 200, res.text
        assert res.json()["created"] == 1
        c = _company(client, headers, f"epsilon-{tag}.com")
        assert c["name"] == f"Epsilon {tag}"
        assert c["country_code"] == "DE"
        assert c["hq_city"] == "Walldorf"
        assert "software" in c["industry_ids"]
        assert c["employees"] == 10001
        assert Decimal(str(c["revenue_eur"])) == Decimal(10**10)
        assert c["crunchbase_id"] == f"epsilon-{tag}"
        assert c["linkedin_url"] == f"https://www.linkedin.com/company/epsilon-{tag}"
        assert c["homepage_url"] == f"https://www.epsilon-{tag}.com"
    finally:
        _cleanup(client, headers, tag)


def test_csv_import_custom_mapping(client: TestClient, headers: dict[str, str], tag: str) -> None:
    text = f"Firma;Webseite;Land;Branche;Mitarbeiter\nZeta {tag};zeta-{tag}.de;Österreich;Banking;2500\n"
    column_map = json.dumps(
        {
            "name": "Firma",
            "domain": "Webseite",
            "country": "Land",
            "industry": "Branche",
            "employees": "Mitarbeiter",
        }
    )
    try:
        res = _upload(client, headers, text, mapping="custom", column_map=column_map)
        assert res.status_code == 200, res.text
        assert res.json()["created"] == 1
        c = _company(client, headers, f"zeta-{tag}.de")
        assert (c["country_code"], c["industry_ids"], c["employees"]) == ("AT", ["banking"], 2500)

        bad = _upload(client, headers, text, mapping="custom", column_map=json.dumps({"name": "Nope"}))
        assert bad.status_code == 422
        assert bad.json()["error"]["code"] == "unknown_columns"
    finally:
        _cleanup(client, headers, tag)


def test_csv_import_limits(
    client: TestClient, headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "IMPORT_MAX_BYTES", 100)
    res = _upload(client, headers, "name,domain\n" + "A,a.com\n" * 50)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"

    monkeypatch.setattr(settings, "IMPORT_MAX_BYTES", 5 * 1024 * 1024)
    monkeypatch.setattr(settings, "IMPORT_MAX_ROWS", 3)
    res = _upload(client, headers, "name,domain\n" + "".join(f"A{i},a{i}.com\n" for i in range(4)))
    assert res.status_code == 422
    body = res.json()["error"]
    assert body["code"] == "too_many_rows"
    assert body["details"] == {"max_rows": 3}
