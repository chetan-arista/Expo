"""
JavaScript tab (Screaming Frog "JavaScript"): what changes when a page's JavaScript runs, for every
internal HTML page that loaded (2xx).

Most filters compare the raw HTML (PageData) with the page as Chrome rendered it (PageData["rendered"],
crawler/render_capture.py). They need JavaScript rendering (Configuration > Rendering: render every page in
Chrome); without it they are "Not checked", and when only some pages were rendered only those are judged.
Three filters work from the HTML alone:
  - Pages with Blocked Resources: the page loads a file robots.txt blocks (an image, script, stylesheet or
    frame found in the HTML that the crawl recorded as Blocked by robots.txt, or a request Chrome was refused
    while rendering).
  - Uses Old AJAX Crawling Scheme URLs: the URL carries _escaped_fragment_ or pages link to it with #!.
  - Uses Old AJAX Crawling Scheme Meta Fragment Tag: <meta name="fragment" content="!">.
Comparisons: texts are compared with whitespace collapsed; canonicals as normalised URLs; noindex / nofollow
from the robots meta tags only (an X-Robots-Tag header applies to both versions). "Contains JavaScript
Content" needs at least 20 more words and 10 % more after rendering, so a cookie banner alone does not count.
JavaScript errors / warnings are Chrome console messages of level error / warning, except failed downloads
(those are listed in the Rendered Page detail tab).
A page whose meta refresh or script sent Chrome to another document while it rendered is not compared: the
rendered title, links and words are the destination's ("Rendered" says "Redirected in Chrome"); the redirect
itself is listed under Response Codes.
"""
from typing import Any, List

from ...url_utils import normalize_url
from ..registry import Column, Filter, Tab, first, register, rendered, rendered_content, seo
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE, category_of,
                       memo)
from .page_titles import content_rows
from .url import _raw_hrefs

JS_CONTENT_MIN_WORDS = 20        # extra words after rendering before a page "contains JavaScript content"
JS_CONTENT_MIN_SHARE = 0.10      # ... and at least this share of the raw word count


def _text(v: Any) -> str:
    return " ".join(str(v or "").split())


def _norm_url(u: str) -> str:
    if not u:
        return ""
    try:
        return normalize_url(u) or u
    except Exception:
        return u


# ---------------------------------------------------------------------------------------------
# Raw vs rendered values
# ---------------------------------------------------------------------------------------------
def original(r, key: str) -> Any:
    """A raw-HTML value of the page. In a render-every-page crawl the page's own values come from the rendered
    HTML (JavaScript rendering mode) and the raw-HTML ones are kept in PageData["original_html"]."""
    page = seo(r)
    kept = page.get("original_html")
    return kept[key] if isinstance(kept, dict) and key in kept else page.get(key)


def raw_title(r) -> str:
    return _text(first(original(r, "titles") or []))


def js_title(rd: dict) -> str:
    return _text(rd.get("title") or first(rd.get("titles") or []))


def raw_description(r) -> str:
    return _text(first(original(r, "meta_descriptions") or []))


def js_description(rd: dict) -> str:
    return _text(rd.get("meta_description") or first(rd.get("meta_descriptions") or []))


def raw_h1(r) -> List[str]:
    return [_text(h) for h in original(r, "h1") or [] if _text(h)]


def js_h1(rd: dict) -> List[str]:
    return [_text(h) for h in rd.get("h1") or [] if _text(h)]


def raw_canonical(r) -> str:
    return _norm_url(first(original(r, "canonicals") or []))


def js_canonical(rd: dict) -> str:
    return _norm_url(rd.get("canonical") or first(rd.get("canonicals") or []))


def _says(values, word: str) -> bool:
    """A robots meta content list carries the directive (or "none", which means noindex + nofollow)."""
    tokens = {p.strip() for v in values or [] for p in str(v or "").lower().replace(";", ",").split(",")}
    return word in tokens or "none" in tokens


def word_counts(r):
    """(raw HTML words, rendered words or None; None too when the render ended on another page)."""
    rd = rendered_content(r)
    return int(original(r, "word_count") or 0), (int(rd.get("word_count") or 0) if rd else None)


def _console(r, level: str) -> List[dict]:
    rd = rendered(r)
    return [c for c in (rd or {}).get("console") or [] if isinstance(c, dict)
            and str(c.get("level") or "").upper() == level and str(c.get("source") or "") != "network"]


# ---------------------------------------------------------------------------------------------
# Blocked resources
# ---------------------------------------------------------------------------------------------
def _blocked_urls(site) -> set:
    """URLs the crawl recorded as Blocked by robots.txt (internal results and external links)."""
    def build():
        out = set()
        for r in site.by_url.values():
            if category_of(r) == "blocked":
                out.add(r.url)
        for e in site.externals or ():
            if category_of(e) == "blocked":
                out.add(getattr(e, "url", ""))
        out.discard("")
        return out
    return memo(site, "js_blocked_urls", build)


