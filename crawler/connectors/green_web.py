"""
Green Web Foundation (kind "sustainability"): an opt-in lookup of whether the crawled hosts run on green energy,
used by the carbon rating (crawler/seo/carbon.py, P18) instead of the grid-average factor. No key is needed; the
admin connects it by ticking the consent box (the crawled host names are then sent to the Green Web Foundation).

Provider document (read 2026-10-28): https://developers.thegreenwebfoundation.org/api/greencheck/v3/check-single-domain/
  GET https://api.thegreenwebfoundation.org/api/v3/greencheck/<hostname>  (bare host: no scheme, port or path)
  green host: {"url", "green": true, "hosted_by", "hosted_by_website", "hosted_by_id", "partner", "modified",
               "supporting_documents": [...]}
  not green:  {"url", "green": false, "data": false}

  check(crawler, hosts=None) -> {"hosts": {host: {"green", "hosted_by", "checked_at"}}, "not_checked": {host: why},
                                 "message"}            POST /api/green-web/check; stored in integrations["green_web"]
  after_crawl(crawler) -> result | None                 "Check after every crawl" (W2's post-crawl hook)
  green_for(site, url_or_host) -> True | False | None   (None = not looked up: carbon uses the grid average)
  carbon_note(site) -> str                              one plain sentence for the carbon rating's "green" source

Rules: hosts of a signed-in crawl and private-network hosts are never sent ("Not checked" with the reason); each
answer is cached for 24 hours (<reports_dir>/connectors/green_web_cache.json); at most MAX_HOSTS hosts per check.
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from .base import Connector, ConnectorError, Field, TestResult, now_iso
from .http import http_error, safe_request
from .registry import register

SOURCE = "green_web"
API = "https://api.thegreenwebfoundation.org/api/v3/greencheck/"
TIMEOUT = (5.0, 15.0)
MAX_ANSWER = 200_000
CACHE_SECONDS = 24 * 3600
MAX_HOSTS = 50
CACHE_FILE = "green_web_cache.json"
TEST_HOST = "www.thegreenwebfoundation.org"
NOT_CONNECTED = ("Not connected - the carbon rating uses the grid-average energy factor (connect Green Web Foundation "
                 "under Connections to check green hosting)")
SIGNED_IN = "Not checked: this crawl was signed in, so its host names are not sent to the Green Web Foundation."
PRIVATE = "Not checked: a private-network host is never sent to the Green Web Foundation."


class GreenWeb(Connector):
    id = "green_web"
    name = "Green Web Foundation"
    kind = "sustainability"
    icon = "leaf"
    sends = ["hostnames"]
    description = ("Checks whether the crawled sites are hosted on green energy, so the carbon rating can use the "
                   "green-hosting factor instead of the grid average.")
    docs = ("https://developers.thegreenwebfoundation.org/api/greencheck/v3/check-single-domain/ (read 2026-10-28)")
    fields = [
        Field("consent", "Send the crawled host names to the Green Web Foundation", "bool", required=True,
              default=False, help="Only host names (e.g. www.example.com) are sent, never pages or addresses; "
                                  "signed-in crawls and private hosts are never sent."),
        Field("auto_check", "Check after every crawl", "bool", default=False,
              help="Look the hosts up automatically when a crawl finishes (answers are cached for 24 hours)."),
    ]

    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        if not config.get("consent"):
            return {"consent": "Tick the box to allow sending the crawled host names to the Green Web Foundation."}
        return {}

    def is_set_up(self) -> bool:
        return bool(self.meta().get("connected")) and bool(self.config().get("consent"))

    def lookup(self, host: str) -> Dict[str, Any]:
        """One greencheck call: {"green", "hosted_by", "checked_at"}. Raises ConnectorError / GreenWebError."""
        resp = safe_request("GET", API + quote(host, safe=""), timeout=TIMEOUT, max_bytes=MAX_ANSWER,
                            headers={"Accept": "application/json"}, allow_private_network=self.allow_private())
        if resp.status_code != 200:
            raise GreenWebError(http_error("The Green Web Foundation", resp), resp.status_code)
        try:
            data = resp.json()
        except ValueError:
            raise GreenWebError("The Green Web Foundation sent an answer that is not JSON.", resp.status_code)
        if not isinstance(data, dict) or "green" not in data:
            raise GreenWebError("The Green Web Foundation sent an unexpected answer.", resp.status_code)
        return {"green": bool(data.get("green")), "hosted_by": str(data.get("hosted_by") or "")[:120],
                "hosted_by_website": str(data.get("hosted_by_website") or "")[:200], "checked_at": now_iso()}

    def _test(self) -> TestResult:
        try:
            res = self.lookup(TEST_HOST)
        except GreenWebError as e:
            return TestResult(False, str(e))
        return TestResult(True, f"The Green Web Foundation answered: {TEST_HOST} is "
                                f"{'green' if res['green'] else 'not listed as green'}"
                                f"{(' (hosted by ' + res['hosted_by'] + ')') if res['hosted_by'] else ''}.")


class GreenWebError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------------------
# Cache (24 h)
# ---------------------------------------------------------------------------------------------
def _cache_path() -> str:
    from . import store
    return os.path.join(store.folder(), CACHE_FILE)


def _fresh(entry: Dict[str, Any]) -> bool:
    try:
        at = datetime.fromisoformat(str(entry.get("checked_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - at).total_seconds() < CACHE_SECONDS


def _cache() -> Dict[str, Any]:
    from . import store
    data = store.read_json(_cache_path())
    return data if isinstance(data, dict) else {}


def _save_cache(data: Dict[str, Any]) -> None:
    from . import store
    fresh = {h: v for h, v in data.items() if isinstance(v, dict) and _fresh(v)}
    if len(fresh) > 5000:
        fresh = dict(sorted(fresh.items(), key=lambda kv: kv[1].get("checked_at") or "")[-5000:])
    store.write_json(_cache_path(), fresh)


# ---------------------------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------------------------
def connector() -> GreenWeb:
    from . import registry
    c = registry.get(SOURCE)
    return c if isinstance(c, GreenWeb) else GreenWeb()


def connected() -> bool:
    try:
        return connector().status().state in ("connected", "error")
    except Exception:
        return False


def crawl_hosts(site) -> List[str]:
    """Hosts of the crawl's internal URLs, the start host first."""
    seen: Dict[str, None] = {}
    start = getattr(site, "start_url", "") or getattr(getattr(site, "config", None), "start_url", "") or ""
    if start:
        h = (urlsplit(start).hostname or "").lower()
        if h:
            seen[h] = None
    for r in getattr(site, "pages", None) or []:
        h = (urlsplit(getattr(r, "url", "") or "").hostname or "").lower()
        if h:
            seen[h] = None
    return list(seen)


