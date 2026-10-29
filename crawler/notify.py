"""
Email notifications (Settings > Notifications, Scheduling > Notifications): the crawl-complete email and the
test email, sent through the custom SMTP server of the settings (crawler/settings_store.py "notifications",
env CRAWLER_SMTP_HOST / _PORT / _USER / _PASSWORD / _ENCRYPTION / _FROM as fallback). Encryption: "tls"
(STARTTLS, default port 587), "ssl" (SMTPS, usually 465) or "none". Standard library only (smtplib).

  configured() -> bool                 an SMTP host is set
  status() -> {...}                    what the dialog shows (never the password)
  build_summary(crawler, site=None, index=None, compare_result=None, ...) -> summary dict (+ "home": how the
                                       start URL answered, "filter_counts" / "filter_not_checked" per
                                       "tab:filter" for the alert rules of crawler/connectors/alerts.py)
  render(summary, brand=None) -> (subject, text, html)
  send_crawl_complete(summary, to, attachments=None) -> {"ok", "sent_to", "error", "attachment_note"}
  send_test(to) -> {"ok", "sent_to", "error"}
  brand_info(reports_dir) -> the email brand: Settings > Branding (company name as the sender and signature, logo
                                       and name in the header, accent colour, footer line, subject prefix), else
                                       ARACHNIX's own mark and name (the default emails are unchanged)
  check_smtp() -> {"ok", "detail", "error"}   sign in to the SMTP server without sending (Connections > Test)
  zip_attachments(files, cap=ATTACH_CAP) -> (zip bytes | None, note)

The summary's numbers reconcile: URLs per status class (category = HTTP status, a 404 is always 4xx) add up
to the URLs crawled, the same counts as the dashboard. Every issue type is listed (name, type, priority,
URLs). A comparison with the previous crawl of the site is added when given. Attachments are zipped and
capped (ATTACH_CAP, 10 MB): a larger zip is left out and the email says so. The SMTP password never appears
in a result or an error message; pass `mask` (e.g. the crawl job's mask) to hide a login's values in the
email text.
"""
import html
import io
import os
import re
import smtplib
import socket
import ssl
import time
import zipfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

DEFAULT_PORT = 587
ATTACH_CAP = 10 * 1024 * 1024
TIMEOUT = 20
MAX_ISSUES_IN_EMAIL = 300
CATEGORY_ORDER = ("2xx", "3xx", "4xx", "5xx", "blocked", "error")
CATEGORY_TEXT = {"2xx": "Success (2xx)", "3xx": "Redirect (3xx)", "4xx": "Broken (4xx)", "5xx": "Server error (5xx)",
                 "blocked": "Blocked by robots.txt", "error": "No response"}
