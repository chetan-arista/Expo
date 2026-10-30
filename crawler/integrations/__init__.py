"""
Optional connections to Google Search Console (GSC), Google Analytics 4 (GA4) and PageSpeed Insights (PSI).
Nobody has to connect anything: when a source is connected and its data was fetched for a crawl, the Search
Console / Analytics / PageSpeed tabs fill; otherwise every filter of those tabs is "Not connected" (or "not
fetched yet"), never a fake 0.

Server-side only: OAuth refresh tokens, service account keys and the PSI key stay in
<reports_dir>/integrations/ (store.py); nothing secret is returned by any function here.

Per-crawl data lives on the crawler as `crawler.integrations` = {source: payload} (plain JSON data, so the saved
crawl store keeps it with the crawl; analysis.build_site copies it to `site.integrations`):
  gsc  {"source", "connected", "status", "fetched_at", "account", "property", "options", "date_start",
        "date_end", "analytics": {"rows": [[url, clicks, impressions, ctr, position]], "row_count", "truncated",
        "status", "message"}, "inspection": {"enabled", "results": {url: compact}, "skipped": {url: why},
        "quota_hit", "inspected", "status", "message"}, "last_error"}
  ga4  {"source", "connected", "status", "fetched_at", "account", "property", "property_name", "stream_uri",
        "origin", "options", "metrics", "date_start", "date_end", "rows": [[url, {metric: value}]], "row_count",
        "truncated", "last_error"}
  psi  {"source", "connected", "status", "fetched_at", "device", "categories", "options",
        "results": {crawl url: compact (psi.compact)}, "quota_hit", "last_error"}

Main entry points (web/app.py wires them to /api/integrations/...):
  status(base_url) ; connection(source)
  google_begin(base_url, sources) ; google_callback(state, code, error) ; google_paste(url)
  add_service_account(json_text, name) ; set_google_client(id, secret) ; update_account(id, name=, auto_connect=)
  disconnect_account(id) ; gsc_properties(account_id) ; ga4_accounts(account_id) ; ga4_streams(property, account_id)
  select(source, values) ; psi_connect(key, auto_connect) ; psi_disconnect()
  effective_options(source, overrides) ; start_fetch(source, crawler, options, urls, on_done) ; stop_fetch(source)
  stop_all(except_crawler=None) ; stop_for_crawler(crawler)   # reset / close / new crawl: stop a replaced crawl's fetch
  jobs(crawler=None) ; auto_fetch(crawler) ; discovered_urls(crawler_or_site, source)
  apply(crawler, source, payload) ; export_state(crawler) ; import_state(crawler, data) ; merge_state(old, new)
  put_result(crawler, source, payload, merge=True) ; get_result(site_or_crawler, source)   # Build 4 sources
  report_summary(site) ; traffic(site, url) ; traffic_weight(site, url) ; judge(tab_id, filter_id, site)
  connection("sheets") ; sheets.export(...) ; sheets.read_list(url)   # Google Sheets (same Google sign-in, source
                                                                       # "sheets" in google_begin; not per crawl)
The registry requirement "integration:<name>" (gsc, gsc_analytics, gsc_inspection, ga4, psi) is answered by
requirement() below, registered when this package is imported.

Build 4 stored sources (STORED_SOURCES: ai, embeddings, languagetool, link_metrics, green_web, logs) use one entry
shape, written with put_result(crawler, source, payload) and read with get_result(site_or_crawler, source); they are
saved and reopened with the crawl like gsc / ga4 / psi, and merge_state merges them per URL (newer wins per URL):
  {"source", "connected": bool, "reason", "status", "fetched_at", "results": {url: ...}, "not_checked": {url: why}}
  (green_web adds "hosts": {host: {"green", "hosted_by", "checked_at"}}, merged per host, its not_checked keyed by
  host; link_metrics adds "providers": {provider: {...}}, merged per provider, and merges its results per page AND
  per provider itself before put_result(merge=False) - see integrations/linkmetrics.py)
A URL with a result is never also "not checked": a later run that skipped it keeps the earlier result.
"""
import copy
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ga4, google_oauth, gsc, linkmetrics, psi, sheets, store
# AuthExpired / QuotaExceeded are re-exported for web/app.py (except I.AuthExpired ...)
from .common import (JOBS, AuthExpired, IntegrationError, Job, NotConnected, QuotaExceeded,  # noqa: F401
                     Stopped, now_iso)

SOURCES = ("gsc", "ga4", "psi")
# Build 4 sources stored per crawl in the one entry shape (PLAN4_build.md 10.3), saved with the crawl
STORED_SOURCES = SOURCES + ("ai", "embeddings", "languagetool", "link_metrics", "green_web", "logs")
NAMES = {"gsc": "Google Search Console", "ga4": "Google Analytics 4", "psi": "PageSpeed Insights"}
NOT_CONNECTED = "Not connected - connect it under Configuration > API Access"
MODULES = {"gsc": gsc, "ga4": ga4, "psi": psi}
FETCHED = ("done", "partial", "stopped")
_live: Dict[str, Tuple[float, Dict[str, Any]]] = {}      # short cache of connection() for the tab resolver
_live_lock = threading.Lock()
_store_lock = threading.RLock()                         # put_result: read-merge-write of crawler.integrations


# ---------------------------------------------------------------------------------------------
# Change listeners: a connect / disconnect / property choice changes what the tabs say ("Not connected" ->
# "not fetched yet"), so the app rebuilds the current crawl's index (add_listener(lambda: refresh(crawler)))
# ---------------------------------------------------------------------------------------------
_listeners: List[Callable[[], None]] = []


