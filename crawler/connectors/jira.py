"""
Jira Cloud (kind "tickets"): push Fix plan groups and Issues as Jira issues, with the crawl's screenshots uploaded
as attachments (tickets_push.py runs the push; this module talks to Jira).

Provider documents (request and response shapes, read 2026-10-28):
  https://developer.atlassian.com/cloud/jira/platform/basic-auth-for-rest-apis/
      HTTP Basic with the account's email address and an API token (id.atlassian.com > Security > API tokens).
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/#api-rest-api-3-issue-post
      POST /rest/api/3/issue {"fields": {"project": {"key"}, "issuetype": {"name"}, "summary", "description": <Atlassian
      Document Format>, "labels": [...], "priority": {"name"}, "assignee": {"accountId"}}}
      -> 201 {"id", "key", "self"}; 400 {"errorMessages": [...], "errors": {field: message}}; 401; 403.
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issue-attachments/
      POST /rest/api/3/issue/{key}/attachments, multipart field "file", header X-Atlassian-Token: no-check
      -> 200 [{"id", "filename", "size", "mimeType", ...}].
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-myself/ (GET /rest/api/3/myself) and
      api-group-projects (GET /rest/api/3/project/{key}), api-group-user-search (GET /rest/api/3/user/search?query=).
  https://developer.atlassian.com/cloud/jira/platform/apis/document/structure/ (Atlassian Document Format).

Jira Cloud only (REST v3 with ADF descriptions); Jira Data Center / Server (REST v2, personal access tokens) is not
supported. The API token is a secret (secrets.json, shown as "set", masked as *** in every message, together with
the Basic header made from it). Calls go through http.safe_request (https only, no redirects to other sites, answers capped).
A priority the project's create screen does not take is dropped once (the ticket then says so in its notes).
"""
import base64
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlsplit

from .base import ConnectorError, Field, Masker, TestResult
from .http import http_error, safe_request
from .registry import register
from .tickets_push import TicketConnector, screenshot_note, merged_labels, throttle

TIMEOUT = (5.0, 30.0)
MAX_ANSWER = 1_000_000
MAX_SUMMARY = 250
MAX_DESCRIPTION = 30_000
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")
LABEL_RE = re.compile(r"^\S{1,255}$")
RETRY_WAIT_CAP = 10.0
PAUSE = 0.0                      # seconds between two tickets (tests keep 0; Jira Cloud allows bursts)


# ---------------------------------------------------------------------------------------------
# Atlassian Document Format (the description)
# ---------------------------------------------------------------------------------------------
def _text(text: str, bold: bool = False, italic: bool = False, link: str = "") -> Optional[Dict[str, Any]]:
    text = str(text or "")
    if not text:
        return None
    node: Dict[str, Any] = {"type": "text", "text": text}
    marks = []
    if bold:
        marks.append({"type": "strong"})
    if italic:
        marks.append({"type": "em"})
    if link and re.match(r"^https?://", link, re.I):
        marks.append({"type": "link", "attrs": {"href": link}})
    if marks:
        node["marks"] = marks
    return node


