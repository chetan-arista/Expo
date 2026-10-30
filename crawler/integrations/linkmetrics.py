"""
Link Metrics (Moz, Majestic, Ahrefs): the per-crawl data of the Link Metrics tab. The provider connectors (keys,
Test, the HTTP calls) are in crawler/connectors/linkmetrics.py; this module runs the fetch job and keeps its result
with the crawl.

  status() -> {provider: {"name", "connected", "available", "key": {"set", "source"}, "reason", "metrics", "scopes",
               "units_used", "unit_cap", "units_left", "unit_label"}}         (API Access dialog, Connections)
  fetch_status(crawler) -> {"providers": status(), "job", "fetched": {...}}    GET /api/linkmetrics/status
  start_fetch(crawler, providers=None, limit=None, dry_run=False)                POST /api/linkmetrics/fetch (admin)
      dry_run -> {"dry_run": True, "estimate_units": {provider: n}, "message": "about N units", ...}
      else    -> the job's public view (integrations JOBS source "link_metrics")
  targets(site) -> the crawl's pages the tab lists (internal HTML pages that answered 2xx, in crawl order)

What is sent: each page's address without its query string or #fragment (two pages that differ only there share
one lookup); never a page of a signed-in crawl ("Not checked: signed-in crawl") and never a host on a private
network. Each provider's units are counted per calendar month (integrations/store.py) against its admin-set cap: a
fetch never starts a batch that would pass the cap, and the pages left are "Not checked" with the reason.

Stored in integrations["link_metrics"] (the 10.3 entry shape, saved with the crawl):
  {"source", "connected": True, "reason", "status", "fetched_at",
   "providers": {provider: {"fetched_at", "status", "urls", "units", "message"}},
   "results": {url: {provider: {metric: value | None} | {"not_checked": reason}}},   (pages with metrics)
   "not_checked": {url: reason}}                                                   (pages without any)
A later fetch merges per page and per provider, so a Majestic run keeps the Moz values of an earlier one.
"""
import copy
import socket
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit, urlunsplit

from . import store
from .common import JOBS, IntegrationError, NotConnected, now_iso

SOURCE = "link_metrics"
DEFAULT_LIMIT = 100
MAX_LIMIT = 5000
NOT_CONNECTED = "Not connected - connect Moz, Majestic or Ahrefs under Connections"
SIGNED_IN = "Not checked: signed-in crawl (its addresses are not sent to link-metric providers)"
PRIVATE_HOST = "Not checked: a private-network address is never sent to link-metric providers"

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "majestic": {"name": "Majestic", "needs": "a Majestic API subscription (API key)",
                 "metrics": ["Trust Flow", "Citation Flow", "External Backlinks", "Referring Domains"],
                 "scopes": ["URL (exact page, query string removed)"]},
    "ahrefs": {"name": "Ahrefs", "needs": "an Ahrefs plan with API v3 access",
               "metrics": ["URL Rating", "Domain Rating", "Backlinks", "Referring Domains", "Organic Traffic"],
               "scopes": ["Exact URL (http + https)"]},
    "moz": {"name": "Moz", "needs": "a Moz API token (or Access ID and Secret Key)",
            "metrics": ["Page Authority", "Domain Authority", "Spam Score", "Linking Root Domains", "External Links"],
            "scopes": ["URL (exact page, query string removed)"]},
}


def _connectors() -> Dict[str, Any]:
    try:
        from ..connectors import registry
        return {pid: registry.get(pid) for pid in PROVIDERS}
    except Exception:
        return {pid: None for pid in PROVIDERS}


def connected(pid: str) -> bool:
    c = _connectors().get(pid)
    try:
        return c is not None and c.status().state in ("connected", "error")
    except Exception:
        return False


def connected_providers() -> List[str]:
    return [pid for pid in ("moz", "majestic", "ahrefs") if connected(pid)]


def status() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    conns = _connectors()
    for pid, p in PROVIDERS.items():
        c = conns.get(pid)
        on = False
        st_detail = ""
        cap = used = 0
        unit_label = "units"
        if c is not None:
            try:
                st = c.status()
                on = st.state in ("connected", "error")
                st_detail = st.detail
                cap, used, unit_label = c.unit_cap(), c.units_used(), c.unit_label
            except Exception:
                on = False
        out[pid] = {"name": p["name"], "connected": on, "available": c is not None,
                    "key": {"set": bool(store.link_key(pid)), "source": store.link_key_source(pid)},
                    "reason": "" if on else f"Not connected - connect {p['name']} under Connections (needs "
                                            f"{p['needs']}).",
                    "detail": st_detail, "metrics": list(p["metrics"]), "scopes": list(p["scopes"]),
                    "units_used": used, "unit_cap": cap, "units_left": max(0, cap - used), "unit_label": unit_label}
    return out


