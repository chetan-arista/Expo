"""
Hreflang tab (Screaming Frog "Hreflang"): the language / region alternates each internal page
declares, from <link rel="alternate" hreflang="..."> in the HTML, from the Link response header and from
<xhtml:link rel="alternate" hreflang="..."> in the XML sitemaps (Google's three methods; site.sitemap_hreflang,
P8). Every check reads the three together; the "Annotation Sources" column says where a page's annotations come
from.

How the counts work:
  - "All" lists internal HTML pages that answered 2xx, plus any other 2xx internal URL (a PDF, say)
    that sent hreflang in its Link header or has alternates in a sitemap.
  - Contains Hreflang + Missing = All whenever any page of the crawl uses hreflang. "Missing" stays
    empty on sites that do not use hreflang at all: a site in one language does not need it.
  - Checks that look at the other end of an annotation (Non-200, Missing Return Links, Inconsistent,
    Non-Canonical, Noindex) only judge URLs the crawl requested. The crawler follows hreflang link elements and
    Link-header hreflang URLs within its scope; an alternate it never requested (outside the crawl scope or limits,
    on another website, or listed only in a sitemap) is never guessed at: a page with such an alternate is "Not
    checked" for those checks (unjudged_alternates), never passed, and a check that could judge no page at all says
    "Not checked: N alternates were not requested" (skip_reason).

Language / region codes: when the "pycountry" package is installed, the language part is checked
against ISO 639 and the region part against ISO 3166-1. Without it, the check falls back to the
code's shape plus a short list of common real-world mistakes (en-uk, jp, cn, ...).
"""
import dataclasses
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List, Optional

from ..registry import Column, Filter, Tab, first, register, seo
from .canonicals import (CANONICAL_1, canonical_target, find_result, is_canonicalised, is_linked,
                         is_start_url, norm, same_url, target_non_200, has_noindex)
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, first_status_code,
                       internal_rows, is_external, memo)
from .page_titles import content_rows

try:                                       # optional: full ISO code lists
    import pycountry                       # type: ignore
except Exception:                          # pragma: no cover - depends on the environment
    pycountry = None

X_DEFAULT = "x-default"

# language(2-3 letters) [-script(4 letters)] [-region(2 letters or 3 digits)]
_CODE_RE = re.compile(r"^([a-z]{2,3})(?:-([a-z]{4}))?(?:-([a-z]{2}|\d{3}))?$")

# Common mistakes seen on real sites: wrong code -> what was probably meant
BAD_LANGUAGE = {"jp": "ja", "cn": "zh", "gb": "en", "us": "en", "dk": "da", "gr": "el",
                "kr": "ko", "cz": "cs", "vn": "vi"}
BAD_REGION = {"uk": "gb", "en": "a country code such as gb or us"}


# ---------------------------------------------------------------------------------------------
# Code validation
# ---------------------------------------------------------------------------------------------
def _iso_lookup(kind: str, **kw) -> bool:
    try:
        return getattr(pycountry, kind).get(**kw) is not None
    except (KeyError, LookupError, AttributeError):
        return False


@lru_cache(maxsize=4096)
def code_problem(code: str) -> str:
    """'' when the hreflang value is usable, else a short explanation of what is wrong."""
    c = (code or "").strip().lower()
    if not c:
        return "empty hreflang value"
    if c == X_DEFAULT:
        return ""
    m = _CODE_RE.match(c)
    if not m:
        if "_" in c:
            return f"{code}: use a hyphen, not an underscore"
        return f"{code}: not in language-region form (like en or en-gb)"
    lang, script, region = m.groups()
    if lang in BAD_LANGUAGE:
        return f"{code}: '{lang}' is not a language code (use '{BAD_LANGUAGE[lang]}')"
    if region in BAD_REGION:
        return f"{code}: '{region}' is not a region code (use {BAD_REGION[region]!s})"
    if pycountry is not None:
        found = _iso_lookup("languages", alpha_2=lang) if len(lang) == 2 else _iso_lookup("languages", alpha_3=lang)
        if not found:
            return f"{code}: '{lang}' is not an ISO 639 language code"
        if script and not _iso_lookup("scripts", alpha_4=script.title()):
            return f"{code}: '{script}' is not an ISO 15924 script code"
        if region and region.isalpha() and not _iso_lookup("countries", alpha_2=region.upper()):
            return f"{code}: '{region}' is not an ISO 3166-1 country code"
    return ""


