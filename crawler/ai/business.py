"""
Business Insights: who is behind a website, what they sell and to whom, the tech they run on, how to
reach them, and what the site's SEO state means for the business.

collect_site_facts(url, crawler=None, session=None) gathers facts from the site itself: from the
finished crawl when there is one (pages, titles, health score, issues, sitemap, outbound links), plus
the HTML of the home page and up to 8 key pages (about, contact, services/products, pricing, careers)
fetched without login cookies, for schema.org data, contacts, social links and technology fingerprints.

build_business_report(url, crawler=None, progress=None) -> BusinessReport dict (see web/app.py API
contract). The rule-based report from site facts is always built first; with AI on, research() (free
evidence: own pages + Wikipedia/Wikidata) and one ask_json() call rewrite the descriptive parts, and
every factual field the AI fills (founded, size, headquarters, legal name) must be backed by the
evidence or the site value is kept. Without AI (or out of quota) the rule-based report is returned with
"ai_used": false and a note saying why.
"""
import json
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from . import client as ai

log = logging.getLogger("crawler.ai.business")

try:   # optional: country names for a country-code TLD
    import pycountry  # type: ignore
except Exception:  # pragma: no cover
    pycountry = None

try:
    from ..config import DEFAULT_USER_AGENT
except Exception:  # pragma: no cover
    DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"

MAX_KEY_PAGES = 8
MAX_LIST = 6
FETCH_TIMEOUT = (6, 15)
MAX_BODY = 3_000_000
TEXT_LIMIT = 6000

# kind -> path segments that name it (matched against the first two path segments)
KEY_PAGE_WORDS = {
    "about": ("about", "about-us", "aboutus", "company", "who-we-are", "our-story", "our-company", "team"),
    "contact": ("contact", "contact-us", "contactus", "get-in-touch", "reach-us", "locations"),
    "services": ("services", "service", "solutions", "what-we-do", "capabilities", "expertise"),
    "products": ("products", "product", "shop", "collections", "store", "catalog", "catalogue"),
    "pricing": ("pricing", "plans", "packages", "price"),
    "careers": ("careers", "career", "jobs", "join-us", "work-with-us", "hiring"),
}
OFFERING_ROOTS = ("services", "service", "solutions", "products", "collections", "what-we-do", "capabilities",
                  "industries", "platforms")
BLOG_WORDS = ("blog", "blogs", "news", "insights", "articles", "resources", "journal")
GENERIC_HEADINGS = {
    "home", "about", "about us", "contact", "contact us", "read more", "learn more", "view all", "services",
    "our services", "testimonials", "faq", "faqs", "blog", "why choose us", "get in touch", "our team", "careers",
    "products", "our products", "shop", "menu", "search", "cart", "login", "sign in", "subscribe", "newsletter",
    "follow us", "quick links", "useful links", "company", "resources", "privacy policy", "terms", "sitemap",
    "solutions", "industries", "case studies", "portfolio", "our work", "we are", "who we are", "what we do",
}
SOCIAL_HOSTS = {
    "facebook.com": "Facebook", "instagram.com": "Instagram", "linkedin.com": "LinkedIn", "twitter.com": "X",
    "x.com": "X", "youtube.com": "YouTube", "tiktok.com": "TikTok", "pinterest.com": "Pinterest",
    "github.com": "GitHub", "threads.net": "Threads", "medium.com": "Medium", "behance.net": "Behance",
    "dribbble.com": "Dribbble", "clutch.co": "Clutch", "glassdoor.com": "Glassdoor", "crunchbase.com": "Crunchbase",
    "wa.me": "WhatsApp", "t.me": "Telegram", "trustpilot.com": "Trustpilot", "g2.com": "G2", "upwork.com": "Upwork",
    "goodfirms.co": "GoodFirms",
}
SHARE_PATH = re.compile(r"sharer|/share|intent/|shareArticle|/plugins/|dialog/|/hashtag/|/search", re.I)
LEGAL_RE = re.compile(r"([A-Z][\w&.,' -]{1,60}?\s(?:Pvt\.?\s*Ltd\.?|Private Limited|Ltd\.?|Limited|LLC|L\.L\.C\.|"
                      r"Inc\.?|Corp\.?|Corporation|GmbH|LLP|PLC|Pty\.? Ltd\.?|S\.A\.|B\.V\.|Co\.))")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
BAD_EMAIL = re.compile(r"\.(png|jpe?g|gif|webp|svg|css|js)$|example\.|sentry|wixpress|@2x|yourdomain|domain\.com|"
                       r"email\.com|u00", re.I)
# a number never runs across a line break (footer lines are joined with "\n": "+1 555 0100" / "123 Trail Rd")
_PH_SEP = r"(?:[^\S\r\n]|[.-])"
PHONE_INTL = re.compile(r"\+\d{1,3}" + _PH_SEP + r"?\(?\d{1,4}\)?(?:" + _PH_SEP + r"?\d{2,5}){2,4}")
PHONE_LABEL = re.compile(r"\b(?:phone|telephone|tel|call us|call|mobile|whatsapp|ph)\b\s*(?:no\.?|number)?\s*"
                         r"[:.\-]?\s*(\+?\(?\d(?:[\d().-]|[^\S\r\n]){7,18}\d)", re.I)
# Founding year and head count from the pages' own words. A year or number counts only when the sentence is
# about the company itself ("Founded in 2008", "We have been making boots since 1952", "a team of 45
# developers"), never a product or customer claim ("a bestseller since 2021", "Trusted by 12,000+ professionals").
_YEAR = r"((?:18|19|20)\d{2})"
FOUNDED_RE = re.compile(r"\b(?:founded|established|incorporated|est\.?|estd\.?)\s*(?:in\s+)?" + _YEAR + r"\b", re.I)
SINCE_RE = re.compile(r"\bsince\s+" + _YEAR + r"\b", re.I)
STARTED_RE = re.compile(r"\b(?:started|began)\s+(?:(?:out|back|off)\s+)?(?:in\s+)?" + _YEAR + r"\b", re.I)
# the company as the subject of the sentence
OWN_SUBJECT_RE = re.compile(r"\b(?:we|we've|we're|we have|we are|us|it all|the (?:company|business|firm|agency)|"
                            r"our (?:company|business|firm|agency|studio|family|founders?|story|journey|doors))\b", re.I)
# what a company does "since <year>" (a product is not "in business" or "serving clients")
ACTIVITY_RE = re.compile(r"\b(?:in business|in operation|in the (?:industry|business|trade)|around|serving|trading|"
                         r"operating|family[- ](?:owned|run)|independently owned|helping|building|making|crafting|"
                         r"designing|creating|developing|providing|delivering|manufacturing|supplying|working with)\b",
                         re.I)
# product / customer claims: a "since" or a head count next to these is marketing, not a company fact
CLAIM_RE = re.compile(r"\b(?:best[- ]?sell\w*|favou?rites?|sold out|top[- ]rated|rated|awards?|award[- ]winning|"
                      r"reviews?|trusted by|loved by|used by|chosen by|recommended|popular|in stock|prices?|"
                      r"join|clients? (?:love|trust|say))\b", re.I)
_SIZE_NUM = r"(\d{1,3}(?:,\d{3})+|\d+)\s*\+?"
WORKFORCE_NOUNS = r"(?:employees|team members|staff members|staff|people on (?:our|the) team)"
SKILL_NOUNS = r"(?:professionals|experts|engineers|developers|designers|specialists|consultants)"
SIZE_RE = re.compile(r"\b" + _SIZE_NUM + r"(?:\s+[A-Za-z][\w-]*){0,2}?\s+(" + WORKFORCE_NOUNS + "|" + SKILL_NOUNS +
                     r")\b", re.I)
# right before the number: words that make the people the company's own ("a team of 45", "our 120", "we employ 30")
_FILLER = r"(?:\s+(?:over|more than|about|around|nearly|almost|roughly|close to|some|a|of|than))*"
OWN_TEAM_RE = re.compile(r"\b(?:(?:a |our |the )?team of|staff of|workforce of|headcount of|crew of|family of|our|"
                         r"we have|we've got|we employ|employs|employing|we are|we're|with a team of)" + _FILLER +
                         r"\s*$", re.I)
OWN_HAVE_RE = re.compile(r"\b(?:has|have|with|now)" + _FILLER + r"\s*$", re.I)   # workforce nouns only
FACT_CLAIM_RE = re.compile(r"\b(?:founded|established|incorporated|since \d{4}|employees|head ?count|team of|"
                           r"staff of|company size|size)\b", re.I)   # an AI source line about founding / size
CUSTOMER_AFTER_RE = re.compile(r"^\s*(?:at|in|from)\s+(?:other|many|leading|top|companies|businesses|brands|retailers|"
                               r"organi[sz]ations|enterprises|salons|stores|fortune)|^\s*(?:who\s+|that\s+)?(?:use|trust|"
                               r"love|rely|choose|recommend|rate|swear|agree|shop|buy)\b", re.I)
