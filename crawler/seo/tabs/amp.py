"""
AMP tab (Screaming Frog "AMP"): AMP versions of internal pages, found through
<link rel="amphtml" href="..."> on the regular (non-AMP) page.

How the counts work:
  - "All" lists the AMP URLs that some crawled page announces with rel="amphtml" AND that the crawl
    itself requested (for example because they are also linked). The crawler does not fetch amphtml
    URLs on its own, so an AMP URL that was never requested is not listed or guessed at.
  - Indexable + Non-Indexable = All.
  - The page checks (canonical, head, body, viewport, AMP script) run on AMP URLs that loaded as HTML
    with a 200; an AMP URL that fails is reported once, under Non-200 Response.

Not checked (we do not collect the data): the <html amp> attribute, the doctype, the meta charset,
the AMP boilerplate CSS, disallowed HTML and full AMP validation. Use the official AMP validator for
those.
"""
import re
from typing import Any, Dict, List

from ..registry import Column, Filter, Tab, first, register, seo
from .canonicals import (CANONICAL_1, canonical_target, find_result, is_self_referencing, norm, same_url,
                         target_non_indexable)
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, STATUS_CODE, first_status_code,
                       is_external, memo, response_time_s, size_bytes)

_AMP_RUNTIME = re.compile(r"//cdn\.ampproject\.org/(?:lts/)?v0\.m?js(?:$|[?#])", re.I)


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def _amp_sources(site) -> Dict[str, List[str]]:
    """normalized AMP URL -> crawled pages that announce it with rel="amphtml"."""
    out: Dict[str, List[str]] = {}
    for r in site.pages:
        u = seo(r).get("amphtml") or ""
        if u:
            out.setdefault(norm(u), []).append(r.url)
    return out


def amp_sources(site) -> Dict[str, List[str]]:
    return memo(site, "amp_sources", lambda: _amp_sources(site))


def is_amp_url(site, url: str) -> bool:
    return bool(url) and norm(url) in amp_sources(site)


def amp_rows(site) -> List[Any]:
    out, seen = [], set()
    for key in amp_sources(site):
        t = find_result(site, key)
        if t is not None and not is_external(t) and t.url not in seen:
            seen.add(t.url)
            out.append(t)
    return out


def _loaded(r) -> bool:
    """The AMP URL answered 200 with an HTML document."""
    return first_status_code(r) == 200 and bool(seo(r).get("is_html"))


def _has_amp_runtime(r) -> bool:
    return any(_AMP_RUNTIME.search(x.get("url") or "") for x in seo(r).get("resources") or ()
               if x.get("type") in ("script", "other"))


def _viewport_ok(r) -> bool:
    v = re.sub(r"\s+", "", (seo(r).get("viewport") or "").lower())
    return "width=device-width" in v


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _missing_return_link(r, site) -> bool:
    """The non-AMP page this AMP URL names as canonical does not announce it back with rel="amphtml"."""
    if not _loaded(r):
        return False
    c = canonical_target(r)
    if not c or is_self_referencing(r) or is_amp_url(site, c):
        return False
    t = find_result(site, c)
    if t is None or is_external(t) or not seo(t).get("is_html") or first_status_code(t) != 200:
        return False
    return not same_url(seo(t).get("amphtml") or "", r.url)


def _canonical_to_amp(r, site) -> bool:
    c = canonical_target(r)
    return _loaded(r) and bool(c) and is_amp_url(site, c)


def _non_indexable_canonical(r, site) -> bool:
    c = canonical_target(r)
    return _loaded(r) and bool(c) and not is_self_referencing(r) and target_non_indexable(site, c)