# ---------------------------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------------------------
def html_entries(r) -> List[dict]:
    return [e for e in (seo(r).get("hreflang") or []) if e.get("lang") or e.get("url")]


def http_entries(r) -> List[dict]:
    return [e for e in (seo(r).get("http_hreflang") or []) if e.get("lang") or e.get("url")]


def sitemap_entries(r, site) -> List[dict]:
    """The page's alternates written in the XML sitemaps (site.sitemap_hreflang; [] without a site)."""
    m = getattr(site, "sitemap_hreflang", None) if site is not None else None
    if not m:
        return []
    url = getattr(r, "url", "") or ""
    found = m.get(url)
    if found is None:
        found = m.get(norm(url))
    return [e for e in found or [] if e.get("lang") or e.get("url")]


def entries(r, site=None) -> List[dict]:
    """Every hreflang annotation of the URL: HTML link elements first, then the HTTP header, then the XML sitemaps
    (only when the site is given)."""
    return html_entries(r) + http_entries(r) + sitemap_entries(r, site)


def has_hreflang(r, site=None) -> bool:
    return bool(entries(r, site))


def annotation_sources(r, site) -> List[str]:
    out = []
    if html_entries(r):
        out.append("HTML")
    if http_entries(r):
        out.append("HTTP Header")
    if sitemap_entries(r, site):
        out.append("Sitemap")
    return out


def _lang(e: dict) -> str:
    return str(e.get("lang") or "").strip().lower()


def _points_to(e: dict, r) -> bool:
    u = e.get("url") or ""
    return same_url(u, r.url) or same_url(u, seo(r).get("final_url") or "")


def _alternates(r, site=None) -> List[dict]:
    """Annotations that point somewhere other than the page itself."""
    return [e for e in entries(r, site) if e.get("url") and not _points_to(e, r)]


def _alt_page(site, url: str) -> Optional[Any]:
    """The crawled internal URL behind an hreflang URL when it answered 200 (an HTML page, or a file such as a PDF
    whose own hreflang comes from its Link header or a sitemap)."""
    t = find_result(site, url)
    if t is None or is_external(t) or first_status_code(t) != 200:
        return None
    return t


def hreflang_rows(site) -> List[Any]:
    rows = content_rows(site)
    seen = {r.url for r in rows}
    extra = [r for r in internal_rows(site)
             if r.url not in seen and category_of(r) == "2xx" and (http_entries(r) or sitemap_entries(r, site))]
    return rows + extra


def code_problems(r, site=None) -> List[str]:
    out: List[str] = []
    for e in entries(r, site):
        p = code_problem(_lang(e))
        if p and p not in out:
            out.append(p)
    return out


# ---------------------------------------------------------------------------------------------
# Alternates the crawl could not judge (P8)
# ---------------------------------------------------------------------------------------------
STATUS_CHECKS = {"non_200"}
PAGE_CHECKS = {"missing_return_links", "inconsistent_return_links", "non_canonical_return_links",
               "noindex_return_links"}


def _unjudged(site, url: str) -> Dict[str, bool]:
    """Whether the crawl can judge an alternate URL: {"status": its status is unknown (never requested, or an
    external link that was not checked), "page": its own hreflang / directives / canonical were not read (never
    requested, or on another website)}."""
    t = find_result(site, url)
    if t is None:
        return {"status": True, "page": True}
    if is_external(t):
        return {"status": category_of(t) == "", "page": True}
    return {"status": False, "page": False}


def unjudged_index(site) -> Dict[str, Dict[str, List[str]]]:
    """page url -> {"status": [alternates], "page": [alternates]} the crawl could not judge (pages with none are left
    out)."""
    def build():
        out: Dict[str, Dict[str, List[str]]] = {}
        cache: Dict[str, Dict[str, bool]] = {}
        for r in hreflang_rows(site):
            status: List[str] = []
            page: List[str] = []
            for e in _alternates(r, site):
                u = e["url"]
                if u not in cache:
                    cache[u] = _unjudged(site, u)
                if cache[u]["status"] and u not in status:
                    status.append(u)
                if cache[u]["page"] and u not in page:
                    page.append(u)
            if status or page:
                out[r.url] = {"status": status, "page": page}
        return out
    return memo(site, "hreflang_unjudged", build)


def _kind(filter_id: str) -> str:
    return "status" if filter_id in STATUS_CHECKS else "page"


