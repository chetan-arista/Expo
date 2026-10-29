"""
URL-trap probe: after the crawl, request two addresses that cannot exist and look at the answer.

  root    <site>/<random>/              should be 404/410. A 200 means the server never says "not found":
                                         every mistyped or invented URL becomes another indexable page
                                         (a soft-404 server).
  nested  <real page>/<random>/         a 200 that serves the same page again is worse: its relative links
                                         now resolve one level deeper, so crawlers find an endless URL space.

A redirect of unknown URLs to the home page is reported too (Google treats that as a soft 404).
run(crawler) returns the report stored as crawler.trap_probe; tabs/hygiene.py shows failing probes as rows.

Idea from open-seo-crawler's URL-trap probe (MIT, see THIRD_PARTY_NOTICES.md).
"""
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

from ..url_utils import normalize_url
from .postcrawl import now_iso, status_report
from .robots_audit import site_origin

MAX_BODY = 2 * 1024 * 1024
SAME_PAGE_SIMILARITY = 90.0        # simhash similarity (%) at which the nested probe "serves the same page"


def _norm(url: str) -> str:
    try:
        return normalize_url(url) or url
    except Exception:
        return url


def _parent_page(crawler) -> Optional[Any]:
    """A crawled HTML page that answered 200 to probe under: one path segment if possible, not the home page."""
    fallback = None
    for r in list(getattr(crawler, "results_list", None) or []):
        seo = getattr(r, "seo", None) or {}
        if getattr(r, "status_code", None) != 200 or not seo.get("is_html"):
            continue
        p = urlparse(r.url)
        if p.query:
            continue
        segs = [s for s in (p.path or "").split("/") if s]
        if len(segs) == 1 and "." not in segs[0]:
            return r
        if segs and fallback is None:
            fallback = r
    return fallback


def _page_data(html: str, final_url: str, resp, crawler) -> Dict[str, Any]:
    from .extract import extract_page
    try:
        return extract_page(html, final_url, resp, getattr(crawler, "base_domain", "") or "")
    except Exception:
        return {}


def _probe(crawler, session, kind: str, url: str, parent=None, timeout: float = 15.0) -> Dict[str, Any]:
    cfg = getattr(crawler, "config", None)
    verify = bool(getattr(cfg, "verify_ssl", False))
    entry: Dict[str, Any] = {"kind": kind, "url": url, "status": None, "location": "", "final_url": "",
                             "verdict": "error", "note": "", "parent": getattr(parent, "url", "") if parent else "",
                             "title": "", "words": 0, "soft_404_signals": [], "page": {}}
    try:
        resp = session.get(url, timeout=(min(timeout, 10.0), timeout), allow_redirects=False, verify=verify)
    except Exception as e:
        entry["note"] = f"No response: {type(e).__name__}"
        return entry
    entry["status"] = resp.status_code
    entry["response_ms"] = int(resp.elapsed.total_seconds() * 1000) if getattr(resp, "elapsed", None) else 0
    code = resp.status_code
    if 300 <= code < 400:
        loc = resp.headers.get("location", "") or ""
        entry["location"] = urljoin(url, loc) if loc else ""
        home = _norm(site_origin(crawler) + "/")
        if entry["location"] and _norm(entry["location"]) == home:
            entry["verdict"] = "redirect_home"
            entry["note"] = ("Unknown addresses are redirected to the home page. Google treats this as a soft 404 "
                             "and visitors land on a page they did not ask for; answer 404 instead.")
        else:
            entry["verdict"] = "ok"
            entry["note"] = f"Redirects (HTTP {code}) to {entry['location'] or 'an unnamed target'}."
        return entry
    if code in (404, 410):
        entry["verdict"] = "ok"
        entry["note"] = f"Correct: an address that does not exist answers {code}."
        return entry
    if code >= 500:
        entry["verdict"] = "server_error"
        entry["note"] = f"An address that does not exist makes the server fail (HTTP {code}) instead of answering 404."
        return entry
    if code >= 400:
        entry["verdict"] = "ok"
        entry["note"] = f"Answers {code} (an error, so it is not indexed; 404 is the usual answer)."
        return entry
    ctype = (resp.headers.get("content-type") or "").lower()
    body = (resp.content or b"")[:MAX_BODY]
    html = ""
    if "html" in ctype or not ctype:
        try:
            html = body.decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, TypeError):
            html = body.decode("utf-8", errors="replace")
    page = _page_data(html, str(resp.url or url), resp, crawler) if html else {}
    entry["final_url"] = str(resp.url or url)
    entry["title"] = (page.get("titles") or [""])[0]
    entry["words"] = int(page.get("word_count") or 0)
    entry["soft_404_signals"] = list(page.get("soft_404_signals") or [])
    entry["page"] = {k: page.get(k) for k in ("titles", "h1", "word_count", "soft_404_signals", "canonicals",
                                                "directives", "content_hash")} if page else {}
    noindex = "noindex" in (page.get("directives") or []) or "none" in (page.get("directives") or [])
    if kind == "nested" and parent is not None:
        pseo = getattr(parent, "seo", None) or {}
        same = bool(page) and (
            (page.get("content_hash") and page.get("content_hash") == pseo.get("content_hash"))
            or (entry["title"] and entry["title"] == ((pseo.get("titles") or [""])[0]))
            or _similar(page.get("simhash") or 0, pseo.get("simhash") or 0))
        if same:
            entry["verdict"] = "infinite_space"
            entry["note"] = ("A made-up address under a real page answers 200 with that same page. Its relative "
                             "links then point one folder deeper each time, so crawlers can follow them forever.")
            return entry
    entry["verdict"] = "soft_404"
    what = "an error page" if entry["soft_404_signals"] else "a normal-looking page"
    entry["note"] = (f"An address that does not exist answers 200 with {what}"
                     + (" (marked noindex, which limits the damage)" if noindex else "")
                     + ". Search engines can index endless copies; the server should answer 404.")
    return entry


