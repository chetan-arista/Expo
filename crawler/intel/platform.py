"""
Platform detection: which CMS / shop system / front-end framework a crawled site runs on, and which
plugins, apps or extensions it uses. Root-cause groups use it to give platform-specific fix steps
(WordPress / Shopify / Magento / Next.js admin paths) and to name the two tools behind a plugin
conflict.

detect_platform(site_or_crawler, fetch=False) -> {"cms", "plugins", "confidence", "evidence", ...}

Signals, all read from the crawl data (no extra requests unless fetch=True):
  - asset paths of every crawled page (scripts, stylesheets, images, iframes) and crawled resource URLs:
    /wp-content/plugins/<slug>/, cdn.shopify.com, /static/version123/frontend/<Vendor>/<theme>/,
    /_next/static/, /_nuxt/, static.wixstatic.com ...
  - response headers: x-powered-by, x-shopify-stage / x-shopid, x-magento-*, link: api.w.org,
    x-redirect-by, set-cookie names ...
  - URL structure of the crawled pages (/products/ + /collections/, /product-category/ ...)
  - XML sitemap file names (sitemap_index.xml + post-sitemap.xml, wp-sitemap.xml ...)
  - fetch=True also reads the home page HTML once (generator meta, SEO-plugin comments, theme names);
    use it only in background jobs: it is one network request.

Each CMS collects a score from the distinct markers it matched; the best one wins and the score maps to
a confidence 0..1. A platform is named only when its score reaches MIN_NAME_SCORE (one strong marker, or
several weak hints together); weak hints alone (URL shapes) leave "cms" empty, with the hint in the
evidence and in "possible", so the fix plan never gives Shopify steps to a site that only looks like a shop.
Evidence lines are short, plain-language and say where each marker was seen.
"""
import math
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

CMS_NAMES = ("WordPress", "Shopify", "Magento 2", "BigCommerce", "Wix", "Squarespace", "Webflow",
             "Drupal", "Joomla", "Next.js", "Nuxt", "Gatsby")
FRAMEWORKS = ("Next.js", "Nuxt", "Gatsby")
MAX_PAGES = 400            # pages sampled for asset/header signals (enough to see every template)
MAX_PLUGINS = 25
# Score needed to NAME a platform: one strong marker (an asset path, a response header, a generator tag)
# or several weak hints together. URL shapes alone (/products/ + /collections/) are only a hint: many
# shops and custom builds use them, and naming the wrong platform gives wrong fix steps.
MIN_NAME_SCORE = 2.0

# (cms, marker, weight, label): substring of an asset / crawled URL (lowercase)
ASSET_MARKERS: List[Tuple[str, str, float, str]] = [
    ("WordPress", "/wp-content/", 3.0, "assets under /wp-content/"),
    ("WordPress", "/wp-includes/", 3.0, "assets under /wp-includes/"),
    ("Shopify", "cdn.shopify.com", 3.5, "assets on cdn.shopify.com"),
    ("Shopify", "/cdn/shop/", 3.0, "assets under /cdn/shop/ (Shopify's own CDN path)"),
    ("Shopify", ".myshopify.com", 2.0, "a .myshopify.com address"),
    ("Magento 2", "/static/version", 3.0, "static files under /static/version.../ (Magento 2 deploy)"),
    ("Magento 2", "/static/frontend/", 2.5, "theme files under /static/frontend/"),
    ("Magento 2", "/media/catalog/product/", 2.0, "product images under /media/catalog/product/"),
    ("Magento 2", "requirejs/require", 1.0, "RequireJS loader (Magento 2 front end)"),
    ("Magento 2", "/mage/", 1.5, "Magento 'mage' scripts"),
    ("BigCommerce", "bigcommerce.com/s-", 3.5, "assets on the BigCommerce CDN"),
    ("BigCommerce", "cdn11.bigcommerce.com", 3.5, "assets on cdn11.bigcommerce.com"),
    ("Wix", "static.wixstatic.com", 3.5, "assets on static.wixstatic.com"),
    ("Wix", "static.parastorage.com", 3.0, "scripts on static.parastorage.com (Wix)"),
    ("Squarespace", "static1.squarespace.com", 3.5, "assets on static1.squarespace.com"),
    ("Squarespace", "assets.squarespace.com", 3.0, "assets on assets.squarespace.com"),
    ("Squarespace", "squarespace-cdn.com", 3.0, "images on squarespace-cdn.com"),
    ("Webflow", "website-files.com", 3.5, "assets on website-files.com (Webflow CDN)"),
    ("Webflow", "uploads-ssl.webflow.com", 3.5, "assets on uploads-ssl.webflow.com"),
    ("Webflow", "/webflow.", 1.5, "the webflow.js script"),
    ("Drupal", "/sites/default/files/", 3.0, "files under /sites/default/files/"),
    ("Drupal", "/core/misc/drupal", 3.0, "Drupal core scripts"),
    ("Joomla", "/media/jui/", 3.0, "Joomla UI assets under /media/jui/"),
    ("Joomla", "/components/com_", 2.0, "Joomla component paths"),
    ("Next.js", "/_next/static/", 3.5, "scripts under /_next/static/"),
    ("Next.js", "/_next/image", 2.0, "images served through /_next/image"),
    ("Nuxt", "/_nuxt/", 3.5, "scripts under /_nuxt/"),
    ("Gatsby", "/webpack-runtime-", 2.5, "webpack-runtime script (Gatsby build)"),
    ("Gatsby", "/page-data/", 2.0, "Gatsby page-data files"),
]

