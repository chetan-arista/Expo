from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# User-agent presets offered in the crawl settings (Screaming Frog's "User-Agent" configuration).
USER_AGENTS: Dict[str, str] = {
    "chrome": DEFAULT_USER_AGENT,
    "googlebot_desktop": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Googlebot/2.1; "
        "+http://www.google.com/bot.html) Chrome/124.0.0.0 Safari/537.36"
    ),
    "googlebot_smartphone": (
        "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36 "
        "(compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    ),
    "bingbot": (
        "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; "
        "+http://www.bing.com/bingbot.htm) Chrome/124.0.0.0 Safari/537.36"
    ),
    # Search and AI crawlers (same strings as crawler/seo/bot_check.py): crawl the site as that bot sees it
    "gptbot": "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot",
    "chatgpt_user": ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; "
                     "+https://openai.com/bot"),
    "oai_searchbot": ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; "
                      "+https://openai.com/searchbot"),
    "claudebot": ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; "
                  "+claudebot@anthropic.com)"),
    "perplexitybot": ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; "
                      "+https://perplexity.ai/perplexitybot)"),
    "applebot": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                 "Version/17.4 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)"),
    "duckduckbot": "DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)",
    "yandexbot": "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)",
}
# robots.txt group a preset obeys (the crawler it pretends to be); presets not listed keep robots_user_agent
ROBOTS_TOKENS: Dict[str, str] = {
    "bingbot": "bingbot", "gptbot": "GPTBot", "chatgpt_user": "ChatGPT-User", "oai_searchbot": "OAI-SearchBot",
    "claudebot": "ClaudeBot", "perplexitybot": "PerplexityBot", "applebot": "Applebot",
    "duckduckbot": "DuckDuckBot", "yandexbot": "YandexBot",
}
MAX_CONCURRENCY = 50

# Common non-HTML binary media and assets that should not be parsed for links
EXCLUDED_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".tiff", ".avif",
    # Audio/Video
    ".mp4", ".mp3", ".webm", ".avi", ".mov", ".mkv", ".flv", ".wav", ".ogg",
    # Documents (we record them if linked, but don't parse them as HTML)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv", ".rtf",
    # Archives / Executables
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".dmg", ".pkg", ".iso", ".apk",
    # Code/Styles/Fonts/Feeds/APIs
    ".css", ".js", ".mjs", ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".json", ".xml", ".rss", ".atom"
}

# Tracking query parameters that pollute URLs and cause duplicate crawl cycles
STRIP_QUERY_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "dclid", "_ga", "_gl", "mc_cid", "mc_eid",
    "ref", "ref_src", "source", "share", "spm", "replytocom"
}

# Crawl scopes (top bar dropdown): the crawled host only (www-insensitive, today's default), the start URL's
# folder only, every subdomain of the site, or the start URL alone (its resources and redirects still checked)
CRAWL_SCOPES = ("subdomain", "subfolder", "all_subdomains", "exact_url")
CRAWL_MODES = ("spider", "list")
MAX_CUSTOM_RULES = 10   # custom searches / extractors / JavaScript snippets per crawl (as Screaming Frog)


def default_url_rewrite() -> Dict[str, Any]:
    """Configuration > URL Rewriting. The defaults reproduce the built-in normalisation exactly (UrlRules in
    crawler/url_utils.py): the tracking parameters above ("utm_*" = every name starting with utm_) are removed
    and the remaining parameters sorted. Rewriting applies to discovered URLs only, never to the start URL or
    to listed URLs."""
    return {
        "remove_params": sorted(STRIP_QUERY_PARAMS) + ["utm_*"],
        "remove_all_params": False,
        "regex_replace": [],          # ordered [{"regex", "replace"}]; "$1" in replace means group 1
        "lowercase": False,
        "sort_params": True,
        "percent_encoding": "upper",  # "upper" = as today (escapes the crawler adds are uppercase), "lower"
    }


def default_analysis() -> Dict[str, bool]:
    """Crawl Analysis steps. The engine honours "auto", "post_crawl_checks" and "bot_check" when it runs the
    post-crawl checks itself; the others are read by the site analysis (crawler/seo/analysis.py)."""
    return {"auto": True, "link_score": True, "sitemaps": True, "near_duplicates": True, "pagination": True,
            "hreflang": True, "post_crawl_checks": True, "bot_check": True, "semantic_similarity": True}


def default_proxy() -> Dict[str, Any]:
    return {"enabled": False, "host": "", "port": 3128, "username": "", "password": ""}


