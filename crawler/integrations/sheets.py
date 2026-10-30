"""
Google Sheets (PLAN4_build.md N12): export a tab view, the issues overview, a report or a bulk export to a new
Google Sheet, and read a list of URLs from a Sheet (Mode > List, scheduled crawl seeds).

It uses the Google sign-in of Configuration > API Access with two more permissions: drive.file (create Sheets and
change only the files ARACHNIX created) and spreadsheets.readonly (read a Sheet you point it at). An account that
signed in before Sheets existed has neither: "Reconnect Google to allow Sheets" (Connect with Google, source
"sheets"; Google keeps the permissions granted before). A service account has them all, but a Sheet it creates
belongs to it: it is shared with the addresses of "Share new Sheets with" (required for a service account).

Provider documents (request and response shapes, read 2026-10-28):
  https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/create
      POST /v4/spreadsheets {"properties": {"title"}, "sheets": [{"properties": {"sheetId", "title",
      "gridProperties": {"rowCount", "columnCount", "frozenRowCount"}}}]} -> {"spreadsheetId", "spreadsheetUrl", ...}
  https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/batchUpdate
      POST /v4/spreadsheets/{id}/values:batchUpdate {"valueInputOption": "RAW", "data": [{"range", "majorDimension",
      "values"}]} (RAW: text starting with "=" stays text, never a formula)
  https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate (repeatCell: bold
      header row)
  https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get and
      spreadsheets.values/get (GET /v4/spreadsheets/{id}/values/{range}?majorDimension=COLUMNS)
  https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/create
      POST /drive/v3/files/{id}/permissions?sendNotificationEmail=false {"type": "user", "role": "writer",
      "emailAddress"}
  https://developers.google.com/workspace/drive/api/reference/rest/v3/about/get (GET /drive/v3/about?fields=user)
  https://developers.google.com/workspace/sheets/api/limits (60 write requests a minute per user; 10 million cells
      per spreadsheet; 50,000 characters per cell)

  connection(account_hint="") -> {"connected", "reason", "needs_reconnect", "account": {email, kind} | None}
  check(account_hint="") -> {"ok", "detail"}                         Connections > Google Sheets > Test
  tables_for(kind, site, index, crawler=None, tab="", filters=None, report_id="", bulk_id="", ctx=None) -> [Table]
  export(kind, site, index, crawler=None, ..., tables=None, title="", ctx=None, share_with=None)
      -> {"url", "id", "title", "sheets": [{"title", "rows", "columns", "not_checked"}], "cells", "truncated_cells",
          "account", "shared_with", "share_errors"}                  POST /api/sheets/export
  export_tables(tables, title, share_with=None, account_hint="") -> the same
  read_list(sheet_url, cap=100_000, account_hint="") -> {"lines", "sheet", "spreadsheet", "column", "header",
      "rows", "truncated"}                                            /api/list/parse {"sheet_url"}; W8 Sheet seeds
  sheet_ref(url) -> (spreadsheet id, gid or None)

Every call goes through connectors.http.safe_request (no redirects followed to other sites, answers capped, errors
with the kind and host only) with the account's bearer token (refreshed once on 401). Private addresses are allowed
only when the API base comes from its environment variable (tests). Errors are the integrations' own classes:
NotConnected (409), AuthExpired (401), QuotaExceeded (429), IntegrationError (400).
"""
import json
import math
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote, urlsplit

from . import google_oauth, store
from .common import (AuthExpired, IntegrationError, NotConnected, QuotaExceeded, RateLimiter, endpoint,
                     endpoint_overridden, google_error, is_daily_quota)

