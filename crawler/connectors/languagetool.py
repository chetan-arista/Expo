"""
LanguageTool (kind "language"): the Grammar Errors filter of the Content tab. Nothing is bundled (LanguageTool's
server is Java, LGPL-2.1): connect your own server (e.g. http://localhost:8081, with 'Allow private network') or,
after an explicit opt-in, the public service at languagetool.org (page text is then sent to languagetool.org).

Provider documents (read 2026-10-28):
  https://languagetool.org/http-api/languagetool-swagger.json  POST /v2/check, form fields text, language
      ("auto" or a code such as en-US), username + apiKey (Premium), level (default | picky); answer {"software",
      "language", "matches": [{"message", "shortMessage", "replacements": [{"value"}], "offset", "length",
      "context": {"text", "offset", "length"}, "sentence", "rule": {"id", "description", "issueType",
      "category": {"id", "name"}}}]}
  https://dev.languagetool.org/public-http-api  the free public API: POST only, 20 requests per IP per minute,
      75 KB per minute, 20 KB per request.

The grammar job (start_check / job_status; integrations JOBS source "languagetool"):
- checks the crawl's indexable internal HTML pages that answered 2xx, in crawl order, at most max_pages per run
  (default 100) and max_chars of each page's content-area text (crawler/seo/page_text.py); a later run checks the
  pages without a result first (only_missing), and the runs merge per URL (integrations.put_result);
- a signed-in crawl is never sent: every page is "Not checked: this crawl was signed in";
- rate-limited (the public limits above for languagetool.org, SELF_HOSTED_PER_MINUTE for your own server);
- results in integrations["languagetool"] = {"source", "connected", "reason", "status", "fetched_at", "server"
  (host only), "results": {url: {"matches": [{"message", "context", "offset", "length", "rule", "category",
  "replacements"}] (at most MAX_MATCHES), "count", "language", "truncated"}}, "not_checked": {url: reason}};
  a page LanguageTool could not check is "Not checked: LanguageTool answered <status>" (never 0 errors).
"""
import threading
import time
from collections import deque
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .base import Connector, ConnectorError, Field, TestResult, now_iso
from .http import check_url, http_error, safe_request
from .registry import register

SOURCE = "languagetool"
PUBLIC_URL = "https://api.languagetool.org/v2/check"
PREMIUM_URL = "https://api.languagetoolplus.com/v2/check"
PUBLIC_PER_MINUTE = 20
PUBLIC_BYTES_PER_MINUTE = 75_000
PUBLIC_BYTES_PER_REQUEST = 19_500      # under the 20 KB per request of the free API (form encoding included)
SELF_HOSTED_PER_MINUTE = 120
DEFAULT_PAGES = 100
MAX_PAGES = 5000
DEFAULT_CHARS = 20_000
MAX_MATCHES = 100                      # matches kept per page ("count" is the full number)
TIMEOUT = (5.0, 60.0)
MAX_ANSWER = 2_000_000
MAX_RETRIES_429 = 2
STOP_AFTER_NETWORK_ERRORS = 3
NOT_CONNECTED = "Not connected - connect LanguageTool under Connections"
SIGNED_IN = "This crawl was signed in: its page text is not sent to LanguageTool."
SAMPLE_TEXT = "This are a short sample sentence for ARACHNIX's grammar check."
LANGUAGES = ["page", "auto", "en-US", "en-GB", "en-AU", "en-CA", "de-DE", "fr", "es", "it", "nl", "pt-PT", "pt-BR",
             "pl-PL", "sv", "da-DK", "ca-ES", "uk-UA", "ru-RU", "ja-JP", "zh-CN"]
LANGUAGE_LABELS = {"page": "The page's own language (lang attribute), else detect", "auto": "Detect for each page"}


