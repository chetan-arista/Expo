"""
Site-wide analysis that runs after (or during) a crawl: inlinks, indexability, Link Score,
duplicate / near-duplicate content, sitemap orphans, crawl depth, broken bookmarks (links to #ids
that do not exist on the target page) and semantic similarity (a TF-IDF approximation, see
compute_semantic). build_site(crawler) returns the read-only Site object that every tab in
crawler/seo/tabs reads (see model.py for the contract).

Crawl Analysis steps (CrawlConfig.analysis): link_score, near_duplicates, sitemaps (orphans) and
semantic_similarity are skipped when turned off; site.analysis_skipped then says why, and the tabs that
need the step report "Not checked" (registry requirement "analysis:<step>"). Semantic similarity is
computed once the crawl has finished (it compares every page with every other one).

Near duplicates use the crawl's threshold (CrawlConfig.near_dup_threshold, 50-100 %, default 90): the LSH bands are
chosen for it (lsh_rows) and every candidate pair is re-checked against it; past NEAR_DUP_MAX_PAIRS candidate pairs
the step is "Not checked" instead of reporting part of the pairs (P13). site.inlinks are hyperlinks (with their rel);
every other way a URL points at another (canonical, hreflang, rel next / prev, refreshes, JavaScript redirect, mobile
alternate, form action, other link elements) is in site.other_inlinks, kept apart so Link Score and the inlink counts
stay hyperlink-based (P6).

Everything here is defensive: a result without PageData, a missing attribute or a malformed URL
never raises, it simply contributes nothing.
"""
import hashlib
import heapq
import math
import struct
import weakref
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import islice
from operator import eq
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple
from urllib.parse import unquote

from ..issues import classify_result
from ..url_utils import normalize_url
from .model import EMPTY_PAGE

DAMPING = 0.85
PAGERANK_MIN_ITER = 10
PAGERANK_MAX_ITER = 50
PAGERANK_TOL = 1e-9

NEAR_DUP_THRESHOLD = 90.0          # Screaming Frog default similarity threshold (%); CrawlConfig.near_dup_threshold
NEAR_DUP_MIN_THRESHOLD = 50.0      # lowest threshold a crawl may set (P13)
NEAR_DUP_MAX_PER_URL = 50          # matches listed per URL (closest first); Site.near_duplicate_counts is exact
NEAR_DUP_MAX_PAIRS = 2_000_000     # candidate page pairs compared at most; past it the step is "Not checked" (P13)
NEAR_DUP_RECALL = 0.999            # LSH bands are chosen so a pair exactly at the threshold is a candidate this often
# Near duplicates compare the pages' sets of word 3-shingles (Jaccard similarity: shared shingles / all
# shingles of the two pages), estimated like Screaming Frog with minhash (PageData "minhash", see
# minhash_signature): one-permutation hashing into MINHASH_BINS bins, empty bins filled by optimal
# densification, similarity = share of equal bins. Candidate pairs come from LSH bands of MINHASH_ROWS bins.
MINHASH_BINS = 128
MINHASH_ROWS = 8                   # bins per LSH band at 90 %: a 90 % pair is a candidate with p > 0.9998, a 50 % one
                                   # 6 %; lower thresholds use fewer rows per band (lsh_rows)
MINHASH_HEX = 8                    # hex digits per bin (32-bit values)
# Legacy fallback for crawls whose pages carry no minhash (saved before it existed): the 64-bit simhash.
# Its Hamming distance d is turned into a Jaccard estimate (simhash_jaccard); d <= 6 reaches 90 %.
SIMHASH_BITS = 64
NEAR_DUP_MAX_DISTANCE = 6

DUPLICATE_KINDS = ("title", "meta_description", "h1", "h2", "content_hash")

# Broken bookmarks: fragments that are script hooks, "scroll to top", app routes (#/, #!) or state (#page=2,
# #:~:text=) rather than element ids; the extractor keeps at most MAX_PAGE_IDS ids per page
BOOKMARK_SKIP = {"", "!", "0", "top", "#", "_", "-"}
MAX_PAGE_IDS = 5000
MAX_BOOKMARKS_PER_URL = 200

# Semantic similarity (TF-IDF approximation of Screaming Frog's embedding-based filters)
SEMANTIC_SIMILAR_MIN = 0.80        # cosine of two pages' TF-IDF vectors at or above this = semantically similar
SEMANTIC_MIN_TERMS = 8             # pages with fewer distinct content words are not compared
SEMANTIC_MIN_PAGES = 3
SEMANTIC_TOP_TERMS = 20            # strongest terms per page used to find candidate pairs
SEMANTIC_MAX_CANDIDATES = 60       # pages compared exactly with each page (most shared strong terms first)
SEMANTIC_POSTING_CAP = 400         # a term on more pages than this is too common to find candidates with
SEMANTIC_MAX_PAGES = 20000         # pages compared (crawl order); the rest are reported as not compared
LOW_RELEVANCE_MIN_PAGES = 10       # the site needs this many comparable pages for a "site topic"
LOW_RELEVANCE_FACTOR = 0.25        # below this share of the median page-to-site similarity = low relevance
LOW_RELEVANCE_FLOOR = 0.03         # ... and always below this absolute cosine
ANALYSIS_STEPS = ("link_score", "sitemaps", "near_duplicates", "pagination", "hreflang", "semantic_similarity")
FINISHED = ("completed", "stopped", "auth_failed")


