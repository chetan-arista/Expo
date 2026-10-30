"""
Passed / Failed / Not checked for every SEO check (GET /api/crawl/checks).

check_statuses(index, crawler=None, site=None) -> [{"tab", "filter", "name", "status", "reason", "urls", ...}]

Every registered tab filter that is an Issue, Warning or Opportunity becomes one check:
  failed       the check ran and flagged URLs (urls = the filter's count, the same number as the Issues tab)
  passed       the check ran and flagged nothing
  not_checked  the data the check needs was not collected, so "0" would be a lie: external links not
               checked, resources (images/scripts) not crawled, no XML sitemap found, robots.txt ignored,
               a value the crawler does not read yet, nothing of that kind on the site (no hreflang, no
               AMP, no structured data: "not_applicable" is true), or a result the crawl cannot judge
               because it stopped early (orphan pages).
A filter whose requirements were not met (registry "requires": JavaScript rendering, a Crawl Analysis step,
a configured rule, a connected integration, data an older crawler did not read) is "not_checked" with the
reason the registry recorded (TabResult.not_checked); "not_connected": true marks the ones that wait for a
connector (Connections), so a surface can show "Not connected" instead of "Not checked".
Checks that need JavaScript rendering are judged only on
the pages that were rendered ("Only N of M pages were rendered" when some were not). Hidden tabs (AMP) are left
out. A few checks Screaming Frog has but this tool cannot run (Core Web Vitals without PageSpeed Insights, the
carbon rating) are listed too, always "not_checked", with "virtual": true, so the report is honest about what was
NOT looked at. Hreflang written in XML sitemaps is read since P8 (its virtual entry is gone); a page whose hreflang
alternates the crawl did not request is not counted as checked by the return-link checks (P8), and a sitemap
comparison made from part of the sitemaps carries a caveat (P9).
"passed" also needs at least one URL the check could actually judge: a header check needs URLs that
answered 2xx with headers, HSTS needs HTTPS URLs, on-page checks need HTML pages that loaded, image sizes
need a known size. "Checked N URLs" counts only those URLs; when there are none the check is
"not_checked" (the data was missing) or "not_applicable" (nothing of that kind, e.g. HSTS on an
http-only site). When no URL of the site answered at all, nothing is "passed".
Checks that measure or compare something a page may not have (PRESENCE: title / description / heading
lengths and duplicates, og:image / og:url / og:title, sitemap <lastmod> dates, the site's tracking tag,
mixed content on HTTPS pages, forms) judge only the rows that have it, and are "not_applicable" when none
does: "Duplicate og:title" on a site without og:title compared nothing, so it is not "passed".
"""
import inspect
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..seo import registry
from ..seo.postcrawl import SiteRow
from ..seo.registry import rendered, seo
from ..seo.tabs.images import image_size, occurrences_of, rendered_sizes
from ..seo.tabs.internal import category_of, content_kind, headers_of, is_external, is_html_doc
from ..seo.tabs.meta_description import descriptions_of
from ..seo.tabs.page_titles import h1_of, text_list, titles_of

try:
    from ..seo.tabs.robots_txt import SITE_FILTERS as ROBOTS_SITE_FILTERS
except Exception:     # older tab module
    ROBOTS_SITE_FILTERS = {"search_blocked", "ai_blocked", "training_opt_out", "content_signal", "ai_partial",
                           "no_sitemap_line", "robots_unreachable", "robots_html", "robots_too_big", "robots_missing",
                           "ai_disallowed_pages"}

STATUSES = ("passed", "failed", "not_checked")

EXTERNAL = {"external_no_response", "external_client_error", "external_server_error", "external_meta_refresh",
            "external_http_refresh"}
# Checks whose "absence of links" logic gives false alarms when the crawl did not reach every page
NEEDS_FULL_CRAWL = {("sitemaps", "orphan"), ("search_console", "orphan_urls"), ("analytics", "orphan_urls")}
CAVEAT_PARTIAL = {("canonicals", "unlinked"), ("pagination", "unlinked"), ("hreflang", "unlinked"),
                  ("links", "non_indexable_inlinks_only"), ("links", "nofollow_inlinks_only"),
                  ("hreflang", "missing_return_links")}
# Feature-specific checks: nothing to check when the site does not use the feature
FEATURE = {
    "hreflang": ("contains_hreflang", "no page uses hreflang (a site in one language does not need it)"),
    "pagination": ("contains_pagination", "no page uses rel=\"next\"/\"prev\" pagination links"),
}
CANONICAL_NEEDS = {"multiple", "multiple_conflicting", "non_indexable_canonical", "canonical_is_relative",
                   "unlinked", "invalid_attribute", "contains_fragment", "outside_head"}
SD_NEEDS = {"validation_errors", "parse_errors", "validation_warnings", "rich_result_errors", "rich_result_warnings"}
RICH_NEEDS = {"rich_result_errors", "rich_result_warnings"}
_RENDER_NOT_DONE = "{unknown} of {total} pages were not rendered in Chrome, so they were not checked."
DUPLICATE_FILTERS = {("page_titles", "duplicate"), ("meta_description", "duplicate"), ("h1", "duplicate"),
                     ("h2", "duplicate"), ("content", "exact_duplicates"), ("content", "near_duplicates")}
# PageData keys some checks read that the extractor may not fill (checked against extract.py's source)
EXTRACTOR_KEYS = {("validation", "head_not_first"): "head_not_first",
                  ("validation", "body_before_html"): "body_before_html"}


# ---------------------------------------------------------------------------------------------
# Which rows a check can judge (a row it cannot judge is never counted as "checked: none found")
# ---------------------------------------------------------------------------------------------
def _answered(r) -> bool:
    """The URL got an HTTP answer (a URL with no response has no headers or HTML to test)."""
    return getattr(r, "status_code", None) is not None


