"""
Per-page SEO extraction: one HTML parse -> one PageData dict (keys and meanings in model.py).

extract_page()          for any response with an HTML body (2xx and 4xx/5xx error pages alike)
extract_headers_only()  for files, HEAD responses, redirects and failed requests (no body to read)
"""
import hashlib
import html as html_lib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Pattern, Sequence, Set, Tuple
from urllib.parse import unquote, urlparse

from ..config import MAX_CUSTOM_RULES
from ..page_parser import SourceLines, make_soup, normalize_cached, tag_index
from ..url_utils import clean_hostname, get_domain, is_internal_url, join_url
from .analysis import minhash_signature
from .model import EMPTY_PAGE

# Elements whose text is never shown to a visitor
HIDDEN_TEXT_TAGS = {"script", "style", "noscript", "template", "title"}
# Screaming Frog's default content area is <body> without <nav> and <footer>; word count, readability,
# duplicate hashes and near duplicates are measured on it (checked against SF's export of the reference site)
CONTENT_EXCLUDED_TAGS = {"nav", "footer"}
# Elements that start a new line of text (an HTML parser's block elements); text inside any other
# element (a, span, strong, ...) runs on without a space, as a browser shows it: "<b>Hello</b>world" is one word
TEXT_BLOCK_TAGS = frozenset("""
    address applet article aside audio blockquote body canvas caption center col colgroup dd del details dir div
    dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html ins
    li listing main marquee math menu nav noframes noscript ol p plaintext pre section svg table tbody td template
    tfoot th thead tr ul video
""".split())
# Elements a <head> may contain; anything else closes the head early in a browser
HEAD_ALLOWED = {"title", "meta", "link", "script", "style", "base", "noscript", "template"}
ROBOTS_META_NAMES = {"robots", "googlebot"}
DIRECTIVE_TOKENS = (
    "index", "noindex", "follow", "nofollow", "none", "noarchive", "nosnippet", "max-snippet",
    "max-image-preview", "max-video-preview", "noimageindex", "notranslate", "unavailable_after",
    "noodp", "noydir",
)
_VALUE_DIRECTIVES = {"max-snippet", "max-image-preview", "max-video-preview", "unavailable_after"}
_ROBOT_PREFIXES = ("googlebot", "robots", "*")

# Soft 404: wording an error page uses. Title/h1 checks are broad; body checks need a full phrase.
SOFT_404_HEADING_PATTERNS = (
    ("404", re.compile(r"\b404\b")),
    ("not found", re.compile(r"\bnot\s+found\b")),
    ("page doesn't exist", re.compile(r"page\s+(?:doesn['’]?t|does\s+not)\s+exist")),
    ("no longer exists", re.compile(r"no\s+longer\s+(?:exists|available)")),
    ("page cannot be found", re.compile(r"(?:can['’]?t|cannot|could\s+not)\s+be\s+found")),
    ("error page", re.compile(r"\berror\s+page\b|\bpage\s+error\b")),
)
SOFT_404_BODY_PATTERNS = (
    ("page not found", re.compile(r"\bpage\s+not\s+found\b")),
    ("error 404", re.compile(r"\b(?:error\s+404|404\s+error|404\s+not\s+found)\b")),
    ("page doesn't exist", re.compile(r"(?:this|the)\s+page\s+(?:doesn['’]?t|does\s+not)\s+exist")),
    ("page cannot be found", re.compile(r"page\s+(?:you\s+(?:are\s+looking\s+for|requested)\s+)?(?:can['’]?t|cannot|could\s+not)\s+be\s+found")),
)

READABILITY_BANDS = ((90, "Very Easy"), (80, "Easy"), (70, "Fairly Easy"), (60, "Normal"),
                     (50, "Fairly Hard"), (30, "Hard"))
_WS = re.compile(r"\s+")
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*", re.UNICODE)
# Sentence (clause) breaks: . ! ? and commas followed by a space. Screaming Frog's sentence counts on the
# reference crawl are matched far better with commas than without; its Flesch scores then agree too.
_SENTENCE_END = re.compile(r"[.!?…,]+(?=[\s\"'”’)\]]|$)")
_VOWEL_GROUPS = re.compile(r"[aeiouy]+")
_LINK_PARAM = re.compile(r';\s*([a-zA-Z*-]+)\s*=\s*(?:"([^"]*)"|([^;,\s]*))')
_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\"]+)", re.I)
_HEADER_REFRESH_URL = re.compile(r"url\s*=\s*['\"]?([^'\";]+)", re.I)    # as tabs/internal.http_refresh_target
_HEAD_OPEN = re.compile(r"<head[\s>/]", re.I)
_BODY_OPEN = re.compile(r"<body[\s>/]", re.I)

# Hygiene / AI-meaning reads (model.py "AI / robots meaning and page hygiene")
# A doctype counts only before any content: a BOM, whitespace, comments or an XML prolog may precede it
_DOCTYPE_START = re.compile(r"﻿?(?:\s|<!--.*?-->|<\?xml[^>]*>)*<!doctype\s+html", re.I | re.S)
_CHARSET_IN = re.compile(r"charset\s*=\s*['\"]?\s*([\w:.-]+)", re.I)
SCOPED_NOINDEX_BOTS = ("googlebot", "bingbot")
# Fragments that are script hooks or "scroll to top", not element targets
_FRAGMENT_SKIP = {"", "!", "0", "top", "#", "_", "-"}
MAX_DEAD_FRAGMENTS = 50
MAX_MALFORMED_HREFS = 50
_BARE_EMAIL = re.compile(r"^[^/\s:?#@]+@[^/\s:?#@]+\.[a-z]{2,}$", re.I)
_BAD_SCHEME = re.compile(
    r"^(?:https?:/?(?!/)|https?;?//|https?://https?[:/]|(?:htp|htps|htttp|htttps|hhtp|hhtps|hhttp|hhttps|ttp|ttps|htpp|htpps):)",
    re.I)
_LOCAL_FILE = re.compile(r"^(?:file:|[a-z]:[\\/])", re.I)
_OTHER_SCHEMES = ("mailto:", "tel:", "sms:", "whatsapp:", "callto:", "skype:", "data:", "intent:", "viber:")


def malformed_href(raw: str) -> Tuple[str, str]:
    """(code, plain reason) when a link's href is written in a broken way, ("", "") when it is fine.
    Codes: javascript, local_file, bad_scheme, bare_email, backslash, spaces."""
    if not raw:
        return "", ""
    c = raw[0]
    if c in "/#?" and " " not in raw and "\\" not in raw:
        return "", ""                                  # fast path: most hrefs
    low = raw.lower()
    if low.startswith("javascript:"):
        return "javascript", "JavaScript pseudo-link (javascript:), not an address crawlers can follow"
    if low.startswith(_OTHER_SCHEMES):
        return "", ""
    if _LOCAL_FILE.match(raw):
        return "local_file", "Points to a file on someone's computer, not a web address"
    if _BAD_SCHEME.match(raw):
        return "bad_scheme", "Broken http(s):// prefix"
    if "@" in raw and _BARE_EMAIL.match(raw):
        return "bare_email", "Email address without mailto: (opens a missing page instead of an email)"
    if "\\" in raw:
        return "backslash", "Backslash instead of a forward slash"
    if any(ch.isspace() for ch in raw):
        return "spaces", "Contains spaces (encode them as %20 or use hyphens)"
    return "", ""


def _says_noindex(values: Iterable[str]) -> bool:
    """True when robots values contain noindex or none (as tokens)."""
    for value in values:
        for part in re.split(r"[,\s]+", (value or "").lower()):
            if part in ("noindex", "none"):
                return True
    return False


def _scope_head(token: str) -> Tuple[str, str]:
    """("crawler name", rest) when a directive part starts with a crawler name ("bingbot: noindex"), else
    ("", token). Value directives (max-snippet: 50) and dates (15:00:00) are not crawler names."""
    if ":" in token:
        head, rest = (s.strip() for s in token.split(":", 1))
        if head and head not in _VALUE_DIRECTIVES and " " not in head and not head.isdigit():
            return head, rest
    return "", token


def x_robots_scopes(values: Iterable[str]) -> Dict[str, List[str]]:
    """X-Robots-Tag values (one per header line) -> {crawler name or "" (every crawler): [directive parts]}.
    "googlebot: noindex, nofollow" scopes the parts after the name to googlebot until the next crawler name or
    the end of that header line."""
    out: Dict[str, List[str]] = {}
    for value in values:
        scope = ""
        for part in (value or "").lower().split(","):
            token = part.strip()
            if not token:
                continue
            head, rest = _scope_head(token)
            if head:
                scope, token = head, rest
                if not token:
                    continue
            out.setdefault(scope, []).append(token)
    return out


def _fresh(value: Any) -> Any:
    """A deep copy of an EMPTY_PAGE value (only lists and dicts are mutable there; strings, numbers, None and
    tuples are shared)."""
    if isinstance(value, list):
        return [_fresh(v) for v in value] if value else []
    if isinstance(value, dict):
        return {k: _fresh(v) for k, v in value.items()}
    return value


def new_page(url: str = "") -> Dict[str, Any]:
    """A fresh PageData with every key at its empty value (a shaped copy: several times faster than deepcopy,
    and every page and checked file gets one)."""
    page = {k: _fresh(v) for k, v in EMPTY_PAGE.items()}
    page["url"] = url
    page["final_url"] = url
    return page


def parse_html(html: str):
    """The single parse used for both SEO extraction and link discovery (lxml when installed)."""
    return make_soup(html)


# ---------------------------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------------------------
def clean_text(value: Any) -> str:
    """Whitespace-collapsed, HTML-unescaped text."""
    if value is None:
        return ""
    # " ".join(split()) == _WS.sub(" ", ...).strip(): split() and \s use the same whitespace set, ~5x faster
    return " ".join(html_lib.unescape(str(value)).split())


# Invisible characters dropped from element text: soft hyphen, zero-width space/non-joiner/joiner,
# word joiner, byte-order mark
_INVISIBLE = re.compile("[­​-‍⁠﻿]")


def _el_text(el) -> str:
    """An element's text as Screaming Frog reads titles, headings and anchors: inline tags join without
    a space ("<b>Systems</b>," -> "Systems,"), block elements and <br> separate words, whitespace
    collapses and invisible characters are dropped (checked against its export of the reference site)."""
    from bs4.element import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction, CData
    skip_types = (Comment, Declaration, Doctype, ProcessingInstruction, CData)
    parts: List[str] = []
    stack = list(reversed(el.contents))
    while stack:
        node = stack.pop()
        if node is None:                       # end of a block element
            parts.append(" ")
        elif isinstance(node, NavigableString):
            if not isinstance(node, skip_types):
                parts.append(str(node))
        elif node.name not in ("script", "style"):
            if node.name in TEXT_BLOCK_TAGS or node.name == "br":
                parts.append(" ")
            if node.name in TEXT_BLOCK_TAGS:
                stack.append(None)
            stack.extend(reversed(node.contents))
    return " ".join(_INVISIBLE.sub("", "".join(parts)).split())


def _visible_strings(root, area: Optional[Tuple[Optional[List[Any]], Set[int]]] = None
                     ) -> Iterable[Tuple[str, bool]]:
    """(text node, inside the content area) for every visible text node under `root`: script, style,
    noscript, template and title subtrees and comments are skipped; nav and footer text is visible
    but outside the content area. `area` (content_area()) narrows the content area to the included elements
    (CrawlConfig.content_area include) and leaves the excluded ones out (exclude wins)."""
    from bs4.element import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction, CData
    skip_types = (Comment, Declaration, Doctype, ProcessingInstruction, CData)
    roots: Optional[Set[int]] = None
    excluded: Set[int] = set()
    if area is not None:
        roots = {id(el) for el in area[0]} if area[0] is not None else None
        excluded = area[1]
    stack = [(root, roots is None, False)]
    while stack:
        node, in_content, blocked = stack.pop()
        if isinstance(node, NavigableString):
            if not isinstance(node, skip_types):
                yield str(node), in_content
            continue
        name = getattr(node, "name", None)
        if name in HIDDEN_TEXT_TAGS:
            continue
        children = getattr(node, "contents", None)
        if children:
            if area is None:
                inner = in_content and name not in CONTENT_EXCLUDED_TAGS
            else:
                nid = id(node)
                blocked = blocked or nid in excluded
                if blocked:
                    inner = False
                elif roots is not None and nid in roots:
                    inner = True                      # an included element counts whole, whatever its tag
                else:
                    inner = in_content and name not in CONTENT_EXCLUDED_TAGS
            stack.extend((c, inner, blocked) for c in reversed(children))


def content_lines(root, skip: Optional[Set[int]] = None) -> Tuple[List[str], int]:
    """(text lines, list items) of the content area under `root`. A line is the text between two block
    element boundaries (or <br>), whitespace-collapsed; inline elements are joined without adding a space.
    Hidden subtrees (script, style, ...) and nav/footer below `root` are left out, and so are the elements whose
    id() is in `skip` (content area excludes). List items are counted because
    Screaming Frog counts each <li> marker as a word (checked against its export of the reference site)."""
    from bs4.element import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction, CData
    skip_types = (Comment, Declaration, Doctype, ProcessingInstruction, CData)
    lines: List[List[str]] = [[]]
    list_items = 0
    stack = [root]
    while stack:
        node = stack.pop()
        if node is None:                       # end of a block element
            if lines[-1]:
                lines.append([])
            continue
        if isinstance(node, NavigableString):
            if not isinstance(node, skip_types):
                lines[-1].append(str(node))
            continue
        name = getattr(node, "name", None)
        if name in HIDDEN_TEXT_TAGS or (name in CONTENT_EXCLUDED_TAGS and node is not root):
            continue
        if skip and id(node) in skip:
            continue
        if (name in TEXT_BLOCK_TAGS or name == "br") and lines[-1]:
            lines.append([])
        if name == "li":
            list_items += 1
        children = getattr(node, "contents", None) or []
        if name in TEXT_BLOCK_TAGS:
            stack.append(None)
        stack.extend(reversed(children))
    return [t for t in (clean_text("".join(parts)) for parts in lines) if t], list_items


