"""
Validation tab (Screaming Frog "Validation"): basic HTML structure checks that decide whether search
engines read a page's <head> the way it was intended, plus file-size limits.

Rows ("All"): every internal URL the crawl requested (pages and files, as in the Internal tab: 441
rows in the reference crawl). The structure checks run on HTML pages that loaded (2xx) and were
parsed; the size checks use the decoded body size (uncompressed), which is what Googlebot's 2MB
per-file limit applies to.

Valid <head> elements (HTML standard, as Screaming Frog uses): title, meta, link, script, style,
base, noscript, template; anything else ends the <head> early for Google.

High Carbon Rating (P18): grams of CO2e per page view and a rating A+ to F from the Sustainable Web
Design model v4 (crawler/seo/carbon.py); flagged at or worse than Preferences carbon_rating_flag (default E).
Not checked when resource crawling or the carbon rating was off. "<head> Not First In <html> Element" and "<body> Element Preceding <html>" read PageData "head_not_first" /
"body_before_html" and stay empty until the extractor fills them.
"""
from typing import Any

from .. import carbon
from ..registry import Column, Filter, Tab, pref, pref_number, register, seo, threshold_filter
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE,
                       category_of, content_kind, header, internal_rows, is_html_doc, size_bytes)

MAX_FILE_BYTES = 2 * 1024 * 1024          # Googlebot reads the first 2MB of an HTML/JS/CSS file


def _page(r) -> bool:
    """HTML page that loaded and was parsed (structure checks apply)."""
    return is_html_doc(r) and category_of(r) == "2xx" and bool(seo(r).get("is_html"))


def _size(r) -> int:
    v = size_bytes(r)
    return v if isinstance(v, int) else 0


def transferred_bytes(r) -> Any:
    p = seo(r)
    if p.get("transferred_bytes"):
        return p["transferred_bytes"]
    cl = header(r, "content-length")
    if cl.strip().isdigit():
        return int(cl)
    return size_bytes(r)


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------
def _invalid_head(r, site) -> bool:
    return _page(r) and seo(r).get("head_count", 0) > 0 and bool(seo(r).get("invalid_head_elements"))


def _body_before_html(r, site) -> bool:
    return _page(r) and bool(seo(r).get("body_before_html"))


def _head_not_first(r, site) -> bool:
    return _page(r) and bool(seo(r).get("head_not_first"))


def _count(key: str, test):
    return lambda r, site: _page(r) and test(int(seo(r).get(key) or 0))


def size_limit(site, key: str) -> float:
    """A file size limit of this crawl in bytes (Preferences html_max_mb / resource_max_mb, default 2MB)."""
    try:
        return float(pref_number(site, key)) * 1024 * 1024
    except (TypeError, ValueError):
        return float(MAX_FILE_BYTES)


def _html_over(r, site) -> bool:
    return is_html_doc(r) and _size(r) > size_limit(site, "html_max_mb")


def _resource_over(r, site) -> bool:
    return content_kind(r) in ("javascript", "css") and _size(r) > size_limit(site, "resource_max_mb")


def _carbon_cell(key: str):
    def get(r, site):
        c = carbon.cached(site, r)
        if c is None or (site is not None and carbon.why_not_checked(site)):
            return ""
        return c[key]
    return get


def _high_carbon(r, site) -> bool:
    c = carbon.cached(site, r)
    return c is not None and carbon.at_or_worse(c["rating"], str(pref(site, "carbon_rating_flag") or "E"))


# ---------------------------------------------------------------------------------------------
# Tab
# ---------------------------------------------------------------------------------------------
COLUMNS = [
    ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS, INDEXABILITY, INDEXABILITY_STATUS,
    Column("size", "Size (Bytes)", lambda r, site: size_bytes(r), "num"),
    Column("transferred", "Transferred (Bytes)", lambda r, site: transferred_bytes(r), "num"),
    Column("head_tags", "<head> Tags", lambda r, site: seo(r).get("head_count", 0) if _page(r) else "", "num"),
    Column("body_tags", "<body> Tags", lambda r, site: seo(r).get("body_count", 0) if _page(r) else "", "num"),
    Column("invalid_head_elements", "Invalid <head> Elements",
           lambda r, site: list(seo(r).get("invalid_head_elements") or []) if _page(r) else [], "list"),
    Column("co2_per_view", "CO2 Per View (g)", _carbon_cell("grams"), "num"),
    Column("carbon_rating", "Carbon Rating", _carbon_cell("rating")),
]

