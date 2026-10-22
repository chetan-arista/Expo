import os
import re
import time
import tempfile
import threading
import asyncio
from typing import Optional, Dict
from collections import deque
from urllib.parse import urlparse, urljoin
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from crawler.config import CrawlConfig, AuthConfig
from crawler.engine import WebsiteCrawler
from crawler.exporter import ExcelExporter, EXPORT_CATEGORIES
from crawler.evidence import EvidenceCollector
from crawler.issues import ISSUE_CATEGORIES, issue_sort_key
from crawler.url_utils import get_domain

app = FastAPI(title="Website Link Crawler API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MAX_SCREENSHOT_ISSUES = int(os.environ.get("CRAWLER_MAX_SCREENSHOTS", "25"))
REPORT_FILE_RE = re.compile(r"^crawl_[\w.-]+\.(xlsx|pdf)$")
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class CrawlJob:
    """Everything belonging to one crawl run. A new crawl gets a new job, so a finishing
    background thread can never overwrite the state of the crawl that replaced it."""

    def __init__(self, crawler: Optional[WebsiteCrawler], base_name: str):
        self.crawler = crawler
        self.base_name = base_name  # crawl_<domain>_<timestamp>
        self.thread: Optional[threading.Thread] = None
        self.logs = deque(maxlen=100)
        self.phase = "crawling"  # crawling -> analyzing -> done
        self.export_file: Optional[str] = None
        self.pdf_file: Optional[str] = None
        self.evidence: Dict[str, dict] = {}
        self.evidence_dir = os.path.join(get_export_dir(), f"{base_name}_evidence")
        self.evidence_progress = {"done": 0, "total": 0}
        self.screenshot_note = ""
        self.cancel_event = threading.Event()

    def log(self, msg: str):
        self.logs.append(msg)

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())


class CrawlSession:
    def __init__(self):
        self.job: Optional[CrawlJob] = None


session = CrawlSession()


class AuthRequest(BaseModel):
    enabled: bool = False
    auth_type: str = "none" # "none", "form", "cookies", "header"
    login_url: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    cookies: Optional[str] = None
    custom_header_name: Optional[str] = None
    custom_header_value: Optional[str] = None

class CrawlRequest(BaseModel):
    url: str
    auth: Optional[AuthRequest] = None
    # Smart defaults for automated crawling:
    max_urls: Optional[int] = 0        # 0 = Unlimited (crawls until queue is exhausted)
    max_depth: Optional[int] = 50
    concurrency: Optional[int] = 6
    timeout: Optional[int] = 15
    use_sitemaps: Optional[bool] = True
    collect_external: Optional[bool] = True
    use_dynamic_js: Optional[bool] = True
    enable_spa_mode: Optional[bool] = False
    delay: Optional[float] = 0.0
    subdomains: Optional[bool] = False
    verify_ssl: Optional[bool] = False
    screenshots: Optional[bool] = True
    force: bool = False

def get_export_dir() -> str:
    """Return a writable directory across both local and serverless/Vercel environments."""
    return tempfile.gettempdir()

def _build_auth(req: CrawlRequest) -> AuthConfig:
    if not (req.auth and req.auth.enabled):
        return AuthConfig()
    return AuthConfig(
        enabled=True,
        auth_type=req.auth.auth_type,
        login_url=req.auth.login_url,
        username=req.auth.username,
        password=req.auth.password,
        cookies=req.auth.cookies,
        custom_header_name=req.auth.custom_header_name,
        custom_header_value=req.auth.custom_header_value
    )

def _normalize_target(raw_url: str) -> str:
    raw_url = raw_url.strip()
    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        raw_url = "https://" + raw_url
    return raw_url

def _base_name(raw_url: str) -> str:
    domain = get_domain(raw_url) or "website"
    return f"crawl_{domain}_{time.strftime('%Y%m%d_%H%M%S')}"