def blocked_resources(r, site) -> List[str]:
    """Files this page loads that robots.txt blocks (from the HTML and from Chrome's requests)."""
    blocked = _blocked_urls(site)
    out: List[str] = []
    for res in seo(r).get("resources") or ():
        u = res.get("url") if isinstance(res, dict) else None
        if u and u in blocked and u not in out:
            out.append(u)
    for u in seo(r).get("background_images") or ():
        if isinstance(u, str) and u in blocked and u not in out:
            out.append(u)
    rd = rendered(r)
    for res in (rd or {}).get("blocked_resources") or ():
        u = res.get("url") if isinstance(res, dict) else None
        if u and u not in out:
            out.append(u)
    return out


# ---------------------------------------------------------------------------------------------
# Tests (False when the page was not rendered: such a page is not judged)
# ---------------------------------------------------------------------------------------------
def _rendered_test(fn):
    def run(r, site):
        rd = rendered(r)
        return rd is not None and bool(fn(r, rd))
    return run


def _compared_test(fn):
    """A raw-vs-rendered comparison: only for pages Chrome stayed on (a page whose meta refresh or script sent
    the browser elsewhere is listed under Response Codes > Redirection instead, not compared with its target)."""
    def run(r, site):
        rd = rendered_content(r)
        return rd is not None and bool(fn(r, rd))
    return run


def _js_content(r, rd) -> bool:
    raw, js = word_counts(r)
    extra = (js or 0) - raw
    return extra >= JS_CONTENT_MIN_WORDS and extra >= JS_CONTENT_MIN_SHARE * max(raw, 1)


def _old_ajax_url(r, site) -> bool:
    if "_escaped_fragment_" in r.url:
        return True
    return any("#!" in raw for raw in _raw_hrefs(site, r.url))


