"""
Link Metrics tab: backlink metrics of the crawled pages from Moz, Majestic and Ahrefs (each an optional connector
with the team's own key; crawler/connectors/linkmetrics.py), fetched on request for this crawl
(crawler/integrations/linkmetrics.py, saved with the crawl in site.integrations["link_metrics"]).

Rows ("All"): internal HTML pages that answered 2xx, in crawl order (the pages a fetch looks up).
Filters (plain filters, not issues) split the rows into three groups that add up to All:
  with_metrics  a provider gave the page a metric above 0 (authority, links, referring domains ...)
  no_metrics    the providers answered for the page, but every metric is 0 or empty (not in their index)
  not_fetched   no provider answered for the page: not fetched yet, past the page limit, the unit cap, a signed-in
                crawl, a private host, or a provider error - the "Link Metrics" column says which
Until a provider is connected every filter is "Not connected"; connected but nothing fetched for this crawl yet is
"Not checked" with the reason. Columns: one per metric of each provider that is connected or has data in this crawl.
"""
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..registry import Column, Filter, Tab, register, register_details, register_requirement
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, is_html_doc

TAB_ID = "link_metrics"
ORDER = ("moz", "majestic", "ahrefs")


def _providers_meta() -> Tuple[Dict[str, List[Tuple[str, str]]], Dict[str, str]]:
    try:
        from ...connectors.linkmetrics import METRICS, NAMES
        return METRICS, NAMES
    except Exception:
        return {}, {}


def entry(site) -> Dict[str, Any]:
    e = (getattr(site, "integrations", None) or {}).get("link_metrics")
    return e if isinstance(e, dict) else {}


def rows(site) -> List[Any]:
    return [r for r in (getattr(site, "pages", None) or []) if is_html_doc(r) and category_of(r) == "2xx"]


def _connected_now() -> List[str]:
    try:
        from ...integrations import linkmetrics
        return linkmetrics.connected_providers()
    except Exception:
        return []


def result(site, url: str) -> Dict[str, Any]:
    res = (entry(site).get("results") or {}).get(url)
    return res if isinstance(res, dict) else {}


def metrics_of(site, url: str) -> Dict[str, Dict[str, Any]]:
    """{provider: {metric: value}} of the providers that answered for this page."""
    return {pid: v for pid, v in result(site, url).items() if isinstance(v, dict) and "not_checked" not in v}


def _positive(values: Dict[str, Any]) -> bool:
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
               for k, v in values.items() if k != "fetched_at")


def state(site, url: str) -> str:
    """"with" | "none" | "not_fetched"."""
    got = metrics_of(site, url)
    if not got:
        return "not_fetched"
    return "with" if any(_positive(v) for v in got.values()) else "none"


def not_fetched_reason(site, url: str) -> str:
    e = entry(site)
    why = (e.get("not_checked") or {}).get(url)
    if why:
        return str(why)
    res = result(site, url)
    parts = [f"{_providers_meta()[1].get(pid, pid)}: {v['not_checked']}" for pid, v in res.items()
             if isinstance(v, dict) and "not_checked" in v]
    if parts:
        return "Not checked - " + "; ".join(parts)
    return "Not fetched yet for this crawl."


def requirement(site, arg: str = "", rows_=()) -> str:
    """"" when link metrics were fetched for this crawl, else why the tab is Not connected / Not checked
    (requirement kind "linkmetrics")."""
    e = entry(site)
    if e.get("results"):
        return ""
    if e.get("status") == "running":
        return "Link metrics are being fetched now; the tab fills in when the fetch is done."
    skipped = [str(v) for v in (e.get("not_checked") or {}).values() if v]
    if skipped:
        top = Counter(skipped).most_common(1)[0][0]
        return f"No page of this crawl got link metrics. {top}"
    on = _connected_now()
    if on:
        names = ", ".join(_providers_meta()[1].get(p, p) for p in on)
        return (f"Link metrics are connected ({names}), but were not fetched for this crawl yet: an admin can run "
                "Fetch link metrics (Connections > Link metrics).")
    try:
        from ...integrations.linkmetrics import NOT_CONNECTED
    except Exception:
        NOT_CONNECTED = "Not connected - connect it under Connections"
    return NOT_CONNECTED