def _loaded_page(r) -> bool:
    """An internal HTML page that loaded (2xx): on-page checks can read it."""
    return is_html_doc(r) and category_of(r) == "2xx" and bool(seo(r).get("is_html"))


def _with_headers(r) -> bool:
    return category_of(r) == "2xx" and headers_of(r) is not None


def _https_with_headers(r) -> bool:
    return _with_headers(r) and str(getattr(r, "url", "") or "").lower().startswith("https://")


def _size_known(r) -> bool:
    v = seo(r).get("size_bytes")
    return isinstance(v, int) and v > 0


def _image_size_known(r) -> bool:
    return category_of(r) == "2xx" and image_size(r) is not None


def _resource_size_known(r) -> bool:
    return content_kind(r) in ("javascript", "css") and category_of(r) == "2xx" and _size_known(r)


def _not_site_row(r) -> bool:
    return not isinstance(r, SiteRow)


_HEADERS_MISSING = "No URL answered 2xx with its response headers recorded, so the headers could not be read."
_NO_PAGE = "No HTML page loaded (2xx), so there was no page to read."


def _is_resource(r) -> bool:
    return content_kind(r) in ("javascript", "css") and category_of(r) == "2xx"


@dataclass(frozen=True)
class Judge:
    """Which rows of a tab one check can judge."""
    test: Callable[[Any], bool]          # this row carries what the check needs
    reason: str                          # why no row could be judged ("not_checked")
    na: bool = False                     # True: the reason is "nothing of that kind" (not applicable)
    caveat: str = ""                     # when only some rows could be judged ({unknown} of {total})
    kind: Optional[Callable[[Any], bool]] = None   # rows of the kind the check is about: none -> not applicable
    kind_reason: str = ""


JUDGE: Dict[Tuple[str, str], Judge] = {
    ("security", "missing_hsts"): Judge(
        _https_with_headers, "Not applicable: no HTTPS URL answered, and browsers ignore HSTS sent over plain http "
                             "(see Security: HTTP URLs).", na=True),
    ("security", "bad_content_type"): Judge(
        lambda r: category_of(r) == "2xx", "No URL answered 2xx, so no Content-Type could be checked."),
    # Images whose size is unknown are never "none found": the Images tab says why (images.size_skip_reason) when no
    # size is known at all; otherwise the images that loaded without a size are the caveat (P11)
    ("images", "over_100_kb"): Judge(
        _image_size_known,
        "The server sent no size for any image (no Content-Length header), so image sizes could not be checked.",
        caveat="{unknown} of {total} images sent no size (no Content-Length header), so they were not checked.",
        kind=lambda r: category_of(r) == "2xx", kind_reason="Not applicable: no image loaded in this crawl."),
    ("validation", "html_over_2mb"): Judge(
        lambda r: is_html_doc(r) and _size_known(r), "No HTML page with a known file size was crawled."),
    ("validation", "resource_over_2mb"): Judge(
        _resource_size_known, "The size of the scripts and stylesheets is not known (no body or Content-Length).",
        kind=_is_resource, kind_reason="Not applicable: no script or stylesheet loaded in this crawl."),
    ("response_codes", "internal_meta_refresh"): Judge(_loaded_page, _NO_PAGE),
    ("response_codes", "internal_http_refresh"): Judge(lambda r: _answered(r) and headers_of(r) is not None,
                                                       _HEADERS_MISSING),
    # External refreshes (P5): only external pages whose answer was read count as checked
    ("response_codes", "external_meta_refresh"): Judge(
        lambda r: is_external(r) and getattr(r, "meta_refresh", None) is not None,
        "No external HTML page could be read for a meta refresh.",
        caveat="{unknown} of {total} external HTML pages could not be read, so they were not checked.",
        kind=lambda r: (is_external(r) and category_of(r) == "2xx"
                        and "html" in (getattr(r, "content_type", "") or "")),
        kind_reason="Not applicable: no external link is an HTML page that answered 2xx."),
    ("response_codes", "external_http_refresh"): Judge(
        lambda r: (is_external(r) and getattr(r, "refresh_checked", False)
                   and category_of(r) not in ("", "blocked", "error")),
        "No external link answered, so no Refresh header could be read.",
        kind=lambda r: is_external(r) and category_of(r) not in ("", "blocked", "error"),
        kind_reason="Not applicable: no external link answered."),
}
for _f in ("missing_csp", "missing_xcto", "missing_xfo", "missing_referrer_policy"):
    JUDGE[("security", _f)] = Judge(_with_headers, _HEADERS_MISSING)
for _f in ("mixed_content", "form_url_insecure", "form_on_http", "unsafe_cross_origin", "protocol_relative"):
    JUDGE[("security", _f)] = Judge(_loaded_page, _NO_PAGE)
# every other filter of these tabs (their rows are every internal URL)
TAB_JUDGE: Dict[str, Judge] = {
    "validation": Judge(_loaded_page, _NO_PAGE),
}
# robots.txt rules that cannot be read when the file did not answer
ROBOTS_RULES = {"search_blocked", "ai_blocked", "training_opt_out", "content_signal", "ai_partial", "no_sitemap_line",
                "robots_html", "robots_too_big", "ai_disallowed_pages"}
ROBOTS_PAGE_RULES = {"ai_disallowed_pages"}      # listed with the site filters, but tested on each page
ROBOTS_FILE = Judge(lambda r: isinstance(r, SiteRow), "robots.txt was not read in this crawl.")
ROBOTS_PAGES = Judge(_not_site_row, "Not applicable: no HTML page loaded.", na=True)