FREE_MAIL = ("gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "rediffmail.com", "aol.com", "icloud.com")

# Plain wording hints (own short lists): industry -> phrases, audience phrases, market regions
INDUSTRY_HINTS = {
    "IT & digital services": ("web development", "website development", "software development", "app development",
                              "it services", "shopify development", "wordpress development", "offshore team",
                              "digital agency", "it professionals", "outsourcing"),
    "Digital marketing": ("digital marketing", "seo services", "ppc", "social media marketing", "email marketing",
                          "performance marketing"),
    "E-commerce / retail": ("add to cart", "shop now", "free shipping", "checkout", "best sellers", "new arrivals"),
    "Healthcare": ("patients", "clinic", "hospital", "healthcare", "doctor"),
    "Education": ("students", "courses", "admissions", "curriculum", "university"),
    "Real estate": ("real estate", "property", "apartments", "listings", "realtor"),
    "Finance": ("loans", "insurance", "investment", "banking", "accounting services"),
    "Travel & hospitality": ("hotel", "booking", "tours", "travel", "resort"),
    "Food & beverage": ("restaurant", "menu", "recipes", "catering", "coffee"),
    "Manufacturing": ("manufacturer", "manufacturing", "factory", "industrial", "oem"),
}
AUDIENCE_TERMS = (
    "startups", "small businesses", "small and medium businesses", "SMBs", "SMEs", "enterprises", "e-commerce brands",
    "ecommerce brands", "online stores", "online sellers", "Amazon sellers", "retailers", "D2C brands", "B2B companies",
    "agencies", "manufacturers", "distributors", "healthcare providers", "patients", "students", "homeowners",
    "developers", "marketers", "US businesses", "US brands", "nonprofits", "restaurants", "law firms", "shoppers",
    "merchants", "brands",
)
MARKETS = (
    ("United States", r"\bUnited States\b|\bUSA\b|\bU\.S\.\b|\bUS\b"), ("United Kingdom", r"\bUnited Kingdom\b|\bUK\b"),
    ("Canada", r"\bCanada\b"), ("Australia", r"\bAustralia\b"), ("Europe", r"\bEurope\b|\bEU\b"),
    ("India", r"\bIndia\b"), ("Middle East", r"\bMiddle East\b|\bUAE\b|\bDubai\b"), ("Germany", r"\bGermany\b"),
    ("Singapore", r"\bSingapore\b"),
)

# --------------------------------------------------------------------------------------------
# Technology fingerprints (own list): (name, kind, [lowercase HTML markers], [header markers "name" or "name:value"])
# kind: cms | ecommerce | framework | analytics | other
# --------------------------------------------------------------------------------------------
TECH = [
    ("Shopify", "ecommerce", ["cdn.shopify.com", "shopify.theme", ".myshopify.com"], ["x-shopid", "x-shopify-stage"]),
    ("WordPress", "cms", ["/wp-content/", "/wp-includes/", 'content="wordpress'], ["link:api.w.org"]),
    ("WooCommerce", "ecommerce", ["/plugins/woocommerce/", "woocommerce-page", "wc-block"], []),
    ("Magento (Adobe Commerce)", "ecommerce", ["mage/cookies", "magento_", "/static/version", "mage-init"],
     ["x-magento-cache-debug", "x-magento-tags"]),
    ("BigCommerce", "ecommerce", ["cdn11.bigcommerce.com", "bigcommerce.com/s-"], []),
    ("PrestaShop", "ecommerce", ["prestashop"], []),
    ("OpenCart", "ecommerce", ["catalog/view/theme/"], []),
    ("Salesforce Commerce Cloud", "ecommerce", ["demandware.static", "/on/demandware.store/"], []),
    ("Shopware", "ecommerce", ["shopware"], []),
    ("Ecwid", "ecommerce", ["app.ecwid.com"], []),
    ("Drupal", "cms", ["drupal.settings", "/sites/default/files/", "data-drupal-"],
     ["x-drupal-cache", "x-generator:drupal"]),
    ("Joomla", "cms", ['content="joomla', "/media/jui/"], []),
    ("Wix", "cms", ["static.wixstatic.com", "wix.com website builder"], ["x-wix-request-id"]),
    ("Squarespace", "cms", ["static1.squarespace.com", "squarespace-cdn.com"], []),
    ("Webflow", "cms", ["data-wf-page", "webflow.js", "website-files.com"], []),
    ("Framer", "cms", ["framerusercontent.com", "framer.com/m/"], []),
    ("Ghost", "cms", ['content="ghost'], []),
    ("HubSpot CMS", "cms", ["hs-sites.com", 'content="hubspot'], []),
    ("Duda", "cms", ["multiscreensite.com", "dudaone"], []),
    ("Next.js", "framework", ["__next_data__", "/_next/static/"], ["x-powered-by:next.js"]),
    ("Nuxt", "framework", ["__nuxt", "/_nuxt/"], []),
    ("Gatsby", "framework", ["___gatsby"], []),
    ("Angular", "framework", ["ng-version="], []),
    ("SvelteKit", "framework", ["__sveltekit"], []),
    ("Astro", "framework", ["astro-island", 'content="astro'], []),
    ("Laravel", "framework", [], ["set-cookie:laravel_session"]),
    ("ASP.NET", "framework", ["__viewstate"], ["x-aspnet-version", "x-powered-by:asp.net"]),
    ("Google Analytics 4", "analytics", ["gtag/js?id=g-", "gtag('config', 'g-", 'gtag("config", "g-'], []),
    ("Universal Analytics (retired)", "analytics", ["google-analytics.com/analytics.js", "gtag/js?id=ua-"], []),
    ("Google Tag Manager", "analytics", ["googletagmanager.com/gtm.js", "googletagmanager.com/ns.html"], []),
    ("Google Ads", "analytics", ["googleadservices.com", "gtag/js?id=aw-", "'aw-", '"aw-'], []),
    ("Meta Pixel", "analytics", ["fbevents.js", "fbq('init'", 'fbq("init"'], []),
    ("Microsoft Clarity", "analytics", ["clarity.ms/tag"], []),
    ("Hotjar", "analytics", ["static.hotjar.com"], []),
    ("LinkedIn Insight Tag", "analytics", ["snap.licdn.com"], []),
    ("TikTok Pixel", "analytics", ["analytics.tiktok.com"], []),
    ("Microsoft Ads (UET)", "analytics", ["bat.bing.com"], []),
    ("Pinterest Tag", "analytics", ["s.pinimg.com/ct/core.js"], []),
    ("Segment", "analytics", ["cdn.segment.com"], []),
    ("Mixpanel", "analytics", ["cdn.mxpnl.com", "mixpanel.init"], []),
    ("Matomo", "analytics", ["matomo.js", "piwik.js"], []),
    ("Plausible", "analytics", ["plausible.io/js"], []),
    ("HubSpot", "analytics", ["js.hs-scripts.com", "js.hs-analytics.net"], []),
    ("Klaviyo", "analytics", ["static.klaviyo.com"], []),
    ("Yandex Metrica", "analytics", ["mc.yandex.ru"], []),
    ("Stripe", "other", ["js.stripe.com"], []),
    ("PayPal", "other", ["paypal.com/sdk/js", "paypalobjects.com"], []),
    ("Razorpay", "other", ["checkout.razorpay.com"], []),
    ("Google reCAPTCHA", "other", ["google.com/recaptcha", "recaptcha/api.js"], []),
    ("Intercom", "other", ["widget.intercom.io"], []),
    ("Tawk.to", "other", ["embed.tawk.to"], []),
    ("Zendesk", "other", ["static.zdassets.com"], []),
    ("Crisp", "other", ["client.crisp.chat"], []),
    ("LiveChat", "other", ["cdn.livechatinc.com"], []),
    ("Calendly", "other", ["assets.calendly.com"], []),
    ("Mailchimp", "other", ["chimpstatic.com", "list-manage.com"], []),
    ("Cookiebot", "other", ["consent.cookiebot.com"], []),
    ("OneTrust", "other", ["cdn.cookielaw.org"], []),
    ("Yotpo", "other", ["staticw2.yotpo.com", "cdn-widgetsrepository.yotpo.com"], []),
    ("Judge.me", "other", ["judge.me"], []),
    ("jQuery", "other", ["jquery.min.js", "/jquery.js", "jquery-"], []),
    ("Bootstrap", "other", ["bootstrap.min.css", "bootstrap.min.js"], []),
    ("Google Fonts", "other", ["fonts.googleapis.com"], []),
    ("Font Awesome", "other", ["font-awesome", "fontawesome"], []),
]
# header -> (name, kind) for hosting and CDN (kind "hosting" | "cdn"); checked as "header" or "header:value-part"
HOSTING = [
    ("cf-ray", "Cloudflare", "cdn"), ("server:cloudflare", "Cloudflare", "cdn"),
    ("x-vercel-id", "Vercel", "hosting"), ("server:vercel", "Vercel", "hosting"),
    ("x-nf-request-id", "Netlify", "hosting"), ("server:netlify", "Netlify", "hosting"),
    ("x-amz-cf-id", "Amazon CloudFront", "cdn"), ("via:cloudfront", "Amazon CloudFront", "cdn"),
    ("server:amazons3", "Amazon S3", "hosting"), ("server:awselb", "Amazon Web Services", "hosting"),
    ("x-fastly-request-id", "Fastly", "cdn"), ("x-served-by:cache-", "Fastly", "cdn"),
    ("server:akamaighost", "Akamai", "cdn"), ("x-akamai-transformed", "Akamai", "cdn"),
    ("x-sucuri-id", "Sucuri", "cdn"), ("x-iinfo", "Imperva", "cdn"), ("x-azure-ref", "Azure Front Door", "cdn"),
    ("x-kinsta-cache", "Kinsta", "hosting"), ("x-powered-by:wp engine", "WP Engine", "hosting"),
    ("wpe-backend", "WP Engine", "hosting"), ("x-github-request-id", "GitHub Pages", "hosting"),
    ("server:github.com", "GitHub Pages", "hosting"), ("server:google frontend", "Google Cloud", "hosting"),
    ("x-render-origin-server", "Render", "hosting"), ("rndr-id", "Render", "hosting"),
    ("fly-request-id", "Fly.io", "hosting"), ("platform:hostinger", "Hostinger", "hosting"),
    ("x-shopify-stage", "Shopify", "hosting"), ("x-shopid", "Shopify", "hosting"),
    ("x-wix-request-id", "Wix", "hosting"), ("via:vegur", "Heroku", "hosting"),
    ("x-pantheon-styx-hostname", "Pantheon", "hosting"), ("x-hcdn-request-id", "Hostinger CDN", "cdn"),
    ("x-litespeed-cache", "LiteSpeed Cache", "other"),
]
WEB_SERVERS = ("nginx", "apache", "litespeed", "microsoft-iis", "openresty", "caddy", "envoy", "gunicorn", "uvicorn")
WP_PLUGINS = {
    "elementor": "Elementor", "elementor-pro": "Elementor Pro", "wordpress-seo": "Yoast SEO",
    "seo-by-rank-math": "Rank Math SEO", "all-in-one-seo-pack": "All in One SEO", "woocommerce": "WooCommerce",
    "contact-form-7": "Contact Form 7", "wpforms-lite": "WPForms", "wpforms": "WPForms", "js_composer": "WPBakery",
    "revslider": "Slider Revolution", "wp-rocket": "WP Rocket", "litespeed-cache": "LiteSpeed Cache",
    "jetpack": "Jetpack", "gravityforms": "Gravity Forms", "wordfence": "Wordfence", "w3-total-cache": "W3 Total Cache",
    "autoptimize": "Autoptimize", "wp-super-cache": "WP Super Cache", "akismet": "Akismet",
    "google-site-kit": "Site Kit by Google", "complianz-gdpr": "Complianz", "cookie-law-info": "CookieYes",
    "wp-smushit": "Smush", "redirection": "Redirection", "header-footer-elementor": "Elementor Header & Footer",
    "essential-addons-for-elementor-lite": "Essential Addons", "updraftplus": "UpdraftPlus",
    "really-simple-ssl": "Really Simple SSL", "mailchimp-for-wp": "MC4WP", "ninja-forms": "Ninja Forms",
    "fluentform": "Fluent Forms", "astra-sites": "Astra Starter Templates", "sitepress-multilingual-cms": "WPML",
    "polylang": "Polylang", "wp-optimize": "WP-Optimize", "insert-headers-and-footers": "WPCode",
}


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _same_site(url: str, domain: str) -> bool:
    h = _host(url)
    return bool(h) and (h == domain or h.endswith("." + domain))


def _clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


ABBREV = re.compile(r"(?:\b(?:Pvt|Ltd|Inc|Co|Corp|St|Rd|Dr|No|Mr|Ms|Mrs|Jr|Sr|vs|etc|approx|Ave|Blvd)|"
                    r"\b[A-Z]|e\.g|i\.e|U\.S|U\.K)\.$")


def _clip(text: Any, sentences: int = 2, chars: int = 300) -> str:
    """At most `sentences` sentences and `chars` characters."""
    t = _clean(text)
    if not t:
        return ""
    parts: List[str] = []
    for piece in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"(])", t):
        if parts and ABBREV.search(parts[-1]):
            parts[-1] += " " + piece      # "Pvt. Ltd." or "St. Louis" is not a sentence end
        else:
            parts.append(piece)
    t = " ".join(parts[:sentences])
    if len(t) > chars:
        cut = t[:chars - 1]
        t = (cut[:cut.rfind(" ")] if " " in cut[int(chars * 0.6):] else cut).rstrip(",;:- ") + "…"
    return t


