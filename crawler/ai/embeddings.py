"""
Page embeddings: each page's main text turned into a list of numbers that captures its meaning, so pages that
say the same thing in different words come out close. Used by the Content tab (Semantically Similar, Low
Relevance Content, Topic Cluster; crawler/seo/tabs/content.py), the topic map and semantic search
(crawler/seo/semantic.py).

Providers (the AI settings of crawler/ai/client.py; Groq, OpenRouter and Anthropic have no embedding API here):
  gemini   Google Gemini embedding API, free tier (GEMINI_API_KEY); GEMINI_EMBED_MODEL, default
           gemini-embedding-2 (gemini-embedding-001 when that model is unknown); 256 numbers per text
  ollama   a local embedding model (OLLAMA_BASE_URL, OLLAMA_EMBED_MODEL, default nomic-embed-text); private
embed(texts, private=False, dims=256, budget="embeddings") -> unit vectors (one per text) or None; it never
raises, and ai.client.last_outcome() / last_note() say why None came back. Requests count against a named AI
budget (client.BUDGETS: "embeddings" for a run, "search" for a search query) and go through the client's rate
limiter on their own minute window (CRAWLER_AI_EMBED_RPM requests, CRAWLER_AI_EMBED_TPM estimated tokens per
minute: the free embedding models have their own limits). Vectors are cached in memory by model + text hash
(CACHE_MAX vectors), so a re-run needs no call for unchanged pages. Text from a signed-in crawl goes only to a
private provider (Ollama) unless CRAWLER_AI_ALLOW_PRIVATE=1. Keys never appear in notes (client._scrub).

The embeddings run (POST /api/embeddings/run {max_pages?}; the integrations JOBS entry "embeddings"):
  - pages: indexable internal HTML pages that answered 2xx, in crawl order, up to max_pages (DEFAULT_PAGES, at
    most MAX_PAGES); the crawl must have finished (pages are compared with each other)
  - text per page: title, H1, meta description and the start of the main text (page_text: the stored HTML, else
    the page's headings and top words), EMBED_TEXT_CHARS characters at most; under MIN_WORDS words: Not checked
  - every page of the pool ends the run embedded or "Not checked" with the reason (over the page limit, too
    little text, AI quota, the run's call limit, stopped, the AI failing)
  - derived results only (never the vectors) in site.integrations["embeddings"] (integrations.put_result, saved
    with the crawl), the 10.3 entry shape plus:
      {"provider", "model", "dims", "key", "pool", "cap", "pages", "calls",
       "similar_threshold", "relevance": {"median", "threshold", "pages"} | {"skipped": reason},
       "clusters": {"k", "silhouette", "items": [{"id", "label", "terms", "size"}], "reason"?},
       "results": {url: {"hash", "closest", "score", "similar": [[url, score] x3], "relevance", "low",
                         "cluster", "x", "y"}},
       "not_checked": {url: reason}}
  - Semantically similar: the closest other page scores at least SIMILAR_MIN (cosine).
  - Relevance: cosine of the page and the mean of every other page (the site's topic). Low relevance: at least
    LOW_RELEVANCE_MIN_PAGES pages, and the score lies far below the site's typical page: more than
    LOW_RELEVANCE_Z robust standard deviations (1.4826 x the median absolute deviation) and at least
    LOW_RELEVANCE_GAP below the median score. Measured: an off-topic recipe and car-sales pages on a marketing
    agency fixture are flagged; none of the 149 pages of a real agency site (rd) is.
  - SIMILAR_MIN was set on real gemini-embedding-2 vectors: a same-meaning pair written with different words
    scores 0.94 (0.0 by words), near twins 0.996, two different services of one agency up to 0.885; on rd it
    flags the 3 closest hub / sub-page pairs (0.93 - 0.96).
Public: embed, embed_status, embed_query, vectors_for, last_note, page_embed_text, run, start_run, run_status,
stop_run, RunError.
"""
import copy
import hashlib
import math
import os
import statistics
import threading
import time
from array import array
from collections import Counter, OrderedDict, defaultdict, deque
from operator import mul
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import client as ai