# AI Crawlers tab: the bot filters judge the sampled pages, the llms.txt filters the llms.txt files (P19)
def _llms_data(r) -> dict:
    return (r.data or {}) if isinstance(r, SiteRow) and r.kind in ("llms_txt", "llms_full_txt") else {}


def _llms_answered(r) -> bool:
    return _llms_data(r).get("state") in ("found", "html")


def _llms_found(r) -> bool:
    return _llms_data(r).get("state") == "found"


def _llms_links_judged(r) -> bool:
    d = _llms_data(r)
    return d.get("state") == "found" and int(d.get("links_ok") or 0) + int(d.get("links_broken_total") or 0) > 0


_NO_LLMS = "Not applicable: the site has no llms.txt."
AI_CRAWLER_JUDGE: Dict[str, Judge] = {
    "llms_txt_html": Judge(_llms_answered, _NO_LLMS, na=True, kind=_llms_answered, kind_reason=_NO_LLMS),
    "llms_txt_invalid": Judge(_llms_found, "llms.txt answers with a web page, so its format could not be checked.",
                              kind=_llms_answered, kind_reason=_NO_LLMS),
    "llms_txt_broken_links": Judge(
        _llms_links_judged, "No link in llms.txt points to a page this crawl requested (other sites or pages "
                            "outside the crawl), so the links could not be checked.",
        kind=lambda r: _llms_found(r) and int(_llms_data(r).get("links_total") or 0) > 0,
        kind_reason="Not applicable: there is no llms.txt with links to check."),
}
AI_CRAWLER_PAGES = Judge(_not_site_row, "No page was checked by the search / AI bot check.")


def _judge_for(tab_id: str, filter_id: str) -> Optional[Judge]:
    j = JUDGE.get((tab_id, filter_id)) or TAB_JUDGE.get(tab_id)
    if j is None and tab_id == "robots_txt":
        # the robots.txt row answers the file's rules; every other filter is tested on the pages
        j = ROBOTS_FILE if filter_id in ROBOTS_SITE_FILTERS and filter_id not in ROBOTS_PAGE_RULES else ROBOTS_PAGES
    if j is None and tab_id == "ai_crawlers":
        j = AI_CRAWLER_JUDGE.get(filter_id, AI_CRAWLER_PAGES)
    return j


# ---------------------------------------------------------------------------------------------
# Checks that measure or compare something a row may not have (a title's length, a sitemap <lastmod>, an
# og:image, the site's tracking tag): only the rows that have it are judged ("Checked N URLs" counts them), and
# when no row has it the check is "not applicable" instead of "passed" (nothing was measured or compared)
# ---------------------------------------------------------------------------------------------
_NO_TITLE = "Not applicable: none of the pages has a title (see Page Titles: Missing)."
_NO_DESCRIPTION = "Not applicable: none of the pages has a meta description (see Meta Description: Missing)."
_NO_H1 = "Not applicable: none of the pages has an h1 (see H1: Missing)."
_NO_H2 = "Not applicable: none of the pages has an h2 (see H2: Missing)."
_NO_LASTMOD = "Not applicable: no URL in the XML sitemaps has a <lastmod> date (see Sitemaps: Missing Lastmod)."


def _has_values(values_of: Callable[[Any], List[str]]) -> Callable[[Any, Any], bool]:
    """A loaded page with at least one non-empty value (title, description, heading)."""
    return lambda r, site: _loaded_page(r) and any(values_of(r))


def _has_tag(key: str) -> Callable[[Any, Any], bool]:
    """A loaded page with at least one such element, empty or not (what a "Multiple" check counts)."""
    return lambda r, site: _loaded_page(r) and len(text_list(r, key)) > 0


def _h2_values(r) -> List[str]:
    return text_list(r, "h2")


def _og_value(key: str) -> Callable[[Any, Any], bool]:
    def test(r, site) -> bool:
        og = seo(r).get("og")
        return _loaded_page(r) and isinstance(og, dict) and bool(" ".join(str(og.get(key) or "").split()))
    return test


def _image_alt(r, site) -> bool:
    return any(str(img.get("alt") or "").strip() for _page, img in occurrences_of(site, r))


def _has_form(r, site) -> bool:
    return _loaded_page(r) and bool(seo(r).get("forms"))


def _https_page(r, site) -> bool:
    return _loaded_page(r) and str(getattr(r, "url", "") or "").lower().startswith("https://")


def _lastmod_listed(r, site) -> bool:
    from ..seo.tabs.sitemaps import SitemapFile, in_sitemap, lastmod_of
    return not isinstance(r, SitemapFile) and in_sitemap(site, r.url) and bool(lastmod_of(site, r.url))


def _lastmod_valid(r, site) -> bool:
    from ..seo.tabs.sitemaps import lastmod_of, parse_lastmod
    return _lastmod_listed(r, site) and parse_lastmod(lastmod_of(site, r.url)) is not None


def _lastmod_comparable(r, site) -> bool:
    """A sitemap URL with a valid <lastmod> that loaded (2xx) and sent a usable Last-Modified header."""
    from ..seo.tabs.sitemaps import SitemapFile, SitemapURL, page_last_modified
    return (not isinstance(r, (SitemapURL, SitemapFile)) and category_of(r) == "2xx"
            and _lastmod_valid(r, site) and page_last_modified(r) is not None)


def _stale_reason(site, rows: List[Any]) -> str:
    if not any(_lastmod_valid(r, site) for r in rows):
        return "Not applicable: no URL in the XML sitemaps has a valid <lastmod> date to compare."
    return ("Not applicable: none of the pages in the XML sitemaps sent a Last-Modified header (or only the "
            "time of the request), so there is no date to compare their <lastmod> with.")


