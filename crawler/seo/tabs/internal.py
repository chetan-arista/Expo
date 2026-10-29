"""
Internal tab (Screaming Frog "Internal"): every internal URL the crawl requested, pages and files
alike, filtered by content type. The content-type buckets are mutually exclusive, so the filter
counts always add up to "All".

This module also holds the small helpers the External, Response Codes, Security and URL tabs share
(content-type bucket, HTTP status text, link counts), so every tab shows the same numbers.
"""
import os
import re
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from ...issues import classify, classify_result, is_site_block_code, site_block_label
from ...url_utils import clean_hostname, get_domain, normalize_url
from ..registry import Column, Filter, Tab, first, register, rendered, seo
from ..text_metrics import pixel_width

# ---------------------------------------------------------------------------------------------
# Row type
# ---------------------------------------------------------------------------------------------
def is_external(r) -> bool:
    """An ExternalLink (outbound link) rather than a crawled internal CrawlResult."""
    return not hasattr(r, "resource_type") and hasattr(r, "domain")


def internal_rows(site) -> List[Any]:
    """Every internal URL once, in crawl order."""
    return list(site.by_url.values())


# ---------------------------------------------------------------------------------------------
# Status / category
# ---------------------------------------------------------------------------------------------
def external_checked(e) -> bool:
    """True once the external link was requested (status known or a failure recorded)."""
    return getattr(e, "status_code", None) is not None or bool((getattr(e, "http_status", "") or "").strip())


def category_of(r) -> str:
    """'2xx' | '3xx' | '4xx' | '5xx' | 'error' | 'blocked' from the HTTP status alone,
    '' for an external link that was never checked."""
    if not is_external(r):
        try:
            return classify_result(r)
        except Exception:
            return "error"
    text = (getattr(r, "http_status", "") or "").lower()
    if "robots" in text:
        return "blocked"
    if not external_checked(r):
        return ""
    return classify(getattr(r, "status_code", None))


_CODE_RE = re.compile(r"\b([1-5]\d\d)\b")


def status_codes_seen(r) -> List[int]:
    """Every HTTP code recorded for the request, first hop first ("301 -> 302 -> 200" -> [301, 302, 200])."""
    chain = redirect_chain(r)
    codes = [c for _, c in chain if c]
    if codes:
        final = getattr(r, "status_code", None)
        return codes + ([final] if final and codes[-1] != final else [])
    text = getattr(r, "http_status", "") or ""
    if "->" in text:
        return [int(c) for c in _CODE_RE.findall(text)]
    code = getattr(r, "status_code", None)
    return [code] if code else []


def first_status_code(r) -> Optional[int]:
    """Status of the first response, the way Screaming Frog reports it (a redirect shows 301,
    not the 200 it leads to). 0 when there was no response or robots.txt blocked the URL;
    None for an external link that was never checked."""
    cat = category_of(r)
    if cat == "":
        return None
    if cat == "blocked" and is_site_block_code(getattr(r, "status_code", None)):
        return int(r.status_code)    # the site's own bot-block code (LinkedIn 999), shown as sent
    if cat in ("blocked", "error"):
        return 0
    codes = status_codes_seen(r)
    return codes[0] if codes else (getattr(r, "status_code", None) or 0)


def status_text(r) -> str:
    """Reason phrase of the first response ("OK", "Moved Permanently"), or why there was none."""
    cat = category_of(r)
    if cat == "blocked":
        code = getattr(r, "status_code", None)
        return site_block_label(code) if is_site_block_code(code) else "Blocked by robots.txt"
    if cat == "":
        return "Not checked"
    if cat == "error":
        return (getattr(r, "http_status", "") or "No Response").strip() or "No Response"
    code = first_status_code(r)
    try:
        return HTTPStatus(code).phrase
    except Exception:
        text = getattr(r, "http_status", "") or ""
        return text.split(" ", 1)[1] if " " in text and "->" not in text else text