def _issues_for_evidence(crawler: WebsiteCrawler) -> list:
    issues = [r for r in crawler.results_list if r.category in ISSUE_CATEGORIES]
    return sorted(issues, key=issue_sort_key)[:MAX_SCREENSHOT_ISSUES]

def build_pdf(job: CrawlJob) -> str:
    from crawler.pdf_report import build_pdf_report
    path = os.path.join(get_export_dir(), f"{job.base_name}_issues.pdf")
    build_pdf_report(
        results=list(job.crawler.results_list),
        filepath=path,
        stats=job.crawler.get_stats(),
        evidence=job.evidence,
        evidence_dir=job.evidence_dir,
        screenshot_note=job.screenshot_note,
    )
    job.pdf_file = path
    return path

def capture_evidence(job: CrawlJob):
    """Screenshots of broken pages and of the parent page with the broken link highlighted."""
    crawler = job.crawler
    issues = _issues_for_evidence(crawler)
    if not issues:
        return
    if job.cancel_event.is_set():
        job.screenshot_note = "Screenshots skipped by user."
        return
    collector = EvidenceCollector(
        out_dir=job.evidence_dir,
        user_agent=crawler.config.user_agent,
        timeout=crawler.config.timeout,
        cookies=crawler.auth_cookies,
        logger=crawler.log,
        cancel_event=job.cancel_event,
    )
    if not collector.start():
        job.screenshot_note = f"Screenshots unavailable: {collector.unavailable_reason} Link positions are shown as page sketches."
        crawler.log(f"[Screenshots] {job.screenshot_note}")
        return
    try:
        total_issues = sum(1 for r in crawler.results_list if r.category in ISSUE_CATEGORIES)
        crawler.log(f"[Screenshots] Capturing visual evidence for {len(issues)} issue(s)...")
        job.evidence_progress = {"done": 0, "total": 1}

        def progress(done, total):
            job.evidence_progress = {"done": done, "total": total}

        job.evidence = collector.capture(issues, on_progress=progress)
        if total_issues > len(issues):
            job.screenshot_note = f"Screenshots captured for the {len(issues)} most severe of {total_issues} issues."
        if job.cancel_event.is_set():
            job.screenshot_note = "Screenshot capture was stopped early; some issues have no screenshot."
    finally:
        collector.close()

def run_crawler_task(job: CrawlJob, export_path: str, screenshots: bool = True):
    crawler = job.crawler
    try:
        results = crawler.run()
        job.phase = "analyzing"
        ExcelExporter.export(
            results=results,
            filepath=export_path,
            stats=crawler.get_stats(),
            external_links=crawler.external_links_list
        )
        job.export_file = export_path
        crawler.log(f"Excel report generated: {os.path.basename(export_path)}")
        if screenshots:
            capture_evidence(job)
        else:
            job.screenshot_note = "Screenshots were disabled for this crawl."
        build_pdf(job)
        crawler.log(f"PDF issue report generated: {os.path.basename(job.pdf_file)}")
    except Exception as e:
        crawler.log(f"Crawl failed with error: {e}")
    finally:
        job.phase = "done"

@app.get("/", response_class=HTMLResponse)
def serve_index():
    static_file = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(static_file, "r", encoding="utf-8") as f:
        return f.read()

@app.get("/healthz")
@app.get("/api/health")
async def health_check():
    """Health check endpoint for Render and uptime monitoring."""
    return {
        "status": "ok",
        "service": "website-crawler-backend",
        "timestamp": time.time()
    }

@app.get("/api/environment")
async def get_environment():
    """Detects whether running in serverless (e.g. Vercel) or persistent container/local (Render)."""
    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
    is_render = bool(os.environ.get("RENDER"))
    platform = "Vercel Serverless" if is_vercel else ("Render Persistent Container" if is_render else "Persistent Server / Local")
    return {
        "is_serverless": is_vercel,
        "platform": platform,
        "is_render": is_render,
        "max_duration_seconds": 60 if is_vercel else None
    }