def add_listener(fn: Callable[[], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def _changed() -> None:
    with _live_lock:
        _live.clear()
    for fn in list(_listeners):
        try:
            fn()
        except Exception:
            pass


def refresh(crawler) -> None:
    """Rebuild this crawl's tabs on the next request (after a connection change)."""
    if crawler is not None:
        _invalidate(crawler)


# ---------------------------------------------------------------------------------------------
# Live connection state (server-wide)
# ---------------------------------------------------------------------------------------------
def _google_account_for(source: str) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """(account with secrets, selection) used for a Google source: the selected account, else the only account
    that granted the source's scope."""
    sel = store.selection(source)
    acc = store.get_account(sel.get("account_id") or "") if sel.get("account_id") else None
    if acc is None:
        usable = [a for a in store.accounts() if google_oauth.has_scope(a, source)]
        acc = usable[0] if len(usable) == 1 else None
    return acc, sel


def connection(source: str) -> Dict[str, Any]:
    """{"connected": bool, "reason": str, ...} for one source, right now (no network call)."""
    if source == "psi":
        key, where = store.psi_key()
        settings = store.psi_settings()
        if not key:
            return {"connected": False, "reason": "Not connected - add a free PageSpeed Insights API key under "
                                                  "Configuration > API Access > PageSpeed Insights.",
                    "key": {"set": False, "source": ""}}
        if not settings["connected"]:
            return {"connected": False, "reason": NOT_CONNECTED, "key": {"set": True, "source": where}}
        return {"connected": True, "reason": "", "key": {"set": True, "source": where},
                "auto_connect": settings["auto_connect"]}
    if source == "sheets":
        return sheets.connection(_sheets_account_hint())
    if source not in ("gsc", "ga4"):
        return {"connected": False, "reason": NOT_CONNECTED}
    acc, sel = _google_account_for(source)
    if acc is None:
        if store.accounts():
            return {"connected": False, "reason": f"Choose the Google account for {NAMES[source]} under "
                                                  "Configuration > API Access."}
        return {"connected": False, "reason": NOT_CONNECTED}
    pub = store.public_account(acc)
    if acc.get("needs_reconnect"):
        return {"connected": False, "account": pub,
                "reason": "The Google sign-in has expired or was removed: connect the account again under "
                          "Configuration > API Access."}
    if not google_oauth.has_scope(acc, source):
        return {"connected": False, "account": pub,
                "reason": f"The Google account did not allow access to {NAMES[source]}: sign in again and tick "
                          "that permission."}
    prop = sel.get("property") or ""
    if not prop:
        what = "Search Console property" if source == "gsc" else "GA4 property and data stream"
        return {"connected": False, "account": pub, "reason": f"Connected to Google, but no {what} is chosen "
                                                              "(Configuration > API Access)."}
    return {"connected": True, "reason": "", "account": pub, "selection": sel,
            "auto_connect": bool(acc.get("auto_connect"))}


def _sheets_account_hint() -> str:
    """The Google account chosen in Connections > Google Sheets ("" = the one that allowed Sheets)."""
    return str(sheets._connector_settings().get("account") or "")


def status(base_url: str = "") -> Dict[str, Any]:
    """Everything the API Access dialog shows (no secrets): per source its state, accounts, selection and the
    running job; the OAuth client set-up help; the link-metric providers (Moz, Majestic, Ahrefs connectors:
    connected or not, units used this month against the cap)."""
    cid, _, client_source = store.client()
    redirect = google_oauth.redirect_uri_for(base_url) if base_url else ""
    out: Dict[str, Any] = {
        "google": {"client": {"set": bool(cid), "source": client_source}, "accounts": store.public_accounts(),
                   "redirect_uri": redirect, "setup": google_oauth.setup_help(redirect)},
        "sources": {}, "link_metrics": linkmetrics.status(), "jobs": JOBS.all(),
    }
    try:                       # ARACHNIX 1.0: sources the page hides (crawler/seo/registry.py V2_FEATURES)
        from ..seo.registry import hidden_api_sources
        out["hidden_sources"] = hidden_api_sources()
    except Exception:
        out["hidden_sources"] = ["majestic", "ahrefs", "moz"]
    for s in SOURCES:
        c = connection(s)
        c.pop("selection", None)
        entry = {"name": NAMES[s], **c, "job": (JOBS.get(s).public() if JOBS.get(s) else None),
                 "options": effective_options(s)}
        if s in ("gsc", "ga4"):
            entry["selection"] = store.selection(s)
        out["sources"][s] = entry
    out["sheets"] = connection("sheets")
    out["ga4_metrics"] = ga4.catalog()
    out["psi_filters"] = [{"id": f.id, "name": f.name, "audits": list(f.audits)} for f in psi.AUDIT_FILTERS]
    return out


# ---------------------------------------------------------------------------------------------
# Google accounts
# ---------------------------------------------------------------------------------------------
def google_begin(base_url: str = "", sources=("gsc", "ga4"), redirect_uri: str = "",
                 login_hint: str = "") -> Dict[str, Any]:
    """Connect with Google: sources gsc, ga4 and / or sheets (Google Sheets export and list import; Google keeps
    what the account allowed before, so "sheets" alone adds the Sheets permissions to an existing sign-in)."""
    ruri = redirect_uri or google_oauth.redirect_uri_for(base_url or "http://localhost:8000")
    wanted = [s for s in (sources or ("gsc", "ga4")) if s in google_oauth.SIGN_IN_SOURCES] or ["gsc", "ga4"]
    return google_oauth.begin(ruri, wanted, login_hint)


def _auto_select(account: Dict[str, Any]) -> None:
    """After a sign-in: use this account for each Google source that has no account yet."""
    for s in ("gsc", "ga4"):
        sel = store.selection(s)
        if (not sel.get("account_id") or store.get_account(sel["account_id"]) is None) \
                and s in [x for x in ("gsc", "ga4") if google_oauth.SOURCE_SCOPES[x] in (account.get("scopes") or [])]:
            store.set_selection(s, {"account_id": account["id"]})


def google_callback(state: str, code: str = "", error: str = "") -> Dict[str, Any]:
    acc = google_oauth.finish(state, code, error)
    _auto_select(acc)
    _changed()
    return acc


def google_paste(url: str) -> Dict[str, Any]:
    acc = google_oauth.finish_from_url(url)
    _auto_select(acc)
    _changed()
    return acc


def add_service_account(json_text: str, name: str = "") -> Dict[str, Any]:
    acc = google_oauth.add_service_account(json_text, name)
    _auto_select(acc)
    _changed()
    return acc


def set_google_client(client_id: str, client_secret: str) -> Dict[str, Any]:
    cid, secret = str(client_id or "").strip(), str(client_secret or "").strip()
    if not cid or not secret:
        store.clear_client()
    else:
        if not cid.endswith(".apps.googleusercontent.com"):
            raise IntegrationError("That does not look like a Google OAuth client ID (it ends with "
                                   ".apps.googleusercontent.com).")
        store.set_client(cid, secret)
    _, _, where = store.client()
    _changed()
    return {"set": bool(where), "source": where}


def update_account(account_id: str, name: Optional[str] = None, auto_connect: Optional[bool] = None) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    if name is not None:
        fields["name"] = str(name).strip()[:80] or None
    if auto_connect is not None:
        fields["auto_connect"] = bool(auto_connect)
    acc = store.update_account(account_id, **fields)
    if acc is None:
        raise IntegrationError("No such Google account.")
    _changed()
    return store.public_account(acc)


def disconnect_account(account_id: str) -> Dict[str, Any]:
    """Revoke at Google + delete here (the sources that used it become Not connected)."""
    out = google_oauth.revoke(account_id)
    _changed()
    return out


def _client_for(source: str, account_id: str = "", job: Optional[Job] = None) -> google_oauth.GoogleClient:
    if account_id:
        acc = store.get_account(account_id)
    else:
        acc, _ = _google_account_for(source)
    if acc is None:
        raise NotConnected(NOT_CONNECTED)
    on_wait = (lambda msg: job.progress(message=msg)) if job is not None else None
    return google_oauth.GoogleClient(acc["id"], job.stop_event if job is not None else None, on_wait)


def gsc_properties(account_id: str = "") -> List[Dict[str, Any]]:
    return gsc.list_properties(_client_for("gsc", account_id))


def ga4_accounts(account_id: str = "") -> List[Dict[str, Any]]:
    return ga4.list_accounts(_client_for("ga4", account_id))


def ga4_streams(prop: str, account_id: str = "") -> List[Dict[str, Any]]:
    return ga4.list_streams(_client_for("ga4", account_id), ga4.check_property(prop))


def select(source: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Saves the account / property (/ GA4 account, stream) choice of a Google source."""
    if source not in ("gsc", "ga4"):
        raise IntegrationError("Unknown source.")
    values = dict(values or {})
    acc_id = str(values.get("account_id") or "")
    acc = store.get_account(acc_id) if acc_id else _google_account_for(source)[0]
    if acc is None:
        raise IntegrationError("Choose a connected Google account first.")
    sel: Dict[str, Any] = {"account_id": acc["id"]}
    if source == "gsc":
        sel["property"] = str(values.get("property") or "").strip()
    else:
        prop = str(values.get("property") or "").strip()
        sel["property"] = ga4.check_property(prop) if prop else ""
        for k in ("account", "property_name", "stream", "stream_name", "stream_uri"):
            if values.get(k):
                sel[k] = str(values[k]).strip()[:300]
    if "auto_connect" in values:
        store.update_account(acc["id"], auto_connect=bool(values["auto_connect"]))
    out = store.set_selection(source, sel)
    _changed()
    return out


def psi_connect(key: str = "", auto_connect: Optional[bool] = None) -> Dict[str, Any]:
    """Saves the PageSpeed key (unless the env key is used) and marks PSI connected. The key is never returned."""
    key = str(key or "").strip()
    fields: Dict[str, Any] = {"connected": True}
    if key and key not in ("***",):
        if len(key) < 20 or any(c.isspace() for c in key):
            raise IntegrationError("That does not look like a Google API key.")
        fields["api_key"] = key
    if auto_connect is not None:
        fields["auto_connect"] = bool(auto_connect)
    store.set_psi(**fields)
    _changed()
    return connection("psi")


def psi_disconnect() -> Dict[str, Any]:
    store.clear_psi_key()
    store.set_psi(connected=False)
    _changed()
    return connection("psi")


# ---------------------------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------------------------
def effective_options(source: str, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Module defaults <- server settings (settings_store page "integrations") <- per-request overrides."""
    values: Dict[str, Any] = {}
    try:
        from .. import settings_store
        saved = (settings_store.effective("integrations") or {}).get(source) or {}
        if isinstance(saved, dict):
            values.update(saved)
    except Exception:
        pass
    values.update(dict(overrides or {}))
    return MODULES[source].options(values)


# ---------------------------------------------------------------------------------------------
# Per-crawl state
# ---------------------------------------------------------------------------------------------
def ensure(crawler) -> Dict[str, Any]:
    data = getattr(crawler, "integrations", None)
    if not isinstance(data, dict):
        data = {}
        setattr(crawler, "integrations", data)
    return data


def _invalidate(crawler) -> None:
    """Make the next site_index() rebuild so the tabs see the new data."""
    lock = getattr(crawler, "_index_lock", None)
    try:
        if lock is not None:
            with lock:
                crawler._index_cache = None
        elif hasattr(crawler, "_index_cache"):
            crawler._index_cache = None
    except Exception:
        pass


def apply(crawler, source: str, payload: Dict[str, Any]) -> None:
    """Store a fetch result on the crawler (merged with what an earlier fetch for this crawl found) and refresh
    the tabs."""
    data = ensure(crawler)
    merged = merge_state({source: data.get(source)} if data.get(source) else {}, {source: payload})
    data[source] = merged.get(source, payload)
    _invalidate(crawler)


def _newer(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return str(b.get("fetched_at") or "") >= str(a.get("fetched_at") or "")


def merge_state(old: Optional[Dict[str, Any]], new: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge two crawler.integrations dicts (saved crawl + fresh fetch): the newer fetch wins per source; GSC URL
    Inspection results and PSI results are merged per URL (newer wins) when they used the same property / device."""
    out = copy.deepcopy(old or {})
    for source, payload in (new or {}).items():
        if not isinstance(payload, dict):
            continue
        prev = out.get(source)
        if not isinstance(prev, dict) or not prev:
            out[source] = copy.deepcopy(payload)
            continue
        first, second = (prev, payload) if _newer(prev, payload) else (payload, prev)
        merged = copy.deepcopy(second)
        if source == "gsc":
            a = (first.get("inspection") or {})
            b = (merged.get("inspection") or {})
            if a.get("results") and first.get("property") == merged.get("property"):
                results = dict(a.get("results") or {})
                results.update(b.get("results") or {})
                skipped = {u: w for u, w in dict(a.get("skipped") or {}, **(b.get("skipped") or {})).items()
                           if u not in results}
                b = dict(b, results=results, skipped=skipped, inspected=len(results))
                merged["inspection"] = b
            if not (merged.get("analytics") or {}).get("status") and first.get("analytics"):
                merged["analytics"] = first["analytics"]
        elif source == "psi":
            if first.get("device") == merged.get("device"):
                results = dict(first.get("results") or {})
                for u, r in (merged.get("results") or {}).items():
                    if r.get("status") == "skipped" and (results.get(u) or {}).get("status") in ("success", "error"):
                        continue          # a stopped run does not erase an earlier result
                    results[u] = r
                merged["results"] = results
        elif source in STORED_SOURCES:
            merged = _merge_entry(first, merged)
        out[source] = merged
    return out


def _merge_entry(older: Dict[str, Any], newer: Dict[str, Any]) -> Dict[str, Any]:
    """Two runs of a Build 4 source in the one entry shape: the newer run's fields win, results merge per URL
    (newer wins per URL), a URL with a result is never "not checked", and green_web hosts merge per host."""
    out = copy.deepcopy(newer)
    results = dict(older.get("results") or {}) if isinstance(older.get("results"), dict) else {}
    if isinstance(newer.get("results"), dict):
        results.update(copy.deepcopy(newer["results"]))
    out["results"] = results
    skipped: Dict[str, Any] = {}
    for part in (older.get("not_checked"), newer.get("not_checked")):
        if isinstance(part, dict):
            skipped.update(part)
    out["not_checked"] = {u: why for u, why in skipped.items() if u not in results}
    if isinstance(older.get("hosts"), dict) or isinstance(newer.get("hosts"), dict):
        hosts = dict(older.get("hosts") or {}) if isinstance(older.get("hosts"), dict) else {}
        hosts.update(copy.deepcopy(newer.get("hosts") or {}) if isinstance(newer.get("hosts"), dict) else {})
        out["hosts"] = hosts
        # green_web keys not_checked by host: a host that was looked up is never also "not checked"
        out["not_checked"] = {k: why for k, why in out["not_checked"].items() if k not in hosts}
    if isinstance(older.get("providers"), dict) or isinstance(newer.get("providers"), dict):
        provs = dict(older.get("providers") or {}) if isinstance(older.get("providers"), dict) else {}
        provs.update(copy.deepcopy(newer.get("providers") or {}) if isinstance(newer.get("providers"), dict) else {})
        out["providers"] = provs           # link_metrics: per provider, the newer run wins
    return out


def put_result(crawler, source: str, payload: Dict[str, Any], merge: bool = True) -> None:
    """Store one Build 4 source's result on the crawler (saved with the crawl) and rebuild the tabs. merge=True
    merges it with what an earlier run for this crawl found (merge_state: per URL, newer wins); merge=False
    replaces it (the caller already merged)."""
    if source not in STORED_SOURCES:
        raise IntegrationError(f"Unknown stored source: {source}")
    if not isinstance(payload, dict):
        raise IntegrationError("A stored result must be a JSON object.")
    entry = copy.deepcopy(payload)
    entry.setdefault("source", source)
    entry.setdefault("fetched_at", now_iso())
    with _store_lock:
        data = ensure(crawler)
        prev = data.get(source)
        if merge and isinstance(prev, dict) and prev:
            data[source] = merge_state({source: prev}, {source: entry}).get(source, entry)
        else:
            data[source] = entry
    with _live_lock:
        _live.clear()
    _invalidate(crawler)


def get_result(site_or_crawler, source: str) -> Optional[Dict[str, Any]]:
    """A copy of the stored entry of one source for this crawl (site.integrations or crawler.integrations), or
    None when nothing was stored."""
    data = getattr(site_or_crawler, "integrations", None)
    entry = data.get(source) if isinstance(data, dict) else None
    return copy.deepcopy(entry) if isinstance(entry, dict) and entry else None


def export_state(crawler) -> Dict[str, Any]:
    """JSON-ready copy of crawler.integrations (no secrets are ever in it) for the saved-crawl store."""
    return copy.deepcopy(getattr(crawler, "integrations", None) or {})


def import_state(crawler, data: Optional[Dict[str, Any]]) -> None:
    """Put a saved crawl's integration data back on the crawler (every stored source), merged with what is
    there."""
    if isinstance(data, dict) and data:
        current = ensure(crawler)
        merged = merge_state(current, {k: v for k, v in data.items() if k in STORED_SOURCES and isinstance(v, dict)})
        current.clear()
        current.update(merged)
        _invalidate(crawler)


# ---------------------------------------------------------------------------------------------
# Fetch jobs
# ---------------------------------------------------------------------------------------------
def _base_payload(source: str, opts: Dict[str, Any], conn: Dict[str, Any]) -> Dict[str, Any]:
    acc = conn.get("account") or {}
    sel = conn.get("selection") or store.selection(source)
    return {"source": source, "connected": True, "reason": "", "status": "running", "fetched_at": now_iso(),
            "account": acc.get("name") or acc.get("email") or "", "property": sel.get("property") or "",
            "options": {k: v for k, v in opts.items()}}


def _site(crawler):
    try:
        site, _ = crawler.site_index(force=True)
        return site
    except Exception:
        return None


def _pages(site) -> List[Any]:
    from ..seo.tabs.internal import category_of, is_html_doc
    return [r for r in (getattr(site, "pages", None) or []) if is_html_doc(r) and category_of(r) == "2xx"]


def _fail(crawler, source: str, message: str) -> None:
    """Record a failed fetch without losing data an earlier fetch found."""
    with _live_lock:
        _live.clear()
    data = ensure(crawler)
    prev = data.get(source)
    if isinstance(prev, dict) and prev.get("status") in FETCHED:
        prev["last_error"] = message
    else:
        data[source] = {"source": source, "connected": True, "status": "error", "fetched_at": now_iso(),
                        "reason": f"The last {NAMES[source]} fetch failed: {message}", "last_error": message}
    _invalidate(crawler)


def _gsc_work(job: Job, crawler, opts: Dict[str, Any], urls: Optional[List[str]]) -> None:
    conn = connection("gsc")
    if not conn["connected"]:
        raise NotConnected(conn["reason"])
    client = _client_for("gsc", conn["account"]["id"], job)
    prop = conn["selection"]["property"]
    payload = _base_payload("gsc", opts, conn)
    try:
        job.progress("Search Analytics", 0, 0, "Search Analytics: reading pages")
        sa = gsc.search_analytics(client, prop, opts, job)
        payload["analytics"] = sa
        payload["date_start"], payload["date_end"] = sa["start"], sa["end"]
    except Stopped:
        payload["analytics"] = {"rows": [], "status": "stopped", "message": "Stopped before any data arrived."}
    payload["inspection"] = {"enabled": bool(opts["url_inspection"]), "results": {}, "skipped": {},
                             "status": "off" if not opts["url_inspection"] else "running"}
    payload["status"] = "stopped" if job.stopped else "done"
    apply(crawler, "gsc", dict(payload))
    if opts["url_inspection"] and not job.stopped:
        site = _site(crawler)
        prev = ((getattr(crawler, "integrations", None) or {}).get("gsc") or {}).get("inspection") or {}
        if urls:
            targets = list(urls)
        else:
            pages = _pages(site) if site is not None else []
            if opts["inspect_skip_nonindexable"] and site is not None:
                pages = [r for r in pages if site.indexable.get(r.url)]
            targets = [r.url for r in pages]
        by_prop: Dict[str, List[str]] = {}
        if opts["multiple_properties"]:
            props = [p["site_url"] for p in gsc.list_properties(client)]
            for u in targets:
                p = gsc.best_property(props, u)
                if p:
                    by_prop.setdefault(p, []).append(u)
        else:
            by_prop[prop] = [u for u in targets if gsc.property_covers(prop, u)]
        covered = {u for us in by_prop.values() for u in us}
        res = gsc.inspect(client, by_prop, opts, job, done=prev.get("results") or {})
        for u in targets:
            if u not in covered:
                res["skipped"][u] = "Not inspected: the URL is not inside the chosen Search Console property."
        payload["inspection"] = {"enabled": True, "results": res["results"], "skipped": res["skipped"],
                                 "quota_hit": res["quota_hit"], "inspected": res["inspected"],
                                 "status": "stopped" if job.stopped else "done",
                                 "message": ("Today's URL Inspection quota was reached; the rest can be inspected "
                                             "tomorrow." if res["quota_hit"] else "")}
        payload["fetched_at"] = now_iso()
        apply(crawler, "gsc", payload)
        if res["quota_hit"]:
            job.message = payload["inspection"]["message"]
    job.progress("Done", job.total or 1, job.total or 1, job.message or "Search Console data fetched.")


def _ga4_work(job: Job, crawler, opts: Dict[str, Any], urls: Optional[List[str]]) -> None:
    conn = connection("ga4")
    if not conn["connected"]:
        raise NotConnected(conn["reason"])
    sel = conn["selection"]
    client = _client_for("ga4", conn["account"]["id"], job)
    origin = ga4.origin_for(sel, getattr(crawler, "start_url", "") or "")
    if not origin:
        raise IntegrationError("No web address to join GA4 paths onto: choose a web data stream.")
    payload = _base_payload("ga4", opts, conn)
    payload.update(property_name=sel.get("property_name") or "", stream_uri=sel.get("stream_uri") or "",
                   origin=origin, metrics=list(opts["metrics"]))
    try:
        rep = ga4.fetch_report(client, sel["property"], opts, origin, job)
    except Stopped:
        rep = {"rows": [], "status": "stopped", "message": "Stopped before any data arrived."}
    payload.update(rows=rep["rows"], row_count=len(rep["rows"]), truncated=rep.get("truncated", False),
                   date_start=rep.get("start", ""), date_end=rep.get("end", ""),
                   quota_left_today=rep.get("quota_left_today"), status="stopped" if job.stopped else "done")
    apply(crawler, "ga4", payload)
    job.progress("Done", 1, 1, f"GA4: {len(rep['rows']):,} pages read.")


def psi_targets(crawler, opts: Dict[str, Any], urls: Optional[List[str]] = None) -> List[str]:
    """URLs to test: the given ones, else the crawl's 2xx HTML pages (indexable only by default) in crawl order,
    skipping pages already tested with this device when only_missing, capped at max_urls."""
    prev = (getattr(crawler, "integrations", None) or {}).get("psi") or {}
    done = (prev.get("results") or {}) if prev.get("device") == opts["device"] else {}
    if urls:
        targets = [u for u in dict.fromkeys(urls) if u]
    else:
        site = _site(crawler)
        pages = _pages(site) if site is not None else []
        if opts["indexable_only"] and site is not None:
            pages = [r for r in pages if site.indexable.get(r.url)]
        targets = [r.url for r in pages]
        if opts["only_missing"]:
            targets = [u for u in targets if (done.get(u) or {}).get("status") != "success"]
    return targets[:opts["max_urls"]]


def _psi_work(job: Job, crawler, opts: Dict[str, Any], urls: Optional[List[str]]) -> None:
    conn = connection("psi")
    if not conn["connected"]:
        raise NotConnected(conn["reason"])
    key, _ = store.psi_key()
    targets = psi_targets(crawler, opts, urls)
    payload = {"source": "psi", "connected": True, "reason": "", "status": "running", "fetched_at": now_iso(),
               "device": opts["device"], "categories": list(opts["categories"]), "options": dict(opts)}
    if not targets:
        payload.update(status="done", results={})
        apply(crawler, "psi", payload)
        job.progress("Done", 0, 0, "Every page was already tested (or there is no page to test).")
        return
    auth = getattr(getattr(crawler, "config", None), "auth", None)
    login_check = psi.needs_login if auth is not None and getattr(auth, "enabled", False) else None
    try:
        res = psi.run(targets, opts, key, job, login_check)
    except QuotaExceeded as e:
        _fail(crawler, "psi", str(e))
        raise
    payload.update(results=res["results"], quota_hit=res["quota_hit"],
                   status="stopped" if job.stopped else "done", fetched_at=now_iso())
    if res["quota_hit"]:
        payload["last_error"] = res.get("quota_message") or psi.QUOTA_LEFT
    apply(crawler, "psi", payload)
    ok = sum(1 for r in res["results"].values() if r.get("status") == "success")
    msg = f"PageSpeed: {ok:,} of {len(targets):,} pages tested."
    if res["quota_hit"]:
        msg += " " + (res.get("quota_message") or psi.QUOTA_LEFT)
    job.progress("Done", len(targets), len(targets), msg)


WORK = {"gsc": _gsc_work, "ga4": _ga4_work, "psi": _psi_work}


def start_fetch(source: str, crawler, options: Optional[Dict[str, Any]] = None, urls: Optional[List[str]] = None,
                on_done: Optional[Callable[[Any, str, Job], None]] = None) -> Dict[str, Any]:
    """Starts a background fetch for one source and returns the job's public view. Refuses when the source is
    not connected, there is no crawl, or a fetch of that source is running."""
    if source not in WORK:
        raise IntegrationError("Unknown source.")
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise IntegrationError("Crawl a site first: the data is matched to the crawled URLs.")
    conn = connection(source)
    if not conn["connected"]:
        raise NotConnected(conn["reason"])
    opts = effective_options(source, options)

    def work(job: Job) -> None:
        try:
            WORK[source](job, crawler, opts, urls)
        except Stopped:
            raise
        except IntegrationError as e:
            _fail(crawler, source, str(e))
            raise
        finally:
            if on_done is not None:
                try:
                    on_done(crawler, source, job)
                except Exception:
                    pass

    return JOBS.start(source, work, owner=crawler).public()


def stop_fetch(source: str) -> Optional[Dict[str, Any]]:
    job = JOBS.stop(source)
    return job.public() if job is not None else None


def stop_all(except_crawler=None) -> List[str]:
    """Stop every running fetch (used on reset / close / a new crawl so a replaced crawl's fetch stops burning
    Google quota and stops blocking the new crawl). Keeps a fetch serving `except_crawler`. The data fetched so
    far stays on the crawl it belongs to. Returns the sources stopped."""
    return JOBS.stop_all(except_owner=except_crawler)


def stop_for_crawler(crawler) -> List[str]:
    """Stop every running fetch tied to `crawler` (that crawl was replaced or closed). Returns the sources."""
    return JOBS.stop_for(crawler)


def jobs(crawler=None) -> Dict[str, Dict[str, Any]]:
    """Public view of every source's job. With `crawler`, a fetch still running for a replaced crawl is hidden
    (the crawl on screen never sees another crawl's live fetch)."""
    return JOBS.all(owner=crawler)


def auto_fetch(crawler, on_done: Optional[Callable[[Any, str, Job], None]] = None) -> List[str]:
    """"Auto Connect on Start": after a crawl finishes, fetch every connected source marked auto-connect
    (PageSpeed also when the server setting integrations.psi.enabled is on). Returns the sources started."""
    started = []
    for s in SOURCES:
        c = connection(s)
        auto = c.get("auto_connect") or (s == "psi" and effective_options("psi").get("enabled"))
        if c.get("connected") and auto and not JOBS.running_for(s, crawler):
            try:
                start_fetch(s, crawler, on_done=on_done)
                started.append(s)
            except IntegrationError:
                continue
    return started


def discovered_urls(target, source: str) -> List[str]:
    """URLs the source knows that the crawl did not find (inside the crawl's scope): for "Crawl New URLs
    Discovered In GSC / GA4" (feed them to List mode / the queue) and the Orphan Pages report."""
    site = target
    if hasattr(target, "site_index"):
        site = _site(target)
    if site is None:
        return []
    if source == "gsc":
        return list(gsc.joined(site)["orphans"].keys())
    if source == "ga4":
        return list(ga4.joined(site)["orphans"].keys())
    return []


# ---------------------------------------------------------------------------------------------
# Requirement "integration:<name>" for the registry ("Not connected" / "not fetched yet")
# ---------------------------------------------------------------------------------------------
def _live_connection(source: str) -> Dict[str, Any]:
    with _live_lock:
        hit = _live.get(source)
        if hit and time.monotonic() - hit[0] < 2.0:
            return hit[1]
    try:
        c = connection(source)
    except Exception:
        c = {"connected": False, "reason": NOT_CONNECTED}
    with _live_lock:
        _live[source] = (time.monotonic(), c)
    return c


def _not_fetched(source: str) -> str:
    job = JOBS.get(source)
    if job is not None and job.status == "running":
        return f"{NAMES[source]} data is being fetched now ({job.done:,} of {job.total:,}); it appears when done."
    c = _live_connection(source)
    if not c.get("connected"):
        return c.get("reason") or NOT_CONNECTED
    action = "Run" if source == "psi" else "Fetch"
    return (f"{NAMES[source]} is connected, but no data was fetched for this crawl yet: press {action} under "
            f"Configuration > API Access > {NAMES[source]}.")


def requirement(site, arg: str, rows=()) -> str:
    """"" when the integration's data is there for this crawl, else why its checks are Not checked."""
    info = (getattr(site, "integrations", None) or {})
    base = arg.split("_", 1)[0] if arg.startswith("gsc_") else arg
    if base not in SOURCES:
        entry = info.get(arg)
        if isinstance(entry, dict) and entry.get("connected"):
            return str(entry.get("reason") or "")
        return NOT_CONNECTED
    entry = info.get(base)
    entry = entry if isinstance(entry, dict) else {}
    fetched = entry.get("status") in FETCHED
    if not fetched:
        if entry.get("status") == "error":
            return str(entry.get("reason") or _not_fetched(base))
        return _not_fetched(base)
    if base == "psi":
        if not (entry.get("results") or {}):
            return "PageSpeed Insights ran for this crawl but had no page to test."
        if not any((r or {}).get("status") == "success" for r in entry["results"].values()):
            first = next(iter(entry["results"].values()))
            return f"PageSpeed Insights could not test any page: {psi.request_status(first)}"
        return ""
    if arg == "gsc_analytics":
        sa = entry.get("analytics") or {}
        if sa.get("status") in ("done", "stopped"):
            return ""
        return "Search Analytics data was not fetched for this crawl."
    if arg == "gsc_inspection":
        insp = entry.get("inspection") or {}
        if insp.get("results"):
            return ""
        if not insp.get("enabled"):
            return ("URL Inspection is off (Configuration > API Access > Google Search Console > URL Inspection: "
                    "Enable URL Inspection), so Google's index status was not checked.")
        if insp.get("quota_hit"):
            return "No URL was inspected: today's URL Inspection quota (2,000 per property) is used up."
        return "No URL of this crawl could be inspected" + (f": {insp.get('message')}" if insp.get("message") else ".")
    return ""


def _register() -> None:
    try:
        from ..seo.registry import register_requirement
        register_requirement("integration", requirement)
    except Exception:
        pass


_register()


# ---------------------------------------------------------------------------------------------
# Helpers for the checks, Fix plan, Excel and PDF
# ---------------------------------------------------------------------------------------------
def judge(tab_id: str, filter_id: str, site) -> Optional[Dict[str, Any]]:
    """For crawler/intel/checks.py: which rows a Search Console / Analytics / PageSpeed check could judge.
    {"test": fn(r) -> bool, "reason": str (none could be judged), "caveat": str with {unknown} / {total}}.
    None = every row of the tab was judged (nothing special)."""
    if tab_id == "pagespeed":
        if filter_id == "core_web_vitals":
            return {"test": lambda r: psi.cwv_assessment(psi.result_for(site, getattr(r, "url", ""))) != "",
                    "reason": "No tested page has real-visitor (Chrome UX Report) data of its own, so Core Web "
                              "Vitals could not be assessed per page.",
                    "caveat": "{unknown} of {total} pages have no real-visitor data of their own (or were not tested), "
                              "so they were not assessed."}
        f = psi.FILTERS_BY_ID.get(filter_id)
        if f is None:
            return None
        return {"test": lambda r: psi.filter_state(psi.result_for(site, getattr(r, "url", "")), f) is not None,
                "reason": "PageSpeed Insights did not test any page of this crawl for this check.",
                "caveat": "{unknown} of {total} pages were not tested by PageSpeed Insights, so they were not checked."}
    if tab_id == "search_console":
        if filter_id in ("url_not_on_google", "indexable_not_indexed", "on_google_has_issues",
                         "canonical_not_selected", "rich_result_invalid", "not_mobile_friendly"):
            insp = gsc.joined(site)["inspection"]
            if filter_id == "not_mobile_friendly":
                return {"test": lambda r: bool((insp.get(getattr(r, "url", "")) or {}).get("mobile")),
                        "reason": "Google no longer reports mobile usability (retired in December 2023), so no page "
                                  "had a mobile-friendly verdict.",
                        "caveat": "{unknown} of {total} pages had no mobile-friendly verdict from Google."}
            return {"test": lambda r: getattr(r, "url", "") in insp,
                    "reason": "No page of this crawl was inspected with the URL Inspection API.",
                    "caveat": "{unknown} of {total} pages were not inspected (URL Inspection allows 2,000 per property "
                              "a day), so they were not checked."}
        return None
    return None


def traffic(site, url: str) -> Dict[str, Any]:
    """{"clicks", "impressions", "position", "sessions"} for a crawled URL (None where not connected / no data)."""
    out: Dict[str, Any] = {"clicks": None, "impressions": None, "position": None, "sessions": None}
    try:
        if gsc.data_of(site).get("status") in FETCHED:
            row = gsc.joined(site)["rows"].get(url)
            out.update(clicks=row["clicks"] if row else 0, impressions=row["impressions"] if row else 0,
                       position=row["position"] if row else None)
        if ga4.data_of(site).get("status") in FETCHED:
            row = ga4.joined(site)["rows"].get(url)
            out["sessions"] = (row or {}).get("sessions") or 0
    except Exception:
        pass
    return out


def traffic_weight(site, url: str) -> float:
    """A ranking boost for the Fix plan: 1.0 without data; up to 3.0 for the pages with the most clicks /
    sessions (log scale), so problems on pages that get traffic come first."""
    import math
    t = traffic(site, url)
    best = max(float(t.get("clicks") or 0), float(t.get("sessions") or 0) / 2.0)
    return 1.0 + min(2.0, math.log10(1.0 + best) / 2.0) if best > 0 else 1.0


def report_summary(site, top: int = 10) -> List[Dict[str, Any]]:
    """One compact table per source for the PDF / Excel. A source without data gives a row with "connected":
    False and its reason, so the report says "Not connected" instead of showing zeros."""
    out: List[Dict[str, Any]] = []
    info = getattr(site, "integrations", None) or {}

    def missing(source: str) -> Dict[str, Any]:
        return {"source": source, "title": NAMES[source], "connected": False,
                "note": requirement(site, source), "columns": [], "rows": [], "totals": {}}

    if (info.get("gsc") or {}).get("status") in FETCHED:
        j = gsc.joined(site)
        rows = sorted(j["rows"].items(), key=lambda kv: (-kv[1]["clicks"], -kv[1]["impressions"]))
        non_idx = [u for u, v in j["rows"].items() if v["impressions"] > 0 and not site.indexable.get(u, False)]
        d = info["gsc"]
        out.append({"source": "gsc", "title": f"Search Console ({d.get('date_start', '')} to {d.get('date_end', '')})",
                    "connected": True, "note": d.get("property", ""),
                    "columns": ["Page", "Clicks", "Impressions", "CTR", "Position"],
                    "rows": [[u, v["clicks"], v["impressions"], round(v["ctr"] * 100, 1), v["position"]]
                             for u, v in rows[:top]],
                    "totals": {"pages_with_clicks": sum(1 for _, v in rows if v["clicks"] > 0),
                               "clicks": sum(v["clicks"] for _, v in rows),
                               "non_indexable_with_impressions": len(non_idx), "orphans": len(j["orphans"]),
                               "inspected": len(j["inspection"])}})
    else:
        out.append(missing("gsc"))
    if (info.get("ga4") or {}).get("status") in FETCHED:
        j = ga4.joined(site)
        rows = sorted(j["rows"].items(), key=lambda kv: -(kv[1].get("sessions") or 0))
        d = info["ga4"]
        out.append({"source": "ga4",
                    "title": f"Google Analytics 4 ({d.get('date_start', '')} to {d.get('date_end', '')})",
                    "connected": True, "note": d.get("property_name") or d.get("property", ""),
                    "columns": ["Page", "Sessions", "Engagement Rate", "Views"],
                    "rows": [[u, v.get("sessions") or 0, v.get("engagementRate"), v.get("screenPageViews")]
                             for u, v in rows[:top]],
                    "totals": {"sessions": sum(v.get("sessions") or 0 for _, v in rows),
                               "pages_with_sessions": sum(1 for _, v in rows if (v.get("sessions") or 0) > 0),
                               "orphans": len(j["orphans"])}})
    else:
        out.append(missing("ga4"))
    d = info.get("psi") or {}
    psi_ok = any((r or {}).get("status") == "success" for r in (d.get("results") or {}).values())
    if d.get("status") in FETCHED and psi_ok:
        results = {u: r for u, r in d["results"].items() if (r or {}).get("status") == "success"}
        cwv = [psi.cwv_assessment(r) for r in results.values()]
        perf = [r["scores"]["performance"] for r in results.values() if "performance" in (r.get("scores") or {})]
        counts = [[f.name, sum(1 for r in results.values() if psi.filter_state(r, f))] for f in psi.AUDIT_FILTERS]
        counts = sorted([c for c in counts if c[1]], key=lambda c: -c[1])
        out.append({"source": "psi", "title": f"PageSpeed Insights ({d.get('device', 'mobile')})", "connected": True,
                    "note": "", "columns": ["Opportunity", "Pages"], "rows": counts[:top],
                    "totals": {"tested": len(results), "cwv_passed": cwv.count("Passed"),
                               "cwv_failed": cwv.count("Failed"), "cwv_no_data": cwv.count(""),
                               "avg_performance": round(sum(perf) / len(perf)) if perf else None,
                               "not_tested": len(d.get("results") or {}) - len(results)}})
    else:
        out.append(missing("psi"))
    return out
