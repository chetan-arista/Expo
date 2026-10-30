"""
N-grams of one page (the URL details' N-grams tab; GET /api/crawl/ngrams): every phrase of n words (1 to 6) in
the page's text, with how often it occurs, so the team sees which phrases a page repeats and whether its main
phrases are in the title, H1 and meta description.

Text: the page's stored HTML (crawler/seo/page_text.py), else one polite fetch of the page with the crawl's own
session (on-site URLs only, no redirects, size-capped); the text read last is kept a few minutes so switching
n, the content area or the stop-word option does not fetch again.
  - content_area=True: the main content (no nav, footer, scripts, hidden parts; the crawl's content-area
    selectors when set), the text the word count reads; False: every visible text of the <body>.
Counting (our own rules):
  - words are letters and digits; an apostrophe or hyphen inside a word keeps it one word (don't, e-mail), and
    numbers keep their separators (1,000  3.5  24/7). Everything is compared in lower case.
  - a phrase never runs across a block (heading, paragraph, list item, cell) or across punctuation that ends a
    sentence or clause (. , ; : ! ? brackets, a dash between words, |, a bullet); quotes, & and / do not break.
  - skip_stop_words: unigrams that are common words (the, and, of ...), and longer phrases that start or end with
    one, are left out (the page's language: English, Spanish, French, German, Italian, Portuguese or Dutch;
    English for others).
  - density: the share of the page's words that belong to the phrase, count x n / words x 100.

Public: ngrams(crawler, url, n=2, content_area=True, limit=100, skip_stop_words=False), phrases(), segments(),
export_rows(), to_csv(), EXPORT_HEADERS, NgramError.
"""
import csv
import io
import re
import threading
import time
from collections import Counter, OrderedDict
from typing import Any, Dict, List, Tuple

MIN_N, MAX_N = 1, 6
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
TEXT_CHARS = 400_000           # page text read
CACHE_SIZE = 16
CACHE_SECONDS = 600
EXPORT_HEADERS = ["N-gram", "Words", "Count", "Density %", "In Title", "In H1", "In Meta Description"]

_TOKEN = re.compile(r"\d+(?:[.,:/]\d+)+|[^\W_]+(?:['\-][^\W_]+)*", re.UNICODE)
_BREAK = frozenset(".,;:!?()[]{}|•·…–—-¡¿")

# Common function words per language (our own short lists; English uses the crawler's top-terms list too)
_STOP = {
    "es": "a al algo como con de del desde donde el ella en entre es esta este esto la las le lo los mas me mi muy "
          "no nos o para pero por que se si sin sobre su sus te tu un una uno y ya",
    "fr": "a au aux avec ce ces dans de des du elle en est et il ils je la le les leur lui ma mais me mes mon ne "
          "nos notre nous on ou par pas pour qu que qui sa se ses son sur ta te tu un une vos votre vous y",
    "de": "aber als am an auch auf aus bei bin bis das dass dem den der des die du ein eine einem einen einer er "
          "es fur für hat ich ihr im in ist ja mit nach nicht noch nur oder sich sie sind so uber über um und uns "
          "von vor war was wenn wie wir zu zum zur",
    "it": "a ad al alla alle anche che chi con da dal dalla de dei del della delle di e ed gli ha i il in la le lo "
          "ma mi ne nei nel nella non o per piu più se si sono su sua suo tra un una uno",
    "pt": "a ao aos as com como da das de do dos e ela ele em entre era essa esse esta este eu foi ha isso já la "
          "lhe mais mas me meu minha na nas no nos não o os ou para pela pelo por que se sem seu sua são também "
          "um uma",
    "nl": "aan al als bij dan dat de die dit door een en er had het hij hoe ik in is je maar met mij naar niet "
          "nog of om ook op over te tot uit van voor was wat we wel wie wij zal ze zich zijn zo",
}
_EN_EXTRA = "i it's its i'm you're we're they're don't can't won't isn't aren't"


class NgramError(ValueError):
    """A bad request (n or limit out of range); the message says which."""


def _stop_words(lang: str) -> frozenset:
    base = (lang or "en").lower().replace("_", "-").split("-")[0]
    if base in _STOP:
        return frozenset(_STOP[base].split())
    try:
        from .extract import STOP_WORDS
    except Exception:
        STOP_WORDS = frozenset()
    return frozenset(STOP_WORDS) | frozenset(_EN_EXTRA.split())


def segments(text: str) -> List[List[str]]:
    """The text as runs of lower-case words that phrases may span (see the module docstring)."""
    out: List[List[str]] = []
    for line in (text or "").replace("’", "'").replace("‘", "'").splitlines():
        cur: List[str] = []
        last = 0
        for m in _TOKEN.finditer(line):
            gap = line[last:m.start()]
            if cur and any(ch in _BREAK for ch in gap):
                out.append(cur)
                cur = []
            cur.append(m.group(0).lower().strip("'"))
            last = m.end()
        if cur:
            out.append(cur)
    return out


def phrases(text: str, n: int, skip_stop_words: bool = False, lang: str = "en") -> Tuple[Counter, int]:
    """(phrase -> count, number of words) for every n-word phrase of the text."""
    stop = _stop_words(lang) if skip_stop_words else frozenset()
    counts: Counter = Counter()
    words = 0
    for seg in segments(text):
        words += len(seg)
        for i in range(len(seg) - n + 1):
            gram = seg[i:i + n]
            if stop and (gram[0] in stop or gram[-1] in stop):
                continue
            counts[" ".join(gram)] += 1
    return counts, words


