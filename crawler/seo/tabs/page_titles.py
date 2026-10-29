"""
Page Titles tab (Screaming Frog "Page Titles"): the <title> of every internal HTML page that
loaded (2xx), with its length in characters and its approximate width in Google's results.

This module also holds the helpers the Meta Description, Meta Keywords, H1 and H2 tabs share
(rows, stored values, duplicate detection, value/length/pixel columns), so all five tabs count
the same way.

How the counts match Screaming Frog (checked against a Screaming Frog 24.3 crawl in research/):
  - "All" lists HTML pages that answered 2xx; redirects and error pages have no content to check.
  - Every filter runs on indexable pages only (Screaming Frog's default "Ignore Non-Indexable URLs
    for On-Page Filters"); non-indexable pages are still listed under "All".
  - Screaming Frog keeps the first two of each element (Title 1/2, H1-1/H1-2, ...), and the length,
    pixel and duplicate checks look at both. "Occurrences" still counts every element on the page.
  - Duplicate: a value that appears more than once across the indexable pages, counting both stored
    values of every page (so a page whose two H1s are identical is a duplicate too).
"""
from collections import Counter
from typing import Any, Callable, List, Optional

from ..registry import Column, Filter, Tab, pref_number, register, seo, threshold_filter
from ..text_metrics import pixel_width
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, is_html_doc, memo

STORED = 2                    # Screaming Frog stores the first two of each element

# Defaults of Configuration > Preferences (crawler.config.DEFAULT_PREFERENCES); the filters read the crawl's own
# numbers through registry.pref_number and show them in their names (P14)
TITLE_MAX_CHARS = 60
TITLE_MIN_CHARS = 30
TITLE_MAX_PX = 561
TITLE_MIN_PX = 200

INDEXABLE_NOTE = " Checked on indexable pages only."


# ---------------------------------------------------------------------------------------------
# Shared helpers (Page Titles, Meta Description, Meta Keywords, H1, H2)
# ---------------------------------------------------------------------------------------------
def content_rows(site) -> List[Any]:
    """Internal HTML pages that loaded (2xx), in crawl order."""
    return [r for r in site.pages if is_html_doc(r) and category_of(r) == "2xx"]


def text_list(r, key: str) -> List[str]:
    """A PageData list as stripped strings (every element, in document order)."""
    return [(v if isinstance(v, str) else str(v or "")).strip() for v in (seo(r).get(key) or [])]


def stored(values: List[str]) -> List[str]:
    """The values Screaming Frog keeps and checks: the first two."""
    return values[:STORED]


def present(values: List[str]) -> List[str]:
    """Stored values that are not empty."""
    return [v for v in stored(values) if v]


def nth(values: List[str], i: int) -> str:
    return values[i] if len(values) > i else ""


def is_missing(values: List[str]) -> bool:
    """No element, or every element is empty / whitespace."""
    return not any(values)


def is_multiple(values: List[str]) -> bool:
    return len(values) > 1


def any_value(values: List[str], test: Callable[[str], bool]) -> bool:
    return any(test(v) for v in present(values))


def duplicate_test(key: str, values_of: Callable[[Any], List[str]]) -> Callable[[Any, Any], bool]:
    """Filter test: one of the page's stored values also appears elsewhere among the indexable
    pages (or twice on the same page)."""
    def counts(site) -> Counter:
        c: Counter = Counter()
        for r in content_rows(site):
            if site.indexable.get(r.url):
                c.update(present(values_of(r)))
        return c

    def test(r, site) -> bool:
        c = memo(site, "dup:" + key, lambda: counts(site))
        return any(c[v] > 1 for v in present(values_of(r)))
    return test


def duplicate_urls(site, key: str, values_of: Callable[[Any], List[str]], r) -> List[str]:
    """Other indexable pages sharing one of this page's stored values (for detail panes)."""
    mine = set(present(values_of(r)))
    if not mine:
        return []
    return [o.url for o in content_rows(site)
            if o.url != r.url and site.indexable.get(o.url) and mine & set(present(values_of(o)))]


def occurrences_column(values_of: Callable[[Any], List[str]]) -> Column:
    return Column("occurrences", "Occurrences", lambda r, site: len(values_of(r)), "num")


def value_columns(id_prefix: str, label: str, values_of: Callable[[Any], List[str]],
                  px_kind: Optional[str] = None, sep: str = " ") -> List[Column]:
    """'<label>1', '<label>1 Length' (and '... Pixel Width') for each stored value.
    sep is what goes between label and number: "Title 1" vs "H1-1"."""
    cols: List[Column] = []
    for i in range(STORED):
        name = f"{label}{sep}{i + 1}"
        cid = f"{id_prefix}_{i + 1}"
        cols.append(Column(cid, name, lambda r, site, i=i: nth(values_of(r), i)))
        cols.append(Column(f"{cid}_length", f"{name} Length",
                           lambda r, site, i=i: len(nth(values_of(r), i)), "num"))
        if px_kind:
            cols.append(Column(f"{cid}_pixel_width", f"{name} Pixel Width",
                               lambda r, site, i=i, k=px_kind: pixel_width(nth(values_of(r), i), k), "num"))
    return cols