def _uniq(items, key=lambda x: x.lower() if isinstance(x, str) else x, limit: int = MAX_LIST) -> list:
    seen, out = set(), []
    for it in items:
        if not it:
            continue
        k = key(it)
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if len(out) >= limit:
            break
    return out


def _n(count: int, word: str) -> str:
    """'1 URL', '3 URLs'."""
    return f"{count:,} {word}{'' if count == 1 else 's'}"


def _progress(cb: Optional[Callable], pct: int, msg: str) -> None:
    if cb:
        try:
            cb(int(pct), msg)
        except Exception:
            pass


# --------------------------------------------------------------------------------------------
# Fetching and parsing pages
# --------------------------------------------------------------------------------------------
def _public_session(user_agent: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent or DEFAULT_USER_AGENT,
                      "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                      "Accept-Language": "en-US,en;q=0.9"})
    return s


def _fetch(session: requests.Session, url: str) -> Optional[dict]:
    """GET a page: {"url","final_url","status","html","headers","ms"} (html "" for non-HTML), None on failure."""
    started = time.time()
    resp = None
    for verify in (True, False):     # tolerate a misconfigured certificate, like the crawler does
        try:
            resp = session.get(url, timeout=FETCH_TIMEOUT, allow_redirects=True, stream=True, verify=verify)
            break
        except requests.exceptions.SSLError:
            continue
        except requests.RequestException:
            return None
    if resp is None:
        return None
    try:
        ctype = resp.headers.get("content-type", "").lower()
        body = b""
        if "html" in ctype or "xml" in ctype or "text/plain" in ctype or not ctype:
            for chunk in resp.iter_content(65536):
                body += chunk
                if len(body) > MAX_BODY:
                    break
        enc = resp.encoding if resp.encoding and resp.encoding.lower() != "iso-8859-1" else None
        html = body.decode(enc or "utf-8", errors="replace") if body else ""
    except requests.RequestException:
        html = ""
    finally:
        resp.close()
    return {"url": url, "final_url": resp.url, "status": resp.status_code, "html": html,
            "headers": {k.lower(): v for k, v in resp.headers.items()}, "ms": int((time.time() - started) * 1000)}


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except Exception:
        return BeautifulSoup(html, "html.parser")


def _jsonld(soup: BeautifulSoup) -> List[dict]:
    """Every JSON-LD object on the page, @graph members flattened, nested objects included."""
    out: List[dict] = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            if "@type" in node:
                out.append(node)
            for k, v in node.items():
                if k != "@context" and isinstance(v, (dict, list)):
                    walk(v, depth + 1)
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            walk(json.loads(raw))
        except ValueError:
            continue
    return out


def _types(obj: dict) -> List[str]:
    t = obj.get("@type")
    return [str(x) for x in (t if isinstance(t, list) else [t]) if x]


ORG_TYPES = re.compile(r"Organization|Corporation|Business|Store|Company|Agency|Service$|Restaurant|Hotel|Clinic|"
                       r"School|NGO|Brand$", re.I)
NOT_ORG = {"WebSite", "WebPage", "Product", "Offer", "BreadcrumbList", "Person", "Article", "BlogPosting",
           "ImageObject", "ListItem", "SearchAction", "ReadAction", "AggregateRating", "Review", "FAQPage",
           "Question", "Answer", "CollectionPage", "ItemList", "VideoObject", "ContactPoint", "PostalAddress",
           "Place", "EntryPoint", "SiteNavigationElement", "PropertyValue", "Rating", "Thing", "Brand"}


def _text_val(v: Any) -> str:
    if isinstance(v, dict):
        return _clean(v.get("name") or v.get("@value") or v.get("url") or "")
    if isinstance(v, list):
        return _text_val(v[0]) if v else ""
    return _clean(v)


def _address_text(addr: Any) -> str:
    if isinstance(addr, list):
        addr = addr[0] if addr else ""
    if isinstance(addr, str):
        return _clean(addr)
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode",
                                       "addressCountry")]
        return ", ".join(_text_val(p) for p in parts if _text_val(p))
    return ""


def _org_facts(items: List[dict]) -> dict:
    """Merged schema.org Organization/LocalBusiness facts (the richest object first)."""
    orgs = [o for o in items if any(ORG_TYPES.search(t) for t in _types(o))
            and not all(t in NOT_ORG for t in _types(o))]
    orgs.sort(key=lambda o: -len(o))
    out: Dict[str, Any] = {}
    for o in orgs:
        def put(key, value):
            if value and not out.get(key):
                out[key] = value
        put("name", _text_val(o.get("name")))
        put("legal_name", _text_val(o.get("legalName")))
        put("description", _clean(o.get("description"))[:600])
        put("slogan", _text_val(o.get("slogan")))
        year = re.search(r"(?:19|20)\d{2}", _text_val(o.get("foundingDate")))
        put("founded", year.group(0) if year else "")
        loc = o.get("location")
        put("address", _address_text(o.get("address") or (loc.get("address") if isinstance(loc, dict) else None)))
        put("telephone", _text_val(o.get("telephone")))
        put("email", _text_val(o.get("email")).replace("mailto:", ""))
        emp = o.get("numberOfEmployees")
        if isinstance(emp, dict):
            emp = emp.get("value") or (f"{emp.get('minValue')}-{emp.get('maxValue')}"
                                       if emp.get("minValue") and emp.get("maxValue") else "")
        put("employees", _text_val(emp))
        put("area_served", _text_val(o.get("areaServed")))
        same = o.get("sameAs") or []
        put("same_as", [s for s in (same if isinstance(same, list) else [same]) if isinstance(s, str)][:12])
        cp = o.get("contactPoint")
        for c in (cp if isinstance(cp, list) else [cp] if isinstance(cp, dict) else []):
            put("telephone", _text_val(c.get("telephone")))
            put("email", _text_val(c.get("email")).replace("mailto:", ""))
        put("types", [t for t in _types(o) if t not in NOT_ORG])
        put("url", _text_val(o.get("url")))
    return out


def _decode_cfemail(hexstr: str) -> str:
    """Cloudflare email obfuscation (data-cfemail): first byte is the XOR key."""
    try:
        data = bytes.fromhex(hexstr)
        return "".join(chr(b ^ data[0]) for b in data[1:])
    except ValueError:
        return ""


def _visible_text(root, sep: str = " ") -> str:
    if root is None:
        return ""
    return root.get_text(sep, strip=True)


def parse_page(url: str, html: str, headers: Optional[dict] = None) -> dict:
    """Facts from one HTML page: title, description, headings, text, JSON-LD, links, contacts, footer."""
    soup = _soup(html)
    ld = _jsonld(soup)

    def meta(**attrs):
        tag = soup.find("meta", attrs=attrs)
        return _clean(tag.get("content")) if tag and tag.get("content") else ""
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        links.append((href, urljoin(url, href), _clean(a.get_text(" ", strip=True))[:120]))
    cf_emails = [_decode_cfemail(t.get("data-cfemail", "")) for t in soup.select("[data-cfemail]")]
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    body = soup.body or soup
    footer = soup.find("footer") or soup.find(id=re.compile("footer", re.I)) or \
        soup.find(class_=re.compile(r"(^|\s|-)footer", re.I))
    footer_lines = _visible_text(footer, "\n") if footer else ""
    addresses = [_clean(a.get_text(" ", strip=True)) for a in soup.find_all("address")]
    full_lines = _visible_text(body, "\n")
    for tag in body.find_all(["nav", "header", "footer"]):
        tag.decompose()
    main = _clean(_visible_text(body))
    if len(main) < 200:
        main = _clean(full_lines)
    h = lambda name: [_clean(x.get_text(" ", strip=True))[:160] for x in soup.find_all(name)]  # noqa: E731
    title = _clean(soup.title.get_text()) if soup.title else ""
    html_tag = soup.find("html")
    return {
        "url": url, "title": title,
        "description": meta(name="description") or meta(property="og:description"),
        "og_site_name": meta(property="og:site_name"), "og_title": meta(property="og:title"),
        "generator": meta(name="generator"), "robots": meta(name="robots").lower(),
        "lang": (html_tag.get("lang") or "").strip() if html_tag else "",
        "hreflang": [(l.get("hreflang") or "").lower() for l in soup.find_all("link", hreflang=True)],
        "h1": [x for x in h("h1") if x][:5], "h2": [x for x in h("h2") if x][:30], "h3": [x for x in h("h3") if x][:40],
        "text": main[:20000], "lines": full_lines[:40000], "footer": footer_lines[:6000],
        "jsonld": ld, "links": links, "cf_emails": [e for e in cf_emails if e], "addresses": addresses[:3],
    }


# --------------------------------------------------------------------------------------------
# Technology detection
# --------------------------------------------------------------------------------------------
def _header_hit(headers: Dict[str, str], marker: str) -> bool:
    name, _, part = marker.partition(":")
    value = headers.get(name)
    if value is None:
        return False
    return part in value.lower() if part else True


def detect_tech(html: str, headers: Optional[Dict[str, str]] = None, extra_html: str = "") -> dict:
    """Platform, plugins, analytics, hosting and CDN from page HTML and response headers.
    Returns {"cms","ecommerce","analytics":[..],"hosting","cdn","other":[..],"plugins":[..],"theme",
    "frameworks":[..],"server","evidence":[..],"confidence": 0..1}."""
    headers = {k.lower(): str(v) for k, v in (headers or {}).items()}
    low = (html[:900_000] + "\n" + extra_html[:600_000]).lower()
    found: Dict[str, List[str]] = {"cms": [], "ecommerce": [], "framework": [], "analytics": [], "other": []}
    evidence: List[str] = []
    for name, kind, markers, hmarkers in TECH:
        hit = next((m for m in markers if m in low), None)
        hhit = next((m for m in hmarkers if _header_hit(headers, m)), None)
        if hit or hhit:
            found[kind].append(name)
            evidence.append(f"{name}: " + (f"'{hit}' in the HTML" if hit else f"'{hhit.split(':')[0]}' header"))
    gen = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', html[:200_000], re.I)
    generator = _clean(gen.group(1)) if gen else ""
    plugins = []
    for slug in re.findall(r"/wp-content/plugins/([a-z0-9_-]+)/", low):
        plugins.append(WP_PLUGINS.get(slug, slug.replace("-", " ").replace("_", " ").title()))
    if "yoast seo plugin" in low:
        plugins.append("Yoast SEO")
    if "optimization by rank math" in low or 'class="rank-math' in low:
        plugins.append("Rank Math SEO")
    plugins = _uniq(plugins, limit=40)
    theme_m = (re.search(r"/wp-content/themes/([a-z0-9_-]+)/", low)
               or re.search(r"shopify\.theme\s*=\s*\{\"name\":\"([^\"]+)", low))
    theme = theme_m.group(1).replace("-", " ").title() if theme_m else ""
    if "WooCommerce" in plugins and "WooCommerce" not in found["ecommerce"]:
        found["ecommerce"].append("WooCommerce")
    hosting, cdn, other_host = [], [], []
    for marker, name, kind in HOSTING:
        if _header_hit(headers, marker):
            (hosting if kind == "hosting" else cdn if kind == "cdn" else other_host).append(name)
            evidence.append(f"{name}: '{marker.split(':')[0]}' header")
    server = headers.get("server", "")
    if server and any(s in server.lower() for s in WEB_SERVERS):
        other_host.append(f"{server.split('/')[0].strip().title()} web server")
    if headers.get("x-powered-by") and "php" in headers["x-powered-by"].lower():
        other_host.append("PHP")
    cms = found["cms"][0] if found["cms"] else ""
    if not cms and "Shopify" in found["ecommerce"]:
        cms = "Shopify"
    if not cms and "Magento (Adobe Commerce)" in found["ecommerce"]:
        cms = "Magento (Adobe Commerce)"
    if not cms and generator:
        cms = generator.split(" ")[0] if len(generator) < 40 else ""
    if cms == "WordPress" and generator.lower().startswith("wordpress"):
        cms = generator   # "WordPress 6.6"
    if not cms and found["framework"]:
        cms = found["framework"][0] + " (custom build)"
    ecommerce = ", ".join(_uniq(found["ecommerce"], limit=3))
    if "Shopify" in found["ecommerce"] and "Shopify" not in hosting:
        hosting.insert(0, "Shopify")
    other = _uniq(found["framework"] + found["other"] + other_host, limit=12)
    signals = len(evidence)
    confidence = 0.0 if not cms and not ecommerce else min(1.0, 0.5 + 0.1 * signals)
    return {"cms": cms, "ecommerce": ecommerce, "analytics": _uniq(found["analytics"], limit=10),
            "hosting": ", ".join(_uniq(hosting, limit=2)), "cdn": ", ".join(_uniq(cdn, limit=2)),
            "other": other, "plugins": plugins, "theme": theme, "frameworks": found["framework"],
            "server": server, "generator": generator, "evidence": evidence[:20], "confidence": round(confidence, 2)}