KINDS = ("tab", "issues", "report", "bulk")
MAX_CELLS = 10_000_000
MAX_CELL_CHARS = 50_000
MAX_SHEETS = 50
WRITE_CHUNK_BYTES = 1_500_000
WRITE_CHUNK_ROWS = 5000
TIMEOUT = (5.0, 60.0)
MAX_ANSWER = 30_000_000
LIST_CAP = 100_000
BLANK_ROOM = 1000                # extra rows read past the cap, for blank rows between the addresses
URL_HEADERS = ("url", "urls", "address", "loc", "page", "link")
RECONNECT = "Reconnect Google to allow Sheets"
NOT_CONNECTED = ("Not connected - connect Google under Configuration > API Access (Google Sheets uses the same "
                 "sign-in), then allow Sheets")
_writes = RateLimiter(50)        # Sheets allows 60 write requests a minute per user


# ---------------------------------------------------------------------------------------------
# Which account
# ---------------------------------------------------------------------------------------------
def _accounts() -> List[Dict[str, Any]]:
    return store.accounts()


def _pick(account_hint: str = "") -> Tuple[Optional[Dict[str, Any]], str, bool]:
    """(account with secrets or None, reason, needs_reconnect)."""
    accounts = _accounts()
    if not accounts:
        return None, NOT_CONNECTED, False
    hint = str(account_hint or "").strip().lower()
    able = [a for a in accounts if google_oauth.has_scope(a, "sheets")]
    if hint:
        chosen = [a for a in accounts if hint in (str(a.get("email") or "").lower(), str(a.get("id") or "").lower())]
        if not chosen:
            return None, (f"The Google account {account_hint} is not connected: connect it under Configuration > "
                          "API Access, or clear the account in Connections > Google Sheets."), False
        acc = chosen[0]
        if not google_oauth.has_scope(acc, "sheets"):
            return acc, _reconnect_reason(acc), True
    elif able:
        acc = able[0]
    else:
        return accounts[0], _reconnect_reason(accounts[0]), True
    if acc.get("needs_reconnect"):
        return acc, ("The Google sign-in has expired or was removed: connect the account again under Configuration > "
                     "API Access."), True
    return acc, "", False


def _reconnect_reason(acc: Dict[str, Any]) -> str:
    missing = google_oauth.missing_sheets_scopes(acc)
    what = []
    if google_oauth.SCOPE_DRIVE_FILE in missing:
        what.append("create Sheets")
    if google_oauth.SCOPE_SHEETS_READ in missing:
        what.append("read Sheets")
    detail = f" (the sign-in did not allow ARACHNIX to {' or '.join(what)})" if what else ""
    return f"{RECONNECT}{detail}: Configuration > API Access > Connect with Google, and tick the Sheets permissions."


TURNED_OFF = "Not connected - Google Sheets is turned off under Connections"


def _turned_off() -> bool:
    """The admin disconnected Google Sheets in Connections (the Google account itself stays)."""
    try:
        from ..connectors import store as cstore
        return bool(cstore.load("google_sheets").get("meta", {}).get("disconnected"))
    except Exception:
        return False


def connection(account_hint: str = "") -> Dict[str, Any]:
    """The Sheets state right now (no network call)."""
    acc, reason, reconnect = _pick(account_hint)
    if _turned_off():
        reason, reconnect = TURNED_OFF, False
    pub = store.public_account(acc) if acc else None
    return {"connected": acc is not None and not reason, "reason": reason, "needs_reconnect": reconnect,
            "account": {"email": pub.get("email") or pub.get("name"), "kind": pub.get("kind")} if pub else None}


def _account(account_hint: str = "") -> Dict[str, Any]:
    if _turned_off():
        raise NotConnected(TURNED_OFF)
    acc, reason, _reconnect = _pick(account_hint)
    if acc is None or reason:
        raise NotConnected(reason or NOT_CONNECTED)
    return acc