class LanguageTool(Connector):
    id = "languagetool"
    name = "LanguageTool"
    kind = "language"
    icon = "grammar"
    sends = ["page_text"]
    description = ("Grammar and style check of the crawled pages (Content > Grammar Errors) with your own "
                   "LanguageTool server or, if you allow it, the public LanguageTool service.")
    docs = ("https://languagetool.org/http-api/languagetool-swagger.json and "
            "https://dev.languagetool.org/public-http-api (read 2026-10-28)")
    fields = [
        Field("server_url", "Your LanguageTool server", "url",
              help="e.g. http://localhost:8081 for a server on this network (turn on 'Allow private network'). "
                   "Leave empty to use the public service below."),
        Field("use_public_api", "Use the public LanguageTool service (page text goes to languagetool.org)", "bool",
              default=False, help="Only public pages are sent; at most 20 pages a minute (the free limits)."),
        Field("username", "Premium username (email)", "text", help="Only for a LanguageTool Premium account."),
        Field("api_key", "Premium API key", "password", secret=True, help="Only for a LanguageTool Premium account."),
        Field("allow_private_network", "Allow private network", "bool", default=False,
              help="Allow a server inside your own network (http:// is then allowed too)."),
        Field("language", "Language", "select", options=list(LANGUAGES), default="page",
              option_labels=dict(LANGUAGE_LABELS)),
        Field("level", "Strictness", "select", options=["default", "picky"], default="default",
              option_labels={"default": "Default", "picky": "Picky (more style rules)"}),
        Field("max_pages", "Pages per run", "number", default=DEFAULT_PAGES, min=1, max=MAX_PAGES,
              help="Indexable pages checked in one run; the rest are Not checked until the next run."),
        Field("max_chars", "Characters per page", "number", default=DEFAULT_CHARS, min=1000, max=100_000,
              help="The start of each page's main text that is checked."),
    ]

    # --- settings ---------------------------------------------------------------------------------
    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        server = str(config.get("server_url") or "").strip()
        if not server and not config.get("use_public_api"):
            return {"server_url": "Enter your LanguageTool server, or tick 'Use the public LanguageTool service'."}
        if bool(config.get("username")) != bool(secrets.get("api_key")):
            return {"api_key": "LanguageTool Premium needs both the username and the API key."}
        if server:
            try:
                check_url(_endpoint_of(server), bool(config.get("allow_private_network")) or self.allow_private_default)
            except ConnectorError as e:
                return {"server_url": str(e)}
        return {}

    def endpoint(self) -> str:
        cfg = self.config()
        server = str(cfg.get("server_url") or "").strip()
        if server:
            return _endpoint_of(server)
        return PREMIUM_URL if (cfg.get("username") and self.secret("api_key")) else PUBLIC_URL

    def is_public(self) -> bool:
        """The text goes to languagetool.org (not a server of the team's own)."""
        return not str(self.config().get("server_url") or "").strip()

    def public_config(self) -> Dict[str, Any]:
        out = super().public_config()
        try:
            out["server_host"] = urlsplit(self.endpoint()).hostname or ""
        except ValueError:
            out["server_host"] = ""
        out["public_service"] = self.is_public()
        return out

    # --- one call -----------------------------------------------------------------------------------
    def check_text(self, text: str, language: str = "auto") -> Dict[str, Any]:
        """One /v2/check call: {"matches": [...], "count", "language"}. Raises ConnectorError (network, kind +
        host only) or LanguageToolError (an HTTP error answer, with its status)."""
        cfg = self.config()
        form = {"text": text, "language": language or "auto"}
        if cfg.get("level") == "picky":
            form["level"] = "picky"
        key = self.secret("api_key")
        if cfg.get("username") and key:
            form["username"] = str(cfg["username"])
            form["apiKey"] = key
        url = self.endpoint()
        resp = None
        for attempt in range(MAX_RETRIES_429 + 1):
            resp = safe_request("POST", url, data=form, allow_private_network=self.allow_private(),
                                timeout=TIMEOUT, max_bytes=MAX_ANSWER, secrets=[key] if key else [],
                                headers={"Accept": "application/json"})
            if resp.status_code != 429 or attempt == MAX_RETRIES_429:
                break
            try:
                wait = float(resp.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0
            time.sleep(min(max(wait, 3.0 * (attempt + 1)), 30.0))
        if resp.status_code != 200:
            raise LanguageToolError(http_error("LanguageTool", resp, self.masker()), resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise LanguageToolError("LanguageTool sent an answer that is not JSON.", resp.status_code)
        return parse_answer(data)

    def _test(self) -> TestResult:
        try:
            res = self.check_text(SAMPLE_TEXT, "en-US")
        except LanguageToolError as e:
            return TestResult(False, str(e))
        host = urlsplit(self.endpoint()).hostname or "LanguageTool"
        return TestResult(True, f"LanguageTool at {host} answered: {res['count']} issue"
                                f"{'s' if res['count'] != 1 else ''} in the sample sentence.")


class LanguageToolError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _endpoint_of(server: str) -> str:
    base = server.strip().rstrip("/")
    if base.endswith("/v2/check"):
        return base
    if base.endswith("/v2"):
        return base + "/check"
    return base + "/v2/check"


def parse_answer(data: Any) -> Dict[str, Any]:
    """{"matches": [{"message", "context", "offset", "length", "rule", "category", "replacements"}] (at most
    MAX_MATCHES), "count", "language"} from a /v2/check answer."""
    data = data if isinstance(data, dict) else {}
    raw = data.get("matches") if isinstance(data.get("matches"), list) else []
    out = []
    for m in raw[:MAX_MATCHES]:
        if not isinstance(m, dict):
            continue
        ctx = m.get("context") if isinstance(m.get("context"), dict) else {}
        rule = m.get("rule") if isinstance(m.get("rule"), dict) else {}
        cat = rule.get("category") if isinstance(rule.get("category"), dict) else {}
        out.append({"message": str(m.get("message") or m.get("shortMessage") or "")[:300],
                    "context": str(ctx.get("text") or m.get("sentence") or "")[:300],
                    "context_offset": _int(ctx.get("offset")), "offset": _int(m.get("offset")),
                    "length": _int(m.get("length")), "rule": str(rule.get("id") or "")[:100],
                    "category": str(cat.get("name") or cat.get("id") or "")[:100],
                    "replacements": [str(r.get("value") or "")[:100] for r in (m.get("replacements") or [])[:3]
                                     if isinstance(r, dict)]})
    lang = data.get("language") if isinstance(data.get("language"), dict) else {}
    return {"matches": out, "count": len(raw), "language": str(lang.get("code") or "")[:20]}


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------------------------
# Rate limit (requests and bytes per rolling minute)
# ---------------------------------------------------------------------------------------------
class Budget:
    def __init__(self, per_minute: int, bytes_per_minute: int = 0):
        self.per_minute = max(1, int(per_minute))
        self.bytes_per_minute = int(bytes_per_minute or 0)
        self._sent: "deque[Tuple[float, int]]" = deque()

    def wait(self, nbytes: int, stop: Optional[threading.Event] = None) -> bool:
        """Waits until one more request of nbytes fits; False when stopped."""
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0][0] >= 60.0:
                self._sent.popleft()
            used = sum(b for _, b in self._sent)
            if len(self._sent) < self.per_minute and (not self.bytes_per_minute or not self._sent
                                                      or used + nbytes <= self.bytes_per_minute):
                self._sent.append((now, nbytes))
                return True
            delay = min(5.0, 60.0 - (now - self._sent[0][0]) + 0.05)
            if stop is not None:
                if stop.wait(max(0.05, delay)):
                    return False
            else:
                time.sleep(max(0.05, delay))


# ---------------------------------------------------------------------------------------------
# The grammar job
# ---------------------------------------------------------------------------------------------
def connector() -> LanguageTool:
    from . import registry
    c = registry.get(SOURCE)
    return c if isinstance(c, LanguageTool) else LanguageTool()


def _private(site, crawler) -> bool:
    cfg = getattr(site, "config", None) or getattr(crawler, "config", None)
    auth = getattr(cfg, "auth", None)
    return bool(getattr(crawler, "auth_used", False)) or bool(
        getattr(auth, "enabled", False) and (getattr(auth, "auth_type", "none") or "none") != "none")


def _pages(site) -> List[Any]:
    """Indexable internal HTML pages that answered 2xx, in crawl order."""
    from ..seo.tabs.internal import category_of, is_html_doc
    return [r for r in getattr(site, "pages", None) or []
            if is_html_doc(r) and category_of(r) == "2xx" and site.indexable.get(r.url)]


def page_language(r, setting: str) -> str:
    """The language sent for one page: the setting, or with "page" the page's lang attribute (en-gb -> en-GB),
    else "auto"."""
    if setting and setting != "page":
        return setting
    seo = getattr(r, "seo", None) or {}
    lang = str(((seo.get("a11y") or {}).get("html_lang") or "")).strip().replace("_", "-")
    if not lang or not all(p.isalnum() for p in lang.split("-")):
        return "auto"
    parts = lang.split("-")
    base = parts[0].lower()
    if len(parts) > 1 and len(parts[1]) == 2:
        return f"{base}-{parts[1].upper()}"
    return base


def _trim(text: str, max_chars: int, max_bytes: int = 0) -> Tuple[str, bool]:
    """The start of the text (cut at a line or word end), within max_chars and, when set, max_bytes of UTF-8."""
    cut = False
    if len(text) > max_chars:
        text, cut = text[:max_chars], True
    if max_bytes:
        while len(text.encode("utf-8")) > max_bytes:
            text, cut = text[:int(len(text) * 0.9)], True
    if cut:
        end = max(text.rfind("\n"), text.rfind(". "), text.rfind(" "))
        if end > len(text) * 0.6:
            text = text[:end + 1]
    return text, cut


def _entry(c: LanguageTool, status: str, results: Dict[str, Any], skipped: Dict[str, str],
           reason: str = "") -> Dict[str, Any]:
    return {"source": SOURCE, "connected": True, "reason": reason, "status": status, "fetched_at": now_iso(),
            "server": urlsplit(c.endpoint()).hostname or "", "public_service": c.is_public(),
            "results": results, "not_checked": skipped}


def start_check(crawler, limit: Optional[int] = None, urls: Optional[List[str]] = None, only_missing: bool = True,
                on_done=None) -> Dict[str, Any]:
    """Starts the grammar job for this crawl (POST /api/grammar/run). Returns the job's public view. Raises
    integrations.NotConnected / IntegrationError (plain messages)."""
    from .. import integrations as integ
    c = connector()
    if c.status().state not in ("connected", "error"):
        raise integ.NotConnected(NOT_CONNECTED)
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise integ.IntegrationError("Crawl a site first: LanguageTool checks the crawled pages.")
    cfg = c.config()
    cap = int(cfg.get("max_pages") or DEFAULT_PAGES)
    if limit:
        cap = max(1, min(int(limit), MAX_PAGES))

    def work(job) -> None:
        try:
            _run(job, crawler, c, cap, int(cfg.get("max_chars") or DEFAULT_CHARS), urls, only_missing)
        finally:
            if on_done is not None:
                try:
                    on_done(crawler, SOURCE, job)
                except Exception:
                    pass

    return integ.JOBS.start(SOURCE, work, owner=crawler).public()


def _run(job, crawler, c: LanguageTool, cap: int, max_chars: int, urls: Optional[List[str]],
         only_missing: bool) -> None:
    from .. import integrations as integ
    from ..seo.page_text import page_text
    site, _ = crawler.site_index(force=True)
    pages = _pages(site)
    if urls:
        wanted = set(urls)
        pages = [r for r in pages if r.url in wanted]
    prev = integ.get_result(crawler, SOURCE) or {}
    done_before = prev.get("results") or {}
    if only_missing and not urls:
        pages = [r for r in pages if r.url not in done_before]
    if _private(site, crawler):
        integ.put_result(crawler, SOURCE, _entry(c, "done", {}, {r.url: SIGNED_IN for r in pages}))
        job.progress("Done", 0, 0, SIGNED_IN)
        return
    if not pages:
        integ.put_result(crawler, SOURCE, _entry(c, "done", {}, {}))
        job.progress("Done", 0, 0, "Every indexable page was already checked (or there is no page to check).")
        return
    take, over = pages[:cap], pages[cap:]
    skipped: Dict[str, str] = {r.url: f"Past the LanguageTool limit of {cap:,} pages per run: run Check grammar "
                                      "again to check the next pages." for r in over}
    results: Dict[str, Any] = {}
    public = c.is_public()
    budget = Budget(PUBLIC_PER_MINUTE, PUBLIC_BYTES_PER_MINUTE) if public else Budget(SELF_HOSTED_PER_MINUTE)
    setting = str(c.config().get("language") or "page")
    job.progress("Checking grammar", 0, len(take), f"LanguageTool: 0 of {len(take):,} pages")
    integ.put_result(crawler, SOURCE, _entry(c, "running", {}, skipped))
    net_errors = 0
    for i, r in enumerate(take):
        if job.stopped:
            for rest in take[i:]:
                skipped[rest.url] = "The grammar check was stopped before this page."
            break
        text, source = page_text(crawler, r.url, content_area=True, max_chars=max_chars + 1000)
        text = (text or "").strip()
        if not text:
            skipped[r.url] = ("No stored text for this page (the page source was not kept)." if not source
                              else "The page has no readable text.")
            job.progress(done=i + 1)
            continue
        text, cut = _trim(text, max_chars, PUBLIC_BYTES_PER_REQUEST if public else 0)
        if not budget.wait(len(text.encode("utf-8")), job.stop_event):
            skipped[r.url] = "The grammar check was stopped before this page."
            continue
        try:
            res = c.check_text(text, page_language(r, setting))
            res["truncated"] = cut
            res["chars"] = len(text)
            results[r.url] = res
            net_errors = 0
        except LanguageToolError as e:
            skipped[r.url] = f"LanguageTool answered {e.status}" if e.status else str(e)
            if e.status == 429:
                for rest in take[i + 1:]:
                    skipped[rest.url] = "LanguageTool's rate limit was reached: run Check grammar again later."
                job.error(str(e))
                break
        except ConnectorError as e:
            skipped[r.url] = f"LanguageTool could not be reached: {e}"
            job.error(str(e))
            net_errors += 1
            if net_errors >= STOP_AFTER_NETWORK_ERRORS:
                for rest in take[i + 1:]:
                    skipped[rest.url] = f"LanguageTool could not be reached: {e}"
                break
        job.progress(done=i + 1, message=f"LanguageTool: {i + 1:,} of {len(take):,} pages")
        if (i + 1) % 10 == 0:
            integ.put_result(crawler, SOURCE, _entry(c, "running", dict(results), dict(skipped)))
    status = "stopped" if job.stopped else "done"
    integ.put_result(crawler, SOURCE, _entry(c, status, results, skipped))
    found = sum(1 for v in results.values() if v.get("count"))
    job.progress("Done", len(take), len(take),
                 f"LanguageTool checked {len(results):,} pages ({found:,} with grammar issues); "
                 f"{len(skipped):,} not checked.")


def job_status(crawler=None) -> Dict[str, Any]:
    """GET /api/grammar/status: {"connected", "reason", "state", "done", "total", "checked", "with_errors",
    "not_checked", "message", "public_service"} for the crawl on screen."""
    from .. import integrations as integ
    c = connector()
    connected = c.status().state in ("connected", "error")
    job = integ.JOBS.get(SOURCE)
    if job is not None and crawler is not None and job.owner is not None and not job.owned_by(crawler):
        job = None
    entry = integ.get_result(crawler, SOURCE) if crawler is not None else None
    results = (entry or {}).get("results") or {}
    return {"connected": connected, "reason": "" if connected else NOT_CONNECTED,
            "state": job.status if job is not None else ("done" if entry else "idle"),
            "done": job.done if job is not None else len(results),
            "total": job.total if job is not None else len(results),
            "checked": len(results), "with_errors": sum(1 for v in results.values() if v.get("count")),
            "not_checked": len((entry or {}).get("not_checked") or {}),
            "message": job.message if job is not None else "",
            "public_service": c.is_public() if connected else False}


register(LanguageTool())
