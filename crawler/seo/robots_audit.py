"""
robots.txt read for search engines and AI crawlers: which crawlers it blocks from the whole site (or
from parts of it), Cloudflare-style Content-Signal lines, and Sitemap lines. Matching reuses the
RFC 9309 / Google rules in crawler/robots.py (the group of the crawler's exact product token, else "*"; longest rule
wins). When the crawl obeyed a custom robots.txt (CrawlConfig.custom_robots_txt, P3) that text is audited instead of
the live file and the report says so (rep["custom"], CUSTOM_NOTE).

run(crawler) fetches robots.txt once and returns the report (stored as crawler.robots_audit);
audit(text, ...) is the pure part. Page-level AI meaning of robots directives (data-nosnippet,
max-snippet, unavailable_after, noindex aimed at one crawler) is read from PageData by tabs/robots_txt.py
with the helpers at the end of this module.

Applebot follows the Googlebot group when robots.txt has no group of its own for it (Apple documents this),
so effective_agent() decides Applebot's rules with Googlebot's group in that case. The user-requested agents
(ChatGPT-User, Claude-User, Perplexity-User) fetch a page when a person asks the assistant about it; their
operators say they may not follow robots.txt, so a block is reported but may not be honoured.

Idea from open-seo-crawler's robots.txt AI audit (MIT, see THIRD_PARTY_NOTICES.md); the bot additions and the
Applebot rule follow siteone-crawler's agent table (MIT).

llms.txt (Build 4, P19): the same post-crawl step fetches /llms.txt and /llms-full.txt once each (at most 1 MB
read), and records whether each file is there, missing (404 / 410), served as a web page, sent with the wrong
type or too big, its Markdown outline (an H1 title, a "> " summary, "## " sections with "- [name](url)" link
lists) and every link in it (rep["llms_txt"]). llms_summary(site) then judges each link against the crawl: a
page of the crawl that answered 200 and is indexable is fine; a page that answered otherwise (or is noindex,
canonicalised, blocked) is a broken link; a URL the crawl did not request is "Not checked", never fine. A file
that could not be read (server error, refused, no answer) is "Not checked", never "missing".
"""
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from ..robots import decide, parse_robots
from ..robots import group_token as robots_group
from .postcrawl import now_iso, status_report

MAX_ROBOTS_BYTES = 500 * 1024      # Google reads the first 500 KiB of a robots.txt and ignores the rest
KEEP_TEXT_BYTES = 200 * 1024       # robots.txt text kept in the report (for page-level matching)
SITE_WIDE_SHARE = 0.9              # a crawler blocked from 90 %+ of the sampled pages counts as blocked site-wide
MAX_PATHS = 300                    # crawled paths sampled for the site-wide / partial decision
MAX_SNIPPET_MIN = 160              # max-snippet below this cuts the text Google and its AI features may show
CUSTOM_NOTE = "Custom robots.txt used for this crawl"   # the crawl obeyed CrawlConfig.custom_robots_txt (P3)


@dataclass(frozen=True)
class Bot:
    token: str          # robots.txt user-agent token (lowercase)
    label: str          # as the operator writes it
    kind: str           # "search" | "ai" (AI answers / assistants) | "training" (model training only)
    owner: str
    effect: str         # what blocking it costs, in plain words