class InspectRequest(BaseModel):
    url: str

@app.post("/api/auth/inspect")
@app.get("/api/auth/inspect")
def inspect_auth_endpoint(url: Optional[str] = None, req: Optional[InspectRequest] = None):
    """
    Fast pre-flight inspection of target URL to determine if login/credentials are required or available.
    Detects HTTP 401/403, redirects to login, password input fields, or login portal links.
    """
    raw_url = (req.url if req and req.url else url) or ""
    raw_url = raw_url.strip()
    if not raw_url:
        return {"requires_auth": False, "login_url": None, "reason": "No URL provided"}

    if not raw_url.startswith("http://") and not raw_url.startswith("https://"):
        raw_url = "https://" + raw_url

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        r = requests.get(raw_url, headers=headers, timeout=6, allow_redirects=True, verify=False)
        final_url = str(r.url)
        status = r.status_code

        # 1. HTTP 401 Unauthorized or 403 Forbidden
        if status in (401, 403):
            return {
                "requires_auth": True,
                "reason": f"HTTP {status} Access Restricted",
                "login_url": final_url,
                "auth_type": "form"
            }

        p_orig = urlparse(raw_url).path.rstrip('/')
        p_final = urlparse(final_url).path.rstrip('/')

        # 2. Redirected to login/signin/auth page
        if p_orig != p_final and any(k in p_final.lower() for k in ['login', 'signin', 'auth']):
            return {
                "requires_auth": True,
                "reason": "Redirected to login portal",
                "login_url": final_url,
                "auth_type": "form"
            }

        # 3. Direct login URL provided
        if any(k in p_final.lower() for k in ['login', 'signin', 'auth']):
            return {
                "requires_auth": True,
                "reason": "Direct login portal URL",
                "login_url": final_url,
                "auth_type": "form"
            }

        # 4. Password input form detected on page (e.g. login form)
        soup = BeautifulSoup(r.text, 'html.parser')
        has_captcha = any(k in r.text.lower() for k in ['turnstile', 'challenges.cloudflare.com', 'hcaptcha', 'cf-challenge', 'datadome'])
        pwd_input = soup.find('input', {'type': 'password'})
        if pwd_input:
            return {
                "requires_auth": True,
                "reason": "Login portal with Cloudflare Turnstile / CAPTCHA detected (Session Cookies recommended)" if has_captcha else "Password login form detected on page",
                "login_url": final_url,
                "auth_type": "cookies" if has_captcha else "form",
                "has_captcha": has_captcha
            }

        # 5. Prominent login link available on page
        for a in soup.find_all('a', href=True):
            href = str(a.get('href') or '').lower()
            text = a.get_text().strip().lower()
            if ('login' in href or 'signin' in href or text in ['log in', 'login', 'sign in']):
                full_login_url = urljoin(final_url, href)
                return {
                    "requires_auth": True,
                    "reason": "Login portal available on website",
                    "login_url": full_login_url,
                    "auth_type": "form",
                    "has_captcha": False
                }

        return {"requires_auth": False, "login_url": None, "reason": "Public access (No login detected)", "has_captcha": False}
    except Exception as e:
        return {"requires_auth": False, "login_url": None, "reason": str(e)}