register_requirement("linkmetrics", requirement)


def _with(r, site) -> bool:
    return state(site, r.url) == "with"


def _none(r, site) -> bool:
    return state(site, r.url) == "none"


def _not_fetched(r, site) -> bool:
    return state(site, r.url) == "not_fetched"


FILTERS: List[Filter] = [
    Filter("with_metrics", "With Metrics", _with, None, None,
           "Pages a link-metrics provider knows: at least one metric (authority, backlinks, referring domains...) is "
           "above 0.", ""),
    Filter("no_metrics", "No Metrics", _none, None, None,
           "Pages the providers answered for, but with no links or authority yet (every metric 0 or empty): usually "
           "new or rarely linked pages.",
           "Link to these pages from strong pages of the site, and earn links from other sites if they matter."),
    Filter("not_fetched", "Not Fetched", _not_fetched, None, None,
           "Pages without link metrics for this crawl: not fetched yet, past the page limit or the monthly unit "
           "cap, a signed-in crawl, a private host, or a provider error. The Link Metrics column says which.",
           "Run Fetch link metrics again for the next pages, or raise the limit or the unit cap."),
]


def _status_col(r, site) -> str:
    st = state(site, r.url)
    if st == "with":
        return "Fetched"
    if st == "none":
        return "Fetched - no metrics"
    return not_fetched_reason(site, r.url)


def _metric_col(pid: str, key: str, label: str, name: str) -> Column:
    def get(r, site):
        v = metrics_of(site, r.url).get(pid)
        if v is None:
            return ""
        val = v.get(key)
        return "" if val is None else val
    return Column(f"{pid}_{key}", f"{name} {label}", get, "num")


BASE = [ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, Column("link_metrics_status", "Link Metrics", _status_col)]


def _all_columns() -> List[Column]:
    metrics, names = _providers_meta()
    cols = list(BASE)
    for pid in ORDER:
        for key, label in metrics.get(pid, []):
            cols.append(_metric_col(pid, key, label, names.get(pid, pid)))
    return cols


def _dynamic(site) -> Tuple[List[Column], List[Filter]]:
    """Columns of the providers that are connected now or have data in this crawl."""
    metrics, names = _providers_meta()
    have = set(_connected_now()) | set((entry(site).get("providers") or {}).keys())
    for per in (entry(site).get("results") or {}).values():
        have |= set(per.keys()) if isinstance(per, dict) else set()
    cols = list(BASE)
    for pid in ORDER:
        if pid in have:
            for key, label in metrics.get(pid, []):
                cols.append(_metric_col(pid, key, label, names.get(pid, pid)))
    return cols, FILTERS


def details(site, url: str) -> Optional[Dict[str, Any]]:
    """URL details section "link_metrics": {provider name: {metric label: value}} for the providers that answered,
    or {"not_checked": reason}; None for a URL the tab does not list."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not is_html_doc(r) or category_of(r) != "2xx":
        return None
    why = requirement(site)
    if why:
        return {"not_checked": why, "not_connected": why.startswith("Not connected")}
    got = metrics_of(site, url)
    if not got:
        return {"not_checked": not_fetched_reason(site, url)}
    metrics, names = _providers_meta()
    out: Dict[str, Any] = {"providers": {}}
    for pid in ORDER:
        if pid in got:
            labels = dict(metrics.get(pid, []))
            out["providers"][names.get(pid, pid)] = {labels.get(k, k): v for k, v in got[pid].items()
                                                     if k != "fetched_at"}
            out["providers"][names.get(pid, pid)]["fetched_at"] = got[pid].get("fetched_at") or ""
    missing = {names.get(pid, pid): v["not_checked"] for pid, v in result(site, url).items()
               if isinstance(v, dict) and "not_checked" in v}
    if missing:
        out["not_checked_providers"] = missing
    out["state"] = state(site, url)
    return out


register_details("link_metrics", details)

register(Tab(id=TAB_ID, name="Link Metrics", order=310, columns=_all_columns(), filters=FILTERS, rows=rows,
             requires=("linkmetrics:",), dynamic=_dynamic, static_filters=True,
             description="Backlink metrics per page from Moz, Majestic or Ahrefs (optional connections, your own "
                         "keys)."))