# (cms, header, value part or "", weight, label)
HEADER_MARKERS: List[Tuple[str, str, str, float, str]] = [
    ("WordPress", "link", "api.w.org", 3.0, "the Link header points to the WordPress REST API"),
    ("WordPress", "x-redirect-by", "wordpress", 2.0, "redirects answered by WordPress (x-redirect-by)"),
    ("WordPress", "x-powered-by", "wp engine", 2.0, "hosted on WP Engine"),
    ("WordPress", "x-kinsta-cache", "", 1.5, "hosted on Kinsta (WordPress host)"),
    ("Shopify", "x-shopify-stage", "", 3.5, "x-shopify-stage header"),
    ("Shopify", "x-shopid", "", 3.5, "x-shopid header"),
    ("Shopify", "x-sorting-hat-shopid", "", 3.0, "x-sorting-hat-shopid header"),
    ("Shopify", "powered-by", "shopify", 3.0, "powered-by: Shopify header"),
    ("Magento 2", "x-magento-cache-debug", "", 3.0, "x-magento-cache-debug header"),
    ("Magento 2", "x-magento-tags", "", 3.0, "x-magento-tags header"),
    ("Magento 2", "x-magento-vary", "", 2.0, "x-magento-vary cookie/header"),
    ("Magento 2", "set-cookie", "mage-", 2.0, "Magento cookies (mage-*)"),
    ("Magento 2", "set-cookie", "x-magento-vary", 2.0, "X-Magento-Vary cookie"),
    ("Wix", "x-wix-request-id", "", 3.5, "x-wix-request-id header"),
    ("Drupal", "x-drupal-cache", "", 3.0, "x-drupal-cache header"),
    ("Drupal", "x-generator", "drupal", 3.0, "x-generator: Drupal header"),
    ("Next.js", "x-powered-by", "next.js", 3.5, "x-powered-by: Next.js header"),
    ("Next.js", "x-nextjs-cache", "", 3.0, "x-nextjs-cache header"),
    ("Next.js", "x-nextjs-prerender", "", 3.0, "x-nextjs-prerender header"),
    ("Nuxt", "x-powered-by", "nuxt", 3.0, "x-powered-by: Nuxt header"),
]

# (cms, regex on the home page HTML (lowercase), weight, label)
HTML_MARKERS: List[Tuple[str, str, float, str]] = [
    ("WordPress", r'name=["\']generator["\'][^>]*content=["\']wordpress', 3.0, "generator meta says WordPress"),
    ("Shopify", r"shopify\.theme\s*=", 3.0, "the Shopify.theme script object"),
    ("Magento 2", r"data-mage-init|x-magento-init", 3.0, "Magento data-mage-init / x-magento-init markup"),
    ("BigCommerce", r"stencil-utils|bigcommerce", 2.0, "BigCommerce Stencil markup"),
    ("Wix", r'content=["\']wix\.com website builder', 3.0, "generator meta says Wix"),
    ("Squarespace", r"squarespace", 1.5, "Squarespace markup"),
    ("Webflow", r"data-wf-(?:site|page)", 3.0, "Webflow data-wf-site attributes"),
    ("Drupal", r'content=["\']drupal|data-drupal-', 3.0, "Drupal markup"),
    ("Joomla", r'content=["\']joomla', 3.0, "generator meta says Joomla"),
    ("Next.js", r"__next_data__|self\.__next_f", 3.0, "Next.js page data script"),
    ("Nuxt", r"window\.__nuxt__|data-n-head|id=\"__nuxt\"", 3.0, "Nuxt page data / root element"),
    ("Gatsby", r"id=\"___gatsby\"|content=[\"']gatsby", 3.0, "Gatsby root element / generator"),
]

