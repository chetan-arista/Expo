"""
Custom Search tab (Screaming Frog "Custom Search"): the pages that contain, or do not contain, the text or
regex of each rule set under Configuration > Custom Search (CrawlConfig.custom_searches, up to 10 rules).

extract.py runs every rule on each internal HTML page's raw HTML or visible text and stores the number of
matches (PageData custom_search {rule name: occurrences}); for a page rendered in Chrome (render every page) the
rules run on the rendered HTML instead, as in Screaming Frog's JavaScript rendering mode. Here each rule becomes one filter and one column:
  - a "contains" rule lists the pages with at least one match;
  - a "does not contain" rule lists the pages with none (the classic "tracking tag missing" check).
"All" lists the pages the rules ran on, so for each rule its filter and the pages it did not list add up to
"All". With no rule set the tab shows only "All" (empty) and says so.

The filter ids are fixed slots (rule_1 .. rule_10), so links, exports and dismissals keep working; their
names and columns come from the crawl's own rules (Tab.dynamic).
"""
from typing import Any, List, Tuple

from ...config import MAX_CUSTOM_RULES
from ..extract import compile_custom_searches
from ..registry import Column, Filter, Tab, register, seo
from .internal import ADDRESS, CONTENT_TYPE, STATUS, STATUS_CODE, internal_rows, is_html_doc, memo

MODE_LABEL = {"contains": "Contains", "not_contains": "Does Not Contain"}


def rules(site) -> List[Any]:
    """The crawl's custom searches as the extractor compiled them (names, modes), [] when none."""
    def build():
        cfg = getattr(site, "config", None)
        raw = getattr(cfg, "custom_searches", None) if cfg is not None else None
        if raw:
            try:
                return compile_custom_searches(raw)[0]
            except Exception:
                return []
        return []
    return memo(site, "custom_search_rules", build)


def counts(r) -> dict:
    v = seo(r).get("custom_search")
    return v if isinstance(v, dict) else {}


def search_rows(site) -> List[Any]:
    """Internal HTML pages the rules ran on (their PageData holds a count for at least one rule)."""
    names = {rule.name for rule in rules(site)}
    if not names:
        return []
    return [r for r in internal_rows(site) if is_html_doc(r) and names & set(counts(r))]


def _matches(r, rule) -> bool:
    n = counts(r).get(rule.name)
    if n is None:
        return False
    return int(n or 0) > 0 if rule.mode != "not_contains" else int(n or 0) == 0


def _slot_test(i: int):
    def test(r, site) -> bool:
        rs = rules(site)
        return i < len(rs) and _matches(r, rs[i])
    return test


def _describe(rule) -> str:
    what = "regex" if getattr(rule, "regex", None) is not None else "text"
    where = "visible text" if rule.where == "text" else "HTML"
    verb = "contain" if rule.mode != "not_contains" else "do not contain"
    return f"Pages whose {where} {verb} the {what} \"{rule.text}\" (rule '{rule.name}')."


SLOTS = [Filter(f"rule_{i + 1}", f"Custom Search {i + 1}", _slot_test(i),
                description="Pages matching one of the crawl's custom search rules.")
         for i in range(MAX_CUSTOM_RULES)]

BASE_COLUMNS = [ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS]


def dynamic(site) -> Tuple[List[Column], List[Filter]]:
    columns = list(BASE_COLUMNS)
    filters: List[Filter] = []
    for i, rule in enumerate(rules(site)[:MAX_CUSTOM_RULES]):
        label = f"{rule.name}: {MODE_LABEL.get(rule.mode, 'Contains')}"
        filters.append(Filter(f"rule_{i + 1}", label, _slot_test(i), description=_describe(rule)))
        columns.append(Column(f"rule_{i + 1}", f"{rule.name} (Matches)",
                              lambda r, site, name=rule.name: counts(r).get(name, ""), "num"))
    return columns, filters


register(Tab(id="custom_search", name="Custom Search", order=214, columns=BASE_COLUMNS, filters=SLOTS,
             rows=search_rows, requires=("config:custom_searches",), dynamic=dynamic,
             description="Pages that contain (or lack) the text or regex of each Custom Search rule."))