@dataclass
class Site:
    """Everything the tabs need about the crawl, computed once (see model.py "Site")."""
    results: List[Any] = field(default_factory=list)
    by_url: Dict[str, Any] = field(default_factory=dict)
    pages: List[Any] = field(default_factory=list)
    externals: List[Any] = field(default_factory=list)
    start_url: str = ""
    base_domain: str = ""
    config: Any = None
    inlinks: Dict[str, List[dict]] = field(default_factory=dict)
    unique_inlinks: Dict[str, int] = field(default_factory=dict)
    outlinks: Dict[str, List[dict]] = field(default_factory=dict)
    link_score: Dict[str, int] = field(default_factory=dict)
    indexable: Dict[str, bool] = field(default_factory=dict)
    indexability_status: Dict[str, str] = field(default_factory=dict)
    duplicates: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    near_duplicates: Dict[str, List[Tuple[str, float]]] = field(default_factory=dict)   # closest first, capped
    near_duplicate_counts: Dict[str, int] = field(default_factory=dict)   # url -> number of near duplicates
    sitemap_urls: Dict[str, List[str]] = field(default_factory=dict)
    orphans: List[str] = field(default_factory=list)
    crawl_depth: Dict[str, int] = field(default_factory=dict)
    # Post-crawl checks (crawler/seo/postcrawl.py stores them on the crawler; JSON-ready dicts, None = did not run)
    bot_report: Optional[dict] = None       # bot_check.run(): pages as a browser vs search / AI bots
    robots_audit: Optional[dict] = None     # robots_audit.run(): robots.txt AI policy, Content-Signal, Sitemap lines
    trap_probe: Optional[dict] = None       # trap_probe.run(): random-URL probes (soft 404 server, URL traps)
    crawl_budget: Optional[dict] = None     # crawl_budget.analyse(site), filled on first use by crawl_budget.get(site)
    # Build 3
    broken_bookmarks: Dict[str, List[dict]] = field(default_factory=dict)   # target url -> [{"fragment",
                                            # "source", "line"}] links to a #fragment no element of it carries
    semantic: Dict[str, Tuple[str, float]] = field(default_factory=dict)  # url -> (closest page, cosine)
    low_relevance: Dict[str, float] = field(default_factory=dict)  # url -> cosine to the rest of the site
    semantic_info: Dict[str, Any] = field(default_factory=dict)    # pages compared, thresholds, median
    sitemap_lastmod: Optional[Dict[str, str]] = None   # url -> <lastmod> as written (None = not recorded)
    sitemap_stats: Optional[Dict[str, dict]] = None    # sitemap url -> {"urls", "size_bytes"} when recorded
    analysis_skipped: Dict[str, str] = field(default_factory=dict)  # Crawl Analysis step -> why it did not run
    near_duplicate_method: str = ""    # "minhash" | "simhash" (legacy crawls) | "" (not run / no text)
    near_duplicate_info: Dict[str, Any] = field(default_factory=dict)   # {"threshold", "rows", "pairs", "capped",
                                       # "method"} of the near-duplicate step (P13; empty when it did not run)
    integrations: Dict[str, dict] = field(default_factory=dict)     # name -> {"connected", "reason", ...}
    crawl_finished: bool = False
    # Build 4 (PLAN4_build.md 10.5; empty until the task that fills them, see model.py)
    other_inlinks: Dict[str, List[dict]] = field(default_factory=dict)   # url -> [{"source", "type", "rel"}] (P6)
    sitemap_hreflang: Dict[str, List[dict]] = field(default_factory=dict)  # url -> [{"lang", "url"}] (P8)
    sitemap_index_full: Optional[dict] = None    # {"limit", "file"} when the sitemap URL index was full (P9)
    sitemaps_not_read: List[str] = field(default_factory=list)   # sitemap files listed but not read (P9)
    sitemap_files_limit: Optional[int] = None    # most sitemap files the crawl read (None: crawled before P9, 25)
    carbon: Dict[str, dict] = field(default_factory=dict)   # url -> {"bytes", "grams", "rating", ...} (P18)
    carbon_info: Dict[str, Any] = field(default_factory=dict)
    llms_txt: Optional[dict] = None              # llms.txt check with the crawl's verdict per link (P19)
    preferences: Dict[str, Any] = field(default_factory=dict)  # CrawlConfig.preferences (registry.pref)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------
def _seo(r) -> dict:
    return getattr(r, "seo", None) or EMPTY_PAGE


def _first(lst, default=""):
    try:
        return lst[0] if lst else default
    except Exception:
        return default


def _norm(url: Any) -> str:
    """Normalized form of a URL for comparisons ("" when it cannot be parsed)."""
    if not url or not isinstance(url, str) or url == "-":
        return ""
    try:
        return normalize_url(url) or url.strip()
    except Exception:
        return url.strip()


def _category(r) -> str:
    try:
        return classify_result(r)
    except Exception:
        return "error"


def _is_html_page(r) -> bool:
    return bool(_seo(r).get("is_html"))


def _page_nofollow(p: dict) -> bool:
    d = p.get("directives") or []
    return "nofollow" in d or "none" in d


# ---------------------------------------------------------------------------------------------
# Indexability
# ---------------------------------------------------------------------------------------------
def indexability_of(r) -> Tuple[bool, str]:
    """(indexable, status) the way Screaming Frog reports it. The HTTP category decides first,
    so the status always agrees with the URL's category; then noindex, then a canonical
    pointing elsewhere. Non-HTML files that load are indexable unless an X-Robots-Tag says no."""
    cat = _category(r)
    if cat == "blocked":
        return False, "Blocked by robots.txt"
    if cat == "error":
        return False, "No Response"
    if cat == "5xx":
        return False, "Server Error"
    if cat == "4xx":
        return False, "Client Error"
    if cat == "3xx":
        return False, "Redirected"
    if getattr(r, "status_code", None) != 200:
        return False, "Non-200"
    p = _seo(r)
    d = p.get("directives") or []
    if "noindex" in d or "none" in d:
        return False, "noindex"
    canonical = _norm(_first(p.get("canonicals") or [])) or _norm(p.get("http_canonical") or "")
    if canonical and canonical != _norm(getattr(r, "url", "")):
        return False, "Canonicalised"
    return True, ""


# ---------------------------------------------------------------------------------------------
# Link rel and the other inlink types (P6)
# ---------------------------------------------------------------------------------------------
# type id -> the name the Inlinks / Outlinks panes and the All Inlinks / All Outlinks exports show in "Type"
OTHER_INLINK_TYPES: Dict[str, str] = {
    "canonical": "Canonical", "hreflang": "Hreflang", "next": "Rel Next", "prev": "Rel Prev",
    "meta_refresh": "Meta Refresh", "http_refresh": "HTTP Refresh", "js_redirect": "JavaScript Redirect",
    "mobile_alternate": "Mobile Alternate", "form_action": "Form Action", "link_element": "Link Element",
}
HEADER_REGION = "HTTP response header"      # where a Link / Refresh header entry sits (Link Position)
HEAD_REGION = "Page <head>"


def link_rel(link: Any) -> str:
    """A hyperlink's rel values as written, space-separated and lower case ("nofollow sponsored"), "" when none."""
    rel = link.get("rel") if isinstance(link, dict) else None
    if isinstance(rel, (list, tuple)):
        return " ".join(str(v).strip().lower() for v in rel if str(v).strip())
    return " ".join(str(rel or "").lower().split())


def _rendered_redirect(p: dict) -> str:
    """Normalized target a rendered page's script sent the browser to ("" when none or not rendered)."""
    rendered = p.get("rendered")
    if not isinstance(rendered, dict) or not rendered.get("js_redirect"):
        return ""
    return _norm(str(rendered.get("js_redirect_url") or ""))