def redirect_chain(r) -> List[Tuple[str, Optional[int]]]:
    """[(url, status)] of each redirect response, when the crawler recorded the hops
    (CrawlResult.redirect_chain / redirect_history: urls, dicts or (url, code) pairs)."""
    raw = getattr(r, "redirect_chain", None) or getattr(r, "redirect_history", None) or []
    hops: List[Tuple[str, Optional[int]]] = []
    try:
        for item in raw:
            if isinstance(item, str):
                hops.append((item, None))
            elif isinstance(item, dict):
                code = item.get("status_code", item.get("status"))
                hops.append((str(item.get("url") or ""), int(code) if str(code or "").isdigit() else None))
            elif isinstance(item, (tuple, list)) and item:
                code = item[1] if len(item) > 1 else None
                hops.append((str(item[0] or ""), int(code) if str(code or "").isdigit() else None))
    except Exception:
        return []
    return hops


def response_time_s(r) -> Any:
    ms = getattr(r, "response_time_ms", None)
    if ms is None or (not ms and category_of(r) in ("", "blocked")):
        return ""
    try:
        return round(int(ms or 0) / 1000.0, 3)
    except Exception:
        return ""


def headers_of(r) -> Optional[Dict[str, str]]:
    """Lowercased response headers, or None when the crawler did not record them."""
    h = getattr(r, "response_headers", None)
    if h is None:
        return None
    try:
        return {str(k).lower(): (", ".join(v) if isinstance(v, (list, tuple)) else str(v)) for k, v in h.items()}
    except Exception:
        return None


def header(r, name: str) -> str:
    return (headers_of(r) or {}).get(name.lower(), "") or ""


# ---------------------------------------------------------------------------------------------
# Content type buckets (Screaming Frog: HTML, JavaScript, CSS, Images, Media, Fonts, XML, PDF,
# Plugins, Other, Unknown)
# ---------------------------------------------------------------------------------------------
CONTENT_KINDS = (
    ("html", "HTML"), ("javascript", "JavaScript"), ("css", "CSS"), ("images", "Images"),
    ("media", "Media"), ("fonts", "Fonts"), ("xml", "XML"), ("pdf", "PDF"), ("plugins", "Plugins"),
    ("other", "Other"), ("unknown", "Unknown"),
)

_EXT_KIND = {
    ".html": "html", ".htm": "html", ".php": "html", ".asp": "html", ".aspx": "html",
    ".js": "javascript", ".mjs": "javascript", ".css": "css",
    ".png": "images", ".jpg": "images", ".jpeg": "images", ".gif": "images", ".webp": "images",
    ".svg": "images", ".bmp": "images", ".ico": "images", ".tif": "images", ".tiff": "images", ".avif": "images",
    ".mp4": "media", ".mp3": "media", ".webm": "media", ".avi": "media", ".mov": "media", ".mkv": "media",
    ".wav": "media", ".ogg": "media", ".m4a": "media", ".m4v": "media",
    ".woff": "fonts", ".woff2": "fonts", ".ttf": "fonts", ".otf": "fonts", ".eot": "fonts",
    ".xml": "xml", ".rss": "xml", ".atom": "xml", ".pdf": "pdf", ".swf": "plugins",
}


def content_type_of(r) -> str:
    """Content-Type header the URL answered with ("" when the server sent none). PageData holds the
    header as received; the result's own field is only a fallback for results without PageData."""
    p = getattr(r, "seo", None)
    ct = p.get("content_type", "") if isinstance(p, dict) and p else getattr(r, "content_type", "")
    return str(ct or "").strip().lower()


def kind_from_content_type(ct: str) -> str:
    """Bucket for a Content-Type value ('' when there is none)."""
    mime = (ct or "").split(";", 1)[0].strip().lower()
    if not mime:
        return ""
    if "html" in mime:
        return "html"
    if "javascript" in mime or "ecmascript" in mime:
        return "javascript"
    if mime == "text/css":
        return "css"
    if mime.startswith("image/"):
        return "images"
    if mime == "application/pdf":
        return "pdf"
    if "shockwave" in mime or "x-java" in mime or "silverlight" in mime:
        return "plugins"
    if mime.startswith(("audio/", "video/")) or "mpegurl" in mime or mime in ("application/ogg", "application/dash+xml"):
        return "media"
    if mime.startswith("font/") or "font" in mime or "woff" in mime:
        return "fonts"
    if "xml" in mime:
        return "xml"
    return "other"


def kind_from_extension(url: str) -> str:
    try:
        return _EXT_KIND.get(os.path.splitext(urlparse(url).path.lower())[1], "")
    except Exception:
        return ""


