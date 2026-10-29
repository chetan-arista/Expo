"""
What a chat or webhook message says (pure functions, no network): the crawl summary of crawler.notify.build_summary
reduced to what may leave the server, and the Slack / Discord, Microsoft Teams and generic JSON bodies built from it.

public_summary() keeps counts, issue names, the site and the dashboard address only: no download links, no keys, no
page text, no filter lists. The site and the dashboard address lose their user info, query and fragment (a dashboard
link must never carry the access key). URLs per status class are always all six classes, so they add up to the
URLs crawled (category = HTTP status: a 404 is always Broken 4xx); `reconciled` says whether they do.
"""
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

BRAND = "ARACHNIX"
EVENTS = ("crawl.finished", "crawl.failed", "issues.changed", "alert")
EVENT_LABELS = {"crawl.finished": "Crawl finished", "crawl.failed": "Crawl failed",
                "issues.changed": "New or returning issues", "alert": "Alerts"}
CATEGORY_ORDER = ("2xx", "3xx", "4xx", "5xx", "blocked", "error")
CATEGORY_TEXT = {"2xx": "Success (2xx)", "3xx": "Redirect (3xx)", "4xx": "Broken (4xx)", "5xx": "Server error (5xx)",
                 "blocked": "Blocked by robots.txt", "error": "No response"}
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
MAX_ISSUES = 8              # issue lines in a chat message (the generic webhook gets every issue)
MAX_ISSUES_JSON = 300
COLORS = {"ok": "#2E7D32", "warn": "#EF6C00", "bad": "#C62828", "info": "#1565C0"}


def clean_url(url: Any) -> str:
    """scheme://host[:port]/path of an http(s) URL (no user info, query or fragment); "" otherwise."""
    try:
        p = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return ""
    host = p.hostname
    if ":" in host:
        host = f"[{host}]"
    try:
        port = p.port
    except ValueError:
        port = None
    netloc = host + (f":{port}" if port else "")
    return urlunsplit((p.scheme.lower(), netloc, p.path or "/", "", ""))


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def public_summary(summary: Dict[str, Any], mask: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """The part of a crawl summary that may be sent to a chat or webhook."""
    s = summary if isinstance(summary, dict) else {}
    m = mask or (lambda t: t)
    cats = s.get("categories") or {}
    categories = {c: _int(cats.get(c)) for c in CATEGORY_ORDER}
    urls = _int(s.get("urls"))
    issues = []
    for i in s.get("issues") or []:
        if isinstance(i, dict):
            issues.append({"name": str(i.get("name") or "")[:200], "type": str(i.get("type") or "")[:20],
                           "priority": str(i.get("priority") or "")[:10], "urls": _int(i.get("urls"))})
    issues.sort(key=lambda i: (PRIORITY_ORDER.get(i["priority"], 3), -i["urls"], i["name"]))
    cmp_ = s.get("compare") if isinstance(s.get("compare"), dict) else None
    compare = None
    if cmp_:
        compare = {k: _int(cmp_.get(k)) for k in ("new", "regressed", "fixed", "not_rechecked", "unchanged",
                                                    "urls_added", "urls_removed", "titles_changed", "status_changed")}
        compare["previous_started"] = str(cmp_.get("previous_started") or "")[:40]
        compare["top"] = [{"name": str(t.get("name") or "")[:200], "previous": _int(t.get("previous")),
                           "current": _int(t.get("current")), "new": _int(t.get("new")), "fixed": _int(t.get("fixed"))}
                          for t in (cmp_.get("top") or [])[:15] if isinstance(t, dict)]
    ext = s.get("external") or {}
    hs = s.get("health_score")
    return {
        "site": m(clean_url(s.get("site"))), "mode": str(s.get("mode") or "spider")[:20],
        "status": str(s.get("status") or "")[:30], "error": m(str(s.get("error") or ""))[:300],
        "started": str(s.get("started") or "")[:40], "finished": str(s.get("finished") or "")[:40],
        "duration": str(s.get("duration") or "")[:40], "urls": urls, "categories": categories,
        "reconciled": sum(categories.values()) == urls,
        "pages": _int(s.get("pages")), "pages_ok": _int(s.get("pages_ok")),
        "health_score": hs if isinstance(hs, (int, float)) else None,
        "external": {"links": _int(ext.get("links")), "checked": _int(ext.get("checked")),
                     "broken": _int(ext.get("broken"))},
        "issues": issues[:MAX_ISSUES_JSON], "issue_types": len(issues), "signed_in": bool(s.get("auth_used")),
        "limitations": [m(str(n))[:300] for n in (s.get("limitations") or [])[:10]],
        "compare": compare, "dashboard_url": clean_url(s.get("dashboard_url")),
    }


def host_of(summary: Dict[str, Any]) -> str:
    try:
        return urlsplit(summary.get("site") or "").hostname or "the site"
    except ValueError:
        return "the site"


def title(event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]] = None) -> str:
    host = host_of(s)
    if event == "alert":
        return f"Alert: {(alert or {}).get('name') or 'alert rule'} - {host}"
    if event == "crawl.failed":
        return f"Crawl failed: {host}"
    if event == "issues.changed":
        c = s.get("compare") or {}
        return (f"New or returning issues on {host}: {c.get('new', 0):,} new, {c.get('regressed', 0):,} came back, "
                f"{c.get('fixed', 0):,} fixed")
    if event == "test":
        return f"{BRAND} test message"
    if s.get("status") == "stopped":
        return f"Crawl stopped: {host} - {s.get('urls', 0):,} URLs"
    hs = s.get("health_score")
    return f"Crawl finished: {host} - {s.get('urls', 0):,} URLs" + (f", health {hs}%" if hs is not None else "")