def _site_tag_known(r, site) -> bool:
    from ..seo.tabs.third_parties import site_tags
    return _loaded_page(r) and bool(site_tags(site))


def _tracking_reason(site, rows: List[Any]) -> str:
    from ..seo.tabs.third_parties import TAG_COVERAGE, TAG_MIN_PAGES, tags_on
    tagged = sum(1 for r in rows if _loaded_page(r) and tags_on(site, r))
    if not tagged:
        return ("Not applicable: no page loads a known tracking tag (Google Tag Manager, GA4, Meta Pixel, "
                "Clarity...), so there is no site-wide tag to look for. Tags added by scripts at runtime are only "
                "seen with JavaScript rendering.")
    return (f"Not applicable: no tracking tag is on at least {int(TAG_COVERAGE * 100)}% of the pages (and on "
            f"{TAG_MIN_PAGES} pages or more), so there is no site-wide tag to look for ({tagged:,} page"
            f"{'s' if tagged != 1 else ''} load one).")


# (tab, filter) -> (row test (r, site), "not applicable" reason or (site, rows) -> reason)
PRESENCE: Dict[Tuple[str, str], Tuple[Callable[[Any, Any], bool], Any]] = {
    ("page_titles", "same_as_h1"): (lambda r, site: _has_values(titles_of)(r, site) and any(h1_of(r)),
                                    "Not applicable: none of the pages has both a title and an h1."),
    ("page_titles", "multiple"): (_has_tag("titles"), _NO_TITLE),
    ("meta_description", "multiple"): (_has_tag("meta_descriptions"), _NO_DESCRIPTION),
    ("h1", "multiple"): (_has_tag("h1"), _NO_H1),
    ("h1", "alt_text_in_h1"): (_has_tag("h1"), _NO_H1),
    ("h1", "non_sequential"): (_has_tag("h1"), _NO_H1),
    ("h2", "multiple"): (_has_tag("h2"), _NO_H2),
    ("h2", "non_sequential"): (_has_tag("h2"), _NO_H2),
    ("images", "alt_text_over_100_characters"): (_image_alt, "Not applicable: no image is given an alt text."),
    ("social", "og_image_relative"): (_og_value("image"),
                                      "Not applicable: none of the pages has an og:image (see Social: Missing "
                                      "og:image)."),
    ("social", "og_url_differs"): (_og_value("url"), "Not applicable: none of the pages has an og:url."),
    ("social", "duplicate_og_title"): (_og_value("title"),
                                       "Not applicable: none of the pages has an og:title (see Social: Missing "
                                       "og:title)."),
    ("sitemaps", "lastmod_invalid"): (_lastmod_listed, _NO_LASTMOD),
    ("sitemaps", "lastmod_future"): (_lastmod_valid, _NO_LASTMOD),
    ("sitemaps", "lastmod_stale"): (_lastmod_comparable, _stale_reason),
    ("third_parties", "tracking_missing"): (_site_tag_known, _tracking_reason),
    ("security", "mixed_content"): (_https_page, "Not applicable: no HTTPS page loaded, and mixed content only "
                                                 "happens on HTTPS pages (see Security: HTTP URLs)."),
    ("security", "form_url_insecure"): (_has_form, "Not applicable: none of the pages has a form."),
    ("security", "form_on_http"): (_has_form, "Not applicable: none of the pages has a form."),
}
for _f in ("duplicate", "over_60_characters", "below_30_characters", "over_561_pixels", "below_200_pixels"):
    PRESENCE[("page_titles", _f)] = (_has_values(titles_of), _NO_TITLE)
for _f in ("duplicate", "over_155_characters", "below_70_characters", "over_985_pixels", "below_400_pixels"):
    PRESENCE[("meta_description", _f)] = (_has_values(descriptions_of), _NO_DESCRIPTION)
for _f in ("duplicate", "over_70_characters"):
    PRESENCE[("h1", _f)] = (_has_values(h1_of), _NO_H1)
    PRESENCE[("h2", _f)] = (_has_values(_h2_values), _NO_H2)


def _presence_judge(key: Tuple[str, str], site, rows: List[Any]) -> Optional[Judge]:
    """The PRESENCE judge of a check (its row test bound to this crawl), None when it has none."""
    entry = PRESENCE.get(key)
    if entry is None or site is None:
        return None
    test, why = entry
    if callable(why):
        try:
            why = why(site, rows)
        except Exception:
            why = "Not applicable: nothing of this kind was found to check."

    def judge_row(r) -> bool:
        return bool(test(r, site))
    return Judge(judge_row, why, na=True)


def _judged(rows: List[Any], test: Callable[[Any], bool]) -> List[Any]:
    out = []
    for r in rows:
        try:
            if test(r):
                out.append(r)
        except Exception:
            continue
    return out


def _site_silent(site) -> str:
    """Why nothing could be checked when no internal URL got an HTTP answer ("" when some did)."""
    rows = list((getattr(site, "by_url", None) or {}).values()) if site is not None else []
    if not rows or any(_answered(r) for r in rows):
        return ""
    first = rows[0]
    why = str(getattr(first, "root_cause", "") or getattr(first, "http_status", "") or "no response").strip()
    return (f"The site did not answer any request ({why[:80]}), so there was nothing to check. "
            "Check the address and that the server is up, then crawl again.")