# ---------------------------------------------------------------------------------------------
# Pages and what is sent
# ---------------------------------------------------------------------------------------------
def targets(site) -> List[Any]:
    """The pages the Link Metrics tab lists and a fetch looks up: internal HTML pages that answered 2xx."""
    from ..seo.tabs.internal import category_of, is_html_doc
    return [r for r in (getattr(site, "pages", None) or []) if is_html_doc(r) and category_of(r) == "2xx"]


def lookup_address(url: str) -> str:
    """The address sent to a provider: the page without its query string and #fragment."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", "", ""))


_host_cache: Dict[str, bool] = {}


def private_host(url: str) -> bool:
    """True when the URL's host is (or resolves to) a private, loopback or link-local address."""
    from ..url_utils import is_internal_address
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True
    if host in _host_cache:
        return _host_cache[host]
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost", ".lan", ".home.arpa")):
        _host_cache[host] = True
        return True
    try:
        from ..subdomains import resolve
        ips = resolve(host)
    except Exception:
        try:
            ips = list({ai[4][0] for ai in socket.getaddrinfo(host, None)})
        except OSError:
            ips = []
    if not ips:
        ips = [host]
    private = any(is_internal_address(ip) for ip in ips)
    if len(_host_cache) > 2000:
        _host_cache.clear()
    _host_cache[host] = private
    return private


def _private_crawl(site, crawler) -> bool:
    cfg = getattr(site, "config", None) or getattr(crawler, "config", None)
    auth = getattr(cfg, "auth", None)
    return bool(getattr(crawler, "auth_used", False)) or bool(
        getattr(auth, "enabled", False) and (getattr(auth, "auth_type", "none") or "none") != "none")


def _choose(providers: Optional[List[str]]) -> List[str]:
    on = connected_providers()
    if providers:
        unknown = [p for p in providers if p not in PROVIDERS]
        if unknown:
            raise IntegrationError(f"Unknown link-metrics provider: {unknown[0][:20]}.")
        off = [p for p in providers if p not in on]
        if off:
            raise NotConnected(f"Not connected - connect {PROVIDERS[off[0]]['name']} under Connections first.")
        return [p for p in ("moz", "majestic", "ahrefs") if p in providers]
    if not on:
        raise NotConnected(NOT_CONNECTED)
    return on


def plan(crawler, limit: Optional[int] = None, only_missing: bool = True,
         providers: Optional[List[str]] = None) -> Dict[str, Any]:
    """What a fetch would do: {"pages": [urls], "lookups": {address: [urls]}, "not_sent": {url: reason},
    "over_limit": [urls], "private_crawl"}."""
    site, _ = crawler.site_index(force=True)
    pages = [r.url for r in targets(site)]
    prev = get_entry(crawler) or {}
    if only_missing and providers:
        done = prev.get("results") or {}
        pages = [u for u in pages if not all(p in (done.get(u) or {}) and "not_checked" not in done[u][p]
                                             for p in providers)]
    cap = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    chosen, over = pages[:cap], pages[cap:]
    out: Dict[str, Any] = {"pages": chosen, "lookups": {}, "not_sent": {}, "over_limit": over,
                           "private_crawl": _private_crawl(site, crawler)}
    if out["private_crawl"]:
        out["not_sent"] = {u: SIGNED_IN for u in chosen}
        return out
    for u in chosen:
        if private_host(u):
            out["not_sent"][u] = PRIVATE_HOST
            continue
        out["lookups"].setdefault(lookup_address(u), []).append(u)
    return out


