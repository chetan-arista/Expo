"""
Trend history (W10) and the small endpoints (W11), PLAN4_build.md 10.4:

  GET  /api/crawls/trend?site=&limit=30   one point per saved crawl of the site, oldest first: {id, at, urls, pages,
       status: {2xx, 3xx, 4xx, 5xx, blocked, error} (the saved crawl's own counts: they add up to its URLs),
       status_labels, indexable | null, issues_by_type | null, health_score, reason?}. A crawl saved without a snapshot
       is a gap (null + "Not recorded for this crawl"), never 0.
  POST /api/crawl/segments/test           {url, rules?} -> {url, segment, in_crawl}
  POST /api/profiles/compare              {a, b} -> {changes: [{option, a, b}]}
  POST /api/robots/test                   {robots_txt?, url, user_agent} -> {allowed, rule, line, group, path, source}
  GET  /api/custom-js/snippets            {snippets}   (also /api/config/js-snippets)
  POST /api/crawl/extract-preview         {url, source, extractors, searches} -> the rules' values on one crawled page
       (U9 point-and-click extractor; the crawl's own code on the stored HTML)
"""
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()
STATUS_KEYS = ("2xx", "3xx", "4xx", "5xx", "blocked", "error")
STATUS_LABELS = {"2xx": "Success (2xx)", "3xx": "Redirect (3xx)", "4xx": "Broken (4xx)", "5xx": "Server error (5xx)",
                 "blocked": "Blocked", "error": "No Response"}
NOT_RECORDED = "Not recorded for this crawl"
_snap_cache: Dict[str, Dict[str, Any]] = {}
_snap_lock = threading.Lock()


def _A():
    import web.app as A
    return A


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        body = None
    return body if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------------------------
# W10: trend
# ---------------------------------------------------------------------------------------------
def _snapshot_facts(snapshot_id: str) -> Optional[Dict[str, Any]]:
    """{"indexable", "issues_by_type"} of one snapshot, cached per snapshot id (snapshots never change)."""
    with _snap_lock:
        if snapshot_id in _snap_cache:
            return _snap_cache[snapshot_id]
    from crawler.seo.compare import load_snapshot
    snap = load_snapshot(snapshot_id, _A().REPORTS_DIR)
    if not isinstance(snap, dict):
        return None
    rows = snap.get("rows") or {}
    indexable = sum(1 for r in (rows.values() if isinstance(rows, dict) else rows)
                    if isinstance(r, dict) and r.get("indexability") == "Indexable")
    counts = snap.get("counts") or {}
    by_type: Dict[str, Dict[str, int]] = {}
    for key, f in (snap.get("filters") or {}).items():
        ty = (f or {}).get("issue_type")
        n = int(counts.get(key) or 0)
        if ty and n:
            d = by_type.setdefault(ty, {"issues": 0, "urls": 0})
            d["issues"] += 1
            d["urls"] += n
    out = {"indexable": indexable, "issues_by_type": by_type}
    with _snap_lock:
        _snap_cache[snapshot_id] = out
        while len(_snap_cache) > 500:
            _snap_cache.pop(next(iter(_snap_cache)))
    return out


@router.get("/api/crawls/trend")
def crawl_trend(site: str = "", limit: int = 30):
    A = _A()
    if not site.strip():
        job = A.session.job
        site = str(getattr(getattr(job, "crawler", None), "start_url", "") or "")
    if not site.strip():
        raise HTTPException(status_code=400, detail="Say which site (site=example.com), or crawl one first.")
    from crawler.store import list_crawls
    crawls = list_crawls(A.REPORTS_DIR, site=site)[:max(1, min(int(limit or 30), 200))]
    points: List[Dict[str, Any]] = []
    for m in reversed(crawls):                 # oldest first
        cats = m.get("categories") or {}
        status = {k: int(cats.get(k) or 0) for k in STATUS_KEYS}
        p = {"id": m.get("id"), "name": m.get("name") or "", "at": m.get("started_at") or m.get("saved_at") or "",
             "urls": int(m.get("urls") or 0), "pages": int(m.get("pages") or 0), "status": status,
             "status_labels": STATUS_LABELS, "health_score": m.get("health_score"),
             "partial": str(m.get("complete") or "") in ("partial", "stopped") or bool(m.get("limit_reached")),
             "indexable": None, "issues_by_type": None}
        facts = _snapshot_facts(m["snapshot_id"]) if m.get("snapshot_id") else None
        if facts is None:
            p["reason"] = NOT_RECORDED
        else:
            p.update(facts)
        if sum(status.values()) != p["urls"]:
            p["note"] = "This crawl's status counts were saved by an older version and do not add up to its URLs."
        points.append(p)
    host = (urlsplit(site if "//" in site else "//" + site).hostname or site).lower()
    return {"site": host, "points": points, "status_labels": STATUS_LABELS}


