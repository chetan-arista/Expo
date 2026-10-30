"""
Google Analytics 4: accounts / properties / web data streams (Admin API accountSummaries, dataStreams) and page
metrics (Data API runReport), joined to the crawl like Screaming Frog's Analytics tab.

  list_accounts(client) -> [{"account", "name", "properties": [{"property", "name", "type"}]}]
  list_streams(client, property) -> [{"stream", "name", "type", "measurement_id", "default_uri"}]
  fetch_report(client, property, opts, origin, job) -> {"rows": [[url, {metric: value}]], ...}
  joined(site) -> {"rows": {crawl url: {metric: value, "api_url"}}, "orphans": {url: {...}}}   (memoised)

Each metric is read per landing page (landingPagePlusQueryString: sessions that STARTED on the URL, the
default) or per page viewed (pagePathPlusQueryString); the two are quite different. Metrics sharing a dimension
are read together, at most 10 per request (Data API limit), ordered by the first metric (sessions first), up to
"Limit Max Results" rows. Paths are joined onto the data stream's web address (its defaultUri), else the
crawl's own origin. A row "(not set)" is dropped.
Bounce rate: GA4's bounceRate is the share of sessions that were not engaged (1 - engagementRate); when it was
not fetched it is derived from engagementRate.
"""
from typing import Any, Dict, List, Optional, Tuple

from .common import (IntegrationError, Job, MatchOptions, Stopped, date_range, endpoint, join, origin_of)

SOURCE = "ga4"
MAX_METRICS_PER_REQUEST = 10
PAGE_SIZE = 100000                # rows per runReport call (API maximum 250,000)
MAX_METRICS = 65
DIMENSIONS = ("landingPagePlusQueryString", "pagePathPlusQueryString", "landingPage", "pagePath")
FILTER_DIMENSIONS = ("sessionDefaultChannelGroup", "firstUserDefaultChannelGroup", "sessionSource",
                     "sessionMedium", "sessionSourceMedium", "deviceCategory", "country")
DEFAULT_METRICS = ["sessions", "screenPageViews", "engagedSessions", "engagementRate", "keyEvents", "eventCount",
                   "totalRevenue"]

# Metric tree of the GA4 > Metrics page (API names; grouped like Screaming Frog's dialog)
METRIC_GROUPS: Dict[str, List[str]] = {
    "Ecommerce": ["addToCarts", "checkouts", "ecommercePurchases", "itemListClickEvents", "itemListClickThroughRate",
                  "itemListViewEvents", "itemViewEvents", "itemsAddedToCart", "itemsCheckedOut", "itemsPurchased",
                  "itemsViewed", "cartToViewRate", "purchaseToViewRate", "transactions", "transactionsPerPurchaser",
                  "firstTimePurchasers", "totalPurchasers"],
    "Event": ["eventCount", "eventCountPerUser", "eventsPerSession", "keyEvents", "sessionKeyEventRate",
              "userKeyEventRate"],
    "Other": ["userEngagementDuration", "scrolledUsers", "crashAffectedUsers", "crashFreeUsersRate"],
    "Page / Screen": ["screenPageViews", "screenPageViewsPerSession", "screenPageViewsPerUser"],
    "Publisher": ["publisherAdClicks", "publisherAdImpressions", "totalAdRevenue", "adUnitExposure"],
    "Revenue": ["totalRevenue", "purchaseRevenue", "grossPurchaseRevenue", "itemRevenue", "refundAmount",
                "averagePurchaseRevenue", "averageRevenuePerUser", "shippingAmount", "taxAmount"],
    "Session": ["sessions", "engagedSessions", "engagementRate", "bounceRate", "averageSessionDuration",
                "sessionsPerUser"],
    "User": ["activeUsers", "totalUsers", "newUsers", "active1DayUsers", "active7DayUsers", "active28DayUsers",
             "dauPerMau", "dauPerWau", "wauPerMau"],
}
LABELS: Dict[str, str] = {
    "sessions": "Sessions", "engagedSessions": "Engaged Sessions", "engagementRate": "Engagement Rate",
    "bounceRate": "Bounce Rate", "averageSessionDuration": "Avg Session Duration (s)", "sessionsPerUser":
    "Sessions per User", "screenPageViews": "Views", "screenPageViewsPerSession": "Views per Session",
    "screenPageViewsPerUser": "Views per User", "eventCount": "Event Count", "keyEvents": "Key Events",
    "sessionKeyEventRate": "Session Key Event Rate", "userKeyEventRate": "User Key Event Rate",
    "totalRevenue": "Total Revenue", "purchaseRevenue": "Purchase Revenue", "activeUsers": "Active Users",
    "totalUsers": "Total Users", "newUsers": "New Users", "ecommercePurchases": "Ecommerce Purchases",
    "addToCarts": "Add To Carts", "checkouts": "Checkouts", "transactions": "Transactions",
    "userEngagementDuration": "User Engagement Duration (s)",
}
RATE_METRICS = {"engagementRate", "bounceRate", "sessionKeyEventRate", "userKeyEventRate", "cartToViewRate",
                "purchaseToViewRate", "itemListClickThroughRate", "crashFreeUsersRate", "dauPerMau", "dauPerWau",
                "wauPerMau"}

