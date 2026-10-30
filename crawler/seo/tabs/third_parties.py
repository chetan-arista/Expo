"""
Third Parties tab (ours, after siteone-crawler's source-domain and technology analysers): which outside
services each internal HTML page that loaded (2xx) pulls in, and whether the site's tracking tags are on
every page.

  - Third-party hosts: hosts of the files a page loads (scripts, stylesheets, images, frames, fonts) that are
    not the site's own domain or one of its subdomains. From the HTML; for pages rendered in Chrome also every
    request the page made (tags that Google Tag Manager injects at runtime are only visible that way).
  - Tracking Tag Missing: a tag (Google Tag Manager, Google tag / GA4, Meta Pixel, Microsoft Clarity,
    Hotjar...) found on at least 80 % of the pages (and on 5 pages or more) is missing from this page, so
    visits to it are probably not measured. GA4 loaded through Google Tag Manager is invisible in the HTML:
    the GTM container stands in for it.
third_party_summary(site) aggregates the hosts for reports (vendor, pages, files, known bytes).
"""
from collections import Counter
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse

from ...url_utils import clean_hostname
from ..registry import Column, Filter, Tab, register, rendered_content, seo
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, memo
from .page_titles import content_rows

MANY_HOSTS = 10                  # a page loading files from more third-party hosts than this is flagged
TAG_COVERAGE = 0.80              # a tag on this share of pages is "the site's tag"
TAG_MIN_PAGES = 5

# tag name -> substrings of the URLs that load it (host + path, lowercase)
TRACKING_TAGS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Google Tag Manager", ("googletagmanager.com/gtm.js", "googletagmanager.com/ns.html")),
    ("Google tag (gtag.js / GA4)", ("googletagmanager.com/gtag/js",)),
    ("Universal Analytics", ("google-analytics.com/analytics.js", "google-analytics.com/ga.js")),
    ("Meta Pixel", ("connect.facebook.net/en_us/fbevents.js", "/fbevents.js", "facebook.com/tr")),
    ("Microsoft Clarity", ("clarity.ms/tag",)),
    ("Hotjar", ("static.hotjar.com",)),
    ("LinkedIn Insight", ("snap.licdn.com",)),
    ("TikTok Pixel", ("analytics.tiktok.com",)),
    ("Segment", ("cdn.segment.com",)),
    ("HubSpot", ("js.hs-scripts.com", "js.hs-analytics.net")),
    ("Adobe Experience Platform Tags", ("assets.adobedtm.com",)),
    ("Matomo", ("matomo.js", "piwik.js")),
    ("Plausible", ("plausible.io/js",)),
    ("Klaviyo", ("static.klaviyo.com",)),
    ("Microsoft Advertising UET", ("bat.bing.com",)),
    ("Pinterest Tag", ("s.pinimg.com/ct/", "ct.pinterest.com")),
    ("X (Twitter) Pixel", ("static.ads-twitter.com",)),
)


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _own(site, host: str) -> bool:
    base = clean_hostname(getattr(site, "base_domain", "") or "")
    h = clean_hostname(host)
    return bool(base) and (h == base or h.endswith("." + base))


def loaded_urls(r) -> List[str]:
    """Every file URL the page loads: HTML resources and images, plus Chrome's requests when rendered."""
    p = seo(r)
    urls = [x.get("url") for x in p.get("resources") or () if isinstance(x, dict) and x.get("url")]
    urls += [i.get("src") for i in p.get("images") or () if isinstance(i, dict) and i.get("src")]
    rd = rendered_content(r)   # a redirect that fired in Chrome loaded the destination's files
    if rd is not None:
        urls += [x.get("url") for x in rd.get("resources") or () if isinstance(x, dict) and x.get("url")]
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def third_party_hosts(site, r) -> List[str]:
    def build():
        seen: Dict[str, None] = {}
        for u in loaded_urls(r):
            h = _host(u)
            if h and not _own(site, h):
                seen.setdefault(h, None)
        return list(seen)
    return memo(site, "tp_hosts:" + r.url, build)


def tags_on(site, r) -> List[str]:
    def build():
        low = [u.lower() for u in loaded_urls(r)]
        return [name for name, needles in TRACKING_TAGS if any(n in u for u in low for n in needles)]
    return memo(site, "tp_tags:" + r.url, build)


