import base64
import ipaddress
import os
import re
from dataclasses import dataclass, replace
from functools import lru_cache
from urllib.parse import (urljoin, urlparse, urlsplit, urlunparse, urlunsplit, parse_qsl, quote_plus,
                          unquote)
from typing import Any, Dict, List, Optional, Pattern, Set, Tuple
from requests.utils import requote_uri
from .config import EXCLUDED_EXTENSIONS, STRIP_QUERY_PARAMS

IGNORED_SCHEMES = {
    "mailto", "tel", "javascript", "whatsapp", "callto", "sms",
    "data", "file", "ftp", "irc", "skype", "intent"
}

MAX_REWRITE_RULES = 50       # regex replace rules kept per crawl
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_JAVA_GROUP = re.compile(r"\$(\d{1,2})")


@dataclass(frozen=True)
class UrlRules:
    """How URLs found during a crawl are rewritten (Configuration > URL Rewriting). DEFAULT_RULES is the built-in
    normalisation: tracking parameters (STRIP_QUERY_PARAMS and every utm_* name) removed, the rest sorted.
    Parameter names match without regard to case; a name ending in "*" matches every name starting with it.
    Hashable, so normalised URLs can be cached per rule set."""
    remove_names: frozenset = frozenset(STRIP_QUERY_PARAMS)
    remove_prefixes: Tuple[str, ...] = ("utm_",)
    remove_all_params: bool = False
    sort_params: bool = True
    regex_replace: Tuple[Tuple[Pattern, str], ...] = ()
    lowercase: bool = False
    percent_lower: bool = False

    def drops(self, name: str) -> bool:
        key = name.lower()
        return key in self.remove_names or (bool(self.remove_prefixes) and key.startswith(self.remove_prefixes))

    @property
    def rewrites(self) -> bool:
        """Steps that run on the normalised URL (regex replace, lowercase, percent-escape case)."""
        return bool(self.regex_replace or self.lowercase or self.percent_lower)


DEFAULT_RULES = UrlRules()
_active = {"rules": DEFAULT_RULES}


def set_active_rules(rules: Optional[UrlRules]) -> None:
    """Makes `rules` the rules normalize_url() uses when it is not given any: the crawl engine sets its crawl's
    rules when it starts, so every module comparing URLs of that crawl rewrites them the same way (one crawl
    runs per server). None restores DEFAULT_RULES."""
    _active["rules"] = rules or DEFAULT_RULES


def active_rules() -> UrlRules:
    return _active["rules"]


def rules_from_config(rewrite: Optional[Dict[str, Any]]) -> Tuple[UrlRules, List[str]]:
    """(UrlRules, problems) from CrawlConfig.url_rewrite. Missing keys keep the built-in behaviour; invalid
    regexes are left out and named in problems. Java-style "$1" in a replacement means group 1."""
    problems: List[str] = []
    if not isinstance(rewrite, dict):
        return DEFAULT_RULES, problems
    names: Set[str] = set()
    prefixes: List[str] = []
    params = rewrite.get("remove_params")
    if params is None:
        names, prefixes = set(DEFAULT_RULES.remove_names), list(DEFAULT_RULES.remove_prefixes)
    else:
        if isinstance(params, str):
            params = params.splitlines()
        for raw in params or ():
            name = str(raw or "").strip().lower()
            if not name:
                continue
            if name.endswith("*"):
                if name[:-1] and name[:-1] not in prefixes:
                    prefixes.append(name[:-1])
            else:
                names.add(name)
    compiled: List[Tuple[Pattern, str]] = []
    for i, rule in enumerate(rewrite.get("regex_replace") or ()):
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("regex") or "")
        if not pattern:
            continue
        if len(compiled) >= MAX_REWRITE_RULES:
            problems.append(f"Only the first {MAX_REWRITE_RULES} regex replace rules are used.")
            break
        repl = _JAVA_GROUP.sub(r"\\g<\1>", str(rule.get("replace") or ""))
        try:
            rx = re.compile(pattern)
            rx.sub(repl, "https://example.com/")   # a bad group reference fails here, not on every URL
        except (re.error, IndexError) as e:
            problems.append(f"Regex replace rule {i + 1} ({pattern[:60]!r}) is not valid and was left out: {e}")
            continue
        compiled.append((rx, repl))
    enc = str(rewrite.get("percent_encoding") or "upper").strip().lower()
    rules = UrlRules(
        remove_names=frozenset(names), remove_prefixes=tuple(prefixes),
        remove_all_params=bool(rewrite.get("remove_all_params")),
        sort_params=rewrite.get("sort_params") is not False,
        regex_replace=tuple(compiled), lowercase=bool(rewrite.get("lowercase")),
        percent_lower=enc == "lower")
    return (DEFAULT_RULES if rules == DEFAULT_RULES else rules), problems


