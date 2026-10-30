"""
Custom JavaScript tab (Screaming Frog "Custom JavaScript"): the value each snippet set under
Configuration > Custom JavaScript (CrawlConfig.custom_js, up to 10; type "extraction" returns a value, type
"action" only acts on the page, e.g. clicks "load more") returned on every rendered page.

Snippets run in Chrome after the page loaded (crawler/render_capture.py), so the tab needs JavaScript
rendering (Configuration > Rendering); without it, or with no snippet set, it shows only "All" and says why.
Values are stored per page (rendered["custom_js"] {snippet name: JSON value}; a snippet that threw an error
is listed in rendered["custom_js_errors"] and shows "Error: ..." in its column). Each extraction snippet has a
filter listing the pages where it returned something (not empty, null or false).
"""
import json
from typing import Any, Dict, List, Tuple

from ...config import MAX_CUSTOM_RULES
from ..registry import Column, Filter, Tab, register, rendered, seo
from .internal import ADDRESS, CONTENT_TYPE, STATUS, STATUS_CODE, memo
from .page_titles import content_rows


def snippets(site) -> List[Dict[str, str]]:
    """[{"name", "type"}] of the extraction snippets, named the way the renderer names them."""
    def build():
        cfg = getattr(site, "config", None)
        raw = [s for s in (getattr(cfg, "custom_js", None) or []) if isinstance(s, dict)
               and str(s.get("code") or "").strip()][:MAX_CUSTOM_RULES] if cfg is not None else []
        out = []
        for i, s in enumerate(raw):
            if str(s.get("type") or "extraction").lower() == "action":
                continue
            out.append({"name": str(s.get("name") or f"Snippet {i + 1}")[:80], "type": "extraction"})
        return out
    return memo(site, "custom_js_snippets", build)


def results(r) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """({name: value}, {name: error}) for a rendered page (({}, {}) otherwise)."""
    rd = rendered(r)
    if rd is None:
        return {}, {}
    vals = rd.get("custom_js")
    if not isinstance(vals, dict) or not vals:
        vals = seo(r).get("custom_js") if isinstance(seo(r).get("custom_js"), dict) else {}
    errs = rd.get("custom_js_errors") if isinstance(rd.get("custom_js_errors"), dict) else {}
    return vals or {}, errs or {}


def _has_value(v: Any) -> bool:
    """A returned 0 is a value (a count of none); None, false, "" and an empty list / object are not.
    (0 == False in Python, so `in (None, "", False)` would drop the 0.)"""
    if v is None or v is False:
        return False
    return not (isinstance(v, (str, list, dict)) and not v)


def _text(v: Any) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return str(v)[:2000]


def _returned(i: int):
    def test(r, site) -> bool:
        sn = snippets(site)
        return i < len(sn) and _has_value(results(r)[0].get(sn[i]["name"]))
    return test


def _cell(name: str):
    def get(r, site):
        vals, errs = results(r)
        if name in errs:
            return f"Error: {errs[name]}"[:300]
        return _text(vals[name]) if name in vals and vals[name] is not None else ""
    return get


SLOTS = [Filter(f"snippet_{i + 1}", f"Snippet {i + 1}", _returned(i), requires=("rendering",),
                description="Rendered pages where one of the crawl's Custom JavaScript snippets returned a value.")
         for i in range(MAX_CUSTOM_RULES)]

BASE_COLUMNS = [ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS]


def dynamic(site) -> Tuple[List[Column], List[Filter]]:
    columns = list(BASE_COLUMNS) + [Column("rendered", "Rendered",
                                           lambda r, site: "Yes" if rendered(r) is not None else "No")]
    filters: List[Filter] = []
    for i, s in enumerate(snippets(site)):
        filters.append(Filter(f"snippet_{i + 1}", s["name"], _returned(i), requires=("rendering",),
                              description=f"Rendered pages where the snippet '{s['name']}' returned a value."))
        columns.append(Column(f"snippet_{i + 1}", s["name"], _cell(s["name"])))
    return columns, filters


register(Tab(id="custom_javascript", name="Custom JavaScript", order=218, columns=BASE_COLUMNS, filters=SLOTS,
             rows=content_rows, requires=("config:custom_js",), dynamic=dynamic,
             description="Values your Custom JavaScript snippets returned on each page rendered in Chrome."))