FILTERS = [
    Filter("invalid_head_elements", "Invalid HTML Elements In <head>", _invalid_head, "Warning", "High",
           "Pages with elements inside <head> that do not belong there (for example <img>, <div>, <iframe> "
           "or <svg>). Google treats the first such element as the end of the <head> and ignores what comes "
           "after it, so a title, canonical, hreflang or robots tag placed below it can be missed.",
           "Keep only title, meta, link, script, style, base, noscript and template elements in <head>. Move "
           "anything else into <body>, or at least below the important tags. Check what plugins or tag "
           "managers inject into the <head>."),
    Filter("body_before_html", "<body> Element Preceding <html>", _body_before_html, "Warning", "High",
           "Pages where a <body> element appears before the opening <html> tag. Browsers and search engines "
           "then start the body early and treat the real <head> (and its meta tags) as body content.",
           "Fix the page template so the document starts with <!DOCTYPE html>, then <html>, <head> and "
           "<body> in that order."),
    Filter("head_not_first", "<head> Not First In <html> Element", _head_not_first, "Warning", "High",
           "Pages where another element comes before <head> inside <html>. Parsers then create an empty "
           "<head> of their own, and the real one, with its title and meta tags, may be ignored.",
           "Make <head> the first element inside <html> and move anything placed before it into <body>."),
    Filter("missing_head", "Missing <head> Tag", _count("head_count", lambda n: n == 0), "Issue", "High",
           "Pages with no <head> element. Without one, search engines have to guess where page metadata "
           "(title, meta description, canonical, robots) begins and ends.",
           "Add a <head> element containing the page's metadata at the top of the document."),
    Filter("multiple_head", "Multiple <head> Tags", _count("head_count", lambda n: n > 1), "Issue", "High",
           "Pages with more than one <head> element. Only the first is treated as the head; tags in the "
           "others can be ignored or end the head early.",
           "Merge everything into a single <head>. This is often caused by templates or plugins that "
           "output a second full HTML document."),
    Filter("missing_body", "Missing <body> Tag", _count("body_count", lambda n: n == 0), "Issue", "High",
           "Pages with no <body> element. Search engines have to guess where the content starts, which can "
           "make them treat head tags as content or content as head tags.",
           "Wrap the visible content of the page in a single <body> element."),
    Filter("multiple_body", "Multiple <body> Tags", _count("body_count", lambda n: n > 1), "Issue", "High",
           "Pages with more than one <body> element, a sign of broken templates that can confuse how "
           "the page is read.",
           "Keep a single <body> element per page."),
    threshold_filter("html_over_2mb", "html_max_mb", "HTML Document Over {n}MB", _html_over,
                     issue_type="Issue", priority="High",
                     description="HTML pages larger than {n}MB (uncompressed). Googlebot only reads the first 2MB of "
                                 "a page, so content and links beyond that point are not indexed.",
                     fix="Reduce the page size: move inline scripts, styles and embedded images (data URIs) into "
                         "separate files, trim repeated markup and split very long pages."),
    threshold_filter("resource_over_2mb", "resource_max_mb", "Resource Over {n}MB", _resource_over,
                     issue_type="Issue", priority="High",
                     description="JavaScript or CSS files larger than {n}MB (uncompressed). Googlebot only reads the "
                                 "first 2MB of a file when rendering, so the rest of the file is ignored and the page "
                                 "may render incompletely.",
                     fix="Split large bundles into smaller files, remove unused code and minify what remains."),
    Filter("high_carbon_rating", "High Carbon Rating", _high_carbon, "Opportunity", "Low",
           "Pages whose page view is estimated to emit a lot of CO2 (Sustainable Web Design model v4: the bytes of "
           "the page and the files it loads, green hosting when the Green Web Foundation lists the host). The "
           "rating runs from A+ to F; pages at the rating chosen under Preferences (default E) or worse are listed. "
           "Files of unknown size are left out, so the real figure can only be higher.",
           "Make the page lighter: compress and resize images (WebP / AVIF), remove unused scripts and styles, "
           "lazy-load what is below the fold and move to a green host.",
           requires=("config:carbon", "config:crawl_resources"),
           short_fix="Cut page weight: smaller images, fewer scripts, green host"),
]

register(Tab(id="validation", name="Validation", order=225, columns=COLUMNS, filters=FILTERS, rows=internal_rows))
