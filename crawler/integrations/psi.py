"""
PageSpeed Insights (API v5 runPagespeed): Core Web Vitals field data from the Chrome UX Report (CrUX, real
visitors, at URL level with the origin as fallback) and Lighthouse lab data (scores, metrics, insights and
diagnostics), stored per crawled URL in a compact form and read by the PageSpeed tab (and, for the three
Lighthouse mobile checks and the Lighthouse accessibility score, by the Mobile and Accessibility tabs).

  key: env PSI_API_KEY, else the key saved under Configuration > API Access (store.psi_key). Google's keyless
       quota is shared by everyone and is 0 a day at the moment (answer 429), so in practice a free key is needed.
  run(urls, opts, key, job, needs_login, done) -> {"results": {url: compact}, "quota_hit", "errors"}
  compact(raw) -> {"status", "message", "scores", "lab", "field", "origin_field", "resources", "audits", ...}
       audits[id] = {"s", "m", "ms", "b", "v", "t", "n", "u"} + "items" (top RESOURCE_ITEMS resources by wasted
       bytes: {"url", "total", "wasted", "wasted_pct", "wasted_ms"}) for the unused CSS / JS and opportunity audits
  helpers for tabs: result_for(site, url), request_status(res), filter_state(res, AuditFilter),
       cwv_assessment(res), mobile_audit(site, url, check), accessibility_score(site, url), details(site, url)

Limits: Google allows 240 requests a minute and 25,000 a day per project; runs use at most `concurrency`
parallel requests under a 240/minute limiter, back off on 429/5xx and stop cleanly when the daily quota is used
up (the URLs left are "Not checked: daily quota reached"). PageSpeed runs on Google's servers, so a page that
needs our login cannot be tested: it is "Not checked: page needs login".

Lighthouse audit ids follow Lighthouse 12/13 (insight audits) with the older ids as fallbacks, so results from
an older Lighthouse still map. An audit counts as failed when Lighthouse scores it below 0.9 (its own pass mark);
an "informative" audit fails when it reports savings; "not applicable" passes; "manual" / "error" audits are
not judged (the URL is not checked for that filter).
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from . import store
from .common import (IntegrationError, Job, QuotaExceeded, RateLimiter, Stopped, call, endpoint, google_error,
                     mask_key, new_session, now_iso)

SOURCE = "psi"
DEVICES = ("mobile", "desktop")
CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
API_CATEGORY = {"performance": "PERFORMANCE", "accessibility": "ACCESSIBILITY", "best-practices": "BEST_PRACTICES",
                "seo": "SEO"}
GROUPS = ("overview", "crux", "lighthouse", "insights", "diagnostics", "mobile_friendly", "accessibility")
DEFAULT_GROUPS = ["overview", "crux", "lighthouse", "insights", "diagnostics", "mobile_friendly"]
PER_MINUTE = 240
DAILY_LIMIT = 25000
PASS_SCORE = 0.9                  # Lighthouse's own pass mark

DEFAULTS: Dict[str, Any] = {
    "enabled": False, "device": "mobile", "categories": ["performance"], "groups": list(DEFAULT_GROUPS),
    "max_urls": 500, "concurrency": 4, "per_minute": PER_MINUTE, "only_missing": True, "indexable_only": True,
    "locale": "en",
}

NEEDS_LOGIN = "Not checked: page needs login (PageSpeed Insights runs on Google's servers and cannot sign in)"
QUOTA_LEFT = "Not checked: the daily PageSpeed quota is used up"
KEYLESS_QUOTA = ("Google's shared keyless PageSpeed quota is used up (it allows 0 requests a day). Add a free API "
                 "key under Configuration > API Access > PageSpeed Insights.")


@dataclass(frozen=True)
class AuditFilter:
    id: str
    name: str                                 # Screaming Frog filter name
    audits: Tuple[str, ...]                   # Lighthouse audit ids, newest first
    issue_type: str
    priority: str
    description: str
    fix: str
    short_fix: str


AUDIT_FILTERS: List[AuditFilter] = [
    AuditFilter("document_request_latency", "Document Request Latency",
                ("document-latency-insight", "server-response-time"), "Opportunity", "High",
                "The HTML page itself arrives slowly: the server takes long to answer, the page redirects first, or "
                "the HTML is sent without compression. Everything else waits for it.",
                "Cache the HTML (page cache / CDN), speed up the server code and database, link straight to the final "
                "URL and enable gzip or brotli.",
                "Speed up and compress the HTML response; avoid redirects"),
    AuditFilter("lcp_request_discovery", "LCP Request Discovery", ("lcp-discovery-insight", "prioritize-lcp-image"),
                "Opportunity", "Medium",
                "The browser finds the image of the largest element late (it is lazy-loaded, set from CSS or "
                "JavaScript) or does not give it high priority, so the main content shows late.",
                "Put the main image in the HTML as a normal <img>, do not lazy-load it and add fetchpriority=\"high\".",
                "Load the main (LCP) image early: no lazy-load, fetchpriority=high"),
    AuditFilter("render_blocking_requests", "Render Blocking Requests",
                ("render-blocking-insight", "render-blocking-resources"), "Opportunity", "High",
                "Stylesheets and scripts in the <head> stop the page from drawing anything until they have loaded.",
                "Inline the critical CSS, load the rest later, and add defer or async to scripts that are not needed "
                "for the first view.",
                "Defer non-critical CSS/JS; inline critical CSS"),
    AuditFilter("network_dependency_tree", "Network Dependency Tree",
                ("network-dependency-tree-insight", "critical-request-chains"), "Opportunity", "Low",
                "Files that load one after another in a chain (a CSS file that pulls a font that pulls another file) "
                "delay the page; Lighthouse also checks preconnects to other servers.",
                "Shorten the chains: preload the key files, preconnect to the few servers you depend on, and load "
                "fewer files in the critical path.",
                "Shorten request chains; preload/preconnect key resources"),
    AuditFilter("efficient_cache_lifetimes", "Use Efficient Cache Lifetimes", ("cache-insight", "uses-long-cache-ttl"),
                "Opportunity", "Medium",
                "Static files (images, scripts, styles, fonts) are sent with a short or missing cache lifetime, so "
                "returning visitors download them again.",
                "Send Cache-Control: max-age of a year for versioned static files (use file names that change when "
                "the file changes).",
                "Cache static files for a long time (Cache-Control max-age)"),
    AuditFilter("layout_shift_culprits", "Layout Shift Culprits", ("cls-culprits-insight", "layout-shift-elements"),
                "Opportunity", "Medium",
                "Parts of the page jump while it loads (images without size, late fonts, injected banners), which "
                "makes visitors click the wrong thing.",
                "Give images and embeds a width and height (or aspect-ratio), reserve space for banners and ads, and "
                "use font-display with matching fallback fonts.",
                "Reserve space for images, ads and banners to stop jumps"),
    AuditFilter("optimize_dom_size", "Optimize DOM Size", ("dom-size-insight", "dom-size"), "Opportunity", "Low",
                "The page has a very large number of HTML elements, which slows down style calculation, layout and "
                "every interaction.",
                "Remove hidden or duplicated markup, paginate or lazy-render long lists and simplify deeply nested "
                "layouts.",
                "Reduce the number of HTML elements on the page"),
    AuditFilter("improve_image_delivery", "Improve Image Delivery",
                ("image-delivery-insight", "uses-optimized-images", "modern-image-formats", "uses-responsive-images",
                 "efficient-animated-content"), "Opportunity", "High",
                "Images are bigger than needed: not compressed, in an old format, or larger than the size they are "
                "shown at.",
                "Serve WebP/AVIF, compress images, and use srcset/sizes so phones get small versions.",
                "Compress images, use WebP/AVIF and responsive sizes"),
    AuditFilter("forced_reflow", "Forced Reflow", ("forced-reflow-insight",), "Opportunity", "Low",
                "JavaScript reads the page's layout right after changing it, forcing the browser to recalculate the "
                "layout again and again.",
                "Batch reads before writes in your scripts (read sizes first, then change styles), or use "
                "requestAnimationFrame.",
                "Batch layout reads before writes in JavaScript"),
    AuditFilter("legacy_javascript", "Legacy JavaScript", ("legacy-javascript-insight", "legacy-javascript"),
                "Opportunity", "Low",
                "Scripts still carry code for very old browsers (polyfills and transforms) that modern browsers do "
                "not need.",
                "Build a modern bundle (no polyfills for features every current browser has) and update old "
                "libraries.",
                "Stop shipping old-browser polyfills to modern browsers"),
    AuditFilter("duplicated_javascript", "Duplicated JavaScript",
                ("duplicated-javascript-insight", "duplicated-javascript"), "Opportunity", "Low",
                "The same library or module is included more than once across the page's scripts.",
                "Deduplicate dependencies in the bundler and load each library once.",
                "Load each JavaScript library only once"),
    AuditFilter("font_display", "Font Display", ("font-display-insight", "font-display"), "Opportunity", "Low",
                "Text stays invisible while web fonts download.",
                "Add font-display: swap (or optional) to every @font-face rule.",
                "Add font-display: swap to @font-face rules"),
    AuditFilter("enormous_network_payloads", "Avoid Enormous Network Payloads", ("total-byte-weight",),
                "Opportunity", "Medium",
                "The page downloads a lot of data in total, which is slow and costly on mobile connections.",
                "Compress and resize images, remove unused scripts and styles, and lazy-load what is below the fold.",
                "Cut the page's total download size"),
    AuditFilter("minify_css", "Minify CSS", ("unminified-css",), "Opportunity", "Low",
                "Stylesheets are sent with comments and spacing that could be removed.",
                "Minify CSS in your build or let the CDN / caching plugin do it.",
                "Minify CSS files"),
    AuditFilter("minify_javascript", "Minify JavaScript", ("unminified-javascript",), "Opportunity", "Low",
                "Scripts are sent unminified.",
                "Minify JavaScript in your build or let the CDN / caching plugin do it.",
                "Minify JavaScript files"),
    AuditFilter("reduce_unused_css", "Reduce Unused CSS", ("unused-css-rules",), "Opportunity", "Medium",
                "Much of the CSS loaded is not used on this page.",
                "Split CSS per template, remove unused rules, and load styles for other pages only there.",
                "Remove or split CSS this page does not use"),
    AuditFilter("reduce_unused_javascript", "Reduce Unused JavaScript", ("unused-javascript",), "Opportunity",
                "Medium",
                "Much of the JavaScript loaded is not used on this page.",
                "Split the bundle by page, load features on demand, and remove plugins and tags nobody uses.",
                "Remove or defer JavaScript this page does not use"),
    AuditFilter("javascript_execution_time", "Reduce JavaScript Execution Time", ("bootup-time",), "Opportunity",
                "Medium",
                "Running the page's scripts keeps the browser busy for a long time.",
                "Remove heavy scripts and third-party tags, split long tasks and defer what is not needed at once.",
                "Run less JavaScript on load; defer third-party tags"),
    AuditFilter("main_thread_work", "Minimize Main-Thread Work", ("mainthread-work-breakdown",), "Opportunity",
                "Medium",
                "The browser's main thread is busy for long (scripts, style and layout), so the page reacts slowly.",
                "Reduce script work, simplify CSS selectors and layouts, and move heavy work off the main thread.",
                "Cut script, style and layout work on load"),
]
FILTERS_BY_ID = {f.id: f for f in AUDIT_FILTERS}

# The three Lighthouse mobile checks Screaming Frog shows in the Mobile tab (+ viewport). Lighthouse 12 dropped
# content-width and font-size; with a newer Lighthouse those stay "not checked".
MOBILE_AUDITS: Dict[str, Tuple[str, ...]] = {
    "target_size": ("target-size", "tap-targets"),
    "content_width": ("content-width",),
    "font_size": ("font-size",),
    "viewport": ("viewport-insight", "viewport"),
}
LAB_METRICS: Dict[str, str] = {
    "fcp_ms": "first-contentful-paint", "lcp_ms": "largest-contentful-paint", "si_ms": "speed-index",
    "tbt_ms": "total-blocking-time", "cls": "cumulative-layout-shift", "tti_ms": "interactive",
    "ttfb_ms": "server-response-time", "total_bytes": "total-byte-weight", "dom_size": "dom-size",
}
CRUX_METRICS: Dict[str, str] = {
    "lcp": "LARGEST_CONTENTFUL_PAINT_MS", "inp": "INTERACTION_TO_NEXT_PAINT", "cls": "CUMULATIVE_LAYOUT_SHIFT_SCORE",
    "fcp": "FIRST_CONTENTFUL_PAINT_MS", "ttfb": "EXPERIMENTAL_TIME_TO_FIRST_BYTE",
}
CRUX_LABEL = {"FAST": "Good", "AVERAGE": "Needs Improvement", "SLOW": "Poor"}
KEEP_AUDITS = {a for f in AUDIT_FILTERS for a in f.audits} | {a for ids in MOBILE_AUDITS.values() for a in ids}


def options(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    o = dict(DEFAULTS)
    for k, v in (values or {}).items():
        if k in DEFAULTS and v is not None:
            o[k] = v
    o["device"] = o["device"] if o["device"] in DEVICES else "mobile"
    cats = [str(c).strip().lower() for c in (o.get("categories") or []) if str(c).strip()]
    groups = [g for g in (o.get("groups") or []) if g in GROUPS]
    if "accessibility" in groups and "accessibility" not in cats:
        cats.append("accessibility")
    if "mobile_friendly" in groups and "accessibility" not in cats:
        cats.append("accessibility")            # target-size lives in the accessibility category
    o["categories"] = [c for c in CATEGORIES if c in cats] or ["performance"]
    if "performance" not in o["categories"]:
        o["categories"].insert(0, "performance")
    o["groups"] = groups or list(DEFAULT_GROUPS)
    for k, lo, hi in (("max_urls", 1, DAILY_LIMIT), ("concurrency", 1, 10), ("per_minute", 1, PER_MINUTE)):
        try:
            o[k] = min(hi, max(lo, int(o[k])))
        except (TypeError, ValueError):
            o[k] = DEFAULTS[k]
    for k in ("enabled", "only_missing", "indexable_only"):
        o[k] = bool(o[k])
    return o


# ---------------------------------------------------------------------------------------------
# One request
# ---------------------------------------------------------------------------------------------
def request_params(url: str, opts: Dict[str, Any], key: str) -> List[Tuple[str, str]]:
    params = [("url", url), ("strategy", opts["device"]), ("locale", opts.get("locale") or "en")]
    params += [("category", API_CATEGORY[c]) for c in opts["categories"]]
    if key:
        params.append(("key", key))
    return params


def _savings(a: Dict[str, Any]) -> Tuple[int, int]:
    d = a.get("details") or {}
    ms = d.get("overallSavingsMs")
    if ms is None:
        ms_vals = [v for k, v in (a.get("metricSavings") or {}).items() if k != "CLS" and isinstance(v, (int, float))]
        ms = max(ms_vals) if ms_vals else 0
    b = d.get("overallSavingsBytes") or 0
    try:
        return int(round(float(ms or 0))), int(round(float(b or 0)))
    except (TypeError, ValueError):
        return 0, 0


def _items(a: Dict[str, Any]) -> Tuple[int, List[str]]:
    d = a.get("details") or {}
    items = d.get("items") if isinstance(d.get("items"), list) else []
    urls = []
    for it in items:
        if not isinstance(it, dict) or len(urls) >= 5:
            continue
        u = it.get("url")
        if not u and isinstance(it.get("node"), dict):
            u = it["node"].get("snippet")
        if u and isinstance(u, str):
            urls.append(u[:300])
    return len(items), urls


RESOURCE_ITEMS = 10              # resource rows kept per audit and URL (the PSI opportunities / coverage reports)
COVERAGE_AUDITS = ("unused-css-rules", "unused-javascript")


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def resource_items(a: Dict[str, Any], limit: int = RESOURCE_ITEMS) -> List[Dict[str, Any]]:
    """The audit's top resources by wasted bytes (then wasted ms): [{"url", "total", "wasted", "wasted_pct",
    "wasted_ms"}] (None where Lighthouse gave no value). Tables nested in a "list" detail (insight audits) are read
    too; rows without a URL are left out."""
    d = a.get("details") or {}
    rows: List[Dict[str, Any]] = []
    if d.get("type") == "list":
        tables = [x for x in (d.get("items") or []) if isinstance(x, dict) and x.get("type") == "table"]
    else:
        tables = [d]
    for t in tables:
        for it in (t.get("items") if isinstance(t.get("items"), list) else []):
            if not isinstance(it, dict):
                continue
            u = it.get("url") if isinstance(it.get("url"), str) else ""
            if not u and isinstance(it.get("source"), dict) and isinstance(it["source"].get("url"), str):
                u = it["source"]["url"]
            if not u:
                continue
            total, wasted = _num(it.get("totalBytes")), _num(it.get("wastedBytes"))
            pct, ms = _num(it.get("wastedPercent")), _num(it.get("wastedMs"))
            if pct is None and total and wasted is not None:
                pct = wasted / total * 100.0
            rows.append({"url": u[:300], "total": int(total) if total is not None else None,
                         "wasted": int(wasted) if wasted is not None else None,
                         "wasted_pct": round(pct, 1) if pct is not None else None,
                         "wasted_ms": int(round(ms)) if ms is not None else None})
    rows.sort(key=lambda r: (-(r["wasted"] or 0), -(r["wasted_ms"] or 0)))
    return rows[:limit]


def _crux(exp: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(exp, dict):
        return {}
    metrics = exp.get("metrics") or {}
    out: Dict[str, Any] = {}
    for short, key in CRUX_METRICS.items():
        m = metrics.get(key)
        if not isinstance(m, dict) or m.get("percentile") is None:
            continue
        p = m.get("percentile")
        out[short] = round(float(p) / 100.0, 3) if short == "cls" else int(p)
        out[f"{short}_cat"] = str(m.get("category") or "")
    if out:
        out["overall"] = str(exp.get("overall_category") or "")
    return out


def compact(raw: Dict[str, Any]) -> Dict[str, Any]:
    """The parts of a runPagespeed answer the tabs and the detail pane use (a few KB instead of ~1 MB)."""
    lr = raw.get("lighthouseResult") or {}
    audits = lr.get("audits") or {}
    out: Dict[str, Any] = {"status": "success", "message": "", "fetched_at": now_iso(),
                           "final_url": lr.get("finalDisplayedUrl") or lr.get("finalUrl") or raw.get("id") or "",
                           "lighthouse": lr.get("lighthouseVersion") or "",
                           "warnings": [str(w)[:200] for w in (lr.get("runWarnings") or [])[:3]]}
    rt = lr.get("runtimeError") or {}
    if isinstance(rt, dict) and rt.get("code") and rt.get("code") != "NO_ERROR":
        out.update(status="error", message=f"Lighthouse: {rt.get('code')}")
    out["scores"] = {cid: int(round(float(c["score"]) * 100)) for cid, c in (lr.get("categories") or {}).items()
                     if isinstance(c, dict) and c.get("score") is not None}
    lab = {}
    for k, aid in LAB_METRICS.items():
        a = audits.get(aid) or {}
        if a.get("numericValue") is not None:
            v = float(a["numericValue"])
            lab[k] = round(v, 3) if k == "cls" else int(round(v))
    out["lab"] = lab
    le = raw.get("loadingExperience") or {}
    url_level = isinstance(le, dict) and not le.get("origin_fallback") and bool(le.get("metrics"))
    out["field"] = _crux(le) if url_level else {}
    out["origin_field"] = _crux(raw.get("originLoadingExperience"))
    res = {}
    for it in ((audits.get("resource-summary") or {}).get("details") or {}).get("items") or []:
        if isinstance(it, dict) and it.get("resourceType"):
            res[it["resourceType"]] = [int(it.get("requestCount") or 0), int(it.get("transferSize") or 0)]
    out["resources"] = res
    kept: Dict[str, Any] = {}
    for aid in KEEP_AUDITS:
        a = audits.get(aid)
        if not isinstance(a, dict):
            continue
        ms, b = _savings(a)
        n, urls = _items(a)
        kept[aid] = {"s": a.get("score"), "m": a.get("scoreDisplayMode") or "", "ms": ms, "b": b,
                     "v": str(a.get("displayValue") or "")[:80], "t": str(a.get("title") or "")[:120], "n": n,
                     "u": urls}
        # top resources (url, total / wasted bytes, wasted %) of the coverage and opportunity audits (P20 report)
        if aid in COVERAGE_AUDITS or (a.get("details") or {}).get("type") in ("opportunity", "list") or ms or b:
            items = resource_items(a)
            if items:
                kept[aid]["items"] = items
    out["audits"] = kept
    a11y = (lr.get("categories") or {}).get("accessibility")
    if isinstance(a11y, dict):
        failed = []
        for ref in a11y.get("auditRefs") or []:
            a = audits.get(ref.get("id") or "") or {}
            if a.get("scoreDisplayMode") in ("binary", "numeric") and a.get("score") is not None \
                    and float(a["score"]) < PASS_SCORE:
                failed.append({"id": ref.get("id"), "t": str(a.get("title") or "")[:120],
                               "w": ref.get("weight") or 0})
        out["a11y_failed"] = failed[:40]
    return out


def _error_result(message: str, status: str = "error") -> Dict[str, Any]:
    return {"status": status, "message": mask_key(message)[:200], "fetched_at": now_iso(), "scores": {}, "lab": {},
            "field": {}, "origin_field": {}, "resources": {}, "audits": {}}


def fetch_one(session: requests.Session, url: str, opts: Dict[str, Any], key: str,
              stop: Optional[threading.Event] = None, on_wait=None) -> Dict[str, Any]:
    """One runPagespeed call -> compact result (status "error" with Google's reason when Lighthouse failed).
    Raises QuotaExceeded for a used-up daily quota and Stopped when stopped."""
    try:
        resp = call(session, "GET", f"{endpoint('psi')}/runPagespeed", params=request_params(url, opts, key),
                    stop=stop, retries=3, timeout=120, on_wait=on_wait)
    except QuotaExceeded as e:
        if not key:
            raise QuotaExceeded(KEYLESS_QUOTA, e.status, e.reason)
        raise
    if resp.status_code == 200:
        try:
            return compact(resp.json())
        except ValueError:
            return _error_result("Error: PageSpeed sent an answer that is not JSON")
    status, message, reason = google_error(resp)
    if status in (400, 403) and ("API key" in message or "API_KEY" in reason or "PERMISSION_DENIED" in reason
                                 or "has not been used" in message or "disabled" in message):
        raise IntegrationError(f"PageSpeed Insights refused the key: {message}", status, reason)
    short = message.split(". ")[0][:160] if message else f"HTTP {status}"
    return _error_result(f"Error: {short}")


def needs_login(url: str, timeout: float = 15.0) -> str:
    """Opens the page without our login cookies: a 401/403 or a landing on a login page means PageSpeed (which
    runs on Google's servers, logged out) cannot test it. Returns the reason, "" when the page is public."""
    from ..url_utils import redirected_to_login
    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, stream=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; SiteAuditor PSI pre-check)"})
        status, final = r.status_code, r.url
        r.close()
    except requests.RequestException:
        return ""
    if status in (401, 403) or redirected_to_login(url, final):
        return NEEDS_LOGIN
    return ""


def run(urls: List[str], opts: Dict[str, Any], key: str, job: Optional[Job] = None,
        login_check: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """Tests the URLs (bounded concurrency, 240/minute). Returns {"results": {url: compact}, "quota_hit",
    "errors", "tested"}; URLs not tested because of a quota or a stop get a "skipped" result saying why."""
    results: Dict[str, Any] = {}
    errors: List[str] = []
    lock = threading.Lock()
    limiter = RateLimiter(int(opts.get("per_minute") or PER_MINUTE))
    stop_all = threading.Event()
    state = {"quota": "", "fatal": "", "n": 0}
    total = len(urls)
    stop_event = job.stop_event if job is not None else None
    if job is not None:
        job.progress("PageSpeed", 0, total, f"PageSpeed: 0 of {total:,}")

    def on_wait(msg: str) -> None:
        if job is not None:
            job.progress(message=f"PageSpeed: {msg}")

    def one(u: str) -> None:
        if stop_all.is_set() or (job is not None and job.stopped):
            return
        try:
            why = login_check(u) if login_check is not None else ""
            if why:
                res = _error_result(why, "skipped")
            else:
                limiter.wait(stop_event)
                res = fetch_one(new_session(), u, opts, key, stop_event, on_wait)
                store.quota_add("psi")
            with lock:
                results[u] = dict(res, device=opts["device"], categories=list(opts["categories"]))
                if res.get("status") == "error" and len(errors) < 20:
                    errors.append(f"{u}: {res.get('message')}")
        except QuotaExceeded as e:
            with lock:
                state["quota"] = str(e)
            stop_all.set()
        except Stopped:
            stop_all.set()
        except IntegrationError as e:
            with lock:
                state["fatal"] = str(e)
            stop_all.set()
        finally:
            with lock:
                state["n"] += 1
                if job is not None:
                    job.progress("PageSpeed", state["n"], total, f"PageSpeed: {state['n']:,} of {total:,}")

    workers = max(1, min(int(opts.get("concurrency") or 4), 10))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, urls))
    reason = (f"{QUOTA_LEFT}." if state["quota"] else f"Not checked: {state['fatal']}" if state["fatal"]
              else "Not checked: the PageSpeed run was stopped.")
    for u in urls:
        if u not in results:
            results[u] = dict(_error_result(reason, "skipped"), device=opts["device"])
    if job is not None:
        for e in errors:
            job.error(e)
        if state["quota"]:
            job.error(state["quota"])
    if state["fatal"] and not any(r.get("status") == "success" for r in results.values()):
        raise IntegrationError(state["fatal"])
    return {"results": results, "quota_hit": bool(state["quota"]), "quota_message": state["quota"],
            "errors": errors, "tested": sum(1 for r in results.values() if r.get("status") != "skipped")}