def compile_selectors(values: Any, what: str = "Content area") -> Tuple[List[Any], List[str]]:
    """(compiled CSS selectors, problems) from a list of CSS selector strings; an invalid one is left out and named
    in the problems (shown with the crawl's configuration notes)."""
    out: List[Any] = []
    problems: List[str] = []
    if not values:
        return out, problems
    try:
        import soupsieve
    except Exception:
        return out, [f"{what} selectors could not be read (soupsieve is missing)."]
    for raw in values if isinstance(values, (list, tuple)) else [values]:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            out.append(soupsieve.compile(text))
        except Exception:
            problems.append(f"{what} selector '{text[:80]}' is not valid CSS, so it is not used.")
    return out, problems


def content_area(body, include: List[Any], exclude: List[Any]) -> Optional[Tuple[Optional[List[Any]], Set[int]]]:
    """The content area chosen under Configuration > Content (CrawlConfig.content_area) on one page:
    (the included elements, top-level only and in page order, or None for the whole <body>; id() of every
    excluded element). None when no selector is set. A page where no include selector matches falls back to the
    whole <body> (so a page without the chosen block is not read as empty); an excluded element always stays out,
    also inside an included one."""
    if not include and not exclude:
        return None
    excluded: Set[int] = set()
    for sel in exclude or ():
        try:
            excluded.update(id(el) for el in sel.select(body))
        except Exception:
            continue
    roots: Optional[List[Any]] = None
    if include:
        picked: List[Any] = []
        seen: Set[int] = set()
        for sel in include:
            try:
                for el in sel.select(body):
                    if id(el) not in seen:
                        seen.add(id(el))
                        picked.append(el)
            except Exception:
                continue
        top = [el for el in picked
               if not any(id(a) in seen or id(a) in excluded for a in el.parents) and id(el) not in excluded]
        if top:
            order = {id(el): i for i, el in enumerate(body.descendants)} if len(top) > 1 else {}
            roots = sorted(top, key=lambda el: order.get(id(el), 0))
    return roots, excluded


def area_lines(body, area: Optional[Tuple[Optional[List[Any]], Set[int]]]) -> Tuple[List[str], int]:
    """content_lines() of a page's content area (content_area(); None = the default area: <body> without nav and
    footer)."""
    if area is None:
        return content_lines(body)
    roots, excluded = area
    lines: List[str] = []
    items = 0
    for root in roots if roots is not None else [body]:
        got, n = content_lines(root, excluded)
        lines += got
        items += n
    return lines, items


@lru_cache(maxsize=100000)
def count_syllables(word: str) -> int:
    """English syllable estimate: vowel groups, minus a silent final e, at least one."""
    w = re.sub(r"[^a-z]", "", word.lower())
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|[^laeiouy]ed|[^laeiouy]e)$", lambda m: m.group(0)[0], w)
    w = re.sub(r"^y", "", w)
    return max(1, len(_VOWEL_GROUPS.findall(w)))


def readability_label(score: Optional[float]) -> str:
    """Flesch reading ease band, the names Screaming Frog uses."""
    if score is None:
        return ""
    for floor, label in READABILITY_BANDS:
        if score >= floor:
            return label
    return "Very Hard"


def text_stats(blocks: Iterable[str], extra_words: int = 0) -> Dict[str, Any]:
    """Word, sentence and Flesch figures for the content area, given its text lines (content_lines()).
    Words are whitespace-separated tokens, plus `extra_words` (one per list item, as Screaming Frog counts).
    Each line holds at least one sentence (headings, buttons and list items end a sentence without
    punctuation) and is split further at . ! ? and commas. Figures are rounded like Screaming Frog (3 places)."""
    if isinstance(blocks, str):
        blocks = [blocks]
    word_count = sentence_count = syllables = 0
    for block in blocks:
        tokens = block.split()
        if not tokens:
            continue
        word_count += len(tokens)
        sentence_count += max(1, sum(1 for part in _SENTENCE_END.split(block) if _WORD.search(part)))
        syllables += sum(count_syllables(t) for t in tokens)
    if not word_count:
        return {"word_count": 0, "sentence_count": 0, "avg_words_per_sentence": 0.0,
                "flesch_reading_ease": None, "readability": ""}
    word_count += max(0, int(extra_words or 0))
    sentence_count = max(1, sentence_count)
    wps = word_count / sentence_count
    flesch = round(206.835 - 1.015 * wps - 84.6 * (syllables / word_count), 3)
    return {"word_count": word_count, "sentence_count": sentence_count,
            "avg_words_per_sentence": round(wps, 3), "flesch_reading_ease": flesch,
            "readability": readability_label(flesch)}


# _BIT_SET[k]: byte value -> 1 when bit (0x80 >> k) is set (simhash_tokens counts bit columns with it)
_BIT_SET = [bytes(1 if value & (0x80 >> k) else 0 for value in range(256)) for k in range(8)]


def simhash(text: str) -> int:
    """64-bit simhash over word 3-shingles (md5 of each shingle); 0 when there is no text."""
    return simhash_tokens(word_tokens(text))


def word_tokens(text: str) -> List[str]:
    """The lowercased words simhash and top_terms are built from."""
    return [t.lower() for t in _WORD.findall(text or "")]


def simhash_tokens(tokens: List[str]) -> int:
    """simhash() of already split words (word_tokens)."""
    if not tokens:
        return 0
    if len(tokens) < 3:
        shingles = [" ".join(tokens)]
    else:
        shingles = [" ".join(t) for t in zip(tokens, tokens[1:], tokens[2:])]
    # Each shingle votes +1/-1 per bit (repeats vote again); a bit is set when most votes are +1. The first 8
    # bytes of each shingle's md5 (most significant byte first) go into one bytes object, and every bit column
    # is counted in C (a slice per byte position, a translate per bit) instead of a Python loop per bit.
    md5 = hashlib.md5
    hashed: Dict[str, bytes] = {}
    rows = []
    for sh in shingles:
        b = hashed.get(sh)
        if b is None:
            b = hashed[sh] = md5(sh.encode("utf-8")).digest()[:8]
        rows.append(b)
    n = len(rows)
    blob = b"".join(rows)
    out = 0
    for byte in range(8):
        column = blob[byte::8]
        for bit in range(8):   # bit 0 = 0x80 of the byte; byte 0 bit 0 is the most significant bit
            if column.translate(_BIT_SET[bit]).count(1) * 2 > n:
                out |= 1 << (63 - 8 * byte - bit)
    return out


def simhash_similarity(a: int, b: int) -> float:
    """Share of equal bits between two simhashes, 0..100."""
    return round((64 - bin((a ^ b) & ((1 << 64) - 1)).count("1")) / 64 * 100, 1)


# ---------------------------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------------------------
def _http_version(response) -> str:
    raw = getattr(response, "raw", None)
    v = getattr(raw, "version", None)
    return {10: "HTTP/1.0", 11: "HTTP/1.1", 20: "HTTP/2", 30: "HTTP/3"}.get(v, "") if v else ""


def parse_link_header(value: str) -> List[Dict[str, str]]:
    """RFC 8288 Link header -> [{"url": ..., "rel": ..., "hreflang": ...}]."""
    out: List[Dict[str, str]] = []
    if not value:
        return out
    for m in re.finditer(r"<([^>]*)>((?:\s*;\s*[a-zA-Z*-]+\s*=\s*(?:\"[^\"]*\"|[^;,\s]*))*)", value):
        item = {"url": m.group(1).strip()}
        for pm in _LINK_PARAM.finditer(m.group(2)):
            item[pm.group(1).lower()] = (pm.group(2) if pm.group(2) is not None else pm.group(3) or "").strip()
        out.append(item)
    return out


MAX_COOKIES = 50
# requests joins repeated Set-Cookie headers with ", "; a new cookie starts after a comma that is followed by
# "name=" (the comma inside "Expires=Wed, 21 Oct 2026" is followed by a date, not a name)
_COOKIE_SPLIT = re.compile(r",\s*(?=[^;,=\s]+=)")
_COOKIE_ATTRS = {"domain": "domain", "path": "path", "expires": "expires", "max-age": "max_age",
                 "samesite": "samesite"}


def set_cookie_headers(response) -> List[str]:
    """Every Set-Cookie header of a response, one string each (urllib3 keeps them apart; requests' merged
    header is split as a fallback)."""
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    getlist = getattr(raw_headers, "getlist", None)
    if callable(getlist):
        try:
            return [str(v) for v in getlist("Set-Cookie")]
        except Exception:
            pass
    headers = getattr(response, "headers", None) or {}
    merged = str(headers.get("set-cookie", "") or "")
    return [part for part in _COOKIE_SPLIT.split(merged) if part.strip()] if merged else []


def parse_set_cookie(value: str) -> Optional[Dict[str, Any]]:
    """{"name","domain","path","expires","max_age","secure","httponly","samesite"} of one Set-Cookie header.
    The cookie's value is never kept."""
    parts = (value or "").split(";")
    name = parts[0].split("=", 1)[0].strip()
    if not name:
        return None
    out: Dict[str, Any] = {"name": name[:200], "domain": "", "path": "", "expires": "", "max_age": "",
                           "secure": False, "httponly": False, "samesite": ""}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        key = key.strip().lower()
        if key == "secure":
            out["secure"] = True
        elif key == "httponly":
            out["httponly"] = True
        elif key in _COOKIE_ATTRS:
            out[_COOKIE_ATTRS[key]] = val.strip()[:120]
    return out


