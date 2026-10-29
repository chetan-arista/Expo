"""
Paste cookies: reading a sign-in session that a person copied from their own browser.

One paste box accepts:
- header      a Cookie request header ("a=1; b=2", with or without "Cookie:", also one per line), or a block of
              request headers copied from DevTools (only Cookie, Set-Cookie and User-Agent are read)
- json        a JSON export: an array of cookie objects (Cookie-Editor, EditThisCookie, DevTools, Selenium,
              Puppeteer / Playwright), {"cookies": [...]} with optional storage ("local" / "session" by origin,
              or Playwright's "origins"), one cookie object, or a {name: value} object
- netscape    a Netscape cookies.txt file ("#HttpOnly_" lines included)
- curl        a "Copy as cURL" command, bash or Windows cmd quoting: its Cookie headers / -b and its User-Agent
              (-H / -A) are read; everything else is ignored
- powershell  a "Copy as PowerShell" snippet (Chrome / Edge on Windows): its cookies and User-Agent
- table       rows copied from the DevTools cookie table (tab-separated name, value, domain, path...)

parse_cookie_paste() returns what was read (CookiePaste); preview_cookies() the masked preview the dashboard
shows. Cookie values are only ever handed to the HTTP session / Chrome; they are never logged or returned.
A cookie pasted without a domain (Cookie header, cURL, a JSON export without one) is host-only, as a browser keeps
it: sent to the site's host and its www / bare twin (and a cURL request's own host on the site), never to the
other subdomains (shop / help / status hosts are often run by third parties). Domain-wide cookies stay so only
when the export says so (JSON hostOnly false, cookies.txt TRUE, a Set-Cookie Domain attribute).
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .browser import _site_cookie, normalize_cookie
from .url_utils import get_domain, registrable_domain

FORMATS = ("header", "json", "netscape", "curl", "powershell", "table")
MASK = "***"
MAX_PASTE_CHARS = 2_000_000       # a pasted cookie export is far smaller; anything bigger is not read
_COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_CTRL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_COOKIE_ATTRS = {"path", "domain", "expires", "max-age", "samesite", "secure", "httponly", "priority",
                 "partitioned", "version", "comment"}
_HEADER_LINE = re.compile(r"^(:?[A-Za-z][A-Za-z0-9_-]*)\s*:\s?(.*)$")
_SECRET_HEADER = re.compile(r"auth|token|key|secret|session|csrf|xsrf|signature|password|cookie", re.I)

# Names that usually carry a sign-in (session ids, auth tokens, framework session cookies)
_SIGNIN_NAME = re.compile(
    r"sess|(?:^|[._-])sid(?:$|[._-])|^sid|auth|token|jwt|login|logged|remember|identity|(?:^|[._-])user|"
    r"account|aspxauth|\.aspnetcore|jsessionid|phpsessid|connect\.sid|laravel|wordpress_logged_in|"
    r"wordpress_sec|^wp[-_]|next-auth|supabase|firebase|clerk|cognito|okta|keycloak|^kc[-_]|saml|oauth|oidc|"
    r"^_[a-z0-9]+_key$|^__client|customer|member", re.I)
# Analytics, consent, load-balancer and bot-check cookies: never the sign-in, whatever their name says
_NOT_SIGNIN = re.compile(
    r"^_ga|^_gid$|^_gat|^_gcl|^__utm|^_fb[pc]$|^_hj|^_cl(ck|sk)$|^__cf_bm$|^cf_clearance$|^__cfruid$|^_cfuvid$|"
    r"^optanon|^cookieconsent|^cookielawinfo|^cmplz|^euconsent|^cookieyes|^nid$|^aec$|^1p_jar$|^_pk_|^mp_|"
    r"^ajs_|^intercom-|^__stripe|^_uet|^_tt_|^_pin_|^li_|^lang$|^locale$|^theme$|csrf|xsrf|^awsalb|^awselb|"
    r"^arraffinity|^__hs|^hubspot|^_dc_gtm|^__gads|^__gpi|^_scid|^_rdt|^muid$|^anonymous|^guest|^visitor", re.I)

NO_SIGNIN_WARNING = ("None of these looks like a sign-in cookie (names such as session, sid, auth or token). "
                     "Copy them while you are signed in, and include HttpOnly cookies: the Application tab "
                     "(Chrome / Edge) or the Storage tab (Firefox) shows them, document.cookie does not.")


def cookie_kind(name: str) -> str:
    """"signin" when the cookie's name looks like it carries the sign-in, else "other"."""
    name = (name or "").strip()
    if not name or _NOT_SIGNIN.search(name):
        return "other"
    return "signin" if _SIGNIN_NAME.search(name) else "other"


