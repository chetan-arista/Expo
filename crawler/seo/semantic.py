"""
Topic clusters, a 2D topic map and semantic search over a crawl's indexable pages (Content tab "Topic Cluster"
column; GET /api/crawl/clusters and GET /api/crawl/semantic-search). Pure Python: numpy is not installed, so the
work is kept small with samples and caps (PLAN4_build.md C6).

Two kinds of page vectors ("method"):
  - "embeddings": the page vectors of the crawl's last embeddings run (crawler/ai/embeddings.py, AI). That run
    groups the pages and places them on the map itself and stores the result with the crawl
    (site.integrations["embeddings"]: a cluster and x / y per URL, and the cluster labels; never the vectors).
    Search embeds the query (one AI call on the "search" budget) and compares it with the page vectors, which are
    kept in the server's memory only (after a restart or for a reopened crawl, run embeddings again).
  - "words": TF-IDF vectors of each page's top content words (PageData top_terms) plus the words of its title
    and H1, with plural endings folded ("policies" -> "policy"). Used when the crawl has no embeddings run, or
    when asked for.
Pages: indexable internal HTML pages that answered 2xx, in crawl order. A page with fewer than 8 different
content words is not grouped by words ("Not checked: too little text"); at most CLUSTER_MAX_PAGES are grouped.

Clusters: spherical k-means (cosine similarity, k-means++ start, single-page moves while they improve the fit, a
fixed seed so the same crawl always gives the same clusters). k is chosen from 3 to 12 by the mean silhouette score
on a sample of at most SILHOUETTE_SAMPLE pages (drawn with a fixed seed), the clusters are then refined on at most FIT_MAX
pages, and every page joins its nearest cluster. Words: a page is grouped on its CLUSTER_TERMS strongest words. A cluster's label is the three words most typical of its pages compared with the whole site (used by
many of its pages, repeated on them, rare elsewhere; a word on every page, such as a brand name, never).
Map: the first two principal components (power iteration) of at most POINTS_MAX pages, scaled to -1..1.
Search: cosine similarity of the query and each page; "words" only finds pages that use the query's words.

Public: clusters(site, method=None), cluster_of(site, url), topic_cluster(site, r), search(site, query, limit=20,
method=None), and the building blocks the embeddings run uses: cluster_vectors(), label_clusters(),
project_points(), word_vectors(), fold(), page_terms().
"""
import hashlib
import math
import random
import re
import threading
import time
from collections import Counter, OrderedDict, defaultdict
from itertools import repeat
from operator import mul
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .registry import is_indexable, seo

Vector = Union[List[float], Dict[str, float]]   # dense (embeddings) or sparse (words), always unit length

MIN_TERMS = 8                 # a page needs this many different content words to be grouped by words
MIN_CLUSTER_PAGES = 6         # fewer pages: no clusters
K_MIN, K_MAX = 3, 12
SILHOUETTE_SAMPLE = 300       # pages the number of clusters is chosen on
FIT_MAX = 1000                # pages the clusters are refined on (the rest join the nearest cluster)
POINTS_MAX = 1000             # pages placed on the map
CLUSTER_MAX_PAGES = 5000      # pages grouped at all
KMEANS_ITERS = 20
REFINE_PASSES = 6
RESTARTS = 2                  # k-means++ starts per k (the tightest is kept)
CLUSTER_TERMS = 40            # words: each page is grouped on its 40 strongest words (search uses them all)
SEED = 20261028
LABEL_TERMS = 3
SEARCH_MAX = 100
CACHE_SIZE = 6
EMBEDDINGS = "embeddings"
WORDS = "words"
METHODS = (EMBEDDINGS, WORDS)
NOT_FINISHED = "Topic clusters are worked out once the crawl has finished."
TOO_FEW = ("Too few pages with enough text to group by topic (at least {n} indexable pages with 8 or more "
           "different content words are needed).")
LITTLE_TEXT = "Too little text to tell its topic (fewer than 8 different content words)."
OVER_CAP = "Over the {n:,}-page limit of topic clusters."
NO_RUN = ("No embeddings run for this crawl yet: run embeddings in the Content tab (AI), or use the word-based "
          "clusters.")


# ---------------------------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------------------------
def dot(a: Vector, b: Vector) -> float:
    """Dot product of two dense lists or two sparse dicts (cosine for unit vectors)."""
    if isinstance(a, dict):
        if len(a) > len(b):
            a, b = b, a
        return sum(map(mul, a.values(), map(b.get, a.keys(), repeat(0.0))))
    return sum(map(mul, a, b))


