from dataclasses import dataclass, field
from typing import List, Optional, Dict

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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

@dataclass
class CrawlConfig:
    start_url: str = ""
    max_urls: int = 0               # 0 = Fully Automatic Unlimited (crawls until queue is exhausted)
    max_depth: int = 50             # Effectively unconstrained recursive depth
    concurrency: int = 6            # Automatically optimized worker concurrency
    timeout: int = 15               # Network timeout in seconds
    max_retries: int = 3            # Auto-retries on connection/read timeout
    delay: float = 0.0              # Polite delay (auto 0.0 for speed)
    use_sitemaps: bool = True       # Automatically probe robots.txt and XML sitemaps in background
    use_dynamic_js: bool = True     # Automatically selectively use headless browser for dynamic/SPA pages
    enable_spa_mode: bool = False   # Discover client-side routes from JS bundles on React/Vue/Vite SPAs
    verify_ssl: bool = False        # Tolerant of misconfigured SSL certificates
    include_subdomains: bool = False # Retain main domain boundary
    allowed_domains: List[str] = field(default_factory=list)
    exclude_patterns: List[str] = field(default_factory=lambda: [
        "/logout", "/signout", "/sign-out", "/auth/logout",
        "/wp-json", "wp-json", "/xmlrpc.php", "wp-login.php",
        "/feed", "?feed=", "?rest_route=", "oembed", "/cgi-bin/"
    ])
    collect_external_links: bool = True # Automatically collect outbound links without following them
    validate_external_links: bool = False # Lightweight HEAD validation of external links
    auth: AuthConfig = field(default_factory=AuthConfig) # Optional authentication credentials
    user_agent: str = DEFAULT_USER_AGENT
    output_file: Optional[str] = None