def other_links_of(r) -> List[Tuple[str, dict]]:
    """[(target, entry)] for every way one crawled URL points at another besides hyperlinks, resources and HTTP
    redirects (the engine's _followed_targets minus those): canonical, hreflang, rel next / prev (HTML <link> and
    the Link header), the AMP link element (generic type "Link element"), mobile alternate, meta refresh, Refresh
    header, rendered JavaScript redirect and form action. entry = {"source", "type", "rel", "via" (html | header |
    script), "region"} plus "lang" (hreflang) and "method" (forms). A URL pointing at itself (a self-referencing
    canonical or hreflang) is left out: it is not a way to reach the page."""
    p = _seo(r)
    src = str(getattr(r, "url", "") or "")
    if not src:
        return []
    own = {src, _norm(p.get("final_url") or "")}
    out: List[Tuple[str, dict]] = []
    seen: Set[tuple] = set()

    def add(target: Any, kind: str, rel: str, via: str, region: str, **extra: Any) -> None:
        t = _norm(target)
        if not t or t in own:
            return
        key = (t, kind, via, extra.get("lang"))
        if key in seen:
            return
        seen.add(key)
        entry = {"source": src, "type": kind, "rel": rel, "via": via, "region": region}
        entry.update(extra)
        out.append((t, entry))

    body = "Page body"
    canon_region = body if p.get("canonical_outside_head") else HEAD_REGION
    for u in p.get("canonicals") or ():
        add(u, "canonical", "canonical", "html", canon_region)
    add(p.get("http_canonical"), "canonical", "canonical", "header", HEADER_REGION)
    hl_region = body if p.get("hreflang_outside_head") else HEAD_REGION
    for h in p.get("hreflang") or ():
        if isinstance(h, dict):
            add(h.get("url"), "hreflang", "alternate", "html", hl_region, lang=str(h.get("lang") or ""))
    for h in p.get("http_hreflang") or ():
        if isinstance(h, dict):
            add(h.get("url"), "hreflang", "alternate", "header", HEADER_REGION, lang=str(h.get("lang") or ""))
    for u in p.get("rel_next_all") or ():
        add(u, "next", "next", "html", HEAD_REGION)
    add(p.get("http_rel_next"), "next", "next", "header", HEADER_REGION)
    for u in p.get("rel_prev_all") or ():
        add(u, "prev", "prev", "html", HEAD_REGION)
    add(p.get("http_rel_prev"), "prev", "prev", "header", HEADER_REGION)
    add(p.get("amphtml"), "link_element", "amphtml", "html", HEAD_REGION)
    add(p.get("mobile_alternate"), "mobile_alternate", "alternate", "html", HEAD_REGION)
    add(p.get("meta_refresh_url"), "meta_refresh", "", "html", HEAD_REGION)
    add(p.get("http_refresh_url"), "http_refresh", "", "header", HEADER_REGION)
    add(_rendered_redirect(p), "js_redirect", "", "script", "JavaScript")
    for f in p.get("forms") or ():
        if isinstance(f, dict) and f.get("action"):
            add(f.get("action"), "form_action", "", "html", "Form",
                method=str(f.get("method") or "get").lower() or "get")
    return out


def compute_other_inlinks(results: Iterable[Any]) -> Dict[str, List[dict]]:
    """url -> [entry] (other_links_of) from every crawled URL, in crawl order: the inlinks that are not hyperlinks,
    resources or HTTP redirects (Site.other_inlinks). Keyed by the normalized target, crawled or not, internal or
    on another host."""
    out: Dict[str, List[dict]] = defaultdict(list)
    for r in results:
        try:
            for target, entry in other_links_of(r):
                out[target].append(entry)
        except Exception:
            continue
    return dict(out)


# ---------------------------------------------------------------------------------------------
# Link Score (internal PageRank)
# ---------------------------------------------------------------------------------------------
def _redirect_target(r) -> str:
    dest = getattr(r, "redirect_destination", None)
    if dest and dest != "-":
        return _norm(dest)
    final = _seo(r).get("final_url") or ""
    if final and _norm(final) != _norm(getattr(r, "url", "")):
        return _norm(final)
    return ""


def compute_link_score(by_url: Dict[str, Any]) -> Dict[str, int]:
    """PageRank over followed internal links between crawled URLs (damping 0.85), then scaled to
    0..100 on a log scale so the strongest URL is 100 and a URL nobody links to is 0.
    HTML pages pass score through their followed links; redirects pass it all to their target."""
    urls = list(by_url.keys())
    n = len(urls)
    if n == 0:
        return {}
    idx = {u: i for i, u in enumerate(urls)}
    outs: List[List[int]] = [[] for _ in range(n)]
    for u, r in by_url.items():
        i = idx[u]
        cat = _category(r)
        if cat == "3xx":
            t = idx.get(_redirect_target(r))
            if t is not None and t != i:
                outs[i] = [t]
            continue
        p = _seo(r)
        if not p.get("is_html") or _page_nofollow(p):
            continue
        seen = set()
        for link in p.get("links") or ():
            try:
                if not link.get("internal") or link.get("nofollow"):
                    continue
                j = idx.get(link.get("url") or "")
                if j is None:
                    j = idx.get(_norm(link.get("url")))
                if j is not None and j != i:
                    seen.add(j)
            except Exception:
                continue
        outs[i] = list(seen)

    ins: List[List[int]] = [[] for _ in range(n)]
    for i, o in enumerate(outs):
        for j in o:
            ins[j].append(i)
    outdeg = [len(o) for o in outs]
    dangling = [i for i in range(n) if outdeg[i] == 0]

    pr = [1.0 / n] * n
    base_const = (1.0 - DAMPING) / n
    for it in range(PAGERANK_MAX_ITER):
        contrib = [pr[i] / outdeg[i] if outdeg[i] else 0.0 for i in range(n)]
        dangling_mass = sum(pr[i] for i in dangling)
        base = base_const + DAMPING * dangling_mass / n
        get = contrib.__getitem__
        new = [base + DAMPING * sum(map(get, ins[j])) if ins[j] else base for j in range(n)]
        delta = sum(abs(a - b) for a, b in zip(new, pr))
        pr = new
        if it + 1 >= PAGERANK_MIN_ITER and delta < PAGERANK_TOL:
            break

    floor = min(pr)
    top = max(pr)
    out: Dict[str, int] = {}
    if top <= floor * (1 + 1e-9):
        # Every URL has the same score (no links, or a perfect cycle).
        has_links = any(outdeg)
        return {u: (100 if has_links else 0) for u in urls}
    span = math.log(top / floor)
    for u, i in idx.items():
        v = math.log(pr[i] / floor) / span * 100 if pr[i] > floor else 0.0
        out[u] = max(0, min(100, int(round(v))))
    return out


# ---------------------------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------------------------
def _dup_value(kind: str, p: dict) -> str:
    if kind == "title":
        v = _first(p.get("titles") or [])
    elif kind == "meta_description":
        v = _first(p.get("meta_descriptions") or [])
    elif kind == "h1":
        v = _first(p.get("h1") or [])
    elif kind == "h2":
        v = _first(p.get("h2") or [])
    else:
        v = p.get("content_hash") or ""
    return v.strip() if isinstance(v, str) else ""


def compute_duplicates(pages: Iterable[Any], indexable: Dict[str, bool]) -> Dict[str, Dict[str, List[str]]]:
    """kind -> value -> urls for exact duplicates among indexable pages (groups of 2+, no empties)."""
    groups: Dict[str, Dict[str, List[str]]] = {k: defaultdict(list) for k in DUPLICATE_KINDS}
    for r in pages:
        url = getattr(r, "url", "")
        if not indexable.get(url):
            continue
        p = _seo(r)
        for kind in DUPLICATE_KINDS:
            v = _dup_value(kind, p)
            if v:
                groups[kind][v].append(url)
    return {k: {v: us for v, us in g.items() if len(us) > 1} for k, g in groups.items()}


def _bands(bits: int, nbands: int) -> List[Tuple[int, int]]:
    """Split `bits` into `nbands` contiguous (shift, mask) bands of near-equal width."""
    out, shift = [], 0
    for b in range(nbands):
        width = bits // nbands + (1 if b < bits % nbands else 0)
        out.append((shift, (1 << width) - 1))
        shift += width
    return out


