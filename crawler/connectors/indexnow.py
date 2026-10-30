"""
IndexNow (kind "indexing"): tell the search engines that take part about new or changed pages of the crawled site.
The engines that take part share each submission with each other: Microsoft Bing, Yandex, Seznam.cz, Naver, Yep and
others. Google does not take part in IndexNow.

Provider documents (read 2026-10-28):
  https://www.indexnow.org/documentation   POST <endpoint>/indexnow, Content-Type application/json; charset=utf-8,
      {"host", "key", "keyLocation"?, "urlList": [...]} (at most 10,000 URLs, all on that host). Key: 8-128 of
      a-z A-Z 0-9 and "-". Key file: https://<host>/<key>.txt holding the key (UTF-8), or keyLocation elsewhere on the
      host (it then covers that folder only). Answers: 200 submitted, 202 received (key check pending), 400 bad
      request, 403 key not valid, 422 URLs not of the host / key not matching, 429 too many requests.
  https://www.indexnow.org/searchengines.json   the engines that take part.

  key_check(host) -> {"ok", "key_url" (the key masked), "detail"}          GET /api/indexnow/key-check (the site owner
                                                                          hosts the file; ARACHNIX only reads it)
  candidates(crawler, source, ...) -> URLs from a comparison, a filter or a list (W2 resolves the comparison)
  submit(crawler, urls, dry_run=True, source="urls") -> {"host", "count", "submitted", "status", "detail",
      "engines_note", "left_out": [{url, reason}], "dry_run", "endpoint"}  POST /api/indexnow/submit
  key_file() -> {"name", "content"}        the key file to put on the site (admin download; the key is a secret
                                           everywhere else: shown as "set" and masked as ***)

Only indexable pages that answered exactly 200, on the verified host (a passed key check), of a crawl that was not
signed in, are sent; every other URL is left out with its reason. One submission a minute; dry run first.
"""
import re
import secrets as pysecrets
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .base import Connector, ConnectorError, Field, Masker, Status, TestResult, now_iso
from .http import http_error, safe_request, short_body
from .registry import register

