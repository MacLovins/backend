from urllib.parse import urlparse


def normalize_domain(url_or_domain: str) -> str:
    """
    Normalize domain string:
    - Strips whitespace
    - Lowercases
    - Strips http/https schemes and paths
    - Strips leading 'www.'
    - Strips port numbers if default
    """
    cleaned = url_or_domain.strip().lower()
    if not cleaned:
        return ""

    if not cleaned.startswith(("http://", "https://")):
        cleaned = "https://" + cleaned

    try:
        parsed = urlparse(cleaned)
        netloc = parsed.netloc or parsed.path
        # remove port if present
        host = netloc.split(":")[0]
        # remove leading www.
        if host.startswith("www."):
            host = host[4:]
        return host.strip("/").strip()
    except Exception:
        # Fallback basic strip
        domain = cleaned.replace("http://", "").replace("https://", "").split("/")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.strip()
