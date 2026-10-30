"""
Events to chat and webhook connectors (PLAN4_build.md 10.2).

  notify_event(event, payload, reports_dir="", only=None, mask=None, alert=None) -> [{"connector", "ok", "detail"}]
      event: crawl.finished | crawl.failed | issues.changed | alert. payload: crawler.notify.build_summary(...)
      (reduced by messages.public_summary to counts, issue names, the site and the dashboard address). Every
      connected notify connector that has the event ticked gets one message; email is not one of them (the
      crawl-complete email keeps its own setting; alert emails go through alerts.py). explicit=True (an alert rule
      that names its channels in `only`) sends even when the connector has the event unticked. Never raises: a
      failure is {"ok": False, "detail": <masked plain sentence>} and the connector shows it as its last delivery.
  events_for(summary) -> [events]   which events a finished crawl raises
  after_crawl(summary, compare_result=None, reports_dir="", mask=None, crawl_key="") -> {"deliveries", "alerts"}
      what the server runs after every crawl (dashboard and scheduled): the crawl event(s) and the alert rules.
"""
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import messages
from .base import ConnectorError

EVENTS = messages.EVENTS
FAILED_STATUSES = ("failed", "auth_failed")


def _registry():
    from . import registry
    return registry


def notify_event(event: str, payload: Dict[str, Any], reports_dir: str = "", only: Optional[Iterable[str]] = None,
                 mask: Optional[Callable[[str], str]] = None, alert: Optional[Dict[str, Any]] = None,
                 explicit: bool = False) -> List[Dict[str, Any]]:
    if event not in EVENTS:
        raise ValueError(f"Unknown event: {event}")
    only_set = set(only) if only is not None else None
    summary = messages.public_summary(payload or {}, mask)
    if not summary.get("dashboard_url"):
        summary["dashboard_url"] = _dashboard_url(reports_dir)
    out: List[Dict[str, Any]] = []
    for c in _registry().by_kind("notify"):
        if c.id == "email_smtp" or c.is_template() or not hasattr(c, "send"):
            continue
        if only_set is not None and c.id not in only_set:
            continue
        try:
            state = c.status().state
        except Exception:
            continue
        if state not in ("connected", "error") or (event not in c.events() and not explicit):
            continue
        try:
            ok, detail = c.send(event, summary, alert)
        except ConnectorError as e:
            ok, detail = False, str(e)
        except Exception as e:          # never let one connector stop the others; the type only (no URL)
            ok, detail = False, f"Sending failed unexpectedly ({type(e).__name__})."
        detail = c.masker()(detail)
        if mask is not None:
            detail = mask(detail)
        try:
            c.record_delivery(ok, detail)
        except Exception:
            pass
        out.append({"connector": c.id, "name": c.name, "ok": bool(ok), "detail": detail})
    return out


def _dashboard_url(reports_dir: str = "") -> str:
    """The dashboard address of Settings > Notifications (or env CRAWLER_DASHBOARD_URL), without query or key."""
    try:
        from .. import settings_store
        return messages.clean_url(settings_store.effective("notifications", reports_dir or None).get("dashboard_url"))
    except Exception:
        return ""


def events_for(summary: Dict[str, Any]) -> List[str]:
    """crawl.failed or crawl.finished, plus issues.changed when the comparison with the previous crawl found new
    or returning issues."""
    s = summary or {}
    if str(s.get("status") or "") in FAILED_STATUSES or s.get("error"):
        return ["crawl.failed"]
    out = ["crawl.finished"]
    c = s.get("compare") or {}
    if int(c.get("new") or 0) + int(c.get("regressed") or 0) > 0:
        out.append("issues.changed")
    return out


def after_crawl(summary: Dict[str, Any], compare_result: Optional[Dict[str, Any]] = None, reports_dir: str = "",
                mask: Optional[Callable[[str], str]] = None, crawl_key: str = "") -> Dict[str, Any]:
    """Sends the crawl's events and evaluates the alert rules (each fires at most once per crawl_key)."""
    deliveries: List[Dict[str, Any]] = []
    for ev in events_for(summary):
        deliveries += notify_event(ev, summary, reports_dir, mask=mask)
    try:
        from . import alerts
        alert_run = alerts.run_after_crawl(summary, compare_result, reports_dir=reports_dir, mask=mask,
                                           crawl_key=crawl_key)
    except Exception as e:
        alert_run = {"error": f"The alert rules could not be evaluated ({type(e).__name__})."}
    return {"deliveries": deliveries, "alerts": alert_run}
