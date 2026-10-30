"""
Plug-in registry for Screaming Frog-style tabs. Each module in crawler/seo/tabs/ defines one or more
Tab objects and calls register(tab). run_all(site) evaluates every filter once and returns a TabIndex
that the API, the dashboard and the Excel exporter all read.

A Tab is what Screaming Frog shows as a top tab (Page Titles, Canonicals, ...):
  - columns: what the grid shows for each URL in this tab
  - filters: the dropdown entries; each filter that is an issue also appears in the Issues overview
  - rows(site): which URLs the tab lists under "All" (default: internal HTML pages)
  - hidden: kept in the code but left out of the tab bar, the Overview, the Issues list, the checks and the
    reports (AMP: deprecated, user decision). tabs() and run_all() skip it; get_tab() still finds it.
  - requires / Filter.requires: what a check needs before it can run, e.g. "rendering" (the pages were
    rendered in Chrome), "config:custom_searches", "analysis:semantic_similarity", "integration:gsc",
    "pagedata:a11y", "linkdata:position_rule" (a key the current extractor writes on every link; P16). When a
    requirement is not met the filter is not evaluated: it counts 0 rows and
    TabResult.not_checked[filter id] says why, so the dashboard and the checks show "Not checked", never 0.
  - skip_reason(site, filter_id): a tab's own "not checked" reason (post-crawl checks that did not run)
  - dynamic(site) -> (columns, filters): per-crawl columns and filters (Custom Search / Extraction /
    JavaScript: one per configured rule). The static `filters` then hold every possible filter id (so ids
    stay valid for exports and dismissals) and run_all stores a per-crawl copy of the tab in TabResult.tab.
  - Filter.group: a sub-heading of the filter list ("WCAG AA / serious"); Filter.label(site): the filter's
    name for one crawl (a threshold the team changed: "Over 50 Characters"); the per-crawl copy of the tab
    in TabResult.tab carries that name, so every surface that reads the evaluated tab shows it. Ids never change.

Build 4 helpers (PLAN4_build.md 10.3):
  register_details(section, fn(site, url) -> dict | None) / details(site, url): per-URL detail sections
    (Structured Data, Accessibility, Spelling, Carbon ...) for the URL details pane and the AI tools; a section
    that could not be built says why ({"not_checked": reason}); None leaves the section out.
  pref(site, key): a threshold of Configuration > Preferences for this crawl (CrawlConfig.preferences), else
    today's default (crawler.config.DEFAULT_PREFERENCES); pref_number / pref_text read it as a number / as a
    filter name shows it, and threshold_filter(id, key, "Over {n} Characters", test, description) builds a filter
    whose name and description follow it (P14: Filter.label and Filter.describe; the id never changes).
"""
import dataclasses
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .model import EMPTY_PAGE, rendered_left_page

SHORT_FIX_MAX = 70          # Filter.short_fix length (the compact PDF prints one line per issue)


@dataclass
class Column:
    id: str
    label: str
    get: Callable[[Any, Any], Any]          # (result, site) -> value shown in the cell
    kind: str = "text"                       # text | url | num | status | bool | list


@dataclass
class Filter:
    id: str
    name: str                                # dropdown label, e.g. "Over 60 Characters"
    test: Callable[[Any, Any], bool]         # (result, site) -> bool
    issue_type: Optional[str] = None         # "Issue" | "Warning" | "Opportunity" | None (plain filter, not an issue)
    priority: Optional[str] = None           # "High" | "Medium" | "Low"
    description: str = ""                    # plain-language: what this means (our own words)
    fix: str = ""                            # plain-language: how to fix it
    only_indexable: bool = False             # Screaming Frog counts many content issues on indexable pages only
    short_fix: str = ""                      # <= 70 characters for the compact PDF ("" = derived from fix)
    requires: Tuple[str, ...] = ()           # requirements of this filter only (see the module docstring)
    of_rows: Optional[Callable[[Any], bool]] = None   # the rows this check covers: the "% of total" base when a
                                             # tab mixes kinds (Performance: pages and files); None = every row
    group: str = ""                          # sub-heading in the filter list, e.g. "WCAG AA / serious"
    label: Optional[Callable[[Any], str]] = None   # site -> the filter's name for this crawl (a changed threshold)
    describe: Optional[Callable[[Any], str]] = None   # site -> the description for this crawl (the same number, P14)


@dataclass
class Tab:
    id: str                                  # url-safe, e.g. "page_titles"
    name: str                                # "Page Titles"
    order: int                               # position in the tab bar (Screaming Frog order)
    columns: List[Column]
    filters: List[Filter]
    rows: Optional[Callable[[Any], List[Any]]] = None   # site -> results listed under "All"; default site.pages
    total_label: str = "All"
    hidden: bool = False                     # left out of tabs(), run_all() and every report (AMP)
    requires: Tuple[str, ...] = ()           # requirements of every filter of the tab
    skip_reason: Optional[Callable[[Any, str], str]] = None    # (site, filter_id) -> "" or why not checked
    dynamic: Optional[Callable[[Any], Tuple[List[Column], List[Filter]]]] = None   # per-crawl columns/filters
    description: str = ""                    # one line about the tab (Overview tooltip)
    static_filters: bool = False             # a dynamic tab whose registered filters are real (listed before
                                             # a crawl), not placeholder slots (Analytics: only columns vary)


_TABS: Dict[str, Tab] = {}


# ----------------------------------------------------------------------------------- ARACHNIX 1.0 switch
# Version 1.0 shows only finished features. Build 4 pieces that are not wired up yet stay in the code but are
# hidden everywhere (tab bar, All-tabs menu, Overview, Issues, checks, exports, PDFs, API Access).
# ONE switch turns them back on for 2.0: set V2_FEATURES = True here, or start the server with
# ARACHNIX_V2_FEATURES=1. The page reads the same lists from /api/integrations ("hidden_sources").
# 2.0 (29 Oct 2026): on by default now that every piece has its screen; ARACHNIX_V2_FEATURES=0 hides them again
V2_FEATURES = os.environ.get("ARACHNIX_V2_FEATURES", "1").strip().lower() not in ("0", "false", "no", "off")
V2_ONLY: Dict[str, Any] = {
    "tabs": ("ai", "link_metrics", "log_files"),                        # AI, Link Metrics, Log Files (P22)
    "filters": {"content": ("grammar_errors", "spelling_errors")},      # LanguageTool; spelling dictionary UI
    "columns": {"content": ("grammar_errors", "grammar_checked", "spelling_errors", "language_checked")},
    "api_sources": ("majestic", "ahrefs", "moz"),                       # Configuration > API Access ("later")
    "bulk_exports": ("spelling_errors", "grammar_errors"),
}