def content_kind(r) -> str:
    """Exactly one bucket per URL, from the Content-Type the URL answered with (a 3xx or 4xx
    answer included, as Screaming Frog files them). A response without a Content-Type is "other"
    (as Screaming Frog files it); a URL that never answered (robots.txt, no response) is
    "unknown". An external link that was never checked has no Content-Type, so its bucket is
    guessed from the file extension and falls back to "unknown"."""
    cat = category_of(r)
    if cat in ("blocked", "error"):
        return "unknown"
    ct = (getattr(r, "content_type", "") or "") if is_external(r) else content_type_of(r)
    k = kind_from_content_type(ct)
    if k:
        return k
    if cat == "" and is_external(r):
        return kind_from_extension(getattr(r, "url", "")) or "unknown"
    return "other"


def is_html_doc(r) -> bool:
    """Internal URL answered with an HTML document (any status)."""
    return not is_external(r) and content_kind(r) == "html"


# ---------------------------------------------------------------------------------------------
# Links (cached per Site)
# ---------------------------------------------------------------------------------------------
def memo(site, key: str, build):
    """Compute something once per Site snapshot."""
    store = site.__dict__.setdefault("_tab_memo", {})
    if key not in store:
        store[key] = build()
    return store[key]


def _host(url: str) -> str:
    try:
        return clean_hostname(get_domain(url) or "")
    except Exception:
        return ""


def cdn_hosts(site) -> Tuple[str, ...]:
    """CrawlConfig.cdn_hosts of the crawl: hosts whose images and files count as the site's own (P11)."""
    hosts = getattr(getattr(site, "config", None), "cdn_hosts", None) or ()
    return tuple(h for h in (clean_hostname(str(x or "").strip().lower()) for x in hosts) if h)


def is_internal_url(site, url: str) -> bool:
    if url in site.by_url:
        return True
    base = clean_hostname(site.base_domain or get_domain(site.start_url) or "")
    h = _host(url)
    if not h:
        return False
    return h == base or any(h == c or h.endswith("." + c) for c in cdn_hosts(site))


def _link_index(site) -> Dict[str, Dict[str, Any]]:
    """url -> {"count": references, "sources": set(pages)} over every hyperlink AND every resource
    reference (img/script/css/iframe) on crawled pages, every CSS background image the resources do not list,
    and every HTTP redirect (the redirecting URL "links" to its target): the same entries the URL details'
    Inlinks, All Inlinks and the per-filter Inlinks exports list (exporter.source_index), so the counts agree.
    Each entry also counts the references by link position (P16): "pos" {content, navigation, footer, other} (the
    four add up to "count"; other = header, sidebar, breadcrumb, form, page body and every place that is not a page
    position: <head>, HTTP redirect, CSS background image) and "content_sources" (pages other than the URL itself
    that point at it from their content)."""
    from ..extract import position_of
    out: Dict[str, Dict[str, Any]] = {}

    def add(url: str, src: str, region: str = ""):
        if not url:
            return
        e = out.get(url)
        if e is None:
            e = out[url] = {"count": 0, "sources": set(), "pos": dict.fromkeys(POSITION_GROUPS, 0),
                            "content_sources": set()}
        e["count"] += 1
        e["sources"].add(src)
        group = position_group(region, position_of)
        e["pos"][group] += 1
        if group == "content" and src != url:
            e["content_sources"].add(src)

    for page in site.pages:
        p = seo(page)
        for link in p.get("links") or ():
            try:
                add(link.get("url") or "", page.url, link.get("region") or "")
            except Exception:
                continue
        listed = set()
        for res in p.get("resources") or ():
            try:
                add(res.get("url") or "", page.url, res.get("region") or "")
                listed.add(res.get("url") or "")
            except Exception:
                continue
        for bg in p.get("background_images") or ():   # type "Background Image" (exporter.background_links)
            if isinstance(bg, str) and bg and bg not in listed and not bg.startswith("data:"):
                listed.add(bg)
                add(bg, page.url)
    for r in (getattr(site, "by_url", None) or {}).values():   # type "HTTP Redirect" (exporter.redirect_links)
        dest = getattr(r, "redirect_destination", None)
        if dest and dest != "-" and category_of(r) == "3xx":
            try:
                add(normalize_url(dest) or dest, r.url)
            except Exception:
                continue
    # Pages crawled without PageData still have hyperlink inlinks from analysis
    for url, lst in (site.inlinks or {}).items():
        if url not in out:
            pos = dict.fromkeys(POSITION_GROUPS, 0)
            content = set()
            for i in lst:
                group = position_group(i.get("region") or "", position_of)
                pos[group] += 1
                if group == "content" and i.get("source") != url:
                    content.add(i.get("source"))
            out[url] = {"count": len(lst), "sources": {i.get("source") for i in lst}, "pos": pos,
                        "content_sources": content}
    return out


