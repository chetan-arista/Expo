"""
Segments (Configuration > Segments): named, coloured groups of URLs defined by rules over crawl data, used for a
Segment column and filter in every grid, the right-hand Segments panel and the Segments Overview report.

A segment: {"name", "color": "#rrggbb", "match": "all" | "any", "rules": [{"field", "op", "value"}],
            "groups": [{"match", "rules"}]}   (groups: optional nested rule groups, combined with the rules)
Segments are evaluated top to bottom and the FIRST matching segment wins, so a URL is in exactly one segment
or in none ("(No segment)"); counts per segment therefore always add up to the total (numbers reconcile). A
rule "segment is X" asks whether the URL matches X's own rules, so X must be listed above it (Screaming Frog:
"dependencies must be above"). A segment never changes a URL's status category.

Fields (FIELDS): address, path, host, query, content_type, status_code, category (2xx / 3xx / 4xx / 5xx /
blocked / error), indexability (Indexable / Non-Indexable), indexability_status, crawl_depth, title, h1,
meta_description, word_count, response_time (ms), inlinks, unique_inlinks, link_score, structured_data (any
type), in_filter ("tab:filter" of the registry, e.g. "page_titles:missing"), segment (an earlier segment).
Operators (OPERATORS): contains, not_contains, equals, not_equals, starts_with, ends_with, regex, not_regex,
gt, gte, lt, lte, empty, not_empty, in, not_in. Text operators ignore case; regex is Python syntax, also
case-insensitive (as Include / Exclude).

  validate_segments(segments) -> (clean list, {index: {field: error}})
  evaluate(site, index, segments, rows=None) -> {url: segment name or ""}   internal URLs + external links
  overview(site, index, assignment, segments, tab=None, filter_id="all") -> {"rows", "total", "reconciled"}
  issues_by_segment(index, assignment, segments) -> [issue rows with per-segment URL counts]  (report)
  counts_for(index, assignment, tab, filter_id) -> {segment: n}       the grid's Segment filter
  suggest(site, index=None) -> [segment]                               page types + top folders, with counts
  SegmentStore(reports_dir, host): list() / save(segments) -> errors / version()   per-site, segments/<host>.json
"""
import hashlib
import json
import os
import re
import tempfile
import threading
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import urlparse

NO_SEGMENT = "(No segment)"
MAX_SEGMENTS = 50
MAX_RULES = 20
MAX_GROUPS = 5
NAME_LIMIT = 60
PALETTE = ("#2E7D32", "#1565C0", "#C2185B", "#EF6C00", "#6A1B9A", "#00838F", "#AD1457", "#558B2F", "#4527A0",
           "#00695C", "#9E9D24", "#5D4037")
CATEGORIES = ("2xx", "3xx", "4xx", "5xx", "blocked", "error")

# field id -> (label, kind): kind text | number | choice | list | filter | segment
FIELDS: "OrderedDict[str, Tuple[str, str]]" = OrderedDict([
    ("address", ("Address", "text")), ("path", ("Path", "text")), ("host", ("Host", "text")),
    ("query", ("Query string", "text")), ("content_type", ("Content type", "text")),
    ("status_code", ("Status code", "number")), ("category", ("Status class", "choice")),
    ("indexability", ("Indexability", "choice")), ("indexability_status", ("Indexability status", "text")),
    ("crawl_depth", ("Crawl depth", "number")), ("title", ("Title", "text")), ("h1", ("H1", "text")),
    ("meta_description", ("Meta description", "text")), ("word_count", ("Word count", "number")),
    ("response_time", ("Response time (ms)", "number")), ("inlinks", ("Inlinks", "number")),
    ("unique_inlinks", ("Unique inlinks", "number")), ("link_score", ("Link score", "number")),
    ("structured_data", ("Structured data type", "list")), ("in_filter", ("In tab filter", "filter")),
    ("segment", ("Segment", "segment")),
])
OPERATORS: "OrderedDict[str, str]" = OrderedDict([
    ("contains", "contains"), ("not_contains", "does not contain"), ("equals", "equals"),
    ("not_equals", "does not equal"), ("starts_with", "starts with"), ("ends_with", "ends with"),
    ("regex", "matches regex"), ("not_regex", "does not match regex"), ("gt", ">"), ("gte", ">="), ("lt", "<"),
    ("lte", "<="), ("empty", "is empty"), ("not_empty", "is not empty"), ("in", "is in"), ("not_in", "is not in"),
])
_TEXT_OPS = ("contains", "not_contains", "equals", "not_equals", "starts_with", "ends_with", "regex", "not_regex",
             "empty", "not_empty")
