"""
Ask ARACHNIX (W3) and the content endpoints (W4), PLAN4_build.md 10.4:

  W3  GET /api/assistant/status, POST /api/assistant/chat, GET /api/assistant/turn/{id}, POST /api/assistant/confirm,
      POST /api/assistant/stop, DELETE /api/assistant/conversations/{id}
      Polled chat (no streaming), conversations in memory only, 20 turns per viewer per hour on top of the
      "assistant" AI budget. The assistant calls ARACHNIX's own tools in-process (actor "assistant": never admin);
      write and custom-tool calls wait for the person's confirmation (the card comes from the server's `pending`).
  W4  GET / PUT /api/ai/prompts, POST /api/ai/run, GET /api/ai/run/{id}, POST /api/ai/run/{id}/stop,
      GET / PUT /api/spelling/dictionary, POST /api/embeddings/run, GET /api/embeddings/run/{id},
      GET /api/crawl/semantic-search, GET /api/crawl/clusters, GET /api/crawl/ngrams

State-changing routes pass the same-origin rule when the server has no access key. web/app.py calls install(app).
"""
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request

from web import agents as _agents

router = APIRouter()
TURNS_PER_HOUR = 20
_turns: Dict[str, Deque[float]] = {}
_turns_lock = threading.Lock()


def _A():
    import web.app as A
    return A


def _same_origin(request: Request) -> None:
    if not _A().access_key():
        _agents.require_same_origin(request)


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = None
    return body if isinstance(body, dict) else {}


def _crawler():
    job = _A().session.job
    return job.crawler if job is not None else None


def _plain(e: Exception, status: int = 400) -> HTTPException:
    msg = str(getattr(e, "message", "") or e) or type(e).__name__
    job = _A().session.job
    return HTTPException(status_code=status, detail=(job.mask(msg) if job is not None else msg)[:600])


def _viewer(request: Request) -> str:
    """Who is chatting: the client's address group (one per PC behind the same key)."""
    try:
        return "viewer:" + str(_A()._client_address(request.scope))[:60]
    except Exception:
        return "viewer:unknown"


def _assistant_api():
    from crawler.mcp import tools as T
    return T.InProcessApi(_A().app, _agents.INTERNAL_SECRET, "assistant", _agents.public_url())


def _assistant():
    from crawler.connectors import assistant
    return assistant


# ---------------------------------------------------------------------------------------------
# W3: Ask ARACHNIX
# ---------------------------------------------------------------------------------------------
@router.get("/api/assistant/status")
def assistant_status(request: Request):
    st = _assistant().status(_assistant_api(), allow_write=True)
    with _turns_lock:
        used = [t for t in _turns.get(_viewer(request), ()) if time.time() - t < 3600]
    st["turns_left"] = max(0, TURNS_PER_HOUR - len(used))
    st["turns_limit"] = TURNS_PER_HOUR
    return st


@router.post("/api/assistant/chat")
async def assistant_chat(request: Request):
    _same_origin(request)
    b = await _body(request)
    viewer = _viewer(request)
    now = time.time()
    with _turns_lock:
        q = _turns.setdefault(viewer, deque())
        while q and now - q[0] >= 3600:
            q.popleft()
        if len(q) >= TURNS_PER_HOUR:
            wait = int(3600 - (now - q[0])) + 1
            raise HTTPException(status_code=429, headers={"Retry-After": str(wait)},
                                detail=f"You asked {TURNS_PER_HOUR} questions in the last hour. Try again in "
                                       f"{max(1, wait // 60)} minutes.")
        q.append(now)
        while len(_turns) > 500:
            _turns.pop(next(iter(_turns)))
    a = _assistant()
    try:
        turn = a.start_turn(b.get("conversation_id") or None, str(b.get("message") or ""), _assistant_api(),
                            viewer, allow_write=True)
    except Exception as e:
        with _turns_lock:
            if _turns.get(viewer):
                _turns[viewer].pop()          # a refused question is not counted
        raise _plain(e)
    return {"conversation_id": turn.conversation_id, "turn_id": turn.id}


@router.get("/api/assistant/turn/{turn_id}")
def assistant_turn(turn_id: str, request: Request, after: int = 0):
    turn = _assistant().get_turn(turn_id, viewer=_viewer(request))
    if turn is None:
        raise HTTPException(status_code=404, detail="This answer is no longer available (conversations are kept "
                                                    "for 2 hours, in memory only).")
    return turn.to_dict(after=max(0, int(after or 0)))


@router.post("/api/assistant/confirm")
async def assistant_confirm(request: Request):
    _same_origin(request)
    b = await _body(request)
    try:
        turn = _assistant().confirm(str(b.get("turn_id") or ""), str(b.get("call_id") or ""),
                                    bool(b.get("approve")), viewer=_viewer(request))
    except Exception as e:
        raise _plain(e)
    return turn.to_dict()


@router.post("/api/assistant/stop")
async def assistant_stop(request: Request):
    _same_origin(request)
    b = await _body(request)
    _assistant().stop(str(b.get("turn_id") or ""), viewer=_viewer(request))
    return {"ok": True}


@router.delete("/api/assistant/conversations/{conversation_id}")
def assistant_forget(conversation_id: str, request: Request):
    _same_origin(request)
    _assistant().forget(conversation_id, viewer=_viewer(request))
    return {"ok": True}