POSITION_GROUPS = ("content", "navigation", "footer", "other")   # the Links tab's inlinks-by-position columns


def position_group(region: str, position_of) -> str:
    """content / navigation / footer for those link positions, "other" for every other place."""
    pos = position_of(region)
    return pos if pos in ("content", "navigation", "footer") else "other"


def link_index(site) -> Dict[str, Dict[str, Any]]:
    return memo(site, "link_index", lambda: _link_index(site))


def _lookup(site, url: str) -> Dict[str, Any]:
    idx = link_index(site)
    e = idx.get(url)
    if e is None:
        try:
            e = idx.get(normalize_url(url) or "")
        except Exception:
            e = None
    return e or {"count": 0, "sources": set()}


def inlinks_count(site, r) -> int:
    """Links and resource references pointing at this URL from crawled pages."""
    return _lookup(site, getattr(r, "url", "")).get("count", 0)


def unique_inlinks_count(site, r) -> int:
    return len(_lookup(site, getattr(r, "url", "")).get("sources", ()))


def inlinks_by_position(site, r) -> Dict[str, int]:
    """{content, navigation, footer, other}: the URL's inlinks (inlinks_count) by the position of the link on the
    page that holds it (P16); the four add up to inlinks_count."""
    pos = _lookup(site, getattr(r, "url", "")).get("pos")
    return dict(pos) if isinstance(pos, dict) else dict.fromkeys(POSITION_GROUPS, 0)


def content_inlink_pages(site, r) -> int:
    """Pages other than the URL itself that link to it from their content (at most unique_inlinks_count)."""
    return len(_lookup(site, getattr(r, "url", "")).get("content_sources", ()))


def outlink_counts(site, r) -> Tuple[int, int, int]:
    """(internal outlinks, unique internal outlinks, external outlinks) of a page: hyperlinks
    (<a>/<area>) only, as Screaming Frog counts them. Resource references (img/script/css/iframe)
    are not outlinks (they still count as inlinks to the file)."""
    def build():
        internal, uniq, external = 0, set(), 0
        for link in seo(r).get("links") or ():
            u = link.get("url") or ""
            if not u:
                continue
            if link.get("internal"):
                internal += 1
                uniq.add(u)
            else:
                external += 1
        return internal, len(uniq), external
    return memo(site, "out:" + getattr(r, "url", ""), build)


# ---------------------------------------------------------------------------------------------
# Common cells
# ---------------------------------------------------------------------------------------------
def indexability_label(site, r) -> str:
    if is_external(r):
        cat = category_of(r)
        return "" if cat == "" else ("Indexable" if cat == "2xx" else "Non-Indexable")
    return "Indexable" if site.indexable.get(r.url) else "Non-Indexable"


_EXT_INDEX_STATUS = {"blocked": "Blocked by robots.txt", "error": "No Response", "3xx": "Redirected",
                     "4xx": "Client Error", "5xx": "Server Error"}


def indexability_status_label(site, r) -> str:
    if is_external(r):
        return _EXT_INDEX_STATUS.get(category_of(r), "")
    return site.indexability_status.get(r.url, "")


def meta_refresh_target(r) -> str:
    """Target of the URL's <meta http-equiv="refresh"> ("" when none, or it reloads the same URL). External links
    carry it from the external check (ExternalLink.meta_refresh_url; read from the first 64 KB of a 2xx HTML
    answer); internal URLs from their PageData."""
    if is_external(r):
        target = getattr(r, "meta_refresh_url", "") or ""
    else:
        target = seo(r).get("meta_refresh_url") or ""
    return target if target and target != r.url else ""


