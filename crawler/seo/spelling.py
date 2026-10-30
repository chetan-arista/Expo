"""
Offline spelling check for the Content tab (Spelling Errors filter, column, export and URL details).

Built on pyspellchecker (MIT; word-frequency dictionaries for en, es, fr, it, pt, de, ru, ar, lv, eu, nl, fa),
loaded lazily, one dictionary per language and shared by every crawl. Nothing leaves the server.

Crawl time (extract.py calls check_page for the content-area text of 2xx HTML pages while
CrawlConfig.spelling is on): the words of the page that the dictionary does not know, with counts, and no
suggestions (finding suggestions costs about half a second per word; they are made later, on demand).
  - Language: the page's lang attribute ("en-GB" -> en). A page without one is checked as the first chosen
    language (English when the setting is "auto"); a language without a dictionary, or one left out of the
    chosen languages, is "Not checked" with the reason.
  - Skipped (never errors): code-like lines, URLs and e-mail addresses, file names and dotted names, words with
    digits or underscores, ALL CAPS, CamelCase / mixed case, capitalised words inside a sentence (names, brands)
    and hyphenated parts of such words, a capitalised first word followed by another capitalised word (a name
    or a Title Case heading) or written capitalised inside a sentence elsewhere on the page, English words
    without a vowel (abbreviations), words under 3 letters, words cut by an ellipsis ("challengin...": an excerpt).
  - Accepted besides the dictionary: a short list of common web and business words and abbreviations of our
    own, British spellings whose American form is known (colour, organisation, centre...), accented English
    loan words (façade), words with letters outside the language's alphabet, English prefixes on known words
    (resize, multichannel), English plurals of known words, possessives and elisions, and for Spanish,
    Portuguese, French, Italian, German and Dutch the plural / inflected forms of known words.
  - Reported: a word the dictionary does not know that is one edit (a letter added, missing, changed or two
    letters swapped) away from a known word - a likely typo ("recieve", "stratergy", "informacion"). The word
    lists miss many real words (new terms, product names, inflected forms), so an unknown word that is not close
    to a known word is not counted: this keeps the filter to real mistakes. In English the close word must be
    reasonably common (NEAR_FREQ); in the other languages a difference in the last three letters only counts when
    it is an accent (the rest is more often a missing inflection than a typo).

After the crawl (site_errors / site_words): a site dictionary is applied before anything is counted.
  - Learned site words: an unknown word seen on at least LEARN_MIN_PAGES pages and LEARN_SHARE of the checked
    pages is taken as a brand or product word, unless it is one edit away from a word the site itself uses on
    as many pages, or from one of the most common words (then it is kept as a typo a template repeats).
  - User words per site: <reports_dir>/spelling/<host>.json, edited from the dashboard.
  - At most CrawlConfig.spelling["max_errors_per_page"] (50) errors per page are reported ("capped").
The Spelling Errors filter, the columns, the export and the URL details all count through site_errors, so the
numbers agree everywhere.

Public: check_page, site_errors, site_words, suggestions, languages, details (registered as "spelling"),
summary, load_dictionary, save_dictionary, pick_language.
"""
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

SUPPORTED = ("en", "es", "fr", "it", "pt", "de", "ru", "ar", "lv", "eu", "nl", "fa")
LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "fr": "French", "it": "Italian", "pt": "Portuguese",
                  "de": "German", "ru": "Russian", "ar": "Arabic", "lv": "Latvian", "eu": "Basque", "nl": "Dutch",
                  "fa": "Persian"}
DEFAULT_LANG = "en"
MAX_UNKNOWN = 200            # distinct unknown words kept per page (PageData["spelling"]["unknown"])
DEFAULT_CAP = 50             # errors reported per page (CrawlConfig.spelling["max_errors_per_page"])
LEARN_MIN_PAGES = 3          # a word on at least this many checked pages ...
LEARN_SHARE = 0.02           # ... and this share of them is learned as a site word (brand / product), unless
                             # it is a typo a template repeats: a word one edit away is a top word of at least as
                             # many of the site's pages, or one of the most common words (TYPO_COMMON_FREQ)
TYPO_COMMON_FREQ = 5_000_000 # "teh" -> "the", "adn" -> "and" (stop words are not among a page's top words)
NEAR_FREQ = 500              # English: a typo is one edit from a known word at least this common
SUGGEST_MAX_WORDS = 20       # suggestions per details() call
SUGGEST_PER_WORD = 5
MIN_LEN, MAX_LEN = 3, 30
MAX_USER_WORDS = 5000
NOT_INSTALLED = "Spelling check not installed (the pyspellchecker package is missing on the server)"