BOTS: Tuple[Bot, ...] = (
    Bot("googlebot", "Googlebot", "search", "Google", "pages drop out of Google Search"),
    Bot("bingbot", "Bingbot", "search", "Microsoft", "pages drop out of Bing, Copilot and the engines Bing feeds"),
    Bot("applebot", "Applebot", "search", "Apple", "pages drop out of Siri and Spotlight suggestions"),
    Bot("gptbot", "GPTBot", "ai", "OpenAI", "OpenAI models cannot learn from the site"),
    Bot("oai-searchbot", "OAI-SearchBot", "ai", "OpenAI", "pages are left out of ChatGPT search answers"),
    Bot("claudebot", "ClaudeBot", "ai", "Anthropic", "Anthropic's Claude models cannot learn from the site"),
    Bot("claude-searchbot", "Claude-SearchBot", "ai", "Anthropic", "pages are left out of Claude's search answers"),
    Bot("perplexitybot", "PerplexityBot", "ai", "Perplexity", "pages are left out of Perplexity answers"),
    Bot("chatgpt-user", "ChatGPT-User", "ai", "OpenAI",
        "ChatGPT cannot open the pages when a user asks about them (user-requested visits)"),
    Bot("claude-user", "Claude-User", "ai", "Anthropic",
        "Claude cannot open the pages when a user asks about them (user-requested visits)"),
    Bot("perplexity-user", "Perplexity-User", "ai", "Perplexity",
        "Perplexity cannot open the pages when a user asks about them (user-requested visits)"),
    Bot("google-extended", "Google-Extended", "ai", "Google", "Gemini apps and grounding may not use the site"),
    Bot("applebot-extended", "Applebot-Extended", "ai", "Apple", "Apple Intelligence models may not use the site"),
    Bot("ccbot", "CCBot", "training", "Common Crawl", "the site is left out of the open Common Crawl dataset"),
    Bot("bytespider", "Bytespider", "training", "ByteDance", "ByteDance models cannot learn from the site"),
    Bot("meta-externalagent", "meta-externalagent", "training", "Meta",
        "Meta's AI models and products cannot learn from or index the site"),
)
# A crawler with no robots.txt group of its own that follows another crawler's group (Apple: "If robots
# instructions don't mention Applebot but do mention Googlebot, Applebot follows the Googlebot instructions")
FALLBACK_GROUP = {"applebot": "googlebot"}
KIND_LABEL = {"search": "Search engine", "ai": "AI assistant", "training": "AI training only"}


def bot(token: str) -> Optional[Bot]:
    token = (token or "").lower()
    return next((b for b in BOTS if b.token == token), None)


# ---------------------------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------------------------
def group_for(groups: Dict[str, list], agent: str) -> str:
    """The user-agent group robots.txt gives `agent` itself (exact product token, case-insensitive, as RFC 9309 and
    Google match it), else "*", else "" (no group: all allowed). A crawler's documented fallback to another
    crawler's group (Applebot -> Googlebot) is applied by effective_agent(), so the report can say so."""
    return robots_group(groups, agent, fallback=False)


def effective_agent(groups: Dict[str, list], token: str) -> str:
    """The user-agent name whose robots.txt group decides for `token`: the token itself, or the crawler it
    falls back to (Applebot -> Googlebot) when it has no specific group and that crawler has one."""
    token = (token or "").lower()
    other = FALLBACK_GROUP.get(token)
    if other and group_for(groups, token) in ("*", "") and group_for(groups, other) not in ("*", ""):
        return other
    return token


