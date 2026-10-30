"""
Links tab (Screaming Frog "Links"): how each internal HTML page that loaded (2xx) links and is
linked: crawl depth, Link Score, inlinks and outlinks, with filters for linking problems.

How the counts match Screaming Frog (checked against a Screaming Frog 24.3 crawl in research/):
  - "All" lists HTML pages that answered 2xx (redirects and error pages are left out: 180 rows).
  - Inlinks / Outlinks / Unique Outlinks use the same counts as the Internal tab. Outlinks are
    <a>/<area> hyperlinks only (home page 138 / 54 unique in the reference); images, scripts and
    stylesheets the page loads are not outlinks.
  - External Outlinks counts external hyperlinks only (the reference shows 10 / 7 unique on most
    pages: the social and map links), not external scripts or stylesheets.
  - "% of Total" = loaded pages (2xx HTML, the rows of this tab) with a hyperlink to the URL / all
    loaded pages * 100 (images and scripts the pages load show 0, as in the reference). Both sides use the same pages, so a site-wide menu link shows exactly 100 even when
    error pages carry the menu too. Rounded to 2 decimals like the reference (42 of 180 pages
    shows 23.33, exported as "23.330").
  - Thresholds are Screaming Frog's default preferences: High Crawl Depth flags depth above 4 (the
    reference crawl reached depth 4 and flagged nothing), High External Outlinks flags more than 10
    followed external links (55 pages in the reference), High Internal Outlinks more than 1,000.
  - A link is nofollow when its rel says so or the page carries a robots nofollow/none directive.
  - A page's links to itself are inlinks, as in Screaming Frog: "Non-Indexable Page Inlinks Only" is 0
    on the reference because every blog post links to itself (an indexable source), although its only
    other inlink is a canonicalised /blogs/page/N/ listing.
  - Non-descriptive anchors (the built-in list plus CrawlConfig.non_descriptive_anchors) match the WHOLE anchor
    text (case and surrounding punctuation ignored):
    "Read more" matches, "Read More \u2192" and "read more about pricing" do not. Matching after
    stripping all punctuation flagged 86 pages where the reference flags 10.

  - "Pages With Uncrawlable Internal Outlinks": links on the site (or with no host) that the crawler
    could not turn into an address to request: javascript: pseudo-links, file paths on someone's computer
    and broken http(s):// prefixes (PageData malformed_hrefs that did not resolve to a link). Hrefs with
    spaces, backslashes or a bare email still resolve to a URL that is requested (and usually shows up as
    a 404), so they are not "uncrawlable"; the Hygiene tab lists them as Malformed Links.

Links that only exist after JavaScript runs are on the JavaScript tab (Contains JavaScript Links).

Inlinks by position (P16): "Inlinks From Content / Navigation / Footer / Other Positions" split the Inlinks count by
where the link sits on the page that holds it (extract.LinkPositioner: Configuration > Link Positions rules, then the
page's own structure); header, sidebar, breadcrumb, form and page-body links and the references that are not page
links (<head>, HTTP redirects, CSS background images) are "other", so the four add up to Inlinks. "Unique Pages
Linking From Content" counts the other pages with a content link to the URL (at most Unique Inlinks). "No Links From
Page Content" flags pages that other pages link to only from menus, headers, footers or sidebars (the start page is
left out). A crawl read before link positions existed shows no position counts and the filter is Not checked.
"""
import re
from typing import Any, Iterable, List, Set, Tuple
from urllib.parse import urlparse

from ...url_utils import clean_hostname
from ..registry import Column, Filter, Tab, links_carry, pref_number, register, seo, threshold_filter
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, content_inlink_pages, inlinks_by_position,
                       inlinks_count, link_index, memo, outlink_counts, unique_inlinks_count)
from .page_titles import content_rows

# Preferences defaults (high_crawl_depth / high_internal_outlinks / high_external_outlinks; the filters read the
# crawl's own numbers through registry.pref_number, P14)
HIGH_CRAWL_DEPTH = 4            # flag pages deeper than this (start page = 0)
HIGH_INTERNAL_OUTLINKS = 1000   # flag pages with more followed internal hyperlinks than this
HIGH_EXTERNAL_OUTLINKS = 10     # flag pages with more followed external hyperlinks than this