# WordPress plugin folder -> display name (unknown folders are title-cased)
WP_PLUGINS: Dict[str, str] = {
    "wordpress-seo": "Yoast SEO", "wordpress-seo-premium": "Yoast SEO Premium", "seo-by-rank-math": "Rank Math SEO",
    "seo-by-rank-math-pro": "Rank Math Pro", "all-in-one-seo-pack": "All in One SEO",
    "all-in-one-seo-pack-pro": "All in One SEO Pro", "autodescription": "The SEO Framework", "wp-seopress": "SEOPress",
    "elementor": "Elementor", "elementor-pro": "Elementor Pro", "woocommerce": "WooCommerce",
    "wp-rocket": "WP Rocket", "litespeed-cache": "LiteSpeed Cache", "w3-total-cache": "W3 Total Cache",
    "wp-super-cache": "WP Super Cache", "autoptimize": "Autoptimize", "wp-optimize": "WP-Optimize",
    "contact-form-7": "Contact Form 7", "wpforms-lite": "WPForms", "wpforms": "WPForms",
    "gravityforms": "Gravity Forms", "ninja-forms": "Ninja Forms", "fluentform": "Fluent Forms",
    "js_composer": "WPBakery Page Builder", "revslider": "Slider Revolution", "jetpack": "Jetpack",
    "wordfence": "Wordfence", "wp-smushit": "Smush", "ewww-image-optimizer": "EWWW Image Optimizer",
    "imagify": "Imagify", "shortpixel-image-optimiser": "ShortPixel", "redirection": "Redirection",
    "header-footer-elementor": "Elementor Header & Footer", "essential-addons-for-elementor-lite": "Essential Addons",
    "sitepress-multilingual-cms": "WPML", "polylang": "Polylang", "google-site-kit": "Site Kit by Google",
    "complianz-gdpr": "Complianz", "cookie-law-info": "CookieYes", "insert-headers-and-footers": "WPCode",
    "really-simple-ssl": "Really Simple SSL", "mailchimp-for-wp": "MC4WP", "beaver-builder-lite-version": "Beaver Builder",
    "divi-builder": "Divi Builder", "astra-addon": "Astra Pro", "wp-schema-pro": "Schema Pro",
    "schema-and-structured-data-for-wp": "Schema & Structured Data for WP", "amp": "AMP",
}
SEO_PLUGINS = ("Yoast SEO", "Yoast SEO Premium", "Rank Math SEO", "Rank Math Pro", "All in One SEO",
               "All in One SEO Pro", "The SEO Framework", "SEOPress", "Schema Pro", "Squirrly SEO")
# Cache / optimisation folders under /wp-content/ that reveal a plugin without a /plugins/ path
WP_CONTENT_HINTS = (("/wp-content/cache/wp-rocket/", "WP Rocket"), ("/wp-content/cache/autoptimize/", "Autoptimize"),
                    ("/wp-content/litespeed/", "LiteSpeed Cache"), ("/wp-content/cache/minify/", "W3 Total Cache"),
                    ("/wp-content/uploads/elementor/", "Elementor"), ("/wp-content/et-cache/", "Divi"),
                    ("/wp-content/cache/flying-press/", "FlyingPress"))
# SEO plugin signatures in HTML comments / markup of the home page (lowercase)
SEO_HTML = (("yoast seo plugin", "Yoast SEO"), ("rank math", "Rank Math SEO"), ("all in one seo", "All in One SEO"),
            ("the seo framework", "The SEO Framework"), ("seopress", "SEOPress"), ("squirrly", "Squirrly SEO"))
# x-redirect-by values -> plugin
REDIRECT_BY = (("yoast", "Yoast SEO"), ("rank math", "Rank Math SEO"), ("aioseo", "All in One SEO"),
               ("all in one seo", "All in One SEO"), ("redirection", "Redirection"))