# --------------------------------------------------------------------------
# Direct Mode for Serverless / Vercel (Synchronous execution within timeout)
# --------------------------------------------------------------------------
@app.post("/api/crawl/direct")
def direct_crawl(req: CrawlRequest):
    """
    Synchronous crawl endpoint specifically optimized for Serverless platforms (Vercel).
    Executes within the 60s function limit and returns completed results & download filename.
    """
    raw_url = _normalize_target(req.url)
    base_name = _base_name(raw_url)
    export_filename = f"{base_name}.xlsx"
    export_path = os.path.join(get_export_dir(), export_filename)

    is_vercel = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))

    config = CrawlConfig(
        start_url=raw_url,
        # On Vercel, cap at 100 URLs to guarantee completion within the 60s lambda limit
        max_urls=min(req.max_urls, 100) if (is_vercel and req.max_urls) else (100 if is_vercel else (req.max_urls or 0)),
        max_depth=req.max_depth or 50,
        concurrency=min(req.concurrency or 6, 8),
        timeout=min(req.timeout or 15, 12) if is_vercel else (req.timeout or 15),
        use_sitemaps=True if req.use_sitemaps is None else req.use_sitemaps,
        collect_external_links=True if req.collect_external is None else req.collect_external,
        use_dynamic_js=False if is_vercel else (True if req.use_dynamic_js is None else req.use_dynamic_js),
        enable_spa_mode=bool(req.enable_spa_mode),
        delay=req.delay or 0.0,
        include_subdomains=bool(req.subdomains),
        verify_ssl=bool(req.verify_ssl),
        auth=_build_auth(req),
        output_file=export_path
    )

    crawler = WebsiteCrawler(config=config)
    results = crawler.run()
    stats = crawler.get_stats()

    ExcelExporter.export(
        results=results,
        filepath=export_path,
        stats=stats,
        external_links=crawler.external_links_list
    )

    job = CrawlJob(crawler, base_name)
    job.export_file = export_path
    job.phase = "done"
    job.screenshot_note = "Screenshots are not available in serverless mode."
    session.job = job

    return {
        "status": "completed",
        "stats": stats,
        "results_count": len(results),
        "external_count": len(crawler.external_links_list),
        "recent_results": [r.to_dict() for r in results],
        "recent_external": [e.to_dict() for e in crawler.external_links_list],
        "export_file": export_filename
    }

# --------------------------------------------------------------------------
# Background Async Mode (For Local Machine, Docker, Render, Railway, VPS)
# --------------------------------------------------------------------------
@app.post("/api/crawl/start")
async def start_crawl(req: CrawlRequest):
    job = session.job
    if job and job.busy:
        if req.force:
            job.cancel_event.set()
            if job.crawler:
                job.crawler.stop()
        else:
            active_url = job.crawler.config.start_url if job.crawler else ""
            raise HTTPException(
                status_code=400,
                detail=f"A crawl is already running for {active_url}."
            )

    raw_url = _normalize_target(req.url)
    base_name = _base_name(raw_url)
    export_filename = f"{base_name}.xlsx"
    export_path = os.path.join(get_export_dir(), export_filename)

    config = CrawlConfig(
        start_url=raw_url,
        max_urls=req.max_urls if req.max_urls is not None else 0, # 0 = unlimited
        max_depth=req.max_depth or 50,
        concurrency=min(req.concurrency or 6, 12),
        timeout=req.timeout or 15,
        use_sitemaps=True if req.use_sitemaps is None else req.use_sitemaps,
        collect_external_links=True if req.collect_external is None else req.collect_external,
        use_dynamic_js=True if req.use_dynamic_js is None else req.use_dynamic_js,
        enable_spa_mode=bool(req.enable_spa_mode),
        delay=req.delay or 0.0,
        include_subdomains=bool(req.subdomains),
        verify_ssl=bool(req.verify_ssl),
        auth=_build_auth(req),
        output_file=export_path
    )

    job = CrawlJob(None, base_name)
    job.crawler = WebsiteCrawler(config=config, on_log=job.log)
    job.crawler.status = "running"
    job.crawler.start_time = time.time()
    session.job = job

    job.thread = threading.Thread(
        target=run_crawler_task,
        args=(job, export_path, req.screenshots is not False),
        daemon=True
    )
    job.thread.start()

    return {"status": "started", "target": raw_url, "output_file": export_filename}

@app.post("/api/crawl/stop")
async def stop_crawl():
    """Stops crawling. If the crawl already finished and screenshots are being captured,
    skips the remaining screenshots instead. Reports are still generated."""
    job = session.job
    if not job or not job.crawler:
        return {"status": "idle"}
    if job.phase == "crawling":
        job.crawler.stop()
    else:
        job.cancel_event.set()
    return {
        "status": "stopped",
        "export_ready": True,
        "export_file": f"{job.base_name}.xlsx"
    }

