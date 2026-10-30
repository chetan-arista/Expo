import html
import re
from functools import lru_cache
from typing import Set, Tuple, Dict, List, Optional, Any
from bs4 import BeautifulSoup
from bs4.element import Tag
from .url_utils import (UrlRules, active_rules, normalize_url, is_internal_url, is_excluded_path, get_domain,
                        join_url)

try:  # lxml is several times faster and builds the same tree a browser does (misplaced <head> tags move to <body>)
    import lxml  # noqa: F401
    HTML_PARSER = "lxml"
except ImportError:  # pragma: no cover - fallback when lxml is not installed
    HTML_PARSER = "html.parser"

# Attributes whose values identify an element in the raw source (for line numbers)
LINE_ATTRS = ("href", "src", "action", "data-href", "data-url", "data-link", "data-target",
              "srcset", "data-src", "poster")
# Possessive quantifiers: runs of plain attribute characters are taken in one step (same matches, the old
# one-character-at-a-time form cost ~25% more on big pages)
_SOURCE_TAG = re.compile(
    r"<!--.*?(?:-->|\Z)|<(/?[a-zA-Z][a-zA-Z0-9:-]*)((?:[^>\"']++|\"[^\"]*+\"|'[^']*+')*+)>", re.S)
_LINE_ATTR_HINT = re.compile(r"href|src|action|data-")   # a tag whose attributes may hold one of LINE_ATTRS
_SOURCE_ATTR = re.compile(r"""([^\s"'>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?""")
_RAW_TEXT_END = {t: re.compile(rf"</{t}\s*>", re.I) for t in ("script", "style", "textarea", "title", "xmp")}


class SourceLines:
    """One regex pass over the raw HTML: every opening tag with its line number.
    lxml does not report source lines, so elements are matched back to the source by
    (tag, attribute, value) in document order. Also answers head/body questions that
    need the source as written (the parser repairs it)."""

    def __init__(self, html_text: str):
        self.tags: List[Tuple[str, int, Dict[str, str]]] = []   # (tag or "/tag", line, {attr: value} for LINE_ATTRS)
        self._index: Dict[tuple, List[int]] = {}
        self._used: Dict[tuple, int] = {}
        self._memo: Dict[int, int] = {}
        pos, line, n = 0, 1, len(html_text or "")
        text = html_text or ""
        search, count, unescape = _SOURCE_TAG.search, text.count, html.unescape   # locals: ~1,000 tags a page
        index = self._index
        while pos < n:
            m = search(text, pos)
            if not m:
                break
            start, end = m.span()
            line += count("\n", pos, start)
            tag_line = line
            line += count("\n", start, end)
            pos = end
            tag = m.group(1)
            if tag:
                tag = tag.lower()
                attrs: Dict[str, str] = {}
                raw_attrs = m.group(2) or ""
                if raw_attrs and _LINE_ATTR_HINT.search(raw_attrs):
                    for am in _SOURCE_ATTR.finditer(raw_attrs):
                        name = am.group(1).lower()
                        if name in LINE_ATTRS and name not in attrs:
                            val = am.group(2) if am.group(2) is not None else (
                                am.group(3) if am.group(3) is not None else (am.group(4) or ""))
                            val = attrs[name] = unescape(val)
                            index.setdefault((tag, name, val), []).append(tag_line)
                self.tags.append((tag, tag_line, attrs))
                end_re = _RAW_TEXT_END.get(tag)
                if end_re and not raw_attrs.rstrip().endswith("/"):
                    em = end_re.search(text, pos)
                    stop = em.end() if em else n
                    line += count("\n", pos, stop)
                    pos = stop

    def line(self, el, attr: str = "") -> int:
        """Source line of a parsed element (0 when it cannot be matched)."""
        key = id(el)
        if key in self._memo:
            return self._memo[key]
        found = 0
        attrs = [attr] if attr else [a for a in LINE_ATTRS if el.get(a) is not None]
        for a in attrs:
            val = el.get(a)
            if not isinstance(val, str):
                continue
            k = (el.name, a, val)
            lines = self._index.get(k)
            if lines:
                i = self._used.get(k, 0)
                found = lines[min(i, len(lines) - 1)]
                self._used[k] = i + 1
                break
        self._memo[key] = found
        return found