# ---------------------------------------------------------------------------------------------
# Reading results (tabs, detail pane)
# ---------------------------------------------------------------------------------------------
def data_of(site) -> Dict[str, Any]:
    d = (getattr(site, "integrations", None) or {}).get(SOURCE)
    return d if isinstance(d, dict) else {}


def result_for(site, url: str) -> Optional[Dict[str, Any]]:
    res = (data_of(site).get("results") or {}).get(url)
    return res if isinstance(res, dict) else None


def tested(res: Optional[Dict[str, Any]]) -> bool:
    return bool(res) and res.get("status") == "success"


def request_status(res: Optional[Dict[str, Any]]) -> str:
    """The PSI Request Status column: Success / Error: ... / Not checked: ... ("" = not requested)."""
    if not res:
        return ""
    if res.get("status") == "success":
        return "Success"
    return str(res.get("message") or ("Error" if res.get("status") == "error" else "Not checked"))


def audit_failed(a: Optional[Dict[str, Any]]) -> Optional[bool]:
    """True / False, or None when Lighthouse did not judge it (manual check, audit error)."""
    if not isinstance(a, dict):
        return None
    mode = a.get("m") or ""
    if mode == "notApplicable":
        return False
    if mode in ("manual", "error"):
        return None
    if mode == "informative":
        return bool(a.get("ms") or a.get("b"))
    score = a.get("s")
    if score is None:
        return None
    try:
        return float(score) < PASS_SCORE
    except (TypeError, ValueError):
        return None