# --------------------------------------------------------------------------------------------
# Contacts and socials
# --------------------------------------------------------------------------------------------
def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


def _phone_ok(phone: str) -> bool:
    d = _digits(phone)
    return 8 <= len(d) <= 15 and len(set(d)) > 2


POSTCODE = re.compile(r"\b\d{5,6}\b|\b\d{3}\s\d{3}\b|\b\d{5}-\d{4}\b|\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b|"
                      r"\b[A-Z]\d[A-Z]\s?\d[A-Z]\d\b")
STREET = re.compile(r"\b(road|rd|street|st|avenue|ave|floor|suite|tower|park|nagar|lane|ln|blvd|boulevard|drive|"
                    r"dr|sector|plaza|building|bldg|highway|hwy|way|square|marg|chowk|estate)\b\.?", re.I)


def _looks_like_address(line: str) -> bool:
    """An unlabelled postal address line: commas, a street word and a postcode."""
    if not (20 <= len(line) <= 200) or "@" in line or "http" in line or line.count(",") < 2:
        return False
    return bool(POSTCODE.search(line) and STREET.search(line))


# One video / post / embed on a social network, not the company's profile page
CONTENT_PATH = re.compile(r"^/(?:embed|watch|shorts|live|v|p|reel|reels|tv|video|videos|status|posts?|pin|"
                          r"photo|photos|events?|hashtag|search|explore|playlist|feed|groups)(?:/|$)|/status/\d+",
                          re.I)


def _social(url: str) -> Optional[Tuple[str, str]]:
    host = _host(url)
    for dom, name in SOCIAL_HOSTS.items():
        if host == dom or host.endswith("." + dom):
            path = urlparse(url).path.strip("/")
            if not path or SHARE_PATH.search(url) or CONTENT_PATH.search(urlparse(url).path):
                return None
            return name, url.split("?")[0].rstrip("/")
    return None


def _contacts(pages: List[dict], org: dict, domain: str, extra_links: List[str]) -> dict:
    emails: List[Tuple[str, str]] = []
    phones: List[Tuple[str, str]] = []
    addresses: List[Tuple[str, str]] = []
    socials: Dict[str, str] = {}
    if org.get("email"):
        emails.append((org["email"], org.get("_source", "")))
    if org.get("telephone"):
        phones.append((org["telephone"], org.get("_source", "")))
    if org.get("address"):
        addresses.append((org["address"], org.get("_source", "")))
    for page in pages:
        src = page["url"]
        for raw, absu, text in page["links"]:
            low = raw.lower()
            if low.startswith("mailto:"):
                emails.append((unquote(raw[7:].split("?")[0]).strip(), src))
            elif low.startswith(("tel:", "callto:")):
                num = unquote(raw.split(":", 1)[1]).strip()
                shown = text if text and _digits(text).endswith(_digits(num)[-8:]) else num
                phones.append((shown, src))
            elif absu.startswith("http"):
                s = _social(absu)
                if s and s[0] not in socials:
                    socials[s[0]] = s[1]
        for e in page["cf_emails"]:
            emails.append((e, src))
        for e in EMAIL_RE.findall(page["lines"]):
            emails.append((e, src))
        text = page["footer"] + "\n" + (page["lines"] if page.get("kind") in ("contact", "home", "about") else "")
        for m in PHONE_LABEL.finditer(text):
            phones.append((m.group(1).strip(), src))
        for m in PHONE_INTL.finditer(text):
            phones.append((m.group(0).strip(), src))
        for a in page["addresses"]:
            addresses.append((a, src))
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for i, line in enumerate(lines):
            m = re.match(r"^(?:address|office address|head office|registered office|headquarters|hq|"
                         r"corporate office|our office)\s*[:\-]?\s*(.*)$", line, re.I)
            if m:
                cand = m.group(1).strip() or (lines[i + 1] if i + 1 < len(lines) else "")
                if 10 <= len(cand) <= 200 and (re.search(r"\d", cand) or "," in cand):
                    addresses.append((cand, src))
            elif _looks_like_address(line):
                addresses.append((line, src))
    for u in list(org.get("same_as") or []) + list(extra_links):
        s = _social(u)
        if s and s[0] not in socials:
            socials[s[0]] = s[1]
    good_emails = []
    for e, src in emails:
        e = e.strip().strip(".").lower()
        if EMAIL_RE.fullmatch(e) and not BAD_EMAIL.search(e):
            good_emails.append((e, src))
    # emails at the company's own domain first
    good_emails.sort(key=lambda x: 0 if x[0].endswith("@" + domain) or x[0].endswith("." + domain) else 1)
    phones = [(re.sub(r"\s+", " ", p).strip(" .-"), s) for p, s in phones if _phone_ok(p)]
    return {
        "emails": _uniq(good_emails, key=lambda x: x[0], limit=4),
        "phones": _uniq(phones, key=lambda x: _digits(x[0])[-10:], limit=4),
        "addresses": _uniq([(a, s) for a, s in addresses if len(a) >= 10], key=lambda x: x[0].lower()[:40],
                           limit=3),
        "socials": socials,
    }


# --------------------------------------------------------------------------------------------
# Key pages
# --------------------------------------------------------------------------------------------
def _kind_of(url: str) -> Tuple[str, int]:
    """(kind, segment index) when the URL is a key page, else ("", 99)."""
    p = urlparse(url)
    if p.query or re.search(r"\.(pdf|jpe?g|png|gif|zip|xml|txt|webp|svg|mp4)$", p.path, re.I):
        return "", 99
    segs = [s for s in p.path.lower().split("/") if s]
    for i, seg in enumerate(segs[:2]):
        seg = re.sub(r"\.(html?|php|aspx?)$", "", seg)
        for kind, words in KEY_PAGE_WORDS.items():
            if seg in words:
                return kind, i + len(segs) - 1
    return "", 99


def _pick_key_pages(urls: List[str], domain: str) -> Dict[str, str]:
    best: Dict[str, Tuple[int, int, str]] = {}
    for u in urls:
        if not _same_site(u, domain):
            continue
        kind, rank = _kind_of(u)
        if not kind:
            continue
        key = (rank, len(u), u)
        if kind not in best or key < best[kind]:
            best[kind] = key
    order = ["about", "contact", "services", "products", "pricing", "careers"]
    return {k: best[k][2] for k in order if k in best}


def _offerings_from_links(links: List[Tuple[str, str]], domain: str) -> List[Tuple[str, str]]:
    """(name, url) of pages directly under a services/products/collections root, in link order."""
    direct, deeper = [], []
    for absu, text in links:
        if not _same_site(absu, domain) or not text:
            continue
        segs = [s for s in urlparse(absu).path.lower().split("/") if s]
        if len(segs) < 2 or segs[0] not in OFFERING_ROOTS:
            continue
        name = _clean(text)
        if not (2 < len(name) <= 60) or len(name.split()) > 8 or name.lower() in GENERIC_HEADINGS:
            continue
        (direct if len(segs) == 2 else deeper).append((name, absu.split("#")[0]))
    return _uniq(direct, key=lambda x: x[0].lower(), limit=12) or _uniq(deeper, key=lambda x: x[0].lower(), limit=12)


def _sitemap_count(session: requests.Session, base: str, robots_text: str = "") -> Optional[int]:
    """URLs listed in the site's XML sitemaps (follows one sitemap-index level, up to 10 child sitemaps)."""
    maps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots_text or "")
    if not maps:
        maps = [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]
    total, seen_any = 0, False
    for sm in maps[:3]:
        got = _fetch(session, sm)
        if not got or got["status"] != 200 or "<" not in got["html"][:500]:
            continue
        seen_any = True
        xml = got["html"]
        if "<sitemapindex" in xml[:2000].lower():
            for child in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)[:10]:
                c = _fetch(session, child.replace("&amp;", "&"))
                if c and c["status"] == 200:
                    total += len(re.findall(r"<url[\s>]", c["html"], re.I))
        else:
            total += len(re.findall(r"<url[\s>]", xml, re.I))
        if total:
            break
    return total if seen_any else None


# --------------------------------------------------------------------------------------------
# Crawl data
# --------------------------------------------------------------------------------------------
def _deliberate(index, issue: dict) -> str:
    """Why an issue's URLs are most likely set that way on purpose ('' when not): noindex on archive pages
    (tags, categories, authors...) is a common, deliberate SEO-plugin setting, not a risk."""
    if (issue.get("tab"), issue.get("filter")) not in (("directives", "noindex"), ("directives", "none")):
        return ""
    try:
        from ..intel.root_cause import ARCHIVE_LABELS, ARCHIVES, PAGED
        tr = index.tabs.get("directives")
        urls = [getattr(r, "url", "") for r in (tr.filter_rows.get(issue.get("filter")) or [])] if tr else []
    except Exception:
        return ""
    kinds: Counter = Counter()
    for u in urls:
        path = urlparse(u).path or "/"
        kind = next((name for name, rx in ARCHIVES if rx.search(path)), "") or ("pagination" if PAGED.search(u) else "")
        if not kind:
            return ""
        kinds[kind] += 1
    if not kinds:
        return ""
    return " and ".join(ARCHIVE_LABELS.get(k, k) for k, _ in kinds.most_common(2)) + ": usually deliberate"