# ---------------------------------------------------------------------------------------------
# Page Titles
# ---------------------------------------------------------------------------------------------
def titles_of(r) -> List[str]:
    return text_list(r, "titles")


def h1_of(r) -> List[str]:
    """Every <h1> in order. An h1 holding only an image counts its alt text as its text, as
    Screaming Frog does (alt texts are matched to the empty h1s in document order)."""
    alts = list(text_list(r, "h1_alt_only"))
    out = []
    for v in text_list(r, "h1"):
        if not v and alts:
            v = alts.pop(0)
        out.append(v)
    return out


def same_as_h1(r, site) -> bool:
    title = nth(titles_of(r), 0)
    return bool(title) and title in present(h1_of(r))


title_duplicate = duplicate_test("title", titles_of)

FILTERS = [
    Filter("missing", "Missing", lambda r, site: is_missing(titles_of(r)),
           issue_type="Issue", priority="High", only_indexable=True,
           description="The page has no <title>, or the title is empty. Search engines then make up "
                       "their own headline for the result, and it is often a poor one." + INDEXABLE_NOTE,
           fix="Add a short, unique <title> in the <head> that says what the page is about and "
               "includes the main search phrase."),
    Filter("duplicate", "Duplicate", title_duplicate,
           issue_type="Opportunity", priority="Medium", only_indexable=True,
           description="The same title is used on more than one indexable page, so searchers and "
                       "search engines cannot tell the pages apart." + INDEXABLE_NOTE,
           fix="Give every page its own title that describes what is different about it. If the "
               "pages really are the same, keep one and redirect or canonicalise the others."),
    threshold_filter("over_60_characters", "title_max_chars", "Over {n} Characters",
                     lambda r, site: any_value(titles_of(r), lambda v: len(v) > pref_number(site, "title_max_chars")),
                     issue_type="Opportunity", priority="Medium", only_indexable=True,
                     description="The title is longer than {n} characters, so Google will probably cut it off "
                                 "with '...' in the results." + INDEXABLE_NOTE,
                     fix="Shorten the title and put the most important words first, so nothing that matters "
                         "is cut off."),
    threshold_filter("below_30_characters", "title_min_chars", "Below {n} Characters",
                     lambda r, site: any_value(titles_of(r), lambda v: len(v) < pref_number(site, "title_min_chars")),
                     issue_type="Opportunity", priority="Medium", only_indexable=True,
                     description="The title is shorter than {n} characters. There is room to describe the page "
                                 "better and match more searches." + INDEXABLE_NOTE,
                     fix="Make the title more descriptive, for example by adding the main topic, a key detail "
                         "or the brand name."),
    threshold_filter("over_561_pixels", "title_max_px", "Over {n} Pixels",
                     lambda r, site: any_value(titles_of(r), lambda v: pixel_width(v, "title") >
                                               pref_number(site, "title_max_px")),
                     issue_type="Opportunity", priority="Medium", only_indexable=True,
                     description="The title is wider than about {n} pixels, the space Google gives a title on "
                                 "desktop, so it will likely be truncated. Wide letters (W, M) use more room than "
                                 "narrow ones (i, l)." + INDEXABLE_NOTE,
                     fix="Shorten the title or use narrower wording, keeping the key words at the start."),
    threshold_filter("below_200_pixels", "title_min_px", "Below {n} Pixels",
                     lambda r, site: any_value(titles_of(r), lambda v: pixel_width(v, "title") <
                                               pref_number(site, "title_min_px")),
                     issue_type="Opportunity", priority="Medium", only_indexable=True,
                     description="The title uses less than {n} pixels of the space Google shows, which leaves "
                                 "most of the headline unused." + INDEXABLE_NOTE,
                     fix="Write a fuller title that explains the page and includes the words people search for."),
    Filter("same_as_h1", "Same as H1", same_as_h1,
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description="The title is exactly the same as the page's H1 heading. That is not wrong, but "
                       "it misses a chance to cover a second wording of the topic." + INDEXABLE_NOTE,
           fix="Consider making the title and the H1 a little different, for example a longer "
               "search-focused title and a shorter on-page heading."),
    Filter("multiple", "Multiple", lambda r, site: is_multiple(titles_of(r)),
           issue_type="Issue", priority="High", only_indexable=True,
           description="The page has more than one <title> element. Search engines may pick the wrong "
                       "one, often because a theme and a plugin both add a title." + INDEXABLE_NOTE,
           fix="Keep exactly one <title> in the <head> and remove the extra ones (check the theme, "
               "SEO plugin and any scripts that add a title)."),
    Filter("outside_head", "Outside <head>", lambda r, site: bool(seo(r).get("title_outside_head")),
           issue_type="Issue", priority="High", only_indexable=True,
           description="A <title> sits outside the <head> section. Search engines may ignore it, and "
                       "it can be a sign that the HTML is broken above it." + INDEXABLE_NOTE,
           fix="Move the <title> into the <head>, and check that nothing invalid (such as an <img> or "
               "<div>) before it closes the <head> early."),
]

COLUMNS = [
    ADDRESS, occurrences_column(titles_of),
    *value_columns("title", "Title", titles_of, px_kind="title"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="page_titles", name="Page Titles", order=60, columns=COLUMNS, filters=FILTERS,
             rows=content_rows))