def _rewrite(url: str, rules: UrlRules) -> str:
    """Regex replace rules (in order), lowercase, then the URL is normalised again with the same parameter
    rules (a rule may add a parameter or change the host) and escapes get the chosen case."""
    out = url
    for rx, repl in rules.regex_replace:
        try:
            out = rx.sub(repl, out)
        except (re.error, IndexError):
            continue
    if rules.lowercase:
        out = out.lower()
    if out != url:
        out = normalize_url(out, rules=replace(rules, regex_replace=(), lowercase=False, percent_lower=False)) \
            or url
    if rules.percent_lower:
        out = _PERCENT_ESCAPE.sub(lambda m: m.group(0).lower(), out)
    return out

def clean_hostname(netloc: str) -> str:
    """Extract and lowercase hostname without port or www prefix."""
    host = netloc.split(":")[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host

def split_url_credentials(url: str) -> Tuple[str, str, str]:
    """(url without its "user:password@", user, password): the user name and password a browser would send as
    HTTP Basic authentication. ("url", "", "") when the URL has none."""
    text = str(url or "")
    try:
        parts = urlsplit(text)
    except ValueError:
        return text, "", ""
    if "@" not in parts.netloc:
        return text, "", ""
    userinfo, _, host = parts.netloc.rpartition("@")
    user, _, password = userinfo.partition(":")
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)), unquote(user), unquote(password)


def basic_auth_value(user: str, password: str) -> str:
    """The Authorization header value of HTTP Basic authentication ("Basic " + base64 of "user:password")."""
    return "Basic " + base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")


def move_url_credentials(config: Any) -> List[str]:
    """A user name and password written in the start URL (https://user:pass@host/: HTTP Basic authentication), or
    in List mode's URLs, are sent as an Authorization header to the crawled site only (config.request_headers,
    which the crawl and its Chrome renders send to the site and nowhere else) and are taken out of the URLs, so
    no crawled URL, export, file name, log line or saved crawl carries the password. An Authorization header the
    user set already wins. Marks config._url_auth; returns the values to mask (password, header value, token).
    Calling it again changes nothing."""
    creds: Optional[Tuple[str, str]] = None
    url, user, password = split_url_credentials(getattr(config, "start_url", "") or "")
    if user or password:
        config.start_url = url
        creds = (user, password)
    seeds = getattr(config, "seed_urls", None)
    if isinstance(seeds, list) and any("@" in str(s) for s in seeds):
        cleaned = []
        for s in seeds:
            u2, us, pw2 = split_url_credentials(str(s))
            if (us or pw2) and creds is None:
                creds = (us, pw2)
            cleaned.append(u2 if (us or pw2) else s)
        config.seed_urls = cleaned
    if creds is None:
        return []
    value = basic_auth_value(*creds)
    headers = [h for h in (getattr(config, "request_headers", None) or []) if isinstance(h, dict)]
    if not any(str(h.get("name") or "").strip().lower() == "authorization" for h in headers):
        config.request_headers = headers + [{"name": "Authorization", "value": value}]
    try:
        config._url_auth = True
    except AttributeError:
        pass
    return [s for s in (creds[1], value, value.split(" ", 1)[1]) if len(s) >= 4]