def redirect_url(r) -> str:
    """Where the URL sends the visitor: HTTP redirect target, else meta refresh / Refresh header target."""
    dest = getattr(r, "redirect_destination", None)
    if dest and dest != "-":
        return dest
    mr = meta_refresh_target(r)
    if mr:
        return mr
    return http_refresh_target(r)


_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)


def http_refresh_target(r) -> str:
    """Absolute target of a 'Refresh: 0; url=...' response header ('' if none or same URL)."""
    value = header(r, "refresh")
    m = _REFRESH_URL.search(value or "")
    if not m:
        return ""
    try:
        target = normalize_url(urljoin(r.url, m.group(1).strip())) or ""
    except Exception:
        return ""
    return target if target and target != r.url else ""


def redirect_type(r) -> str:
    if category_of(r) == "3xx":
        return "HTTP Redirect"
    if meta_refresh_target(r):
        return "Meta Refresh"
    if http_refresh_target(r):
        return "HTTP Refresh"
    return ""


JS_REDIRECT = "JavaScript Redirect"


def redirect_hop(r) -> Tuple[str, str]:
    """(normalized target, kind) of the one hop a URL makes when it sends the visitor elsewhere: an HTTP redirect
    (3xx), a meta refresh, a Refresh header (redirect_type) or, for an internal page rendered in Chrome, a
    JavaScript redirect; ("", "") when it makes none. Redirect chains and loops walk these hops (P4)."""
    kind = redirect_type(r)
    if kind:
        dest = redirect_url(r)
        try:
            return (normalize_url(dest) or dest or ""), kind
        except Exception:
            return dest or "", kind
    if not is_external(r):
        rd = rendered(r)
        if rd and rd.get("js_redirect") and rd.get("js_redirect_url"):
            target = str(rd.get("js_redirect_url") or "")
            try:
                target = normalize_url(target) or target
            except Exception:
                pass
            if target and target != r.url:
                return target, JS_REDIRECT
    return "", ""


def size_bytes(r) -> Any:
    p = seo(r)
    if p.get("size_bytes"):
        return p["size_bytes"]
    cl = header(r, "content-length")
    return int(cl) if cl.strip().isdigit() else ""


def last_modified(r) -> str:
    return seo(r).get("last_modified") or header(r, "last-modified")


def canonical_of(r) -> str:
    p = seo(r)
    return first(p.get("canonicals") or []) or p.get("http_canonical") or ""


ADDRESS = Column("address", "Address", lambda r, site: r.url, "url")
CONTENT_TYPE = Column("content_type", "Content Type", lambda r, site: content_type_of(r))
STATUS_CODE = Column("status_code", "Status Code", lambda r, site: first_status_code(r), "status")
STATUS = Column("status", "Status", lambda r, site: status_text(r))
INDEXABILITY = Column("indexability", "Indexability", lambda r, site: indexability_label(site, r))
INDEXABILITY_STATUS = Column("indexability_status", "Indexability Status",
                             lambda r, site: indexability_status_label(site, r))
INLINKS = Column("inlinks", "Inlinks", lambda r, site: inlinks_count(site, r), "num")
RESPONSE_TIME = Column("response_time", "Response Time (s)", lambda r, site: response_time_s(r), "num")
REDIRECT_URL = Column("redirect_url", "Redirect URL", lambda r, site: redirect_url(r), "url")
REDIRECT_TYPE = Column("redirect_type", "Redirect Type", lambda r, site: redirect_type(r))


def _html(fn):
    """Cell only for HTML documents (files show blank, not 0)."""
    return lambda r, site: fn(r, site) if is_html_doc(r) else ""


def _title_1(r) -> str:
    return first(seo(r).get("titles") or [])


def _titled(fn):
    """Cell for HTML documents and for files that carry a title (a PDF's document title)."""
    return lambda r, site: fn(r, site) if is_html_doc(r) or _title_1(r) else ""


def _pdf(r) -> dict:
    p = seo(r).get("pdf")
    return p if isinstance(p, dict) else {}