# ---------------------------------------------------------------------------------------------
# W4: AI tab prompts and runs
# ---------------------------------------------------------------------------------------------
@router.get("/api/ai/prompts")
def ai_prompts():
    from crawler.ai import prompts
    return prompts.public_view(_A().REPORTS_DIR)


@router.put("/api/ai/prompts")
async def ai_prompts_save(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.ai import prompts
    try:
        prompts.save_prompts(b.get("prompts"), _A().REPORTS_DIR)
    except Exception as e:
        raise _plain(e)
    return prompts.public_view(_A().REPORTS_DIR)


@router.post("/api/ai/run")
async def ai_run(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.ai import runner
    try:
        return runner.start_run(_crawler(), prompt_ids=b.get("prompt_ids") or None, max_pages=b.get("max_pages"),
                                reports_dir=_A().REPORTS_DIR)
    except Exception as e:
        raise _plain(e)


@router.get("/api/ai/run/{job_id}")
def ai_run_status(job_id: str):
    from crawler.ai import runner
    st = runner.run_status(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="No such AI run.")
    return st


@router.post("/api/ai/run/{job_id}/stop")
def ai_run_stop(job_id: str, request: Request):
    _same_origin(request)
    from crawler.ai import runner
    st = runner.stop_run(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="No such AI run.")
    return st


# ---------------------------------------------------------------------------------------------
# W4: spelling dictionary
# ---------------------------------------------------------------------------------------------
def _site_host(site_param: str) -> str:
    raw = (site_param or "").strip() or str(getattr(_crawler(), "start_url", "") or "")
    host = (urlsplit(raw if "//" in raw else "//" + raw).hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Say which site's dictionary (site=example.com), or crawl one.")
    return host


@router.get("/api/spelling/dictionary")
def spelling_dictionary(site: str = ""):
    from crawler.seo import spelling
    host = _site_host(site)
    out = {"site": host, "words": spelling.load_dictionary(host, _A().REPORTS_DIR), "languages": spelling.languages(),
           "learned": []}
    job = _A().session.job
    if job is not None and job.crawler is not None and \
            (urlsplit(job.crawler.start_url).hostname or "").lower() == host:
        s, index = _A()._site_index(job)
        if s is not None:
            try:
                sw = spelling.site_words(s, _A().REPORTS_DIR)
                as_list = lambda d: [{"word": w, "pages": int(n)} for w, n in  # noqa: E731
                                     sorted((d or {}).items(), key=lambda x: (-x[1], x[0]))]
                out["learned"] = as_list(sw.get("learned"))
                out["kept"] = as_list(sw.get("kept"))
                out["summary"] = {k: sw.get(k) for k in ("threshold", "pages_checked") if k in sw}
            except Exception:
                pass
    return out


@router.put("/api/spelling/dictionary")
async def spelling_dictionary_save(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.seo import spelling
    host = _site_host(str(b.get("site") or ""))
    words = spelling.update_dictionary(host, add=b.get("add"), remove=b.get("remove"), reports_dir=_A().REPORTS_DIR)
    job = _A().session.job
    if job is not None and job.crawler is not None:
        try:
            job.crawler._index_cache = None        # the Spelling Errors counts follow the new dictionary
        except Exception:
            pass
    return {"site": host, "words": words}


# ---------------------------------------------------------------------------------------------
# W4: embeddings, semantic search, clusters, n-grams
# ---------------------------------------------------------------------------------------------
@router.post("/api/embeddings/run")
async def embeddings_run(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.ai import embeddings
    try:
        return embeddings.start_run(_crawler(), max_pages=b.get("max_pages"))
    except Exception as e:
        raise _plain(e)


@router.get("/api/embeddings/run/{job_id}")
def embeddings_status(job_id: str):
    from crawler.ai import embeddings
    st = embeddings.run_status(job_id)
    if st is None:
        raise HTTPException(status_code=404, detail="No such embeddings run.")
    return st


def _site():
    job = _A().session.job
    s, index = _A()._site_index(job) if job is not None else (None, None)
    if s is None:
        raise HTTPException(status_code=400, detail="Crawl a site first.")
    return s


@router.get("/api/crawl/semantic-search")
def semantic_search(q: str = "", limit: int = 20, method: Optional[str] = None):
    if not q.strip():
        raise HTTPException(status_code=400, detail="Type what to search for.")
    from crawler.seo import semantic
    return semantic.search(_site(), q.strip()[:300], limit=max(1, min(int(limit or 20), 100)), method=method or None)


@router.get("/api/crawl/clusters")
def topic_clusters(method: Optional[str] = None):
    from crawler.seo import semantic
    return semantic.public(semantic.clusters(_site(), method=method or None))


@router.get("/api/crawl/ngrams")
def page_ngrams(url: str = "", n: int = 2, content_area: bool = True, limit: int = 100, skip_stop_words: bool = False):
    if not url:
        raise HTTPException(status_code=400, detail="Choose a page (url=).")
    _site()
    from crawler.seo import ngrams
    return ngrams.ngrams(_crawler(), url, n=max(1, min(int(n or 2), 6)), content_area=bool(content_area),
                         limit=max(1, min(int(limit or 100), 1000)), skip_stop_words=bool(skip_stop_words))


def install(app) -> None:
    if getattr(app.state, "ai_routes_installed", False):
        return
    app.include_router(router)
    app.state.ai_routes_installed = True
