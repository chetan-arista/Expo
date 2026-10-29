"""
AI Crawlers tab: the search / AI bot readability check (crawler/seo/bot_check.py, run after the crawl).
Rows are the sampled pages. Each filter lists the pages where at least one crawler of that kind was
blocked, got a challenge page, was told noindex, got different or thinner content, or was sent
elsewhere; the full page x crawler matrix is served by GET /api/crawl/bots.

The requests are sent from this server with each crawler's user agent: CDNs check real crawlers by IP
address, so the real crawler can be treated differently (the report notes say so).
While the check has not run, every bot filter here is "Not checked" (tab.skip_reason).

llms.txt (P19): the site's /llms.txt (and /llms-full.txt when the site serves something there) are rows of their
own, read after the crawl with the robots.txt check (robots_audit.llms_check; links judged against the crawl by
robots_audit.llms_summary -> site.llms_txt). The bot filters look at the sampled pages only and the llms.txt
filters at the llms rows only (Filter.of_rows), so neither changes the other's counts or "% of total". No llms.txt
is a plain filter (the file is optional); a file that could not be read is "Not checked", never "missing".
"""
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Sequence

from ..bot_check import AGENTS
from ..postcrawl import SiteRow, not_run_reason, report_ran
from ..registry import Column, Filter, Tab, register
from .internal import ADDRESS, INDEXABILITY, memo


def _report(site) -> Optional[dict]:
    rep = getattr(site, "bot_report", None)
    return rep if report_ran(rep) else None


def bot_rows(site) -> List[Any]:
    """The sampled pages that were fully checked, in sample order."""
    def build():
        rep = _report(site)
        if not rep:
            return []
        pages = rep.get("pages") or {}
        return [site.by_url[u] for u in rep.get("sample") or [] if u in pages and u in site.by_url]
    return memo(site, "ai_crawler_rows", build)


# ---------------------------------------------------------------------------------------------
# llms.txt rows (P19)
# ---------------------------------------------------------------------------------------------
LLMS_KINDS = {"/llms.txt": "llms_txt", "/llms-full.txt": "llms_full_txt"}
LLMS_LABELS = {"llms_txt": "llms.txt", "llms_full_txt": "llms-full.txt"}
LLMS_FILTERS = ("llms_txt_missing", "llms_txt_invalid", "llms_txt_broken_links", "llms_txt_html")


def is_llms_row(r) -> bool:
    return isinstance(r, SiteRow) and r.kind in LLMS_LABELS


def _bot_row(r) -> bool:
    return not isinstance(r, SiteRow)


def llms_info(site) -> Optional[dict]:
    info = getattr(site, "llms_txt", None)
    return info if isinstance(info, dict) else None


def llms_rows(site) -> List[Any]:
    """/llms.txt as a row whenever the check got this far, /llms-full.txt only when the site serves something
    there (the file is optional, so its absence is not listed)."""
    def build():
        info = llms_info(site)
        if not info:
            return []
        out = []
        for f in info.get("files") or []:
            if f.get("path") != "/llms.txt" and f.get("state") not in ("found", "html"):
                continue
            code = f.get("http_status")
            try:
                phrase = HTTPStatus(code).phrase if code else ""
            except ValueError:
                phrase = ""
            out.append(SiteRow(url=f.get("url") or "", status_code=code,
                               http_status=f"{code} {phrase}".strip() if code else "No Response",
                               content_type=str(f.get("content_type") or "").lower(),
                               kind=LLMS_KINDS.get(f.get("path"), "llms_txt"), data=f,
                               error_message=f.get("reason") if f.get("state") == "not_checked" else None,
                               discovery_source="llms.txt check"))
        return out
    return memo(site, "llms_txt_rows", build)


def ai_rows(site) -> List[Any]:
    """The tab's rows: the sampled pages, then the llms.txt files."""
    return bot_rows(site) + llms_rows(site)


def _llms(test):
    return lambda r, site: is_llms_row(r) and bool(test(r.data))


def _missing(d: dict) -> bool:
    return d.get("path") == "/llms.txt" and d.get("state") == "missing"


