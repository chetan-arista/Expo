"""
Site structure views (right panel "Site Structure" and "Response Times", the grid's directory-tree view and
the word clouds of Visualisations). Every count is taken from the same internal URLs as the Internal tab
("All" = every internal URL once), and the category of a URL is its HTTP status class, so the numbers add up:
the folder tree's root, the crawl-depth histogram and the response-time buckets each total the URL count.

    site_structure(site, levels=0) -> {"total", "indexable", "status", "tree": [host nodes], "depth": [...],
                                       "top_folders": [...]}
    response_times(site, buckets=None, scope="all") -> {"total", "buckets": [...], "no_response", "blocked", ...}
    urls_in_bucket(site, index, buckets=None, scope="all") -> [urls]
    tree_view(rows, path="") -> {"path", "parent", "folders": [...], "rows": [...], "total"}
    word_cloud(site, url, source="anchors"|"body", limit=100) -> {"words": [{"text", "count"}], ...}
"""
import math
import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

CATEGORY_ORDER = ("5xx", "4xx", "error", "3xx", "blocked", "2xx")    # worst first
CATEGORIES = ("2xx", "3xx", "4xx", "5xx", "error", "blocked")
DEPTH_CAP = 10                      # depths from 10 on share one "10+" bar
TOP_FOLDERS = 20
DEFAULT_BUCKETS = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10)   # seconds: 0-1 s, 1-2 s ... 9-10 s, then 10 s+
NO_RESPONSE = "No response"
WORD_LIMIT = 100
_WORD = re.compile(r"[^\W\d_][\w'’-]*", re.UNICODE)


def _category(r) -> str:
    from ..exporter import row_category
    return row_category(r) or "error"


def _internal(site) -> List[Any]:
    """Every internal URL once, in crawl order (the Internal tab's "All")."""
    return list((getattr(site, "by_url", None) or {}).values()) if site is not None else []


def _worst(counts: Dict[str, int]) -> str:
    for c in CATEGORY_ORDER:
        if counts.get(c):
            return c
    return ""


def folder_of(url: str) -> str:
    """The folder a URL sits in, as an absolute URL ending in "/": https://h/blog/post -> https://h/blog/,
    https://h/blog/ -> https://h/blog/ (a folder's own address is its index page), https://h/a -> https://h/."""
    try:
        p = urlsplit(url or "")
    except ValueError:
        return ""
    if not p.netloc:
        return ""
    path = p.path or "/"
    return f"{p.scheme}://{p.netloc}{path[:path.rfind('/') + 1] or '/'}"


def _host_root(url: str) -> str:
    try:
        p = urlsplit(url or "")
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}/" if p.netloc else ""


def _segments(folder: str) -> Tuple[str, List[str]]:
    """(host root, path segments) of a folder URL."""
    root = _host_root(folder)
    rest = folder[len(root):] if root and folder.startswith(root) else ""
    return root, [s for s in rest.split("/") if s]


# ---------------------------------------------------------------------------------------------
# Site Structure: folder tree + crawl depth
# ---------------------------------------------------------------------------------------------
def _new_node(path: str, name: str) -> dict:
    return {"path": path, "name": name, "urls": 0, "indexable": 0, "own": 0,
            "status": {c: 0 for c in CATEGORIES}, "worst": "", "children": {}}


def _finish(node: dict, level: int, levels: int) -> dict:
    kids = sorted(node["children"].values(), key=lambda n: (-n["urls"], n["path"]))
    node["worst"] = _worst(node["status"])
    node["non_indexable"] = node["urls"] - node["indexable"]
    if levels and level >= levels:
        node["children"] = []
        node["more_folders"] = len(kids)
    else:
        node["children"] = [_finish(k, level + 1, levels) for k in kids]
    return node