@dataclass
class PastedCookie:
    """One cookie read from the paste (normalize_cookie format, value included) and whether it can be used."""
    cookie: Dict[str, Any]
    expired: bool = False
    other_site: bool = False
    # Pasted without a domain: host-only for cookie["domain"] (the site's host); also sent, host-only, to these
    also: List[str] = field(default_factory=list)
    hostless: bool = False

    @property
    def usable(self) -> bool:
        return not self.expired and not self.other_site


@dataclass
class CookiePaste:
    """What a paste held. `rows` lists every cookie read (expired and other sites' too, for the preview);
    `cookies` only those that will be sent."""
    format: str = ""
    rows: List[PastedCookie] = field(default_factory=list)
    local: Dict[str, Dict[str, str]] = field(default_factory=dict)
    session: Dict[str, Dict[str, str]] = field(default_factory=dict)
    user_agent: str = ""
    request_url: str = ""           # cURL / PowerShell: the address the copied request went to
    request_is_page: bool = False   # ... and it was a plain GET for a page (not an API call or a form post)
    rejected: int = 0               # cookies left out: a character a web request cannot carry
    warnings: List[str] = field(default_factory=list)
    error: str = ""                 # why nothing usable was read ('' when something was)
    secrets: List[str] = field(default_factory=list)   # other secret values in the paste (tokens, passwords)

    @property
    def cookies(self) -> List[Dict[str, Any]]:
        """The cookies that will be sent: a cookie pasted without a domain once per host it belongs to."""
        out: List[Dict[str, Any]] = []
        for r in self.rows:
            if r.usable:
                out.append(r.cookie)
                out.extend(dict(r.cookie, domain=host) for host in r.also)
        return out

    def storage_origins(self) -> int:
        return len(set(self.local) | set(self.session))

    def session_state(self) -> Dict[str, Any]:
        """The login in the session_state format (browser.export_session) with the usable cookies."""
        return {"cookies": [dict(c) for c in self.cookies], "local": dict(self.local), "session": dict(self.session)}

    def all_values(self) -> List[str]:
        """Every secret value the paste holds (cookie values, storage values, tokens), for masking."""
        out = [str(r.cookie.get("value") or "") for r in self.rows] + list(self.secrets)
        for store in (self.local, self.session):
            for vals in store.values():
                out += [str(v) for v in (vals or {}).values()]
        return [v for v in out if v]


# ---------------------------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------------------------
_CURL_START = re.compile(r"^(?:\$\s*|>\s*|PS [^>\n]*>\s*)?(?:[\w:./\\-]*[\\/])?curl(?:\.exe)?(?=\s|$)", re.I)
_PS_MARKERS = re.compile(r"New-Object\s+System\.Net\.Cookie|Invoke-(?:WebRequest|RestMethod)|WebRequestSession",
                         re.I)


def _netscape_cols(line: str) -> Optional[List[str]]:
    """The 7 fields of a cookies.txt line (tabs, or spaces when a copy turned tabs into spaces); None when the
    line is not one."""
    if line.startswith("#HttpOnly_"):
        line = line[len("#HttpOnly_"):]
    cols = line.split("\t")
    if len(cols) < 6:
        cols = line.split(None, 6)
    if len(cols) < 6:
        return None
    if cols[1].strip().upper() not in ("TRUE", "FALSE") or cols[3].strip().upper() not in ("TRUE", "FALSE"):
        return None
    if not re.match(r"^-?\d+(?:\.\d+)?$", cols[4].strip()):
        return None
    return cols


def detect_format(raw: str) -> str:
    """Which of FORMATS the paste is ("header" when nothing more specific fits)."""
    text = (raw or "").strip().lstrip("\ufeff")
    if not text:
        return ""
    if _CURL_START.match(text):
        return "curl"
    if _PS_MARKERS.search(text):
        return "powershell"
    if text[0] in "[{":
        return "json"
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if re.match(r"^#\s*(?:Netscape\s+)?HTTP\s+Cookie\s+File", lines[0], re.I):
        return "netscape"
    if any(_netscape_cols(ln) for ln in lines if not ln.lstrip().startswith("#") or ln.startswith("#HttpOnly_")):
        return "netscape"
    return "header"


