"""
The ARACHNIX Companion browser extension (W6, PLAN4_build.md 10.4): it hands the person's signed-in session for one
site to a crawl, instead of a cookie paste.

  POST /api/extension/session          {cookies, start_url, user_agent?, test?} -> {handoff_id, expires_in, preview,
                                       test}: a hand-off kept in memory only for 10 minutes (id =
                                       secrets.token_urlsafe(32)); preview and test reuse the cookie-paste routes'
                                       own path (_cookie_paste, preview_cookies, test_cookies), values masked
  POST /api/extension/session/read     {handoff_id} -> {start_url, preview, user_agent_present, expires_in}
  POST /api/extension/session/delete   {handoff_id} -> {ok}
  GET  /api/extension/download         the extension's folder as a .zip, built in memory

POST /api/crawl/start (auth.handoff_id) consumes a hand-off once, for the same registrable domain as its start URL
only (a second use: 410; another site: 400); /api/auth/cookies/preview and /test read one without consuming it. The
id travels only in request bodies (never a path or query) and the access log masks "handoff" / "handoff_id".
chrome-extension:// origins pass the same-origin rule on these routes only (web/agents.same_origin_ok).
"""
import io
import os
import secrets
import threading
import time
import zipfile
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from web import agents as _agents

router = APIRouter()
TTL = 10 * 60
MAX_HANDOFFS = 200
_handoffs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
GONE = "This sign-in hand-off was already used or has expired. Click the ARACHNIX Companion button again."


def _A():
    import web.app as A
    return A


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = None
    return body if isinstance(body, dict) else {}


def _same_origin(request: Request) -> None:
    if not _A().access_key():
        _agents.require_same_origin(request)


def _prune(now: float) -> None:
    for k in [k for k, v in _handoffs.items() if v["expires"] <= now]:
        _handoffs.pop(k, None)
    while len(_handoffs) > MAX_HANDOFFS:
        _handoffs.pop(next(iter(_handoffs)))


def _get(handoff_id: str, consume: bool = False) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        _prune(now)
        rec = _handoffs.get(str(handoff_id or ""))
        if rec is None:
            raise HTTPException(status_code=410, detail=GONE)
        if consume:
            _handoffs.pop(str(handoff_id), None)
        return dict(rec)


def _domain(url: str) -> str:
    from crawler.url_utils import get_domain
    try:
        return (get_domain(url) or "").lower()
    except Exception:
        return ""


def _preview(A, raw: str, start: str, secrets_: list) -> Dict[str, Any]:
    from crawler.auth import preview_cookies
    try:
        out = preview_cookies(raw, start)
    except Exception as e:
        return {"error": f"The cookies could not be read ({type(e).__name__}).", "cookies": [], "count": 0}
    for row in out.get("cookies") or []:
        row["value"] = "***"
    out["warnings"] = [A.mask_secrets(w, secrets_) for w in out.get("warnings") or []]
    out["error"] = A.mask_secrets(out.get("error") or "", secrets_)
    return out


@router.post("/api/extension/session")
async def create_session(request: Request):
    _same_origin(request)
    A = _A()
    b = await _body(request)
    req = A.CookiePasteRequest(cookies=b.get("cookies"), start_url=b.get("start_url"),
                               user_agent=b.get("user_agent"))
    raw, start, secret_values = A._cookie_paste(req, need_url=True)
    if not raw.strip():
        A._bad("No cookies were sent for this site.")
    ua = str(b.get("user_agent") or "").strip()
    if ua:
        ua = A._test_user_agent(ua) or ""
    preview = _preview(A, raw, start, secret_values)
    test = None
    if b.get("test"):
        test = A.cookies_test(A.CookiePasteRequest(cookies=raw, start_url=start, user_agent=ua or None))
    hid = secrets.token_urlsafe(32)
    now = time.time()
    with _lock:
        _prune(now)
        _handoffs[hid] = {"cookies": raw, "start_url": start, "user_agent": ua, "expires": now + TTL,
                          "domain": _domain(start), "preview": preview}
    return Response(content=A.json.dumps({"handoff_id": hid, "expires_in": TTL, "preview": preview, "test": test}),
                    media_type="application/json", headers={"Cache-Control": "no-store"})


@router.post("/api/extension/session/read")
async def read_session(request: Request):
    _same_origin(request)
    b = await _body(request)
    rec = _get(str(b.get("handoff_id") or ""))
    return {"start_url": rec["start_url"], "preview": rec["preview"], "user_agent_present": bool(rec["user_agent"]),
            "expires_in": max(0, int(rec["expires"] - time.time()))}


@router.post("/api/extension/session/delete")
async def delete_session(request: Request):
    _same_origin(request)
    b = await _body(request)
    with _lock:
        _handoffs.pop(str(b.get("handoff_id") or ""), None)
    return {"ok": True}


@router.get("/api/extension/download")
def download_extension():
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extensions", "chrome")
    if not os.path.isdir(root):
        raise HTTPException(status_code=404, detail="The extension is not part of this server's files.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d, dirs, files in os.walk(root):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", "node_modules")]
            for f in sorted(files):
                path = os.path.join(d, f)
                z.write(path, os.path.join("arachnix-companion", os.path.relpath(path, root)))
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="arachnix-companion.zip"'})


# ---------------------------------------------------------------------------------------------
# Used by web/app.py
# ---------------------------------------------------------------------------------------------
def apply_to_crawl(req: Any) -> None:
    """POST /api/crawl/start with auth.handoff_id: consumes the hand-off (same registrable domain only) and turns it
    into a cookie sign-in; the id itself is dropped from the request."""
    auth = getattr(req, "auth", None)
    hid = str(getattr(auth, "handoff_id", "") or "") if auth is not None else ""
    if not hid:
        return
    start = str(getattr(req, "url", "") or "")
    with _lock:
        _prune(time.time())
        rec = _handoffs.get(hid)
        if rec is None:
            raise HTTPException(status_code=410, detail=GONE)
        if rec["domain"] and _domain(start if "://" in start else "https://" + start) != rec["domain"]:
            raise HTTPException(status_code=400, detail="This sign-in hand-off belongs to another website: it can "
                                                        "only be used for a crawl of the site it came from.")
        _handoffs.pop(hid, None)
    auth.enabled = True
    auth.auth_type = "cookies"
    auth.cookies = rec["cookies"]
    if rec["user_agent"] and hasattr(auth, "user_agent") and not getattr(auth, "user_agent", None):
        auth.user_agent = rec["user_agent"]
    auth.handoff_id = None


def apply_to_paste(req: Any) -> None:
    """POST /api/auth/cookies/preview | test with handoff_id and no cookies: reads the hand-off (not consumed)."""
    hid = str(getattr(req, "handoff_id", "") or "")
    if not hid or getattr(req, "cookies", None):
        return
    rec = _get(hid)
    req.cookies = rec["cookies"]
    if not (getattr(req, "start_url", None) or getattr(req, "url", None)):
        req.start_url = rec["start_url"]
    if rec["user_agent"] and not getattr(req, "user_agent", None):
        req.user_agent = rec["user_agent"]
    req.handoff_id = None


def install(app) -> None:
    if getattr(app.state, "extension_installed", False):
        return
    app.include_router(router)
    app.state.extension_installed = True