# ---------------------------------------------------------------------------------------------
# Build 4 options (PLAN4_build.md 10.7). Every default keeps the behaviour the crawler had before the option
# existed; normalize_options() puts a value of the wrong type or out of range back in its allowed range and
# validate_options() gives the plain error a form or a profile check shows.
# ---------------------------------------------------------------------------------------------
# Thresholds of the tabs (Configuration > Preferences), read through crawler.seo.registry.pref(site, key)
DEFAULT_PREFERENCES: Dict[str, Any] = {
    "title_max_chars": 60, "title_min_chars": 30, "title_max_px": 561, "title_min_px": 200,
    "desc_max_chars": 155, "desc_min_chars": 70, "desc_max_px": 985, "desc_min_px": 400,
    "url_max_chars": 115, "h1_max_chars": 70, "h2_max_chars": 70, "alt_max_chars": 100, "image_max_kb": 100,
    "low_content_words": 200, "high_internal_outlinks": 1000, "high_external_outlinks": 10,
    "high_crawl_depth": 4, "html_max_mb": 2, "resource_max_mb": 2, "bounce_rate_pct": 70,
    "carbon_rating_flag": "E",
}
PREFERENCE_RANGES: Dict[str, tuple] = {    # (lowest, highest) allowed per numeric preference
    "title_max_chars": (1, 1000), "title_min_chars": (0, 1000), "title_max_px": (1, 5000), "title_min_px": (0, 5000),
    "desc_max_chars": (1, 2000), "desc_min_chars": (0, 2000), "desc_max_px": (1, 10000), "desc_min_px": (0, 10000),
    "url_max_chars": (10, 5000), "h1_max_chars": (1, 1000), "h2_max_chars": (1, 1000), "alt_max_chars": (1, 2000),
    "image_max_kb": (1, 100_000), "low_content_words": (0, 100_000), "high_internal_outlinks": (1, 100_000),
    "high_external_outlinks": (0, 100_000), "high_crawl_depth": (1, 1000), "html_max_mb": (0.1, 100),
    "resource_max_mb": (0.1, 1000), "bounce_rate_pct": (1, 100),
}
CARBON_RATINGS = ("A+", "A", "B", "C", "D", "E", "F")
CRAWL_TYPES = ("images", "css", "js", "media", "fonts", "iframes")
FOLLOW_TYPES = ("canonicals", "pagination", "hreflang", "meta_refresh", "http_refresh", "js_redirects", "iframes",
                "mobile_alternate")
LIMIT_KEYS = ("per_depth", "folder_depth", "query_strings", "per_host")
LINK_POSITIONS = ("content", "navigation", "header", "footer", "sidebar", "breadcrumb", "form", "page_body")
LINK_POSITION_MATCH = ("class", "id", "css", "xpath")
BLOCK_ADDRESSES = ("", "metadata", "private")
COOKIE_STORAGE = ("session", "none")
RENDER_DEVICES = ("desktop", "mobile", "tablet")
MAX_LIST_ITEMS = 100                 # phrases, selectors, hosts or rules in one list option
MAX_ITEM_CHARS = 300
MAX_CUSTOM_ROBOTS_BYTES = 500 * 1024  # what Google reads of a robots.txt


def default_preferences() -> Dict[str, Any]:
    return dict(DEFAULT_PREFERENCES)


def default_content_area() -> Dict[str, List[str]]:
    return {"include": [], "exclude": []}


def default_crawl_types() -> Dict[str, bool]:
    return {k: True for k in CRAWL_TYPES}


def default_follow() -> Dict[str, bool]:
    return {k: True for k in FOLLOW_TYPES}


def default_limits() -> Dict[str, int]:
    return {k: 0 for k in LIMIT_KEYS}


def default_carbon() -> Dict[str, Any]:
    return {"enabled": True}


def default_spelling() -> Dict[str, Any]:
    return {"enabled": True, "languages": "auto", "max_errors_per_page": 50}

@dataclass
class AuthConfig:
    enabled: bool = False
    auth_type: str = "none"         # "none", "form", "cookies", "header"
    login_url: Optional[str] = None  # URL of the login page (auto-detected if blank)
    username: Optional[str] = None   # Username / email
    password: Optional[str] = None   # Password
    cookies: Optional[str] = None    # Raw cookie string (e.g. "key=val; key2=val2") or JSON
    custom_header_name: Optional[str] = None # e.g. "Authorization"
    custom_header_value: Optional[str] = None # e.g. "Bearer <token>"
    live_session_id: Optional[str] = None     # auth_type "live": session finished in the live login window
    username_selector: Optional[str] = None   # optional CSS selectors when the login form is unusual
    password_selector: Optional[str] = None
    submit_selector: Optional[str] = None