# ---------------------------------------------------------------------------------------------
# Registrable domain ("the site": example.co.uk for shop.example.co.uk), offline. A compact built-in subset of the
# public suffix rules: country-code registries that register names one level down (co.uk, com.au, co.in, ne.jp,
# state.us...), common hosting platforms where every customer gets a subdomain (myapp.vercel.app is a site of its
# own, never "vercel.app"), and cloud hosts whose every name is a separate site. Unknown multi-part suffixes fall
# back to the last two labels.
# ---------------------------------------------------------------------------------------------
_CC_SECOND_LEVEL = frozenset((
    "ac", "ad", "art", "asn", "biz", "co", "com", "conf", "ed", "edu", "firm", "gen", "geek", "go", "gob", "gouv",
    "gov", "govt", "gr", "id", "idv", "in", "ind", "info", "int", "iwi", "kiwi", "law", "lg", "ltd", "maori", "me",
    "med", "mil", "mod", "mus", "name", "nat", "ne", "net", "ngo", "nhs", "nic", "nom", "or", "org", "plc",
    "police", "pp", "priv", "pro", "res", "sch", "school", "tm",
))
# Country codes whose two-letter second level is a region (ca.us, mi.it), and Japan's prefectures and designated
# cities (example.osaka.jp)
_CC_REGION_2 = frozenset(("us", "it"))
_JP_REGIONS = frozenset((
    "aichi", "akita", "aomori", "chiba", "ehime", "fukui", "fukuoka", "fukushima", "gifu", "gunma", "hiroshima",
    "hokkaido", "hyogo", "ibaraki", "ishikawa", "iwate", "kagawa", "kagoshima", "kanagawa", "kochi", "kumamoto",
    "kyoto", "mie", "miyagi", "miyazaki", "nagano", "nagasaki", "nara", "niigata", "oita", "okayama", "okinawa",
    "osaka", "saga", "saitama", "shiga", "shimane", "shizuoka", "tochigi", "tokushima", "tokyo", "tottori",
    "toyama", "wakayama", "yamagata", "yamaguchi", "yamanashi",
    "kawasaki", "kitakyushu", "kobe", "nagoya", "sapporo", "sendai", "yokohama",
))
# Platforms where the site is one label under the platform's domain (team.github.io, shop.myshopify.com)
_PLATFORM_SUFFIXES = frozenset((
    "github.io", "gitlab.io", "vercel.app", "now.sh", "netlify.app", "herokuapp.com", "pages.dev", "workers.dev",
    "web.app", "firebaseapp.com", "appspot.com", "azurewebsites.net", "azurestaticapps.net", "azureedge.net",
    "cloudapp.net", "trafficmanager.net", "azure-api.net", "cloudfront.net", "blogspot.com", "wordpress.com",
    "wpcomstaging.com", "wixsite.com", "myshopify.com", "onrender.com", "fly.dev", "railway.app",
    "up.railway.app", "glitch.me", "ngrok.io", "ngrok.app", "ngrok-free.app", "repl.co", "replit.app",
    "replit.dev", "surge.sh", "webflow.io", "readthedocs.io", "gitbook.io", "notion.site", "pantheonsite.io",
    "wpengine.com", "wpenginepowered.com", "kinsta.cloud", "b-cdn.net", "r2.dev", "lovable.app", "bubbleapps.io",
    "framer.app", "framer.website", "carrd.co", "square.site", "weebly.com", "deno.dev", "edgeone.app",
    "translate.goog", "hubspotpagebuilder.com",
    # CentralNic-style names sold one level down
    "uk.com", "us.com", "eu.com", "gb.com", "gb.net", "uk.net", "de.com", "jpn.com", "br.com", "cn.com", "za.com",
    "ru.com", "sa.com", "hu.net", "se.net", "in.net", "us.org", "co.com",
))
# Cloud hosts where every full name is a separate customer (bucket.s3.amazonaws.com, svc-x.a.run.app)
_HOST_IS_SITE = frozenset((
    "amazonaws.com", "elasticbeanstalk.com", "awsapprunner.com", "amplifyapp.com", "on.aws",
    "googleusercontent.com", "run.app", "cloudfunctions.net", "cloudapp.azure.com", "azurecontainerapps.io",
))


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