def make_soup(html_text: str) -> BeautifulSoup:
    """Parse once per page; the soup carries a SourceLines index when the parser has no line numbers."""
    soup = BeautifulSoup(html_text or "", HTML_PARSER)
    if HTML_PARSER != "html.parser":
        soup._crawler_source_lines = SourceLines(html_text or "")
    return soup


def release_soup(soup) -> None:
    """Breaks up a parsed page once nothing reads it any more. A bs4 tree is one big reference cycle (parents,
    children, next/previous elements), so without this every page waits for Python's full garbage collection,
    which grows with the crawl (measured: 14% of a 3,000-page crawl's time). decompose() on the root object does
    not reach the elements, so each top-level child is decomposed. The tree must not be used afterwards."""
    if soup is None:
        return
    try:
        for child in list(soup.contents):
            child.decompose()
        builder = soup.__dict__.get("builder")
        soup.__dict__.clear()
        if builder is not None:
            builder.__dict__.clear()   # the builder and its lxml parser point back at the soup
    except Exception:
        pass   # the garbage collector still frees it


def looks_like_url(value: str) -> bool:
    """A data-* attribute value that is a URL or path. Values such as data-target="#menu" or
    data-target="logo_attachment" name page elements and must not become links."""
    v = (value or "").strip().lower()
    return v.startswith(("http://", "https://", "//", "/", "./", "../")) or "/" in v


# Attributes looked up across the whole page (link discovery and structured data)
INDEXED_ATTRS = ("data-href", "data-url", "data-link", "data-target", "itemtype", "typeof")


class TagIndex:
    """Every element of a parsed page by tag name and by a few attributes, built in one walk.
    bs4's find_all walks the whole tree each call; extraction and discovery need ~20 such lookups."""

    def __init__(self, soup):
        self.all: List[Tag] = []
        self.by_name: Dict[str, List[Tag]] = {}
        self.by_attr: Dict[str, List[Tag]] = {a: [] for a in INDEXED_ATTRS}
        for el in soup.descendants:
            if not isinstance(el, Tag):
                continue
            self.all.append(el)
            self.by_name.setdefault(el.name, []).append(el)
            if el.attrs:
                for a in INDEXED_ATTRS:
                    if a in el.attrs:
                        self.by_attr[a].append(el)

    def tags(self, *names: str) -> List[Tag]:
        """Elements with any of these names, in document order."""
        if len(names) == 1:
            return self.by_name.get(names[0], [])
        wanted = set(names)
        return [el for el in self.all if el.name in wanted]

    def with_attr(self, attr: str) -> List[Tag]:
        return self.by_attr.get(attr, [])


def tag_index(soup) -> TagIndex:
    """The page's TagIndex, built on first use and kept on the soup."""
    idx = soup.__dict__.get("_crawler_tag_index")
    if idx is None:
        idx = TagIndex(soup)
        soup._crawler_tag_index = idx
    return idx


# Pages repeat the same hrefs (menus, footers); normalizing is pure, so cache it across pages. The URL
# Rewriting rules in force (url_utils.active_rules) are part of the cache key.
_normalize_lru = lru_cache(maxsize=50000)(normalize_url)


_HAS_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def normalize_cached(raw_url: str, base_url: str = "", rules: Optional[UrlRules] = None) -> Optional[str]:
    """normalize_url, cached. A relative href is joined to the page URL first, so the same menu link on every
    page shares one cache entry (normalize_url(join_url(base, href)) is exactly normalize_url(href, base) for an
    href without a scheme; test_normalize_equiv.py checks it)."""
    rules = rules if rules is not None else active_rules()
    if base_url and raw_url:
        href = raw_url.strip()
        if href and not _HAS_SCHEME.match(href):
            try:
                return _normalize_lru(join_url(base_url, href), "", rules)
            except Exception:
                return None
    return _normalize_lru(raw_url, base_url, rules)