# Third-party script hosts -> Shopify app / site add-on names (shown as plugins for any platform)
APP_HOSTS = (("static.klaviyo.com", "Klaviyo"), ("judge.me", "Judge.me reviews"), ("yotpo.com", "Yotpo"),
             ("okendo.io", "Okendo reviews"), ("loox.io", "Loox reviews"), ("stamped.io", "Stamped reviews"),
             ("rebuyengine.com", "Rebuy"), ("rechargecdn.com", "Recharge subscriptions"), ("privy.com", "Privy"),
             ("gorgias.chat", "Gorgias"), ("tidio.co", "Tidio chat"), ("searchanise", "Searchanise search"),
             ("boostcommerce", "Boost search & filter"), ("shopifycloud", "Shopify built-in scripts"),
             ("hotjar.com", "Hotjar"), ("googletagmanager.com", "Google Tag Manager"),
             ("connect.facebook.net", "Meta Pixel"), ("clarity.ms", "Microsoft Clarity"),
             ("js.hs-scripts.com", "HubSpot"), ("cdn.cookielaw.org", "OneTrust"), ("consent.cookiebot.com", "Cookiebot"),
             ("widget.trustpilot.com", "Trustpilot"), ("cdn.jsdelivr.net/npm/swiper", "Swiper"))
SITEMAP_HINTS = (("sitemap_index.xml", "WordPress", "Yoast SEO or Rank Math sitemap (sitemap_index.xml)", 1.0),
                 ("wp-sitemap", "WordPress", "WordPress core sitemap (wp-sitemap.xml)", 2.0),
                 ("sitemap_products_", "Shopify", "Shopify product sitemaps (sitemap_products_1.xml)", 2.0),
                 ("/pub/media/sitemap", "Magento 2", "Magento sitemap under /pub/media/", 1.5),
                 ("/xmlsitemap.php", "BigCommerce", "BigCommerce sitemap (xmlsitemap.php)", 2.5))
_MAGENTO_THEME = re.compile(r"/static/(?:version\d+/)?frontend/([A-Za-z0-9_]+)/([A-Za-z0-9_-]+)/")
_MAGENTO_MODULE = re.compile(r"/static/(?:version\d+/)?frontend/[^/]+/[^/]+/[^/]+/([A-Z][A-Za-z0-9]+_[A-Za-z0-9]+)/")
_WP_PLUGIN = re.compile(r"/wp-content/plugins/([a-z0-9_.-]+)/", re.I)
_WP_THEME = re.compile(r"/wp-content/themes/([a-z0-9_.-]+)/", re.I)
_SHOPIFY_THEME = re.compile(r'shopify\.theme\s*=\s*\{[^}]*?"name"\s*:\s*"([^"]{1,60})"', re.I)
_GENERATOR = re.compile(r'<meta[^>]+name=["\']generator["\'][^>]*content=["\']([^"\']{1,80})["\']'
                        r'|<meta[^>]+content=["\']([^"\']{1,80})["\'][^>]*name=["\']generator["\']', re.I)


def empty_platform() -> dict:
    return {"cms": "", "plugins": [], "confidence": 0.0, "evidence": [], "frontend": "", "theme": "", "hosting": ""}


def _results_of(obj: Any) -> List[Any]:
    """Crawl results from a Site (results), a WebsiteCrawler (results_list) or a plain list."""
    if obj is None:
        return []
    if isinstance(obj, (list, tuple)):
        return list(obj)
    for name in ("results", "results_list"):
        val = getattr(obj, name, None)
        if isinstance(val, (list, tuple)):
            return list(val)
    return []


def _seo(r) -> dict:
    p = getattr(r, "seo", None)
    return p if isinstance(p, dict) else {}


def _is_page(r) -> bool:
    return bool(_seo(r).get("is_html"))


def _sitemap_files(obj: Any) -> List[str]:
    raw = getattr(obj, "sitemap_urls", None) or getattr(obj, "sitemap_index", None) or {}
    files = set()
    try:
        for srcs in raw.values():
            for s in (srcs if isinstance(srcs, (list, tuple, set)) else [srcs]):
                if s:
                    files.add(str(s).lower())
    except Exception:
        return []
    return sorted(files)[:200]


def _start_url(obj: Any) -> str:
    return str(getattr(obj, "start_url", "") or "")


def _session_of(obj: Any):
    return getattr(obj, "http_session", None) or getattr(obj, "session", None)