def class_line(s: Dict[str, Any]) -> str:
    cats = s.get("categories") or {}
    parts = " + ".join(f"{CATEGORY_TEXT[c]} {cats.get(c, 0):,}" for c in CATEGORY_ORDER)
    line = f"URLs crawled: {s.get('urls', 0):,} = {parts}"
    if not s.get("reconciled", True):
        line += " (these do not add up: see the dashboard)"
    return line


def issue_lines(s: Dict[str, Any], limit: int = MAX_ISSUES) -> List[str]:
    issues = s.get("issues") or []
    if not issues:
        return ["Issues: none found."]
    out = [f"Issues: {s.get('issue_types', len(issues)):,} types"]
    for i in issues[:limit]:
        out.append(f"- [{i['priority'] or '-'}] {i['name']} ({i['type'] or 'Issue'}): {i['urls']:,} URLs")
    if len(issues) > limit:
        out.append(f"- ... and {len(issues) - limit:,} more issue types in the dashboard")
    return out


def compare_lines(s: Dict[str, Any]) -> List[str]:
    c = s.get("compare")
    if not c:
        return []
    when = f" ({c['previous_started']})" if c.get("previous_started") else ""
    return [f"Since the previous crawl{when}: {c['new']:,} new, {c['regressed']:,} came back, {c['fixed']:,} fixed, "
            f"{c['not_rechecked']:,} not re-checked",
            f"URLs added {c['urls_added']:,}, gone {c['urls_removed']:,}; titles changed {c['titles_changed']:,}; "
            f"status changed {c['status_changed']:,}"]


def lines(event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]] = None,
          limit: int = MAX_ISSUES) -> List[str]:
    """The message body as plain lines."""
    out: List[str] = []
    if alert:
        out.append(str(alert.get("message") or ""))
    if s.get("site"):
        out.append(f"Site: {s['site']}")
    if event == "crawl.failed" and s.get("error"):
        out.append(f"Error: {s['error']}")
    out.append(class_line(s))
    if s.get("health_score") is not None:
        out.append(f"Health score: {s['health_score']}% ({s.get('pages_ok', 0):,} of {s.get('pages', 0):,} pages load)")
    out += issue_lines(s, limit)
    out += compare_lines(s)
    if s.get("limitations"):
        out.append("This crawl covers part of the site: " + "; ".join(s["limitations"][:3]))
    if s.get("signed_in"):
        out.append("This crawl was signed in: only counts and issue names are shared here.")
    if s.get("dashboard_url"):
        out.append(f"Open the dashboard: {s['dashboard_url']}")
    return [x for x in out if x]


