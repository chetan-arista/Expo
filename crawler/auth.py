"""
Logging the crawler in to a protected website, and keeping it logged in.

Ways in (AuthConfig.auth_type):
- "form"    username + password: a plain HTML form is posted over HTTP; forms that need JavaScript (React,
            Next.js, hydrated buttons, CSRF set by script) are filled in headless Chrome. The Chrome that
            logged in stays open and becomes the crawl's shared browser (see browser.SharedBrowser).
- "cookies" a pasted session in any format people copy from their own browser (cookie_import): a Cookie
            header ("a=1; b=2"), a JSON export (Cookie-Editor style, Playwright storage state, or
            {"cookies": [...], "local": {origin: {...}}, "session": {origin: {...}}}), a Netscape cookies.txt,
            a "Copy as cURL" command (bash or cmd) or a "Copy as PowerShell" snippet. preview_cookies() shows
            what was read (values masked); test_cookies() checks the cookies on a page that needs sign-in.
- "live"    the session captured by the live login window (live_browser.LiveBrowserManager.finish).
- "header"  a custom request header (e.g. Authorization), added to every request to the site.

The login is kept as session_state (full cookies + web storage), shared by the HTTP session and Chrome.
Passwords and cookie values are never logged.

Bot checks (CAPTCHA widgets, Cloudflare / firewall challenge pages): ARACHNIX never tries to pass, solve or get
around one and never uses a solving service. detect_bot_check() / browser_bot_check() recognise one, and the
sign-in reports it in plain words with the routes that work (the person's own browser session through Paste
cookies, or an allow rule the site owner adds); see bot_check_message() and challenge_status().
"""
import ipaddress
import json
import os
import re
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .browser import (SharedBrowser, chrome_available, clear_session, cookies_to_jar, export_session,
                      jar_to_cookies, storage_origins)
from .config import AuthConfig, DEFAULT_USER_AGENT
from .cookie_import import CookiePaste, cookie_kind, parse_cookie_paste, preview_cookies  # noqa: F401 (API)
from .net_guard import WAF_SCAN_CHARS, SiteSession, detect_waf
from .url_utils import get_domain, is_internal_url, is_login_url, is_logout_url, is_unsafe_url, normalize_url

AuthResult = Tuple[bool, str, Dict[str, str], Optional[str], Set[str]]

# Bot checks: the plain guidance shown wherever one is met (sign-in test, form login, live window, crawl)
BOT_CHECK_MSG = "This site's sign-in uses a bot check that ARACHNIX can't complete for you."
BOT_CHECK_CRAWL_MSG = "This site answers ARACHNIX with a bot check that ARACHNIX can't complete for you."
BOT_CHECK_COOKIES_MSG = ("This site answers ARACHNIX with a bot check that ARACHNIX can't complete for you, even with "
                         "the pasted cookies.")
BOT_CHECK_ROUTES = ("Sign in with your own browser, then use Paste cookies. If you own the site, allow ARACHNIX in "
                    "your security settings: a rule that skips the check for your office IP address, or for a "
                    "secret request header that ARACHNIX sends (Options > HTTP headers).")
BOT_CHECK_OWNER_ROUTE = ("If you own the site, allow ARACHNIX in your security settings: a rule that skips the check "
                         "for your office IP address, or for a secret request header that ARACHNIX sends "
                         "(Options > HTTP headers).")
CAPTCHA_BLOCKED_MSG = f"{BOT_CHECK_MSG} {BOT_CHECK_ROUTES}"
NO_CHROME_MSG = (
    "This site's login form needs a real browser (it runs on JavaScript), and this server has no Chrome. "
    "Paste session cookies instead, or run the crawler where Chrome is installed."
)
# Login pages tried when no login URL is given and the start page does not lead to one
LOGIN_PATHS = ["/login", "/signin", "/sign-in", "/auth/login", "/account/login", "/customer/account/login",
               "/users/sign_in", "/my-account", "/wp-login.php"]
# Pages tried after a login to find the member area, and to confirm the login on a members-only page
# (Shopify /account, WooCommerce /my-account, Magento /customer/account)
MEMBER_PATHS = ["/dashboard", "/account", "/my-account", "/customer/account", "/app"]
MAX_MEMBER_CHECKS = 9         # pages tried when confirming a login (see _check_member_access)
_ACCOUNT_LINK = re.compile(r"account|dashboard|profile|orders|member|portal|settings|my-", re.I)
_ADMIN_HOME = re.compile(r"^/wp-admin/?$", re.I)
USER_SELECTORS = [
    "input[autocomplete='username']", "input[type='email']", "input[name='email']", "input[name='username']",
    "input[name='user']", "input[name='login']", "input[name='log']", "input[id*='email' i]",
    "input[id*='user' i]", "input[placeholder*='email' i]", "input[placeholder*='user' i]", "input[type='text']",
]
PASS_SELECTORS = ["input[type='password']", "input[name='password']", "input[name='pass']", "input[name='pwd']"]

_SET_VALUE_JS = """
const [el, value] = arguments;
const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
el.dispatchEvent(new Event('input', {bubbles: true}));
el.dispatchEvent(new Event('change', {bubbles: true}));
return el.value === value;
"""

_PASSWORD_VISIBLE_JS = """
const docs = [document];
for (const f of document.querySelectorAll('iframe, frame')) {
  try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {}   // same-origin frames only
}
for (const d of docs) {
  for (const el of d.querySelectorAll('input[type=password]')) {
    const r = el.getBoundingClientRect(), s = (d.defaultView || window).getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none') return true;
  }
}
return false;
"""

# Error messages a login form shows after a failed attempt: alerts and assertive live regions only
# (class names such as "error" or "alert" are also used by decorative elements and gave false failures)
_LOGIN_ERROR_JS = """
for (const el of document.querySelectorAll('[role=alert], [aria-live=assertive]')) {
  if ((el.id || '').toLowerCase().includes('announcer')) continue;
  const r = el.getBoundingClientRect(), t = (el.innerText || '').trim();
  if (t && r.width > 0 && r.height > 0) return t.slice(0, 200);
}
return '';
"""

# One-time-code fields (a postcode or card security code field is not one)
_OTP_SELECTOR = ("input[autocomplete=one-time-code], input[name*=otp i], input[name*=totp i], input[id*=otp i], "
                 "input[name*='2fa' i], input[name*=mfa i], input[name*=verification_code i], "
                 "input[name*=verify_code i], input[name*=auth_code i], input[name=code][inputmode=numeric]")
_OTP_JS = f"return !!document.querySelector({json.dumps(_OTP_SELECTOR)});"

SECOND_STEP_MSG = ("The site asks for a second login step (a one-time code). Log in through the live login window "
                   "(\"Log in through a browser window\") instead.")
NO_FIELD_MSG = ("Could not find the password field on the login page. Log in through the live login window "
                "(\"Log in through a browser window\"), or paste session cookies.")
FRAME_FIELD_MSG = ("The login form sits inside an embedded frame the crawler could not fill in. Log in through the "
                   "live login window (\"Log in through a browser window\"), or paste session cookies.")
# What a site says after a refused login, in the page itself (HTTP answers) or after submitting
_LOCKOUT_TEXT = re.compile(r"too many (?:failed |unsuccessful |invalid )?(?:login |sign[- ]?in )?attempts|"
                           r"account (?:is |has been )?(?:temporarily )?(?:locked|blocked|suspended|disabled)|"
                           r"temporarily (?:locked|blocked)|locked out", re.I)
_EMAIL_STEP_TEXT = re.compile(r"check your (?:e-?mail|inbox)|we (?:have )?(?:sent|e-?mailed) you an? "
                              r"(?:link|code|e-?mail)|(?:verification|confirmation|magic) (?:link|code|e-?mail)", re.I)
_REFUSED_TEXT = re.compile(r"invalid|incorrect|wrong|not (?:recogni[sz]ed|valid|match|correct|found)|"
                           r"unknown (?:user|e-?mail|account)|failed|does not exist|no account|not registered|"
                           r"locked|too many|try again", re.I)
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_HEADER_ERROR = re.compile(r"invalid (?:leading whitespace|header|return character)|header value|"
                           r"header (?:name|part)", re.I)


def shows_login_form(page) -> bool:
    """True when the HTML (a string or a parsed BeautifulSoup) contains a login form: a <form> with exactly one
    password field and a username / email box. A change-password form (two or three password fields) or a
    "confirm with your password" box without a username field is not one."""
    if page is None:
        return False
    if isinstance(page, str):
        if "password" not in page.lower():
            return False
        try:
            page = BeautifulSoup(page, "html.parser")
        except Exception:
            return False
    try:
        fields = page.find_all("input", attrs={"type": re.compile(r"^\s*password\s*$", re.I)})
    except Exception:
        return False
    seen = set()
    for pw in fields:
        form = pw.find_parent("form")
        if form is None or id(form) in seen:
            continue
        seen.add(id(form))
        if len(form.find_all("input", attrs={"type": re.compile(r"^\s*password\s*$", re.I)})) != 1:
            continue
        for inp in form.find_all("input"):
            itype = str(inp.get("type") or "text").strip().lower()
            if itype == "email":
                return True
            if itype in ("text", "tel"):
                hint = " ".join(str(inp.get(a) or "") for a in ("name", "id", "autocomplete", "placeholder"))
                if re.search(r"user|mail|login|log|account|phone|member|customer|name|id", hint, re.I):
                    return True
    return False


def _soup(page) -> Optional[BeautifulSoup]:
    if page is None or hasattr(page, "find_all"):
        return page
    try:
        return BeautifulSoup(page or "", "html.parser")
    except Exception:
        return None


def page_error_text(page) -> str:
    """The error a login page shows after a refused attempt ("Invalid login or password."): alerts, assertive
    live regions and error/notice boxes whose text reads like a refusal. '' when there is none."""
    soup = _soup(page)
    if soup is None:
        return ""
    boxes = soup.select("[role=alert], [aria-live=assertive], #login_error, [class*=error], [class*=alert], "
                        "[class*=notice], [class*=message]")
    for el in boxes[:40]:
        text = re.sub(r"\s+", " ", el.get_text(" ") or "").strip()
        if text and len(text) <= 300 and _REFUSED_TEXT.search(text):
            return text[:200]
    return ""


def second_step_reason(page) -> str:
    """Why a page reached after submitting the password is not the member area, when it says so: a one-time
    code step, a locked account / too many attempts, a confirm-by-email step, or the site's own error message.
    '' when the page gives no such sign."""
    soup = _soup(page)
    if soup is None:
        return ""
    try:
        if soup.select_one(_OTP_SELECTOR) is not None:
            return SECOND_STEP_MSG
    except Exception:
        pass
    text = re.sub(r"\s+", " ", soup.get_text(" ") or "")[:20000]
    heading = ""
    for tag in ("h1", "h2", "[role=alert]", "title"):
        el = soup.select_one(tag)
        if el is not None and el.get_text(strip=True):
            heading = re.sub(r"\s+", " ", el.get_text(" ")).strip()[:120]
            break
    if _LOCKOUT_TEXT.search(text):
        return (f"The site says the account is locked or had too many login attempts (\"{heading}\"). Wait and try "
                "again later, or log in through the live login window.")
    if _EMAIL_STEP_TEXT.search(text):
        return (f"The site asks to confirm the login by email (\"{heading}\"). Log in through the live login window "
                "instead, or paste session cookies.")
    err = page_error_text(soup)
    if err:
        return f"The site refused the login: {err}"
    return ""


def secret_values(auth: Optional[AuthConfig], start_url: str = "https://example.com/") -> List[str]:
    """Every value of `auth` that must never appear in a log line or an API answer (password, header value and
    its token, every pasted cookie value however it was pasted), plus their escaped forms (a value with a line
    break shows up as backslash-n in an error message). Longest first."""
    if auth is None:
        return []
    found = [auth.password or "", auth.custom_header_value or ""]
    found += (auth.custom_header_value or "").split()[1:]          # the token of "Bearer <token>"
    return _secret_set(found + paste_secrets(auth.cookies or "", start_url))


