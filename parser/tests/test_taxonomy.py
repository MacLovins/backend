from leadradar_parser import country_catalog, industry_taxonomy

REQUIRED_IDS = {
    "logistics",
    "airlines",
    "rail",
    "postal_courier",
    "automotive",
    "manufacturing",
    "chemicals",
    "pharma",
    "medical_devices",
    "healthcare",
    "banking",
    "insurance",
    "financial_markets",
    "telecom",
    "energy_utilities",
    "oil_gas",
    "water",
    "retail",
    "consumer_goods",
    "food_beverage",
    "public_sector",
    "digital_infrastructure",
    "it_services",
    "software",
    "media",
    "construction",
    "real_estate",
}


def test_required_industry_ids_exist_and_have_wikidata_ids() -> None:
    industries = {item.id: item for item in industry_taxonomy()}
    assert REQUIRED_IDS <= industries.keys()
    assert all(industries[item].wikidata for item in REQUIRED_IDS)


def test_country_catalog_has_unique_iso_codes() -> None:
    countries = country_catalog()
    assert len({country.code for country in countries}) == len(countries)
    assert {"DE", "AT", "RO"} <= {country.code for country in countries}