def fetch_home_html(obj: Any, timeout: int = 8, max_bytes: int = 1_500_000) -> Tuple[str, Dict[str, str]]:
    """The start page HTML and its headers (lowercase keys), or ("", {}) on any problem. Uses the crawl's
    own session when there is one (same cookies and User-Agent)."""
    url = _start_url(obj)
    if not url:
        return "", {}
    try:
        import requests
        session = _session_of(obj)
        cfg = getattr(obj, "config", None)
        headers = {"User-Agent": getattr(cfg, "user_agent", "") or "Mozilla/5.0 (compatible; SEO audit)"}
        getter = session.get if session is not None else requests.get
        resp = getter(url, timeout=timeout, headers=None if session is not None else headers, stream=True,
                      verify=bool(getattr(cfg, "verify_ssl", False)), allow_redirects=True)
        raw = resp.raw.read(max_bytes, decode_content=True) if resp.raw is not None else b""
        resp.close()
        enc = resp.encoding or "utf-8"
        return raw.decode(enc, errors="replace"), {str(k).lower(): str(v) for k, v in resp.headers.items()}
    except Exception:
        return "", {}


class _Score:
    """Distinct markers matched per CMS, with where they were seen."""

    def __init__(self):
        self.score: Dict[str, float] = defaultdict(float)
        self.seen: Dict[Tuple[str, str], int] = {}
        self.evidence: List[Tuple[str, str]] = []

    def hit(self, cms: str, label: str, weight: float, count: int = 1):
        key = (cms, label)
        if key in self.seen:
            self.seen[key] += count
            return
        self.seen[key] = count
        self.score[cms] += weight
        self.evidence.append(key)


def _confidence(score: float) -> float:
    return round(min(0.99, 1.0 - math.exp(-score / 3.0)), 2)


def _title(slug: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]+", slug) if w)[:60]


def detect_platform(site_or_crawler: Any, fetch: bool = False, html: str = "",
                    html_headers: Optional[Dict[str, str]] = None) -> dict:
    """{"cms", "plugins", "confidence", "evidence", "frontend", "theme", "hosting"} for a Site, a
    WebsiteCrawler or a list of CrawlResults. fetch=True reads the home page HTML once (background jobs
    only); `html` passes HTML already at hand instead. Never raises."""
    try:
        return _detect(site_or_crawler, fetch, html, html_headers)
    except Exception:
        return empty_platform()


