"""
Robots.txt tab: what robots.txt and the robots directives mean for search engines and AI assistants.

The first row is the site's robots.txt itself (from crawler/seo/robots_audit.py, run after the crawl):
crawlers blocked from the whole site, AI-training opt-outs, Content-Signal lines, Sitemap lines and
robots.txt errors. The other rows are the internal HTML pages that answered 2xx: sections disallowed for
AI crawlers, data-nosnippet, max-snippet, an unavailable_after date that has passed, a noindex aimed at
Googlebot or Bingbot only, and index / noindex in conflict.
Site-level filters are "Not checked" until robots_audit has run (tab.skip_reason).
When the crawl obeyed a custom robots.txt (CrawlConfig.custom_robots_txt, P3) the row audits that text instead of
the live file and says "Custom robots.txt used for this crawl" (it has no HTTP status: nothing was fetched).
"""
from http import HTTPStatus
from typing import Any, List, Optional

from ..postcrawl import SiteRow, not_run_reason, report_ran
from ..robots_audit import (CUSTOM_NOTE, MAX_SNIPPET_MIN, conflicting_index, max_snippet, page_blocked_bots,
                            partial_ai_bots, unavailable_after, unavailable_after_passed)
from ..registry import Column, Filter, Tab, register, seo
from .internal import ADDRESS, first_status_code, indexability_label, memo
from .page_titles import content_rows


def _audit(site) -> Optional[dict]:
    rep = getattr(site, "robots_audit", None)
    return rep if report_ran(rep) else None


def robots_row(site) -> Optional[SiteRow]:
    """The robots.txt URL as a row with its own HTTP status (None until the audit ran)."""
    def build():
        rep = _audit(site)
        if not rep:
            return None
        code = rep.get("http_status")
        if rep.get("custom"):
            return SiteRow(url=rep.get("url") or "", status_code=None, http_status=CUSTOM_NOTE,
                           content_type="text/plain", kind="robots", data=rep, discovery_source=CUSTOM_NOTE)
        try:
            phrase = HTTPStatus(code).phrase if code else ""
        except ValueError:
            phrase = ""
        return SiteRow(url=rep.get("final_url") or rep.get("url") or "", status_code=code,
                       http_status=f"{code} {phrase}".strip() if code else (rep.get("error") or "No Response"),
                       content_type=rep.get("content_type") or "", kind="robots", data=rep,
                       error_message=rep.get("error") or None, discovery_source="robots.txt check")
    return memo(site, "robots_txt_row", build)


def robots_rows(site) -> List[Any]:
    row = robots_row(site)
    return ([row] if row is not None else []) + content_rows(site)


def _is_robots(r) -> bool:
    return isinstance(r, SiteRow) and r.kind == "robots"


def _site(test):
    """A site-level test, true only on the robots.txt row."""
    return lambda r, site: _is_robots(r) and bool(test(r.data))


def _page(test):
    """A page-level test (never the robots.txt row)."""
    return lambda r, site: not isinstance(r, SiteRow) and bool(test(r, site))


def _issue_ids(rep: dict) -> List[str]:
    return [i.get("id") for i in rep.get("issues") or []]


def _groups(site):
    def build():
        rep = _audit(site)
        if not rep or not rep.get("found"):
            return None
        from ...robots import parse_robots
        return parse_robots(rep.get("text") or "")
    return memo(site, "robots_txt_groups", build)


def ai_disallowed(r, site) -> List[str]:
    """AI crawlers robots.txt keeps off this page (cached per Site: the filter and two columns ask)."""
    if isinstance(r, SiteRow):
        return []
    groups = _groups(site)
    bots = memo(site, "robots_txt_partial_bots", lambda: partial_ai_bots(groups, _audit(site)))
    if not bots:
        return []
    cache = memo(site, "robots_txt_ai_disallowed", dict)
    if r.url not in cache:
        cache[r.url] = page_blocked_bots(groups, _audit(site), r.url, bots)
    return cache[r.url]