def _para(*nodes: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    content = [n for n in nodes if n]
    return {"type": "paragraph", "content": content} if content else None


def _labelled(label: str, text: str) -> Optional[Dict[str, Any]]:
    return _para(_text(f"{label}: ", bold=True), _text(text)) if str(text or "").strip() else None


def _list(items: List[str], ordered: bool = False, links: bool = False) -> Optional[Dict[str, Any]]:
    entries = []
    for item in items:
        item = str(item or "").strip()
        if item:
            entries.append({"type": "listItem", "content": [_para(_text(item, link=item if links else ""))]})
    if not entries:
        return None
    return {"type": "orderedList" if ordered else "bulletList", "content": entries}


def _code(text: str) -> Optional[Dict[str, Any]]:
    text = str(text or "").strip()
    if not text:
        return None
    return {"type": "codeBlock", "attrs": {"language": "html"}, "content": [{"type": "text", "text": text}]}


def adf_description(t: Dict[str, Any], site_url: str = "", shots: str = "", dashboard_url: str = "") -> Dict[str, Any]:
    """The ticket as an ADF document (our own layout; same content as the ticket exports)."""
    blocks: List[Optional[Dict[str, Any]]] = [
        _labelled("Root cause", t.get("root_cause", "")),
        _labelled("Why it matters", t.get("why", "")),
        _labelled("Site", site_url),
        _labelled("Affected", f"{int(t.get('urls_count') or 0):,} URL(s) - issues: " + "; ".join(t.get("issues") or [])),
    ]
    if t.get("sample_urls"):
        blocks += [_para(_text("Sample URLs:", bold=True)), _list(t["sample_urls"], links=True)]
    blocks.append(_labelled("Where", t.get("where", "")))
    if t.get("element"):
        blocks += [_para(_text("Element:", bold=True)), _code(t["element"])]
    if t.get("steps"):
        blocks += [_para(_text("Steps to fix:", bold=True)), _list(t["steps"], ordered=True)]
    if t.get("platform_steps"):
        blocks += [_para(_text("Platform steps:", bold=True)), _list(t["platform_steps"])]
    blocks += [_labelled("How to verify", t.get("verify", "")), _labelled("Prevent it", t.get("prevent", "")),
               _labelled("Screenshots", shots)]
    impact = int(t.get("impact") or 0)
    footer = (f"Priority {t.get('priority')}: {t.get('priority_reason') or '-'}. Effort {t.get('effort') or '-'}, "
              f"impact {f'{impact}/100' if impact else '-'}, cause {t.get('verified') or '-'} by the crawl data. "
              f"Ticket {t.get('id')} from the ARACHNIX audit.")
    blocks.append(_para(_text(footer, italic=True)))
    if dashboard_url:
        blocks.append(_para(_text("Open ARACHNIX: "), _text(dashboard_url, link=dashboard_url)))
    doc = {"type": "doc", "version": 1, "content": [b for b in blocks if b]}
    return doc


def adf_plain(doc: Dict[str, Any]) -> str:
    """The text of an ADF document (the dry-run preview; also a size check)."""
    lines: List[str] = []

    def walk(node: Any, prefix: str = "") -> None:
        kind = node.get("type")
        if kind in ("paragraph", "codeBlock"):
            lines.append(prefix + "".join(c.get("text", "") for c in node.get("content") or []))
        elif kind in ("bulletList", "orderedList"):
            for i, item in enumerate(node.get("content") or [], 1):
                mark = f"{i}. " if kind == "orderedList" else "- "
                for child in item.get("content") or []:
                    walk(child, mark)
        else:
            for child in node.get("content") or []:
                walk(child, prefix)
    walk(doc)
    return "\n".join(lines)


def _trim(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Keeps the description under Jira's field limit (drops the element, then shortens texts)."""
    if len(adf_plain(doc)) <= MAX_DESCRIPTION:
        return doc
    doc["content"] = [b for b in doc["content"] if b.get("type") != "codeBlock"]
    for b in doc["content"]:
        for n in b.get("content") or []:
            if n.get("type") == "text" and len(n.get("text", "")) > 2000:
                n["text"] = n["text"][:2000] + "..."
    return doc


# ---------------------------------------------------------------------------------------------
# The connector
# ---------------------------------------------------------------------------------------------
class Jira(TicketConnector):
    id = "jira"
    name = "Jira"
    service = "Jira"
    icon = "jira"
    uploads_screenshots = True
    sends = ["urls", "titles", "screenshots"]
    description = ("Push Fix plan groups and issues as Jira issues, with fix steps, sample URLs and the screenshots "
                   "attached.")
    docs = ("https://developer.atlassian.com/cloud/jira/platform/rest/v3/api-group-issues/ and "
            "api-group-issue-attachments (read 2026-10-28)")
    fields = [
        Field("site", "Jira site", "url", required=True,
              help="Your Jira Cloud address, e.g. https://yourteam.atlassian.net"),
        Field("email", "Account email", "text", required=True,
              help="The email address of the Atlassian account the API token belongs to."),
        Field("api_token", "API token", "password", required=True, secret=True,
              help="Create one at id.atlassian.com > Security > API tokens."),
        Field("project", "Project key", "text", required=True, help="The key of the project, e.g. SEO."),
        Field("issue_type", "Issue type", "text", default="Task",
              help="The issue type to create, as named in the project (Task, Bug, Story...)."),
        Field("assignee", "Assignee", "text",
              help="Optional: the Atlassian account ID or email address of the person to assign."),
        Field("labels", "Extra labels", "list",
              help="Optional: labels added to every ticket (no spaces), besides ARACHNIX's own."),
        Field("attach_screenshots", "Attach screenshots", "bool", default=True,
              help="Upload the crawl's screenshots of each problem as attachments."),
    ]

    # --- settings --------------------------------------------------------------------------------
    def validate(self, config, secrets):
        clean, changes, errors = super().validate(config, secrets)
        if "site" not in errors and clean.get("site"):
            base, why = site_base(clean["site"])
            if why:
                errors["site"] = why
            else:
                clean["site"] = base
        clean["project"] = str(clean.get("project") or "").strip().upper()
        if "project" not in errors and clean["project"] and not PROJECT_RE.match(clean["project"]):
            errors["project"] = "Use the project key (letters and digits, starting with a letter), e.g. SEO."
        clean["email"] = str(clean.get("email") or "").strip()
        if "email" not in errors and clean["email"] and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean["email"]):
            errors["email"] = "Enter the account's email address."
        clean["issue_type"] = str(clean.get("issue_type") or "Task").strip() or "Task"
        bad = [x for x in clean.get("labels") or [] if not LABEL_RE.match(x)]
        if bad and "labels" not in errors:
            errors["labels"] = f"Labels cannot contain spaces: {bad[0][:40]!r}."
        return clean, changes, errors

    def masker(self) -> Masker:
        token = self.secret("api_token")
        values = [token] if token else []
        email = str(self.config().get("email") or "")
        if token and email:
            values.append(base64.b64encode(f"{email}:{token}".encode()).decode())
        return Masker(values)

    def target(self) -> str:
        cfg = self.config()
        return f"jira:{site_host_of(cfg.get('site'))}/{cfg.get('project') or ''}"

    def target_label(self) -> str:
        cfg = self.config()
        return f"{site_host_of(cfg.get('site'))}, project {cfg.get('project') or '-'}"

    def attach_screenshots(self) -> bool:
        return bool(self.config().get("attach_screenshots", True))

    def extra_labels(self) -> List[str]:
        return [str(x) for x in self.config().get("labels") or []]

    # --- calls ---------------------------------------------------------------------------------------
    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        cfg = self.config()
        raw = f"{cfg.get('email') or ''}:{self.secret('api_token')}".encode("utf-8")
        h = {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii"), "Accept": "application/json"}
        h.update(extra or {})
        return h

    def _call(self, method: str, path: str, **kw):
        base = str(self.config().get("site") or "").rstrip("/")
        headers = self._headers(kw.pop("headers", None))
        for attempt in range(2):
            resp = safe_request(method, base + path, headers=headers, timeout=TIMEOUT, max_bytes=MAX_ANSWER,
                                allow_private_network=self.allow_private(), **kw)
            if resp.status_code != 429 or attempt:
                return resp
            try:
                wait = float(resp.headers.get("Retry-After") or 2)
            except ValueError:
                wait = 2.0
            if wait > RETRY_WAIT_CAP:
                return resp
            throttle(wait)
        return resp

    def _error(self, resp, what: str) -> ConnectorError:
        mask = self.masker()
        if resp.status_code == 401:
            return ConnectorError("Jira refused the sign-in: check the account email and the API token.", "auth",
                                  status=401)
        if resp.status_code == 403:
            return ConnectorError(f"{what}: this account has no permission for it in Jira (403).", "auth", status=403)
        if resp.status_code == 429:
            return ConnectorError("Jira asked ARACHNIX to slow down (429): push the rest in a minute.", "rate",
                                  status=429)
        detail = jira_message(resp)
        msg = http_error("Jira", resp, mask, detail=detail)
        return ConnectorError(f"{what}: {msg}", "http", status=resp.status_code)

    def _test(self) -> TestResult:
        cfg = self.config()
        resp = self._call("GET", "/rest/api/3/myself")
        if resp.status_code != 200:
            return TestResult(False, str(self._error(resp, "Signing in")))
        me = _json(resp)
        who = str(me.get("displayName") or me.get("emailAddress") or "the account")
        proj = self._call("GET", "/rest/api/3/project/" + quote(str(cfg.get("project") or ""), safe=""))
        if proj.status_code == 404:
            return TestResult(False, f"Signed in as {who}, but project {cfg.get('project')} was not found or this "
                                     "account cannot see it.")
        if proj.status_code != 200:
            return TestResult(False, str(self._error(proj, "Reading the project")))
        p = _json(proj)
        return TestResult(True, f"Signed in to Jira as {who}; project {p.get('key') or cfg.get('project')} "
                                f"({p.get('name') or 'no name'}) found.")

    def _assignee(self) -> Optional[Dict[str, str]]:
        value = str(self.config().get("assignee") or "").strip()
        if not value:
            return None
        if "@" not in value:
            return {"accountId": value}
        resp = self._call("GET", "/rest/api/3/user/search", params={"query": value, "maxResults": 2})
        if resp.status_code != 200:
            err = self._error(resp, "Finding the assignee")
            if err.kind == "http":
                err.kind = "config"                     # the same answer for every ticket: stop the push
            raise err
        users = resp.json() if resp.content else []
        users = users if isinstance(users, list) else []
        if not users or not isinstance(users[0], dict) or not users[0].get("accountId"):
            raise ConnectorError(f"No Jira user was found for the assignee {value!r}: fix it under Connections > "
                                 "Jira.", "config")
        return {"accountId": str(users[0]["accountId"])}

    def body(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> str:
        return adf_plain(self._description(t, ctx))

    def _description(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        from .messages import clean_url
        shots = screenshot_note(t, bool(ctx.get("attach")))
        return _trim(adf_description(t, ctx.get("site_url", ""), shots, clean_url(ctx.get("dashboard_url"))))

    def create_ticket(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.config()
        fields: Dict[str, Any] = {
            "project": {"key": cfg.get("project")}, "issuetype": {"name": cfg.get("issue_type") or "Task"},
            "summary": " ".join(f"[SEO] {t.get('title')}".split())[:MAX_SUMMARY],
            "description": self._description(t, ctx), "labels": merged_labels(t, self),
            "priority": {"name": t.get("priority") or "Medium"},
        }
        if "jira_assignee" not in ctx:                 # looked up once per push
            ctx["jira_assignee"] = self._assignee()
        if ctx["jira_assignee"]:
            fields["assignee"] = ctx["jira_assignee"]
        notes: List[str] = []
        resp = self._call("POST", "/rest/api/3/issue", json={"fields": fields},
                          headers={"Content-Type": "application/json"})
        if resp.status_code == 400 and "priority" in (_json(resp).get("errors") or {}):
            fields.pop("priority", None)              # not on the project's create screen: create it without
            notes.append("Priority not set: the project's create screen has no priority field.")
            resp = self._call("POST", "/rest/api/3/issue", json={"fields": fields},
                              headers={"Content-Type": "application/json"})
        if resp.status_code not in (200, 201):
            raise self._error(resp, "Creating the ticket")
        made = _json(resp)
        key = str(made.get("key") or "")
        if not key:
            raise ConnectorError("Jira did not answer with the new ticket's key.", "http")
        url = f"{str(cfg.get('site') or '').rstrip('/')}/browse/{quote(key)}"
        attached, problems = 0, []
        if ctx.get("attach"):
            for name, path in (t.get("shot_files") or {}).items():
                ok, why = self._attach(key, name, path)
                attached += 1 if ok else 0
                if not ok:
                    problems.append(f"{name}: {why}")
        if PAUSE:
            throttle(PAUSE)
        return {"key": key, "url": url, "attachments": attached, "attachment_errors": problems, "notes": notes}

    def _attach(self, key: str, name: str, path: str):
        try:
            with open(path, "rb") as fh:
                data = fh.read(20 * 1024 * 1024 + 1)
        except OSError:
            return False, "the screenshot is no longer on the server"
        if len(data) > 20 * 1024 * 1024:
            return False, "the screenshot is larger than 20 MB"
        mime = "image/png" if name.lower().endswith(".png") else "image/jpeg"
        try:
            resp = self._call("POST", f"/rest/api/3/issue/{quote(key)}/attachments",
                              headers={"X-Atlassian-Token": "no-check"}, files={"file": (name, data, mime)})
        except ConnectorError as e:
            return False, self.masker()(str(e))
        if resp.status_code != 200:
            return False, str(self._error(resp, "Uploading"))
        return True, ""


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def site_base(value: str):
    """(scheme://host[:port], "") of a Jira site address, or ("", why)."""
    try:
        p = urlsplit(str(value or "").strip())
        port = p.port
    except ValueError:
        return "", "That is not a valid web address."
    if p.scheme.lower() not in ("https", "http") or not p.hostname:
        return "", "Give the site address, e.g. https://yourteam.atlassian.net"
    if p.username or p.password:
        return "", "Put the email and API token in their own fields, not inside the address."
    if (p.path or "/").rstrip("/") or p.query or p.fragment:
        return "", "Give the site address only (no path), e.g. https://yourteam.atlassian.net"
    host = p.hostname.lower()
    return f"{p.scheme.lower()}://{host}{(':' + str(port)) if port else ''}", ""


def site_host_of(value: Any) -> str:
    try:
        return (urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""


def _json(resp) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def jira_message(resp) -> str:
    """Jira's own error text: errorMessages and per-field errors, on one line."""
    data = _json(resp)
    parts = [str(m) for m in data.get("errorMessages") or [] if m]
    parts += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return " ".join(" ".join(parts).split())[:300]


def connector() -> Jira:
    from . import registry
    c = registry.get("jira")
    return c if isinstance(c, Jira) else Jira()


register(Jira())