# Checks this tool cannot run (listed so the report says so). An entry disappears once a registered tab
# provides it: the PageSpeed one when the PageSpeed Insights tab (crawler/seo/tabs/pagespeed.py) exists.
VIRTUAL: List[Tuple[str, str, str, str]] = [
    ("pagespeed", "core_web_vitals", "Core Web Vitals (LCP, INP, CLS)", "pagespeed"),
    ("validation", "high_carbon_rating", "High Carbon Rating", "carbon"),
]
VIRTUAL_TABS = {"pagespeed": "PageSpeed"}
VIRTUAL_REASON = {
    "pagespeed": "Needs Google PageSpeed Insights: connect it under Connections (a free Google API key).",
    "carbon": "Needs a CO2 model that this tool does not have.",
}


def _virtual_provided(tab_id: str, filter_id: str) -> bool:
    """A registered tab now runs this check itself (so the virtual entry is dropped)."""
    tab = registry.get_tab(tab_id)
    if tab is None:
        return False
    if tab_id == "pagespeed":
        return True
    return any(f.id == filter_id for f in tab.filters)


def _requires(tab, f) -> Tuple[str, ...]:
    return tuple(getattr(tab, "requires", ()) or ()) + tuple(getattr(f, "requires", ()) or ())


def _site_judge(tab, f, site) -> Optional[Judge]:
    """Judges for the Build 3 tabs whose rows a check can only partly judge (rendered pages, files whose headers
    were recorded, pages the newer extractor read). None = the static JUDGE tables decide."""
    key = (tab.id, f.id)
    reqs = _requires(tab, f)
    if tab.id in ("pagespeed", "search_console") and site is not None:
        # Only the pages PageSpeed tested / Google inspected count as checked
        try:
            from ..integrations import judge as integration_judge
            j = integration_judge(tab.id, f.id, site)
        except Exception:
            j = None
        if j:
            return Judge(j["test"], j["reason"], caveat=j.get("caveat", ""))
    if tab.id == "mobile" and "mobile_measure" in reqs:
        from ..seo.tabs.mobile import measured
        return Judge(lambda r: measured(r, site, f.id),
                     "No page was measured at phone size in Chrome or tested by PageSpeed Insights.",
                     caveat="{unknown} of {total} pages were not measured at phone size, so they were not checked.",
                     kind=_loaded_page)
    if any(r.startswith("rendering") for r in reqs):
        if key == ("images", "incorrectly_sized"):
            sizes = rendered_sizes(site) if site is not None else {}
            return Judge(lambda r: getattr(r, "url", "") in sizes,
                         "No image was measured on a rendered page.",
                         caveat="{unknown} of {total} images were not seen on a page rendered in Chrome, so they "
                                "were not checked.",
                         kind=lambda r: category_of(r) == "2xx",
                         kind_reason="Not applicable: no image loaded in this crawl.")
        if tab.id == "mobile":
            from ..seo.tabs.mobile import mobile_metrics
            return Judge(lambda r: bool(mobile_metrics(r)), "No page was measured at phone size in Chrome.",
                         caveat="{unknown} of {total} pages were not measured at phone size in Chrome, so they were "
                                "not checked.", kind=_loaded_page)
        if tab.id == "accessibility":
            from ..seo.tabs.accessibility import rendered_a11y
            return Judge(lambda r: rendered_a11y(r) is not None,
                         "No page's accessibility tree could be read in Chrome.",
                         caveat="{unknown} of {total} pages were not rendered in Chrome, so they were not checked.",
                         kind=_loaded_page)
        return Judge(lambda r: rendered(r) is not None and _loaded_page(r), registry.RENDER_REASON,
                     caveat=_RENDER_NOT_DONE, kind=_loaded_page)
    if tab.id == "accessibility":
        from ..seo.tabs.accessibility import evaluate
        return Judge(lambda r: evaluate(r) is not None, registry.OLD_BUILD_REASON,
                     caveat="{unknown} of {total} pages could not be read for these checks.", kind=_loaded_page)
    if tab.id == "social":
        return Judge(lambda r: _loaded_page(r) and seo(r).get("og") is not None, registry.OLD_BUILD_REASON)
    if tab.id == "performance":
        from ..seo.tabs.performance import is_file, response_ms
        if f.id in ("uncacheable", "short_cache"):
            return Judge(lambda r: is_file(r) and headers_of(r) is not None,
                         "No script, stylesheet, image or font answered with its response headers recorded.",
                         caveat="{unknown} of {total} files had no recorded response headers, so they were not "
                                "checked.",
                         kind=is_file, kind_reason="Not applicable: no script, stylesheet, image or font loaded "
                                                   "in this crawl.")
        if f.id in ("slow_1s", "slow_3s"):
            return Judge(lambda r: _loaded_page(r) and response_ms(r) is not None,
                         "No page had a measured response time.", kind=_loaded_page)
        if f.id == "heavy_pages":
            def sized(r) -> bool:
                p = seo(r)
                return _loaded_page(r) and (not int(p.get("resources_count") or 0)
                                            or int(p.get("resources_sized") or 0) > 0)
            return Judge(sized, "The sizes of the files the pages load were not recorded (resource crawling off, or "
                                "no file sent a size).",
                         caveat="{unknown} of {total} pages load files whose sizes were not recorded, so they were not "
                                "checked.", kind=_loaded_page)
        return Judge(_loaded_page, _NO_PAGE)
    if tab.id == "third_parties":
        return Judge(_loaded_page, _NO_PAGE)
    if key == ("canonicals", "non_indexable_canonical") and site is not None:
        # a canonical whose target was not requested (or is on another host while external checks were off) cannot
        # be judged: those pages are not counted as checked (P25)
        from ..seo.tabs.canonicals import canonical_target, is_canonicalised, target_unchecked
        return Judge(lambda r: not (is_canonicalised(r) and target_unchecked(site, canonical_target(r))),
                     "No canonical target could be checked.",
                     caveat="{unknown} of {total} pages point the canonical at a URL that was not checked, so they "
                            "were not judged.")
    if tab.id == "hreflang" and site is not None:
        # the other end of an annotation can only be judged when the crawl requested the alternate (P8)
        from ..seo.tabs import hreflang as hl
        if f.id in hl.STATUS_CHECKS or f.id in hl.PAGE_CHECKS:
            fid = f.id
            return Judge(lambda r: hl.fully_judged(site, r, fid),
                         hl.unjudged_reason(site, fid) or "No hreflang alternate was requested by the crawl.",
                         caveat="{unknown} of {total} pages with hreflang alternates have alternates the crawl did "
                                "not request, so they were not checked.",
                         kind=lambda r: hl.has_alternates(r, site),
                         kind_reason="Not applicable: no page lists another language version.")
    if key in (("structured_data", "schema_not_on_page"), ("structured_data", "schema_price_not_on_page")):
        return Judge(lambda r: int(seo(r).get("schema_values_checked") or 0) > 0,
                     "Not applicable: no page has JSON-LD names, codes, prices or contact details to compare with "
                     "the visible text.", na=True)
    if key in (("content", "semantically_similar"), ("content", "low_relevance")):
        from ..seo.analysis import SEMANTIC_MIN_TERMS
        skipped = int((getattr(site, "semantic_info", None) or {}).get("not_compared") or 0)
        return Judge(lambda r: _loaded_page(r) and len(seo(r).get("top_terms") or []) >= SEMANTIC_MIN_TERMS,
                     "No indexable page had enough text to compare.",
                     caveat="{unknown} of {total} pages had too little text to compare." +
                            (f" {skipped:,} pages beyond the first 20,000 were not compared." if skipped else ""),
                     kind=_loaded_page)
    return None