# ---------------------------------------------------------------------------------------------
# Command-line splitting (cURL commands as browsers copy them)
# ---------------------------------------------------------------------------------------------
_ANSI_SIMPLE = {"n": "\n", "r": "\r", "t": "\t", "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
                "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}


def _ansi_c(text: str, i: int) -> Tuple[str, int]:
    """Reads a bash $'...' string whose body starts at text[i]; returns (value, index after the closing ')."""
    out: List[str] = []
    n = len(text)
    while i < n and text[i] != "'":
        c = text[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        e = text[i + 1]
        if e in _ANSI_SIMPLE:
            out.append(_ANSI_SIMPLE[e])
            i += 2
        elif e in "xuU":
            size = {"x": 2, "u": 4, "U": 8}[e]
            m = re.match(r"[0-9a-fA-F]{1,%d}" % size, text[i + 2:i + 2 + size])
            if m:
                try:
                    out.append(chr(int(m.group(0), 16)))
                except (ValueError, OverflowError):
                    pass
                i += 2 + len(m.group(0))
            else:
                out.append("\\" + e)
                i += 2
        elif e in "01234567":
            m = re.match(r"[0-7]{1,3}", text[i + 1:i + 4])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
        elif e == "c" and i + 2 < n:
            out.append(chr(ord(text[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + e)
            i += 2
    return "".join(out), i + 1


def split_posix(text: str) -> List[str]:
    """Words of a bash command line: '...', "...", $'...', backslash escapes and line continuations."""
    words: List[str] = []
    cur: List[str] = []
    have = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            nxt = text[i + 1:i + 2]
            if nxt == "\n":
                i += 2
            elif nxt == "\r" and text[i + 2:i + 3] == "\n":
                i += 3
            else:
                cur.append(nxt)
                have = True
                i += 2
            continue
        if c in " \t\r\n":
            if have:
                words.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        if c == "#" and not have:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            cur.append(text[i + 1:j])
            have = True
            i = j + 1
            continue
        if c == "$" and text[i + 1:i + 2] == "'":
            value, i = _ansi_c(text, i + 2)
            cur.append(value)
            have = True
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n and text[i + 1] in '"\\$`\n':
                    if text[i + 1] != "\n":
                        cur.append(text[i + 1])
                    i += 2
                    continue
                cur.append(text[i])
                i += 1
            i += 1
            have = True
            continue
        cur.append(c)
        have = True
        i += 1
    if have:
        words.append("".join(cur))
    return words


def _cmd_unescape(text: str) -> str:
    """What cmd.exe hands to a program: ^X becomes X outside "..." (^" too, as Chrome's "Copy as cURL (cmd)"
    writes every quote), and a caret at the end of a line joins the next line."""
    out: List[str] = []
    quoted = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            quoted = not quoted
            out.append(c)
            i += 1
            continue
        if c == "^" and not quoted and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "\r" and text[i + 2:i + 3] == "\n":
                i += 3
            elif nxt == "\n":
                i += 2
            else:
                out.append(nxt)
                i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def split_windows(text: str) -> List[str]:
    """Words of a Windows command line (cmd.exe caret escapes first, then the C runtime's rules: "..." groups,
    \\" is a quote, "" inside quotes is a quote, backslashes are literal unless they precede a quote)."""
    text = _cmd_unescape(text)
    words: List[str] = []
    cur: List[str] = []
    have = quoted = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            j = i
            while j < n and text[j] == "\\":
                j += 1
            count = j - i
            if j < n and text[j] == '"':
                cur.append("\\" * (count // 2))
                if count % 2:
                    cur.append('"')
                    j += 1
            else:
                cur.append("\\" * count)
            have = True
            i = j
            continue
        if c == '"':
            if quoted and text[i + 1:i + 2] == '"':
                cur.append('"')
                i += 2
            else:
                quoted = not quoted
                i += 1
            have = True
            continue
        if c in " \t\r\n" and not quoted:
            if have:
                words.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have = True
        i += 1
    if have:
        words.append("".join(cur))
    return words


def split_command(text: str) -> List[str]:
    """The words of a pasted command, with cmd quoting when it uses carets (^" or ^ at line ends), else bash."""
    if re.search(r'\^"|\^[ \t]*\r?\n', text):
        return split_windows(text)
    return split_posix(text)


# Options of curl that take a value (the value is skipped, or read when it matters)
_CURL_VALUE_OPTS = {
    "-H", "--header", "-b", "--cookie", "-A", "--user-agent", "--url", "-X", "--request", "-d", "--data",
    "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json", "-F", "--form", "--form-string",
    "-e", "--referer", "-u", "--user", "-U", "--proxy-user", "--oauth2-bearer", "-o", "--output", "-x", "--proxy",
    "-m", "--max-time", "--connect-timeout", "-w", "--write-out", "-c", "--cookie-jar", "-T", "--upload-file",
    "-r", "--range", "-K", "--config", "-E", "--cert", "--key", "--cacert", "--capath", "--resolve",
    "--connect-to", "--interface", "-Y", "--speed-limit", "-y", "--speed-time", "-z", "--time-cond",
    "--limit-rate", "-C", "--continue-at", "--retry", "--retry-delay", "--retry-max-time", "--max-redirs",
    "--max-filesize", "-D", "--dump-header", "--trace", "--trace-ascii", "--stderr", "--proto", "--noproxy",
    "--proxy-header", "--cert-type", "--key-type", "--pass", "--aws-sigv4", "--url-query", "--variable",
    "--output-dir", "--ciphers", "--local-port", "--unix-socket", "--dns-servers", "--preproxy",
}
_CURL_SECRET_OPTS = {"-u", "--user", "-U", "--proxy-user", "--oauth2-bearer", "-d", "--data", "--data-raw",
                     "--data-binary", "--data-ascii", "--data-urlencode", "--json", "-F", "--form", "--form-string",
                     "--pass", "--aws-sigv4", "--proxy-header"}
_CURL_BODY_OPTS = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json",
                   "-F", "--form", "--form-string", "-T", "--upload-file"}


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
class _Reader:
    """Collects cookies into a CookiePaste, filling in the site's domain where the paste has none."""

    def __init__(self, start_url: str, out: CookiePaste):
        self.out = out
        start_url = (start_url or "").strip()
        if start_url and "://" not in start_url:
            start_url = "https://" + start_url          # "example.com" typed without a scheme
        self.start_url = start_url
        self.host = (urlparse(self.start_url).hostname or "").lower()
        self.base = get_domain(self.start_url)
        self.is_ip = bool(re.match(r"^[\d.]+$", self.host)) or self.host == "localhost" or ":" in self.host
        self.https = self.start_url.lower().startswith("https://")
        self._seen: Dict[Tuple[str, str, str], int] = {}
        # The www / bare twin of the site's host (www.example.com <-> example.com): the same site for a pasted
        # cookie. None for an IP address, localhost or another subdomain (app.example.com).
        self.twin = ""
        if self.host and not self.is_ip:
            if self.host.startswith("www."):
                self.twin = self.host[4:]
            elif "." in self.host and self.host == registrable_domain(self.host):
                self.twin = "www." + self.host

    def site_hosts(self, request_url: str = "") -> List[str]:
        """Hosts a cookie pasted without a domain is sent to (host-only each): the site's host, its www / bare
        twin, and the host a copied request (cURL / PowerShell) went to when it is on the site."""
        hosts = [h for h in (self.host, self.twin) if h]
        req = (urlparse(request_url).hostname or "").lower() if request_url else ""
        if req and req not in hosts and self.base and _site_cookie(req, self.base):
            hosts.append(req)
        return hosts

    def bare(self, name: str, value: str) -> Dict[str, Any]:
        """A cookie pasted without a domain (filled in by keep(): host-only for the site's host)."""
        return {"name": name, "value": value, "domain": "", "path": "/",
                "secure": self.https or name.startswith(("__Secure-", "__Host-"))}

    def keep(self, item: Optional[Dict[str, Any]], default_secure: Optional[bool] = None):
        """Adds one cookie (any exporter's field names). Cookies that cannot travel in a request are counted
        in `rejected`; a repeat of the same name, domain and path replaces the earlier one."""
        ck = normalize_cookie(item) if item else None
        if not ck:
            return
        hostless = not ck["domain"]
        if hostless:
            ck["domain"] = self.host          # host-only (no leading dot); finish() adds the twin hosts
            if default_secure is not None and not ck["secure"]:
                ck["secure"] = default_secure
        value = ck["value"]
        try:
            value.encode("latin-1")
            ok = True
        except UnicodeEncodeError:
            ok = False
        if not ok or not _COOKIE_NAME.match(ck["name"]) or _CTRL_CHARS.search(value) or ";" in value:
            self.out.rejected += 1
            if value:
                self.out.secrets.append(value)
            return
        key = (ck["name"], ck["domain"].lstrip(".").lower(), ck["path"])
        row = PastedCookie(cookie=ck, hostless=hostless)
        if key in self._seen:
            self.out.rows[self._seen[key]] = row
        else:
            self._seen[key] = len(self.out.rows)
            self.out.rows.append(row)

    def finish(self):
        """Gives each cookie pasted without a domain its other hosts (the www / bare twin, a cURL request's host);
        a cookie the paste also names for that host itself is left as given."""
        hosts = self.site_hosts(self.out.request_url)
        named = {(r.cookie["name"], r.cookie["domain"].lstrip(".").lower(), r.cookie["path"]) for r in self.out.rows}
        for row in self.out.rows:
            if row.hostless and row.cookie["domain"]:
                ck = row.cookie
                row.also = [h for h in hosts if h != ck["domain"] and (ck["name"], h, ck["path"]) not in named]

    def pairs(self, text: str, attributes: bool = True):
        """name=value pairs of a Cookie header ("a=1; b=2")."""
        for pair in text.split(";"):
            if "=" not in pair:
                continue
            k, v = pair.split("=", 1)
            k, v = k.strip(), v.strip()
            if k and not (attributes and k.lower() in _COOKIE_ATTRS):
                self.keep(self.bare(k, v))

    def set_cookie(self, text: str):
        """One Set-Cookie header value: the cookie with its attributes."""
        parts = [p.strip() for p in text.split(";")]
        if not parts or "=" not in parts[0]:
            return
        name, value = (s.strip() for s in parts[0].split("=", 1))
        item: Dict[str, Any] = self.bare(name, value)
        item["secure"] = False
        for attr in parts[1:]:
            k, _, v = attr.partition("=")
            k = k.strip().lower()
            if k == "domain" and v.strip():
                item["domain"] = "." + v.strip().lstrip(".")
            elif k == "path" and v.strip():
                item["path"] = v.strip()
            elif k == "secure":
                item["secure"] = True
            elif k == "httponly":
                item["httpOnly"] = True
            elif k == "samesite":
                item["sameSite"] = v.strip()
            elif k == "max-age" and re.match(r"^-?\d+$", v.strip()):
                item["expires"] = time.time() + int(v.strip()) if int(v.strip()) > 0 else 1
            elif k == "expires" and "expires" not in item:
                item["expires"] = v.strip()
        self.keep(item)

    def note_secret(self, value: str):
        value = (value or "").strip()
        if len(value) >= 4:
            self.out.secrets.append(value)
            for word in value.split():
                if len(word) >= 8:
                    self.out.secrets.append(word)


def _read_json(text: str, out: CookiePaste, rd: _Reader):
    try:
        parsed = json.loads(text)
    except ValueError as e:
        line = getattr(e, "lineno", None)
        where = f" (line {line}, column {getattr(e, 'colno', '?')})" if line else ""
        out.error = (f"The paste looks like a JSON cookie export but could not be read{where}. Copy the whole "
                     "export again.")
        return
    items: List[Any] = []
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        lowered = {str(k).lower(): v for k, v in parsed.items()}
        if isinstance(lowered.get("cookies"), list):
            items = lowered["cookies"]
            for kind in ("local", "session"):
                block = lowered.get(kind)
                if isinstance(block, dict):
                    store = out.local if kind == "local" else out.session
                    for origin, vals in block.items():
                        if isinstance(vals, dict) and str(origin).startswith(("http://", "https://")):
                            store[str(origin)] = {str(k): str(v) for k, v in vals.items()}
            for entry in lowered.get("origins") or []:          # Playwright storage state
                if isinstance(entry, dict) and str(entry.get("origin") or "").startswith(("http://", "https://")):
                    vals = {str(x.get("name")): str(x.get("value") or "") for x in entry.get("localStorage") or []
                            if isinstance(x, dict) and x.get("name") is not None}
                    if vals:
                        out.local[str(entry["origin"])] = vals
        elif normalize_cookie(parsed) is not None:
            items = [parsed]                                    # a single cookie object
        else:
            for k, v in parsed.items():                         # {name: value}
                if isinstance(v, (str, int, float)) and not isinstance(v, bool):
                    rd.keep(rd.bare(str(k).strip(), str(v)))
    for item in items:
        if isinstance(item, dict):
            rd.keep(item, default_secure=rd.https)


def _read_netscape(text: str, out: CookiePaste, rd: _Reader):
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if not line.strip() or (line.lstrip().startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        cols = _netscape_cols(line)
        if cols is None:
            continue
        domain, sub, path, secure, expiry, name = (c.strip() for c in cols[:6])
        value = cols[6].strip() if len(cols) > 6 else ""
        host_only = sub.upper() != "TRUE"
        rd.keep({"name": name, "value": value, "domain": domain.lstrip(".") if host_only else "." + domain.lstrip("."),
                 "path": path or "/", "secure": secure.upper() == "TRUE", "httpOnly": line.startswith("#HttpOnly_"),
                 "expires": expiry})


def _table_row(cols: List[str], rd: _Reader) -> bool:
    """A row copied from the DevTools cookie table: name, value, domain, path, expires, size, HttpOnly,
    Secure, SameSite... True when it was one."""
    if len(cols) < 2 or "=" in cols[0] or not cols[0] or cols[0].lower() == "name":
        return False
    item: Dict[str, Any] = rd.bare(cols[0], cols[1])
    if len(cols) > 2 and ("." in cols[2] or cols[2] == "localhost"):
        item["domain"] = cols[2]
        item["path"] = cols[3] if len(cols) > 3 and cols[3].startswith("/") else "/"
    if len(cols) > 4 and cols[4] and cols[4].lower() != "session":
        item["expires"] = cols[4]
    if len(cols) > 6:
        item["httpOnly"] = cols[6]
    if len(cols) > 7:
        item["secure"] = cols[7] or item["secure"]
    if len(cols) > 8 and cols[8]:
        item["sameSite"] = cols[8]
    rd.keep(item)
    return True


def _read_lines(text: str, out: CookiePaste, rd: _Reader):
    """Cookie headers, request header blocks, Set-Cookie lines and DevTools table rows."""
    table = header = 0
    other_auth = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "\t" in line and _table_row([c.strip() for c in line.split("\t")], rd):
            table += 1
            continue
        m = _HEADER_LINE.match(line)
        if m:
            name, value = m.group(1).lower(), m.group(2).strip()
            if name == "cookie":
                rd.pairs(value)
                header += 1
            elif name == "set-cookie":
                rd.set_cookie(value)
                header += 1
            elif name == "user-agent":
                out.user_agent = value
            elif _SECRET_HEADER.search(name):
                rd.note_secret(value)
                other_auth = other_auth or name in ("authorization", "x-api-key", "x-auth-token")
            continue                                         # any other request header is ignored
        rd.pairs(line)
        header += 1
    out.format = "table" if table and not header else "header"
    if other_auth:
        out.warnings.append("The paste also has an Authorization / token header. ARACHNIX reads only cookies here; "
                            "if the site signs in with that header, add it under Sign-in > Custom header.")


def _read_curl(text: str, out: CookiePaste, rd: _Reader):
    words = split_command(text)
    while words and words[0] in ("$", ">"):
        words = words[1:]
    if words and _CURL_START.match(words[0] + " "):
        words = words[1:]
    method, has_body, accept, dest, mode = "", False, "", "", ""
    url, cookie_file, other_auth, saw_cookie = "", False, False, False
    i = 0
    while i < len(words):
        w = words[i]
        opt, value = w, None
        if w.startswith("--") and "=" in w and w.split("=", 1)[0] in _CURL_VALUE_OPTS:
            opt, value = w.split("=", 1)
        elif not w.startswith("--") and len(w) > 2 and w[:2] in ("-H", "-b", "-A", "-X", "-d", "-u", "-e", "-F"):
            opt, value = w[:2], w[2:]
        elif w in _CURL_VALUE_OPTS:
            value = words[i + 1] if i + 1 < len(words) else ""
            i += 1
        elif not w.startswith("-") and not url:
            url = w
        i += 1
        if value is None:
            if opt in ("-G", "--get"):
                method = method or "GET"
            continue
        if opt in _CURL_SECRET_OPTS:
            rd.note_secret(value)
        if opt in _CURL_BODY_OPTS:
            has_body = True
        if opt in ("-H", "--header", "--proxy-header"):
            m = _HEADER_LINE.match(value.strip())
            if not m or opt == "--proxy-header":
                continue
            name, hval = m.group(1).lower(), m.group(2).strip()
            if name == "cookie":
                rd.pairs(hval)
                saw_cookie = True
            elif name == "user-agent":
                out.user_agent = hval
            elif name == "accept":
                accept = hval.lower()
            elif name == "sec-fetch-dest":
                dest = hval.lower()
            elif name == "sec-fetch-mode":
                mode = hval.lower()
            elif _SECRET_HEADER.search(name):
                rd.note_secret(hval)
                other_auth = other_auth or name in ("authorization", "x-api-key", "x-auth-token")
        elif opt in ("-b", "--cookie"):
            if "=" in value:
                rd.pairs(value)
                saw_cookie = True
            else:
                cookie_file = True
        elif opt in ("-A", "--user-agent"):
            out.user_agent = value.strip()
        elif opt == "--url" and not url:
            url = value
        elif opt in ("-X", "--request"):
            method = value.strip().upper()
        elif opt in ("-u", "--user", "--oauth2-bearer"):
            other_auth = True
    out.format = "curl"
    if url.startswith(("http://", "https://")):
        out.request_url = url
        verb = method or ("POST" if has_body else "GET")
        out.request_is_page = verb == "GET" and not has_body and (
            "text/html" in accept or dest == "document" or mode == "navigate")
    if cookie_file:
        out.warnings.append("The cURL command reads cookies from a file (-b with a file name), which ARACHNIX "
                            "can't open. Paste the file's contents instead.")
    if other_auth:
        out.warnings.append("The cURL command also signs in with a header or user name (Authorization / -u). "
                            "ARACHNIX reads only cookies and the User-Agent from it; if the site needs that header, "
                            "add it under Sign-in > Custom header.")
    if not saw_cookie and not out.rows and not cookie_file:
        out.error = ("The cURL command has no cookies. In the Network tab, pick the page's own request (made while "
                     "you are signed in), then Copy as cURL again.")


def _ps_string(token: str) -> str:
    """A PowerShell string literal's value ("..." with backtick escapes, '...' with doubled quotes)."""
    token = token.strip()
    if token.startswith("'"):
        return token[1:-1].replace("''", "'")
    body = token[1:-1]
    body = re.sub(r"\$\(\[char\](\d+)\)", lambda m: chr(int(m.group(1)) % 0x110000), body)
    escapes = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}
    out: List[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "`" and i + 1 < len(body):
            out.append(escapes.get(body[i + 1], body[i + 1]))
            i += 2
        elif c == '"' and body[i + 1:i + 2] == '"':
            out.append('"')
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


_PS_STR = r"(\"(?:[^\"`]|`.|\"\")*\"|'(?:[^']|'')*')"
_PS_COOKIE = re.compile(r"New-Object\s+System\.Net\.Cookie\s*\(\s*" + r"\s*,\s*".join([_PS_STR] * 4) + r"\s*\)",
                        re.I | re.S)
_PS_UA = re.compile(r"(?:\$\w+\.UserAgent\s*=\s*|-UserAgent\s+)" + _PS_STR, re.I)
_PS_URI = re.compile(r"-Uri\s+" + _PS_STR, re.I)
_PS_HEADER = re.compile(_PS_STR + r"\s*=\s*" + _PS_STR)


def _read_powershell(text: str, out: CookiePaste, rd: _Reader):
    out.format = "powershell"
    for m in _PS_COOKIE.finditer(text):
        name, value, path, domain = (_ps_string(g) for g in m.groups())
        rd.keep({"name": name, "value": value, "path": path or "/", "domain": domain}, default_secure=rd.https)
    ua = _PS_UA.search(text)
    if ua:
        out.user_agent = _ps_string(ua.group(1))
    for m in _PS_HEADER.finditer(text):
        name, value = _ps_string(m.group(1)).lower(), _ps_string(m.group(2))
        if name == "cookie":
            rd.pairs(value)
        elif name == "user-agent" and not out.user_agent:
            out.user_agent = value
        elif _SECRET_HEADER.search(name):
            rd.note_secret(value)
    uri = _PS_URI.search(text)
    if uri:
        out.request_url = _ps_string(uri.group(1))
        verb = re.search(r"-Method\s+[\"']?(\w+)", text, re.I)
        out.request_is_page = (not verb or verb.group(1).upper() == "GET") and not re.search(r"-Body\b", text, re.I) \
            and bool(re.search(r"text/html|\"sec-fetch-dest\"\s*=\s*\"document\"", text, re.I))
    if not out.rows:
        out.error = ("The PowerShell snippet has no cookies. Copy it from the page's own request while you are "
                     "signed in, or use Copy as cURL instead.")


def parse_cookie_paste(raw: str, start_url: str, now: Optional[float] = None) -> CookiePaste:
    """Reads a paste in any of FORMATS for the site at `start_url`. Cookies without a domain belong to that
    site; cookies of other sites and expired ones are kept in `rows` (for the preview) but not in `cookies`.
    Warnings and the error are plain sentences without any cookie value."""
    out = CookiePaste()
    text = (raw or "").strip().lstrip("\ufeff")
    if not text:
        out.error = "Nothing was pasted."
        return out
    if len(text) > MAX_PASTE_CHARS:
        out.error = "The paste is too large to be a cookie export (over 2 MB). Copy only the cookies."
        return out
    rd = _Reader(start_url, out)
    fmt = detect_format(text)
    out.format = fmt
    if fmt == "curl":
        _read_curl(text, out, rd)
    elif fmt == "powershell":
        _read_powershell(text, out, rd)
    elif fmt == "json":
        _read_json(text, out, rd)
    elif fmt == "netscape":
        _read_netscape(text, out, rd)
    else:
        _read_lines(text, out, rd)
    rd.finish()
    _check(out, rd, time.time() if now is None else now)
    return out


def _check(out: CookiePaste, rd: _Reader, now: float):
    """Marks expired and other sites' cookies and adds the warnings / error."""
    expired: List[str] = []
    others: List[str] = []
    for row in out.rows:
        ck = row.cookie
        row.expired = bool(ck.get("expires")) and float(ck["expires"]) <= now
        row.other_site = bool(rd.base) and not _site_cookie(ck.get("domain") or "", rd.base)
        if row.other_site:
            d = (ck.get("domain") or "").lstrip(".")
            if d not in others:
                others.append(d)
        elif row.expired:
            expired.append(ck["name"])
    usable = [r for r in out.rows if r.usable]
    if others:
        n = sum(1 for r in out.rows if r.other_site)
        out.warnings.append(f"{n} cookie{'s' if n != 1 else ''} belong{'s' if n == 1 else ''} to another site "
                            f"({', '.join(others[:3])}{'...' if len(others) > 3 else ''}) and won't be sent.")
    if expired:
        names = ", ".join(sorted(set(expired))[:5])
        out.warnings.append(f"{len(expired)} cookie{'s have' if len(expired) != 1 else ' has'} expired ({names}). "
                            "Sign in again in your browser and copy fresh cookies.")
    if out.rejected:
        out.warnings.append(f"{out.rejected} cookie{'s were' if out.rejected != 1 else ' was'} left out: the name or "
                            "value has a line break or another character a web request can't carry.")
    if usable and not any(cookie_kind(r.cookie["name"]) == "signin" for r in usable) and not out.storage_origins():
        out.warnings.append(NO_SIGNIN_WARNING)
    if out.user_agent and (_CTRL_CHARS.search(out.user_agent) or len(out.user_agent) > 512):
        out.user_agent = ""
    if not usable and not out.storage_origins() and not out.error:
        if out.rows:
            out.error = ("None of the pasted cookies can be used for this site: "
                         + ("they have expired." if not others else "they belong to another site or have expired.")
                         + " Sign in again in your browser and copy fresh cookies.")
        else:
            out.error = ("No cookies could be read from the paste. Paste a Cookie header (name=value; name2=value2), "
                         "a JSON cookie export, a cookies.txt file, or a whole cURL command.")


def _iso(ts: Optional[float]) -> Optional[str]:
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def preview_cookies(raw: str, start_url: str) -> Dict[str, Any]:
    """The masked preview for POST /api/auth/cookies/preview: {format, count, usable, cookies: [{name, domain
    (".example.com": the domain and all its subdomains; "www.example.com": that host only), hosts (the hosts a
    host-only cookie is sent to: a cookie pasted without a domain also goes to the www / bare twin), path, secure,
    http_only, expires (ISO 8601 UTC or None for a session cookie), expired, other_site, kind, value: "***"}],
    user_agent, warnings, error, storage_origins}. Values are never included."""
    p = parse_cookie_paste(raw, start_url)
    rows = []
    for r in p.rows:
        ck = r.cookie
        rows.append({"name": ck["name"], "domain": ck.get("domain") or "",
                     "hosts": [ck.get("domain") or ""] + list(r.also) if ck.get("domain") else [],
                     "path": ck.get("path") or "/",
                     "secure": bool(ck.get("secure")), "http_only": bool(ck.get("httpOnly")),
                     "expires": _iso(ck.get("expires")), "expired": r.expired, "other_site": r.other_site,
                     "kind": cookie_kind(ck["name"]), "value": MASK})
    return {"format": p.format or "header", "count": len(rows), "usable": sum(1 for r in p.rows if r.usable), "cookies": rows,
            "user_agent": p.user_agent, "warnings": list(p.warnings), "error": p.error,
            "storage_origins": p.storage_origins()}