def site_structure(site, levels: int = 0) -> dict:
    """Folder tree (one root per host) with, per folder, the URLs inside it and below ("urls"), those
    directly in it ("own"), the indexable ones, the count per status class and the worst class; plus the
    crawl-depth histogram (0 .. 9, "10+") and the biggest folders. levels > 0 cuts the tree below that many
    folder levels (a cut node says how many sub-folders it has in "more_folders")."""
    results = _internal(site)
    indexable = getattr(site, "indexable", None) or {}
    depths = getattr(site, "crawl_depth", None) or {}
    roots: Dict[str, dict] = {}
    status_total = {c: 0 for c in CATEGORIES}
    depth_rows: Dict[str, dict] = {}
    flat: Dict[str, dict] = {}
    n_indexable = 0
    for r in results:
        url = getattr(r, "url", "") or ""
        cat = _category(r)
        ok = bool(indexable.get(url))
        n_indexable += ok
        status_total[cat] = status_total.get(cat, 0) + 1

        d = depths.get(url, getattr(r, "depth", 0))
        try:
            d = int(d or 0)
        except (TypeError, ValueError):
            d = 0
        key = f"{DEPTH_CAP}+" if d >= DEPTH_CAP else str(d)
        row = depth_rows.setdefault(key, {"depth": key, "urls": 0, "indexable": 0})
        row["urls"] += 1
        row["indexable"] += ok

        folder = folder_of(url)
        root_url, segs = _segments(folder)
        if not root_url:
            continue
        node = roots.setdefault(root_url, _new_node(root_url, urlsplit(root_url).netloc))
        chain = [node]
        path = root_url
        for seg in segs:
            path = f"{path}{seg}/"
            node = node["children"].setdefault(path, _new_node(path, seg))
            chain.append(node)
        for n in chain:
            n["urls"] += 1
            n["indexable"] += ok
            n["status"][cat] = n["status"].get(cat, 0) + 1
        chain[-1]["own"] += 1
        for n in chain[1:]:
            flat[n["path"]] = n

    total = len(results)
    order = [str(i) for i in range(DEPTH_CAP)] + [f"{DEPTH_CAP}+"]
    depth = []
    for key in order:
        row = depth_rows.get(key)
        if row is None:
            continue
        row["non_indexable"] = row["urls"] - row["indexable"]
        row["pct"] = round(row["urls"] / total * 100, 2) if total else 0.0
        depth.append(row)
    top = sorted(flat.values(), key=lambda n: (-n["urls"], n["path"]))[:TOP_FOLDERS]
    top_folders = [{"path": n["path"], "urls": n["urls"], "indexable": n["indexable"],
                    "non_indexable": n["urls"] - n["indexable"], "worst": _worst(n["status"]),
                    "pct": round(n["urls"] / total * 100, 2) if total else 0.0} for n in top]
    tree = [_finish(n, 0, levels) for n in sorted(roots.values(), key=lambda n: (-n["urls"], n["path"]))]
    return {"total": total, "indexable": n_indexable, "non_indexable": total - n_indexable,
            "status": status_total, "tree": tree, "depth": depth, "top_folders": top_folders}


# ---------------------------------------------------------------------------------------------
# Response Times
# ---------------------------------------------------------------------------------------------
def _scope_rows(site, scope: str) -> List[Any]:
    rows = _internal(site)
    if scope == "html":
        from .tabs.internal import is_html_doc
        rows = [r for r in rows if is_html_doc(r)]
    return rows


def _seconds(r) -> Optional[float]:
    """Response time in seconds, None when the URL got no answer (no response, blocked by robots.txt)."""
    if _category(r) in ("error", "blocked"):
        return None
    ms = getattr(r, "response_time_ms", None)
    try:
        return max(0.0, float(ms or 0) / 1000.0)
    except (TypeError, ValueError):
        return None


def _edges(buckets: Optional[Sequence[float]]) -> List[float]:
    edges = sorted({float(b) for b in (buckets or DEFAULT_BUCKETS) if b is not None and float(b) > 0})
    return edges or [float(b) for b in DEFAULT_BUCKETS]


def _fmt(x: float) -> str:
    return f"{x:g}"


def bucket_labels(buckets: Optional[Sequence[float]] = None) -> List[str]:
    edges = _edges(buckets)
    labels, lo = [], 0.0
    for hi in edges:
        labels.append(f"{_fmt(lo)} - {_fmt(hi)} s")
        lo = hi
    labels.append(f"{_fmt(lo)} s +")
    return labels


def bucket_index(seconds: float, buckets: Optional[Sequence[float]] = None) -> int:
    """Bucket of a time: [lo, hi) per bucket, the last one open-ended."""
    edges = _edges(buckets)
    for i, hi in enumerate(edges):
        if seconds < hi:
            return i
    return len(edges)


def _percentile(sorted_vals: List[float], pct: float) -> Optional[float]:
    if not sorted_vals:
        return None
    k = max(0, min(len(sorted_vals) - 1, math.ceil(pct / 100.0 * len(sorted_vals)) - 1))
    return round(sorted_vals[k], 3)


