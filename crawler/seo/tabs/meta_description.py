"""
Meta Description tab (Screaming Frog "Meta Description"): the <meta name="description"> of every
internal HTML page that loaded (2xx), with its length and approximate width in Google's results.

As in Screaming Frog, the length and pixel filters check both stored descriptions of a page, so a
page with a good first tag and an over-long second tag is still listed under "Over 155 Characters".
Every filter runs on indexable pages only (see page_titles.py for the shared rules).
"""
from typing import List

from ..registry import Filter, Tab, pref_number, register, seo, threshold_filter
from ..text_metrics import pixel_width
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import (INDEXABLE_NOTE, any_value, content_rows, duplicate_test, is_missing, is_multiple,
                          occurrences_column, text_list, value_columns)

# Preferences defaults (the filters read the crawl's numbers, P14)
DESCRIPTION_MAX_CHARS = 155
DESCRIPTION_MIN_CHARS = 70
DESCRIPTION_MAX_PX = 985
DESCRIPTION_MIN_PX = 400


def descriptions_of(r) -> List[str]:
    return text_list(r, "meta_descriptions")


def _px(v: str) -> int:
    return pixel_width(v, "description")


FILTERS = [
    Filter("missing", "Missing", lambda r, site: is_missing(descriptions_of(r)),
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description="The page has no meta description, or it is empty. Google will then build the "
                       "snippet under the title from text on the page, which may not sell the page "
                       "well." + INDEXABLE_NOTE,
           fix="Write a meta description for the page: one or two sentences that sum up what the "
               "visitor gets and why they should click."),
    Filter("duplicate", "Duplicate", duplicate_test("meta_description", descriptions_of),
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description="The same meta description is used on more than one indexable page, so the "
                       "pages look alike in search results. Placeholder text copied from a template "
                       "(for example 'Lorem ipsum') often causes this." + INDEXABLE_NOTE,
           fix="Write a unique description for each page that reflects its own content."),
    threshold_filter("over_155_characters", "desc_max_chars", "Over {n} Characters",
                     lambda r, site: any_value(descriptions_of(r), lambda v: len(v) > pref_number(site, "desc_max_chars")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="A meta description is longer than {n} characters, so Google will likely cut it "
                                 "short in the results. Both description tags of a page are checked." + INDEXABLE_NOTE,
                     fix="Shorten the description and put the key message and call to action first."),
    threshold_filter("below_70_characters", "desc_min_chars", "Below {n} Characters",
                     lambda r, site: any_value(descriptions_of(r), lambda v: len(v) < pref_number(site, "desc_min_chars")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="A meta description is shorter than {n} characters, leaving most of the snippet "
                                 "space unused." + INDEXABLE_NOTE,
                     fix="Expand the description with what the page offers and a reason to click."),
    threshold_filter("over_985_pixels", "desc_max_px", "Over {n} Pixels",
                     lambda r, site: any_value(descriptions_of(r), lambda v: _px(v) > pref_number(site, "desc_max_px")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="A meta description is wider than about {n} pixels, the room Google gives a "
                                 "desktop snippet, so the end will probably be cut off." + INDEXABLE_NOTE,
                     fix="Trim the description so the important part fits; keep the main point near the start."),
    threshold_filter("below_400_pixels", "desc_min_px", "Below {n} Pixels",
                     lambda r, site: any_value(descriptions_of(r), lambda v: _px(v) < pref_number(site, "desc_min_px")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="A meta description fills less than {n} pixels of the snippet, so it says very "
                                 "little in the results." + INDEXABLE_NOTE,
                     fix="Write a fuller description that explains the page and invites the click."),
    Filter("multiple", "Multiple", lambda r, site: is_multiple(descriptions_of(r)),
           issue_type="Issue", priority="Medium", only_indexable=True,
           description="The page has more than one meta description tag, often because the theme and "
                       "an SEO plugin each add one. Search engines may use the wrong one." + INDEXABLE_NOTE,
           fix="Keep a single meta description in the <head> and remove the duplicate source (theme "
               "setting, plugin or hard-coded tag)."),
    Filter("outside_head", "Outside <head>",
           lambda r, site: bool(seo(r).get("meta_description_outside_head")),
           issue_type="Issue", priority="Medium", only_indexable=True,
           description="A meta description sits outside the <head> section, where search engines "
                       "may ignore it. Invalid HTML earlier in the head can push it out." + INDEXABLE_NOTE,
           fix="Move the meta description into the <head> and remove anything invalid (such as an "
               "<img> or <div>) that closes the <head> early."),
]

COLUMNS = [
    ADDRESS, occurrences_column(descriptions_of),
    *value_columns("meta_description", "Meta Description", descriptions_of, px_kind="description"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="meta_description", name="Meta Description", order=70, columns=COLUMNS, filters=FILTERS,
             rows=content_rows))