def source_line(el, attr: str = "") -> int:
    """Line of an element in the page's HTML source (html.parser knows it; lxml needs the index)."""
    line = getattr(el, "sourceline", None)
    if line:
        return line
    root = el
    while getattr(root, "parent", None) is not None:
        root = root.parent
    lines = root.__dict__.get("_crawler_source_lines")
    return lines.line(el, attr) if lines else 0

JS_PATH_PATTERN = re.compile(
    r"""(?:href|url|path|location|window\.location)["']?\s*[:=]\s*["'](/[^"'\s<>#\\]+)["']""",
    re.IGNORECASE
)

# Page regions used to describe where a link sits ("Footer", "Navigation menu", ...)
REGION_TAGS = {
    "header": "Header", "nav": "Navigation menu", "footer": "Footer",
    "aside": "Sidebar", "main": "Main content", "form": "Form",
}
REGION_ROLES = {
    "banner": "Header", "navigation": "Navigation menu", "contentinfo": "Footer",
    "complementary": "Sidebar", "main": "Main content",
}
REGION_HINTS = (
    ("breadcrumb", "Breadcrumb"), ("footer", "Footer"), ("header", "Header"), ("masthead", "Header"),
    ("sidebar", "Sidebar"), ("menu", "Navigation menu"), ("nav", "Navigation menu"),
)
_SIMPLE_ID = re.compile(r"^[A-Za-z][\w-]*$")


def _region(el) -> str:
    """Closest semantic page region containing the element."""
    ancestors = [p for p in el.parents if p.name and p.name not in ("html", "[document]")]
    for p in ancestors:
        if p.name in REGION_TAGS:
            return REGION_TAGS[p.name]
        role = str(p.get("role") or "").lower()
        if role in REGION_ROLES:
            return REGION_ROLES[role]
    for p in ancestors:
        tokens = set(re.split(r"[\s_-]+", (" ".join(p.get("class") or []) + " " + str(p.get("id") or "")).lower()))
        for key, label in REGION_HINTS:
            if key in tokens:
                return label
    return "Page body"


def _nth_of_type(node) -> Tuple[int, int]:
    """(1-based position among same-name siblings, number of such siblings), cached on the parent."""
    parent = node.parent
    if parent is None:
        return 1, 1
    cache = parent.__dict__.get("_crawler_nth")
    if cache is None:
        cache, counts = {}, {}
        for child in parent.children:
            name = getattr(child, "name", None)
            if name:
                counts[name] = counts.get(name, 0) + 1
                cache[id(child)] = (counts[name], name)
        cache = {k: (pos, counts[name]) for k, (pos, name) in cache.items()}
        parent._crawler_nth = cache
    return cache.get(id(node), (1, 1))


def _css_path(el, max_depth: int = 12) -> str:
    """Short CSS selector for the element, anchored at the nearest id or body."""
    parts = []
    node = el
    while node is not None and node.name and node.name not in ("[document]", "html"):
        node_id = str(node.get("id") or "")
        if node_id and _SIMPLE_ID.match(node_id):
            parts.append(f"{node.name}#{node_id}")
            break
        if node.name == "body":
            parts.append("body")
            break
        idx, same_name = _nth_of_type(node)
        if same_name > 1:
            parts.append(f"{node.name}:nth-of-type({idx})")
        else:
            parts.append(node.name)
        if len(parts) >= max_depth:
            break
        node = node.parent
    return " > ".join(reversed(parts))


def _element_snippet(el, attr: str, text: str) -> str:
    """Compact one-line HTML for the element: <a class=".." href="..">text</a>."""
    attrs = []
    for key in ("id", "class", "rel", attr):
        val = el.get(key)
        if val:
            val = " ".join(val) if isinstance(val, list) else str(val)
            attrs.append(f'{key}="{val[:80]}"')
    if el.name in ("link", "area", "img", "input"):
        return f"<{el.name} {' '.join(attrs)}>"[:240]
    inner = text[:60] if text else ""
    return f"<{el.name} {' '.join(attrs)}>{inner}</{el.name}>"[:240]