def response_times(site, buckets: Optional[Sequence[float]] = None, scope: str = "all") -> dict:
    """URLs per response-time bucket. scope "all" = every internal URL (the Internal tab's "All"), "html" =
    internal HTML pages (Internal > HTML). URLs requested without an answer go to the "No response" row (the NO
    RESPONSE tile) and URLs robots.txt kept us from requesting to "blocked" (the BLOCKED tile), so
    sum(buckets) + no_response + blocked == total."""
    rows = _scope_rows(site, scope)
    labels = bucket_labels(buckets)
    counts = [0] * len(labels)
    times: List[float] = []
    no_response = blocked = 0
    for r in rows:
        s = _seconds(r)
        if s is None:
            if _category(r) == "blocked":
                blocked += 1
            else:
                no_response += 1
            continue
        times.append(s)
        counts[bucket_index(s, buckets)] += 1
    total = len(rows)
    edges = _edges(buckets)
    out = []
    for i, label in enumerate(labels):
        out.append({"index": i, "label": label, "min_s": edges[i - 1] if i else 0.0,
                    "max_s": edges[i] if i < len(edges) else None, "urls": counts[i],
                    "pct": round(counts[i] / total * 100, 2) if total else 0.0})
    times.sort()
    return {"scope": scope, "total": total, "answered": len(times), "buckets": out,
            "no_response": no_response, "no_response_label": NO_RESPONSE,
            "no_response_pct": round(no_response / total * 100, 2) if total else 0.0,
            "blocked": blocked, "blocked_label": "Blocked by robots.txt",
            "blocked_pct": round(blocked / total * 100, 2) if total else 0.0,
            "average_s": round(sum(times) / len(times), 3) if times else None,
            "median_s": _percentile(times, 50), "p90_s": _percentile(times, 90),
            "max_s": round(times[-1], 3) if times else None}


def urls_in_bucket(site, index: Optional[int], buckets: Optional[Sequence[float]] = None,
                   scope: str = "all") -> List[str]:
    """The URLs of one bucket (index None = every URL without a response time: the "No response" and
    "blocked" rows), in crawl order."""
    out = []
    for r in _scope_rows(site, scope):
        s = _seconds(r)
        if (s is None and index is None) or (s is not None and index is not None
                                             and bucket_index(s, buckets) == index):
            out.append(getattr(r, "url", ""))
    return out


# ---------------------------------------------------------------------------------------------
# Directory tree view of a grid
# ---------------------------------------------------------------------------------------------
def tree_view(rows: Sequence[dict], path: str = "") -> dict:
    """The grid rows ({"url", ...}) of one folder: its sub-folders with the number of rows inside each
    (recursively) and the rows directly in it (a folder's own URL is listed in that folder, first). path ""
    opens the top: the host root when every row is on one host, else one folder per host.
    {"path", "parent", "folders": [{"path", "name", "count", "row"}], "rows": [...], "total"}; folder["row"]
    is the grid row of the folder's own URL when that URL is in the grid (None otherwise)."""
    rows = list(rows or ())
    by_url = {r.get("url"): r for r in rows if r.get("url")}
    if not path:
        hosts = sorted({_host_root(r.get("url") or "") for r in rows} - {""})
        if len(hosts) != 1:
            folders = []
            for h in hosts:
                n = sum(1 for r in rows if (r.get("url") or "").startswith(h))
                folders.append({"path": h, "name": urlsplit(h).netloc, "count": n, "row": by_url.get(h)})
            other = [r for r in rows if not _host_root(r.get("url") or "")]
            return {"path": "", "parent": "", "folders": folders, "rows": other, "total": len(rows)}
        path = hosts[0]
    if not path.endswith("/"):
        path = folder_of(path) or path + "/"
    root, segs = _segments(path)
    parent = "" if not segs else root + "".join(f"{s}/" for s in segs[:-1])
    here: List[dict] = []
    children: Dict[str, int] = {}
    total = 0
    for r in rows:
        f = folder_of(r.get("url") or "")
        if not f.startswith(path):
            continue
        total += 1
        if f == path:
            here.append(r)
            continue
        nxt = f[len(path):].split("/", 1)[0]
        child = f"{path}{nxt}/"
        children[child] = children.get(child, 0) + 1
    here.sort(key=lambda r: 0 if r.get("url") == path else 1)
    folders = [{"path": c, "name": c[len(path):].rstrip("/"), "count": n, "row": by_url.get(c)}
               for c, n in sorted(children.items())]
    return {"path": path, "parent": parent, "folders": folders, "rows": here, "total": total}