@lru_cache(maxsize=8192)
def registrable_domain(host_or_url: str) -> str:
    """The registrable domain of a host or URL: aristasystems.in for www.aristasystems.in, example.co.uk for
    shop.example.co.uk, team.github.io for docs.team.github.io. Works offline (rules above). An IP address or a
    one-label name (localhost) is its own domain; "" when there is no host."""
    text = str(host_or_url or "").strip().lower()
    if not text:
        return ""
    if "/" in text or "?" in text or "#" in text:
        try:
            text = urlsplit(text if "://" in text else "//" + text).hostname or ""
        except ValueError:
            return ""
    else:
        text = text.rsplit("@", 1)[-1]
        if text.startswith("["):
            return text.split("]", 1)[0].strip("[")
        text = text.split(":", 1)[0]
    text = text.strip().rstrip(".")
    if not text or _is_ip_literal(text):
        return text
    if not text.isascii():
        try:
            text = text.encode("idna").decode("ascii")
        except UnicodeError:
            return text
    labels = text.split(".")
    if len(labels) < 2:
        return text
    for k in range(min(len(labels), 4), 0, -1):
        suffix = ".".join(labels[-k:])
        if suffix in _HOST_IS_SITE:
            return text
        if suffix in _PLATFORM_SUFFIXES:
            return ".".join(labels[-(k + 1):]) if len(labels) > k else text
    tld, second = labels[-1], labels[-2]
    if len(labels) >= 3 and len(tld) == 2 and tld.isalpha() and (
            second in _CC_SECOND_LEVEL or (tld in _CC_REGION_2 and len(second) == 2 and second.isalpha())
            or (tld == "jp" and second in _JP_REGIONS)):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def host_in_domain(host: str, domain: str) -> bool:
    """True when `host` is `domain` itself or one of its subdomains (port and case ignored)."""
    h = str(host or "").strip().lower().split(":", 1)[0].rstrip(".")
    d = str(domain or "").strip().lower().split(":", 1)[0].rstrip(".")
    return bool(h and d) and (h == d or h.endswith("." + d))


def is_internal_address(address: str) -> bool:
    """True for an address ARACHNIX never opens on its own initiative: private networks (10/8, 172.16/12,
    192.168/16, fc00::/7), loopback, link-local (cloud metadata at 169.254.169.254), carrier-grade NAT, multicast,
    reserved and unspecified addresses. Anything that is not an IP address counts as internal (refused)."""
    try:
        ip = ipaddress.ip_address(str(address or "").split("%", 1)[0].strip("[] "))
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (not ip.is_global) or ip.is_multicast


def get_domain(url: str) -> str:
    """Return core domain of a URL (e.g. 'aristasystems.in')."""
    try:
        parsed = urlparse(url)
        return clean_hostname(parsed.netloc)
    except Exception:
        return ""

def is_valid_http_scheme(url: str) -> bool:
    """Check if URL starts with http or https."""
    try:
        scheme = urlparse(url).scheme.lower()
        return scheme in ("http", "https")
    except Exception:
        return False