def unjudged_alternates(site, r, filter_id: str) -> List[str]:
    """The alternates of page r that the check `filter_id` could not judge ([] for other checks)."""
    if filter_id not in STATUS_CHECKS and filter_id not in PAGE_CHECKS:
        return []
    return (unjudged_index(site).get(getattr(r, "url", "")) or {}).get(_kind(filter_id), [])


def has_alternates(r, site) -> bool:
    return bool(_alternates(r, site))


def fully_judged(site, r, filter_id: str) -> bool:
    """Page r has alternates and the check could judge every one of them."""
    return has_alternates(r, site) and not unjudged_alternates(site, r, filter_id)


def unjudged_reason(site, filter_id: str) -> str:
    """Why pages with alternates could not be judged by this check (the "Not checked" reason with the counts), else
    ''."""
    kind = _kind(filter_id)
    pages = [u for u, v in unjudged_index(site).items() if v.get(kind)]
    if not pages:
        return ""
    alts = {a for u in pages for a in unjudged_index(site)[u][kind]}
    if kind == "status":
        return (f"{len(alts):,} alternate URL(s) of {len(pages):,} page(s) were not requested by the crawl, so their "
                "status is not known (outside the crawl scope or limits, listed only in a sitemap, or on another "
                "website: other websites are checked only through the links pages make to them).")
    return (f"{len(alts):,} alternate URL(s) of {len(pages):,} page(s) were not requested by the crawl or are on "
            "another website, so their own hreflang, noindex and canonical could not be read (outside the crawl "
            "scope or limits, listed only in a sitemap, or on another website: other websites are never crawled).")


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _non_200(r, site) -> bool:
    return any(target_non_200(site, e["url"]) for e in entries(r, site) if e.get("url"))


def _unlinked(r, site) -> bool:
    return any(not is_start_url(site, e["url"]) and not is_linked(site, e["url"]) for e in _alternates(r, site))


def no_return_from(r, site) -> List[str]:
    """Alternates the crawl requested (answered 200) that do not list this page in their own hreflang."""
    out = []
    for e in _alternates(r, site):
        t = _alt_page(site, e["url"])
        if t is not None and not any(_points_to(x, r) for x in entries(t, site)) and e["url"] not in out:
            out.append(e["url"])
    return out


def _missing_return(r, site) -> bool:
    return bool(no_return_from(r, site))


def _inconsistent_return(r, site) -> bool:
    """An alternate links back to this page under a different code than the page uses for itself."""
    own = {_lang(e) for e in entries(r, site) if _points_to(e, r)}
    if not own:
        return False
    for e in _alternates(r, site):
        t = _alt_page(site, e["url"])
        if t is None:
            continue
        back = {_lang(x) for x in entries(t, site) if _points_to(x, r)}
        if back - own:
            return True
    return False


def _non_canonical_return(r, site) -> bool:
    for e in _alternates(r, site):
        t = find_result(site, e["url"])
        if t is not None and not is_external(t) and site.indexability_status.get(t.url) == "Canonicalised":
            return True
    return False


def _noindex_return(r, site) -> bool:
    return any(has_noindex(site, e["url"]) for e in _alternates(r, site))


def _multiple_entries(r, site) -> bool:
    urls: Dict[str, set] = defaultdict(set)
    for e in entries(r, site):
        if e.get("url"):
            urls[_lang(e)].add(norm(e["url"]))
    return any(len(v) > 1 for v in urls.values())


def _missing_self(r, site) -> bool:
    if not has_hreflang(r, site):
        return False
    canon = canonical_target(r)
    return not any(_points_to(e, r) or (canon and same_url(e.get("url") or "", canon)) for e in entries(r, site))


def _not_using_canonical(r, site) -> bool:
    if not has_hreflang(r, site) or not is_canonicalised(r):
        return False
    canon = canonical_target(r)
    return not any(same_url(e.get("url") or "", canon) for e in entries(r, site))


def _missing_x_default(r, site) -> bool:
    return has_hreflang(r, site) and not any(_lang(e) == X_DEFAULT for e in entries(r, site))


def _site_uses_hreflang(site) -> bool:
    return memo(site, "hreflang_in_use", lambda: any(has_hreflang(r, site) for r in hreflang_rows(site)))