# ---------------------------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------------------------
def _request(acc: Dict[str, Any], method: str, url: str, api: str, what: str, **kw):
    """One authorised call: 401 refreshes the token once; 429 / 5xx wait (Retry-After, at most 3 times).
    Returns the response (any other status); raises IntegrationError subclasses."""
    from ..connectors.base import ConnectorError
    from ..connectors.http import safe_request
    token = google_oauth.access_token(acc["id"])
    resp = None
    refreshed = False
    for attempt in range(5):
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        headers.update(kw.get("headers") or {})
        args = {k: v for k, v in kw.items() if k != "headers"}
        try:
            resp = safe_request(method, url, headers=headers, timeout=TIMEOUT, max_bytes=MAX_ANSWER,
                                allow_private_network=endpoint_overridden(api), **args)
        except ConnectorError as e:
            raise IntegrationError(f"{what}: {e}", 0, e.kind)
        if resp.status_code == 401 and not refreshed:
            token = google_oauth.access_token(acc["id"], force_refresh=True)
            refreshed = True
            continue
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < 3:
            status, message, reason = google_error(resp)
            if resp.status_code == 429 and is_daily_quota(status, message, reason):
                raise QuotaExceeded(f"{what}: Google's daily limit for Sheets is used up ({message}).", status, reason)
            try:
                wait = float(resp.headers.get("Retry-After") or 0)
            except ValueError:
                wait = 0.0
            time.sleep(min(30.0, wait or (2.0 ** attempt)))
            continue
        return resp
    return resp


def _fail(acc: Dict[str, Any], resp, what: str) -> IntegrationError:
    status, message, reason = google_error(resp)
    who = acc.get("email") or "the Google account"
    if status == 401:
        store.update_account(acc["id"], needs_reconnect=True)
        return AuthExpired(f"{what}: Google no longer accepts this sign-in; connect the account again under "
                           "Configuration > API Access.", status, reason)
    if status == 403 and ("insufficient" in message.lower() or "scope" in message.lower()
                          or "ACCESS_TOKEN_SCOPE_INSUFFICIENT" in reason):
        return NotConnected(f"{RECONNECT}: {what.lower()} needs the Sheets permissions.", status, reason)
    if status == 403 and ("has not been used" in message or "is disabled" in message or "SERVICE_DISABLED" in reason):
        return IntegrationError(f"{what}: the Google Sheets API or Google Drive API is not enabled in the Google "
                                "Cloud project of the OAuth client (APIs & Services > Library).", status, reason)
    if status == 403:
        return IntegrationError(f"{what}: {who} has no access ({message or 'permission denied'}).", status, reason)
    if status == 404:
        return IntegrationError(f"{what}: Google found no such Sheet (check the link, and that it is shared with "
                                f"{who}).", status, reason)
    if status == 429:
        return QuotaExceeded(f"{what}: Google asked ARACHNIX to slow down; try again in a minute.", status, reason)
    return IntegrationError(f"{what}: Google answered {status}: {message or 'no detail'}", status, reason)


def _json(resp) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def check(account_hint: str = "") -> Dict[str, Any]:
    """Connections > Google Sheets > Test: one read-only Drive call with the account's token."""
    acc = _account(account_hint)
    url = endpoint("drive") + "/about"
    resp = _request(acc, "GET", url, "drive", "Testing Google Sheets", params={"fields": "user(emailAddress,displayName)"})
    if resp.status_code != 200:
        raise _fail(acc, resp, "Testing Google Sheets")
    user = _json(resp).get("user") or {}
    who = user.get("emailAddress") or user.get("displayName") or acc.get("email") or "the account"
    return {"ok": True, "detail": f"Google answered: signed in as {who}; ARACHNIX can create Sheets in its Drive "
                                  "and read Sheets shared with it."}


# ---------------------------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------------------------
def _table(t: Any) -> Dict[str, Any]:
    """{"title", "headers", "rows", "not_checked"} from an exporter.Table or a dict of the same keys."""
    if isinstance(t, dict):
        return {"title": str(t.get("title") or t.get("name") or "Sheet"), "headers": list(t.get("headers") or []),
                "rows": list(t.get("rows") or []), "not_checked": str(t.get("not_checked") or "")}
    return {"title": str(getattr(t, "title", "") or "Sheet"), "headers": list(getattr(t, "headers", []) or []),
            "rows": list(getattr(t, "rows", []) or []), "not_checked": str(getattr(t, "not_checked", "") or "")}