def site_tags(site) -> List[str]:
    """Tags present on at least TAG_COVERAGE of the pages (and on TAG_MIN_PAGES pages or more)."""
    def build():
        pages = content_rows(site)
        if not pages:
            return []
        counts: Counter = Counter()
        for r in pages:
            counts.update(set(tags_on(site, r)))
        return [t for t, n in counts.items() if n >= TAG_MIN_PAGES and n >= TAG_COVERAGE * len(pages)]
    return memo(site, "tp_site_tags", build)


def missing_tags(site, r) -> List[str]:
    have = set(tags_on(site, r))
    return [t for t in site_tags(site) if t not in have]


def _vendor(host: str) -> str:
    try:
        from ...intel.platform import APP_HOSTS
    except Exception:
        APP_HOSTS = ()
    for needle, name in APP_HOSTS:
        if needle.split("/", 1)[0] in host:
            return name
    for name, needles in TRACKING_TAGS:
        if any(n.split("/", 1)[0] in host for n in needles if "." in n.split("/", 1)[0]):
            return name
    return ""


def third_party_summary(site) -> List[Dict[str, Any]]:
    """One row per third-party host, most widely loaded first: {"host", "vendor", "pages", "pct_pages",
    "files", "bytes_known"} (bytes from the external link checker's Content-Length when it ran)."""
    def build():
        pages = content_rows(site)
        sizes: Dict[str, int] = {}
        for e in site.externals or ():
            v = getattr(e, "size_bytes", None)
            if isinstance(v, int) and v > 0:
                sizes[getattr(e, "url", "")] = v
        per_host: Dict[str, Dict[str, Any]] = {}
        for r in pages:
            for u in set(loaded_urls(r)):
                h = _host(u)
                if not h or _own(site, h):
                    continue
                e = per_host.setdefault(h, {"host": h, "vendor": _vendor(h), "pages": set(), "files": set()})
                e["pages"].add(r.url)
                e["files"].add(u)
        out = []
        for h, e in per_host.items():
            out.append({"host": h, "vendor": e["vendor"], "pages": len(e["pages"]),
                        "pct_pages": round(len(e["pages"]) / max(1, len(pages)) * 100, 1), "files": len(e["files"]),
                        "bytes_known": sum(sizes.get(u, 0) for u in e["files"])})
        out.sort(key=lambda x: (-x["pages"], -x["files"], x["host"]))
        return out
    return memo(site, "tp_summary", build)


FILTERS = [
    Filter("many_hosts", f"Loads Over {MANY_HOSTS} Third-Party Hosts",
           lambda r, site: len(third_party_hosts(site, r)) > MANY_HOSTS, "Opportunity", "Low",
           description=f"The page loads files from more than {MANY_HOSTS} other websites (apps, widgets, "
                       "trackers, fonts, CDNs). Each one costs a connection and download time, and any of them can "
                       "slow or break the page.",
           fix="Review the Third-Party Hosts list with the site owner and remove apps and tags that are no "
               "longer used; load the rest with async/defer.",
           short_fix="Remove unused apps and tags; load the rest async"),
    Filter("tracking_missing", "Tracking Tag Missing", lambda r, site: bool(missing_tags(site, r)),
           "Warning", "Medium",
           description=f"A tracking tag the site uses on at least {int(TAG_COVERAGE * 100)}% of its pages is "
                       "missing here, so visits to this page are probably not counted in analytics. Found from "
                       "the files pages load (tags added at runtime need JavaScript rendering).",
           fix="Add the missing tag (see Missing Tags) to this page's template, usually the shared header.",
           short_fix="Add the site's analytics tag to this template"),
]

COLUMNS = [
    ADDRESS,
    Column("third_party_count", "Third-Party Hosts", lambda r, site: len(third_party_hosts(site, r)), "num"),
    Column("third_party_hosts", "Third-Party Host List", lambda r, site: third_party_hosts(site, r), "list"),
    Column("tracking_tags", "Tracking Tags", lambda r, site: tags_on(site, r), "list"),
    Column("missing_tags", "Missing Tags", lambda r, site: missing_tags(site, r), "list"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="third_parties", name="Third Parties", order=290, columns=COLUMNS, filters=FILTERS,
             rows=content_rows,
             description="Outside services each page loads, and tracking tags missing from some pages."))