def _broken_links(d: dict) -> bool:
    return d.get("state") == "found" and int(d.get("links_broken_total") or 0) > 0


def llms_findings(r) -> str:
    """Plain summary of one llms file: why it could not be read, or its problems and notes."""
    if not is_llms_row(r):
        return ""
    d = r.data or {}
    if d.get("state") != "found":
        return d.get("reason") or ""
    parts = list(d.get("problems") or []) + list(d.get("notes") or [])
    broken = int(d.get("links_broken_total") or 0)
    if broken:
        sample = ", ".join(f"{b.get('url')} ({b.get('reason')})" for b in (d.get("links_broken") or [])[:3])
        parts.append(f"{broken:,} link{'s' if broken != 1 else ''} to broken or non-indexable pages: {sample}"
                     + (" ..." if broken > 3 else ""))
    nc = int(d.get("links_not_checked") or 0)
    if nc:
        why = "; ".join(f"{n:,} {k}" for k, n in sorted((d.get("not_checked_reasons") or {}).items(),
                                                         key=lambda kv: -kv[1]))
        parts.append(f"{nc:,} link{'s' if nc != 1 else ''} not checked ({why}).")
    return " ".join(parts) or "Follows the llms.txt format; every link checked points to an indexable page."


def llms_skip_reason(site, filter_id: str) -> str:
    """Why an llms.txt filter is "Not checked" ("" when it ran)."""
    info = llms_info(site)
    if info is None:
        rep = getattr(site, "robots_audit", None)
        if report_ran(rep):
            return "This crawl was made before llms.txt was checked; run a new crawl to check it."
        return not_run_reason(rep, "robots.txt and llms.txt check")
    if info.get("error") and not info.get("files"):
        return f"The llms.txt check could not run ({info['error']})."
    files = info.get("files") or []
    main = next((f for f in files if f.get("path") == "/llms.txt"), None)
    if main is None:
        return "llms.txt was not checked for this crawl."
    if main.get("state") == "not_checked":
        return main.get("reason") or "llms.txt could not be read."
    if filter_id == "llms_txt_missing":
        return ""
    found = [f for f in files if f.get("state") == "found"]
    if filter_id == "llms_txt_invalid" and not found and any(f.get("state") == "html" for f in files):
        return ("llms.txt answers with a web page, so its format could not be checked (see llms.txt Served As A "
                "Web Page).")
    if filter_id == "llms_txt_broken_links" and found and not any(
            int(f.get("links_ok") or 0) + int(f.get("links_broken_total") or 0) for f in found):
        total = sum(int(f.get("links_total") or 0) for f in found)
        if total:
            return (f"None of the {total:,} links in llms.txt points to a page this crawl requested (other sites or "
                    "pages outside the crawl), so they could not be checked.")
    return ""


def _page(site, r) -> Dict[str, Any]:
    rep = _report(site) or {}
    return (rep.get("pages") or {}).get(getattr(r, "url", ""), {}) or {}


def agents_in(site, r, states: Sequence[str], kinds: Sequence[str] = ("search", "ai", "training"),
              skip: Sequence[str] = ()) -> List[str]:
    """Labels of the crawlers of these kinds whose answer on this page is in one of these states."""
    cells = _page(site, r).get("cells") or {}
    return [a.label for a in AGENTS[1:] if a.kind in kinds and a.id not in skip
            and (cells.get(a.id) or {}).get("state") in states]


def _has(states: Sequence[str], kinds: Sequence[str] = ("search", "ai", "training"), skip: Sequence[str] = ()):
    return lambda r, site: bool(agents_in(site, r, states, kinds, skip))


def _mobile_differs(r, site) -> bool:
    return bool((_page(site, r).get("mobile") or {}).get("differs"))


def _js(key: str):
    return lambda r, site: bool((_page(site, r).get("js") or {}).get(key))


def _robots_disallowed(r, site) -> List[str]:
    robots = _page(site, r).get("robots") or {}
    return [a.label for a in AGENTS[1:] if robots.get(a.id) and not robots[a.id].get("allowed", True)]