def tables_for(kind: str, site, index, crawler=None, tab: str = "", filters: Optional[Sequence[str]] = None,
               report_id: str = "", bulk_id: str = "", ctx: Optional[Dict[str, Any]] = None) -> List[Any]:
    """The tables a Sheets export holds, built exactly like the Excel / CSV exports (so the numbers match):
    tab = one sheet per filter of the tab (exporter.tab_table); issues = the issues overview; report = a Reports menu
    item; bulk = a Bulk Export item. ctx = the API's report context (dismissal-adjusted issues, segments...)."""
    from .. import reports
    from ..exporter import tab_table
    ctx = dict(ctx or {})
    if kind not in KINDS:
        raise IntegrationError("Choose what to export: a tab, the issues, a report or a bulk export.")
    if site is None or index is None:
        raise IntegrationError("There is no finished crawl to export yet.")
    if kind == "tab":
        tr = (getattr(index, "tabs", None) or {}).get(tab)
        if tr is None:
            raise IntegrationError(f"Unknown tab: {str(tab)[:40]}.")
        out = []
        for fid in list(dict.fromkeys(filters or ["all"]))[:MAX_SHEETS]:
            t = tab_table(site, index, tab, fid)
            if t is None:
                raise IntegrationError(f"Unknown filter: {str(fid)[:40]}.")
            out.append(t)
        return out
    if kind == "issues":
        return reports.issues_overview(site, index, crawler, ctx)
    if kind == "report":
        try:
            return reports.report_tables(report_id, site, index, crawler, **ctx)
        except KeyError:
            raise IntegrationError(f"Unknown report: {str(report_id)[:60]}.")
    if reports.is_file_export(bulk_id):
        raise IntegrationError("This bulk export is a set of files (a zip), not a table: download it instead.")
    try:
        return reports.bulk_tables(bulk_id, site, index, crawler, **ctx)
    except KeyError:
        raise IntegrationError(f"Unknown export: {str(bulk_id)[:60]}.")


def cell(v: Any) -> Tuple[Any, bool]:
    """(Sheets value, cut) of one cell: numbers stay numbers, Yes / No for booleans, lists joined with " | ",
    text capped at the 50,000 characters a cell holds."""
    if v is None:
        return "", False
    if isinstance(v, bool):
        return ("Yes" if v else "No"), False
    if isinstance(v, int):
        return v, False
    if isinstance(v, float):
        return (v if math.isfinite(v) else ""), False
    if isinstance(v, (list, tuple, set)):
        text = " | ".join("" if x is None else str(x) for x in v)
    elif isinstance(v, dict):
        text = json.dumps(v, ensure_ascii=False, default=str)
    else:
        text = str(v)
    text = text.replace("\x00", "")
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS - 3] + "...", True
    return text, False


