"""
CONTRACT for the SEO audit layer (Screaming Frog parity). Every module in crawler/seo codes against
the names in this file; change a name here and you must change every user of it.

Data flow
---------
1. During the crawl, for every fetched URL the engine calls
       extract.extract_page(html, final_url, response, ctx) -> PageData dict   (HTML 2xx/3xx/4xx/5xx with a body)
       extract.extract_headers_only(response) -> PageData dict               (files, HEAD responses, no body)
   and stores it on CrawlResult.seo. It also stores CrawlResult.response_headers (lowercased dict).
2. After the crawl, analysis.build_site(results, sitemap_index, config) -> Site (below).
3. registry.run_all(site) evaluates every Tab's Filters and returns a TabIndex.
4. web/app.py serves TabIndex through /api/crawl/tabs, /api/crawl/tab/{id}, /api/crawl/url,
   /api/crawl/issues-overview; exporter writes the same rows to Excel.

PageData (a plain dict; keys always present, empty values when not applicable)
---------------------------------------------------------------------------
Identity / HTTP
  url: str                      normalized URL as crawled (same as CrawlResult.url)
  final_url: str                after redirects
  status_code: int|None
  content_type: str             lowercased, e.g. "text/html; charset=utf-8"
  is_html: bool                 content_type contains "html" and a body was parsed
  http_version: str             "HTTP/1.1" / "HTTP/2" ("" if unknown)
  size_bytes: int               length of the (decoded) response body
  transferred_bytes: int        Content-Length header if present else size_bytes
  response_time_ms: int
  last_modified: str            Last-Modified header or ""
  language: str                 <html lang> value or ""
  # response headers stay on CrawlResult.response_headers (dict, lowercase keys)

Titles / meta (lists keep document order; text is whitespace-collapsed and HTML-unescaped)
  titles: list[str]             every <title> (head and body)
  title_outside_head: bool      a <title> appears outside <head>
  meta_descriptions: list[str]  every <meta name="description" content>
  meta_description_outside_head: bool
  meta_keywords: list[str]
  # pixel widths are computed by text_metrics.pixel_width(text, kind) -> int (kind "title"|"description")

Headings
  h1: list[str]                 text of every <h1>, in order ("" for an empty one)
  h2: list[str]
  h1_alt_only: list[str]        alt text of <img> inside an <h1> that has no other text
  h1_img_alts: list[str]        alt text of the <img>s inside each <h1> that has any, text or not
  headings: list[tuple[int, str]]   every h1..h6 as (level, text) in document order (for non-sequential checks)

Content
  word_count: int               words in visible body text (scripts/styles/nav excluded? NO: all visible body text,
                                matching Screaming Frog's default "content area" = whole <body>)
  sentence_count: int
  avg_words_per_sentence: float
  flesch_reading_ease: float|None
  readability: str              "Very Easy" | "Easy" | "Fairly Easy" | "Normal" | "Fairly Hard" | "Hard" | "Very Hard" | ""
  text_ratio: float             visible text chars / html chars * 100
  content_hash: str             md5 of normalized visible text (exact duplicate detection)
  simhash: int                  64-bit simhash of word 3-shingles (legacy near duplicates); 0 if no text
  minhash: str                  minhash signature of the content area's word 3-shingles (near duplicates:
                                analysis.minhash_signature, 128 bins x 8 hex digits); "" if no text
  lorem_ipsum: bool             visible text contains "lorem ipsum"
  soft_404_signals: list[str]   phrases like "page not found", "404" in title/h1/body found on a 200 page

Canonicals / pagination / hreflang / AMP
  canonicals: list[str]         absolute normalized URLs from <link rel="canonical">
  canonicals_raw: list[str]     href values as written
  canonical_outside_head: bool
  http_canonical: str           canonical from the Link response header, "" if none
  rel_next: str, rel_prev: str  absolute, "" if none (HTML <link>)
  http_rel_next: str, http_rel_prev: str
  hreflang: list[dict]          [{"lang": "en-gb", "url": abs}] from <link rel="alternate" hreflang>
  http_hreflang: list[dict]
  amphtml: str                  <link rel="amphtml"> href, ""
  mobile_alternate: str         <link rel="alternate" media="only screen..."> href, ""

Directives
  meta_robots: list[str]        content of every <meta name="robots"> and <meta name="googlebot">, lowercased
  x_robots_tag: list[str]       X-Robots-Tag header values, lowercased
  directives: list[str]         union of tokens: index, noindex, follow, nofollow, none, noarchive, nosnippet,
                                max-snippet, max-image-preview, max-video-preview, noimageindex, notranslate,
                                unavailable_after, noodp, noydir (token names only, no values)
  meta_refresh: str             content of <meta http-equiv="refresh">, ""
  meta_refresh_url: str         absolute target of the refresh, ""

Links (outlinks of this page; anchors, area, link-in-content; NOT resource tags)
  links: list[dict]             {"url": abs normalized, "raw": href, "anchor": text or img alt,
                                 "alt_only": bool (anchor came from img alt), "rel": list[str],
                                 "nofollow": bool, "target_blank": bool, "noopener": bool (rel has noopener/noreferrer),
                                 "internal": bool, "region": "Header"|"Navigation menu"|"Main content"|"Footer"|
                                 "Sidebar"|"Breadcrumb"|"Form"|"Page body" (extract.LinkPositioner; P16),
                                 "position_rule": "" | the Link Positions rule that placed it ("class: promo"),
                                 "line": int}
  resources: list[dict]         {"url": abs, "type": "image"|"script"|"css"|"iframe"|"other"} from src/href of
                                img (src and every srcset candidate), script, link[rel=stylesheet], iframe,
                                source (every srcset candidate inside <picture>), video, audio; "internal" is also
                                true on a CrawlConfig.cdn_hosts host; "candidate": True marks another size / format
                                of an <img> (requested and checked, not counted in page weight) (P11)
  forms: list[dict]             {"action": abs or "", "method": "get"|"post", "insecure": bool (http action on https page)}
  mixed_content: list[str]      http:// resource URLs loaded by an https page
  protocol_relative: list[str]  "//host/..." resource URLs (img/script/css/iframe...; not <a> hyperlinks)

Images (<img> on this page)
  images: list[dict]            {"src": abs, "alt": str|None (None = no alt attribute), "has_width": bool,
                                 "has_height": bool, "in_link": bool, "line": int}

Structured data
  structured_data: dict         {"jsonld": [types], "microdata": [types], "rdfa": [types],
                                 "jsonld_parse_errors": int, "types": [unique types]}

Mobile / validation
  viewport: str                 <meta name="viewport"> content, ""
  head_count: int, body_count: int        number of <head> / <body> tags in the raw HTML
  invalid_head_elements: list[str]        element names inside <head> that are not allowed there (e.g. "img", "div")

AI / robots meaning and page hygiene (tabs robots_txt.py and hygiene.py; has_doctype None = a page parsed
by an older build, so none of these keys were read for it and the checks report "Not checked")
  data_nosnippet_count: int     elements carrying a data-nosnippet attribute (kept out of snippets and AI answers)
  scoped_noindex: list[str]     crawlers a noindex names on its own ("googlebot", "bingbot") while the
                                generic robots meta / X-Robots-Tag says nothing of the kind
  has_doctype: bool|None        a <!doctype html> starts the document (False = quirks mode)
  charset: str|None             declared character set, lowercased (HTTP header first, then <meta>); "" = none
  dom_nodes: int                number of elements in the parsed document
  dead_fragments: list[str]     same-page "#id" link targets that no element's id (or <a name>) matches
  malformed_hrefs: list[dict]   {"raw", "reason", "line"} for links written in a broken way (bare email,
                                spaces, backslashes, javascript:, broken http:// prefix, local file path)

Build 3 additions (static keys filled by extract.py from the same single parse; engine keys by engine.py)
  ids: list[str]                element ids and <a name> anchors in document order (bookmark targets), max 5,000
  meta_fragment: bool           <meta name="fragment" content="!"> (old AJAX crawling scheme)
  plugins: list[str]            "Flash: <url>" / "Java: <url>" / "Silverlight: <url>" from <object>/<embed>/<applet>
  background_images: list[str]  absolute URLs from background / background-image url() in style="" and <style>
  top_terms: list[(term, count)]  top 100 words of the content area, English stop words and numbers left out
                                (2xx pages only; pairs are tuples, JSON gives [term, count])
  og: {"title","description","image","type","url","site_name"}   values as written ("" when absent)
  twitter: {"card","title","description","image"}
  a11y: dict | None             static accessibility checks (None = not checked):
        {"html_lang": str|None (None = no lang attribute), "inputs_without_label": int,
         "controls_without_name": int (= empty_links + empty_buttons), "images_without_alt": int,
         "duplicate_ids": [ids], "aria_broken_refs": [ids that aria-labelledby/-describedby/-controls/-owns or
         <label for> name but no element has], "has_main_landmark": bool, "heading_skips": ["h2 -> h4"],
         "empty_links": int, "empty_buttons": int, "tabindex_positive": int}
  custom_search: {name: occurrences}       CrawlConfig.custom_searches (counts; filters decide contains / not)
  custom_extraction: {name: [values]}      CrawlConfig.custom_extractors (max 20 values of 2,000 chars each)
  custom_js: {name: value}                 rendered pages only (W1-render)
  cookies: list[dict]           {"name","domain","path","expires","max_age","secure","httponly","samesite"}
  resources_count: int          resources this page loads (seo["resources"] without srcset / <picture> candidates)
  resource_bytes_total: int     known sizes of its internal resources (set by the engine as they are checked)
  resource_bytes_external: int  known sizes of its resources on other hosts; resources_sized: how many had a size
  rendered: dict | None         see PLAN.md (W1-render); also "promoted": bool (the page's main SEO values were
                                read from this render) and "link_targets": [{"url","rel"}] (canonical / next /
                                prev / hreflang URLs only the rendered page has)
  original_html: dict           render-every-page crawls only: {key: raw-HTML value} of every key above that
                                was replaced by its rendered value (JavaScript rendering mode, as Screaming
                                Frog); absent when the page was not promoted. A key missing from it has the
                                same value in the raw and the rendered HTML.
  schema_visible_mismatch: list[{"type","field","value"}]   JSON-LD values (name, headline, sku, telephone,
                                email, address parts, offers price) the visible page does not show
  schema_visible_unverified: list[{"type","field","value","reason"}]  values that could not be judged (a price
                                on a page whose HTML shows no prices: needs rendering), never counted as failed
  schema_values_checked: int    JSON-LD values compared (0 with JSON-LD present = nothing checkable)

Build 4 additions (PLAN4_build.md 10.5; None = not checked; the task that fills each key is named)
  http_refresh_url: str         P4: absolute target of a Refresh response header ("" when none, or no url in it);
                                the crawl queues it like a meta refresh target (one chain: at most max_redirects hops)
  x_robots_tag                  P7: one entry per X-Robots-Tag header line (a repeated header is not joined)
  links[]                       keep "rel"; gain "position_rule": the user rule that set the position (P16)
  images[]                      gain "from": "src" | "srcset" | "picture" | "rendered" and "element": index of the
                                <img> (alt / width / height counted once per element) (P11, A4)
  structured_data.items[]       gain "values": {path: [<=3 values]} (<=200 paths), "errors" / "warnings":
                                [{"path", "message"}] (P12)
  spelling: dict | None         C1 (called from extract.py for 2xx HTML pages while CrawlConfig.spelling is on):
                                {"lang", "checked": bool, "reason", "words": int, "unknown": {word: count}
                                (<= 200 distinct), "capped": bool}; error counts come from spelling.site_errors
  axe: dict | None              A2 (engine audit_hook or the render): {"version", "ran": bool, "reason", "ms",
                                "violations": [{"id", "impact", "level", "tags", "nodes", "targets", "snippets"}],
                                "incomplete": [ids], "passes": [ids]}
  pdf: dict | None              P21: {"title", "author", "subject", "keywords", "pages", "created", "modified",
                                "producer", "word_count", "encrypted": bool, "reason"}
  rendered.device: str          A5

Site (built after the crawl by analysis.build_site; read-only for tabs)
--------------------------------------------------------------------
  site.results: list[CrawlResult]          every internal URL crawled, crawl order
  site.by_url: dict[str, CrawlResult]
  site.pages: list[CrawlResult]            internal HTML results with seo["is_html"]
  site.externals: list                     ExternalLink objects (with .status_code when validated)
  site.start_url, site.base_domain
  site.config: CrawlConfig
  site.inlinks: dict[url, list[dict]]      {"source": url, "anchor": str, "nofollow": bool, "region": str, "line": int,
                                           "rel": "nofollow sponsored" (rel values, "" when none; P6)}
  site.unique_inlinks: dict[url, int]      distinct source pages
  site.outlinks: dict[url, list[dict]]     == page seo["links"]
  site.link_score: dict[url, int]          internal PageRank scaled 0..100 (Screaming Frog Link Score)
  site.indexable: dict[url, bool]
  site.indexability_status: dict[url, str] "" | "Non-200" | "Redirected" | "Client Error" | "Server Error" |
                                           "noindex" | "Canonicalised" | "Blocked by robots.txt" | "No Response"
  site.duplicates: dict[str, dict[str, list[url]]]  kind -> value -> urls, kinds: "title", "meta_description",
                                           "h1", "h2", "content_hash"  (only indexable pages, values with >1 url)
  site.near_duplicates: dict[url, list[(url, similarity_pct)]]   minhash Jaccard of word 3-shingles >= 90%,
                        closest first, at most analysis.NEAR_DUP_MAX_PER_URL per URL (legacy crawls: simhash)
  site.near_duplicate_counts: dict[url, int]                       exact number of near duplicates per URL
  site.near_duplicate_info: dict           {"threshold" (CrawlConfig.near_dup_threshold, 50-100), "rows" (LSH bins per
                                           band), "pairs" (candidate pairs compared), "capped", "method"} (P13; over
                                           2,000,000 candidate pairs the step is Not checked: analysis_skipped)
  site.sitemap_urls: dict[url, list[sitemap_url]]   URLs listed in XML sitemaps (from the sitemap parser)
  site.orphans: list[url]                  in a sitemap but never linked from a crawled page
  site.crawl_depth: dict[url, int]
  Post-crawl checks (crawler/seo/postcrawl.py stores them on the crawler; JSON-ready dicts, None = did not run):
  site.bot_report                          bot_check.run(): pages fetched as a browser and as search / AI bots
  site.robots_audit                        robots_audit.run(): robots.txt AI policy, Content-Signal, Sitemap lines
  site.trap_probe                          trap_probe.run(): random-URL probes (soft 404 server, infinite URL space)
  site.crawl_budget                        crawl_budget.analyse(site), computed on first use (crawl_budget.get)
  Build 3 fields (analysis.build_site):
  site.broken_bookmarks: dict[url, list]   {"fragment", "source", "line"}: links to a #fragment the page lacks
  site.semantic: dict[url, (url, cosine)]  closest indexable page by TF-IDF on top_terms (Semantically Similar)
  site.low_relevance: dict[url, cosine]    similarity to the rest of the site (Low Relevance Content)
  site.semantic_info: dict                 method, pages, thresholds, skipped / not-compared notes
  site.sitemap_lastmod: dict[url, str]|None  <lastmod> as written (None = not recorded by this crawl)
  site.sitemap_stats                       crawler.sitemap_stats: {sitemap file url: {"urls": entries (<url>, or
                                           <sitemap> for an index), "size_bytes": uncompressed size (None = not
                                           read to its end), "index": bool}} for each file read (None = none read);
                                           crawler.sitemaps_not_read lists files left unread (max_sitemap_files per
                                           crawl, 25 before P9)
  site.analysis_skipped: dict[step, str]   Crawl Analysis steps turned off or not run, with the reason
  site.integrations: dict                  crawler.integrations (Search Console / GA4 / PageSpeed data, no secrets;
                                           Build 4 adds ai, embeddings, languagetool, link_metrics, green_web, logs
                                           through integrations.put_result)
  site.crawl_finished: bool
  Build 4 fields (analysis.build_site; empty until the task that fills them):
  site.other_inlinks: dict[url, list]      {"source", "type", "rel", "via" (html | header | script), "region",
                                           "lang" (hreflang), "method" (form)}: type canonical, hreflang, next, prev,
                                           meta_refresh, http_refresh, js_redirect, mobile_alternate, form_action,
                                           link_element (amphtml and other link elements); keyed by the normalized
                                           target, internal or not; a URL pointing at itself is left out (P6; kept
                                           apart from site.inlinks so Link Score and inlink counts stay)
  site.sitemap_hreflang: dict[url, list]   [{"lang", "url"}] from <xhtml:link rel="alternate"> in the XML sitemap
                                           entries of indexed page URLs (P8; at most 100,000 alternates per crawl)
  site.sitemap_index_full: dict | None     {"limit", "file", "left_out"} when the sitemap URL index stopped at
                                           max_sitemap_index_urls (P9): URLs past it are "Not checked"
  site.sitemaps_not_read: list[url]        sitemap files listed but not read (P9; == crawler.sitemaps_not_read)
  site.sitemap_files_limit: int | None     the most sitemap files the crawl could read (None: before P9, 25)
  site.carbon: dict[url, dict]             {"bytes", "grams", "rating", "green", "estimate", "sized"} (P18);
  site.carbon_info: dict                   the model, its citation and the green-hosting source
  site.llms_txt: dict | None               the llms.txt / llms-full.txt check with the crawl's verdict on every
                                           link (P19; None = the check did not run for this crawl)
  site.preferences: dict                   CrawlConfig.preferences (thresholds; read through registry.pref)

Helpers available to tabs (crawler/seo/registry.py):  seo(r) -> PageData (empty dict for none),
is_indexable(site, r), first(lst, default="").
"""
from typing import Any, Dict