# Common web and business words the English word list lacks (our own list; lower case)
EXTRA_EN = frozenset("""
activewear adblock adblocker adtech analytics api apis athleisure backend backlink backlinks blockchain chatbot
chatbots checkbox checkboxes clickthrough cofounder cofounders compostable crawlability cybersecurity cyber
dataset datasets decarbonization devops devsecops dropdown dropdowns dropshipping ebook ebooks ecommerce edtech
elearning eshop eyewear favicon favicons fintech frontend fullstack gamification geofencing geolocation
geotargeting hashtag hashtags healthtech hreflang inbox indexability indexable influencer influencers infographic
infographics insourcing insurtech kubernetes livestream livestreaming login logins logout martech metadata
microservice microservices monetization nearshoring offboarding offshoring offsite omnichannel onboarding onsite
pageload pageview pageviews paywall podcasts popup popups proptech realtime remarketing repost reposts reshoring
reskilling retargeting retweet saas serverless signin signup signups sitemap sitemaps smartphone smartphones
smartwatch telehealth telemedicine timelines timestamp timestamps tooltip tooltips upcycled upcycling upsell
upselling upskilling uptime url urls usernames username vlog vlogs wearable wearables webinar webinars webpage
webpages wellbeing wifi wishlist wishlists workflow workflows nofollow noindex unsubscribe subscribers checkout
homepage website websites email emails online offline startup startups dashboard dashboards
etc alt seo tech admin admins bio bios pre ons multi non sub info demo demos promo promos faq faqs kpi kpis vs
approx misc dept govt intl mgmt est ltd inc corp llc pvt jan feb mar apr jun jul aug sep sept oct nov dec mon tue
tues wed thu thur thurs fri sat sun app apps blog blogs ecom reel reels emoji emojis selfie selfies bot bots org
orgs edu gov res versa brainer hoodie hoodies meme memes bing perf dup dups dedup dedupe
""".split())
# Words used in many languages' web pages (loan words; lower case)
EXTRA_ALL = frozenset("""
online offline email emails website websites blog blogs marketing software hardware startup startups cookie cookies
newsletter login logout web webinar podcast smartphone app apps wifi streaming chat chatbot
""".split())

# British spelling -> American form (English only; a word is accepted when its American form is known)
_UK_US = (("isations", "izations"), ("isation", "ization"), ("ising", "izing"), ("ised", "ized"), ("ises", "izes"),
          ("isers", "izers"), ("iser", "izer"), ("ise", "ize"), ("ysing", "yzing"), ("ysed", "yzed"), ("yse", "yze"),
          ("ours", "ors"), ("our", "or"), ("tres", "ters"), ("tre", "ter"), ("ogues", "ogs"), ("ogue", "og"),
          ("ence", "ense"), ("llers", "lers"), ("ller", "ler"), ("lled", "led"), ("lling", "ling"),
          ("mmes", "ms"), ("mme", "m"), ("ae", "e"), ("oe", "e"), ("eable", "able"))
_UK_WORDS = frozenset("aluminium cheque cheques grey kerb plough programme jewellery manoeuvre storey tyre tyres "
                      "whilst amongst learnt spelt dreamt enrol enrolment fulfil fulfilment mum".split())

# Inflected forms: (ending, replacement) tried in order; the word is known when the result is
_INFLECT = {
    "es": (("iones", "ión"), ("ones", "ón"), ("ces", "z"), ("es", ""), ("s", ""), ("as", "a"), ("os", "o"),
           ("amos", "ar"), ("emos", "er"), ("imos", "ir"), ("ando", "ar"), ("iendo", "er"), ("iendo", "ir"),
           ("ado", "ar"), ("ada", "ar"), ("ados", "ar"), ("adas", "ar"), ("ido", "er"), ("ido", "ir"),
           ("ida", "er"), ("idas", "ir"), ("idos", "ir"), ("mente", "")),
    "pt": (("ões", "ão"), ("ães", "ão"), ("ns", "m"), ("ais", "al"), ("eis", "el"), ("is", "l"), ("es", ""),
           ("s", ""), ("amos", "ar"), ("emos", "er"), ("imos", "ir"), ("ando", "ar"), ("endo", "er"),
           ("indo", "ir"), ("ado", "ar"), ("ada", "ar"), ("ido", "er"), ("ido", "ir"), ("mente", "")),
    "fr": (("aux", "al"), ("eaux", "eau"), ("s", ""), ("x", ""), ("es", "e"), ("ées", "er"), ("ée", "er"),
           ("és", "er"), ("é", "er"), ("ons", "er"), ("ez", "er"), ("ent", "er"), ("ment", "")),
    "it": (("i", "o"), ("i", "e"), ("e", "a"), ("che", "ca"), ("ghe", "ga"), ("chi", "co"), ("ghi", "go"),
           ("iamo", "are"), ("iamo", "ere"), ("iamo", "ire"), ("ato", "are"), ("ata", "are"), ("ati", "are"),
           ("ate", "are"), ("uto", "ere"), ("ito", "ire"), ("mente", "")),
    "de": (("en", ""), ("n", ""), ("e", ""), ("er", ""), ("es", ""), ("s", ""), ("nen", "")),
    "nl": (("en", ""), ("s", ""), ("'s", ""), ("e", "")),
}
# English prefixes accepted on a known word ("un" is left out: "untill" must stay a typo)
_EN_PREFIXES = ("re", "pre", "non", "co", "multi", "sub", "over", "under", "out", "cross", "inter", "micro", "self",
                "super", "mega", "mini", "anti", "auto", "hyper", "omni", "e")