def unit(v: Vector) -> Vector:
    """v scaled to length 1 (empty when v is all zeros)."""
    if isinstance(v, dict):
        n = math.sqrt(sum(w * w for w in v.values()))
        return {t: w / n for t, w in v.items()} if n > 0 else {}
    n = math.sqrt(sum(map(mul, v, v)))
    return [x / n for x in v] if n > 0 else []


def vsum(vs: Sequence[Vector]) -> Vector:
    """Element-wise sum of vectors of one kind."""
    if not vs:
        return []
    if isinstance(vs[0], dict):
        acc: Dict[str, float] = defaultdict(float)
        for v in vs:
            for t, w in v.items():
                acc[t] += w
        return dict(acc)
    return [math.fsum(col) for col in zip(*vs)] if len(vs) < 64 else list(map(sum, zip(*vs)))


# ---------------------------------------------------------------------------------------------
# Words: TF-IDF vectors of the pages' top content words
# ---------------------------------------------------------------------------------------------
_WORD = re.compile(r"[^\W\d_]{2,}(?:['’][^\W\d_]+)?", re.UNICODE)
_KEEP_S = ("ss", "us", "is", "ous", "ics", "ies", "news", "series", "species")


def fold(word: str) -> str:
    """Lower case, with an English plural ending folded ("policies" -> "policy", "boxes" -> "box",
    "returns" -> "return"), so the singular and the plural count as one word."""
    w = (word or "").lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) <= 4:
        return w
    if w.endswith("ies") and w not in ("series", "species"):
        return w[:-3] + "y"
    if w.endswith(_KEEP_S):
        return w
    if w.endswith(("ches", "shes", "sses", "xes", "zes")):
        return w[:-2]
    if w.endswith("s"):
        return w[:-1]
    return w


def _stop_words() -> frozenset:
    try:
        from .extract import STOP_WORDS
        return STOP_WORDS
    except Exception:
        return frozenset()


def tokens(text: str) -> List[str]:
    """The folded content words of a text (no stop words, at least 3 letters)."""
    stop = _stop_words()
    out = []
    for m in _WORD.finditer(text or ""):
        w = m.group(0).lower()
        if len(w) >= 3 and w not in stop:
            out.append(fold(w))
    return out


def _first(r, key: str) -> str:
    vals = seo(r).get(key) or []
    for v in vals:
        s = (v if isinstance(v, str) else str(v or "")).strip()
        if s:
            return s
    return ""


def page_terms(r, display: Optional[Dict[str, Counter]] = None) -> Counter:
    """Folded term -> count: the page's top content words plus the words of its first title and H1. `display`
    collects the written forms of each folded term (for labels)."""
    tc: Counter = Counter()
    for item in seo(r).get("top_terms") or []:
        try:
            t, c = str(item[0]), int(item[1])
        except Exception:
            continue
        if t and c > 0:
            f = fold(t)
            tc[f] += c
            if display is not None:
                display[f][t.lower()] += c
    stop = _stop_words()
    for text in (_first(r, "titles"), _first(r, "h1")):
        for m in _WORD.finditer(text):
            w = m.group(0).lower()
            if len(w) >= 3 and w not in stop:
                f = fold(w)
                tc[f] += 1
                if display is not None:
                    display[f][w] += 1
    return tc


def pages_of(site) -> List[Any]:
    """Indexable internal HTML pages that answered 2xx, in crawl order (the pages clustered and searched)."""
    try:
        from .tabs.page_titles import content_rows
        rows = content_rows(site)
    except Exception:
        rows = []
    return [r for r in rows if is_indexable(site, r)]


def _fingerprint(site, pages: Sequence[Any]) -> str:
    h = hashlib.sha1()
    for r in pages:
        s = seo(r)
        h.update(f"{r.url}\x00{s.get('content_hash') or ''}\x00{_first(r, 'titles')}\x00{_first(r, 'h1')}\x01"
                 .encode("utf-8", "replace"))
    return h.hexdigest()


def _memo(site) -> Dict[str, Any]:
    """Per-Site memo (a Site is rebuilt whenever the crawl changes; the column asks for every row)."""
    memo = getattr(site, "_semantic_memo", None)
    if not isinstance(memo, dict):
        memo = {}
        try:
            setattr(site, "_semantic_memo", memo)
        except Exception:
            pass
    return memo


_cache: "OrderedDict[Tuple[str, str], Any]" = OrderedDict()
_cache_lock = threading.Lock()
_compute_lock = threading.Lock()


def _cached(key: Tuple[str, str], build):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    with _compute_lock:                 # one computation at a time (a second caller then finds it cached)
        with _cache_lock:
            if key in _cache:
                return _cache[key]
        value = build()
        with _cache_lock:
            _cache[key] = value
            while len(_cache) > CACHE_SIZE:
                _cache.popitem(last=False)
        return value


