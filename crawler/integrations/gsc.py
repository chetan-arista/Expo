"""
Google Search Console: properties (sites.list), Search Analytics per page (searchAnalytics.query) and the URL
Inspection API (urlInspection.index.inspect), joined to the crawl like Screaming Frog's Search Console tab.

  list_properties(account_id) -> [{"site_url", "permission", "kind", "label"}]
  best_property(props, url) -> the most specific property that covers the URL (None when none does)
  search_analytics(client, prop, opts, job) -> {"rows": [[url, clicks, impressions, ctr, position]], ...}
  inspect(client, urls_by_prop, opts, job, done) -> {"results": {url: compact}, "skipped": {url: why}, ...}
  joined(site) -> {"rows": {crawl url: {...}}, "orphans": [url], "inspection": {crawl url: compact}}  (memoised)

Quotas (Google): Search Analytics ~1,200 queries a minute per site; URL Inspection 2,000 URLs a day and 600 a
minute per property. A used-up daily quota stops the job cleanly; the URLs left are "Not inspected: daily quota
reached" and the counter in store.quota (per property, per day) keeps the next run from wasting calls.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from . import store
from .common import (IntegrationError, Job, JoinResult, MatchOptions, QuotaExceeded, RateLimiter, Stopped,
                     date_range, endpoint, join, normalize, now_iso)

SOURCE = "gsc"
ROW_LIMIT = 25000                 # rows per Search Analytics request (API maximum)
INSPECT_DAILY_LIMIT = 2000        # URL Inspection API: per property per day
INSPECT_PER_MINUTE = 600
INSPECT_WORKERS = 2
DEVICES = {"all": "", "desktop": "DESKTOP", "mobile": "MOBILE", "tablet": "TABLET"}
SEARCH_TYPES = ("web", "image", "video", "news", "discover", "googleNews")
QUERY_OPS = {"none": "", "contains": "contains", "not_contains": "notContains", "equals": "equals",
             "not_equals": "notEquals", "regex": "includingRegex", "not_regex": "excludingRegex"}

DEFAULTS: Dict[str, Any] = {
    "date_range": "30d", "start_date": "", "end_date": "",
    "device": "all", "country": "", "search_type": "web",
    "query_filter": {"op": "none", "value": ""},
    "match_trailing_slash": True, "match_case": False, "match_protocol": True, "match_www": False,
    "limit_max_results": 100000, "crawl_new_urls": False, "data_state": "final",
    "url_inspection": False, "inspection_limit": INSPECT_DAILY_LIMIT, "inspect_skip_nonindexable": False,
    "multiple_properties": False, "inspection_language": "en-US",
}

# Inspection summary labels (Screaming Frog wording)
ON_GOOGLE = "URL is on Google"
ON_GOOGLE_ISSUES = "URL is on Google, but has issues"
NOT_ON_GOOGLE = "URL is not on Google"


def options(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """DEFAULTS overlaid with the given values, cleaned (unknown choices fall back to the default)."""
    o = dict(DEFAULTS)
    for k, v in (values or {}).items():
        if k in DEFAULTS and v is not None:
            o[k] = v
    o["device"] = o["device"] if o["device"] in DEVICES else "all"
    o["search_type"] = o["search_type"] if o["search_type"] in SEARCH_TYPES else "web"
    qf = o.get("query_filter") if isinstance(o.get("query_filter"), dict) else {}
    op = qf.get("op") if qf.get("op") in QUERY_OPS else "none"
    o["query_filter"] = {"op": op, "value": str(qf.get("value") or "")[:4000]}
    o["country"] = _country(o.get("country"))
    for k in ("limit_max_results", "inspection_limit"):
        try:
            o[k] = max(1, int(o[k]))
        except (TypeError, ValueError):
            o[k] = DEFAULTS[k]
    o["inspection_limit"] = min(o["inspection_limit"], INSPECT_DAILY_LIMIT)
    for k in ("match_trailing_slash", "match_case", "match_protocol", "match_www", "crawl_new_urls",
              "url_inspection", "inspect_skip_nonindexable", "multiple_properties"):
        o[k] = bool(o[k])
    return o


def _country(value: Any) -> str:
    """ISO 3166-1 alpha-3, lower case as the API wants it ("" = all countries). Alpha-2 is converted."""
    code = str(value or "").strip()
    if not code or code.lower() == "none":
        return ""
    if len(code) == 2:
        try:
            import pycountry
            c = pycountry.countries.get(alpha_2=code.upper())
            return c.alpha_3.lower() if c else ""
        except Exception:
            return ""
    return code.lower() if len(code) == 3 and code.isalpha() else ""


# ---------------------------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------------------------
def property_kind(site_url: str) -> str:
    return "domain" if str(site_url).startswith("sc-domain:") else "url_prefix"


def property_covers(prop: str, url: str) -> bool:
    """A Domain property covers every protocol and subdomain of its domain; a URL-prefix property covers the URLs
    that start with it."""
    if not prop or not url:
        return False
    if prop.startswith("sc-domain:"):
        domain = prop.split(":", 1)[1].lower().strip(".")
        host = (urlsplit(url).hostname or "").lower()
        return host == domain or host.endswith("." + domain)
    return url.startswith(prop) or url.lower().startswith(prop.lower())


def best_property(props: Iterable[str], url: str) -> Optional[str]:
    """The most specific property covering the URL: the longest URL prefix, else the Domain property."""
    best, score = None, -1
    for p in props:
        if not property_covers(p, url):
            continue
        s = len(p) + 1000 if not p.startswith("sc-domain:") else len(p)
        if s > score:
            best, score = p, s
    return best


def list_properties(client) -> List[Dict[str, Any]]:
    data = client.json("GET", f"{endpoint('gsc')}/sites", "Search Console properties")
    out = []
    for e in data.get("siteEntry") or []:
        url = str(e.get("siteUrl") or "")
        perm = str(e.get("permissionLevel") or "")
        if not url or perm == "siteUnverifiedUser":
            continue
        kind = property_kind(url)
        out.append({"site_url": url, "permission": perm, "kind": kind,
                    "label": (url.split(":", 1)[1] + " (Domain)") if kind == "domain" else url})
    out.sort(key=lambda p: (p["kind"] != "domain", p["site_url"]))
    return out


# ---------------------------------------------------------------------------------------------
# Search Analytics
# ---------------------------------------------------------------------------------------------
def query_body(opts: Dict[str, Any], start: str, end: str) -> Dict[str, Any]:
    body: Dict[str, Any] = {"startDate": start, "endDate": end, "dimensions": ["page"],
                            "type": opts["search_type"], "dataState": opts.get("data_state") or "final"}
    filters = []
    if DEVICES.get(opts["device"]):
        filters.append({"dimension": "device", "operator": "equals", "expression": DEVICES[opts["device"]]})
    if opts["country"]:
        filters.append({"dimension": "country", "operator": "equals", "expression": opts["country"]})
    qf = opts["query_filter"]
    if QUERY_OPS.get(qf["op"]) and qf["value"]:
        filters.append({"dimension": "query", "operator": QUERY_OPS[qf["op"]], "expression": qf["value"]})
    if filters:
        body["dimensionFilterGroups"] = [{"groupType": "and", "filters": filters}]
    return body


def search_analytics(client, prop: str, opts: Dict[str, Any], job: Optional[Job] = None) -> Dict[str, Any]:
    """Every page's clicks / impressions / CTR / position for the date range, up to limit_max_results rows,
    25,000 per request."""
    start, end = date_range(opts["date_range"], opts.get("start_date", ""), opts.get("end_date", ""))
    url = f"{endpoint('gsc')}/sites/{quote(prop, safe='')}/searchAnalytics/query"
    limit = int(opts["limit_max_results"])
    rows: List[List[Any]] = []
    truncated = False
    status = "done"
    while len(rows) < limit:
        body = query_body(opts, start, end)
        body["rowLimit"] = min(ROW_LIMIT, limit - len(rows))
        body["startRow"] = len(rows)
        try:
            if job is not None and job.stopped:
                raise Stopped("Stopped.")
            data = client.json("POST", url, "Search Analytics", json=body)
        except Stopped:
            status = "stopped"            # keep the pages read so far
            break
        page = data.get("rows") or []
        for r in page:
            keys = r.get("keys") or []
            if not keys:
                continue
            rows.append([str(keys[0]), int(round(float(r.get("clicks") or 0))),
                         int(round(float(r.get("impressions") or 0))), float(r.get("ctr") or 0.0),
                         round(float(r.get("position") or 0.0), 2)])
        if job is not None:
            job.progress("Search Analytics", message=f"Search Analytics: {len(rows):,} pages read")
        if len(page) < body["rowLimit"]:
            break
    else:
        truncated = True
    return {"rows": rows, "row_count": len(rows), "truncated": truncated, "start": start, "end": end,
            "status": status, "message": "Stopped: only part of the data was read." if status == "stopped" else ""}


# ---------------------------------------------------------------------------------------------
# URL Inspection
# ---------------------------------------------------------------------------------------------
def compact_inspection(result: Dict[str, Any], prop: str) -> Dict[str, Any]:
    """The fields the tab shows, from one UrlInspectionResult (AMP left out: deprecated)."""
    idx = result.get("indexStatusResult") or {}
    out: Dict[str, Any] = {
        "verdict": idx.get("verdict") or "VERDICT_UNSPECIFIED",
        "coverage": idx.get("coverageState") or "",
        "robots": idx.get("robotsTxtState") or "",
        "indexing": idx.get("indexingState") or "",
        "last_crawl": idx.get("lastCrawlTime") or "",
        "fetch": idx.get("pageFetchState") or "",
        "crawled_as": idx.get("crawledAs") or "",
        "google_canonical": idx.get("googleCanonical") or "",
        "user_canonical": idx.get("userCanonical") or "",
        "sitemaps": list(idx.get("sitemap") or [])[:5],
        "referring": list(idx.get("referringUrls") or [])[:5],
        "link": result.get("inspectionResultLink") or "",
        "property": prop, "inspected_at": now_iso(),
        "mobile": None, "rich": None,
    }
    mob = result.get("mobileUsabilityResult")
    if isinstance(mob, dict) and mob.get("verdict") not in (None, "", "VERDICT_UNSPECIFIED"):
        out["mobile"] = {"verdict": mob.get("verdict"),
                         "issues": [str(i.get("message") or i.get("issueType") or "")
                                    for i in mob.get("issues") or [] if isinstance(i, dict)][:10]}
    rich = result.get("richResultsResult")
    if isinstance(rich, dict):
        types, items, errors, warnings = [], [], 0, 0
        error_types = []
        for d in rich.get("detectedItems") or []:
            t = str(d.get("richResultType") or "")
            if t and t not in types:
                types.append(t)
            for it in d.get("items") or []:
                issues = []
                for iss in it.get("issues") or []:
                    sev = str(iss.get("severity") or "")
                    if sev == "ERROR":
                        errors += 1
                        if t and t not in error_types:
                            error_types.append(t)
                    elif sev == "WARNING":
                        warnings += 1
                    issues.append({"msg": str(iss.get("issueMessage") or "")[:200], "sev": sev})
                if len(items) < 10:
                    items.append({"type": t, "name": str(it.get("name") or "")[:120], "issues": issues[:5]})
        out["rich"] = {"verdict": rich.get("verdict") or "", "types": types, "error_types": error_types,
                       "errors": errors, "warnings": warnings, "items": items}
    return out


def summary_label(c: Optional[Dict[str, Any]]) -> str:
    if not c:
        return ""
    verdict = c.get("verdict")
    if verdict in ("PASS", "PARTIAL"):
        bad = verdict == "PARTIAL"
        if (c.get("rich") or {}).get("verdict") == "FAIL" or (c.get("mobile") or {}).get("verdict") == "FAIL":
            bad = True
        return ON_GOOGLE_ISSUES if bad else ON_GOOGLE
    if verdict in ("FAIL", "NEUTRAL"):
        return NOT_ON_GOOGLE
    return "Unknown"


def inspect(client, urls_by_prop: Dict[str, List[str]], opts: Dict[str, Any], job: Optional[Job] = None,
            done: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Inspects the URLs (grouped by the property they belong to) within today's quota. URLs already in `done`
    (earlier runs for this crawl) are not inspected again."""
    done = dict(done or {})
    results: Dict[str, Any] = {}
    skipped: Dict[str, str] = {}
    errors: List[str] = []
    quota_hit = False
    limiter = RateLimiter(INSPECT_PER_MINUTE)
    url = f"{endpoint('gsc_inspect')}/urlInspection/index:inspect"
    lock = threading.Lock()
    todo: List[Tuple[str, str]] = []
    for prop, urls in urls_by_prop.items():
        left = max(0, min(int(opts["inspection_limit"]), INSPECT_DAILY_LIMIT) - store.quota_used("gsc_inspect", prop))
        for u in urls:
            if u in done:
                continue
            if left <= 0:
                skipped[u] = "Not inspected: today's URL Inspection quota for this property is used up (2,000 a day)."
                quota_hit = True
                continue
            todo.append((prop, u))
            left -= 1
    total = len(todo)
    if job is not None:
        job.progress("URL Inspection", 0, total, f"URL Inspection: 0 of {total:,}")
    stop_all = threading.Event()
    counter = {"n": 0}

    def one(item: Tuple[str, str]) -> None:
        nonlocal quota_hit
        prop, u = item
        if stop_all.is_set() or (job is not None and job.stopped):
            return
        limiter.wait(job.stop_event if job is not None else None)
        try:
            data = client.json("POST", url, "URL Inspection",
                               json={"inspectionUrl": u, "siteUrl": prop,
                                     "languageCode": opts.get("inspection_language") or "en-US"})
            store.quota_add("gsc_inspect", prop)
            res = compact_inspection(data.get("inspectionResult") or {}, prop)
            with lock:
                results[u] = res
        except QuotaExceeded:
            store.quota_fill("gsc_inspect", prop, INSPECT_DAILY_LIMIT)
            with lock:
                quota_hit = True
                skipped[u] = "Not inspected: Google says today's URL Inspection quota is used up."
            stop_all.set()
        except Stopped:
            stop_all.set()
        except IntegrationError as e:
            with lock:
                skipped[u] = f"Not inspected: {e}"
                if len(errors) < 20:
                    errors.append(f"{u}: {e}")
        finally:
            with lock:
                counter["n"] += 1
                if job is not None:
                    job.progress("URL Inspection", counter["n"], total,
                                 f"URL Inspection: {counter['n']:,} of {total:,}")

    with ThreadPoolExecutor(max_workers=INSPECT_WORKERS) as pool:
        list(pool.map(one, todo))
    for prop, u in todo:
        if u not in results and u not in skipped:
            skipped[u] = ("Not inspected: the daily quota was reached." if quota_hit
                          else "Not inspected: the fetch was stopped.")
    if job is not None:
        for e in errors:
            job.error(e)
    return {"results": results, "skipped": skipped, "quota_hit": quota_hit, "errors": errors,
            "inspected": len(results)}


