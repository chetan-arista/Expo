"""
Post-crawl checks that need requests of their own. run(crawler, options) runs them once a crawl has
finished (call it from a background thread, never inside a request):

  robots_audit  robots.txt read for search and AI crawlers (blocked bots, Content-Signal, Sitemap lines)
  trap_probe    two random URLs: does the server answer 200 for addresses that do not exist?
  bot_check     a sample of pages fetched as a browser and as search / AI crawlers, compared

Each one stores a JSON-ready dict on the crawler (crawler.robots_audit, crawler.trap_probe,
crawler.bot_report) with "status": "done" | "running" | "stopped" | "skipped" | "error" and a plain
"reason"; analysis.build_site copies them onto the Site, where tabs robots_txt / hygiene / ai_crawlers
read them. A check that did not run says why, so the dashboard reports "Not checked" instead of "0".

Ideas ported from open-seo-crawler (MIT, see THIRD_PARTY_NOTICES.md); the structure and code are ours.
"""
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

DEFAULT_OPTIONS: Dict[str, Any] = {
    "robots_audit": True,      # fetch and read robots.txt
    "trap_probe": True,        # two random-URL requests
    "bot_check": True,         # sample pages as browser + 10 crawlers (about 12 requests per page)
    "bot_sample": 10,          # pages in the bot check sample
    "render": True,            # also render the sample in Chrome (content that needs JavaScript)
    "stop_event": None,        # threading.Event that cancels the checks (default: the crawler's stop)
    "on_progress": None,       # callable(step: str, message: str)
}
# crawler attribute each check stores its report under
ATTRS = {"robots_audit": "robots_audit", "trap_probe": "trap_probe", "bot_check": "bot_report"}


@dataclass
class SiteRow:
    """A URL a post-crawl check requested itself (robots.txt, a probe URL). Not a crawl result, but it
    carries the attribute names tab grids read, so its row shows its own HTTP status and category."""
    url: str
    status_code: Optional[int] = None
    http_status: str = ""
    redirect_destination: Optional[str] = None
    content_type: str = ""
    response_time_ms: int = 0
    blocked: bool = False
    resource_type: str = "file"
    depth: int = 0
    parent_url: str = ""
    page_title: str = ""
    discovery_source: str = "Post-crawl check"
    error_message: Optional[str] = None
    seo: Optional[Dict[str, Any]] = field(default=None, repr=False)
    response_headers: Dict[str, str] = field(default_factory=dict, repr=False)
    kind: str = ""                                   # "robots" | "probe_root" | "probe_nested"
    data: Dict[str, Any] = field(default_factory=dict, repr=False)   # the check's own entry


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def status_report(status: str, reason: str = "", **extra) -> Dict[str, Any]:
    """A report that carries only its state ("skipped", "stopped", "error", "running")."""
    out = {"status": status, "reason": reason, "checked_at": now_iso()}
    out.update(extra)
    return out


def report_ran(report: Optional[dict]) -> bool:
    """True when a post-crawl report holds results (finished, or stopped part-way with some results)."""
    return isinstance(report, dict) and report.get("status") in ("done", "stopped") and not report.get("empty")


def not_run_reason(report: Optional[dict], what: str) -> str:
    """Plain reason a check's filters are "Not checked" ("" when the check ran)."""
    if report_ran(report):
        return ""
    if not isinstance(report, dict):
        return f"The {what} did not run for this crawl (it runs after the crawl finishes)."
    status = report.get("status")
    why = report.get("reason") or ""
    if status == "running":
        return f"The {what} is still running."
    if status == "skipped":
        return f"The {what} was skipped" + (f": {why}" if why else ".")
    if status == "stopped":
        return f"The {what} was stopped before it checked anything."
    return f"The {what} could not run" + (f": {why}" if why else ".")


def stop_event_for(crawler, options: Optional[dict] = None) -> threading.Event:
    ev = (options or {}).get("stop_event")
    if ev is None:
        ev = getattr(crawler, "_stop_event", None)
    return ev if ev is not None else threading.Event()


def _log(crawler, message: str):
    try:
        crawler.log(message)
    except Exception:
        pass


def _refresh_index(crawler):
    """New reports change what the tabs show: let the next site_index() call rebuild."""
    if hasattr(crawler, "_index_key"):
        try:
            crawler._index_key = None
        except Exception:
            pass


def run(crawler, options: Optional[dict] = None) -> Dict[str, str]:
    """Runs robots_audit, trap_probe and bot_check (as enabled in `options`, see DEFAULT_OPTIONS) and
    stores each report on the crawler. Returns {check: status}. Never raises."""
    from . import bot_check, robots_audit, trap_probe

    opts = dict(DEFAULT_OPTIONS)
    opts.update(options or {})
    progress: Callable[[str, str], None] = opts.get("on_progress") or (lambda step, msg: None)
    stop = stop_event_for(crawler, opts)
    summary: Dict[str, str] = {}
    results = list(getattr(crawler, "results_list", None) or [])
    crawl_status = (getattr(crawler, "status", "") or "").lower()

    def nothing_to_check() -> str:
        if crawl_status == "auth_failed":
            return "the login failed, so nothing was crawled"
        if not results:
            return "the crawl has no results"
        return ""

    steps = (
        ("robots_audit", lambda: robots_audit.run(crawler)),
        ("trap_probe", lambda: trap_probe.run(crawler)),      # two requests: runs even after a stopped crawl
        ("bot_check", lambda: bot_check.run(crawler, sample=int(opts.get("bot_sample") or 10),
                                            render=bool(opts.get("render")), stop_event=stop,
                                            on_progress=lambda msg: progress("bot_check", msg))),
    )
    for name, fn in steps:
        attr = ATTRS[name]
        if opts.get(name) == "deferred":
            continue          # the caller runs this step itself afterwards (the bot check in the background)
        why = nothing_to_check()
        if not opts.get(name):
            report = status_report("skipped", "turned off for this crawl")
        elif why:
            report = status_report("skipped", why)
        elif name == "bot_check" and stop.is_set():
            report = status_report("skipped", "the crawl was stopped, so the bot check did not start")
        else:
            progress(name, f"Running {name.replace('_', ' ')}")
            started = time.time()
            try:
                report = fn()
            except Exception as e:   # a check must never break the crawl
                report = status_report("error", f"{type(e).__name__}: {str(e)[:160]}")
            if isinstance(report, dict):
                report.setdefault("seconds", round(time.time() - started, 1))
        if not isinstance(report, dict):
            report = status_report("error", "the check returned nothing")
        setattr(crawler, attr, report)
        summary[name] = report.get("status", "error")
        _log(crawler, f"[post-crawl] {name.replace('_', ' ')}: {summary[name]}"
                      + (f" ({report.get('reason')})" if report.get("reason") else ""))
        _refresh_index(crawler)
    return summary