def _strongest(v: Dict[str, float], n: int = CLUSTER_TERMS) -> Dict[str, float]:
    """The n largest weights of a sparse vector, at unit length again."""
    if len(v) <= n:
        return v
    return unit(dict(sorted(v.items(), key=lambda kv: (-kv[1], kv[0]))[:n]))


def _words_fingerprint(site) -> str:
    memo = _memo(site)
    if "fingerprint" not in memo:
        memo["fingerprint"] = _fingerprint(site, pages_of(site))
    return memo["fingerprint"]


def word_vectors(site) -> Dict[str, Any]:
    """{"vectors": {url: sparse unit vector}, "idf": {term: weight}, "display": {term: written form},
    "terms": {url: Counter of folded terms}, "skipped": {url: reason}, "fingerprint"} for the pages of
    pages_of(site); kept on the Site only (it is large), while the clusters made from it are cached by
    fingerprint across Site rebuilds."""
    memo = _memo(site)
    if "words" in memo:
        return memo["words"]
    display: Dict[str, Counter] = defaultdict(Counter)
    counts: Dict[str, Counter] = {}
    skipped: Dict[str, str] = {}
    for r in pages_of(site):
        tc = page_terms(r, display)
        if len(tc) >= MIN_TERMS:
            counts[r.url] = tc
        else:
            skipped[r.url] = LITTLE_TEXT
    n = len(counts)
    df: Counter = Counter()
    for tc in counts.values():
        df.update(tc.keys())
    idf = {t: math.log((1 + n) / (1 + d)) + 1.0 for t, d in df.items()}
    vectors = {u: unit({t: (1.0 + math.log(c)) * idf[t] for t, c in tc.items()}) for u, tc in counts.items()}
    shown = {t: forms.most_common(1)[0][0] for t, forms in display.items() if forms}
    out = {"vectors": vectors, "idf": idf, "display": shown, "skipped": skipped,
           "fingerprint": _words_fingerprint(site), "terms": counts}
    memo["words"] = out
    return out


# ---------------------------------------------------------------------------------------------
# k-means (cosine), silhouette, principal components
# ---------------------------------------------------------------------------------------------
def _nearest(v: Vector, centroids: Sequence[Vector]) -> Tuple[int, float]:
    best, best_s = 0, -2.0
    for c, cen in enumerate(centroids):
        s = dot(v, cen)
        if s > best_s:
            best, best_s = c, s
    return best, best_s


def _scorer(vecs: Sequence[Vector]):
    """f(i, centre) -> dot(vecs[i], centre) for a dense or sparse (dict) centre, with each sparse vector's terms
    unpacked once (k-means scores every vector against every centre many times)."""
    if vecs and isinstance(vecs[0], dict):
        keys = [tuple(v.keys()) for v in vecs]
        vals = [tuple(v.values()) for v in vecs]

        def sparse(i: int, centre: Dict[str, float]) -> float:
            return sum(map(mul, vals[i], map(centre.get, keys[i], repeat(0.0))))
        return sparse

    def dense(i: int, centre: List[float]) -> float:
        return sum(map(mul, vecs[i], centre))
    return dense


def _closest(score, i: int, centroids: Sequence[Vector]) -> Tuple[int, float]:
    best, best_s = 0, -2.0
    for c, cen in enumerate(centroids):
        s = score(i, cen)
        if s > best_s:
            best, best_s = c, s
    return best, best_s


def _kpp(vecs: Sequence[Vector], k: int, rng: random.Random) -> List[Vector]:
    """k-means++ start: each next centre is drawn with a chance that grows with its distance from the others."""
    n = len(vecs)
    centres = [vecs[rng.randrange(n)]]
    best = [dot(v, centres[0]) for v in vecs]
    while len(centres) < k:
        weights = [max(0.0, 1.0 - s) ** 2 for s in best]
        total = sum(weights)
        if total <= 0:
            idx = rng.randrange(n)
        else:
            pick, acc, idx = rng.random() * total, 0.0, n - 1
            for i, w in enumerate(weights):
                acc += w
                if acc >= pick:
                    idx = i
                    break
        centres.append(vecs[idx])
        best = [max(b, dot(v, centres[-1])) for b, v in zip(best, vecs)]
    return list(centres)


