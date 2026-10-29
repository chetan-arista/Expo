"""
XML sitemap and image sitemap generator (Sitemaps > XML Sitemap / Images Sitemap), built from a finished
crawl. The options follow Screaming Frog's "XML Sitemap Export Configuration" dialog, with its defaults:
only internal HTML pages that answered 2xx and are indexable go in; noindex, canonicalised and paginated
pages, PDFs and 3xx / 4xx / 5xx URLs can be added one by one.

    name, data, mime = build_sitemap(site, crawler, kind="xml"|"images", options)
    info = preview(site, kind, options)      # how many URLs / files, and why the others were left out

Output follows sitemaps.org: UTF-8, URLs escaped, at most 50,000 URLs and 50 MB (uncompressed) per file. A
bigger sitemap is split into sitemap_1.xml, sitemap_2.xml ... plus a sitemap_index.xml listing them, sent
as one .zip; the index needs the address the files will live at (options["base_url"], default the site's
root). lastmod comes from each page's Last-Modified header (left out for a page that sent none), a fixed
date, or nothing; priority and changefreq are off by default and can follow the crawl depth.
Images use Google's image extension (image:image / image:loc; caption and title are no longer read by
Google, so they are not written); hreflang alternates use xhtml:link.
"""
import io
import re
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit
from xml.sax.saxutils import escape

SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
IMAGE_NS = "http://www.google.com/schemas/sitemap-image/1.1"
XHTML_NS = "http://www.w3.org/1999/xhtml"
MAX_URLS = 50_000                       # sitemaps.org: URLs per file
MAX_BYTES = 50 * 1024 * 1024            # sitemaps.org: 50 MB uncompressed per file
MAX_LOC = 2048                          # sitemaps.org: characters in a <loc>
MAX_IMAGES_PER_PAGE = 1000              # Google: image:image entries per <url>
XML_MIME = "application/xml"
ZIP_MIME = "application/zip"
CHANGEFREQS = ("always", "hourly", "daily", "weekly", "monthly", "yearly", "never")

DEFAULT_PRIORITY = {0: "1.0", 1: "0.9", 2: "0.8", 3: "0.7", 4: "0.6"}    # crawl depth -> priority (5+ = 0.5)
DEFAULT_CHANGEFREQ = {0: "daily", 1: "daily", 2: "weekly", 3: "monthly", 4: "monthly"}   # 5+ = yearly

DEFAULT_OPTIONS: Dict[str, Any] = {
    # Pages
    "include_noindex": False,           # pages with a noindex directive
    "include_canonicalised": False,     # pages whose canonical points to another URL
    "include_paginated": False,         # page 2+ of a series (the page has rel="prev")
    "include_pdfs": False,              # PDF files that answered 2xx
    "include_3xx": False, "include_4xx": False, "include_5xx": False,   # non-200 URLs (off: they hurt)
    # lastmod / priority / changefreq
    "lastmod": "server",                # server (Last-Modified header) | custom | none
    "lastmod_date": "",                 # YYYY-MM-DD for "custom" (default today)
    "priority": "none",                 # none | depth
    "changefreq": "none",               # none | depth | last_modified
    # Images (kind "images", or images inside the XML sitemap)
    "images": False,                    # add image:image entries to the XML sitemap
    "image_hosts": "internal",          # internal (the crawled site + cdn_hosts) | all
    "cdn_hosts": [],                    # extra image hosts counted as the site's own (CDNs)
    "image_max_pages": 10,              # leave out images used on more pages than this (logos, icons); 0 = no cap
    "exclude_images_without_alt": False,
    "include_noindex_images": False,    # images whose own answer carried X-Robots-Tag: noindex
    # hreflang
    "hreflang": False,                  # add xhtml:link alternates from the pages' hreflang annotations
    "hreflang_valid_only": True,        # leave out alternates that fail the Hreflang checks (bad code, non-200,
                                        # noindex, canonicalised elsewhere, no return link)
    # Files
    "max_urls_per_file": MAX_URLS,      # split threshold (never above 50,000)
    "base_url": "",                     # where the split files will be published (sitemap index <loc>)
}


class SitemapEmpty(ValueError):
    """No URL matches the options: sitemaps.org requires at least one <url> per file."""


