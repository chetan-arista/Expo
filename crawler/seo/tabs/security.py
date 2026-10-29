"""
Security tab (Screaming Frog "Security"): HTTP vs HTTPS, mixed content, insecure forms, unsafe
target="_blank" links, protocol-relative links, security response headers and bad content types.
"All" lists every internal URL.

The response-header checks (HSTS, CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy) run
on every internal URL that answered 2xx, pages and files alike, as Screaming Frog does: the reference
crawl flags 435 of 441 URLs (98.64 %), images, scripts and stylesheets included; only its 5 redirects
and 1 404 are left out. X-Frame-Options passes only with DENY or SAMEORIGIN (a CSP frame-ancestors
rule does not count, matching Screaming Frog's definition). Protocol-Relative Resource Links looks at
resources (images, scripts, CSS, frames) only, not at <a href="//..."> hyperlinks.
"""
import os
import re
from typing import List, Optional
from urllib.parse import urlparse

from ..registry import Column, Filter, Tab, first, register, seo
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE,
                       canonical_of, category_of, content_type_of, headers_of, internal_rows, is_html_doc)

SECURE_REFERRER_POLICIES = {"no-referrer-when-downgrade", "strict-origin-when-cross-origin",
                            "no-referrer", "strict-origin"}
SECURE_FRAME_OPTIONS = {"deny", "sameorigin"}
_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

# File extension -> test the Content-Type must pass
_EXPECTED_TYPE = {
    ".js": lambda m: "javascript" in m or "ecmascript" in m, ".mjs": lambda m: "javascript" in m,
    ".css": lambda m: m == "text/css",
    ".pdf": lambda m: m == "application/pdf",
    ".json": lambda m: "json" in m,
    ".woff": lambda m: "font" in m or "woff" in m or m == "application/octet-stream",
    ".woff2": lambda m: "font" in m or "woff" in m or m == "application/octet-stream",
    ".ttf": lambda m: "font" in m or "truetype" in m or m == "application/octet-stream",
    ".otf": lambda m: "font" in m or "opentype" in m or m == "application/octet-stream",
}
for _ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif", ".tif", ".tiff"):
    _EXPECTED_TYPE[_ext] = lambda m: m.startswith("image/")


def _https(r) -> bool:
    return r.url.lower().startswith("https://")


def _header_page(r) -> bool:
    """Internal URL that answered 2xx (any content type) and whose response headers were recorded."""
    return category_of(r) == "2xx" and headers_of(r) is not None


def _h(r, name: str) -> str:
    return ((headers_of(r) or {}).get(name, "") or "").strip()


# ---------------------------------------------------------------------------------------------
# Header checks
# ---------------------------------------------------------------------------------------------
def missing_hsts(r, site) -> bool:
    # Browsers ignore HSTS sent over plain HTTP, so only HTTPS pages are checked
    return _header_page(r) and _https(r) and not _h(r, "strict-transport-security")


def missing_csp(r, site) -> bool:
    return _header_page(r) and not _h(r, "content-security-policy")


def missing_xcto(r, site) -> bool:
    return _header_page(r) and _h(r, "x-content-type-options").lower() != "nosniff"


def missing_xfo(r, site) -> bool:
    """No X-Frame-Options header with DENY or SAMEORIGIN (Screaming Frog's definition)."""
    return _header_page(r) and _h(r, "x-frame-options").lower() not in SECURE_FRAME_OPTIONS


def referrer_policy(r) -> str:
    """The policy the browser applies: the last recognised value of the header."""
    tokens = [t.strip().lower() for t in _h(r, "referrer-policy").split(",") if t.strip()]
    return tokens[-1] if tokens else ""


def missing_referrer_policy(r, site) -> bool:
    return _header_page(r) and referrer_policy(r) not in SECURE_REFERRER_POLICIES


def missing_headers(r) -> List[str]:
    out = []
    for name, test in (("HSTS", missing_hsts), ("Content-Security-Policy", missing_csp),
                       ("X-Content-Type-Options", missing_xcto), ("X-Frame-Options", missing_xfo),
                       ("Referrer-Policy", missing_referrer_policy)):
        if test(r, None):
            out.append(name)
    return out