def merge_cookies(http: Iterable[dict], rendered: Iterable[dict], page_url: str) -> List[Dict[str, Any]]:
    """A page's cookies: the ones its server answer set (Set-Cookie), then the ones Chrome received while
    rendering it (render_capture: "source" request / script), once each by name + domain (a cookie without a
    Domain attribute belongs to the page's host). Values are never kept; at most MAX_COOKIES."""
    host = (urlparse(page_url or "").hostname or "").lower()
    out: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for c in list(http or []) + list(rendered or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        key = (str(c["name"]), str(c.get("domain") or "").lstrip(".").lower() or host)
        if key in seen:
            continue
        seen.add(key)
        out.append({k: v for k, v in c.items() if k != "value"})
        if len(out) >= MAX_COOKIES:
            break
    return out


def response_cookies(response) -> List[Dict[str, Any]]:
    """The cookies a response sets (names and attributes only), at most MAX_COOKIES."""
    if response is None:
        return []
    out: List[Dict[str, Any]] = []
    for header in set_cookie_headers(response):
        cookie = parse_set_cookie(header)
        if cookie:
            out.append(cookie)
            if len(out) >= MAX_COOKIES:
                break
    return out


def _applies_to_google(scope: str) -> bool:
    """A crawler scope that Googlebot obeys: none, "robots", "*" or a Googlebot name ("googlebot-news")."""
    return not scope or scope.startswith(_ROBOT_PREFIXES)


def directive_tokens(values: Iterable[str]) -> List[str]:
    """Known robots directive names in a list of meta robots / X-Robots-Tag values (one value per tag or header
    line). A crawler name scopes the parts after it, up to the next crawler name or the end of that value:
    "bingbot: noindex, nofollow" says nothing to Google, so it gives no token; "googlebot: noindex, nofollow"
    gives both. Parts scoped to a crawler other than googlebot / robots / * are skipped."""
    found: List[str] = []
    for value in values:
        scope = ""
        for part in (value or "").split(","):  # date parts of unavailable_after fall out below
            token = part.strip().lower()
            if not token:
                continue
            head, rest = _scope_head(token)
            if head:
                scope, token = head, rest
                if not token:
                    continue
            if not _applies_to_google(scope):
                continue  # a directive for some other crawler ("bingbot: noindex, nofollow")
            if ":" in token:
                name = token.split(":", 1)[0].strip()
                if name not in _VALUE_DIRECTIVES:
                    continue
                token = name
            token = token.split()[0] if token.split() else ""
            if token in DIRECTIVE_TOKENS and token not in found:
                found.append(token)
    return found


def header_lines(response, name: str) -> List[str]:
    """Every line of a response header, in order (requests joins a repeated header with ", "; the raw urllib3
    headers keep the lines apart). Falls back to the joined value as one line."""
    raw = getattr(getattr(response, "raw", None), "headers", None)
    getlist = getattr(raw, "getlist", None)
    if callable(getlist):
        try:
            lines = [str(v) for v in getlist(name) if v is not None]
            if lines:
                return lines
        except Exception:
            pass
    headers = getattr(response, "headers", None) or {}
    value = headers.get(name) if hasattr(headers, "get") else None
    return [str(value)] if value else []


def _abs(raw: str, base: str) -> str:
    """Absolute normalized URL, falling back to a plain join for non-http values."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    norm = normalize_cached(raw, base_url=base)
    if norm:
        return norm
    try:
        return join_url(base, raw)
    except Exception:
        return raw


def _apply_headers(page: Dict[str, Any], response, base: str) -> None:
    """Fill the PageData fields that come from the HTTP response itself."""
    if response is None:
        return
    headers = getattr(response, "headers", None) or {}
    page["status_code"] = getattr(response, "status_code", None)
    page["content_type"] = str(headers.get("content-type", "") or "").lower()
    page["http_version"] = _http_version(response)
    page["last_modified"] = str(headers.get("last-modified", "") or "")
    elapsed = getattr(response, "elapsed", None)
    if elapsed is not None:
        page["response_time_ms"] = int(elapsed.total_seconds() * 1000)
    body = getattr(response, "_content", False)
    if isinstance(body, (bytes, bytearray)):
        page["size_bytes"] = len(body)
    length = str(headers.get("content-length", "") or "").strip()
    if length.isdigit():
        page["transferred_bytes"] = int(length)
        if not page["size_bytes"]:
            page["size_bytes"] = int(length)
    else:
        page["transferred_bytes"] = page["size_bytes"]

    page["cookies"] = response_cookies(response)
    # P4: a "Refresh: 0; url=..." header sends the visitor on like a redirect; its absolute target (the crawl queues it)
    refresh = _HEADER_REFRESH_URL.search(str(headers.get("refresh", "") or ""))
    page["http_refresh_url"] = _abs(refresh.group(1).strip(), base) if refresh else ""
    page["x_robots_tag"] = [v for v in (line.strip().lower() for line in header_lines(response, "x-robots-tag")) if v]
    for item in parse_link_header(str(headers.get("link", "") or "")):
        rels = item.get("rel", "").lower().split()
        url = _abs(item["url"], base)
        if "canonical" in rels and not page["http_canonical"]:
            page["http_canonical"] = url
        if "next" in rels and not page["http_rel_next"]:
            page["http_rel_next"] = url
        if "prev" in rels and not page["http_rel_prev"]:
            page["http_rel_prev"] = url
        if "alternate" in rels and item.get("hreflang"):
            page["http_hreflang"].append({"lang": item["hreflang"].lower(), "url": url})
    page["directives"] = directive_tokens(page["x_robots_tag"])


def extract_headers_only(response, url: str) -> Dict[str, Any]:
    """PageData for a URL without a parsed body: files, HEAD checks, redirects and failures."""
    page = new_page(url)
    if response is None:
        return page
    final = str(getattr(response, "url", "") or url)
    page["final_url"] = final
    _apply_headers(page, response, final)
    return page


# ---------------------------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------------------------
def _in_head(el, has_head: bool) -> bool:
    for p in el.parents:
        name = getattr(p, "name", None)
        if name == "head":
            return True
        if name == "body":
            return False
    return not has_head  # html.parser without a <head>: tags before any <body> count as head


def _in_svg(el) -> bool:
    return any(getattr(p, "name", None) == "svg" for p in el.parents)


def _attr_list(el, attr: str) -> List[str]:
    val = el.get(attr)
    if val is None:
        return []
    if isinstance(val, list):
        return [str(v).lower() for v in val]
    return str(val).lower().split()


def srcset_urls(value: str) -> List[str]:
    """Every candidate URL of a srcset attribute, in order ("a.jpg 480w, b.jpg 800w" -> [a.jpg, b.jpg]). As browsers
    read it, a candidate's URL runs to the next whitespace (so a URL may hold commas, as image CDNs write them:
    "/w_480,h_320/a.jpg"); a comma that ends the URL, or one after its descriptors, starts the next candidate."""
    out: List[str] = []
    text = value or ""
    i, n = 0, len(text)
    while i < n:
        while i < n and (text[i].isspace() or text[i] == ","):
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not text[j].isspace():
            j += 1
        url = text[i:j]
        if url.endswith(","):
            url = url.rstrip(",")
            i = j
        else:
            depth, k = 0, j
            while k < n:                       # descriptors ("480w", "2x") up to the next comma
                c = text[k]
                if c == "(":
                    depth += 1
                elif c == ")":
                    depth = max(0, depth - 1)
                elif c == "," and depth == 0:
                    break
                k += 1
            i = k + 1
        if url and url not in out:
            out.append(url)
    return out


def _first_srcset(value: str) -> str:
    urls = srcset_urls(value)
    return urls[0] if urls else ""


def _picture_of(el) -> Any:
    """The <picture> an <img> or <source> belongs to, or None. lxml's HTML parser does not know <source> is a void
    element and nests what follows inside it, so the closest <picture> above is looked up (not only the parent)."""
    for p in el.parents:
        name = getattr(p, "name", None)
        if name == "picture":
            return p
        if name not in ("source", "img"):
            return None
    return None


def _src_attr(el, attr: str) -> str:
    """The attribute an element's URL was read from (src may be empty when srcset/data-src is used)."""
    if attr != "src" or str(el.get("src") or "").strip():
        return attr
    return "srcset" if el.get("srcset") else ("data-src" if el.get("data-src") else attr)


def _schema_type(value: str) -> str:
    value = str(value or "").strip()
    for prefix in ("https://schema.org/", "http://schema.org/", "schema:"):
        if value.lower().startswith(prefix):
            return value[len(prefix):]
    return value


def _jsonld_types(data: Any) -> List[str]:
    """@type of each top-level item and of each @graph member."""
    types: List[str] = []
    items = data if isinstance(data, list) else [data]
    for item in items:
        if not isinstance(item, dict):
            continue
        t = item.get("@type")
        for v in (t if isinstance(t, list) else [t]):
            if v:
                types.append(_schema_type(v))
        graph = item.get("@graph")
        if graph:
            types.extend(_jsonld_types(graph))
    return types


SD_MAX_DEPTH = 6          # nested levels kept of a JSON-LD item's values (P12)
SD_MAX_NODES = 300        # values kept per item
SD_MAX_LIST = 20          # entries kept of one list value
SD_MAX_TEXT = 200         # characters kept of one text value


def _sd_values(node: Any, budget: List[int], depth: int = 0) -> Any:
    """A capped copy of a JSON-LD value (dicts, lists, text cut at SD_MAX_TEXT) for the schema.org checks and the
    Structured Data Details pane; @graph members are items of their own and left out."""
    budget[0] -= 1
    if isinstance(node, dict):
        if depth >= SD_MAX_DEPTH or budget[0] <= 0:
            return {"@type": node.get("@type")} if node.get("@type") else {}
        out = {}
        for k, v in node.items():
            if not isinstance(k, str) or k == "@graph" or k == "@context" or budget[0] <= 0:
                continue
            out[k] = _sd_values(v, budget, depth + 1)
        return out
    if isinstance(node, list):
        return [_sd_values(v, budget, depth + 1) for v in node[:SD_MAX_LIST] if budget[0] > 0]
    if isinstance(node, str):
        return node[:SD_MAX_TEXT]
    return node if isinstance(node, (int, float, bool)) or node is None else str(node)[:SD_MAX_TEXT]


def _sd_paths(node: Any, prefix: str = "", out: Optional[List[str]] = None) -> List[str]:
    """Dotted property paths of a values tree ("offers", "offers.price", "location.address")."""
    out = [] if out is None else out
    items = node if isinstance(node, list) else [node]
    for n in items:
        if not isinstance(n, dict):
            continue
        for k, v in n.items():
            if not isinstance(k, str) or k.startswith("@"):
                continue
            path = f"{prefix}.{k}" if prefix else k
            if path not in out and len(out) < SD_MAX_NODES:
                out.append(path)
            _sd_paths(v, path, out)
    return out


def _jsonld_items(data: Any) -> List[Dict[str, Any]]:
    """{"format", "type", "props", "values", "paths"} for each top-level item and @graph member: props for the
    basic property checks, values (a capped copy) and paths (dotted names) for the schema.org checks (P12)."""
    out: List[Dict[str, Any]] = []
    for item in (data if isinstance(data, list) else [data]):
        if not isinstance(item, dict):
            continue
        t = item.get("@type")
        props = [k for k in item.keys() if isinstance(k, str) and not k.startswith("@")]
        values = paths = None
        for v in (t if isinstance(t, list) else [t]):
            if v:
                if values is None:
                    values = _sd_values(item, [SD_MAX_NODES])
                    paths = _sd_paths(values)
                out.append({"format": "jsonld", "type": _schema_type(v), "props": props, "values": values,
                            "paths": paths})
        if item.get("@graph"):
            out.extend(_jsonld_items(item["@graph"]))
    return out


def _scoped_props(el, prop_attr: str, scope_attr: str) -> List[str]:
    """Property names (itemprop / property) whose nearest enclosing item is `el`."""
    props: List[str] = []
    for d in el.find_all(attrs={prop_attr: True}):
        owner = d.find_parent(attrs={scope_attr: True})
        if owner is el:
            for name in str(d.get(prop_attr) or "").split():
                name = name.rsplit("/", 1)[-1].split(":", 1)[-1]
                if name and name not in props:
                    props.append(name)
    return props


# JSON-LD values compared with the visible page (siteone-crawler's "schema that does not match the page"):
# an item's own name / headline, product codes, contact details and address parts anywhere in it, and the
# price inside its offers. Names of these types are not expected to be shown as written.
SCHEMA_NAME_SKIP = {"imageobject", "videoobject", "breadcrumblist", "listitem", "searchaction", "entrypoint",
                    "readaction", "propertyvaluespecification", "sitenavigationelement", "wpheader", "wpfooter",
                    "wpsidebar"}
SCHEMA_ANY_FIELDS = ("sku", "telephone", "email", "streetAddress", "postalCode", "addressLocality")
MAX_SCHEMA_VALUES = 200
MAX_SCHEMA_REPORTED = 50


def _schema_values(data: Any, out: List[Tuple[str, str, str]], top: bool = True, owner: str = "",
                   in_offers: bool = False, depth: int = 0) -> None:
    """(type, field, value) of the JSON-LD values worth comparing with the visible page (see SCHEMA_ANY_FIELDS)."""
    if depth > 6 or len(out) >= MAX_SCHEMA_VALUES:
        return
    if isinstance(data, list):
        for item in data:
            _schema_values(item, out, top, owner, in_offers, depth + 1)
        return
    if not isinstance(data, dict):
        return
    t = data.get("@type")
    t = next((v for v in (t if isinstance(t, list) else [t]) if isinstance(v, str) and v), "")
    kind = _schema_type(t) if t else owner

    def add(name: str, value: Any):
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, (int, float)):
            value = repr(value)
        if isinstance(value, str):
            value = clean_text(value)
            if value and len(value) <= 300 and len(out) < MAX_SCHEMA_VALUES:
                out.append((kind or "Thing", name, value))

    for key, value in data.items():
        if not isinstance(key, str) or key.startswith("@") and key != "@graph":
            continue
        if key == "@graph":
            _schema_values(value, out, True, "", False, depth + 1)
        elif key in ("name", "headline"):
            if top and kind.lower() not in SCHEMA_NAME_SKIP:
                add(key, value)
        elif key in SCHEMA_ANY_FIELDS:
            add(key, value)
        elif key == "price" and in_offers:
            add(key, value)
        elif key == "offers":
            _schema_values(value, out, False, kind, True, depth + 1)
        elif isinstance(value, (dict, list)):
            _schema_values(value, out, False, kind, in_offers, depth + 1)


def _structured_data(idx, values: Optional[List[Tuple[str, str, str]]] = None) -> Dict[str, Any]:
    sd = {"jsonld": [], "microdata": [], "rdfa": [], "jsonld_parse_errors": 0, "types": [], "items": []}
    for script in idx.tags("script"):
        stype = str(script.get("type") or "").strip().lower()
        if stype != "application/ld+json":
            continue
        raw = (script.string if script.string is not None else script.get_text()) or ""
        raw = raw.strip()
        if raw.startswith("<!--"):
            raw = raw[4:]
        if raw.endswith("-->"):
            raw = raw[:-3]
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw, strict=False)
            sd["jsonld"].extend(_jsonld_types(data))
            sd["items"].extend(_jsonld_items(data))
        except (ValueError, TypeError, RecursionError):
            sd["jsonld_parse_errors"] += 1
            continue
        if values is not None:
            try:
                _schema_values(data, values)
            except RecursionError:
                pass
    for el in idx.with_attr("itemtype"):
        if el.get("itemprop") is not None:
            continue  # nested property of another item
        for t in str(el.get("itemtype") or "").split():
            sd["microdata"].append(_schema_type(t.rstrip("/").rsplit("/", 1)[-1] if "/" in t else t))
            sd["items"].append({"format": "microdata", "type": sd["microdata"][-1],
                                "props": _scoped_props(el, "itemprop", "itemscope")})
    for el in idx.with_attr("typeof"):
        if el.get("property") is not None:
            continue
        for t in str(el.get("typeof") or "").split():
            sd["rdfa"].append(_schema_type(t.split(":", 1)[-1] if ":" in t and "//" not in t else t))
            sd["items"].append({"format": "rdfa", "type": sd["rdfa"][-1],
                                "props": _scoped_props(el, "property", "typeof")})
    seen: Set[str] = set()
    for t in sd["jsonld"] + sd["microdata"] + sd["rdfa"]:
        if t and t not in seen:
            seen.add(t)
            sd["types"].append(t)
    return sd