@dataclass
class CrawlConfig:
    start_url: str = ""
    max_urls: int = 0               # 0 = Fully Automatic Unlimited (crawls until queue is exhausted)
    max_depth: int = 0              # Max clicks from the start URL (0 = no limit); P2: the true click depth
                                    # decides, whatever order parallel workers find the links in
    concurrency: int = 6            # Parallel worker threads (1..MAX_CONCURRENCY)
    max_urls_per_second: float = 0.0 # Speed limit for requests to the crawled site (0 = unlimited)
    timeout: int = 15               # Network timeout in seconds
    max_retries: int = 3            # Auto-retries of 500/502/504 answers and failed connects (never 403/429)
    delay: float = 0.0              # Polite delay (auto 0.0 for speed)
    use_sitemaps: bool = True       # Automatically probe robots.txt and XML sitemaps in background
    use_dynamic_js: bool = True     # Automatically selectively use headless browser for dynamic/SPA pages
    enable_spa_mode: bool = False   # Discover client-side routes from JS bundles on React/Vue/Vite SPAs
    verify_ssl: bool = False        # Tolerant of misconfigured SSL certificates
    include_subdomains: bool = False # Retain main domain boundary
    allowed_domains: List[str] = field(default_factory=list)
    # Regex filters on internal URLs (re.search). Include: when non-empty, only matching URLs are
    # crawled (the start URL is always allowed). Exclude: matching URLs are never crawled.
    include_patterns: List[str] = field(default_factory=list)
    exclude_regex: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=lambda: [
        "/logout", "/signout", "/sign-out", "/auth/logout",
        "/wp-json", "wp-json", "/xmlrpc.php", "wp-login.php",
        "/feed", "?feed=", "?rest_route=", "oembed", "/cgi-bin/"
    ])
    respect_robots: bool = True     # Skip URLs robots.txt disallows (reported as "Blocked by robots.txt")
    robots_user_agent: str = "Googlebot" # robots.txt group to obey: what Google may crawl is what matters for SEO
    collect_external_links: bool = True # Automatically collect outbound links without following them
    validate_external_links: bool = False # Legacy switch; same effect as check_external_links
    check_external_links: bool = True # HEAD (GET fallback) every unique external link for its status code
    crawl_resources: bool = True     # Also check internal images, scripts, stylesheets and iframes
    # Per-page limits (Screaming Frog's "Max Page Size" / "Max Links per URL"): a huge page is read only
    # up to this many KB (decompressed) and only its first links are kept, so one page cannot stall the
    # crawl or exhaust memory (parsing takes roughly 70x the HTML size in RAM at its peak, so 5 MB keeps
    # a small 512 MB container safe). 0 = no limit.
    max_page_size_kb: int = 5120
    max_links_per_page: int = 10000
    # Network guards (crawler/net_guard.py). Adaptive throttle: a 429/503 answer pauses the host (Retry-After
    # honoured), slows it down and puts the URL back in the queue (at most 3 times); firewall / bot-challenge
    # answers slow it down hard; normal answers bring the speed back gradually.
    adaptive_throttle: bool = True
    # Crawl traps: URLs with a folder repeated 3+ times, endless calendar pages, page-builder pagination,
    # session ids or more than max_url_length characters are skipped (logged and counted, not errors)
    skip_crawl_traps: bool = True
    max_url_length: int = 2000
    # After the crawl (crawler/seo/postcrawl.py): robots.txt audit, URL-trap probe and the AI/search-bot
    # readability check on a sample of bot_check_sample pages
    post_crawl_checks: bool = True
    bot_check_sample: int = 10
    # Snapshot of every finished crawl for crawl-to-crawl comparison (crawler/seo/compare.py), written under
    # <reports_dir>/snapshots/<host>/ ("" = env CRAWLER_REPORTS_DIR, else the system temp folder)
    save_snapshot: bool = True
    reports_dir: str = ""
    auth: AuthConfig = field(default_factory=AuthConfig) # Optional authentication credentials
    user_agent: str = DEFAULT_USER_AGENT  # effective User-Agent string sent with every request
    user_agent_preset: str = "chrome"     # key of USER_AGENTS, or "custom" to send user_agent as given
    output_file: Optional[str] = None
    # Crawl scope (CRAWL_SCOPES): "all_subdomains" is the same as include_subdomains=True (kept in sync)
    crawl_scope: str = "subdomain"
    # Mode: "spider" follows links from start_url; "list" crawls exactly seed_urls (depth 0: their links are
    # recorded, not followed; redirects are followed so chains resolve; resources checked when crawl_resources)
    mode: str = "spider"
    seed_urls: List[str] = field(default_factory=list)
    # Find subdomains (crawler/subdomains.py): other hosts of the site's registrable domain to crawl too, as bare
    # names ("blog.example.com") or home URLs ("http://blog.example.com/"). Spider mode only: each host's home page
    # is an extra start URL and the host is in scope whatever crawl_scope is (Exact URL: only its home page).
    # Hosts of other domains, without DNS or on a private network address are left out. List mode ignores it.
    extra_hosts: List[str] = field(default_factory=list)
    list_max_depth: int = 0           # list mode: clicks followed from the listed URLs (0 = only the list)
    url_rewrite: Dict[str, Any] = field(default_factory=default_url_rewrite)
    # Extra HTTP headers ([{"name", "value"}]) sent to the crawled site only (never to other sites); values are
    # never logged
    request_headers: List[Dict[str, str]] = field(default_factory=list)
    # Custom Search: [{"name", "mode": "contains"|"not_contains", "text", "regex": bool, "where": "html"|"text",
    # "case_sensitive": bool}] -> PageData custom_search {name: occurrences}; at most MAX_CUSTOM_RULES
    custom_searches: List[Dict[str, Any]] = field(default_factory=list)
    # Custom Extraction: [{"name", "type": "xpath"|"css"|"regex", "expr", "extract": "text"|"inner_html"|
    # "element"|"attribute", "attr"}] -> PageData custom_extraction {name: [values]}; at most MAX_CUSTOM_RULES
    custom_extractors: List[Dict[str, Any]] = field(default_factory=list)
    # Custom JavaScript: [{"name", "code", "type": "extraction"|"action"}], run only when render_all is on
    custom_js: List[Dict[str, Any]] = field(default_factory=list)
    render_all: bool = False          # render EVERY internal HTML page in Chrome (slower) -> PageData "rendered"
    store_source: bool = True         # keep each page's raw HTML (gzip, on disk) for View Source; off on serverless
    analysis: Dict[str, bool] = field(default_factory=default_analysis)
    proxy: Dict[str, Any] = field(default_factory=default_proxy)   # filled from the server settings
    # siteone-crawler's loop guard: once 5 URLs ending in the same path segment answered 4xx/5xx, further URLs
    # ending in it that are linked only from error pages are skipped (listed as skipped). A link on a page that
    # loaded is always crawled, so every broken link is reported
    loop_guard: bool = True
    # ---- Build 4 options (PLAN4_build.md 10.7; the task that reads each one is named after it) ----
    max_redirects: int = 10            # P4: hops followed in one redirect chain before "Redirect limit reached"
    custom_robots_txt: str = ""        # P3: replaces the live robots.txt for this crawl ("" = the live file)
    sitemap_urls: List[str] = field(default_factory=list)   # P9: extra XML sitemap addresses to read
    max_sitemap_files: int = 100       # P9: sitemap files read per crawl
    max_sitemap_only_urls: int = 1000  # P9: URLs found only in sitemaps that are crawled (0 = no limit)
    max_sitemap_index_urls: int = 100_000   # P9: URLs the sitemap index keeps (past it: "Not checked")
    cdn_hosts: List[str] = field(default_factory=list)      # P11: hosts whose images / files count as internal
    near_dup_threshold: float = 90.0   # P13: near-duplicate similarity in % (50-100)
    content_area: Dict[str, List[str]] = field(default_factory=default_content_area)   # P13: CSS selectors
    preferences: Dict[str, Any] = field(default_factory=default_preferences)           # P14 (registry.pref)
    soft_404_phrases: List[str] = field(default_factory=list)         # P14: added to the built-in list
    non_descriptive_anchors: List[str] = field(default_factory=list)  # P14: added to the built-in list
    link_positions: List[Dict[str, str]] = field(default_factory=list)  # P16: [{"position", "match", "value"}]
    crawl_types: Dict[str, bool] = field(default_factory=default_crawl_types)          # P17
    follow: Dict[str, bool] = field(default_factory=default_follow)                    # P17
    follow_internal_nofollow: bool = True      # P17
    list_follow_canonicals: bool = False       # P17
    cookie_storage: str = "session"            # P17: "session" | "none"
    assume_html: bool = False                  # P17: no Content-Type + parsed as HTML -> HTML
    ignore_non_indexable_issues: bool = False  # P17: on-page issue filters count indexable pages only
    limits: Dict[str, int] = field(default_factory=default_limits)    # P17: 0 = no limit
    block_addresses: str = ""                  # P24: "" | "metadata" | "private" (agent crawls, Render)
    carbon: Dict[str, Any] = field(default_factory=default_carbon)    # P18
    spelling: Dict[str, Any] = field(default_factory=default_spelling)   # C1 (called from extract.py)
    a11y_audit: bool = False                   # A2: axe-core audit of pages that are not rendered anyway
    a11y_audit_max_pages: int = 500
    a11y_audit_max_minutes: int = 30
    render_device: str = "desktop"             # A5: "desktop" | "mobile" | "tablet"
    render_timeout: float = 8.0                # A5: seconds a render waits for late content (2-30)
    flatten_shadow_dom: bool = False           # A5
    flatten_iframes: bool = False              # A5

    def __post_init__(self):
        self.user_agent = effective_user_agent(self.user_agent_preset, self.user_agent)
        token = ROBOTS_TOKENS.get((self.user_agent_preset or "").lower())
        if token and self.robots_user_agent == "Googlebot":
            self.robots_user_agent = token  # obey the robots.txt group of the crawler we pretend to be
        sync_scope(self)
        normalize_options(self)


