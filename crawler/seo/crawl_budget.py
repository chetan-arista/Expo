"""
Crawl budget: the query parameters found in internal links and crawled URLs, what each one does
(tracking, sort / view, filter, pagination, session, search, action, calendar, language / currency),
how much of the crawl they waste, a ready-to-paste robots.txt Disallow block and canonical advice.
Also labels why an internal URL redirects (redirect_type: HTTP to HTTPS, www, trailing slash, case...).

analyse(site) is pure (crawl data only, no requests); get(site) computes it once and keeps it on
site.crawl_budget. tabs/parameters.py lists the parameter URLs.

Files a page needs to render (images, scripts, stylesheets, fonts, media; is_asset) are left out of the
judgement: their parameters are versions, sizes or quality (?ver=, ?w=, Next.js /_next/image?q=75), not page
variants, and a robots.txt rule that blocks them stops Google from rendering the pages.

Idea from open-seo-crawler's crawl-budget analyser (MIT, see THIRD_PARTY_NOTICES.md); rules are ours.
"""
import re
from collections import defaultdict
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urljoin, urlparse

from ..issues import classify_result
from .registry import Column

VOLUME_TRAP = 25                   # an unknown parameter behind this many distinct linked URLs is worth review
MAX_EXAMPLES = 3
ASSET_KINDS = ("images", "javascript", "css", "fonts", "media")   # content kinds pages need to render

# class -> (label, plain advice). Order = how wasteful, worst first.
CLASSES: Dict[str, Tuple[str, str]] = {
    "session": ("Session ID", "Keep the visitor's session in a cookie, never in the URL: every visit makes new URLs."),
    "action": ("Action link (cart, wishlist, reply)",
               "Cart, wishlist and comment-reply links are not pages: add rel=\"nofollow\" to those buttons and "
               "block the parameter in robots.txt."),
    "tracking": ("Tracking tag", "Use tracking tags (utm_, gclid...) only in ads and emails, never in links between "
                                 "your own pages; every page's canonical should leave them out."),
    "calendar": ("Calendar / date", "Calendar pages link endlessly into the future and past: noindex far-off dates "
                                    "and block the date parameters."),
    "search": ("Internal search", "Keep internal search results out of the index: noindex them, then block "
                                  "the search parameter in robots.txt."),
    "sort": ("Sort / view", "Sorted and re-arranged views (?sort=, ?view=, ?limit=) show the same products: point "
                            "their canonical at the plain page."),
    "filter": ("Filter (faceted navigation)",
               "Filtered pages should be canonicalised to the unfiltered category, unless a filter is a landing page "
               "you want ranked: then give it a clean URL, its own title and keep it crawlable."),
    "locale": ("Language / currency", "Serve languages and currencies on separate URLs with hreflang instead of "
                                      "switch parameters."),
    "pagination": ("Pagination", "Keep page 2, 3... crawlable with a self-referencing canonical (not pointing at "
                                 "page 1) so the products on them are found."),
    "other": ("Other parameter", "Check what this parameter changes; if the page content stays the same, "
                                 "canonicalise to the URL without it."),
}
# Classes whose URLs are duplicates or non-pages: counted as wasted and blocked in the generated robots.txt
WASTE_CLASSES = ("session", "action", "tracking", "calendar", "search", "sort", "filter")
# Page-builder AJAX pagination (Elementor e-page-<widget>) and Jetpack infinite scroll: endless canonicalised copies
ALWAYS_TRAP = {"e-page-*", "infinity"}

_TRACKING = {"gclid", "fbclid", "msclkid", "dclid", "gbraid", "wbraid", "gad_source", "gad_campaignid", "yclid",
             "mc_cid", "mc_eid", "_ga", "_gl", "igshid", "srsltid", "ref", "ref_src", "_hsenc", "_hsmi",
             "mkt_tok", "trk", "_pos", "_sid", "_ss", "_psq", "pr_prod_strat", "pr_rec_id", "pr_rec_pid",
             "pr_ref_pid", "pr_seq", "campaign", "affiliate", "aff", "clickid"}
_SESSION = {"phpsessid", "sessionid", "session_id", "sid", "jsessionid", "sessid", "___sid", "zenid", "oscsid", "cfid",
            "cftoken"}
_ACTION = {"add-to-cart", "add_to_cart", "add_to_wishlist", "add-to-wishlist", "add-to-compare", "remove_item",
           "undo_item", "removed_item", "wc-ajax", "_wpnonce", "replytocom", "ical", "outlook-ical", "share",
           "print", "wishlist_action", "action"}
_CALENDAR = {"tribe-bar-date", "eventdisplay", "date", "month", "year", "day", "week", "calendar", "cal",
             "event_date", "start_date", "end_date"}