def _low_snippet(r, site) -> bool:
    v = max_snippet(seo(r))
    return v is not None and 0 <= v < MAX_SNIPPET_MIN


def _site_list(key: str):
    return lambda r, site: ", ".join(r.data.get(key) or []) if _is_robots(r) else ""


def _row_type(r, site) -> str:
    if _is_robots(r):
        return "robots.txt (custom)" if _custom(r) else "robots.txt"
    return "Page"


def _indexability(r, site) -> str:
    return "" if isinstance(r, SiteRow) else indexability_label(site, r)


def _signals(r, site) -> str:
    if not _is_robots(r):
        return ""
    return ", ".join(f"{k}={v}" for k, v in (r.data.get("content_signals") or {}).items())


def _sitemaps(r, site):
    return len(r.data.get("sitemaps") or []) if _is_robots(r) else ""


def _custom(r) -> bool:
    return _is_robots(r) and bool(r.data.get("custom"))


def _status_code(r, site):
    """The robots.txt row of a custom robots.txt has no HTTP status (nothing was fetched)."""
    return "" if _custom(r) else first_status_code(r)


def _findings(r, site) -> str:
    if _is_robots(r):
        text = " ".join(i.get("text", "") for i in r.data.get("issues") or [])
        if _custom(r):
            text = (f"{CUSTOM_NOTE}: the live file was not read, and these findings describe the custom text. "
                    + text).strip()
        return text
    p = seo(r)
    out = []
    bots = ai_disallowed(r, site)
    if bots:
        out.append(f"robots.txt disallows {', '.join(bots)}")
    if p.get("data_nosnippet_count"):
        out.append(f"{p['data_nosnippet_count']} data-nosnippet section(s)")
    if _low_snippet(r, site):
        out.append(f"max-snippet:{max_snippet(p)}")
    if unavailable_after_passed(p):
        out.append("unavailable_after date passed")
    if p.get("scoped_noindex"):
        out.append("noindex only for " + ", ".join(b.capitalize() for b in p["scoped_noindex"]))
    if conflicting_index(p):
        out.append("index and noindex both set")
    return "; ".join(out)