FILTERS = [
    Filter("contains_hreflang", "Contains hreflang", lambda r, site: has_hreflang(r, site),
           description="The URL lists language or region alternates with hreflang (in the HTML, in a Link "
                       "response header or in an XML sitemap)."),
    Filter("non_200", "Non-200 hreflang URLs", _non_200,
           issue_type="Issue", priority="High",
           description="An hreflang URL does not answer 200: it is blocked, gives no response, redirects or "
                       "returns an error. Search engines skip alternates that do not load. Only URLs the "
                       "crawl requested are checked; a page with an alternate the crawl did not request is Not "
                       "checked, never passed.",
           fix="Point every hreflang annotation at the final, working URL of each language version, or "
               "fix the page that fails."),
    Filter("unlinked", "Unlinked hreflang URLs", _unlinked,
           issue_type="Issue", priority="Medium",
           description="An hreflang URL is not linked from any crawled page with a normal link, so the "
                       "annotation is the only way to find it.",
           fix="Link the language versions to each other with ordinary <a href> links (a language "
               "switcher works well), or correct the hreflang URL if it is wrong."),
    Filter("missing_return_links", "Missing Return Links", _missing_return,
           issue_type="Issue", priority="High",
           description="An alternate page was crawled but does not list this page in its own hreflang (HTML, Link "
                       "header or sitemap). Annotations must be confirmed from both sides, otherwise search engines "
                       "may ignore them. The No Return Link From column names the alternate.",
           fix="Give every language version the same full set of hreflang links, each one including "
               "all the others and itself."),
    Filter("inconsistent_return_links", "Inconsistent Language & Region Return Links", _inconsistent_return,
           issue_type="Issue", priority="High",
           description="An alternate page links back to this page with a different language or region "
                       "code than this page uses for itself (for example de on one side, de-at on the "
                       "other).",
           fix="Use one code per URL everywhere: the code a page gives itself must match the code every "
               "other version gives it."),
    Filter("non_canonical_return_links", "Non-Canonical Return Links", _non_canonical_return,
           issue_type="Issue", priority="High",
           description="An hreflang URL points to a page whose canonical names a different URL. Hreflang "
                       "should only list the canonical version of each page.",
           fix="Replace the hreflang URL with the canonical URL of that language version."),
    Filter("noindex_return_links", "Noindex Return Links", _noindex_return,
           issue_type="Issue", priority="High",
           description="An hreflang URL points to a page marked noindex, so that language version cannot "
                       "appear in search and the annotation is wasted.",
           fix="Remove noindex from the alternate if it should be found, or take it out of the hreflang "
               "set."),
    Filter("incorrect_codes", "Incorrect Language & Region Codes", lambda r, site: bool(code_problems(r, site)),
           issue_type="Issue", priority="High",
           description="An hreflang value is not a valid code. The language must be an ISO 639 code (en, "
                       "de, ja) and the optional region an ISO 3166-1 code (gb, us), joined by a hyphen; "
                       "x-default is also allowed. Common slips are en-uk (should be en-gb), jp (ja) and "
                       "cn as a language (zh).",
           fix="Correct the code; see the Code Problems column for what is wrong with each one."),
    Filter("multiple_entries", "Multiple Entries", _multiple_entries,
           issue_type="Issue", priority="High",
           description="The same language or region code is used for more than one URL, so search engines "
                       "cannot tell which page to show for it.",
           fix="Keep one URL per code. Check for a theme and a plugin both writing hreflang links."),
    Filter("missing_self_reference", "Missing Self Reference", _missing_self,
           issue_type="Warning", priority="Low",
           description="The page lists other language versions but not itself. Each page should include "
                       "itself in its own hreflang set.",
           fix="Add an hreflang link to the page's own (canonical) URL with its own language code."),
    Filter("not_using_canonical", "Not Using Canonical", _not_using_canonical,
           issue_type="Issue", priority="High",
           description="The page's canonical points to another URL, but its hreflang set does not include "
                       "that canonical URL, so the annotations describe a URL that is not meant to be "
                       "indexed.",
           fix="Put hreflang on canonical URLs only, and make the self reference use the canonical URL."),
    Filter("missing_x_default", "Missing X-Default", _missing_x_default,
           issue_type="Warning", priority="Low",
           description="The page has hreflang but no x-default entry, the fallback for visitors whose "
                       "language or region matches none of the listed versions.",
           fix="Add hreflang=\"x-default\" pointing to the default version (often the language picker or "
               "the main language)."),
    Filter("missing", "Missing", lambda r, site: not has_hreflang(r, site) and _site_uses_hreflang(site),
           description="The page has no hreflang although other pages in the crawl use it. Stays empty on "
                       "sites without hreflang."),
    Filter("outside_head", "Outside <head>", lambda r, site: bool(seo(r).get("hreflang_outside_head")),
           issue_type="Issue", priority="High",
           description="An hreflang link element sits in the <body>. Search engines only read hreflang "
                       "links in the <head>.",
           fix="Move the hreflang links into the <head>, and check that nothing before them (an <img>, "
               "<div> or script output) ends the head early."),
]