# Keys every PageData dict carries, with their empty values; extract.py starts from a copy of this.
EMPTY_PAGE: Dict[str, Any] = {
    "url": "", "final_url": "", "status_code": None, "content_type": "", "is_html": False, "http_version": "",
    "size_bytes": 0, "transferred_bytes": 0, "response_time_ms": 0, "last_modified": "", "language": "",
    "titles": [], "title_outside_head": False, "meta_descriptions": [], "meta_description_outside_head": False,
    "meta_keywords": [],
    "h1": [], "h2": [], "h1_alt_only": [], "headings": [],
    "h1_img_alts": [],                  # alt text of the <img>s inside each <h1> that has any (with or without text)
    "word_count": 0, "sentence_count": 0, "avg_words_per_sentence": 0.0, "flesch_reading_ease": None,
    "readability": "", "text_ratio": 0.0, "content_hash": "", "simhash": 0, "minhash": "", "lorem_ipsum": False,
    "soft_404_signals": [],
    "canonicals": [], "canonicals_raw": [], "canonical_outside_head": False, "http_canonical": "",
    "rel_next": "", "rel_prev": "", "http_rel_next": "", "http_rel_prev": "",
    "hreflang": [], "http_hreflang": [], "amphtml": "", "mobile_alternate": "",
    "canonical_invalid_attrs": [],      # hreflang/lang/media/type attributes found on a rel=canonical <link>
    "rel_next_all": [], "rel_prev_all": [],   # every HTML rel=next / rel=prev URL (rel_next/rel_prev keep the first)
    "hreflang_outside_head": False,     # an hreflang <link> sits outside <head>
    "meta_robots_outside_head": False,  # a robots/googlebot <meta> sits outside <head>
    "meta_robots": [], "x_robots_tag": [], "directives": [], "meta_refresh": "", "meta_refresh_url": "",
    "links": [], "resources": [], "forms": [], "mixed_content": [], "protocol_relative": [],
    "images": [],
    # structured_data["items"]: [{"format": "jsonld"|"microdata"|"rdfa", "type": str, "props": [names]}] per item
    "structured_data": {"jsonld": [], "microdata": [], "rdfa": [], "jsonld_parse_errors": 0, "types": [], "items": []},
    "viewport": "", "head_count": 0, "body_count": 0, "invalid_head_elements": [],
    "head_not_first": False,            # another element precedes <head> inside <html> (Validation tab)
    "body_before_html": False,          # a <body> tag appears before the opening <html> tag (Validation tab)
    # AI / robots meaning and hygiene (see the docstring above; has_doctype None = not read for this page)
    "data_nosnippet_count": 0,          # elements with a data-nosnippet attribute
    "scoped_noindex": [],               # "googlebot" / "bingbot": a noindex aimed at that crawler only
    "has_doctype": None,                # True / False once read; None = not read (older build or no HTML)
    "charset": None,                    # declared charset, lowercased; "" = none declared; None = not read
    "dom_nodes": 0,                     # elements in the parsed document
    "dead_fragments": [],               # same-page #fragment link targets with no matching id / <a name>
    "malformed_hrefs": [],              # [{"raw", "reason", "line"}] links written in a broken way
    # Build 3 static keys (extract.py; see the docstring "Build 3 additions")
    "ids": [], "meta_fragment": False, "plugins": [], "background_images": [], "top_terms": [],
    "og": {"title": "", "description": "", "image": "", "type": "", "url": "", "site_name": ""},
    "twitter": {"card": "", "title": "", "description": "", "image": ""},
    "a11y": None,                       # dict once the HTML was read (None = not checked)
    "custom_search": {}, "custom_extraction": {}, "custom_js": {},
    "cookies": [],                      # Set-Cookie of this URL's answer: names and attributes, never values
    "resource_bytes_total": 0, "resource_bytes_external": 0, "resources_sized": 0, "resources_count": 0,
    "rendered": None,                   # filled when the page was rendered in Chrome (render_all)
    "schema_visible_mismatch": [], "schema_visible_unverified": [], "schema_values_checked": 0,
    # Build 4 keys (see the docstring "Build 4 additions"; None = not checked)
    "http_refresh_url": "",             # P4: target of a Refresh response header
    "spelling": None,                   # C1, called from extract.py
    "axe": None,                        # A2: axe-core audit
    "pdf": None,                        # P21: PDF properties
}

# rendered["redirect_type"] values after which Chrome showed ANOTHER document than the crawled page ("history_api"
# keeps the same document): its title, links, DOM checks, images and snippets describe the destination
LEFT_PAGE_REDIRECTS = ("javascript", "meta_refresh", "http_refresh", "unknown")


def rendered_left_page(rendered: Any) -> bool:
    """True when a page's render ended on another document (a JavaScript, meta refresh or HTTP refresh redirect
    fired while Chrome waited), so the rendered content must not be read as the crawled page's own."""
    return isinstance(rendered, dict) and str(rendered.get("redirect_type") or "") in LEFT_PAGE_REDIRECTS


ISSUE_TYPES = ("Issue", "Warning", "Opportunity")   # Screaming Frog's three issue types
PRIORITIES = ("High", "Medium", "Low")