@app.post("/api/crawl/pause")
async def pause_crawl():
    if session.job and session.job.crawler:
        session.job.crawler.pause()
        return {"status": "paused"}
    return {"status": "idle"}

@app.post("/api/crawl/resume")
async def resume_crawl():
    if session.job and session.job.crawler:
        session.job.crawler.resume()
        return {"status": "resumed"}
    return {"status": "idle"}

@app.post("/api/crawl/reset")
async def reset_crawl():
    job = session.job
    if job and job.busy:
        return {"status": "running", "detail": "A crawl is currently in progress."}
    session.job = None
    return {"status": "idle"}

def _public_status(job: CrawlJob) -> str:
    """running/paused while crawling, 'analyzing' while reports & screenshots are built."""
    status = job.crawler.status
    if status in ("completed", "stopped") and job.busy:
        return "analyzing"
    return status

@app.get("/api/crawl/status")
async def get_status(since: int = 0, ext_since: int = 0):
    """
    Live crawl status. `since` / `ext_since` are cursors into the full result lists so the
    client receives every row exactly once (up to 1000 per poll).
    """
    job = session.job
    if not job or not job.crawler:
        return {
            "status": "idle",
            "phase": "idle",
            "stats": {
                "status": "idle",
                "elapsed_seconds": 0,
                "total_discovered": 0,
                "total_crawled": 0,
                "queue_size": 0,
                "status_2xx": 0,
                "status_3xx": 0,
                "status_4xx": 0,
                "status_5xx": 0,
                "status_err": 0,
                "failed_count": 0,
                "auth_required_count": 0,
                "total_external": 0,
                "unique_external_domains": 0,
                "start_url": "",
                "auth_used": False,
                "auth_status": "Not required",
                "auth_error": None,
                "limitations": []
            },
            "results_count": 0,
            "external_count": 0,
            "new_results": [],
            "new_external": [],
            "next_since": 0,
            "next_ext_since": 0,
            "recent_results": [],
            "recent_external": [],
            "logs": [],
            "export_ready": False,
            "report_ready": False
        }

    crawler = job.crawler
    stats = crawler.get_stats()
    stats["status"] = _public_status(job)
    results = crawler.results_list
    externals = crawler.external_links_list
    total_results, total_external = len(results), len(externals)
    since = max(0, min(since, total_results))
    ext_since = max(0, min(ext_since, total_external))
    new_results = [r.to_dict() for r in results[since:since + 1000]]
    new_external = [e.to_dict() for e in externals[ext_since:ext_since + 1000]]
    report_ready = job.phase == "done" and bool(job.export_file)

    return {
        "status": stats["status"],
        "phase": job.phase,
        "stats": stats,
        "results_count": total_results,
        "external_count": total_external,
        "new_results": new_results,
        "new_external": new_external,
        "next_since": since + len(new_results),
        "next_ext_since": ext_since + len(new_external),
        "recent_results": [r.to_dict() for r in results[-50:]],
        "recent_external": [e.to_dict() for e in externals[-50:]],
        "logs": list(job.logs)[-30:],
        "evidence_progress": job.evidence_progress,
        "screenshot_note": job.screenshot_note,
        "export_ready": report_ready,
        "report_ready": report_ready,
        "pdf_ready": bool(job.pdf_file),
        "export_file": os.path.basename(job.export_file) if job.export_file else None,
        "auth_used": stats.get("auth_used", False),
        "auth_status": stats.get("auth_status", "Not required"),
        "auth_error": stats.get("auth_error"),
        "limitations": stats.get("limitations", [])
    }