JOB_SOURCE = "embeddings"
EMBED_PROVIDERS = ("gemini", "ollama")
GEMINI_EMBED_MODELS = ("gemini-embedding-2", "gemini-embedding-001")
OLLAMA_EMBED_MODEL = "nomic-embed-text"
DEFAULT_DIMS = 256
GEMINI_BATCH = 100            # texts per request (the API's limit)
OLLAMA_BATCH = 32
BATCH_TOKENS = 12_000         # estimated tokens per request
EMBED_TEXT_CHARS = 1500       # text embedded per page
MIN_WORDS = 8                 # a page with fewer words of text is not compared
DEFAULT_PAGES = 500
MAX_PAGES_CAP = 2000          # CRAWLER_AI_EMBED_MAX_PAGES may raise the limit up to this
CACHE_MAX = 10_000            # vectors kept in memory (about 1 KB each at 256 numbers)
SIMILAR_MIN = 0.92            # closest page at or above this cosine = semantically similar
SIMILAR_KEEP = 3              # closest pages stored per page
LOW_RELEVANCE_MIN_PAGES = 10
LOW_RELEVANCE_Z = 3.5         # robust standard deviations below the median relevance ...
LOW_RELEVANCE_GAP = 0.05      # ... and at least this far below it
MAX_FAILED_BATCHES = 3
TOKEN_CHARS = 4.0             # characters per token (estimate)

NO_PROVIDER = ("Embeddings need Google Gemini (GEMINI_API_KEY, free tier) or a local Ollama model (OLLAMA_BASE_URL "
               "and OLLAMA_EMBED_MODEL)")
PRIVATE_REASON = ("Private crawl: text from signed-in pages is not sent to a free cloud AI (use a local Ollama "
                  "embedding model, or set CRAWLER_AI_ALLOW_PRIVATE=1 on the server).")
REASONS = {
    "cap": "Over this run's page limit ({n:,} pages): run embeddings again with a higher limit.",
    "little": "Too little text to compare (fewer than {n} words).",
    "budget": "The AI call limit of this run was reached ({n} calls).",
    "quota": "AI quota reached - try again later.",
    "stopped": "The run was stopped before this page.",
    "off": "AI off - no embedding provider is configured.",
    "private": PRIVATE_REASON,
    "error": "The AI did not return embeddings for this page ({note}).",
    "failing": "The run stopped after the AI failed {n} times in a row ({note}).",
}


