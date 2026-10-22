import os
import re
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode
from typing import Optional, Set
from .config import EXCLUDED_EXTENSIONS, STRIP_QUERY_PARAMS

IGNORED_SCHEMES = {
    "mailto", "tel", "javascript", "whatsapp", "callto", "sms",
    "data", "file", "ftp", "irc", "skype", "intent"
}

def clean_hostname(netloc: str) -> str:
    """Extract and lowercase hostname without port or www prefix."""
    host = netloc.split(":")[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host

def get_domain(url: str) -> str:
    """Return core domain of a URL (e.g. 'aristasystems.in')."""
    try:
        parsed = urlparse(url)
        return clean_hostname(parsed.netloc)
    except Exception:
        return ""

def is_valid_http_scheme(url: str) -> bool:
    """Check if URL starts with http or https."""
    try:
        scheme = urlparse(url).scheme.lower()
        return scheme in ("http", "https")
    except Exception:
        return False

def normalize_url(raw_url: str, base_url: str = "") -> Optional[str]:
    """
    Resolve relative URLs, strip fragments, normalize slashes,
    remove tracking query params, and canonicalize path.
    """
    if not raw_url:
        return None

    raw_url = raw_url.strip()

    # Quick ignore check
    lower_raw = raw_url.lower()
    for sch in IGNORED_SCHEMES:
        if lower_raw.startswith(sch + ":"):
            return None

    # Resolve against base URL if relative
    if base_url:
        try:
            full_url = urljoin(base_url, raw_url)
        except Exception:
            return None
    else:
        full_url = raw_url

    try:
        parsed = urlparse(full_url)
    except Exception:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    netloc = parsed.netloc.lower()
    # Remove standard ports
    if ":" in netloc:
        host, port = netloc.split(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    # Normalize path
    path = parsed.path
    if not path:
        path = "/"
    else:
        # Collapse multiple slashes
        path = re.sub(r"/{2,}", "/", path)

    # Clean query parameters
    query_str = ""
    if parsed.query:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=False)
        cleaned_pairs = [
            (k, v) for k, v in query_pairs
            if k.lower() not in STRIP_QUERY_PARAMS and not k.lower().startswith("utm_")
        ]
        if cleaned_pairs:
            cleaned_pairs.sort(key=lambda x: x[0])
            query_str = urlencode(cleaned_pairs)

    # Reconstruct without fragment
    normalized = urlunparse((scheme, netloc, path, "", query_str, ""))
    return normalized

def is_internal_url(
    url: str,
    base_domain: str,
    include_subdomains: bool = False,
    allowed_domains: Optional[Set[str]] = None
) -> bool:
    """
    Determine if a given URL is strictly internal to the base website.
    """
    if not url:
        return False

    url_domain = get_domain(url)
    if not url_domain:
        return False

    clean_base = clean_hostname(base_domain)
    clean_url = clean_hostname(url_domain)

    # Exact domain match
    if clean_url == clean_base:
        return True

    # Allowed domains list
    if allowed_domains:
        if clean_url in allowed_domains or url_domain in allowed_domains:
            return True
        for ad in allowed_domains:
            if clean_url == clean_hostname(ad):
                return True

    # Subdomain match (e.g. blog.example.com when base is example.com)
    if include_subdomains:
        if clean_url.endswith("." + clean_base):
            return True

    return False

def is_parsable_html_url(url: str) -> bool:
    """
    Check if URL does not end in a known binary or non-HTML asset extension.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        _, ext = os.path.splitext(path)
        if ext and ext in EXCLUDED_EXTENSIONS:
            return False
        return True
    except Exception:
        return True

def is_excluded_path(url: str, exclude_patterns: list) -> bool:
    """Check if URL contains any excluded pattern."""
    if not exclude_patterns:
        return False
    lower_url = url.lower()
    for pattern in exclude_patterns:
        if pattern.lower() in lower_url:
            return True
    return False