OPS_BY_KIND = {
    "text": _TEXT_OPS, "list": _TEXT_OPS,
    "number": ("equals", "not_equals", "gt", "gte", "lt", "lte", "empty", "not_empty"),
    "choice": ("equals", "not_equals"),
    "filter": ("in", "not_in"), "segment": ("in", "not_in"),
}
_NO_VALUE = ("empty", "not_empty")
_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_FILTER_KEY = re.compile(r"^[a-z0-9_]{1,60}:[A-Za-z0-9_.-]{1,80}$")
_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def clean_name(text: Any) -> str:
    return " ".join(str(text or "").split())[:NAME_LIMIT]


def _check_rule(rule: Any, above: Set[str], e: Dict[str, str], where: str) -> Optional[dict]:
    if not isinstance(rule, dict):
        e[where] = "A rule needs a field, an operator and a value."
        return None
    field, op = str(rule.get("field") or "").strip(), str(rule.get("op") or "").strip()
    value = rule.get("value")
    if field not in FIELDS:
        e[where] = f"Unknown field: {field or '(none)'}."
        return None
    kind = FIELDS[field][1]
    if op not in OPS_BY_KIND[kind]:
        e[where] = f"'{OPERATORS.get(op, op)}' cannot be used with {FIELDS[field][0]}."
        return None
    if op in _NO_VALUE:
        return {"field": field, "op": op, "value": ""}
    text = "" if value is None else str(value).strip()
    if not text:
        e[where] = f"Enter a value for {FIELDS[field][0]}."
        return None
    if op in ("regex", "not_regex"):
        try:
            re.compile(text, re.I)
        except re.error as ex:
            e[where] = f"Invalid regex: {ex}."
            return None
    if kind == "number":
        try:
            float(text)
        except ValueError:
            e[where] = f"{FIELDS[field][0]} needs a number."
            return None
    if field == "category" and text.lower() not in CATEGORIES:
        e[where] = "Status class is one of " + ", ".join(CATEGORIES) + "."
        return None
    if field == "indexability" and text.lower() not in ("indexable", "non-indexable"):
        e[where] = "Indexability is Indexable or Non-Indexable."
        return None
    if kind == "filter" and not _FILTER_KEY.match(text):
        e[where] = "Choose a tab filter (tab:filter)."
        return None
    if kind == "segment" and clean_name(text) not in above:
        e[where] = f"The segment '{text}' must be listed above this one."
        return None
    return {"field": field, "op": op, "value": clean_name(text) if kind == "segment" else text[:2000]}