def kmeans(vecs: Sequence[Vector], k: int, rng: random.Random, init: Optional[Sequence[Vector]] = None,
           iters: int = KMEANS_ITERS, passes: int = REFINE_PASSES) -> Tuple[List[int], List[Vector], float]:
    """(cluster of each vector, unit centroids, total similarity to the own centroid)."""
    n = len(vecs)
    k = max(1, min(k, n))
    centroids = list(init) if init else _kpp(vecs, k, rng)
    score = _scorer(vecs)
    assign = [-1] * n
    sims = [0.0] * n
    for it in range(iters):
        changed = 0
        for i in range(n):
            c, s = _closest(score, i, centroids)
            sims[i] = s
            if assign[i] != c:
                assign[i] = c
                changed += 1
        if it and not changed:
            break
        groups: List[List[Vector]] = [[] for _ in range(k)]
        for i, c in enumerate(assign):
            groups[c].append(vecs[i])
        taken = set()
        for c in range(k):
            if groups[c]:
                centroids[c] = unit(vsum(groups[c])) or centroids[c]
            else:                       # an empty cluster restarts at the page that fits its cluster worst
                order = sorted(range(n), key=lambda i: sims[i])
                far = next((i for i in order if i not in taken), order[0])
                taken.add(far)
                centroids[c] = vecs[far]
    for i in range(n):
        assign[i] = _closest(score, i, centroids)[0]
    assign, centroids = _refine(vecs, assign, k, score, passes)
    total = sum(score(i, centroids[assign[i]]) for i in range(n))
    return assign, centroids, total


def _add(a: Vector, b: Vector, sign: float) -> Vector:
    if isinstance(a, dict):
        out = dict(a)
        for t, w in b.items():
            out[t] = out.get(t, 0.0) + sign * w
        return out
    return [x + sign * y for x, y in zip(a, b)]


def _refine(vecs: Sequence[Vector], assign: List[int], k: int, score=None,
            passes: int = REFINE_PASSES) -> Tuple[List[int], List[Vector]]:
    """First-variation refinement of spherical k-means: move one vector at a time to another cluster while that
    raises the objective (the sum over clusters of the length of the cluster's vector sum, i.e. the total
    similarity of every vector to its centroid). It escapes the local optima the assign / update loop stops in."""
    score = score or _scorer(vecs)
    sums: List[Vector] = []
    size = Counter(assign)
    for c in range(k):
        members = [vecs[i] for i, a in enumerate(assign) if a == c]
        sums.append(vsum(members) if members else ({} if isinstance(vecs[0], dict) else [0.0] * len(vecs[0])))
    norms2 = [dot(s, s) for s in sums]
    for _ in range(passes):
        moved = 0
        for i, x in enumerate(vecs):
            a = assign[i]
            if size[a] <= 1:
                continue
            xx = dot(x, x)
            ax = score(i, sums[a])
            base_a = math.sqrt(max(0.0, norms2[a]))
            new_a = math.sqrt(max(0.0, norms2[a] - 2 * ax + xx))
            best_b, best_gain, best_bx = -1, 1e-9, 0.0
            for b in range(k):
                if b == a:
                    continue
                bx = score(i, sums[b])
                gain = new_a + math.sqrt(max(0.0, norms2[b] + 2 * bx + xx)) - base_a - math.sqrt(max(0.0, norms2[b]))
                if gain > best_gain:
                    best_b, best_gain, best_bx = b, gain, bx
            if best_b >= 0:
                sums[a] = _add(sums[a], x, -1.0)
                sums[best_b] = _add(sums[best_b], x, 1.0)
                norms2[a] = max(0.0, norms2[a] - 2 * ax + xx)
                norms2[best_b] = norms2[best_b] + 2 * best_bx + xx
                size[a] -= 1
                size[best_b] += 1
                assign[i] = best_b
                moved += 1
        if not moved:
            break
    centroids = [unit(s) or (vecs[assign.index(c)] if c in assign else vecs[0]) for c, s in enumerate(sums)]
    return assign, centroids


def _distances(vecs: Sequence[Vector]) -> List[List[float]]:
    n = len(vecs)
    d = [[0.0] * n for _ in range(n)]
    for i in range(n):
        vi, row = vecs[i], d[i]
        for j in range(i + 1, n):
            x = 1.0 - dot(vi, vecs[j])
            row[j] = x
            d[j][i] = x
    return d


def silhouette(dist: List[List[float]], assign: Sequence[int], k: int) -> float:
    """Mean silhouette score (-1..1; higher = tighter, better separated clusters); a lone page scores 0."""
    n = len(assign)
    size = Counter(assign)
    total = 0.0
    for i in range(n):
        ci = assign[i]
        if size[ci] <= 1:
            continue
        sums = [0.0] * k
        row = dist[i]
        for j in range(n):
            sums[assign[j]] += row[j]
        a = sums[ci] / (size[ci] - 1)
        others = [sums[c] / size[c] for c in range(k) if c != ci and size[c]]
        if not others:
            continue
        b = min(others)
        m = max(a, b)
        total += (b - a) / m if m > 0 else 0.0
    return total / n if n else 0.0