def estimate(crawler, providers: Optional[List[str]] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """The dry run: units each provider would use for this fetch, against what is left of its monthly cap."""
    chosen = _choose(providers)
    conns = _connectors()
    p = plan(crawler, limit, True, chosen)
    n = len(p["lookups"])
    per: Dict[str, Any] = {}
    est: Dict[str, int] = {}
    for pid in chosen:
        c = conns[pid]
        units = c.estimate_units(n)
        cap, used = c.unit_cap(), c.units_used()
        left = max(0, cap - used)
        est[pid] = units
        per[pid] = {"name": c.name, "lookups": n, "units": units, "unit_label": c.unit_label, "used": used,
                    "cap": cap, "left": left, "fits": units <= left}
    parts = [f"{v['name']}: about {v['units']:,} {v['unit_label']} ({v['left']:,} left this month)" +
             ("" if v["fits"] else " - more than is left, so the fetch stops at the cap") for v in per.values()]
    msg = (f"{len(p['pages']):,} pages ({n:,} addresses to look up). " + "; ".join(parts) + ".") if n else \
        "No page of this crawl can be sent: " + (Counter(p["not_sent"].values()).most_common(1)[0][0]
                                                 if p["not_sent"] else "there is no page to look up.")
    return {"dry_run": True, "estimate_units": est, "providers": per, "pages": len(p["pages"]), "lookups": n,
            "not_sent": len(p["not_sent"]), "over_limit": len(p["over_limit"]), "private_crawl": p["private_crawl"],
            "message": msg}


# ---------------------------------------------------------------------------------------------
# The fetch job
# ---------------------------------------------------------------------------------------------
def get_entry(site_or_crawler) -> Optional[Dict[str, Any]]:
    from . import get_result
    return get_result(site_or_crawler, SOURCE)


def start_fetch(crawler, providers: Optional[List[str]] = None, limit: Optional[int] = None,
                dry_run: bool = False, only_missing: bool = True, on_done=None) -> Dict[str, Any]:
    """POST /api/linkmetrics/fetch (admin). Raises NotConnected / IntegrationError (plain messages)."""
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise IntegrationError("Crawl a site first: link metrics are looked up for the crawled pages.")
    chosen = _choose(providers)
    if dry_run:
        return estimate(crawler, chosen, limit)
    conns = _connectors()
    if all(conns[p].units_used() >= conns[p].unit_cap() for p in chosen):
        names = ", ".join(conns[p].name for p in chosen)
        raise IntegrationError(f"The monthly unit cap is used up for {names}: nothing was fetched. An admin can raise "
                               "the cap in Connections, or wait for next month.")

    def work(job) -> None:
        try:
            _run(job, crawler, chosen, limit, only_missing)
        finally:
            if on_done is not None:
                try:
                    on_done(crawler, SOURCE, job)
                except Exception:
                    pass

    return JOBS.start(SOURCE, work, owner=crawler).public()


def stop_fetch() -> Optional[Dict[str, Any]]:
    job = JOBS.stop(SOURCE)
    return job.public() if job is not None else None


def _merge(prev: Dict[str, Any], results: Dict[str, Dict[str, Any]], skipped: Dict[str, str],
           prov: Dict[str, Dict[str, Any]], status: str) -> Dict[str, Any]:
    """prev entry + this run, per page and per provider. A page lands in results once any provider gave it
    metrics; otherwise it stays in not_checked with the reason."""
    merged = copy.deepcopy(prev.get("results") or {}) if isinstance(prev.get("results"), dict) else {}
    pending: Dict[str, Dict[str, Any]] = {}
    for url, per in results.items():
        cur = dict(merged.get(url) or {})
        for pid, val in per.items():
            if "not_checked" in val and pid in cur and "not_checked" not in cur[pid]:
                continue                      # a failed run does not erase earlier metrics
            cur[pid] = val
        if any("not_checked" not in v for v in cur.values()):
            merged[url] = cur
        else:
            pending[url] = cur
    not_checked = {u: r for u, r in (prev.get("not_checked") or {}).items() if u not in merged}
    for url, per in pending.items():
        reasons = [f"{_name(pid)}: {v['not_checked']}" for pid, v in per.items()]
        not_checked[url] = "Not checked - " + "; ".join(reasons) if reasons else "Not checked"
    for url, why in skipped.items():
        if url not in merged:
            not_checked[url] = why
    providers = copy.deepcopy(prev.get("providers") or {})
    providers.update(prov)
    return {"source": SOURCE, "connected": True, "reason": "", "status": status, "fetched_at": now_iso(),
            "providers": providers, "results": merged, "not_checked": not_checked}


def _name(pid: str) -> str:
    return PROVIDERS.get(pid, {}).get("name", pid)


def _run(job, crawler, chosen: List[str], limit: Optional[int], only_missing: bool) -> None:
    from . import put_result
    from ..connectors.base import ConnectorError
    from ..connectors.linkmetrics import LinkMetricsError
    conns = _connectors()
    p = plan(crawler, limit, only_missing, chosen)
    prev = get_entry(crawler) or {}
    skipped: Dict[str, str] = dict(p["not_sent"])
    for u in p["over_limit"]:
        skipped.setdefault(u, f"Not checked: past the limit of {len(p['pages']):,} pages for one fetch - fetch "
                              "again for the next pages.")
    results: Dict[str, Dict[str, Any]] = {}
    prov: Dict[str, Dict[str, Any]] = {}
    lookups = list(p["lookups"].items())
    if not lookups:
        put_result(crawler, SOURCE, _merge(prev, {}, skipped, prov, "done"), merge=False)
        why = Counter(p["not_sent"].values()).most_common(1)[0][0] if p["not_sent"] else "no page to look up"
        job.progress("Done", 0, 0, f"Nothing was sent: {why}.")
        return
    total = len(lookups) * len(chosen)
    job.progress("Fetching link metrics", 0, total, f"Link metrics: 0 of {total:,} lookups")
    done = 0
    messages: List[str] = []
    for pid in chosen:
        c = conns[pid]
        left = max(0, c.unit_cap() - c.units_used())
        used_now, fetched, status, note = 0, 0, "done", ""
        net_errors = 0
        for i in range(0, len(lookups), c.batch):
            batch = lookups[i:i + c.batch]
            if job.stopped:
                status, note = "stopped", "Stopped before these pages."
                for addr, urls in lookups[i:]:
                    for u in urls:
                        results.setdefault(u, {})[pid] = {"not_checked": "the fetch was stopped before this page"}
                break
            cost = c.estimate_units(len(batch))
            if cost > left:
                status = "capped"
                note = (f"Stopped at the monthly cap of {c.unit_cap():,} {c.unit_label} ({c.units_used():,} used): "
                        f"{fetched:,} of {len(lookups):,} addresses fetched.")
                for addr, urls in lookups[i:]:
                    for u in urls:
                        results.setdefault(u, {})[pid] = {"not_checked": f"the monthly {c.name} cap was reached"}
                break
            try:
                rows, units = c.fetch([addr for addr, _urls in batch])
            except LinkMetricsError as e:
                for addr, urls in batch:
                    for u in urls:
                        results.setdefault(u, {})[pid] = {"not_checked": f"{c.name} answered {e.status or 'an error'}"}
                job.error(str(e))
                if e.stop:
                    status, note = "error", str(e)
                    for addr, urls in lookups[i + c.batch:]:
                        for u in urls:
                            results.setdefault(u, {})[pid] = {"not_checked": f"{c.name} stopped answering: {e}"[:200]}
                    break
                continue
            except ConnectorError as e:
                net_errors += 1
                for addr, urls in batch:
                    for u in urls:
                        results.setdefault(u, {})[pid] = {"not_checked": f"{c.name} could not be reached"}
                job.error(str(e))
                if net_errors >= 2:
                    status, note = "error", str(e)
                    for addr, urls in lookups[i + c.batch:]:
                        for u in urls:
                            results.setdefault(u, {})[pid] = {"not_checked": f"{c.name} could not be reached"}
                    break
                continue
            store.link_units_add(pid, units)
            left -= units
            used_now += units
            for (addr, urls), metrics in zip(batch, rows):
                fetched += 1
                for u in urls:
                    results.setdefault(u, {})[pid] = dict(metrics, fetched_at=now_iso()) \
                        if "not_checked" not in metrics else metrics
            done += len(batch)
            job.progress(done=done, message=f"Link metrics: {done:,} of {total:,} lookups")
            put_result(crawler, SOURCE, _merge(prev, results, skipped, prov, "running"), merge=False)
        prov[pid] = {"fetched_at": now_iso(), "status": status, "urls": fetched, "units": used_now,
                     "message": note}
        if note:
            messages.append(f"{c.name}: {note}")
        done = max(done, (chosen.index(pid) + 1) * len(lookups))
        job.progress(done=done)
    final = "stopped" if job.stopped else "done"
    put_result(crawler, SOURCE, _merge(prev, results, skipped, prov, final), merge=False)
    got = sum(1 for per in results.values() if any("not_checked" not in v for v in per.values()))
    msg = f"Link metrics fetched for {got:,} pages."
    if messages:
        msg += " " + " ".join(messages)
    job.progress("Done", total, total, msg)


def fetch_status(crawler=None) -> Dict[str, Any]:
    """GET /api/linkmetrics/status: providers (connected, units used / cap), the job and this crawl's counts."""
    job = JOBS.get(SOURCE)
    if job is not None and crawler is not None and job.owner is not None and not job.owned_by(crawler):
        job = None
    entry = get_entry(crawler) if crawler is not None else None
    results = (entry or {}).get("results") or {}
    return {"providers": status(), "job": job.public() if job is not None else None,
            "fetched": {"pages_with_data": len(results), "not_checked": len((entry or {}).get("not_checked") or {}),
                        "fetched_at": (entry or {}).get("fetched_at") or "",
                        "providers": copy.deepcopy((entry or {}).get("providers") or {})},
            "connected": bool(connected_providers()), "reason": "" if connected_providers() else NOT_CONNECTED}