FILTERS = [
    Filter("non_200", "Non-200 Response", lambda r, site: first_status_code(r) != 200,
           issue_type="Issue", priority="High",
           description="The AMP URL does not answer 200: it redirects, returns an error, or gave no "
                       "response. The regular page announces an AMP version that does not work.",
           fix="Fix the AMP page, or update or remove the rel=\"amphtml\" link on the regular page."),
    Filter("missing_non_amp_return", "Missing Non-AMP Return Link", _missing_return_link,
           issue_type="Issue", priority="High",
           description="The regular page that this AMP URL names as its canonical does not point back to "
                       "it with rel=\"amphtml\". The pairing has to be declared on both pages.",
           fix="Add <link rel=\"amphtml\" href=\"(AMP URL)\"> to the regular page, or correct the AMP "
               "page's canonical."),
    Filter("missing_canonical_to_non_amp", "Missing Canonical to Non-AMP", _canonical_to_amp,
           issue_type="Issue", priority="High",
           description="The AMP URL's canonical points to an AMP URL (itself or another one) instead of the "
                       "regular page that announces it.",
           fix="Set the AMP page's canonical to the regular (non-AMP) version of the page."),
    Filter("non_indexable_canonical", "Non-Indexable Canonical", _non_indexable_canonical,
           issue_type="Issue", priority="High",
           description="The AMP URL's canonical points to a page that cannot be indexed (blocked, redirect, "
                       "error or noindex). Only canonical targets the crawl requested are checked.",
           fix="Point the canonical at the final, working, indexable regular page."),
    Filter("missing_canonical", "Missing Canonical", lambda r, site: _loaded(r) and not canonical_target(r),
           issue_type="Issue", priority="High",
           description="The AMP page has no canonical. AMP requires one, pointing to the regular page (or "
                       "to itself for an AMP-only page).",
           fix="Add <link rel=\"canonical\"> to the AMP page's <head>."),
    Filter("missing_head", "Missing Head Tag", lambda r, site: _loaded(r) and not seo(r).get("head_count"),
           issue_type="Issue", priority="High",
           description="The AMP page has no <head> tag, which AMP requires.",
           fix="Add an explicit <head> section to the AMP template."),
    Filter("missing_body", "Missing Body Tag", lambda r, site: _loaded(r) and not seo(r).get("body_count"),
           issue_type="Issue", priority="High",
           description="The AMP page has no <body> tag, which AMP requires.",
           fix="Add an explicit <body> section to the AMP template."),
    Filter("invalid_viewport", "Missing/Invalid Meta Viewport Tag", lambda r, site: _loaded(r) and not _viewport_ok(r),
           issue_type="Issue", priority="High",
           description="The AMP page has no <meta name=\"viewport\">, or its content lacks "
                       "width=device-width, which AMP requires.",
           fix="Add <meta name=\"viewport\" content=\"width=device-width\"> to the <head>."),
    Filter("missing_amp_script", "Missing AMP Script", lambda r, site: _loaded(r) and not _has_amp_runtime(r),
           issue_type="Issue", priority="High",
           description="The AMP page does not load the AMP runtime (cdn.ampproject.org/v0.js). Only its "
                       "presence is checked, not its attributes.",
           fix="Add <script async src=\"https://cdn.ampproject.org/v0.js\"></script> to the <head>."),
    Filter("indexable", "Indexable", lambda r, site: bool(site.indexable.get(r.url)),
           issue_type="Warning", priority="High",
           description="The AMP URL can be indexed on its own. Usually an AMP page should be canonicalised "
                       "to its regular page, unless it is an AMP-only page.",
           fix="Check the AMP page's canonical points to the regular version of the page."),
    Filter("non_indexable", "Non-Indexable", lambda r, site: not site.indexable.get(r.url),
           description="The AMP URL cannot be indexed on its own (normally because it is canonicalised to "
                       "its regular page, which is the expected setup)."),
]


COLUMNS = [
    ADDRESS,
    Column("amp_link_from", "Announced By", lambda r, site: len(amp_sources(site).get(norm(r.url), [])), "num"),
    STATUS_CODE,
    INDEXABILITY, INDEXABILITY_STATUS,
    Column("title_1", "Title 1", lambda r, site: first(seo(r).get("titles") or [])),
    CANONICAL_1,
    Column("h1_1", "H1-1", lambda r, site: first(seo(r).get("h1") or [])),
    Column("word_count", "Word Count", lambda r, site: seo(r).get("word_count") or 0, "num"),
    Column("size", "Size (bytes)", lambda r, site: size_bytes(r), "num"),
    Column("crawl_depth", "Crawl Depth", lambda r, site: site.crawl_depth.get(r.url, ""), "num"),
    Column("response_time", "Response Time (s)", lambda r, site: response_time_s(r), "num"),
]

register(Tab(id="amp", name="AMP", order=175, columns=COLUMNS, filters=FILTERS, rows=amp_rows,
             hidden=True))   # AMP is deprecated (user decision): kept, but hidden everywhere