class RunError(Exception):
    """An embeddings run cannot start; the message says why in plain words."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or "").strip() or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(_env(name) or default))
    except ValueError:
        return default


# ---------------------------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------------------------
_dead: set = set()                      # Gemini embedding models that answered 404
_window_tokens: Dict[str, deque] = {}   # limiter window -> (time, estimated tokens) of the last minute
_tok_lock = threading.Lock()


def _models(p: ai.Provider) -> List[str]:
    if p.name == "gemini":
        chosen = _env("GEMINI_EMBED_MODEL")
        names = [chosen] if chosen else list(GEMINI_EMBED_MODELS)
        return [m for m in names if m not in _dead] or names[-1:]
    return [_env("OLLAMA_EMBED_MODEL", OLLAMA_EMBED_MODEL)]


def model_key(provider: str, model: str, dims: int) -> str:
    """"provider:model:dims" - vectors with different keys are never compared."""
    return f"{provider}:{model}:{int(dims) if provider == 'gemini' else 0}"


def _window(p: ai.Provider) -> str:
    return f"{p.name}-embed"


def _chain(private: bool, dims: int, only: str = "") -> List[Tuple[ai.Provider, str, str]]:
    """(provider, model, key) to try in order: configured embedding providers that are not resting, and only
    private-safe ones for private text; `only` keeps the one key a run is pinned to."""
    out = []
    for p in ai._providers():
        if p.name not in EMBED_PROVIDERS or ai._cooling(_window(p)):
            continue
        if private and not ai._allow_private() and not p.private_ok:
            continue
        for m in _models(p):
            key = model_key(p.name, m, dims)
            if not only or key == only:
                out.append((p, m, key))
    return out


def embed_status(private: bool = False, dims: int = DEFAULT_DIMS) -> Dict[str, Any]:
    """{"enabled", "provider", "model", "dims", "key", "private_ok", "reason"} of the embedding provider in use."""
    providers = ai._providers()
    off = {"enabled": False, "provider": "", "model": "", "dims": dims, "key": "", "private_ok": False}
    if not providers:
        return dict(off, reason=f"{ai.AI_OFF_NOTE}: {NO_PROVIDER}.")
    capable = [p for p in providers if p.name in EMBED_PROVIDERS]
    if not capable:
        names = ", ".join(p.label for p in providers)
        return dict(off, reason=f"{NO_PROVIDER}; the AI in use ({names}) has no embedding API here.")
    usable = [p for p in capable if not private or ai._allow_private() or p.private_ok]
    if not usable:
        return dict(off, reason=PRIVATE_REASON)
    resting = [p for p in usable if ai._cooling(_window(p))]
    p = next((x for x in usable if x not in resting), usable[0])
    model = _models(p)[0]
    out = {"enabled": True, "provider": p.name, "model": model, "dims": dims if p.name == "gemini" else 0,
           "key": model_key(p.name, model, dims), "private_ok": p.private_ok or ai._allow_private(),
           "reason": f"Using {p.label}: {model}."}
    if len(resting) == len(usable):
        out.update(enabled=False, reason=ai.QUOTA_NOTE.split(" - ")[0] + " for embeddings - try again later.")
    return out


# ---------------------------------------------------------------------------------------------
# Cache (model key + text hash -> unit vector), in memory only
# ---------------------------------------------------------------------------------------------
_cache: "OrderedDict[Tuple[str, str], array]" = OrderedDict()
_cache_lock = threading.Lock()


def text_hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:20]


def _cache_get(key: str, h: str) -> Optional[List[float]]:
    with _cache_lock:
        v = _cache.get((key, h))
        if v is None:
            return None
        _cache.move_to_end((key, h))
        return list(v)


def _cache_put(key: str, h: str, vec: List[float]) -> None:
    with _cache_lock:
        _cache[(key, h)] = array("f", vec)
        _cache.move_to_end((key, h))
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)


def _unit(v: Sequence[float]) -> List[float]:
    v = [float(x) for x in v]
    n = math.sqrt(sum(map(mul, v, v)))
    return [x / n for x in v] if n > 0 else []


def vectors_for(entry: Dict[str, Any], urls: Optional[Sequence[str]] = None) -> Tuple[Dict[str, List[float]], int]:
    """({url: unit vector} of the pages an embeddings run stored, as far as the memory cache still holds them,
    number of those pages whose vector is gone). `urls` limits it to these pages (in that order)."""
    key = str(entry.get("key") or "")
    results = entry.get("results") or {}
    out: Dict[str, List[float]] = {}
    missing = 0
    for url in (urls if urls is not None else list(results)):
        rec = results.get(url)
        if not isinstance(rec, dict) or not rec.get("hash"):
            continue
        v = _cache_get(key, rec["hash"])
        if v is None:
            missing += 1
        else:
            out[url] = v
    return out, missing


# ---------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------
def _tokens(texts: Sequence[str]) -> int:
    return int(sum(len(t) for t in texts) / TOKEN_CHARS) + 4 * len(texts)


def _wait_tokens(window: str, need: int) -> None:
    """Wait (cancellably) until `need` estimated tokens fit in this minute's window of CRAWLER_AI_EMBED_TPM."""
    tpm = max(1000, _env_int("CRAWLER_AI_EMBED_TPM", 30_000))
    while True:
        with _tok_lock:
            win = _window_tokens.setdefault(window, deque())
            now = time.monotonic()
            while win and now - win[0][0] >= 60:
                win.popleft()
            used = sum(n for _, n in win)
            if not win or used + need <= tpm:
                win.append((now, need))
                return
            wait = 60 - (now - win[0][0]) + 0.05
        ai._pause(min(wait, 60))


def batches(texts: Sequence[str], provider: str = "gemini") -> List[List[int]]:
    """Index groups of `texts`, one request each (at most GEMINI_BATCH / OLLAMA_BATCH texts and BATCH_TOKENS)."""
    size = GEMINI_BATCH if provider == "gemini" else OLLAMA_BATCH
    out: List[List[int]] = []
    cur: List[int] = []
    tok = 0
    for i, t in enumerate(texts):
        n = _tokens([t])
        if cur and (len(cur) >= size or tok + n > BATCH_TOKENS):
            out.append(cur)
            cur, tok = [], 0
        cur.append(i)
        tok += n
    if cur:
        out.append(cur)
    return out