# Anchor texts that say nothing about the target (whole anchor, lowercased, edge punctuation trimmed)
NON_DESCRIPTIVE_ANCHORS = frozenset({
    "click here", "click this", "go", "here", "right here", "this", "this page", "this link",
    "link", "more", "more info", "more information", "information", "learn more", "read more",
    "see more", "find out more", "start",
})

_LOCAL_HOSTS = {"localhost", "0.0.0.0", "::1", "[::1]"}
_EDGE_PUNCT = " \t\r\n.,;:!?'\"()[]\u2018\u2019\u201c\u201d"


# ---------------------------------------------------------------------------------------------
# Link helpers
# ---------------------------------------------------------------------------------------------
def links_rows(site) -> List[Any]:
    return memo(site, "links_rows", lambda: content_rows(site))


def page_nofollow(p: dict) -> bool:
    """The page tells robots not to follow any of its links (meta robots / X-Robots-Tag)."""
    d = p.get("directives") or []
    return "nofollow" in d or "none" in d


def _hyperlinks(r) -> List[dict]:
    return [l for l in (seo(r).get("links") or []) if isinstance(l, dict) and l.get("url")]


def _followed(r) -> Tuple[List[dict], List[dict]]:
    """(followed internal hyperlinks, followed external hyperlinks) of a page."""
    if page_nofollow(seo(r)):
        return [], []
    internal, external = [], []
    for l in _hyperlinks(r):
        if l.get("nofollow"):
            continue
        (internal if l.get("internal") else external).append(l)
    return internal, external


def external_hyperlinks(r) -> Tuple[int, int]:
    """(external hyperlinks, unique external hyperlink URLs) of a page."""
    links = [l for l in _hyperlinks(r) if not l.get("internal")]
    return len(links), len({l["url"] for l in links})


def norm_anchor(text: str) -> str:
    """Anchor text for the non-descriptive check: lowercased, whitespace collapsed, quotes and
    sentence punctuation trimmed from the ends (inner characters and arrows are kept)."""
    return " ".join((text or "").lower().split()).strip(_EDGE_PUNCT)


def non_descriptive_anchors(site) -> frozenset:
    """The built-in generic anchors plus the team's own for this crawl (CrawlConfig.non_descriptive_anchors, P14:
    other languages, house phrases), each read the way norm_anchor reads a link's text."""
    def build():
        extra = getattr(getattr(site, "config", None), "non_descriptive_anchors", None) or []
        own = {norm_anchor(str(x)) for x in extra if isinstance(x, str)}
        return NON_DESCRIPTIVE_ANCHORS | frozenset(a for a in own if a)
    if site is None:
        return NON_DESCRIPTIVE_ANCHORS
    return memo(site, "non_descriptive_anchors", build)