_SEARCH = {"s", "q", "query", "search", "search_query", "searchterm", "search_term", "keyword", "keywords", "k"}
_SORT = {"orderby", "order", "sort", "sort_by", "sortby", "sort_order", "dir", "product_list_order",
         "product_list_dir", "view", "display", "layout", "mode", "grid", "per_page", "perpage", "limit",
         "product_list_limit", "product_list_mode", "show", "items_per_page", "view_mode"}
_FILTER = {"filter", "filters", "filter_by", "color", "colour", "size", "brand", "price", "min_price", "max_price",
           "rating_filter", "swoof", "jsf", "manufacturer", "material", "product_cat", "product_tag", "constraint",
           "cat", "category", "tag", "style", "width", "length", "availability", "in_stock"}
_PAGINATION = {"page", "paged", "pg", "p", "pagenum", "start", "offset", "tribe_paged", "product-page", "infinity"}
_LOCALE = {"currency", "lang", "language", "locale", "country", "___store", "___from_store", "hl", "region"}


def norm_key(key: str) -> str:
    """Lowercased parameter name; page-builder widget ids collapse (e-page-1a2b3c4 -> e-page-*), Shopify
    filter facets collapse (filter.v.price.gte -> filter.v.*), utm_ tags collapse (utm_source -> utm_*)."""
    k = (key or "").strip().lower()
    if k.startswith("utm_"):
        return "utm_*"
    m = re.match(r"^(filter\.[vp])\.", k)
    if m:
        return m.group(1) + ".*"
    m = re.match(r"^([a-z][a-z0-9]*-[a-z0-9]+)-[0-9a-f]{6,}$", k)
    return (m.group(1) + "-*") if m else k


def classify_key(key: str) -> str:
    """Class of a normalized parameter name (see CLASSES)."""
    k = key[:-1] if key.endswith("*") else key
    if key == "utm_*" or k in _TRACKING:
        return "tracking"
    if k in _SESSION:
        return "session"
    if k in _ACTION:
        return "action"
    if k.startswith("e-page-") or k in _PAGINATION:
        return "pagination"
    if k.startswith(("filter.", "e-filter-", "pa_", "filter_", "query_type_")) or k.endswith("_filter") or k in _FILTER:
        return "filter"
    if k in _SORT:
        return "sort"
    if k in _SEARCH:
        return "search"
    if k in _CALENDAR:
        return "calendar"
    if k in _LOCALE:
        return "locale"
    return "other"


def robots_rules(key: str, names: Optional[List[str]] = None) -> List[str]:
    """Disallow lines matching the parameter as the first or a later one (?key= and &key=). robots.txt
    paths are case-sensitive, so the names as written on the site (`names`) are used when known."""
    if not key or "$" in key or ("*" in key and not key.endswith("*")):
        return []
    if key.endswith("*"):
        stem = key[:-1]
        return [f"Disallow: /*?{stem}", f"Disallow: /*&{stem}"]
    out: List[str] = []
    for name in (names or [key])[:3]:
        if not name or "$" in name or "*" in name or any(ch.isspace() for ch in name):
            continue
        out += [f"Disallow: /*?{name}=", f"Disallow: /*&{name}="]
    return out


# ---------------------------------------------------------------------------------------------
# Redirect type
# ---------------------------------------------------------------------------------------------
_INDEX_FILE = re.compile(r"/index\.(?:html?|php|aspx?|jsp)$", re.I)


def redirect_type(url: str, target: str) -> str:
    """Why `url` redirects to `target`, in plain words: "HTTP to HTTPS", "www added" / "www removed",
    "Trailing slash added" / "removed", "Case", "Index file", "Parameters", "Other domain", "Other"
    (a different page), "Same URL" (cookie or loop: not a real move). Several join with " + "."""
    if not url or not target:
        return ""
    try:
        a = urlparse(url)
        b = urlparse(urljoin(url, target))
    except Exception:
        return ""
    ha, hb = (a.hostname or "").lower(), (b.hostname or "").lower()
    bare_a, bare_b = ha[4:] if ha.startswith("www.") else ha, hb[4:] if hb.startswith("www.") else hb
    if bare_a != bare_b:
        return "Other domain"
    labels: List[str] = []
    if a.scheme.lower() != b.scheme.lower():
        labels.append("HTTP to HTTPS" if b.scheme.lower() == "https" else "HTTPS to HTTP")
    if ha != hb:
        labels.append("www added" if hb.startswith("www.") else "www removed")
    pa, pb = a.path or "/", b.path or "/"
    other = False
    if pa != pb:
        if pa.rstrip("/") == pb.rstrip("/"):
            labels.append("Trailing slash added" if pb.endswith("/") else "Trailing slash removed")
        elif pa.lower() == pb.lower():
            labels.append("Case")
        elif pa.lower().rstrip("/") == pb.lower().rstrip("/"):
            labels += ["Case", "Trailing slash added" if pb.endswith("/") else "Trailing slash removed"]
        elif _INDEX_FILE.sub("/", pa) == pb or _INDEX_FILE.sub("", pa) == pb.rstrip("/"):
            labels.append("Index file")
        else:
            other = True
    if a.query != b.query and not other:
        labels.append("Parameters")
    if other:
        labels.append("Other")
    return " + ".join(labels) if labels else "Same URL"