def _all_same(r, site) -> bool:
    cells = _page(site, r).get("cells") or {}
    return bool(cells) and all((c or {}).get("state") == "same" for c in cells.values())


def _cell_list(states: Sequence[str], kinds: Sequence[str] = ("search", "ai", "training"), skip: Sequence[str] = ()):
    return lambda r, site: ", ".join(agents_in(site, r, states, kinds, skip))


def _baseline(key: str):
    return lambda r, site: (_page(site, r).get("baseline") or {}).get(key, "")


def _js_words(r, site) -> str:
    js = _page(site, r).get("js") or {}
    return f"{js['raw_words']} / {js['rendered_words']}" if js else ""


def _mobile(r, site) -> str:
    m = _page(site, r).get("mobile") or {}
    if not m:
        return ""
    if not m.get("differs"):
        return "Same"
    fields = m.get("fields") or []
    return ("Different " + ", ".join("H1" if f == "h1" else f for f in fields)) if fields else \
        f"{m.get('mobile_words')} vs {m.get('desktop_words')} words"


SKIP_DESKTOP = ("googlebot_desktop",)    # mobile-first indexing: desktop differences are shown in their own filter

BOT_FILTERS = [
    Filter("search_blocked", "Search Engines Blocked Or Challenged", _has(("blocked", "challenged"), ("search",)),
           issue_type="Issue", priority="High",
           description="A request sending a search engine's user agent (Googlebot, Bingbot or Applebot) was refused "
                       "or got a challenge page while a browser got the page. If the real crawler is treated the same "
                       "way, these pages drop out of search.",
           fix="Check the firewall / CDN bot settings (Cloudflare, Sucuri, Wordfence, a hosting WAF): allow verified "
               "search engine crawlers, and do not challenge them."),
    Filter("ai_blocked", "AI Assistants Blocked Or Challenged", _has(("blocked", "challenged"), ("ai",)),
           issue_type="Issue", priority="Medium",
           description="A request sending the user agent of an AI assistant's crawler (ChatGPT, Perplexity, Claude) "
                       "was refused or got a challenge page. Those assistants then cannot read, quote or recommend "
                       "these pages.",
           fix="If you want to appear in AI answers, allow these crawlers in the CDN / firewall bot rules (Cloudflare: "
               "Security > Bots, 'Block AI bots' off) and in robots.txt."),
    Filter("training_blocked", "AI Training Crawlers Blocked", _has(("blocked", "challenged"), ("training",)),
           description="CCBot (Common Crawl, used to train AI models) was refused. This only affects model training "
                       "data, not search or AI answers; often deliberate."),
    Filter("noindex_for_crawler", "Noindex For One Crawler", _has(("noindex",)),
           issue_type="Issue", priority="High",
           description="A crawler gets a noindex (meta tag or X-Robots-Tag naming it, or served only to it) that a "
                       "browser does not see, so that search engine or assistant drops the page.",
           fix="Remove the crawler-specific noindex (an SEO plugin setting, a CDN rule or server code that varies "
               "headers by user agent), unless it is intended."),
    Filter("different_content", "Different Content For Crawlers", _has(("different",), skip=SKIP_DESKTOP),
           issue_type="Warning", priority="Medium",
           description="A crawler got a different status, title, H1, canonical or main text than a browser. Search "
                       "engines can treat serving different content to crawlers as cloaking, and the page may be "
                       "indexed with the wrong text.",
           fix="Serve crawlers the same HTML as visitors: check user-agent rules in the CDN, server, caching or "
               "pre-rendering service."),
    Filter("redirected", "Crawlers Redirected Elsewhere", _has(("redirected",), skip=SKIP_DESKTOP),
           issue_type="Warning", priority="Medium",
           description="A crawler was sent to a different URL than a browser (often a country, language or bot "
                       "page), so the page it indexes is not the one visitors see.",
           fix="Do not redirect by user agent; if you redirect by location, exclude crawlers or use hreflang."),
    Filter("thinner", "Thinner Page For Crawlers", _has(("thinner",), skip=SKIP_DESKTOP),
           issue_type="Warning", priority="Low",
           description="A crawler got less than 70% of the text a browser got (content left out for bots or loaded "
                       "only for real browsers).",
           fix="Make sure the full text is in the HTML for every visitor, crawlers included."),
    Filter("mobile_desktop", "Googlebot Mobile And Desktop Differ", _mobile_differs,
           issue_type="Warning", priority="Medium",
           description="Googlebot smartphone and Googlebot desktop got a different title, H1, canonical, URL or much "
                       "less text. Google indexes the mobile version, so whatever mobile lacks is lost.",
           fix="Give the mobile page the same content, headings and canonical as the desktop page."),
    Filter("needs_js", "Content Needs JavaScript", _js("needs_js"),
           issue_type="Warning", priority="Medium",
           description="Less than half of the page's text is in the HTML; the rest appears only after JavaScript runs. "
                       "Most AI crawlers do not run JavaScript, and Google renders later, so that text may be missed.",
           fix="Render the main content on the server (SSR / pre-rendering), or put it in the HTML."),
    Filter("noindex_removed_by_js", "Noindex Removed By JavaScript", _js("noindex_removed"),
           issue_type="Issue", priority="High",
           description="The HTML says noindex and JavaScript removes it later. Google may skip rendering a noindex "
                       "page, so it stays out of the index.",
           fix="Remove the noindex from the HTML itself if the page should be indexed."),
    Filter("robots_disallowed", "Disallowed In robots.txt For A Crawler", lambda r, site: bool(_robots_disallowed(r, site)),
           description="robots.txt tells at least one of the checked crawlers not to fetch this page (see the "
                       "Robots.txt tab for what that means)."),
    Filter("inconclusive", "Browser Refused Too", _has(("inconclusive",)),
           description="A browser request from this server was refused too, so the crawlers could not be compared "
                       "for this page (the server or CDN blocks automated requests from this server)."),
    Filter("all_same", "Same For Every Crawler", _all_same,
           description="Every crawler got the same page as a browser."),
]
for _f in BOT_FILTERS:
    _f.of_rows = _bot_row          # bot filters judge the sampled pages only