def is_localhost(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host in _LOCAL_HOSTS or host.endswith(".localhost") or host.startswith("127.")


def hyperlink_sources(site, r) -> Set[str]:
    """Pages with a hyperlink (<a>/<area>) to this URL. Images, scripts and stylesheets a page loads
    do not count: Screaming Frog's "% of Total" is 0 for every image and script in the reference."""
    return {i.get("source") for i in (site.inlinks or {}).get(r.url, []) or [] if i.get("source")}


def _internal_inlinks(site, r) -> List[Tuple[dict, bool]]:
    """[(inlink, nofollow)] from crawled pages, the page's links to itself included (Screaming Frog
    counts a self-link as an inlink: a blog post linking to itself has 2 unique inlinks when one other
    page links to it); nofollow includes a page-level nofollow on the source."""
    out = []
    for i in (site.inlinks or {}).get(r.url, []) or []:
        src = i.get("source") or ""
        if not src:
            continue
        src_r = site.by_url.get(src)
        nf = bool(i.get("nofollow")) or (src_r is not None and page_nofollow(seo(src_r)))
        out.append((i, nf))
    return out


_HTTPISH = re.compile(r"^(?:https?|htp|htps|htttp|htttps|hhtp|hhtps|hhttp|hhttps|ttp|ttps|htpp|htpps)[:;]", re.I)
_RAW_HOST = re.compile(r"^[a-z]+[:;]+[/\\]*([^/\\?#\s:]+)", re.I)
_LOCAL_PATH = re.compile(r"^[a-z]:[\\/]", re.I)


def _raw_host(raw: str) -> str:
    """Host written in a broken absolute href ("http:/www.site.com/x", "https;//site.com"), "" when the href
    names no host (relative, javascript:, a file path, a bare word)."""
    low = (raw or "").strip().lower()
    if not low or low.startswith(("javascript:", "file:")) or _LOCAL_PATH.match(low) or not _HTTPISH.match(low):
        return ""
    m = _RAW_HOST.match(low)
    host = m.group(1) if m else ""
    return host if "." in host else ""


def uncrawlable_outlinks(r, site) -> List[dict]:
    """malformed_hrefs of the page that pointed at this site (or at no host) and never resolved to a link the
    crawler could request."""
    p = seo(r)
    bad = p.get("malformed_hrefs") or []
    if not bad:
        return []
    resolved = {l.get("raw") for l in p.get("links") or () if isinstance(l, dict)}
    base = clean_hostname(getattr(site, "base_domain", "") or "")
    out = []
    for m in bad:
        raw = str(m.get("raw") or "")
        if not raw or raw in resolved:
            continue
        host = _raw_host(raw)
        if host and base and clean_hostname(host) != base and not clean_hostname(host).endswith("." + base):
            continue                                    # a broken link to another website
        out.append(m)
    return out


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _high_depth(r, site) -> bool:
    d = site.crawl_depth.get(r.url)
    return d is not None and int(d) > pref_number(site, "high_crawl_depth")


def _no_internal_outlinks(r, site) -> bool:
    return not any(l.get("internal") and l["url"] != r.url for l in _hyperlinks(r))


def _internal_nofollow_outlinks(r, site) -> bool:
    internal = [l for l in _hyperlinks(r) if l.get("internal")]
    if not internal:
        return False
    return page_nofollow(seo(r)) or any(l.get("nofollow") for l in internal)


def _no_anchor(r, site) -> bool:
    return any(l.get("internal") and not (l.get("anchor") or "").strip() for l in _hyperlinks(r))


def _non_descriptive(r, site) -> bool:
    generic = non_descriptive_anchors(site)
    return any(l.get("internal") and norm_anchor(l.get("anchor") or "") in generic for l in _hyperlinks(r))


def _high_external(r, site) -> bool:
    return len(_followed(r)[1]) > pref_number(site, "high_external_outlinks")


def _high_internal(r, site) -> bool:
    return len(_followed(r)[0]) > pref_number(site, "high_internal_outlinks")


def _follow_and_nofollow_inlinks(r, site) -> bool:
    flags = {nf for _, nf in _internal_inlinks(site, r)}
    return flags == {True, False}


def _nofollow_inlinks_only(r, site) -> bool:
    flags = {nf for _, nf in _internal_inlinks(site, r)}
    return flags == {True}


def _localhost(r, site) -> bool:
    p = seo(r)
    urls: Iterable[str] = [l.get("url") or "" for l in p.get("links") or []]
    urls = list(urls) + [x.get("url") or "" for x in p.get("resources") or [] if isinstance(x, dict)]
    return any(is_localhost(u) for u in urls if u)


def positions_read(site) -> bool:
    """The crawl's links carry their position as the current extractor reads it (False for a crawl read before
    P16, whose position columns stay empty and whose No Links From Page Content is Not checked)."""
    return memo(site, "links_positions_read", lambda: links_carry(site, "position_rule") is not False)


def _by_position(group: str):
    def get(r, site):
        return inlinks_by_position(site, r)[group] if positions_read(site) else ""
    return get


def other_inlink_pages(site, r) -> int:
    """Pages other than the URL itself with any link or reference to it."""
    e = link_index(site).get(getattr(r, "url", "")) or {}
    return len((e.get("sources") or set()) - {getattr(r, "url", "")})


def _no_content_inlinks(r, site) -> bool:
    """Other pages link here, but none from its content (only menus, header, footer, sidebar...). The crawl's start
    pages (depth 0: the home page is reached from logos and menus by design) are left out."""
    if not site.crawl_depth.get(r.url, 1):
        return False
    return content_inlink_pages(site, r) == 0 and other_inlink_pages(site, r) > 0


def _non_indexable_inlinks_only(r, site) -> bool:
    if not site.indexable.get(r.url):
        return False
    sources: Set[str] = {i.get("source") for i, _ in _internal_inlinks(site, r)}
    return bool(sources) and not any(site.indexable.get(s) for s in sources)


# ---------------------------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------------------------
def _loaded_pages(site) -> Set[str]:
    """URLs of the internal HTML pages that loaded (2xx): the pages "% of Total" is measured against."""
    return memo(site, "links_loaded_pages", lambda: {p.url for p in links_rows(site)})


def pct_of_total(site, r) -> float:
    """Share of the loaded HTML pages that hyperlink to this URL. Numerator and denominator use the
    same set of pages, so links from error pages or redirects never push the value above 100."""
    loaded = _loaded_pages(site)
    if not loaded:
        return 0.0
    sources = hyperlink_sources(site, r) & loaded
    return round(len(sources) / len(loaded) * 100, 2)   # Screaming Frog rounds to 2 places (23.33)


COLUMNS = [
    ADDRESS, INDEXABILITY, INDEXABILITY_STATUS,
    Column("crawl_depth", "Crawl Depth", lambda r, site: site.crawl_depth.get(r.url, ""), "num"),
    Column("link_score", "Link Score", lambda r, site: site.link_score.get(r.url, ""), "num"),
    Column("inlinks", "Inlinks", lambda r, site: inlinks_count(site, r), "num"),
    Column("unique_inlinks", "Unique Inlinks", lambda r, site: unique_inlinks_count(site, r), "num"),
    Column("pct_of_total", "% of Total", lambda r, site: pct_of_total(site, r), "num"),
    Column("outlinks", "Outlinks", lambda r, site: outlink_counts(site, r)[0], "num"),
    Column("unique_outlinks", "Unique Outlinks", lambda r, site: outlink_counts(site, r)[1], "num"),
    Column("external_outlinks", "External Outlinks", lambda r, site: external_hyperlinks(r)[0], "num"),
    Column("unique_external_outlinks", "Unique External Outlinks",
           lambda r, site: external_hyperlinks(r)[1], "num"),
    Column("uncrawlable_outlinks", "Uncrawlable Outlinks", lambda r, site: len(uncrawlable_outlinks(r, site)), "num"),
    Column("inlinks_content", "Inlinks From Content", _by_position("content"), "num"),
    Column("inlinks_navigation", "Inlinks From Navigation", _by_position("navigation"), "num"),
    Column("inlinks_footer", "Inlinks From Footer", _by_position("footer"), "num"),
    Column("inlinks_other", "Inlinks From Other Positions", _by_position("other"), "num"),
    Column("unique_content_inlinks", "Unique Pages Linking From Content",
           lambda r, site: content_inlink_pages(site, r) if positions_read(site) else "", "num"),
]

FILTERS = [
    Filter("uncrawlable_outlinks", "Pages With Uncrawlable Internal Outlinks",
           lambda r, site: bool(uncrawlable_outlinks(r, site)), "Warning", "High",
           "Pages with links to their own site that cannot be followed: javascript: pseudo-links, file paths on "
           "someone's computer or a broken http:// prefix. Search engines cannot follow them, so whatever they "
           "point to gets no visit and no link value from here.",
           "Give each link a real address in href (an absolute or root-relative URL). Use a <button> for "
           "actions that are not navigation, and correct broken prefixes such as http:/ or https;//.",
           short_fix="Give these links a real URL in href; use <button> for actions",
           requires=("pagedata:has_doctype",)),
    threshold_filter("high_crawl_depth", "high_crawl_depth", "Pages With High Crawl Depth", _high_depth,
                     issue_type="Opportunity", priority="Medium",
                     description="Pages that take more than {n} clicks to reach from the start page. Pages buried "
                                 "deep in the site get less internal link value and are found less often by search "
                                 "engines and visitors.",
                     fix="Link important deep pages from higher-level pages (home page, main sections, hub pages "
                         "or related articles) so they sit fewer clicks from the start. Less important pages can "
                         "stay deeper."),
    Filter("no_internal_outlinks", "Pages Without Internal Outlinks", _no_internal_outlinks, "Warning", "High",
           "Pages that do not link to any other page on the site. Search engines and visitors reach a dead "
           "end here. This often happens when the menu or links are only added by JavaScript.",
           "Add normal <a href> links to related internal pages. If links are built by JavaScript, make sure "
           "they are present as real links in the HTML (or crawl with JavaScript rendering to confirm)."),
    Filter("internal_nofollow_outlinks", "Internal Nofollow Outlinks", _internal_nofollow_outlinks,
           "Warning", "Low",
           "Pages that link to other pages on the same site with rel=\"nofollow\" (or that tell robots not to "
           "follow any link on the page). Search engines usually ignore these links, so the linked pages get "
           "no value from them.",
           "Remove nofollow from links to your own pages unless you really want search engines to skip them. "
           "To keep a page out of the index, use noindex on that page instead."),
    Filter("no_anchor_text", "Internal Outlinks With No Anchor Text", _no_anchor, "Opportunity", "Low",
           "Pages with internal links that have no visible text, or image links whose image has no alt text. "
           "Without anchor text, visitors (including screen-reader users) and search engines get no hint "
           "about what the linked page is about.",
           "Give every internal link short, descriptive text. For image links, add alt text that describes "
           "the destination; for icon links, add visible or accessible text."),
    Filter("non_descriptive_anchor", "Non-Descriptive Anchor Text In Internal Outlinks", _non_descriptive,
           "Opportunity", "Low",
           "Pages with internal links whose text is generic, such as \"click here\", \"read more\" or "
           "\"learn more\". Generic text tells visitors and search engines nothing about the target page.",
           "Rewrite the link text to say what the visitor will find, e.g. \"read our pricing guide\" "
           "instead of \"read more\"."),
    threshold_filter("high_external_outlinks", "high_external_outlinks", "Pages With High External Outlinks",
                     _high_external, issue_type="Warning", priority="Low",
                     description="Pages with more than {n} followed links to other websites. This can be "
                                 "perfectly fine (sources, partners, social profiles) but is worth a check.",
                     fix="Make sure the external links go to trustworthy, relevant sites that help the visitor. "
                         "Remove links that add nothing, and add rel=\"nofollow\" or \"sponsored\" to paid or "
                         "untrusted links."),
    threshold_filter("high_internal_outlinks", "high_internal_outlinks", "Pages With High Internal Outlinks",
                     _high_internal, issue_type="Warning", priority="Low",
                     description="Pages with more than {n} followed links to other pages on the site. Very long "
                                 "link lists are hard to use and spread the page's link value very thin.",
                     fix="Trim the links to the ones visitors actually need (simplify mega menus, paginate long "
                         "lists) and keep the most important links prominent."),
    Filter("follow_nofollow_inlinks", "Follow & Nofollow Internal Inlinks To Page",
           _follow_and_nofollow_inlinks, "Warning", "Low",
           "Pages that some internal links point to normally and other internal links point to with "
           "nofollow. The mix is often a mistake in a template or menu.",
           "Check the nofollow links pointing at this page (see its inlinks) and remove nofollow unless it "
           "is intended."),
    Filter("nofollow_inlinks_only", "Internal Nofollow Inlinks Only", _nofollow_inlinks_only, "Warning", "Low",
           "Pages that are only linked from the site with nofollow links. Search engines may not find or "
           "value these pages through your own site.",
           "Remove nofollow from the internal links to this page, or add normal links from relevant pages. "
           "If the page should not be indexed, use noindex instead."),
    Filter("outlinks_to_localhost", "Outlinks To Localhost", _localhost, "Issue", "High",
           "Pages that link to or load files from localhost or 127.0.0.1, a developer's own computer. "
           "These links are broken for every real visitor and search engine.",
           "Replace the localhost addresses with the live URLs (usually left over from a development or "
           "staging copy of the site)."),
    Filter("no_content_inlinks", "No Links From Page Content", _no_content_inlinks, "Opportunity", "Low",
           "Pages that other pages of the site link to only from menus, the header, the footer, sidebars or "
           "breadcrumbs, never from the text of a page. Links inside the content show readers and search engines "
           "how pages relate and carry the most weight.",
           "Link to this page from the text of related pages (articles, guides, category descriptions) with "
           "anchor text that says what the page is about.",
           short_fix="Link to it from the text of related pages", requires=("linkdata:position_rule",)),
    Filter("non_indexable_inlinks_only", "Non-Indexable Page Inlinks Only", _non_indexable_inlinks_only,
           "Warning", "High",
           "Indexable pages that are only linked from pages that are themselves non-indexable (noindex, "
           "canonicalised to another URL, etc.). Search engines may visit those source pages rarely, so "
           "this page can be found late and get little link value.",
           "Add links to this page from indexable pages, such as a relevant category, hub or article page."),
]

register(Tab(id="links", name="Links", order=170, columns=COLUMNS, filters=FILTERS, rows=links_rows))