DEFAULTS: Dict[str, Any] = {
    "date_range": "30d", "start_date": "", "end_date": "",
    "metrics": list(DEFAULT_METRICS), "dimension": "landingPagePlusQueryString", "metric_dimensions": {},
    "filter": {"dimension": "", "value": ""},
    "match_trailing_slash": True, "match_case": False, "match_protocol": True, "match_www": False,
    "limit_max_results": 100000, "crawl_new_urls": False,
}


def label(metric: str) -> str:
    if metric in LABELS:
        return LABELS[metric]
    out = "".join(" " + c if c.isupper() else c for c in metric).strip()
    return out[:1].upper() + out[1:]


def all_metrics() -> List[str]:
    return [m for group in METRIC_GROUPS.values() for m in group]


def catalog() -> Dict[str, Any]:
    """The Metrics page tree: groups, labels, defaults, the always-on metric and the limits."""
    return {"groups": [{"name": g, "metrics": [{"id": m, "label": label(m)} for m in ms]}
                       for g, ms in METRIC_GROUPS.items()],
            "defaults": list(DEFAULT_METRICS), "required": ["sessions"], "max": MAX_METRICS,
            "dimensions": list(DIMENSIONS), "filter_dimensions": list(FILTER_DIMENSIONS)}


def options(values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    v = dict(values or {})
    o = dict(DEFAULTS)
    for k, val in v.items():
        if k in DEFAULTS and val is not None:
            o[k] = val
    metrics = [str(m).strip() for m in (o.get("metrics") or []) if str(m).strip()]
    metrics = [m for m in metrics if m.replace("_", "").replace(":", "").isalnum()]
    if "sessions" not in metrics:
        metrics.insert(0, "sessions")              # cannot be unselected (Screaming Frog)
    o["metrics"] = list(dict.fromkeys(metrics))[:MAX_METRICS]
    o["dimension"] = o["dimension"] if o["dimension"] in DIMENSIONS else "landingPagePlusQueryString"
    md = o.get("metric_dimensions") if isinstance(o.get("metric_dimensions"), dict) else {}
    o["metric_dimensions"] = {m: d for m, d in md.items() if m in o["metrics"] and d in DIMENSIONS}
    flt = o.get("filter") if isinstance(o.get("filter"), dict) else {}
    if not flt.get("value") and v.get("channel_filter"):               # settings_store's simple form
        flt = {"dimension": "sessionDefaultChannelGroup", "value": v["channel_filter"]}
    dim = flt.get("dimension") if flt.get("dimension") in FILTER_DIMENSIONS else ""
    o["filter"] = {"dimension": dim, "value": str(flt.get("value") or "").strip()[:200] if dim else ""}
    try:
        o["limit_max_results"] = max(1, int(o["limit_max_results"]))
    except (TypeError, ValueError):
        o["limit_max_results"] = DEFAULTS["limit_max_results"]
    for k in ("match_trailing_slash", "match_case", "match_protocol", "match_www", "crawl_new_urls"):
        o[k] = bool(o[k])
    return o


def property_name(prop: str) -> str:
    p = str(prop or "").strip()
    return p if p.startswith("properties/") else f"properties/{p}" if p.isdigit() else p


# ---------------------------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------------------------
def list_accounts(client) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    token = ""
    for _ in range(50):
        params = {"pageSize": 200}
        if token:
            params["pageToken"] = token
        data = client.json("GET", f"{endpoint('ga4_admin')}/accountSummaries", "GA4 accounts", params=params)
        for a in data.get("accountSummaries") or []:
            out.append({"account": a.get("account") or "", "name": a.get("displayName") or a.get("account") or "",
                        "properties": [{"property": p.get("property") or "", "name": p.get("displayName") or "",
                                        "type": p.get("propertyType") or ""}
                                       for p in a.get("propertySummaries") or []]})
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return out


def list_streams(client, prop: str) -> List[Dict[str, Any]]:
    out = []
    data = client.json("GET", f"{endpoint('ga4_admin')}/{property_name(prop)}/dataStreams", "GA4 data streams",
                       params={"pageSize": 200})
    for s in data.get("dataStreams") or []:
        web = s.get("webStreamData") or {}
        out.append({"stream": s.get("name") or "", "name": s.get("displayName") or "", "type": s.get("type") or "",
                    "measurement_id": web.get("measurementId") or "", "default_uri": web.get("defaultUri") or ""})
    return out


# ---------------------------------------------------------------------------------------------
# Data API
# ---------------------------------------------------------------------------------------------
def _groups(opts: Dict[str, Any]) -> List[Tuple[str, List[str]]]:
    """(dimension, metrics) requests: metrics grouped by their dimension, at most 10 per request, sessions first."""
    by_dim: Dict[str, List[str]] = {}
    for m in opts["metrics"]:
        by_dim.setdefault(opts["metric_dimensions"].get(m) or opts["dimension"], []).append(m)
    out = []
    for dim, ms in by_dim.items():
        ms = sorted(ms, key=lambda m: m != "sessions")
        for i in range(0, len(ms), MAX_METRICS_PER_REQUEST):
            out.append((dim, ms[i:i + MAX_METRICS_PER_REQUEST]))
    return out


def _value(raw: Any, kind: str) -> Any:
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if kind == "TYPE_INTEGER":
        return int(round(f))
    return round(f, 4)


def _to_url(value: str, origin: str) -> str:
    v = (value or "").strip()
    if not v or v == "(not set)":
        return ""
    if v.startswith(("http://", "https://")):
        return v
    if not v.startswith("/"):
        v = "/" + v
    return origin + v if origin else ""


def report_body(dim: str, metrics: List[str], opts: Dict[str, Any], start: str, end: str, limit: int,
                offset: int) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "dateRanges": [{"startDate": start, "endDate": end}],
        "dimensions": [{"name": dim}], "metrics": [{"name": m} for m in metrics],
        "limit": limit, "offset": offset, "keepEmptyRows": False, "returnPropertyQuota": True,
        "orderBys": [{"metric": {"metricName": metrics[0]}, "desc": True}],
    }
    flt = opts["filter"]
    if flt.get("dimension") and flt.get("value"):
        body["dimensionFilter"] = {"filter": {"fieldName": flt["dimension"],
                                              "stringFilter": {"matchType": "EXACT", "value": flt["value"],
                                                               "caseSensitive": False}}}
    return body


