"""
Connections hub endpoints (Build 4 W2, PLAN4_build.md 10.4): connectors, custom MCP servers and their tools, alert
rules, ticket pushes, IndexNow, link metrics, grammar (LanguageTool), Green Web and Google Sheets exports, plus the
after-crawl hook (crawl events to Slack / Teams / Discord / webhooks and the alert rules).

Writes that change the server (connect, test, disconnect, custom MCP, tools, alerts, link-metrics fetches that spend
paid units) are admin-only (web/app.py _require_admin: never an AI agent); every other state-changing route passes the
same-origin rule when the server has no access key. Secrets never come back: Connector.public() shows {"set": bool}.
web/app.py calls install(app) at its end and after_crawl(job) from its follow-up work.
"""
import os
import threading
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from web import agents as _agents

router = APIRouter()


def _A():
    import web.app as A
    return A


def _admin(request: Request) -> None:
    _A()._require_admin(request)


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


def _job():
    return _A().session.job


def _mask(text: Any) -> str:
    job = _job()
    return job.mask(text) if job is not None else str(text or "")


def _plain(e: Exception) -> HTTPException:
    """ConnectorError / IntegrationError / ValueError -> 400 with the plain message (already free of secrets)."""
    msg = str(getattr(e, "message", "") or e) or type(e).__name__
    return HTTPException(status_code=400, detail=_mask(msg)[:600])


def _connectors():
    from crawler import connectors
    return connectors


def _get(cid: str):
    c = _connectors().get(cid)
    if c is None:
        raise HTTPException(status_code=404, detail="No such connection.")
    return c


def _status_dict(st) -> Dict[str, Any]:
    from dataclasses import asdict, is_dataclass
    return asdict(st) if is_dataclass(st) else dict(st or {})


# ---------------------------------------------------------------------------------------------
# Connectors
# ---------------------------------------------------------------------------------------------
@router.get("/api/connectors")
def list_connectors(request: Request):
    ok, why = _A().settings_editable(request)
    items = []
    for c in _connectors().all_connectors():
        try:
            items.append(c.public())
        except Exception as e:
            items.append({"id": c.id, "name": getattr(c, "name", c.id),
                          "status": {"state": "error", "detail": f"Could not be read ({type(e).__name__})."}})
    from crawler.connectors import registry
    storage = _A().REPORTS_DIR
    temp = os.path.abspath(storage) == os.path.abspath(__import__("tempfile").gettempdir())
    return {"editable": ok, "why": why, "connectors": items, "load_errors": registry.load_errors(),
            "storage_warning": ("Connections are kept in the system temp folder, which Windows may clean: set "
                                "CRAWLER_REPORTS_DIR to a lasting folder (DEPLOY.md, section 1).") if temp else ""}


@router.get("/api/connectors/{cid}")
def get_connector(cid: str):
    return _get(cid).public()


@router.post("/api/connectors/custom-mcp")
async def add_custom_mcp(request: Request):
    _admin(request)
    b = await _body(request)
    from crawler.connectors import custom_mcp
    try:
        c = custom_mcp.create(str(b.get("name") or ""), str(b.get("url") or ""),
                              header_name=str(b.get("header_name") or ""), header_value=str(b.get("header_value") or ""),
                              allow_private_network=bool(b.get("allow_private_network")))
    except Exception as e:
        raise _plain(e)
    return {"id": (getattr(c, "config", None) or {}).get("id", ""), "status": _status_dict(c)}


@router.post("/api/connectors/{cid}/connect")
async def connect_connector(cid: str, request: Request):
    _admin(request)
    c = _get(cid)
    b = await _body(request)
    try:
        st = c.connect(b.get("config") if isinstance(b.get("config"), dict) else {},
                       b.get("secrets") if isinstance(b.get("secrets"), dict) else {})
    except Exception as e:
        raise _plain(e)
    return _status_dict(st)


@router.post("/api/connectors/{cid}/test")
def test_connector(cid: str, request: Request):
    _admin(request)
    res = _get(cid).test()
    return _status_dict(res)


@router.post("/api/connectors/{cid}/disconnect")
def disconnect_connector(cid: str, request: Request):
    _admin(request)
    try:
        st = _get(cid).disconnect()
    except Exception as e:
        raise _plain(e)
    return _status_dict(st)