# ---------------------------------------------------------------------------------------------
# W11: small endpoints
# ---------------------------------------------------------------------------------------------
@router.post("/api/crawl/segments/test")
async def segment_test(request: Request):
    """Which segment a URL of the loaded crawl falls into: the given rules (unsaved), else the saved ones."""
    A = _A()
    b = await _body(request)
    url = str(b.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Enter a URL of the crawl to test.")
    job = A.session.job
    site, index = A._site_index(job) if job is not None else (None, None)
    if site is None:
        raise HTTPException(status_code=400, detail="Crawl a site first: segments are tested on its URLs.")
    from crawler import segments
    rules = b.get("rules")
    if rules is None:
        try:
            rules = A._segments_store_or_400("").list()
        except Exception:
            rules = []
    clean, errors = segments.validate_segments(rules)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(f"Segment {k}: {', '.join(v.values())}"
                                                             for k, v in list(errors.items())[:3]))
    cands = A._url_candidates(url)
    row = next((site.by_url[c] for c in cands if c in site.by_url), None)
    if row is None:
        return {"url": url, "in_crawl": False, "segment": None,
                "reason": "This URL is not in the loaded crawl."}
    got = segments.evaluate(site, index, clean, rows=[row])
    return {"url": row.url, "in_crawl": True, "segment": got.get(row.url) or None}


@router.post("/api/profiles/compare")
async def profile_compare(request: Request):
    A = _A()
    b = await _body(request)
    st = A._settings()
    pa, pb = st.load_profile(str(b.get("a") or ""), A.REPORTS_DIR), st.load_profile(str(b.get("b") or ""), A.REPORTS_DIR)
    if pa is None or pb is None:
        raise HTTPException(status_code=404, detail="Choose two saved profiles.")
    oa, ob = pa.get("options") or {}, pb.get("options") or {}
    changes = [{"option": k, "a": oa.get(k), "b": ob.get(k)} for k in sorted(set(oa) | set(ob)) if oa.get(k) != ob.get(k)]
    return {"a": pa.get("name"), "b": pb.get("name"), "changes": changes}


def _live_robots(job, url: str) -> str:
    """The live robots.txt of the URL's host, fetched once with the loaded crawl's own session (its address guard and
    headers), for a host of that crawl only."""
    crawler = getattr(job, "crawler", None)
    if crawler is None:
        raise HTTPException(status_code=400, detail="Paste the robots.txt text, or crawl the site first.")
    p = urlsplit(url if "//" in url else "https://" + url)
    try:
        from crawler.url_utils import is_internal_url
        ours = is_internal_url(f"{p.scheme}://{p.netloc}/", crawler.base_domain,
                               include_subdomains=True, allowed_domains=getattr(crawler, "allowed_domains", None))
    except Exception:
        ours = False
    if not ours:
        raise HTTPException(status_code=400, detail="That URL is not on the crawled site: paste its robots.txt text.")
    try:
        resp = crawler.session.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10, allow_redirects=True)
        return resp.text[:500 * 1024] if resp.status_code == 200 else ""
    except Exception:
        raise HTTPException(status_code=400, detail="The live robots.txt could not be read: paste its text instead.")