def _cfg(crawler, site):
    return getattr(crawler, "config", None) or getattr(site, "config", None)


def _stats(crawler) -> dict:
    try:
        return crawler.get_stats() if crawler is not None and hasattr(crawler, "get_stats") else {}
    except Exception:
        return {}


_SOURCE: Dict[str, str] = {}


def _extractor_sets(key: str) -> bool:
    """True when crawler/seo/extract.py fills this PageData key (otherwise it always stays at its default)."""
    if "src" not in _SOURCE:
        try:
            from ..seo import extract
            _SOURCE["src"] = inspect.getsource(extract)
        except Exception:
            _SOURCE["src"] = ""
    src = _SOURCE["src"]
    return not src or f'"{key}"' in src or f"'{key}'" in src


def _incomplete(crawler, stats: dict, cfg) -> str:
    """Why the crawl may not have reached every page ("" when it finished normally)."""
    status = (stats.get("status") or getattr(crawler, "status", "") or "").lower()
    if status in ("stopped",):
        return "the crawl was stopped before it finished"
    if status in ("running", "paused"):
        return "the crawl is still running"
    if status == "auth_failed":
        return "the login failed, so protected pages were not crawled"
    if getattr(crawler, "_memory_stop", False):
        return "the server's memory was nearly full, so new URLs were not queued"
    if stats.get("limit_reached"):
        return f"the crawl stopped at the Max URLs limit ({getattr(cfg, 'max_urls', 0):,})"
    if int(stats.get("not_crawled_max_depth") or 0) > 0:
        return f"pages deeper than the Max depth ({getattr(cfg, 'max_depth', 0)}) were not crawled"
    if cfg is not None and (getattr(cfg, "include_patterns", None) or getattr(cfg, "exclude_regex", None)):
        return "include/exclude rules left part of the site out of the crawl"
    return ""


def _tab_skip(tab, site, filter_id: str) -> str:
    """A tab's own "not checked" reason (tab.skip_reason(site, filter_id), set by tabs whose data comes from
    a post-crawl check), "" when it has none or the check ran."""
    fn = getattr(tab, "skip_reason", None)
    if fn is None or site is None:
        return ""
    try:
        return fn(site, filter_id) or ""
    except Exception:
        return ""


SITEMAP_FILE_CHECKS = {("sitemaps", "over_50mb"), ("sitemaps", "over_50k_urls")}   # judge sitemap files
SITEMAP_FILES_READ = 25          # sitemap files a crawl read at most before Max sitemap files existed (P9)


def _sitemap_files_limit(crawler, site) -> int:
    """The most sitemap files the crawl could read (crawler.sitemap_files_limit; older crawls: 25)."""
    for obj in (crawler, site):
        v = getattr(obj, "sitemap_files_limit", None) if obj is not None else None
        if isinstance(v, int) and v > 0:
            return v
    return SITEMAP_FILES_READ


def _sitemap_files(site) -> int:
    """How many sitemap files the crawl read (the Sitemaps tab's per-file stats)."""
    try:
        from ..seo.tabs.sitemaps import sitemap_stats
        return len(sitemap_stats(site))
    except Exception:
        return len(getattr(site, "sitemap_stats", None) or {})


def check_statuses(index, crawler=None, site=None) -> List[dict]:
    """One entry per issue-type filter (plus the virtual checks). Never raises; [] without an index."""
    if index is None:
        return []
    if site is None and crawler is not None and hasattr(crawler, "site_index"):
        try:
            site, _ = crawler.site_index()
        except Exception:
            site = None
    try:
        return _checks(index, crawler, site)
    except Exception:
        return []


