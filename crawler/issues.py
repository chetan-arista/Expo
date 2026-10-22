"""
Single source of truth for result categories and plain-language issue diagnosis.
Used by the engine (stats), the exporters (Excel/PDF) and the web API.
"""
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

CATEGORY_LABELS = {
    "2xx": "Healthy (2xx)",
    "3xx": "Redirects (3xx)",
    "4xx": "Broken (4xx)",
    "5xx": "Server errors (5xx)",
    "error": "Network errors",
}

ISSUE_CATEGORIES = ("5xx", "4xx", "error")

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def classify(status_code: Optional[int], redirect_destination: Optional[str] = None) -> str:
    """Mutually exclusive category for a crawled URL. Final status wins over redirects."""
    if status_code is None:
        return "error"
    if status_code >= 500:
        return "5xx"
    if status_code >= 400:
        return "4xx"
    if 300 <= status_code < 400 or (redirect_destination and redirect_destination != "-"):
        return "3xx"
    if 200 <= status_code < 300:
        return "2xx"
    return "error"


def classify_result(r) -> str:
    return classify(r.status_code, r.redirect_destination)


# status code -> (title, one-line summary, likely causes, fix, severity)
_LIBRARY: Dict[int, tuple] = {
    400: ("Bad request", "The server rejected the request as malformed.",
          ["Link contains invalid characters or broken encoding", "Required query parameters are missing"],
          "Correct the link's URL format on the parent page.", "medium"),
    401: ("Login required", "The page needs authentication before it can be viewed.",
          ["Member-only page linked from a public page", "Session expired or no credentials supplied"],
          "Hide the link for logged-out visitors, or crawl with session cookies.", "medium"),
    403: ("Access forbidden", "The server refused to show this page.",
          ["Folder or file permissions block public access", "Firewall / bot protection blocked the request",
           "Page restricted to logged-in users"],
          "Check server permissions; if intentional, remove the public link.", "medium"),
    404: ("Page not found", "The link points to a page that does not exist.",
          ["Page was deleted or moved without a redirect", "Typo or wrong path in the link",
           "Wrong letter case in the URL path"],
          "Fix the link on the parent page, or add a 301 redirect to the new page.", "high"),
    405: ("Method not allowed", "The URL exists but does not accept a normal visit (GET).",
          ["Link points to a form/API endpoint instead of a page"],
          "Point the link to the page that contains the form, not the endpoint.", "medium"),
    408: ("Request timeout", "The server gave up waiting for the request.",
          ["Server overloaded or slow to respond"],
          "Check server performance and hosting limits.", "medium"),
    410: ("Page gone", "The page was intentionally and permanently removed.",
          ["Content retired on purpose but links were not cleaned up"],
          "Remove or replace the link on the parent page.", "high"),
    429: ("Rate limited", "The server is throttling requests from the crawler.",
          ["Too many requests in a short time", "Bot protection rate limits"],
          "Usually not a real visitor problem; re-crawl slower to confirm.", "low"),
    500: ("Server error", "The page crashed on the server.",
          ["Bug or unhandled exception in the page code", "Database or plugin failure",
           "Misconfigured server rules (.htaccess)"],
          "Check the server error logs at the time of the crawl.", "critical"),
    502: ("Bad gateway", "A proxy/CDN could not get a valid response from the web server.",
          ["Origin server is down or crashing", "CDN/proxy misconfiguration"],
          "Check the origin server health and CDN settings.", "critical"),
    503: ("Service unavailable", "The server is temporarily unable to serve the page.",
          ["Maintenance mode enabled", "Server overloaded or out of resources"],
          "Check maintenance mode and server capacity.", "critical"),
    504: ("Gateway timeout", "The upstream server took too long to answer the proxy/CDN.",
          ["Slow database queries or heavy page", "Origin server overloaded"],
          "Optimise the page or increase upstream timeouts.", "critical"),
}


