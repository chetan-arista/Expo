"""
Push developer tickets to Jira Cloud or GitHub Issues (PLAN4_build.md N4; POST /api/tickets/push,
GET /api/tickets/pushed). The connectors themselves are jira.py and github_issues.py; this module builds the tickets,
applies the private-crawl rule, remembers what was pushed and runs the push.

  push(connector_id, source, ids, *, intel_result=None, issues=None, index=None, site=None, crawler=None,
       evidence=None, evidence_dir="", dismissed=None, site_url="", dashboard_url="", dry_run=True,
       confirm_private=False, mask=None)
      -> {"connector", "service", "target", "dry_run", "count", "created": [{"ref", "title", "key", "url", ...}],
          "skipped": [{"ref", "title", "reason", "key", "url"}], "errors": [{"ref", "title", "error"}],
          "missing": [ids not found], "preview": [...] (dry run), "private", "private_notice", "needs_confirm",
          "refused", "message"}
  pushed_items(connector_id="") -> [{"fingerprint", "connector", "target", "site", "source", "ref", "title", "key",
                                     "url", "at"}]  newest first
  forget(fingerprint) -> bool       (admin) push that ticket again, e.g. after it was deleted in the tracker

Tickets come from crawler.tickets.make_tickets, so they read like the ticket exports:
  source "fix_plan"  ids = Fix plan group ids (the /api/intel/errors result, dismissed URLs already left out)
  source "issues"    ids = Issues tab rows "tab:filter": one ticket per issue filter with up to 5 sample URLs of the
                     filter (dismissed URLs left out), its description as the cause and its fix as the step.
At most MAX_PUSH tickets per push, in fix-first order.

A pushed ticket is remembered by a fingerprint of (service and project or repository, site host, source, group or
issue id) in <reports_dir>/connectors/tickets_pushed.json: pushing it again creates nothing and answers "already
pushed" with the existing key and link.

Signed-in crawls (PLAN4_build.md 10.8): the preview says how many URLs and screenshots would go to the service and
the push needs confirm_private. GitHub reads the repository's visibility first and refuses every ticket of a
signed-in crawl when the repository is public. GitHub has no API for uploading pictures, so its tickets name the
screenshot ARACHNIX keeps and until when (never a link that carries the access key); Jira receives the screenshots
as attachments (unless switched off). Every text that leaves passes the crawl's mask (login values) and the
connector's masker (its token).
"""
import hashlib
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .base import Connector, ConnectorError, now_iso
from . import store

SOURCES = ("fix_plan", "issues")
MAX_PUSH = 25
PUSHED_FILE = "tickets_pushed.json"
MAX_PUSHED = 5000
SAMPLE_URLS = 5
BODY_PREVIEW = 6000
_lock = threading.RLock()