LLMS_TXT_FILTERS = [
    Filter("llms_txt_missing", "No llms.txt", _llms(_missing), of_rows=is_llms_row,
           description="The site has no /llms.txt (it answers 404 or 410). llms.txt is an optional Markdown file "
                       "that points AI tools at the pages that matter most and says in a line what the site is; "
                       "without it they work from the pages as they find them. Shown for information, not as an "
                       "issue."),
    Filter("llms_txt_html", "llms.txt Served As A Web Page", _llms(lambda d: d.get("state") == "html"),
           issue_type="Warning", priority="Medium", of_rows=is_llms_row,
           description="/llms.txt (or /llms-full.txt) answers with an HTML page instead of a Markdown text file, "
                       "often the site's 'page not found' page sent with status 200. AI tools that ask for the file "
                       "get a web page with nothing they can use.",
           fix="Publish a plain-text Markdown file at /llms.txt, or let the address answer 404 until you have one.",
           short_fix="Serve llms.txt as a Markdown text file, or answer 404"),
    Filter("llms_txt_invalid", "llms.txt Not In The Expected Format",
           _llms(lambda d: d.get("state") == "found" and bool(d.get("problems"))),
           issue_type="Warning", priority="Low", of_rows=is_llms_row,
           description="The llms.txt file is there but does not follow the format: it does not open with a '# Name' "
                       "heading, is sent with a type other than text/plain or text/markdown, or is over 1 MB. "
                       "The Findings column says which.",
           fix="Start the file with '# Site name', then an optional '> one-line summary' and '## Section' lists of "
               "'- [Page name](URL): note' lines; send it as text/plain or text/markdown and keep it short.",
           short_fix="Fix the llms.txt outline: '# Name', summary, link lists"),
    Filter("llms_txt_broken_links", "llms.txt Links To Broken Or Non-Indexable Pages", _llms(_broken_links),
           issue_type="Warning", priority="Medium", of_rows=is_llms_row,
           description="Links in llms.txt (or llms-full.txt) point to pages of the crawl that do not answer 200 or "
                       "cannot be indexed (broken, redirected, noindex, canonicalised, blocked). AI tools that follow "
                       "them reach the wrong page or none. Links the crawl did not request are counted as Not "
                       "checked, never as fine.",
           fix="Update each listed link to the final, indexable URL of the page, or remove it from the file.",
           short_fix="Point llms.txt links at working, indexable pages"),
]
FILTERS = BOT_FILTERS + LLMS_TXT_FILTERS