def _phrase_set(text: str, n: int) -> set:
    out = set()
    for seg in segments(text):
        for i in range(len(seg) - n + 1):
            out.add(" ".join(seg[i:i + n]))
    return out


_text_cache: "OrderedDict[Tuple[int, str, bool], Tuple[float, str, str]]" = OrderedDict()
_cache_lock = threading.Lock()


def _page_text(crawler, url: str, content_area: bool) -> Tuple[str, str]:
    key = (id(crawler), url, bool(content_area))
    now = time.monotonic()
    with _cache_lock:
        hit = _text_cache.get(key)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            _text_cache.move_to_end(key)
            return hit[1], hit[2]
    from .page_text import page_text
    text, source = page_text(crawler, url, content_area=content_area, max_chars=TEXT_CHARS, allow_fetch=True)
    if text:
        with _cache_lock:
            _text_cache[key] = (now, text, source)
            _text_cache.move_to_end(key)
            while len(_text_cache) > CACHE_SIZE:
                _text_cache.popitem(last=False)
    return text, source


def _result(crawler, url: str):
    from .page_text import _result as find
    return find(crawler, url)


def _first(seo: Dict[str, Any], key: str) -> str:
    for v in seo.get(key) or []:
        s = (v if isinstance(v, str) else str(v or "")).strip()
        if s:
            return s
    return ""


def _empty(url: str, n: int, content_area: bool, limit: int, skip: bool, reason: str) -> Dict[str, Any]:
    return {"url": url, "n": n, "content_area": content_area, "skip_stop_words": skip, "source": "",
            "language": "", "words": 0, "total": 0, "distinct": 0, "limit": limit, "truncated": False,
            "ngrams": [], "reason": reason}


def ngrams(crawler, url: str, n: int = 2, content_area: bool = True, limit: int = DEFAULT_LIMIT,
           skip_stop_words: bool = False) -> Dict[str, Any]:
    """{"url", "n", "content_area", "skip_stop_words", "source" ("stored" | "fetched" | ""), "language", "words",
    "total" (phrases counted), "distinct", "limit", "truncated", "ngrams": [{"phrase", "count", "density",
    "in_title", "in_h1", "in_description"}] (most frequent first), "reason" ("" or why the page could not be
    read)}. NgramError for n outside 1-6 or limit outside 1-1000."""
    try:
        n = int(n)
        limit = int(limit)
    except (TypeError, ValueError):
        raise NgramError("n and limit must be whole numbers.")
    if not MIN_N <= n <= MAX_N:
        raise NgramError(f"n must be {MIN_N} to {MAX_N} words.")
    if not 1 <= limit <= MAX_LIMIT:
        raise NgramError(f"limit must be 1 to {MAX_LIMIT:,}.")
    content_area, skip = bool(content_area), bool(skip_stop_words)
    url = str(url or "").strip()
    r = _result(crawler, url) if crawler is not None and url else None
    if r is None:
        return _empty(url, n, content_area, limit, skip, "This URL is not part of the crawl.")
    seo = getattr(r, "seo", None) or {}
    status = getattr(r, "status_code", None)
    if not seo.get("is_html") or status is None or not 200 <= int(status) < 300:
        return _empty(url, n, content_area, limit, skip, "N-grams are counted for HTML pages that loaded (2xx).")
    text, source = _page_text(crawler, url, content_area)
    if not text:
        return _empty(url, n, content_area, limit, skip, "Not checked: the page's HTML was not stored with the crawl "
                                                         "and it could not be read again.")
    lang = str(seo.get("language") or (seo.get("spelling") or {}).get("lang") or "en")
    counts, words = phrases(text, n, skip, lang)
    title = _phrase_set(_first(seo, "titles"), n)
    h1 = _phrase_set(_first(seo, "h1"), n)
    desc = _phrase_set(_first(seo, "meta_descriptions"), n)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    rows = [{"phrase": p, "count": c, "density": round(c * n / words * 100, 2) if words else 0.0,
             "in_title": p in title, "in_h1": p in h1, "in_description": p in desc} for p, c in ordered[:limit]]
    return {"url": url, "n": n, "content_area": content_area, "skip_stop_words": skip, "source": source,
            "language": lang.split("-")[0].lower(), "words": words, "total": sum(counts.values()),
            "distinct": len(counts), "limit": limit, "truncated": len(ordered) > limit, "ngrams": rows,
            "reason": ""}


def export_rows(result: Dict[str, Any]) -> List[List[Any]]:
    """The n-grams of ngrams() as export rows (EXPORT_HEADERS)."""
    n = result.get("n") or 0
    return [[g["phrase"], n, g["count"], g["density"], "Yes" if g["in_title"] else "No",
             "Yes" if g["in_h1"] else "No", "Yes" if g["in_description"] else "No"]
            for g in result.get("ngrams") or []]


def to_csv(result: Dict[str, Any]) -> str:
    """CSV text of ngrams() (a header row, then one row per phrase; formula-like cells are quoted as text)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(EXPORT_HEADERS)
    for row in export_rows(result):
        w.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in row])
    return buf.getvalue()