def redirect_type_of(r) -> str:
    """redirect_type() for a crawled result that is a 3xx ("" otherwise)."""
    try:
        if classify_result(r) != "3xx":
            return ""
    except Exception:
        return ""
    dest = getattr(r, "redirect_destination", None)
    if not dest or dest == "-":
        dest = (getattr(r, "seo", None) or {}).get("final_url") or ""
    return redirect_type(getattr(r, "url", ""), dest)


# For the Response Codes / Internal tabs to import: why each redirect happens
REDIRECT_KIND = Column("redirect_kind", "Redirect Kind", lambda r, site: redirect_type_of(r))


# ---------------------------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------------------------
def _params(url: str) -> List[Tuple[str, str]]:
    try:
        q = urlparse(url).query
    except Exception:
        return []
    return parse_qsl(q, keep_blank_values=True) if q else []


@lru_cache(maxsize=50000)
def url_classes(url: str) -> Tuple[str, ...]:
    """Classes of every parameter in a URL, most wasteful first, no repeats."""
    found = {classify_key(norm_key(k)) for k, _ in _params(url)}
    return tuple(c for c in CLASSES if c in found)


def is_asset(r) -> bool:
    """A crawled file a page needs to render (image, script, stylesheet, font, media) by its Content-Type."""
    from .tabs.internal import content_kind       # here: the tabs import this module
    try:
        return content_kind(r) in ASSET_KINDS
    except Exception:
        return False


def _robots_groups(site) -> Optional[dict]:
    rep = getattr(site, "robots_audit", None)
    if not isinstance(rep, dict) or not rep.get("found"):
        return None
    try:
        from ..robots import parse_robots
        return parse_robots(rep.get("text") or "")
    except Exception:
        return None


def _platform(site) -> str:
    try:
        from ..intel.platform import detect_platform, platform_family
        return platform_family(detect_platform(site)) or ""
    except Exception:
        return ""


PLATFORM_NOTES = {
    "shopify": "Shopify: edit robots.txt through the robots.txt.liquid theme template (Online Store > Themes > "
               "Edit code); Shopify already blocks cart, checkout and sort_by URLs by default.",
    "wordpress": "WordPress: edit robots.txt in your SEO plugin (Yoast: Tools > File editor; Rank Math: General "
                 "Settings > Edit robots.txt) or as a file in the site root.",
    "magento": "Magento: Content > Design > Configuration > (store view) > Search Engine Robots > Edit custom "
               "instruction of robots.txt File.",
}