def element_meta(el, attr: str, tag_label: str, text: str = "") -> Dict[str, str]:
    """Describes the element that links to a URL: text, region, selector and HTML snippet."""
    return {
        "anchor_text": text,
        "tag": tag_label,
        "region": _region(el),
        "selector": _css_path(el),
        "html": _element_snippet(el, attr, text),
        "raw_href": str(el.get(attr) or "").strip(),
        "line": source_line(el, attr),  # line in the page's HTML source
    }


def _link_text(a) -> str:
    text = (a.get_text() or "").strip()
    if not text:
        text = a.get("aria-label", "") or a.get("title", "")
        if not text:
            img = a.find("img")
            if img is not None:
                text = img.get("alt", "") or "[image]"
    return re.sub(r"\s+", " ", str(text)).strip()[:100]

class PageParser:
    @staticmethod
    def extract_title(html_content: str, soup: Optional[BeautifulSoup] = None) -> str:
        """Extract clean page title with robust fallbacks (`soup`: the page already parsed)."""
        if not html_content and soup is None:
            return ""

        try:
            if soup is None:
                soup = BeautifulSoup(html_content, "html.parser")

            # 1. Standard <title>
            head_title = soup.head.find("title") if soup.head else None
            head_title = head_title or soup.find("title")
            if head_title and head_title.string:
                title = head_title.string.strip()
                if title:
                    return html.unescape(re.sub(r"\s+", " ", title))

            # 2. OpenGraph / Twitter meta tags
            meta_og = soup.find("meta", property="og:title") or soup.find("meta", attrs={"name": "og:title"})
            if meta_og and meta_og.get("content"):
                return html.unescape(meta_og["content"].strip())

            meta_tw = soup.find("meta", attrs={"name": "twitter:title"})
            if meta_tw and meta_tw.get("content"):
                return html.unescape(meta_tw["content"].strip())

            # 3. First <h1>
            h1 = soup.find("h1")
            if h1:
                h1_text = h1.get_text().strip()
                if h1_text:
                    return html.unescape(re.sub(r"\s+", " ", h1_text))

        except Exception:
            pass

        return ""

    @staticmethod
    def extract_links(
        html_content: str,
        current_url: str,
        base_domain: str,
        include_subdomains: bool = False,
        allowed_domains: Optional[Set[str]] = None,
        exclude_patterns: Optional[list] = None,
        collect_external: bool = True,
        known_urls: Optional[Set[str]] = None,
        soup: Optional[BeautifulSoup] = None,
        max_links: int = 0
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
        """
        Extract both internal and external links from HTML content.
        - Internal links: {url: element_meta} describing the first element on this page that
          links to the URL (link text, page region, CSS selector, HTML snippet).
          Metadata is skipped (empty dict) for URLs in `known_urls`, which are already queued.
        - External links: {url: {url, domain, parent_url, anchor_text, discovery_source}}.
          They are recorded only and will NOT be crawled.
        `soup` reuses a parse made for the SEO extraction instead of parsing again.
        `max_links` > 0 reads only that many <a> elements (huge pages).
        """
        internal_links: Dict[str, Dict[str, str]] = {}
        external_links: Dict[str, Dict[str, Any]] = {}

        if not html_content and soup is None:
            return internal_links, external_links

        exclude_patterns = exclude_patterns or []
        known_urls = known_urls if known_urls is not None else set()

        def add_internal(norm: str, make_meta) -> None:
            if norm in internal_links or is_excluded_path(norm, exclude_patterns):
                return
            internal_links[norm] = {} if norm in known_urls else make_meta()

        try:
            if soup is None:
                soup = BeautifulSoup(html_content, "html.parser")

            idx = tag_index(soup)

            # 1. Standard anchor tags <a> (with anchor text / label)
            for n_a, a in enumerate(idx.tags("a")):
                if max_links and n_a >= max_links:
                    break
                if a.get("href") is None:
                    continue
                norm = normalize_cached(a["href"], base_url=current_url)
                if not norm:
                    continue

                if is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                    add_internal(norm, lambda: element_meta(a, "href", "a", _link_text(a)))
                elif collect_external and norm not in external_links:
                    external_links[norm] = {
                        "url": norm,
                        "domain": get_domain(norm),
                        "parent_url": current_url,
                        "anchor_text": _link_text(a) or "-",
                        "discovery_source": "Page Link"
                    }

            # 2. Link tags (canonical, alternate) - primarily internal
            for lk in idx.tags("link"):
                if lk.get("href") is None:
                    continue
                rel = lk.get("rel", [])
                if isinstance(rel, str):
                    rel = [rel]
                # Skip non-HTML links (e.g. JSON API, RSS feeds, oEmbed, RSD)
                lk_type = str(lk.get("type") or "").lower()
                if lk_type and any(bad in lk_type for bad in ["json", "xml", "rss", "atom", "oembed"]):
                    continue
                if any(r in ("canonical", "alternate", "next", "prev") for r in rel):
                    norm = normalize_cached(lk["href"], base_url=current_url)
                    if norm and is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                        add_internal(norm, lambda: {
                            **element_meta(lk, "href", "link", f"{' '.join(rel)} link"),
                            "region": "Page <head> metadata",
                        })

            # 3. Elements with data-href or data-url attributes
            for attr in ("data-href", "data-url", "data-link", "data-target"):
                for el in idx.with_attr(attr):
                    if not looks_like_url(el[attr]):
                        continue
                    norm = normalize_cached(el[attr], base_url=current_url)
                    if not norm:
                        continue
                    if is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                        add_internal(norm, lambda: element_meta(el, attr, attr, _link_text(el)))
                    elif collect_external:
                        if norm not in external_links:
                            external_links[norm] = {
                                "url": norm,
                                "domain": get_domain(norm),
                                "parent_url": current_url,
                                "anchor_text": f"data-{attr}",
                                "discovery_source": "Data Attribute"
                            }

            # 4. Form actions
            for form in idx.tags("form"):
                if form.get("action") is None:
                    continue
                norm = normalize_cached(form["action"], base_url=current_url)
                if norm and is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                    add_internal(norm, lambda: element_meta(form, "action", "form", "[form submit]"))

            # 5. Image maps <area>
            for area in idx.tags("area"):
                if area.get("href") is None:
                    continue
                norm = normalize_cached(area["href"], base_url=current_url)
                if not norm:
                    continue
                if is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                    add_internal(norm, lambda: element_meta(area, "href", "area", area.get("alt", "") or "[image map]"))
                elif collect_external:
                    if norm not in external_links:
                        external_links[norm] = {
                            "url": norm,
                            "domain": get_domain(norm),
                            "parent_url": current_url,
                            "anchor_text": area.get("alt", "") or "Image Map Area",
                            "discovery_source": "Area Map"
                        }

            # 6. Inline script router links / path patterns (incl. Next.js RSC payloads, where
            #    props are escaped JSON inside self.__next_f.push(...): \"href\":\"/projects\")
            for script in idx.tags("script"):
                if script.string:
                    code = script.string.replace('\\"', '"')
                    for match in JS_PATH_PATTERN.findall(code):
                        if match.startswith(("/_next/", "/api/")):
                            continue
                        norm = normalize_cached(match, base_url=current_url)
                        if norm and is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                            add_internal(norm, lambda: {
                                "anchor_text": "", "tag": "script", "region": "Inline script",
                                "selector": "", "html": f'"{match}"', "raw_href": match,
                            })

        except Exception:
            pass

        return internal_links, external_links

    @staticmethod
    def extract_internal_links(
        html_content: str,
        current_url: str,
        base_domain: str,
        include_subdomains: bool = False,
        allowed_domains: Optional[Set[str]] = None,
        exclude_patterns: Optional[list] = None
    ) -> Set[str]:
        """Backward-compatible wrapper for extracting internal links only."""
        internal, _ = PageParser.extract_links(
            html_content=html_content,
            current_url=current_url,
            base_domain=base_domain,
            include_subdomains=include_subdomains,
            allowed_domains=allowed_domains,
            exclude_patterns=exclude_patterns,
            collect_external=False
        )
        return set(internal)