def v2_hidden(kind: str, item_id: str, tab_id: str = "") -> bool:
    """True when this item is 2.0-only and the 1.0 switch hides it. kind: tabs | filters | columns |
    api_sources | bulk_exports (filters and columns take their tab id)."""
    if V2_FEATURES:
        return False
    spec = V2_ONLY.get(kind) or ()
    if isinstance(spec, dict):
        spec = spec.get(tab_id) or ()
    return item_id in spec


def hidden_api_sources() -> List[str]:
    return [] if V2_FEATURES else list(V2_ONLY["api_sources"])


def _v1_view(tab: Tab) -> Tab:
    """The tab as version 1.0 shows it (see V2_FEATURES): 2.0-only tabs hidden, 2.0-only filters and columns
    left out. Returns the tab itself when nothing changes."""
    if V2_FEATURES:
        return tab
    changes: Dict[str, Any] = {}
    if v2_hidden("tabs", tab.id) and not tab.hidden:
        changes["hidden"] = True
    fl = [f for f in tab.filters if not v2_hidden("filters", f.id, tab.id)]
    if len(fl) != len(tab.filters):
        changes["filters"] = fl
    cl = [c for c in tab.columns if not v2_hidden("columns", c.id, tab.id)]
    if len(cl) != len(tab.columns):
        changes["columns"] = cl
    return dataclasses.replace(tab, **changes) if changes else tab


def register(tab: Tab) -> Tab:
    tab = _v1_view(tab)
    _TABS[tab.id] = tab
    return tab


def tabs(include_hidden: bool = False) -> List[Tab]:
    """Registered tabs in tab-bar order (hidden tabs only with include_hidden)."""
    _load_builtin()
    return sorted((t for t in _TABS.values() if include_hidden or not t.hidden), key=lambda t: t.order)


def all_tabs() -> List[Tab]:
    return tabs(include_hidden=True)


def get_tab(tab_id: str) -> Optional[Tab]:
    _load_builtin()
    return _TABS.get(tab_id)


_loaded = False
_load_lock = threading.RLock()
_LOADED_MODULES: List[str] = []          # tab modules imported (each registers its checks)
_LOAD_FAILURES: Dict[str, str] = {}      # tab module -> "ExceptionType: message" (its checks are MISSING)

# A tab module that fails to import means every crawl silently lacks its checks. Server start (web/app.py)
# and the command line (cli.py) refuse to run then (require_all_tabs); this switch turns that into a loud
# warning instead. Hidden 2.0 tabs (V2_ONLY) still import and register: hidden is not failed.
ALLOW_BROKEN_ENV = "ARACHNIX_ALLOW_BROKEN_TABS"


class TabLoadError(RuntimeError):
    """One or more tab modules failed to import (their checks would be missing from every crawl)."""


def _load_builtin():
    """Import every module in crawler/seo/tabs so they register themselves. Thread-safe: a caller
    arriving while another thread loads waits, and _loaded is set only once every module is in.
    A tab module that fails to import is recorded in _LOAD_FAILURES (load_failures(), /healthz, the
    "checks incomplete" flag on every index, export and PDF) and the other tabs keep working; server start
    and the CLI refuse to run with a failure (require_all_tabs)."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        import importlib
        import pkgutil
        import sys
        from . import tabs as tabs_pkg
        for mod in pkgutil.iter_modules(tabs_pkg.__path__):
            try:
                importlib.import_module(f"{tabs_pkg.__name__}.{mod.name}")
                _LOADED_MODULES.append(mod.name)
            except Exception as e:      # report it loudly, keep the other tabs
                _LOAD_FAILURES[mod.name] = f"{type(e).__name__}: {e}"
                print(f"[seo.registry] ERROR: tab module {mod.name} failed to load: {type(e).__name__}: {e} "
                      f"- its checks are MISSING from every crawl", file=sys.stderr)
        for tab in _TABS.values():
            for f in tab.filters:
                if not f.short_fix:
                    f.short_fix = SHORT_FIXES.get((tab.id, f.id), "")
        _loaded = True


def load_failures() -> Dict[str, str]:
    """{tab module: "ExceptionType: message"} for every tab module that failed to import ({} when all loaded)."""
    _load_builtin()
    return dict(_LOAD_FAILURES)


def loaded_modules() -> List[str]:
    """The tab modules that imported (and registered their checks), in load order."""
    _load_builtin()
    return list(_LOADED_MODULES)


def checks_status() -> Dict[str, Any]:
    """{"complete", "loaded": [module], "failed": {module: error}} for /healthz, crawls and snapshots."""
    failed = load_failures()
    return {"complete": not failed, "loaded": loaded_modules(), "failed": failed}


def checks_warning(failed: Optional[Dict[str, str]] = None) -> str:
    """One warning line for exports and PDFs when check modules are missing ("" when every check loaded).
    `failed` defaults to this process's load failures (every index here is built by this process)."""
    failed = load_failures() if failed is None else failed
    if not failed:
        return ""
    names = ", ".join(f"{m} ({err[:120]})" for m, err in sorted(failed.items()))
    return (f"WARNING: checks incomplete - {len(failed)} check module(s) failed to load: {names}. Issues from "
            f"these checks are missing from this report; fix the error and restart ARACHNIX, then re-run the crawl.")