@router.get("/api/connectors/{cid}/tools")
def connector_tools(cid: str, refresh: bool = False):
    if not cid.startswith("custom_mcp:"):
        raise HTTPException(status_code=404, detail="Only custom MCP servers have tools.")
    _get(cid)
    from crawler.connectors import custom_mcp
    try:
        return {"tools": custom_mcp.list_tools(cid, refresh=refresh),
                "note": "Enabled tools can be used by everyone who can open this dashboard."}
    except Exception as e:
        raise _plain(e)


@router.put("/api/connectors/{cid}/tools")
async def set_connector_tools(cid: str, request: Request):
    _admin(request)
    if not cid.startswith("custom_mcp:"):
        raise HTTPException(status_code=404, detail="Only custom MCP servers have tools.")
    _get(cid)
    b = await _body(request)
    from crawler.connectors import custom_mcp
    try:
        return {"tools": custom_mcp.set_tools(cid, b.get("enabled") or [], b.get("run_without_asking") or [])}
    except Exception as e:
        raise _plain(e)


# ---------------------------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------------------------
@router.get("/api/alerts")
def get_alerts():
    from crawler.connectors import alerts
    return alerts.state(_A().REPORTS_DIR)


@router.put("/api/alerts")
async def put_alerts(request: Request):
    _admin(request)
    b = await _body(request)
    from crawler.connectors import alerts
    rules, errors = alerts.save_rules(b.get("rules"), _A().REPORTS_DIR)
    if errors:
        raise HTTPException(status_code=400, detail="; ".join(f"{k}: {v}" for k, v in list(errors.items())[:5]))
    return alerts.state(_A().REPORTS_DIR)


# ---------------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------------
@router.post("/api/tickets/push")
async def push_tickets(request: Request):
    _same_origin(request)
    b = await _body(request)
    A = _A()
    job = _job()
    if job is None or job.crawler is None:
        raise HTTPException(status_code=400, detail="Crawl a site first.")
    site, index = A._site_index(job)
    if index is None:
        raise HTTPException(status_code=400, detail="Wait until the crawl's reports are ready.")
    from crawler.connectors import tickets_push
    intel = A._intel_errors(job, site, index)
    try:
        d = A._dismissals(job, index)
    except Exception:
        d = None
    try:
        return tickets_push.push(
            str(b.get("connector") or ""), str(b.get("source") or ""), list(b.get("ids") or []),
            intel_result=intel, issues=(d or {}).get("issues") if d else index.issues, index=index, site=site,
            crawler=job.crawler, evidence={"seo": job.seo_evidence, "links": job.evidence},
            evidence_dir=job.evidence_dir, site_url=job.crawler.start_url, dashboard_url=_dashboard_url(),
            dry_run=b.get("dry_run") is not False, confirm_private=bool(b.get("confirm_private")), mask=job.mask)
    except Exception as e:
        raise _plain(e)


@router.get("/api/tickets/pushed")
def pushed_tickets(connector: str = ""):
    from crawler.connectors import tickets_push
    return {"items": tickets_push.pushed_items(connector)}