def audit_of(res: Optional[Dict[str, Any]], ids: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    audits = (res or {}).get("audits") or {}
    for aid in ids:
        if aid in audits:
            return dict(audits[aid], id=aid)
    return None


def filter_state(res: Optional[Dict[str, Any]], f: AuditFilter) -> Optional[bool]:
    """Failed (True) / passed (False) / not judged for this URL (None)."""
    if not tested(res):
        return None
    return audit_failed(audit_of(res, f.audits))


def cwv_assessment(res: Optional[Dict[str, Any]], allow_origin: bool = False) -> str:
    """"Passed" / "Failed" from the URL's real-visitor data (LCP, INP, CLS at the 75th percentile; INP may be
    missing on pages with few interactions); "" without such data. allow_origin=True falls back to the origin."""
    if not tested(res):
        return ""
    fd = res.get("field") or {}
    if not fd and allow_origin:
        fd = res.get("origin_field") or {}
    cats = [fd.get(f"{m}_cat") for m in ("lcp", "inp", "cls") if fd.get(f"{m}_cat")]
    if not fd.get("lcp_cat") or not fd.get("cls_cat"):
        return ""
    return "Passed" if all(c == "FAST" for c in cats) else "Failed"


def field_scope(res: Optional[Dict[str, Any]]) -> str:
    if not tested(res):
        return ""
    if res.get("field"):
        return "URL"
    if res.get("origin_field"):
        return "Origin"
    return "No field data"


def mobile_audit(site, url: str, check: str) -> Optional[bool]:
    """Lighthouse verdict of a mobile check for a crawled URL ("target_size", "content_width", "font_size",
    "viewport"): True = failed, False = passed, None = not checked (no PSI result, or this Lighthouse version no
    longer runs that audit)."""
    res = result_for(site, url)
    if not tested(res):
        return None
    return audit_failed(audit_of(res, MOBILE_AUDITS.get(check, ())))


def accessibility_score(site, url: str) -> Optional[int]:
    """Lighthouse accessibility score 0-100 (None unless PSI ran with the accessibility category)."""
    res = result_for(site, url)
    if not tested(res):
        return None
    v = (res.get("scores") or {}).get("accessibility")
    return int(v) if isinstance(v, (int, float)) else None


def opportunities(res: Optional[Dict[str, Any]]) -> List[str]:
    return [f.name for f in AUDIT_FILTERS if filter_state(res, f)]


def total_savings(res: Optional[Dict[str, Any]]) -> Tuple[int, int]:
    ms = b = 0
    for f in AUDIT_FILTERS:
        a = audit_of(res, f.audits)
        if a and audit_failed(a):
            ms += int(a.get("ms") or 0)
            b += int(a.get("b") or 0)
    return ms, b


def details(site, url: str) -> Optional[Dict[str, Any]]:
    """Lighthouse Details (bottom tab) for one URL: status, scores, field and lab data, and every mapped check."""
    res = result_for(site, url)
    if not res:
        return None
    out = {"status": request_status(res), "device": res.get("device"), "fetched_at": res.get("fetched_at"),
           "lighthouse": res.get("lighthouse"), "final_url": res.get("final_url"), "scores": res.get("scores") or {},
           "lab": res.get("lab") or {}, "field": res.get("field") or {}, "origin_field": res.get("origin_field") or {},
           "field_scope": field_scope(res), "cwv": cwv_assessment(res), "resources": res.get("resources") or {},
           "warnings": res.get("warnings") or [], "checks": []}
    for f in AUDIT_FILTERS:
        a = audit_of(res, f.audits)
        state = filter_state(res, f)
        out["checks"].append({"filter": f.id, "name": f.name, "audit": (a or {}).get("id") or "",
                              "state": "failed" if state else "passed" if state is False else "not_checked",
                              "savings_ms": (a or {}).get("ms") or 0, "savings_bytes": (a or {}).get("b") or 0,
                              "display": (a or {}).get("v") or "", "examples": (a or {}).get("u") or []})
    out["mobile"] = {k: mobile_audit(site, url, k) for k in MOBILE_AUDITS}
    if "a11y_failed" in res:
        out["accessibility_failed"] = res.get("a11y_failed") or []
    return out