# ---------------------------------------------------------------------------------------------
# Join with the crawl (memoised on the Site snapshot)
# ---------------------------------------------------------------------------------------------
def _memo(site, key: str, build):
    cache = site.__dict__.setdefault("_integrations_memo", {})
    if key not in cache:
        cache[key] = build()
    return cache[key]


def data_of(site) -> Dict[str, Any]:
    d = (getattr(site, "integrations", None) or {}).get(SOURCE)
    return d if isinstance(d, dict) else {}


def _crawl_urls(site) -> List[str]:
    return list((getattr(site, "by_url", None) or {}).keys())


def _internal(site, url: str) -> bool:
    try:
        from ..url_utils import is_internal_url
        cfg = getattr(site, "config", None)
        return is_internal_url(url, getattr(site, "base_domain", "") or "",
                               bool(getattr(cfg, "include_subdomains", False)),
                               set(getattr(cfg, "allowed_domains", None) or []) or None)
    except Exception:
        return True


def joined(site) -> Dict[str, Any]:
    def build() -> Dict[str, Any]:
        d = data_of(site)
        opts = MatchOptions.from_options(d.get("options") or {})
        sa = d.get("analytics") or {}
        rows = [(r[0], r[1], {"clicks": r[1], "impressions": r[2], "ctr": r[3], "position": r[4], "url": r[0]})
                for r in sa.get("rows") or [] if isinstance(r, list) and len(r) >= 5]
        res: JoinResult = join(((u, rank, p) for u, rank, p in rows), _crawl_urls(site), opts)
        by_url = {crawl: dict(payload, api_url=api) for crawl, (api, payload) in res.matched.items()}
        orphans: Dict[str, Dict[str, Any]] = {}
        for api, payload in res.unmatched:
            if _internal(site, api) and api not in orphans:
                orphans[api] = payload
        insp = (d.get("inspection") or {}).get("results") or {}
        inspected: Dict[str, Any] = {}
        for u, c in insp.items():
            target = u if u in site.by_url else normalize(u)
            if target in site.by_url:
                inspected[target] = c
        return {"rows": by_url, "orphans": orphans, "inspection": inspected,
                "skipped": dict((d.get("inspection") or {}).get("skipped") or {})}
    return _memo(site, "gsc_join", build)