def _row_type(r, site) -> str:
    return LLMS_LABELS.get(r.kind, "File") if isinstance(r, SiteRow) else "Page"


def _llms_value(key: str):
    return lambda r, site: (r.data or {}).get(key, "") if is_llms_row(r) and (r.data or {}).get("state") == "found" \
        else ""


COLUMNS = [
    ADDRESS,
    Column("row_type", "Type", _row_type),
    Column("browser_status", "Browser Status", _baseline("status"), "status"),
    Column("browser_words", "Browser Words", _baseline("words"), "num"),
    Column("blocked_for", "Blocked", _cell_list(("blocked",))),
    Column("challenged_for", "Challenge Page", _cell_list(("challenged",))),
    Column("noindex_for", "Noindex For", _cell_list(("noindex",))),
    Column("different_for", "Different Content", _cell_list(("different",), skip=SKIP_DESKTOP)),
    Column("redirected_for", "Redirected", _cell_list(("redirected",), skip=SKIP_DESKTOP)),
    Column("thinner_for", "Thinner", _cell_list(("thinner",), skip=SKIP_DESKTOP)),
    Column("robots_disallows", "robots.txt Disallows", lambda r, site: ", ".join(_robots_disallowed(r, site))),
    Column("mobile_vs_desktop", "Mobile vs Desktop", _mobile),
    Column("raw_rendered_words", "Raw / Rendered Words", _js_words),
    Column("summary", "Findings", lambda r, site: llms_findings(r) if is_llms_row(r) else
           ("; ".join(_page(site, r).get("issues") or []) or "Same as a browser")),
    Column("llms_status", "llms.txt Status", lambda r, site: r.status_code if is_llms_row(r) else "", "status"),
    Column("llms_title", "llms.txt Title", _llms_value("title")),
    Column("llms_links", "llms.txt Links", _llms_value("links_total"), "num"),
    Column("llms_broken", "Broken Links", _llms_value("links_broken_total"), "num"),
    Column("llms_not_checked", "Links Not Checked", _llms_value("links_not_checked"), "num"),
    Column("indexability", INDEXABILITY.label,
           lambda r, site: "" if isinstance(r, SiteRow) else INDEXABILITY.get(r, site), INDEXABILITY.kind),
]

_JS_FILTERS = {"needs_js", "noindex_removed_by_js"}


def skip_reason(site, filter_id: str) -> str:
    """Why a filter of this tab is "Not checked" ("" when it ran)."""
    if filter_id in LLMS_FILTERS:
        return memo(site, f"llms_skip_{filter_id}", lambda: llms_skip_reason(site, filter_id))
    reason = not_run_reason(getattr(site, "bot_report", None), "search / AI bot check")
    if reason:
        return reason
    rep = _report(site) or {}
    if filter_id in _JS_FILTERS and not rep.get("rendered"):
        notes = [n for n in rep.get("notes") or [] if "JavaScript" in n]
        return notes[0] if notes else "The sample pages were not rendered in Chrome."
    if filter_id == "robots_disallowed" and not any((p.get("robots") or {}) for p in (rep.get("pages") or {}).values()):
        return "robots.txt was not read, so its rules are not shown per crawler."
    return ""


TAB = register(Tab(id="ai_crawlers", name="AI Crawlers", order=230, columns=COLUMNS, filters=FILTERS, rows=ai_rows,
                   total_label="All"))
TAB.skip_reason = skip_reason
