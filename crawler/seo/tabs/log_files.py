"""
Log Files tab (P22): what the server's access log says the bots requested, per crawled URL (crawler/logs: uploaded
under Log Files > Upload, counts only). Rows: the crawl's internal URLs. Until a log is uploaded for the crawl every
filter is "Not checked" with that reason, never 0.

Filters: Googlebot Hits, Not Hit By Googlebot (indexable pages Googlebot never requested in the log's period), AI Bot
Hits, Bot Requests With 4xx / 5xx (what the bot got, from the log), Unverified Bot Hits (requests that claim to be
Googlebot / Bingbot / Applebot from addresses whose reverse DNS is not theirs).
Reports (registered with crawler/reports.py): log_orphans (bot-requested URLs the crawl never found) and log_bot_hits
(hits per bot).
"""
from typing import Any, Dict, List

from ..registry import Column, Filter, Tab, is_indexable, register
from .internal import ADDRESS, INDEXABILITY, STATUS_CODE, internal_rows, is_html_doc

NO_LOG = "No server log file was uploaded for this crawl (Log Files > Upload a log)."
AI_KINDS = ("ai", "training")


def entry(site) -> Dict[str, Any]:
    e = (getattr(site, "integrations", None) or {}).get("logs")
    return e if isinstance(e, dict) and e.get("status") == "done" else {}


def rec(site, r) -> Dict[str, Any]:
    return (entry(site).get("results") or {}).get(getattr(r, "url", ""), {}) if site is not None else {}


def _kind(site, label: str) -> str:
    return ((entry(site).get("bots") or {}).get(label) or {}).get("kind", "")


def hits(site, r, which: str = "") -> int:
    h = rec(site, r).get("hits") or {}
    if which == "ai":
        return sum(n for b, n in h.items() if _kind(site, b) in AI_KINDS)
    if which:
        return int(h.get(which, 0))
    return sum(h.values())


def _status_class(site, r, cls: str) -> bool:
    return any(str(code).startswith(cls) and n for code, n in (rec(site, r).get("statuses") or {}).items())


def _skip(site, filter_id: str) -> str:
    return "" if entry(site) else NO_LOG


COLUMNS = [
    ADDRESS, STATUS_CODE, INDEXABILITY,
    Column("bot_hits", "Bot Hits", lambda r, site: hits(site, r) if entry(site) else "", "num"),
    Column("googlebot_hits", "Googlebot Hits", lambda r, site: hits(site, r, "Googlebot") if entry(site) else "", "num"),
    Column("bingbot_hits", "Bingbot Hits", lambda r, site: hits(site, r, "Bingbot") if entry(site) else "", "num"),
    Column("ai_bot_hits", "AI Bot Hits", lambda r, site: hits(site, r, "ai") if entry(site) else "", "num"),
    Column("last_bot_hit", "Last Bot Hit", lambda r, site: rec(site, r).get("last_hit", "")),
    Column("log_statuses", "Status Codes In The Log",
           lambda r, site: ", ".join(f"{k} x{v}" for k, v in sorted((rec(site, r).get("statuses") or {}).items()))),
    Column("unverified_hits", "Unverified Bot Hits", lambda r, site: rec(site, r).get("unverified", "") if entry(site)
           else "", "num"),
]