def _request(p: ai.Provider, model: str, texts: List[str], dims: int) -> List[List[float]]:
    """One embedding request (counted against the current budget); unit vectors in the order of `texts`."""
    _wait_tokens(_window(p), _tokens(texts))
    for attempt in range(2):
        ai._acquire(p, window=_window(p), rpm=max(1, _env_int("CRAWLER_AI_EMBED_RPM", 30)))
        try:
            if p.name == "gemini":
                url = f"{p.base_url}/v1beta/models/{model}:batchEmbedContents"
                body = {"requests": [{"model": f"models/{model}", "content": {"parts": [{"text": t}]},
                                      "outputDimensionality": int(dims)} for t in texts]}
                data = ai._post(url, {"x-goog-api-key": p.key, "Content-Type": "application/json"}, body)
                raw = [e.get("values") if isinstance(e, dict) else None for e in data.get("embeddings") or []]
                tin = int((data.get("usageMetadata") or {}).get("promptTokenCount") or 0)
            else:
                headers = {"Content-Type": "application/json"}
                if p.key:
                    headers["Authorization"] = f"Bearer {p.key}"
                data = ai._post(f"{p.base_url}/embeddings", headers, {"model": model, "input": texts})
                items = sorted((d for d in data.get("data") or [] if isinstance(d, dict)),
                               key=lambda d: int(d.get("index") or 0))
                raw = [d.get("embedding") for d in items]
                tin = int((data.get("usage") or {}).get("prompt_tokens") or 0)
        except ai._Quota as q:
            if attempt == 0 and not q.daily and not q.too_large and \
                    0 < q.retry_after <= ai._env_num("CRAWLER_AI_MAX_WAIT", 60):
                ai._pause(q.retry_after)
                continue
            raise
        except ai._Transient:
            if attempt == 0:
                ai._pause(2.0)
                continue
            raise
        vecs = [_unit(v) if isinstance(v, list) and v else [] for v in raw]
        if len(vecs) != len(texts) or any(not v for v in vecs) or len({len(v) for v in vecs}) != 1:
            raise ai._BadRequest(f"{p.name}: the answer did not hold one vector per text")
        ai._used_model[p.name] = model
        ai._record(p, model, tin, 0)
        return vecs
    raise ai._Transient("retry failed")


_notes = threading.local()


def last_note() -> str:
    """Why this thread's last embed() returned None (plain words, no keys)."""
    return getattr(_notes, "note", "") or ai.fallback_note()


def last_key() -> str:
    """The model key ("provider:model:dims") that answered this thread's last embed() ("" when none did)."""
    return getattr(_notes, "key", "") or ""


def embed(texts: List[str], private: bool = False, dims: int = DEFAULT_DIMS, budget: str = "embeddings",
          only: str = "") -> Optional[List[List[float]]]:
    """Unit vectors for `texts` (same order) from the first embedding provider that answers, or None (AI off, no
    embedding provider, private text without a private provider, quota, the budget used up, stopped, error).
    Cached texts need no request; every request counts against the named `budget`. `only`: the model key every
    vector must come from (a run compares its vectors with each other). Never raises."""
    ai._begin_call()
    _notes.note = ""
    _notes.key = ""
    try:
        with ai.job_scope(), ai._budget_scope(budget):
            return _embed([str(t or "") for t in texts or []], private, int(dims or DEFAULT_DIMS), only)
    except ai._Cancelled:
        ai._jobs.stopped = True
        ai._jobs.outcome = "stopped"
        _notes.note = ai.STOPPED_NOTE
        return None
    except Exception as exc:   # never raises
        ai._note_error(f"embeddings: unexpected {type(exc).__name__}: {exc}")
        ai._jobs.outcome = "error"
        _notes.note = ai.ERROR_NOTE
        return None


def _embed(texts: List[str], private: bool, dims: int, only: str) -> Optional[List[List[float]]]:
    ai._check()
    if not texts:
        ai._jobs.outcome = "ok"
        return []
    chain = _chain(private, dims, only)
    if not chain:
        status = embed_status(private, dims)
        if not ai._providers() or not any(p.name in EMBED_PROVIDERS for p in ai._providers()):
            ai._jobs.outcome = "off"
        elif private and status.get("reason") == PRIVATE_REASON:
            ai._jobs.outcome = "private"
        elif only and not any(model_key(p.name, m, dims) == only for p in ai._providers()
                              if p.name in EMBED_PROVIDERS for m in _models(p)):
            ai._jobs.outcome = "error"
            _notes.note = "the embedding model of the last run is no longer configured"
            return None
        else:
            ai._jobs.outcome = "quota"
        _notes.note = status.get("reason") or ai.fallback_note()
        return None
    saw_quota = False
    rested: set = set()                 # providers resting after a quota / key answer: their other models wait too
    for p, model, key in chain:
        if p.name in rested:
            continue
        hashes = [text_hash(t) for t in texts]
        out: List[Optional[List[float]]] = [_cache_get(key, h) for h in hashes]
        need = [i for i, v in enumerate(out) if v is None]
        try:
            for group in batches([texts[i] for i in need], p.name):
                idx = [need[j] for j in group]
                vecs = _request(p, model, [texts[i] for i in idx], dims)
                for i, v in zip(idx, vecs):
                    out[i] = list(array("f", v))      # the cache's precision, so a cached re-run is identical
                    _cache_put(key, hashes[i], v)
            ai._jobs.outcome = "ok"
            _notes.key = key
            return [v for v in out if v is not None]
        except ai._Budget as exc:
            ai._note_error(str(exc))
            ai._jobs.outcome = "budget"
            _notes.note = str(exc)
            return None
        except ai._Quota as q:
            ai._cool(_window(p), 3600 if q.daily else max(q.retry_after, 30) if not q.too_large else 5,
                     "quota" if not q.too_large else "request too large")
            ai._note_error(f"{p.name} embeddings: {q}")
            saw_quota = saw_quota or not q.too_large
            rested.add(p.name)
            continue
        except ai._Fatal as exc:
            ai._cool(_window(p), 600, "key rejected")
            ai._note_error(f"{p.name} embeddings: {exc}")
            rested.add(p.name)
            continue
        except ai._BadRequest as exc:
            text = str(exc).lower()
            if p.name == "gemini" and (exc.status == 404 or ("not found" in text and "model" in text)):
                _dead.add(model)
            ai._note_error(f"{p.name} embeddings: {exc}")
            continue
        except ai._Transient as exc:
            ai._note_error(f"{p.name} embeddings: {exc}")
            continue
    ai._jobs.outcome = "quota" if saw_quota else "error"
    _notes.note = ai.QUOTA_NOTE if saw_quota else ai._state.get("last_error") or ai.ERROR_NOTE
    return None