def color(event: str, s: Dict[str, Any]) -> str:
    if event in ("crawl.failed", "alert"):
        return COLORS["bad"]
    if event == "issues.changed":
        return COLORS["warn"]
    if event == "test":
        return COLORS["info"]
    cats = s.get("categories") or {}
    return COLORS["warn"] if cats.get("4xx") or cats.get("5xx") or cats.get("error") else COLORS["ok"]


def facts(s: Dict[str, Any]) -> List[Tuple[str, str]]:
    out = [("URLs crawled", f"{s.get('urls', 0):,}")]
    cats = s.get("categories") or {}
    out += [(CATEGORY_TEXT[c], f"{cats.get(c, 0):,}") for c in CATEGORY_ORDER]
    if s.get("health_score") is not None:
        out.append(("Health score", f"{s['health_score']}%"))
    return out


# ---------------------------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------------------------
def slack_body(event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Slack incoming webhook body; Discord takes the same through its Slack-compatible endpoint."""
    head = title(event, s, alert)
    text = [ln for ln in lines(event, s, alert) if not ln.startswith(("URLs crawled:", "Health score:"))]
    att = {"color": color(event, s), "title": head, "text": "\n".join(text),
           "fields": [{"title": k, "value": v, "short": True} for k, v in facts(s)],
           "footer": BRAND, "ts": int(time.time())}
    return {"text": head, "attachments": [att]}


def test_text(events: List[str]) -> str:
    names = ", ".join(EVENT_LABELS.get(e, e) for e in events) or "nothing (no event is ticked)"
    return f"This channel is connected to {BRAND}. It will get: {names}."


def slack_test_body(events: List[str]) -> Dict[str, Any]:
    return {"text": f"{BRAND} test message: {test_text(events)}"}


def _text_block(text: str, **kw: Any) -> Dict[str, Any]:
    return dict({"type": "TextBlock", "text": text, "wrap": True}, **kw)


def teams_body(event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Microsoft Teams Workflows webhook body: a message with one Adaptive Card."""
    body: List[Dict[str, Any]] = [_text_block(title(event, s, alert), size="Medium", weight="Bolder")]
    if alert:
        body.append(_text_block(str(alert.get("message") or ""), color="Attention"))
    if s.get("site"):
        body.append(_text_block(s["site"], isSubtle=True, spacing="None"))
    if event == "crawl.failed" and s.get("error"):
        body.append(_text_block(f"Error: {s['error']}", color="Attention"))
    body.append({"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts(s)]})
    if not s.get("reconciled", True):
        body.append(_text_block("The status classes do not add up to the URLs crawled: see the dashboard.",
                                color="Warning"))
    body.append(_text_block("\n\n".join(issue_lines(s))))
    cmp_ = compare_lines(s)
    if cmp_:
        body.append(_text_block("\n\n".join(cmp_)))
    if s.get("signed_in"):
        body.append(_text_block("This crawl was signed in: only counts and issue names are shared here.",
                                isSubtle=True))
    card: Dict[str, Any] = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                            "version": "1.2", "body": body}
    if s.get("dashboard_url"):
        card["actions"] = [{"type": "Action.OpenUrl", "title": "Open the dashboard", "url": s["dashboard_url"]}]
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def teams_test_body(events: List[str]) -> Dict[str, Any]:
    card = {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.2",
            "body": [_text_block(f"{BRAND} test message", size="Medium", weight="Bolder"),
                     _text_block(test_text(events))]}
    return {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive",
                                                "contentUrl": None, "content": card}]}


def generic_body(event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The generic webhook's JSON: the event, a title and text for people, and the public summary for machines."""
    out: Dict[str, Any] = {"event": event, "id": uuid.uuid4().hex, "sent_at": _now(), "source": BRAND,
                           "title": title(event, s, alert), "text": "\n".join(lines(event, s, alert)), "summary": s}
    if alert:
        out["alert"] = {k: alert.get(k) for k in ("rule_id", "name", "kind", "message", "value", "threshold")}
    return out


def generic_test_body(events: List[str]) -> Dict[str, Any]:
    return {"event": "test", "id": uuid.uuid4().hex, "sent_at": _now(), "source": BRAND,
            "title": f"{BRAND} test message", "text": test_text(events), "events": list(events)}


def _now() -> str:
    from .base import now_iso
    return now_iso()