def _donor_order(bins: int) -> List[Tuple[int, ...]]:
    """For each bin, the other bins in a fixed pseudo-random order (md5 of "bin:other"): an empty bin takes the
    value of the first non-empty bin in its order (optimal densification). Fixed for good, so signatures of
    different crawls stay comparable."""
    out = []
    for j in range(bins):
        others = [b for b in range(bins) if b != j]
        others.sort(key=lambda b: hashlib.md5(f"{j}:{b}".encode("ascii")).digest())
        out.append(tuple(others))
    return out


_DONORS = _donor_order(MINHASH_BINS)
_MINHASH_STRUCT = struct.Struct(f">{MINHASH_BINS}I")
_LANE_LOW = sum(1 << (32 * i) for i in range(MINHASH_BINS))   # lowest bit of every 32-bit bin of a signature int


def _unequal_bins(x: int) -> int:
    """Number of non-zero 32-bit bins of x (the XOR of two signatures as ints): each bin's bits are folded into
    its lowest bit (the folds never reach the lowest bit from a neighbouring bin)."""
    x |= x >> 16
    x |= x >> 8
    x |= x >> 4
    x |= x >> 2
    x |= x >> 1
    return (x & _LANE_LOW).bit_count()


def shingles_of(tokens: List[str]) -> Set[str]:
    """The set of word 3-shingles of lowercased words (one shingle of all words when there are fewer than 3)."""
    if not tokens:
        return set()
    if len(tokens) < 3:
        return {" ".join(tokens)}
    return {" ".join(t) for t in zip(tokens, tokens[1:], tokens[2:])}


def minhash_signature(tokens: List[str]) -> str:
    """Minhash signature of a page's word 3-shingle set (tokens = extract.word_tokens of the content area), as
    MINHASH_BINS x 8 hex digits; "" when there is no text. One md5 per distinct shingle: its low bits pick a bin,
    its top 32 bits are the value, each bin keeps its smallest value (one-permutation hashing) and an empty bin
    copies its first non-empty donor bin (optimal densification). The share of equal bins of two signatures
    estimates the Jaccard similarity of the two shingle sets (minhash_similarity)."""
    shingles = shingles_of(tokens)
    if not shingles:
        return ""
    nbins = MINHASH_BINS
    low = nbins - 1
    bins: List[Optional[int]] = [None] * nbins
    md5 = hashlib.md5
    for sh in shingles:
        h = int.from_bytes(md5(sh.encode("utf-8")).digest()[:8], "big")
        b = h & low
        v = h >> 32
        cur = bins[b]
        if cur is None or v < cur:
            bins[b] = v
    if None in bins:
        own = list(bins)
        for j in range(nbins):
            if own[j] is None:
                for d in _DONORS[j]:
                    if own[d] is not None:
                        bins[j] = own[d]
                        break
    return _MINHASH_STRUCT.pack(*bins).hex()


def minhash_values(sig: Any) -> Optional[Tuple[int, ...]]:
    """A minhash_signature() as a tuple of MINHASH_BINS ints, or None when it is missing or malformed."""
    if not isinstance(sig, str) or len(sig) != MINHASH_BINS * MINHASH_HEX:
        return None
    try:
        return _MINHASH_STRUCT.unpack(bytes.fromhex(sig))
    except (ValueError, struct.error):
        return None


def _minhash_pct(matches: int) -> float:
    return round(matches / MINHASH_BINS * 100, 1)


def minhash_similarity(a: Any, b: Any) -> float:
    """Estimated Jaccard similarity (0..100) of two pages' shingle sets from their minhash signatures (hex
    strings or minhash_values tuples); 0.0 when either is missing."""
    va = a if isinstance(a, tuple) else minhash_values(a)
    vb = b if isinstance(b, tuple) else minhash_values(b)
    if not va or not vb or len(va) != len(vb):
        return 0.0
    return _minhash_pct(sum(map(eq, va, vb)))


def simhash_jaccard(distance: int) -> float:
    """Legacy simhash: the Jaccard similarity (0..100) a Hamming distance of `distance` bits stands for. A bit
    differs with probability angle / pi of the two shingle vectors and, for two sets of about the same size,
    cosine = 2J / (1 + J), so J = cos / (2 - cos): 0 bits = 100 %, 6 bits = 91.7 %, 7 bits = 89.0 %, 32 bits
    (unrelated text) = 0 %."""
    c = math.cos(math.pi * max(0, min(SIMHASH_BITS, distance)) / SIMHASH_BITS)
    return round(c / (2 - c) * 100, 1) if c > 1e-9 else 0.0


class _NearSink:
    """Near-duplicate pairs of fingerprints (index n, index m, pct) kept as exact counts plus the
    NEAR_DUP_MAX_PER_URL closest neighbours of each fingerprint: a family of thousands of alike pages has millions
    of pairs, and keeping all of them took gigabytes."""

    def __init__(self, sizes: List[int]):
        self.sizes = sizes                                          # fingerprint index -> URLs that have it
        self.count: Dict[int, int] = defaultdict(int)               # index -> URLs of its near fingerprints
        self.best: Dict[int, List[Tuple[float, int]]] = defaultdict(list)   # index -> min-heap (pct, -other)

    def add(self, n: int, m: int, pct: float) -> None:
        self.count[n] += self.sizes[m]
        self.count[m] += self.sizes[n]
        self._keep(self.best[n], pct, m)
        self._keep(self.best[m], pct, n)

    @staticmethod
    def _keep(heap: List[Tuple[float, int]], pct: float, other: int) -> None:
        item = (pct, -other)                  # equal similarity: the page found first wins
        if len(heap) < NEAR_DUP_MAX_PER_URL:
            heapq.heappush(heap, item)
        elif item > heap[0]:
            heapq.heapreplace(heap, item)

    def closest(self, n: int) -> List[Tuple[int, float]]:
        """[(other index, pct)], closest first."""
        return [(-neg, pct) for pct, neg in sorted(self.best.get(n, ()), reverse=True)]


def _band_pairs(nitems: int, nbands: int, band_key: Callable[[int, int], Any]) -> Iterator[Tuple[int, Any]]:
    """LSH candidates: yields (n, items m > n to compare with n) so that every pair of items sharing at least one
    band comes up exactly once. Bands are bucketed one at a time; a pair sharing several bands is compared in
    the first one only, which a bucket finds by grouping its members by the earlier bands (keeping a set of
    every pair compared would grow with the square of a family of alike pages)."""
    for b in range(nbands):
        buckets: Dict[Any, List[int]] = defaultdict(list)
        for n in range(nitems):
            buckets[band_key(b, n)].append(n)
        for members in buckets.values():
            size = len(members)
            if size < 2:
                continue
            earlier: List[Dict[Any, Set[int]]] = []
            for j in range(b):
                g: Dict[Any, Set[int]] = defaultdict(set)
                for n in members:
                    g[band_key(j, n)].add(n)
                earlier.append(g)
            for i in range(size - 1):
                n = members[i]
                seen: Optional[Set[int]] = None
                for j, g in enumerate(earlier):
                    s = g[band_key(j, n)]
                    if len(s) == size:        # n shares band j with the whole bucket: compared there already
                        seen = s
                        break
                    if len(s) > 1:
                        seen = s if seen is None else seen | s
                if seen is None:
                    yield n, members[i + 1:]
                elif len(seen) < size:
                    yield n, set(members[i + 1:]).difference(seen)