def _dashboard_url() -> str:
    try:
        cfg = _A()._settings().effective("notifications", _A().REPORTS_DIR)
        return str(cfg.get("dashboard_url") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------------------------
# IndexNow
# ---------------------------------------------------------------------------------------------
@router.get("/api/indexnow/key-check")
def indexnow_key_check(host: str = ""):
    from crawler.connectors import indexnow
    crawler = _crawler()
    try:
        return indexnow.key_check(host=host, origin=getattr(crawler, "start_url", "") or "")
    except Exception as e:
        raise _plain(e)


@router.post("/api/indexnow/submit")
async def indexnow_submit(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.connectors import indexnow
    crawler = _crawler()
    source = str(b.get("source") or "urls")
    try:
        compare_urls = None
        if source == "compare":
            compare_urls = indexnow.compare_candidates(b.get("a") or "", b.get("b") or "current", _A().REPORTS_DIR,
                                                       current=_A()._current_snapshot("current", ""),
                                                       url_map=b.get("url_map") or None)   # the comparison on screen
        urls = indexnow.candidates(crawler, source, tab=str(b.get("tab") or ""), filters=b.get("filters") or None,
                                   urls=b.get("urls") or None, compare_urls=compare_urls)
        return indexnow.submit(crawler, urls, dry_run=b.get("dry_run") is not False, source=source)
    except Exception as e:
        raise _plain(e)


# ---------------------------------------------------------------------------------------------
# Link metrics (paid units: admin only), grammar, Green Web, Sheets
# ---------------------------------------------------------------------------------------------
@router.post("/api/linkmetrics/fetch")
async def linkmetrics_fetch(request: Request):
    _admin(request)
    b = await _body(request)
    from crawler.integrations import linkmetrics
    try:
        if b.get("dry_run"):
            return linkmetrics.estimate(_crawler(), providers=b.get("providers") or None, limit=b.get("limit"))
        return linkmetrics.start_fetch(_crawler(), providers=b.get("providers") or None, limit=b.get("limit"))
    except Exception as e:
        raise _plain(e)


@router.get("/api/linkmetrics/status")
def linkmetrics_status():
    from crawler.integrations import linkmetrics
    return {"providers": linkmetrics.status(), "job": linkmetrics.fetch_status(_crawler())}


@router.post("/api/grammar/run")
async def grammar_run(request: Request):
    _same_origin(request)
    b = await _body(request)
    from crawler.connectors import languagetool
    try:
        return languagetool.start_check(_crawler(), limit=b.get("limit"))
    except Exception as e:
        raise _plain(e)


@router.get("/api/grammar/status")
def grammar_status():
    from crawler.connectors import languagetool
    return languagetool.job_status(_crawler())


@router.post("/api/green-web/check")
async def green_web_check(request: Request):
    _same_origin(request)
    from crawler.connectors import green_web
    crawler = _crawler()
    if crawler is None:
        raise HTTPException(status_code=400, detail="Crawl a site first.")
    try:
        return green_web.check(crawler)
    except Exception as e:
        raise _plain(e)


@router.post("/api/sheets/export")
async def sheets_export(request: Request):
    _same_origin(request)
    b = await _body(request)
    A = _A()
    job = _job()
    site, index = A._site_index(job) if job is not None else (None, None)
    if index is None:
        raise HTTPException(status_code=400, detail="Crawl a site first.")
    from crawler.integrations import sheets
    try:
        return sheets.export(str(b.get("kind") or "tab"), site, index, job.crawler, tab=str(b.get("tab") or ""),
                             filters=b.get("filters") or None, report_id=str(b.get("report_id") or ""))
    except TypeError:
        return sheets.export(str(b.get("kind") or "tab"), site, index, job.crawler, tab=str(b.get("tab") or ""),
                             filters=b.get("filters") or None)
    except Exception as e:
        raise _plain(e)


# ---------------------------------------------------------------------------------------------
# After every crawl: crawl events and alert rules (W2), Green Web when connected
# ---------------------------------------------------------------------------------------------
def after_crawl(job) -> None:
    """Runs in its own thread from web/app.py's follow-up work. Never raises."""
    def work():
        A = _A()
        try:
            crawler = job.crawler
            if crawler is None or job.retired:
                return
            try:
                from crawler.connectors import green_web
                if green_web.connected():
                    green_web.after_crawl(crawler)
            except Exception:
                pass
            channels = [c for c in _connectors().by_kind("notify") if c.status().state == "connected"
                        and c.id != "email_smtp"]
            from crawler.connectors import alerts
            rules = alerts.load_rules(A.REPORTS_DIR)
            if not channels and not rules:
                return
            site, index = A._site_index(job)
            compare_result = A._safe(lambda: A._comparison_with_previous(job), None)
            summary = A._notify().build_summary(crawler, site=site, index=index, compare_result=compare_result,
                                                dashboard_url=_dashboard_url())
            from crawler.connectors import events
            res = events.after_crawl(summary, compare_result, reports_dir=A.REPORTS_DIR, mask=job.mask,
                                     crawl_key=str(job.snapshot_id or job.base_name or ""))
            sent = [d for d in res.get("deliveries") or [] if d.get("ok")]
            failed = [d for d in res.get("deliveries") or [] if not d.get("ok")]
            if sent or failed:
                crawler.log(f"[Connections] Crawl summary sent to {len(sent)} channel(s)"
                            + (f"; {len(failed)} failed: " + "; ".join(job.mask(d.get('detail'))[:120]
                                                                     for d in failed[:3]) if failed else "") + ".")
        except Exception as e:
            try:
                job.crawler.log(f"[Connections] The crawl events could not be sent ({type(e).__name__}).")
            except Exception:
                pass
    threading.Thread(target=work, daemon=True, name="connections-after-crawl").start()


def install(app) -> None:
    if getattr(app.state, "connections_installed", False):
        return
    app.include_router(router)
    app.state.connections_installed = True
