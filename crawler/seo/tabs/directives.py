"""
Directives tab (Screaming Frog "Directives"): robots directives of every internal URL that answered
2xx, from <meta name="robots"> / <meta name="googlebot"> in HTML and from the X-Robots-Tag response
header (which is how files such as PDFs and images carry them). Pages, images, scripts and
stylesheets are all listed, matching Screaming Frog (435 rows on the reference crawl).

Each directive filter is a plain token check on PageData["directives"]; a URL can be in several.
Meta refresh is listed here for reference only: it is reported as an issue in Response Codes
("Internal Redirection (Meta Refresh)"), so it is not counted twice.
"""
from typing import Any, List

from ..registry import Column, Filter, Tab, first, register, seo
from .canonicals import CANONICAL_1, HTTP_CANONICAL
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, internal_rows


def directive_rows(site) -> List[Any]:
    """Every internal URL that answered 2xx, in crawl order."""
    return [r for r in internal_rows(site) if category_of(r) == "2xx"]


def has(token: str):
    return lambda r, site: token in (seo(r).get("directives") or [])


def _nth(key: str, i: int):
    def get(r, site):
        values = seo(r).get(key) or []
        return values[i] if len(values) > i else ""
    return get


FILTERS = [
    Filter("index", "Index", has("index"),
           description="The URL says \"index\" explicitly. Indexing is the default, so the tag is harmless "
                       "but not needed."),
    Filter("noindex", "Noindex", has("noindex"),
           issue_type="Warning", priority="High",
           description="The URL asks search engines not to show it in search results. It can still be "
                       "crawled. Fine for thank-you pages, tag archives and the like, but a serious problem "
                       "on a page you want people to find.",
           fix="Check the list: remove noindex from every page that should appear in search (often set by "
               "an SEO plugin or a leftover from a staging site)."),
    Filter("follow", "Follow", has("follow"),
           description="The URL says \"follow\" explicitly. Following links is the default, so the tag is "
                       "harmless but not needed."),
    Filter("nofollow", "Nofollow", has("nofollow"),
           issue_type="Warning", priority="High",
           description="The URL asks search engines not to follow any link on it, so the pages it links to "
                       "get no value from it and may not be discovered.",
           fix="Remove nofollow from the page unless you really do not want its links followed; to "
               "nofollow single links, use rel=\"nofollow\" on those links instead."),
    Filter("none", "None", has("none"),
           issue_type="Warning", priority="High",
           description="The URL uses \"none\", which means noindex and nofollow together: it stays out of "
                       "search results and its links are not followed.",
           fix="Remove \"none\" unless the page should be fully hidden from search engines."),
    Filter("noarchive", "NoArchive", has("noarchive"),
           description="The URL asks search engines not to keep a cached copy of it."),
    Filter("nosnippet", "NoSnippet", has("nosnippet"),
           issue_type="Warning", priority="Low",
           description="The URL asks search engines not to show a text or video preview under it in results. "
                       "The listing then looks bare and tends to get fewer clicks.",
           fix="Remove nosnippet unless you have a reason to hide previews; use max-snippet to limit "
               "length instead."),
    Filter("max_snippet", "Max-Snippet", has("max-snippet"),
           description="The URL limits how many characters of text search engines may show as a preview "
                       "(-1 = no limit, 0 = none)."),
    Filter("max_image_preview", "Max-Image-Preview", has("max-image-preview"),
           description="The URL sets the largest image preview search engines may show "
                       "(none, standard or large)."),
    Filter("max_video_preview", "Max-Video-Preview", has("max-video-preview"),
           description="The URL limits how many seconds of video preview search engines may show "
                       "(-1 = no limit, 0 = still image only)."),
    Filter("noodp", "NoODP", has("noodp"),
           issue_type="Warning", priority="Low",
           description="The URL uses noodp, an old tag for the long-closed Open Directory Project. Search "
                       "engines ignore it.",
           fix="Remove noodp from the robots tag; it does nothing."),
    Filter("noydir", "NoYDIR", has("noydir"),
           issue_type="Warning", priority="Low",
           description="The URL uses noydir, an old tag for the closed Yahoo Directory. Search engines "
                       "ignore it.",
           fix="Remove noydir from the robots tag; it does nothing."),
    Filter("noimageindex", "NoImageIndex", has("noimageindex"),
           issue_type="Issue", priority="Low",
           description="The URL asks search engines not to index the images on it. Those images will not "
                       "appear in image search through this page.",
           fix="Remove noimageindex unless you deliberately want the page's images kept out of image search."),
    Filter("notranslate", "NoTranslate", has("notranslate"),
           issue_type="Warning", priority="Low",
           description="The URL asks Google not to offer a translation of it in search results, which can "
                       "cost visitors who read other languages.",
           fix="Remove notranslate unless translations of the page would be wrong or harmful."),
    Filter("unavailable_after", "Unavailable_After", has("unavailable_after"),
           issue_type="Warning", priority="Medium",
           description="The URL tells search engines to drop it from results after a set date. Once the date "
                       "passes, the page disappears from search even if it is still live.",
           fix="Check the date is right; remove the directive from pages that should stay in search."),
    Filter("refresh", "Refresh", lambda r, site: bool(seo(r).get("meta_refresh")),
           description="The page has a <meta http-equiv=\"refresh\">, which reloads it or sends visitors to "
                       "another URL after a delay. Redirecting refreshes are reported in Response Codes."),
    Filter("outside_head", "Outside <head>", lambda r, site: bool(seo(r).get("meta_robots_outside_head")),
           issue_type="Issue", priority="High",
           description="A robots meta tag sits in the <body>. Search engines may ignore it, so a noindex "
                       "or nofollow there may not work (or may apply when you did not expect it).",
           fix="Move the robots meta tag into the <head>, and check that nothing before it (an <img>, "
               "<div> or script output) ends the head early."),
]

COLUMNS = [
    ADDRESS,
    Column("occurrences", "Occurrences",
           lambda r, site: len(seo(r).get("meta_robots") or []) + len(seo(r).get("x_robots_tag") or []), "num"),
    Column("meta_robots_1", "Meta Robots 1", _nth("meta_robots", 0)),
    Column("meta_robots_2", "Meta Robots 2", _nth("meta_robots", 1)),
    Column("x_robots_tag_1", "X-Robots-Tag 1", _nth("x_robots_tag", 0)),
    Column("meta_refresh_1", "Meta Refresh 1", lambda r, site: seo(r).get("meta_refresh") or ""),
    CANONICAL_1, HTTP_CANONICAL,
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="directives", name="Directives", order=150, columns=COLUMNS, filters=FILTERS,
             rows=directive_rows))