def validate_segments(segments: Any) -> Tuple[List[dict], Dict[int, Dict[str, str]]]:
    """(clean segments, {position (1-based): {"name" | "rule 2" | "group 1 rule 1": error}})."""
    errors: Dict[int, Dict[str, str]] = {}
    out: List[dict] = []
    if not isinstance(segments, list):
        return [], {0: {"segments": "Segments must be a list."}}
    if len(segments) > MAX_SEGMENTS:
        errors[0] = {"segments": f"At most {MAX_SEGMENTS} segments."}
    seen: Set[str] = set()
    for pos, seg in enumerate(segments[:MAX_SEGMENTS], 1):
        e: Dict[str, str] = {}
        if not isinstance(seg, dict):
            errors[pos] = {"segment": "Not a segment."}
            continue
        name = clean_name(seg.get("name"))
        if not name:
            e["name"] = "Enter a name."
        elif name.lower() == NO_SEGMENT.lower() or name.lower() in {s.lower() for s in seen}:
            e["name"] = f"The name '{name}' is already used."
        color = str(seg.get("color") or "").strip()
        if not _COLOR.match(color):
            color = PALETTE[(pos - 1) % len(PALETTE)]
        match = "any" if str(seg.get("match") or "all").lower() == "any" else "all"
        rules = []
        for i, rule in enumerate((seg.get("rules") or [])[:MAX_RULES], 1):
            r = _check_rule(rule, seen, e, f"rule {i}")
            if r:
                rules.append(r)
        groups = []
        for gi, g in enumerate((seg.get("groups") or [])[:MAX_GROUPS], 1):
            if not isinstance(g, dict):
                continue
            g_rules = []
            for i, rule in enumerate((g.get("rules") or [])[:MAX_RULES], 1):
                r = _check_rule(rule, seen, e, f"group {gi} rule {i}")
                if r:
                    g_rules.append(r)
            if g_rules:
                groups.append({"match": "any" if str(g.get("match") or "all").lower() == "any" else "all",
                               "rules": g_rules})
        if not rules and not groups and not e:
            e["rules"] = "Add at least one rule."
        if e:
            errors[pos] = e
        if name:
            seen.add(name)
            out.append({"name": name, "color": color, "match": match, "rules": rules, "groups": groups})
    return out, errors