def paste_secrets(raw: str, start_url: str = "https://example.com/") -> List[str]:
    """Every value in a cookie paste that must never be shown: each cookie value however it was pasted (all
    formats, other sites' and expired cookies too), storage values, tokens and passwords a cURL command
    carries, plus the raw name=value parts and lines as a fallback."""
    raw = raw or ""
    if not raw.strip():
        return []
    found: List[str] = []
    try:
        found += parse_cookie_paste(raw, start_url).all_values()
    except Exception:
        pass
    found += [part.partition("=")[2].strip().strip("'\"^") for part in re.split(r"[;\r\n\t]", raw)]
    found += [line.strip() for line in raw.splitlines()]
    return found


def _secret_set(found: List[str]) -> List[str]:
    out = set()
    for s in found:
        if len(s or "") >= 4:
            out.add(s)
            escaped = s.encode("unicode_escape").decode("ascii", "ignore")
            if escaped != s:
                out.add(escaped)
    return sorted(out, key=len, reverse=True)


def mask_text(text: str, secrets: List[str]) -> str:
    """`text` with every secret (longest first) replaced by ***."""
    text = text or ""
    for s in secrets:
        if s and s in text:
            text = text.replace(s, "***")
    return text


def auth_user_agent(auth: Optional[AuthConfig]) -> str:
    """The User-Agent the sign-in settings ask for (auth.user_agent, e.g. the one a pasted cURL command carried
    and the person chose to use), '' when none or when it holds characters a request cannot carry."""
    ua = str(getattr(auth, "user_agent", "") or "").strip() if auth is not None else ""
    return ua if ua and len(ua) <= 512 and not _CTRL_CHARS.search(ua) else ""


# ------------------------------------------------------------------------------------------------------------
# Bot checks. ARACHNIX never tries to pass, solve or get around a CAPTCHA / challenge and never uses a solving
# service: these helpers only recognise one so the person is told plainly and pointed at the routes that work.
# ------------------------------------------------------------------------------------------------------------
# Firewall / CDN checks that answer instead of the page (net_guard.detect_waf labels) vs. CAPTCHA widgets that
# sit in the sign-in form itself (the site's own code shows them)
_FORM_WIDGETS = ("Cloudflare Turnstile", "Google reCAPTCHA", "hCaptcha", "Friendly Captcha", "Arkose Labs",
                 "GeeTest", "MTCaptcha", "Image CAPTCHA")
_OWNER_HINTS = {
    "Cloudflare": "In Cloudflare: Security > WAF > Custom rules, then a rule with the action Skip.",
    "Sucuri": "In Sucuri: Firewall > Access Control, then allowlist your office IP address.",
    "Wordfence": "In Wordfence: Firewall > Firewall Options, then add your office IP to the allowlisted IP addresses.",
    "AWS WAF": "In AWS WAF: a rule with the action Allow, placed before the bot-control rule.",
    "Vercel": "In Vercel: Firewall > Configure, then a custom rule with the action Bypass.",
}
# CAPTCHA widgets in a login form: (provider, CSS class) for the widget's element, (provider, part of a frame
# address) for a rendered widget frame
_WIDGET_CLASSES = (("Cloudflare Turnstile", "cf-turnstile"), ("Google reCAPTCHA", "g-recaptcha"),
                   ("hCaptcha", "h-captcha"), ("Friendly Captcha", "frc-captcha"), ("MTCaptcha", "mtcaptcha"),
                   ("GeeTest", "geetest_holder"))
_WIDGET_FRAMES = (("Cloudflare Turnstile", "challenges.cloudflare.com"), ("Google reCAPTCHA", "/recaptcha/"),
                  ("hCaptcha", "hcaptcha.com"), ("Arkose Labs", "arkoselabs.com"), ("Arkose Labs", "funcaptcha"),
                  ("DataDome", "captcha-delivery.com"))
_IMAGE_CAPTCHA = re.compile(r"captcha", re.I)
_WIDGET_RESPONSE_FIELDS = {"g-recaptcha-response", "h-captcha-response", "cf-turnstile-response",
                           "frc-captcha-solution"}
# A widget class on the submit button itself is a "button binding": score-only reCAPTCHA v3 / invisible v2 and
# invisible hCaptcha, which show the visitor nothing to pass
_BINDING_TAGS = {"button", "input", "a"}
# Frames of invisible / score-only widgets: the reCAPTCHA v3 badge (anchor ...&size=invisible), invisible hCaptcha
_INVISIBLE_FRAME = re.compile(r"[?&#]size=invisible|checkbox-invisible", re.I)
# Inline styles and classes that hide a field from people (a honeypot only bots fill in)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|"
                           r"(?:left|top)\s*:\s*-\d{3,}|(?:width|height)\s*:\s*0(?:px)?\s*(?:;|$)", re.I)
_HIDDEN_CLASSES = {"hidden", "d-none", "hide", "is-hidden", "honeypot", "hp", "visually-hidden", "sr-only",
                   "screen-reader-text", "invisible"}


def bot_check_message(provider: str = "", where: str = "sign-in") -> str:
    """The plain sentence for a bot check met at `where` ("sign-in", "cookies" or "crawl"), with the routes
    that work. `provider` (e.g. "Cloudflare Turnstile") is added in brackets when known."""
    lead = {"crawl": BOT_CHECK_CRAWL_MSG, "cookies": BOT_CHECK_COOKIES_MSG}.get(where, BOT_CHECK_MSG)
    if provider and provider != "Bot check":
        lead = f"{lead[:-1]} ({provider})."
    routes = BOT_CHECK_OWNER_ROUTE if where == "cookies" else BOT_CHECK_ROUTES
    return f"{lead} {routes}"


def owner_steps(provider: str = "") -> List[str]:
    """Short steps for a site owner who wants to let ARACHNIX through their own bot check."""
    if provider in _FORM_WIDGETS:
        steps = [f"The sign-in form itself shows the check ({provider}); it comes from the site's own code or a "
                 "plugin, not from a firewall.",
                 "Let the form skip the check for your office IP address, or for requests that carry a secret "
                 "header (a setting in the CAPTCHA plugin, or a small code change).",
                 "If you chose the header: in ARACHNIX, Options > HTTP headers, add the same header name and "
                 "value (for example X-ARACHNIX-Key and a long random value).",
                 "Test the sign-in again."]
        return steps
    vendor = next((k for k in _OWNER_HINTS if provider.startswith(k)), "")
    named = provider and provider != "Bot check"
    steps = [f"Open your firewall / CDN settings{f' ({provider})' if named else ''}.",
             "Add an allow (skip) rule for your office IP address, or for a request header such as "
             "X-ARACHNIX-Key with a long random value.",
             "If you chose the header: in ARACHNIX, Options > HTTP headers, add the same header name and value.",
             "Test the sign-in again."]
    if vendor:
        steps.insert(1, _OWNER_HINTS[vendor])
    return steps


def bot_check_info(provider: str, where: str = "sign-in") -> Dict[str, Any]:
    """{provider, kind: "form" (a widget in the sign-in form) | "firewall" (answers instead of the page),
    advice (bot_check_message), owner_steps, where} for one bot check."""
    return {"provider": provider or "Bot check", "kind": "form" if provider in _FORM_WIDGETS else "firewall",
            "advice": bot_check_message(provider, where), "owner_steps": owner_steps(provider), "where": where}


def challenge_status(provider: str = "", count: int = 0, first_url: str = "", where: str = "sign-in"
                     ) -> Dict[str, Any]:
    """The "challenge" object of the API (auth test, crawl status): {seen, provider, count, first_url, advice,
    owner_steps}. Not seen: {seen: False, provider: "", count: 0, first_url: "", advice: "", owner_steps: []}."""
    if not count and not provider:
        return {"seen": False, "provider": "", "count": 0, "first_url": "", "advice": "", "owner_steps": []}
    return {"seen": True, "provider": provider or "Bot check", "count": max(1, int(count or 0)),
            "first_url": first_url or "", "advice": bot_check_message(provider, where),
            "owner_steps": owner_steps(provider)}


def _hidden_field(el, stop=None) -> bool:
    """A form field hidden from people (a honeypot): the hidden attribute, aria-hidden, tabindex -1, or an inline
    style / class that hides it or one of its parents up to `stop` (the form)."""
    if str(el.get("tabindex") or "").strip() == "-1":
        return True
    node = el
    while node is not None and getattr(node, "name", None) and hasattr(node, "get"):
        if node.has_attr("hidden") or str(node.get("aria-hidden") or "").strip().lower() == "true":
            return True
        if _HIDDEN_STYLE.search(str(node.get("style") or "")):
            return True
        if {str(c).lower() for c in (node.get("class") or [])} & _HIDDEN_CLASSES:
            return True
        if node is stop:
            break
        node = node.parent
    return False


def _invisible_widget(el) -> bool:
    """A score-only / invisible widget: a button binding (reCAPTCHA v3, invisible v2 / hCaptcha), data-size
    invisible, the reCAPTCHA v3 badge or an invisible widget's frame."""
    if el.name in _BINDING_TAGS or str(el.get("data-size") or "").strip().lower() == "invisible":
        return True
    if el.name == "iframe" and _INVISIBLE_FRAME.search(str(el.get("src") or "")):
        return True
    node = el
    while node is not None and getattr(node, "name", None) and hasattr(node, "get"):
        if "grecaptcha-badge" in {str(c).lower() for c in (node.get("class") or [])}:
            return True
        node = node.parent
    return False


def form_bot_check(page) -> str:
    """The CAPTCHA widget a sign-in page shows in (or next to) its login form: "Cloudflare Turnstile", "Google
    reCAPTCHA", "hCaptcha", "Image CAPTCHA"... '' when there is none. Invisible / score-only checks (reCAPTCHA
    v3 and its badge, button bindings, invisible widgets), a hidden honeypot field and a script that is only
    loaded do not count; widgets inside other forms (newsletter, comments) are ignored."""
    soup = _soup(page)
    if soup is None:
        return ""
    try:
        forms = [f for f in soup.find_all("form")
                 if f.find("input", attrs={"type": re.compile(r"^\s*password\s*$", re.I)}) is not None]
    except Exception:
        return ""
    if not forms:
        return ""
    selector = ", ".join([f".{cls}" for _, cls in _WIDGET_CLASSES] + ["iframe[src]"])
    try:
        candidates = soup.select(selector)
    except Exception:
        candidates = []
    for el in candidates:
        # In the sign-in form, or beside it (outside any other form: a newsletter box's widget does not count)
        holder = el.find_parent("form")
        if holder is not None and not any(holder is f for f in forms):
            continue
        if _invisible_widget(el):
            continue
        if el.name == "iframe":
            src = str(el.get("src") or "").lower()
            for provider, part in _WIDGET_FRAMES:
                if part in src:
                    return provider
            continue
        classes = {str(c).lower() for c in (el.get("class") or [])}
        for provider, cls in _WIDGET_CLASSES:
            if cls in classes:
                return provider
    for form in forms:
        for inp in form.find_all("input"):
            name = " ".join(str(inp.get(a) or "") for a in ("name", "id"))
            itype = str(inp.get("type") or "text").strip().lower()
            if itype not in ("hidden", "submit", "button") and _IMAGE_CAPTCHA.search(name) and \
                    str(inp.get("name") or "").lower() not in _WIDGET_RESPONSE_FIELDS and \
                    not _hidden_field(inp, form):
                return "Image CAPTCHA"
    return ""


