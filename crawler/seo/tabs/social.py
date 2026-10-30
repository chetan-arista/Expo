"""
Social tab (ours, after siteone-crawler's Open Graph analyser): how each internal HTML page that loaded (2xx)
looks when it is shared on Facebook, LinkedIn, X, WhatsApp or Pinterest, read from its Open Graph and
Twitter Card tags (PageData og / twitter, extract.py).

Like the on-page tabs, the filters look at indexable pages only (pages kept out of search are rarely shared).
og:image Not Absolute flags images given as a relative path, which most sharing platforms cannot load.
og:url Differs From Canonical compares normalised URLs. Duplicate og:title groups indexable pages that share
the same og:title (empty titles are not grouped).
"""
from typing import Any, Dict, List

from ...url_utils import normalize_url
from ..registry import Column, Filter, Tab, register, seo
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, canonical_of, memo
from .page_titles import INDEXABLE_NOTE, content_rows


def og(r) -> Dict[str, str]:
    v = seo(r).get("og")
    return v if isinstance(v, dict) else {}


def twitter(r) -> Dict[str, str]:
    v = seo(r).get("twitter")
    return v if isinstance(v, dict) else {}


def _val(d: Dict[str, str], key: str) -> str:
    return " ".join(str(d.get(key) or "").split())


def _norm(u: str) -> str:
    try:
        return normalize_url(u) or u
    except Exception:
        return u


def _og_url_differs(r, site) -> bool:
    u = _val(og(r), "url")
    canonical = canonical_of(r) or r.url
    return bool(u) and u.startswith(("http://", "https://")) and _norm(u) != _norm(canonical)


def _image_relative(r, site) -> bool:
    img = _val(og(r), "image")
    return bool(img) and not img.lower().startswith(("http://", "https://"))


def _og_title_groups(site) -> Dict[str, List[str]]:
    def build():
        groups: Dict[str, List[str]] = {}
        for r in content_rows(site):
            if not site.indexable.get(r.url):
                continue
            t = _val(og(r), "title").lower()
            if t:
                groups.setdefault(t, []).append(r.url)
        return {t: us for t, us in groups.items() if len(us) > 1}
    return memo(site, "social_og_title_groups", build)


def _duplicate_title(r, site) -> bool:
    t = _val(og(r), "title").lower()
    return bool(t) and t in _og_title_groups(site)


def _missing(d, key: str):
    return lambda r, site: not _val(d(r), key)


_REQ = ("pagedata:og",)

FILTERS = [
    Filter("missing_og_title", "Missing og:title", _missing(og, "title"), "Opportunity", "Low",
           only_indexable=True, requires=_REQ,
           description="The page has no og:title. Sharing platforms then guess a title (often the page title, "
                       "sometimes a menu label)." + INDEXABLE_NOTE,
           fix="Add <meta property=\"og:title\" content=\"...\"> with a clear, shareable title.",
           short_fix="Add an og:title meta tag"),
    Filter("missing_og_description", "Missing og:description", _missing(og, "description"), "Opportunity", "Low",
           only_indexable=True, requires=_REQ,
           description="The page has no og:description, so shares show a guessed snippet or none." + INDEXABLE_NOTE,
           fix="Add <meta property=\"og:description\" content=\"...\"> with one or two inviting sentences.",
           short_fix="Add an og:description meta tag"),
    Filter("missing_og_image", "Missing og:image", _missing(og, "image"), "Opportunity", "Medium",
           only_indexable=True, requires=_REQ,
           description="The page has no og:image, so shares on Facebook, LinkedIn, WhatsApp or Pinterest show no "
                       "picture or a random one. Posts with an image get far more clicks." + INDEXABLE_NOTE,
           fix="Add <meta property=\"og:image\" content=\"https://...\"> pointing to an image of at least "
               "1200 x 630 pixels.",
           short_fix="Add an og:image (1200x630, absolute https URL)"),
    Filter("og_image_relative", "og:image Not Absolute", _image_relative, "Warning", "Low",
           only_indexable=True, requires=_REQ,
           description="og:image is a relative path (e.g. /img/share.jpg). Most sharing platforms need a full "
                       "https:// address and show no image otherwise." + INDEXABLE_NOTE,
           fix="Write og:image as a full address starting with https://.",
           short_fix="Use a full https:// URL in og:image"),
    Filter("og_url_differs", "og:url Differs From Canonical", _og_url_differs, "Warning", "Low",
           only_indexable=True, requires=_REQ,
           description="og:url points to a different address than the page's canonical, so likes and shares "
                       "may be counted for another URL." + INDEXABLE_NOTE,
           fix="Set og:url to the page's canonical URL.",
           short_fix="Set og:url to the canonical URL"),
    Filter("missing_twitter_card", "Missing twitter:card", _missing(twitter, "card"), "Opportunity", "Low",
           only_indexable=True, requires=("pagedata:twitter",),
           description="The page has no twitter:card, so posts on X show a plain link without the large image "
                       "preview." + INDEXABLE_NOTE,
           fix="Add <meta name=\"twitter:card\" content=\"summary_large_image\"> (X reuses og:title, "
               "og:description and og:image).",
           short_fix="Add twitter:card summary_large_image"),
    Filter("duplicate_og_title", "Duplicate og:title", _duplicate_title, "Opportunity", "Low",
           only_indexable=True, requires=_REQ,
           description="Several indexable pages share the same og:title, so their shares look identical."
                       + INDEXABLE_NOTE,
           fix="Give each page its own og:title (usually the page's own title).",
           short_fix="Give each page its own og:title"),
]

COLUMNS = [
    ADDRESS,
    Column("og_title", "og:title", lambda r, site: _val(og(r), "title")),
    Column("og_description", "og:description", lambda r, site: _val(og(r), "description")),
    Column("og_image", "og:image", lambda r, site: _val(og(r), "image"), "url"),
    Column("og_type", "og:type", lambda r, site: _val(og(r), "type")),
    Column("og_url", "og:url", lambda r, site: _val(og(r), "url"), "url"),
    Column("og_site_name", "og:site_name", lambda r, site: _val(og(r), "site_name")),
    Column("twitter_card", "twitter:card", lambda r, site: _val(twitter(r), "card")),
    Column("twitter_image", "twitter:image", lambda r, site: _val(twitter(r), "image"), "url"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="social", name="Social", order=280, columns=COLUMNS, filters=FILTERS, rows=content_rows,
             description="Open Graph and Twitter Card tags: how pages look when shared on social media."))