def _sheet_titles(tables: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for t in tables:
        base = re.sub(r"[\x00-\x1f\[\]:*?/\\]+", " ", t["title"]).strip()[:90] or "Sheet"
        name, n = base, 2
        while name.lower() in (o.lower() for o in out):
            name, n = f"{base[:85]} ({n})", n + 1
        out.append(name)
    return out


def _a1(title: str, cell_ref: str) -> str:
    return "'" + title.replace("'", "''") + "'!" + cell_ref


def col_letter(n: int) -> str:
    """1 -> A, 27 -> AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


# ---------------------------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------------------------
def export_tables(tables: List[Any], title: str, share_with: Optional[List[str]] = None,
                  account_hint: str = "") -> Dict[str, Any]:
    """A new Google Sheet with one sheet per table (bold, frozen header row), shared with `share_with`."""
    acc = _account(account_hint)
    rows_in = [_table(t) for t in tables or []][:MAX_SHEETS]
    if not rows_in:
        raise IntegrationError("There is nothing to export.")
    share = [s for s in (share_with or []) if s]
    if acc.get("kind") == "service_account" and not share:
        raise IntegrationError("A Sheet made by a service account belongs to that account only: add who to share new "
                               "Sheets with under Connections > Google Sheets.")
    names = _sheet_titles(rows_in)
    prepared, cells, cut = [], 0, 0
    for t, name in zip(rows_in, names):
        width = max([len(t["headers"])] + [len(r) for r in t["rows"] if isinstance(r, (list, tuple))] + [1])
        values = [[cell(h)[0] for h in t["headers"]] + [""] * (width - len(t["headers"]))]
        for r in t["rows"]:
            row = []
            for v in (list(r) if isinstance(r, (list, tuple)) else [r]):
                val, was_cut = cell(v)
                cut += 1 if was_cut else 0
                row.append(val)
            values.append(row)
        cells += len(values) * width
        prepared.append({"title": name, "values": values, "width": width, "rows": len(values) - 1,
                         "not_checked": t["not_checked"]})
    if cells > MAX_CELLS:
        raise IntegrationError(f"This export has {cells:,} cells; a Google Sheet holds at most {MAX_CELLS:,}. Filter "
                               "it, or download it as Excel or CSV instead.")
    title = " ".join(str(title or "ARACHNIX export").split())[:150] or "ARACHNIX export"
    body = {"properties": {"title": title},
            "sheets": [{"properties": {"sheetId": i, "title": p["title"], "index": i,
                                       "gridProperties": {"rowCount": max(2, p["rows"] + 1),
                                                          "columnCount": max(1, p["width"]), "frozenRowCount": 1}}}
                       for i, p in enumerate(prepared)]}
    base = endpoint("sheets")
    _writes.wait()
    resp = _request(acc, "POST", base + "/spreadsheets", "sheets", "Creating the Sheet", json=body,
                    headers={"Content-Type": "application/json"})
    if resp.status_code != 200:
        raise _fail(acc, resp, "Creating the Sheet")
    made = _json(resp)
    sid = str(made.get("spreadsheetId") or "")
    if not sid:
        raise IntegrationError("Creating the Sheet: Google did not answer with the new Sheet's id.")
    url = str(made.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sid}/edit")
    for p in prepared:
        _write_values(acc, base, sid, p)
    fmt = [{"repeatCell": {"range": {"sheetId": i, "startRowIndex": 0, "endRowIndex": 1},
                           "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                           "fields": "userEnteredFormat.textFormat.bold"}} for i in range(len(prepared))]
    _writes.wait()
    resp = _request(acc, "POST", f"{base}/spreadsheets/{quote(sid)}:batchUpdate", "sheets", "Formatting the Sheet",
                    json={"requests": fmt}, headers={"Content-Type": "application/json"})
    notes = [] if resp.status_code == 200 else ["The header rows could not be made bold."]
    shared, share_errors = _share(acc, sid, share)
    store.update_account(acc["id"], last_used_at=_now())
    return {"url": url, "id": sid, "title": title, "account": acc.get("email") or "",
            "sheets": [{"title": p["title"], "rows": p["rows"], "columns": p["width"],
                        "not_checked": p["not_checked"]} for p in prepared],
            "cells": cells, "truncated_cells": cut, "shared_with": shared, "share_errors": share_errors,
            "notes": notes}


def _write_values(acc: Dict[str, Any], base: str, sid: str, p: Dict[str, Any]) -> None:
    """Writes one sheet's rows in chunks (by size and row count), RAW so nothing is read as a formula."""
    values = p["values"]
    start = 0
    while start < len(values):
        size, end = 0, start
        while end < len(values) and end - start < WRITE_CHUNK_ROWS:
            size += len(json.dumps(values[end], ensure_ascii=False, default=str)) + 2
            if size > WRITE_CHUNK_BYTES and end > start:
                break
            end += 1
        data = [{"range": _a1(p["title"], f"A{start + 1}"), "majorDimension": "ROWS", "values": values[start:end]}]
        _writes.wait()
        resp = _request(acc, "POST", f"{base}/spreadsheets/{quote(sid)}/values:batchUpdate", "sheets",
                        "Writing the Sheet", json={"valueInputOption": "RAW", "data": data},
                        headers={"Content-Type": "application/json"})
        if resp.status_code != 200:
            raise _fail(acc, resp, f"Writing the sheet {p['title']}")
        start = end


def _share(acc: Dict[str, Any], sid: str, emails: List[str]) -> Tuple[List[str], List[str]]:
    shared, errors = [], []
    for email in emails[:20]:
        resp = _request(acc, "POST", f"{endpoint('drive')}/files/{quote(sid)}/permissions", "drive",
                        "Sharing the Sheet", params={"sendNotificationEmail": "false", "supportsAllDrives": "true"},
                        json={"type": "user", "role": "writer", "emailAddress": email},
                        headers={"Content-Type": "application/json"})
        if resp.status_code == 200:
            shared.append(email)
        else:
            _status, message, _reason = google_error(resp)
            errors.append(f"{email}: {message or 'Google answered ' + str(resp.status_code)}")
    return shared, errors


def _now() -> str:
    from .common import now_iso
    return now_iso()


def export(kind: str, site, index, crawler=None, tab: str = "", filters: Optional[Sequence[str]] = None,
           report_id: str = "", bulk_id: str = "", tables: Optional[List[Any]] = None, title: str = "",
           ctx: Optional[Dict[str, Any]] = None, share_with: Optional[List[str]] = None,
           account_hint: Optional[str] = None) -> Dict[str, Any]:
    """POST /api/sheets/export: the tables of `kind` (or the given `tables`, e.g. the grid of a multi-filter
    view) in a new Google Sheet. share_with / account_hint default to the Google Sheets connector's settings."""
    settings = _connector_settings()
    if share_with is None:
        share_with = settings.get("share_with") or []
    if account_hint is None:
        account_hint = settings.get("account") or ""
    _account(account_hint)                     # not connected: say so before building anything
    if tables is None:
        tables = tables_for(kind, site, index, crawler, tab, filters, report_id, bulk_id, ctx)
    host = ""
    try:
        host = urlsplit(str(getattr(site, "start_url", "") or getattr(crawler, "start_url", "") or "")).netloc
    except ValueError:
        pass
    first = _table(tables[0])["title"] if tables else ""
    what = {"tab": first if len(tables or []) == 1 else first.split(" - ")[0], "issues": "Issues overview",
            "report": first if len(tables or []) == 1 else report_id, "bulk": first or bulk_id}.get(kind, kind)
    if not title:
        title = f"ARACHNIX - {host + ' - ' if host else ''}{what or kind} - {time.strftime('%Y-%m-%d %H:%M')}"
    return export_tables(tables, title, share_with, account_hint)


def _connector_settings() -> Dict[str, Any]:
    """The Google Sheets connector's saved settings (share_with, account), {} when it has none."""
    try:
        from ..connectors import registry
        c = registry.get("google_sheets")
        return c.config() if c is not None else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------------------------
# Reading a URL list
# ---------------------------------------------------------------------------------------------
def sheet_ref(url: str) -> Tuple[str, Optional[int]]:
    """(spreadsheet id, gid or None) of a Google Sheets link or a bare id. IntegrationError otherwise."""
    text = str(url or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{25,100}", text):
        return text, None
    try:
        p = urlsplit(text)
    except ValueError:
        p = None
    if p is None or p.scheme not in ("http", "https") or (p.hostname or "").lower() != "docs.google.com":
        raise IntegrationError("Paste the Google Sheet's address (https://docs.google.com/spreadsheets/d/...).")
    if "/spreadsheets/d/e/" in p.path:
        raise IntegrationError("That is a 'Publish to the web' link: use the Sheet's own address from the browser's "
                               "address bar.")
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,100})", p.path)
    if not m:
        raise IntegrationError("That address is not a Google Sheet (expected https://docs.google.com/spreadsheets/d/...).")
    gid = None
    for part in (p.query, p.fragment):
        g = (parse_qs(part).get("gid") or [""])[0]
        if g.isdigit():
            gid = int(g)
            break
    return m.group(1), gid