def embed_query(query: str, entry: Dict[str, Any]) -> Optional[List[float]]:
    """The vector of a search query, from the model an embeddings run used (the "search" budget); None when the
    AI is not available (last_note() says why)."""
    key = str(entry.get("key") or "")
    dims = int(entry.get("dims") or DEFAULT_DIMS)
    out = embed([query], private=False, dims=dims or DEFAULT_DIMS, budget="search", only=key)
    return out[0] if out else None


# ---------------------------------------------------------------------------------------------
# Text per page
# ---------------------------------------------------------------------------------------------
def _seo(r) -> Dict[str, Any]:
    return getattr(r, "seo", None) or {}


def _first(r, key: str) -> str:
    for v in _seo(r).get(key) or []:
        s = " ".join((v if isinstance(v, str) else str(v or "")).split())
        if s:
            return s
    return ""


def page_embed_text(crawler, r) -> Tuple[str, str]:
    """(text, source) embedded for one page: its title, H1, meta description and the start of its main text
    (source "stored" / "fetched"), or its headings and top words when the HTML is not available ("fields")."""
    head = []
    for part in (_first(r, "titles"), _first(r, "h1"), _first(r, "meta_descriptions")):
        if part and part not in head:
            head.append(part)
    body, source = "", ""
    try:
        from ..seo.page_text import page_text
        body, source = page_text(crawler, r.url, content_area=True, max_chars=EMBED_TEXT_CHARS * 2)
    except Exception:
        body, source = "", ""
    if not body.strip():
        h2 = [" ".join(str(x).split()) for x in (_seo(r).get("h2") or [])[:6] if str(x or "").strip()]
        terms = [str(t[0]) for t in (_seo(r).get("top_terms") or [])[:40] if isinstance(t, (list, tuple)) and t]
        body = ". ".join(h2) + ("\n" + " ".join(terms) if terms else "")
        source = "fields"
    text = "\n".join(head + [" ".join(body.split())]).strip()
    if len(text) > EMBED_TEXT_CHARS:
        cut = text[:EMBED_TEXT_CHARS]
        space = cut.rfind(" ")
        text = cut[:space] if space > EMBED_TEXT_CHARS * 0.8 else cut
    return text, source


# ---------------------------------------------------------------------------------------------
# Derived results
# ---------------------------------------------------------------------------------------------
def similar_pages(vecs: Dict[str, List[float]], keep: int = SIMILAR_KEEP) -> Dict[str, List[Tuple[str, float]]]:
    """url -> its `keep` closest other pages [(url, cosine)], closest first (ties: the URL that sorts first)."""
    urls = list(vecs)
    best: Dict[str, List[Tuple[float, str]]] = {u: [] for u in urls}
    for i, u in enumerate(urls):
        vu = vecs[u]
        for w in urls[i + 1:]:
            s = sum(map(mul, vu, vecs[w]))
            for a, b in ((u, w), (w, u)):
                lst = best[a]
                if len(lst) < keep or s > lst[-1][0] or (s == lst[-1][0] and b < lst[-1][1]):
                    lst.append((s, b))
                    lst.sort(key=lambda x: (-x[0], x[1]))
                    del lst[keep:]
    return {u: [(b, round(min(1.0, s), 4)) for s, b in lst] for u, lst in best.items()}