def _near_dup_lists(groups: Dict[Any, List[Tuple[str, str]]], keys: List[Any], sink: _NearSink
                    ) -> Tuple[Dict[str, List[Tuple[str, float]]], Dict[str, int]]:
    """(url -> [(other_url, pct)] closest first, at most NEAR_DUP_MAX_PER_URL; url -> the exact number of its near
    duplicates) from pages grouped by identical fingerprint (groups[keys[i]]) and the fingerprints' near
    neighbours (sink). Pages with the same content_hash (exact duplicates, which always share a fingerprint)
    are left out of each other's lists and counts."""
    cap = NEAR_DUP_MAX_PER_URL
    out: Dict[str, List[Tuple[str, float]]] = {}
    counts: Dict[str, int] = {}
    for i, key in enumerate(keys):
        members = groups[key]
        near_n = sink.count.get(i, 0)
        if len(members) < 2 and not near_n:
            continue
        tail = (list(islice(((u2, pct) for m, pct in sink.closest(i) for u2, _c in groups[keys[m]]), cap))
                if near_n else [])
        per_hash = Counter(c for _u, c in members)
        for url, chash in members:
            # Same fingerprint, different text: 100 % similar but not an exact duplicate
            same_n = len(members) - 1 - ((per_hash[chash] - 1) if chash else 0)
            if same_n:
                same = list(islice(((u2, 100.0) for u2, c2 in members
                                    if u2 != url and not (chash and c2 == chash)), cap))
                found = (same + tail)[:cap]
            else:
                found = tail                   # shared by the group's pages (nothing changes it afterwards)
            if found:
                out[url] = found
                counts[url] = same_n + near_n
    return out, counts


def near_threshold(value: Any) -> float:
    """A near-duplicate threshold (%) from a crawl's config or a number: clamped to 50-100, 90 when unset."""
    v = getattr(value, "near_dup_threshold", value)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return NEAR_DUP_THRESHOLD
    if v != v:                      # NaN
        return NEAR_DUP_THRESHOLD
    return max(NEAR_DUP_MIN_THRESHOLD, min(100.0, v))


