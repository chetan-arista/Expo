"""
robots.txt rules, matched the way RFC 9309 and Google match them:
  - a group applies to a crawler when its User-agent product token equals the crawler's token, case-insensitively
    ("User-agent: Googlebot/2.1" is read as "googlebot"); a "User-agent: google" group does NOT apply to Googlebot.
    Groups that name the same token are merged. Without a group of its own a crawler obeys the "*" group (none =
    everything allowed). A few crawlers are documented to fall back to another crawler's group first
    (FALLBACK_TOKENS: Googlebot-Image / -News / -Video and Google-InspectionTool follow "googlebot", Applebot
    follows "googlebot" as Apple documents).
  - '*' and '$' wildcards; the longest matching rule decides, Allow wins a tie; /robots.txt itself is always allowed.
One robots.txt per host, fetched once with the crawl's session; a 429/503 answer is retried (honouring Retry-After,
at most ROBOTS_MAX_WAIT seconds per wait) before the crawl goes on without the file's rules. A custom robots.txt
(CrawlConfig.custom_robots_txt) replaces the live file of the crawled site's hosts for the whole crawl.

test(robots_txt, url, user_agent) is the robots.txt tester: {"allowed", "rule", "line", "group", "path"} with the
same decision the crawl makes.
"""
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

ROBOTS_RETRIES = 2       # extra tries after a 429 / 503 answer
ROBOTS_MAX_WAIT = 10.0   # seconds waited at most before each retry
MAX_ROBOTS_BYTES = 500 * 1024   # Google reads the first 500 KiB of a robots.txt and ignores the rest

Rule = Tuple[bool, str, "re.Pattern"]  # (allow, pattern as written, compiled)

# A crawler without a group of its own that obeys another crawler's group before "*" (Google documents
# Googlebot-Image / -News / -Video and Google-InspectionTool with the tokens "<own>, Googlebot"; Apple documents that
# Applebot follows the Googlebot instructions when robots.txt does not mention Applebot)
FALLBACK_TOKENS: Dict[str, str] = {
    "googlebot-image": "googlebot", "googlebot-news": "googlebot", "googlebot-video": "googlebot",
    "google-inspectiontool": "googlebot", "applebot": "googlebot",
}
_TOKEN = re.compile(r"[A-Za-z_-]+")


def _compile(pattern: str) -> "re.Pattern":
    anchored = pattern.endswith("$")
    body = re.escape(pattern[:-1] if anchored else pattern).replace(r"\*", ".*")
    return re.compile(body + ("$" if anchored else ""))


def agent_token(value: str) -> str:
    """The product token of a user-agent value, lowercased: its leading letters, '-' and '_' ("Googlebot/2.1" ->
    "googlebot"); "*" for the catch-all; "" when the value does not start with a token."""
    v = (value or "").strip()
    if v == "*" or (v.startswith("*") and (len(v) == 1 or v[1].isspace())):
        return "*"
    m = _TOKEN.match(v)
    return m.group(0).lower() if m else ""