def _similar(a: int, b: int) -> bool:
    if not a or not b:
        return False
    same_bits = 64 - bin((a ^ b) & ((1 << 64) - 1)).count("1")
    return same_bits / 64 * 100 >= SAME_PAGE_SIMILARITY


def run(crawler, stop_event=None, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Probes a random address at the site root and under a real page (two requests, no redirects
    followed). Report: {"status", "probes": [...], "soft_404", "nested_trap", "redirect_home", ...}."""
    origin = site_origin(crawler)
    if not origin:
        return status_report("skipped", "the start URL has no host")
    session = getattr(crawler, "http_session", None) or getattr(crawler, "session", None)
    if session is None:
        import requests
        session = requests.Session()
    cfg = getattr(crawler, "config", None)
    t = timeout or min(float(getattr(cfg, "timeout", 15) or 15), 20.0)
    token = f"seo-check-{uuid.uuid4().hex[:10]}"
    parent = _parent_page(crawler)
    jobs = [("root", f"{origin}/{token}/", None)]
    if parent is not None:
        jobs.append(("nested", f"{parent.url.split('?', 1)[0].rstrip('/')}/{token}/", parent))
    probes: List[Dict[str, Any]] = []
    for kind, url, par in jobs:
        if stop_event is not None and stop_event.is_set():
            break
        probes.append(_probe(crawler, session, kind, url, par, t))
    verdicts = {p["kind"]: p["verdict"] for p in probes}
    rep: Dict[str, Any] = {
        "status": "done" if len(probes) == len(jobs) else "stopped",
        "reason": "" if parent is not None else "no crawled page to probe under, so only the root probe ran",
        "checked_at": now_iso(), "origin": origin, "probes": probes,
        "soft_404": any(p["verdict"] == "soft_404" for p in probes),
        "nested_trap": verdicts.get("nested") == "infinite_space",
        "redirect_home": any(p["verdict"] == "redirect_home" for p in probes),
        "server_error": any(p["verdict"] == "server_error" for p in probes),
    }
    rep["healthy"] = not (rep["soft_404"] or rep["nested_trap"] or rep["redirect_home"] or rep["server_error"]) \
        and all(p["verdict"] != "error" for p in probes)
    if not probes:
        rep["empty"] = True
    return rep