FILTERS = [
    Filter("blocked_resources", "Pages with Blocked Resources", lambda r, site: bool(blocked_resources(r, site)),
           "Warning", "High",
           description="The page loads images, scripts, stylesheets or frames that robots.txt blocks. Search "
                       "engines render pages before judging them; without those files they may see a broken "
                       "layout or miss content.",
           fix="Allow the blocked files in robots.txt (see Blocked Resources for the addresses); files on "
               "other sites you cannot change are fine if the page works without them.",
           short_fix="Allow the page's CSS/JS/image files in robots.txt"),
    Filter("js_links", "Contains JavaScript Links", _compared_test(lambda r, rd: rd.get("js_links")),
           "Warning", "Low", requires=("rendering",),
           description="Some links on the page only exist after JavaScript runs (they are not in the HTML the "
                       "server sends). Search engines find them only if they render the page, which can be "
                       "late or not at all.",
           fix="Put important links (menus, pagination, related products) in the HTML as normal <a href> "
               "links; see JavaScript Links for the ones that are only added by scripts.",
           short_fix="Put important links in the HTML as <a href> links"),
    Filter("js_content", "Contains JavaScript Content", _compared_test(_js_content), "Warning", "Low",
           requires=("rendering",),
           description=f"The page shows at least {JS_CONTENT_MIN_WORDS} more words (and "
                       f"{int(JS_CONTENT_MIN_SHARE * 100)}% more) after JavaScript runs than the HTML the server "
                       "sends. Search engines that do not render, or render late, miss that text.",
           fix="Render the main content on the server (server-side rendering or pre-rendering) so it is in "
               "the HTML; compare HTML Word Count with Rendered HTML Word Count.",
           short_fix="Render main content on the server so it is in the HTML"),
    Filter("noindex_original_only", "Noindex Only in Original HTML",
           _compared_test(lambda r, rd: _says(original(r, "meta_robots"), "noindex")
                          and not _says(rd.get("meta_robots"), "noindex")),
           "Issue", "High", requires=("rendering",),
           description="The HTML from the server says noindex, but JavaScript removes it. Google may never run "
                       "the script: it sees the noindex first and drops the page.",
           fix="Remove noindex from the server HTML if the page should be indexed; never rely on JavaScript to "
               "remove it.",
           short_fix="Remove noindex from the server HTML"),
    Filter("nofollow_original_only", "Nofollow Only in Original HTML",
           _compared_test(lambda r, rd: _says(original(r, "meta_robots"), "nofollow")
                          and not _says(rd.get("meta_robots"), "nofollow")),
           "Issue", "High", requires=("rendering",),
           description="The HTML from the server says nofollow, but JavaScript removes it. Search engines may "
                       "act on the nofollow in the HTML and not follow the page's links.",
           fix="Remove nofollow from the server HTML if the links should be followed.",
           short_fix="Remove nofollow from the server HTML"),
    Filter("canonical_rendered_only", "Canonical Only in Rendered HTML",
           _compared_test(lambda r, rd: not raw_canonical(r) and js_canonical(rd)),
           "Warning", "Medium", requires=("rendering",),
           description="The canonical tag is only added by JavaScript. Google may ignore canonicals that are "
                       "not in the server HTML.",
           fix="Output the canonical tag in the HTML the server sends.",
           short_fix="Put the canonical tag in the server HTML"),
    Filter("canonical_mismatch", "Canonical Mismatch",
           _compared_test(lambda r, rd: raw_canonical(r) and js_canonical(rd)
                          and raw_canonical(r) != js_canonical(rd)),
           "Issue", "High", requires=("rendering",),
           description="The canonical in the server HTML and the canonical after JavaScript runs point to "
                       "different URLs. Search engines get two answers and may pick the wrong one.",
           fix="Make the HTML and the rendered page carry the same canonical (stop the script from changing "
               "it, or output the right one on the server).",
           short_fix="Make HTML and rendered canonical the same"),
    Filter("title_rendered_only", "Page Title Only in Rendered HTML",
           _compared_test(lambda r, rd: not raw_title(r) and js_title(rd)),
           "Warning", "Medium", requires=("rendering",),
           description="The page title is only set by JavaScript; the HTML from the server has none.",
           fix="Output the <title> in the server HTML.",
           short_fix="Put the <title> in the server HTML"),
    Filter("title_updated", "Page Title Updated by JavaScript",
           _compared_test(lambda r, rd: raw_title(r) and js_title(rd) and raw_title(r) != js_title(rd)),
           "Warning", "Low", requires=("rendering",),
           description="JavaScript changes the page title after the page loads. Search engines may show either "
                       "version.",
           fix="Output the final title in the server HTML so both versions agree.",
           short_fix="Output the final title in the server HTML"),
    Filter("description_rendered_only", "Meta Description Only in Rendered HTML",
           _compared_test(lambda r, rd: not raw_description(r) and js_description(rd)),
           "Warning", "Medium", requires=("rendering",),
           description="The meta description is only added by JavaScript; the HTML from the server has none.",
           fix="Output the meta description in the server HTML.",
           short_fix="Put the meta description in the server HTML"),
    Filter("description_updated", "Meta Description Updated by JavaScript",
           _compared_test(lambda r, rd: raw_description(r) and js_description(rd)
                          and raw_description(r) != js_description(rd)),
           "Warning", "Low", requires=("rendering",),
           description="JavaScript changes the meta description after the page loads. Search engines may use "
                       "either version.",
           fix="Output the final meta description in the server HTML.",
           short_fix="Output the final meta description in the HTML"),
    Filter("h1_rendered_only", "H1 Only in Rendered HTML",
           _compared_test(lambda r, rd: not raw_h1(r) and js_h1(rd)),
           "Warning", "Medium", requires=("rendering",),
           description="The page's H1 heading only appears after JavaScript runs.",
           fix="Output the H1 in the server HTML.",
           short_fix="Put the H1 in the server HTML"),
    Filter("h1_updated", "H1 Updated by JavaScript",
           _compared_test(lambda r, rd: raw_h1(r) and js_h1(rd) and raw_h1(r) != js_h1(rd)),
           "Warning", "Low", requires=("rendering",),
           description="JavaScript changes the page's H1 headings after the page loads.",
           fix="Output the final H1 in the server HTML so both versions agree.",
           short_fix="Output the final H1 in the server HTML"),
    Filter("old_ajax_urls", "Uses Old AJAX Crawling Scheme URLs", _old_ajax_url, "Warning", "Medium",
           description="The page is reached through #! (hashbang) links or an _escaped_fragment_ address, the "
                       "AJAX crawling scheme Google stopped supporting in 2015. Content behind #! may not be "
                       "indexed.",
           fix="Give each view a normal URL (History API paths) and render its content on the server or with "
               "JavaScript at that URL.",
           short_fix="Replace #! URLs with normal paths (History API)"),
    Filter("old_ajax_meta", "Uses Old AJAX Crawling Scheme Meta Fragment Tag",
           lambda r, site: bool(seo(r).get("meta_fragment")), "Warning", "Medium",
           requires=("pagedata:meta_fragment",),
           description="The page carries <meta name=\"fragment\" content=\"!\">, part of the AJAX crawling "
                       "scheme Google no longer supports.",
           fix="Remove the meta fragment tag and make sure the page's content is in its HTML or rendered at "
               "its own URL.",
           short_fix="Remove the meta fragment tag"),
    Filter("js_errors", "Pages with JavaScript Errors", lambda r, site: bool(_console(r, "SEVERE")),
           "Warning", "Medium", requires=("rendering",),
           description="Chrome's console showed errors while rendering the page (uncaught script errors, "
                       "console errors, blocked scripts). Broken scripts can hide content, links or tracking.",
           fix="Open the Chrome Console Log tab for the page and fix the errors, starting with your own "
               "scripts (third-party ones can be removed or updated).",
           short_fix="Fix the JavaScript errors shown in the console log"),
    Filter("js_warnings", "Pages with JavaScript Warnings", lambda r, site: bool(_console(r, "WARNING")),
           "Warning", "Low", requires=("rendering",),
           description="Chrome's console showed warnings while rendering the page (deprecated features, "
                       "console warnings).",
           fix="Review the warnings in the Chrome Console Log tab; fix the ones from your own code.",
           short_fix="Review the console warnings from your own code"),
    Filter("chrome_issues", "Pages with Chrome Issues",
           _rendered_test(lambda r, rd: rd.get("chrome_issues")), "Warning", "Low",
           requires=("rendering:chrome_issues_checked",),
           description="Chrome's DevTools reported issues for the page (for example cookies blocked by "
                       "SameSite rules, mixed content, blocked requests or deprecated features).",
           fix="Review the issues listed for the page and fix the ones that affect what visitors see.",
           short_fix="Fix the issues Chrome DevTools reports"),
]