def _family_fallback(code: int) -> tuple:
    if code >= 500:
        return ("Server error", f"The server failed with HTTP {code}.",
                ["Server-side failure while generating the page"],
                "Check the server error logs.", "critical")
    return ("Client error", f"The server rejected the request with HTTP {code}.",
            ["Link points to an invalid or restricted URL"],
            "Review the link target on the parent page.", "medium")


def diagnose(r) -> Optional[Dict[str, Any]]:
    """
    Explain why a crawled URL failed. Returns None for healthy/redirect results.
    `r` is a CrawlResult (duck-typed: status_code, url, http_status, error_message,
    discovery_source, server, redirect_destination).
    """
    category = classify(r.status_code, r.redirect_destination)
    if category not in ISSUE_CATEGORIES:
        return None

    server = (getattr(r, "server", "") or "").lower()
    source = (r.discovery_source or "").lower()
    link_tag = (getattr(r, "link_tag", "") or "").lower()
    causes: List[str] = []

    if category == "error":
        err = (r.error_message or r.http_status or "").lower()
        if "timeout" in err or "timed out" in err:
            title, summary, causes, fix, severity = (
                "Timed out", "The server did not respond in time.",
                ["Server is overloaded or very slow", "Page is too heavy to generate quickly"],
                "Check server response times and hosting limits.", "high")
        elif "ssl" in err or "certificate" in err:
            title, summary, causes, fix, severity = (
                "SSL certificate problem", "The secure connection could not be established.",
                ["Expired or self-signed certificate", "Certificate does not cover this hostname"],
                "Renew/fix the SSL certificate for this host.", "high")
        elif "name or service" in err or "getaddrinfo" in err or "nodename" in err or "resolve" in err:
            title, summary, causes, fix, severity = (
                "Domain not found", "The hostname could not be resolved (DNS).",
                ["Typo in the domain", "Domain or subdomain no longer exists"],
                "Fix the link's domain or restore the DNS record.", "high")
        else:
            title, summary, causes, fix, severity = (
                "Connection failed", "The crawler could not connect to the server.",
                ["Server down or refusing connections", "Firewall blocking the request"],
                "Check that the server is up and reachable.", "high")
        code_label = r.http_status or "Network error"
    else:
        code = int(r.status_code)
        title, summary, base_causes, fix, severity = _LIBRARY.get(code) or _family_fallback(code)
        causes = list(base_causes)
        code_label = r.http_status or str(code)

        # Context-specific hints (put the most specific first)
        if code in (403, 429, 503) and ("cloudflare" in server or "ddos" in server or "akamai" in server):
            causes.insert(0, f"Blocked by {server.split('/')[0].title()} bot protection; may load fine in a real browser")
            severity = "low"
        if code in (404, 410):
            path = urlparse(r.url).path
            if "xml sitemap" in source or link_tag == "sitemap":
                causes.insert(0, "Listed in the XML sitemap but the page no longer exists")
                fix = "Remove the URL from the sitemap (or restore the page)."
            elif link_tag in ("script", "bundle"):
                causes.insert(0, "Path came from JavaScript code; it may not be a real page")
                severity = "low"
            if any(c.isupper() for c in path):
                causes.insert(1, "URL path contains capital letters; servers are often case-sensitive")
            if "%20" in path or " " in path or "%25" in path:
                causes.insert(0, "URL contains spaces or double-encoded characters")
        if r.redirect_destination and r.redirect_destination != "-":
            causes.insert(0, f"Redirect chain ends on an error page ({r.redirect_destination})")

    return {
        "category": category,
        "code_label": code_label,
        "title": title,
        "summary": summary,
        "causes": causes[:3],
        "fix": fix,
        "severity": severity,
    }


def issue_sort_key(r) -> tuple:
    """Most severe first, then by status code, then URL."""
    d = diagnose(r) or {}
    return (SEVERITY_ORDER.get(d.get("severity", "low"), 9), -(r.status_code or 999), r.url)