def _detect(obj: Any, fetch: bool, html: str, html_headers: Optional[Dict[str, str]]) -> dict:
    results = _results_of(obj)
    sc = _Score()
    plugins: Counter = Counter()
    themes: Counter = Counter()
    mag_modules: Counter = Counter()
    hosting = ""

    pages = [r for r in results if _is_page(r)][:MAX_PAGES]
    asset_urls: List[str] = []
    for r in results[: MAX_PAGES * 4]:
        if not _is_page(r):
            asset_urls.append(str(getattr(r, "url", "") or ""))
    for p in pages:
        for res in _seo(p).get("resources") or ():
            try:
                asset_urls.append(str(res.get("url") or ""))
            except Exception:
                continue

    # Asset paths
    marker_counts: Counter = Counter()
    for u in asset_urls:
        low = u.lower()
        for cms, marker, weight, label in ASSET_MARKERS:
            if marker in low:
                marker_counts[(cms, label, weight)] += 1
        m = _WP_PLUGIN.search(u)
        if m:
            slug = m.group(1).lower()
            plugins[WP_PLUGINS.get(slug, _title(slug))] += 1
        for path, name in WP_CONTENT_HINTS:
            if path in low:
                plugins[name] += 1
        m = _WP_THEME.search(u)
        if m:
            themes[_title(m.group(1))] += 1
        m = _MAGENTO_THEME.search(u)
        if m and m.group(1) != "Magento":
            themes[f"{m.group(1)}/{m.group(2)}"] += 1
        elif m:
            themes[f"Magento/{m.group(2)}"] += 1
        m = _MAGENTO_MODULE.search(u)
        if m and not m.group(1).startswith(("Magento_", "mage")):
            mag_modules[m.group(1)] += 1
        for host, name in APP_HOSTS:
            if host in low:
                plugins[name] += 1
        if "/cdn/shop/t/" in low or ("cdn.shopify.com" in low and "/t/" in low):
            themes["Shopify theme"] += 1
    for (cms, label, weight), n in marker_counts.items():
        sc.hit(cms, f"{label} ({n} files)", weight, n)

    # Response headers (pages first: files on a CDN carry other headers)
    header_hits: Counter = Counter()
    for r in (pages or results[:MAX_PAGES]):
        headers = getattr(r, "response_headers", None) or {}
        if not isinstance(headers, dict):
            continue
        for cms, name, part, weight, label in HEADER_MARKERS:
            v = headers.get(name)
            if v is not None and (not part or part in str(v).lower()):
                header_hits[(cms, label, weight)] += 1
        rb = str(headers.get("x-redirect-by", "") or "").lower()
        for marker, name in REDIRECT_BY:
            if marker in rb:
                plugins[name] += 1
        if not hosting:
            hosting = _hosting(headers)
    redirects_by_wp = 0
    for r in results[: MAX_PAGES * 2]:
        if _is_page(r):
            continue          # pages were counted above
        rb = str((getattr(r, "response_headers", None) or {}).get("x-redirect-by", "") or "").lower()
        if rb == "wordpress":
            redirects_by_wp += 1
        for marker, name in REDIRECT_BY:
            if marker in rb:
                plugins[name] += 1
                break
    if redirects_by_wp:
        header_hits[("WordPress", "redirects answered by WordPress (x-redirect-by)", 2.0)] += redirects_by_wp
    for (cms, label, weight), n in header_hits.items():
        sc.hit(cms, f"{label} ({n} pages)", weight, n)

    # URL structure of the crawled pages
    paths = [urlparse(str(getattr(r, "url", "") or "")).path.lower() for r in pages]
    has = lambda part: sum(1 for p in paths if part in p)   # noqa: E731
    if has("/products/") and has("/collections/"):
        sc.hit("Shopify", "store URLs /products/ and /collections/", 1.5)
    if has("/product-category/") or (has("/product/") and has("/cart/")):
        plugins["WooCommerce"] += 1
        sc.hit("WordPress", "WooCommerce store URLs (/product/, /product-category/)", 1.0)
    if has("/tag/") and has("/category/"):
        sc.hit("WordPress", "blog archive URLs /tag/ and /category/", 0.5)
    if sum(1 for p in paths if p.endswith(".html")) >= max(3, len(paths) // 3) and has("/catalog/"):
        sc.hit("Magento 2", "catalog .html URLs", 0.5)

    # Sitemap file names
    files = _sitemap_files(obj)
    for marker, cms, label, weight in SITEMAP_HINTS:
        if any(marker in f for f in files):
            sc.hit(cms, label, weight)

    # Home page HTML (optional)
    if not html and fetch:
        html, fetched_headers = fetch_home_html(obj)
        html_headers = html_headers or fetched_headers
    if html:
        low = html.lower()
        for cms, pattern, weight, label in HTML_MARKERS:
            if re.search(pattern, low):
                sc.hit(cms, f"{label} (home page)", weight)
        for marker, name in SEO_HTML:
            if marker in low:
                plugins[name] += 1
        m = _SHOPIFY_THEME.search(html)
        if m:
            themes[m.group(1)] += 3
        for m in _GENERATOR.finditer(html):
            gen = (m.group(1) or m.group(2) or "").strip()
            gl = gen.lower()
            if gl.startswith("wordpress"):
                continue
            for word, name in (("woocommerce", "WooCommerce"), ("elementor", "Elementor"), ("wpbakery", "WPBakery Page Builder"),
                               ("site kit", "Site Kit by Google"), ("divi", "Divi"), ("powered by slider revolution", "Slider Revolution")):
                if word in gl:
                    plugins[name] += 1
        for m in _WP_PLUGIN.finditer(html):
            slug = m.group(1).lower()
            plugins[WP_PLUGINS.get(slug, _title(slug))] += 1
        if html_headers:
            for cms, name, part, weight, label in HEADER_MARKERS:
                v = html_headers.get(name)
                if v is not None and (not part or part in str(v).lower()):
                    sc.hit(cms, f"{label} (home page)", weight)

    if not sc.score:
        out = empty_platform()
        out["hosting"] = hosting
        out["plugins"] = [n for n, _ in plugins.most_common(MAX_PLUGINS)]
        if not results:
            out["evidence"] = ["No crawl data yet."]
        else:
            out["evidence"] = ["No CMS fingerprint found in asset paths, headers or URLs (custom build or unknown platform)."]
        return out

    ranked = sorted(sc.score.items(), key=lambda kv: -kv[1])
    cms_ranked = [(c, s) for c, s in ranked if c not in FRAMEWORKS]
    fw_ranked = [(c, s) for c, s in ranked if c in FRAMEWORKS]
    if cms_ranked and cms_ranked[0][1] >= MIN_NAME_SCORE:
        cms, score = cms_ranked[0]
    else:
        cms, score = ranked[0]
    if score < MIN_NAME_SCORE:
        # weak hints only: say so, but do not name a platform (fix steps stay generic)
        out = empty_platform()
        out["hosting"] = hosting
        out["plugins"] = [n for n, _ in plugins.most_common(MAX_PLUGINS)]
        hints = [label for c, label in sc.evidence if c == cms][:3]
        out["evidence"] = [f"Not detected: only a weak hint ({'; '.join(hints)}) that could mean {cms} "
                           f"({_confidence(score):.0%} sure). That is not enough to name the platform, so fix steps "
                           "stay generic."]
        out["possible"] = cms
        out["possible_confidence"] = _confidence(score)
        return out
    frontend = fw_ranked[0][0] if fw_ranked and fw_ranked[0][0] != cms and fw_ranked[0][1] >= 2.5 else ""
    if cms in FRAMEWORKS:
        frontend = ""

    plugin_list: List[str] = []
    if cms == "Magento 2":
        plugin_list += [f"Extension: {m}" for m, _ in mag_modules.most_common(10)]
    for name, _ in plugins.most_common():
        if name not in plugin_list:
            plugin_list.append(name)
    if frontend:
        plugin_list.insert(0, f"{frontend} (front end)")
    if cms == "WordPress" and "Yoast SEO" not in plugin_list and "Rank Math SEO" not in plugin_list \
            and any("sitemap_index.xml" in f for f in files):
        plugin_list.append("Yoast SEO or Rank Math (from the sitemap name)")

    evidence = [f"{label}" for c, label in sc.evidence if c == cms][:6]
    if frontend:
        evidence += [f"{label}" for c, label in sc.evidence if c == frontend][:2]
    others = [f"{c} ({_confidence(s):.0%})" for c, s in ranked[1:3] if c not in (cms, frontend) and s >= 2.0]
    if others:
        evidence.append("Also some signs of: " + ", ".join(others))
    theme = themes.most_common(1)[0][0] if themes else ""
    return {"cms": cms, "plugins": plugin_list[:MAX_PLUGINS], "confidence": _confidence(score), "evidence": evidence,
            "frontend": frontend, "theme": theme, "hosting": hosting}


_HOSTING = (("x-kinsta-cache", "", "Kinsta"), ("x-powered-by", "wp engine", "WP Engine"), ("wpe-backend", "", "WP Engine"),
            ("x-vercel-id", "", "Vercel"), ("x-nf-request-id", "", "Netlify"), ("cf-ray", "", "Cloudflare"),
            ("x-shopify-stage", "", "Shopify"), ("x-shopid", "", "Shopify"), ("x-wix-request-id", "", "Wix"),
            ("x-amz-cf-id", "", "Amazon CloudFront"), ("x-fastly-request-id", "", "Fastly"),
            ("server", "cloudflare", "Cloudflare"), ("server", "litespeed", "LiteSpeed server"),
            ("server", "nginx", "Nginx server"), ("server", "apache", "Apache server"))


def _hosting(headers: Dict[str, str]) -> str:
    for name, part, label in _HOSTING:
        v = headers.get(name)
        if v is not None and (not part or part in str(v).lower()):
            return label
    return ""


def seo_plugins(platform: dict) -> List[str]:
    """SEO plugins named in a detect_platform() result (for plugin-conflict explanations)."""
    out = []
    for p in platform.get("plugins") or []:
        base = p.split(" (")[0]
        if base in SEO_PLUGINS or "SEO" in base or "Schema" in base:
            out.append(p)
    return out


def platform_family(platform: dict) -> str:
    """'wordpress' | 'shopify' | 'magento' | 'nextjs' | '' - the key the fix-step tables use."""
    cms = (platform or {}).get("cms") or ""
    front = (platform or {}).get("frontend") or ""
    if cms == "WordPress":
        return "wordpress"
    if cms == "Shopify":
        return "shopify"
    if cms.startswith("Magento"):
        return "magento"
    if cms == "Next.js" or front == "Next.js":
        return "nextjs"
    return ""
