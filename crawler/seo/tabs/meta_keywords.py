"""
Meta Keywords tab (Screaming Frog "Meta Keywords"): the <meta name="keywords"> of every internal
HTML page that loaded (2xx).

Google and Bing ignore meta keywords for ranking, so, as in Screaming Frog, none of these filters
is an issue: they are listed for information only (issue_type None) and never reach the Issues
overview. Filters run on indexable pages only, like the other on-page tabs.
"""
from typing import List

from ..registry import Filter, Tab, register
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import (INDEXABLE_NOTE, content_rows, duplicate_test, is_missing, is_multiple,
                          occurrences_column, text_list, value_columns)

IGNORED_NOTE = (" Google does not use meta keywords, so this is for information only and is not "
                "counted as an issue.")


def keywords_of(r) -> List[str]:
    return text_list(r, "meta_keywords")


FILTERS = [
    Filter("missing", "Missing", lambda r, site: is_missing(keywords_of(r)), only_indexable=True,
           description="The page has no meta keywords tag (or it is empty). This is normal and needs no "
                       "action." + IGNORED_NOTE + INDEXABLE_NOTE,
           fix="Nothing to do. Only add meta keywords if another search engine or internal site search "
               "you rely on reads them."),
    Filter("duplicate", "Duplicate", duplicate_test("meta_keywords", keywords_of), only_indexable=True,
           description="The same meta keywords are used on more than one indexable page, which usually "
                       "means a site-wide default." + IGNORED_NOTE + INDEXABLE_NOTE,
           fix="No SEO fix needed. If you keep the tag, consider removing the site-wide default; it can "
               "also reveal your target terms to competitors."),
    Filter("multiple", "Multiple", lambda r, site: is_multiple(keywords_of(r)), only_indexable=True,
           description="The page has more than one meta keywords tag." + IGNORED_NOTE + INDEXABLE_NOTE,
           fix="Tidy-up only: keep one tag or remove them all."),
]

COLUMNS = [
    ADDRESS, occurrences_column(keywords_of),
    *value_columns("meta_keywords", "Meta Keywords", keywords_of),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="meta_keywords", name="Meta Keywords", order=80, columns=COLUMNS, filters=FILTERS,
             rows=content_rows))