KEY_RE = re.compile(r"^[A-Za-z0-9-]{8,128}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
MAX_URLS = 10_000
MIN_INTERVAL = 60.0
TIMEOUT = (5.0, 30.0)
ENDPOINTS = {
    "indexnow": "https://api.indexnow.org/indexnow",
    "bing": "https://www.bing.com/indexnow",
    "yandex": "https://yandex.com/indexnow",
    "seznam": "https://search.seznam.cz/indexnow",
    "naver": "https://searchadvisor.naver.com/indexnow",
    "yep": "https://indexnow.yep.com/indexnow",
}
ENDPOINT_LABELS = {"indexnow": "api.indexnow.org (shared with every engine)", "bing": "Microsoft Bing",
                   "yandex": "Yandex", "seznam": "Seznam.cz", "naver": "Naver", "yep": "Yep"}
ENGINES_NOTE = ("IndexNow shares each submission with the search engines that take part: Microsoft Bing, Yandex, "
                "Seznam.cz, Naver, Yep and others. Google does not take part in IndexNow, so this does not reach "
                "Google.")
STATUS_TEXT = {
    200: "submitted: the search engines received the URLs",
    202: "received: the key is still being checked; the URLs are kept until it is",
    400: "refused: the request was not in the expected format",
    403: "refused: the key is not valid (the key file was not found on the site, or it does not hold the key)",
    422: "refused: some URLs are not on the host, or the key does not match the protocol",
    429: "refused: too many submissions; wait before submitting again",
}
SIGNED_IN = "This crawl was signed in: its URLs may not be public, so they are not sent."


class IndexNow(Connector):
    id = "indexnow"
    name = "IndexNow"
    kind = "indexing"
    icon = "indexnow"
    sends = ["urls"]
    description = ("Tell Bing, Yandex, Seznam.cz, Naver, Yep and the other IndexNow engines about new or changed "
                   "pages. Google does not take part in IndexNow.")
    docs = "https://www.indexnow.org/documentation and https://www.indexnow.org/searchengines.json (read 2026-10-28)"
    fields = [
        Field("host", "Site host", "text", required=True,
              help="The host whose URLs you submit, e.g. www.example.com (one host per key file)."),
        Field("key", "IndexNow key", "password", secret=True,
              help="8-128 letters, digits or dashes. Leave empty to have ARACHNIX make one; then download the key "
                   "file and put it at the root of the site."),
        Field("key_location", "Key file address", "url",
              help="Only when the key file is not at https://<host>/<key>.txt (it then covers its own folder only)."),
        Field("endpoint", "Send to", "select", options=list(ENDPOINTS), default="indexnow",
              option_labels=dict(ENDPOINT_LABELS),
              help="Every engine that takes part shares submissions with the others."),
    ]

    # --- settings ---------------------------------------------------------------------------------
    def validate(self, config, secrets):
        clean, changes, errors = super().validate(config, secrets)
        clean["host"] = str(clean.get("host") or "").strip().lower().rstrip(".")
        if "host" not in errors and clean["host"] and not HOST_RE.match(clean["host"]):
            errors["host"] = "Give the host only, e.g. www.example.com (no https:// and no path)."
        given = changes.get("key") if "key" in changes else None
        if given is not None and given and not KEY_RE.match(given):
            errors["key"] = "The key must be 8-128 letters, digits or dashes."
        if not errors and (("key" in changes and not changes["key"]) or ("key" not in changes and
                                                                          not self.secret("key"))):
            changes["key"] = new_key()            # made for the admin (or a cleared key replaced); download the file
        return clean, changes, errors

    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        loc = str(config.get("key_location") or "").strip()
        if loc:
            p = urlsplit(loc)
            if (p.hostname or "").lower() != config.get("host") or p.scheme not in ("https", "http"):
                return {"key_location": "The key file must be an address on the same host."}
            if not p.path.endswith(".txt"):
                return {"key_location": "The key file must be a .txt file."}
        return {}

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        before = (self.config().get("host"), self.secret("key"), self.config().get("key_location"))
        st = super().connect(config, secrets)
        if (self.config().get("host"), self.secret("key"), self.config().get("key_location")) != before:
            self._set_meta(verified=None)           # a new host, key or file: check the key file again
            st = self.status()
        return st

    def masker(self) -> Masker:
        key = self.secret("key")
        return Masker([key] if key else [])

    def key_url(self, origin: str = "") -> str:
        """The key file's address: the key location, else <origin>/<key>.txt (origin = the crawled site's own
        scheme://host[:port] when it is this host, else https://<host>)."""
        cfg = self.config()
        loc = str(cfg.get("key_location") or "").strip()
        if loc:
            return loc
        base = f"https://{cfg.get('host')}"
        if origin:
            p = urlsplit(origin)
            if (p.hostname or "").lower() == cfg.get("host") and p.scheme in ("http", "https"):
                base = f"{p.scheme}://{p.netloc}"
        return f"{base}/{self.secret('key')}.txt"

    def verified(self) -> Dict[str, Any]:
        v = self.meta().get("verified")
        return v if isinstance(v, dict) else {}

    def public_config(self) -> Dict[str, Any]:
        out = super().public_config()
        v = self.verified()
        out["key_url"] = self.masker()(self.key_url(self.meta().get("origin") or "")) if self.secret("key") else ""
        out["verified"] = bool(v.get("ok")) and v.get("host") == self.config().get("host")
        out["verified_at"] = v.get("at") or ""
        out["engines_note"] = ENGINES_NOTE
        out["last_submit"] = self.meta().get("last_submit") or None
        return out

    def endpoint(self) -> str:
        return ENDPOINTS.get(str(self.config().get("endpoint") or ""), ENDPOINTS["indexnow"])

    # --- the key file ---------------------------------------------------------------------------------
    def key_check(self, host: str = "", origin: str = "") -> Dict[str, Any]:
        """Reads the key file from the site (the site owner put it there). {"ok", "key_url" (masked), "detail"}.
        origin: the crawled site's scheme://host[:port] (used when it is this host)."""
        cfg = self.config()
        host = (host or cfg.get("host") or "").strip().lower()
        mask = self.masker()
        key = self.secret("key")
        if not self.is_set_up() or not key:
            return {"ok": False, "key_url": "", "detail": "Not connected: connect IndexNow first."}
        if host != cfg.get("host"):
            return {"ok": False, "key_url": "", "detail": f"IndexNow is set up for {cfg.get('host')}, not {host}."}
        url = self.key_url(origin)
        try:
            resp = safe_request("GET", url, timeout=(5.0, 15.0), max_bytes=10_000,
                                allow_private_network=self.allow_private(), headers={"Accept": "text/plain"})
        except ConnectorError as e:
            res = {"ok": False, "key_url": mask(url), "detail": f"The key file could not be read: {mask(str(e))}"}
            self._set_meta(verified={"ok": False, "host": host, "at": now_iso(), "detail": res["detail"]})
            return res
        if resp.status_code != 200:
            res = {"ok": False, "key_url": mask(url),
                   "detail": f"The site answered {resp.status_code} for the key file: put a text file named "
                             f"<key>.txt holding the key at the root of {host} (download it in Connections)."}
        else:
            text = (resp.content or b"")[:1000].decode("utf-8", "replace").strip().lstrip("﻿")
            first = text.splitlines()[0].strip() if text else ""
            if first == key:
                res = {"ok": True, "key_url": mask(url), "detail": f"The key file on {host} holds the key."}
            elif not text:
                res = {"ok": False, "key_url": mask(url), "detail": "The key file is empty: it must hold the key."}
            else:
                res = {"ok": False, "key_url": mask(url),
                       "detail": "The key file was found but holds a different text than the key."}
        self._set_meta(verified={"ok": res["ok"], "host": host, "at": now_iso(), "detail": res["detail"]},
                       origin=origin or None)
        return res

    def _test(self) -> TestResult:
        res = self.key_check(origin=self.meta().get("origin") or "")
        return TestResult(bool(res["ok"]), res["detail"] + (" IndexNow itself answers when you submit URLs."
                                                            if res["ok"] else ""))

    # --- submitting -------------------------------------------------------------------------------------
    def submit_urls(self, urls: List[str]) -> Tuple[int, str]:
        """One POST of up to MAX_URLS URLs. (HTTP status, plain detail). Raises ConnectorError (network)."""
        cfg = self.config()
        body: Dict[str, Any] = {"host": cfg.get("host"), "key": self.secret("key"), "urlList": list(urls)}
        if str(cfg.get("key_location") or "").strip():
            body["keyLocation"] = cfg["key_location"].strip()
        resp = safe_request("POST", self.endpoint(), json=body, timeout=TIMEOUT, max_bytes=100_000,
                            headers={"Content-Type": "application/json; charset=utf-8"},
                            allow_private_network=self.allow_private(), secrets=[self.secret("key")])
        what = STATUS_TEXT.get(resp.status_code)
        host = urlsplit(self.endpoint()).hostname or "IndexNow"
        if what:
            detail = f"{host} answered {resp.status_code}: {what}."
            extra = short_body(resp, 160)
            if resp.status_code >= 400 and extra:
                detail += f" ({self.masker()(extra)})"
        else:
            detail = http_error(host, resp, self.masker())
        return resp.status_code, detail


def new_key() -> str:
    """A new IndexNow key: 32 hexadecimal characters."""
    return pysecrets.token_hex(16)


def connector() -> IndexNow:
    from . import registry
    c = registry.get("indexnow")
    return c if isinstance(c, IndexNow) else IndexNow()


def key_check(host: str = "", origin: str = "") -> Dict[str, Any]:
    """GET /api/indexnow/key-check?host= (the API passes the loaded crawl's origin when it is that host)."""
    return connector().key_check(host, origin)


def key_file() -> Dict[str, str]:
    """The key file the site owner puts at the root of the site (admin download only)."""
    c = connector()
    key = c.secret("key")
    if not c.is_set_up() or not key:
        raise ConnectorError("Connect IndexNow first.", "invalid")
    return {"name": f"{key}.txt", "content": key}


def _private_crawl(site, crawler) -> bool:
    cfg = getattr(site, "config", None) or getattr(crawler, "config", None)
    auth = getattr(cfg, "auth", None)
    return bool(getattr(crawler, "auth_used", False)) or bool(
        getattr(auth, "enabled", False) and (getattr(auth, "auth_type", "none") or "none") != "none")


def candidates(crawler, source: str, tab: str = "", filters: Optional[List[str]] = None,
               urls: Optional[List[str]] = None, compare_urls: Optional[List[str]] = None) -> List[str]:
    """The URLs a submission starts from: "urls" (given), "filter" (the rows of tab + filters, any of them) or
    "compare" (compare_urls: the comparison's new and changed URLs, resolved by the API)."""
    if source == "urls":
        return [str(u).strip() for u in (urls or []) if str(u or "").strip()]
    if source == "compare":
        return [str(u).strip() for u in (compare_urls or []) if str(u or "").strip()]
    if source == "filter":
        if crawler is None:
            return []
        site, index = crawler.site_index()
        res = index.tabs.get(tab) if index is not None else None
        if res is None:
            raise ConnectorError(f"Unknown tab: {str(tab)[:40]}.", "invalid")
        out: Dict[str, None] = {}
        for fid in (filters or ["all"]):
            rows = res.rows if fid == "all" else res.filter_rows.get(fid)
            if rows is None:
                raise ConnectorError(f"Unknown filter: {str(fid)[:40]}.", "invalid")
            for r in rows:
                out[getattr(r, "url", "")] = None
        return [u for u in out if u]
    raise ConnectorError("Choose the URLs: from the comparison, a filter or a list.", "invalid")


def compare_candidates(a: Any, b: Any = "current", reports_dir: Optional[str] = None,
                       current: Optional[dict] = None, url_map: Any = None) -> List[str]:
    """New and changed URLs between two crawls (URLs only in the newer crawl, and URLs whose title, meta
    description, H1, content, canonical, status or indexability changed), for source "compare"."""
    from ..seo import compare as cmp
    out: Dict[str, None] = {}
    lists = [("internal:all", "new")] + [("", f"changed:{el}") for el in
                                         ("title", "meta_description", "h1", "content", "canonical", "status",
                                          "indexability")]
    for key, kind in lists:
        got = cmp.compare_urls(a, b, key, kind, reports_dir=reports_dir, current=current, url_map=url_map,
                               limit=5000)
        for u in got.get("urls") or []:
            out[u] = None
        total = int(got.get("total") or 0)
        offset = 5000
        while offset < total and len(out) < MAX_URLS * 2:
            more = cmp.compare_urls(a, b, key, kind, reports_dir=reports_dir, current=current, url_map=url_map,
                                    offset=offset, limit=5000)
            for u in more.get("urls") or []:
                out[u] = None
            offset += 5000
    return list(out)


def _eligible(crawler, urls: List[str], host: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """(URLs to send, [{url, reason}] left out)."""
    from ..seo.tabs.internal import category_of, is_html_doc
    site, _ = crawler.site_index()
    by_url = getattr(site, "by_url", None) or {r.url: r for r in getattr(site, "results", []) or []}
    send: List[str] = []
    left: List[Dict[str, str]] = []
    seen = set()
    private = _private_crawl(site, crawler)
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        if private:
            left.append({"url": u, "reason": SIGNED_IN})
            continue
        p = urlsplit(u)
        if (p.hostname or "").lower() != host:
            left.append({"url": u, "reason": f"Another host: IndexNow is set up for {host} only."})
            continue
        if p.scheme not in ("http", "https"):
            left.append({"url": u, "reason": "Not a web page address."})
            continue
        r = by_url.get(u)
        if r is None:
            left.append({"url": u, "reason": "Not in this crawl, so its status is not known."})
            continue
        code = int(getattr(r, "status_code", 0) or 0)
        if code != 200:
            cat = category_of(r)
            left.append({"url": u, "reason": f"Answered {code or 'no response'} ({cat}), not 200."})
            continue
        if not is_html_doc(r):
            left.append({"url": u, "reason": "Not an HTML page."})
            continue
        if not site.indexable.get(u, False):
            why = (getattr(site, "indexability_status", None) or {}).get(u) or "not indexable"
            left.append({"url": u, "reason": f"Not indexable ({why})."})
            continue
        send.append(u)
    return send, left


def submit(crawler, urls: List[str], dry_run: bool = True, source: str = "urls") -> Dict[str, Any]:
    """POST /api/indexnow/submit. A dry run sends nothing and shows what would be sent. Raises ConnectorError
    (plain messages) when IndexNow is not ready."""
    c = connector()
    if not c.is_set_up():
        raise ConnectorError("Not connected - connect IndexNow under Connections.", "invalid")
    if crawler is None or not (getattr(crawler, "results_list", None) or []):
        raise ConnectorError("Crawl the site first: only crawled, indexable 200 pages are sent.", "invalid")
    host = str(c.config().get("host") or "")
    send, left = _eligible(crawler, list(urls or []), host)
    over = send[MAX_URLS:]
    send = send[:MAX_URLS]
    left += [{"url": u, "reason": f"Over the {MAX_URLS:,} URLs of one submission: submit again in a minute."}
             for u in over]
    out: Dict[str, Any] = {"host": host, "count": len(send), "submitted": 0, "status": None, "detail": "",
                           "engines_note": ENGINES_NOTE, "left_out": left[:2000], "left_out_count": len(left),
                           "dry_run": bool(dry_run), "endpoint": urlsplit(c.endpoint()).hostname or "",
                           "source": source, "urls": send[:200] if dry_run else []}
    v = c.verified()
    verified = bool(v.get("ok")) and v.get("host") == host
    if dry_run:
        out["detail"] = (f"{len(send):,} URL{'s' if len(send) != 1 else ''} would be sent to "
                         f"{out['endpoint']}; {len(left):,} left out." +
                         ("" if verified else " Check the key file first (it is not verified yet)."))
        out["verified"] = verified
        return out
    if not verified:
        raise ConnectorError("Check the key file first: IndexNow refuses a key the site does not host "
                             "(Connections > IndexNow > Check key file).", "invalid")
    if not send:
        out["detail"] = "Nothing to send: no URL is an indexable 200 page of the verified host."
        return out
    last = float(c.meta().get("last_submit_ts") or 0)
    wait = MIN_INTERVAL - (time.time() - last)
    if wait > 0:
        raise ConnectorError(f"One submission a minute: try again in {int(wait) + 1} seconds.", "invalid")
    c._set_meta(last_submit_ts=time.time())
    try:
        status, detail = c.submit_urls(send)
    except ConnectorError as e:
        c._set_meta(last_submit={"ok": False, "at": now_iso(), "count": len(send), "status": None,
                                 "detail": str(e)})
        raise
    ok = status in (200, 202)
    out.update(status=status, detail=detail, submitted=len(send) if ok else 0)
    c._set_meta(last_submit={"ok": ok, "at": now_iso(), "count": len(send), "status": status, "detail": detail})
    c.record_delivery(ok, detail)                               # a failed one shows as the "error" state
    if ok:
        c._set_meta(tested_live=True, tested_at=now_iso())      # a real 2xx from the IndexNow service
    return out


register(IndexNow())