def _head_checks(lines: SourceLines, html: str) -> Tuple[int, int, List[str]]:
    """(<head> count, <body> count, invalid element names inside the first <head>) from the raw source."""
    head_count = sum(1 for t, _, _ in lines.tags if t == "head")
    body_count = sum(1 for t, _, _ in lines.tags if t == "body")
    invalid: List[str] = []
    in_head = False
    for tag, _, _ in lines.tags:
        if tag == "head" and not in_head:
            in_head = True
            continue
        if not in_head:
            continue
        if tag in ("/head", "body", "/html"):
            break
        if tag.startswith("/") or tag == "head" or tag == "html":
            continue
        if tag not in HEAD_ALLOWED and tag not in invalid:
            invalid.append(tag)
    return head_count, body_count, invalid


def _document_order(lines: SourceLines) -> Tuple[bool, bool]:
    """(head_not_first, body_before_html) from the source as written (the parser repairs both):
    head_not_first - inside <html>, another element opens before the <head> (a parser then starts a <head> of
    its own and the real one's tags can land in <body>); body_before_html - a <body> tag comes before the
    opening <html> tag. Comments, the doctype and the text of scripts and styles are not elements here."""
    html_at = body_at = -1
    for i, (tag, _, _) in enumerate(lines.tags):
        if tag == "html" and html_at < 0:
            html_at = i
        elif tag == "body" and body_at < 0:
            body_at = i
        if html_at >= 0 and body_at >= 0:
            break
    body_before_html = 0 <= body_at < html_at
    head_not_first = False
    if html_at >= 0 and any(t == "head" for t, _, _ in lines.tags[html_at + 1:]):
        first = next((t for t, _, _ in lines.tags[html_at + 1:] if not t.startswith("/")), "")
        head_not_first = first not in ("head", "")
    return head_not_first, body_before_html


def _soft_404(titles: List[str], h1: List[str], text: str, phrases: Sequence[str] = ()) -> List[str]:
    """Error-page wording in the title, H1 and the start of the content text; `phrases` are the team's own
    (CrawlConfig.soft_404_phrases, any language, lower case) looked for in the content text as whole phrases."""
    signals: List[str] = []
    for label, values in (("Title", titles), ("H1", h1)):
        blob = " ".join(values).lower()
        if not blob:
            continue
        for phrase, rx in SOFT_404_HEADING_PATTERNS:
            if rx.search(blob):
                signals.append(f'{label} contains "{phrase}"')
    body = text[:5000].lower()
    for phrase, rx in SOFT_404_BODY_PATTERNS:
        if rx.search(body):
            signals.append(f'Page text contains "{phrase}"')
    if phrases:
        flat = " ".join(body.split())
        for phrase in phrases:
            if phrase and phrase in flat and f'Page text contains "{phrase}"' not in signals:
                signals.append(f'Page text contains "{phrase}"')
    return signals


# ---------------------------------------------------------------------------------------------
# Custom Search / Custom Extraction (CrawlConfig.custom_searches / custom_extractors)
# ---------------------------------------------------------------------------------------------
CUSTOM_VALUE_MAX = 2000      # characters kept per extracted value
CUSTOM_MATCHES_MAX = 20      # values kept per extractor and page
SEARCH_COUNT_MAX = 100000    # occurrences counted per search and page
CUSTOM_EXPR_MAX = 1000       # characters of a rule's text / expression
EXTRACT_MODES = ("text", "inner_html", "element", "attribute")
# Where a Custom Search looks (P15): raw source, visible text, the raw <head> / <body>, visible text without the
# words of links, or the HTML of the elements a selector ("css:..." / "xpath:...") matches
SEARCH_WHERE = ("html", "text", "head", "body", "text_no_anchors", "element")
SEARCH_WHERE_LABELS = {"html": "HTML", "text": "Page Text", "head": "Head", "body": "Body",
                       "text_no_anchors": "Page Text No Anchors", "element": "Element"}


@dataclass
class CustomSearch:
    name: str
    mode: str                      # "contains" | "not_contains" (the filter; the count is stored either way)
    where: str                     # SEARCH_WHERE: "html" (raw source) | "text" (visible text) | "head" | "body" |
                                   # "text_no_anchors" | "element" (the HTML of the elements `selector` matches)
    text: str                      # plain text, lowercased unless case_sensitive
    regex: Optional[Pattern] = None
    case_sensitive: bool = False
    selector: Any = None           # where == "element": ("css", soupsieve) | ("xpath", lxml XPath)


@dataclass
class CustomExtractor:
    name: str
    kind: str                      # "xpath" | "css" | "regex"
    extract: str                   # EXTRACT_MODES (regex: group 1 when the pattern has a group, else the match)
    attr: str
    compiled: Any                  # lxml XPath | soupsieve selector | re.Pattern
    source: str = "html"           # regex only: "html" (raw source) | "text" (visible text) (P15)


def _rule_name(raw: Any, used: Set[str], fallback: str) -> str:
    name = clean_text(raw)[:80] or fallback
    base, n = name, 2
    while name in used:
        name, n = f"{base} ({n})", n + 1
    used.add(name)
    return name


def compile_custom_searches(rules: Any) -> Tuple[List[CustomSearch], List[str]]:
    """Validated, compiled custom searches (at most MAX_CUSTOM_RULES) and a plain note per rule left out."""
    out: List[CustomSearch] = []
    problems: List[str] = []
    used: Set[str] = set()
    for i, rule in enumerate(rules or ()):
        if not isinstance(rule, dict):
            continue
        text = str(rule.get("text") or rule.get("expr") or "")
        if not text.strip():
            continue
        if len(out) >= MAX_CUSTOM_RULES:
            problems.append(f"Only the first {MAX_CUSTOM_RULES} custom searches are used.")
            break
        name = _rule_name(rule.get("name"), used, f"Search {len(out) + 1}")
        if len(text) > CUSTOM_EXPR_MAX:
            problems.append(f"Custom search '{name}' is longer than {CUSTOM_EXPR_MAX} characters and was left out.")
            continue
        mode = "not_contains" if str(rule.get("mode") or "").lower().replace(" ", "_") in (
            "not_contains", "does_not_contain") else "contains"
        where = str(rule.get("where") or "html").strip().lower().replace(" ", "_")
        where = {"page_text": "text", "page_text_no_anchors": "text_no_anchors"}.get(where, where)
        if where not in SEARCH_WHERE:
            where = "html"
        selector = None
        if where == "element":
            selector, why = compile_selector(rule.get("selector"))
            if selector is None:
                problems.append(f"Custom search '{name}' searches in an element but {why}; it was left out.")
                continue
        case = bool(rule.get("case_sensitive"))
        rx = None
        if rule.get("regex"):
            try:
                rx = re.compile(text, 0 if case else re.I)
            except re.error as e:
                problems.append(f"Custom search '{name}' is not a valid regex and was left out: {e}")
                continue
        else:
            text = _WS.sub(" ", text) if where in ("text", "text_no_anchors") else text
        out.append(CustomSearch(name, mode, where, text if case else text.lower(), rx, case, selector))
    return out, problems


def compile_selector(raw: Any) -> Tuple[Any, str]:
    """(("css" | "xpath", compiled), "") for "css:<selector>" / "xpath:<expression>" (a bare value starting with
    "/" or "(" is XPath, anything else CSS), else (None, plain reason)."""
    value = str(raw or "").strip()
    if not value:
        return None, "names no selector (css:... or xpath:...)"
    if len(value) > CUSTOM_EXPR_MAX:
        return None, f"its selector is longer than {CUSTOM_EXPR_MAX} characters"
    low = value.lower()
    if low.startswith("css:"):
        kind, expr = "css", value[4:].strip()
    elif low.startswith("xpath:"):
        kind, expr = "xpath", value[6:].strip()
    else:
        kind, expr = ("xpath" if value.startswith(("/", "(")) else "css"), value
    try:
        if kind == "xpath":
            from lxml import etree
            return ("xpath", etree.XPath(expr)), ""
        import soupsieve
        return ("css", soupsieve.compile(expr)), ""
    except Exception as e:
        first = str(e).splitlines()[0][:120] if str(e) else type(e).__name__
        return None, f"its {kind.upper()} selector is not valid ({first})"