def sync_scope(config: "CrawlConfig") -> None:
    """Keeps crawl_scope and the older include_subdomains switch in agreement (both are read elsewhere) and
    makes mode one of CRAWL_MODES."""
    scope = (getattr(config, "crawl_scope", "") or "subdomain").strip().lower()
    if scope not in CRAWL_SCOPES:
        scope = "subdomain"
    if scope == "all_subdomains":
        config.include_subdomains = True
    elif scope == "subdomain" and config.include_subdomains:
        scope = "all_subdomains"
    config.crawl_scope = scope
    mode = (getattr(config, "mode", "") or "spider").strip().lower()
    config.mode = mode if mode in CRAWL_MODES else "spider"


def effective_user_agent(preset: str, user_agent: str = "") -> str:
    """The User-Agent string to send: a preset wins unless the caller set a custom string."""
    preset = (preset or "chrome").strip().lower()
    if preset == "custom":
        return (user_agent or "").strip() or DEFAULT_USER_AGENT
    if preset in USER_AGENTS and (not user_agent or user_agent in USER_AGENTS.values()):
        return USER_AGENTS[preset]
    return user_agent or DEFAULT_USER_AGENT


# ---------------------------------------------------------------------------------------------
# Build 4 options: shape and range checks
# ---------------------------------------------------------------------------------------------
def _num(value: Any, default: float, lo: float, hi: float, as_int: bool = True) -> Any:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    if n != n:                     # NaN
        return default
    n = min(max(n, lo), hi)
    return int(n) if as_int else n