# What browsers trim around an href (C0 controls and space, WHATWG URL parsing) and drop inside it (tab, newline).
# A no-break space is NOT trimmed: href="/page&nbsp;" opens /page%C2%A0 in a browser, so it does here too.
_URL_TRIM = "".join(chr(c) for c in range(0x21))
_URL_DROP = re.compile(r"[\t\n\r]")


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 5.2.4 as browsers apply it to every http(s) URL, absolute ones included:
    /a/b/../c/./d -> /a/c/d (Python's urljoin leaves an absolute URL's dot segments in place)."""
    if "/." not in path:
        return path
    segments = path.split("/")
    out: List[str] = []
    for seg in segments[1:]:
        if seg == "..":
            if out:
                out.pop()
        elif seg != ".":
            out.append(seg)
    if segments[-1] in (".", ".."):
        out.append("")          # "/a/b/.." is the folder "/a/"
    return "/" + "/".join(out)


def join_url(base: str, ref: str) -> str:
    """`ref` resolved against `base` as a browser resolves a link (RFC 3986 5.2). Python's urljoin drops empty
    path segments when it merges a relative path ("x//y" on /a/ gives /a/x/y, "z" on /a//b/ gives /a/b/z, "../z"
    on /x//y climbs one folder too many), so the URL requested would not be the one the link points to; those
    references are merged here, everything else (absolute, //host, /path, ?query, #fragment) goes to urljoin."""
    try:
        ref = _URL_DROP.sub("", ref.lstrip(_URL_TRIM))   # what urlsplit removes too
        r = urlsplit(ref)
        b = urlsplit(base)
    except (ValueError, TypeError, AttributeError):
        return urljoin(base, ref)
    if r.scheme or r.netloc or not r.path or r.path.startswith("/") or b.scheme not in ("http", "https") \
            or not ref.startswith(r.path):
        return urljoin(base, ref)
    folder = b.path[:b.path.rfind("/") + 1] if b.path else "/"
    # the rest of the reference as written ("?" and "#" included even when empty, as urljoin keeps them)
    return f"{b.scheme}://{b.netloc}{_remove_dot_segments(folder + r.path)}{ref[len(r.path):]}"


def query_params(query: str) -> List[Tuple[str, str, bool]]:
    """(name, value, has "=") for each "&"-separated part of a query, decoded as parse_qsl decodes them ("+" is
    a space). Unlike parse_qsl(keep_blank_values=False) a part with no value is kept, and "?print" stays apart
    from "?print=" (servers may tell them apart). Empty parts ("a=1&&b=2") are dropped, as browsers send them
    unchanged but parse_qsl and every server ignore them."""
    out: List[Tuple[str, str, bool]] = []
    for part in query.split("&"):
        if not part:
            continue
        name, eq, value = part.partition("=")
        out.append((unquote(name.replace("+", " ")), unquote(value.replace("+", " ")), bool(eq)))
    return out


def normalize_url(raw_url: str, base_url: str = "", rules: Optional[UrlRules] = None) -> Optional[str]:
    """
    Resolve relative URLs, strip fragments, normalize slashes,
    remove tracking query params, and canonicalize path.
    `rules` (default: active_rules(), normally DEFAULT_RULES) chooses which parameters are removed, whether the
    rest are sorted, and the URL Rewriting steps (regex replace, lowercase, escape case).
    """
    if not raw_url:
        return None
    if rules is None:
        rules = _active["rules"]

    raw_url = _URL_DROP.sub("", raw_url.strip(_URL_TRIM))
    if not raw_url:
        return None

    # Quick ignore check
    lower_raw = raw_url.lower()
    for sch in IGNORED_SCHEMES:
        if lower_raw.startswith(sch + ":"):
            return None

    # Resolve against base URL if relative
    if base_url:
        try:
            full_url = join_url(base_url, raw_url)
        except Exception:
            return None
    else:
        full_url = raw_url

    try:
        parsed = urlparse(full_url)
    except Exception:
        return None

    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    netloc = parsed.netloc.lower()
    if not netloc.isascii():
        # Internationalised host names travel as punycode, as requests sends them
        try:
            netloc = netloc.encode("idna").decode("ascii")
        except Exception:
            pass
    # Remove standard ports
    if ":" in netloc:
        host, port = netloc.split(":", 1)
        if (scheme == "http" and port == "80") or (scheme == "https" and port == "443"):
            netloc = host

    # Normalize path. Repeated slashes stay as written: a browser (and Screaming Frog) requests /x//y, which
    # can answer differently from /x/y, and the URL tab's "Multiple Slashes" filter must see them
    path = parsed.path
    if not path:
        path = "/"
    else:
        # "." and ".." segments resolved as the browser does (it never requests them)
        path = _remove_dot_segments(path)
        # Percent-encode non-ASCII characters and spaces exactly as requests does on the wire, so the
        # URL we store is the URL fetched (no false redirect, no second copy of the same file)
        path = requote_uri(path)

    # Clean query parameters (a parameter without a value, ?print or ?s=, is kept as written)
    query_str = ""
    if parsed.query and not rules.remove_all_params:
        cleaned = [(k, v, eq) for k, v, eq in query_params(parsed.query) if not rules.drops(k)]
        if cleaned:
            if rules.sort_params:
                cleaned.sort(key=lambda x: x[0])
            query_str = "&".join(quote_plus(k, safe="") + ("=" + quote_plus(v, safe="") if eq else "")
                                 for k, v, eq in cleaned)

    # Reconstruct without fragment
    normalized = urlunparse((scheme, netloc, path, "", query_str, ""))
    if rules.rewrites:
        normalized = _rewrite(normalized, rules)
    return normalized

def is_internal_url(
    url: str,
    base_domain: str,
    include_subdomains: bool = False,
    allowed_domains: Optional[Set[str]] = None
) -> bool:
    """
    Determine if a given URL is strictly internal to the base website.
    """
    if not url:
        return False

    url_domain = get_domain(url)
    if not url_domain:
        return False

    clean_base = clean_hostname(base_domain)
    clean_url = clean_hostname(url_domain)

    # Exact domain match
    if clean_url == clean_base:
        return True

    # Allowed domains list
    if allowed_domains:
        if clean_url in allowed_domains or url_domain in allowed_domains:
            return True
        for ad in allowed_domains:
            if clean_url == clean_hostname(ad):
                return True

    # Subdomain match (e.g. blog.example.com when base is example.com)
    if include_subdomains:
        if clean_url.endswith("." + clean_base):
            return True

    return False

def is_parsable_html_url(url: str) -> bool:
    """
    Check if URL does not end in a known binary or non-HTML asset extension.
    """
    try:
        parsed = urlparse(url)
        path = parsed.path.lower()
        _, ext = os.path.splitext(path)
        if ext and ext in EXCLUDED_EXTENSIONS:
            return False
        return True
    except Exception:
        return True

LOGIN_SEGMENTS = {"login", "log-in", "log_in", "signin", "sign-in", "sign_in", "logon", "auth", "sso",
                  "authenticate", "wp-login.php"}

# Sign-out addresses (/logout, /api/auth/signout, /users/sign_out, ?action=logout, /log-off, WooCommerce's
# /my-account/customer-logout/, ?logout=1, OpenCart's route=account/logout, doLogout): a logged-in crawl never
# requests them, in HTTP or in the browser. The word must start a word or a camelCase part, so
# /catalog-outlet or /blogout-sale are not taken for sign-out links.
LOGOUT_RE = re.compile(r"(?i:(?<![a-z])(?:log[-_]?(?:out|off)|sign[-_]?(?:out|off)))"
                       r"|(?<=[a-z])(?:Log[-_]?(?:[Oo]ut|[Oo]ff)|Sign[-_]?(?:[Oo]ut|[Oo]ff))")
LOGOUT_EXCLUDE_PATTERNS = ["/logout", "/log-out", "/log_out", "/signout", "/sign-out", "/sign_out",
                           "/logoff", "/log-off", "action=logout"]

# Links that change data when they are merely opened (GET) by someone logged in: CMS admin screens (WordPress
# /wp-admin/, whose row actions such as post.php?action=trash or plugins.php?action=deactivate are plain
# links), links carrying a one-time form token (WordPress _wpnonce, Magento form_key, CSRF tokens), cart
# changes and delete/remove/cancel steps. A logged-in crawl, and the browser it uses, never opens them.
ADMIN_PATH_RE = re.compile(r"/wp-admin(?:/|$)", re.I)
ACTION_PARAMS = {"action", "do", "op", "task", "cmd", "act", "operation", "doaction", "action2"}
ACTION_WORD_RE = re.compile(
    r"trash|delet|remov|activat|spam|approv|destroy|cancel|disabl|enabl|upgrad|updat|install|empty|purge|"
    r"reset|revok|clear|suspend|publish|restor|archiv|duplicat|unsubscrib|resend|lock|block|ban(?:$|[-_])|"
    r"log[-_]?out|sign[-_]?out", re.I)
TOKEN_PARAMS = {"_wpnonce", "wpnonce", "_nonce", "nonce", "form_key", "csrf", "_csrf", "csrf_token",
                "csrfmiddlewaretoken", "authenticity_token", "_token", "xsrf", "_xsrf"}
DATA_CHANGE_PARAMS = {"remove_item", "undo_item", "cancel_order", "order_again", "add-to-cart", "add_to_cart",
                      "add-to-wishlist", "remove_from_wishlist", "remove_from_compare", "empty_cart", "empty-cart",
                      "clear-cart", "clear_cart", "delete", "remove", "unsubscribe", "deactivate"}
DATA_CHANGE_SEGMENTS = {"delete", "remove", "destroy", "trash", "deactivate", "unsubscribe", "cancel", "empty",
                        "clear", "form_key", "uenc"}
CART_CHANGE_RE = re.compile(r"/cart/(?:add|change|clear|update|remove)(?:\.js)?(?:/|$)", re.I)


def is_logout_url(url: str) -> bool:
    """True when the URL's path or query names a sign-out action (percent-escapes decoded first, so
    route=account%2Flogout counts)."""
    try:
        p = urlparse(unquote(url or ""))
    except Exception:
        return False
    return bool(LOGOUT_RE.search(p.path + "?" + p.query))


def changes_data_url(url: str) -> bool:
    """True when opening the URL while logged in would change data on the site (see ADMIN_PATH_RE and the
    lists above): an admin screen, an action link with a one-time token, a cart change or a delete step."""
    try:
        p = urlparse(url or "")
        path = unquote(p.path or "")
        query = parse_qsl(p.query, keep_blank_values=True)
    except Exception:
        return False
    if ADMIN_PATH_RE.search(path) or CART_CHANGE_RE.search(path):
        return True
    if any(seg.lower() in DATA_CHANGE_SEGMENTS for seg in path.split("/") if seg):
        return True
    for key, value in query:
        k = key.strip().lower()
        if k in TOKEN_PARAMS or k in DATA_CHANGE_PARAMS:
            return True
        if k in ACTION_PARAMS and ACTION_WORD_RE.search(value or ""):
            return True
    return False


def is_unsafe_url(url: str) -> bool:
    """A sign-out address or a link that changes data (see is_logout_url and changes_data_url): a logged-in
    crawl and its browser never open it."""
    return is_logout_url(url) or changes_data_url(url)

def is_login_url(url: str) -> bool:
    """True when a path segment names a login page (/login, /auth/signin, /wp-login.php).
    Whole segments only, so /author/jane or /authority-guide do not count."""
    try:
        path = urlparse(url).path.lower()
    except Exception:
        return False
    return any(seg in LOGIN_SEGMENTS for seg in path.split("/") if seg)

def redirected_to_login(requested_url: str, final_url: str) -> bool:
    """A request for a normal page that ended on a login page: the session is missing or expired."""
    return bool(final_url) and is_login_url(final_url) and not is_login_url(requested_url)

def is_excluded_path(url: str, exclude_patterns: list) -> bool:
    """Check if URL contains any excluded pattern."""
    if not exclude_patterns:
        return False
    lower_url = url.lower()
    for pattern in exclude_patterns:
        if pattern.lower() in lower_url:
            return True
    return False
