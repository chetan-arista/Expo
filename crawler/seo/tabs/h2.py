"""
H2 tab (Screaming Frog "H2"): the <h2> headings of every internal HTML page that loaded (2xx).

Screaming Frog stores the first two h2s (H2-1, H2-2); Over 70 Characters and Duplicate check both,
Occurrences counts them all. Every filter runs on indexable pages only (see page_titles.py).

Non-Sequential: the heading right before the page's first h2 is not an h1 (or the h2 is the very
first heading). This reproduces Screaming Frog's count exactly on the reference crawl (47 pages).
"""
from typing import List

from ..registry import Filter, Tab, pref_number, register, threshold_filter
from .h1 import HEADING_MAX_CHARS, heading_levels
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import (INDEXABLE_NOTE, any_value, content_rows, duplicate_test, is_missing, is_multiple,
                          occurrences_column, text_list, value_columns)


def h2_of(r) -> List[str]:
    return text_list(r, "h2")


def h2_non_sequential(r, site) -> bool:
    levels = heading_levels(r)
    if 2 not in levels:
        return False
    i = levels.index(2)
    return i == 0 or levels[i - 1] != 1


FILTERS = [
    Filter("missing", "Missing", lambda r, site: is_missing(h2_of(r)),
           issue_type="Warning", priority="Low", only_indexable=True,
           description="The page has no <h2>, or its h2s are empty. Subheadings break a page into "
                       "sections that are easier to read and to understand." + INDEXABLE_NOTE,
           fix="Add h2 subheadings for the main sections of the content, where the page has enough "
               "text to need them."),
    Filter("duplicate", "Duplicate", duplicate_test("h2", h2_of),
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description="The same h2 text (first or second h2) appears on more than one indexable page, "
                       "or twice on one page. Repeated template headings are the usual cause." + INDEXABLE_NOTE,
           fix="Where the heading introduces the page's own content, make it specific to that page. "
               "Shared template headings (for example 'Contact us') can stay."),
    threshold_filter("over_70_characters", "h2_max_chars", "Over {n} Characters",
                     lambda r, site: any_value(h2_of(r), lambda v: len(v) > pref_number(site, "h2_max_chars")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="The first or second h2 is longer than {n} characters, which is more like a "
                                 "sentence than a heading." + INDEXABLE_NOTE,
                     fix="Shorten the subheading to the point of the section and move the rest into the text."),
    Filter("multiple", "Multiple", lambda r, site: is_multiple(h2_of(r)),
           issue_type="Warning", priority="Low", only_indexable=True,
           description="The page has more than one <h2>. This is normal for longer pages; it is listed "
                       "so you can check the subheadings make sense together." + INDEXABLE_NOTE,
           fix="Usually nothing to do. Check that each h2 starts a real section and that the order "
               "reads like an outline."),
    Filter("non_sequential", "Non-Sequential", h2_non_sequential,
           issue_type="Warning", priority="Low", only_indexable=True,
           description="The first h2 does not follow the h1 directly: another heading level (h3-h6) "
                       "comes between them, or the h2 appears before the h1. The outline then skips a "
                       "level, which confuses screen readers and search engines." + INDEXABLE_NOTE,
           fix="Make the h2 the next heading after the h1, and use h3-h6 only inside h2 sections. "
               "Style smaller headings with CSS instead of picking a lower heading level."),
]

COLUMNS = [
    ADDRESS, occurrences_column(h2_of),
    *value_columns("h2", "H2", h2_of, sep="-"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="h2", name="H2", order=100, columns=COLUMNS, filters=FILTERS, rows=content_rows))