def lsh_rows(threshold: float) -> int:
    """Minhash bins per LSH band for a threshold: the most rows (fewest candidate pairs to compare) with which a
    pair exactly at the threshold still shares a band with probability NEAR_DUP_RECALL or more (1 - (1 - s^r)^b,
    b = MINHASH_BINS // r bands). 90 % and above give MINHASH_ROWS (8); 85 % gives 7; 50 % gives 2. Every candidate
    is re-checked against the threshold, so the rows only decide which pairs are compared, never which are kept."""
    s = max(0.0, min(1.0, threshold / 100.0))
    for r in range(MINHASH_ROWS, 0, -1):
        if 1.0 - (1.0 - s ** r) ** (MINHASH_BINS // r) >= NEAR_DUP_RECALL:
            return r
    return 1


NearLists = Tuple[Dict[str, List[Tuple[str, float]]], Dict[str, int], int]   # (lists, counts, pairs compared)


class _PairCap(Exception):
    """More candidate pairs than NEAR_DUP_MAX_PAIRS: the near-duplicate step stops (Not checked)."""


def _near_minhash(groups: Dict[str, List[Tuple[str, str]]], threshold: float = NEAR_DUP_THRESHOLD,
                  max_pairs: int = NEAR_DUP_MAX_PAIRS) -> "NearLists":
    """Near duplicates among pages with a minhash signature: signatures sharing an LSH band (lsh_rows(threshold)
    bins) are compared bin by bin, and a pair is kept at `threshold` % equal bins or more. A pair whose signatures
    differ in more bits than `need` equal bins allow (32 per unequal bin at most) is dropped with one XOR, so pages
    that only share a template cost almost nothing. Returns (lists, counts, candidate pairs compared); raises
    _PairCap past `max_pairs` candidate pairs (0 = no cap)."""
    rows = lsh_rows(threshold)
    width = rows * MINHASH_HEX
    nbands = MINHASH_BINS // rows
    need = math.ceil(threshold / 100 * MINHASH_BINS)
    nbins = MINHASH_BINS
    max_bits = 32 * (nbins - need)
    pct = [_minhash_pct(m) for m in range(nbins + 1)]
    sigs = list(groups.keys())
    ints = [int(s, 16) for s in sigs]
    sink = _NearSink([len(groups[s]) for s in sigs])
    add = sink.add
    compared = 0
    for n, todo in _band_pairs(len(sigs), nbands, lambda b, n: sigs[n][b * width:(b + 1) * width]):
        ia = ints[n]
        if max_pairs:
            compared += len(todo)
            if compared > max_pairs:
                raise _PairCap()
        for m in todo:
            x = ia ^ ints[m]
            if x.bit_count() > max_bits:
                continue
            matches = nbins - _unequal_bins(x)
            if matches >= need:
                add(n, m, pct[matches])
    near, counts = _near_dup_lists(groups, sigs, sink)
    return near, counts, compared


def simhash_max_distance(threshold: float) -> int:
    """Most differing simhash bits whose Jaccard estimate (simhash_jaccard) still reaches the threshold (90 % -> 6)."""
    k = 0
    while k < SIMHASH_BITS and simhash_jaccard(k + 1) >= threshold:
        k += 1
    return k


def _near_simhash(groups: Dict[int, List[Tuple[str, str]]], threshold: float = NEAR_DUP_THRESHOLD,
                  max_pairs: int = NEAR_DUP_MAX_PAIRS) -> "NearLists":
    """Legacy near duplicates from 64-bit simhashes (crawls saved before minhash): pigeonhole banding (with a
    distance <= k at least one of k+1 bands matches exactly, so only simhashes sharing a band are compared).
    Raises _PairCap past `max_pairs` candidate pairs (0 = no cap)."""
    k = NEAR_DUP_MAX_DISTANCE if threshold == NEAR_DUP_THRESHOLD else simhash_max_distance(threshold)
    hashes = list(groups.keys())
    bands = _bands(SIMHASH_BITS, k + 1)
    sink = _NearSink([len(groups[h]) for h in hashes])
    compared = 0
    for n, todo in _band_pairs(len(hashes), len(bands),
                               lambda b, n: (hashes[n] >> bands[b][0]) & bands[b][1]):
        a = hashes[n]
        if max_pairs:
            compared += len(todo)
            if compared > max_pairs:
                raise _PairCap()
        for m in todo:
            d = (a ^ hashes[m]).bit_count()
            if d <= k:
                sink.add(n, m, simhash_jaccard(d))
    near, counts = _near_dup_lists(groups, hashes, sink)
    return near, counts, compared


def near_duplicate_run(pages: Iterable[Any], indexable: Dict[str, bool], threshold: float = NEAR_DUP_THRESHOLD,
                       max_pairs: int = NEAR_DUP_MAX_PAIRS) -> Dict[str, Any]:
    """near_duplicate_index at a threshold, with what the run did: {"near", "counts", "method", "threshold",
    "rows" (LSH rows per band), "pairs" (candidate pairs compared), "capped" (True: more than max_pairs candidate
    pairs, nothing is reported), "reason" (why the step is Not checked, "" when it ran)}."""
    threshold = near_threshold(threshold)
    info: Dict[str, Any] = {"near": {}, "counts": {}, "method": "", "threshold": threshold,
                            "rows": lsh_rows(threshold), "pairs": 0, "capped": False, "reason": ""}
    by_sig: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    by_hash: Dict[int, List[Tuple[str, str]]] = defaultdict(list)
    legacy = False
    for r in pages:
        url = getattr(r, "url", "")
        if not indexable.get(url):
            continue
        p = _seo(r)
        chash = p.get("content_hash") or ""
        h = p.get("simhash") or 0
        has_simhash = isinstance(h, int) and h > 0
        sig = p.get("minhash") or ""
        if minhash_values(sig) is not None:
            by_sig[sig].append((url, chash))
        elif has_simhash:
            legacy = True
        if has_simhash:
            by_hash[h & ((1 << SIMHASH_BITS) - 1)].append((url, chash))
    try:
        if legacy:
            info["near"], info["counts"], info["pairs"] = _near_simhash(by_hash, threshold, max_pairs)
            info["method"] = "simhash"
        elif by_sig:
            info["near"], info["counts"], info["pairs"] = _near_minhash(by_sig, threshold, max_pairs)
            info["method"] = "minhash"
    except _PairCap:
        info.update(near={}, counts={}, pairs=max_pairs, capped=True, method="minhash" if not legacy else "simhash",
                    reason=(f"Near duplicates were not worked out: more than {max_pairs:,} pairs of pages looked "
                            f"alike at the {threshold:g}% threshold, too many to compare. Raise the threshold "
                            "(Configuration > Content) or narrow the content area, then run the crawl again."))
    return info


def near_duplicate_index(pages: Iterable[Any], indexable: Dict[str, bool], threshold: float = NEAR_DUP_THRESHOLD
                         ) -> Tuple[Dict[str, List[Tuple[str, float]]], Dict[str, int], str]:
    """(url -> [(other_url, similarity_pct)] closest first, url -> number of near duplicates, method) for
    indexable pages whose content-area text is at least `threshold` % alike (CrawlConfig.near_dup_threshold, 90 by
    default): the Jaccard similarity of their word 3-shingle sets, estimated with minhash (PageData "minhash"). Each
    list keeps the NEAR_DUP_MAX_PER_URL closest pages; the count is exact. Exact duplicates (same content_hash) are
    left out; compute_duplicates reports them. method is "minhash"; "simhash" when a page with text has no minhash
    signature (a crawl saved before signatures existed: every page is then compared by simhash); "" when no
    indexable page has text. No cap on candidate pairs here (near_duplicate_run has one)."""
    info = near_duplicate_run(pages, indexable, threshold, max_pairs=0)
    return info["near"], info["counts"], info["method"]


def near_duplicate_groups(pages: Iterable[Any], indexable: Dict[str, bool]
                          ) -> Tuple[Dict[str, List[Tuple[str, float]]], str]:
    """(url -> [(other_url, similarity_pct)], method): near_duplicate_index without the counts."""
    near, _counts, method = near_duplicate_index(pages, indexable)
    return near, method


def compute_near_duplicates(pages: Iterable[Any], indexable: Dict[str, bool]) -> Dict[str, List[Tuple[str, float]]]:
    """url -> [(other_url, similarity_pct)], closest first (see near_duplicate_index)."""
    return near_duplicate_index(pages, indexable)[0]


# ---------------------------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------------------------
def _sitemap_map(raw: Any) -> Dict[str, List[str]]:
    """Accepts {url: [sitemap, ...]}, {url: sitemap}, or [(url, sitemap), ...]."""
    out: Dict[str, List[str]] = {}
    try:
        items = raw.items() if isinstance(raw, dict) else (raw or [])
        for item in items:
            try:
                if isinstance(item, (tuple, list)) and len(item) >= 2:
                    url, src = item[0], item[1]
                else:
                    url, src = item, ""
                u = _norm(url)
                if not u:
                    continue
                if isinstance(src, (list, tuple, set)):
                    srcs = [str(s) for s in src if s]
                else:
                    srcs = [str(src)] if src else []
                lst = out.setdefault(u, [])
                for s in srcs:
                    if s not in lst:
                        lst.append(s)
            except Exception:
                continue
    except Exception:
        return {}
    return out


# ---------------------------------------------------------------------------------------------
# Broken bookmarks (Screaming Frog URL > Broken Bookmark)
# ---------------------------------------------------------------------------------------------
def bookmark_fragment(raw: str) -> str:
    """The #fragment of an href when it names an element id ("" for none, script hooks, app routes such as
    #/cart or #!/x, state such as #page=2 or #:~:text=...)."""
    if not raw or "#" not in raw:
        return ""
    frag = raw.split("#", 1)[1].strip()
    if frag.lower() in BOOKMARK_SKIP or frag[0] in "/!:" or "=" in frag or any(c.isspace() for c in frag):
        return ""
    return frag


def _loaded_html(r) -> bool:
    return _is_html_page(r) and _category(r) == "2xx"


def _bookmark_target(site: "Site", url: str) -> Optional[Any]:
    """The crawled 2xx HTML page a link lands on (one redirect followed), None when it is not one."""
    r = site.by_url.get(url) or site.by_url.get(_norm(url))
    if r is not None and _category(r) == "3xx":
        r = site.by_url.get(_redirect_target(r) or "")
    return r if r is not None and _loaded_html(r) else None


def compute_broken_bookmarks(site: "Site") -> Dict[str, List[dict]]:
    """target url -> [{"fragment", "source", "line"}]: internal links (from 2xx HTML pages) to "#id" on a
    crawled 2xx HTML page where no element id (or <a name>) matches. Targets read by an older extractor (no
    "ids" key) or with the id list cut at MAX_PAGE_IDS are not judged."""
    out: Dict[str, List[dict]] = defaultdict(list)
    id_sets: Dict[str, Optional[Set[str]]] = {}
    for page in site.pages:
        if not _loaded_html(page):
            continue
        for link in _seo(page).get("links") or ():
            try:
                raw = link.get("raw") or ""
                if "#" not in raw or not link.get("internal"):
                    continue
                frag = bookmark_fragment(raw)
                if not frag:
                    continue
                target = _bookmark_target(site, link.get("url") or "")
                if target is None:
                    continue
                if target.url not in id_sets:
                    ids = _seo(target).get("ids")
                    id_sets[target.url] = set(ids) if isinstance(ids, list) and len(ids) < MAX_PAGE_IDS else None
                ids = id_sets[target.url]
                if ids is None or frag in ids or unquote(frag) in ids:
                    continue
                found = out[target.url]
                if len(found) < MAX_BOOKMARKS_PER_URL and not any(
                        b["fragment"] == frag and b["source"] == page.url for b in found):
                    found.append({"fragment": frag, "source": page.url, "line": int(link.get("line") or 0)})
            except Exception:
                continue
    return dict(out)


# ---------------------------------------------------------------------------------------------
# Semantic similarity (TF-IDF approximation)
# ---------------------------------------------------------------------------------------------
def _term_counts(pages: Iterable[Any], indexable: Dict[str, bool]) -> Dict[str, Dict[str, int]]:
    """url -> {word: count} of the top content words (PageData top_terms) of each indexable 2xx HTML page with
    at least SEMANTIC_MIN_TERMS distinct words."""
    counts: Dict[str, Dict[str, int]] = {}
    for r in pages:
        url = getattr(r, "url", "")
        if not indexable.get(url) or not _loaded_html(r):
            continue
        tc: Dict[str, int] = {}
        for item in _seo(r).get("top_terms") or []:
            try:
                t, c = item[0], int(item[1])
            except Exception:
                continue
            if isinstance(t, str) and t and c > 0:
                tc[t] = c
        if len(tc) >= SEMANTIC_MIN_TERMS:
            counts[url] = tc
    return counts


def _normalise(v: Dict[str, float]) -> Dict[str, float]:
    norm = math.sqrt(sum(w * w for w in v.values()))
    return {t: w / norm for t, w in v.items()} if norm > 0 else {}


def _tfidf_vectors(counts: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, float]]:
    """L2-normalised TF-IDF vectors: words on every page weigh 0 (they say nothing about one page's topic)."""
    n = len(counts)
    df: Counter = Counter()
    for tc in counts.values():
        df.update(tc.keys())
    out = {}
    for url, tc in counts.items():
        v = _normalise({t: (1.0 + math.log(c)) * math.log(n / df[t]) for t, c in tc.items() if df[t] < n})
        if v:
            out[url] = v
    return out