# ---------------------------------------------------------------------------------------------
# The base class of the ticket connectors
# ---------------------------------------------------------------------------------------------
class TicketConnector(Connector):
    """A tracker ARACHNIX can create tickets in. Subclasses implement target(), create_ticket() and body()."""

    kind = "tickets"
    service = ""                         # "Jira" | "GitHub"
    uploads_screenshots = False

    def target(self) -> str:
        """"<service>:<project or repository>" (part of each fingerprint)."""
        return ""

    def target_label(self) -> str:
        return self.target().split(":", 1)[-1]

    def target_info(self, dry_run: bool = False) -> Dict[str, Any]:
        """{"public": True | False | None, "detail"} about where the tickets go (GitHub reads the visibility)."""
        return {"public": None, "detail": ""}

    def refuse_private(self, info: Dict[str, Any]) -> str:
        """Why a signed-in crawl may not be pushed there at all ("" = allowed after the confirm)."""
        return ""

    def attach_screenshots(self) -> bool:
        return False

    def extra_labels(self) -> List[str]:
        return []

    def body(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> str:
        """The ticket text as the service will show it (plain or Markdown; for the preview)."""
        return ""

    def create_ticket(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Creates one ticket: {"key", "url", "attachments": n, "attachment_errors": [...], "notes": [...]}.
        Raises ConnectorError (masked, kind "auth" / "rate" stop the rest of the push)."""
        raise ConnectorError("This connector cannot create tickets.", "invalid")


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def private_crawl(site, crawler) -> bool:
    """The crawl was signed in (same rule as the other connectors and fixfiles)."""
    cfg = getattr(site, "config", None) or getattr(crawler, "config", None)
    auth = getattr(cfg, "auth", None)
    return bool(getattr(crawler, "auth_used", False)) or bool(
        getattr(auth, "enabled", False) and (getattr(auth, "auth_type", "none") or "none") != "none")


def site_host(site_url: str) -> str:
    try:
        return (urlsplit(str(site_url or "")).hostname or "").lower()
    except ValueError:
        return ""


def fingerprint(target: str, host: str, source: str, ref: str) -> str:
    raw = "\n".join((str(target or ""), str(host or ""), str(source or ""), str(ref or "")))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def retention_hours() -> float:
    """How long the server keeps crawl screenshots (web/app.py REPORT_RETENTION_HOURS)."""
    try:
        return max(1.0, float(os.environ.get("CRAWLER_REPORT_RETENTION_HOURS") or 72))
    except ValueError:
        return 72.0


def kept_until(path: str) -> str:
    """The date (UTC) the server deletes a screenshot: its evidence folder's last change + the retention."""
    try:
        folder = os.path.dirname(path)
        base = os.path.getmtime(folder) if os.path.isdir(folder) else os.path.getmtime(path)
    except OSError:
        return ""
    return datetime.fromtimestamp(base + retention_hours() * 3600, tz=timezone.utc).strftime("%Y-%m-%d")


def screenshot_note(t: Dict[str, Any], uploaded: bool) -> str:
    """What the ticket says about its screenshots (Jira: attached; GitHub: kept in ARACHNIX until <date>)."""
    names = list(t.get("screenshots") or [])
    if not names:
        return ""
    have = [n for n in names if n in (t.get("shot_files") or {})]
    gone = [n for n in names if n not in have]
    if uploaded and have:
        text = f"{len(have)} screenshot{'s' if len(have) != 1 else ''} attached to this ticket ({', '.join(have)})."
    elif have:
        dates = sorted(d for d in (kept_until(t["shot_files"][n]) for n in have) if d)
        until = f" until {dates[0]}" if dates else ""
        text = (f"Screenshot{'s' if len(have) != 1 else ''} kept in ARACHNIX{until}: {', '.join(have)} "
                f"(Fix plan / ticket exports of this crawl). This tracker cannot receive uploads from ARACHNIX.")
    else:
        text = ""
    if gone:
        text = (text + " " if text else "") + (f"No longer on the server (crawl again to capture): {', '.join(gone)}.")
    return text


def _mask_all(value: Any, mask: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return mask(value)
    if isinstance(value, list):
        return [_mask_all(v, mask) for v in value]
    if isinstance(value, dict):
        return {k: (v if k in ("shot_files", "screenshots") else _mask_all(v, mask)) for k, v in value.items()}
    return value


def _combine(*masks: Optional[Callable[[str], str]]) -> Callable[[str], str]:
    usable = [m for m in masks if m is not None]

    def run(text: str) -> str:
        out = str(text if text is not None else "")
        for m in usable:
            try:
                out = m(out)
            except Exception:
                pass
        return out
    return run


# ---------------------------------------------------------------------------------------------
# Building the tickets
# ---------------------------------------------------------------------------------------------
def _issue_key(value: Any) -> str:
    """"tab:filter" from "tab:filter", "tab/filter" or {"tab", "filter"}."""
    if isinstance(value, dict):
        return f"{value.get('tab') or ''}:{value.get('filter') or ''}"
    text = str(value or "").strip()
    if ":" not in text and "/" in text:
        text = text.replace("/", ":", 1)
    return text


def _issue_groups(ids: List[str], issues: Optional[List[dict]], index, dismissed: Optional[dict]
                  ) -> Tuple[List[dict], List[str]]:
    """Ticket groups (the root_cause shape) for Issues tab rows; ids not in the Issues tab are returned apart."""
    rows = list(issues if issues is not None else (getattr(index, "issues", None) or []))
    by_key = {f"{i.get('tab')}:{i.get('filter')}": i for i in rows if isinstance(i, dict)}
    groups, missing = [], []
    drop_all = dismissed if isinstance(dismissed, dict) else {}
    for raw in ids:
        key = _issue_key(raw)
        issue = by_key.get(key)
        if issue is None:
            missing.append(str(raw)[:120])
            continue
        tab, flt = key.split(":", 1)
        tr = (getattr(index, "tabs", None) or {}).get(tab) if index is not None else None
        hits = list((getattr(tr, "filter_rows", None) or {}).get(flt) or []) if tr is not None else []
        drop = set(drop_all.get(key) or ())
        urls = [getattr(r, "url", "") for r in hits if getattr(r, "url", "") and getattr(r, "url", "") not in drop]
        groups.append({
            "id": key, "title": str(issue.get("name") or key), "category": str(issue.get("tab_name") or tab),
            "urls_count": int(issue.get("urls") or len(urls)), "sample_urls": urls[:SAMPLE_URLS],
            "issues": [str(issue.get("name") or key)], "root_cause": str(issue.get("description") or ""),
            "why": "", "prevent": "", "fix_steps": [str(issue.get("fix") or "")] if issue.get("fix") else [],
            "platform_steps": [], "effort": "", "impact": 0, "rank": 0, "verified": "detected",
            "issue_keys": [{"tab": tab, "filter": flt}], "intentional": False, "where": "", "element": "",
        })
    return groups, missing


def build_tickets(source: str, ids: List[Any], intel_result: Optional[dict] = None,
                  issues: Optional[List[dict]] = None, index=None, evidence: Any = None,
                  dismissed: Optional[dict] = None, site_url: str = "", evidence_dir: str = ""
                  ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """(tickets in fix-first order with their screenshot files located, ids that were not found)."""
    from .. import tickets as tk
    if source == "fix_plan":
        wanted = [str(i or "").strip() for i in ids]
        groups_all = list((intel_result or {}).get("groups") or [])
        by_id = {str(g.get("id") or ""): g for g in groups_all if isinstance(g, dict)}
        groups = [by_id[i] for i in dict.fromkeys(wanted) if i in by_id]
        missing = [i[:120] for i in dict.fromkeys(wanted) if i not in by_id]
        result = {"groups": groups, "platform": (intel_result or {}).get("platform") or {}}
    elif source == "issues":
        groups, missing = _issue_groups(list(dict.fromkeys(_issue_key(i) for i in ids)), issues, index, dismissed)
        result = {"groups": groups, "platform": (intel_result or {}).get("platform") or {}}
    else:
        raise ConnectorError("Choose what to push: Fix plan groups or Issues.", "invalid")
    made = tk.make_tickets(result, evidence)
    if source == "issues":                      # an issue filter has no effort estimate: no "effort-medium" label
        for t in made:
            t["labels"] = [x for x in t["labels"] if not x.startswith("effort-")]
    try:
        tk.locate_screenshots(made, site_url, evidence_dir)
    except Exception:
        pass
    return made, missing


# ---------------------------------------------------------------------------------------------
# What was pushed
# ---------------------------------------------------------------------------------------------
def _pushed_path() -> str:
    return os.path.join(store.folder(), PUSHED_FILE)


def _load_pushed() -> Dict[str, Dict[str, Any]]:
    data = store.read_json(_pushed_path())
    items = data.get("items") if isinstance(data, dict) else None
    return dict(items) if isinstance(items, dict) else {}


def _save_pushed(items: Dict[str, Dict[str, Any]]) -> None:
    if len(items) > MAX_PUSHED:
        keep = sorted(items.values(), key=lambda e: str(e.get("at") or ""))[-MAX_PUSHED:]
        items = {e["fingerprint"]: e for e in keep}
    store.write_json(_pushed_path(), {"items": items})


def pushed_items(connector_id: str = "") -> List[Dict[str, Any]]:
    """GET /api/tickets/pushed: every remembered ticket (newest first), optionally for one connector."""
    with _lock:
        items = list(_load_pushed().values())
    if connector_id:
        items = [e for e in items if e.get("connector") == connector_id]
    items.sort(key=lambda e: str(e.get("at") or ""), reverse=True)
    keys = ("fingerprint", "connector", "target", "site", "source", "ref", "title", "key", "url", "at")
    return [{k: e.get(k) for k in keys} for e in items]


def forget(fp: str) -> bool:
    """Forgets one pushed ticket (admin), so it can be pushed again."""
    with _lock:
        items = _load_pushed()
        if items.pop(str(fp or ""), None) is None:
            return False
        _save_pushed(items)
        return True


def _remember(entry: Dict[str, Any]) -> None:
    with _lock:
        items = _load_pushed()
        items[entry["fingerprint"]] = entry
        _save_pushed(items)


# ---------------------------------------------------------------------------------------------
# The push
# ---------------------------------------------------------------------------------------------
def connector(connector_id: str) -> TicketConnector:
    from . import registry
    c = registry.get(str(connector_id or ""))
    if not isinstance(c, TicketConnector):
        raise ConnectorError("Choose Jira or GitHub Issues.", "invalid")
    return c


def _private_notice(c: TicketConnector, n_urls: int, n_shots: int, info: Dict[str, Any]) -> str:
    what = f"{n_urls:,} URL{'s' if n_urls != 1 else ''}"
    if n_shots:
        what += f" and {n_shots:,} screenshot{'s' if n_shots != 1 else ''}"
    where = c.target_label()
    if info.get("public") is False:
        where += ", a private repository" if c.service == "GitHub" else ""
    verb = "goes" if n_urls == 1 and not n_shots else "go"
    return (f"This crawl was signed in: {what} {verb} to {c.service} ({where}). Pages behind a sign-in may be "
            "private: confirm to push.")


def push(connector_id: str, source: str, ids: List[Any], *, intel_result: Optional[dict] = None,
         issues: Optional[List[dict]] = None, index=None, site=None, crawler=None, evidence: Any = None,
         evidence_dir: str = "", dismissed: Optional[dict] = None, site_url: str = "", dashboard_url: str = "",
         dry_run: bool = True, confirm_private: bool = False,
         mask: Optional[Callable[[str], str]] = None) -> Dict[str, Any]:
    """POST /api/tickets/push. Raises ConnectorError (400) when the push cannot start; per-ticket problems are
    listed under "errors"."""
    c = connector(connector_id)
    if not c.is_set_up():
        raise ConnectorError(f"Not connected - connect {c.name} under Connections.", "invalid")
    if source not in SOURCES:
        raise ConnectorError("Choose what to push: Fix plan groups or Issues.", "invalid")
    ids = [i for i in (ids or []) if str(i or "").strip() or isinstance(i, dict)]
    if not ids:
        raise ConnectorError("Choose at least one Fix plan group or issue to push.", "invalid")
    if len(ids) > MAX_PUSH:
        raise ConnectorError(f"At most {MAX_PUSH} tickets per push: choose fewer and push the rest next.", "invalid")
    if source == "fix_plan" and not (intel_result or {}).get("groups"):
        raise ConnectorError("There is no Fix plan for this crawl yet: crawl the site first.", "invalid")
    if source == "issues" and index is None and not issues:
        raise ConnectorError("There is no crawl to take the issues from: crawl the site first.", "invalid")
    site_url = site_url or str(getattr(crawler, "start_url", "") or getattr(site, "start_url", "") or "")
    tickets, missing = build_tickets(source, ids, intel_result, issues, index, evidence, dismissed, site_url,
                                     evidence_dir)
    masking = _combine(mask, c.masker())
    tickets = [_mask_all(t, masking) for t in tickets]
    host = site_host(site_url)
    target = c.target()
    private = private_crawl(site, crawler)
    attach = c.attach_screenshots()
    info = c.target_info(dry_run=dry_run)
    refusal = c.refuse_private(info) if private else ""
    ctx = {"site_url": masking(site_url), "dashboard_url": dashboard_url, "attach": attach, "mask": masking}
    with _lock:
        known = _load_pushed()
    n_urls = len({u for t in tickets for u in t.get("sample_urls") or []})
    n_shots = sum(len(t.get("shot_files") or {}) for t in tickets) if attach else 0
    out: Dict[str, Any] = {
        "connector": c.id, "service": c.service, "target": c.target_label(), "dry_run": bool(dry_run),
        "count": len(tickets), "created": [], "skipped": [], "errors": [], "missing": missing,
        "private": private, "private_notice": None, "needs_confirm": False, "refused": bool(refusal),
        "visibility": info.get("detail") or "", "message": "",
    }
    if private:
        out["private_notice"] = refusal or _private_notice(c, n_urls, n_shots, info)
        out["needs_confirm"] = not refusal and not confirm_private
    if dry_run:
        preview = []
        for t in tickets:
            fp = fingerprint(target, host, source, t["group_id"])
            prev = known.get(fp)
            preview.append({
                "ref": t["group_id"], "id": t["id"], "title": t["title"], "priority": t["priority"],
                "labels": merged_labels(t, c), "urls_count": t["urls_count"], "sample_urls": t["sample_urls"],
                "screenshots": list((t.get("shot_files") or {}).keys()) if attach else [],
                "screenshot_note": screenshot_note(t, attach),
                "fingerprint": fp, "already": ({"key": prev.get("key"), "url": prev.get("url"), "at": prev.get("at")}
                                               if prev else None),
                "body": c.body(t, ctx)[:BODY_PREVIEW]})
        out["preview"] = preview
        new = sum(1 for p in preview if not p["already"])
        out["message"] = (refusal or f"{new:,} new ticket{'s' if new != 1 else ''} would be created in "
                                     f"{c.service} ({c.target_label()}); {len(preview) - new:,} already pushed.")
        return out
    if refusal:
        out["message"] = refusal
        return out
    if out["needs_confirm"]:
        out["message"] = "Nothing was pushed yet: this crawl was signed in, so confirm first."
        return out
    stop = ""
    for t in tickets:
        ref = t["group_id"]
        fp = fingerprint(target, host, source, ref)
        with _lock:
            prev = _load_pushed().get(fp)
        if prev:
            out["skipped"].append({"ref": ref, "title": t["title"], "reason": "already pushed",
                                   "key": prev.get("key"), "url": prev.get("url"), "at": prev.get("at")})
            continue
        if stop:
            out["errors"].append({"ref": ref, "title": t["title"], "error": f"Not pushed: {stop}"})
            continue
        try:
            res = c.create_ticket(t, ctx)
        except ConnectorError as e:
            msg = masking(str(e))
            out["errors"].append({"ref": ref, "title": t["title"], "error": msg})
            if e.kind in ("auth", "rate", "config", "refused_address", "dns", "ssl", "timeout", "refused"):
                stop = msg
            continue
        entry = {"fingerprint": fp, "connector": c.id, "target": c.target_label(), "site": host, "source": source,
                 "ref": ref, "title": t["title"], "key": res.get("key"), "url": res.get("url"), "at": now_iso()}
        _remember(entry)
        out["created"].append({"ref": ref, "title": t["title"], "key": res.get("key"), "url": res.get("url"),
                               "attachments": int(res.get("attachments") or 0),
                               "attachment_errors": list(res.get("attachment_errors") or []),
                               "notes": list(res.get("notes") or [])})
    made, skipped, failed = len(out["created"]), len(out["skipped"]), len(out["errors"])
    if made:
        c._set_meta(tested_live=True, tested_at=now_iso())        # a real 2xx answer from the service
        c.record_delivery(True, f"{made} ticket{'s' if made != 1 else ''} created in {c.target_label()}.")
    elif failed:
        c.record_delivery(False, out["errors"][0]["error"])
    out["message"] = (f"{made:,} ticket{'s' if made != 1 else ''} created in {c.service} ({c.target_label()}), "
                      f"{skipped:,} already pushed, {failed:,} not pushed.")
    return out


def merged_labels(t: Dict[str, Any], c: TicketConnector) -> List[str]:
    """The ticket's own labels, then the connector's extra labels (no repeats)."""
    out: List[str] = []
    for label in list(t.get("labels") or []) + list(c.extra_labels()):
        label = str(label or "").strip()
        if label and label not in out:
            out.append(label)
    return out


def throttle(seconds: float) -> None:
    if seconds > 0:
        time.sleep(seconds)