def merged_options(options: Optional[dict]) -> Dict[str, Any]:
    """DEFAULT_OPTIONS overlaid with the known keys of `options` (unknown keys are ignored)."""
    out = dict(DEFAULT_OPTIONS)
    for k, v in (options or {}).items():
        if k in DEFAULT_OPTIONS and v is not None:
            out[k] = v
    try:
        out["max_urls_per_file"] = max(1, min(MAX_URLS, int(out["max_urls_per_file"] or MAX_URLS)))
    except (TypeError, ValueError):
        out["max_urls_per_file"] = MAX_URLS
    try:
        out["image_max_pages"] = max(0, int(out["image_max_pages"] or 0))
    except (TypeError, ValueError):
        out["image_max_pages"] = DEFAULT_OPTIONS["image_max_pages"]
    hosts = out.get("cdn_hosts") or []
    if isinstance(hosts, str):
        hosts = re.split(r"[\s,]+", hosts)
    out["cdn_hosts"] = [h.strip().lower().lstrip(".") for h in hosts if h and h.strip()]
    return out


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def _seo(r) -> dict:
    return getattr(r, "seo", None) or {}


def _category(r) -> str:
    from .exporter import row_category
    return row_category(r) or "error"


def _kind(r) -> str:
    try:
        from .seo.tabs.internal import content_kind
        return content_kind(r)
    except Exception:
        return "html" if _seo(r).get("is_html") else ""


def loc(url: str) -> str:
    """A URL as sitemaps.org wants it: non-ASCII characters and spaces percent-encoded (existing %XX kept),
    then XML-escaped (& ' " < >)."""
    safe = quote(url.strip(), safe=":/?#[]@!$&'()*+,;=%~")
    return escape(safe, {"'": "&apos;", '"': "&quot;"})


def _w3c_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).date().isoformat() if dt.tzinfo else dt.date().isoformat()