def _crawl_facts(crawler) -> Optional[dict]:
    """Health score, broken URLs, top SEO issues, sitemap size and page list from a finished crawl."""
    results = list(getattr(crawler, "results_list", None) or [])
    if not results:
        return None
    from ..issues import classify_result, health_score, is_page
    health = health_score(results)
    counts = Counter(classify_result(r) for r in results)
    issues: List[dict] = []
    try:
        _, index = crawler.site_index()
        issues = list(getattr(index, "issues", []) or [])
    except Exception as exc:
        log.info("business: no SEO index (%s)", exc)
    pages = []
    for r in results:
        seo = r.seo or {}
        if is_page(r) and classify_result(r) == "2xx" and seo.get("is_html"):
            pages.append({"url": r.url, "title": (seo.get("titles") or [r.page_title or ""])[0],
                          "description": (seo.get("meta_descriptions") or [""])[0],
                          "h1": list(seo.get("h1") or [])[:3], "words": seo.get("word_count", 0),
                          "types": list((seo.get("structured_data") or {}).get("types") or [])})
    externals = [e.url for e in (getattr(crawler, "external_links_list", None) or []) if getattr(e, "url", "")]
    on_purpose = {id(i): _deliberate(index, i) for i in issues[:40]} if issues else {}
    return {
        "urls": len(results), "pages": health["pages"], "pages_ok": health["pages_ok"], "health_score": health["score"],
        "broken_4xx": counts["4xx"], "server_5xx": counts["5xx"], "no_response": counts["error"],
        "redirects": counts["3xx"], "blocked": counts["blocked"],
        "issues": [{"name": i.get("name", ""), "urls": i.get("urls", 0), "priority": i.get("priority", ""),
                    "type": i.get("issue_type", ""), "fix": i.get("fix", ""),
                    "deliberate": on_purpose.get(id(i), "")} for i in issues[:40]],
        "sitemap_urls": len(getattr(crawler, "sitemap_index", None) or {}) or None,
        "page_list": pages, "externals": externals[:5000],
        "auth_used": bool(getattr(crawler, "auth_used", False)),
        "start_url": getattr(crawler, "start_url", ""),
        "home_ms": next((int(getattr(r, "response_time_ms", 0) or 0) for r in results
                         if getattr(r, "url", "") == getattr(crawler, "start_url", "")), 0),
    }


# --------------------------------------------------------------------------------------------
# Site facts
# --------------------------------------------------------------------------------------------
def _name_from_titles(titles: List[str]) -> str:
    """The brand part most titles share ("Services | Acme" -> "Acme")."""
    tails = Counter()
    for t in titles:
        parts = [p.strip() for p in re.split(r"\s[|–—:\-·]\s|\s::\s", t) if p.strip()]
        if len(parts) >= 2:
            tails[parts[-1]] += 1
            tails[parts[0]] += 0.5
    if not tails:
        return ""
    name, n = tails.most_common(1)[0]
    return name if (n >= 2 or len(titles) <= 2) and len(name) <= 60 else ""


def _tld_country(domain: str) -> str:
    tld = domain.rsplit(".", 1)[-1].upper()
    if len(tld) != 2 or pycountry is None or tld in ("IO", "AI", "CO", "ME", "TV", "LY", "FM", "GG"):
        return ""
    c = pycountry.countries.get(alpha_2="GB" if tld == "UK" else tld)
    return getattr(c, "common_name", None) or getattr(c, "name", "") if c else ""


def _sentences(text: str) -> List[str]:
    """A page's text as sentences: blocks split at line breaks, then at sentence ends ("Est." or "Ltd." end none)."""
    out: List[str] = []
    for block in re.split(r"\n+", text or ""):
        parts: List[str] = []
        for piece in re.split(r"(?<=[.!?])\s+", block.strip()):
            if parts and (ABBREV.search(parts[-1]) or re.search(r"(?i)\bestd?\.$", parts[-1])):
                parts[-1] += " " + piece
            elif piece:
                parts.append(piece)
        out += parts
    return out


def _mentions(text: str, name: str) -> bool:
    return len(name or "") >= 3 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text, re.I) is not None


def _founding_year(sentence: str, name: str) -> str:
    """The founding year a sentence states about the company itself ("Founded in 2008", "Est. 1998", "We have
    been making boots since 1952", "Family owned since 1952", "It all started in 2015"), else "". A "since"
    about a product or a customer claim ("a bestseller since 2021") is not one."""
    now = time.localtime().tm_year
    m = FOUNDED_RE.search(sentence)
    if m and int(m.group(1)) <= now:
        return m.group(1)
    for m in SINCE_RE.finditer(sentence):
        pre, post = sentence[:m.start()][-90:], sentence[m.end():][:60]
        if int(m.group(1)) > now or CLAIM_RE.search(pre):
            continue
        act = ACTIVITY_RE.search(pre)
        # "We have been helping brands since", "Acme has been serving salons since", "Proudly serving Dallas since"
        if act and (OWN_SUBJECT_RE.search(pre) or _mentions(pre, name) or len(pre[:act.start()].split()) <= 1):
            return m.group(1)
        # "Since 2010, we have ..."
        lead = re.match(r"\s*,?\s*(\S+(?:\s+\S+)?)", post)
        if not pre.strip(" ,") and lead and (OWN_SUBJECT_RE.match(lead.group(1)) or _mentions(lead.group(1), name)):
            return m.group(1)
    m = STARTED_RE.search(sentence)
    if m and int(m.group(1)) <= now:
        pre = sentence[:m.start()][-60:]
        if (OWN_SUBJECT_RE.search(pre) or _mentions(pre, name)) and not CLAIM_RE.search(pre):
            return m.group(1)
    return ""


def _head_count(sentence: str) -> str:
    """The company's own head count a sentence states ("a team of 45 developers", "our 120 engineers", "Acme
    has 150 employees"), else "". People the company sells to or is rated by ("Trusted by 12,000+
    professionals", "Over 500 experts recommend our boots") are not its size."""
    for m in SIZE_RE.finditer(sentence):
        pre, post = sentence[:m.start()][-60:], sentence[m.end():][:50]
        if CLAIM_RE.search(pre):
            continue
        if OWN_TEAM_RE.search(pre):
            return _clean(m.group(0))
        workforce = re.fullmatch(WORKFORCE_NOUNS, m.group(2), re.I) is not None
        if workforce and (OWN_HAVE_RE.search(pre) or not pre.strip()) and not CUSTOMER_AFTER_RE.search(post):
            return _clean(m.group(0))
    return ""