def _checks(index, crawler, site) -> List[dict]:
    cfg = _cfg(crawler, site)
    stats = _stats(crawler)
    incomplete = _incomplete(crawler, stats, cfg)
    check_ext = bool(getattr(crawler, "check_external", False)) if crawler is not None else \
        bool(getattr(cfg, "check_external_links", False) or getattr(cfg, "validate_external_links", False))
    collect_ext = bool(getattr(cfg, "collect_external_links", True)) if cfg is not None else True
    resources = bool(getattr(cfg, "crawl_resources", True)) if cfg is not None else True
    use_sitemaps = bool(getattr(cfg, "use_sitemaps", True)) if cfg is not None else True
    robots = bool(getattr(cfg, "respect_robots", True)) if cfg is not None else True
    max_depth = int(getattr(cfg, "max_depth", 50) or 50) if cfg is not None else 50
    has_sitemap = bool(getattr(site, "sitemap_urls", None)) if site is not None else False
    externals = list(getattr(site, "externals", None) or []) if site is not None else []
    ext_checked = sum(1 for e in externals if getattr(e, "status_code", None) is not None
                      or (getattr(e, "http_status", "") or "").strip())
    loaded = [p for p in (getattr(site, "pages", None) or [])] if site is not None else []
    silent = _site_silent(site)

    def count(tab: str, flt: str) -> int:
        tr = index.tabs.get(tab)
        return len(tr.filter_rows.get(flt, [])) if tr is not None else 0

    def rows(tab: str) -> int:
        tr = index.tabs.get(tab)
        return len(tr.rows) if tr is not None else 0

    out: List[dict] = []
    for base_tab in registry.tabs():
        tr = index.tabs.get(base_tab.id)
        tab = tr.tab if tr is not None else base_tab          # per-crawl filters of dynamic tabs
        not_run = getattr(tr, "not_checked", None) or {}
        for f in tab.filters:
            if not f.issue_type:
                continue
            n = len(tr.filter_rows.get(f.id, [])) if tr is not None else 0
            entry = {"tab": tab.id, "filter": f.id, "name": f"{tab.name}: {f.name}", "status": "failed" if n else "passed",
                     "reason": "", "urls": n, "issue_type": f.issue_type, "priority": f.priority or "Low",
                     "not_applicable": False, "virtual": False, "not_connected": False}
            skip = ""          # reason the check did not run
            na = ""            # reason there was nothing to check
            caveat = ""
            key = (tab.id, f.id)

            # Tabs fed by post-crawl checks (crawler/seo/postcrawl.py) say themselves when a filter did not run
            tab_skip = _tab_skip(tab, site, f.id) if tr is not None else ""
            if tr is None:
                skip = "This tab did not run for this crawl."
            elif not_run.get(f.id) and not n:
                skip = not_run[f.id]
            elif tab_skip:
                skip = tab_skip
            elif tab.id == "response_codes" and f.id in EXTERNAL:
                if not collect_ext:
                    skip = "External links were not collected in this crawl."
                elif not check_ext:
                    skip = "External links were not checked (turn on 'Check external links' in the crawl settings)."
                elif externals and not ext_checked:
                    skip = "The external link check did not run (the crawl ended before it started)."
                elif not externals:
                    na = "The site links to no external URLs."
                elif ext_checked < len(externals):
                    caveat = f"{len(externals) - ext_checked:,} of {len(externals):,} external links were not checked."
            elif key == ("response_codes", "internal_blocked_robots") and not robots:
                skip = "robots.txt was ignored in this crawl ('Respect robots.txt' was off)."
            elif key == ("images", "over_100_kb"):
                if not resources and not check_ext:
                    skip = "Image sizes need the images to be requested: resource crawling and external checks were off."
                elif not resources:
                    caveat = "Internal images were not requested (resource crawling off): only external image sizes are known."
                elif not check_ext:
                    caveat = "External images were not requested: only internal image sizes are known."
            elif key == ("validation", "resource_over_2mb") and not resources:
                skip = "Scripts, stylesheets and images were not crawled (resource crawling off)."
            elif key == ("security", "bad_content_type") and not resources:
                caveat = "Only pages were checked: resources (images, scripts, CSS) were not crawled."
            elif tab.id == "sitemaps":
                if not use_sitemaps:
                    skip = "Sitemap discovery was turned off for this crawl."
                elif not has_sitemap:
                    skip = "No XML sitemap was found for this site, so it could not be compared with the crawl."
                elif f.id == "over_50mb" and not getattr(site, "sitemap_stats", None):
                    skip = "The size of each sitemap file is not recorded by the crawler."
                elif key in NEEDS_FULL_CRAWL and incomplete and not n:
                    # nothing found on a partial crawl is not a pass; what was found is listed with a caveat
                    skip = (f"Cannot be judged: {incomplete}, so a page may look unlinked only because the page "
                            "linking to it was not crawled.")
            elif key == ("links", "high_crawl_depth") and max_depth <= registry.pref_number(site, "high_crawl_depth"):
                deep = registry.pref_text(site, "high_crawl_depth")
                skip = f"The crawl's Max depth is {max_depth}, so pages deeper than {deep} clicks were never reached."
            elif key in EXTRACTOR_KEYS and not _extractor_sets(EXTRACTOR_KEYS[key]):
                skip = "The crawler does not read this part of the HTML yet."
            elif tab.id in FEATURE:
                plain, why = FEATURE[tab.id]
                if count(tab.id, plain) == 0:
                    na = f"Not applicable: {why}."
            elif tab.id == "canonicals" and f.id in CANONICAL_NEEDS and count("canonicals", "contains_canonical") == 0:
                na = "Not applicable: no page declares a canonical URL."
            elif tab.id == "structured_data" and f.id in SD_NEEDS and count("structured_data", "contains") == 0:
                na = "Not applicable: no page has structured data."
            elif tab.id == "structured_data" and f.id in RICH_NEEDS and count("structured_data", "rich_result_detected") == 0:
                na = "Not applicable: no page has markup for a feature Google shows as a rich result."
            elif key == ("content", "low_relevance") and site is not None and not n \
                    and (getattr(site, "semantic_info", None) or {}).get("low_relevance_skipped"):
                na = "Not applicable: " + site.semantic_info["low_relevance_skipped"][0].lower() + \
                     site.semantic_info["low_relevance_skipped"][1:]
            elif tab.id == "amp" and rows("amp") == 0:
                na = "Not applicable: the site has no AMP pages."
            elif key in DUPLICATE_FILTERS and site is not None and \
                    sum(1 for p in loaded if site.indexable.get(p.url, False)) < 2:
                na = "Not applicable: fewer than two indexable pages to compare."
            if tab.id == "robots_txt" and f.id in ROBOTS_RULES and not skip and not n \
                    and count("robots_txt", "robots_unreachable"):
                skip = "robots.txt could not be read (server error or no answer), so its rules could not be checked."
            if silent and not n:
                skip, na = silent, ""

            pool_n = 0
            if not skip and not na and tr is not None:
                base = tr.rows
                if f.only_indexable and site is not None:
                    base = [r for r in base if site.indexable.get(getattr(r, "url", ""), False)]
                judge = (_presence_judge(key, site, base) or _site_judge(tab, f, site)
                         or _judge_for(tab.id, f.id))
                pool = _judged(base, judge.test) if judge else base
                kind = _judged(base, judge.kind) if judge and judge.kind else base
                pool_n = len(pool)
                if not pool and not n:
                    if judge and not kind and judge.kind_reason:
                        na = judge.kind_reason
                    elif base and judge:
                        # rows exist, but none carries what this check needs
                        if judge.na:
                            na = judge.reason
                        else:
                            skip = judge.reason
                    else:
                        noun = "indexable pages" if f.only_indexable else "URLs of this kind"
                        na = f"Not applicable: no {noun} in this crawl."
                elif judge and judge.caveat and len(kind) > len(pool):
                    caveat = (caveat + " " if caveat else "") + judge.caveat.format(
                        unknown=f"{len(kind) - len(pool):,}", total=f"{len(kind):,}")
            if key == ("content", "near_duplicates") and not skip and not na \
                    and getattr(site, "near_duplicate_method", "") == "simhash":
                caveat = (caveat + " " if caveat else "") + (
                    "Compared with the older simhash fingerprint (the pages carry no minhash signature), which is "
                    "less precise for pages close to 90% alike.")
            if (key in CAVEAT_PARTIAL or key in NEEDS_FULL_CRAWL) and incomplete and n:
                caveat = (caveat + " " if caveat else "") + f"May include false alarms: {incomplete}."
            unit = ("URL", "URLs")
            if key in SITEMAP_FILE_CHECKS and not skip and not na:
                # these look at the sitemap files themselves, not at the crawled URLs
                unit = ("sitemap file", "sitemap files")
                pool_n = _sitemap_files(site)
                unread = len(getattr(crawler, "sitemaps_not_read", None) or []) if crawler is not None else 0
                if unread:
                    caveat = (caveat + " " if caveat else "") + (
                        f"{unread:,} more sitemap file{'s were' if unread != 1 else ' was'} listed but not read "
                        f"(at most {_sitemap_files_limit(crawler, site):,} are read per crawl).")
            elif tab.id == "sitemaps" and n and not skip and not na and site is not None:
                # URL checks found something in the sitemaps that were read; more may be in what was not read (P9)
                from ..seo.tabs.sitemaps import sitemap_gap
                gap = sitemap_gap(site)
                if gap:
                    caveat = (caveat + " " if caveat else "") + f"Only part of the sitemaps was read: {gap}."

            if skip:
                entry.update(status="not_checked", reason=skip,
                             not_connected=skip.lower().startswith("not connected"))
            elif na:
                entry.update(status="not_checked", reason=na, not_applicable=True)
            elif n:
                entry["reason"] = f"Flagged on {n:,} {unit[n != 1]}." + (f" {caveat}" if caveat else "")
            else:
                entry["reason"] = (f"Checked {pool_n:,} {unit[pool_n != 1]}: none found."
                                   + (f" {caveat}" if caveat else "")
                                   + (f" Partial crawl: {incomplete}." if incomplete else ""))
            out.append(entry)

    for tab_id, fid, name, why in VIRTUAL:
        if _virtual_provided(tab_id, fid):
            continue
        tab = registry.get_tab(tab_id)
        label = tab.name if tab is not None else VIRTUAL_TABS.get(tab_id, tab_id.replace("_", " ").title())
        out.append({"tab": tab_id, "filter": fid, "name": f"{label}: {name}", "status": "not_checked",
                    "reason": VIRTUAL_REASON[why], "urls": 0,
                    "issue_type": None, "priority": None, "not_applicable": False, "virtual": True,
                    "not_connected": False})
    return out


def summary(checks: List[dict]) -> Dict[str, int]:
    """{"passed", "failed", "not_checked", "not_applicable", "dismissed"} counts that add up to every check, as the
    dashboard's chips count them (virtual checks included in not_checked). A check about something the site does
    not use is "not_applicable", not "not_checked"; a failed check the team dismissed completely is "dismissed",
    not failed."""
    out = {"passed": 0, "failed": 0, "not_checked": 0, "not_applicable": 0, "dismissed": 0}
    for c in checks:
        if c.get("not_applicable"):
            out["not_applicable"] += 1
        elif c.get("dismissed") and c.get("status") == "failed":
            out["dismissed"] += 1
        else:
            out[c["status"]] = out.get(c["status"], 0) + 1
    return out