def _lower_headers(headers) -> Dict[str, str]:
    try:
        return {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    except Exception:
        return {}


def detect_bot_check(status: Optional[int] = None, headers: Optional[Dict[str, str]] = None, body: str = "",
                     where: str = "sign-in", login_page: bool = False) -> Optional[Dict[str, Any]]:
    """bot_check_info() when an HTTP answer is a bot challenge, else None: a challenge header or a firewall
    block page (net_guard.detect_waf), or, on a sign-in page (`login_page`), a CAPTCHA widget in the login form
    (form_bot_check). `headers` may use any case."""
    label = detect_waf(status, _lower_headers(headers), (body or "")[:WAF_SCAN_CHARS]) if status is not None else ""
    if not label and login_page and body and (status is None or 200 <= status < 300):
        label = form_bot_check(body)
    return bot_check_info(label, where) if label else None


def response_bot_check(resp, where: str = "sign-in", login_page: bool = False) -> Optional[Dict[str, Any]]:
    """detect_bot_check() for a requests response (the start of its body is read)."""
    if resp is None:
        return None
    try:
        # A sign-in page is read whole (its form can sit far down a page full of inline styles)
        body = resp.text[:2_000_000] if login_page else resp.text[:WAF_SCAN_CHARS]
    except Exception:
        body = ""
    return detect_bot_check(resp.status_code, resp.headers, body, where=where, login_page=login_page)


_BROWSER_CHALLENGE_JS = """
const title = (document.title || '').toLowerCase();
if (title.includes('just a moment') || title.includes('attention required')
    || document.querySelector('#challenge-form, #cf-challenge-running, .cf-browser-verification, #challenge-stage')) {
  return 'Cloudflare';
}
const hosts = [['challenges.cloudflare.com', 'Cloudflare Turnstile'], ['recaptcha', 'Google reCAPTCHA'],
               ['hcaptcha', 'hCaptcha'], ['captcha-delivery', 'DataDome'], ['arkoselabs', 'Arkose Labs'],
               ['funcaptcha', 'Arkose Labs']];
const classes = [['cf-turnstile', 'Cloudflare Turnstile'], ['g-recaptcha', 'Google reCAPTCHA'],
                 ['h-captcha', 'hCaptcha'], ['frc-captcha', 'Friendly Captcha']];
const shown = (el) => { const r = el.getBoundingClientRect(), st = getComputedStyle(el);
  return r.width > 30 && r.height > 30 && r.right > 0 && r.bottom > 0 && st.visibility !== 'hidden'
    && st.display !== 'none' && st.opacity !== '0'; };
// Score-only / invisible checks show the visitor nothing to pass: the reCAPTCHA v3 badge, a widget class on the
// submit button (button binding), data-size="invisible" and invisible widgets' frames
const quiet = (el) => !!el.closest('.grecaptcha-badge') || ['BUTTON', 'INPUT', 'A'].includes(el.tagName)
  || (el.getAttribute('data-size') || '').toLowerCase() === 'invisible'
  || /[?&#]size=invisible|checkbox-invisible/i.test(el.getAttribute('src') || '');
for (const f of document.querySelectorAll('iframe[src]')) {
  if (!shown(f) || quiet(f)) continue;
  for (const [h, name] of hosts) { if ((f.src || '').includes(h)) return name; }
}
for (const [c, name] of classes) {
  for (const el of document.getElementsByClassName(c)) { if (shown(el) && !quiet(el)) return name; }
}
const fields = 'input[name="cf-turnstile-response"], textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"]';
for (const f of document.querySelectorAll(fields)) {
  if (f.parentElement && shown(f.parentElement) && !quiet(f.parentElement)) {
    return {'cf-turnstile-response': 'Cloudflare Turnstile', 'g-recaptcha-response': 'Google reCAPTCHA',
            'h-captcha-response': 'hCaptcha'}[f.name];
  }
}
for (const el of document.querySelectorAll('input[name*=captcha i], input[id*=captcha i]')) {
  const r = el.getBoundingClientRect(), st = getComputedStyle(el);
  if ((el.type || 'text') !== 'hidden' && !/-response$|-solution$/.test(el.name || '') && r.width > 0 && r.height > 0
      && r.right > 0 && r.bottom > 0 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0'
      && el.tabIndex !== -1 && !el.closest('[hidden], [aria-hidden="true"]')) return 'Image CAPTCHA';
}
if (/verify you are human|tick the box|complete the (security )?check|i'?m not a robot/i.test(document.body ? document.body.innerText : '')) {
  return 'Bot check';
}
return null;
"""


def browser_bot_check(driver, where: str = "sign-in") -> Optional[Dict[str, Any]]:
    """bot_check_info() when the page open in `driver` shows a bot check the visitor must pass (a full-page
    challenge or a rendered CAPTCHA widget), else None. A script that is only loaded does not count. For the
    sign-in in Chrome and the live login window; nothing here interacts with the check."""
    if driver is None:
        return None
    try:
        label = driver.execute_script(_BROWSER_CHALLENGE_JS)
    except Exception:
        return None
    return bot_check_info(str(label), where) if label else None


def crawl_challenge_status(crawler) -> Dict[str, Any]:
    """challenge_status() for GET /api/crawl/status: crawled pages a bot check / firewall answered instead of
    the page (CrawlResult root cause "waf"; status codes and categories unchanged), else a bot check the
    sign-in met (auth_handler.challenge). count = pages blocked; first_url = the first one in crawl order."""
    results: List[Any] = []
    lock = getattr(crawler, "_lock", None)
    try:
        if lock is not None:
            with lock:
                results = list(getattr(crawler, "results_list", []) or [])
        else:
            results = list(getattr(crawler, "results_list", []) or [])
    except Exception:
        results = []
    blocked = []
    for r in results:
        try:
            if r.root_cause_type == "waf":
                blocked.append(r)
        except Exception:
            continue
    if blocked:
        labels: Dict[str, int] = {}
        for r in blocked:
            label = getattr(r, "waf", "") or "Bot check"
            labels[label] = labels.get(label, 0) + 1
        named = [k for k in labels if k != "Bot check" and not k.startswith("sudden burst")]
        provider = max(named, key=lambda k: labels[k]) if named else max(labels, key=lambda k: labels[k])
        return challenge_status(provider, len(blocked), blocked[0].url, "crawl")
    handler = getattr(crawler, "auth_handler", None)
    c = getattr(handler, "challenge", None) if handler is not None else None
    if c:
        return challenge_status(c.get("provider", ""), c.get("count", 0), c.get("first_url", ""),
                                c.get("where", "sign-in"))
    return challenge_status()


def live_window_bot_check(session_id: str) -> Dict[str, Any]:
    """challenge_status() for what the live login window `session_id` shows now (a bot check ARACHNIX can't
    complete, with the plain guidance), {seen: False, ...} when none or when the window is gone. Read-only:
    nothing is clicked or typed."""
    from .live_browser import get_live_manager
    try:
        sess = get_live_manager()._session(session_id)
    except Exception:
        return challenge_status()
    with sess.lock:
        found = browser_bot_check(sess.driver)
        url = sess.current_url() if found else ""
    return challenge_status(found["provider"], 1, url, "sign-in") if found else challenge_status()


class AuthHandler:
    """
    Logs the crawler in and keeps the login as session_state (full cookies + web storage). A browser
    login runs in `browser` (the crawl's SharedBrowser), which stays logged in for rendering and screenshots.
    After authenticate(): method, session_state, landing_url, browser_only (the site accepts the login only
    in Chrome, not over plain HTTP) and probe_url (a page that sends logged-out visitors to the login page).
    """

    def __init__(
        self,
        auth_config: AuthConfig,
        user_agent: str,
        timeout: int = 15,
        verify_ssl: bool = False,
        logger: Optional[Callable[[str], None]] = None,
        browser: Optional[SharedBrowser] = None,
    ):
        self.config = auth_config
        # A User-Agent chosen in the sign-in settings (e.g. offered from a pasted cURL command) wins
        self.auth_user_agent = auth_user_agent(auth_config)
        self.user_agent = self.auth_user_agent or user_agent
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.logger = logger or (lambda msg: None)
        self._own_browser = browser is None
        self.browser = browser or SharedBrowser(user_agent, timeout, logger=self.logger)
        self.method = ""
        self.session_state: Optional[Dict[str, Any]] = None
        self.browser_only = False
        self.landing_url: Optional[str] = None
        self.login_url: Optional[str] = None
        self.probe_url: Optional[str] = None
        # How the probe page turns logged-out visitors away ('login page', 'HTTP 401', 'HTTP 403', 'login form')
        self.probe_kind = ""
        # "verified" (a members-only page opened with the login), "refused" or "unknown" (none found), and the
        # members-only page it was confirmed on (also for apps checked only in the browser, without a probe)
        self.member_check = "unknown"
        self.confirmed_url: Optional[str] = None
        # A login form on a page of a logged-in crawl means the session ended (False when logged-in pages
        # show one anyway, e.g. an always-present login popup)
        self.form_signal = True
        self.start_url = ""
        self._definite = False      # the last failed login attempt got a clear "no" from the site
        # A bot check met while signing in: {"provider", "first_url", "count", "where"} (see challenge_info)
        self.challenge: Optional[Dict[str, Any]] = None

    def log(self, msg: str):
        self.logger(f"[Auth] {msg}")

    # ------------------------------------------------------------------
    # Bot checks (recognised and reported, never completed)
    # ------------------------------------------------------------------
    def _note_challenge(self, provider: str, url: str, where: str = "") -> str:
        """Records a bot check met on `url`; returns the plain message for the sign-in result. `where` defaults
        to "cookies" for a pasted session (the person already used their own browser) else "sign-in"."""
        provider = provider or "Bot check"
        where = where or ("cookies" if self.method == "cookies" else "sign-in")
        if self.challenge is None:
            self.challenge = {"provider": provider, "first_url": url or "", "count": 0, "where": where}
            self.log(f"A bot check ({provider}) answered on {url}. ARACHNIX does not try to complete it; "
                     "sign in with your own browser and paste the cookies, or allow ARACHNIX in the site's "
                     "security settings.")
        self.challenge["count"] += 1
        return bot_check_message(self.challenge["provider"], self.challenge["where"])

    def _bot_answer(self, resp, where: str = "", login_page: bool = False) -> str:
        """'' for a normal answer; the bot-check message (recorded) when `resp` is a challenge."""
        found = response_bot_check(resp, login_page=login_page)
        if not found:
            return ""
        return self._note_challenge(found["provider"], str(getattr(resp, "url", "") or ""), where)

    def challenge_info(self) -> Dict[str, Any]:
        """The "challenge" object for API answers: {seen, provider, count, first_url, advice, owner_steps}."""
        c = self.challenge or {}
        return challenge_status(c.get("provider", ""), c.get("count", 0), c.get("first_url", ""),
                                c.get("where", "sign-in"))

    @property
    def driver(self):
        """The logged-in Chrome (the shared browser), or None."""
        return self.browser.driver if self.browser.alive() else None

    def close(self):
        """Closes Chrome when this handler started it itself (a crawl's shared browser is closed by the crawl)."""
        if self._own_browser:
            self.browser.close()

    # ------------------------------------------------------------------
    def authenticate(self, session: requests.Session, start_url: str) -> AuthResult:
        """
        Logs in as configured. Returns (success, message, {cookie name: value}, landing URL, member links
        found on the landing page). On success session_state holds the login and the HTTP session's cookie
        jar carries it (real domains and paths; no fixed Cookie header, so refreshed tokens are used).
        """
        self.start_url = start_url
        self.browser.site_url = self.browser.site_url or start_url
        if not self.config.enabled:
            return True, "No authentication required (Public Crawl)", {}, None, set()
        session.headers.pop("Cookie", None)
        if self.auth_user_agent and session.headers.get("User-Agent") != self.auth_user_agent:
            # The sign-in settings chose a User-Agent (the one the person's own browser sent with the cookies)
            session.headers["User-Agent"] = self.auth_user_agent
            if not self.browser.alive():
                self.browser.user_agent = self.auth_user_agent
            self.log("Using the User-Agent chosen in the sign-in settings for this crawl.")
        auth_type = (self.config.auth_type or "").strip().lower()

        has_header = bool((self.config.custom_header_name or "").strip() and self.config.custom_header_value)
        if has_header:
            self._apply_header(session)
        if auth_type == "header":
            return self._header_only(session, start_url) if has_header else (
                False, "A header name and value are required for header authentication", {}, None, set())
        if auth_type == "live":
            return self._apply_live(session, start_url)
        # A leftover cookie string does not override an explicit username + password login
        wants_password = auth_type == "form" and bool((self.config.username or "").strip() and self.config.password)
        if auth_type == "cookies" or ((self.config.cookies or "").strip() and not wants_password):
            return self._apply_cookies(session, start_url)
        return self._password_login(session, start_url)

    # ------------------------------------------------------------------
    # Header / cookies / live window
    # ------------------------------------------------------------------
    def _safe_error(self, e: BaseException) -> str:
        """An exception as one short line with every secret masked. Errors about a header value are reworded:
        requests and http.client quote the whole value (cookie values included) in that message."""
        text = str(e) or type(e).__name__
        if _HEADER_ERROR.search(text):
            return ("a cookie or header value contains characters a web request cannot carry (such as a line "
                    "break); paste each cookie as name=value, separated by ';' or one per line")
        text = text.splitlines()[0][:160]
        for secret in secret_values(self.config, self.start_url or "https://example.com/"):
            text = text.replace(secret, "***")
        return text

    def _apply_header(self, session: requests.Session):
        name = (self.config.custom_header_name or "").strip()
        value = str(self.config.custom_header_value or "").strip()
        if _CTRL_CHARS.search(name + value) or not re.match(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$", name):
            self.log("The custom header name or value contains characters a web request cannot carry; "
                     "it was not sent (value hidden).")
            return
        session.headers[name] = value
        self.log(f"Sending the custom header '{name}' (value hidden) with every request to the site.")

    def _header_only(self, session: requests.Session, start_url: str) -> AuthResult:
        self.method = "header"
        name = (self.config.custom_header_name or "").strip()
        if name not in session.headers:
            return False, ("The custom header could not be used: its name or value contains a line break or "
                           "another character a web request cannot carry."), {}, None, set()
        try:
            r = session.get(start_url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
        except Exception as e:
            return False, f"Could not reach {start_url} with the header: {self._safe_error(e)}", {}, None, set()
        bot = self._bot_answer(r)
        if bot:
            return False, bot, {}, None, set()
        if r.status_code in (401, 403) or is_login_url(str(r.url)):
            return False, (f"The site still refused access with the header (HTTP {r.status_code} on {r.url}). "
                           "Check the header name and value."), {}, None, set()
        self.landing_url = str(r.url)
        self.session_state = {"cookies": jar_to_cookies(session.cookies, start_url), "local": {}, "session": {}}
        verdict, sample, _ = self._check_member_access(session, start_url, self.landing_url, r.text,
                                                       plain_headers=(name,))
        if verdict == "refused":
            return False, (f"The site still refused access with the header: {sample} asks for a login. "
                           "Check the header name and value."), {}, None, set()
        links = self._links_from_html(r.text, str(r.url))
        return True, (f"Header authentication accepted (HTTP {r.status_code})" + self._unconfirmed_note()), {}, \
            self.landing_url, links

    def _apply_live(self, session: requests.Session, start_url: str) -> AuthResult:
        from .live_browser import session_state_for
        sid = (getattr(self.config, "live_session_id", None) or "").strip()
        state = session_state_for(sid) if sid else None
        if not state or not (state.get("cookies") or storage_origins(state)):
            return False, ("The browser login window session was not found or has expired. Open the login "
                           "window again, log in, and press Done."), {}, None, set()
        return self._use_session_state(session, start_url, state, "live")

    _FORMAT_NAMES = {"header": "a Cookie header", "json": "a JSON cookie export", "netscape": "a cookies.txt file",
                     "curl": "a cURL command", "powershell": "a PowerShell snippet", "table": "the DevTools cookie table"}

    def _apply_cookies(self, session: requests.Session, start_url: str) -> AuthResult:
        paste = parse_cookie_paste(self.config.cookies or "", start_url)
        state = paste.session_state()
        if paste.rejected:
            self.log(f"Left out {paste.rejected} pasted cookie(s) whose name or value has a line break or another "
                     "character a web request cannot carry (values hidden).")
        expired = sum(1 for r in paste.rows if r.expired and not r.other_site)
        others = sum(1 for r in paste.rows if r.other_site)
        if expired or others:
            parts = ([f"{expired} expired cookie(s)"] if expired else []) + \
                ([f"{others} cookie(s) of other sites"] if others else [])
            self.log(f"Left out {' and '.join(parts)} (values hidden).")
        if paste.user_agent and not self.auth_user_agent:
            self.log("The pasted cookies came with the browser's User-Agent; the crawl keeps its own User-Agent "
                     "(choose the browser's one in the sign-in settings to use it).")
        if not (state["cookies"] or storage_origins(state)):
            return False, (paste.error or "Could not read the pasted cookies. Paste a Cookie header, a JSON cookie "
                           "export, a cookies.txt file or a whole cURL command."), {}, None, set()
        self.log(f"Read the pasted session from {self._FORMAT_NAMES.get(paste.format, 'the paste')}.")
        return self._use_session_state(session, start_url, state, "cookies")

    @staticmethod
    def parse_cookie_input(raw: str, start_url: str) -> Optional[Dict[str, Any]]:
        """A pasted session as session_state ({"cookies", "local", "session", "rejected"}), None for an empty
        paste. Every format cookie_import reads is accepted (Cookie header, JSON export, cookies.txt, cURL,
        PowerShell, DevTools table rows). Cookies without a domain belong to the crawled site; expired cookies
        and other sites' cookies are left out; cookies with a character a request cannot carry are left out
        and counted in "rejected"."""
        if not (raw or "").strip():
            return None
        paste = parse_cookie_paste(raw, start_url)
        state = paste.session_state()
        state["rejected"] = paste.rejected
        return state

    def _use_session_state(self, session: requests.Session, start_url: str, state: Dict[str, Any],
                           method: str) -> AuthResult:
        """A ready-made login (pasted cookies or the live window): into the cookie jar and the shared
        browser, then checked on the start page."""
        self.method = method
        cookies_to_jar(session.cookies, state["cookies"])
        session.headers.pop("Cookie", None)
        self.session_state = state
        self.browser.set_session(state)
        names = sorted({c["name"] for c in state["cookies"]})
        self.log(f"Loaded {len(names)} session cookies ({', '.join(names)[:200]}) and storage for "
                 f"{storage_origins(state)} origin(s); values hidden.")

        landing, html = start_url, ""
        try:
            r = session.get(start_url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
            bot = self._bot_answer(r, where="cookies" if method == "cookies" else "sign-in")
            if bot:
                return False, bot, {}, None, set()
            landing, html = str(r.url), r.text
            http_ok = self._wall(r) in ("", "login form")   # a login form alone is judged by comparison below
        except Exception as e:
            self.log(f"Notice probing the start page with the session: {self._safe_error(e)}")
            http_ok = False
        # A public start page opens with any cookies: the session counts as working only once a members-only
        # page opens with it (or none can be found to tell by; the result then says so)
        verdict, sample = "refused", start_url
        if http_ok:
            verdict, refused_at, _ = self._check_member_access(session, start_url, landing, html)
            sample = refused_at or sample
        if verdict == "unknown" and storage_origins(state) and chrome_available() and not is_login_url(landing):
            # A login kept in web storage (single-page apps) shows only in a browser: the app must open with it
            # and ask a browser without it to log in
            seen_before = (self.challenge or {}).get("count", 0)
            seen = self._browser_shows_app(landing)
            if seen is not None and not seen[0]:
                if (self.challenge or {}).get("count", 0) > seen_before:
                    return False, bot_check_message(self.challenge["provider"], self.challenge["where"]), {}, None, set()
                return False, (f"The saved login did not work: {seen[1]} still asks for a login with this session. "
                               "Log in again and copy a fresh session."), {}, None, set()
            if seen is not None and self._logged_out_in_browser(landing):
                self.member_check, self.confirmed_url = "verified", landing
        if verdict == "refused":
            seen_before = (self.challenge or {}).get("count", 0)
            in_browser = self._browser_shows_app(sample)
            if (in_browser is None or not in_browser[0]) and (self.challenge or {}).get("count", 0) > seen_before:
                # Chrome met a bot check on that page: say so instead of blaming the session
                return False, bot_check_message(self.challenge["provider"], self.challenge["where"]), {}, None, set()
            if in_browser is None or not in_browser[0]:
                where = landing if not http_ok and is_login_url(landing) else sample
                return False, (f"The saved login did not work: {where} still asks for a login with this session. "
                               "Log in again and copy a fresh session."), {}, None, set()
            self.browser_only = True
            self.member_check = "verified"
            self.probe_url, self.probe_kind, self.confirmed_url = sample, "login page", sample
            if not http_ok:
                landing, html = in_browser[1], in_browser[2]
            self.log("The site accepts this session only in Chrome, not over plain HTTP: logged-in pages "
                     "are loaded through the browser.")

        links = self._links_from_html(html, landing)
        if not self.browser_only:
            links |= self._member_links(session, start_url, links)
        self.landing_url = landing
        label = "Live browser login" if method == "live" else "Session cookies"
        self.log(f"{label} accepted. Discovered {len(links)} initial member links."
                 + ("" if self.member_check == "verified" else " The login could not be confirmed on a "
                    "members-only page (none was found)."))
        return (True, f"{label}: {len(names)} cookies loaded ({len(links)} member links discovered)"
                + self._unconfirmed_note(), {c["name"]: c["value"] for c in state["cookies"]}, landing, links)

    # ------------------------------------------------------------------
    # Confirming a login on a members-only page
    # ------------------------------------------------------------------
    def _wall(self, resp) -> str:
        """Why `resp` is the site's login wall instead of the page: 'login page' (it ended on one), 'HTTP 401',
        'HTTP 403' or 'login form' (a login form shown in place of the page, as WooCommerce's /my-account
        does). '' for a normal answer."""
        if is_login_url(str(resp.url)):
            return "login page"
        if resp.status_code in (401, 403):
            return f"HTTP {resp.status_code}"
        ctype = (resp.headers.get("content-type") or "").lower()
        if resp.status_code == 200 and ("html" in ctype or not ctype) and shows_login_form(resp.text or ""):
            return "login form"
        return ""

    def _member_candidates(self, start_url: str, landing: Optional[str], html: str) -> List[str]:
        """Pages that may be members-only: the landing page, the start page, the usual account paths and
        account-like links on the landing page (never a sign-out or data-changing link)."""
        out: List[str] = []
        origin = "{0.scheme}://{0.netloc}".format(urlparse(start_url))
        links = sorted(u for u in self._links_from_html(html, landing or start_url) if _ACCOUNT_LINK.search(u))
        for url in [landing, start_url] + [origin + p for p in MEMBER_PATHS] + links[:4]:
            norm = normalize_url(url or "") or ""
            if not norm or norm in out or is_login_url(norm):
                continue
            # The bare admin dashboard (where a WordPress admin login lands) only shows a page: it may be
            # opened to confirm the login, unlike admin screens and action links (see url_utils.is_unsafe_url)
            parsed = urlparse(norm)
            admin_home = bool(_ADMIN_HOME.match(parsed.path or "")) and not parsed.query
            if admin_home or not is_unsafe_url(norm):
                out.append(norm)
        return out[:MAX_MEMBER_CHECKS]

    def _check_member_access(self, session: requests.Session, start_url: str, landing: Optional[str],
                             html: str = "", plain_headers: Tuple[str, ...] = ()) -> Tuple[str, str, str]:
        """Confirms the login on a members-only page: a page that a logged-out visitor cannot open (login page,
        401, or a login form in its place) is opened with the session. Returns (verdict, refused page, its
        HTML): "verified" (it opened; probe_url and probe_kind are set to it), "refused" (members-only pages
        still ask for a login) or "unknown" (no members-only page was found). `plain_headers` are left out of
        the logged-out requests (a custom auth header)."""
        plain = SiteSession()
        plain.headers.update({k: v for k, v in session.headers.items()
                              if k.lower() != "cookie" and k.lower() not in {h.lower() for h in plain_headers}})
        if isinstance(session, SiteSession) and session.site_headers:
            # the crawl's custom HTTP headers stay on the crawled site here too (redirects to a sign-in host)
            plain.scope_like(session, [k for k in session.site_headers if k in plain.headers])
        refused, refused_html = "", ""
        timeout = min(self.timeout, 10)
        # A login form on a logged-in page is a sign of a lost session only where logged-in pages show none
        self.form_signal = not shows_login_form(html or "")
        for url in self._member_candidates(start_url, landing, html):
            if self.challenge is not None and self.challenge.get("count", 0) >= 2:
                break               # the site keeps answering with a bot check: stop asking it
            try:
                out = plain.get(url, timeout=timeout, verify=self.verify_ssl, allow_redirects=True)
            except Exception:
                continue
            found = response_bot_check(out)
            if found:
                self._note_challenge(found["provider"], url)
                continue            # a challenge says nothing about the login
            plain_wall = self._wall(out)
            if not plain_wall:
                continue            # public: says nothing about the login
            try:
                mine = session.get(url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
            except Exception:
                continue
            found = response_bot_check(mine)
            if found:
                self._note_challenge(found["provider"], url)
                continue
            my_wall = self._wall(mine)
            step = second_step_reason(mine.text or "") if not my_wall else ""
            if step and not step.startswith("The site refused"):
                # A half-logged-in session opens the one-time-code (or lockout) page itself: not the member area
                if not refused:
                    refused, refused_html = url, mine.text or ""
                continue
            if not my_wall:
                self.probe_url, self.probe_kind, self.confirmed_url = url, plain_wall, url
                self.member_check = "verified"
                self.form_signal = self.form_signal and not shows_login_form(mine.text or "")
                return "verified", "", ""
            member_path = urlparse(url).path.rstrip("/") in {p.rstrip("/") for p in MEMBER_PATHS}
            strong = plain_wall in ("login page", "HTTP 401") or (my_wall == "login form" and member_path)
            if strong and not refused:
                refused, refused_html = url, mine.text or ""
        self.member_check = "refused" if refused else "unknown"
        return self.member_check, refused, refused_html

    def _logged_out_in_browser(self, url: str) -> bool:
        """True when `url`, opened in a separate Chrome without any login, ends on a login page or shows a
        password field (so the logged-in browser that shows the app there really is logged in). False when it
        opens like that for anyone, or when no second Chrome could start."""
        from .browser import block_dangerous, close_chrome, new_chrome
        driver = new_chrome(self.user_agent, min(self.timeout, 20), eager=True, wait=5)
        if driver is None:
            return False
        try:
            block_dangerous(driver)
            self._get(driver, url)
            self._wait_ready(driver, 8)
            self._wait_for_links_to_settle(driver, max_wait=5)
            return is_login_url(driver.current_url) or self._password_visible(driver)
        except Exception:
            return False
        finally:
            close_chrome(driver)

    def _unconfirmed_note(self) -> str:
        return ("" if self.member_check == "verified" else
                "; not confirmed - no members-only page was found to check the login on")

    def _browser_shows_app(self, url: str) -> Optional[Tuple[bool, str, str]]:
        """Opens `url` in the shared browser: (logged in, final URL, HTML), or None without Chrome. A one-time
        code, lockout or confirm-by-email page is not logged in either."""
        if not chrome_available():
            return None
        with self.browser.hold(timeout=120) as driver:
            if driver is None:
                return None
            self._get(driver, url)
            self._wait_ready(driver, 10)
            self._wait_for_links_to_settle(driver, max_wait=6)
            current = driver.current_url
            html = driver.page_source or ""
            challenge = self._visible_challenge(driver)
            if challenge:
                self._note_challenge(challenge, current)
                return False, current, html
            step = second_step_reason(html)
            ok = not is_login_url(current) and not self._password_visible(driver) and \
                not (step and not step.startswith("The site refused"))
            return ok, current, html

    def _member_links(self, session: requests.Session, start_url: str, known: Set[str]) -> Set[str]:
        found: Set[str] = set()
        base_dom = get_domain(start_url)
        for path in MEMBER_PATHS:
            probe = urljoin(start_url, path)
            if probe in known:
                continue
            try:
                r = session.get(probe, timeout=5, verify=self.verify_ssl, allow_redirects=True)
            except Exception:
                continue
            if r.status_code == 200 and not is_login_url(str(r.url)) and \
                    is_internal_url(str(r.url), base_dom):
                found.add(normalize_url(str(r.url)) or probe)
                found |= self._links_from_html(r.text, str(r.url))
        return found

    # ------------------------------------------------------------------
    # Username + password
    # ------------------------------------------------------------------
    def _password_login(self, session: requests.Session, start_url: str) -> AuthResult:
        username = (self.config.username or "").strip()
        password = self.config.password or ""
        if not username or not password:
            return False, "Username/email and password (or session cookies) are required for authentication", {}, None, set()

        login_url = self._find_login_url(session, start_url)
        self.login_url = login_url
        if self.challenge is not None:
            # The site answered the way to its sign-in with a bot check: Chrome would meet it too
            return False, bot_check_message(self.challenge["provider"], self.challenge["where"]), {}, None, set()
        self.log(f"Attempting authentication at: {login_url}")

        ok, msg, cookies, landing, links = self._try_http_post_login(session, login_url, username, password, start_url)
        if ok:
            return ok, msg, cookies, landing, links
        if self._definite:
            # The site clearly said no (wrong details, locked account, a second step): trying again in Chrome
            # would only cost one more failed attempt on the client's site (login-limit plugins lock at ~4)
            self.log(f"Login refused by the site; not retrying in Chrome: {msg}")
            return False, msg, {}, None, set()
        self.log(f"Plain HTTP form login did not work ({msg}); logging in with headless Chrome...")
        if not chrome_available():
            return False, NO_CHROME_MSG, {}, None, set()
        return self._try_browser_login(session, login_url, username, password, start_url)

    def _find_login_url(self, session: requests.Session, start_url: str) -> str:
        """The configured login URL; else where the start page sends a visitor; else a common login path
        that shows a password field."""
        configured = (self.config.login_url or "").strip()
        if configured:
            return configured
        try:
            r = session.get(start_url, timeout=min(self.timeout, 10), verify=self.verify_ssl, allow_redirects=True)
            if self._bot_answer(r):
                return start_url          # recorded in self.challenge; no more requests to the site
            if is_login_url(str(r.url)) or self._has_password_field(r.text):
                return str(r.url)
        except Exception:
            pass
        parsed = urlparse(start_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        for path in LOGIN_PATHS:
            try:
                r = session.get(origin + path, timeout=5, verify=self.verify_ssl, allow_redirects=True)
            except Exception:
                continue
            if self._bot_answer(r):
                return str(r.url)
            if r.status_code == 200 and (self._has_password_field(r.text) or is_login_url(str(r.url))):
                return str(r.url)
        return start_url

    @staticmethod
    def _has_password_field(html: str) -> bool:
        return bool(html) and "password" in html.lower() and \
            BeautifulSoup(html, "html.parser").find("input", attrs={"type": "password"}) is not None

    def _login_form(self, soup):
        for form in soup.find_all("form"):
            if form.find("input", attrs={"type": "password"}):
                return form
        return None

    def _try_http_post_login(self, session: requests.Session, login_url: str, username: str, password: str,
                             start_url: str) -> AuthResult:
        """Posts a plain HTML login form (hidden CSRF fields included). Counts as logged in only when the
        answer is not a login page, shows no password field, and a members-only page then opens with the
        session (see _check_member_access; an MFA step, a lockout notice or a "check your email" page are not
        the member area). Sets self._definite when the site clearly refused (its own error message, a
        locked account, a second step), so the caller does not try again in Chrome."""
        self._definite = False
        try:
            r = session.get(login_url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
            bot = self._bot_answer(r, login_page=True)
            if bot:
                # A challenge page, or a CAPTCHA widget in the login form: Chrome would meet the same check,
                # and ARACHNIX does not try to complete it
                self._definite = True
                return False, bot, {}, None, set()
            if r.status_code != 200:
                return False, f"Login page returned HTTP {r.status_code}", {}, None, set()
            soup = BeautifulSoup(r.text, "html.parser")
            form = self._login_form(soup)
            if form is None:
                where = "it sits in an embedded frame" if soup.find(["iframe", "frame"]) else "built by JavaScript"
                return False, f"No HTML login form on the page ({where})", {}, None, set()
            if (form.get("method") or "post").strip().lower() != "post":
                return False, "The login form does not post its data", {}, None, set()
            post_url = urljoin(str(r.url), str(form.get("action") or "")) or str(r.url)

            payload: Dict[str, str] = {}
            user_name = self._form_field_name(form, getattr(self.config, "username_selector", None), user=True)
            pass_name = self._form_field_name(form, getattr(self.config, "password_selector", None), user=False)
            if not user_name or not pass_name:
                return False, "Could not map username and password fields in HTML form", {}, None, set()
            for inp in form.find_all(["input", "select", "textarea"]):
                name = str(inp.get("name") or "")
                if not name or name in payload:
                    continue
                itype = str(inp.get("type") or "text").lower()
                if itype in ("submit", "button", "image", "reset", "file"):
                    continue
                if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
                    continue
                payload[name] = str(inp.get("value") or "")
            payload[user_name] = username
            payload[pass_name] = password
            submit = form.find(["button", "input"], attrs={"type": "submit", "name": True})
            if submit is not None:
                payload.setdefault(str(submit.get("name")), str(submit.get("value") or ""))

            before_error = page_error_text(soup)   # a notice the login page always shows is not an answer
            resp = session.post(post_url, data=payload, timeout=self.timeout, verify=self.verify_ssl,
                                allow_redirects=True, headers={"Referer": str(r.url)})
            final = str(resp.url)
            bot = self._bot_answer(resp, login_page=True)
            if bot:
                self._definite = True
                return False, bot, {}, None, set()
            if resp.status_code >= 400:
                return False, f"The login form answered HTTP {resp.status_code}", {}, None, set()
            if is_login_url(final) or self._has_password_field(resp.text):
                err = page_error_text(resp.text)
                if err and err != before_error:
                    self._definite = True
                    return False, f"The site refused the login: {self._mask(err)}", {}, None, set()
                return False, "The site showed the login form again (wrong details, or the form needs JavaScript)", {}, None, set()
            verdict, refused_at, _ = self._check_member_access(session, start_url, final, resp.text)
            reason = second_step_reason(resp.text)
            strong = bool(reason) and not reason.startswith("The site refused")   # a code / lockout / email step
            if verdict == "refused" or (verdict == "unknown" and strong):
                if reason:
                    # A second step or a lockout is a clear answer; an error text on some other page may not be
                    self._definite = strong
                    return False, self._mask(reason), {}, None, set()
                return False, (f"The site accepted the form, but {refused_at} still asks for a login "
                               "(wrong details, or the form needs JavaScript)"), {}, None, set()
        except Exception as e:
            return False, f"HTTP POST login attempt failed: {self._safe_error(e)}", {}, None, set()

        session.headers.pop("Cookie", None)
        self.method = "http"
        self.session_state = {"cookies": jar_to_cookies(session.cookies, start_url), "local": {}, "session": {}}
        self.browser.set_session(self.session_state)
        self.landing_url = final
        links = self._links_from_html(resp.text, final)
        cookies = {c["name"]: c["value"] for c in self.session_state["cookies"]}
        self.log(f"HTTP form login successful ({len(cookies)} session cookies; landed on {final}"
                 + (f"; confirmed on {self.probe_url}" if self.member_check == "verified" else
                    "; no members-only page was found to confirm it") + ").")
        return True, "HTTP form authentication succeeded" + self._unconfirmed_note(), cookies, final, links

    def _mask(self, text: str) -> str:
        """`text` with the username and password replaced by ***."""
        for secret in ((self.config.password or ""), (self.config.username or "").strip()):
            if secret:
                text = text.replace(secret, "***")
        return text

    @staticmethod
    def _form_field_name(form, selector: Optional[str], user: bool) -> str:
        if selector:
            try:
                el = form.select_one(selector)
                if el is not None and el.get("name"):
                    return str(el.get("name"))
            except Exception:
                pass
        inputs = form.find_all("input")
        if not user:
            pw = form.find("input", attrs={"type": "password"})
            return str(pw.get("name") or "") if pw is not None else ""
        words = ("user", "mail", "login", "log", "account", "id", "name")
        texts = [i for i in inputs if str(i.get("type") or "text").lower() in ("text", "email", "tel") and i.get("name")]
        for inp in texts:
            if any(w in (str(inp.get("name")) + " " + str(inp.get("id") or "")).lower() for w in words):
                return str(inp.get("name"))
        return str(texts[0].get("name")) if texts else ""

    # ------------------------------------------------------------------
    # Browser login
    # ------------------------------------------------------------------
    def _try_browser_login(self, session: requests.Session, login_url: str, username: str, password: str,
                           start_url: str, fresh: bool = False) -> AuthResult:
        self._definite = False
        with self.browser.hold(timeout=120) as driver:
            if driver is None:
                reason = self.browser.last_error or "Chrome could not start"
                return False, f"Browser login unavailable: {reason}", {}, None, set()
            try:
                ok, msg = self._browser_login_steps(driver, login_url, username, password, fresh=fresh)
            except Exception as e:   # e.g. a form re-rendered while it was being filled (stale element)
                ok, msg = False, f"The login page changed while the crawler was filling it in ({self._safe_error(e)}). Try again."
            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
            if not ok:
                return False, msg, {}, None, set()
            # Apps draw their menus after login and may set the session cookie only after redirecting
            self._wait_for_links_to_settle(driver)
            landing = driver.current_url
            state = export_session(driver, start_url)
            html = driver.page_source or ""

        self.method = "browser"
        self.session_state = state
        self.browser.set_session(state, apply=False)
        session.headers.pop("Cookie", None)
        cookies_to_jar(session.cookies, state["cookies"])
        self.landing_url = landing
        # Confirmed on a members-only page: over HTTP, else in the browser (sites that accept the login only
        # there); a lockout, MFA or "check your email" page after submitting is not the member area
        verdict, refused_at, _ = self._check_member_access(session, start_url, landing, html)
        if verdict == "refused":
            seen = self._browser_shows_app(refused_at)
            if seen is not None and seen[0]:
                verdict, self.browser_only = "verified", True
                self.member_check, self.probe_url, self.probe_kind = "verified", refused_at, "login page"
                self.confirmed_url = refused_at
        reason = second_step_reason(html)
        if verdict == "refused" or (verdict == "unknown" and reason and not reason.startswith("The site refused")):
            self._definite = bool(reason)
            return False, self._mask(reason or f"Logged in, but {refused_at} still asks for a login. Check the email "
                                     "and password, or log in through the live login window."), {}, None, set()
        if verdict == "unknown":
            self.browser_only = not self._http_accepts(session, landing)
            # Apps that keep the login in localStorage look the same over HTTP with or without it: compare the
            # landing page in a second Chrome that has no login
            if not is_login_url(landing) and self._logged_out_in_browser(landing):
                self.member_check, self.confirmed_url = "verified", landing
        if self.browser_only:
            self.log("The site does not accept the login over plain HTTP (it sent the crawler back to the login "
                     "page): logged-in pages are loaded through Chrome instead.")
        else:
            self.log(f"HTTP session verified with the browser's {len(state['cookies'])} cookies.")
        links = self._links_from_html(html, landing)
        cookies = {c["name"]: c["value"] for c in state["cookies"]}
        self.log(f"Discovered {len(links)} authenticated internal links on landing page ({landing})")
        label = "Authentication verified successfully" if self.member_check == "verified" else "Logged in with Chrome"
        return (True, f"{label} ({len(cookies)} session cookies, {storage_origins(state)} storage origin(s), "
                      f"{len(links)} links)" + self._unconfirmed_note(), cookies, landing, links)

    def _browser_login_steps(self, driver, login_url: str, username: str, password: str,
                             fresh: bool = False) -> Tuple[bool, str]:
        """Fills and submits the login form in `driver`. Success = the browser left the login page and no
        password field is visible. Returns (ok, message)."""
        if fresh:
            clear_session(driver)
        self.log(f"Loading login page in Headless Chrome: {login_url}")
        self._get(driver, login_url)
        if fresh:
            try:
                driver.execute_script("try{localStorage.clear();sessionStorage.clear();}catch(e){}")
                self._get(driver, login_url)
            except Exception:
                pass
        self._wait_ready(driver, 10)
        challenge = self._visible_challenge(driver)
        if challenge:
            return False, self._note_challenge(challenge, self._current_url(driver))

        # SSO widgets and hosted login boxes put the form in an <iframe>: when the page has frames and its own
        # document shows no login fields, the form is filled in inside the frame
        framed = self._has_frames(driver)
        user_input, pass_input = self._find_fields(driver, wait=4.0 if framed else 10.0)
        in_frame = False
        if pass_input is None and user_input is None and (framed or self._has_frames(driver)):
            in_frame = self._enter_login_frame(driver)
            user_input, pass_input = self._find_fields(driver, wait=6.0 if not in_frame else 4.0)
        if pass_input is None and user_input is None and not in_frame and not is_login_url(driver.current_url) \
                and not self._password_visible(driver):
            return True, "Already logged in"
        if pass_input is None and user_input is not None:
            # Two-step login: the email first, then the password on the next screen
            self._fill(driver, user_input, username)
            nxt = self._find_login_submit(driver, user_input)
            if nxt is not None:
                self._click(driver, nxt)
            else:
                self._press_enter(user_input)
            _, pass_input = self._find_fields(driver, wait=10.0, password_only=True)
            user_input = None
        if pass_input is None:
            if not in_frame and self._has_frames(driver):
                return False, FRAME_FIELD_MSG
            return False, NO_FIELD_MSG

        self._wait_hydrated(driver, pass_input)
        if user_input is not None and not self._fill(driver, user_input, username):
            return False, "Could not type into the email/username field"
        if not self._fill(driver, pass_input, password):
            return False, "Could not type into the password field"
        submit = self._custom(driver, getattr(self.config, "submit_selector", None)) or \
            self._find_login_submit(driver, pass_input)
        banner = self._login_error(driver)  # an alert already showing before submitting is not an answer
        if submit is not None:
            self._wait_enabled(driver, submit)
            # Hydration can re-render the form (the old fields go stale) and wipe what was typed before it
            # finished: find the fields again and type once more
            if (user_input is not None and self._value(user_input) != username) or self._value(pass_input) != password:
                fresh_user, fresh_pass = self._find_fields(driver, wait=3.0)
                if fresh_pass is not None:
                    pass_input = fresh_pass
                    user_input = fresh_user if user_input is not None else None
                if user_input is not None and self._value(user_input) != username:
                    self._fill(driver, user_input, username)
                if self._value(pass_input) != password:
                    self._fill(driver, pass_input, password)
                submit = self._custom(driver, getattr(self.config, "submit_selector", None)) or \
                    self._find_login_submit(driver, pass_input) or submit
            if not self._click(driver, submit):
                self._press_enter(pass_input)
        else:
            self._press_enter(pass_input)
        self.log("Credentials submitted. Waiting for authentication response...")
        if in_frame:
            time.sleep(0.5)
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

        submitted = time.time()
        while time.time() - submitted < 20:
            time.sleep(0.5)
            try:
                current = driver.current_url
                pw_visible = self._password_visible(driver)
            except Exception:
                continue
            if not pw_visible and not is_login_url(current):
                return True, "Logged in"   # confirmed on a members-only page next (_try_browser_login)
            if time.time() - submitted > 2.0 and pw_visible:
                err = self._login_error(driver)
                if err and err != banner:
                    err = self._mask(err)
                    self.log(f"Login rejected by website: {err}")
                    self._definite = True
                    return False, f"Authentication failed: {err}"
            challenge = self._visible_challenge(driver)
            if challenge:
                return False, self._note_challenge(challenge, self._current_url(driver))
        if self._otp_visible(driver):
            return False, SECOND_STEP_MSG
        return False, "Submitted credentials but could not confirm successful login. Check email/password."

    @staticmethod
    def _value(el) -> Optional[str]:
        """The field's current value, None when the element is gone (re-rendered)."""
        try:
            return el.get_attribute("value") or ""
        except Exception:
            return None

    @staticmethod
    def _press_enter(el):
        from selenium.webdriver.common.keys import Keys
        try:
            el.send_keys(Keys.RETURN)
        except Exception:
            pass

    @staticmethod
    def _otp_visible(driver) -> bool:
        try:
            return bool(driver.execute_script(_OTP_JS))
        except Exception:
            return False

    @staticmethod
    def _has_frames(driver) -> bool:
        from selenium.webdriver.common.by import By
        try:
            return any(f.is_displayed() for f in driver.find_elements(By.CSS_SELECTOR, "iframe, frame"))
        except Exception:
            return False

    @staticmethod
    def _enter_login_frame(driver) -> bool:
        """Switches the driver into the first visible frame that holds a password field (True), else stays in
        the page (False)."""
        from selenium.webdriver.common.by import By
        try:
            frames = [f for f in driver.find_elements(By.CSS_SELECTOR, "iframe, frame") if f.is_displayed()]
        except Exception:
            return False
        for frame in frames[:8]:
            try:
                driver.switch_to.frame(frame)
                if any(el.is_displayed() for el in driver.find_elements(By.CSS_SELECTOR, "input[type='password']")):
                    return True
            except Exception:
                pass
            try:
                driver.switch_to.default_content()
            except Exception:
                return False
        return False

    @staticmethod
    def _get(driver, url: str):
        try:
            driver.get(url)
        except Exception:
            try:
                driver.execute_script("window.stop();")
            except Exception:
                pass

    @staticmethod
    def _wait_ready(driver, max_wait: float = 10.0):
        end = time.time() + max_wait
        while time.time() < end:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.25)

    def _custom(self, driver, selector: Optional[str]):
        if not selector:
            return None
        from selenium.webdriver.common.by import By
        try:
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                if el.is_displayed():
                    return el
        except Exception:
            pass
        return None

    def _find_fields(self, driver, wait: float = 10.0, password_only: bool = False):
        """(username input, password input) once visible; forms drawn by JavaScript appear late."""
        from selenium.webdriver.common.by import By

        def first(selectors):
            for sel in selectors:
                try:
                    for el in driver.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed() and el.is_enabled():
                            return el
                except Exception:
                    continue
            return None

        def in_form(field):
            # The username box of the password's own form (not a site search box in the header)
            try:
                form = field.find_element(By.XPATH, "./ancestor::form[1]")
            except Exception:
                return None
            for sel in USER_SELECTORS:
                try:
                    for el in form.find_elements(By.CSS_SELECTOR, sel):
                        if el.is_displayed() and el.is_enabled():
                            return el
                except Exception:
                    continue
            return None

        end = time.time() + wait
        user_el = pass_el = None
        while True:
            pass_el = self._custom(driver, getattr(self.config, "password_selector", None)) or first(PASS_SELECTORS)
            if not password_only:
                user_el = (self._custom(driver, getattr(self.config, "username_selector", None))
                           or (in_form(pass_el) if pass_el is not None else None) or first(USER_SELECTORS))
                if user_el is not None and pass_el is not None and user_el == pass_el:
                    user_el = None
            if pass_el is not None or time.time() > end:
                break
            if user_el is not None and time.time() > end - wait / 2:
                break  # a username field alone: two-step login
            time.sleep(0.4)
        return user_el, pass_el

    @staticmethod
    def _fill(driver, el, value: str) -> bool:
        """Types `value` and checks the field holds it; falls back to the native value setter plus input
        events (what React and Vue listen to) when typing was swallowed."""
        try:
            el.clear()
        except Exception:
            pass
        try:
            el.send_keys(value)
        except Exception:
            pass
        try:
            if (el.get_attribute("value") or "") == value:
                return True
            return bool(driver.execute_script(_SET_VALUE_JS, el, value))
        except Exception:
            return False

    @staticmethod
    def _wait_enabled(driver, el, max_wait: float = 10.0):
        end = time.time() + max_wait
        while time.time() < end:
            try:
                if el.is_enabled() and el.get_attribute("aria-disabled") != "true":
                    return True
            except Exception:
                return False
            time.sleep(0.25)
        return False

    def _wait_hydrated(self, driver, pass_input, max_wait: float = 10.0):
        """Server-rendered login forms work only once their JavaScript has loaded: wait for the page to
        finish loading and for the submit button to be enabled."""
        self._wait_ready(driver, max_wait)
        submit = self._find_login_submit(driver, pass_input)
        if submit is not None:
            self._wait_enabled(driver, submit, max_wait)
        time.sleep(0.3)

    @staticmethod
    def _click(driver, el) -> bool:
        """Clicks `el` (a script click when a real one fails); False when neither worked."""
        try:
            el.click()
            return True
        except Exception:
            try:
                driver.execute_script("arguments[0].click();", el)
                return True
            except Exception:
                return False

    @staticmethod
    def _password_visible(driver) -> bool:
        try:
            return bool(driver.execute_script(_PASSWORD_VISIBLE_JS))
        except Exception:
            return False

    @staticmethod
    def _login_error(driver) -> str:
        try:
            return str(driver.execute_script(_LOGIN_ERROR_JS) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _find_login_submit(driver, field):
        """The button that submits the credentials: a visible submit control in the field's own <form>,
        preferring "Sign in"/"Log in" labels and skipping social / sign-up buttons. None = press Enter."""
        from selenium.webdriver.common.by import By
        skip = ("google", "github", "microsoft", "apple", "facebook", "create", "sign up", "signup", "register",
                "forgot", "show", "hide")
        prefer = ("sign in", "log in", "login", "signin", "continue", "submit", "next")
        try:
            form = field.find_element(By.XPATH, "./ancestor::form[1]")
            candidates = form.find_elements(By.CSS_SELECTOR, "button, input[type='submit']")
        except Exception:
            candidates = driver.find_elements(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']")
        usable = []
        for el in candidates:
            try:
                if not el.is_displayed():
                    continue
                label = ((el.text or "") + " " + (el.get_attribute("value") or "")).strip().lower()
                btype = (el.get_attribute("type") or "submit").lower()
                if btype not in ("submit", "") or any(s in label for s in skip):
                    continue
                usable.append((label, el))
            except Exception:
                continue
        for label, el in usable:
            if any(p in label for p in prefer):
                return el
        return usable[0][1] if usable else None

    @staticmethod
    def _visible_challenge(driver) -> Optional[str]:
        """The provider of a CAPTCHA / bot challenge the visitor must pass ("Cloudflare", "Cloudflare
        Turnstile", "Google reCAPTCHA", "Bot check"...): a rendered widget or a full-page interstitial; a script
        tag that is only loaded does not count. None when the page shows none (see browser_bot_check)."""
        found = browser_bot_check(driver)
        return found["provider"] if found else None

    @staticmethod
    def _current_url(driver) -> str:
        try:
            return driver.current_url or ""
        except Exception:
            return ""

    @staticmethod
    def _wait_for_links_to_settle(driver, max_wait: float = 10.0):
        """Polls until the page is loaded and its <a href> count has been stable for ~1.5s."""
        start = time.time()
        last_count, stable_since = -1, time.time()
        while time.time() - start < max_wait:
            time.sleep(0.5)
            try:
                ready = driver.execute_script("return document.readyState") == "complete"
                count = driver.execute_script("return document.querySelectorAll('a[href]').length")
            except Exception:
                continue
            if count != last_count:
                last_count, stable_since = count, time.time()
            elif ready and time.time() - stable_since >= 1.5:
                return

    # ------------------------------------------------------------------
    # Checks and renewal
    # ------------------------------------------------------------------
    def _http_accepts(self, session: requests.Session, url: Optional[str]) -> bool:
        """True when the HTTP session opens `url` without being sent to the login page."""
        if not url:
            return True
        try:
            r = session.get(url, timeout=self.timeout, verify=self.verify_ssl, allow_redirects=True)
        except Exception as e:
            self.log(f"Could not verify the HTTP session: {self._safe_error(e)}")
            return True
        if self._bot_answer(r):
            return False
        # 401/403 and 5xx (bot walls answer 403/503 to non-browsers) mean plain HTTP is not accepted
        return not (is_login_url(str(r.url)) or r.status_code in (401, 403) or r.status_code >= 500)

    def session_valid(self, session: requests.Session) -> Optional[bool]:
        """Whether the session is still logged in (None when there is no page to tell by). Sites that accept
        the login only in Chrome are checked in the browser."""
        if not self.probe_url:
            return None
        if self.browser_only:
            seen = self._browser_shows_app(self.probe_url)
            return None if seen is None else seen[0]
        try:
            r = session.get(self.probe_url, timeout=min(self.timeout, 10), verify=self.verify_ssl, allow_redirects=True)
        except Exception:
            return None
        if r.status_code == 429 or r.status_code >= 500:
            return None             # busy or failing server: says nothing about the login
        wall = self._wall(r)
        if wall == "HTTP 403" and self.probe_kind != "HTTP 403":
            return None             # a firewall's 403, not this site's login wall
        return not wall

    def relogin(self, session: requests.Session) -> Tuple[bool, str]:
        """Logs in again with the same username and password after the session expired. Pasted cookies,
        the live window and headers cannot be renewed automatically."""
        if self.method not in ("http", "browser"):
            return False, "The saved session has expired and cannot be renewed automatically; log in again."
        username = (self.config.username or "").strip()
        password = self.config.password or ""
        login_url = self.login_url or self._find_login_url(session, self.start_url)
        session.cookies.clear()
        if self.method == "http":
            ok, msg, _, _, _ = self._try_http_post_login(session, login_url, username, password, self.start_url)
            if ok or self._definite or not chrome_available():
                return ok, msg
        ok, msg, _, _, _ = self._try_browser_login(session, login_url, username, password, self.start_url, fresh=True)
        return ok, msg

    # ------------------------------------------------------------------
    def _links_from_html(self, html: str, base_url: str) -> Set[str]:
        links: Set[str] = set()
        if not html:
            return links
        base_dom = get_domain(base_url)
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return links
        for a in soup.find_all("a", href=True):
            href = str(a.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:")):
                continue
            clean = normalize_url(urljoin(base_url, href))
            if clean and is_internal_url(clean, base_dom) and not is_unsafe_url(clean):
                links.add(clean)
        return links

    def test(self, session: requests.Session, start_url: str, screenshot_dir: str = "") -> Dict[str, Any]:
        """Logs in, checks that a protected page opens, optionally screenshots it, and closes the browser.
        Shape: {"ok", "method", "landing_url", "cookies_count", "storage_origins", "message", "screenshot",
        "challenge"} where "challenge" is challenge_info() (a bot check met on the way, never completed)."""
        out: Dict[str, Any] = {"ok": False, "method": "", "landing_url": "", "cookies_count": 0,
                               "storage_origins": 0, "message": "", "screenshot": "", "challenge": challenge_status()}
        try:
            try:
                ok, msg, _, landing, _ = self.authenticate(session, start_url)
            except Exception as e:
                out["message"] = f"The login stopped with an unexpected error: {self._safe_error(e)}"
                return out
            state = self.session_state or {}
            method = {"live": "cookies"}.get(self.method, self.method or "http")
            out.update(ok=ok, method=method, landing_url=landing or "", message=msg,
                       cookies_count=cookie_count(state), storage_origins=storage_origins(state))
            if not ok:
                return out
            if (self.member_check != "verified" or not self.confirmed_url) and self.challenge is not None:
                # The members-only pages answered with a bot check, so the login could not be confirmed
                out.update(ok=False, message=bot_check_message(self.challenge["provider"], self.challenge["where"]))
                return out
            if self.member_check != "verified" or not self.confirmed_url:
                # The start page opens with or without a login: nothing proves the login works
                out.update(ok=False, message=(
                    f"{msg.split(';')[0]}, but the login could not be confirmed: no page tried "
                    f"({urlparse(start_url).path or '/'}, the landing page, /account, /my-account...) asks a "
                    "logged-out visitor to log in. Enter a members-only page (for example /my-account) as the "
                    "start URL and test again."))
                return out
            target = self.confirmed_url
            if self.browser_only:
                seen = self._browser_shows_app(target)
                page_ok = bool(seen and seen[0])
            else:
                page_ok = self._http_accepts(session, target)
            if not page_ok:
                message = f"Logged in, but {target} still sends the crawler to the login page."
                if self.challenge is not None:
                    message = bot_check_message(self.challenge["provider"], self.challenge["where"])
                out.update(ok=False, message=message)
                return out
            out["message"] = f"{msg}. Protected page opened: {target}"
            if screenshot_dir and chrome_available():
                out["screenshot"] = self._screenshot(target, screenshot_dir)
            return out
        finally:
            out["challenge"] = self.challenge_info()
            self.close()

    def _screenshot(self, url: str, out_dir: str) -> str:
        try:
            os.makedirs(out_dir, exist_ok=True)
        except OSError:
            return ""
        with self.browser.hold(timeout=60) as driver:
            if driver is None:
                return ""
            from .browser import set_media_blocked
            set_media_blocked(driver, False)
            self._get(driver, url)
            self._wait_ready(driver, 8)
            self._wait_for_links_to_settle(driver, max_wait=5)
            name = f"login_test_{int(time.time())}.jpg"
            try:
                import io
                from PIL import Image
                img = Image.open(io.BytesIO(driver.get_screenshot_as_png())).convert("RGB")
                if img.width > 1000:
                    img = img.resize((1000, int(img.height * 1000 / img.width)))
                img.save(os.path.join(out_dir, name), "JPEG", quality=72, optimize=True)
                return name
            except Exception:
                return ""


def test_login(auth_config: AuthConfig, start_url: str, user_agent: str = DEFAULT_USER_AGENT, timeout: int = 15,
               verify_ssl: bool = False, screenshot_dir: str = "",
               logger: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """For POST /api/auth/test: logs in with `auth_config`, verifies a protected page loads, closes everything.
    Returns {"ok", "method", "landing_url", "cookies_count", "storage_origins", "message", "screenshot",
    "challenge"}. The sign-in settings' own User-Agent (auth_config.user_agent) wins over `user_agent`."""
    user_agent = auth_user_agent(auth_config) or user_agent
    session = SiteSession()      # keeps host-only pasted cookies on their own host
    session.headers.update({"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"})
    handler = AuthHandler(auth_config, user_agent, timeout=timeout, verify_ssl=verify_ssl, logger=logger)
    return handler.test(session, normalize_url(start_url) or start_url, screenshot_dir=screenshot_dir)


# ------------------------------------------------------------------------------------------------------------
# "Test these cookies": one page that needs sign-in, fetched with the pasted cookies and (when it opens)
# once without them. Two or three requests; a sign-out address is never requested.
# ------------------------------------------------------------------------------------------------------------
# Private paths a robots.txt often disallows, best first ("customer/account" is Magento's)
_PRIVATE_PATHS = ("dashboard", "my-account", "myaccount", "account", "customer/account", "members", "member",
                  "portal", "app", "profile", "settings", "user", "admin", "wp-admin")
_SSO_HOST = re.compile(r"^(?:login|signin|sign-in|auth|sso|accounts?|id|idp|identity|oauth)\.", re.I)
_SIGNOUT_TEXT = re.compile(r"\b(?:log ?out|sign ?out|log ?off)\b", re.I)
_SIGNIN_TEXT = re.compile(r"^\s*(?:log ?in|sign ?in|login|signin)\s*$", re.I)
_EXPIRY_HINT = "Cookies expire, and signing out ends them: sign in again in your browser and copy fresh cookies."
_TEST_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


def _refused_host(url: str) -> str:
    """Why ARACHNIX will not request `url` for a test ('' when it may): no host, or a cloud metadata /
    link-local address."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return "That address is not a web page."
    if host in ("metadata.google.internal", "metadata"):
        return "That address is not allowed."
    try:
        if ipaddress.ip_address(socket.gethostbyname(host)).is_link_local:
            return "That address is not allowed."
    except (OSError, ValueError, UnicodeError):
        pass
    return ""


def cookie_count(state: Dict[str, Any]) -> int:
    """Cookies in a session state, a pasted cookie sent to the www and the bare host (two jar entries) counted once."""
    return len({(c.get("name"), c.get("path") or "/", c.get("value")) for c in state.get("cookies") or []
                if isinstance(c, dict)})


def _same_site(url: str, start_url: str) -> bool:
    from .browser import _site_cookie
    host = (urlparse(url or "").hostname or "").lower()
    return bool(host) and _site_cookie(host, get_domain(start_url))


def _openable(url: str) -> bool:
    """A page the test may open with someone's real session: never a sign-out or data-changing address (the
    bare WordPress dashboard only shows a page)."""
    parsed = urlparse(url)
    return (bool(_ADMIN_HOME.match(parsed.path or "")) and not parsed.query) or not is_unsafe_url(url)


def _robots_private_page(session: requests.Session, start_url: str, timeout, verify: bool) -> str:
    """A private page the site's robots.txt disallows (/dashboard, /my-account...), '' when none."""
    from .robots import parse_robots
    parsed = urlparse(start_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    try:
        r = session.get(origin + "/robots.txt", timeout=timeout, verify=verify, allow_redirects=True)
    except Exception:
        return ""
    if r.status_code != 200 or "html" in (r.headers.get("content-type") or "").lower():
        return ""
    best_rank, best = len(_PRIVATE_PATHS), ""
    for rules in parse_robots((r.text or "")[:200000]).values():
        for allow, pattern, _ in rules:
            path = pattern[:-1] if pattern.endswith("$") else pattern
            if allow or not path.startswith("/") or "*" in path or "?" in path:
                continue
            key = re.sub(r"^[a-z]{2}(?:-[a-z]{2})?/", "", path.strip("/").lower())
            if key in _PRIVATE_PATHS and _PRIVATE_PATHS.index(key) < best_rank:
                url = normalize_url(origin + path)
                if url and _openable(url):
                    best_rank, best = _PRIVATE_PATHS.index(key), url
    return best


def _cookie_test_page(paste: CookiePaste, start_url: str, test_url: Optional[str], session: requests.Session,
                      timeout, verify: bool) -> Tuple[str, str]:
    """(page to test, '') or ('', why none can be used): `test_url`; else the page a pasted cURL / PowerShell
    command requested; else a private path robots.txt disallows; else the start URL."""
    given = (test_url or "").strip()
    if given:
        url = normalize_url(given if "://" in given else urljoin(start_url, given)) or ""
        if not url.startswith(("http://", "https://")):
            return "", "The test page is not a web address."
        if not _same_site(url, start_url):
            return "", "The test page must be on the same site as the start URL."
        if is_login_url(url):
            return "", "Enter a page that needs sign-in, not the sign-in page itself."
        if not _openable(url):
            return "", ("That page signs out or changes data, so ARACHNIX won't open it with your cookies. "
                        "Enter another page that needs sign-in.")
        return url, ""
    if paste.request_url and paste.request_is_page:
        url = normalize_url(paste.request_url) or ""
        if url and _same_site(url, start_url) and not is_login_url(url) and _openable(url):
            return url, ""
    return _robots_private_page(session, start_url, timeout, verify) or start_url, ""


def _view_wall(view, target: str) -> str:
    """How a fetched page (login_guard.fetch_view) turned the visitor away: 'unsafe' (it redirects to a
    sign-out or data-changing address, never followed), 'login page', 'HTTP 401', 'HTTP 403', 'login form',
    or ''."""
    chain = list(view.hops) + ([view.final_url] if view.final_url else [])
    if any(is_unsafe_url(h) for h in view.hops) and view.final_status is None:
        return "unsafe"
    target_host = urlparse(target).hostname or ""
    if not is_login_url(target):
        for hop in chain:
            host = urlparse(hop).hostname or ""
            if is_login_url(hop) or (host and host != target_host and _SSO_HOST.match(host)):
                return "login page"
    if view.final_status in (401, 403):
        return f"HTTP {view.final_status}"
    if view.html and view.final_status and 200 <= view.final_status < 300 and shows_login_form(view.html):
        return "login form"
    return ""


def _shows_link(html: str, base_url: str, signout: bool) -> bool:
    """A sign-out (or, with signout=False, a sign-in) link or button on the page."""
    soup = _soup(html or "")
    if soup is None:
        return False
    for el in soup.find_all(["a", "button"])[:3000]:
        href = str(el.get("href") or el.get("formaction") or "").strip()
        text = el.get_text(" ", strip=True)[:40]
        url = urljoin(base_url, href) if href and not href.startswith(("#", "javascript:")) else ""
        if signout and ((url and is_logout_url(url)) or (text and len(text) < 30 and _SIGNOUT_TEXT.search(text))):
            return True
        if not signout and ((url and is_login_url(url)) or (text and _SIGNIN_TEXT.match(text))):
            return True
    return False


def test_cookies(raw: str, start_url: str, test_url: Optional[str] = None, user_agent: Optional[str] = None,
                 timeout: int = 15, verify_ssl: bool = False) -> Dict[str, Any]:
    """For POST /api/auth/cookies/test: are these pasted cookies signed in? Opens one page that needs sign-in
    (`test_url`; else the page a pasted cURL command requested; else a private path the robots.txt disallows,
    such as /dashboard; else the start URL) with the cookies and, when it opens, once more without them.
    Returns {"signed_in": True | False | None (couldn't tell), "status" (final HTTP status with the cookies),
    "final_url", "reason", "test_url", "user_agent_used": "given" | "pasted" | "default", "challenge"}.
    The User-Agent is `user_agent`, else the one the paste carried, else the default. Cookie values never
    appear in the answer, and a sign-out address is never requested."""
    from .login_guard import fetch_view
    out: Dict[str, Any] = {"signed_in": None, "status": None, "final_url": "", "reason": "", "test_url": "",
                           "user_agent_used": "default", "challenge": challenge_status()}
    start = (start_url or "").strip()
    if start and "://" not in start:
        start = "https://" + start
    start = normalize_url(start) or start
    secrets = _secret_set(paste_secrets(raw, start or "https://example.com/"))

    def done(signed_in: Optional[bool], reason: str, view=None) -> Dict[str, Any]:
        out["signed_in"] = signed_in
        out["reason"] = mask_text(reason, secrets)
        if view is not None:
            out["status"] = view.final_status
            out["final_url"] = mask_text(view.final_url or "", secrets)
        out["test_url"] = mask_text(out["test_url"], secrets)
        return out

    if not start.startswith(("http://", "https://")):
        return done(None, "Enter the website address first.")
    refused = _refused_host(start)
    if refused:
        return done(None, refused)
    paste = parse_cookie_paste(raw, start)
    if not paste.cookies:
        if paste.storage_origins() and not paste.rows:
            return done(None, "The paste holds browser storage but no cookies, which only the crawl's Chrome can "
                              "use; it is checked when the crawl starts.")
        return done(None, paste.error or "No cookies for this site could be read from the paste.")
    ua, used = (user_agent or "").strip(), "given"
    if not ua or _CTRL_CHARS.search(ua) or len(ua) > 512:
        ua, used = (paste.user_agent, "pasted") if paste.user_agent else (DEFAULT_USER_AGENT, "default")
    out["user_agent_used"] = used
    headers = {"User-Agent": ua, "Accept": _TEST_ACCEPT, "Accept-Language": "en-US,en;q=0.9"}
    t = (min(int(timeout or 15), 10), int(timeout or 15))
    anon, mine = requests.Session(), SiteSession()   # SiteSession: host-only pasted cookies stay on their host
    anon.headers.update(headers)
    mine.headers.update(headers)
    cookies_to_jar(mine.cookies, paste.cookies)
    note = (" Some pasted cookies had already expired." if any(r.expired for r in paste.rows) else "")
    storage = (" The sign-in is also kept in browser storage, which only the crawl's Chrome can use."
               if paste.storage_origins() else "")
    try:
        target, problem = _cookie_test_page(paste, start, test_url, anon, t, verify_ssl)
        if not target:
            return done(None, problem)
        out["test_url"] = target
        refused = _refused_host(target)
        if refused:
            return done(None, refused)
        v = fetch_view(mine, target, timeout=t, verify=verify_ssl, unsafe=is_unsafe_url)
        if v.error:
            return done(None, f"Couldn't tell: ARACHNIX couldn't open {target} ({v.error}).", v)
        if v.waf:
            out["challenge"] = challenge_status(v.waf, 1, v.final_url or target, "cookies")
            return done(None, "Couldn't tell. " + bot_check_message(v.waf, "cookies"), v)
        wall = _view_wall(v, target)
        if wall == "unsafe":
            return done(None, f"Couldn't tell: {target} leads to a sign-out or data-changing address, which ARACHNIX "
                              "never opens with your cookies (it could end your browser's session). Enter another "
                              "page that needs sign-in.", v)
        if is_login_url(target):
            # Only the sign-in page itself to go by (the start URL): a signed-in visitor is usually sent on,
            # while a signed-out one stays on it
            moved = not wall and not is_login_url(v.final_url or target) and 200 <= (v.final_status or 0) < 300 \
                and not shows_login_form(v.html or "")
            if moved:
                a = fetch_view(anon, target, timeout=t, verify=verify_ssl, unsafe=is_unsafe_url)
                stays = not a.error and not a.waf and (is_login_url(a.final_url or target)
                                                       or shows_login_form(a.html or ""))
                if stays:
                    return done(True, f"Signed in: with these cookies the sign-in page sent ARACHNIX on to "
                                      f"{v.final_url}; without them it stays on the sign-in page.", v)
            return done(None, f"Couldn't tell: the start URL is the sign-in page ({target}). Enter a page that needs "
                              "sign-in (for example /dashboard) and test again.", v)
        if wall in ("login page", "HTTP 401", "login form"):
            how = {"login page": f"sent ARACHNIX to the login page ({v.final_url})",
                   "HTTP 401": "answered HTTP 401 (sign-in needed)",
                   "login form": "shows a sign-in form"}[wall]
            return done(False, f"Not signed in: with these cookies {target} {how}.{note} {_EXPIRY_HINT}", v)
        if wall == "HTTP 403":
            return done(None, f"Couldn't tell: {target} answered HTTP 403 (access refused) with these cookies. The "
                              "account may not be allowed to open this page, or a firewall refused ARACHNIX. Try a "
                              "page your account can open.", v)
        step = second_step_reason(v.html) if v.html else ""
        if step and not step.startswith("The site refused"):
            return done(False, f"Not signed in: {step}", v)
        if v.final_status is None or v.final_status >= 400:
            return done(None, f"Couldn't tell: {target} answered HTTP {v.final_status}. Enter another page that "
                              "needs sign-in.", v)
        # It opened with the cookies: does it ask a signed-out visitor to sign in?
        a = fetch_view(anon, target, timeout=t, verify=verify_ssl, unsafe=is_unsafe_url)
        if a.error or a.waf:
            why = a.error or f"a bot check ({a.waf}) answered instead"
            return done(None, f"Couldn't tell: {target} opened with these cookies, but ARACHNIX couldn't see what a "
                              f"signed-out visitor gets ({why}).{storage}", v)
        anon_wall = _view_wall(a, target)
        if anon_wall in ("login page", "HTTP 401", "HTTP 403", "login form"):
            how = {"login page": "sends visitors to the login page", "login form": "shows a sign-in form"
                   }.get(anon_wall, f"refuses it ({anon_wall})")
            return done(True, f"Signed in: {target} opened with these cookies; without them the site {how}.", v)
        if _shows_link(v.html, v.final_url or target, signout=True) and \
                not _shows_link(a.html, a.final_url or target, signout=True):
            return done(True, f"Signed in: with these cookies {target} shows a sign-out link; without them it "
                              "doesn't.", v)
        if _shows_link(a.html, a.final_url or target, signout=False) and \
                not _shows_link(v.html, v.final_url or target, signout=False) and v.html:
            return done(True, f"Signed in: without these cookies {target} offers a sign-in link; with them it "
                              "doesn't.", v)
        return done(None, f"Couldn't tell: {target} opens for everyone, with or without these cookies. Enter a page "
                          "that needs sign-in (for example /dashboard or /my-account) and test again." + storage, v)
    except Exception as e:
        return done(None, f"Couldn't tell: the test stopped with an error ({type(e).__name__}).")
    finally:
        anon.close()
        mine.close()