def parse_bulk_searches(text: str, defaults: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Custom Search rules from a paste, one per line (Bulk add): "text", or "name<TAB>text" / "name | text";
    every rule takes the other fields from `defaults` (mode, where, regex, case_sensitive, selector)."""
    out: List[Dict[str, Any]] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        name, value = "", line
        for sep in ("\t", " | "):
            if sep in line:
                name, value = (x.strip() for x in line.split(sep, 1))
                break
        rule = dict(defaults or {})
        rule.update({"name": name or value[:80], "text": value})
        out.append(rule)
    return out[:MAX_CUSTOM_RULES]


def compile_custom_extractors(rules: Any) -> Tuple[List[CustomExtractor], List[str]]:
    """Validated, compiled custom extractors (at most MAX_CUSTOM_RULES) and a plain note per rule left out."""
    out: List[CustomExtractor] = []
    problems: List[str] = []
    used: Set[str] = set()
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        expr = str(rule.get("expr") or "").strip()
        if not expr:
            continue
        if len(out) >= MAX_CUSTOM_RULES:
            problems.append(f"Only the first {MAX_CUSTOM_RULES} custom extractors are used.")
            break
        name = _rule_name(rule.get("name"), used, f"Extractor {len(out) + 1}")
        kind = str(rule.get("type") or "css").strip().lower()
        extract = str(rule.get("extract") or "text").strip().lower().replace(" ", "_")
        extract = extract if extract in EXTRACT_MODES else "text"
        attr = str(rule.get("attr") or "").strip()
        if extract == "attribute" and not attr and kind != "regex":
            problems.append(f"Custom extractor '{name}' extracts an attribute but names none; it was left out.")
            continue
        if len(expr) > CUSTOM_EXPR_MAX:
            problems.append(f"Custom extractor '{name}' is longer than {CUSTOM_EXPR_MAX} characters and was left out.")
            continue
        try:
            if kind == "xpath":
                from lxml import etree
                compiled = etree.XPath(expr)
            elif kind == "regex":
                if "case_sensitive" in rule:          # P15; older rules carry ignore_case (default: case matters)
                    flags = 0 if rule.get("case_sensitive") else re.I
                else:
                    flags = re.I if rule.get("ignore_case") else 0
                compiled = re.compile(expr, flags)
            else:
                import soupsieve
                kind, compiled = "css", soupsieve.compile(expr)
        except Exception as e:
            problems.append(f"Custom extractor '{name}' ({kind}) is not valid and was left out: "
                            f"{str(e).splitlines()[0][:160] if str(e) else type(e).__name__}")
            continue
        source = "text" if kind == "regex" and str(rule.get("source") or "").lower() == "text" else "html"
        out.append(CustomExtractor(name, kind, extract, attr, compiled, source))
    return out, problems


@dataclass
class ExtractOptions:
    """Per-crawl settings for extract_page, compiled once: custom searches and extractors, whether to keep
    top_terms, a pool that makes strings repeated across pages (ids, terms) share one copy in memory, the spelling
    options, the Link Positions rules (P16), the content area selectors (P13) and the CDN hosts whose files count
    as the site's own (P11)."""
    searches: List[CustomSearch] = field(default_factory=list)
    extractors: List[CustomExtractor] = field(default_factory=list)
    top_terms: bool = True
    pool: Optional[Dict[str, str]] = None
    problems: List[str] = field(default_factory=list)
    spelling: Optional[Dict[str, Any]] = None   # CrawlConfig.spelling while it is on (None = no spelling check)
    link_positions: List["PositionRule"] = field(default_factory=list)   # CrawlConfig.link_positions, compiled
    content_include: List[Any] = field(default_factory=list)   # CrawlConfig.content_area["include"], compiled
    content_exclude: List[Any] = field(default_factory=list)   # CrawlConfig.content_area["exclude"], compiled
    cdn_hosts: Tuple[str, ...] = ()             # CrawlConfig.cdn_hosts: resources there are internal
    soft_404_phrases: Tuple[str, ...] = ()      # CrawlConfig.soft_404_phrases, lower case (P14)

    @classmethod
    def from_config(cls, config: Any = None, **overrides: Any) -> "ExtractOptions":
        """From a CrawlConfig (or keyword overrides: custom_searches, custom_extractors, top_terms, spelling,
        link_positions, content_area, cdn_hosts, soft_404_phrases)."""
        def pick(name: str, default: Any) -> Any:
            return overrides[name] if name in overrides else getattr(config, name, default)
        searches, p1 = compile_custom_searches(pick("custom_searches", None))
        extractors, p2 = compile_custom_extractors(pick("custom_extractors", None))
        spelling = pick("spelling", None)
        spelling = dict(spelling) if isinstance(spelling, dict) and spelling.get("enabled") else None
        positions, p3 = compile_link_positions(pick("link_positions", None))
        area = pick("content_area", None)
        area = area if isinstance(area, dict) else {}
        include, p4 = compile_selectors(area.get("include"), "Content area include")
        exclude, p5 = compile_selectors(area.get("exclude"), "Content area exclude")
        cdn = tuple(h for h in (clean_hostname(str(x or "").strip().lower()) for x in pick("cdn_hosts", None) or ())
                    if h)
        phrases = tuple(p for p in (" ".join(str(x).lower().split()) for x in pick("soft_404_phrases", None) or ()
                                    if isinstance(x, str)) if p)
        return cls(searches=searches, extractors=extractors, top_terms=bool(pick("top_terms", True)),
                   pool={}, problems=p1 + p2 + p3 + p4 + p5, spelling=spelling, link_positions=positions,
                   content_include=include, content_exclude=exclude, cdn_hosts=cdn, soft_404_phrases=phrases)


def on_cdn(url: str, cdn_hosts: Iterable[str]) -> bool:
    """The URL is on one of the CDN hosts (or a subdomain of one) the crawl counts as the site's own."""
    if not cdn_hosts or not url:
        return False
    try:
        host = clean_hostname(get_domain(url) or "")
    except Exception:
        return False
    return bool(host) and any(host == h or host.endswith("." + h) for h in cdn_hosts)


# ---------------------------------------------------------------------------------------------
# Link positions (P16): where on the page a link sits
# ---------------------------------------------------------------------------------------------
# Configuration > Link Positions ids (config.LINK_POSITIONS) and the label every surface shows
POSITION_LABELS: Dict[str, str] = {
    "content": "Main content", "navigation": "Navigation menu", "header": "Header", "footer": "Footer",
    "sidebar": "Sidebar", "breadcrumb": "Breadcrumb", "form": "Form", "page_body": "Page body"}
_LABEL_POSITION = {v: k for k, v in POSITION_LABELS.items()}
_SECTIONING = frozenset({"article", "aside", "main", "nav", "section"})
_ROLE_REGIONS = {"banner": "Header", "navigation": "Navigation menu", "menubar": "Navigation menu",
                 "menu": "Navigation menu", "contentinfo": "Footer", "complementary": "Sidebar",
                 "main": "Main content", "article": "Main content"}
_TOKEN_SPLIT = re.compile(r"[\s_\-]+")
_NAV_WORDS = frozenset({"nav", "navs", "navbar", "navigation", "navmenu", "navlinks", "menu", "menus", "menubar",
                        "megamenu", "mainmenu", "topmenu", "submenu", "subnav", "topnav", "mainnav", "sitenav",
                        "primarynav"})
_HEADER_WORDS = frozenset({"header", "masthead", "topbar"})
_CONTENT_WORDS = frozenset({"content", "maincontent", "main", "article", "post", "entry", "story", "prose"})
# Words that make "header" / "footer" in a class the heading or foot of a part of the page, not of the page
# (page builders put "widget" / "section" on every block, so those words are not among them)
_COMPONENT_WORDS = frozenset({"card", "entry", "post", "article", "panel", "modal", "dialog", "table", "product",
                              "comment", "comments", "accordion", "tab", "slide", "popup", "tile", "teaser"})
_PAGE_WIDE_FORM_IDS = frozenset({"form1", "aspnetform"})


def position_of(region: Any) -> str:
    """The Link Positions id of a link's region label ("Main content" -> "content", "Navigation menu" ->
    "navigation"); "other" for a place that is not a position on the page (Page <head>, HTTP redirect, CSS background
    image, an HTTP header)."""
    return _LABEL_POSITION.get(str(region or ""), "other")


def _nav_word(t: str) -> bool:
    """A class / id word that names a menu: exact words only ("headermenu" is not one: a header bar's class)."""
    return t in _NAV_WORDS


def _token_region(tokens: Set[str]) -> str:
    """The region a set of class / id words names ("" when none): breadcrumb, footer, sidebar, navigation, header,
    content, in that order ("footer-menu" is the footer, "header-menu" the navigation). A header or footer word next
    to a component word ("card-header", "entry-footer") is the heading or foot of that component, not of the page."""
    if not tokens:
        return ""
    if any("breadcrumb" in t or t == "crumbs" for t in tokens):
        return "Breadcrumb"
    component = not tokens.isdisjoint(_COMPONENT_WORDS)
    if not component and any(t == "colophon" or t.startswith("footer") or t.endswith("footer") for t in tokens):
        return "Footer"
    nav = any(_nav_word(t) for t in tokens)
    if any(t.startswith("sidebar") or t.endswith("sidebar") for t in tokens):
        return "Navigation menu" if nav else "Sidebar"      # "menu-sidebar": an off-canvas menu panel
    if nav:
        return "Navigation menu"
    if not component and any(t in _HEADER_WORDS or t.startswith("header") or t.endswith("header") for t in tokens):
        return "Header"
    if not tokens.isdisjoint(_CONTENT_WORDS):
        return "Main content"
    return ""


@dataclass
class PositionRule:
    """One Configuration > Link Positions rule: links inside an element that matches go to `position`."""
    position: str                  # config.LINK_POSITIONS id
    match: str                     # "class" | "id" | "css" | "xpath"
    value: str                     # lower-cased for class / id
    label: str                     # "class: promo" (PageData links[].position_rule)
    compiled: Any = None           # soupsieve selector (css) | lxml XPath (xpath)


def compile_link_positions(rules: Any) -> Tuple[List[PositionRule], List[str]]:
    """([PositionRule], problems) from CrawlConfig.link_positions ([{"position", "match", "value"}], already
    cleaned by config.clean_link_positions). An invalid CSS selector or XPath is left out and named."""
    out: List[PositionRule] = []
    problems: List[str] = []
    for i, rule in enumerate(rules or []):
        if not isinstance(rule, dict):
            continue
        position = str(rule.get("position") or "").strip().lower()
        match = str(rule.get("match") or "").strip().lower()
        value = str(rule.get("value") or "").strip()
        if position not in POSITION_LABELS or match not in ("class", "id", "css", "xpath") or not value:
            problems.append(f"Link position rule {i + 1} is incomplete, so it is not used.")
            continue
        compiled = None
        try:
            if match == "css":
                import soupsieve
                compiled = soupsieve.compile(value)
            elif match == "xpath":
                from lxml import etree
                compiled = etree.XPath(value)
        except Exception:
            problems.append(f"Link position rule {i + 1}: '{value[:80]}' is not a valid "
                            f"{'CSS selector' if match == 'css' else 'XPath'}, so it is not used.")
            continue
        out.append(PositionRule(position=position, match=match,
                                value=value.lower() if match in ("class", "id") else value,
                                label=f"{match}: {value[:100]}", compiled=compiled))
    return out, problems


class LinkPositioner:
    """The position of each link (and resource) of one page: Configuration > Link Positions rules first (the first
    rule, in list order, that matches the link or an element around it), then the page's own structure. Automatic
    rules: the closest element around the link that names a region wins - its tag (header, footer, nav, aside,
    main, article, form), its ARIA role, then its class / id words (navbar, megamenu, menu, nav, breadcrumb, footer,
    header, sidebar, content...; <body> itself is not read). Content is a weak region: any other region around or
    inside it wins (a sidebar inside <main> is the sidebar). A menu inside the footer or a sidebar counts as the
    footer / sidebar. A <header> or <footer> inside an article, section, aside, main or nav is that part's own
    heading or foot, not the page's (as the HTML accessibility mapping reads it). A <form> that wraps the whole page
    (ASP.NET WebForms) is ignored. No region around the link: the page's content ("Main content"; the body minus
    header, menus, footer and sidebars is the content, as the word count reads it). "Page body" is only given by a
    Link Positions rule."""

    def __init__(self, soup, html: str, idx, rules: Optional[List[PositionRule]] = None):
        self.soup = soup
        self.html = html
        self.idx = idx
        self.rules = rules or []
        self._own: Dict[int, str] = {}
        self._matched: Dict[str, Set[int]] = {}
        self._lxml_map: Any = False

    # -- user rules ----------------------------------------------------------------------------
    def _rule_ids(self, rule: PositionRule) -> Set[int]:
        hit = self._matched.get(rule.label)
        if hit is not None:
            return hit
        hit = set()
        try:
            if rule.match == "css":
                hit = {id(el) for el in rule.compiled.select(self.soup)}
            elif rule.match == "xpath":
                hit = self._xpath_ids(rule)
        except Exception:
            hit = set()
        self._matched[rule.label] = hit
        return hit

    def _xpath_ids(self, rule: PositionRule) -> Set[int]:
        """id() of the soup elements an XPath rule matches: the page is parsed once more with lxml and the two trees
        are matched element by element in page order (both come from lxml's HTML parser); nothing matches when the
        trees differ."""
        if self._lxml_map is False:
            self._lxml_map = None
            try:
                from lxml import html as lxml_html
                parser = lxml_html.HTMLParser(encoding="utf-8")
                tree = lxml_html.document_fromstring((self.html or "<html></html>").encode("utf-8"), parser=parser)
                elems = [e for e in tree.iter() if isinstance(e.tag, str)]
                tags = self.idx.all
                if len(elems) == len(tags) and all(str(e.tag).lower() == t.name for e, t in zip(elems, tags)):
                    self._lxml_map = (tree, {e: i for i, e in enumerate(elems)})
            except Exception:
                self._lxml_map = None
        if not self._lxml_map:
            return set()
        tree, pos = self._lxml_map
        found = rule.compiled(tree)
        tags = self.idx.all
        return {id(tags[pos[e]]) for e in (found if isinstance(found, list) else []) if e in pos}

    def _rule_for(self, chain: List[Any]) -> Optional[PositionRule]:
        for rule in self.rules:
            if rule.match in ("class", "id"):
                attr = rule.match
                for el in chain:
                    v = el.get(attr)
                    text = " ".join(v) if isinstance(v, list) else str(v or "")
                    if rule.value in text.lower():
                        return rule
            else:
                ids = self._rule_ids(rule)
                if ids and any(id(el) in ids for el in chain):
                    return rule
        return None

    # -- automatic -----------------------------------------------------------------------------
    def _scoped(self, el) -> bool:
        """A <header> / <footer> inside sectioning content belongs to that section, not to the page."""
        for p in el.parents:
            name = getattr(p, "name", None)
            if name in ("body", "html", None, "[document]"):
                return False
            if name in _SECTIONING or str(p.get("role") or "").lower() in ("main", "article"):
                return True
        return False

    def _page_wide_form(self, form) -> bool:
        try:
            if form.find(["header", "nav", "footer", "main", "aside", "article"]) is not None:
                return True
            parent = getattr(form.parent, "name", None)
            if parent in ("body", "html", "[document]", None):
                return (str(form.get("id") or "").lower() in _PAGE_WIDE_FORM_IDS
                        or len(form.find_all("a", limit=10)) >= 10)
        except Exception:
            return False
        return False

    def _own_region(self, el) -> str:
        key = id(el)
        hit = self._own.get(key)
        if hit is not None:
            return hit
        name = el.name
        cls = el.get("class")
        words = (" ".join(cls) if isinstance(cls, list) else str(cls or "")) + " " + str(el.get("id") or "")
        aria = str(el.get("aria-label") or "")
        tokens = {t for t in _TOKEN_SPLIT.split((words + " " + aria).lower()) if t}
        region = ""
        if any("breadcrumb" in t for t in tokens) or "breadcrumblist" in str(el.get("itemtype") or "").lower():
            region = "Breadcrumb"
        elif name in ("header", "footer") and not self._scoped(el):
            region = "Header" if name == "header" else "Footer"
        elif name == "nav":
            region = "Navigation menu"
        elif name == "aside":
            region = "Sidebar"
        elif name in ("main", "article"):
            region = "Main content"
        elif name == "form":
            region = "" if self._page_wide_form(el) else "Form"
        if not region:
            region = _ROLE_REGIONS.get(str(el.get("role") or "").strip().lower(), "")
        if not region and name not in ("header", "footer", "form"):
            region = _token_region(tokens)
        elif not region and name in ("header", "footer"):
            region = _token_region(tokens - {"header", "footer"}) if tokens else ""
        self._own[key] = region
        return region

    def region(self, el) -> Tuple[str, str]:
        """(region label, the user rule that placed it or "") of an element of this page."""
        chain = [el]
        for p in el.parents:
            if getattr(p, "name", None) in ("body", "html", None, "[document]"):
                break
            chain.append(p)
        if self.rules:
            rule = self._rule_for(chain)
            if rule is not None:
                return POSITION_LABELS[rule.position], rule.label
        found = [r for r in (self._own_region(p) for p in chain[1:]) if r]
        if not found:
            return "Main content", ""
        others = [r for r in found if r != "Main content"]
        if not others:
            return "Main content", ""
        first = others[0]
        if first == "Navigation menu":
            outer = next((r for r in others[1:] if r in ("Footer", "Sidebar")), None)
            if outer:
                return outer, ""
        return first, ""


SPELLING_NOT_INSTALLED = "Spelling check not installed"
_spelling_module: List[Any] = []          # [module] once imported, [None] when it could not be


def spelling_check(text: str, lang: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """PageData["spelling"] for one page's content-area text: crawler/seo/spelling.check_page (content lane),
    or {"checked": False, "reason"} when the module is missing or fails on this page."""
    if not _spelling_module:
        try:
            from . import spelling as module
        except Exception:
            module = None
        _spelling_module.append(module)
    module = _spelling_module[0]
    if module is None or not callable(getattr(module, "check_page", None)):
        return {"lang": lang or "", "checked": False, "reason": SPELLING_NOT_INSTALLED, "words": 0,
                "unknown": {}, "capped": False}
    try:
        out = module.check_page(text, lang, options)
    except Exception as e:
        return {"lang": lang or "", "checked": False, "words": 0, "unknown": {}, "capped": False,
                "reason": f"The spelling check failed on this page ({type(e).__name__})."}
    return out if isinstance(out, dict) else {"lang": lang or "", "checked": False, "words": 0, "unknown": {},
                                              "capped": False, "reason": "The spelling check returned nothing."}


DEFAULT_OPTIONS = ExtractOptions()


def _clip(value: Any) -> str:
    value = "" if value is None else str(value)
    return value if len(value) <= CUSTOM_VALUE_MAX else value[:CUSTOM_VALUE_MAX]


def _xpath_value(item: Any, rule: CustomExtractor) -> str:
    from lxml import etree
    if isinstance(item, etree._Element):
        if rule.extract == "attribute":
            return _clip(item.get(rule.attr) or "")
        if rule.extract == "element":
            return _clip(etree.tostring(item, encoding="unicode", method="html", with_tail=False))
        if rule.extract == "inner_html":
            inner = (html_lib.escape(item.text, quote=False) if item.text else "") + "".join(
                etree.tostring(child, encoding="unicode", method="html") for child in item)
            return _clip(inner)
        try:
            return _clip(clean_text(item.text_content()))
        except Exception:
            return ""
    if isinstance(item, bool):
        return "true" if item else "false"
    if isinstance(item, float):
        return str(int(item)) if item.is_integer() else repr(item)
    return _clip(clean_text(item) if rule.extract == "text" else str(item))


def _css_value(el, rule: CustomExtractor) -> str:
    if rule.extract == "attribute":
        val = el.get(rule.attr)
        return _clip(" ".join(val) if isinstance(val, list) else (val or ""))
    if rule.extract == "element":
        return _clip(str(el))
    if rule.extract == "inner_html":
        return _clip(el.decode_contents())
    return _clip(_el_text(el))


def run_custom_rules(page: Dict[str, Any], html: str, text: str, soup, options: ExtractOptions) -> None:
    """Fills page["custom_search"] ({name: occurrences}) and page["custom_extraction"] ({name: [values]}).
    A rule that fails on this page gives no values; it never breaks the page's extraction."""
    if options.searches:
        lowered: Dict[Any, str] = {}
        hays: Dict[Any, str] = {"html": html, "text": text}
        for rule in options.searches:
            key = rule.where if rule.where != "element" else ("element", id(rule))
            hay = hays.get(key)
            if hay is None:
                try:
                    hay = hays[key] = _search_hay(rule, html, soup)
                except Exception:
                    hay = hays[key] = ""
            try:
                if rule.regex is not None:
                    count = 0
                    for _ in rule.regex.finditer(hay):
                        count += 1
                        if count >= SEARCH_COUNT_MAX:
                            break
                elif rule.case_sensitive:
                    count = hay.count(rule.text)
                else:
                    low = lowered.get(key)
                    if low is None:
                        low = lowered[key] = hay.lower()
                    count = low.count(rule.text)
            except Exception:
                count = 0
            page["custom_search"][rule.name] = count
    if not options.extractors:
        return
    tree = None
    for rule in options.extractors:
        values: List[str] = []
        try:
            if rule.kind == "regex":
                for m in rule.compiled.finditer(text if rule.source == "text" else html):
                    values.append(_clip((m.group(1) if rule.compiled.groups else m.group(0)) or "").strip())
                    if len(values) >= CUSTOM_MATCHES_MAX:
                        break
            elif rule.kind == "css":
                for el in rule.compiled.select(soup, limit=CUSTOM_MATCHES_MAX):
                    values.append(_css_value(el, rule))
            else:
                if tree is None:
                    from lxml import html as lxml_html
                    parser = lxml_html.HTMLParser(encoding="utf-8")
                    tree = lxml_html.document_fromstring((html or "<html></html>").encode("utf-8"), parser=parser)
                found = rule.compiled(tree)
                for item in (found if isinstance(found, list) else [found])[:CUSTOM_MATCHES_MAX]:
                    values.append(_xpath_value(item, rule))
        except Exception:
            values = []
        page["custom_extraction"][rule.name] = [v for v in values if v != ""]


def _text_without_links(root) -> str:
    """Visible text under `root` without the text of links (Custom Search "Page Text No Anchors")."""
    from bs4.element import NavigableString, Comment, Declaration, Doctype, ProcessingInstruction, CData
    skip_types = (Comment, Declaration, Doctype, ProcessingInstruction, CData)
    parts: List[str] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if isinstance(node, NavigableString):
            if not isinstance(node, skip_types):
                parts.append(str(node))
            continue
        name = getattr(node, "name", None)
        if name in HIDDEN_TEXT_TAGS or name == "a":
            continue
        stack.extend(reversed(getattr(node, "contents", None) or ()))
    return clean_text(" ".join(parts))


def _search_hay(rule: "CustomSearch", html: str, soup) -> str:
    """The part of the page a Custom Search with where head / body / text_no_anchors / element looks in."""
    if rule.where == "head":
        head = soup.find("head") if soup is not None else None
        return str(head) if head is not None else ""
    if rule.where == "body":
        body = soup.find("body") if soup is not None else None
        return str(body) if body is not None else ""
    if rule.where == "text_no_anchors":
        body = (soup.find("body") or soup) if soup is not None else None
        return _text_without_links(body) if body is not None else ""
    if rule.where == "element" and rule.selector is not None:
        kind, compiled = rule.selector
        if kind == "css":
            if soup is None:
                return ""
            return "\n".join(str(el) for el in compiled.select(soup, limit=CUSTOM_MATCHES_MAX))
        from lxml import etree, html as lxml_html
        tree = lxml_html.document_fromstring((html or "<html></html>").encode("utf-8"),
                                             parser=lxml_html.HTMLParser(encoding="utf-8"))
        found = compiled(tree)
        out = []
        for item in (found if isinstance(found, list) else [found])[:CUSTOM_MATCHES_MAX]:
            out.append(etree.tostring(item, encoding="unicode", method="html") if hasattr(item, "tag") else str(item))
        return "\n".join(out)
    return html


def custom_rules_on_html(html: str, options: ExtractOptions) -> Tuple[Dict[str, int], Dict[str, List[str]]]:
    """({search name: occurrences}, {extractor name: [values]}) of one HTML document, read the way
    extract_page reads a page. Used for the rendered HTML of pages rendered in Chrome: as in Screaming Frog's
    JavaScript rendering mode, Custom Search and Custom Extraction then run on the rendered page."""
    page: Dict[str, Any] = {"custom_search": {}, "custom_extraction": {}}
    if html and (options.searches or options.extractors):
        soup = parse_html(html)
        body = soup.find("body") or soup
        text = clean_text(" ".join(chunk for chunk, _ in _visible_strings(body)))
        run_custom_rules(page, html, text, soup, options)
    return page["custom_search"], page["custom_extraction"]


# ---------------------------------------------------------------------------------------------
# Static accessibility, plugins, background images, top terms, schema vs visible page
# ---------------------------------------------------------------------------------------------
MAX_IDS = 5000
MAX_A11Y_LIST = 50
MAX_BACKGROUND_IMAGES = 200
MAX_PLUGINS = 20
TOP_TERMS = 100
# Attributes the element walk looks at (dispatch by name keeps the walk to one set test per attribute)
_WALK_ATTRS = frozenset({"id", "name", "data-nosnippet", "role", "tabindex", "style", "aria-labelledby",
                         "aria-describedby", "aria-controls", "aria-owns"})
_IDREF_ATTRS = ("aria-labelledby", "aria-describedby", "aria-controls", "aria-owns")
_BG_DECL = re.compile(r"background(?:-image)?\s*:([^;}]*)", re.I)
_CSS_URL = re.compile(r"""url\(\s*(['"]?)(.*?)\1\s*\)""", re.I | re.S)
_PLUGIN_TYPES = (("shockwave", "Flash"), ("futuresplash", "Flash"), ("silverlight", "Silverlight"),
                 ("x-java", "Java"), ("application/java", "Java"))
_PLUGIN_EXT = {".swf": "Flash", ".xap": "Silverlight", ".class": "Java", ".jar": "Java"}
_FLASH_CLSID = "d27cdb6e-ae6d-11cf-96b8-444553540000"
_PLUGIN_PARAMS = ("movie", "src", "code", "filename", "source")
# Common English function words left out of top_terms (our own short list)
STOP_WORDS = frozenset("""
a about above after again against all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here
hers him his how if in into is it its itself just me more most my no nor not now of off on once only or other our
ours out over own same she should so some such than that the their theirs them then there these they this those
through to too under until up very was we were what when where which while who whom why will with would you your
yours us get got may might must shall upon via per etc yes ie eg within without across onto
""".split())
_PHONE_LIKE = re.compile(r"\+?\d[\d\s().\-/]{4,}\d")
_NUMBER = re.compile(r"\d[\d,.]*\d|\d")
# A price shown on the page: a currency sign or code (or the word price / MRP) right before a number, or a
# currency code right after one. Found with str.find (a regex alternation tried at every position is slow)
_PRICE_BEFORE = ("$", "€", "£", "¥", "₹", "rs", "inr", "usd", "eur", "gbp", "aud", "cad", "sgd", "aed", "price",
                 "mrp", "sale")
_PRICE_AFTER = ("inr", "usd", "eur", "gbp", "aud", "cad", "sgd", "aed", "$", "€", "£", "₹")
_NUM_FOLLOWS = re.compile(r"\.?\s*[:\-]?\s*\d")
_NUM_PRECEDES = re.compile(r"\d\s*$")
_QUOTE_PAIRS = (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), (" ", " "))


def _is_hidden(el) -> bool:
    """Hidden from assistive technology: aria-hidden / hidden on it or an ancestor, or display:none inline."""
    node = el
    while node is not None and getattr(node, "name", None) not in (None, "[document]", "body", "html"):
        attrs = node.attrs or {}
        if str(attrs.get("aria-hidden") or "").strip().lower() == "true" or "hidden" in attrs:
            return True
        style = str(attrs.get("style") or "").replace(" ", "").lower()
        if "display:none" in style or "visibility:hidden" in style:
            return True
        node = node.parent
    return False


def _has_name(el, text: str = "") -> bool:
    """A link or button has an accessible name: text, aria-label/-labelledby, title, an image alt or an
    SVG <title> inside it."""
    if text:
        return True
    for attr in ("aria-label", "aria-labelledby", "title"):
        if str(el.get(attr) or "").strip():
            return True
    if el.name == "input":
        return bool(str(el.get("value") or el.get("alt") or "").strip())
    for img in el.find_all("img"):
        if clean_text(img.get("alt")):
            return True
    for t in el.find_all("title"):
        if clean_text(t.get_text()):
            return True
    return False


def _control_labelled(el, labels_for: Dict[str, bool]) -> bool:
    """A form control has a label (axe's rule as siteone-crawler ports it): aria-label/-labelledby, title, a
    <label for> with text, a wrapping <label> with text, or a placeholder (input and textarea only)."""
    for attr in ("aria-label", "aria-labelledby", "title"):
        if str(el.get(attr) or "").strip():
            return True
    cid = el.get("id")
    if cid and labels_for.get(cid if isinstance(cid, str) else " ".join(cid)):
        return True
    if el.name in ("input", "textarea") and str(el.get("placeholder") or "").strip():
        return True
    wrap = el.find_parent("label")
    return wrap is not None and bool(_el_text(wrap))


def _accessibility(idx, page: Dict[str, Any], ids_seen: Dict[str, int], refs: List[str], has_main: bool,
                   tabindex_positive: int, empty_links: int, html_lang: Optional[str]) -> Dict[str, Any]:
    labels_for: Dict[str, bool] = {}
    for lab in idx.tags("label"):
        target = lab.get("for")
        if target:
            target = target if isinstance(target, str) else " ".join(target)
            labels_for[target] = labels_for.get(target, False) or bool(_el_text(lab))
            refs.append(target)
    unlabeled = empty_buttons = 0
    for name in ("input", "select", "textarea"):
        for el in idx.tags(name):
            if name == "input":
                itype = str(el.get("type") or "text").strip().lower()
                if itype in ("hidden", "submit", "reset"):
                    continue    # hidden, or a button with a default name ("Submit")
                if itype in ("button", "image"):
                    if not _has_name(el) and not _is_hidden(el):
                        empty_buttons += 1
                    continue
            if not _control_labelled(el, labels_for) and not _is_hidden(el):
                unlabeled += 1
    for el in idx.tags("button"):
        if not _has_name(el, _el_text(el)) and not _is_hidden(el):
            empty_buttons += 1
    images_without_alt = 0
    for img in idx.tags("img"):
        if img.get("alt") is None and str(img.get("role") or "").lower() not in ("presentation", "none") \
                and str(img.get("aria-hidden") or "").lower() != "true":
            images_without_alt += 1
    broken: List[str] = []
    for ref in refs:
        if ref and ref not in ids_seen and ref not in broken:
            broken.append(ref)
            if len(broken) >= MAX_A11Y_LIST:
                break
    skips: List[str] = []
    prev = None
    for level, _ in page["headings"]:
        if prev is not None and level > prev + 1:
            label = f"h{prev} -> h{level}"
            if label not in skips:
                skips.append(label)
        prev = level
    return {
        "html_lang": html_lang,
        "inputs_without_label": unlabeled,
        "controls_without_name": empty_links + empty_buttons,
        "images_without_alt": images_without_alt,
        "duplicate_ids": [k for k, n in ids_seen.items() if n > 1][:MAX_A11Y_LIST],
        "aria_broken_refs": broken,
        "has_main_landmark": has_main,
        "heading_skips": skips[:20],
        "empty_links": empty_links,
        "empty_buttons": empty_buttons,
        "tabindex_positive": tabindex_positive,
    }


def _background_urls(css: str, base: str, out: List[str], seen: Set[str]) -> None:
    """url() values of background / background-image declarations, absolute (data: URIs left out)."""
    for decl in _BG_DECL.finditer(css):
        for m in _CSS_URL.finditer(decl.group(1)):
            raw = m.group(2).strip()
            key = "\0" + raw                      # the value as written, already resolved on this page
            if not raw or key in seen or raw[:5].lower() == "data:" or len(out) >= MAX_BACKGROUND_IMAGES:
                continue
            seen.add(key)
            url = _abs(raw, base)
            if url and url not in seen:
                seen.add(url)
                out.append(url)


def _plugins(idx, base: str) -> List[str]:
    """Browser plugins the page embeds (Flash, Java, Silverlight), which no current browser runs."""
    found: List[str] = []
    for el in idx.tags("object", "embed", "applet"):   # document order (only pages that have any)
        ptype = str(el.get("type") or "").lower()
        kind = next((label for needle, label in _PLUGIN_TYPES if needle in ptype), "")
        srcs = [str(el.get(a) or "").strip() for a in ("data", "src", "code", "archive", "movie")]
        if el.name == "object":
            for param in el.find_all("param"):
                if str(param.get("name") or "").strip().lower() in _PLUGIN_PARAMS:
                    srcs.append(str(param.get("value") or "").strip())
            classid = str(el.get("classid") or "").lower()
            if not kind and _FLASH_CLSID in classid:
                kind = "Flash"
            elif not kind and "java" in classid:
                kind = "Java"
        srcs = [s for s in srcs if s]
        if not kind:
            for s in srcs:
                path = s.split("?", 1)[0].split("#", 1)[0].lower()
                kind = next((label for ext, label in _PLUGIN_EXT.items() if path.endswith(ext)), "")
                if kind:
                    break
        if not kind and el.name == "applet":
            kind = "Java"
        if kind:
            entry = f"{kind}: {_abs(srcs[0], base)}" if srcs else kind
            if entry not in found:
                found.append(entry)
                if len(found) >= MAX_PLUGINS:
                    break
    return found


def _top_terms(tokens: List[str], pool: Optional[Dict[str, str]]) -> List[Tuple[str, int]]:
    """Top TOP_TERMS (word, count) of the content area's words (word_tokens); counted in C, then the filter
    runs once per distinct word (short words, stop words and words starting with a digit are left out)."""
    counts = Counter(tokens)
    for word in [w for w in counts if len(w) < 3 or w in STOP_WORDS or w[0].isdigit()]:
        del counts[word]
    top = counts.most_common(TOP_TERMS)
    if pool is None:
        return top
    return [(pool.setdefault(t, t), n) for t, n in top]


def _parse_number(raw: str) -> Optional[float]:
    """A number as a page writes it: 1,299.00 / 1.299,00 / 19,99 / 1,29,999 (Indian grouping)."""
    s = raw.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("." + tail if len(tail) in (1, 2) else tail)
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _boundary(text: str, i: int) -> bool:
    """No letter or digit at position i (or i is outside the text)."""
    return i < 0 or i >= len(text) or not (text[i].isalnum() or text[i] == "_")


def has_word(hay: str, needle: str) -> bool:
    """`needle` occurs in `hay` as whole words (str.find, then the characters around each hit are checked)."""
    i = hay.find(needle)
    while i >= 0:
        if _boundary(hay, i - 1) and _boundary(hay, i + len(needle)):
            return True
        i = hay.find(needle, i + 1)
    return False


def shows_price(low: str) -> bool:
    """The lowercased visible text shows a price (see _PRICE_BEFORE / _PRICE_AFTER)."""
    for sign in _PRICE_BEFORE:
        i = low.find(sign)
        while i >= 0:
            end = i + len(sign)
            boundary_ok = not sign.isalpha() or (_boundary(low, i - 1) and _boundary(low, end))
            if boundary_ok and _NUM_FOLLOWS.match(low, end):
                return True
            i = low.find(sign, end)
    for code in _PRICE_AFTER:
        i = low.find(code)
        while i >= 0:
            boundary_ok = not code.isalpha() or _boundary(low, i + len(code))
            if boundary_ok and _NUM_PRECEDES.search(low, max(0, i - 4), i):
                return True
            i = low.find(code, i + len(code))
    return False


def _plain_quotes(text: str) -> str:
    for curly, plain in _QUOTE_PAIRS:
        if curly in text:
            text = text.replace(curly, plain)
    return text


def _norm_value(value: str) -> str:
    return " ".join(_plain_quotes(html_lib.unescape(value)).split()).lower()


def schema_vs_page(values: List[Tuple[str, str, str]], visible: str, extra: str,
                   contact_hrefs: List[str]) -> Tuple[List[dict], List[dict], int]:
    """(mismatches, unverified, values checked): JSON-LD values the visible page does not show. Phones and
    postcodes match by their digits, prices numerically (within 0.005), other values as whole words of the
    visible text, title or og:site_name. A price on a page whose HTML shows no price at all is unverified
    (it is probably drawn by JavaScript), never a mismatch."""
    mismatch: List[dict] = []
    unverified: List[dict] = []
    checked = 0
    if not values:
        return mismatch, unverified, checked
    # The visible text is already unescaped and whitespace-collapsed (clean_text)
    hay = _plain_quotes(_norm_value(extra) + " " + visible).lower()
    links = " ".join(contact_hrefs).lower()
    digit_groups: Optional[Set[str]] = None
    numbers: Optional[List[float]] = None
    price_shown: Optional[bool] = None
    for kind, name, value in values:
        ok: Optional[bool] = None
        if name in ("telephone", "postalCode") and re.search(r"\d", value):
            digits = re.sub(r"\D", "", value)
            if len(digits) < 4:
                continue
            if digit_groups is None:
                digit_groups = {re.sub(r"\D", "", m) for m in _PHONE_LIKE.findall(visible + " " + links)}
                digit_groups |= set(re.findall(r"\d+", visible))
            key = digits[-8:]
            ok = any(key in g for g in digit_groups)
        elif name == "price":
            target = _parse_number(value)
            if target is None:
                continue
            if price_shown is None:
                price_shown = shows_price(hay)
            if not price_shown:
                if len(unverified) < MAX_SCHEMA_REPORTED:
                    unverified.append({"type": kind, "field": name, "value": value[:200],
                                       "reason": "the page's HTML shows no prices (probably added by JavaScript)"})
                continue
            if numbers is None:
                numbers = [n for n in (_parse_number(m) for m in _NUMBER.findall(visible)) if n is not None]
            ok = any(abs(n - target) < 0.005 for n in numbers)
        else:
            needle = _norm_value(value)
            if len(needle) < 2:
                continue
            if name == "email" and needle in links:
                ok = True
            else:
                ok = has_word(hay, needle)
        checked += 1
        if not ok and len(mismatch) < MAX_SCHEMA_REPORTED:
            mismatch.append({"type": kind, "field": name, "value": value[:200]})
    return mismatch, unverified, checked


def extract_page(html: str, final_url: str, response, base_domain: str, include_subdomains: bool = False,
                 allowed_domains: Optional[Set[str]] = None, soup=None, url: str = "",
                 max_links: int = 0, options: Optional[ExtractOptions] = None) -> Dict[str, Any]:
    """PageData for an HTML response. `soup` reuses a parse made by the caller (see parse_html);
    `url` is the URL as crawled (defaults to final_url); `max_links` > 0 keeps only the first links;
    `options` (ExtractOptions.from_config) adds custom searches / extractors and shares repeated strings."""
    options = options or DEFAULT_OPTIONS
    pool = options.pool
    html = html or ""
    page = new_page(url or final_url)
    page["final_url"] = final_url or page["url"]
    _apply_headers(page, response, page["final_url"])
    if not page["size_bytes"]:
        page["size_bytes"] = len(html.encode("utf-8", errors="ignore"))
        if not page["transferred_bytes"]:
            page["transferred_bytes"] = page["size_bytes"]
    if soup is None:
        soup = parse_html(html)
    lines = soup.__dict__.get("_crawler_source_lines") or SourceLines(html)
    idx = tag_index(soup)
    page["is_html"] = True
    has_head = bool(idx.tags("head"))
    positions = LinkPositioner(soup, html, idx, options.link_positions)
    cdn_hosts = options.cdn_hosts

    # Base URL for relative links (<base href> wins over the page URL)
    base = page["final_url"]
    base_el = next((b for b in idx.tags("base") if b.get("href")), None)
    if base_el:
        try:
            base = join_url(base, str(base_el["href"]).strip())
        except Exception:
            pass
    https_page = page["final_url"].lower().startswith("https:")

    html_el = next(iter(idx.tags("html")), None)
    html_lang: Optional[str] = None                # a11y: None = the <html> element has no lang attribute
    if html_el is not None:
        page["language"] = str(html_el.get("lang") or html_el.get("xml:lang") or "").strip()
        if html_el.get("lang") is not None or html_el.get("xml:lang") is not None:
            html_lang = page["language"]

    # Titles (SVG <title> elements are icon labels, not page titles)
    for t in idx.tags("title"):
        if _in_svg(t):
            continue
        page["titles"].append(_el_text(t))
        if not _in_head(t, has_head):
            page["title_outside_head"] = True

    # Meta tags
    generic_robots: List[str] = []                 # <meta name="robots"> only
    bot_robots: Dict[str, List[str]] = {}          # <meta name="googlebot"> / <meta name="bingbot">
    meta_charset: Optional[str] = None
    og, tw = page["og"], page["twitter"]
    for m in idx.tags("meta"):
        name = str(m.get("name") or "").strip().lower()
        equiv = str(m.get("http-equiv") or "").strip().lower()
        content = m.get("content")
        prop = str(m.get("property") or "").strip().lower() or name
        if prop.startswith("og:"):
            key = prop[3:]
            if key in og and not og[key]:
                og[key] = clean_text(content)
        elif prop.startswith("twitter:"):
            key = "image" if prop == "twitter:image:src" else prop[8:]
            if key in tw and not tw[key]:
                tw[key] = clean_text(content)
        if name == "fragment" and clean_text(content) == "!":
            page["meta_fragment"] = True
        if name == "description":
            page["meta_descriptions"].append(clean_text(content))
            if not _in_head(m, has_head):
                page["meta_description_outside_head"] = True
        elif name == "keywords":
            page["meta_keywords"].append(clean_text(content))
        elif name in ROBOTS_META_NAMES:
            page["meta_robots"].append(clean_text(content).lower())
            if not _in_head(m, has_head):
                page["meta_robots_outside_head"] = True
        elif name == "viewport" and not page["viewport"]:
            page["viewport"] = clean_text(content)
        if name == "robots":
            generic_robots.append(str(content or ""))
        elif name in SCOPED_NOINDEX_BOTS:
            bot_robots.setdefault(name, []).append(str(content or ""))
        if meta_charset is None:
            if m.get("charset") is not None:
                meta_charset = clean_text(m.get("charset")).lower()
            elif equiv == "content-type":
                cm = _CHARSET_IN.search(str(content or ""))
                meta_charset = cm.group(1).lower() if cm else None
        if equiv == "refresh" and not page["meta_refresh"]:
            page["meta_refresh"] = clean_text(content)
            rm = _REFRESH_URL.search(page["meta_refresh"])
            if rm:
                page["meta_refresh_url"] = _abs(rm.group(1).strip(), base)
    page["directives"] = directive_tokens(page["meta_robots"] + page["x_robots_tag"])

    # A noindex aimed at Googlebot or Bingbot alone (meta named after it, or "googlebot: noindex" in the header)
    xrt = x_robots_scopes(page["x_robots_tag"])
    if not (_says_noindex(generic_robots) or _says_noindex(xrt.get("", []))):
        page["scoped_noindex"] = [b for b in SCOPED_NOINDEX_BOTS
                                  if _says_noindex(bot_robots.get(b, [])) or _says_noindex(xrt.get(b, []))]
    # Character set: the HTTP header wins over <meta> in browsers
    hm = _CHARSET_IN.search(page["content_type"] or "")
    page["charset"] = (hm.group(1).lower() if hm else "") or meta_charset or ""
    page["has_doctype"] = bool(_DOCTYPE_START.match(html[:8192]))

    # Headings
    for h in idx.tags("h1", "h2", "h3", "h4", "h5", "h6"):
        level = int(h.name[1])
        text = _el_text(h)
        page["headings"].append((level, text))
        if level == 1:
            page["h1"].append(text)
            alts = [clean_text(img.get("alt")) for img in h.find_all("img") if clean_text(img.get("alt"))]
            if alts:
                page["h1_img_alts"].append(" ".join(alts))
                if not text:
                    page["h1_alt_only"].append(" ".join(alts))
        elif level == 2:
            page["h2"].append(text)

    # <link> elements: canonical, pagination, hreflang, AMP, mobile alternate, stylesheets
    resources: List[Dict[str, Any]] = []
    seen_resources: Dict[str, Dict[str, Any]] = {}     # url -> its resources[] entry

    def add_resource(el, attr: str, raw: str, rtype: str, candidate: bool = False):
        raw = (raw or "").strip()
        if not raw:
            return
        if raw.startswith("//"):
            page["protocol_relative"].append(raw)
        absu = normalize_cached(raw, base_url=base)
        if not absu:
            return
        known = seen_resources.get(absu)
        if known is not None:
            if not candidate:
                known.pop("candidate", None)     # also an element's own file: page weight counts it
            return
        if https_page and absu.startswith("http:"):
            page["mixed_content"].append(absu)
        in_head = _in_head(el, has_head)
        resources.append({
            "url": absu, "type": rtype, "tag": el.name, "raw": raw,
            "internal": (is_internal_url(absu, base_domain, include_subdomains, allowed_domains)
                         or on_cdn(absu, cdn_hosts)),
            "region": "Page <head>" if in_head else positions.region(el)[0],
            "line": lines.line(el, _src_attr(el, attr)),
            "alt": clean_text(el.get("alt")) if el.name == "img" else "",
        })
        seen_resources[absu] = resources[-1]
        if candidate:
            resources[-1]["candidate"] = True     # another size / format of an <img>: requested, not page weight

    for lk in idx.tags("link"):
        rels = _attr_list(lk, "rel")
        href = str(lk.get("href") or "").strip()
        if not href:
            continue
        if "canonical" in rels:
            page["canonicals_raw"].append(href)
            page["canonicals"].append(_abs(href, base))
            if not _in_head(lk, has_head):
                page["canonical_outside_head"] = True
            for attr in ("hreflang", "lang", "media", "type"):
                if lk.get(attr) is not None and attr not in page["canonical_invalid_attrs"]:
                    page["canonical_invalid_attrs"].append(attr)
        if "next" in rels:
            page["rel_next_all"].append(_abs(href, base))
            page["rel_next"] = page["rel_next"] or page["rel_next_all"][0]
        if "prev" in rels:
            page["rel_prev_all"].append(_abs(href, base))
            page["rel_prev"] = page["rel_prev"] or page["rel_prev_all"][0]
        if "amphtml" in rels and not page["amphtml"]:
            page["amphtml"] = _abs(href, base)
        if "alternate" in rels:
            if lk.get("hreflang"):
                page["hreflang"].append({"lang": str(lk.get("hreflang")).strip().lower(), "url": _abs(href, base)})
                if not _in_head(lk, has_head):
                    page["hreflang_outside_head"] = True
            elif lk.get("media") and not page["mobile_alternate"]:
                page["mobile_alternate"] = _abs(href, base)
        if "stylesheet" in rels:
            add_resource(lk, "href", href, "css")

    # Resources: images (every srcset candidate and <picture> source too, P11), scripts, frames, media
    for el in idx.tags("img", "script", "iframe", "frame", "source", "video", "audio", "embed"):
        name = el.name
        if name == "img":
            src = str(el.get("src") or "").strip() or _first_srcset(str(el.get("srcset") or el.get("data-src") or ""))
            add_resource(el, "src", src, "image")
            for cand in srcset_urls(str(el.get("srcset") or "")):
                add_resource(el, "srcset", cand, "image", candidate=True)
        elif name == "script":
            add_resource(el, "src", str(el.get("src") or ""), "script")
        elif name in ("iframe", "frame"):
            add_resource(el, "src", str(el.get("src") or ""), "iframe")
        elif name == "source":
            if _picture_of(el) is not None:
                for cand in srcset_urls(str(el.get("srcset") or "")) or [str(el.get("src") or "").strip()]:
                    add_resource(el, "srcset", cand, "image", candidate=True)
            else:
                src = str(el.get("src") or "").strip() or _first_srcset(str(el.get("srcset") or ""))
                add_resource(el, "src", src, "other")
        else:
            add_resource(el, "src", str(el.get("src") or ""), "other")
            if name == "video" and el.get("poster"):
                add_resource(el, "poster", str(el.get("poster")), "image")
    page["resources"] = resources

    # Images: one entry per image file an <img> may load (P11): its src first ("from": "src"; the first srcset
    # candidate when it has no src), then its other srcset candidates ("srcset") and the srcset candidates of the
    # <source> elements of its <picture> ("picture"). "element" is the <img>'s place among the page's <img> tags, so
    # alt text, width and height are counted once per element (images_tab.primary_images) and every candidate is a
    # file of its own for size, status and format.
    for element, img in enumerate(idx.tags("img")):
        raw_src = str(img.get("src") or "").strip()
        cands = srcset_urls(str(img.get("srcset") or ""))
        if raw_src:
            wanted = [(raw_src, "src", img, "src")]
        elif cands:
            wanted = [(cands[0], "srcset", img, "srcset")]
        else:
            data_src = str(img.get("data-src") or "").strip()
            wanted = [(data_src, "src", img, "data-src")] if data_src else []
        wanted += [(c, "srcset", img, "srcset") for c in cands]
        picture = _picture_of(img)
        if picture is not None:
            for source in picture.find_all("source"):
                if _picture_of(source) is picture:
                    wanted += [(c, "picture", source, "srcset") for c in srcset_urls(str(source.get("srcset") or ""))]
        if not wanted:
            continue
        alt = img.get("alt")
        alt = None if alt is None else clean_text(alt)
        has_width = bool(str(img.get("width") or "").strip())
        has_height = bool(str(img.get("height") or "").strip())
        in_link = any(getattr(p, "name", None) == "a" for p in img.parents)
        taken: Set[str] = set()
        for raw, origin, holder_el, attr in wanted:
            src = normalize_cached(raw, base_url=base) if raw else None
            if not src or src in taken:
                continue
            taken.add(src)
            page["images"].append({
                "src": src, "alt": alt, "has_width": has_width, "has_height": has_height, "in_link": in_link,
                "line": lines.line(holder_el, attr), "from": origin, "element": element,
            })

    # Links: <a href> and <area href>. A protocol-relative href is not recorded in protocol_relative:
    # that check (Security tab) is about resources (images, scripts, CSS), not hyperlinks.
    self_url = normalize_cached(page["final_url"]) or page["final_url"]
    fragments: List[str] = []                      # same-page "#id" targets, checked against the ids below
    empty_links = 0                                # a11y: <a href> with no accessible name
    contact_hrefs: List[str] = []                  # mailto: / tel: targets (schema contact values)
    for a in idx.tags("a", "area"):
        if max_links and len(page["links"]) >= max_links:
            break
        raw = str(a.get("href") or "").strip()
        if not raw:
            continue
        bad_code, bad_reason = malformed_href(raw)
        if bad_code and len(page["malformed_hrefs"]) < MAX_MALFORMED_HREFS:
            page["malformed_hrefs"].append({"raw": raw[:200], "code": bad_code, "reason": bad_reason,
                                            "line": lines.line(a, "href")})
        absu = normalize_cached(raw, base_url=base)
        if not absu:
            # javascript:, mailto:, tel: and other non-web schemes
            if raw[:7].lower() == "mailto:" or raw[:4].lower() == "tel:":
                if len(contact_hrefs) < 50:
                    contact_hrefs.append(unquote(raw.split(":", 1)[1].split("?", 1)[0]))
            if a.name == "a" and not _has_name(a, _el_text(a)) and not _is_hidden(a):
                empty_links += 1
            continue
        hash_at = raw.find("#")
        if hash_at >= 0 and absu == self_url:
            frag = raw[hash_at + 1:]
            if frag.lower() not in _FRAGMENT_SKIP and frag[0] not in "/!":
                fragments.append(frag)
        rel = _attr_list(a, "rel")
        alt_only = False
        if a.name == "area":
            anchor = clean_text(a.get("alt"))
        else:
            anchor = _el_text(a)
            if not anchor:
                alts = [clean_text(img.get("alt")) for img in a.find_all("img") if img.get("alt")]
                if alts:
                    anchor, alt_only = " ".join(alts), True
                if not _has_name(a, anchor) and not _is_hidden(a):
                    empty_links += 1
        region, position_rule = positions.region(a)
        page["links"].append({
            "url": absu, "raw": raw, "anchor": anchor[:300], "alt_only": alt_only, "rel": rel,
            "nofollow": "nofollow" in rel,
            "target_blank": str(a.get("target") or "").strip().lower() == "_blank",
            "noopener": "noopener" in rel or "noreferrer" in rel,
            "internal": is_internal_url(absu, base_domain, include_subdomains, allowed_domains),
            "region": region, "position_rule": position_rule,
            "line": lines.line(a, "href"),
        })

    # Forms
    for form in idx.tags("form"):
        action_raw = str(form.get("action") or "").strip()
        action = _abs(action_raw, base) if action_raw else ""
        page["forms"].append({
            "action": action,
            "method": (str(form.get("method") or "get").strip().lower() or "get"),
            "insecure": https_page and action.lower().startswith("http:"),
        })

    # Structured data (and the JSON-LD values compared with the visible page below)
    schema_values: List[Tuple[str, str, str]] = []
    page["structured_data"] = _structured_data(idx, schema_values)

    # One walk over the elements: DOM size, data-nosnippet sections, ids and <a name> anchors (bookmark
    # targets, duplicate ids), ARIA id references, main landmark role, positive tabindex and inline
    # background images. One set test per attribute keeps it cheap.
    nosnippet = 0
    tabindex_positive = 0
    has_main = bool(idx.tags("main"))
    ids_seen: Dict[str, int] = {}                  # element id -> times used
    ids: Set[str] = set()                          # ids and <a name> values (same-page #fragment targets)
    id_list: List[str] = []
    refs: List[str] = []
    backgrounds: List[str] = []
    seen_bg: Set[str] = set()
    for el in idx.all:
        attrs = el.attrs
        if not attrs:
            continue
        for key in attrs:
            if key not in _WALK_ATTRS:
                continue
            value = attrs[key]
            if key == "id":
                if not value:
                    continue
                v = value if isinstance(value, str) else " ".join(value)
                n = ids_seen.get(v)
                ids_seen[v] = (n or 0) + 1
                if n is None and v not in ids:
                    ids.add(v)
                    if len(id_list) < MAX_IDS:
                        id_list.append(pool.setdefault(v, v) if pool is not None else v)
            elif key == "name":
                if el.name == "a" and value:
                    v = str(value)
                    if v not in ids:
                        ids.add(v)
                        if len(id_list) < MAX_IDS:
                            id_list.append(pool.setdefault(v, v) if pool is not None else v)
            elif key == "data-nosnippet":
                nosnippet += 1
            elif key == "role":
                if not has_main and str(value).strip().lower() == "main":
                    has_main = True
            elif key == "tabindex":
                try:
                    if int(str(value).strip()) > 0:
                        tabindex_positive += 1
                except ValueError:
                    pass
            elif key == "style":
                if "url(" in value and "ackground" in value:
                    _background_urls(value, base, backgrounds, seen_bg)
            elif value:
                refs.extend((value if isinstance(value, str) else " ".join(value)).split())
    page["ids"] = id_list
    page["dom_nodes"] = len(idx.all)
    page["data_nosnippet_count"] = nosnippet
    for style in idx.tags("style"):
        css = style.string if style.string is not None else style.get_text()
        if css and "url(" in css:
            _background_urls(css, base, backgrounds, seen_bg)
    page["background_images"] = backgrounds
    page["plugins"] = _plugins(idx, base) if (idx.tags("object") or idx.tags("embed") or idx.tags("applet")) else []
    dead: List[str] = []
    for frag in fragments:
        if frag in ids or frag in dead:
            continue
        try:
            if unquote(frag) in ids:
                continue
        except Exception:
            pass
        dead.append(frag)
        if len(dead) >= MAX_DEAD_FRAGMENTS:
            break
    page["dead_fragments"] = dead

    # Content
    body = next(iter(idx.tags("body")), None) or soup
    area = content_area(body, options.content_include, options.content_exclude)   # P13; None = the default area
    visible: List[str] = []
    blocks: List[str] = []
    for chunk, in_content in _visible_strings(body, area):
        visible.append(chunk)
        if in_content:
            block = clean_text(chunk)
            if block:
                blocks.append(block)
    text = clean_text(" ".join(visible))          # everything a visitor can read (text ratio)
    content = " ".join(blocks)                     # the content area (nav and footer left out)
    text_lines, list_items = area_lines(body, area)   # same area, words split the way a reader sees them
    page.update(text_stats(text_lines, extra_words=list_items))
    page["text_ratio"] = round(len(text) / len(html) * 100, 2) if html else 0.0
    page["content_hash"] = hashlib.md5(content.lower().encode("utf-8")).hexdigest() if content else ""
    tokens = word_tokens(content)                  # one split serves the simhash, minhash and top terms
    page["simhash"] = simhash_tokens(tokens)
    page["minhash"] = minhash_signature(tokens)    # near duplicates (analysis.near_duplicate_index)
    page["lorem_ipsum"] = "lorem ipsum" in text.lower()
    page["soft_404_signals"] = _soft_404(page["titles"], page["h1"], content, options.soft_404_phrases)
    status = page["status_code"]
    if options.top_terms and tokens and (status is None or 200 <= status < 300):
        page["top_terms"] = _top_terms(tokens, pool)
    if options.spelling is not None and status is not None and 200 <= status < 300:
        page["spelling"] = spelling_check("\n".join(text_lines), page["language"], options.spelling)

    # JSON-LD values the visible page does not show (title and og:site_name count as visible)
    if schema_values:
        extra = " ".join(page["titles"][:1] + [og.get("site_name") or ""])
        (page["schema_visible_mismatch"], page["schema_visible_unverified"],
         page["schema_values_checked"]) = schema_vs_page(schema_values, text, extra, contact_hrefs)

    # Static accessibility checks, custom searches and extractors
    page["a11y"] = _accessibility(idx, page, ids_seen, refs, has_main, tabindex_positive, empty_links, html_lang)
    page["resources_count"] = sum(1 for res in resources if not res.get("candidate"))
    if options.searches or options.extractors:
        run_custom_rules(page, html, text, soup, options)

    # Validation
    page["head_count"], page["body_count"], page["invalid_head_elements"] = _head_checks(lines, html)
    page["head_not_first"], page["body_before_html"] = _document_order(lines)
    return page