COLUMNS = [
    ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS, INDEXABILITY, INDEXABILITY_STATUS,
    Column("title_1", "Title 1", lambda r, site: first(seo(r).get("titles") or [])),
    Column("title_1_length", "Title 1 Length", _titled(lambda r, site: len(_title_1(r))), "num"),
    Column("title_1_pixel_width", "Title 1 Pixel Width", _titled(lambda r, site: pixel_width(_title_1(r), "title")), "num"),
    Column("meta_description_1", "Meta Description 1", lambda r, site: first(seo(r).get("meta_descriptions") or [])),
    Column("meta_description_1_length", "Meta Description 1 Length",
           _html(lambda r, site: len(first(seo(r).get("meta_descriptions") or []))), "num"),
    Column("h1_1", "H1-1", lambda r, site: first(seo(r).get("h1") or [])),
    Column("meta_robots_1", "Meta Robots 1", lambda r, site: first(seo(r).get("meta_robots") or [])),
    Column("x_robots_tag_1", "X-Robots-Tag 1", lambda r, site: first(seo(r).get("x_robots_tag") or [])),
    Column("canonical", "Canonical Link Element 1", lambda r, site: canonical_of(r), "url"),
    Column("size", "Size (Bytes)", lambda r, site: size_bytes(r), "num"),
    Column("word_count", "Word Count", _html(lambda r, site: seo(r).get("word_count") or 0), "num"),
    Column("text_ratio", "Text Ratio", _html(lambda r, site: round(seo(r).get("text_ratio") or 0.0, 3)), "num"),
    Column("crawl_depth", "Crawl Depth", lambda r, site: site.crawl_depth.get(r.url, ""), "num"),
    Column("link_score", "Link Score", lambda r, site: site.link_score.get(r.url, ""), "num"),
    INLINKS,
    Column("unique_inlinks", "Unique Inlinks", lambda r, site: unique_inlinks_count(site, r), "num"),
    Column("outlinks", "Outlinks", _html(lambda r, site: outlink_counts(site, r)[0]), "num"),
    Column("unique_outlinks", "Unique Outlinks", _html(lambda r, site: outlink_counts(site, r)[1]), "num"),
    Column("external_outlinks", "External Outlinks", _html(lambda r, site: outlink_counts(site, r)[2]), "num"),
    RESPONSE_TIME,
    Column("last_modified", "Last Modified", lambda r, site: last_modified(r)),
    REDIRECT_URL, REDIRECT_TYPE,
    Column("language", "Language", lambda r, site: seo(r).get("language") or ""),
    Column("http_version", "HTTP Version", lambda r, site: seo(r).get("http_version") or ""),
    # P21: PDF properties (pypdf), blank on other rows; the reason when a PDF could not be read
    Column("pdf_pages", "PDF Pages", lambda r, site: _pdf(r).get("pages") if _pdf(r).get("pages") is not None
           else "", "num"),
    Column("pdf_words", "PDF Word Count", lambda r, site: _pdf(r).get("words") if _pdf(r).get("words") is not None
           else "", "num"),
    Column("pdf_author", "PDF Author", lambda r, site: _pdf(r).get("author") or ""),
    Column("pdf_note", "PDF Not Read", lambda r, site: "" if _pdf(r).get("read") else _pdf(r).get("reason") or ""),
]

_KIND_HELP = {
    "html": "Web pages (HTML documents), including error pages that return HTML.",
    "javascript": "JavaScript files loaded by your pages.",
    "css": "Stylesheets loaded by your pages.",
    "images": "Image files (Content-Type image/...).",
    "media": "Audio and video files.",
    "fonts": "Web font files.",
    "xml": "XML files such as sitemaps and feeds.",
    "pdf": "PDF documents.",
    "plugins": "Browser-plugin files such as Flash (.swf) or Java applets.",
    "other": "Anything else that answered, including responses that sent no Content-Type header.",
    "unknown": "URLs that never answered, so their type is unknown: blocked by robots.txt or no response.",
}


def _kind_filter(kind: str, name: str) -> Filter:
    return Filter(kind, name, lambda r, site, k=kind: content_kind(r) == k, description=_KIND_HELP[kind])


register(Tab(
    id="internal", name="Internal", order=10, columns=COLUMNS,
    filters=[_kind_filter(k, n) for k, n in CONTENT_KINDS],
    rows=internal_rows,
))