def relevance(vecs: Dict[str, List[float]]) -> Tuple[Dict[str, float], Dict[str, Any]]:
    """(url -> cosine of the page and the mean of every other page, info {"median", "mad", "threshold", "pages"}
    or {"skipped": reason}) and the low-relevance cut-off."""
    urls = list(vecs)
    n = len(urls)
    if n < 2:
        return {}, {"skipped": f"Fewer than {LOW_RELEVANCE_MIN_PAGES} pages with embeddings to tell the site's "
                               "main topic."}
    total = list(map(sum, zip(*vecs.values())))
    total_sq = sum(map(mul, total, total))
    scores: Dict[str, float] = {}
    for u in urls:
        v = vecs[u]
        pt = sum(map(mul, v, total))
        rest_sq = max(0.0, total_sq - 2 * pt + sum(map(mul, v, v)))
        scores[u] = round((pt - sum(map(mul, v, v))) / math.sqrt(rest_sq), 4) if rest_sq > 0 else 0.0
    if n < LOW_RELEVANCE_MIN_PAGES:
        return scores, {"skipped": f"Fewer than {LOW_RELEVANCE_MIN_PAGES} pages with embeddings to tell the site's "
                                   "main topic.", "pages": n}
    vals = sorted(scores.values())
    median = statistics.median(vals)
    mad = statistics.median(abs(x - median) for x in vals)
    cut = median - max(LOW_RELEVANCE_Z * 1.4826 * mad, LOW_RELEVANCE_GAP)
    return scores, {"median": round(median, 4), "mad": round(mad, 4), "threshold": round(cut, 4), "pages": n}