def segments_version(segments: Any) -> str:
    """Short hash of a segment list (memo keys)."""
    return hashlib.md5(json.dumps(segments or [], sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------------------------
def _first(values: Any) -> str:
    try:
        for v in values or ():
            if str(v or "").strip():
                return str(v)
    except TypeError:
        pass
    return ""


def _category(r) -> str:
    from .issues import classify
    return classify(getattr(r, "status_code", None), getattr(r, "redirect_destination", None),
                    getattr(r, "blocked", False))


class _Ctx:
    """Field values of one URL, read on first use."""

    __slots__ = ("r", "site", "url", "_cache")

    def __init__(self, r, site):
        self.r, self.site, self.url, self._cache = r, site, str(getattr(r, "url", "") or ""), {}

    def get(self, field: str) -> Any:
        if field in self._cache:
            return self._cache[field]
        v = self._read(field)
        self._cache[field] = v
        return v

    def _read(self, field: str) -> Any:
        r, site, url = self.r, self.site, self.url
        seo = getattr(r, "seo", None) or {}
        if field == "address":
            return url
        if field in ("path", "host", "query"):
            try:
                p = urlparse(url)
            except ValueError:
                return ""
            return {"path": p.path or "/", "host": p.netloc, "query": p.query}[field]
        if field == "content_type":
            return str(getattr(r, "content_type", "") or "")
        if field == "status_code":
            return getattr(r, "status_code", None)
        if field == "category":
            return _category(r)
        if field == "indexability":
            idx = getattr(site, "indexable", None) or {}
            return "Indexable" if idx.get(url) else "Non-Indexable"
        if field == "indexability_status":
            return str((getattr(site, "indexability_status", None) or {}).get(url, "") or "")
        if field == "crawl_depth":
            d = (getattr(site, "crawl_depth", None) or {}).get(url)
            return d if d is not None else getattr(r, "depth", None)
        if field == "title":
            return _first(seo.get("titles")) or str(getattr(r, "page_title", "") or "")
        if field == "h1":
            return _first(seo.get("h1"))
        if field == "meta_description":
            return _first(seo.get("meta_descriptions"))
        if field == "word_count":
            return seo.get("word_count") if seo else None
        if field == "response_time":
            return getattr(r, "response_time_ms", None)
        if field == "inlinks":
            return len((getattr(site, "inlinks", None) or {}).get(url) or ())
        if field == "unique_inlinks":
            return int((getattr(site, "unique_inlinks", None) or {}).get(url) or 0)
        if field == "link_score":
            return (getattr(site, "link_score", None) or {}).get(url)
        if field == "structured_data":
            sd = seo.get("structured_data") if isinstance(seo.get("structured_data"), dict) else {}
            return [str(t) for t in sd.get("types") or [] if t]
        return None


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _test_text(op: str, value: Any, want: str, rx) -> bool:
    if op == "empty":
        return value in (None, "", [])
    if op == "not_empty":
        return value not in (None, "", [])
    items = value if isinstance(value, list) else [value]
    texts = ["" if v is None else str(v) for v in items] or [""]
    w = want.lower()
    if op == "contains":
        return any(w in t.lower() for t in texts)
    if op == "not_contains":
        return not any(w in t.lower() for t in texts)
    if op == "equals":
        return any(t.lower() == w for t in texts)
    if op == "not_equals":
        return not any(t.lower() == w for t in texts)
    if op == "starts_with":
        return any(t.lower().startswith(w) for t in texts)
    if op == "ends_with":
        return any(t.lower().endswith(w) for t in texts)
    if op == "regex":
        return any(rx.search(t) for t in texts)
    if op == "not_regex":
        return not any(rx.search(t) for t in texts)
    return False


def _test_number(op: str, value: Any, want: Optional[float]) -> bool:
    if op == "empty":
        return value is None or value == ""
    if op == "not_empty":
        return not (value is None or value == "")
    n = _num(value)
    if n is None or want is None:
        return False
    return {"equals": n == want, "not_equals": n != want, "gt": n > want, "gte": n >= want, "lt": n < want,
            "lte": n <= want}.get(op, False)


class _Compiled:
    """A validated segment ready to test URLs."""

    def __init__(self, seg: dict):
        self.name, self.color = seg["name"], seg["color"]
        self.match_any = seg.get("match") == "any"
        self.rules = [self._compile(r) for r in seg.get("rules") or []]
        self.groups = [("any" == g.get("match"), [self._compile(r) for r in g.get("rules") or []])
                       for g in seg.get("groups") or []]

    @staticmethod
    def _compile(rule: dict) -> Tuple[str, str, Any, Any]:
        field, op, value = rule["field"], rule["op"], rule.get("value") or ""
        rx = re.compile(value, re.I) if op in ("regex", "not_regex") else None
        num = _num(value) if FIELDS[field][1] == "number" else None
        return field, op, value, (rx if rx is not None else num)

    @staticmethod
    def _rule(rule, ctx: _Ctx, filters: "_FilterSets", matched: Set[str]) -> bool:
        field, op, value, extra = rule
        kind = FIELDS[field][1]
        if kind == "filter":
            inside = ctx.url in filters.get(value)
            return inside if op == "in" else not inside
        if kind == "segment":
            inside = value in matched
            return inside if op == "in" else not inside
        v = ctx.get(field)
        if kind == "number":
            return _test_number(op, v, extra)
        return _test_text(op, v, value, extra)

    def test(self, ctx: _Ctx, filters: "_FilterSets", matched: Set[str]) -> bool:
        parts = [self._rule(r, ctx, filters, matched) for r in self.rules]
        for any_, rules in self.groups:
            res = [self._rule(r, ctx, filters, matched) for r in rules]
            parts.append(any(res) if any_ else all(res))
        if not parts:
            return False
        return any(parts) if self.match_any else all(parts)


class _FilterSets:
    """URLs of a registry tab filter ("tab:filter", "tab:all" = the tab's rows), built when first asked."""

    def __init__(self, index):
        self.index = index
        self._sets: Dict[str, Set[str]] = {}

    def get(self, key: str) -> Set[str]:
        hit = self._sets.get(key)
        if hit is None:
            hit = set()
            tab, _, fid = key.partition(":")
            tr = (getattr(self.index, "tabs", None) or {}).get(tab)
            if tr is not None:
                rows = tr.rows if fid == "all" else tr.filter_rows.get(fid, [])
                hit = {str(getattr(r, "url", "") or "") for r in rows or ()}
            self._sets[key] = hit
        return hit


def _all_rows(site, rows: Optional[Iterable[Any]]) -> List[Any]:
    if rows is not None:
        return list(rows)
    out, seen = [], set()
    for r in list((getattr(site, "by_url", None) or {}).values()) + list(getattr(site, "externals", None) or []):
        u = getattr(r, "url", None)
        if u and u not in seen:
            seen.add(u)
            out.append(r)
    return out


def evaluate(site, index, segments: Any, rows: Optional[Iterable[Any]] = None) -> Dict[str, str]:
    """{url: segment name, "" for none} for every internal URL and external link (or the given rows). A segment
    with an invalid rule is left out whole (never applied with part of its rules; validate_segments says why)."""
    clean, errors = validate_segments(segments or [])
    bad = set()
    pos = 0
    for i, seg in enumerate(segments or [], 1):
        if isinstance(seg, dict) and clean_name(seg.get("name")):
            if i in errors:
                bad.add(pos)
            pos += 1
    compiled = [_Compiled(s) for j, s in enumerate(clean) if j not in bad]
    filters = _FilterSets(index)
    out: Dict[str, str] = {}
    for r in _all_rows(site, rows):
        ctx = _Ctx(r, site)
        if not ctx.url or ctx.url in out:
            continue
        matched: Set[str] = set()
        assigned = ""
        for seg in compiled:
            if seg.test(ctx, filters, matched):
                matched.add(seg.name)
                if not assigned:
                    assigned = seg.name
        out[ctx.url] = assigned
    return out


# ---------------------------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------------------------
def _names_colors(segments: Any) -> List[Tuple[str, str]]:
    clean, _e = validate_segments(segments or [])
    return [(s["name"], s["color"]) for s in clean]


def _issue_hits(index) -> List[Tuple[dict, Set[str]]]:
    out = []
    for issue in getattr(index, "issues", None) or []:
        tr = (getattr(index, "tabs", None) or {}).get(issue.get("tab"))
        hits = tr.filter_rows.get(issue.get("filter"), []) if tr is not None else []
        out.append((issue, {str(getattr(h, "url", "") or "") for h in hits} - {""}))
    return out


def overview(site, index, assignment: Dict[str, str], segments: Any, tab: Optional[str] = None,
             filter_id: str = "all") -> Dict[str, Any]:
    """Segments panel / Segments Overview: one row per segment (in order) plus "(No segment)" with URLs,
    % of total, indexable / non-indexable, URLs per status class, issue URLs and issues by priority (URL x
    issue pairs), average crawl depth and response time. Covers the internal URLs, or the URLs of one
    tab:filter. The URL counts add up to "total" ("reconciled")."""
    if tab:
        tr = (getattr(index, "tabs", None) or {}).get(tab)
        rows = (tr.rows if filter_id in ("", "all") else tr.filter_rows.get(filter_id, [])) if tr is not None else []
        urls = list(OrderedDict.fromkeys(str(getattr(r, "url", "") or "") for r in rows if getattr(r, "url", "")))
    else:
        urls = list((getattr(site, "by_url", None) or {}).keys())
    by_url = getattr(site, "by_url", None) or {}
    ext = {getattr(e, "url", ""): e for e in getattr(site, "externals", None) or []}
    issue_hits = _issue_hits(index)
    order = _names_colors(segments)
    stats: "OrderedDict[str, dict]" = OrderedDict()
    for name, color in order + [(NO_SEGMENT, "#9E9E9E")]:
        stats[name] = {"name": name, "color": color, "urls": 0, "indexable": 0, "non_indexable": 0,
                       "categories": {c: 0 for c in CATEGORIES}, "issue_urls": 0,
                       "issues_by_priority": {"High": 0, "Medium": 0, "Low": 0}, "_depth": [], "_rt": []}
    indexable = getattr(site, "indexable", None) or {}
    depth = getattr(site, "crawl_depth", None) or {}
    for u in urls:
        name = assignment.get(u) or NO_SEGMENT
        row = stats.get(name) or stats[NO_SEGMENT]
        row["urls"] += 1
        r = by_url.get(u) or ext.get(u)
        if u in by_url:
            row["indexable" if indexable.get(u) else "non_indexable"] += 1
            if depth.get(u) is not None:
                row["_depth"].append(depth[u])
        if r is not None:
            row["categories"][_category(r)] += 1
            rt = getattr(r, "response_time_ms", None)
            if rt:
                row["_rt"].append(rt)
        flagged = False
        for issue, hit in issue_hits:
            if u in hit:
                flagged = True
                pr = issue.get("priority") or "Low"
                row["issues_by_priority"][pr] = row["issues_by_priority"].get(pr, 0) + 1
        if flagged:
            row["issue_urls"] += 1
    total = len(urls)
    out_rows = []
    for row in stats.values():           # "(No segment)" is always listed, also at 0
        d, rt = row.pop("_depth"), row.pop("_rt")
        row["avg_depth"] = round(sum(d) / len(d), 2) if d else None
        row["avg_response_ms"] = round(sum(rt) / len(rt)) if rt else None
        row["pct"] = round(row["urls"] / total * 100, 2) if total else 0.0
        out_rows.append(row)
    return {"rows": out_rows, "total": total, "reconciled": sum(r["urls"] for r in out_rows) == total,
            "scope": f"{tab}:{filter_id or 'all'}" if tab else "internal"}


def issues_by_segment(index, assignment: Dict[str, str], segments: Any) -> Dict[str, Any]:
    """Reports > Segments Overview: one row per issue with the URLs of it in each segment ("segments": {name: n},
    "(No segment)" included); each row's segment counts add up to its "urls" (unique URLs)."""
    names = [n for n, _c in _names_colors(segments)] + [NO_SEGMENT]
    rows = []
    for issue, hit in _issue_hits(index):
        per = Counter(assignment.get(u) or NO_SEGMENT for u in hit)
        rows.append({"tab": issue.get("tab"), "filter": issue.get("filter"), "name": issue.get("name"),
                     "issue_type": issue.get("issue_type"), "priority": issue.get("priority"), "urls": len(hit),
                     "segments": {n: per.get(n, 0) for n in names}})
    return {"segments": names, "rows": rows}


def counts_for(index, assignment: Dict[str, str], tab: str, filter_id: str = "all") -> Dict[str, int]:
    """{segment: URLs} among the URLs of one tab filter (the grid toolbar's Segment filter)."""
    tr = (getattr(index, "tabs", None) or {}).get(tab)
    if tr is None:
        return {}
    rows = tr.rows if filter_id in ("", "all") else tr.filter_rows.get(filter_id, [])
    urls = {str(getattr(r, "url", "") or "") for r in rows or ()} - {""}
    return dict(Counter(assignment.get(u) or NO_SEGMENT for u in urls))


# ---------------------------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------------------------
PAGE_TYPES = (
    ("Home page", "any", [{"field": "path", "op": "equals", "value": "/"}]),
    ("Product pages", "any", [{"field": "structured_data", "op": "regex", "value": r"^(Product|ProductGroup|Offer)$"},
                              {"field": "path", "op": "regex",
                               "value": r"/(products?|shop|store|items?)/[^/]+"}]),
    ("Blog & articles", "any", [{"field": "structured_data", "op": "regex",
                                 "value": r"^(Article|BlogPosting|NewsArticle|TechArticle)$"},
                                {"field": "path", "op": "regex",
                                 "value": r"/(blog|blogs|news|articles?|posts?|insights|stories|press)/[^/]+"}]),
    ("Category & tag pages", "any", [{"field": "path", "op": "regex",
                                      "value": r"/(category|categories|collections?|tags?|topics?)/"}]),
    ("Broken URLs (4xx/5xx)", "any", [{"field": "status_code", "op": "gte", "value": "400"}]),
)


def _folder(url: str) -> str:
    try:
        segs = [s for s in (urlparse(url).path or "/").split("/") if s]
    except ValueError:
        segs = []
    return f"/{segs[0]}/" if segs else "/"


def suggest(site, index=None, max_folders: int = 8, min_urls: int = 2) -> List[dict]:
    """Ready-made segments for this crawl: page types that match at least one URL, then the biggest top
    folders. Each carries "count" (URLs it gets when the list is used as is, first match wins)."""
    pages = [r for r in (getattr(site, "by_url", None) or {}).values()]
    suggestions: List[dict] = []
    for name, match, rules in PAGE_TYPES:
        suggestions.append({"name": name, "match": match, "rules": rules, "groups": []})
    folders = Counter(_folder(getattr(r, "url", "")) for r in pages if (getattr(r, "seo", None) or {}).get("is_html"))
    folders.pop("/", None)
    for folder, n in folders.most_common(max_folders):
        if n < min_urls:
            break
        suggestions.append({"name": f"Folder {folder}", "match": "all",
                            "rules": [{"field": "path", "op": "starts_with", "value": folder}], "groups": []})
    for i, s in enumerate(suggestions):
        s["color"] = PALETTE[i % len(PALETTE)]
    assignment = evaluate(site, index, suggestions, rows=pages)
    counts = Counter(v for v in assignment.values() if v)
    out = [dict(s, count=counts.get(s["name"], 0)) for s in suggestions if counts.get(s["name"], 0)]
    for i, s in enumerate(out):
        s["color"] = PALETTE[i % len(PALETTE)]
    return out


# ---------------------------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------------------------
def _host_key(url_or_host: str) -> str:
    try:
        from .seo.compare import host_key
        return host_key(url_or_host)
    except Exception:
        return re.sub(r"[^a-z0-9.-]+", "_", str(url_or_host or "").lower())[:200] or "site"


def _file_lock(path: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


class SegmentStore:
    """The segments of one site, persisted as reports_dir/segments/<host>.json. Thread-safe."""

    def __init__(self, reports_dir: Optional[str] = None, host: str = ""):
        root = reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()
        self.host = _host_key(host) if host else "_all"
        self.path = os.path.join(root, "segments", f"{self.host}.json")
        self._lock = _file_lock(self.path)

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def list(self) -> List[dict]:
        with self._lock:
            clean, _e = validate_segments(self._load().get("segments") or [])
            return clean

    def updated_at(self) -> str:
        return str(self._load().get("updated_at") or "")

    def version(self) -> str:
        return segments_version(self.list())

    def save(self, segments: Any) -> Dict[int, Dict[str, str]]:
        """Validates and stores the whole list (in order). Returns {} when saved, else the errors (nothing
        saved)."""
        clean, errors = validate_segments(segments)
        if errors:
            return errors
        with self._lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"host": self.host, "segments": clean,
                           "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")},
                          fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        return {}

    def clear(self) -> None:
        with self._lock:
            try:
                os.remove(self.path)
            except OSError:
                pass


def store_for(reports_dir: Optional[str], start_url: str) -> SegmentStore:
    return SegmentStore(reports_dir, start_url)


def field_options() -> Dict[str, Any]:
    """For the editor: fields with their kind and allowed operators, operator labels, choices."""
    return {"fields": [{"id": f, "label": label, "kind": kind, "ops": list(OPS_BY_KIND[kind])}
                       for f, (label, kind) in FIELDS.items()],
            "operators": [{"id": k, "label": v} for k, v in OPERATORS.items()],
            "choices": {"category": list(CATEGORIES), "indexability": ["Indexable", "Non-Indexable"]},
            "palette": list(PALETTE), "no_segment": NO_SEGMENT}