def raw_groups(text: str) -> List[Dict[str, Any]]:
    """[{"agents": [...], "allow": n, "disallow": n}] in file order, for display."""
    out: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    in_rules = False
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (p.strip() for p in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if current is None or in_rules:
                current = {"agents": [], "allow": 0, "disallow": 0}
                out.append(current)
                in_rules = False
            current["agents"].append(value)
        elif key in ("allow", "disallow") and current is not None:
            in_rules = True
            if value:
                current[key] += 1
    return out


def content_signals(text: str) -> Tuple[List[str], Dict[str, str]]:
    """(the Content-Signal lines as written, merged {signal: "yes"|"no"}), e.g. ai-train=no."""
    lines: List[str] = []
    merged: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line.lower().startswith("content-signal"):
            continue
        if ":" not in line:
            continue
        lines.append(line[:200])
        for part in line.split(":", 1)[1].split(","):
            if "=" in part:
                k, v = (s.strip().lower() for s in part.split("=", 1))
                if k:
                    merged[k] = v
    return lines, merged


def sitemap_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line.lower().startswith("sitemap:"):
            url = line.split(":", 1)[1].strip()
            if url and url not in out:
                out.append(url)
    return out


def _path_of(url: str) -> str:
    try:
        p = urlparse(url)
    except Exception:
        return "/"
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def bot_policy(groups: Dict[str, list], b: Bot, paths: List[str]) -> Dict[str, Any]:
    """How robots.txt treats one crawler: the group used, root decision, share of sampled paths blocked."""
    agent = effective_agent(groups, b.token)
    root_ok, root_rule = decide(groups, agent, "/")
    blocked = [p for p in paths if not decide(groups, agent, p)[0]]
    share = len(blocked) / len(paths) if paths else (0.0 if root_ok else 1.0)
    site_wide = (not root_ok) or (bool(paths) and share >= SITE_WIDE_SHARE)
    group = group_for(groups, agent) or "(none)"
    if agent != b.token:
        group = f"{group} (follows Googlebot's rules)"
    return {
        "token": b.token, "label": b.label, "kind": b.kind, "owner": b.owner, "effect": b.effect,
        "group": group,
        "root_allowed": root_ok, "rule": root_rule,
        "blocked_share": round(share * 100, 1),
        "state": "blocked" if site_wide else ("partial" if blocked else "allowed"),
        "example_blocked": blocked[:3],
    }


# ---------------------------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------------------------
def _issue(rid: str, issue_type: str, priority: str, text: str, bots: Iterable[str] = ()) -> Dict[str, Any]:
    return {"id": rid, "issue_type": issue_type, "priority": priority, "text": text, "bots": list(bots)}


def _names(items: List[str]) -> str:
    return ", ".join(items[:-1]) + f" and {items[-1]}" if len(items) > 1 else (items[0] if items else "")


def audit(text: str, http_status: Optional[int], url: str, content_type: str = "", error: str = "",
          paths: Optional[List[str]] = None, size_bytes: Optional[int] = None) -> Dict[str, Any]:
    """The robots.txt report from its text and fetch result. `paths` are crawled page paths (with query)
    used to tell a crawler blocked from the whole site from one blocked from some sections."""
    text = text or ""
    size = len(text.encode("utf-8", errors="ignore")) if size_bytes is None else int(size_bytes)
    rep: Dict[str, Any] = {
        "status": "done", "reason": "", "checked_at": now_iso(), "url": url, "http_status": http_status,
        "content_type": (content_type or "").lower(), "error": error, "found": False, "size_bytes": size,
        "too_big": size > MAX_ROBOTS_BYTES, "groups": [], "bots": [], "search_blocked": [], "ai_blocked": [],
        "ai_partial": [], "training_blocked": [], "content_signal_lines": [], "content_signals": {},
        "sitemaps": [], "issues": [], "text": "",
    }
    issues = rep["issues"]
    if error or http_status is None:
        rep["reason"] = error or "no response"
        issues.append(_issue("unreachable", "Issue", "High",
                             "robots.txt could not be loaded (" + (error or "no response") + "). Google treats an "
                             "unreachable robots.txt like a server error and may stop crawling the site."))
        return rep
    if http_status >= 500:
        issues.append(_issue("server_error", "Issue", "High",
                             f"robots.txt answers with a server error (HTTP {http_status}). Google then stops "
                             "crawling the whole site until robots.txt loads again."))
        return rep
    if http_status >= 400:
        issues.append(_issue("missing", "Info", "Low",
                             f"No robots.txt (HTTP {http_status}): every crawler may crawl everything. That is "
                             "allowed, but you cannot point crawlers at your XML sitemap or keep them out of "
                             "cart, search and filter pages."))
        return rep
    rep["found"] = True
    if "html" in rep["content_type"] and "<html" in text[:2000].lower():
        issues.append(_issue("html", "Warning", "High",
                             "robots.txt returns a web page instead of plain text, so crawlers find no valid "
                             "rules in it (often a 'page not found' page served with status 200)."))
        text = ""
    rep["text"] = text[:KEEP_TEXT_BYTES]
    if rep["too_big"]:
        issues.append(_issue("too_big", "Warning", "Medium",
                             f"robots.txt is {size // 1024} KB. Google reads only the first 500 KB and "
                             "ignores every rule after that."))
    groups = parse_robots(text[:MAX_ROBOTS_BYTES])
    rep["groups"] = raw_groups(text[:MAX_ROBOTS_BYTES])[:100]
    sample = list(dict.fromkeys(p for p in (paths or []) if p))[:MAX_PATHS]
    for b in BOTS:
        pol = bot_policy(groups, b, sample)
        rep["bots"].append(pol)
        if pol["state"] == "blocked":
            {"search": rep["search_blocked"], "ai": rep["ai_blocked"],
             "training": rep["training_blocked"]}[b.kind].append(b.label)
        elif pol["state"] == "partial" and b.kind == "ai":
            rep["ai_partial"].append(b.label)

    rep["content_signal_lines"], rep["content_signals"] = content_signals(text)
    rep["sitemaps"] = sitemap_lines(text)[:50]
    sig = rep["content_signals"]

    if rep["search_blocked"]:
        issues.append(_issue("search_blocked", "Issue", "High",
                             f"robots.txt blocks {_names(rep['search_blocked'])} from the whole site, so its "
                             "pages can drop out of those search engines.", rep["search_blocked"]))
    if rep["ai_blocked"] or sig.get("ai-input") == "no":
        parts = []
        if rep["ai_blocked"]:
            effects = {b.label: b.effect for b in BOTS}
            parts.append("robots.txt blocks these AI crawlers from the whole site: "
                         + "; ".join(f"{label} ({effects.get(label, '')})" for label in rep["ai_blocked"]))
        if sig.get("ai-input") == "no":
            parts.append("a Content-Signal line says ai-input=no (do not use the pages in AI answers)")
        issues.append(_issue("ai_blocked", "Issue", "Medium", "; ".join(parts) + ".", rep["ai_blocked"]))
    if rep["training_blocked"] or sig.get("ai-train") == "no":
        who = _names(rep["training_blocked"]) if rep["training_blocked"] else "AI training (Content-Signal ai-train=no)"
        issues.append(_issue("training_blocked", "Info", "Low",
                             f"Opted out of AI training: {who}. This only keeps the site out of model training "
                             "data; search results and AI answers are not affected.", rep["training_blocked"]))
    if rep["ai_partial"]:
        issues.append(_issue("ai_partial", "Warning", "Medium",
                             f"robots.txt keeps {_names(rep['ai_partial'])} out of some sections of the site "
                             "(see the pages listed under 'Disallowed For AI Crawlers' in the Robots.txt tab).", rep["ai_partial"]))
    if not rep["sitemaps"]:
        issues.append(_issue("no_sitemap", "Opportunity", "Low",
                             "robots.txt has no 'Sitemap:' line. Adding one lets every crawler find your XML "
                             "sitemap, not only the search engines you submitted it to."))
    return rep


def run(crawler, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Fetches the site's robots.txt with the crawl's session and audits it (crawler.robots_audit), then checks
    /llms.txt and /llms-full.txt (rep["llms_txt"])."""
    origin = site_origin(crawler)
    if not origin:
        return status_report("skipped", "the start URL has no host")
    url = origin + "/robots.txt"
    cfg = getattr(crawler, "config", None)
    session = getattr(crawler, "http_session", None) or getattr(crawler, "session", None)
    paths = crawled_paths(crawler)
    if session is None:
        import requests
        session = requests.Session()
    t = timeout or min(float(getattr(cfg, "timeout", 15) or 15), 15.0)
    verify = bool(getattr(cfg, "verify_ssl", False))
    custom = getattr(cfg, "custom_robots_txt", "") or ""
    rep: Optional[Dict[str, Any]] = None
    resp = None
    if isinstance(custom, str) and custom.strip():
        # The crawl obeyed a custom robots.txt instead of the live file (P3): audit what the crawl used, and say so
        rep = audit(custom.lstrip("\ufeff"), 200, url, "text/plain", paths=paths,
                    size_bytes=len(custom.encode("utf-8", errors="ignore")))
        rep["custom"] = True
        rep["custom_note"] = CUSTOM_NOTE
    else:
        try:
            resp = session.get(url, timeout=(min(t, 10.0), t), allow_redirects=True, verify=verify)
        except Exception as e:
            rep = audit("", None, url, error=f"{type(e).__name__}: {str(e)[:120]}", paths=paths)
    if resp is not None:
        body = resp.content or b""
        try:
            text = body[:MAX_ROBOTS_BYTES + 1024].decode(resp.encoding or "utf-8", errors="replace")
        except (LookupError, TypeError):
            text = body[:MAX_ROBOTS_BYTES + 1024].decode("utf-8", errors="replace")
        rep = audit(text.lstrip("\ufeff"), resp.status_code, url, resp.headers.get("content-type", ""),
                    paths=paths, size_bytes=len(body))
        final = str(getattr(resp, "url", "") or url)
        if final != url:
            rep["final_url"] = final
    try:
        rep["llms_txt"] = llms_check(session, origin, t, verify)
    except Exception as e:      # the robots.txt audit stands on its own
        rep["llms_txt"] = {"checked_at": now_iso(), "origin": origin, "files": [],
                           "error": f"{type(e).__name__}: {str(e)[:120]}"}
    return rep


# ---------------------------------------------------------------------------------------------
# llms.txt / llms-full.txt (P19)
# ---------------------------------------------------------------------------------------------
LLMS_FILES = ("/llms.txt", "/llms-full.txt")
LLMS_MAX_BYTES = 1024 * 1024          # each file is read up to 1 MB
LLMS_MAX_LINKS = 2000                 # links kept per file
LLMS_TYPES = ("text/plain", "text/markdown", "text/x-markdown")
LLMS_MISSING = (404, 410)
_MD_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^\"'\n]*[\"'])?\s*\)")
_AUTOLINK = re.compile(r"<(https?://[^>\s]+)>", re.I)
_LIST_ITEM = re.compile(r"^\s*[-*+]\s+")


def _short_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _looks_html(text: str, content_type: str) -> bool:
    if "html" in _short_type(content_type):
        return True
    head = (text or "")[:2048].lstrip("\ufeff \t\r\n").lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def parse_llms(text: str, base_url: str, full: bool = False) -> Dict[str, Any]:
    """The Markdown outline of an llms.txt file: {"title", "summary", "h1_count", "sections": [names],
    "links": [{"text", "url", "section", "in_list"}] (<= LLMS_MAX_LINKS), "links_total", "list_links",
    "problems": [plain sentences that break the format], "notes": [optional parts that are missing]}.
    `full`: llms-full.txt, whose body is free-form text, so only its links are read."""
    from urllib.parse import urljoin, urldefrag
    lines = (text or "").lstrip("\ufeff").splitlines()
    out: Dict[str, Any] = {"title": "", "summary": "", "h1_count": 0, "sections": [], "links": [], "links_total": 0,
                           "list_links": 0, "problems": [], "notes": []}
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    if first.startswith("# "):
        out["title"] = first[2:].strip()[:300]
    section, in_fence, seen_section = "", False, False
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if line.startswith("# "):
            out["h1_count"] += 1
        elif line.startswith("## "):
            section = line[3:].strip()[:200]
            seen_section = True
            if len(out["sections"]) < 200:
                out["sections"].append(section)
        elif line.startswith(">") and not out["summary"] and not seen_section:
            out["summary"] = line.lstrip(">").strip()[:500]
        found = [m.group(2) for m in _MD_LINK.finditer(line)] + [m.group(1) for m in _AUTOLINK.finditer(line)]
        in_list = bool(_LIST_ITEM.match(raw))
        for href in found:
            try:
                absolute = urldefrag(urljoin(base_url, href.strip()))[0]
            except Exception:
                continue
            if not absolute.lower().startswith(("http://", "https://")):
                continue
            out["links_total"] += 1
            if in_list:
                out["list_links"] += 1
            if len(out["links"]) < LLMS_MAX_LINKS:
                text_m = next((m.group(1) for m in _MD_LINK.finditer(line) if m.group(2) == href), "")
                out["links"].append({"text": text_m.strip()[:200], "url": absolute, "section": section,
                                     "in_list": in_list})
    if full:
        return out
    if not out["title"]:
        out["problems"].append("It does not start with a '# Name' heading: the llms.txt format opens with one H1 "
                               "naming the site or project.")
    if out["h1_count"] > 1:
        out["notes"].append(f"It has {out['h1_count']} H1 headings; the format expects one, at the top.")
    if not out["summary"]:
        out["notes"].append("No '> summary' line under the title (optional, but it tells AI tools what the site "
                            "is about in one sentence).")
    if not out["list_links"]:
        out["notes"].append("No link lists ('## Section' headings with '- [name](url): notes' lines), so AI tools "
                            "get no pages to read.")
    return out


def _fetch_llms(session, url: str, timeout: float, verify: bool) -> Dict[str, Any]:
    """One GET of an llms file (redirects followed, body read up to LLMS_MAX_BYTES + 1)."""
    entry: Dict[str, Any] = {"url": url, "final_url": url, "http_status": None, "content_type": "", "error": "",
                             "size_bytes": 0, "too_big": False, "text": ""}
    try:
        with session.get(url, timeout=(min(timeout, 10.0), timeout), allow_redirects=True, verify=verify,
                         stream=True) as resp:
            entry["http_status"] = resp.status_code
            entry["final_url"] = str(getattr(resp, "url", "") or url)
            entry["content_type"] = str(resp.headers.get("content-type", "") or "")
            if 200 <= resp.status_code < 300:
                data = bytearray()
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > LLMS_MAX_BYTES:
                        break
                entry["too_big"] = len(data) > LLMS_MAX_BYTES
                length = str(resp.headers.get("content-length", "") or "").strip()
                entry["size_bytes"] = int(length) if length.isdigit() and entry["too_big"] else len(data)
                body = bytes(data[:LLMS_MAX_BYTES])
                try:
                    entry["text"] = body.decode(resp.encoding or "utf-8", errors="replace")
                except LookupError:
                    entry["text"] = body.decode("utf-8", errors="replace")
    except Exception as e:
        entry["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    return entry


def _llms_state(entry: Dict[str, Any], path: str) -> Tuple[str, str]:
    """("found" | "missing" | "html" | "not_checked", plain reason) of one fetched llms file."""
    code = entry.get("http_status")
    name = path.lstrip("/")
    if code is None:
        return "not_checked", (f"{name} could not be loaded ({entry.get('error') or 'no response'}), so it was not "
                               "checked.")
    if code in LLMS_MISSING:
        return "missing", f"{name} answers HTTP {code}: there is no {name} on this site."
    if code >= 500:
        return "not_checked", f"{name} answered HTTP {code} (server error), so it could not be read."
    if code == 429:
        return "not_checked", f"{name} answered HTTP 429 (too many requests), so it could not be read."
    if code >= 400:
        return "not_checked", (f"{name} answered HTTP {code} (access refused, possibly bot protection), so it could "
                               "not be read.")
    if not 200 <= code < 300:
        return "not_checked", f"{name} answered HTTP {code}, so it could not be read."
    if _looks_html(entry.get("text") or "", entry.get("content_type") or ""):
        return "html", (f"{name} answers with a web page (HTML) instead of a Markdown text file, so AI tools find "
                        "no usable content in it (often a 'page not found' page sent with status 200).")
    return "found", ""


def llms_check(session, origin: str, timeout: float = 15.0, verify: bool = False) -> Dict[str, Any]:
    """Fetches /llms.txt and /llms-full.txt of the site once and reads them: {"checked_at", "origin", "files":
    [{"path", "url", "final_url", "http_status", "content_type", "state", "reason", "size_bytes", "too_big",
    "title", "summary", "sections", "links", "links_total", "list_links", "problems", "notes"}]}."""
    files = []
    for path in LLMS_FILES:
        fetched = _fetch_llms(session, origin + path, timeout, verify)
        state, reason = _llms_state(fetched, path)
        entry = {k: fetched[k] for k in ("url", "final_url", "http_status", "content_type", "size_bytes", "too_big")}
        entry.update(path=path, state=state, reason=reason, title="", summary="", sections=[], links=[],
                     links_total=0, list_links=0, problems=[], notes=[], h1_count=0)
        if state == "found":
            full = path == "/llms-full.txt"
            entry.update(parse_llms(fetched.get("text") or "", fetched["final_url"], full=full))
            ctype = _short_type(fetched.get("content_type") or "")
            if not ctype:
                entry["problems"].append("It is sent without a Content-Type header; use text/plain or text/markdown "
                                         "so tools read it as text.")
            elif ctype not in LLMS_TYPES:
                entry["problems"].append(f"It is sent as '{ctype}' instead of text/plain or text/markdown, so tools "
                                         "may not read it as text.")
            if fetched.get("too_big"):
                (entry["notes"] if full else entry["problems"]).append(
                    "It is over 1 MB; only the first 1 MB was read" + ("." if full else
                                                                      ": keep llms.txt short and put the full text in "
                                                                      "llms-full.txt."))
        files.append(entry)
    return {"checked_at": now_iso(), "origin": origin, "files": files}


def _link_verdict(site, url: str) -> Tuple[str, str]:
    """("ok" | "broken" | "not_checked", reason) of one llms.txt link, judged against the crawl."""
    from .tabs.canonicals import find_result
    from .tabs.internal import category_of, first_status_code, is_external
    t = find_result(site, url)
    if t is None:
        return "not_checked", "not requested by this crawl (outside its scope or limits)"
    cat = category_of(t)
    code = first_status_code(t)
    if is_external(t):
        if cat == "":
            return "not_checked", "on another site, and external links were not checked"
        if cat == "blocked":
            return "not_checked", "on another site whose robots.txt blocks the check"
        if cat == "2xx":
            return "ok", ""
        return "broken", f"answers {code}" if code else "gives no response"
    if cat == "2xx" and code == 200 and site.indexable.get(t.url):
        return "ok", ""
    status = site.indexability_status.get(t.url, "") or ("Non-200" if code != 200 else "")
    label = {"Client Error": f"broken ({code})", "Server Error": f"server error ({code})",
             "Redirected": f"redirects ({code})", "No Response": "gives no response",
             "Blocked by robots.txt": "blocked by robots.txt", "noindex": "noindex",
             "Canonicalised": "canonicalised to another URL", "Non-200": f"answers {code}"}.get(status, status)
    return "broken", label or "not an indexable 200 page"


def llms_summary(site) -> Optional[Dict[str, Any]]:
    """site.llms_txt: the llms.txt check of rep["llms_txt"] with every link judged against the crawl (None when
    the check did not run for this crawl). Each file gains "links_ok", "links_broken": [{"url", "text", "reason"}]
    (<= 500), "links_broken_total", "links_not_checked" (count) and "not_checked_reasons" {reason: count}."""
    rep = getattr(site, "robots_audit", None)
    if not isinstance(rep, dict) or not isinstance(rep.get("llms_txt"), dict):
        return None
    info = rep["llms_txt"]
    out = {k: v for k, v in info.items() if k != "files"}
    out["files"] = []
    for f in info.get("files") or []:
        entry = dict(f)
        ok = broken_total = not_checked = 0
        broken: List[Dict[str, str]] = []
        reasons: Dict[str, int] = {}
        if f.get("state") == "found":
            for link in f.get("links") or []:
                verdict, why = _link_verdict(site, link.get("url") or "")
                if verdict == "ok":
                    ok += 1
                elif verdict == "broken":
                    broken_total += 1
                    if len(broken) < 500:
                        broken.append({"url": link.get("url") or "", "text": link.get("text") or "", "reason": why})
                else:
                    not_checked += 1
                    reasons[why] = reasons.get(why, 0) + 1
            extra = max(0, int(f.get("links_total") or 0) - len(f.get("links") or []))
            if extra:
                not_checked += extra
                reasons["past the first 2,000 links of the file"] = extra
        entry.update(links_ok=ok, links_broken=broken, links_broken_total=broken_total,
                     links_not_checked=not_checked, not_checked_reasons=reasons)
        out["files"].append(entry)
    return out


# ---------------------------------------------------------------------------------------------
# Crawl helpers
# ---------------------------------------------------------------------------------------------
def site_origin(crawler) -> str:
    """scheme://host of the site as it answered: the start URL, or where it redirected to."""
    start = getattr(crawler, "start_url", "") or ""
    results = getattr(crawler, "results_list", None) or []
    for r in results[:20]:
        seo = getattr(r, "seo", None) or {}
        if seo.get("is_html") and getattr(r, "status_code", None) == 200:
            final = seo.get("final_url") or getattr(r, "url", "")
            p = urlparse(final)
            if p.scheme and p.netloc:
                return f"{p.scheme}://{p.netloc}"
    p = urlparse(start)
    return f"{p.scheme}://{p.netloc}" if p.scheme and p.netloc else ""


def crawled_paths(crawler_or_site) -> List[str]:
    """Paths (with query) of crawled HTML pages that answered 200, shallowest first."""
    results = getattr(crawler_or_site, "results_list", None)
    if results is None:
        results = getattr(crawler_or_site, "results", None) or []
    pages = [r for r in results if getattr(r, "status_code", None) == 200
             and (getattr(r, "seo", None) or {}).get("is_html")]
    pages.sort(key=lambda r: (int(getattr(r, "depth", 0) or 0), len(getattr(r, "url", ""))))
    return list(dict.fromkeys(_path_of(r.url) for r in pages))[:MAX_PATHS]


# ---------------------------------------------------------------------------------------------
# Page-level meaning of robots directives (PageData -> values for tabs/robots_txt.py)
# ---------------------------------------------------------------------------------------------
_MAX_SNIPPET = re.compile(r"max-snippet\s*:\s*(-?\d+)")
_UNAVAILABLE = re.compile(r"unavailable_after\s*:\s*(.+)$")


def _robots_values(page: dict) -> List[str]:
    return [v for v in (page.get("meta_robots") or []) + (page.get("x_robots_tag") or []) if v]


def max_snippet(page: dict) -> Optional[int]:
    """Smallest max-snippet value the page sets (-1 = no limit), None when it sets none."""
    found = [int(m.group(1)) for v in _robots_values(page) for m in _MAX_SNIPPET.finditer(v.lower())]
    return min(found) if found else None


def _parse_date(text: str) -> Optional[datetime]:
    text = text.strip().strip("'\"")
    if not text:
        return None
    try:
        d = parsedate_to_datetime(text)          # RFC 822 / 850 ("Wed, 25 Jun 2025 15:00:00 GMT")
        if d is not None:
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        d = datetime.fromisoformat(text.replace("Z", "+00:00"))   # ISO 8601
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    for fmt in ("%d %b %Y %H:%M:%S %Z", "%d %B %Y %H:%M:%S %Z", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def unavailable_after(page: dict) -> Tuple[str, Optional[datetime]]:
    """(the date as written, parsed date or None) of the first unavailable_after directive."""
    for value in _robots_values(page):
        m = _UNAVAILABLE.search(value)
        if not m:
            continue
        rest = m.group(1)
        parts = rest.split(",")
        # The date may contain commas ("wed, 25 jun 2025 ..."): take the longest leading run that parses
        for n in range(len(parts), 0, -1):
            cand = ",".join(parts[:n]).strip()
            d = _parse_date(cand)
            if d is not None:
                return cand, d
        return parts[0].strip(), None
    return "", None


def unavailable_after_passed(page: dict, now: Optional[datetime] = None) -> bool:
    _, d = unavailable_after(page)
    return d is not None and d < (now or datetime.now(timezone.utc))


def conflicting_index(page: dict) -> bool:
    d = page.get("directives") or []
    return "index" in d and ("noindex" in d or "none" in d)


def partial_ai_bots(groups: Dict[str, list], report: Optional[dict]) -> List[Bot]:
    """AI crawlers that may crawl the site in general but whose robots.txt group has Disallow rules
    (crawlers blocked from the whole site are reported once on the robots.txt row instead)."""
    if not groups or not isinstance(report, dict):
        return []
    whole = set(report.get("ai_blocked") or [])
    out = []
    for b in BOTS:
        if b.kind != "ai" or b.label in whole:
            continue
        token = group_for(groups, effective_agent(groups, b.token))
        if token and any(not allow for allow, _, _ in groups.get(token) or []):
            out.append(b)
    return out


def page_blocked_bots(groups: Dict[str, list], report: Optional[dict], url: str,
                      bots: Optional[List[Bot]] = None) -> List[str]:
    """Labels of the AI crawlers robots.txt keeps off this page while they may crawl the rest of the site.
    Pass `bots` (partial_ai_bots) when checking many pages."""
    candidates = partial_ai_bots(groups, report) if bots is None else bots
    if not candidates:
        return []
    path = _path_of(url)
    return [b.label for b in candidates if not decide(groups, effective_agent(groups, b.token), path)[0]]