FILTERS = [
    # robots.txt (site level)
    Filter("search_blocked", "Search Engines Blocked", _site(lambda d: d.get("search_blocked")),
           issue_type="Issue", priority="High",
           description="robots.txt blocks a search engine (Googlebot, Bingbot or Applebot) from the whole site, so "
                       "its pages can drop out of that search engine.",
           fix="Remove the 'Disallow: /' for that crawler (or for 'User-agent: *') unless the site must stay "
               "out of search."),
    Filter("ai_blocked", "AI Assistants Blocked", _site(lambda d: "ai_blocked" in _issue_ids(d)),
           issue_type="Issue", priority="Medium",
           description="robots.txt keeps AI crawlers (GPTBot, OAI-SearchBot, ClaudeBot, PerplexityBot, "
                       "Google-Extended, Applebot-Extended...) out of the whole site, or a Content-Signal line says "
                       "ai-input=no. Those assistants cannot read the site, so it is not quoted or recommended in "
                       "their answers.",
           fix="If you want to appear in AI answers, remove the Disallow lines for those crawlers (keep blocking "
               "training-only crawlers such as CCBot if you prefer)."),
    Filter("training_opt_out", "AI Training Opt-Out", _site(lambda d: "training_blocked" in _issue_ids(d)),
           description="robots.txt keeps training-only crawlers (CCBot, Bytespider) out, or says ai-train=no. This "
                       "only keeps the site out of AI training data; search and AI answers are not affected."),
    Filter("content_signal", "Content-Signal Set", _site(lambda d: d.get("content_signal_lines")),
           description="robots.txt has Content-Signal lines (search / ai-input / ai-train yes or no), which tell AI "
                       "companies how the content may be used."),
    Filter("ai_partial", "AI Crawlers Kept Out Of Sections", _site(lambda d: d.get("ai_partial")),
           description="robots.txt keeps some AI crawlers out of parts of the site. The pages affected are listed "
                       "under 'Disallowed For AI Crawlers'."),
    Filter("no_sitemap_line", "No Sitemap Line", _site(lambda d: d.get("found") and not d.get("sitemaps")),
           issue_type="Opportunity", priority="Low",
           description="robots.txt does not name your XML sitemap. A 'Sitemap:' line lets every crawler find it, not "
                       "only the search engines you submitted it to.",
           fix="Add a line such as 'Sitemap: https://www.example.com/sitemap.xml' to robots.txt."),
    Filter("robots_unreachable", "robots.txt Server Error Or Unreachable",
           _site(lambda d: {"server_error", "unreachable"} & set(_issue_ids(d))),
           issue_type="Issue", priority="High",
           description="robots.txt answered with a server error or not at all. Google then stops crawling the whole "
                       "site until robots.txt loads again.",
           fix="Make /robots.txt answer 200 (or 404 if you have none) quickly and reliably."),
    Filter("robots_html", "robots.txt Serves A Web Page", _site(lambda d: "html" in _issue_ids(d)),
           issue_type="Warning", priority="High",
           description="/robots.txt answers with an HTML page (often a 'not found' page with status 200), so "
                       "crawlers find no valid rules in it.",
           fix="Serve a plain-text robots.txt, or answer 404 when there is none."),
    Filter("robots_too_big", "robots.txt Over 500 KB", _site(lambda d: d.get("too_big")),
           issue_type="Warning", priority="Medium",
           description="Google reads only the first 500 KB of robots.txt; rules after that are ignored.",
           fix="Shorten robots.txt: group rules with wildcards instead of listing URLs one by one."),
    Filter("custom_robots_txt", "Custom robots.txt Used", _site(lambda d: d.get("custom")),
           description="The crawl obeyed a custom robots.txt given in the crawl settings instead of the site's live "
                       "file, so Blocked by Robots.txt and every robots.txt finding here describe that custom text. "
                       "Use it to test a new robots.txt before it goes live."),
    Filter("robots_missing", "No robots.txt", _site(lambda d: "missing" in _issue_ids(d)),
           description="The site has no robots.txt (404). Everything may be crawled, which is allowed, but you "
                       "cannot point crawlers at your sitemap or keep them out of cart, search and filter pages."),
    # Pages
    Filter("ai_disallowed_pages", "Disallowed For AI Crawlers", _page(lambda r, site: ai_disallowed(r, site)),
           issue_type="Warning", priority="Medium",
           description="robots.txt keeps at least one AI crawler off this page while it may crawl the rest of the "
                       "site, so AI assistants cannot read or quote this page.",
           fix="Check the Disallow rules for those crawlers in robots.txt; remove them for pages you want in AI "
               "answers."),
    Filter("data_nosnippet", "data-nosnippet Sections", _page(lambda r, site: seo(r).get("data_nosnippet_count")),
           issue_type="Warning", priority="Low",
           description="Parts of the page carry data-nosnippet. Google keeps them out of search snippets and out of "
                       "AI Overviews / AI Mode answers.",
           fix="Keep data-nosnippet only on text you really do not want quoted (prices that change, legal text)."),
    Filter("scoped_noindex", "Noindex Only For Googlebot Or Bingbot", _page(lambda r, site: seo(r).get("scoped_noindex")),
           issue_type="Warning", priority="High",
           description="A noindex names one search engine (<meta name=\"googlebot\"> / \"bingbot\" or 'googlebot: "
                       "noindex' in X-Robots-Tag) while the general robots tag allows indexing: the page disappears "
                       "from that search engine only, which is easy to miss.",
           fix="Remove the crawler-specific noindex unless you want the page out of that search engine."),
    Filter("max_snippet_low", f"Max-Snippet Under {MAX_SNIPPET_MIN}", _page(_low_snippet),
           issue_type="Warning", priority="Medium",
           description=f"max-snippet is set below {MAX_SNIPPET_MIN} characters. Google shows at most that much text "
                       "from the page in results, AI Overviews and AI Mode included, so the listing says little.",
           fix="Remove max-snippet or set it to -1 (no limit) unless a short preview is intended."),
    Filter("unavailable_after_passed", "Unavailable_After Date Passed", _page(lambda r, site: unavailable_after_passed(seo(r))),
           issue_type="Issue", priority="High",
           description="The page carries an unavailable_after date that has already passed, so Google drops it "
                       "from results although it is still online.",
           fix="Remove the unavailable_after directive (or move the date) if the page should stay in search."),
    Filter("conflicting_index", "Conflicting Index And Noindex", _page(lambda r, site: conflicting_index(seo(r))),
           issue_type="Warning", priority="Medium",
           description="The page says both index and noindex (in different robots tags or the X-Robots-Tag header). "
                       "Search engines obey the stricter one, so the page is not indexed.",
           fix="Keep one robots directive per page, set in one place (the SEO plugin or the server, not both)."),
]