@router.post("/api/robots/test")
async def robots_test(request: Request):
    """The robots.txt tester: the given text, else the custom robots.txt of the loaded crawl, else its live file."""
    A = _A()
    b = await _body(request)
    url = str(b.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Enter the URL to test.")
    ua = str(b.get("user_agent") or "Googlebot").strip()[:200]
    text, source = b.get("robots_txt"), "given"
    if text is None:
        job = A.session.job
        cfg = getattr(getattr(job, "crawler", None), "config", None)
        text, source = str(getattr(cfg, "custom_robots_txt", "") or ""), "custom"
        if not text.strip():
            text, source = _live_robots(job, url), "live"
    from crawler import robots
    out = robots.test(str(text or ""), url, ua)
    out["source"] = source
    return out


@router.get("/api/custom-js/snippets")
@router.get("/api/config/js-snippets")
def js_snippets():
    from crawler.seo import js_snippets as lib
    return {"snippets": lib.snippets()}


# ---------------------------------------------------------------------------------------------
# W8: scheduled runs' files and the equivalent command line
# ---------------------------------------------------------------------------------------------
def _scheduler():
    holder = _A()._scheduler_holder or {}
    sch = holder.get("scheduler") or holder.get("passive")
    if sch is None:
        raise HTTPException(status_code=409, detail=(_A()._scheduler_holder or {}).get("reason") or
                            "The scheduler is not running on this server.")
    return sch


def _run_files(task_id: str, run_id: str):
    import os
    run = next((r for r in _scheduler().history(task_id, limit=1000) if r.get("id") == run_id), None)
    if run is None:
        raise HTTPException(status_code=404, detail="No such run of this task.")
    root = os.path.realpath(os.path.join(_A().REPORTS_DIR, "scheduled"))
    out = []
    for path in run.get("files") or []:
        real = os.path.realpath(str(path))
        if real.startswith(root + os.sep) and os.path.isfile(real):
            out.append(real)
    return run, out


@router.get("/api/schedules/{task_id}/runs/{run_id}/files")
def schedule_run_files(task_id: str, run_id: str):
    import os
    run, files = _run_files(task_id, run_id)
    return {"run": run_id, "status": run.get("status"), "files": [{"name": os.path.basename(p), "bytes": os.path.getsize(p)}
                                                                  for p in files],
            "note": "" if files else "This run kept no export files (or the retention rules removed them)."}


@router.get("/api/schedules/{task_id}/runs/{run_id}/files/{name}")
def schedule_run_file(task_id: str, run_id: str, name: str):
    import os
    from fastapi.responses import FileResponse
    _run, files = _run_files(task_id, run_id)
    path = next((p for p in files if os.path.basename(p) == name), None)
    if path is None:
        raise HTTPException(status_code=404, detail="No such file in this run.")
    return FileResponse(path, filename=name)


@router.get("/api/schedules/{task_id}/command")
def schedule_command(task_id: str):
    """The command line that runs the same crawl once by hand (cli.py), from the task's start, profile and exports."""
    t = _scheduler().get(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="No such task.")
    st, ex = t.get("start") or {}, t.get("exports") or {}
    parts = ["python", "cli.py"]
    notes = []
    if (st.get("mode") or "spider") == "list":
        parts.append("--list urls.txt")
        notes.append("Put the task's URL list in urls.txt (one per line).")
    else:
        parts.append(repr(str(st.get("url") or "")).replace("'", '"'))
    if st.get("profile"):
        parts.append(f'--profile "{st["profile"]}"')
    if st.get("options"):
        parts.append("--config task_options.json")
        notes.append("Save the task's crawl options as task_options.json (File > Scheduling > Export).")
    for item in (ex.get("items") or [])[:20]:
        parts.append(f"--report {item}")
    if ex.get("format") == "csv":
        parts.append("--format csv")
    if ex.get("pdf"):
        parts.append("--pdf")
    if ex.get("sitemap"):
        parts.append("--sitemap")
    if ex.get("save_crawl"):
        parts.append("--save-crawl")
    login = (st.get("login") or {})
    if login.get("type") in ("cookies", "header"):
        notes.append(f"The sign-in is read from the server variable {login.get('env')}: set it in the shell too.")
    return {"command": " ".join(parts), "notes": notes}


# ---------------------------------------------------------------------------------------------
# P22 / W11: server log upload (streamed to a temp file, at most 200 MB; deleted after parsing)
# ---------------------------------------------------------------------------------------------
_logs_job: Dict[str, Any] = {"state": "idle"}
_logs_lock = threading.Lock()


@router.post("/api/logs/upload")
async def logs_upload(request: Request, verify: bool = True):
    import os
    import tempfile
    A = _A()
    if not A.access_key():
        from web import agents
        agents.require_same_origin(request)
    job = A.session.job
    crawler = getattr(job, "crawler", None)
    if crawler is None or not getattr(crawler, "results_list", None):
        raise HTTPException(status_code=400, detail="Crawl the site first: the log is matched to its URLs.")
    with _logs_lock:
        if _logs_job.get("state") == "running":
            raise HTTPException(status_code=409, detail="A log file is being read: wait for it to finish.")
    from crawler.logs import MAX_BYTES
    fd, path = tempfile.mkstemp(prefix="arachnix_log_", suffix=".log")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise HTTPException(status_code=413, detail="The log file is larger than 200 MB: upload one day or "
                                                                "one week at a time (or a .gz).")
                out.write(chunk)
    except HTTPException:
        os.remove(path)
        raise
    if not size:
        os.remove(path)
        raise HTTPException(status_code=400, detail="The upload was empty.")

    def work():
        from crawler import integrations
        from crawler.logs import analyse
        try:
            def progress(n):
                with _logs_lock:
                    _logs_job["lines"] = n
            entry = analyse(path, crawler, verify=verify, progress=progress)
            integrations.put_result(crawler, "logs", entry, merge=False)
            with _logs_lock:
                _logs_job.update(state="done", lines=entry.get("lines"), parsed=entry.get("parsed"),
                                 bot_hits=entry.get("bot_hits"), reason=entry.get("reason", ""))
        except Exception as e:
            with _logs_lock:
                _logs_job.update(state="error", reason=f"The log file could not be read ({type(e).__name__}).")
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
    with _logs_lock:
        _logs_job.clear()
        _logs_job.update(state="running", bytes=size, lines=0)
    threading.Thread(target=work, daemon=True, name="log-upload").start()
    return {"job_id": "logs", "state": "running", "bytes": size}


@router.get("/api/logs/status")
def logs_status():
    with _logs_lock:
        return dict(_logs_job)


@router.delete("/api/logs")
def logs_delete(request: Request):
    A = _A()
    if not A.access_key():
        from web import agents
        agents.require_same_origin(request)
    job = A.session.job
    crawler = getattr(job, "crawler", None)
    data = getattr(crawler, "integrations", None)
    if isinstance(data, dict) and data.pop("logs", None) is not None:
        try:
            from crawler import integrations
            integrations._invalidate(crawler)
        except Exception:
            pass
    with _logs_lock:
        _logs_job.clear()
        _logs_job["state"] = "idle"
    return {"ok": True}


# ---------------------------------------------------------------------------------------------
# U9: Custom Extraction / Custom Search preview on one crawled page (the point-and-click extractor)
# ---------------------------------------------------------------------------------------------
PREVIEW_VALUES_MAX = 20
PREVIEW_VALUE_CHARS = 2000


@router.post("/api/crawl/extract-preview")
async def extract_preview(request: Request):
    """{url, source: "original" | "rendered", extractors: [rule], searches: [rule]} -> {url, source, extraction:
    {name: [values]}, counts: {name: n}, search: {name: occurrences}, problems, reason?}. The rules run on the crawl's
    stored HTML of that page with the code the crawl uses (custom_rules_on_html), after the logins' values are masked,
    so the preview is what the crawl extracts. At most 10 rules of each kind; the first 20 values of each are sent."""
    b = await _body(request)
    A = _A()
    job = A.session.job
    if job is None or job.crawler is None:
        raise HTTPException(status_code=404, detail="No crawl data available.")
    site, _ = A._site_index(job)
    r = A._find_crawled(site, str(b.get("url") or "").strip()) if site is not None else None
    if r is None:
        raise HTTPException(status_code=404, detail="Pick a page of the crawl.")
    from crawler.seo.extract import MAX_CUSTOM_RULES, ExtractOptions, custom_rules_on_html
    source = "rendered" if str(b.get("source") or "").strip().lower() == "rendered" else "original"
    out: Dict[str, Any] = {"url": r.url, "source": source, "extraction": {}, "counts": {}, "search": {}, "problems": []}
    if source == "rendered":
        rstore = A._safe(lambda: A._rendered_store(job), None)
        html = A._safe(lambda: rstore.get(r.url), None) if rstore is not None else None
        why = A._safe(lambda: A._not_rendered_reason(job, r.url), "") or "This page was not rendered in Chrome."
    else:
        store = getattr(job.crawler, "source_store", None)
        html = A._safe(lambda: store.get(r.url), None) if store is not None else None
        why = ("The page HTML was not kept for this crawl (Store HTML is off)." if store is None else
               "Only HTML pages of the crawled site are kept; this URL is not one.")
    if html is None:
        out["reason"] = why
        return out
    rules = lambda k: [x for x in (b.get(k) or []) if isinstance(x, dict)][:MAX_CUSTOM_RULES] if isinstance(b.get(k), list) else []
    opts = ExtractOptions.from_config(None, custom_searches=rules("searches"), custom_extractors=rules("extractors"),
                                      top_terms=False)
    out["problems"] = [str(p) for p in opts.problems]
    if not (opts.searches or opts.extractors):
        return out
    from starlette.concurrency import run_in_threadpool     # the parse and the user's rules: off the event loop
    search, extraction = await run_in_threadpool(lambda: custom_rules_on_html(job.mask(html), opts))
    out["search"] = dict(search)
    for name, values in extraction.items():
        out["counts"][name] = len(values)
        out["extraction"][name] = [str(v)[:PREVIEW_VALUE_CHARS] for v in values[:PREVIEW_VALUES_MAX]]
    return out


def install(app) -> None:
    if getattr(app.state, "v2_misc_installed", False):
        return
    app.include_router(router)
    app.state.v2_misc_installed = True