def fetch_report(client, prop: str, opts: Dict[str, Any], origin: str, job: Optional[Job] = None) -> Dict[str, Any]:
    start, end = date_range(opts["date_range"], opts.get("start_date", ""), opts.get("end_date", ""))
    url = f"{endpoint('ga4_data')}/{property_name(prop)}:runReport"
    limit = int(opts["limit_max_results"])
    merged: Dict[str, Dict[str, Any]] = {}
    truncated = False
    quota: Dict[str, Any] = {}
    groups = _groups(opts)
    status = "done"
    for gi, (dim, metrics) in enumerate(groups, 1):
        if status == "stopped":
            break
        offset = 0
        while offset < limit:
            size = min(PAGE_SIZE, limit - offset)
            try:
                if job is not None and job.stopped:
                    raise Stopped("Stopped.")
                data = client.json("POST", url, "GA4 report",
                                   json=report_body(dim, metrics, opts, start, end, size, offset))
            except Stopped:
                status = "stopped"        # keep the pages read so far
                break
            headers = [h.get("name") for h in data.get("metricHeaders") or []]
            kinds = [h.get("type") or "" for h in data.get("metricHeaders") or []]
            rows = data.get("rows") or []
            for r in rows:
                dv = r.get("dimensionValues") or []
                u = _to_url(str((dv[0] or {}).get("value") or "") if dv else "", origin)
                if not u:
                    continue
                vals = r.get("metricValues") or []
                entry = merged.setdefault(u, {})
                for name, kind, mv in zip(headers, kinds, vals):
                    if name and name not in entry:
                        entry[name] = _value((mv or {}).get("value"), kind)
            quota = data.get("propertyQuota") or quota
            offset += len(rows)
            row_count = int(data.get("rowCount") or 0)
            if job is not None:
                job.progress("GA4 report", gi - 1, len(groups),
                             f"GA4: request {gi} of {len(groups)}, {len(merged):,} pages read")
            if len(rows) < size or (row_count and offset >= row_count):
                break
        else:
            truncated = True
    if status == "stopped":
        truncated = True
    for entry in merged.values():
        if "bounceRate" not in entry and isinstance(entry.get("engagementRate"), (int, float)):
            entry["bounceRate"] = round(1.0 - float(entry["engagementRate"]), 4)
    rows_out = [[u, m] for u, m in merged.items()]
    rows_out.sort(key=lambda x: -(x[1].get("sessions") or 0))
    tokens = (quota.get("tokensPerDay") or {}) if isinstance(quota, dict) else {}
    return {"rows": rows_out, "row_count": len(rows_out), "truncated": truncated, "start": start, "end": end,
            "requests": len(groups), "status": status,
            "message": "Stopped: only part of the data was read." if status == "stopped" else "",
            "quota_left_today": tokens.get("remaining") if isinstance(tokens, dict) else None}