COLUMNS = [
    ADDRESS,
    Column("status_code", "Status Code", _status_code, "status"),
    Column("row_type", "Type", _row_type),
    Column("findings", "Findings", _findings),
    Column("search_blocked", "Search Engines Blocked", _site_list("search_blocked")),
    Column("ai_blocked", "AI Crawlers Blocked", _site_list("ai_blocked")),
    Column("training_blocked", "Training Crawlers Blocked", _site_list("training_blocked")),
    Column("content_signal", "Content-Signal", _signals),
    Column("sitemap_lines", "Sitemap Lines", _sitemaps, "num"),
    Column("ai_disallowed", "Disallowed For", lambda r, site: ", ".join(ai_disallowed(r, site))),
    Column("data_nosnippet", "data-nosnippet",
           lambda r, site: "" if isinstance(r, SiteRow) else seo(r).get("data_nosnippet_count") or 0, "num"),
    Column("max_snippet", "Max-Snippet",
           lambda r, site: "" if isinstance(r, SiteRow) or max_snippet(seo(r)) is None else max_snippet(seo(r)), "num"),
    Column("unavailable_after", "Unavailable After",
           lambda r, site: "" if isinstance(r, SiteRow) else unavailable_after(seo(r))[0]),
    Column("scoped_noindex", "Noindex Only For",
           lambda r, site: "" if isinstance(r, SiteRow) else ", ".join(seo(r).get("scoped_noindex") or [])),
    Column("indexability", "Indexability", _indexability),
]

SITE_FILTERS = {"search_blocked", "ai_blocked", "training_opt_out", "content_signal", "ai_partial", "no_sitemap_line",
                "robots_unreachable", "robots_html", "robots_too_big", "robots_missing", "ai_disallowed_pages",
                "custom_robots_txt"}
NEEDS_FILE = {"no_sitemap_line", "robots_too_big"}     # nothing to judge without a robots.txt file
EXTRACTOR_FILTERS = {"data_nosnippet", "scoped_noindex"}


def skip_reason(site, filter_id: str) -> str:
    """Why a filter of this tab is "Not checked" ("" when it ran)."""
    if filter_id in SITE_FILTERS:
        reason = not_run_reason(getattr(site, "robots_audit", None), "robots.txt check")
        if reason:
            return reason
        rep = _audit(site) or {}
        if filter_id in NEEDS_FILE and not rep.get("found"):
            code = rep.get("http_status")
            return "No robots.txt rules to read" + (f" (it answered HTTP {code})." if code else ".")
        return ""
    if filter_id in EXTRACTOR_FILTERS:
        pages = content_rows(site)
        if pages and all(seo(r).get("has_doctype") is None for r in pages):
            return "These pages were read by an older version of the crawler; run a new crawl to check this."
    return ""


TAB = register(Tab(id="robots_txt", name="Robots.txt", order=240, columns=COLUMNS, filters=FILTERS, rows=robots_rows))
TAB.skip_reason = skip_reason