@app.get("/api/crawl/issues")
async def get_issues():
    """All 4xx / 5xx / network issues with diagnosis, link position and screenshot URLs."""
    job = session.job
    if not job or not job.crawler:
        return {"issues": [], "phase": "idle", "screenshot_note": ""}
    issues = sorted([r for r in job.crawler.results_list if r.category in ISSUE_CATEGORIES], key=issue_sort_key)
    items = []
    for r in issues:
        ev = job.evidence.get(r.url) or {}
        item = r.to_dict()
        item["position"] = ev.get("position")
        item["parent_shot"] = f"/api/crawl/evidence/{ev['parent_shot']}" if ev.get("parent_shot") else None
        item["error_shot"] = f"/api/crawl/evidence/{ev['error_shot']}" if ev.get("error_shot") else None
        items.append(item)
    return {"issues": items, "phase": job.phase, "screenshot_note": job.screenshot_note}

@app.get("/api/crawl/evidence/{name}")
async def get_evidence(name: str):
    job = session.job
    safe = os.path.basename(name)
    if not job or not re.fullmatch(r"[\w.-]+\.(jpg|png)", safe):
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    path = os.path.join(job.evidence_dir, safe)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Screenshot not found.")
    return FileResponse(path, media_type="image/jpeg" if safe.endswith(".jpg") else "image/png")

@app.get("/api/crawl/report/excel")
def download_category_excel(category: str = "all"):
    """Excel report for one category: all, 2xx, 3xx, 4xx, 5xx, error, issues or external."""
    job = session.job
    if not job or not job.crawler:
        raise HTTPException(status_code=404, detail="No crawl data available.")
    if category not in EXPORT_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category. Use one of: {', '.join(EXPORT_CATEGORIES)}")
    crawler = job.crawler
    suffix = "" if category == "all" else f"_{category}"
    path = os.path.join(get_export_dir(), f"{job.base_name}{suffix}.xlsx")
    ExcelExporter.export(
        results=list(crawler.results_list),
        filepath=path,
        stats=crawler.get_stats(),
        external_links=list(crawler.external_links_list),
        category=category
    )
    return FileResponse(path=path, filename=os.path.basename(path), media_type=XLSX_MEDIA)

@app.get("/api/crawl/report/pdf")
def download_pdf_report():
    """Visual PDF issue report (summary + one card per issue with screenshots)."""
    job = session.job
    if not job or not job.crawler:
        raise HTTPException(status_code=404, detail="No crawl data available.")
    if job.phase == "analyzing":
        raise HTTPException(status_code=409, detail="The PDF report is still being generated.")
    path = job.pdf_file if (job.pdf_file and os.path.exists(job.pdf_file)) else build_pdf(job)
    return FileResponse(path=path, filename=os.path.basename(path), media_type="application/pdf")

@app.get("/api/crawl/download/{filename}")
async def download_by_filename(filename: str):
    safe_filename = os.path.basename(filename)
    # Only serve report files this app generated, never arbitrary temp files
    if not REPORT_FILE_RE.match(safe_filename):
        raise HTTPException(status_code=404, detail="File not found or expired.")
    filepath = os.path.join(get_export_dir(), safe_filename)
    for _ in range(30):
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            break
        await asyncio.sleep(0.5)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found or expired.")
    media = "application/pdf" if safe_filename.endswith(".pdf") else XLSX_MEDIA
    return FileResponse(path=filepath, filename=safe_filename, media_type=media)

@app.get("/api/crawl/export")
async def download_export():
    job = session.job
    if job and job.export_file:
        filepath = job.export_file
        for _ in range(30):
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                break
            await asyncio.sleep(0.5)
        if os.path.exists(filepath):
            return FileResponse(path=filepath, filename=os.path.basename(filepath), media_type=XLSX_MEDIA)
    raise HTTPException(status_code=404, detail="No export file available.")

if os.path.exists("web/static"):
    app.mount("/static", StaticFiles(directory="web/static"), name="static")
elif os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.app:app", host="0.0.0.0", port=8000, reload=True)