def _private_crawl(site, crawler) -> bool:
    cfg = getattr(site, "config", None) or getattr(crawler, "config", None)
    auth = getattr(cfg, "auth", None)
    return bool(getattr(crawler, "auth_used", False)) or bool(
        getattr(auth, "enabled", False) and (getattr(auth, "auth_type", "none") or "none") != "none")


def check(crawler, hosts: Optional[List[str]] = None) -> Dict[str, Any]:
    """Looks up the crawl's hosts (cached 24 h) and stores the result with the crawl. Raises
    integrations.NotConnected / IntegrationError (plain messages)."""
    from .. import integrations as integ
    from ..integrations.linkmetrics import private_host
    c = connector()
    if c.status().state not in ("connected", "error"):
        raise integ.NotConnected(NOT_CONNECTED)
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise integ.IntegrationError("Crawl a site first: the Green Web check looks up the crawled hosts.")
    site, _ = crawler.site_index(force=True)
    wanted = [h.lower() for h in (hosts or crawl_hosts(site)) if h]
    wanted = list(dict.fromkeys(wanted))
    over = wanted[MAX_HOSTS:]
    wanted = wanted[:MAX_HOSTS]
    found: Dict[str, Any] = {}
    skipped: Dict[str, str] = {h: f"Not checked: more than {MAX_HOSTS} hosts in this crawl." for h in over}
    if _private_crawl(site, crawler):
        skipped.update({h: SIGNED_IN for h in wanted})
        wanted = []
    cache = _cache()
    errors = 0
    for h in wanted:
        if private_host("https://" + h + "/"):
            skipped[h] = PRIVATE
            continue
        hit = cache.get(h)
        if isinstance(hit, dict) and _fresh(hit):
            found[h] = dict(hit, cached=True)
            continue
        if errors >= 3:
            skipped[h] = "Not checked: the Green Web Foundation could not be reached."
            continue
        try:
            res = c.lookup(h)
        except GreenWebError as e:
            skipped[h] = f"Not checked: {e}" if e.status != 404 else "Not checked: the Green Web Foundation did " \
                                                                     "not know this host."
            errors += 1 if e.status in (0, 429) or e.status >= 500 else 0
            continue
        except ConnectorError as e:
            skipped[h] = f"Not checked: {e}"
            errors += 1
            continue
        found[h] = res
        cache[h] = res
    _save_cache(cache)
    entry = {"source": SOURCE, "connected": True, "reason": "", "status": "done", "fetched_at": now_iso(),
             "results": {}, "hosts": {h: {"green": v["green"], "hosted_by": v.get("hosted_by", ""),
                                          "checked_at": v.get("checked_at", "")} for h, v in found.items()},
             "not_checked": skipped}
    integ.put_result(crawler, SOURCE, entry)
    green = sum(1 for v in found.values() if v["green"])
    msg = (f"{len(found):,} host{'s' if len(found) != 1 else ''} checked: {green:,} green"
           + (f"; {len(skipped):,} not checked" if skipped else "") + ".")
    if found and not skipped:
        try:
            c._set_meta(tested_live=True, tested_at=now_iso())
        except Exception:
            pass
    return {"hosts": entry["hosts"], "not_checked": skipped, "message": msg}