def _parse(text: str, with_lines: bool = False) -> Dict[str, list]:
    """{token: rules}; rules are (allow, pattern, compiled) or, with_lines, (allow, pattern, compiled, line number)."""
    groups: Dict[str, list] = {}
    agents: List[str] = []
    in_rules = False
    for n, raw in enumerate((text or "").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        field, value = (part.strip() for part in line.split(":", 1))
        field = field.lower()
        if field == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            token = agent_token(value)
            if token and token not in agents:
                agents.append(token)
                groups.setdefault(token, [])
        elif field in ("allow", "disallow") and agents:
            in_rules = True
            if not value:
                continue  # "Disallow:" with no path allows everything
            rule = (field == "allow", value, _compile(value)) + ((n,) if with_lines else ())
            for agent in agents:
                groups[agent].append(rule)
    return groups


def parse_robots(text: str) -> Dict[str, List[Rule]]:
    """{user-agent product token (lowercase, or "*"): rules}. Consecutive User-agent lines share one group; groups
    naming the same token are merged."""
    return _parse(text)


def group_token(groups: Dict[str, list], agent: str, fallback: bool = True) -> str:
    """The group robots.txt applies to `agent`: the group of its exact product token, else (fallback) the group of
    the crawler it is documented to follow (FALLBACK_TOKENS), else "*", else "" (no group: everything allowed)."""
    token = agent_token(agent)
    if token and token != "*" and token in groups:
        return token
    other = FALLBACK_TOKENS.get(token) if fallback else None
    if other and other in groups:
        return other
    return "*" if "*" in groups else ""


def _best(rules: list, path: str):
    best = None
    for rule in rules:
        if rule[2].match(path):
            if best is None or len(rule[1]) > len(best[1]) or (len(rule[1]) == len(best[1]) and rule[0]):
                best = rule
    return best


def decide(groups: Dict[str, List[Rule]], agent: str, path: str) -> Tuple[bool, str]:
    """(allowed, the rule that decided or '') for a path such as '/shop?page=2'."""
    if path == "/robots.txt" or path.startswith("/robots.txt?"):
        return True, ""
    token = group_token(groups, agent)
    if not token:
        return True, ""
    best = _best(groups[token], path)
    if best is None:
        return True, ""
    return best[0], f"{'Allow' if best[0] else 'Disallow'}: {best[1]}"


def url_path(url: str) -> str:
    """The part of a URL robots.txt rules match: path (at least "/") plus "?query"."""
    p = urlparse(url or "")
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def test(robots_txt: str, url: str, user_agent: str) -> Dict[str, Any]:
    """The robots.txt tester: {"allowed", "rule" ("Disallow: /x" or ""), "line" (1-based line of that rule, None when
    no rule decided), "group" (the User-agent group used: a token, "*" or "" for none), "path"}. Uses the same parser
    and decision as the crawl (only the first 500 KB of the text count, as for Google)."""
    text = (robots_txt or "")[:MAX_ROBOTS_BYTES]
    raw = (url or "").strip()
    path = url_path(raw) if "://" in raw else (raw if raw.startswith("/") else "/" + raw)
    groups = _parse(text, with_lines=True)
    token = group_token(groups, user_agent)
    out: Dict[str, Any] = {"allowed": True, "rule": "", "line": None, "group": token, "path": path}
    if path == "/robots.txt" or path.startswith("/robots.txt?") or not token:
        return out
    best = _best(groups[token], path)
    if best is not None:
        out.update(allowed=best[0], rule=f"{'Allow' if best[0] else 'Disallow'}: {best[1]}", line=best[3])
    return out


class RobotsPolicy:
    def __init__(self, session, agent: str = "Googlebot", timeout: int = 10, verify_ssl: bool = False,
                 logger: Optional[Callable[[str], None]] = None, custom_text: Optional[str] = None,
                 custom_for: Optional[Callable[[str], bool]] = None):
        self.session = session
        self.agent = agent
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.logger = logger or (lambda msg: None)
        # Custom robots.txt: replaces the live file of every origin custom_for(origin) accepts (the crawled site)
        self.custom_text = custom_text if custom_text else None
        self.custom_for = custom_for
        self._groups: Dict[str, Dict[str, List[Rule]]] = {}
        # origin -> {"status": HTTP status or None, "error": str, "rules": int, "tries": int, "custom": bool}
        self.fetch_info: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._origin_locks: Dict[str, threading.Lock] = {}  # one fetch per host; hosts load in parallel

    def _uses_custom(self, origin: str) -> bool:
        if self.custom_text is None:
            return False
        if self.custom_for is None:
            return True
        try:
            return bool(self.custom_for(origin + "/"))
        except Exception:
            return False

    def _rules_for(self, origin: str) -> Dict[str, List[Rule]]:
        with self._lock:
            if origin in self._groups:
                return self._groups[origin]
            origin_lock = self._origin_locks.setdefault(origin, threading.Lock())
        with origin_lock:
            if origin in self._groups:
                return self._groups[origin]
            groups: Dict[str, List[Rule]] = {}
            info: Dict[str, Any] = {"status": None, "error": "", "rules": 0, "tries": 0, "custom": False}
            if self._uses_custom(origin):
                groups = parse_robots(self.custom_text[:MAX_ROBOTS_BYTES])
                info.update(custom=True, rules=sum(len(v) for v in groups.values()))
                self.logger(f"[robots.txt] Using the custom robots.txt for {origin} ({info['rules']} rules); the "
                            "live file is not read.")
                self.fetch_info[origin] = info
                self._groups[origin] = groups
                return groups
            try:
                r = self._fetch(f"{origin}/robots.txt", info)
                info["status"] = r.status_code
                if 200 <= r.status_code < 300:
                    groups = parse_robots(r.text)
                    info["rules"] = sum(len(v) for v in groups.values())
                    self.logger(f"[robots.txt] Loaded {origin}/robots.txt ({info['rules']} rules)")
                elif r.status_code >= 500 or r.status_code == 429:
                    self.logger(f"[robots.txt] {origin}/robots.txt returned {r.status_code}; crawling without its rules")
                elif r.status_code in (401, 403):
                    self.logger(f"[robots.txt] {origin}/robots.txt returned {r.status_code} (access refused, "
                                "possibly bot protection); crawling without its rules")
            except Exception as e:
                info["error"] = str(e)[:200]
                self.logger(f"[robots.txt] Could not fetch {origin}/robots.txt ({e}); crawling without its rules")
            self.fetch_info[origin] = info
            self._groups[origin] = groups
            return groups

    def _fetch(self, url: str, info: Dict[str, Any]):
        """GET robots.txt; a 429 / 503 answer is tried again after its Retry-After (capped)."""
        from .net_guard import retry_after_seconds
        for attempt in range(ROBOTS_RETRIES + 1):
            info["tries"] = attempt + 1
            r = self.session.get(url, timeout=self.timeout, verify=self.verify_ssl)
            if r.status_code not in (429, 503) or attempt == ROBOTS_RETRIES:
                return r
            wait = retry_after_seconds(r.headers.get("retry-after"))
            wait = min(ROBOTS_MAX_WAIT, wait if wait is not None else 2.0 ** attempt)
            self.logger(f"[robots.txt] {url} answered {r.status_code}; trying again in {wait:g}s")
            r.close()
            time.sleep(wait)
        return r

    def check(self, url: str) -> Tuple[bool, str]:
        """(allowed, deciding rule) for `url` under its host's robots.txt."""
        p = urlparse(url)
        return decide(self._rules_for(f"{p.scheme}://{p.netloc}"), self.agent, url_path(url))
