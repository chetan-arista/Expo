import html
import re
from typing import Set, Tuple, Dict, Optional, Any
from bs4 import BeautifulSoup
from .url_utils import normalize_url, is_internal_url, is_excluded_path, get_domain

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
        idx = 1 + len(node.find_previous_siblings(node.name))
        if idx > 1 or node.find_next_sibling(node.name) is not None:
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
    def extract_title(html_content: str) -> str:
        """Extract clean page title with robust fallbacks."""
        if not html_content:
            return ""

        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # 1. Standard <title>
            if soup.title and soup.title.string:
                title = soup.title.string.strip()
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
        known_urls: Optional[Set[str]] = None
    ) -> Tuple[Dict[str, Dict[str, str]], Dict[str, Dict[str, Any]]]:
        """
        Extract both internal and external links from HTML content.
        - Internal links: {url: element_meta} describing the first element on this page that
          links to the URL (link text, page region, CSS selector, HTML snippet).
          Metadata is skipped (empty dict) for URLs in `known_urls`, which are already queued.
        - External links: {url: {url, domain, parent_url, anchor_text, discovery_source}}.
          They are recorded only and will NOT be crawled.
        """
        internal_links: Dict[str, Dict[str, str]] = {}
        external_links: Dict[str, Dict[str, Any]] = {}

        if not html_content:
            return internal_links, external_links

        exclude_patterns = exclude_patterns or []
        known_urls = known_urls if known_urls is not None else set()

        def add_internal(norm: str, make_meta) -> None:
            if norm in internal_links or is_excluded_path(norm, exclude_patterns):
                return
            internal_links[norm] = {} if norm in known_urls else make_meta()

        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # 1. Standard anchor tags <a> (with anchor text / label)
            for a in soup.find_all("a", href=True):
                norm = normalize_url(a["href"], base_url=current_url)
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
            for lk in soup.find_all("link", href=True):
                rel = lk.get("rel", [])
                if isinstance(rel, str):
                    rel = [rel]
                # Skip non-HTML links (e.g. JSON API, RSS feeds, oEmbed, RSD)
                lk_type = str(lk.get("type") or "").lower()
                if lk_type and any(bad in lk_type for bad in ["json", "xml", "rss", "atom", "oembed"]):
                    continue
                if any(r in ("canonical", "alternate", "next", "prev") for r in rel):
                    norm = normalize_url(lk["href"], base_url=current_url)
                    if norm and is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                        add_internal(norm, lambda: {
                            **element_meta(lk, "href", "link", f"{' '.join(rel)} link"),
                            "region": "Page <head> metadata",
                        })

            # 3. Elements with data-href or data-url attributes
            for attr in ("data-href", "data-url", "data-link", "data-target"):
                for el in soup.find_all(attrs={attr: True}):
                    norm = normalize_url(el[attr], base_url=current_url)
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
            for form in soup.find_all("form", action=True):
                norm = normalize_url(form["action"], base_url=current_url)
                if norm and is_internal_url(norm, base_domain, include_subdomains, allowed_domains):
                    add_internal(norm, lambda: element_meta(form, "action", "form", "[form submit]"))

            # 5. Image maps <area>
            for area in soup.find_all("area", href=True):
                norm = normalize_url(area["href"], base_url=current_url)
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
            for script in soup.find_all("script"):
                if script.string:
                    code = script.string.replace('\\"', '"')
                    for match in JS_PATH_PATTERN.findall(code):
                        if match.startswith(("/_next/", "/api/")):
                            continue
                        norm = normalize_url(match, base_url=current_url)
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
