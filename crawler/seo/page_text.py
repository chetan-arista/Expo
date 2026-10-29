"""
The HTML and readable text of one crawled page, for features that read pages after the crawl (AI tab prompts,
n-grams, semantic search, LanguageTool, the All Page Text export). PLAN4_build.md 10.3:

  page_html(crawler, url, max_chars=500_000, allow_fetch=False) -> (html, "stored" | "fetched" | "")
  page_text(crawler, url, content_area=True, max_chars=20_000, allow_fetch=False) -> (text, source)

The HTML comes from the crawl's View Source store (the rendered HTML when the page's values were read from its
render, as in a render-every-page crawl); only when the caller allows it, and only for a page of the crawled
site, is the page fetched once more with the crawl's own session (size-capped, never following redirects to
another site). "" as the source means no HTML is available: the caller reports "Not checked" with its reason.

Text: content_area=True gives the content area the word count reads (the <body> without nav, footer, scripts
and other hidden parts), narrowed by CrawlConfig.content_area (include / exclude CSS selectors) when set;
content_area=False gives every visible text of the <body>. Lines keep their block boundaries ("\\n"), so a
reader sees sentences and headings apart.
"""
from typing import Any, List, Optional, Tuple

MAX_FETCH_BYTES = 3 * 1024 * 1024     # a fetched page is read up to this size
MAX_TEXT_HTML = 2_000_000             # HTML characters parsed for text


def _rendered_store(crawler):
    """The store holding the rendered HTML beside the raw HTML (saved crawl or live crawl), or None."""
    store = getattr(crawler, "source_store", None)
    if store is None:
        return None
    fn = getattr(store, "rendered", None)
    if callable(fn):
        try:
            return fn()
        except Exception:
            return None
    base = getattr(store, "dir", "") or ""
    if not base:
        return None
    import os
    path = os.path.join(base, "rendered")
    if not os.path.isdir(path):
        return None
    try:
        from ..source_store import SourceStore
        return SourceStore.open(path)
    except Exception:
        return None


def _result(crawler, url: str):
    results = getattr(crawler, "results", None)
    if isinstance(results, dict):
        return results.get(url)
    for r in getattr(crawler, "results_list", None) or ():
        if getattr(r, "url", "") == url:
            return r
    return None


def _promoted(r) -> bool:
    """The page's tab values were read from its rendered HTML (render every page in Chrome)."""
    seo = getattr(r, "seo", None) or {}
    return isinstance(seo.get("original_html"), dict)


def _stored(crawler, url: str) -> Optional[str]:
    r = _result(crawler, url)
    if r is not None and _promoted(r):
        rstore = _rendered_store(crawler)
        if rstore is not None:
            try:
                html = rstore.get(url)
            except Exception:
                html = None
            if html:
                return html
    store = getattr(crawler, "source_store", None)
    if store is None:
        return None
    try:
        return store.get(url)
    except Exception:
        return None


def _fetch(crawler, url: str) -> Optional[str]:
    """One GET of a page of the crawled site with the crawl's session (no redirects, size-capped)."""
    if not str(url or "").lower().startswith(("http://", "https://")):
        return None
    on_site = getattr(crawler, "on_site", None)
    try:
        if callable(on_site) and not on_site(url):
            return None
    except Exception:
        return None
    if not callable(on_site) and _result(crawler, url) is None:
        return None
    session = getattr(crawler, "session", None)
    if session is None:
        return None
    cfg = getattr(crawler, "config", None)
    timeout = float(getattr(cfg, "timeout", 15) or 15)
    try:
        with session.get(url, timeout=(min(timeout, 10.0), timeout), allow_redirects=False, stream=True,
                         verify=bool(getattr(cfg, "verify_ssl", False))) as resp:
            if not 200 <= resp.status_code < 300 or "html" not in (resp.headers.get("content-type") or "").lower():
                return None
            data = bytearray()
            for chunk in resp.iter_content(65536):
                data += chunk
                if len(data) >= MAX_FETCH_BYTES:
                    break
            enc = resp.encoding or "utf-8"
        try:
            return bytes(data[:MAX_FETCH_BYTES]).decode(enc, errors="replace")
        except LookupError:
            return bytes(data[:MAX_FETCH_BYTES]).decode("utf-8", errors="replace")
    except Exception:
        return None


def page_html(crawler, url: str, max_chars: int = 500_000, allow_fetch: bool = False) -> Tuple[str, str]:
    """(HTML, "stored" | "fetched" | "") of one crawled page; ("", "") when none is available."""
    if crawler is None or not url:
        return "", ""
    html = _stored(crawler, url)
    if html:
        return html[:max_chars] if max_chars else html, "stored"
    if allow_fetch:
        html = _fetch(crawler, url)
        if html:
            return html[:max_chars] if max_chars else html, "fetched"
    return "", ""


def _area_selectors(crawler) -> Tuple[List[str], List[str]]:
    area = getattr(getattr(crawler, "config", None), "content_area", None)
    if not isinstance(area, dict):
        return [], []
    return list(area.get("include") or []), list(area.get("exclude") or [])


def html_text(html: str, content_area: bool = True, include: Optional[List[str]] = None,
              exclude: Optional[List[str]] = None) -> str:
    """Readable text of an HTML document, one line per block (see the module docstring). The content area is read
    exactly as the crawl's word count reads it (extract.content_area / area_lines: the included elements, or the
    whole <body> when none of them is on the page; excluded elements, nav and footer left out)."""
    from .extract import _visible_strings, area_lines, clean_text, compile_selectors, parse_html
    from .extract import content_area as area_of
    soup = parse_html((html or "")[:MAX_TEXT_HTML])
    try:
        body = soup.find("body") or soup
        if not content_area:
            parts = [clean_text(t) for t, _ in _visible_strings(body)]
            return "\n".join(p for p in parts if p)
        inc = compile_selectors(include)[0]          # an invalid selector is skipped (config validation reports it)
        exc = compile_selectors(exclude)[0]
        return "\n".join(area_lines(body, area_of(body, inc, exc))[0])
    finally:
        try:
            from ..page_parser import release_soup
            release_soup(soup)
        except Exception:
            pass


def page_text(crawler, url: str, content_area: bool = True, max_chars: int = 20_000,
              allow_fetch: bool = False) -> Tuple[str, str]:
    """(text, "stored" | "fetched" | "") of one crawled page; ("", "") when its HTML is not available."""
    html, source = page_html(crawler, url, max_chars=MAX_TEXT_HTML, allow_fetch=allow_fetch)
    if not html:
        return "", ""
    include, exclude = _area_selectors(crawler) if content_area else ([], [])
    text = html_text(html, content_area, include, exclude)
    return (text[:max_chars] if max_chars else text), source