def _spread(n: int, m: int) -> List[int]:
    """m indexes spread evenly over range(n) (all when n <= m)."""
    if n <= m:
        return list(range(n))
    return sorted({i * n // m for i in range(m)})


def _sample(n: int, m: int, seed: int) -> List[int]:
    """m indexes of range(n) drawn with a fixed seed, in order (all when n <= m); unlike an even spread it cannot
    line up with a pattern in the crawl order."""
    if n <= m:
        return list(range(n))
    return sorted(random.Random(seed).sample(range(n), m))


def cluster_vectors(vecs: Dict[str, Vector], seed: int = SEED) -> Dict[str, Any]:
    """Groups unit vectors by topic. {"k", "silhouette", "assign": {url: cluster 1..k (largest first)},
    "sizes": {cluster: pages}} or {"reason"} when there are too few."""
    urls = list(vecs)
    n = len(urls)
    if n < MIN_CLUSTER_PAGES:
        return {"reason": TOO_FEW.format(n=MIN_CLUSTER_PAGES)}
    sample = [vecs[urls[i]] for i in _sample(n, SILHOUETTE_SAMPLE, seed)]
    dist = _distances(sample)
    best: Optional[Tuple[float, int, List[Vector]]] = None
    for k in range(K_MIN, max(K_MIN, min(K_MAX, len(sample) // 2)) + 1):
        run = None
        for r in range(RESTARTS):
            out = kmeans(sample, k, random.Random(seed + 7919 * k + r))
            if run is None or out[2] > run[2] + 1e-12:
                run = out
        score = silhouette(dist, run[0], k)
        if best is None or score > best[0] + 1e-9:
            best = (score, k, run[1])
    sil, k, centroids = best
    fit = [vecs[urls[i]] for i in _sample(n, FIT_MAX, seed + 1)]
    _, centroids, _ = kmeans(fit, k, random.Random(seed), init=centroids)
    raw = [_nearest(vecs[u], centroids)[0] for u in urls]
    size = Counter(raw)
    first = {}
    for i, c in enumerate(raw):
        first.setdefault(c, i)
    order = sorted(size, key=lambda c: (-size[c], first[c]))
    renum = {c: i + 1 for i, c in enumerate(order)}
    assign = {u: renum[c] for u, c in zip(urls, raw)}
    return {"k": len(order), "silhouette": round(sil, 4), "assign": assign,
            "sizes": {renum[c]: size[c] for c in order}}


def label_clusters(assign: Dict[str, int], terms: Dict[str, Counter], display: Dict[str, str],
                   top: int = LABEL_TERMS) -> Dict[int, List[str]]:
    """cluster -> its most typical words (written forms), scored share of the cluster's pages using the word x
    its mean count on them x log(grouped pages / pages using it anywhere): a word every page uses (a brand name
    in the template) scores 0, a word many pages of the cluster repeat and few others use scores high."""
    members: Dict[int, List[str]] = defaultdict(list)
    for u, c in assign.items():
        members[c].append(u)
    n = max(1, len(assign))
    df_all: Counter = Counter()
    for u in assign:
        df_all.update((terms.get(u) or {}).keys())
    out: Dict[int, List[str]] = {}
    for c, urls in members.items():
        df: Counter = Counter()
        tf: Counter = Counter()
        for u in urls:
            tc = terms.get(u) or {}
            df.update(tc.keys())
            tf.update(tc)
        size = max(1, len(urls))
        scored = []
        for t, d in df.items():
            idf = math.log(n / df_all[t]) if df_all[t] else 0.0
            if idf <= 0:
                continue
            scored.append(((d / size) * (tf[t] / size) * idf, tf[t], t))
        scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
        out[c] = [display.get(t, t) for _, _, t in scored[:top]]
    return out


def _centre(vecs: Sequence[Vector]) -> Vector:
    total = vsum(vecs)
    n = float(len(vecs))
    if isinstance(total, dict):
        return {t: w / n for t, w in total.items()}
    return [x / n for x in total]


def _component(vecs: Sequence[Vector], mean: Vector, cols, rng: random.Random,
               against: Optional[Vector] = None, iters: int = 100) -> Vector:
    """The strongest direction of the centred vectors (power iteration), orthogonal to `against`."""
    sparse = isinstance(mean, dict)
    if sparse:
        w: Vector = unit({t: rng.uniform(-1, 1) for t in mean})
    else:
        w = unit([rng.uniform(-1, 1) for _ in range(len(mean))])

    def deflate(v: Vector) -> Vector:
        if against is None:
            return v
        p = dot(v, against)
        if sparse:
            out = dict(v)
            for t, a in against.items():
                out[t] = out.get(t, 0.0) - p * a
            return out
        return [x - p * a for x, a in zip(v, against)]
    w = unit(deflate(w))
    for _ in range(iters):
        mw = dot(mean, w)
        ys = [dot(v, w) - mw for v in vecs]
        sy = sum(ys)
        if sparse:
            z: Dict[str, float] = defaultdict(float)
            for y, v in zip(ys, vecs):
                if y:
                    for t, x in v.items():
                        z[t] += y * x
            for t, m in mean.items():
                z[t] -= m * sy
            nz: Vector = dict(z)
        else:
            nz = [sum(map(mul, ys, col)) - m * sy for col, m in zip(cols, mean)]
        nz = unit(deflate(nz))
        if not nz:
            return w
        if abs(dot(nz, w)) > 1 - 1e-9:
            return nz
        w = nz
    return w


def project_points(vecs: Sequence[Vector], seed: int = SEED) -> List[Tuple[float, float]]:
    """(x, y) of each vector on its first two principal components, scaled to -1..1."""
    n = len(vecs)
    if n == 0:
        return []
    if n == 1:
        return [(0.0, 0.0)]
    rng = random.Random(seed)
    mean = _centre(vecs)
    cols = None if isinstance(mean, dict) else list(zip(*vecs))
    c1 = _component(vecs, mean, cols, rng)
    c2 = _component(vecs, mean, cols, rng, against=c1)
    m1, m2 = dot(mean, c1), dot(mean, c2)
    xs = [dot(v, c1) - m1 for v in vecs]
    ys = [dot(v, c2) - m2 for v in vecs] if c2 else [0.0] * n
    # a fixed sign: the farthest point of each axis lies on its positive side
    if max(xs) < -min(xs):
        xs = [-x for x in xs]
    if max(ys) < -min(ys):
        ys = [-y for y in ys]
    scale = max(max(abs(x) for x in xs), max(abs(y) for y in ys)) or 1.0
    return [(round(x / scale, 4), round(y / scale, 4)) for x, y in zip(xs, ys)]


def map_sample(assign: Dict[str, int], limit: int = POINTS_MAX) -> List[str]:
    """The URLs placed on the map: all of them, or `limit` spread over the clusters in proportion (crawl order)."""
    urls = list(assign)
    if len(urls) <= limit:
        return urls
    by: Dict[int, List[str]] = defaultdict(list)
    for u in urls:
        by[assign[u]].append(u)
    keep = set()
    for c, members in by.items():
        share = max(1, round(limit * len(members) / len(urls)))
        keep.update(members[i] for i in _spread(len(members), share))
    return [u for u in urls if u in keep][:limit]


# ---------------------------------------------------------------------------------------------
# Clusters of one crawl
# ---------------------------------------------------------------------------------------------
def embeddings_entry(site) -> Optional[Dict[str, Any]]:
    """site.integrations["embeddings"] when an embeddings run stored results for this crawl, else None."""
    e = (getattr(site, "integrations", None) or {}).get("embeddings")
    return e if isinstance(e, dict) and isinstance(e.get("results"), dict) and e["results"] else None


def _title(site, url: str) -> str:
    r = (getattr(site, "by_url", None) or {}).get(url)
    return _first(r, "titles") if r is not None else ""


def _empty(method: str, reason: str) -> Dict[str, Any]:
    return {"method": method, "reason": reason, "k": 0, "silhouette": None, "pages": 0, "clusters": [],
            "points": [], "points_total": 0, "not_checked": 0, "not_checked_reasons": {}, "assign": {},
            "skipped": {}}


def _finish(site, method: str, k: int, sil, assign: Dict[str, int], labels: Dict[int, List[str]],
            points: List[Dict[str, Any]], skipped: Dict[str, str], note: str = "") -> Dict[str, Any]:
    sizes = Counter(assign.values())
    members: Dict[int, List[str]] = defaultdict(list)
    for u, c in assign.items():
        members[c].append(u)
    items = []
    for c in sorted(sizes):
        words = labels.get(c) or []
        items.append({"id": c, "label": ", ".join(words) or f"Cluster {c}", "terms": words, "size": sizes[c],
                      "examples": [{"url": u, "title": _title(site, u)} for u in members[c][:3]]})
    reasons = Counter(skipped.values())
    return {"method": method, "reason": "", "k": k, "silhouette": sil, "pages": len(assign), "clusters": items,
            "points": points, "points_total": len(assign), "not_checked": len(skipped),
            "not_checked_reasons": dict(reasons.most_common()), "assign": assign, "skipped": skipped,
            "note": note}


def _from_embeddings(site, entry: Dict[str, Any]) -> Dict[str, Any]:
    info = entry.get("clusters") if isinstance(entry.get("clusters"), dict) else {}
    pages = pages_of(site)
    results = entry.get("results") or {}
    nc = entry.get("not_checked") or {}
    assign: Dict[str, int] = {}
    skipped: Dict[str, str] = {}
    points = []
    for r in pages:
        rec = results.get(r.url)
        if isinstance(rec, dict) and rec.get("cluster"):
            assign[r.url] = int(rec["cluster"])
            if rec.get("x") is not None:
                points.append({"url": r.url, "x": rec["x"], "y": rec["y"], "cluster": int(rec["cluster"]),
                               "title": _first(r, "titles")})
        elif isinstance(rec, dict):
            skipped[r.url] = str(info.get("reason") or "Not grouped in the last embeddings run.")
        else:
            skipped[r.url] = str(nc.get(r.url) or "Not part of the last embeddings run (run it again).")
    if not assign:
        return dict(_empty(EMBEDDINGS, str(info.get("reason") or "The embeddings run grouped no pages.")),
                    not_checked=len(skipped), skipped=skipped,
                    not_checked_reasons=dict(Counter(skipped.values()).most_common()))
    labels = {int(it["id"]): list(it.get("terms") or []) for it in info.get("items") or [] if isinstance(it, dict)}
    out = _finish(site, EMBEDDINGS, int(info.get("k") or len(set(assign.values()))), info.get("silhouette"),
                  assign, labels, points, skipped, note=str(info.get("note") or ""))
    out["points_total"] = len(assign)
    return out


def _from_words(site) -> Dict[str, Any]:
    if not getattr(site, "crawl_finished", False):
        return _empty(WORDS, NOT_FINISHED)

    def build():
        started = time.time()
        wv = word_vectors(site)
        vectors = wv["vectors"]
        skipped = dict(wv["skipped"])
        urls = list(vectors)
        if len(urls) > CLUSTER_MAX_PAGES:
            for u in urls[CLUSTER_MAX_PAGES:]:
                skipped[u] = OVER_CAP.format(n=CLUSTER_MAX_PAGES)
            urls = urls[:CLUSTER_MAX_PAGES]
        res = cluster_vectors({u: _strongest(vectors[u]) for u in urls})
        if res.get("reason"):
            return dict(_empty(WORDS, res["reason"]), skipped=skipped, not_checked=len(skipped))
        assign = res["assign"]
        labels = label_clusters(assign, wv["terms"], wv["display"])
        shown = map_sample(assign)
        xy = project_points([vectors[u] for u in shown])
        by_url = getattr(site, "by_url", None) or {}
        points = [{"url": u, "x": x, "y": y, "cluster": assign[u], "title": _first(by_url[u], "titles")
                   if u in by_url else ""} for u, (x, y) in zip(shown, xy)]
        out = _finish(site, WORDS, res["k"], res["silhouette"], assign, labels, points, skipped)
        out["seconds"] = round(time.time() - started, 2)
        return out
    return _cached(("clusters", _words_fingerprint(site)), build)


def clusters(site, method: Optional[str] = None) -> Dict[str, Any]:
    """Topic clusters of the crawl: {"method", "reason" ("" when grouped), "k", "silhouette", "pages",
    "clusters": [{"id", "label", "terms", "size", "examples": [{"url", "title"}]}], "points": [{"url", "x", "y",
    "cluster", "title"}], "points_total", "not_checked", "not_checked_reasons", "assign", "skipped", "note"}.
    method None prefers the embeddings run's clusters and falls back to words."""
    memo = _memo(site)
    key = "clusters:" + str(method or "")
    if key in memo:
        return memo[key]
    entry = embeddings_entry(site)
    try:
        if method == EMBEDDINGS and entry is None:
            out = _empty(EMBEDDINGS, NO_RUN)
        elif method in (None, EMBEDDINGS) and entry is not None:
            out = _from_embeddings(site, entry)
        else:
            out = _from_words(site)
    except Exception as e:             # remembered, so the column does not retry the work for every row
        out = _empty(EMBEDDINGS if method == EMBEDDINGS or (method is None and entry is not None) else WORDS,
                     f"Topic clusters could not be worked out ({type(e).__name__}).")
    memo[key] = out
    return out


def public(result: Dict[str, Any]) -> Dict[str, Any]:
    """clusters() without the per-URL maps (GET /api/crawl/clusters)."""
    return {k: v for k, v in result.items() if k not in ("assign", "skipped")}


def cluster_of(site, url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """({"id", "label", "method"} or None, why it has none) for one URL."""
    c = clusters(site)
    cid = (c.get("assign") or {}).get(url)
    if cid:
        item = next((it for it in c["clusters"] if it["id"] == cid), None)
        return {"id": cid, "label": item["label"] if item else f"Cluster {cid}", "method": c["method"]}, ""
    return None, str((c.get("skipped") or {}).get(url) or c.get("reason") or "")


def topic_cluster(site, r) -> str:
    """The Content tab's Topic Cluster cell: the cluster's label, "Not checked: <why>" for an indexable page that
    was not grouped, "" for other rows."""
    from .tabs.internal import category_of, is_html_doc
    if not is_html_doc(r) or category_of(r) != "2xx" or not is_indexable(site, r):
        return ""
    found, why = cluster_of(site, r.url)
    if found:
        return found["label"]
    return "Not checked: " + (why or "not grouped")


# ---------------------------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------------------------
def _words_search(site, query: str, limit: int) -> Dict[str, Any]:
    wv = word_vectors(site)
    q = Counter(tokens(query))
    idf = wv["idf"]
    known = {t: c for t, c in q.items() if t in idf}
    out = {"method": WORDS, "query": query, "results": [], "note": ""}
    if not q:
        out["note"] = "Type a few words that describe what you are looking for."
        return out
    if not known:
        out["note"] = ("None of these words is among the main words of the crawled pages; run embeddings (AI) to "
                       "search by meaning.")
        return out
    qv = unit({t: (1.0 + math.log(c)) * idf[t] for t, c in known.items()})
    scored = [(dot(qv, v), u) for u, v in wv["vectors"].items()]
    scored = [(s, u) for s, u in scored if s > 0]
    scored.sort(key=lambda x: (-x[0], x[1]))
    out["results"] = [{"url": u, "score": round(s, 4), "title": _title(site, u)} for s, u in scored[:limit]]
    return out


def search(site, query: str, limit: int = 20, method: Optional[str] = None) -> Dict[str, Any]:
    """Pages ranked by how close they are to `query`: {"method", "query", "results": [{"url", "score", "title"}],
    "note"}. method None: by meaning when this crawl has an embeddings run whose page vectors are in memory and the
    AI answers, else by words (the note says why)."""
    query = " ".join(str(query or "").split())[:500]
    try:
        limit = max(1, min(int(limit or 20), SEARCH_MAX))
    except (TypeError, ValueError):
        limit = 20
    note = ""
    entry = embeddings_entry(site)
    if method != WORDS and query:
        if entry is None:
            note = NO_RUN if method == EMBEDDINGS else ""
        else:
            from ..ai import embeddings as E
            vecs, missing = E.vectors_for(entry, [r.url for r in pages_of(site)])
            if not vecs or missing > max(0, len(vecs) // 20):
                note = ("The page vectors of the last embeddings run are no longer in the server's memory (the server "
                        "restarted or the crawl was reopened): run embeddings again to search by meaning.")
            else:
                qv = E.embed_query(query, entry)
                if qv is None:
                    note = "Search by meaning is not available right now (" + E.last_note() + "); searched by words."
                else:
                    scored = sorted(((dot(qv, v), u) for u, v in vecs.items()), key=lambda x: (-x[0], x[1]))
                    return {"method": EMBEDDINGS, "query": query, "note": "",
                            "results": [{"url": u, "score": round(s, 4), "title": _title(site, u)}
                                        for s, u in scored[:limit]]}
    if method == EMBEDDINGS:
        return {"method": EMBEDDINGS, "query": query, "results": [], "note": note or "Type what you are looking for."}
    out = _words_search(site, query, limit)
    if note and not out["note"]:
        out["note"] = note
    elif note:
        out["note"] = note + " " + out["note"]
    return out


def semantic_details(site, url: str) -> Optional[Dict[str, Any]]:
    """URL details section "semantic": the page's topic cluster and its closest pages by meaning (embeddings) or
    by words; None for rows that are not indexable 2xx HTML pages."""
    from .tabs.internal import category_of, is_html_doc
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not is_html_doc(r) or category_of(r) != "2xx" or not is_indexable(site, r):
        return None
    found, why = cluster_of(site, url)
    out: Dict[str, Any] = {"cluster": found, "cluster_not_checked": "" if found else (why or "not grouped")}
    rec = ((embeddings_entry(site) or {}).get("results") or {}).get(url)
    if isinstance(rec, dict):
        out.update(method=EMBEDDINGS, similar=[{"url": u, "score": s} for u, s in rec.get("similar") or []],
                   relevance=rec.get("relevance"), low_relevance=bool(rec.get("low")))
    else:
        m = (getattr(site, "semantic", None) or {}).get(url)
        out.update(method=WORDS, similar=[{"url": m[0], "score": m[1]}] if m else [],
                   relevance=(getattr(site, "low_relevance", None) or {}).get(url),
                   low_relevance=url in (getattr(site, "low_relevance", None) or {}))
        nc = ((embeddings_entry(site) or {}).get("not_checked") or {}).get(url)
        if nc:
            out["embeddings_not_checked"] = nc
    return out


def _register() -> None:
    try:
        from .registry import register_details
        register_details("semantic", semantic_details)
    except Exception:
        pass


_register()