def _tf_vectors(counts: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, float]]:
    """L2-normalised log term-frequency vectors (no IDF: the words most pages share ARE the site's topic)."""
    out = {}
    for url, tc in counts.items():
        v = _normalise({t: 1.0 + math.log(c) for t, c in tc.items()})
        if v:
            out[url] = v
    return out


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    get = b.get
    return sum(w * get(t, 0.0) for t, w in a.items())


def compute_semantic(pages: Iterable[Any], indexable: Dict[str, bool]) -> Tuple[Dict[str, Tuple[str, float]],
                                                                               Dict[str, float], Dict[str, Any]]:
    """(closest: url -> (most similar other page, cosine), low_relevance: url -> cosine to the rest of the site,
    info). An approximation of Screaming Frog's AI-embedding filters from each indexable page's top content
    words. Similar pairs: TF-IDF vectors (words every page uses do not count), pages sharing strong topic
    words are candidates and are compared exactly. Relevance: plain term-frequency vectors, the site topic is
    the sum of every page vector, compared with each page minus itself."""
    counts = _term_counts(pages, indexable)
    info: Dict[str, Any] = {"method": "TF-IDF cosine of each page's top content words (approximation)",
                            "pages": len(counts), "similar_threshold": SEMANTIC_SIMILAR_MIN}
    if len(counts) > SEMANTIC_MAX_PAGES:
        info["not_compared"] = len(counts) - SEMANTIC_MAX_PAGES
        counts = dict(list(counts.items())[:SEMANTIC_MAX_PAGES])
        info["pages"] = len(counts)
    if len(counts) < SEMANTIC_MIN_PAGES:
        info["low_relevance_skipped"] = (f"Fewer than {LOW_RELEVANCE_MIN_PAGES} indexable pages with enough text "
                                         "to tell the site's main topic.")
        return {}, {}, info
    vecs = _tfidf_vectors(counts)
    urls = list(vecs)
    top: Dict[str, List[str]] = {u: [t for t, _ in sorted(v.items(), key=lambda kv: -kv[1])[:SEMANTIC_TOP_TERMS]]
                                 for u, v in vecs.items()}
    postings: Dict[str, List[str]] = defaultdict(list)
    for u, ts in top.items():
        for t in ts:
            postings[t].append(u)
    closest: Dict[str, Tuple[str, float]] = {}
    for u in urls:
        shared: Counter = Counter()
        for t in top[u]:
            lst = postings.get(t) or ()
            if len(lst) > SEMANTIC_POSTING_CAP:
                continue
            shared.update(lst)
        shared.pop(u, None)
        best_u, best_s = "", 0.0
        for other, _ in shared.most_common(SEMANTIC_MAX_CANDIDATES):
            s = _cosine(vecs[u], vecs[other])
            if s > best_s or (s == best_s and other < best_u):
                best_u, best_s = other, s
        if best_u:
            closest[u] = (best_u, round(min(1.0, best_s), 4))

    low: Dict[str, float] = {}
    if len(counts) >= LOW_RELEVANCE_MIN_PAGES:
        tf = _tf_vectors(counts)
        total: Dict[str, float] = defaultdict(float)
        for v in tf.values():
            for t, w in v.items():
                total[t] += w
        total_sq = sum(w * w for w in total.values())
        sims: Dict[str, float] = {}
        for u, v in tf.items():
            # cosine(page, site total minus the page): dot = p.T - 1, |T - p|^2 = |T|^2 - 2 p.T + 1
            pt = sum(w * total[t] for t, w in v.items())
            rest_sq = max(0.0, total_sq - 2 * pt + 1.0)
            sims[u] = (pt - 1.0) / math.sqrt(rest_sq) if rest_sq > 0 else 0.0
        ordered = sorted(sims.values())
        median = ordered[len(ordered) // 2]
        cut = max(LOW_RELEVANCE_FLOOR, median * LOW_RELEVANCE_FACTOR) if median > 0 else 0.0
        info.update(site_median=round(median, 4), low_relevance_threshold=round(cut, 4))
        low = {u: round(max(0.0, s), 4) for u, s in sims.items() if s < cut}
    else:
        info["low_relevance_skipped"] = (f"Fewer than {LOW_RELEVANCE_MIN_PAGES} indexable pages with enough text "
                                         "to tell the site's main topic.")
    return closest, low, info


# crawler -> (fingerprint key, near_duplicate_index result); weak, so a finished crawler is not kept alive and
# the cache never becomes part of a saved crawl's state
_NEAR_CACHE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _near_cached(crawler, site: "Site") -> Dict[str, Any]:
    """near_duplicate_run at the crawl's threshold (CrawlConfig.near_dup_threshold), reused while the indexable
    pages, their fingerprints and the threshold are unchanged (the index is rebuilt many times during and after a
    crawl)."""
    threshold = near_threshold(site.config) if site.config is not None else NEAR_DUP_THRESHOLD
    try:
        prints = frozenset((r.url, _seo(r).get("minhash") or "", _seo(r).get("simhash") or 0,
                            _seo(r).get("content_hash") or "")
                           for r in site.pages if site.indexable.get(r.url))
        key = hash((threshold, NEAR_DUP_MAX_PAIRS, prints))
    except Exception:
        key = None
    try:
        cached = _NEAR_CACHE.get(crawler)
    except TypeError:                      # an object that cannot be weakly referenced: no reuse
        cached = None
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    result = near_duplicate_run(site.pages, site.indexable, threshold, NEAR_DUP_MAX_PAIRS)
    if key is not None:
        try:
            _NEAR_CACHE[crawler] = (key, result)
        except TypeError:
            pass
    return result


def _semantic_cached(crawler, site: "Site"):
    """compute_semantic, reused while the indexable pages and their text are unchanged (post-crawl checks
    rebuild the index several times after a crawl)."""
    try:
        key = hash(frozenset((r.url, _seo(r).get("content_hash") or "") for r in site.pages
                             if site.indexable.get(r.url)))
    except Exception:
        key = None
    cached = getattr(crawler, "_semantic_cache", None)
    if key is not None and isinstance(cached, tuple) and len(cached) == 2 and cached[0] == key:
        return cached[1]
    try:
        result = compute_semantic(site.pages, site.indexable)
    except Exception:
        result = ({}, {}, {})
    if key is not None:
        try:
            crawler._semantic_cache = (key, result)
        except Exception:
            pass
    return result


def _steps(cfg) -> Dict[str, bool]:
    steps = getattr(cfg, "analysis", None) if cfg is not None else None
    return steps if isinstance(steps, dict) else {}


def _step_off(steps: Dict[str, bool], name: str) -> str:
    if steps.get(name) is False:
        label = name.replace("_", " ").title()
        return f"The Crawl Analysis step '{label}' was turned off (Crawl Analysis > Configure)."
    return ""


# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------
def build_site(crawler) -> Site:
    """Snapshot the crawler's results and compute every site-wide fact the tabs read."""
    results = list(getattr(crawler, "results_list", None) or [])
    externals = list(getattr(crawler, "external_links_list", None) or [])
    site = Site(
        results=results,
        externals=externals,
        start_url=getattr(crawler, "start_url", "") or "",
        base_domain=getattr(crawler, "base_domain", "") or "",
        config=getattr(crawler, "config", None),
    )
    for name in ("bot_report", "robots_audit", "trap_probe"):
        value = getattr(crawler, name, None)
        setattr(site, name, value if isinstance(value, dict) else None)
    lastmod = getattr(crawler, "sitemap_lastmod", None)
    site.sitemap_lastmod = dict(lastmod) if isinstance(lastmod, dict) else None
    stats = getattr(crawler, "sitemap_stats", None)
    site.sitemap_stats = dict(stats) if isinstance(stats, dict) and stats else None
    # P8: hreflang alternates written in the XML sitemaps; P9: the sitemap URL index stopped at its limit
    alternates = getattr(crawler, "sitemap_hreflang", None)
    site.sitemap_hreflang = alternates if isinstance(alternates, dict) else {}
    full = getattr(crawler, "sitemap_index_full", None)
    site.sitemap_index_full = dict(full) if isinstance(full, dict) else None
    site.sitemaps_not_read = list(getattr(crawler, "sitemaps_not_read", None) or [])
    limit = getattr(crawler, "sitemap_files_limit", None)
    site.sitemap_files_limit = int(limit) if isinstance(limit, int) and limit > 0 else None
    integ = getattr(crawler, "integrations", None)
    site.integrations = dict(integ) if isinstance(integ, dict) else {}
    site.crawl_finished = (str(getattr(crawler, "status", "") or "").lower() in FINISHED
                           or getattr(crawler, "status", None) is None)
    prefs = getattr(site.config, "preferences", None)
    site.preferences = dict(prefs) if isinstance(prefs, dict) else {}
    steps = _steps(site.config)

    for r in results:
        url = getattr(r, "url", None)
        if not url or url in site.by_url:
            continue
        site.by_url[url] = r
    site.pages = [r for r in site.by_url.values() if _is_html_page(r)]

    # Depth and indexability for every internal URL
    for url, r in site.by_url.items():
        try:
            site.crawl_depth[url] = int(getattr(r, "depth", 0) or 0)
        except Exception:
            site.crawl_depth[url] = 0
        try:
            ok, status = indexability_of(r)
        except Exception:
            ok, status = False, "No Response"
        site.indexable[url] = ok
        site.indexability_status[url] = status
    # llms.txt links judged against the crawl (P19; None when the check did not run)
    try:
        from .robots_audit import llms_summary
        site.llms_txt = llms_summary(site)
    except Exception:
        site.llms_txt = None

    # Outlinks / inlinks (Screaming Frog credits the exact linked URL, not a redirect target)
    inlinks: Dict[str, List[dict]] = defaultdict(list)
    sources: Dict[str, set] = defaultdict(set)
    for r in site.pages:
        src = r.url
        links = _seo(r).get("links") or []
        site.outlinks[src] = links
        for link in links:
            try:
                if not link.get("internal"):
                    continue
                dest = link.get("url") or ""
                if not dest:
                    continue
                inlinks[dest].append({
                    "source": src,
                    "anchor": link.get("anchor") or "",
                    "nofollow": bool(link.get("nofollow")),
                    "region": link.get("region") or "",
                    "line": int(link.get("line") or 0),
                    "rel": link_rel(link),
                })
                sources[dest].add(src)
            except Exception:
                continue
    site.inlinks = dict(inlinks)
    site.unique_inlinks = {u: len(s) for u, s in sources.items()}
    # Every other way a crawled URL points at another one (canonical, hreflang, rel next / prev, refreshes,
    # JavaScript redirects, mobile alternates, form actions, other link elements): kept apart from site.inlinks so
    # Link Score and the inlink counts stay hyperlink-based (P6)
    try:
        site.other_inlinks = compute_other_inlinks(site.by_url.values())
    except Exception:
        site.other_inlinks = {}

    for step in ANALYSIS_STEPS:
        why = _step_off(steps, step)
        if why:
            site.analysis_skipped[step] = why

    if "link_score" in site.analysis_skipped:
        site.link_score = {}
    else:
        try:
            site.link_score = compute_link_score(site.by_url)
        except Exception:
            site.link_score = {}
    try:
        site.duplicates = compute_duplicates(site.pages, site.indexable)
    except Exception:
        site.duplicates = {k: {} for k in DUPLICATE_KINDS}
    if "near_duplicates" not in site.analysis_skipped:
        try:
            info = _near_cached(crawler, site)
            site.near_duplicates, site.near_duplicate_counts, site.near_duplicate_method = \
                info["near"], info["counts"], info["method"]
            site.near_duplicate_info = {k: info[k] for k in ("threshold", "rows", "pairs", "capped", "method")}
            if info.get("capped"):
                site.analysis_skipped["near_duplicates"] = info["reason"]   # Not checked, never "none found"
        except Exception:
            site.near_duplicates, site.near_duplicate_counts, site.near_duplicate_method = {}, {}, ""
    try:
        site.broken_bookmarks = compute_broken_bookmarks(site)
    except Exception:
        site.broken_bookmarks = {}
    if "semantic_similarity" not in site.analysis_skipped:
        if not site.crawl_finished:
            site.analysis_skipped["semantic_similarity"] = ("Semantic similarity is worked out once the crawl "
                                                            "has finished.")
        else:
            site.semantic, site.low_relevance, site.semantic_info = _semantic_cached(crawler, site)

    # Sitemaps and orphans
    site.sitemap_urls = _sitemap_map(getattr(crawler, "sitemap_index", None) or {})
    if "sitemaps" in site.analysis_skipped:
        site.orphans = []
        return site
    linked = set(site.inlinks.keys())
    linked_norm = {_norm(u) for u in linked}
    start = _norm(site.start_url)
    site.orphans = [u for u in site.sitemap_urls
                    if u != start and u not in linked and u not in linked_norm]
    return site
