"""
H1 tab (Screaming Frog "H1"): the <h1> headings of every internal HTML page that loaded (2xx).

Screaming Frog stores the first two h1s (H1-1, H1-2); Over 70 Characters and Duplicate check both,
Occurrences counts them all. An h1 that holds only an image takes the image's alt text as its
text. Every filter runs on indexable pages only (see page_titles.py for the shared rules).
"""
from typing import List

from ..registry import Filter, Tab, pref_number, register, seo, threshold_filter
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import (INDEXABLE_NOTE, any_value, content_rows, duplicate_test, h1_of, is_missing,
                          is_multiple, occurrences_column, text_list, value_columns)

HEADING_MAX_CHARS = 70        # Preferences default (h1_max_chars / h2_max_chars, P14)


def heading_levels(r) -> List[int]:
    """Level of every h1..h6 in document order."""
    out = []
    for h in seo(r).get("headings") or ():
        try:
            out.append(int(h[0] if isinstance(h, (list, tuple)) else h.get("level")))
        except Exception:
            continue
    return out


def h1_non_sequential(r, site) -> bool:
    """The page has an h1, but the first heading on the page is not an h1."""
    levels = heading_levels(r)
    return 1 in levels and levels[0] != 1


def alt_text_in_h1(r, site) -> bool:
    """An <h1> holds an image with alt text, alone or next to text (e.g. a theme's logo inside the h1)."""
    return any(text_list(r, "h1_img_alts")) or any(text_list(r, "h1_alt_only"))


FILTERS = [
    Filter("missing", "Missing", lambda r, site: is_missing(h1_of(r)),
           issue_type="Issue", priority="Medium", only_indexable=True,
           description="The page has no <h1>, or its h1 is empty. The h1 is the main on-page heading; "
                       "it tells visitors and search engines what the page is about." + INDEXABLE_NOTE,
           fix="Add one clear <h1> near the top of the page that names the page's topic."),
    Filter("duplicate", "Duplicate", duplicate_test("h1", h1_of),
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description="The same h1 text is used on more than one indexable page (or twice on the same "
                       "page), which makes it harder to tell the pages apart." + INDEXABLE_NOTE,
           fix="Give each page a heading that describes what is specific to it."),
    threshold_filter("over_70_characters", "h1_max_chars", "Over {n} Characters",
                     lambda r, site: any_value(h1_of(r), lambda v: len(v) > pref_number(site, "h1_max_chars")),
                     issue_type="Opportunity", priority="Low", only_indexable=True,
                     description="An h1 is longer than {n} characters. Long headings are harder to scan and dilute "
                                 "the main topic." + INDEXABLE_NOTE,
                     fix="Shorten the heading to the core topic and move the detail into the text below it."),
    Filter("multiple", "Multiple", lambda r, site: is_multiple(h1_of(r)),
           issue_type="Warning", priority="Medium", only_indexable=True,
           description="The page has more than one <h1>. That is allowed in HTML5, but a single main "
                       "heading gives the clearest structure." + INDEXABLE_NOTE,
           fix="Keep one h1 for the main topic and turn the other h1s into h2s (or remove them if a "
               "theme adds them, for example around the logo)."),
    Filter("alt_text_in_h1", "Alt Text in H1", alt_text_in_h1,
           issue_type="Warning", priority="Low", only_indexable=True,
           description="An h1 contains an image with alt text, on its own or next to the heading text. "
                       "Often a theme wraps the logo in the h1, so every page shares the logo's alt text as "
                       "(part of) its main heading; an image-only h1 is also weaker than a text heading."
                       + INDEXABLE_NOTE,
           fix="Keep the image out of the h1 (move the logo outside it) and use real text for the main "
               "heading; if the image must stay, make sure its alt text is a good heading for the page."),
    Filter("non_sequential", "Non-Sequential", h1_non_sequential,
           issue_type="Warning", priority="Low", only_indexable=True,
           description="The page has an h1, but another heading (h2-h6) comes before it. Headings "
                       "should start with the h1 and step down in order, which helps screen readers "
                       "and search engines follow the page." + INDEXABLE_NOTE,
           fix="Make the h1 the first heading on the page; turn earlier headings (often in the header "
               "or a banner) into plain styled text."),
]

COLUMNS = [
    ADDRESS, occurrences_column(h1_of),
    *value_columns("h1", "H1", h1_of, sep="-"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="h1", name="H1", order=90, columns=COLUMNS, filters=FILTERS, rows=content_rows))