def last_modified(r) -> Optional[datetime]:
    """The page's Last-Modified header as a datetime (None when absent or unreadable)."""
    raw = _seo(r).get("last_modified") or (getattr(r, "response_headers", None) or {}).get("last-modified", "")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _custom_date(opts: dict) -> str:
    text = str(opts.get("lastmod_date") or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            datetime.strptime(text, "%Y-%m-%d")
            return text
        except ValueError:
            pass
    return time.strftime("%Y-%m-%d", time.gmtime())


def _depth(site, r) -> int:
    try:
        return int((getattr(site, "crawl_depth", None) or {}).get(r.url, getattr(r, "depth", 0)) or 0)
    except (TypeError, ValueError):
        return 0


def _changefreq_from_age(dt: Optional[datetime], now: datetime) -> str:
    if dt is None:
        return ""
    days = (now - dt).total_seconds() / 86400.0
    if days <= 1:
        return "daily"
    if days <= 7:
        return "weekly"
    if days <= 31:
        return "monthly"
    return "yearly"


# ---------------------------------------------------------------------------------------------
# Which URLs go in
# ---------------------------------------------------------------------------------------------
def _paginated(r) -> bool:
    p = _seo(r)
    return bool(p.get("rel_prev") or p.get("http_rel_prev") or p.get("rel_prev_all"))


def select_pages(site, options: Optional[dict] = None) -> Tuple[List[Any], Dict[str, int]]:
    """(results that go in the sitemap, {reason left out: count}) in crawl order. Every internal URL is
    either selected or counted under one reason, so selected + sum(excluded) == internal URLs."""
    opts = merged_options(options)
    picked: List[Any] = []
    excluded: Dict[str, int] = {}

    def skip(reason: str):
        excluded[reason] = excluded.get(reason, 0) + 1

    status = getattr(site, "indexability_status", None) or {}
    for r in (getattr(site, "by_url", None) or {}).values():
        url = getattr(r, "url", "") or ""
        cat = _category(r)
        kind = _kind(r)
        if not url.lower().startswith(("http://", "https://")):
            skip("Not an http(s) URL")
            continue
        if len(loc(url)) > MAX_LOC:
            skip(f"URL over {MAX_LOC} characters")
            continue
        if cat in ("error", "blocked"):
            skip("Blocked by robots.txt" if cat == "blocked" else "No response")
            continue
        if cat in ("3xx", "4xx", "5xx"):
            if opts.get(f"include_{cat}"):
                picked.append(r)
            else:
                skip(f"{cat} answer")
            continue
        if kind == "pdf":
            if not opts["include_pdfs"]:
                skip("PDF")
                continue
        elif kind != "html":
            skip("Not an HTML page (image, script, stylesheet or other file)")
            continue
        why = status.get(url, "")
        if why == "noindex" and not opts["include_noindex"]:
            skip("Noindex")
            continue
        if why == "Canonicalised" and not opts["include_canonicalised"]:
            skip("Canonicalised")
            continue
        if why not in ("", "noindex", "Canonicalised"):
            skip(f"Non-indexable ({why})")
            continue
        if kind == "html" and _paginated(r) and not opts["include_paginated"]:
            skip("Paginated (page 2+)")
            continue
        picked.append(r)
    return picked, excluded


def _site_hosts(site) -> set:
    return {urlsplit(u).netloc.lower() for u in (getattr(site, "by_url", None) or {})}


def _host_ok(host: str, own: set, cdns: List[str]) -> bool:
    host = host.lower()
    return host in own or any(host == c or host.endswith("." + c) for c in cdns)


def _image_pages(site) -> Dict[str, int]:
    """image src -> number of distinct crawled pages that show it."""
    c: Dict[str, set] = {}
    for p in getattr(site, "pages", None) or ():
        for img in _seo(p).get("images") or ():
            src = img.get("src") or ""
            if src:
                c.setdefault(src, set()).add(p.url)
    return {k: len(v) for k, v in c.items()}


def _image_noindex(r) -> bool:
    """The image's own answer told Google not to index it (X-Robots-Tag noindex / none, one header line at a
    time; a line aimed at another crawler, "bingbot: noindex", does not count)."""
    from .seo.extract import directive_tokens
    lines = _seo(r).get("x_robots_tag") or []
    if not lines:
        xr = (getattr(r, "response_headers", None) or {}).get("x-robots-tag", "") or ""
        lines = [str(xr)] if xr else []
    d = directive_tokens(lines)
    return "noindex" in d or "none" in d


def page_images(site, r, opts: dict, uses: Dict[str, int], own_hosts: set,
                why: Optional[Dict[str, set]] = None) -> List[str]:
    """The images of one page that qualify for an image sitemap, in page order (max 1,000). `why`, when
    given, collects {reason left out: {image urls}}."""
    from .exporter import externals_by_url
    ext = externals_by_url(site)
    out: List[str] = []
    seen = set()

    def skip(reason: str, src: str):
        if why is not None:
            why.setdefault(reason, set()).add(src)

    from .seo.tabs.images import primary_images
    for img in primary_images(_seo(r).get("images") or ()):   # one file per <img>: srcset sizes are not listed
        src = (img.get("src") or "").strip()
        if not src or src in seen or not src.lower().startswith(("http://", "https://")):
            continue
        seen.add(src)
        if len(loc(src)) > MAX_LOC:
            skip(f"address over {MAX_LOC} characters", src)
            continue
        if opts["image_hosts"] != "all" and not _host_ok(urlsplit(src).netloc, own_hosts, opts["cdn_hosts"]):
            skip("hosted on another site (add its host as a CDN)", src)
            continue
        if opts["exclude_images_without_alt"] and not str(img.get("alt") or "").strip():
            skip("no alt text", src)
            continue
        if opts["image_max_pages"] and uses.get(src, 0) > opts["image_max_pages"]:
            skip(f"used on more than {opts['image_max_pages']} pages (logo / icon)", src)
            continue
        target = site.by_url.get(src) or ext.get(src)
        if target is not None:
            cat = _category(target)
            if cat and cat != "2xx":
                skip("broken or redirected", src)
                continue                 # a broken or redirected image does not belong in a sitemap
            if not opts["include_noindex_images"] and _image_noindex(target):
                skip("noindex (X-Robots-Tag)", src)
                continue
        out.append(src)
        if len(out) >= MAX_IMAGES_PER_PAGE:
            break
    return out


def _alternates(r, site=None) -> List[Tuple[str, str]]:
    """(code, URL) of the page's hreflang annotations: HTML, Link header and (with the site) the XML sitemaps (P8)."""
    out: List[Tuple[str, str]] = []
    seen = set()
    p = _seo(r)
    listed = []
    if site is not None:
        from .seo.tabs.hreflang import sitemap_entries
        listed = sitemap_entries(r, site)
    for e in list(p.get("hreflang") or []) + list(p.get("http_hreflang") or []) + list(listed):
        lang = str(e.get("lang") or "").strip()
        url = str(e.get("url") or "").strip()
        if not lang or not url.lower().startswith(("http://", "https://")):
            continue
        key = (lang.lower(), url)
        if key not in seen:
            seen.add(key)
            out.append((lang, url))
    return out


def alternate_problem(site, r, lang: str, url: str) -> str:
    """Why an hreflang alternate of page r fails the Hreflang tab's checks ("" when it passes or could not be
    judged): an invalid language / region code, a URL that does not answer 200, a noindex or canonicalised
    page, or an alternate page that does not link back to r."""
    from .seo.tabs import hreflang as hl
    from .seo.tabs.canonicals import find_result, has_noindex, same_url, target_non_200
    from .seo.tabs.internal import is_external
    if hl.code_problem(lang.lower()):
        return "invalid language / region code"
    if same_url(url, r.url) or same_url(url, _seo(r).get("final_url") or ""):
        return ""
    if target_non_200(site, url):
        return "does not answer 200"
    if has_noindex(site, url):
        return "noindex"
    t = find_result(site, url)
    if t is not None and not is_external(t) and \
            (getattr(site, "indexability_status", None) or {}).get(t.url) == "Canonicalised":
        return "canonicalised to another URL"
    page = hl._alt_page(site, url)
    if page is not None and not any(hl._points_to(x, r) for x in hl.entries(page, site)):
        return "no return link"
    return ""


def page_alternates(site, r, opts: dict, why: Optional[Dict[str, int]] = None) -> List[Tuple[str, str]]:
    """The hreflang alternates written for page r: all of them, or (hreflang_valid_only) those that pass the
    Hreflang checks. `why` counts the left-out ones by reason."""
    out = []
    for lang, url in _alternates(r, site):
        if opts.get("hreflang_valid_only"):
            try:
                problem = alternate_problem(site, r, lang, url)
            except Exception:
                problem = ""
            if problem:
                if why is not None:
                    why[problem] = why.get(problem, 0) + 1
                continue
        out.append((lang, url))
    return out


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------
def _entry(site, r, kind: str, opts: dict, now: datetime, uses: Dict[str, int], own_hosts: set) -> Optional[str]:
    """One <url> element, or None when an image sitemap page has no qualifying image."""
    images = page_images(site, r, opts, uses, own_hosts) if (kind == "images" or opts["images"]) else []
    if kind == "images" and not images:
        return None
    parts = [f"<url><loc>{loc(r.url)}</loc>"]
    if kind == "xml":
        lm = None
        if opts["lastmod"] == "server":
            dt = last_modified(r)
            lm = _w3c_date(dt) if dt else None
        elif opts["lastmod"] == "custom":
            lm = _custom_date(opts)
        if lm:
            parts.append(f"<lastmod>{lm}</lastmod>")
        cf = ""
        if opts["changefreq"] == "depth":
            cf = DEFAULT_CHANGEFREQ.get(_depth(site, r), "yearly")
        elif opts["changefreq"] == "last_modified":
            cf = _changefreq_from_age(last_modified(r), now)
        if cf in CHANGEFREQS:
            parts.append(f"<changefreq>{cf}</changefreq>")
        if opts["priority"] == "depth":
            parts.append(f"<priority>{DEFAULT_PRIORITY.get(_depth(site, r), '0.5')}</priority>")
        if opts["hreflang"]:
            for lang, url in page_alternates(site, r, opts):
                parts.append(f'<xhtml:link rel="alternate" hreflang="{escape(lang, {chr(34): "&quot;"})}" '
                             f'href="{loc(url)}"/>')
    for src in images:
        parts.append(f"<image:image><image:loc>{loc(src)}</image:loc></image:image>")
    parts.append("</url>")
    return "".join(parts)


def _urlset_open(kind: str, opts: dict) -> str:
    ns = [f'xmlns="{SITEMAP_NS}"']
    if kind == "images" or opts["images"]:
        ns.append(f'xmlns:image="{IMAGE_NS}"')
    if kind == "xml" and opts["hreflang"]:
        ns.append(f'xmlns:xhtml="{XHTML_NS}"')
    return '<?xml version="1.0" encoding="UTF-8"?>\n<urlset ' + " ".join(ns) + ">\n"


_CLOSE = "</urlset>\n"


def _split(entries: List[str], head: str, max_urls: int) -> List[bytes]:
    """Files of at most max_urls entries and MAX_BYTES bytes each."""
    files: List[bytes] = []
    head_b, close_b = head.encode("utf-8"), _CLOSE.encode("utf-8")
    cur: List[bytes] = []
    size = len(head_b) + len(close_b)
    for e in entries:
        b = (e + "\n").encode("utf-8")
        if cur and (len(cur) >= max_urls or size + len(b) > MAX_BYTES):
            files.append(head_b + b"".join(cur) + close_b)
            cur, size = [], len(head_b) + len(close_b)
        cur.append(b)
        size += len(b)
    if cur:
        files.append(head_b + b"".join(cur) + close_b)
    return files


def _base_url(site, opts: dict) -> str:
    base = str(opts.get("base_url") or "").strip()
    if not base:
        start = getattr(site, "start_url", "") or next(iter(getattr(site, "by_url", None) or {}), "")
        p = urlsplit(start)
        base = f"{p.scheme}://{p.netloc}/" if p.netloc else ""
    return base if base.endswith("/") else base + "/"


def sitemap_files(site, kind: str = "xml", options: Optional[dict] = None) -> List[Tuple[str, bytes]]:
    """[(file name, xml bytes)]: one sitemap, or the split files followed by the sitemap index.
    Raises SitemapEmpty when nothing qualifies, ValueError for an unknown kind."""
    if kind not in ("xml", "images"):
        raise ValueError("kind must be 'xml' or 'images'")
    opts = merged_options(options)
    pages, excluded = select_pages(site, opts)
    now = datetime.now(timezone.utc)
    uses = _image_pages(site) if (kind == "images" or opts["images"]) else {}
    own = _site_hosts(site)
    entries = [e for e in (_entry(site, r, kind, opts, now, uses, own) for r in pages) if e]
    if not entries:
        if kind == "images" and pages:
            why: Dict[str, set] = {}
            for r in pages:
                page_images(site, r, opts, uses, own, why)
            detail = ", ".join(f"{len(v):,} {k}" for k, v in sorted(why.items(), key=lambda kv: -len(kv[1])))
            raise SitemapEmpty(f"None of the {len(pages):,} pages that qualify shows an image that can go in an "
                               "image sitemap" + (f" (images left out: {detail})." if detail else
                                                  ": they have no images."))
        what = "page with a qualifying image" if kind == "images" else "URL"
        raise SitemapEmpty(f"No {what} matches the sitemap options (left out: "
                           + (", ".join(f"{k} {v:,}" for k, v in sorted(excluded.items())) or "nothing crawled")
                           + ").")
    stem = "sitemap" if kind == "xml" else "image-sitemap"
    chunks = _split(entries, _urlset_open(kind, opts), opts["max_urls_per_file"])
    if len(chunks) == 1:
        return [(f"{stem}.xml", chunks[0])]
    names = [f"{stem}_{i}.xml" for i in range(1, len(chunks) + 1)]
    base = _base_url(site, opts)
    today = now.date().isoformat()
    index = ['<?xml version="1.0" encoding="UTF-8"?>\n', f'<sitemapindex xmlns="{SITEMAP_NS}">\n']
    for n in names:
        index.append(f"<sitemap><loc>{loc(base + n)}</loc><lastmod>{today}</lastmod></sitemap>\n")
    index.append("</sitemapindex>\n")
    return list(zip(names, chunks)) + [(f"{stem}_index.xml", "".join(index).encode("utf-8"))]


def preview(site, kind: str = "xml", options: Optional[dict] = None) -> dict:
    """What a sitemap with these options holds, for the dialog: {"urls", "files", "excluded",
    "images", "options"} (files = number of sitemap files, not counting the index)."""
    opts = merged_options(options)
    pages, excluded = select_pages(site, opts)
    images = 0
    with_images = 0
    if kind == "images" or opts["images"]:
        uses = _image_pages(site)
        own = _site_hosts(site)
        for r in pages:
            n = len(page_images(site, r, opts, uses, own))
            images += n
            with_images += 1 if n else 0
    urls = with_images if kind == "images" else len(pages)
    files = max(1, -(-urls // opts["max_urls_per_file"])) if urls else 0
    if kind == "images":
        excluded = dict(excluded)
        if len(pages) - with_images:
            excluded["No qualifying image"] = len(pages) - with_images
    out = {"kind": kind, "urls": urls, "files": files, "images": images, "excluded": excluded, "options": opts}
    if kind == "xml" and opts["hreflang"]:
        left_out: Dict[str, int] = {}
        out["alternates"] = sum(len(page_alternates(site, r, opts, left_out)) for r in pages)
        out["alternates_left_out"] = left_out      # {reason: count} (hreflang_valid_only)
    return out


def build_sitemap(site, crawler=None, kind: str = "xml", options: Optional[dict] = None) -> Tuple[str, bytes, str]:
    """(file name, bytes, media type): sitemap.xml / image-sitemap.xml, or a .zip with the split files and
    the sitemap index when there are more than 50,000 URLs or 50 MB. `crawler` is accepted for the API's
    uniform call shape; everything comes from the Site."""
    files = sitemap_files(site, kind, options)
    if len(files) == 1:
        return files[0][0], files[0][1], XML_MIME
    host = urlsplit(_base_url(site, merged_options(options))).netloc or "site"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files:
            zf.writestr(name, data)
    stem = "sitemaps" if kind == "xml" else "image-sitemaps"
    return f"{re.sub(r'[^A-Za-z0-9.-]+', '_', host)}_{stem}.zip", buf.getvalue(), ZIP_MIME
