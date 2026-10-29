"""
Search / AI bot readability check. A sample of crawled pages is fetched again as a browser (twice, to see
how much the page changes on its own) and as each crawler below, without cookies, and every answer is
compared with the browser's: HTTP status, CDN challenge pages, firewall blocks, where redirects end, a
noindex aimed at that crawler (meta or X-Robots-Tag), title, H1, canonical and the main text (3-word
shingle overlap). Googlebot smartphone is also compared with Googlebot desktop (mobile-first indexing)
and, when Chrome is available, the raw HTML with the rendered page (content that needs JavaScript).

These requests come from this server with a crawler's user agent. CDNs verify real crawlers by IP
address, so a block, a challenge or a clean pass here can differ from what the real crawler gets: the
report says "a request sending X's user agent", never "what X sees".

run(crawler, sample=10) -> report (stored as crawler.bot_report, served by GET /api/crawl/bots).
Idea from open-seo-crawler's bot readability check (MIT, see THIRD_PARTY_NOTICES.md); code is ours.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse

import requests
import urllib3

from ..config import USER_AGENTS
from ..url_utils import normalize_url
from .postcrawl import now_iso, status_report

CHROME_VERSION = "141.0.0.0"
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BROWSER_UA = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              f"Chrome/{CHROME_VERSION} Safari/537.36")


@dataclass(frozen=True)
class Agent:
    id: str
    label: str
    kind: str        # "baseline" | "search" | "ai" | "training"
    token: str       # robots.txt token and robots <meta name>, "" for the browser
    ua: str


def _modern(ua: str) -> str:
    return ua.replace("Chrome/124.0.0.0", f"Chrome/{CHROME_VERSION}")


AGENTS: Tuple[Agent, ...] = (
    Agent("browser", "Browser (Chrome)", "baseline", "", BROWSER_UA),
    Agent("googlebot_smartphone", "Googlebot smartphone", "search", "googlebot",
          _modern(USER_AGENTS["googlebot_smartphone"])),
    Agent("googlebot_desktop", "Googlebot desktop", "search", "googlebot", _modern(USER_AGENTS["googlebot_desktop"])),
    Agent("bingbot", "Bingbot", "search", "bingbot", _modern(USER_AGENTS["bingbot"])),
    Agent("applebot", "Applebot", "search", "applebot",
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/17.4 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)"),
    Agent("oai_searchbot", "OAI-SearchBot", "ai", "oai-searchbot",
          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; "
          "+https://openai.com/searchbot"),
    Agent("chatgpt_user", "ChatGPT-User", "ai", "chatgpt-user",
          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; "
          "+https://openai.com/bot"),
    Agent("perplexitybot", "PerplexityBot", "ai", "perplexitybot",
          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; "
          "+https://perplexity.ai/perplexitybot)"),
    Agent("gptbot", "GPTBot", "ai", "gptbot",
          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot"),
    Agent("claudebot", "ClaudeBot", "ai", "claudebot",
          "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"),
    Agent("ccbot", "CCBot", "training", "ccbot", "CCBot/2.0 (https://commoncrawl.org/faq/)"),
)
AGENT_BY_ID = {a.id: a for a in AGENTS}
KIND_LABEL = {"baseline": "Browser", "search": "Search engine", "ai": "AI assistant", "training": "AI training"}

BLOCK_STATUSES = (401, 403, 406, 429, 451, 503)
# Worst first: an agent's verdict over the sample is its worst page state
STATE_ORDER = ("blocked", "challenged", "noindex", "different", "redirected", "thinner", "error",
               "inconclusive", "same")
STATE_TEXT = {
    "blocked": "Blocked", "challenged": "Challenge page", "noindex": "noindex for this crawler",
    "different": "Different content", "redirected": "Redirected elsewhere", "thinner": "Thinner page",
    "error": "No answer", "inconclusive": "Cannot judge", "same": "Same as a browser",
}
THIN_SHARE = 0.7                 # a bot page with < 70 % of the browser's words is "thinner"
JS_SHARE = 0.5                   # raw HTML with < 50 % of the rendered words "needs JavaScript"
MAX_BODY = 3 * 1024 * 1024
MAX_WORDS = 5000

# Markers of challenge pages (a test a browser can pass) and firewall block pages, per provider
_CHALLENGE_MARKERS = (
    ("Cloudflare", ("cf-chl", "challenge-platform", "just a moment...", "_cf_chl_opt", "cf-turnstile")),
    ("DataDome", ("captcha-delivery.com", "datadome")),
    ("HUMAN (PerimeterX)", ("px-captcha", "perimeterx")),
    ("Imperva", ("_incapsula_resource", "incapsula incident")),
    ("AWS WAF", ("awswaf", "aws-waf-token")),
    ("Vercel", ("vercel security checkpoint",)),
    ("Sucuri", ("sucuri website firewall",)),
)
_BLOCK_MARKERS = (
    ("Cloudflare", ("attention required! | cloudflare", "error code: 1020", "cloudflare ray id")),
    ("Akamai", ("errors.edgesuite.net", "reference&#32;&#35;", "you don't have permission to access")),
    ("Wordfence", ("wordfence",)),
    ("Sucuri", ("sucuri", "access denied - sucuri")),
    ("Imperva", ("incapsula",)),
)


# ---------------------------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------------------------
def _section(url: str) -> str:
    try:
        segs = [s for s in urlparse(url).path.split("/") if s]
    except Exception:
        return ""
    return segs[0].lower() if segs else ""


def sample_urls(results: List[Any], start_url: str = "", n: int = 10) -> List[str]:
    """The start page, then one page per top-level section (shallowest first), then the shallowest
    pages left. Only indexable HTML pages that answered 200; one URL per distinct content."""
    from .analysis import indexability_of
    start = normalize_url(start_url) or start_url
    pages = []
    for r in results or []:
        seo = getattr(r, "seo", None) or {}
        if getattr(r, "status_code", None) != 200 or not seo.get("is_html"):
            continue
        try:
            ok, _ = indexability_of(r)
        except Exception:
            ok = False
        if ok:
            pages.append(r)
    pages.sort(key=lambda r: (0 if r.url == start else 1, int(getattr(r, "depth", 0) or 0), len(r.url)))
    chosen: List[str] = []
    sections: Set[str] = set()
    bodies: Set[str] = set()

    def take(r) -> bool:
        h = (getattr(r, "seo", None) or {}).get("content_hash") or ""
        if h and h in bodies:
            return False
        chosen.append(r.url)
        if h:
            bodies.add(h)
        return True

    for r in pages:
        if len(chosen) >= n:
            break
        sec = _section(r.url)
        if (not chosen or sec not in sections) and take(r):
            sections.add(sec)
    for r in pages:
        if len(chosen) >= n:
            break
        if r.url not in chosen:
            take(r)
    return chosen


# ---------------------------------------------------------------------------------------------
# Fetch and read
# ---------------------------------------------------------------------------------------------
def detect_block(status: Optional[int], headers: Dict[str, str], head: str) -> Tuple[str, str]:
    """("challenge" | "waf" | "", provider) from the status, headers and the first KB of the body."""
    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return "challenge", "Cloudflare"
    if status not in BLOCK_STATUSES:
        return "", ""
    low = (head or "").lower()
    for provider, marks in _CHALLENGE_MARKERS:
        if any(m in low for m in marks):
            return "challenge", provider
    for provider, marks in _BLOCK_MARKERS:
        if any(m in low for m in marks):
            return "waf", provider
    server = (headers.get("server") or "").lower()
    if "cloudflare" in server:
        return "waf", "Cloudflare"
    if "akamai" in server:
        return "waf", "Akamai"
    return "", ""


def fetch(url: str, ua: str, timeout: float = 15.0, verify: bool = False) -> Dict[str, Any]:
    """One cookie-less GET with a given user agent (redirects followed)."""
    headers = {"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    t0 = time.monotonic()
    try:
        with requests.get(url, headers=headers, timeout=(min(timeout, 10.0), timeout), allow_redirects=True,
                          verify=verify, stream=True) as resp:
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= MAX_BODY:
                    break
            body = b"".join(chunks)
            hdrs = {str(k).lower(): str(v) for k, v in resp.headers.items()}
            status, final, hops = resp.status_code, str(resp.url), len(resp.history)
            enc = resp.encoding if "charset" in (hdrs.get("content-type") or "").lower() else None
    except requests.RequestException as e:
        return {"error": f"{type(e).__name__}: {str(e)[:120]}", "ms": int((time.monotonic() - t0) * 1000)}
    ctype = (hdrs.get("content-type") or "").lower()
    try:
        html = body.decode(enc or "utf-8", errors="replace") if ("html" in ctype or not ctype) else ""
    except LookupError:
        html = body.decode("utf-8", errors="replace")
    kind, provider = detect_block(status, hdrs, html[:4000])
    return {"status": status, "final_url": final, "hops": hops, "ms": int((time.monotonic() - t0) * 1000),
            "bytes": len(body), "challenge": kind == "challenge", "waf": provider if kind else "",
            "_html": html, "_headers": hdrs}


def read_page(html: str, final_url: str, headers: Dict[str, str], token: str = "") -> Dict[str, Any]:
    """The fields compared between agents, from one response."""
    from ..page_parser import make_soup, tag_index
    from .extract import _el_text, _in_svg, _says_noindex, content_lines, x_robots_scopes
    out = {"title": "", "h1": "", "canonical": "", "noindex": False, "words": 0, "shingles": set()}
    xrt = x_robots_scopes([headers.get("x-robots-tag") or ""])
    noindex = _says_noindex(xrt.get("", [])) or (bool(token) and _says_noindex(xrt.get(token, [])))
    if not html:
        out["noindex"] = noindex
        return out
    try:
        soup = make_soup(html)
        idx = tag_index(soup)
    except Exception:
        out["noindex"] = noindex
        return out
    title = next((t for t in idx.tags("title") if not _in_svg(t)), None)
    out["title"] = _el_text(title) if title is not None else ""
    h1 = next(iter(idx.tags("h1")), None)
    out["h1"] = _el_text(h1) if h1 is not None else ""
    for lk in idx.tags("link"):
        rel = lk.get("rel") or []
        rels = [str(v).lower() for v in (rel if isinstance(rel, list) else str(rel).split())]
        if "canonical" in rels and lk.get("href"):
            try:
                out["canonical"] = normalize_url(urljoin(final_url, str(lk.get("href")).strip())) or ""
            except Exception:
                out["canonical"] = ""
            break
    names = {"robots"} | ({token} if token else set())
    metas = [str(m.get("content") or "") for m in idx.tags("meta")
             if str(m.get("name") or "").strip().lower() in names]
    out["noindex"] = noindex or _says_noindex(metas)
    body = next(iter(idx.tags("body")), None) or soup
    lines, _ = content_lines(body)
    words = " ".join(lines).split()
    low = [w.lower() for w in words[:MAX_WORDS]]
    out["words"] = len(words)
    out["shingles"] = {" ".join(low[i:i + 3]) for i in range(max(0, len(low) - 2))}
    return out


def similarity(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _same_url(a: str, b: str) -> bool:
    try:
        return (normalize_url(a) or a) == (normalize_url(b) or b)
    except Exception:
        return a == b


def compare(base: dict, base_read: dict, snap: dict, read: Optional[dict], selfsim: float) -> Dict[str, Any]:
    """One agent's answer against the browser's: state, HTTP status and a short detail."""
    if snap.get("error"):
        return {"state": "error", "status": None, "detail": snap["error"][:120]}
    status = snap.get("status")
    bstatus = base.get("status")
    if base.get("challenge") or base.get("waf") or bstatus != 200:
        return {"state": "inconclusive", "status": status,
                "detail": f"a browser request got HTTP {bstatus} too, so this cannot be compared"}
    if snap.get("challenge"):
        return {"state": "challenged", "status": status,
                "detail": f"{snap.get('waf') or 'CDN'} challenge page (HTTP {status})"}
    if snap.get("waf") or status in BLOCK_STATUSES:
        who = f", {snap['waf']}" if snap.get("waf") else ""
        return {"state": "blocked", "status": status, "detail": f"HTTP {status}{who}"}
    if status != bstatus:
        return {"state": "different", "status": status, "fields": ["status"],
                "detail": f"HTTP {status} (a browser got {bstatus})"}
    if not _same_url(snap.get("final_url") or "", base.get("final_url") or ""):
        return {"state": "redirected", "status": status, "detail": f"ends at {snap.get('final_url')}"}
    read = read or {}
    if read.get("noindex") and not base_read.get("noindex"):
        return {"state": "noindex", "status": status, "detail": "a noindex applies to this crawler"}
    fields = [f for f in ("title", "h1", "canonical") if (read.get(f) or "") != (base_read.get(f) or "")]
    sim = similarity(base_read["shingles"], read.get("shingles") or set())
    mine = read.get("shingles") or set()
    inside = len(base_read["shingles"] & mine) / len(mine) if mine else 1.0
    threshold = min(0.8, selfsim - 0.15)        # a page that changes by itself gets a looser threshold
    bwords, words = base_read["words"], read.get("words", 0)
    shorter = bwords >= 50 and words < THIN_SHARE * bwords
    out = {"status": status, "words": words, "sim": round(sim, 2)}
    if sim < threshold and bwords >= 20 and not (shorter and inside >= 0.8):
        fields.append("text")
    if fields:
        out.update(state="different", fields=fields,
                   detail="different " + ", ".join("H1" if f == "h1" else f for f in fields))
        return out
    if shorter:
        out.update(state="thinner", detail=f"{words} of {bwords} words")
        return out
    out.update(state="same", detail="same page as a browser")
    return out


# ---------------------------------------------------------------------------------------------
# Rendering (content that needs JavaScript), only for the sample
# ---------------------------------------------------------------------------------------------
def _render_all(crawler, urls: List[str], stop: threading.Event, timeout: float) -> Tuple[Dict[str, str], str]:
    """({url: rendered HTML}, note). A public (cookie-less) browser: the crawl's shared browser when the
    crawl was not logged in, else a fresh Chrome; nothing when Chrome is unavailable."""
    try:
        from ..browser import chrome_available, close_chrome, new_chrome
    except Exception:
        return {}, "Content that needs JavaScript was not measured: the browser module is unavailable."
    if not chrome_available():
        return {}, "Content that needs JavaScript was not measured: Chrome is not available on this server."
    out: Dict[str, str] = {}

    def render(driver):
        for u in urls:
            if stop.is_set():
                break
            try:
                driver.get(u)
                end = time.time() + min(timeout, 10)
                while time.time() < end:
                    if driver.execute_script("return document.readyState") == "complete":
                        break
                    time.sleep(0.25)
                time.sleep(1.0)
                out[u] = driver.page_source or ""
            except Exception:
                continue

    logged_in = bool(getattr(crawler, "session_state", None)) or bool(getattr(crawler, "auth_used", False))
    lease = getattr(crawler, "browser_lease", None)
    try:
        if not logged_in and lease is not None:
            with lease(timeout=30) as driver:
                if driver is None:
                    return {}, "Content that needs JavaScript was not measured: the browser was busy."
                render(driver)
        else:
            driver = new_chrome(BROWSER_UA, int(timeout), eager=True, wait=20)
            if driver is None:
                return {}, "Content that needs JavaScript was not measured: Chrome could not start."
            try:
                render(driver)
            finally:
                close_chrome(driver)
    except Exception as e:
        return out, f"Rendering stopped early ({type(e).__name__})."
    return out, ""


# ---------------------------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------------------------
def _robots_groups(crawler) -> Tuple[Optional[dict], str]:
    rep = getattr(crawler, "robots_audit", None)
    if not isinstance(rep, dict) or rep.get("status") != "done":
        return None, "robots.txt was not read, so its rules are not shown per crawler."
    if not rep.get("found"):
        return {}, ""
    from ..robots import parse_robots
    return parse_robots(rep.get("text") or ""), ""


def _worst(states: List[str]) -> str:
    for s in STATE_ORDER:
        if s in states:
            return s
    return "same"


def _headline(verdicts: Dict[str, dict], pages_checked: int) -> List[str]:
    """Plain-language summary lines, worst news first."""
    lines: List[str] = []
    by_state: Dict[str, List[str]] = {}
    for aid, v in verdicts.items():
        by_state.setdefault(v["state"], []).append(aid)

    def names(ids: List[str]) -> str:
        labels = [AGENT_BY_ID[i].label for i in ids]
        return ", ".join(labels[:-1]) + f" and {labels[-1]}" if len(labels) > 1 else labels[0]

    for state, many, one in (
            ("blocked", "are blocked", "is blocked"),
            ("challenged", "get a challenge page instead of the content", "gets a challenge page instead of the content"),
            ("noindex", "are told not to index the page", "is told not to index the page"),
            ("different", "get different content", "gets different content"),
            ("redirected", "are sent to a different URL", "is sent to a different URL"),
            ("thinner", "get less text", "gets less text")):
        ids = by_state.get(state) or []
        if not ids:
            continue
        counts = {i: verdicts[i]["counts"].get(state, 0) for i in ids}
        of = f"of {pages_checked} page{'s' if pages_checked != 1 else ''}"
        if len(set(counts.values())) == 1:
            lines.append(f"{names(ids)} {one if len(ids) == 1 else many} on {counts[ids[0]]} {of}.")
        else:
            parts = [f"{AGENT_BY_ID[i].label} on {n}" for i, n in sorted(counts.items(), key=lambda kv: -kv[1])]
            lines.append(f"{names(ids)} {many} ({', '.join(parts)} {of}).")
    if not lines and pages_checked:
        ok = by_state.get("same") or []
        if ok and len(ok) == len(verdicts):
            lines.append(f"Every crawler got the same {pages_checked} pages as a browser.")
        elif by_state.get("inconclusive"):
            lines.append("A browser request was refused too, so the crawlers could not be compared.")
    # robots.txt is separate from the server's answer: a crawler that obeys it never asks for these pages
    by_count: Dict[int, List[str]] = {}
    for aid, v in verdicts.items():
        if v.get("robots_disallowed"):
            by_count.setdefault(v["robots_disallowed"], []).append(aid)
    for n, ids in sorted(by_count.items(), key=lambda kv: -kv[0]):
        lines.append(f"robots.txt tells {names(ids)} not to crawl {n} of {pages_checked} "
                     f"page{'s' if pages_checked != 1 else ''}, so {'it' if len(ids) == 1 else 'they'} should not "
                     "read them.")
    return lines


def run(crawler, sample: int = 10, render: bool = True, stop_event: Optional[threading.Event] = None,
        delay: Optional[float] = None, on_progress: Optional[Callable[[str], None]] = None,
        urls: Optional[List[str]] = None) -> Dict[str, Any]:
    """Fetches `sample` pages (or the given `urls`) as a browser x2 and as every crawler in AGENTS,
    paced across 3 workers, and returns the report. Stops early when `stop_event` is set."""
    stop = stop_event or getattr(crawler, "_stop_event", None) or threading.Event()
    say = on_progress or (lambda msg: None)
    cfg = getattr(crawler, "config", None)
    timeout = min(float(getattr(cfg, "timeout", 15) or 15), 20.0)
    verify = bool(getattr(cfg, "verify_ssl", False))
    pace = max(0.25, float(delay if delay is not None else (getattr(cfg, "delay", 0) or 0)))
    results = list(getattr(crawler, "results_list", None) or [])
    urls = list(urls or sample_urls(results, getattr(crawler, "start_url", ""), max(1, min(int(sample), 50))))
    if not urls:
        return status_report("skipped", "no indexable HTML page answered 200 in this crawl")

    live = status_report("running", "", sample=urls, progress={"done": 0, "total": 0})
    try:
        crawler.bot_report = live      # the dashboard can show progress while it runs
    except Exception:
        pass
    jobs = [(u, a.id) for u in urls for a in AGENTS] + [(u, "browser_again") for u in urls]
    live["progress"]["total"] = len(jobs)
    say(f"Checking {len(urls)} pages as a browser and {len(AGENTS) - 1} crawlers")
    snaps: Dict[Tuple[str, str], dict] = {}
    lock = threading.Lock()
    next_at = [0.0]

    def work(job):
        if stop.is_set():
            return job, None
        with lock:
            now = time.monotonic()
            start = max(now, next_at[0])
            next_at[0] = start + pace
        if start > now:
            time.sleep(start - now)
        if stop.is_set():
            return job, None
        ua = BROWSER_UA if job[1] == "browser_again" else AGENT_BY_ID[job[1]].ua
        return job, fetch(job[0], ua, timeout, verify)

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="botcheck") as pool:
        for fut in as_completed([pool.submit(work, j) for j in jobs]):
            try:
                job, snap = fut.result()
            except Exception:
                continue
            if snap is not None:
                snaps[job] = snap
            live["progress"]["done"] += 1
    stopped = stop.is_set()

    groups, robots_note = _robots_groups(crawler)
    rendered: Dict[str, str] = {}
    notes: List[str] = ["These requests came from this server with each crawler's user agent. CDNs check real "
                        "crawlers by IP address, so the real crawler can be treated differently."]
    if robots_note:
        notes.append(robots_note)
    if render and not stopped:
        say("Rendering the sample in Chrome")
        rendered, note = _render_all(crawler, urls, stop, timeout)
        if note:
            notes.append(note)
    elif not render:
        notes.append("Content that needs JavaScript was not measured (rendering turned off).")

    pages: Dict[str, dict] = {}
    for u in urls:
        base = snaps.get((u, "browser"))
        if base is None or any((u, a.id) not in snaps for a in AGENTS):
            continue                                   # not fully checked (stopped)
        if base.get("error"):
            pages[u] = {"baseline": {"error": base["error"]}, "cells": {}, "issues": [], "robots": {},
                        "mobile": None, "js": None}
            continue
        base_read = read_page(base.get("_html", ""), base.get("final_url") or u, base.get("_headers") or {})
        again = snaps.get((u, "browser_again")) or {}
        selfsim = similarity(base_read["shingles"],
                             read_page(again.get("_html", ""), again.get("final_url") or u,
                                       again.get("_headers") or {})["shingles"]) if again.get("_html") else 1.0
        cells: Dict[str, dict] = {}
        reads: Dict[str, Tuple[dict, Optional[dict]]] = {}
        for a in AGENTS[1:]:
            snap = snaps[(u, a.id)]
            rd = None if snap.get("error") else read_page(snap.get("_html", ""), snap.get("final_url") or u,
                                                          snap.get("_headers") or {}, a.token)
            reads[a.id] = (snap, rd)
            cells[a.id] = compare(base, base_read, snap, rd, selfsim)
        issues: List[str] = []
        for a in AGENTS[1:]:
            c = cells[a.id]
            if c["state"] not in ("same", "inconclusive") and a.id != "googlebot_desktop":
                issues.append(f"{a.label}: {STATE_TEXT[c['state']].lower()} ({c.get('detail', '')})")
        robots: Dict[str, dict] = {}
        if groups is not None:
            from ..robots import decide
            p = urlparse(u)
            path = (p.path or "/") + (f"?{p.query}" if p.query else "")
            for a in AGENTS[1:]:
                ok, rule = decide(groups, a.token, path)
                robots[a.id] = {"allowed": ok, "rule": rule}
        # Mobile-first: Googlebot smartphone against Googlebot desktop
        mobile = None
        m_snap, m_read = reads.get("googlebot_smartphone", ({}, None))
        d_snap, d_read = reads.get("googlebot_desktop", ({}, None))
        if m_read and d_read and m_snap.get("status") == d_snap.get("status") == 200:
            diff = [f for f in ("title", "h1", "canonical") if (m_read[f] or "") != (d_read[f] or "")]
            if not _same_url(m_snap.get("final_url") or "", d_snap.get("final_url") or ""):
                diff.append("URL")
            mobile = {"mobile_words": m_read["words"], "desktop_words": d_read["words"], "fields": diff,
                      "differs": bool(diff) or (d_read["words"] >= 50 and m_read["words"] < THIN_SHARE * d_read["words"])}
            if mobile["differs"]:
                issues.append("Googlebot smartphone and desktop get different pages ("
                              + (", ".join("H1" if f == "h1" else f for f in diff) if diff else
                                 f"{m_read['words']} of {d_read['words']} words on mobile") + ")")
        js = None
        r_html = rendered.get(u)
        if r_html:
            r_read = read_page(r_html, u, {})
            raw_meta_noindex = read_page(base.get("_html", ""), u, {})["noindex"]
            js = {"raw_words": base_read["words"], "rendered_words": r_read["words"],
                  "needs_js": r_read["words"] >= 50 and base_read["words"] < JS_SHARE * r_read["words"],
                  "noindex_removed": raw_meta_noindex and not r_read["noindex"]}
            if js["needs_js"]:
                issues.append(f"Content needs JavaScript (crawlers that do not run it get {base_read['words']} "
                              f"of {r_read['words']} words)")
            if js["noindex_removed"]:
                issues.append("noindex in the HTML is removed by JavaScript (Google may not render a noindex "
                              "page, so it can stay out of the index)")
        pages[u] = {
            "baseline": {"status": base.get("status"), "final_url": base.get("final_url"), "words": base_read["words"],
                         "title": base_read["title"], "selfsim": round(selfsim, 2), "ms": base.get("ms"),
                         "challenge": bool(base.get("challenge")), "waf": base.get("waf") or ""},
            "cells": cells, "robots": robots, "mobile": mobile, "js": js, "issues": issues,
        }

    verdicts: Dict[str, dict] = {}
    for a in AGENTS[1:]:
        counts: Dict[str, int] = {}
        for pg in pages.values():
            st = (pg["cells"].get(a.id) or {}).get("state")
            if st:
                counts[st] = counts.get(st, 0) + 1
        verdicts[a.id] = {"state": _worst(list(counts)) if counts else "error", "counts": counts,
                          "checked": sum(counts.values()), "label": STATE_TEXT.get(_worst(list(counts)), ""),
                          # sampled pages robots.txt tells this crawler not to fetch (the answer above is the
                          # server's; a crawler that obeys robots.txt never asks)
                          "robots_disallowed": sum(1 for pg in pages.values()
                                                   if ((pg.get("robots") or {}).get(a.id) or {}).get("allowed") is False)}
    report: Dict[str, Any] = {
        "status": "stopped" if stopped else "done",
        "reason": "stopped before every page was checked" if stopped else "",
        "checked_at": now_iso(), "sample": urls, "pages_checked": len(pages),
        "agents": [{"id": a.id, "label": a.label, "kind": a.kind, "kind_label": KIND_LABEL[a.kind],
                    "token": a.token} for a in AGENTS],
        "pages": pages, "verdicts": verdicts, "rendered": bool(rendered), "notes": notes,
        "headline": _headline(verdicts, len(pages)),
        "requests": len(snaps),
    }
    if not pages:
        report["empty"] = True
    try:
        crawler.bot_report = report
    except Exception:
        pass
    return report