# ---------------------------------------------------------------------------------------------
# Join with the crawl (memoised on the Site snapshot)
# ---------------------------------------------------------------------------------------------
def data_of(site) -> Dict[str, Any]:
    d = (getattr(site, "integrations", None) or {}).get(SOURCE)
    return d if isinstance(d, dict) else {}


def joined(site) -> Dict[str, Any]:
    from .gsc import _internal, _memo

    def build() -> Dict[str, Any]:
        d = data_of(site)
        opts = MatchOptions.from_options(d.get("options") or {})
        rows = [(r[0], (r[1] or {}).get("sessions") or 0, r[1]) for r in d.get("rows") or []
                if isinstance(r, list) and len(r) >= 2 and isinstance(r[1], dict)]
        res = join(rows, list((getattr(site, "by_url", None) or {}).keys()), opts)
        by_url = {crawl: dict(payload, api_url=api) for crawl, (api, payload) in res.matched.items()}
        orphans: Dict[str, Dict[str, Any]] = {}
        for api, payload in res.unmatched:
            if _internal(site, api) and api not in orphans:
                orphans[api] = payload
        return {"rows": by_url, "orphans": orphans, "metrics": list(d.get("metrics") or [])}
    return _memo(site, "ga4_join", build)


def origin_for(selection: Dict[str, Any], start_url: str) -> str:
    return origin_of(selection.get("stream_uri") or "") or origin_of(start_url or "")


def check_property(prop: str) -> str:
    p = property_name(prop)
    if not p.startswith("properties/") or not p.split("/", 1)[1].isdigit():
        raise IntegrationError("Choose a GA4 property first (Account > Property > Data Stream).")
    return p