Attachment = Union[str, Tuple[str, bytes]]


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
def smtp_settings(settings: Optional[Dict[str, Any]] = None, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The effective SMTP settings (password included: server use only). `settings` overrides the stored ones
    (e.g. values typed in the dialog before saving, for Send Test Email)."""
    from .settings_store import effective
    cfg = effective("notifications", reports_dir)
    for k, v in (settings or {}).items():
        if k in cfg and v not in (None, "") and not (k == "smtp_password" and (v == "***" or isinstance(v, dict))):
            cfg[k] = v
    try:
        cfg["smtp_port"] = int(cfg.get("smtp_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        cfg["smtp_port"] = DEFAULT_PORT
    cfg["encryption"] = str(cfg.get("encryption") or "tls").lower()
    if cfg["encryption"] not in ("tls", "ssl", "none"):
        cfg["encryption"] = "tls"
    return cfg


def configured(settings: Optional[Dict[str, Any]] = None, reports_dir: Optional[str] = None) -> bool:
    return bool(smtp_settings(settings, reports_dir).get("smtp_host"))


def status(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    cfg = smtp_settings(None, reports_dir)
    return {"configured": bool(cfg.get("smtp_host")), "host": cfg.get("smtp_host") or "",
            "port": cfg.get("smtp_port"), "encryption": cfg.get("encryption"),
            "from": _from_address(cfg), "auth": bool(cfg.get("smtp_username")),
            "email_on_complete": bool(cfg.get("email_on_complete")), "recipients": list(cfg.get("recipients") or []),
            "attach_exports": bool(cfg.get("attach_exports")), "attach_cap_bytes": ATTACH_CAP}


def _from_address(cfg: Dict[str, Any]) -> str:
    addr = str(cfg.get("from_address") or "").strip()
    if not addr and "@" in str(cfg.get("smtp_username") or ""):
        addr = str(cfg["smtp_username"]).strip()
    if not addr:
        host = re.sub(r"[^A-Za-z0-9.-]", "", socket.gethostname() or "localhost") or "localhost"
        addr = f"arachnix@{host if '.' in host else host + '.local'}"
    return addr


# ---------------------------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------------------------
def _iso(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _duration(seconds: Optional[float]) -> str:
    if seconds is None or seconds < 0:
        return ""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s" if h else f"{m}m {s:02d}s"


def build_summary(crawler, site=None, index=None, compare_result: Optional[dict] = None,
                  dashboard_url: str = "", issues: Optional[List[dict]] = None, status_override: str = "",
                  error: str = "", links: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Everything the crawl-complete email shows, as plain data. index: TabIndex or (site, index); issues:
    the issues overview to list (default index.issues; pass the one with dismissed URLs taken out to match the
    dashboard). compare_result: compare.compare(previous, current). links: {"Excel": url, "PDF": url}."""
    from .issues import classify_result, health_score
    if isinstance(index, tuple) and len(index) == 2:
        site, index = site or index[0], index[1]
    results = list(getattr(crawler, "results_list", None) or [])
    externals = list(getattr(crawler, "external_links_list", None) or [])
    counts = {c: 0 for c in CATEGORY_ORDER}
    for r in results:
        try:
            counts[classify_result(r)] = counts.get(classify_result(r), 0) + 1
        except Exception:
            counts["error"] += 1
    h = health_score(results) if results else {"score": None, "pages": 0, "pages_ok": 0, "files": 0}
    # checked external links: an answer, or a recorded failure (no answer counts as broken)
    ext_checked = [e for e in externals if getattr(e, "status_code", None) is not None
                   or getattr(e, "http_status", "")]
    ext_broken = sum(1 for e in ext_checked if getattr(e, "status_code", None) is None
                     or e.status_code >= 400)
    try:
        stats = crawler.get_stats() if hasattr(crawler, "get_stats") else {}
    except Exception:
        stats = {}
    started = float(getattr(crawler, "start_time", 0) or 0)
    finished = float(getattr(crawler, "end_time", 0) or 0)
    issue_rows = issues if issues is not None else list(getattr(index, "issues", None) or [])
    cov = stats.get("coverage") if isinstance(stats.get("coverage"), dict) else {}
    out = {
        "site": str(getattr(crawler, "start_url", "") or ""), "mode": str(getattr(crawler, "mode", "") or "spider"),
        "status": status_override or str(getattr(crawler, "status", "") or ""), "error": str(error or ""),
        "started": _iso(started), "finished": _iso(finished),
        "duration": _duration(finished - started) if started and finished else "",
        "urls": len(results), "categories": counts, "reconciled": sum(counts.values()) == len(results),
        "pages": int(h.get("pages") or 0), "pages_ok": int(h.get("pages_ok") or 0), "health_score": h.get("score"),
        "files": int(h.get("files") or 0),
        "external": {"links": len(externals), "checked": len(ext_checked), "broken": ext_broken},
        "issues": [{"name": i.get("name") or "", "type": i.get("issue_type") or "", "priority": i.get("priority") or "",
                    "urls": int(i.get("urls") or 0)} for i in issue_rows],
        "auth_used": bool(getattr(crawler, "auth_used", False)),
        "limitations": [str(n) for n in (cov or {}).get("notes") or []][:10],
        "dashboard_url": str(dashboard_url or ""), "links": dict(links or {}),
        "compare": _compare_summary(compare_result),
        "home": home_page(crawler),
    }
    counts, not_checked = filter_counts(index)
    if counts:
        out["filter_counts"] = counts             # {"tab:filter": URLs} (alert rules; not sent to webhooks)
        out["filter_not_checked"] = not_checked   # {"tab:filter": why it did not run}
    return out


def home_page(crawler) -> Dict[str, Any]:
    """How the start URL answered, following its redirects inside the crawl: {"url", "crawled", "status",
    "category", "final_url", "final_status", "final_category", "hops"} ("crawled": False when it is not among
    the crawled URLs, e.g. a list crawl)."""
    from .issues import classify_result
    start = str(getattr(crawler, "start_url", "") or "")
    results = getattr(crawler, "results", None)
    results = results if isinstance(results, dict) else {r.url: r for r in getattr(crawler, "results_list", None) or []}
    r = results.get(start)
    if r is None and start:
        try:
            from .url_utils import normalize_url
            r = results.get(normalize_url(start) or start)
        except Exception:
            r = None
    if r is None:
        return {"url": start, "crawled": False}
    out: Dict[str, Any] = {"url": r.url, "crawled": True, "status": r.status_code, "category": classify_result(r)}
    cur, hops, seen = r, 0, {r.url}
    while classify_result(cur) == "3xx" and cur.redirect_destination and hops < 10:
        nxt = results.get(cur.redirect_destination)
        if nxt is None or nxt.url in seen:
            break
        seen.add(nxt.url)
        cur, hops = nxt, hops + 1
    out.update(final_url=cur.url, final_status=cur.status_code, final_category=classify_result(cur), hops=hops)
    return out


def filter_counts(index) -> Tuple[Dict[str, int], Dict[str, str]]:
    """({"tab:filter": URLs in the filter}, {"tab:filter": why the filter did not run}) of a TabIndex (the
    counts of the tab dropdowns); ({}, {}) without one."""
    counts: Dict[str, int] = {}
    not_checked: Dict[str, str] = {}
    tabs = getattr(index, "tabs", None)
    if not isinstance(tabs, dict):
        return counts, not_checked
    for tab_id, tr in tabs.items():
        why_tab = (getattr(tr, "not_checked", None) or {}).get("all", "")
        for fid, rows in (getattr(tr, "filter_rows", None) or {}).items():
            key = f"{tab_id}:{fid}"
            counts[key] = len(rows or [])
            why = (getattr(tr, "not_checked", None) or {}).get(fid) or why_tab
            if why and not rows:
                not_checked[key] = str(why)
    return counts, not_checked


def _compare_summary(c: Optional[dict]) -> Optional[Dict[str, Any]]:
    if not isinstance(c, dict) or c.get("error") or not c.get("a"):
        return None
    s = c.get("summary") or {}
    ch = c.get("changed") or {}
    top = [i for i in c.get("issues") or [] if i.get("new_count") or i.get("regressed_count") or i.get("fixed_count")]
    top.sort(key=lambda i: -(i.get("new_count", 0) + i.get("regressed_count", 0) + i.get("fixed_count", 0)))
    return {"previous_id": (c.get("a") or {}).get("id"), "previous_started": (c.get("a") or {}).get("started_at"),
            "new": int(s.get("new") or 0), "regressed": int(s.get("regressed") or 0),
            "fixed": int(s.get("fixed") or 0), "not_rechecked": int(s.get("not_rechecked") or 0),
            "unchanged": int(s.get("unchanged") or 0),
            "urls_added": int(ch.get("urls_added") or 0), "urls_removed": int(ch.get("urls_removed") or 0),
            "titles_changed": int(ch.get("titles_count") or 0), "status_changed": int(ch.get("status_count") or 0),
            "top": [{"name": i.get("name"), "previous": i.get("a"), "current": i.get("b"),
                     "new": i.get("new_count", 0) + i.get("regressed_count", 0), "fixed": i.get("fixed_count", 0)}
                    for i in top[:15]],
            "note": c.get("note") or ""}


def _host(url: str) -> str:
    return re.sub(r"^https?://", "", url or "").split("/")[0] or url or "site"


def render(summary: Dict[str, Any], brand: Optional[Dict[str, Any]] = None) -> Tuple[str, str, str]:
    """(subject, plain text, HTML) of the crawl-complete email. brand: brand_info() (default: the saved one)."""
    s = summary
    b = brand if brand is not None else brand_info()
    host = _host(s.get("site", ""))
    st = s.get("status") or ""
    if st in ("failed", "auth_failed") or s.get("error"):
        subject = f"Crawl failed: {host}"
    elif st == "stopped":
        subject = f"Crawl stopped: {host} - {s.get('urls', 0):,} URLs"
    else:
        hs = s.get("health_score")
        subject = f"Crawl finished: {host} - {s.get('urls', 0):,} URLs" + (f", health {hs}%" if hs is not None else "")
    cats = s.get("categories") or {}
    lines = [subject, "", f"Site: {s.get('site', '')}"]
    subject = b["subject_prefix"] + subject     # "[ARACHNIX] Crawl finished: ..." (easy to filter in a mailbox)
    if s.get("mode") == "list":
        lines.append("Mode: List")
    lines += [f"Started: {s.get('started', '')}", f"Finished: {s.get('finished', '')}"]
    if s.get("duration"):
        lines.append(f"Duration: {s['duration']}")
    if s.get("error"):
        lines += ["", f"Error: {s['error']}"]
    lines += ["", f"URLs crawled: {s.get('urls', 0):,}"]
    for c in CATEGORY_ORDER:
        lines.append(f"  {CATEGORY_TEXT[c]}: {int(cats.get(c, 0)):,}")
    if s.get("health_score") is not None:
        lines.append(f"Health score: {s['health_score']}% ({s.get('pages_ok', 0):,} of {s.get('pages', 0):,} pages load)")
    ext = s.get("external") or {}
    if ext.get("links"):
        lines.append(f"External links: {ext.get('links', 0):,} ({ext.get('checked', 0):,} checked, "
                     f"{ext.get('broken', 0):,} broken)")
    if s.get("limitations"):
        lines += ["", "This crawl covers part of the site:"] + [f"  - {n}" for n in s["limitations"]]
    issues = s.get("issues") or []
    lines += ["", f"Issues ({len(issues)} types):"]
    if not issues:
        lines.append("  None found.")
    for i in issues[:MAX_ISSUES_IN_EMAIL]:
        lines.append(f"  [{i.get('priority', '')}] {i.get('name', '')} ({i.get('type', '')}): {i.get('urls', 0):,} URLs")
    if len(issues) > MAX_ISSUES_IN_EMAIL:
        lines.append(f"  ... and {len(issues) - MAX_ISSUES_IN_EMAIL} more in the dashboard.")
    cmp_ = s.get("compare")
    if cmp_:
        lines += ["", f"Compared with the previous crawl ({cmp_.get('previous_started') or cmp_.get('previous_id')}):",
                  f"  New issues: {cmp_['new']:,}  Came back: {cmp_['regressed']:,}  Fixed: {cmp_['fixed']:,}  "
                  f"Not re-checked: {cmp_['not_rechecked']:,}",
                  f"  URLs added: {cmp_['urls_added']:,}  URLs gone: {cmp_['urls_removed']:,}  "
                  f"Titles changed: {cmp_['titles_changed']:,}  Status changed: {cmp_['status_changed']:,}"]
        for t in cmp_.get("top") or []:
            lines.append(f"  {t['name']}: {t['previous']:,} -> {t['current']:,} (+{t['new']:,} / -{t['fixed']:,})")
        if cmp_.get("note"):
            lines.append(f"  {cmp_['note']}")
    if s.get("attachment_note"):
        lines += ["", s["attachment_note"]]
    if s.get("dashboard_url"):
        lines += ["", f"Open the dashboard: {s['dashboard_url']}"]
    for label, url in (s.get("links") or {}).items():
        lines.append(f"{label}: {url}")
    lines += ["", f"Sent by {b['name']}."]
    if b.get("footer"):
        lines.append(b["footer"])
    return subject, "\n".join(lines), _html(subject, s, b)


BRAND_NAME = "ARACHNIX"
SUBJECT_PREFIX = f"[{BRAND_NAME}] "
LEGACY_FROM_NAMES = ("Site Auditor", "ARACHNIX")   # the old default sender names: shown as the new product name
BRAND_CID = "slickup-mark"
BRAND_PNG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "static", "brand",
                         "mark-180.png")