def derive(site, vecs: Dict[str, List[float]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """(per-URL results, relevance info, clusters info) from the run's page vectors."""
    from ..seo import semantic
    sim = similar_pages(vecs)
    rel, rel_info = relevance(vecs)
    cut = rel_info.get("threshold")
    out: Dict[str, Dict[str, Any]] = {}
    for u in vecs:
        near = sim.get(u) or []
        out[u] = {"closest": near[0][0] if near else "", "score": near[0][1] if near else None,
                  "similar": [[a, b] for a, b in near], "relevance": rel.get(u),
                  "low": bool(cut is not None and rel.get(u) is not None and rel[u] < cut)}
    res = semantic.cluster_vectors(vecs)
    if res.get("reason"):
        return out, rel_info, {"reason": res["reason"], "k": 0, "items": []}
    assign = res["assign"]
    terms: Dict[str, Counter] = {}
    forms: Dict[str, Counter] = defaultdict(Counter)
    by_url = getattr(site, "by_url", None) or {}
    for u in assign:
        r = by_url.get(u)
        terms[u] = semantic.page_terms(r, forms) if r is not None else Counter()
    display = {t: c.most_common(1)[0][0] for t, c in forms.items() if c}
    labels = semantic.label_clusters(assign, terms, display)
    shown = semantic.map_sample(assign)
    for u, (x, y) in zip(shown, semantic.project_points([vecs[u] for u in shown])):
        out[u]["x"], out[u]["y"] = x, y
    for u, c in assign.items():
        out[u]["cluster"] = c
    sizes = Counter(assign.values())
    items = [{"id": c, "label": ", ".join(labels.get(c) or []) or f"Cluster {c}", "terms": labels.get(c) or [],
              "size": sizes[c]} for c in sorted(sizes)]
    note = "" if len(shown) == len(assign) else f"{len(shown):,} of {len(assign):,} pages are placed on the map."
    return out, rel_info, {"k": res["k"], "silhouette": res["silhouette"], "items": items, "note": note}


# ---------------------------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------------------------
def max_pages_limit() -> int:
    return max(1, min(_env_int("CRAWLER_AI_EMBED_MAX_PAGES", DEFAULT_PAGES), MAX_PAGES_CAP))


def is_private(site, crawler=None) -> bool:
    from .runner import is_private as private
    return private(site, crawler)


def pool_pages(site) -> List[Any]:
    """Indexable internal HTML pages that answered 2xx, in crawl order."""
    from ..seo.semantic import pages_of
    return pages_of(site)


def current_entry(target) -> Dict[str, Any]:
    data = getattr(target, "integrations", None) or {}
    entry = data.get(JOB_SOURCE) if isinstance(data, dict) else None
    return copy.deepcopy(entry) if isinstance(entry, dict) else {}


def _store(crawler, entry: Dict[str, Any]) -> None:
    try:
        from ..integrations import put_result
    except ImportError:
        put_result = None
    if put_result is not None:
        put_result(crawler, JOB_SOURCE, entry, merge=False)
        return
    from .. import integrations
    integrations.ensure(crawler)[JOB_SOURCE] = entry
    integrations._invalidate(crawler)


def run(crawler, max_pages: Optional[int] = None, stop_event: Optional[threading.Event] = None,
        job=None) -> Dict[str, Any]:
    """Embed the crawl's pages and store the derived results; returns {"embedded", "cached", "not_checked",
    "total", "quota", "halted", "calls_used", "calls_left", "pool", "cap", "status"}."""
    site, _ = crawler.site_index(force=True)
    private = is_private(site, crawler)
    pool = pool_pages(site)
    cap = max(1, min(int(max_pages or DEFAULT_PAGES), max_pages_limit()))
    status = embed_status(private)
    key, dims = status.get("key") or "", int(status.get("dims") or 0) or DEFAULT_DIMS
    not_checked: Dict[str, str] = {}
    take = pool[:cap]
    for r in pool[cap:]:
        not_checked[r.url] = REASONS["cap"].format(n=cap)
    if job is not None:
        job.progress("Reading the pages", done=0, total=len(pool))
    texts: Dict[str, Tuple[str, str]] = {}
    for r in take:
        text, _ = page_embed_text(crawler, r)
        if len(text.split()) < MIN_WORDS:
            not_checked[r.url] = REASONS["little"].format(n=MIN_WORDS)
        else:
            texts[r.url] = (text, text_hash(text))
    vecs: Dict[str, List[float]] = {}
    need: List[str] = []
    for url, (text, h) in texts.items():
        v = _cache_get(key, h) if key else None
        if v is not None:
            vecs[url] = v
        else:
            need.append(url)
    cached = len(vecs)
    halted, quota, failed, note = "", False, 0, ""
    if need and not status.get("enabled"):
        why = status.get("reason") or REASONS["off"]
        halted = REASONS["private"] if why == PRIVATE_REASON else why
    groups = batches([texts[u][0] for u in need], status.get("provider") or "gemini") if need else []
    ai.start_budget("embeddings", len(groups) + 2)
    done = len(pool) - len(need)
    with ai.job_scope(stop_event):
        for group in groups:
            urls = [need[i] for i in group]
            if not halted and stop_event is not None and stop_event.is_set():
                halted = REASONS["stopped"]
            if halted:
                for u in urls:
                    not_checked[u] = halted
                continue
            out = embed([texts[u][0] for u in urls], private=private, dims=dims, budget="embeddings", only=key)
            if out is None:
                outcome = ai.last_outcome()
                if outcome == "quota":
                    halted, quota = REASONS["quota"], True
                elif outcome == "budget":
                    halted = REASONS["budget"].format(n=ai.budget_left("embeddings")["limit"])
                elif outcome == "stopped":
                    halted = REASONS["stopped"]
                elif outcome == "off":
                    halted = REASONS["off"]
                elif outcome == "private":
                    halted = REASONS["private"]
                else:
                    failed += 1
                    note = ai._scrub(last_note())[:160]
                    if failed >= MAX_FAILED_BATCHES:
                        halted = REASONS["failing"].format(n=failed, note=note)
                why = halted or REASONS["error"].format(note=note)
                for u in urls:
                    not_checked[u] = why
                if job is not None:
                    job.error(why)
            else:
                failed = 0
                for u, v in zip(urls, out):
                    vecs[u] = v
            done += len(urls)
            if job is not None:
                job.progress("Asking the AI for embeddings", done=done, total=len(pool))
    if job is not None:
        job.progress("Comparing the pages", done=len(pool), total=len(pool))
    ordered = {r.url: vecs[r.url] for r in take if r.url in vecs}
    results, rel_info, cl_info = derive(site, ordered) if ordered else ({}, {"skipped": "No page was embedded."},
                                                                      {"reason": "No page was embedded.", "k": 0,
                                                                       "items": []})
    for u, rec in results.items():
        rec["hash"] = texts[u][1]
    expected = (REASONS["cap"].format(n=cap), REASONS["little"].format(n=MIN_WORDS))
    stopped = halted == REASONS["stopped"]
    state = "stopped" if stopped else ("partial" if halted or any(why not in expected for why in not_checked.values())
                                       else "done")
    left = ai.budget_left("embeddings")
    entry = {"source": JOB_SOURCE, "connected": bool(results), "reason": "" if results else (halted or
             "No page was embedded."), "status": state, "fetched_at": _now(), "provider": status.get("provider", ""),
             "model": status.get("model", ""), "dims": status.get("dims", dims), "key": key, "pool": len(pool),
             "cap": cap, "pages": len(results), "calls": left["used"], "similar_threshold": SIMILAR_MIN,
             "relevance": rel_info, "clusters": cl_info, "results": results, "not_checked": not_checked}
    if results or not current_entry(crawler).get("results"):
        _store(crawler, entry)           # a run that embedded nothing keeps the crawl's earlier results
    return {"embedded": len(results), "cached": min(cached, len(results)), "not_checked": len(not_checked),
            "total": len(pool), "quota": quota, "halted": halted, "calls_used": left["used"],
            "calls_left": left["left"], "pool": len(pool), "cap": cap, "status": state}


_runs: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_runs_lock = threading.Lock()


def _remember(job_id: str, info: Dict[str, Any]) -> None:
    with _runs_lock:
        _runs[job_id] = info
        _runs.move_to_end(job_id)
        while len(_runs) > 20:
            _runs.popitem(last=False)


def start_run(crawler, max_pages: Optional[int] = None) -> Dict[str, Any]:
    """Start a background embeddings run (POST /api/embeddings/run); returns run_status() of the new job. RunError
    when it cannot start: no crawl, the crawl still running, a bad max_pages, no embedding provider, a signed-in
    crawl and a free cloud AI, or a run already going."""
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise RunError("Crawl a site first: embeddings are made from the crawled pages.")
    if max_pages is not None:
        try:
            max_pages = int(max_pages)
        except (TypeError, ValueError):
            raise RunError("max_pages must be a whole number.")
        if not 1 <= max_pages <= max_pages_limit():
            raise RunError(f"max_pages must be 1 to {max_pages_limit():,}.")
    site, _ = crawler.site_index(force=True)
    if not getattr(site, "crawl_finished", False):
        raise RunError("The crawl is still running: run embeddings once it has finished (every page is compared "
                       "with the others).")
    private = is_private(site, crawler)
    status = embed_status(private)
    if not status.get("enabled"):
        raise RunError(status.get("reason") or REASONS["off"])
    if not pool_pages(site):
        raise RunError("This crawl has no indexable HTML page to embed.")
    from ..integrations.common import JOBS, IntegrationError

    def work(job) -> None:
        info: Dict[str, Any] = {}
        _remember(job.id, info)
        out = run(crawler, max_pages=max_pages, stop_event=job.stop_event, job=job)
        info.update(out)
        msg = (f"{out['embedded']:,} pages compared ({out['cached']:,} from the cache), "
               f"{out['not_checked']:,} Not checked" + (f": {out['halted']}" if out["halted"] else "."))
        job.progress("Finished", done=out["total"], total=out["total"], message=msg)

    try:
        job = JOBS.start(JOB_SOURCE, work, owner=crawler)
    except IntegrationError:
        raise RunError("An embeddings run is already going: stop it or wait for it to finish.")
    return run_status(job.id) or {"id": job.id, "state": "running"}


def run_status(job_id: str) -> Optional[Dict[str, Any]]:
    """{"id", "state", "phase", "done", "total", "percent", "message", "errors", "embedded", "cached",
    "not_checked", "quota", "calls_used", "calls_left"} of a run (None for an unknown id)."""
    from ..integrations.common import JOBS
    job = JOBS.get(JOB_SOURCE)
    with _runs_lock:
        info = dict(_runs.get(job_id) or {})
    if job is None or job.id != job_id:
        if not info:
            return None
        return dict({"id": job_id, "state": info.get("state", "done")}, **info)
    pub = job.public()
    left = ai.budget_left("embeddings")
    out = {"id": job.id, "state": pub["status"], "phase": pub["phase"], "done": pub["done"], "total": pub["total"],
           "percent": pub["percent"], "message": pub["message"], "errors": pub["errors"],
           "started_at": pub["started_at"], "finished_at": pub["finished_at"],
           "embedded": info.get("embedded", 0), "cached": info.get("cached", 0),
           "not_checked": info.get("not_checked", 0), "quota": bool(info.get("quota")),
           "calls_used": info.get("calls_used", left["used"]), "calls_left": info.get("calls_left", left["left"])}
    if pub["status"] != "running":
        _remember(job.id, dict(info, **{k: out[k] for k in ("state", "done", "total", "message", "finished_at")}))
    return out


def stop_run(job_id: str) -> Optional[Dict[str, Any]]:
    """Stops the run (pages not reached become Not checked: stopped); None for an unknown id."""
    from ..integrations.common import JOBS
    job = JOBS.get(JOB_SOURCE)
    if job is None or job.id != job_id:
        return run_status(job_id)
    JOBS.stop(JOB_SOURCE)
    return run_status(job_id)