def _nth_entry(getter, i: int, field: str):
    def get(r, site):
        es = getter(r)
        return (_lang(es[i]) if field == "lang" else es[i].get("url") or "") if len(es) > i else ""
    return get


def _nth_sitemap(i: int, field: str):
    def get(r, site):
        es = sitemap_entries(r, site)
        return (_lang(es[i]) if field == "lang" else es[i].get("url") or "") if len(es) > i else ""
    return get


def _not_requested(r, site) -> List[str]:
    v = unjudged_index(site).get(getattr(r, "url", "")) or {}
    return list(dict.fromkeys(list(v.get("status") or []) + list(v.get("page") or [])))


COLUMNS = [
    ADDRESS,
    Column("title_1", "Title 1", lambda r, site: first(seo(r).get("titles") or [])),
    Column("occurrences", "Occurrences", lambda r, site: len(entries(r, site)), "num"),
    Column("sources", "Annotation Sources", lambda r, site: annotation_sources(r, site), "list"),
    Column("html_hreflang_1", "HTML hreflang 1", _nth_entry(html_entries, 0, "lang")),
    Column("html_hreflang_1_url", "HTML hreflang 1 URL", _nth_entry(html_entries, 0, "url"), "url"),
    Column("html_hreflang_2", "HTML hreflang 2", _nth_entry(html_entries, 1, "lang")),
    Column("html_hreflang_2_url", "HTML hreflang 2 URL", _nth_entry(html_entries, 1, "url"), "url"),
    Column("http_hreflang_1", "HTTP hreflang 1", _nth_entry(http_entries, 0, "lang")),
    Column("http_hreflang_1_url", "HTTP hreflang 1 URL", _nth_entry(http_entries, 0, "url"), "url"),
    Column("sitemap_hreflang_1", "Sitemap hreflang 1", _nth_sitemap(0, "lang")),
    Column("sitemap_hreflang_1_url", "Sitemap hreflang 1 URL", _nth_sitemap(0, "url"), "url"),
    Column("sitemap_hreflang_2", "Sitemap hreflang 2", _nth_sitemap(1, "lang")),
    Column("sitemap_hreflang_2_url", "Sitemap hreflang 2 URL", _nth_sitemap(1, "url"), "url"),
    Column("no_return_from", "No Return Link From", lambda r, site: no_return_from(r, site), "list"),
    Column("not_requested", "Alternates Not Requested", _not_requested, "list"),
    Column("code_problems", "Code Problems", lambda r, site: code_problems(r, site), "list"),
    CANONICAL_1,
    INDEXABILITY, INDEXABILITY_STATUS,
]

# Checks that compare pages with each other run in Crawl Analysis (step "Hreflang")
ANALYSIS_FILTERS = {"unlinked", "missing_return_links", "inconsistent_return_links", "non_canonical_return_links",
                    "noindex_return_links"}
FILTERS = [dataclasses.replace(f, requires=("analysis:hreflang",)) if f.id in ANALYSIS_FILTERS else f
           for f in FILTERS]


def skip_reason(site, filter_id: str) -> str:
    """A check of the other end of the annotations is "Not checked" (never passed) when it found nothing and no page
    with alternates could be fully judged (every such page has an alternate the crawl did not request)."""
    test = _TESTS.get(filter_id)
    if test is None:
        return ""
    pages = [r for r in hreflang_rows(site) if has_alternates(r, site)]
    if not pages or any(fully_judged(site, r, filter_id) for r in pages):
        return ""
    if any(test(r, site) for r in hreflang_rows(site)):
        return ""          # it found some: the check ran (the pages it could not judge are a caveat, intel/checks.py)
    return unjudged_reason(site, filter_id)


_TESTS = {"non_200": _non_200, "missing_return_links": _missing_return,
          "inconsistent_return_links": _inconsistent_return, "non_canonical_return_links": _non_canonical_return,
          "noindex_return_links": _noindex_return}


register(Tab(id="hreflang", name="Hreflang", order=160, columns=COLUMNS, filters=FILTERS,
             rows=hreflang_rows, skip_reason=skip_reason))
