"""
Single source of truth for result categories and plain-language issue diagnosis.
Used by the engine (stats), the exporters (Excel/PDF) and the web API.
"""
from typing import Optional, List, Dict, Any
from urllib.parse import urlparse

# One name per status bucket, used by the dashboard, the Excel workbook and the PDF alike
CATEGORY_LABELS = {
    "2xx": "2xx Success",
    "3xx": "3xx Redirection",
    "4xx": "4xx Client Error",
    "5xx": "5xx Server Error",
    "error": "No Response",
    "blocked": "Blocked by robots.txt",
}
# Upper-case short forms for tiles (dashboard and PDF)
CATEGORY_SHORT = {
    "2xx": "2XX OK",
    "3xx": "3XX REDIRECT",
    "4xx": "4XX CLIENT ERR",
    "5xx": "5XX SERVER ERR",
    "error": "NO RESPONSE",
    "blocked": "BLOCKED",
}

ISSUE_CATEGORIES = ("5xx", "4xx", "error")

# Links taken from code rather than from an element a visitor can click
CODE_LINK_TAGS = ("bundle", "script")

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# Codes above 599 are not HTTP statuses: sites send them to refuse automated checkers (LinkedIn answers 999).
# They are counted as Blocked ("Blocked by site (999)"), never as a 5xx server error.
def is_site_block_code(status_code: Optional[int]) -> bool:
    try:
        return status_code is not None and int(status_code) > 599
    except (TypeError, ValueError):
        return False


def site_block_label(status_code: Optional[int]) -> str:
    return f"Blocked by site ({int(status_code)})"


def classify(status_code: Optional[int], redirect_destination: Optional[str] = None,
             blocked: bool = False) -> str:
    """Mutually exclusive category for a crawled URL, from its own HTTP status alone (the crawler
    does not follow redirects inside one request: a 301 is a 3xx even when its target is a 404,
    and that target is crawled and counted as a URL of its own). How the URL was found never
    changes the category: a 404 is counted as broken whether the link sits in HTML or in JavaScript."""
    if blocked:
        return "blocked"
    if status_code is None:
        return "error"
    if is_site_block_code(status_code):
        return "blocked"         # LinkedIn's 999 and other non-standard codes: the site refused the checker
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
    return classify(r.status_code, r.redirect_destination, getattr(r, "blocked", False))


def found_in_code(r) -> bool:
    """The link to this URL was read from JavaScript, not from an element in the page's HTML."""
    return getattr(r, "link_tag", "") in CODE_LINK_TAGS


def is_page(r) -> bool:
    """An HTML page (including error pages), as opposed to a file (image, PDF, script)."""
    return getattr(r, "resource_type", "page") == "page" and classify_result(r) != "blocked"


def health_score(results) -> dict:
    """Share of HTML pages that load (2xx/3xx). Files and robots-blocked URLs are
    reported separately and never move the score."""
    pages = [r for r in results if is_page(r)]
    ok = sum(1 for r in pages if classify_result(r) in ("2xx", "3xx"))
    return {
        "score": round(ok / len(pages) * 100) if pages else 100,
        "pages": len(pages),
        "pages_ok": ok,
        "files": sum(1 for r in results if getattr(r, "resource_type", "page") == "file"),
    }


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
    category = classify_result(r)
    if category == "4xx" and found_in_code(r):
        # A path read only from the site's JavaScript: no link or button on the crawled pages leads there
        siblings = getattr(r, "code_siblings", None) or []
        greyed = getattr(r, "code_greyed", None) or {}
        source = getattr(r, "source_file", "") or "an inline script"
        path = urlparse(r.url).path or "/"
        causes = []
        if greyed:
            tip = f' ("{greyed["title"]}")' if greyed.get("title") else ""
            summary = (f'The menu shows "{greyed.get("label", "")}" greyed out{tip}, so visitors cannot click it. '
                       f"The path {path} behind it is only in the site's JavaScript and answers {r.status_code} "
                       "when opened directly.")
            causes.append("A menu item that stays switched off until something is set up (an account setting, "
                          "a first project or a permission)")
        else:
            summary = (f"The path {path} is written in the site's JavaScript ({source}) but no link or button on "
                       f"the crawled pages leads to it; opened directly it answers {r.status_code}.")
            if siblings:
                causes.append(f"Listed in the same code as working pages ({', '.join(siblings[:3])}): probably a "
                              "menu item that is hidden or switched off for this visitor")
            else:
                causes.append("Code for a page that was never built or was removed")
        causes += [f"A piece the app adds to a longer address (for example /<project>{path})",
                   "Unused code left behind after a redesign"]
        return {
            "category": category,
            "code_label": r.http_status or str(r.status_code),
            "title": "Found in code, not clickable",
            "summary": summary,
            "causes": causes,
            "fix": "Nothing is broken for visitors today because they cannot click it. If the item should work, "
                   "build the page or point the code at the right address; if not, remove the path from the code.",
            "severity": "low",
        }
    if category == "blocked" and is_site_block_code(getattr(r, "status_code", None)):
        label = site_block_label(r.status_code)
        return {
            "category": category,
            "code_label": label,
            "title": label,
            "summary": "The site refused the automated check (a bot block, not a server error). The link usually "
                       "works for visitors.",
            "causes": ["The site blocks crawlers and link checkers (LinkedIn answers 999)"],
            "fix": "Open the link in a browser to confirm it works; no change is needed when it does.",
            "severity": "low",
        }
    if category == "blocked":
        return {
            "category": category,
            "code_label": "Blocked by robots.txt",
            "title": "Blocked by robots.txt",
            "summary": "robots.txt tells search engines not to crawl this URL, so it was not fetched.",
            "causes": ["An intentional rule for private or low-value pages",
                       "A rule broader than intended (for example Disallow: / left from staging)"],
            "fix": "Confirm the block is intended. If the page should rank, remove the matching "
                   "Disallow rule from robots.txt.",
            "severity": "medium",
        }
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
        elif "redirect" in err and ("exceed" in err or "too many" in err or "loop" in err):
            title, summary, causes, fix, severity = (
                "Redirect loop", "The server answered, but its redirects never reach a page that loads.",
                ["Two redirect rules send visitors back and forth (for example with and without a slash)",
                 "A CMS, plugin or CDN rule conflicts with a server rule"],
                "Check the redirect rules for this URL and remove the one that sends it back; every redirect "
                "should end on a page that returns 200.", "high")
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
            if any(c.isupper() for c in path):
                causes.insert(1, "URL path contains capital letters; servers are often case-sensitive")
            if "%20" in path or " " in path or "%25" in path:
                causes.insert(0, "URL contains spaces or double-encoded characters")
            if getattr(r, "resource_type", "page") == "file" and link_tag in ("img", "source", "picture", "image"):
                # an image the page shows, not a link anyone clicks: visitors see a missing picture
                title = "Image not found"
                summary = "The page shows an image file that no longer exists, so visitors see an empty or broken image."
                causes = ["The file was deleted or renamed in the media library after the page was published"] + \
                    [c for c in causes if "capital" in c or "spaces" in c] + ["The page copies an old image address"]
                fix = ("Re-upload the image under the same address, or replace it on the page with an existing one "
                       "(or remove it).")
                severity = "medium"
        if source == "redirect" or link_tag == "redirect":
            # Reached only through a redirect: the redirect rule is what needs changing
            causes.insert(0, f"A redirect sends visitors here: {r.parent_url} redirects to this URL")
            fix = ("Change the redirect on the URL that points here to a page that works (or bring this page "
                   "back), and update links to the redirecting URL to use the final address.")

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