def _rd_cell(fn, default=""):
    return lambda r, site: fn(r, rendered(r)) if rendered(r) is not None else default


def _cmp_cell(fn, default=""):
    """A rendered value compared with the HTML: blank when the render ended on another page."""
    return lambda r, site: fn(r, rendered_content(r)) if rendered_content(r) is not None else default


def _rendered_label(r, site) -> str:
    rd = rendered(r)
    if rd is None:
        return "No"
    return "Yes" if rendered_content(r) is not None else "Redirected in Chrome"


def _js_share(r, rd) -> Any:
    raw, js = word_counts(r)
    return round((js - raw) / js * 100, 1) if js else 0


COLUMNS = [
    ADDRESS, STATUS_CODE, STATUS,
    Column("html_word_count", "HTML Word Count", lambda r, site: word_counts(r)[0], "num"),
    Column("rendered_word_count", "Rendered HTML Word Count", _cmp_cell(lambda r, rd: word_counts(r)[1]), "num"),
    Column("word_count_change", "Word Count Change",
           _cmp_cell(lambda r, rd: word_counts(r)[1] - word_counts(r)[0]), "num"),
    Column("js_word_pct", "JS Word Count %", _cmp_cell(_js_share), "num"),
    Column("title_1", "Title 1", lambda r, site: raw_title(r)),
    Column("rendered_title_1", "Rendered Title 1", _cmp_cell(lambda r, rd: js_title(rd))),
    Column("h1_1", "H1-1", lambda r, site: first(raw_h1(r))),
    Column("rendered_h1_1", "Rendered H1-1", _cmp_cell(lambda r, rd: first(js_h1(rd)))),
    Column("canonical", "Canonical Link Element 1", lambda r, site: raw_canonical(r), "url"),
    Column("rendered_canonical", "Rendered Canonical Link Element 1", _cmp_cell(lambda r, rd: js_canonical(rd)), "url"),
    Column("js_links", "JavaScript Links", _cmp_cell(lambda r, rd: len(rd.get("js_links") or []), ""), "num"),
    Column("blocked_resources", "Blocked Resources", lambda r, site: blocked_resources(r, site), "list"),
    Column("js_errors", "JS Errors", _rd_cell(lambda r, rd: len(_console(r, "SEVERE"))), "num"),
    Column("js_warnings", "JS Warnings", _rd_cell(lambda r, rd: len(_console(r, "WARNING"))), "num"),
    Column("chrome_issues", "Chrome Issues", _rd_cell(lambda r, rd: len(rd.get("chrome_issues") or [])), "num"),
    Column("rendered", "Rendered", _rendered_label),
    INDEXABILITY, INDEXABILITY_STATUS,
]


def skip_reason(site, filter_id: str) -> str:
    """Why Pages with Blocked Resources is "Not checked" ("" when it ran)."""
    if filter_id != "blocked_resources":
        return ""
    cfg = getattr(site, "config", None)
    if cfg is not None and not getattr(cfg, "respect_robots", True):
        return "robots.txt was ignored in this crawl ('Respect robots.txt' was off), so nothing was blocked."
    if cfg is not None and not getattr(cfg, "crawl_resources", True) \
            and not any(rendered(p) is not None for p in site.pages):
        return ("Images, scripts and stylesheets were not requested (resource crawling off) and no page was "
                "rendered, so blocked files could not be seen.")
    return ""


register(Tab(id="javascript", name="JavaScript", order=165, columns=COLUMNS, filters=FILTERS, rows=content_rows,
             skip_reason=skip_reason,
             description="What changes when a page's JavaScript runs (rendered in Chrome) compared with its HTML."))
