"""
Site graph data for the dashboard's visual site map (GET /api/crawl/graph?limit=500).

build_graph(site, index, limit=500) -> {"nodes", "edges", "clustered", "total_nodes", "total_edges", "legend"}

  nodes   every internal page URL (HTML pages, error pages, redirects, robots-blocked pages; not images,
          scripts or other files): {"id": url, "label": path ("blog · /about" on another host of the site),
          "depth": crawl depth, "score": Link Score 0-100 (size), "indexable": bool, "category": "2xx"|"3xx"|
          "4xx"|"5xx"|"error"|"blocked" (colour, from the HTTP status alone), "group": first path segment ("/"
          for the home page; "blog · /docs/" on another host), "folder": first path segment, "host": host name,
          "site_host": "" for the start host (and its www twin) else its short name ("blog"), "match": text that
          finds the group's URLs in the grid, "status", "title", "inlinks"}
  edges   followed internal links between two nodes ({"source", "target"}, one per pair, no self links)
          plus a redirect edge from each 3xx node to its target ({"kind": "redirect"})

A crawl can hold several hosts (Find subdomains, All Subdomains): pages of a host other than the start host are
labelled and grouped with its short name ("shop · /", "blog · /docs/"), so every host's home page is told apart and
no group mixes hosts.

Above `limit` nodes the graph is clustered: the strongest pages (Link Score, the home page always) stay
as their own nodes and every other page joins a node for its host and first path segment ("/blog/ (+120)" with
"count" pages, "categories" {category: n}, "worst_category", "indexable_count", the depth of its
shallowest page, the score of its strongest page and "top_url"); small sections (a single remaining
page, or past the section budget) are merged into one "(other pages)" node ("hosts" {host: pages} says which
hosts it spans; its label names how many). Edges between nodes carry
"weight" (links between them). At most min(3000, max(600, 4 x nodes)) edges are sent (_cap_edges: each node's
link from its shallowest linking page, every redirect, then the heaviest links); "edges_total" is the count
before that cap and "edges_capped" says whether it applied. Every page URL crawled is in exactly one node, so the node counts
(page = 1, cluster = "count") add up to the pages crawled and "categories" reconciles (nothing is dropped).

Drill-down tree (GET /api/crawl/graph/tree): build_tree(site, node=None, depth=2, limit=50) -> {"node": ...}

  The crawled pages (the same URLs as the graph's nodes; files too with files=True) as a tree: the registrable
  domain, then its hosts (a.b.example.com under b.example.com when that host was crawled, else under the domain;
  http:// and https:// of one host are separate hosts, as their URLs are), then URL folders, then pages. A folder
  whose only content is one item is folded into it ("docs/intro/"), so a page is never alone in a folder of its
  own. Every page is in exactly one leaf; each node carries "count" (pages below it), "categories" {category: n}
  (from the HTTP status, as the graph's nodes), "worst_category" and "indexable_count". Children are listed
  lazily: at most `limit` per node (the folder's own page first, then sub-hosts, folders by size, pages with
  problems first), "more" = children not listed, with "more_count" / "more_categories" for the pages in them.
  DrillTree.search(q) finds nodes by name, page title or (a query with "/") address.
"""
import hashlib
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlsplit

from ..issues import classify_result
from ..url_utils import registrable_domain

DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
LABEL_LIMIT = 60
OTHER_ID = "cluster:(other)"
CATEGORY_ORDER = ("4xx", "5xx", "error", "3xx", "blocked", "2xx")   # worst first
EDGE_CAP_MIN, EDGE_CAP_MAX, EDGES_PER_NODE = 600, 3000, 4   # edges sent (the dashboard draws the same number)


def _category(r) -> str:
    try:
        return classify_result(r)
    except Exception:
        return "error"


def _is_node(r) -> bool:
    """A page-like internal URL (not a file such as an image, script or PDF)."""
    return getattr(r, "resource_type", "page") == "page"


def _path(url: str) -> str:
    try:
        parts = urlparse(url)
    except Exception:
        return url
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


HOST_SEP = " · "


