"""
External tab (Screaming Frog "External"): every URL on another site that crawled pages link to or
load (scripts, stylesheets, images, iframes), once each. Filters by content type (same buckets as
Internal, from the Content-Type the link check received) and by response class. Broken external links
are reported as issues in the Response Codes tab, as Screaming Frog does, so nothing here is an issue.
"""
from ..registry import Column, Filter, Tab, register
from .internal import (ADDRESS, CONTENT_KINDS, INDEXABILITY, INDEXABILITY_STATUS, INLINKS, REDIRECT_URL,
                       RESPONSE_TIME, STATUS, STATUS_CODE, category_of, content_kind, unique_inlinks_count)


def external_rows(site):
    return list(site.externals or [])


def _content_type(r, site) -> str:
    return (getattr(r, "content_type", "") or "").strip().lower()


def _anchor(r, site) -> str:
    a = getattr(r, "anchor_text", "") or ""
    return "" if a == "-" else a


COLUMNS = [
    ADDRESS,
    Column("content_type", "Content Type", _content_type),
    STATUS_CODE, STATUS, INDEXABILITY, INDEXABILITY_STATUS,
    INLINKS,
    Column("unique_inlinks", "Unique Inlinks", lambda r, site: unique_inlinks_count(site, r), "num"),
    Column("domain", "Domain", lambda r, site: getattr(r, "domain", "") or ""),
    Column("found_on", "Parent Link", lambda r, site: getattr(r, "parent_url", "") or "", "url"),
    Column("anchor", "Anchor Text", _anchor),
    REDIRECT_URL, RESPONSE_TIME,
]

_KIND_HELP = {
    "html": "Outbound links to web pages.",
    "javascript": "JavaScript files your pages load from other sites.",
    "css": "Stylesheets your pages load from other sites.",
    "images": "Images your pages load or link to on other sites.",
    "media": "Audio and video files on other sites.",
    "fonts": "Web fonts loaded from other sites.",
    "xml": "XML files on other sites.",
    "pdf": "PDF documents on other sites.",
    "plugins": "Browser-plugin files on other sites.",
    "other": "Anything else that answered, including answers that sent no Content-Type header.",
    "unknown": "Type unknown: the URL did not answer or the other site's robots.txt blocked it. "
               "When link checking is off, the type is guessed from the file extension.",
}

_STATUS_FILTERS = [
    ("blocked_robots", "Blocked by Robots.txt", "blocked",
     "The other site's robots.txt does not allow crawlers to request this URL, so it was not checked, or the "
     "site refused the check with a bot-block code such as LinkedIn's 999 (Status: \"Blocked by site (999)\")."),
    ("no_response", "No Response", "error",
     "The request got no answer: timeout, refused connection, DNS or SSL failure."),
    ("success", "Success (2xx)", "2xx", "The URL loads normally."),
    ("redirection", "Redirection (3xx)", "3xx", "The URL redirects somewhere else."),
    ("client_error", "Client Error (4xx)", "4xx", "The URL is broken or refuses access (404, 403, 410...)."),
    ("server_error", "Server Error (5xx)", "5xx", "The other site's server failed to answer properly."),
    ("not_checked", "Not Checked", "",
     "Recorded but never requested (external link checking is turned off or still running)."),
]


def _kind_filter(kind: str, name: str) -> Filter:
    return Filter(kind, name, lambda r, site, k=kind: content_kind(r) == k, description=_KIND_HELP[kind])


def _status_filter(fid: str, name: str, cat: str, desc: str) -> Filter:
    return Filter(fid, name, lambda r, site, c=cat: category_of(r) == c, description=desc)


register(Tab(
    id="external", name="External", order=20, columns=COLUMNS,
    filters=[_kind_filter(k, n) for k, n in CONTENT_KINDS] + [_status_filter(*f) for f in _STATUS_FILTERS],
    rows=external_rows,
))