_ELISION_PREFIX = frozenset("l d j m n s t c qu jusqu lorsqu puisqu quoiqu dell all dall nell sull un quell "
                            "bell sant".split())

# Characters that never belong to a word in prose (inside a token they mark code, a URL or a file name)
_SKIP_RE = re.compile(r"[\d_/\\=<>{}\[\]|~^`$%&*+#@.]")    # digits, code, URLs, e-mails, dotted names
_ENDS = ".!?:…"
_ELLIPSES = ("…", "...")
_STRIP = "\"'“”‘’«»„‚()[]{}<>,.;:!?¿¡…*•·|/\\–—-"
_BULLETS = ("-", "\u2013", "\u2014", "\u2022", "\u00b7", "|")
_HYPHENS = re.compile("[-\u2013\u2014]")
_QUOTES = "'\u2019"
_CLOSERS = "\"'\u201d\u2019)]"
_SENTENCE_END = re.compile(r"[.!?…:]['\"”’)\]]*$")
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*$")
_VOWELS = set("aeiouy")
_CODE_LINE = re.compile(r"=>|->|::|</?[a-zA-Z][^>]*>|&&|\|\||[!=]==|:=|\+\+|;\s*$|^\s*(?:\$ |>>> |#!|//|/\*)|"
                        r"\b(?:function|def|var|const|let|class|import|from|elif|println|printf)\s+[\w.]+\s*[=(:{;]")


class _NotInstalled(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Dictionaries
# ---------------------------------------------------------------------------------------------
_checkers: Dict[str, Any] = {}
_load_lock = threading.Lock()
_known_cache: Dict[Tuple[str, str], bool] = {}
_KNOWN_CACHE_MAX = 300_000


def languages() -> List[str]:
    """Language codes with a dictionary."""
    return list(SUPPORTED)


def _checker(lang: str):
    """The pyspellchecker SpellChecker of one language (loaded once; about 1 s and 15 MB for English)."""
    sc = _checkers.get(lang)
    if sc is not None:
        return sc
    with _load_lock:
        sc = _checkers.get(lang)
        if sc is None:
            try:
                from spellchecker import SpellChecker
            except ImportError:
                raise _NotInstalled(NOT_INSTALLED)
            sc = SpellChecker(language=lang, distance=1)
            _checkers[lang] = sc
    return sc


def _freq(sc, word: str) -> int:
    return int(sc.word_frequency.dictionary.get(word, 0))


def _in_dict(sc, lang: str, word: str) -> bool:
    return word in sc.word_frequency.dictionary or word in EXTRA_ALL or (lang == "en" and word in EXTRA_EN)


def _us_forms(word: str) -> List[str]:
    out = [word.replace(uk, us) for uk, us in _UK_US if uk in word]
    combined = word
    for uk, us in _UK_US:
        if uk in combined:
            combined = combined.replace(uk, us)
    if combined != word:
        out.append(combined)
    return out


def _inflected(sc, lang: str, word: str) -> bool:
    for ending, repl in _INFLECT.get(lang, ()):
        if word.endswith(ending) and len(word) - len(ending) >= 3:
            base = word[:len(word) - len(ending)] + repl
            if base in sc.word_frequency.dictionary:
                return True
    return False


def _plain(word: str) -> str:
    """The word without accents (for "only an accent differs" checks)."""
    return "".join(c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c))


_common: Dict[str, Tuple[int, int]] = {}      # language -> (90th, 97th percentile word frequency of its list)
_alphabets: Dict[str, set] = {}               # language -> the letters its word list uses