# ---------------------------------------------------------------------------------------------
# Content checks
# ---------------------------------------------------------------------------------------------
def unsafe_cross_origin(r, site) -> bool:
    if not is_html_doc(r):
        return False
    return any(not l.get("internal") and l.get("target_blank") and not l.get("noopener")
               for l in seo(r).get("links") or ())


def bad_content_type_reason(r) -> Optional[str]:
    """Why the declared Content-Type looks wrong for a URL that loaded, or None (a missing header
    is not flagged, matching Screaming Frog)."""
    if category_of(r) != "2xx":
        return None
    ct = content_type_of(r)
    mime = ct.split(";", 1)[0].strip()
    if not mime:
        return None      # no header at all: Screaming Frog files these under Internal > Other, not here
    if not _MIME_RE.match(mime):
        return f"Invalid MIME type '{mime}'"
    try:
        ext = os.path.splitext(urlparse(r.url).path.lower())[1]
    except Exception:
        ext = ""
    test = _EXPECTED_TYPE.get(ext)
    if test and not test(mime):
        return f"'{ext}' file served as {mime}"
    return None


FILTERS = [
    Filter("http_urls", "HTTP URLs", lambda r, site: r.url.lower().startswith("http://"), "Issue", "High",
           description="Internal URLs on plain HTTP. Nothing sent to or from them is encrypted, browsers label "
                       "them 'Not secure', and Google prefers HTTPS pages.",
           fix="Serve the whole site over HTTPS, 301-redirect every HTTP URL to its HTTPS version, and update "
               "internal links, canonicals and sitemaps to HTTPS."),
    Filter("https_urls", "HTTPS URLs", lambda r, site: r.url.lower().startswith("https://"),
           description="Internal URLs served securely over HTTPS."),
    Filter("mixed_content", "Mixed Content",
           lambda r, site: is_html_doc(r) and _https(r) and bool(seo(r).get("mixed_content")), "Issue", "High",
           description="HTTPS pages that load images, scripts or stylesheets over plain HTTP. Browsers block or "
                       "warn about these files, so parts of the page can break and the padlock is lost.",
           fix="Change those resource URLs to HTTPS (the files must be available over HTTPS). The page's "
               "Details pane lists each HTTP resource."),
    Filter("form_url_insecure", "Form URL Insecure",
           lambda r, site: is_html_doc(r) and any(f.get("insecure") for f in seo(r).get("forms") or ()),
           "Issue", "High",
           description="Pages with a form that sends its data to an HTTP address. Whatever visitors type "
                       "(names, emails, passwords) travels unencrypted, and browsers warn before submitting.",
           fix="Change the form's action to the HTTPS address."),
    Filter("form_on_http", "Form on HTTP URL",
           lambda r, site: is_html_doc(r) and not _https(r) and bool(seo(r).get("forms")), "Issue", "High",
           description="Forms on pages served over plain HTTP. Data typed into them can be read or changed on "
                       "the way, and browsers mark the fields 'Not secure'.",
           fix="Move the page to HTTPS and redirect the HTTP version."),
    Filter("unsafe_cross_origin", "Unsafe Cross-Origin Links", unsafe_cross_origin, "Warning", "Low",
           description="Pages that open links to other websites in a new tab (target=\"_blank\") without "
                       "rel=\"noopener\" or \"noreferrer\". In older browsers the other site can take control of "
                       "your tab and swap it for a fake page.",
           fix="Add rel=\"noopener\" (or \"noopener noreferrer\") to every external link that uses "
               "target=\"_blank\"; most CMSs and themes have a setting for this."),
    Filter("protocol_relative", "Protocol-Relative Resource Links",
           lambda r, site: is_html_doc(r) and bool(seo(r).get("protocol_relative")), "Warning", "Low",
           description="Pages that load images, scripts, stylesheets or frames from addresses starting '//' "
                       "(no http: or https:). On an HTTP page these load insecurely, and the shortcut is no "
                       "longer needed. Ordinary links written this way are not counted.",
           fix="Write those resource URLs in full with https://."),
    Filter("missing_hsts", "Missing HSTS Header", missing_hsts, "Warning", "Low",
           description="HTTPS URLs that answered without a Strict-Transport-Security header, so browsers may "
                       "still try the insecure HTTP version first. Every URL that loaded (2xx) is checked, "
                       "files included, as Screaming Frog does; plain-HTTP URLs are listed under HTTP URLs.",
           fix="Send 'Strict-Transport-Security: max-age=31536000; includeSubDomains' from the server or CDN "
               "once every page works over HTTPS."),
    Filter("missing_csp", "Missing Content-Security-Policy Header", missing_csp, "Warning", "Low",
           description="URLs that answered without a Content-Security-Policy header, which tells the browser "
                       "where scripts, styles and frames may load from and limits the damage of injected code. "
                       "Only presence is checked, on every URL that loaded (2xx).",
           fix="Add a Content-Security-Policy header. Start in Report-Only mode to see what would break, then "
               "enforce it."),
    Filter("missing_xcto", "Missing X-Content-Type-Options Header", missing_xcto, "Warning", "Low",
           description="URLs that answered without 'X-Content-Type-Options: nosniff', so the browser may guess "
                       "a file's type instead of trusting the declared one. Checked on every URL that loaded (2xx).",
           fix="Send 'X-Content-Type-Options: nosniff' on every response from the server or CDN."),
    Filter("missing_xfo", "Missing X-Frame-Options Header", missing_xfo, "Warning", "Low",
           description="URLs that answered without an X-Frame-Options header set to DENY or SAMEORIGIN, so other "
                       "sites may show them inside a frame (click-jacking). Checked on every URL that loaded "
                       "(2xx). A CSP frame-ancestors rule does a similar job in modern browsers but does not "
                       "count here.",
           fix="Send 'X-Frame-Options: SAMEORIGIN' from the server or CDN (keep a CSP 'frame-ancestors' rule "
               "too if you have one)."),
    Filter("missing_referrer_policy", "Missing Secure Referrer-Policy Header", missing_referrer_policy,
           "Warning", "Low",
           description="Pages without a safe Referrer-Policy header (no-referrer, no-referrer-when-downgrade, "
                       "strict-origin or strict-origin-when-cross-origin), so full page addresses may be passed "
                       "to other sites. Checked on every URL that loaded (2xx).",
           fix="Send 'Referrer-Policy: strict-origin-when-cross-origin' from the server or CDN."),
    Filter("bad_content_type", "Bad Content Type", lambda r, site: bad_content_type_reason(r) is not None,
           "Warning", "Low",
           description="URLs whose Content-Type header is malformed or does not match the file (for "
                       "example a .js file or an image served as text/html). With nosniff on, browsers refuse "
                       "to use such files; often it is an error page answering with 200.",
           fix="Fix the server's MIME type settings so each file is sent with its real type, and make missing "
               "files return 404 instead of an HTML page."),
]

register(Tab(
    id="security", name="Security", order=30,
    columns=[ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS,
             Column("http_version", "HTTP Version", lambda r, site: seo(r).get("http_version") or ""),
             INDEXABILITY, INDEXABILITY_STATUS,
             Column("canonical", "Canonical Link Element 1", lambda r, site: canonical_of(r), "url"),
             Column("meta_robots_1", "Meta Robots 1", lambda r, site: first(seo(r).get("meta_robots") or [])),
             Column("x_robots_tag_1", "X-Robots-Tag 1", lambda r, site: first(seo(r).get("x_robots_tag") or [])),
             Column("missing_headers", "Missing Security Headers", lambda r, site: missing_headers(r), "list"),
             Column("content_type_problem", "Content Type Problem",
                    lambda r, site: bad_content_type_reason(r) or "")],
    filters=FILTERS,
    rows=internal_rows,
))