def _brand_png() -> bytes:
    """The ARACHNIX mark as PNG bytes for the email header ('' when the file is missing)."""
    try:
        with open(BRAND_PNG, "rb") as f:
            return f.read()
    except OSError:
        return b""


LOGO_CID = "brand-logo"
LINK_COLOR = "#244B30"


def brand_info(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The email brand from Settings > Branding (settings_store.branding): {"custom", "name", "company_name",
    "subject_prefix", "accent", "link_color", "footer", "logo": (bytes, subtype) | None, "logo_size": (w, h)}.
    Without branding (or when it cannot be read): ARACHNIX's own name, prefix and mark."""
    try:
        from .settings_store import branding, branding_logo
        b = branding(reports_dir)
        logo = branding_logo(reports_dir) if b.get("logo_path") else None
    except Exception:
        b, logo = {}, None
    name = str(b.get("name") or BRAND_NAME)
    accent = str(b.get("accent_color") or "#2F6B3F")
    return {"custom": bool(b.get("custom")), "name": name, "company_name": str(b.get("company_name") or ""),
            "subject_prefix": str(b.get("subject_prefix") or SUBJECT_PREFIX), "accent": accent,
            "link_color": accent if b.get("accent_custom") and _contrast(accent) >= 4.5 else LINK_COLOR,
            "footer": str(b.get("footer") or ""),
            "logo": (logo[0], logo[1].split("/", 1)[-1]) if logo else None,
            "logo_size": (int(b.get("logo_width") or 0), int(b.get("logo_height") or 0))}


def _contrast(hex_color: str) -> float:
    """WCAG contrast ratio of a #RRGGBB colour against white (a link colour must stay readable)."""
    try:
        rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    except (ValueError, IndexError):
        return 0.0
    lum = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 1.05 / (0.2126 * lum[0] + 0.7152 * lum[1] + 0.0722 * lum[2] + 0.05)


def _brand_header_html(brand: Optional[Dict[str, Any]] = None) -> str:
    """Email header: the mark (inline image) + the ARACHNIX name, or the name alone when the image is missing.
    With a brand: its logo (inline image) and company name, in its accent colour."""
    e = html.escape
    b = brand or {}
    box = ('style="margin:0 0 14px;padding:0 0 10px;border-bottom:1px solid #e6e6e6;font-size:18px;'
           'font-weight:700;color:#1C211E"')
    if b.get("custom"):
        logo = ""
        if b.get("logo"):
            w, h = b.get("logo_size") or (0, 0)
            width = max(1, min(240, round(32 * w / h))) if w and h else 32
            logo = (f'<img src="cid:{LOGO_CID}" width="{width}" height="32" alt="" style="vertical-align:middle;'
                    'border:0;margin-right:10px">')
        name = (f'<span style="vertical-align:middle;color:{e(b.get("accent") or "#1C211E")}">'
                f'{e(b.get("company_name") or "")}</span>') if b.get("company_name") else ""
        if not logo and not name:
            name = f'<span style="vertical-align:middle">{e(b.get("name") or BRAND_NAME)}</span>'
        return f"<div {box}>{logo}{name}</div>"
    mark = (f'<img src="cid:{BRAND_CID}" width="28" height="28" alt="" style="vertical-align:middle;border:0;'
            'border-radius:6px;margin-right:8px">') if os.path.exists(BRAND_PNG) else ""
    return (f'<div {box}>{mark}<span style="vertical-align:middle;font-weight:800;letter-spacing:.06em">'
            f'{e(BRAND_NAME)}</span></div>')


def _plain_subject(subject: str, prefix: str = SUBJECT_PREFIX) -> str:
    """The subject without the "[ARACHNIX] " prefix (the email header already shows the name)."""
    return subject[len(prefix):] if prefix and subject.startswith(prefix) else subject


def _from_name(cfg: Dict[str, Any], brand: Optional[Dict[str, Any]] = None) -> str:
    """The sender name: the one set under Settings > Notifications, else the brand's company name, else ARACHNIX
    (the default "ARACHNIX" and the old "Site Auditor" count as not set when a company name is branded)."""
    name = str(cfg.get("from_name") or "").strip()
    company = str((brand or {}).get("company_name") or "").strip()
    if not name or name in LEGACY_FROM_NAMES or (company and name == BRAND_NAME):
        return company or BRAND_NAME
    return name


def _html(subject: str, s: Dict[str, Any], brand: Optional[Dict[str, Any]] = None) -> str:
    b = brand if brand is not None else brand_info()
    e = html.escape
    cats = s.get("categories") or {}
    colors = {"2xx": "#2E7D32", "3xx": "#1565C0", "4xx": "#C62828", "5xx": "#8E24AA", "blocked": "#6D6D6D",
              "error": "#EF6C00"}
    td = 'style="padding:4px 10px;border-bottom:1px solid #e6e6e6"'
    th = 'style="padding:4px 10px;border-bottom:2px solid #cfcfcf;text-align:left"'
    parts = ['<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b1b1b;max-width:760px">',
             _brand_header_html(b),
             f'<h2 style="margin:0 0 6px;font-size:19px">{e(_plain_subject(subject, b["subject_prefix"]))}</h2>',
             f'<p style="margin:0 0 12px;color:#555">{e(s.get("site", ""))}<br>{e(s.get("started", ""))} to '
             f'{e(s.get("finished", ""))}{" (" + e(s["duration"]) + ")" if s.get("duration") else ""}</p>']
    if s.get("error"):
        parts.append(f'<p style="color:#C62828"><b>Error:</b> {e(s["error"])}</p>')
    parts.append(f'<table style="border-collapse:collapse;margin-bottom:12px"><tr><th {th}>URLs crawled</th>'
                 f'<th {th} align="right">{int(s.get("urls", 0)):,}</th></tr>')
    for c in CATEGORY_ORDER:
        parts.append(f'<tr><td {td}><span style="color:{colors[c]}">&#9632;</span> {e(CATEGORY_TEXT[c])}</td>'
                     f'<td {td} align="right">{int(cats.get(c, 0)):,}</td></tr>')
    if s.get("health_score") is not None:
        parts.append(f'<tr><td {td}><b>Health score</b></td><td {td} align="right"><b>{e(str(s["health_score"]))}%'
                     f'</b></td></tr>')
    parts.append("</table>")
    if s.get("limitations"):
        parts.append('<p style="color:#8a5a00"><b>This crawl covers part of the site:</b><br>'
                     + "<br>".join(e(n) for n in s["limitations"]) + "</p>")
    issues = s.get("issues") or []
    parts.append(f'<h3 style="font-size:16px;margin:14px 0 6px">Issues ({len(issues)} types)</h3>')
    if issues:
        parts.append(f'<table style="border-collapse:collapse"><tr><th {th}>Issue</th><th {th}>Type</th>'
                     f'<th {th}>Priority</th><th {th} align="right">URLs</th></tr>')
        for i in issues[:MAX_ISSUES_IN_EMAIL]:
            parts.append(f'<tr><td {td}>{e(i.get("name", ""))}</td><td {td}>{e(i.get("type", ""))}</td>'
                         f'<td {td}>{e(i.get("priority", ""))}</td><td {td} align="right">{int(i.get("urls", 0)):,}'
                         '</td></tr>')
        parts.append("</table>")
    else:
        parts.append("<p>None found.</p>")
    cmp_ = s.get("compare")
    if cmp_:
        parts.append(f'<h3 style="font-size:16px;margin:14px 0 6px">Compared with the previous crawl '
                     f'({e(str(cmp_.get("previous_started") or cmp_.get("previous_id") or ""))})</h3>'
                     f'<p>New issues: <b>{cmp_["new"]:,}</b> &middot; Came back: <b>{cmp_["regressed"]:,}</b> &middot; '
                     f'Fixed: <b>{cmp_["fixed"]:,}</b> &middot; Not re-checked: {cmp_["not_rechecked"]:,}<br>'
                     f'URLs added: {cmp_["urls_added"]:,} &middot; URLs gone: {cmp_["urls_removed"]:,} &middot; '
                     f'Titles changed: {cmp_["titles_changed"]:,} &middot; Status changed: {cmp_["status_changed"]:,}</p>')
        if cmp_.get("top"):
            parts.append(f'<table style="border-collapse:collapse"><tr><th {th}>Issue</th><th {th} align="right">Before'
                         f'</th><th {th} align="right">Now</th><th {th} align="right">New</th>'
                         f'<th {th} align="right">Fixed</th></tr>')
            for t in cmp_["top"]:
                parts.append(f'<tr><td {td}>{e(str(t["name"]))}</td><td {td} align="right">{t["previous"]:,}</td>'
                             f'<td {td} align="right">{t["current"]:,}</td><td {td} align="right">{t["new"]:,}</td>'
                             f'<td {td} align="right">{t["fixed"]:,}</td></tr>')
            parts.append("</table>")
    if s.get("attachment_note"):
        parts.append(f'<p style="color:#555">{e(s["attachment_note"])}</p>')
    if s.get("dashboard_url"):
        parts.append(f'<p><a href="{e(s["dashboard_url"], quote=True)}" style="color:{e(b["link_color"])};'
                     'font-weight:600">Open the dashboard</a></p>')
    for label, url in (s.get("links") or {}).items():
        parts.append(f'<p><a href="{e(url, quote=True)}">{e(label)}</a></p>')
    parts.append(f'<p style="color:#888;font-size:12px">Sent by {e(b["name"])}.'
                 + (f'<br>{e(b["footer"])}' if b.get("footer") else "") + '</p></div>')
    return "".join(parts)


# ---------------------------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------------------------
def zip_attachments(files: Iterable[Attachment], cap: int = ATTACH_CAP) -> Tuple[Optional[bytes], str]:
    """(zip bytes, "") of the files (paths or (name, bytes)), or (None, why) when there are none or the zip is
    larger than `cap`."""
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in files or ():
            if isinstance(f, tuple) and len(f) == 2:
                name, data = f
                zf.writestr(os.path.basename(str(name)) or f"file{count + 1}", data)
                count += 1
            elif isinstance(f, str) and os.path.isfile(f):
                zf.write(f, os.path.basename(f))
                count += 1
            if buf.tell() > cap:
                break
    if not count:
        return None, ""
    data = buf.getvalue()
    if len(data) > cap:
        return None, (f"The exports ({len(data) / 1048576:.1f} MB zipped) are larger than the "
                      f"{cap / 1048576:.0f} MB limit for an email, so they are not attached: download them from "
                      "the dashboard.")
    return data, ""


# ---------------------------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------------------------
def _plain_error(exc: BaseException, cfg: Dict[str, Any]) -> str:
    host, port = cfg.get("smtp_host"), cfg.get("smtp_port")
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        msg = "The SMTP server refused the username or password."
    elif isinstance(exc, smtplib.SMTPNotSupportedError):
        msg = (f"The SMTP server on port {port} does not offer encryption (STARTTLS): choose SSL or None, or "
               "another port.")
    elif isinstance(exc, smtplib.SMTPRecipientsRefused):
        msg = "The SMTP server refused the recipients: " + ", ".join(sorted(exc.recipients))[:300]
    elif isinstance(exc, smtplib.SMTPSenderRefused):
        msg = f"The SMTP server refused the sender address ({exc.sender})."
    elif isinstance(exc, ssl.SSLError):
        msg = f"The secure connection to {host}:{port} failed ({exc.reason or exc}). Check the encryption setting."
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        msg = f"The SMTP server {host}:{port} did not answer in time."
    elif isinstance(exc, ConnectionRefusedError):
        msg = f"Could not connect to the SMTP server {host}:{port} (connection refused)."
    elif isinstance(exc, socket.gaierror):
        msg = f"The SMTP host '{host}' could not be found."
    elif isinstance(exc, smtplib.SMTPServerDisconnected):
        msg = f"The SMTP server {host}:{port} closed the connection ({exc})."
    elif isinstance(exc, smtplib.SMTPException):
        msg = f"The SMTP server answered with an error: {exc}"
    else:
        msg = f"Sending failed: {type(exc).__name__}: {exc}"
    secret = str(cfg.get("smtp_password") or "")
    if len(secret) >= 3:
        msg = msg.replace(secret, "***")
    return msg[:400]


def send(to: Union[str, List[str]], subject: str, text: str, html_body: Optional[str] = None,
         attachments: Optional[List[Tuple[str, bytes, str]]] = None, settings: Optional[Dict[str, Any]] = None,
         reports_dir: Optional[str] = None, brand: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Sends one email. attachments: [(file name, bytes, mime type "type/subtype")]. brand: brand_info() (default:
    the saved one) for the sender name and the header logo. Returns {"ok", "sent_to", "error"} (never raises)."""
    from .settings_store import parse_emails
    cfg = smtp_settings(settings, reports_dir)
    b = brand if brand is not None else brand_info(reports_dir)
    recipients, bad = parse_emails(to)
    if not cfg.get("smtp_host"):
        return {"ok": False, "sent_to": [], "error": "Email is not set up: enter the SMTP server under Settings > "
                                                     "Notifications (or set CRAWLER_SMTP_HOST)."}
    if not recipients:
        return {"ok": False, "sent_to": [], "error": "No valid recipient address" + (f" ({', '.join(bad[:3])})" if bad
                                                                                       else ".")}
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((_from_name(cfg, b), _from_address(cfg)))
    msg["To"] = ", ".join(recipients)
    msg["Message-ID"] = make_msgid(domain=_from_address(cfg).split("@")[-1])
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
        logo = _brand_png() if BRAND_CID in html_body else b""
        if logo:   # the mark in the email header, as an inline image (most mail apps block remote images)
            msg.get_payload()[-1].add_related(logo, maintype="image", subtype="png", cid=f"<{BRAND_CID}>",
                                              filename="arachnix.png", disposition="inline")
        if f"cid:{LOGO_CID}" in html_body and b.get("logo"):      # the brand's own logo (Settings > Branding)
            data, subtype = b["logo"]
            msg.get_payload()[-1].add_related(data, maintype="image", subtype=subtype, cid=f"<{LOGO_CID}>",
                                              filename=f"logo.{'jpg' if subtype == 'jpeg' else subtype}",
                                              disposition="inline")
    for name, data, mime in attachments or []:
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=name)
    server = None
    try:
        server = _open(cfg)
        refused = server.send_message(msg) or {}
        sent = [r for r in recipients if r not in refused]
        out = {"ok": bool(sent), "sent_to": sent, "error": ""}
        if refused:
            out["error"] = "Refused by the server: " + ", ".join(sorted(refused))[:300]
        if bad:
            out["skipped"] = bad
        return out
    except Exception as e:
        return {"ok": False, "sent_to": [], "error": _plain_error(e, cfg)}
    finally:
        _close(server)


def _open(cfg: Dict[str, Any]) -> smtplib.SMTP:
    """A connection to the SMTP server of `cfg`, encrypted as set and signed in when a username is set."""
    host, port, enc = cfg["smtp_host"], int(cfg["smtp_port"]), cfg["encryption"]
    ctx = ssl.create_default_context()
    if enc == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    else:
        server = smtplib.SMTP(host, port, timeout=TIMEOUT)
    try:
        if enc != "ssl":
            server.ehlo()
            if enc == "tls":
                server.starttls(context=ctx)
                server.ehlo()
        if cfg.get("smtp_username"):
            server.login(str(cfg["smtp_username"]), str(cfg.get("smtp_password") or ""))
    except Exception:
        _close(server)
        raise
    return server


def _close(server: Optional[smtplib.SMTP]) -> None:
    if server is None:
        return
    try:
        server.quit()
    except Exception:
        try:
            server.close()
        except Exception:
            pass


def check_smtp(settings: Optional[Dict[str, Any]] = None, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """Connects to the SMTP server, sets up the encryption and signs in, without sending an email (Connections >
    Email > Test). Returns {"ok", "detail", "error"} (never raises; the password never appears)."""
    cfg = smtp_settings(settings, reports_dir)
    if not cfg.get("smtp_host"):
        return {"ok": False, "detail": "", "error": "Email is not set up: enter the SMTP server under Settings > "
                                                    "Notifications (or set CRAWLER_SMTP_HOST)."}
    server = None
    try:
        server = _open(cfg)
        code, _msg = server.noop()
        signed = " and signed in" if cfg.get("smtp_username") else ""
        enc = {"tls": "STARTTLS", "ssl": "SSL"}.get(cfg["encryption"], "no encryption")
        return {"ok": 200 <= int(code) < 400, "error": "" if 200 <= int(code) < 400 else f"The server answered {code}.",
                "detail": f"Connected to {cfg['smtp_host']}:{cfg['smtp_port']} ({enc}){signed}."}
    except Exception as e:
        return {"ok": False, "detail": "", "error": _plain_error(e, cfg)}
    finally:
        _close(server)


def send_crawl_complete(summary: Dict[str, Any], to: Union[str, List[str]],
                        attachments: Optional[Iterable[Attachment]] = None, settings: Optional[Dict[str, Any]] = None,
                        reports_dir: Optional[str] = None, mask: Optional[Callable[[str], str]] = None,
                        cap: int = ATTACH_CAP) -> Dict[str, Any]:
    """Emails the crawl summary (build_summary) to `to`. attachments: export file paths or (name, bytes), sent
    as one zip when it fits under `cap`. mask: hides secrets in the text (e.g. CrawlJob.mask)."""
    s = dict(summary or {})
    if not s.get("dashboard_url"):
        s["dashboard_url"] = str(smtp_settings(settings, reports_dir).get("dashboard_url") or "")
    files = []
    if attachments:
        data, note = zip_attachments(attachments, cap)
        if data:
            stamp = time.strftime("%Y%m%d_%H%M")
            files.append((f"crawl-exports_{_host(s.get('site', '')).replace(':', '_')}_{stamp}.zip", data,
                          "application/zip"))
        s["attachment_note"] = note
    brand = brand_info(reports_dir)
    subject, text, html_body = render(s, brand)
    if mask is not None:
        subject, text, html_body = mask(subject), mask(text), mask(html_body)
    out = send(to, subject, text, html_body, files, settings, reports_dir, brand=brand)
    out["attachment_note"] = s.get("attachment_note", "")
    out["attached"] = bool(files)
    out["subject"] = subject
    return out


def send_test(to: Union[str, List[str]], settings: Optional[Dict[str, Any]] = None,
              reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """Settings > Notifications > Send Test Email."""
    cfg = smtp_settings(settings, reports_dir)
    b = brand_info(reports_dir)
    server = f"{cfg.get('smtp_host')}:{cfg.get('smtp_port')} ({cfg.get('encryption', '').upper()})"
    text = (f"This is a test email from {b['name']}.\n\n"
            f"SMTP server: {server}\n"
            "If you can read this, crawl notifications will reach this address.")
    html_body = None
    if b["custom"]:             # a branded test shows the header, logo and footer the crawl emails will have
        e = html.escape
        text += f"\n\nSent by {b['name']}." + (f"\n{b['footer']}" if b.get("footer") else "")
        html_body = ('<div style="font-family:Segoe UI,Arial,sans-serif;font-size:14px;color:#1b1b1b;max-width:760px">'
                     + _brand_header_html(b) + f'<p>This is a test email from {e(b["name"])}.</p>'
                     f'<p style="color:#555">SMTP server: {e(server)}<br>If you can read this, crawl notifications '
                     'will reach this address.</p>'
                     f'<p style="color:#888;font-size:12px">Sent by {e(b["name"])}.'
                     + (f'<br>{e(b["footer"])}' if b.get("footer") else "") + "</p></div>")
    return send(to, f"{b['subject_prefix']}Test email from {b['name']}", text, html_body, None, settings,
                reports_dir, brand=b)