def allow_broken_tabs() -> bool:
    return os.environ.get(ALLOW_BROKEN_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def require_all_tabs(where: str = "ARACHNIX") -> Dict[str, str]:
    """Called at server start and by the CLI: raises TabLoadError naming each tab module that failed to import
    and its exception, unless ARACHNIX_ALLOW_BROKEN_TABS=1, which prints a loud warning instead. Returns the
    failures ({} when every module loaded)."""
    import sys
    failed = load_failures()
    if not failed:
        return failed
    lines = "\n".join(f"  - crawler/seo/tabs/{m}.py: {err}" for m, err in sorted(failed.items()))
    msg = (f"{where}: {len(failed)} SEO check module(s) failed to load, so crawls would silently miss their "
           f"checks:\n{lines}")
    if allow_broken_tabs():
        bar = "!" * 78
        print(f"\n{bar}\nWARNING {msg}\nStarting anyway because {ALLOW_BROKEN_ENV}=1. Reports will be flagged "
              f"\"checks incomplete\".\n{bar}\n", file=sys.stderr, flush=True)
        return failed
    raise TabLoadError(f"{msg}\nFix the error above, or set {ALLOW_BROKEN_ENV}=1 to start anyway (reports are "
                       f"then flagged \"checks incomplete\").")


# One-line fixes (<= SHORT_FIX_MAX characters) for the compact PDF, for filters defined without short_fix
SHORT_FIXES: Dict[Tuple[str, str], str] = {
    ("security", "http_urls"): "Serve every page over HTTPS and 301-redirect HTTP to HTTPS",
    ("security", "mixed_content"): "Load images, scripts and styles over https://",
    ("security", "form_url_insecure"): "Point the form's action at an https:// address",
    ("security", "form_on_http"): "Move the page to HTTPS and redirect the HTTP version",
    ("security", "unsafe_cross_origin"): "Add rel=\"noopener\" to links that open in a new tab",
    ("security", "protocol_relative"): "Write resource URLs in full with https://",
    ("security", "missing_hsts"): "Send a Strict-Transport-Security header",
    ("security", "missing_csp"): "Add a Content-Security-Policy header",
    ("security", "missing_xcto"): "Send X-Content-Type-Options: nosniff",
    ("security", "missing_xfo"): "Send X-Frame-Options: SAMEORIGIN",
    ("security", "missing_referrer_policy"): "Send Referrer-Policy: strict-origin-when-cross-origin",
    ("security", "bad_content_type"): "Send each file with its correct Content-Type",
    ("response_codes", "internal_blocked_robots"): "Unblock in robots.txt or stop linking to these URLs",
    ("response_codes", "internal_no_response"): "Check the server, firewall and SSL for these URLs",
    ("response_codes", "internal_redirection"): "Link straight to the final URL instead of the redirect",
    ("response_codes", "internal_meta_refresh"): "Replace the meta refresh with a 301 redirect",
    ("response_codes", "internal_http_refresh"): "Replace the Refresh header with a 301 redirect",
    ("response_codes", "internal_redirect_chain"): "Redirect straight to the final URL in one hop",
    ("response_codes", "internal_redirect_loop"): "Fix the redirect rules so each URL ends on a 200 page",
    ("response_codes", "internal_client_error"): "Fix or remove the links, or 301-redirect the URL",
    ("response_codes", "internal_server_error"): "Check the server error logs and fix the failing code",
    ("response_codes", "external_no_response"): "Check the link in a browser; replace it if the site is gone",
    ("response_codes", "external_client_error"): "Replace or remove the broken external link",
    ("response_codes", "external_server_error"): "Re-check later; replace the link if it keeps failing",
    ("url", "non_ascii"): "Use plain a-z, 0-9 and hyphens in URLs",
    ("url", "underscores"): "Use hyphens instead of underscores in new URLs",
    ("url", "uppercase"): "Use lowercase URLs and redirect the uppercase versions",
    ("url", "multiple_slashes"): "Fix the template that builds '//' and redirect it",
    ("url", "repetitive_path"): "Make relative links absolute (start with /)",
    ("url", "contains_space"): "Use hyphens instead of spaces and redirect the old URL",
    ("url", "internal_search"): "Noindex search result pages and stop linking to them",
    ("url", "parameters"): "Link to clean URLs; canonicalise parameter versions",
    ("url", "ga_tracking"): "Remove utm_/_ga parameters from internal links",
    ("url", "over_115"): "Use shorter, descriptive slugs for new pages",
    ("page_titles", "missing"): "Add a short, unique <title> to the page",
    ("page_titles", "duplicate"): "Give every page its own title",
    ("page_titles", "over_60_characters"): "Shorten the title; put key words first",
    ("page_titles", "below_30_characters"): "Make the title more descriptive",
    ("page_titles", "over_561_pixels"): "Shorten the title so it is not cut off in results",
    ("page_titles", "below_200_pixels"): "Write a fuller, more descriptive title",
    ("page_titles", "same_as_h1"): "Optional: vary the title and H1 slightly",
    ("page_titles", "multiple"): "Keep exactly one <title> in the <head>",
    ("page_titles", "outside_head"): "Move the <title> into the <head>",
    ("meta_description", "missing"): "Write a 1-2 sentence meta description",
    ("meta_description", "duplicate"): "Write a unique meta description for each page",
    ("meta_description", "over_155_characters"): "Shorten the meta description to about 155 characters",
    ("meta_description", "below_70_characters"): "Expand the meta description with a reason to click",
    ("meta_description", "over_985_pixels"): "Trim the meta description so it is not cut off",
    ("meta_description", "below_400_pixels"): "Write a fuller meta description",
    ("meta_description", "multiple"): "Keep a single meta description in the <head>",
    ("meta_description", "outside_head"): "Move the meta description into the <head>",
    ("h1", "missing"): "Add one clear <h1> naming the page's topic",
    ("h1", "duplicate"): "Give each page its own H1",
    ("h1", "over_70_characters"): "Shorten the H1 to the core topic",
    ("h1", "multiple"): "Keep one H1; turn the others into H2s",
    ("h1", "alt_text_in_h1"): "Move the image (logo) out of the H1; use text",
    ("h1", "non_sequential"): "Make the H1 the first heading on the page",
    ("h2", "missing"): "Add H2 subheadings for the main sections",
    ("h2", "duplicate"): "Make page-specific H2s unique",
    ("h2", "over_70_characters"): "Shorten the H2 to the point of the section",
    ("h2", "multiple"): "Usually fine; keep H2s for real sections",
    ("h2", "non_sequential"): "Put the first H2 after the H1",
    ("content", "exact_duplicates"): "Keep one page; redirect or canonicalise the copies",
    ("content", "near_duplicates"): "Make each page distinct or merge them",
    ("content", "low_content_pages"): "Add useful content where it helps the visitor",
    ("content", "soft_404_pages"): "Return a real 404 for error pages, or reword the page",
    ("content", "readability_difficult"): "Use shorter sentences and simpler words",
    ("content", "readability_very_difficult"): "Split long sentences and replace jargon",
    ("content", "lorem_ipsum_placeholder"): "Replace the placeholder text with real content",
    ("images", "over_100_kb"): "Compress and resize the image (WebP/AVIF)",
    ("images", "missing_alt_text"): "Write alt text for meaningful images",
    ("images", "missing_alt_attribute"): "Add an alt attribute (alt=\"\" if decorative)",
    ("images", "alt_text_over_100_characters"): "Shorten the alt text to one sentence",
    ("images", "missing_size_attributes"): "Add width and height to the <img> tag",
    ("canonicals", "canonicalised"): "Check the canonical target is the page you want ranked",
    ("canonicals", "missing"): "Add a self-referencing canonical link",
    ("canonicals", "multiple"): "Keep a single canonical per page",
    ("canonicals", "multiple_conflicting"): "Remove the conflicting canonicals; keep one",
    ("canonicals", "non_indexable_canonical"): "Point the canonical at a working, indexable URL",
    ("canonicals", "canonical_is_relative"): "Write the canonical as an absolute https:// URL",
    ("canonicals", "unlinked"): "Link to the canonical URL from your pages",
    ("canonicals", "invalid_attribute"): "Remove the extra attribute from the canonical link",
    ("canonicals", "contains_fragment"): "Remove the #fragment from the canonical URL",
    ("canonicals", "outside_head"): "Move the canonical link into the <head>",
    ("pagination", "not_in_anchor"): "Link next/previous pages with plain <a href> links",
    ("pagination", "non_200"): "Point pagination links at working, final URLs",
    ("pagination", "unlinked"): "Link every page of the series with <a href>",
    ("pagination", "non_indexable"): "Make paginated pages indexable and self-canonical",
    ("pagination", "multiple"): "Keep one rel=next and one rel=prev per page",
    ("pagination", "loop"): "End the series: no rel=next on the last page",
    ("pagination", "sequence_error"): "Make rel=next and rel=prev point back to each other",
    ("directives", "noindex"): "Remove noindex from pages that should be in search",
    ("directives", "nofollow"): "Remove page-level nofollow unless intended",
    ("directives", "none"): "Remove robots 'none' unless the page must be hidden",
    ("directives", "nosnippet"): "Remove nosnippet unless previews must be hidden",
    ("directives", "noodp"): "Remove noodp; it does nothing",
    ("directives", "noydir"): "Remove noydir; it does nothing",
    ("directives", "noimageindex"): "Remove noimageindex unless intended",
    ("directives", "notranslate"): "Remove notranslate unless translation is wrong",
    ("directives", "unavailable_after"): "Check the unavailable_after date is right",
    ("directives", "outside_head"): "Move the robots meta tag into the <head>",
    ("hreflang", "non_200"): "Point hreflang at working, final URLs",
    ("hreflang", "unlinked"): "Link language versions to each other with <a href>",
    ("hreflang", "missing_return_links"): "Add matching hreflang links on every version",
    ("hreflang", "inconsistent_return_links"): "Use the same language code for a URL everywhere",
    ("hreflang", "non_canonical_return_links"): "Use canonical URLs in hreflang",
    ("hreflang", "noindex_return_links"): "Remove noindex or drop the URL from hreflang",
    ("hreflang", "incorrect_codes"): "Fix the language/region codes (e.g. en-gb)",
    ("hreflang", "multiple_entries"): "Keep one URL per language code",
    ("hreflang", "missing_self_reference"): "Add an hreflang link to the page itself",
    ("hreflang", "not_using_canonical"): "Put hreflang on canonical URLs only",
    ("hreflang", "missing_x_default"): "Add an hreflang x-default link",
    ("hreflang", "outside_head"): "Move hreflang links into the <head>",
    ("links", "high_crawl_depth"): "Link deep pages from higher-level pages",
    ("links", "no_internal_outlinks"): "Add <a href> links to related pages",
    ("links", "internal_nofollow_outlinks"): "Remove nofollow from internal links",
    ("links", "no_anchor_text"): "Give every internal link descriptive text",
    ("links", "non_descriptive_anchor"): "Replace 'click here' with descriptive link text",
    ("links", "high_external_outlinks"): "Keep only useful, trusted external links",
    ("links", "high_internal_outlinks"): "Trim long link lists and mega menus",
    ("links", "follow_nofollow_inlinks"): "Remove unintended nofollow from links to the page",
    ("links", "nofollow_inlinks_only"): "Add followed internal links to the page",
    ("links", "outlinks_to_localhost"): "Replace localhost links with live URLs",
    ("links", "non_indexable_inlinks_only"): "Link to the page from indexable pages",
    ("sitemaps", "not_in_sitemap"): "Add indexable pages to the XML sitemap",
    ("sitemaps", "orphan"): "Link to the page, or remove it from the sitemap",
    ("sitemaps", "non_indexable_in_sitemap"): "List only final, indexable URLs in the sitemap",
    ("sitemaps", "multiple_sitemaps"): "List each URL in one sitemap only",
    ("sitemaps", "over_50k_urls"): "Split the sitemap into files of up to 50,000 URLs",
    ("sitemaps", "over_50mb"): "Split the sitemap into files under 50MB",
    ("validation", "invalid_head_elements"): "Move non-head elements out of the <head>",
    ("validation", "body_before_html"): "Start the document with <!DOCTYPE html><html>",
    ("validation", "head_not_first"): "Make <head> the first element inside <html>",
    ("validation", "missing_head"): "Add a <head> element to the template",
    ("validation", "multiple_head"): "Merge into a single <head>",
    ("validation", "missing_body"): "Wrap the content in a single <body>",
    ("validation", "multiple_body"): "Keep a single <body> per page",
    ("validation", "html_over_2mb"): "Reduce HTML size: move inline code and data out",
    ("validation", "resource_over_2mb"): "Split or trim the large script/stylesheet",
    ("ai_crawlers", "search_blocked"): "Allow search crawlers in the firewall/CDN bot settings",
    ("ai_crawlers", "ai_blocked"): "Allow AI crawlers if you want to appear in AI answers",
    ("ai_crawlers", "noindex_for_crawler"): "Remove the crawler-specific noindex",
    ("ai_crawlers", "different_content"): "Serve crawlers the same HTML as visitors",
    ("ai_crawlers", "redirected"): "Do not redirect by user agent",
    ("ai_crawlers", "thinner"): "Put the full text in the HTML for every visitor",
    ("ai_crawlers", "mobile_desktop"): "Give mobile the same content as desktop",
    ("ai_crawlers", "needs_js"): "Render main content on the server (SSR)",
    ("ai_crawlers", "noindex_removed_by_js"): "Remove noindex from the HTML itself",
    ("robots_txt", "search_blocked"): "Remove 'Disallow: /' for search crawlers",
    ("robots_txt", "ai_blocked"): "Allow AI crawlers in robots.txt if you want AI traffic",
    ("robots_txt", "no_sitemap_line"): "Add a 'Sitemap:' line to robots.txt",
    ("robots_txt", "robots_unreachable"): "Make /robots.txt answer 200 (or 404) quickly",
    ("robots_txt", "robots_html"): "Serve robots.txt as plain text, or answer 404",
    ("robots_txt", "robots_too_big"): "Shorten robots.txt below 500 KB",
    ("robots_txt", "ai_disallowed_pages"): "Review Disallow rules for AI crawlers",
    ("robots_txt", "data_nosnippet"): "Use data-nosnippet only on text you must hide",
    ("robots_txt", "scoped_noindex"): "Remove the crawler-specific noindex",
    ("robots_txt", "max_snippet_low"): "Remove max-snippet or set it to -1",
    ("robots_txt", "unavailable_after_passed"): "Remove the expired unavailable_after directive",
    ("robots_txt", "conflicting_index"): "Set one robots directive per page, in one place",
    ("hygiene", "staging_links"): "Replace staging/private links with live URLs",
    ("hygiene", "dead_anchors"): "Add the missing id or fix the #link",
    ("hygiene", "http_internal_links"): "Change internal links to https://",
    ("hygiene", "malformed_links"): "Correct the href (mailto: for emails)",
    ("hygiene", "javascript_links"): "Use a real href, or a <button> for actions",
    ("hygiene", "no_compression"): "Turn on gzip/brotli for HTML",
    ("hygiene", "no_cache_control"): "Send a Cache-Control header for HTML",
    ("hygiene", "missing_doctype"): "Start the page with <!DOCTYPE html>",
    ("hygiene", "no_charset"): "Add <meta charset=\"utf-8\">",
    ("hygiene", "non_utf8_charset"): "Use UTF-8 and declare charset=utf-8",
    ("hygiene", "large_dom"): "Simplify the template: fewer nested elements",
    ("hygiene", "cross_host_canonical"): "Point the canonical at this page's own host",
    ("hygiene", "crawl_trap_url"): "Fix relative links; block trap parameters",
    ("hygiene", "soft_404_server"): "Answer HTTP 404 for URLs that do not exist",
    ("hygiene", "infinite_url_space"): "Answer 404 for unknown sub-paths",
    ("hygiene", "unknown_redirect_home"): "Answer 404 instead of redirecting home",
    ("hygiene", "unknown_server_error"): "Make unknown URLs answer 404, not 5xx",
    ("parameters", "session"): "Keep sessions in cookies, never in URLs",
    ("parameters", "action"): "Make cart/action links nofollow or buttons",
    ("parameters", "indexable_variants"): "Canonicalise parameter URLs to the clean page",
    ("parameters", "builder_pagination"): "Block the page-builder parameter in robots.txt",
}


# ---------------------------------------------------------------------------------------------
# Helpers for tab modules
# ---------------------------------------------------------------------------------------------
def seo(r) -> dict:
    """PageData of a result (EMPTY_PAGE when the URL had no parsable body)."""
    return getattr(r, "seo", None) or EMPTY_PAGE


def first(lst, default=""):
    return lst[0] if lst else default


def is_indexable(site, r) -> bool:
    return bool(site.indexable.get(r.url, False))


def rendered(r) -> Optional[dict]:
    """PageData["rendered"] of a result (None when the page was not rendered in Chrome)."""
    v = seo(r).get("rendered")
    return v if isinstance(v, dict) else None


def rendered_content(r) -> Optional[dict]:
    """rendered(r) when Chrome stayed on the crawled page; None when the render ended on another document (a
    JavaScript / meta refresh / HTTP refresh redirect fired), whose title, links, DOM checks and image sizes
    belong to the destination and must not be judged as this page's (the page is then judged from its HTML)."""
    rd = rendered(r)
    return None if rd is None or rendered_left_page(rd) else rd


def short_fix_of(f: Filter) -> str:
    """The filter's one-line fix (<= SHORT_FIX_MAX characters): short_fix, else the first sentence of fix,
    else fix cut at a word boundary."""
    if f.short_fix:
        return f.short_fix[:SHORT_FIX_MAX]
    text = " ".join((f.fix or "").split())
    if not text:
        return ""
    sentence = text.split(". ", 1)[0].rstrip(".")
    if len(sentence) <= SHORT_FIX_MAX:
        return sentence
    cut = sentence[:SHORT_FIX_MAX - 3].rsplit(" ", 1)[0].rstrip(",;:(")
    return cut + "..."


# ---------------------------------------------------------------------------------------------
# Requirements ("Not checked" before a check runs)
# ---------------------------------------------------------------------------------------------
RENDER_REASON = ("Needs JavaScript rendering (Configuration > Rendering: render every page in Chrome); "
                 "no page of this crawl was rendered.")
OLD_BUILD_REASON = "These pages were read by an older version of the crawler; run a new crawl to check this."
NOT_CONNECTED = "Not connected - connect it under Configuration > API Access"

# config attribute -> why its checks did not run when it is off / empty
CONFIG_REASONS: Dict[str, str] = {
    "respect_robots": "robots.txt was ignored in this crawl ('Respect robots.txt' was off).",
    "crawl_resources": "Images, scripts and stylesheets were not requested (resource crawling was off).",
    "custom_searches": "No Custom Search rule is set (Configuration > Custom Search).",
    "custom_extractors": "No Custom Extraction rule is set (Configuration > Custom Extraction).",
    "custom_js": "No Custom JavaScript snippet is set (Configuration > Custom JavaScript).",
    "render_all": RENDER_REASON,
    "use_sitemaps": "Sitemap discovery was turned off for this crawl.",
    "spelling": "The spelling check was off for this crawl (Configuration > Content > Spelling).",
    "a11y_audit": ("The accessibility audit (axe-core) was off for this crawl: turn it on, or render every page "
                   "in Chrome."),
    "carbon": "The carbon rating was off for this crawl (Configuration > Preferences).",
    "check_external_links": "External links were not checked (turn on 'Check external links' in the crawl settings).",
}
# PageData key -> why its checks did not run when no page of the crawl carries it (None on every page)
PAGEDATA_REASONS: Dict[str, str] = {
    "spelling": "The spelling check did not run for this crawl (it was off, or the pages were read by an older "
                "version of the crawler).",
    "axe": ("The accessibility audit (axe-core) did not run for this crawl: turn on the accessibility audit or "
            "render every page in Chrome."),
    "pdf": "The properties of the PDF files were not read in this crawl (run a new crawl to read them).",
}
FILE_PAGEDATA = {"pdf"}      # PageData keys of files (PDFs), not of HTML pages
ANALYSIS_LABELS: Dict[str, str] = {
    "link_score": "Link Score", "sitemaps": "Sitemaps", "near_duplicates": "Near Duplicates",
    "pagination": "Pagination", "hreflang": "Hreflang", "semantic_similarity": "Semantic Similarity",
}
# rendered-dict key -> what Chrome could not measure
RENDER_KEY_LABELS: Dict[str, str] = {
    "mobile": "the phone-size measurements", "a11y_rendered": "the accessibility tree",
    "chrome_issues_checked": "the DevTools issues", "img_sizes": "the image sizes",
}

_RESOLVERS: Dict[str, Callable[[Any, str, Sequence[Any]], str]] = {}


def register_requirement(kind: str, fn: Callable[[Any, str, Sequence[Any]], str]) -> None:
    """Adds (or replaces) the resolver of one requirement kind: fn(site, arg, rows) -> "" when met, else the
    plain reason. The integrations package registers "integration" to report its own connection state."""
    _RESOLVERS[kind] = fn


def _cfg(site):
    return getattr(site, "config", None)


def _pages_of(site, rows: Sequence[Any]) -> List[Any]:
    """The HTML pages among a tab's rows, or the site's pages when the tab lists something else (images)."""
    pages = [r for r in rows if seo(r).get("is_html")]
    return pages or list(getattr(site, "pages", None) or [])


def _rendering(site, arg: str, rows: Sequence[Any]) -> str:
    done = [r for r in _pages_of(site, rows) if rendered(r) is not None]
    if not done:
        return RENDER_REASON
    if arg:
        if not any(rendered(r).get(arg) not in (None, False) for r in done):
            what = RENDER_KEY_LABELS.get(arg, arg.replace("_", " "))
            return f"The pages were rendered in Chrome, but {what} could not be read (see the render notes)."
    return ""


def _config(site, arg: str, rows: Sequence[Any]) -> str:
    cfg = _cfg(site)
    if cfg is None or not hasattr(cfg, arg):
        return ""          # no config recorded (older saved crawl) or no such setting: judge from the data
    value = getattr(cfg, arg, None)
    if isinstance(value, dict) and "enabled" in value:      # {"enabled": bool, ...} options (spelling, carbon)
        value = value.get("enabled")
    if not value:
        return CONFIG_REASONS.get(arg, f"'{arg.replace('_', ' ')}' was off for this crawl.")
    return ""


def _analysis(site, arg: str, rows: Sequence[Any]) -> str:
    skipped = getattr(site, "analysis_skipped", None) or {}
    if arg in skipped:
        return skipped[arg]
    cfg = _cfg(site)
    steps = getattr(cfg, "analysis", None) if cfg is not None else None
    if isinstance(steps, dict) and steps.get(arg) is False:
        return (f"The Crawl Analysis step '{ANALYSIS_LABELS.get(arg, arg)}' was turned off "
                "(Crawl Analysis > Configure).")
    return ""


def _integration(site, arg: str, rows: Sequence[Any]) -> str:
    """site.integrations: {name: {"connected": bool, "reason": str}} (filled by the integrations package)."""
    info = (getattr(site, "integrations", None) or {}).get(arg)
    if not isinstance(info, dict):
        return NOT_CONNECTED
    if not info.get("connected"):
        return str(info.get("reason") or NOT_CONNECTED)
    return str(info.get("reason") or "")    # connected; a reason = no data fetched for this crawl yet


def _pagedata(site, arg: str, rows: Sequence[Any]) -> str:
    """A PageData key filled by the current extractor; pages from an older build lack it (None). A key whose
    value says the step did not run ({"checked": False} / {"ran": False} with a reason) on every page gives
    that reason (e.g. "Spelling check not installed")."""
    pages = [r for r in rows if getattr(r, "seo", None)] if arg in FILE_PAGEDATA else _pages_of(site, rows)
    if not pages:
        return ""
    values = [seo(r).get(arg) for r in pages]
    if all(v is None for v in values):
        return PAGEDATA_REASONS.get(arg, OLD_BUILD_REASON)
    if all(v is None or (isinstance(v, dict) and (v.get("checked") is False or v.get("ran") is False))
           for v in values):
        reasons = [str(v.get("reason") or "") for v in values if isinstance(v, dict) and v.get("reason")]
        if reasons:
            return max(sorted(set(reasons)), key=reasons.count)
        return PAGEDATA_REASONS.get(arg, OLD_BUILD_REASON)
    return ""


LINKDATA_REASONS: Dict[str, str] = {
    "position_rule": ("The link positions of these pages were read by an older version of the crawler; run a new "
                      "crawl to check this."),
}


def links_carry(site, key: str, rows: Sequence[Any] = ()) -> Optional[bool]:
    """True when the pages' links carry `key` (read by the current extractor), False when they do not (an older
    crawl), None when no page has a link to look at."""
    for r in _pages_of(site, rows):
        for link in seo(r).get("links") or ():
            if isinstance(link, dict):
                return key in link
    return None


def _linkdata(site, arg: str, rows: Sequence[Any]) -> str:
    """A key every link of the current extractor carries (links[].position_rule); pages read by an older build lack
    it."""
    if links_carry(site, arg, rows) is False:
        return LINKDATA_REASONS.get(arg, OLD_BUILD_REASON)
    return ""


register_requirement("rendering", _rendering)
register_requirement("config", _config)
register_requirement("analysis", _analysis)
register_requirement("integration", _integration)
register_requirement("pagedata", _pagedata)
register_requirement("linkdata", _linkdata)


def requirement_reason(req: str, site, rows: Sequence[Any] = ()) -> str:
    """"" when the requirement is met, else the plain reason the check could not run."""
    kind, _, arg = (req or "").partition(":")
    fn = _RESOLVERS.get(kind)
    if fn is None:
        return ""
    try:
        return fn(site, arg, rows) or ""
    except Exception:
        return ""


def unmet(reqs: Sequence[str], site, rows: Sequence[Any], cache: Optional[Dict[str, str]] = None) -> str:
    """Reason of the first requirement in `reqs` that is not met ("" when all are)."""
    for req in reqs or ():
        if cache is not None and req in cache:
            why = cache[req]
        else:
            why = requirement_reason(req, site, rows)
            if cache is not None:
                cache[req] = why
        if why:
            return why
    return ""


# ---------------------------------------------------------------------------------------------
# Preferences (thresholds) and per-URL detail sections
# ---------------------------------------------------------------------------------------------
def pref(site, key: str) -> Any:
    """A threshold for this crawl: site.preferences[key] (CrawlConfig.preferences), else today's default."""
    from ..config import DEFAULT_PREFERENCES
    for prefs in (getattr(site, "preferences", None), getattr(_cfg(site), "preferences", None)):
        if isinstance(prefs, dict) and prefs.get(key) not in (None, ""):
            return prefs[key]
    return DEFAULT_PREFERENCES.get(key)


def pref_number(site, key: str) -> Any:
    """A numeric Preferences threshold for this crawl (int when it is a whole number), the default when the stored
    value is not a number or outside its allowed range. Cached per Site (filters read it for every row)."""
    from ..config import DEFAULT_PREFERENCES, PREFERENCE_RANGES
    cache = site.__dict__.setdefault("_pref_numbers", {}) if site is not None and hasattr(site, "__dict__") else None
    if cache is not None and key in cache:
        return cache[key]
    default = DEFAULT_PREFERENCES.get(key)
    value = pref(site, key) if site is not None else default
    try:
        n = float(value)
        lo, hi = PREFERENCE_RANGES.get(key, (float("-inf"), float("inf")))
        if n != n or n < lo or n > hi:
            raise ValueError
        out: Any = int(n) if n == int(n) else n
    except (TypeError, ValueError, OverflowError):
        out = default
    if cache is not None:
        cache[key] = out
    return out


def pref_text(site, key: str) -> str:
    """A Preferences threshold as a filter name shows it: "1,000", "2.5", "E"."""
    if key == "carbon_rating_flag":
        return str(pref(site, key) if site is not None else carbon_flag_default())
    n = pref_number(site, key)
    if isinstance(n, float):
        return f"{n:,.2f}".rstrip("0").rstrip(".")
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def carbon_flag_default() -> str:
    from ..config import DEFAULT_PREFERENCES
    return str(DEFAULT_PREFERENCES.get("carbon_rating_flag", "E"))


def threshold_filter(fid: str, key: str, name: str, test: Callable[[Any, Any], bool], description: str = "",
                     **kw: Any) -> Filter:
    """A Filter whose name and description carry a Preferences threshold ("{n}" in the texts): the registered
    filter shows today's default ("Over 60 Characters") and Filter.label / Filter.describe give this crawl's
    number ("Over 50 Characters"). The id never changes."""
    default = pref_text(None, key)
    return Filter(fid, name.format(n=default), test, description=description.format(n=default),
                  label=lambda site: name.format(n=pref_text(site, key)),
                  describe=(lambda site: description.format(n=pref_text(site, key))) if "{n}" in description
                  else None, **kw)


_DETAILS: Dict[str, Callable[[Any, str], Optional[dict]]] = {}
# Modules outside crawler/seo/tabs that register a details section when imported (loaded on first use)
DETAIL_PROVIDERS = ("crawler.seo.spelling", "crawler.seo.carbon", "crawler.seo.pdf_meta")
_details_loaded = False


def register_details(section: str, fn: Callable[[Any, str], Optional[dict]]) -> None:
    """Adds (or replaces) one per-URL detail section: fn(site, url) -> dict, {"not_checked": reason}, or None
    when the section does not apply to that URL."""
    _DETAILS[section] = fn


def detail_sections() -> List[str]:
    """Names of the registered detail sections."""
    _load_details()
    return list(_DETAILS)


def _load_details() -> None:
    global _details_loaded
    if _details_loaded:
        return
    _load_builtin()
    import importlib
    for name in DETAIL_PROVIDERS:
        try:
            importlib.import_module(name)
        except Exception:
            continue          # not built yet / optional dependency missing: its section is simply absent
    _details_loaded = True


def details(site, url: str, sections: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """{section: dict | {"not_checked": reason}} for one URL; sections whose provider returns None are left out.
    `sections` limits the result to these names."""
    _load_details()
    out: Dict[str, Any] = {}
    for section, fn in list(_DETAILS.items()):
        if sections is not None and section not in sections:
            continue
        try:
            value = fn(site, url)
        except Exception as e:
            value = {"not_checked": f"This section could not be built for this URL ({type(e).__name__})."}
        if value is not None:
            out[section] = value
    return out


# ---------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------
@dataclass
class TabResult:
    tab: Tab                                     # the tab as evaluated (per-crawl copy for dynamic tabs)
    rows: List[Any]                              # results under "All"
    filter_rows: Dict[str, List[Any]] = field(default_factory=dict)   # filter id -> results
    not_checked: Dict[str, str] = field(default_factory=dict)         # filter id ("all" = the tab) -> why it
                                                                      # did not run


@dataclass
class TabIndex:
    tabs: Dict[str, TabResult]
    issues: List[dict]                           # Issues overview rows (Screaming Frog "Issues" tab)
    checks_loaded: List[str] = field(default_factory=list)       # tab modules whose checks ran
    checks_failed: Dict[str, str] = field(default_factory=dict)  # tab module -> load error (checks MISSING)

    @property
    def checks_incomplete(self) -> bool:
        return bool(self.checks_failed)


def _rows_for(tab: Tab, site) -> List[Any]:
    return list(tab.rows(site)) if tab.rows else list(site.pages)


def filter_label(f: Filter, site) -> str:
    """The filter's name for this crawl: Filter.label(site) when it has one, else Filter.name."""
    if f.label is None or site is None:
        return f.name
    try:
        name = f.label(site)
    except Exception:
        return f.name
    return str(name) if name else f.name


def filter_description(f: Filter, site) -> str:
    """The filter's description for this crawl: Filter.describe(site) when it has one, else Filter.description."""
    if f.describe is None or site is None:
        return f.description
    try:
        text = f.describe(site)
    except Exception:
        return f.description
    return str(text) if text else f.description


def effective_tab(tab: Tab, site) -> Tab:
    """The tab with its per-crawl columns and filters (tab.dynamic) and per-crawl filter names and descriptions
    (Filter.label / Filter.describe: a threshold changed under Preferences), else the tab itself."""
    if site is None:
        return tab
    if tab.dynamic is not None:
        try:
            columns, filters = tab.dynamic(site)
            tab = dataclasses.replace(tab, columns=list(columns), filters=list(filters))
        except Exception:
            pass
    if any(f.label is not None or f.describe is not None for f in tab.filters):
        named = []
        for f in tab.filters:
            name, about = filter_label(f, site), filter_description(f, site)
            named.append(dataclasses.replace(f, name=name, description=about)
                         if (name, about) != (f.name, f.description) else f)
        if any(a is not b for a, b in zip(named, tab.filters)):
            tab = dataclasses.replace(tab, filters=named)
    return _v1_view(tab) if tab.dynamic is not None else tab


def filter_reason(tab: Tab, f: Filter, site, rows: Sequence[Any], cache: Optional[Dict[str, str]] = None) -> str:
    """Why this filter is "Not checked" for this crawl ("" when it ran): unmet requirements first, then the
    tab's own skip_reason."""
    why = unmet(tuple(tab.requires) + tuple(f.requires), site, rows, cache)
    if why:
        return why
    if tab.skip_reason is not None and site is not None:
        try:
            return tab.skip_reason(site, f.id) or ""
        except Exception:
            return ""
    return ""


# P17 "Ignore non-indexable URLs for on-page filters": the page tabs whose issue filters then count indexable pages
# only. Their built-in issue filters already do (only_indexable, as Screaming Frog counts them), so the option matters
# for issue filters added later without it; the Images tab lists image files, not pages, and is never narrowed.
ON_PAGE_TABS = ("page_titles", "meta_description", "meta_keywords", "h1", "h2", "content")
IGNORE_NON_INDEXABLE_NOTE = ("On-page issues (titles, descriptions, headings, content) count indexable pages only in "
                             "this crawl (Configuration > Spider: ignore non-indexable URLs).")


def run_all(site) -> TabIndex:
    out: Dict[str, TabResult] = {}
    issues: List[dict] = []
    for base_tab in tabs():
        try:
            rows = _rows_for(base_tab, site)
        except Exception:
            rows = []
        tab = effective_tab(base_tab, site)
        tr = TabResult(tab=tab, rows=rows)
        cache: Dict[str, str] = {}
        tab_reason = unmet(tuple(tab.requires), site, rows, cache)
        if tab_reason:
            tr.not_checked["all"] = tab_reason          # why the whole tab is empty / not checked
        for f in tab.filters:
            blocked = unmet(tuple(tab.requires) + tuple(f.requires), site, rows, cache)
            if blocked:
                tr.filter_rows[f.id] = []
                tr.not_checked[f.id] = blocked
                continue
            indexable_only = f.only_indexable or (f.issue_type and tab.id in ON_PAGE_TABS and
                                                  getattr(_cfg(site), "ignore_non_indexable_issues", False) is True)
            pool = [r for r in rows if is_indexable(site, r)] if indexable_only else rows
            hits = []
            for r in pool:
                try:
                    if f.test(r, site):
                        hits.append(r)
                except Exception:
                    continue
            tr.filter_rows[f.id] = hits
            if not hits and tab.skip_reason is not None:
                try:
                    why = tab.skip_reason(site, f.id) or ""
                except Exception:
                    why = ""
                if why:
                    tr.not_checked[f.id] = why
            if f.issue_type and hits:
                issues.append({
                    "tab": tab.id, "tab_name": tab.name, "filter": f.id,
                    "name": f"{tab.name}: {f.name}", "issue_type": f.issue_type, "priority": f.priority or "Low",
                    "urls": len(hits), "pct": round(len(hits) / max(1, _covered(rows, f.of_rows)) * 100, 2),
                    "description": f.description, "fix": f.fix, "short_fix": short_fix_of(f),
                })
        out[tab.id] = tr
    pr = {"High": 0, "Medium": 1, "Low": 2}
    ty = {"Issue": 0, "Warning": 1, "Opportunity": 2}
    issues.sort(key=lambda i: (pr.get(i["priority"], 3), ty.get(i["issue_type"], 3), -i["urls"]))
    return TabIndex(tabs=out, issues=issues, checks_loaded=loaded_modules(), checks_failed=load_failures())


def filter_base(tr: "TabResult", f: Filter) -> int:
    """The base of a filter's "% of total" in a tab result: the rows its check covers (Filter.of_rows, e.g. the
    internal URLs for Response Codes' Internal filters), else every row of the tab. Reports that print a
    filter's % use this so every surface shows the same number."""
    return _covered(tr.rows, f.of_rows)


def _covered(rows: List[Any], of_rows: Optional[Callable[[Any], bool]]) -> int:
    """How many rows a check covers (the base of its "% of total")."""
    if of_rows is None:
        return len(rows)
    n = 0
    for r in rows:
        try:
            n += 1 if of_rows(r) else 0
        except Exception:
            continue
    return n


def cell_value(col: Column, r, site):
    try:
        v = col.get(r, site)
    except Exception:
        v = ""
    return "" if v is None else v


def tab_summary(index: TabIndex) -> List[dict]:
    """For /api/crawl/tabs and the Overview tree: every tab with its filter counts. A filter that could not
    run carries "not_checked" (the reason; its count is 0 because nothing was checked, not because nothing
    was found)."""
    out = []
    for tab_id, tr in sorted(index.tabs.items(), key=lambda kv: kv[1].tab.order):
        tab = tr.tab
        if tab.hidden:
            continue
        nc = getattr(tr, "not_checked", None) or {}
        # a dynamic tab not evaluated for a crawl (the registered object itself) has only placeholder slots
        filters = [] if (tab.dynamic is not None and _TABS.get(tab.id) is tab
                         and not tab.static_filters) else tab.filters
        out.append({
            "id": tab.id, "name": tab.name, "total": len(tr.rows), "hidden": False,
            "description": tab.description, "requires": list(tab.requires),
            "columns": [{"id": c.id, "label": c.label, "kind": c.kind} for c in tab.columns],
            "not_checked": nc.get("all", ""),
            "filters": [{"id": "all", "name": tab.total_label, "count": len(tr.rows), "not_checked": nc.get("all", "")}] + [
                {"id": f.id, "name": f.name, "count": len(tr.filter_rows.get(f.id, [])),
                 "issue_type": f.issue_type, "priority": f.priority, "description": f.description, "fix": f.fix,
                 "short_fix": short_fix_of(f), "requires": list(f.requires), "not_checked": nc.get(f.id, ""),
                 "base": _covered(tr.rows, f.of_rows) if f.of_rows is not None else None, "group": f.group}
                for f in filters],
        })
    return out