def read_list(sheet_url: str, cap: int = LIST_CAP, account_hint: Optional[str] = None) -> Dict[str, Any]:
    """The URL column of a Sheet (the sheet the link points at, else the first): the column whose header is URL /
    Address / Loc / Page / Link, else column A from its first row - like an uploaded .xlsx. Returns the raw
    lines; the API turns them into addresses (repeats and non-addresses reported) as for any list."""
    sid, gid = sheet_ref(sheet_url)
    if account_hint is None:
        account_hint = _connector_settings().get("account") or ""
    acc = _account(account_hint)
    base = endpoint("sheets")
    cap = max(1, int(cap or LIST_CAP))
    resp = _request(acc, "GET", f"{base}/spreadsheets/{quote(sid)}", "sheets", "Opening the Sheet",
                    params={"fields": "properties.title,sheets.properties(sheetId,title,gridProperties)"})
    if resp.status_code != 200:
        raise _fail(acc, resp, "Opening the Sheet")
    meta = _json(resp)
    sheets = [s.get("properties") or {} for s in meta.get("sheets") or [] if isinstance(s, dict)]
    if not sheets:
        raise IntegrationError("Opening the Sheet: it has no sheets.")
    sheet = next((s for s in sheets if gid is not None and s.get("sheetId") == gid), sheets[0])
    name = str(sheet.get("title") or "Sheet1")
    total_rows = int((sheet.get("gridProperties") or {}).get("rowCount") or 0)
    head = _request(acc, "GET", f"{base}/spreadsheets/{quote(sid)}/values/{quote(_a1(name, '1:1'), safe='')}",
                    "sheets", "Reading the Sheet", params={"majorDimension": "ROWS"})
    if head.status_code != 200:
        raise _fail(acc, head, "Reading the Sheet")
    first = ((_json(head).get("values") or [[]]) or [[]])[0]
    labels = [str(c or "").strip().lower() for c in first]
    col = next((i for i, h in enumerate(labels) if h in URL_HEADERS), None)
    start = 2 if col is not None else 1
    col = (col or 0) + 1
    letter = col_letter(col)
    end = start + cap + BLANK_ROOM          # room for blank rows between the addresses
    rng = _a1(name, f"{letter}{start}:{letter}{end}")
    resp = _request(acc, "GET", f"{base}/spreadsheets/{quote(sid)}/values/{quote(rng, safe='')}", "sheets",
                    "Reading the Sheet", params={"majorDimension": "COLUMNS", "valueRenderOption": "FORMATTED_VALUE"})
    if resp.status_code != 200:
        raise _fail(acc, resp, "Reading the Sheet")
    column = ((_json(resp).get("values") or [[]]) or [[]])[0]
    lines = [str(v).strip() for v in column if str(v if v is not None else "").strip()]
    # Google answers up to the last non-empty cell of the range: a full range means the column may go on
    truncated = len(lines) > cap or (total_rows > end and len(column) >= end - start + 1)
    store.update_account(acc["id"], last_used_at=_now())
    return {"lines": lines[:cap], "sheet": name, "spreadsheet": str((meta.get("properties") or {}).get("title") or ""),
            "column": letter, "header": first[col - 1] if start == 2 and len(first) >= col else "",
            "rows": len(lines[:cap]), "truncated": bool(truncated), "account": acc.get("email") or ""}