# ---------------------------------------------------------------------------------------------
# Word clouds (Visualisations > Inlink Anchor Text / Body Text Word Cloud)
# ---------------------------------------------------------------------------------------------
def _stop_words() -> frozenset:
    try:
        from .extract import STOP_WORDS
        return STOP_WORDS
    except Exception:
        return frozenset()


def words_of(texts: Iterable[str]) -> Counter:
    """Words of the texts, lowercased, with the same rules as the body-text terms: words shorter than three
    letters, stop words and words starting with a digit are left out."""
    stop = _stop_words()
    c: Counter = Counter()
    for t in texts:
        for w in _WORD.findall(str(t or "").lower()):
            w = w.strip("'’-")
            if len(w) >= 3 and w not in stop:
                c[w] += 1
    return c


def all_words_of(texts: Iterable[str]) -> Counter:
    """Every word of the texts, lowercased, stop words and short words included (numbers left out): what the
    anchor cloud shows when the anchors hold nothing but common words."""
    c: Counter = Counter()
    for t in texts:
        for w in _WORD.findall(str(t or "").lower()):
            w = w.strip("'’-")
            if w and not w[0].isdigit():
                c[w] += 1
    return c


def _find(site, url: str):
    from ..exporter import find_url
    return find_url(site, url)


def word_cloud(site, url: str, source: str = "anchors", limit: int = WORD_LIMIT) -> dict:
    """Words for one URL's cloud: source "anchors" = the anchor text (or linked image alt) of every internal
    link pointing at it; "body" = the most used words of its visible text (PageData top_terms).
    {"url", "source", "words": [{"text", "count"}], "links"/"total_words", "note", "not_checked"};
    not_checked says why there can be no cloud (URL not crawled, words not recorded), never an empty
    cloud passed off as a result."""
    out = {"url": url, "source": source, "words": [], "note": "", "not_checked": ""}
    if source not in ("anchors", "body"):
        raise ValueError("source must be 'anchors' or 'body'")
    r, internal = _find(site, url)
    if r is None:
        out["not_checked"] = "This URL is not in the crawl."
        return out
    out["url"] = getattr(r, "url", url)
    if source == "anchors":
        inl = (getattr(site, "inlinks", None) or {}).get(out["url"]) or []
        out["links"] = len(inl)
        anchors = [i.get("anchor") or "" for i in inl]
        counts = words_of(anchors)
        out["words"] = [{"text": w, "count": n} for w, n in counts.most_common(limit)]
        if not inl:
            out["note"] = "No crawled page links to this URL, so it has no inlink anchor text."
        elif not counts:
            common = all_words_of(anchors)
            if common:
                out["words"] = [{"text": w, "count": n} for w, n in common.most_common(limit)]
                out["common_only"] = True
                out["note"] = ("The links to this URL use only common words (shown here), such as 'click', 'here' "
                               "or 'more'. Words that describe the page tell visitors and search engines more.")
            else:
                out["note"] = "The links to this URL have no anchor text words (empty anchors or images without alt)."
        return out
    seo = (getattr(r, "seo", None) or {}) if internal else {}
    if not internal:
        out["not_checked"] = "External URLs are not downloaded, so their text was not read."
        return out
    if not seo.get("is_html"):
        out["not_checked"] = "Only HTML pages have body text."
        return out
    if _category(r) != "2xx":
        out["not_checked"] = "The page did not load (no 2xx answer), so its text was not counted."
        return out
    terms = seo.get("top_terms")
    words = int(seo.get("word_count") or 0)
    out["total_words"] = words
    if not terms:
        if words == 0:
            out["note"] = "The page has no visible text."
        else:
            out["not_checked"] = ("The page's words were not recorded (crawled by an older version); "
                                  "run a new crawl to see its word cloud.")
        return out
    pairs = []
    for t in terms:
        try:
            term, count = t[0], int(t[1])
        except (TypeError, ValueError, IndexError):
            continue
        if term:
            pairs.append({"text": str(term), "count": count})
    out["words"] = sorted(pairs, key=lambda p: (-p["count"], p["text"]))[:limit]
    return out