def analyse(site) -> Dict[str, Any]:
    """Parameter report for a Site (see module docstring). Never raises; empty shapes without data."""
    buckets: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"linked": set(), "crawled": [], "max_value": 0,
                                                               "examples": [], "raw": set()})
    for page in getattr(site, "pages", None) or []:
        seo = getattr(page, "seo", None) or {}
        for link in seo.get("links") or ():
            try:
                raw = link.get("raw") or ""
                if "?" not in raw or not link.get("internal"):
                    continue
                absu = urljoin(page.url, raw).split("#", 1)[0]
                for k, v in _params(absu):
                    b = buckets[norm_key(k)]
                    b["linked"].add(absu)
                    b["raw"].add(k)
                    if v.isdigit() and len(v) < 10:
                        b["max_value"] = max(b["max_value"], int(v))
                    if len(b["examples"]) < MAX_EXAMPLES and absu not in b["examples"]:
                        b["examples"].append(absu)
            except Exception:
                continue
    by_url = getattr(site, "by_url", None) or {}
    for url, row in by_url.items():
        if "?" not in url or is_asset(row):
            continue
        seen: Set[str] = set()
        for raw_key, _ in _params(url):
            k = norm_key(raw_key)
            buckets[k]["raw"].add(raw_key)
            if k in seen:
                continue
            seen.add(k)
            buckets[k]["crawled"].append(url)
            if len(buckets[k]["examples"]) < MAX_EXAMPLES and url not in buckets[k]["examples"]:
                buckets[k]["examples"].append(url)

    groups = _robots_groups(site)
    indexable = getattr(site, "indexable", None) or {}
    status = getattr(site, "indexability_status", None) or {}
    params: List[Dict[str, Any]] = []
    for key, b in buckets.items():
        cls = classify_key(key)
        linked, crawled = len(b["linked"]), len(b["crawled"])
        trap = cls in WASTE_CLASSES or key in ALWAYS_TRAP or (cls == "other" and max(linked, crawled) >= VOLUME_TRAP)
        already = False
        if groups is not None and b["examples"]:
            try:
                from ..robots import decide
                ex = urlparse(b["examples"][0])
                already = not decide(groups, "googlebot", (ex.path or "/") + "?" + ex.query)[0]
            except Exception:
                already = False
        label, advice = CLASSES[cls]
        params.append({
            "key": key, "names": sorted(b["raw"])[:5] or [key], "class": cls, "label": label, "advice": advice,
            "linked_urls": linked, "crawled": crawled,
            "indexable": sum(1 for u in b["crawled"] if indexable.get(u)),
            "canonicalised": sum(1 for u in b["crawled"] if status.get(u) == "Canonicalised"),
            "noindex": sum(1 for u in b["crawled"] if status.get(u) == "noindex"),
            "max_value": b["max_value"], "examples": b["examples"][:MAX_EXAMPLES],
            "trap": trap, "review": cls == "other" and trap, "already_blocked": already,
            "rules": robots_rules(key, sorted(b["raw"])) if trap else [],
        })
    order = {c: i for i, c in enumerate(CLASSES)}
    params.sort(key=lambda p: (not p["trap"], order.get(p["class"], 99), -max(p["linked_urls"], p["crawled"])))

    # Wasted share: crawled internal URLs that only re-show another page or are not pages at all
    total = len(by_url)
    param_urls = [u for u in by_url if "?" in u]
    wasted: Set[str] = set()
    for u in param_urls:
        if is_asset(by_url[u]):
            continue
        if any(c in WASTE_CLASSES for c in url_classes(u)) or status.get(u) == "Canonicalised":
            wasted.add(u)
    kinds = {u: redirect_type_of(r) for u, r in by_url.items()}
    redirects: Dict[str, int] = defaultdict(int)
    for kind in kinds.values():
        if kind:
            redirects[kind] += 1
    redirect_urls = sum(redirects.values())
    inlinks = getattr(site, "inlinks", None) or {}
    links_to_redirects = sum(len(inlinks.get(u, [])) for u, kind in kinds.items() if kind)

    lines = ["# Suggested by the crawl-budget check: review before you paste it.",
             "# A URL blocked here keeps its canonical and noindex hidden from Google, so block parameters",
             "# only after their pages are canonicalised or out of the index.", "User-agent: *"]
    new_rules: List[str] = []
    review: List[str] = []
    for p in params:
        if not p["trap"] or p["already_blocked"]:
            continue
        for rule in p["rules"]:
            target = review if p["review"] else new_rules
            if rule not in target:
                target.append(rule)
    block = ""
    if new_rules or review:
        block = "\n".join(lines + new_rules + ([f"# {r}   (review: unknown parameter with many URLs)" for r in review]))
    advice_classes = [c for c in CLASSES if any(p["class"] == c and (p["crawled"] or p["linked_urls"]) for p in params)]
    platform = _platform(site)
    notes = []
    if PLATFORM_NOTES.get(platform):
        notes.append(PLATFORM_NOTES[platform])
    if not total:
        notes.append("Nothing was crawled, so there is nothing to measure.")
    return {
        "params": params,
        "total_urls": total,
        "param_urls": len(param_urls),
        "wasted_urls": len(wasted),
        "wasted_share": round(len(wasted) / total * 100, 1) if total else 0.0,
        "wasted_examples": sorted(wasted)[:10],
        "redirects": {"urls": redirect_urls, "links_to_redirects": links_to_redirects, "types": dict(redirects)},
        "robots_block": block,
        "already_blocked": [p["key"] for p in params if p["already_blocked"]],
        "canonical_advice": [{"class": c, "label": CLASSES[c][0], "advice": CLASSES[c][1]} for c in advice_classes],
        "platform": platform,
        "notes": notes,
    }


def get(site) -> Dict[str, Any]:
    """analyse(site), computed once per Site and kept on site.crawl_budget."""
    rep = getattr(site, "crawl_budget", None)
    if isinstance(rep, dict):
        return rep
    try:
        rep = analyse(site)
    except Exception as e:
        rep = {"params": [], "total_urls": 0, "param_urls": 0, "wasted_urls": 0, "wasted_share": 0.0,
               "wasted_examples": [], "redirects": {"urls": 0, "links_to_redirects": 0, "types": {}},
               "robots_block": "", "already_blocked": [], "canonical_advice": [], "platform": "",
               "notes": [f"The crawl-budget analysis failed ({type(e).__name__})."]}
    try:
        site.crawl_budget = rep
    except Exception:
        pass
    return rep
