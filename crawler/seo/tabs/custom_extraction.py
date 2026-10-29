"""
Custom Extraction tab (Screaming Frog "Custom Extraction"): the values each extractor set under
Configuration > Custom Extraction (CrawlConfig.custom_extractors: XPath, CSS selector or regex, up to 10)
pulled out of every internal HTML page.

extract.py stores the values per page (PageData custom_extraction {extractor name: [values]}, at most 20 of
2,000 characters each); for a page rendered in Chrome (render every page) they come from the rendered HTML, as
in Screaming Frog's JavaScript rendering mode (extract.custom_rules_on_html). The grid shows them as Screaming Frog does: columns "<name> 1", "<name> 2"... one per
value found (as many as the page with the most values). Each extractor also has a filter listing the pages
where it found at least one value. With no extractor set the tab shows only "All" (empty) and says so.
"""
from typing import Any, List, Tuple

from ...config import MAX_CUSTOM_RULES
from ..extract import compile_custom_extractors
from ..registry import Column, Filter, Tab, register, seo
from .internal import ADDRESS, STATUS, STATUS_CODE, internal_rows, is_html_doc, memo

MAX_VALUE_COLUMNS = 20


def extractors(site) -> List[Any]:
    """The crawl's extractors as the extractor compiled them (names), [] when none."""
    def build():
        cfg = getattr(site, "config", None)
        raw = getattr(cfg, "custom_extractors", None) if cfg is not None else None
        if raw:
            try:
                return compile_custom_extractors(raw)[0]
            except Exception:
                return []
        return []
    return memo(site, "custom_extractors", build)


def values(r) -> dict:
    v = seo(r).get("custom_extraction")
    return v if isinstance(v, dict) else {}


def extraction_rows(site) -> List[Any]:
    """Internal HTML pages the extractors ran on."""
    names = {e.name for e in extractors(site)}
    if not names:
        return []
    return [r for r in internal_rows(site) if is_html_doc(r) and names & set(values(r))]


def _found(i: int):
    def test(r, site) -> bool:
        es = extractors(site)
        return i < len(es) and bool(values(r).get(es[i].name))
    return test


SLOTS = [Filter(f"extractor_{i + 1}", f"Extractor {i + 1}", _found(i),
                description="Pages where one of the crawl's extractors found a value.")
         for i in range(MAX_CUSTOM_RULES)]

BASE_COLUMNS = [ADDRESS, STATUS_CODE, STATUS]


def _nth(name: str, n: int):
    def get(r, site):
        vals = values(r).get(name) or []
        return vals[n] if n < len(vals) else ""
    return get


def dynamic(site) -> Tuple[List[Column], List[Filter]]:
    columns = list(BASE_COLUMNS)
    filters: List[Filter] = []
    rows = extraction_rows(site)
    for i, e in enumerate(extractors(site)[:MAX_CUSTOM_RULES]):
        kind = {"xpath": "XPath", "css": "CSS selector", "regex": "regex"}.get(e.kind, e.kind)
        filters.append(Filter(f"extractor_{i + 1}", e.name, _found(i),
                              description=f"Pages where the {kind} extractor '{e.name}' found at least one value."))
        most = max((len(values(r).get(e.name) or []) for r in rows), default=0)
        for n in range(max(1, min(most, MAX_VALUE_COLUMNS))):
            columns.append(Column(f"extractor_{i + 1}_{n + 1}", f"{e.name} {n + 1}", _nth(e.name, n)))
    return columns, filters


register(Tab(id="custom_extraction", name="Custom Extraction", order=216, columns=BASE_COLUMNS, filters=SLOTS,
             rows=extraction_rows, requires=("config:custom_extractors",), dynamic=dynamic,
             description="Values pulled from each page by your Custom Extraction rules (XPath, CSS or regex)."))