def after_crawl(crawler) -> Optional[Dict[str, Any]]:
    """"Check after every crawl": runs check() when Green Web is connected with auto_check on (W2 calls it in the
    post-crawl thread); None when it is off or not connected. Never raises."""
    try:
        c = connector()
        if c.status().state not in ("connected", "error") or not c.config().get("auto_check"):
            return None
        return check(crawler)
    except Exception as e:          # a lookup problem must not break the crawl's post-processing
        return {"hosts": {}, "not_checked": {}, "message": f"The Green Web check failed ({type(e).__name__})."}


def green_for(site_or_crawler, url_or_host: str) -> Optional[bool]:
    """True / False from the stored Green Web result for the URL's host; None when it was not looked up."""
    e = (getattr(site_or_crawler, "integrations", None) or {}).get(SOURCE)
    if not isinstance(e, dict):
        return None
    host = str(url_or_host or "").lower()
    if "/" in host or ":" in host:
        host = (urlsplit(url_or_host if "//" in url_or_host else "//" + url_or_host).hostname or "").lower()
    v = (e.get("hosts") or {}).get(host)
    return bool(v.get("green")) if isinstance(v, dict) and "green" in v else None


def carbon_note(site) -> str:
    """Where the carbon rating's green / grey factor comes from, in one sentence."""
    e = (getattr(site, "integrations", None) or {}).get(SOURCE)
    if not isinstance(e, dict):
        if connected():
            return ("Green hosting was not checked for this crawl yet (Connections > Green Web Foundation > Check): "
                    "the grid-average energy factor is used.")
        return NOT_CONNECTED
    hosts = e.get("hosts") or {}
    green = sorted(h for h, v in hosts.items() if v.get("green"))
    grey = sorted(h for h, v in hosts.items() if not v.get("green"))
    parts = []
    if green:
        parts.append(f"green hosting for {', '.join(green[:3])}{' and others' if len(green) > 3 else ''}")
    if grey:
        parts.append(f"the grid average for {', '.join(grey[:3])}{' and others' if len(grey) > 3 else ''} "
                     "(not listed as green)")
    nc = e.get("not_checked") or {}
    if nc:
        parts.append(f"the grid average for {len(nc)} host{'s' if len(nc) != 1 else ''} not checked")
    return ("Green Web Foundation: " + "; ".join(parts) + ".") if parts else NOT_CONNECTED


register(GreenWeb())