def collect_site_facts(url: str, crawler=None, session: Optional[requests.Session] = None,
                       progress: Optional[Callable] = None) -> dict:
    """Facts about the business from its own website (see module docstring). Never raises for network errors:
    what could not be fetched is listed in facts["errors"]."""
    errors: List[str] = []
    crawl = None
    if crawler is not None:
        try:
            crawl = _crawl_facts(crawler)
        except Exception as exc:
            errors.append(f"crawl data unavailable ({type(exc).__name__})")
    url = (url or (crawl or {}).get("start_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    ua = getattr(getattr(crawler, "config", None), "user_agent", "") if crawler is not None else ""
    session = session or _public_session(ua)
    _progress(progress, 8, "Reading the home page")
    home = _fetch(session, url) if url else None
    if not home or home["status"] >= 400 or not home["html"]:
        errors.append(f"home page could not be read ({home['status'] if home else 'no response'})")
    final = (home or {}).get("final_url") or url
    domain = _host(final) or _host(url)
    private = bool(crawl and crawl.get("auth_used"))

    fetched = [home] if home and home.get("html") else []
    parsed: List[dict] = []
    if fetched:
        hp = parse_page(final, home["html"], home["headers"])
        hp["kind"] = "home"
        hp["status"] = home["status"]
        parsed.append(hp)
    # key pages: from the crawl's page list (public crawls only) and the home page's links
    candidates = [absu for _, absu, _ in (parsed[0]["links"] if parsed else [])]
    if crawl and not private:
        candidates += [p["url"] for p in crawl["page_list"]]
    key_pages = _pick_key_pages(candidates, domain)
    _progress(progress, 15, f"Reading {len(key_pages)} key pages")
    targets = [u for u in key_pages.values() if u.split("#")[0].rstrip("/") != final.rstrip("/")][:MAX_KEY_PAGES]
    if targets:
        with ThreadPoolExecutor(max_workers=4) as pool:
            got = list(pool.map(lambda u: _fetch(session, u), targets))
        kinds = {v: k for k, v in key_pages.items()}
        for target, page in zip(targets, got):
            if not page or not page["html"]:
                errors.append(f"{target} could not be read")
                continue
            pp = parse_page(page["final_url"], page["html"], page["headers"])
            pp["kind"] = kinds.get(target, "")
            pp["status"] = page["status"]
            parsed.append(pp)
            fetched.append(page)
    _progress(progress, 35, "Detecting the platform and tools")
    ok_pages = [p for p in parsed if p.get("status", 0) < 400]
    items = [o for p in ok_pages for o in p["jsonld"]]
    org = _org_facts(items)
    if org:
        org["_source"] = next((p["url"] for p in ok_pages if _org_facts(p["jsonld"])), final)
    site_name = next((_text_val(o.get("name")) for o in items if "WebSite" in _types(o) and o.get("name")), "")
    og_name = next((p["og_site_name"] for p in ok_pages if p["og_site_name"]), "")
    titles = [p["title"] for p in ok_pages if p["title"]]
    if crawl and not private:
        titles += [p["title"] for p in crawl["page_list"][:200] if p["title"]]
    name = org.get("name") or og_name or site_name or _name_from_titles(titles) or \
        (domain.split(".")[0].replace("-", " ").title() if domain else "")
    products = _uniq([_text_val(o.get("name")) for o in items if "Product" in _types(o)], limit=10)
    currencies = _uniq([_text_val(o.get("priceCurrency")) for o in items if o.get("priceCurrency")], limit=3)

    tech = detect_tech(home["html"] if home else "", home["headers"] if home else {},
                       "\n".join(f["html"][:200_000] for f in fetched[1:]))
    if not tech.get("cms") and crawler is not None:
        # the audit's own detector (headers, asset paths, cookies of every crawled page): the business report
        # and the audit PDF then name the same platform; it names one only on strong evidence
        try:
            from crawler.intel.platform import detect_platform
            plat = detect_platform(crawler)
            if plat.get("cms"):
                tech["cms"] = str(plat["cms"])
                tech["evidence"] = (tech.get("evidence") or []) + [
                    f"{plat['cms']}: {e}" for e in (plat.get("evidence") or [])[:3]]
                tech["confidence"] = max(float(tech.get("confidence") or 0), float(plat.get("confidence") or 0))
        except Exception:
            pass
    externals = (crawl or {}).get("externals") or []
    contact = _contacts(ok_pages, org, domain, externals)

    # copyright, founded, size from the pages' own words
    copyright_info: Dict[str, Any] = {}
    for p in ok_pages:
        footer = (p["footer"] or p["lines"][-3000:]).replace("\n", " ")
        m = re.search(r"(?:©|&copy;|\(c\)|copyright).{0,120}", footer, re.I)
        if m:
            line = _clean(m.group(0))
            years = [int(y) for y in re.findall(r"\b(?:19|20)\d{2}\b", line)]
            bare = re.sub(r"(?i)copyright|\(c\)|&copy;|©|all rights reserved|\b(?:19|20)\d{2}\b|[-–]", " ", line)
            legal = LEGAL_RE.search(_clean(bare))
            copyright_info = {"text": line[:140], "year": max(years) if years else None,
                              "legal_name": _clean(legal.group(1)) if legal else "", "url": p["url"]}
            break
    founded = {"value": org.get("founded", ""), "url": org.get("_source", "")} if org.get("founded") else {}
    size = {"value": str(org["employees"]), "url": org.get("_source", "")} if org.get("employees") else {}
    # every sentence that states the founding year / head count of the company itself (the AI's values are
    # checked against these too, see _merge_ai); schema.org data comes first
    founded_quotes: List[str] = []
    size_quotes: List[str] = []
    for p in sorted(ok_pages, key=lambda x: {"about": 0, "home": 1}.get(x.get("kind"), 2)):
        for sentence in _sentences(p.get("lines") or p["text"]):
            year = _founding_year(sentence, name)
            if year:
                founded_quotes.append(_clip(sentence, 1, 200))
                if not founded:
                    founded = {"value": year, "url": p["url"], "quote": _clip(sentence, 1, 200)}
            count = _head_count(sentence)
            if count:
                size_quotes.append(_clip(sentence, 1, 200))
                if not size:
                    size = {"value": count, "url": p["url"], "quote": _clip(sentence, 1, 200)}
    founded_quotes, size_quotes = _uniq(founded_quotes, limit=10), _uniq(size_quotes, limit=10)

    # offerings: child pages of services/products roots, then Product schema, then headings
    links = [(absu, text) for p in ok_pages for _, absu, text in p["links"]]
    offerings = [{"name": n, "url": u} for n, u in _offerings_from_links(links, domain)]
    if not offerings and products:
        offerings = [{"name": n, "url": final} for n in products]
    if not offerings:
        src = next((p for p in ok_pages if p.get("kind") in ("services", "products")), None) or \
            (ok_pages[0] if ok_pages else None)
        if src:
            heads = [h for h in src["h2"] + src["h3"] if 2 < len(h) <= 60 and len(h.split()) <= 7
                     and h.lower().strip(" :") not in GENERIC_HEADINGS and not h.endswith("?")]
            offerings = [{"name": h, "url": src["url"]} for h in _uniq(heads, limit=8)]

    text_all = " ".join(p["text"][:8000] for p in ok_pages if p.get("kind") in ("home", "about", "services",
                                                                                   "products", "pricing", ""))
    low = text_all.lower()
    industry_scores = {ind: sum(low.count(w) for w in words) for ind, words in INDUSTRY_HINTS.items()}
    industry, score = max(industry_scores.items(), key=lambda kv: kv[1]) if industry_scores else ("", 0)
    if tech["ecommerce"] and industry_scores.get("E-commerce / retail", 0) >= 2:
        industry, score = "E-commerce / retail", max(score, 3)
    audience = Counter()
    for term in AUDIENCE_TERMS:
        n = len(re.findall(r"\b" + re.escape(term.lower()) + r"\b", low))
        if n:
            audience[term] = n
    if "brands" in audience and len(audience) > 1:
        del audience["brands"]
    markets = Counter()
    for label, pattern in MARKETS:
        n = len(re.findall(pattern, text_all))
        if n:
            markets[label] = n
    has_blog = any(any(seg in BLOG_WORDS for seg in urlparse(u).path.lower().split("/")[:2])
                   for u in candidates if _same_site(u, domain))
    robots = _fetch(session, urljoin(final, "/robots.txt")) if final else None
    robots_text = robots["html"] if robots and robots["status"] == 200 else ""
    sitemap_urls = (crawl or {}).get("sitemap_urls")
    if sitemap_urls is None and final:
        _progress(progress, 42, "Counting sitemap URLs")
        sitemap_urls = _sitemap_count(session, final, robots_text)
    home_parsed = parsed[0] if parsed and parsed[0].get("kind") == "home" else {}
    return {
        "url": url, "final_url": final, "domain": domain, "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
        "source": "crawl" if crawl else "fetch", "private": private, "name": name,
        "name_source": org.get("_source") if org.get("name") else final,
        "site_name": og_name or site_name,
        "pages": [{"url": p["url"], "kind": p.get("kind", ""), "status": p.get("status"), "title": p["title"],
                   "description": p["description"], "h1": p["h1"], "h2": p["h2"][:12], "text": p["text"][:TEXT_LIMIT],
                   "lang": p["lang"]} for p in parsed],
        "key_pages": key_pages, "org": {k: v for k, v in org.items() if not k.startswith("_")},
        "org_source": org.get("_source", ""), "products": products, "currencies": currencies,
        "offerings": offerings, "contact": contact, "socials": contact["socials"], "tech": tech,
        "copyright": copyright_info, "founded": founded, "size": size,
        "founded_quotes": founded_quotes, "size_quotes": size_quotes,
        "industry_guess": industry if score >= 3 else "", "audience_terms": [t for t, _ in audience.most_common(8)],
        "markets": [m for m, _ in markets.most_common(4)], "tld_country": _tld_country(domain),
        "has_blog": has_blog, "sitemap_urls": sitemap_urls, "robots_txt": bool(robots_text),
        "home": {"status": (home or {}).get("status"), "ms": (home or {}).get("ms"),
                 "https": final.startswith("https://"),
                 "robots": home_parsed.get("robots", ""), "description": home_parsed.get("description", ""),
                 "h1": home_parsed.get("h1", []), "title": home_parsed.get("title", ""),
                 "hreflang": home_parsed.get("hreflang", [])},
        "crawl": {k: v for k, v in crawl.items() if k not in ("page_list", "externals")} if crawl else None,
        "errors": errors,
    }


# --------------------------------------------------------------------------------------------
# Rule-based report
# --------------------------------------------------------------------------------------------
def _empty_report(url: str) -> dict:
    return {
        "url": url, "generated_at": time.strftime("%Y-%m-%d %H:%M"), "ai_used": False, "note": "",
        "company": {"name": "", "legal_name": "", "tagline": "", "what_they_do": "", "founded": "", "headquarters": "",
                    "size": "", "industry": ""},
        "offerings": [], "audience": [],
        "market": {"segment": "", "positioning": "", "geography": "", "trends": []},
        "competitors": [],
        "contact": {"emails": [], "phones": [], "address": "", "socials": {}},
        "tech_stack": {"cms": "", "ecommerce": "", "analytics": [], "hosting": "", "cdn": "", "other": []},
        "seo_snapshot": {"health_score": None, "pages": 0, "top_issues": [], "business_impact": "", "checked": "none"},
        "opportunities": [], "risks": [], "sources": [], "ai_unverified": [], "ai": {"provider": "", "model": ""},
    }


def _quick_checks(facts: dict) -> List[str]:
    pages = [p for p in facts["pages"] if p.get("status") and p["status"] < 400]
    n = len(pages)
    if not n:
        return []
    out = []
    bad = [p for p in facts["pages"] if p.get("status") and p["status"] >= 400]
    if bad:
        codes = ", ".join(str(p["status"]) for p in bad[:3])
        out.append(f"{_n(len(bad), 'key page')} {'returns' if len(bad) == 1 else 'return'} an error ({codes})")
    for label, test in (("no meta description", lambda p: not p["description"]),
                        ("no H1 heading", lambda p: not p["h1"]), ("more than one H1", lambda p: len(p["h1"]) > 1),
                        ("no page title", lambda p: not p["title"])):
        k = sum(1 for p in pages if test(p))
        if k:
            out.append(f"{k} of {n} key pages {'has' if k == 1 else 'have'} {label}")
    return out


def _seo_snapshot(facts: dict) -> dict:
    crawl = facts.get("crawl")
    if not crawl:
        checks = _quick_checks(facts)
        n = len([p for p in facts["pages"] if p.get("status") and p["status"] < 400])
        impact = ("Not checked: run a full crawl for the health score. "
                  + (f"A quick look at {n} key pages found {len(checks)} thing{'s' if len(checks) != 1 else ''} to fix."
                     if checks else f"A quick look at {n} key pages found no basic problems." if n else ""))
        return {"health_score": None, "pages": n, "top_issues": checks[:5], "business_impact": impact.strip(),
                "checked": "quick" if n else "none"}
    top = []
    if crawl["broken_4xx"]:
        top.append(f"{_n(crawl['broken_4xx'], 'broken URL')} (4xx Client Error)")
    if crawl["server_5xx"]:
        top.append(f"{_n(crawl['server_5xx'], 'server error')} (5xx Server Error)")
    if crawl["no_response"]:
        top.append(f"{_n(crawl['no_response'], 'URL')} with no response")
    # status buckets come from the HTTP status above (one source of truth); the SEO list adds the rest
    issues = [i for i in crawl["issues"] if not i["name"].startswith("Response Codes")]
    for i in issues:
        if len(top) >= 5:
            break
        if i["type"] in ("Issue", "Warning") and not i.get("deliberate"):
            top.append(f"{i['name']} ({_n(i['urls'], 'URL')})")
    pages, ok = crawl["pages"], crawl["pages_ok"]
    failing = pages - ok
    if failing:
        impact = (f"{failing:,} of {_n(pages, 'page')} {'fails' if failing == 1 else 'fail'} to load "
                  f"({failing / max(pages, 1):.0%}): visitors and search engines hit dead ends there, which costs "
                  "leads and rankings.")
    elif [i for i in issues if not i.get("deliberate")]:
        first = next(i for i in issues if not i.get("deliberate"))
        impact = (f"All {_n(pages, 'page')} load. The biggest gain now is fixing “{first['name']}” on "
                  f"{_n(first['urls'], 'URL')}.")
    else:
        impact = f"All {pages:,} pages load and no major SEO issues were found."
    return {"health_score": crawl["health_score"], "pages": pages, "top_issues": top[:5], "business_impact": impact,
            "checked": "crawl"}


def _opportunities_risks(facts: dict) -> Tuple[List[str], List[str]]:
    opp, risk = [], []
    tech, org, crawl, home = facts["tech"], facts["org"], facts.get("crawl"), facts["home"]
    contact = facts["contact"]
    if crawl:
        if crawl["broken_4xx"]:
            n = crawl["broken_4xx"]
            risk.append(f"{_n(n, 'URL')} {'answers' if n == 1 else 'answer'} “not found” (4xx): visitors "
                        "and Google hit dead ends.")
        if crawl["server_5xx"]:
            n = crawl["server_5xx"]
            risk.append(f"{_n(n, 'URL')} {'fails' if n == 1 else 'fail'} with server errors (5xx): lost visits "
                        "and crawl budget.")
        if crawl["no_response"]:
            risk.append(f"{_n(crawl['no_response'], 'URL')} gave no response at all.")
        high = [i for i in crawl["issues"] if i["priority"] == "High" and i["type"] == "Issue"
                and not i["name"].startswith("Response Codes") and not i.get("deliberate")][:2]
        for i in high:
            opp.append(f"Fix “{i['name']}” on {_n(i['urls'], 'URL')}.")
    page_ok = bool(home.get("status") and home["status"] < 400)
    h1 = (home.get("h1") or [""])[0]
    ref_words = set(re.findall(r"[a-z]{4,}", f"{home.get('title', '')} {home.get('description', '')}".lower()))
    if page_ok and h1 and len(h1.split()) <= 3 and not set(re.findall(r"[a-z]{4,}", h1.lower())) & ref_words:
        opp.append(f"Make the home page H1 (now “{_clip(h1, 1, 40)}”) say what you offer and for whom.")
    elif page_ok and not h1:
        opp.append("Give the home page one H1 heading that says what you offer and for whom.")
    desc = home.get("description") or ""
    if page_ok and not desc:
        opp.append("Write a home page meta description that states the offer, so search results invite the click.")
    elif len(desc) > 160:
        opp.append(f"Shorten the home meta description ({len(desc)} characters; Google shows about 155).")
    if not org:
        opp.append("Add Organization schema (name, logo, contacts, social profiles) so Google can show a brand panel.")
    elif contact["addresses"] and not any(re.search("LocalBusiness|Store|Service", t) for t in org.get("types", [])):
        opp.append("Add LocalBusiness schema with the office address to show up in local searches.")
    elif not org.get("address") and not org.get("telephone"):
        opp.append("Add the address and phone number to the Organization schema so Google can trust the contact "
                   "details.")
    if not contact["addresses"]:
        opp.append("Show the office address on the site to build trust and local visibility.")
    if not tech["analytics"]:
        opp.append("Install analytics (GA4 through Google Tag Manager) to measure leads and sales.")
    elif tech["ecommerce"] and not any(a in tech["analytics"] for a in ("Meta Pixel", "Google Ads", "TikTok Pixel")):
        opp.append("Add ad conversion tags (Google Ads, Meta Pixel) to measure and retarget shoppers.")
    if not facts["has_blog"]:
        opp.append("Start a blog or resource hub answering buyers' questions to earn search traffic.")
    if "LinkedIn" not in facts["socials"]:
        opp.append("Link the company's LinkedIn page from the site footer to build trust.")
    if crawl and crawl["pages"] < 15:
        opp.append("Create one page per service or product so each can rank for its own searches.")
    if facts["sitemap_urls"] is None:
        opp.append("Publish an XML sitemap and list it in robots.txt so new pages are found faster.")
    if home.get("status") and not home.get("https"):
        risk.append("The site does not use HTTPS: browsers mark it “Not secure”.")
    if "noindex" in (home.get("robots") or ""):
        risk.append("The home page tells search engines not to index it (noindex).")
    # the fastest of our measurements: one fetch made while other jobs load the server is no proof of slowness
    ms = min([m for m in (home.get("ms"), (crawl or {}).get("home_ms")) if m] or [0])
    if ms > 2500:
        risk.append(f"The home page took {ms / 1000:.1f} s to respond: slow pages lose visitors.")
    year = (facts.get("copyright") or {}).get("year")
    if year and year < time.localtime().tm_year - 1:
        risk.append(f"The footer still says © {year}: the site can look unmaintained.")
    if not contact["emails"] and not contact["phones"]:
        risk.append("No email or phone number was found on the key pages: leads may not know how to reach you.")
    elif contact["emails"] and all(e.split("@")[-1] in FREE_MAIL for e, _ in contact["emails"]):
        risk.append("The contact email is a free webmail address, which looks less trustworthy.")
    elif contact["emails"] and all(re.match(r"(hr|careers?|jobs?|recruit\w*|hiring|talent)@", e)
                                   for e, _ in contact["emails"]):
        risk.append(f"Only recruitment email addresses were found ({contact['emails'][0][0]}): sales enquiries "
                    "have no direct email.")
    if len(tech["plugins"]) >= 20:
        risk.append(f"{len(tech['plugins'])} WordPress plugins are loaded: each one adds security and speed risk.")
    if "Universal Analytics (retired)" in tech["analytics"]:
        risk.append("The site still loads retired Universal Analytics code, which no longer collects data.")
    if home.get("status") and home["status"] >= 400:
        risk.insert(0, f"The home page answers HTTP {home['status']}.")
    if not crawl:
        checks = _quick_checks(facts)
        if checks:
            opp.insert(0, f"Fix the basics: {checks[0]}.")
        n = facts.get("sitemap_urls")
        opp = opp[:MAX_LIST - 1] + ["Run a full crawl to find broken links and SEO issues on every page"
                                    + (f" ({n:,} URLs in the sitemap)." if n else ".")]
    return opp[:MAX_LIST], risk[:MAX_LIST]


def rule_based_report(facts: dict) -> dict:
    """BusinessReport from site facts only (no AI)."""
    rep = _empty_report(facts["url"])
    final = facts["final_url"]
    org, tech, contact = facts["org"], facts["tech"], facts["contact"]
    home = next((p for p in facts["pages"] if p["kind"] == "home"), facts["pages"][0] if facts["pages"] else {})
    sources: List[dict] = []

    def src(claim: str, url: str):
        if claim and url:
            sources.append({"claim": _clip(claim, 1, 140), "from": "site", "url": url})
    c = rep["company"]
    c["name"] = facts["name"]
    src(f"Company name: {c['name']}", facts.get("name_source") or final)
    legal = org.get("legal_name") or (facts.get("copyright") or {}).get("legal_name", "")
    if legal and legal.lower() != c["name"].lower():
        c["legal_name"] = legal
        src(f"Legal name: {legal}", facts.get("org_source") if org.get("legal_name") else facts["copyright"].get("url"))
    tagline = org.get("slogan") or ""
    title_head = re.split(r"\s[|–—\-:]\s", home.get("title") or "")[0].strip()
    if not tagline and 3 <= len(title_head.split()) <= 12 and title_head.lower() != c["name"].lower():
        tagline = title_head
    if not tagline and home.get("h1") and 1 < len(home["h1"][0].split()) <= 12:
        tagline = home["h1"][0]
    c["tagline"] = _clip(tagline, 1, 120)
    if c["tagline"]:
        src(f"Tagline: {c['tagline']}", home.get("url") or final)
    what = org.get("description") or home.get("description") or ""
    c["what_they_do"] = _clip(what)
    if c["what_they_do"]:
        src("What they do (site description)", facts.get("org_source") if org.get("description") else home.get("url"))
    if facts["founded"]:
        c["founded"] = facts["founded"]["value"]
        src(f"Founded {c['founded']}", facts["founded"]["url"])
    if contact["addresses"]:
        c["headquarters"] = _clip(contact["addresses"][0][0], 1, 160)
        src("Address", contact["addresses"][0][1])
    if facts["size"]:
        c["size"] = facts["size"]["value"]
        src(f"Size: {c['size']}", facts["size"]["url"])
    c["industry"] = facts.get("industry_guess", "")
    if c["industry"]:
        src(f"Industry {c['industry']} (inferred from the site's wording)", final)
    rep["offerings"] = [o["name"] for o in facts["offerings"][:MAX_LIST]]
    for o in facts["offerings"][:MAX_LIST]:
        src(f"Offering: {o['name']}", o["url"])
    rep["audience"] = [t[0].upper() + t[1:] for t in facts["audience_terms"][:MAX_LIST]]
    m = rep["market"]
    if tech["ecommerce"]:
        m["segment"] = f"Online store ({tech['ecommerce']})"
    elif c["industry"]:
        m["segment"] = c["industry"]
    geo = list(facts["markets"])
    if facts.get("tld_country") and facts["tld_country"] not in geo:
        geo.append(f"{facts['tld_country']} (domain)")
    if org.get("area_served"):
        geo.insert(0, org["area_served"])
    m["geography"] = ", ".join(_uniq(geo, limit=4))
    if m["geography"]:
        src(f"Markets mentioned: {m['geography']}", final)
    rep["contact"] = {
        "emails": [e for e, _ in contact["emails"]], "phones": [p for p, _ in contact["phones"]],
        "address": contact["addresses"][0][0] if contact["addresses"] else "", "socials": dict(contact["socials"]),
    }
    for e, s in contact["emails"][:2]:
        src(f"Email {e}", s)
    for p, s in contact["phones"][:2]:
        src(f"Phone {p}", s)
    other = list(tech["other"])
    if tech.get("plugins"):
        other = [f"WordPress plugins: {', '.join(tech['plugins'][:6])}"
                 + (f" +{len(tech['plugins']) - 6} more" if len(tech["plugins"]) > 6 else "")] + other
    rep["tech_stack"] = {"cms": tech["cms"], "ecommerce": tech["ecommerce"], "analytics": tech["analytics"][:8],
                         "hosting": tech["hosting"], "cdn": tech["cdn"], "other": other[:8]}
    if tech["cms"] or tech["ecommerce"]:
        src(f"Platform: {tech['cms'] or tech['ecommerce']} (detected in HTML/headers)", final)
    rep["seo_snapshot"] = _seo_snapshot(facts)
    rep["opportunities"], rep["risks"] = _opportunities_risks(facts)
    rep["sources"] = _uniq(sources, key=lambda s: s["claim"].lower(), limit=24)
    rep["url"] = final or facts["url"]
    return rep


# --------------------------------------------------------------------------------------------
# AI merge
# --------------------------------------------------------------------------------------------
class _AICompany(BaseModel):
    name: str = ""
    legal_name: str = ""
    tagline: str = ""
    what_they_do: str = ""
    founded: str = ""
    headquarters: str = ""
    size: str = ""
    industry: str = ""


class _AIMarket(BaseModel):
    segment: str = ""
    positioning: str = ""
    geography: str = ""
    trends: List[str] = Field(default_factory=list)


class _AICompetitor(BaseModel):
    name: str = ""
    url: str = ""
    note: str = ""


class _AISource(BaseModel):
    claim: str = ""
    origin: str = "site"
    url: str = ""


class _AIBusiness(BaseModel):
    company: _AICompany = Field(default_factory=_AICompany)
    offerings: List[str] = Field(default_factory=list)
    audience: List[str] = Field(default_factory=list)
    market: _AIMarket = Field(default_factory=_AIMarket)
    competitors: List[_AICompetitor] = Field(default_factory=list)
    seo_business_impact: str = ""
    opportunities: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    sources: List[_AISource] = Field(default_factory=list)


BUSINESS_SYSTEM = (
    "You are a business analyst at an SEO agency writing a short company profile for the team. Use ONLY the "
    "evidence given (site facts, the rule-based draft, research findings, crawled page excerpts). Never invent: "
    "a field the evidence does not support stays an empty string. Founded year, size, headquarters and legal "
    "name must come word for word from the evidence. You MAY add market positioning, trends and up to 5 "
    "likely competitors from your general knowledge (companies of similar size with a similar offer and market, "
    "not industry giants unless the evidence shows this company is one): give each such claim a source with "
    "origin \"ai\", and end "
    f"every competitor note with \"({ai.UNVERIFIED})\". Every list has at most 6 items; every string is at most "
    "2 short sentences in plain language, no marketing fluff. Opportunities and risks are concrete actions or "
    "threats for this business, using the SEO numbers given. sources: one entry per factual claim with origin "
    "\"site\" (the company's pages), \"web\" (Wikipedia/Wikidata) or \"ai\", and the evidence URL."
)


def _facts_for_prompt(facts: dict, draft: dict) -> str:
    keep = {k: facts.get(k) for k in ("url", "domain", "name", "site_name", "org", "products", "currencies",
                                      "founded", "size", "industry_guess", "audience_terms", "markets",
                                      "tld_country", "copyright", "has_blog", "sitemap_urls")}
    keep["offerings"] = facts["offerings"][:12]
    keep["tech"] = {k: facts["tech"].get(k) for k in ("cms", "ecommerce", "analytics", "hosting", "cdn", "plugins")}
    keep["key_pages"] = {p["kind"] or "page": {"url": p["url"], "title": p["title"], "description": p["description"],
                                               "h1": p["h1"][:2], "h2": p["h2"][:8]} for p in facts["pages"]}
    keep["seo"] = draft["seo_snapshot"]
    if facts.get("crawl"):
        keep["crawl_issues"] = [f"{i['name']} ({i['urls']} URLs, {i['priority']})"
                                + (f" - {i['deliberate']}, not a risk" if i.get("deliberate") else "")
                                for i in facts["crawl"]["issues"][:8]]
    d = {k: draft[k] for k in ("company", "offerings", "audience", "market", "opportunities", "risks")}
    return ("SITE FACTS (JSON):\n" + json.dumps(keep, ensure_ascii=False, default=str)[:9000]
            + "\n\nRULE-BASED DRAFT (JSON):\n" + json.dumps(d, ensure_ascii=False)[:4000])


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _backed(value: str, evidence: str) -> bool:
    """A factual value is backed when its key tokens (years/numbers, else the first words) appear in the evidence."""
    v = _norm(value)
    if not v:
        return False
    nums = re.findall(r"\d+", v)
    if nums:   # whole numbers only: "12" is not backed by a phone number "0120"
        tokens = set(evidence.split())
        return all(n in tokens for n in nums)
    words = [w for w in v.split() if len(w) > 2][:3]
    return bool(words) and all(w in evidence for w in words)


def _same_point(a: str, b: str) -> bool:
    """Two opportunity / risk lines that make the same point (the same count, or mostly the same words)."""
    nums_a = {n for n in re.findall(r"\d[\d,]*", a) if n.replace(",", "") not in ("0", "1")}
    if nums_a & set(re.findall(r"\d[\d,]*", b)):
        return True
    wa = {w for w in re.findall(r"[a-z]{4,}", a.lower())}
    wb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
    shared = len(wa & wb)
    return bool(wa and wb) and (shared >= 0.5 * min(len(wa), len(wb)) or (shared >= 3 and shared >= 0.33 * min(len(wa), len(wb))))


def _merge_ai(rep: dict, out: _AIBusiness, facts: dict, research: Optional[dict]) -> dict:
    web_urls = {s.get("url") for s in (research or {}).get("sources", []) + (research or {}).get("evidence", [])
                if s.get("url") and not _same_site(s.get("url", ""), facts["domain"])}
    evidence = _norm(" ".join([json.dumps(facts.get("org", {})), " ".join(p["text"] for p in facts["pages"]),
                               json.dumps(facts.get("copyright", {})), (research or {}).get("text", ""),
                               " ".join(f["text"] for f in (research or {}).get("findings", []))]))
    # founded / size: a year or number somewhere on the pages is not enough ("a bestseller since 2021"); it must
    # be in a sentence about the company itself, in schema.org data, or in a Wikipedia / Wikidata finding
    web_text = " ".join(f["text"] for f in (research or {}).get("findings", []) if f.get("from") == "web")
    strict = {key: _norm(" ".join([json.dumps(facts.get("org", {})), (facts.get(key) or {}).get("value", ""),
                                   " ".join(facts.get(f"{key}_quotes") or []), web_text]))
              for key in ("founded", "size")}
    c = rep["company"]
    for key in ("name", "tagline", "what_they_do", "industry"):
        val = _clip(getattr(out.company, key), 2, 300 if key == "what_they_do" else 140)
        if val:
            c[key] = val
    for key in ("legal_name", "founded", "headquarters", "size"):
        val = _clip(getattr(out.company, key), 1, 160)
        if val and _backed(val, strict.get(key, evidence)):
            c[key] = val
    for key in ("offerings", "audience"):
        vals = _uniq([_clip(v, 2, 80) for v in getattr(out, key)])
        if vals:
            rep[key] = vals
    for key in ("opportunities", "risks"):
        # the rule-based items are checked facts (an email, a schema, a number): keep those the AI did not
        # already say, after the AI's own wording
        vals = _uniq([_clip(v, 2, 200) for v in getattr(out, key)])
        rest = [r for r in rep[key] if not any(_same_point(r, v) for v in vals)]
        if vals:
            rep[key] = _uniq(vals[:4] + rest + vals[4:])
    m = rep["market"]
    unverified = set()
    for key in ("segment", "positioning", "geography"):
        val = _clip(getattr(out.market, key), 2, 220)
        if val:
            m[key] = val
    trends = _uniq([_clip(t, 1, 160) for t in out.market.trends])
    if trends:
        m["trends"] = trends
    comps = []
    for comp in out.competitors:
        name = _clean(comp.name)
        if not name or _norm(name) == _norm(c["name"]) or (comp.url and _host(comp.url) == facts["domain"]):
            continue
        note = _clip(comp.note, 1, 160)
        if comp.url not in web_urls and ai.UNVERIFIED not in note:
            note = (note + " " if note else "") + f"({ai.UNVERIFIED})"
        url = comp.url.strip()
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url}" if "." in url and " " not in url else ""
        comps.append({"name": name[:80], "url": url, "note": note})
    rep["competitors"] = comps[:MAX_LIST]
    impact = _clip(out.seo_business_impact, 2, 300)
    if impact and rep["seo_snapshot"]["checked"] == "crawl":   # without a crawl the text keeps "Not checked"
        rep["seo_snapshot"]["business_impact"] = impact
    sources = list(rep["sources"])
    cited = {s["url"] for s in sources}
    for s in out.sources:
        claim = _clip(s.claim, 1, 140)
        if not claim:
            continue
        if s.origin == "site" and s.url.strip() in cited:
            continue      # that page already backs a specific site fact
        if FACT_CLAIM_RE.search(claim) and not _backed(claim, strict["founded"] + " " + strict["size"]):
            continue      # a founding year / size the company statements do not back (see strict above)
        origin, url = s.origin, s.url.strip()
        if origin == "site" and not (url and _same_site(url, facts["domain"])):
            origin = "ai"
        if origin == "web" and url not in web_urls:
            origin = "ai"
        if origin not in ("site", "web"):
            origin, url = "ai", ""
            unverified.add("market" if any(w in claim.lower() for w in ("market", "trend", "position", "segment"))
                           else "competitors" if "compet" in claim.lower() else "other")
        sources.append({"claim": claim, "from": origin, "url": url})
    for f in (research or {}).get("findings", []):
        if f["from"] == "web":
            sources.append({"claim": _clip(f["text"], 1, 140), "from": "web", "url": f["url"]})
    if rep["competitors"]:
        unverified.add("competitors")
    if m["trends"] or m["positioning"]:
        unverified.add("market")
    rep["sources"] = _uniq(sources, key=lambda s: s["claim"].lower(), limit=26)
    rep["ai_unverified"] = sorted(u for u in unverified if u != "other")
    return rep


# --------------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------------
def build_business_report(url: str = "", crawler=None, progress: Optional[Callable] = None,
                          facts: Optional[dict] = None, stop_event=None) -> dict:
    """BusinessReport dict (see module docstring). progress(pct, message) is called along the way.
    stop_event (threading.Event, optional): once set, or once a newer crawl starts (ai.reset_usage), the AI
    steps are skipped or abandoned and the rule-based report is returned with a note saying so."""
    with ai.job_scope(stop_event):
        return _build_business_report(url, crawler, progress, facts)


def _build_business_report(url: str, crawler, progress: Optional[Callable], facts: Optional[dict]) -> dict:
    _progress(progress, 2, "Starting")
    if facts is None:
        facts = collect_site_facts(url, crawler=crawler, progress=progress)
    _progress(progress, 50, "Building the profile from site facts")
    rep = rule_based_report(facts)
    status = ai.ai_status()
    if not facts["pages"]:
        rep["note"] = "The site could not be read: " + "; ".join(facts["errors"][:2])
        _progress(progress, 100, "Done")
        return rep
    if status["enabled"] and ai.cancelled():
        rep["note"] = ai.STOPPED_NOTE
        _progress(progress, 100, "Stopped")
        return rep
    if not status["enabled"]:
        rep["note"] = ai.AI_OFF_NOTE
        _progress(progress, 100, "Done")
        return rep
    if facts.get("private") and not status["private_ok"]:
        # a logged-in crawl's facts stay off free cloud providers: say so instead of "AI unavailable"
        rep["note"] = ai.PRIVATE_NOTE
        _progress(progress, 100, "Done")
        return rep
    private = bool(facts.get("private")) and not status["private_ok"]
    _progress(progress, 58, "Researching (company pages, Wikipedia, Wikidata)")
    research_facts = {"url": facts["final_url"], "domain": facts["domain"], "name": facts["name"],
                      "org": facts["org"], "socials": facts["socials"],
                      "pages": [{"url": p["url"], "title": p["title"], "text": p["text"][:2500]}
                                for p in facts["pages"] if p.get("status") and p["status"] < 400][:6]}
    question = (f"What does {facts['name']} ({facts['domain']}) do, for whom, where, since when and how big is it? "
                "Which market segment is it in and who are its likely competitors?")
    research = ai.research(question, "You are a careful business researcher.", research_facts)
    if ai.cancelled():
        rep["note"] = ai.STOPPED_NOTE
        _progress(progress, 100, "Stopped")
        return rep
    _progress(progress, 78, "Writing the profile with AI")
    pages = "\n\n".join(ai.wrap_page(p["url"], p["text"], 1500) for p in facts["pages"]
                        if p.get("status") and p["status"] < 400 and p["kind"] in ("home", "about", "services",
                                                                                     "products"))[:7000]
    research_txt = ""
    if research:
        research_txt = ("\n\nRESEARCH SUMMARY:\n" + research["text"] + "\nFINDINGS:\n" +
                        "\n".join(f"- [{f['from']}] {f['text']} {f['url']}" for f in research["findings"]))
    content = _facts_for_prompt(facts, rep) + research_txt + "\n\nPAGE EXCERPTS:\n" + pages
    out = ai.ask_json(BUSINESS_SYSTEM, content, _AIBusiness, effort="low", max_tokens=3000, private=private)
    if out is None:
        rep["note"] = ai.fallback_note()
        _progress(progress, 100, "Stopped" if rep["note"] == ai.STOPPED_NOTE else "Done")
        return rep
    rep = _merge_ai(rep, out, facts, research)
    rep["ai_used"] = True
    used = ai.usage_summary()
    rep["ai"] = {"provider": used.get("provider") or status["provider"], "model": used.get("model") or status["model"]}
    rep["note"] = ("AI summary of site facts" + (" and Wikipedia/Wikidata" if research and any(
        f["from"] == "web" for f in research.get("findings", [])) else "") +
        "; competitor and market notes marked unverified come from the AI model's general knowledge.")
    _progress(progress, 100, "Done")
    return rep