def _str_list(value: Any, limit: int = MAX_LIST_ITEMS, chars: int = MAX_ITEM_CHARS) -> List[str]:
    """A list of short non-empty strings (a newline-separated string is split into lines)."""
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for v in value:
        s = str(v if v is not None else "").strip()
        if s and s not in out:
            out.append(s[:chars])
        if len(out) >= limit:
            break
    return out


def _bool_map(value: Any, keys: tuple, default: bool) -> Dict[str, bool]:
    base = {k: default for k in keys}
    if isinstance(value, dict):
        for k in keys:
            if k in value:
                base[k] = bool(value[k])
    return base


def clean_preferences(value: Any) -> Dict[str, Any]:
    """The preferences with every known key (defaults for missing or unusable values); unknown keys dropped."""
    out = default_preferences()
    if not isinstance(value, dict):
        return out
    for key, default in DEFAULT_PREFERENCES.items():
        if key not in value or value[key] in (None, ""):
            continue
        if key == "carbon_rating_flag":
            v = str(value[key]).strip().upper()
            out[key] = v if v in CARBON_RATINGS else default
            continue
        lo, hi = PREFERENCE_RANGES[key]
        out[key] = _num(value[key], default, lo, hi, as_int=isinstance(default, int))
    return out


def clean_link_positions(value: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for rule in value if isinstance(value, list) else []:
        if not isinstance(rule, dict):
            continue
        position = str(rule.get("position") or "").strip().lower()
        match = str(rule.get("match") or "").strip().lower()
        text = str(rule.get("value") or "").strip()[:MAX_ITEM_CHARS]
        if position in LINK_POSITIONS and match in LINK_POSITION_MATCH and text:
            out.append({"position": position, "match": match, "value": text})
        if len(out) >= MAX_LIST_ITEMS:
            break
    return out


def normalize_options(config: Any) -> None:
    """Puts every Build 4 option of `config` in its allowed shape and range, in place (a missing attribute gets
    its default). Called by CrawlConfig.__post_init__ and again by the engine, so values set after construction
    are cleaned too. Defaults keep the behaviour the crawler had before the option existed."""
    def get(name: str, default: Any) -> Any:
        return getattr(config, name, default)

    config.max_redirects = _num(get("max_redirects", 10), 10, 1, 50)
    robots = get("custom_robots_txt", "") or ""
    config.custom_robots_txt = str(robots)[:MAX_CUSTOM_ROBOTS_BYTES] if isinstance(robots, str) else ""
    config.sitemap_urls = [u for u in _str_list(get("sitemap_urls", []), 50, 2000)
                           if u.lower().startswith(("http://", "https://"))]
    config.max_sitemap_files = _num(get("max_sitemap_files", 100), 100, 1, 1000)
    config.max_sitemap_only_urls = _num(get("max_sitemap_only_urls", 1000), 1000, 0, 10_000_000)
    config.max_sitemap_index_urls = _num(get("max_sitemap_index_urls", 100_000), 100_000, 1, 10_000_000)
    config.cdn_hosts = [h.lower().split("://", 1)[-1].split("/", 1)[0]
                        for h in _str_list(get("cdn_hosts", []), 50, 253)]
    config.near_dup_threshold = _num(get("near_dup_threshold", 90.0), 90.0, 50.0, 100.0, as_int=False)
    area = get("content_area", None)
    area = area if isinstance(area, dict) else {}
    config.content_area = {"include": _str_list(area.get("include"), 20),
                           "exclude": _str_list(area.get("exclude"), 20)}
    config.preferences = clean_preferences(get("preferences", None))
    config.soft_404_phrases = _str_list(get("soft_404_phrases", []), MAX_LIST_ITEMS, 200)
    config.non_descriptive_anchors = _str_list(get("non_descriptive_anchors", []), MAX_LIST_ITEMS, 200)
    config.link_positions = clean_link_positions(get("link_positions", []))
    config.crawl_types = _bool_map(get("crawl_types", None), CRAWL_TYPES, True)
    config.follow = _bool_map(get("follow", None), FOLLOW_TYPES, True)
    config.follow_internal_nofollow = bool(get("follow_internal_nofollow", True))
    config.list_follow_canonicals = bool(get("list_follow_canonicals", False))
    storage = str(get("cookie_storage", "session") or "session").strip().lower()
    config.cookie_storage = storage if storage in COOKIE_STORAGE else "session"
    config.assume_html = bool(get("assume_html", False))
    config.ignore_non_indexable_issues = bool(get("ignore_non_indexable_issues", False))
    limits = get("limits", None)
    limits = limits if isinstance(limits, dict) else {}
    config.limits = {k: _num(limits.get(k, 0), 0, 0, 10_000_000) for k in LIMIT_KEYS}
    block = str(get("block_addresses", "") or "").strip().lower()
    config.block_addresses = block if block in BLOCK_ADDRESSES else ""
    carbon = get("carbon", None)
    config.carbon = {"enabled": bool(carbon.get("enabled", True)) if isinstance(carbon, dict) else True}
    spelling = get("spelling", None)
    spelling = spelling if isinstance(spelling, dict) else {}
    langs = spelling.get("languages", "auto")
    if isinstance(langs, (list, tuple)):
        langs = [str(x).strip().lower()[:10] for x in langs if str(x).strip()][:10] or "auto"
    else:
        langs = str(langs or "auto").strip().lower()[:100] or "auto"
    config.spelling = {"enabled": bool(spelling.get("enabled", True)), "languages": langs,
                       "max_errors_per_page": _num(spelling.get("max_errors_per_page", 50), 50, 1, 500)}
    config.a11y_audit = bool(get("a11y_audit", False))
    config.a11y_audit_max_pages = _num(get("a11y_audit_max_pages", 500), 500, 1, 5000)
    config.a11y_audit_max_minutes = _num(get("a11y_audit_max_minutes", 30), 30, 1, 240)
    device = str(get("render_device", "desktop") or "desktop").strip().lower()
    config.render_device = device if device in RENDER_DEVICES else "desktop"
    config.render_timeout = _num(get("render_timeout", 8.0), 8.0, 2.0, 30.0, as_int=False)
    config.flatten_shadow_dom = bool(get("flatten_shadow_dom", False))
    config.flatten_iframes = bool(get("flatten_iframes", False))


def _range_error(value: Any, lo: float, hi: float) -> str:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "Must be a number."
    if n != n or n < lo or n > hi:
        return f"Must be between {lo:,g} and {hi:,g}."
    return ""


OPTION_RANGES: Dict[str, tuple] = {
    "max_redirects": (1, 50), "max_sitemap_files": (1, 1000), "max_sitemap_only_urls": (0, 10_000_000),
    "max_sitemap_index_urls": (1, 10_000_000), "near_dup_threshold": (50, 100), "a11y_audit_max_pages": (1, 5000),
    "a11y_audit_max_minutes": (1, 240), "render_timeout": (2, 30),
}


def selector_error(value: str, kind: str = "css") -> bool:
    """True when `value` is not a valid CSS selector (kind "css") or XPath (kind "xpath")."""
    try:
        if kind == "xpath":
            from lxml import etree
            etree.XPath(value)
        else:
            import soupsieve
            soupsieve.compile(value)
        return False
    except Exception:
        return True


def validate_options(options: Any) -> Dict[str, str]:
    """Plain errors for the Build 4 options in a crawl request or profile ({} = valid; unknown and absent
    options are not checked). settings_store.validate_profile and the crawl-start route can merge these."""
    e: Dict[str, str] = {}
    if not isinstance(options, dict):
        return e

    def has(name: str) -> bool:
        return name in options and options[name] not in (None, "")

    for name, (lo, hi) in OPTION_RANGES.items():
        if has(name):
            why = _range_error(options[name], lo, hi)
            if why:
                e[name] = why
    if has("custom_robots_txt"):
        if not isinstance(options["custom_robots_txt"], str):
            e["custom_robots_txt"] = "Must be the text of a robots.txt file."
        elif len(options["custom_robots_txt"].encode("utf-8", "ignore")) > MAX_CUSTOM_ROBOTS_BYTES:
            e["custom_robots_txt"] = "The robots.txt text is over 500 KB (Google reads only the first 500 KB)."
    if has("sitemap_urls"):
        urls = _str_list(options["sitemap_urls"], 10_000, 5000)
        bad = [u for u in urls if not u.lower().startswith(("http://", "https://"))]
        if bad:
            e["sitemap_urls"] = f"Not a web address: {bad[0][:80]} (start it with https://)."
        elif len(urls) > 50:
            e["sitemap_urls"] = "At most 50 sitemap addresses."
    for name, limit in (("cdn_hosts", 50), ("soft_404_phrases", MAX_LIST_ITEMS),
                        ("non_descriptive_anchors", MAX_LIST_ITEMS)):
        if has(name):
            if not isinstance(options[name], (list, str)):
                e[name] = "Must be a list (one entry per line)."
            elif len(_str_list(options[name], 10_000, 10_000)) > limit:
                e[name] = f"At most {limit} entries."
    area = options.get("content_area")
    if area not in (None, ""):
        if not isinstance(area, dict):
            e["content_area"] = "Must have 'include' and 'exclude' lists of CSS selectors."
        else:
            for k in ("include", "exclude"):
                values = _str_list(area.get(k), 10_000, 10_000)
                if len(values) > 20:
                    e["content_area"] = f"At most 20 '{k}' selectors."
                    break
                bad = next((v for v in values if selector_error(v, "css")), "")
                if bad:
                    e["content_area"] = f"'{bad[:80]}' is not a valid CSS selector ({k})."
                    break
    prefs = options.get("preferences")
    if prefs not in (None, ""):
        if not isinstance(prefs, dict):
            e["preferences"] = "Must be an object of thresholds."
        else:
            for key, value in prefs.items():
                if key not in DEFAULT_PREFERENCES or value in (None, ""):
                    continue
                if key == "carbon_rating_flag":
                    if str(value).strip().upper() not in CARBON_RATINGS:
                        e["preferences"] = ("The carbon rating to flag must be one of "
                                            + ", ".join(CARBON_RATINGS) + ".")
                    continue
                why = _range_error(value, *PREFERENCE_RANGES[key])
                if why:
                    e["preferences"] = f"{key.replace('_', ' ')}: {why}"
    rules = options.get("link_positions")
    if rules not in (None, ""):
        if not isinstance(rules, list):
            e["link_positions"] = "Must be a list of rules."
        else:
            for i, rule in enumerate(rules):
                r = rule if isinstance(rule, dict) else {}
                if str(r.get("position") or "").lower() not in LINK_POSITIONS:
                    e["link_positions"] = (f"Rule {i + 1}: the position must be one of "
                                           + ", ".join(LINK_POSITIONS) + ".")
                elif str(r.get("match") or "").lower() not in LINK_POSITION_MATCH:
                    e["link_positions"] = f"Rule {i + 1}: match by class, id, css or xpath."
                elif not str(r.get("value") or "").strip():
                    e["link_positions"] = f"Rule {i + 1}: the value is empty."
                elif str(r.get("match") or "").lower() in ("css", "xpath") and \
                        selector_error(str(r.get("value")), str(r.get("match")).lower()):
                    e["link_positions"] = (f"Rule {i + 1}: '{str(r.get('value'))[:80]}' is not a valid "
                                           f"{'CSS selector' if str(r.get('match')).lower() == 'css' else 'XPath'}.")
                if "link_positions" in e:
                    break
            if "link_positions" not in e and len(rules) > MAX_LIST_ITEMS:
                e["link_positions"] = f"At most {MAX_LIST_ITEMS} rules."
    for name, keys in (("crawl_types", CRAWL_TYPES), ("follow", FOLLOW_TYPES)):
        v = options.get(name)
        if v not in (None, "") and (not isinstance(v, dict) or any(k not in keys for k in v)):
            e[name] = "Unknown option; use " + ", ".join(keys) + "."
    lim = options.get("limits")
    if lim not in (None, ""):
        if not isinstance(lim, dict) or any(k not in LIMIT_KEYS for k in lim):
            e["limits"] = "Unknown limit; use " + ", ".join(LIMIT_KEYS) + "."
        else:
            for k, v in lim.items():
                why = _range_error(v, 0, 10_000_000) if v not in (None, "") else ""
                if why:
                    e["limits"] = f"{k.replace('_', ' ')}: {why}"
    for name, allowed in (("cookie_storage", COOKIE_STORAGE), ("block_addresses", BLOCK_ADDRESSES),
                          ("render_device", RENDER_DEVICES)):
        if name in options and options[name] is not None and str(options[name]).strip().lower() not in allowed:
            e[name] = "Must be one of " + ", ".join(repr(a) for a in allowed) + "."
    sp = options.get("spelling")
    if sp not in (None, ""):
        if not isinstance(sp, dict):
            e["spelling"] = "Must be an object (enabled, languages, max_errors_per_page)."
        elif sp.get("max_errors_per_page") not in (None, ""):
            why = _range_error(sp["max_errors_per_page"], 1, 500)
            if why:
                e["spelling"] = f"max errors per page: {why}"
    if options.get("carbon") not in (None, "") and not isinstance(options.get("carbon"), dict):
        e["carbon"] = "Must be an object with 'enabled'."
    e.update(_custom_rule_errors(options))
    return e


SEARCH_WHERE = ("html", "text", "head", "body", "text_no_anchors", "element")   # P15 (seo.extract.SEARCH_WHERE)


def _custom_rule_errors(options: Dict[str, Any]) -> Dict[str, str]:
    """P15: a Custom Search 'where' we know (element with a selector that compiles), and Custom JavaScript snippet
    names that are unique (a value is stored under its snippet's name)."""
    e: Dict[str, str] = {}
    searches = options.get("custom_searches")
    if isinstance(searches, list):
        for i, rule in enumerate(searches, 1):
            if not isinstance(rule, dict):
                continue
            label = str(rule.get("name") or f"Search {i}")[:80]
            where = str(rule.get("where") or "html").strip().lower().replace(" ", "_")
            where = {"page_text": "text", "page_text_no_anchors": "text_no_anchors"}.get(where, where)
            if where not in SEARCH_WHERE:
                e["custom_searches"] = (f"Custom search '{label}': 'where' must be one of "
                                        + ", ".join(SEARCH_WHERE) + ".")
                break
            if where == "element":
                from .seo.extract import compile_selector
                compiled, why = compile_selector(rule.get("selector"))
                if compiled is None:
                    e["custom_searches"] = f"Custom search '{label}' searches in an element but {why}."
                    break
    snippets = options.get("custom_js")
    if isinstance(snippets, list):
        seen: Dict[str, int] = {}
        for i, s in enumerate(snippets, 1):
            if not isinstance(s, dict) or not str(s.get("code") or "").strip():
                continue
            name = " ".join(str(s.get("name") or f"Snippet {i}").split())[:80]
            if name.lower() in seen:
                e["custom_js"] = (f"Two Custom JavaScript snippets are called '{name}'. Give each snippet its own "
                                  "name: its value is stored under that name.")
                break
            seen[name.lower()] = i
    return e