def _common_freq(sc, lang: str) -> Tuple[int, int]:
    """How often a word must occur in the language's list to count as common (the lists use different scales)."""
    hit = _common.get(lang)
    if hit is None:
        values = sorted(sc.word_frequency.dictionary.values())
        n = len(values)
        hit = (values[n * 90 // 100], values[n * 97 // 100]) if n else (0, 0)
        _common[lang] = hit
    return hit


def _near_miss(sc, lang: str, word: str) -> bool:
    """True when a known word is one edit away from `word` (a likely typo). English: that word must occur at least
    NEAR_FREQ times in the list. Other languages (whose lists miss many real and inflected words): it must be one
    of the most common words of the list and differ before the last three letters, or differ only by an accent
    and be fairly common ("informacion" for "información")."""
    if lang == "en":
        return any(c != word and _freq(sc, c) >= NEAR_FREQ for c in sc.edit_distance_1(word))
    fairly, very = _common_freq(sc, lang)
    plain = _plain(word)
    stem = len(word) - 3
    for cand in sc.edit_distance_1(word):
        if cand == word or cand not in sc.word_frequency.dictionary:
            continue
        freq = _freq(sc, cand)
        if _plain(cand) == plain:
            if freq >= fairly:
                return True                  # a missing or wrong accent
            continue
        if freq < very:
            continue
        i = 0
        while i < min(len(cand), len(word)) and cand[i] == word[i]:
            i += 1
        if i < stem:
            return True
    return False


def _is_known(sc, lang: str, word: str) -> bool:
    """`word` (lower case, ’ as ') is spelled correctly as far as this check can tell."""
    key = (lang, word)
    hit = _known_cache.get(key)
    if hit is not None:
        return hit
    ok = _judge(sc, lang, word)
    if len(_known_cache) > _KNOWN_CACHE_MAX:
        _known_cache.clear()
    _known_cache[key] = ok
    return ok


def _letters(sc, lang: str) -> set:
    hit = _alphabets.get(lang)
    if hit is None:
        hit = _alphabets[lang] = set(sc.word_frequency.letters) | set("'")
    return hit


def _judge(sc, lang: str, word: str) -> bool:
    if _in_dict(sc, lang, word):
        return True
    if not set(word) <= _letters(sc, lang):
        return True                              # letters of another alphabet: a foreign word, not a typo
    if lang == "en" and not word.isascii() and _in_dict(sc, lang, _plain(word)):
        return True                              # an accented loan word: facade -> façade, cafe -> café
    if "'" in word:
        if word.endswith("'s") and _in_dict(sc, lang, word[:-2]):
            return True
        head, _, tail = word.partition("'")
        if lang != "en" and head in _ELISION_PREFIX and (len(tail) < MIN_LEN or _is_known(sc, lang, tail)):
            return True
        if all(len(p) < MIN_LEN or _in_dict(sc, lang, p) for p in word.split("'")):
            return True
    if word.endswith("s'") and _in_dict(sc, lang, word[:-1]):
        return True
    if lang == "en":
        if word in _UK_WORDS or any(_in_dict(sc, lang, f) for f in _us_forms(word)):
            return True
        if any(word.startswith(pre) and len(word) - len(pre) >= MIN_LEN and _freq(sc, word[len(pre):]) >= 100
               for pre in _EN_PREFIXES):
            return True
        if word.endswith("s") and len(word) > MIN_LEN and any(
                _in_dict(sc, lang, base) for base in (word[:-1], word[:-2] if word.endswith("es") else "",
                                                      word[:-3] + "y" if word.endswith("ies") else "") if base):
            return True                            # plural of a known word: memes, hoodies
    elif _inflected(sc, lang, word):
        return True
    return not _near_miss(sc, lang, word)        # only likely typos are reported


# ---------------------------------------------------------------------------------------------
# Words to check
# ---------------------------------------------------------------------------------------------
def _looks_like_code(line: str) -> bool:
    """A line of program code, a shell command or markup shown on the page (skipped as a whole)."""
    if len(line) < 6:
        return False
    symbols = sum(line.count(c) for c in "{}[]();=<>$\\|`")
    words = max(1, len(line.split()))
    if symbols >= 4 and symbols / words >= 0.5:
        return True
    return symbols >= 2 and bool(_CODE_LINE.search(line))


def _skip_token(core: str) -> bool:
    """A token that is not a prose word: URL, e-mail, file or dotted name (example.com, node.js, e.g), code,
    number or a word with digits."""
    return bool(_SKIP_RE.search(core))


def _chunks(line: str) -> List[Tuple[str, bool]]:
    """(token without surrounding punctuation, starts a sentence) for one line; skipped tokens come back as ""."""
    out: List[Tuple[str, bool]] = []
    start = True                                  # each line (heading, paragraph, list item) starts a sentence
    for chunk in line.split():
        core = chunk.strip(_STRIP)
        if not core:
            start = start or chunk in _BULLETS or bool(_SENTENCE_END.search(chunk))
            continue
        if core != chunk and (chunk.rstrip(_CLOSERS).endswith(_ELLIPSES) or chunk.startswith(_ELLIPSES)):
            core = ""                              # cut by an ellipsis: "challengin..." is an excerpt
        elif not core.isalpha() and _SKIP_RE.search(core):
            core = ""
        out.append((core, start))
        last = chunk[-1]
        start = last in _ENDS or (last in _CLOSERS and bool(_SENTENCE_END.search(chunk)))
    return out


def _title_like(core: str) -> bool:
    return bool(core) and core[0].isupper() and not core.isupper()


def candidates(text: str) -> Iterator[str]:
    """The words of `text` the check looks up, as written (see the module docstring for what is skipped)."""
    lines = [_chunks(line) for line in (text or "").split("\n") if line.strip() and not _looks_like_code(line)]
    names = {_HYPHENS.split(core)[0].lower() for chunks in lines for core, at_start in chunks
             if not at_start and core and core[0].isupper() and not core.isupper()}   # capitalised inside a
    for chunks in lines:                                                               # sentence: a name
        for n, (core, at_start) in enumerate(chunks):
            if not core:
                continue
            if core.isalpha():                     # a plain word: the common case, checked quickly
                if not core.islower():
                    if core.isupper() or core[1:] != core[1:].lower():
                        continue                   # ALL CAPS or CamelCase: acronyms, iPhone, JavaScript
                    if not at_start:
                        continue                   # capitalised inside a sentence: a name or brand
                    nxt = chunks[n + 1][0] if n + 1 < len(chunks) else ""
                    if _title_like(nxt) or core.lower() in names:
                        continue                   # "Lijo Thomas", "Shopify Development": a name or a heading
                if MIN_LEN <= len(core) <= MAX_LEN:
                    yield core
                continue
            parts = [p.strip(_QUOTES) for p in _HYPHENS.split(core)]
            if any(p[:1].isupper() for p in parts[1:]):
                continue                           # Coca-Cola, Rolls-Royce: a name
            if at_start and _title_like(core):
                nxt = chunks[n + 1][0] if n + 1 < len(chunks) else ""
                if _title_like(nxt) or parts[0].lower() in names:
                    continue
            for i, part in enumerate(parts):
                if not (MIN_LEN <= len(part) <= MAX_LEN) or not _WORD.match(part):
                    continue
                if part.isupper() or part[1:] != part[1:].lower():
                    continue                       # ALL CAPS / CamelCase
                if part[0].isupper() and not (at_start and i == 0):
                    continue                       # capitalised inside a sentence: a name or brand
                yield part


def _norm(word: str) -> str:
    return word.lower().replace("’", "'")


# ---------------------------------------------------------------------------------------------
# Crawl time: one page
# ---------------------------------------------------------------------------------------------
def _allowed(options: Optional[Dict[str, Any]]) -> List[str]:
    """The languages chosen in CrawlConfig.spelling["languages"] ([] = "auto": every dictionary)."""
    chosen = (options or {}).get("languages", "auto")
    if isinstance(chosen, str):
        chosen = [] if chosen.strip().lower() in ("", "auto") else re.split(r"[\s,;]+", chosen)
    out = []
    for c in chosen or []:
        code = str(c).strip().lower().replace("_", "-").split("-")[0]
        if code and code != "auto" and code not in out:
            out.append(code)
    return out


def pick_language(declared: str, options: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
    """(dictionary language, "" or why the page is not checked) for a page's lang attribute."""
    allowed = _allowed(options)
    primary = (declared or "").strip().lower().replace("_", "-").split("-")[0]
    if not primary:
        code = next((c for c in allowed if c in SUPPORTED), DEFAULT_LANG if not allowed else "")
        if not code:
            return allowed[0], f"No spelling dictionary for \"{allowed[0]}\", the language chosen for pages " \
                               "that declare none."
        return code, ""
    if primary not in SUPPORTED:
        return primary, (f"No spelling dictionary for the page language \"{declared.strip()}\" (dictionaries: "
                         + ", ".join(SUPPORTED) + ").")
    if allowed and primary not in allowed:
        return primary, (f"The page language \"{declared.strip()}\" is not one of the spelling languages chosen ("
                         + ", ".join(allowed) + ").")
    return primary, ""


def check_page(text: str, lang: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """PageData["spelling"] for one page's content-area text: {"lang", "declared", "checked", "reason",
    "words", "unknown": {word: count} (<= MAX_UNKNOWN distinct, most frequent first), "capped"}.
    No suggestions here (see suggestions())."""
    declared = str(lang or "").strip()
    code, why = pick_language(declared, options)
    base = {"lang": code, "declared": declared, "checked": False, "reason": why, "words": 0, "unknown": {},
            "capped": False}
    if why:
        return base
    try:
        sc = _checker(code)
    except _NotInstalled as e:
        return dict(base, reason=str(e))
    except Exception as e:
        return dict(base, reason=f"The {LANGUAGE_NAMES.get(code, code)} dictionary could not be loaded "
                                 f"({type(e).__name__}).")
    counts: Counter = Counter()
    words = 0
    for raw in candidates(text):
        words += 1
        w = _norm(raw)
        if not _is_known(sc, code, w):
            if code == "en" and not (_VOWELS & set(w)):
                continue                           # crm, pdf, html: an abbreviation, not a word
            counts[w] += 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return dict(base, checked=True, words=words, unknown=dict(ranked[:MAX_UNKNOWN]),
                capped=len(ranked) > MAX_UNKNOWN)


# ---------------------------------------------------------------------------------------------
# User dictionary per site (<reports_dir>/spelling/<host>.json)
# ---------------------------------------------------------------------------------------------
_dict_lock = threading.RLock()
_dict_version: Dict[str, int] = {}
_USER_WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*$")


def _reports_dir(reports_dir: Optional[str] = None) -> str:
    return reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def host_key(url_or_host: str) -> str:
    """The site a dictionary belongs to: the host name without "www." ("" when there is none)."""
    s = str(url_or_host or "").strip().lower()
    host = (urlparse(s if "//" in s else f"//{s}").hostname or "") if s else ""
    host = host[4:] if host.startswith("www.") else host
    return re.sub(r"[^a-z0-9.-]", "_", host)[:200]


def _dict_path(host: str, reports_dir: Optional[str] = None) -> str:
    return os.path.join(_reports_dir(reports_dir), "spelling", f"{host_key(host) or 'default'}.json")


def clean_words(words: Any) -> List[str]:
    """User words as stored: lower case, letters with inner apostrophes / hyphens, 1-50 characters, unique."""
    out = set()
    for w in words if isinstance(words, (list, tuple, set)) else re.split(r"[\s,;]+", str(words or "")):
        w = _norm(str(w or "").strip().strip(_STRIP.replace("-", "")))
        if 1 <= len(w) <= 50 and _USER_WORD.match(w):
            out.add(w)
    return sorted(out)[:MAX_USER_WORDS]


def load_dictionary(host: str, reports_dir: Optional[str] = None) -> List[str]:
    """The words the team added for this site."""
    with _dict_lock:
        try:
            with open(_dict_path(host, reports_dir), "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
    return clean_words(data.get("words") if isinstance(data, dict) else [])


def save_dictionary(host: str, words: List[str], reports_dir: Optional[str] = None) -> None:
    """Replace this site's user words (atomic write)."""
    words = clean_words(words)
    path = _dict_path(host, reports_dir)
    with _dict_lock:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".spelling.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"host": host_key(host), "words": words,
                           "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f, ensure_ascii=False,
                          indent=1)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        key = host_key(host)
        _dict_version[key] = _dict_version.get(key, 0) + 1
        _mtimes.pop(path, None)


def update_dictionary(host: str, add: Any = None, remove: Any = None,
                      reports_dir: Optional[str] = None) -> List[str]:
    """Add and / or remove user words of one site; returns the words now stored."""
    with _dict_lock:
        words = set(load_dictionary(host, reports_dir))
        words |= set(clean_words(add or []))
        words -= set(clean_words(remove or []))
        save_dictionary(host, sorted(words), reports_dir)
        return load_dictionary(host, reports_dir)


_mtimes: Dict[str, Tuple[float, int]] = {}    # dictionary path -> (when read, mtime): read at most every 2 s


def _version(host: str, reports_dir: Optional[str]) -> Tuple[int, int]:
    """Changes when the site's user words change: at once for edits made here (save_dictionary), within two
    seconds for edits of the file by another process."""
    path = _dict_path(host, reports_dir)
    now = time.monotonic()
    hit = _mtimes.get(path)
    if hit is not None and now - hit[0] < 2.0:
        mtime = hit[1]
    else:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = 0
        _mtimes[path] = (now, mtime)
    return _dict_version.get(host_key(host), 0), mtime


# ---------------------------------------------------------------------------------------------
# After the crawl: site dictionary and error counts (every surface counts through site_errors)
# ---------------------------------------------------------------------------------------------
def _seo(r) -> Dict[str, Any]:
    return getattr(r, "seo", None) or {}


def page_spelling(site, url: str) -> Optional[Dict[str, Any]]:
    """PageData["spelling"] of one URL (None when the page was not checked at all)."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    sp = _seo(r).get("spelling") if r is not None else None
    return sp if isinstance(sp, dict) else None


def _site_host(site) -> str:
    return host_key(getattr(site, "start_url", "") or getattr(site, "base_domain", ""))


def _cap(site) -> int:
    opts = getattr(getattr(site, "config", None), "spelling", None)
    try:
        return max(1, int((opts or {}).get("max_errors_per_page") or DEFAULT_CAP))
    except (TypeError, ValueError, AttributeError):
        return DEFAULT_CAP


def _site_vocabulary(site) -> Counter:
    """How many of the site's pages have each word among their top content words (PageData top_terms)."""
    vocab: Counter = Counter()
    for r in getattr(site, "pages", None) or ():
        terms = set()
        for item in _seo(r).get("top_terms") or ():
            term = item[0] if isinstance(item, (list, tuple)) and item else item
            if isinstance(term, str):
                terms.add(term.lower())
        vocab.update(terms)
    return vocab


def _likely_typo(word: str, pages: int, lang: str, vocab: Counter) -> bool:
    """A repeated unknown word that is a typo a template repeats, not a brand or product name: one edit away from
    a known word that is a top word of at least as many of the site's pages ("stratergy" on a site that says
    "strategy" everywhere), or from one of the most common words of the language ("teh"). A word that differs from
    it only in its last letter is taken as a name (Keepa, Canva)."""
    try:
        sc = _checker(lang if lang in SUPPORTED else DEFAULT_LANG)
    except Exception:
        return False
    for c in sc.edit_distance_1(word):
        if c == word or c not in sc.word_frequency.dictionary:
            continue
        i = 0
        while i < min(len(c), len(word)) and c[i] == word[i]:
            i += 1
        if i >= len(word) - 1 and i >= len(c) - 1:
            continue                         # only the last letter differs: Keepa, Canva are names, not typos
        if vocab.get(c, 0) >= pages or _freq(sc, c) >= TYPO_COMMON_FREQ:
            return True
    return False


def site_words(site, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The site dictionary of this crawl: {"host", "learned": {word: pages}, "kept": {word: pages} (repeated
    but kept as likely typos, see _likely_typo), "user": [words], "all": set,
    "threshold": pages, "pages_checked"}. Cached on the site; the cache follows edits of the user dictionary."""
    host = _site_host(site)
    version = _version(host, reports_dir)
    cache = getattr(site, "_spelling_site_words", None)
    if isinstance(cache, dict) and cache.get("version") == version:
        return cache
    pages = 0
    df: Counter = Counter()
    lang_of: Dict[str, str] = {}
    for r in getattr(site, "pages", None) or ():
        sp = _seo(r).get("spelling")
        if not isinstance(sp, dict) or not sp.get("checked"):
            continue
        pages += 1
        for w in sp.get("unknown") or {}:
            df[w] += 1
            lang_of.setdefault(w, str(sp.get("lang") or DEFAULT_LANG))
    threshold = max(LEARN_MIN_PAGES, math.ceil(LEARN_SHARE * pages))
    learned: Dict[str, int] = {}
    kept: Dict[str, int] = {}
    repeated = [w for w, n in df.items() if n >= threshold]
    vocab = _site_vocabulary(site) if repeated else Counter()
    for w in repeated:
        (kept if _likely_typo(w, df[w], lang_of[w], vocab) else learned)[w] = df[w]
    user = load_dictionary(host, reports_dir)
    out = {"host": host, "version": version, "learned": learned, "kept": kept, "user": user,
           "all": set(learned) | set(user), "threshold": threshold, "pages_checked": pages, "errors": {}}
    try:
        site._spelling_site_words = out
    except Exception:
        pass
    return out


def site_errors(site, url: str, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The spelling errors of one URL after the site dictionary and the per-page cap:
    {"checked", "reason", "lang", "error_count", "errors": [{"word", "count"}], "capped", "occurrences",
    "site_words": [words of the page taken as site words]}."""
    words = site_words(site, reports_dir)
    memo = words["errors"]
    if url in memo:
        return memo[url]
    sp = page_spelling(site, url)
    if sp is None or not sp.get("checked"):
        sp = sp or {}
        out = {"checked": False, "reason": str(sp.get("reason") or ""), "lang": str(sp.get("lang") or ""),
               "error_count": 0, "errors": [], "capped": False, "occurrences": 0, "site_words": []}
    else:
        ignore = words["all"]
        unknown = sp.get("unknown") or {}
        errs = sorted(((w, int(c)) for w, c in unknown.items() if w not in ignore), key=lambda wc: (-wc[1], wc[0]))
        cap = _cap(site)
        shown = errs[:cap]
        out = {"checked": True, "reason": "", "lang": str(sp.get("lang") or ""), "error_count": len(shown),
               "errors": [{"word": w, "count": c} for w, c in shown],
               "capped": len(errs) > cap or bool(sp.get("capped")), "occurrences": sum(c for _, c in shown),
               "site_words": sorted(w for w in unknown if w in ignore)}
    memo[url] = out
    return out


_suggest_cache: Dict[Tuple[str, str], List[str]] = {}


def suggestions(words: List[str], lang: str = DEFAULT_LANG) -> Dict[str, List[str]]:
    """Likely intended words for each misspelled word (one edit away, most common first; cached per word).
    Used by the URL details and the export only (not at crawl time)."""
    lang = lang if lang in SUPPORTED else DEFAULT_LANG
    try:
        sc = _checker(lang)
    except Exception:
        return {}
    out: Dict[str, List[str]] = {}
    for w in words or []:
        w = _norm(str(w or ""))
        if not w:
            continue
        key = (lang, w)
        if key not in _suggest_cache:
            found = [c for c in sc.edit_distance_1(w) if c != w and c in sc.word_frequency.dictionary]
            found.sort(key=lambda c: (-_freq(sc, c), c))
            if len(_suggest_cache) > 50_000:
                _suggest_cache.clear()
            _suggest_cache[key] = found[:SUGGEST_PER_WORD]
        out[w] = list(_suggest_cache[key])
    return out


def not_checked_reason(site, r) -> str:
    """Why a 2xx HTML page has no spelling result."""
    cfg = getattr(site, "config", None)
    opts = getattr(cfg, "spelling", None) if cfg is not None else None
    if isinstance(opts, dict) and not opts.get("enabled", True):
        return "The spelling check was off for this crawl (Configuration > Content > Spelling)."
    return ("The spelling check did not run for this page (it was off, or the page was read by an older version "
            "of the crawler).")


def _is_page(r) -> bool:
    try:
        from .tabs.internal import category_of, is_html_doc
        return is_html_doc(r) and category_of(r) == "2xx"
    except Exception:
        return bool(_seo(r).get("is_html"))


def details(site, url: str) -> Optional[Dict[str, Any]]:
    """The Spelling section of the URL details (registry "spelling"): the errors with suggestions (for the first
    SUGGEST_MAX_WORDS words), the language checked, the site words seen on the page; {"not_checked": reason}
    when the page was not checked; None for a URL that is not a 2xx HTML page."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not _is_page(r):
        return None
    sp = page_spelling(site, url)
    if sp is None:
        return {"not_checked": not_checked_reason(site, r)}
    if not sp.get("checked"):
        return {"not_checked": str(sp.get("reason") or "The spelling check did not run for this page."),
                "lang": str(sp.get("lang") or ""), "declared": str(sp.get("declared") or "")}
    e = site_errors(site, url)
    lang = str(sp.get("lang") or DEFAULT_LANG)
    sugg = suggestions([x["word"] for x in e["errors"][:SUGGEST_MAX_WORDS]], lang)
    indexable = bool((getattr(site, "indexable", None) or {}).get(url, False))
    return {"lang": lang, "language": LANGUAGE_NAMES.get(lang, lang), "declared": str(sp.get("declared") or ""),
            "assumed": not str(sp.get("declared") or "").strip(), "words_checked": int(sp.get("words") or 0),
            "error_count": e["error_count"], "capped": e["capped"], "cap": _cap(site), "occurrences": e["occurrences"],
            "errors": [dict(x, suggestions=sugg.get(x["word"])) if i < SUGGEST_MAX_WORDS else dict(x, suggestions=None)
                       for i, x in enumerate(e["errors"])],
            "site_words": e["site_words"], "indexable": indexable,
            "counted": indexable and e["error_count"] > 0}


def summary(site, top: int = 20) -> Dict[str, Any]:
    """Right-panel numbers: pages checked / with errors (indexable, as the filter) / not checked (with reasons),
    languages, the most common misspelled words and the site dictionary size."""
    words = site_words(site)
    checked = with_errors = 0
    not_checked: Counter = Counter()
    langs: Counter = Counter()
    word_pages: Counter = Counter()
    word_count: Counter = Counter()
    indexable = getattr(site, "indexable", None) or {}
    for r in getattr(site, "pages", None) or ():
        if not _is_page(r):
            continue
        sp = _seo(r).get("spelling")
        if not isinstance(sp, dict) or not sp.get("checked"):
            not_checked[str((sp or {}).get("reason") or not_checked_reason(site, r))] += 1
            continue
        checked += 1
        langs[str(sp.get("lang") or "")] += 1
        if not indexable.get(r.url, False):
            continue
        e = site_errors(site, r.url)
        if e["error_count"] > 0:
            with_errors += 1
        for x in e["errors"]:
            word_pages[x["word"]] += 1
            word_count[x["word"]] += x["count"]
    return {"pages_checked": checked, "pages_with_errors": with_errors,
            "pages_not_checked": sum(not_checked.values()),
            "not_checked_reasons": [{"reason": k, "pages": v} for k, v in not_checked.most_common()],
            "languages": [{"lang": k, "language": LANGUAGE_NAMES.get(k, k), "pages": v}
                          for k, v in langs.most_common()],
            "top_words": [{"word": w, "pages": n, "occurrences": word_count[w]}
                          for w, n in word_pages.most_common(top)],
            "learned": [{"word": w, "pages": n} for w, n in sorted(words["learned"].items(), key=lambda kv: -kv[1])],
            "kept_as_typos": [{"word": w, "pages": n} for w, n in sorted(words["kept"].items(), key=lambda kv: -kv[1])],
            "user_words": len(words["user"]), "learn_threshold": words["threshold"], "cap": _cap(site)}


def _register() -> None:
    try:
        from .registry import register_details
        register_details("spelling", details)
    except Exception:
        pass


_register()