def _hostname(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except Exception:
        return ""


def _bare(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def site_host_label(host: str, start_host: str) -> str:
    """'' for the start host (and its www / bare twin); for another host of the same site the part before the
    registrable domain ("blog" for blog.example.com, "a.blog" for a.blog.example.com, "www.blog" kept whole);
    for another domain its name without "www." (a host the start page redirects to)."""
    host, start = (host or "").lower(), (start_host or "").lower()
    if not host or not start or _bare(host) == _bare(start):
        return ""
    domain = registrable_domain(host)
    if domain and domain == registrable_domain(start) and host != domain and host.endswith("." + domain):
        return host[:-(len(domain) + 1)]
    return _bare(host)


def _label(url: str, site_host: str = "") -> str:
    path = _path(url)
    text = f"{site_host}{HOST_SEP}{path}" if site_host else path
    return text if len(text) <= LABEL_LIMIT else text[:LABEL_LIMIT - 3] + "..."


def group_of(url: str) -> str:
    """First path segment as a section name: "/blog/" for /blog/post-1, "/" for the home page."""
    try:
        segs = [s for s in (urlparse(url).path or "/").split("/") if s]
    except Exception:
        segs = []
    return f"/{segs[0]}/" if segs else "/"


def _title(r) -> str:
    p = getattr(r, "seo", None) or {}
    titles = p.get("titles") or []
    return str(titles[0] if titles else (getattr(r, "page_title", "") or ""))[:120]


def _redirect_target(r, by_url: Dict[str, Any]) -> str:
    dest = getattr(r, "redirect_destination", None) or ""
    if not dest or dest == "-":
        return ""
    if dest in by_url:
        return dest
    try:
        from ..url_utils import normalize_url
        norm = normalize_url(dest) or ""
    except Exception:
        norm = ""
    return norm if norm in by_url else ""


def _empty() -> dict:
    return {"nodes": [], "edges": [], "clustered": False, "total_nodes": 0, "total_edges": 0, "edges_total": 0,
            "edges_capped": False,
            "legend": _legend(), "categories": {}}


def _legend() -> dict:
    return {"size": "Link Score (0-100): how much internal link strength the page gets",
            "colour": "HTTP status category (2xx ok, 3xx redirect, 4xx broken, 5xx server error, "
                      "error no response, blocked by robots.txt); non-indexable 2xx pages are marked",
            "depth": "clicks from the start page"}


def build_graph(site, index=None, limit: int = DEFAULT_LIMIT) -> dict:
    """Nodes and edges of the crawled site (see the module docstring). Never raises."""
    try:
        return _build(site, max(10, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT)))
    except Exception:
        return _empty()


def _build(site, limit: int) -> dict:
    if site is None:
        return _empty()
    by_url = getattr(site, "by_url", None) or {}
    pages = [r for r in by_url.values() if _is_node(r)]
    if not pages:
        return _empty()
    depth = getattr(site, "crawl_depth", {}) or {}
    score = getattr(site, "link_score", {}) or {}
    indexable = getattr(site, "indexable", {}) or {}
    istatus = getattr(site, "indexability_status", {}) or {}
    unique_in = getattr(site, "unique_inlinks", {}) or {}
    node_ids = {r.url for r in pages}
    start_host = _hostname(getattr(site, "start_url", "") or "")
    if not start_host:
        home = min(pages, key=lambda r: (int(depth.get(r.url, getattr(r, "depth", 0)) or 0), len(r.url)))
        start_host = _hostname(home.url)

    nodes = []
    for r in pages:
        u = r.url
        host = _hostname(u)
        short = site_host_label(host, start_host)
        folder = group_of(u)
        try:
            netloc = urlsplit(u).netloc.lower()
        except Exception:
            netloc = host
        nodes.append({
            "id": u, "label": _label(u, short), "depth": int(depth.get(u, getattr(r, "depth", 0)) or 0),
            "score": int(score.get(u, 0) or 0), "indexable": bool(indexable.get(u, False)),
            "category": _category(r), "group": f"{short}{HOST_SEP}{folder}" if short else folder,
            "folder": folder, "host": host, "site_host": short,
            "match": (netloc + folder) if short else (folder if folder != "/" else ""),
            "status": getattr(r, "status_code", None),
            "indexability": istatus.get(u, ""), "title": _title(r), "inlinks": int(unique_in.get(u, 0) or 0),
        })

    pairs: Dict[Tuple[str, str], str] = {}
    for src, links in (getattr(site, "outlinks", None) or {}).items():
        if src not in node_ids:
            continue
        for link in links or ():
            try:
                if not link.get("internal") or link.get("nofollow"):
                    continue
                dst = link.get("url") or ""
            except Exception:
                continue
            if dst and dst != src and dst in node_ids:
                pairs.setdefault((src, dst), "link")
    for r in pages:
        if _category(r) == "3xx":
            dst = _redirect_target(r, by_url)
            if dst and dst != r.url and dst in node_ids:
                pairs[(r.url, dst)] = "redirect"
    categories = dict(Counter(n["category"] for n in nodes))

    if len(nodes) <= limit:
        edges = [{"source": s, "target": t, **({"kind": "redirect"} if k == "redirect" else {})}
                 for (s, t), k in pairs.items()]
        shown = _cap_edges(nodes, edges)
        return {"nodes": nodes, "edges": shown, "clustered": False, "total_nodes": len(nodes),
                "total_edges": len(edges), "edges_total": len(edges), "edges_capped": len(shown) < len(edges),
                "legend": _legend(), "categories": categories}
    return _clustered(nodes, pairs, limit, categories)


def _cap_edges(nodes: List[dict], edges: List[dict]) -> List[dict]:
    """At most min(EDGE_CAP_MAX, max(EDGE_CAP_MIN, EDGES_PER_NODE x nodes)) edges, picked the way the dashboard
    draws them: every node keeps the edge from its shallowest linking node (the heaviest on a tie), then every
    redirect, then the heaviest other edges. A mega-menu links every page to every page: all of them would be
    tens of MB of JSON and a frozen browser, and draw nothing readable."""
    cap = min(EDGE_CAP_MAX, max(EDGE_CAP_MIN, EDGES_PER_NODE * len(nodes)))
    if len(edges) <= cap:
        return edges
    depth = {n["id"]: int(n.get("depth") or 0) for n in nodes}

    def weight(e: dict) -> int:
        return int(e.get("weight") or 1)
    into: Dict[str, int] = {}
    for i, e in enumerate(edges):
        b = into.get(e["target"])
        if b is None:
            into[e["target"]] = i
            continue
        cur = edges[b]
        d_new, d_cur = depth.get(e["source"], 0), depth.get(cur["source"], 0)
        if d_new < d_cur or (d_new == d_cur and weight(e) > weight(cur)):
            into[e["target"]] = i
    keep = set(into.values())
    for i, e in enumerate(edges):
        if e.get("kind") == "redirect" and len(keep) < cap:
            keep.add(i)
    rest = sorted((i for i in range(len(edges)) if i not in keep),
                  key=lambda i: (-weight(edges[i]), depth.get(edges[i]["source"], 0), i))
    for i in rest:
        if len(keep) >= cap:
            break
        keep.add(i)
    return [edges[i] for i in sorted(keep)]


def _worst(cats: Counter) -> str:
    for c in CATEGORY_ORDER:
        if cats.get(c):
            return c
    return "2xx"


def _clustered(nodes: List[dict], pairs: Dict[Tuple[str, str], str], limit: int, categories: dict) -> dict:
    groups: Dict[str, List[dict]] = defaultdict(list)
    for n in nodes:
        groups[n["group"]].append(n)
    max_sections = max(1, limit // 4)
    budget = limit - max_sections - 1                      # room for single pages
    ranked = sorted(nodes, key=lambda n: (n["group"] != "/" or n["depth"] != 0, -n["score"], n["depth"], n["id"]))
    single = {n["id"] for n in ranked[:budget]}
    rest: Dict[str, List[dict]] = defaultdict(list)
    for g, members in groups.items():
        for m in members:
            if m["id"] not in single:
                rest[g].append(m)
    sections = sorted((g for g in rest if len(rest[g]) >= 2), key=lambda g: (-len(rest[g]), g))[:max_sections]
    kept = set(sections)
    other = [m for g, members in rest.items() if g not in kept for m in members]

    owner: Dict[str, str] = {}
    out_nodes: List[dict] = []

    def cluster_node(cid: str, label: str, members: List[dict], group: str) -> dict:
        cats = Counter(m["category"] for m in members)
        top = max(members, key=lambda m: (m["score"], -m["depth"]))
        idx = sum(1 for m in members if m["indexable"])
        hosts = Counter(m.get("host") or "" for m in members)
        one_host = len(hosts) == 1
        return {"id": cid, "label": label, "depth": min(m["depth"] for m in members),
                "score": max(m["score"] for m in members), "indexable": idx * 2 >= len(members),
                "category": cats.most_common(1)[0][0], "worst_category": _worst(cats), "group": group,
                "folder": top.get("folder") if one_host else "", "host": top.get("host") if one_host else "",
                "site_host": top.get("site_host") if one_host else "", "match": top.get("match") if one_host else "",
                "hosts": dict(hosts), "count": len(members), "categories": dict(cats), "indexable_count": idx,
                "cluster": True, "top_url": top["id"], "inlinks": sum(m["inlinks"] for m in members)}

    for n in ranked[:budget]:
        out_nodes.append(dict(n, count=1))
        owner[n["id"]] = n["id"]
    for g in sections:
        members = rest[g]
        cid = f"cluster:{g}"
        out_nodes.append(cluster_node(cid, f"{g} (+{len(members)})", members, g))
        for m in members:
            owner[m["id"]] = cid
    if len(other) == 1:
        out_nodes.append(dict(other[0], count=1))
        owner[other[0]["id"]] = other[0]["id"]
    elif other:
        n_hosts = len({m.get("host") or "" for m in other})
        where = f", {n_hosts} hosts" if n_hosts > 1 else ""
        out_nodes.append(cluster_node(OTHER_ID, f"(other pages{where}) (+{len(other)})", other, "(other)"))
        for m in other:
            owner[m["id"]] = OTHER_ID

    weights: Counter = Counter()
    redirects = set()
    for (s_, t), kind in pairs.items():
        a, b = owner.get(s_), owner.get(t)
        if a and b and a != b:
            weights[(a, b)] += 1
            if kind == "redirect":
                redirects.add((a, b))
    edges = [{"source": a, "target": b, "weight": w, **({"kind": "redirect"} if (a, b) in redirects
                                                         and w == 1 else {})}
             for (a, b), w in weights.most_common()]
    clusters = sum(1 for n in out_nodes if n.get("cluster"))
    shown = _cap_edges(out_nodes, edges)
    return {"nodes": out_nodes, "edges": shown, "clustered": True, "total_nodes": len(nodes),
            "total_edges": len(pairs), "edges_total": len(edges), "edges_capped": len(shown) < len(edges),
            "legend": _legend(), "categories": categories,
            "note": f"{len(nodes)} pages: the {len(out_nodes) - clusters} strongest are shown one by one, the "
                    f"rest grouped by host and first folder into {clusters} group(s)."}


def graph_for(crawler, limit: int = DEFAULT_LIMIT) -> dict:
    """build_graph() for a crawler's current index (empty shape without a crawl)."""
    if crawler is None or not hasattr(crawler, "site_index"):
        return _empty()
    try:
        site, index = crawler.site_index()
    except Exception:
        return _empty()
    return build_graph(site, index, limit)


# ---------------------------------------------------------------------------------------------
# Drill-down tree: domain > hosts > folders > pages (GET /api/crawl/graph/tree)
# ---------------------------------------------------------------------------------------------
TREE_DEPTH = 2              # levels listed below the asked node
TREE_LIMIT = 50             # children listed per node; the rest are "more"
TREE_MAX_LIMIT = 500
TREE_MAX_DEPTH = 64
TREE_BUDGET = 2000          # nodes one answer lists at most (Expand all)
TREE_MAX_BUDGET = 5000
TREE_MAX_IDS = 2000         # include / expand ids one request may name
TREE_SEARCH_LIMIT = 200
TREE_NAME_LIMIT = 160
MAX_SEGMENTS = 40           # deeper folders are one folder (URL traps)
KIND_ORDER = {"domain": 0, "host": 1, "folder": 2, "page": 3}
_SEVERITY = {c: i for i, c in enumerate(CATEGORY_ORDER)}


@dataclass
class TreeNode:
    """One node of the drill-down tree. Pages are leaves; the other kinds count the pages below them."""
    id: str
    name: str
    kind: str                                   # domain | host | folder | page
    url: str = ""                               # a page's URL; a host's / folder's address ending in "/"
    parent: str = ""
    children: List[str] = field(default_factory=list)
    count: int = 0
    categories: Counter = field(default_factory=Counter)
    indexable_count: int = 0
    level: int = 0
    note: str = ""
    page: Optional[dict] = None                 # pages: status, category, indexable, indexability, title, ...


class _Trie:
    """The folders of one host while the tree is built."""
    __slots__ = ("subs", "pages")

    def __init__(self):
        self.subs: Dict[str, "_Trie"] = {}
        self.pages: List[tuple] = []            # (row, last path segment, query)


class _Item:
    """A folder or page before it becomes a TreeNode (folding a one-item folder needs its whole subtree)."""
    __slots__ = ("kind", "name", "path", "row", "own", "kids", "count")

    def __init__(self, kind: str, name: str, path: str, row=None, own: bool = False, kids=None):
        self.kind, self.name, self.path, self.row, self.own = kind, name, path, row, own
        self.kids: List["_Item"] = kids or []
        self.count = 1 if kind == "page" else sum(k.count for k in self.kids)


def _natural(name: str) -> tuple:
    """Sort key that puts brand-2 before brand-10 (numbers inside names compare as numbers)."""
    return tuple(int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", str(name).lower()))), str(name)


def _short(text: str, limit: int = TREE_NAME_LIMIT) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _url_parts(url: str) -> Tuple[str, str, str, str]:
    """(host key "scheme://netloc" as the URL writes it, host name, path, query); the host key is "" when the
    URL has no host."""
    try:
        p = urlsplit(url or "")
        host = (p.hostname or "").lower()
    except ValueError:
        return "", "", "/", ""
    path = p.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return (f"{p.scheme}://{p.netloc}" if p.netloc else ""), host, path, p.query


class DrillTree:
    """The whole drill-down tree of one crawl (built once per analysis), answered in slices."""

    def __init__(self, nodes: Dict[str, TreeNode], root: str, domain: str, total: int, files: bool, version: str):
        self.nodes, self.root, self.domain, self.total, self.files, self.version = (
            nodes, root, domain, total, files, version)
        self.rank: Dict[str, int] = {}
        for n in nodes.values():
            for i, c in enumerate(n.children):
                self.rank[c] = i
        kinds = Counter(n.kind for n in nodes.values())
        self.kind_counts = {"hosts": kinds.get("host", 0), "folders": kinds.get("folder", 0),
                            "pages": kinds.get("page", 0)}

    def _out(self, n: TreeNode) -> dict:
        o = {"id": n.id, "name": n.name, "kind": n.kind, "url": n.url, "count": n.count,
             "categories": dict(n.categories), "worst_category": _worst(n.categories),
             "indexable_count": n.indexable_count, "child_count": len(n.children), "loaded": not n.children,
             "children": [], "more": len(n.children), "rank": self.rank.get(n.id, 0), "level": n.level}
        if n.note:
            o["note"] = n.note
        if n.page:
            o.update(n.page)
        return o

    def path_to(self, node_id: str) -> List[str]:
        """Ids from the root down to the node's parent."""
        out, n = [], self.nodes.get(node_id)
        while n is not None and n.parent:
            out.append(n.parent)
            n = self.nodes.get(n.parent)
        return out[::-1]

    def slice(self, node: Optional[str] = None, depth: int = TREE_DEPTH, limit: int = TREE_LIMIT,
              offset: int = 0, include: Iterable[str] = (), expand: Iterable[str] = (),
              budget: int = TREE_BUDGET) -> dict:
        """The node with `depth` levels of children (at most `limit` each; the asked node's from `offset`),
        plus the nodes in `expand` opened at any level and the nodes in `include` listed even past the limit
        (the way to a search result). At most `budget` nodes: a node whose children would go past it is sent
        unopened ("loaded": false). KeyError for an unknown node."""
        n = self.nodes[node or self.root]
        depth = max(0, min(int(TREE_DEPTH if depth is None else depth), TREE_MAX_DEPTH))
        limit = max(1, min(int(limit or TREE_LIMIT), TREE_MAX_LIMIT))
        budget = max(1, min(int(budget or TREE_BUDGET), TREE_MAX_BUDGET))
        exp = set(list(expand or ())[:TREE_MAX_IDS])
        inc_by_parent: Dict[str, List[str]] = defaultdict(list)
        for i in list(include or ())[:TREE_MAX_IDS]:
            k = self.nodes.get(i)
            if k is not None:
                inc_by_parent[k.parent].append(i)
        top = self._out(n)
        emitted = 1
        queue = deque([(top, n, 0)])
        while queue:
            out, cur, lvl = queue.popleft()
            if not cur.children or not (lvl < depth or cur.id in exp):
                continue
            lo = min(max(0, int(offset or 0)), len(cur.children)) if lvl == 0 else 0
            idx = list(range(lo, min(len(cur.children), lo + limit)))
            have = set(idx)
            for cid in inc_by_parent.get(cur.id, ()):
                r = self.rank.get(cid)
                if r is not None and r not in have:
                    idx.append(r)
                    have.add(r)
            if lvl > 0 and emitted + len(idx) > budget:
                continue
            idx.sort()
            kids = [self.nodes[cur.children[i]] for i in idx]
            emitted += len(kids)
            listed = [self._out(k) for k in kids]
            shown: Counter = Counter()
            for k in kids:
                shown.update(k.categories)
            out.update(children=listed, loaded=True, offset=lo, more=len(cur.children) - len(kids),
                       more_count=cur.count - sum(k.count for k in kids),
                       more_categories=dict(cur.categories - shown))
            for k, o in zip(kids, listed):
                queue.append((o, k, lvl + 1))
        return top

    def search(self, query: str, limit: int = TREE_SEARCH_LIMIT) -> dict:
        """Nodes whose name or page title contains the text (case ignored); a text with "/" or ":" is looked
        for in the address instead. Hosts and folders first, shallow first. "inside" counts the matches below
        each node (all of them, not only the ones listed)."""
        q = str(query or "").strip().lower()
        limit = max(1, min(int(limit or TREE_SEARCH_LIMIT), 1000))
        if not q:
            return {"query": query, "total": 0, "matches": [], "inside": {}}
        by_address = "/" in q or ":" in q
        hits = []
        for n in self.nodes.values():
            if n.kind == "domain":
                continue
            if by_address:
                hay = n.url.lower()
            else:
                hay = n.name.lower() + ("\n" + str(n.page.get("title") or "").lower() if n.page else "")
            if q in hay:
                hits.append(n)
        hits.sort(key=lambda n: (KIND_ORDER.get(n.kind, 9), n.level, -n.count, _natural(n.name)))
        inside: Counter = Counter()
        for n in hits:
            p = n.parent
            while p:
                inside[p] += 1
                k = self.nodes.get(p)
                p = k.parent if k is not None else ""
        return {"query": query, "total": len(hits), "limit": limit,
                "matches": [{"id": n.id, "name": n.name, "kind": n.kind, "url": n.url, "count": n.count,
                             "worst_category": _worst(n.categories), "path": self.path_to(n.id)}
                            for n in hits[:limit]],
                "inside": dict(inside.most_common(TREE_MAX_IDS))}

    def answer(self, **kw) -> dict:
        """The API answer: {"node", "domain", "total", "version", "files", "categories", "counts"}."""
        return {"node": self.slice(**kw), "domain": self.domain, "total": self.total, "version": self.version,
                "files": self.files, "categories": dict(self.nodes[self.root].categories),
                "counts": self.kind_counts}


def _trie_size(trie: _Trie) -> int:
    n, stack = 0, [trie]
    while stack:
        t = stack.pop()
        n += len(t.pages)
        stack.extend(t.subs.values())
    return n


def _subtree_order(nodes: Dict[str, TreeNode], root_id: str) -> List[str]:
    order, stack = [], [root_id]
    while stack:
        nid = stack.pop()
        order.append(nid)
        stack.extend(nodes[nid].children)
    return order


def _finish(nodes: Dict[str, TreeNode], root_id: str) -> None:
    """Levels top-down, then pages / categories / indexable pages below each node bottom-up (no recursion)."""
    order = _subtree_order(nodes, root_id)
    for nid in order:
        n = nodes[nid]
        for c in n.children:
            nodes[c].level = n.level + 1
    for nid in reversed(order):
        n = nodes[nid]
        if n.kind == "page":
            continue
        n.count, n.indexable_count, n.categories = 0, 0, Counter()
        for c in n.children:
            k = nodes[c]
            n.count += k.count
            n.indexable_count += k.indexable_count
            n.categories.update(k.categories)


def build_tree_index(site, files: bool = False) -> DrillTree:
    """The drill-down tree of the crawl (see the module docstring). files=True counts every internal URL
    (images, scripts and PDFs too: the Internal tab's "All"), else the page-like URLs of the site graph."""
    by_url = (getattr(site, "by_url", None) or {}) if site is not None else {}
    rows = [r for r in by_url.values() if getattr(r, "url", "") and (files or _is_node(r))]
    if not rows:
        return DrillTree({}, "", "", 0, files, "")
    from ..url_utils import host_in_domain, registrable_domain
    indexable = getattr(site, "indexable", {}) or {}
    istatus = getattr(site, "indexability_status", {}) or {}
    depth = getattr(site, "crawl_depth", {}) or {}

    # 1. hosts and their folders
    hosts: Dict[str, _Trie] = {}
    host_names: Dict[str, str] = {}
    cat_of: Dict[str, str] = {}
    for r in rows:
        key, hname, path, query = _url_parts(r.url)
        cat_of[r.url] = _category(r)
        trie = hosts.get(key)
        if trie is None:
            trie = hosts[key] = _Trie()
            host_names[key] = hname
        cut = path.rfind("/")
        segs = path[:cut + 1].split("/")[1:-1]
        if len(segs) > MAX_SEGMENTS:
            segs = segs[:MAX_SEGMENTS - 1] + ["/".join(segs[MAX_SEGMENTS - 1:])]
        for s in segs:
            nxt = trie.subs.get(s)
            if nxt is None:
                nxt = trie.subs[s] = _Trie()
            trie = nxt
        trie.pages.append((r, path[cut + 1:], query))

    start = str(getattr(site, "start_url", "") or "")
    domain = registrable_domain(start) if start else ""
    if not domain:
        doms = Counter(registrable_domain(h) for h in host_names.values() if h)
        domain = doms.most_common(1)[0][0] if doms else "(no host)"

    # 2. folders as items: a folder holding one item is folded into it, so no page sits alone in a folder
    def sort_items(items: List[_Item]) -> List[_Item]:
        def key(it: _Item):
            if it.kind == "page" and it.own:
                return (0, 0, 0, _natural(it.name))
            if it.kind == "folder":
                return (1, -it.count, 0, _natural(it.name))
            return (2, 0, _SEVERITY.get(cat_of.get(it.row.url, "2xx"), 9), _natural(it.name))
        return sorted(items, key=key)

    def content(trie: _Trie, folder_name: str, path: str) -> List[_Item]:
        items = [_Item("page", (last or folder_name) + (f"?{q}" if q else ""), path, row=r, own=not last)
                 for r, last, q in trie.pages]
        items.extend(folder(sub, seg, f"{path}{seg}/") for seg, sub in trie.subs.items())
        return sort_items(items)

    def folder(trie: _Trie, seg: str, path: str) -> _Item:
        name = f"{seg}/"
        kids = content(trie, name, path)
        if len(kids) == 1:
            only = kids[0]
            if not (only.kind == "page" and only.own and only.path == path):
                only.name = name + only.name          # "docs/" + "intro/" = "docs/intro/"
            only.own = False                          # in its parent it is not that folder's own page
            return only
        return _Item("folder", name, path, kids=kids)

    nodes: Dict[str, TreeNode] = {}
    root_id = f"d:{domain}"
    nodes[root_id] = TreeNode(root_id, domain, "domain")

    def add_items(items: List[_Item], parent: TreeNode, host_key: str) -> None:
        stack = [(items, parent)]
        while stack:
            batch, par = stack.pop()
            for it in batch:
                if it.kind == "page":
                    u = it.row.url
                    cat = cat_of.get(u, "error")
                    ok = bool(indexable.get(u, False))
                    status = getattr(it.row, "status_code", None)
                    try:
                        title = _title(it.row)
                    except Exception:
                        title = ""
                    node = TreeNode(f"p:{u}", _short(it.name), "page", url=u, parent=par.id, count=1,
                                    categories=Counter({cat: 1}), indexable_count=1 if ok else 0,
                                    page={"status": status if isinstance(status, int) else None, "category": cat,
                                          "indexable": ok, "indexability": str(istatus.get(u, "") or ""),
                                          "title": title,
                                          "depth": int(depth.get(u, getattr(it.row, "depth", 0)) or 0),
                                          "folder": f"{host_key}{it.path}" if host_key else ""})
                else:
                    node = TreeNode(f"f:{host_key}{it.path}", _short(it.name), "folder",
                                    url=f"{host_key}{it.path}" if host_key else "", parent=par.id)
                if node.id in nodes:      # one node per id (distinct URLs never collide)
                    continue
                nodes[node.id] = node
                par.children.append(node.id)
                if it.kind == "folder":
                    stack.append((it.kids, node))

    # 3. hosts: nested by label inside the domain; http:// and https:// of one host apart (their URLs differ)
    by_name: Dict[str, List[str]] = defaultdict(list)
    for key, h in host_names.items():
        by_name[h].append(key)
    size = {key: _trie_size(trie) for key, trie in hosts.items()}

    def host_parent(key: str) -> str:
        h = host_names[key]
        if not h or not host_in_domain(h, domain):
            return root_id
        labels = h.split(".")
        for i in range(1, len(labels)):
            anc = ".".join(labels[i:])
            if len(anc) <= len(domain):
                break
            keys = by_name.get(anc)
            if keys:
                best = sorted(keys, key=lambda k: (not k.startswith("https://"), -size[k], k))[0]
                return f"h:{best}"
        return root_id

    for key, h in host_names.items():
        scheme, _, netloc = key.partition("://")
        name = netloc if len(by_name[h]) == 1 or scheme == "https" else key
        node = TreeNode(f"h:{key}", _short(name or "(URLs without a host)"), "host", url=f"{key}/" if key else "")
        if h and not host_in_domain(h, domain):
            node.note = f"Outside {domain}"
        nodes[node.id] = node
    start_key = _url_parts(start)[0] if start else ""
    nested: Dict[str, List[str]] = defaultdict(list)
    for key in sorted(host_names, key=lambda k: (k != start_key, -size[k], k)):
        par = host_parent(key)
        nodes[f"h:{key}"].parent = par
        if par == root_id:
            nodes[root_id].children.append(f"h:{key}")
        else:
            nested[par].append(f"h:{key}")
    for key, trie in hosts.items():
        node = nodes[f"h:{key}"]
        items = content(trie, "/", "/")
        add_items([it for it in items if it.kind == "page" and it.own], node, key)   # the home page first
        node.children.extend(nested.get(node.id, ()))
        add_items([it for it in items if not (it.kind == "page" and it.own)], node, key)
    _finish(nodes, root_id)
    sig = hashlib.blake2b(digest_size=8)
    for u in sorted(cat_of):
        sig.update(f"{u}\t{cat_of[u]}\t{1 if indexable.get(u) else 0}\n".encode("utf-8", "replace"))
    return DrillTree(nodes, root_id, domain, len(rows), files, sig.hexdigest())


def build_tree(site, node: Optional[str] = None, depth: int = TREE_DEPTH, limit: int = TREE_LIMIT,
               offset: int = 0, include: Iterable[str] = (), expand: Iterable[str] = (),
               budget: int = TREE_BUDGET, files: bool = False, tree: Optional[DrillTree] = None) -> dict:
    """One slice of the drill-down tree as the API answers it (see DrillTree.slice / answer); `tree` reuses a
    built tree. {"node": None, ...} without pages; KeyError for an unknown node id."""
    t = tree if tree is not None else build_tree_index(site, files)
    if not t.root:
        return {"node": None, "domain": "", "total": 0, "version": "", "files": files, "categories": {},
                "counts": {"hosts": 0, "folders": 0, "pages": 0}}
    return t.answer(node=node, depth=depth, limit=limit, offset=offset, include=include, expand=expand,
                    budget=budget)

