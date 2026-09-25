from leadradar_core.utils.domain import normalize_domain


def test_normalize_domain():
    assert normalize_domain("https://www.dhl.com/global/en/home.html") == "dhl.com"
    assert normalize_domain("http://DHL.COM/") == "dhl.com"
    assert normalize_domain("WWW.ORANGE-SYSTEMS.MD") == "orange-systems.md"
    assert normalize_domain("  siemens.de/careers  ") == "siemens.de"
    assert normalize_domain("subdomain.example.co.uk:443") == "subdomain.example.co.uk"