FILTERS = [
    Filter("googlebot_hits", "Googlebot Hits", lambda r, site: hits(site, r, "Googlebot") > 0,
           description="URLs Googlebot requested in the period the log covers."),
    Filter("not_hit_by_googlebot", "Not Hit By Googlebot",
           lambda r, site: bool(entry(site)) and is_html_doc(r) and is_indexable(site, r) and
           hits(site, r, "Googlebot") == 0, "Opportunity", "Medium",
           "Indexable pages that Googlebot did not request in the period the log covers: they may be poorly linked, "
           "new, or seen as low value.",
           "Link to these pages from strong pages, list them in the XML sitemap and check they are not too deep; "
           "a longer log period gives a fuller picture.",
           short_fix="Link these pages better and list them in the sitemap"),
    Filter("ai_bot_hits", "AI Bot Hits", lambda r, site: hits(site, r, "ai") > 0,
           description="URLs requested by AI crawlers (GPTBot, ClaudeBot, PerplexityBot, CCBot ...)."),
    Filter("bot_4xx", "Bot Requests With 4xx", lambda r, site: _status_class(site, r, "4"), "Issue", "High",
           "URLs where a bot got a client error (4xx) according to the log, even if the crawl found them working.",
           "Fix or redirect the URLs bots keep requesting; check the log's dates against your changes.",
           short_fix="Fix or redirect URLs bots get errors on"),
    Filter("bot_5xx", "Bot Requests With 5xx", lambda r, site: _status_class(site, r, "5"), "Issue", "High",
           "URLs where a bot got a server error (5xx) according to the log: the server failed while the bot visited.",
           "Check the server's error logs at those times; frequent 5xx answers slow Google's crawling of the site.",
           short_fix="Find why the server failed for bots"),
    Filter("unverified_bot_hits", "Unverified Bot Hits", lambda r, site: int(rec(site, r).get("unverified") or 0) > 0,
           "Warning", "Low",
           "Requests that claim to be Googlebot, Bingbot or Applebot from addresses whose reverse DNS is not theirs: "
           "scrapers pretending to be a search engine.",
           "Block or rate-limit the addresses in your firewall or CDN (verify bots by reverse DNS, never by user agent).",
           short_fix="Block fake search-engine bots at the firewall"),
]

register(Tab(id="log_files", name="Log Files", order=320, columns=COLUMNS, filters=FILTERS, rows=internal_rows,
             skip_reason=_skip,
             description="What the server's access log says the bots requested (upload a log file)."))


# ---------------------------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------------------------
def log_orphans(site, index, crawler, ctx: dict):
    from ...exporter import Table, not_checked_table
    e = entry(site)
    if not e:
        return [not_checked_table("Log Orphans", NO_LOG)]
    rows = [[o["url"], o["hits"], ", ".join(o.get("bots") or []),
             ", ".join(f"{k} x{v}" for k, v in sorted((o.get("statuses") or {}).items())), o.get("last_hit", "")]
            for o in e.get("orphans") or []]
    return [Table("Log Orphans (requested by bots, not found by the crawl)",
                  ["Address", "Bot Hits", "Bots", "Status Codes In The Log", "Last Bot Hit"], rows, url_cols=(1,))]


def log_bot_hits(site, index, crawler, ctx: dict):
    from ...exporter import Table, not_checked_table
    e = entry(site)
    if not e:
        return [not_checked_table("Log Bot Hits", NO_LOG)]
    rows = [[label, b.get("kind", ""), b.get("hits", 0), b.get("urls", 0)]
            for label, b in sorted((e.get("bots") or {}).items(), key=lambda x: -x[1].get("hits", 0))]
    summary = [["Lines read", e.get("lines", 0)], ["Requests understood", e.get("parsed", 0)],
               ["Bot requests", e.get("bot_hits", 0)], ["Addresses verified as real bots", e.get("verified_ips", 0)],
               ["Addresses that faked a bot", e.get("unverified_ips", 0)],
               ["Addresses not verified (over the limit or DNS too slow)", e.get("not_verified_ips", 0)]]
    return [Table("Log Bot Hits", ["Bot", "Kind", "Hits", "URLs"], rows), Table("Log Summary", ["Item", "Value"], summary)]


def _register_reports():
    try:
        from ..registry import v2_hidden
        if v2_hidden("tabs", "log_files"):
            return                         # ARACHNIX 1.0: hidden with its tab (ARACHNIX_V2_FEATURES)
        from ... import reports
        have = {r["id"] for r in reports.REPORTS}
        for rid, name, fn, about in (("log_orphans", "Log Orphans", log_orphans,
                                      "URLs bots requested (server log) that the crawl never found."),
                                     ("log_bot_hits", "Log Bot Hits", log_bot_hits, "Requests per bot in the server log.")):
            if rid not in have:
                reports.REPORTS.append(reports._r(rid, name, "Log Files", name, description=about))
            reports._BUILDERS[rid] = fn
    except Exception:
        pass


_register_reports()
