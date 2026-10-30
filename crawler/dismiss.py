"""
Dismissed issues (POST/DELETE /api/issues/dismiss, GET /api/issues/dismissed).

A dismissal says "we know, this is fine": a whole issue filter (pattern empty or "*") or only the URLs of
it that match a glob. Patterns starting with "/" match the URL's path (and query), e.g. "/blog/*";
anything else matches the full URL, e.g. "*://shop.example.com/legal/*". Matching ignores case.

  store = DismissStore(reports_dir, host)       persisted as reports_dir/dismissed/<host>.json
  store.add(tab, filter, pattern, note) -> id
  store.remove(id) -> bool
  store.list() -> [{"id", "tab", "filter", "pattern", "note", "created_at"}]
  store.apply(index) -> {"issues", "dismissed", "dismissed_urls", "summary"}
  store.export_entries() -> [entry]             saved with a crawl (crawler/store.py)
  store.merge(entries, replace=False) -> {"added", "kept", "skipped"}   an opened / imported crawl's dismissals

apply() never makes anything silently disappear: dismissed URLs drop out of the issue COUNTS only (the
tabs keep listing them), every dismissal is listed with how many URLs it covered in this crawl (0 = it
matched nothing this time), and an issue whose every URL was dismissed moves from "issues" to
"dismissed". "dismissed_urls" ({"tab:filter": [urls]}) is what crawler.intel.root_cause.analyse(...,
dismissed=...) takes.
"""
import fnmatch
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional
from urllib.parse import urlparse

MAX_ENTRIES = 500
NOTE_LIMIT = 500
SAMPLE_URLS = 20
ENTRY_KEYS = ("id", "tab", "filter", "pattern", "note", "name", "created_at")
_locks: Dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _host_key(url_or_host: str) -> str:
    try:
        from .seo.compare import host_key
        return host_key(url_or_host)
    except Exception:
        return re.sub(r"[^a-z0-9.-]+", "_", str(url_or_host or "").lower())[:200] or "site"


def _file_lock(path: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


def url_matches(url: str, pattern: str) -> bool:
    """A URL matches an empty / "*" pattern always; "/..." patterns match path + query; others the full URL."""
    pattern = (pattern or "").strip()
    if pattern in ("", "*"):
        return True
    url = str(url or "")
    if pattern.startswith("/"):
        parts = urlparse(url)
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    else:
        target = url
    return fnmatch.fnmatchcase(target.lower(), pattern.lower())


class DismissStore:
    """Dismissed issues of one site, persisted as JSON. Thread-safe."""

    def __init__(self, reports_dir: Optional[str] = None, host: str = ""):
        root = reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()
        self.host = _host_key(host) if host else "_all"
        self.path = os.path.join(root, "dismissed", f"{self.host}.json")
        self._lock = _file_lock(self.path)

    # -- persistence ----------------------------------------------------------------------------
    def _load(self) -> List[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            items = data.get("dismissed") if isinstance(data, dict) else data
            return [d for d in items or [] if isinstance(d, dict) and d.get("id")]
        except (OSError, ValueError):
            return []

    def _save(self, items: List[dict]) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"host": self.host, "dismissed": items}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    # -- API ------------------------------------------------------------------------------------
    def add(self, tab: str, filter_id: str, pattern: str = "", note: str = "", name: str = "") -> str:
        """Dismiss an issue filter (or the URLs of it matching `pattern`). The same tab + filter +
        pattern again updates the note and keeps the id. Returns the id ("" for an invalid request)."""
        tab, filter_id = str(tab or "").strip(), str(filter_id or "").strip()
        pattern = str(pattern or "").strip()
        if not tab or not filter_id or len(pattern) > 2000:
            return ""
        pattern = "" if pattern == "*" else pattern
        note = " ".join(str(note or "").split())[:NOTE_LIMIT]
        with self._lock:
            items = self._load()
            for d in items:
                if d.get("tab") == tab and d.get("filter") == filter_id and (d.get("pattern") or "") == pattern:
                    d["note"] = note or d.get("note", "")
                    if name:
                        d["name"] = name
                    self._save(items)
                    return d["id"]
            if len(items) >= MAX_ENTRIES:
                return ""
            entry = {"id": uuid.uuid4().hex[:12], "tab": tab, "filter": filter_id, "pattern": pattern,
                     "note": note, "name": name,
                     "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            items.append(entry)
            self._save(items)
            return entry["id"]

    def remove(self, dismiss_id: str) -> bool:
        with self._lock:
            items = self._load()
            kept = [d for d in items if d.get("id") != dismiss_id]
            if len(kept) == len(items):
                return False
            self._save(kept)
            return True

    def list(self) -> List[dict]:
        with self._lock:
            return [dict(d) for d in self._load()]

    def matching(self, tab: str, filter_id: str) -> List[dict]:
        return [d for d in self.list() if d.get("tab") == tab and d.get("filter") == filter_id]

    def is_dismissed(self, tab: str, filter_id: str, url: str) -> bool:
        return any(url_matches(url, d.get("pattern") or "") for d in self.matching(tab, filter_id))

    def export_entries(self) -> List[dict]:
        """Every dismissal as plain data (saved with a crawl by crawler/store.py, and in exports)."""
        return [{k: d.get(k, "") for k in ENTRY_KEYS} for d in self.list()]

    def merge(self, entries: List[dict], replace: bool = False) -> dict:
        """Adds dismissals brought by an opened or imported crawl. An entry with the same tab + filter + pattern
        as one already stored keeps the stored one (its note too); replace=True drops the stored ones first.
        Invalid entries are skipped. Returns {"added", "kept", "skipped"}."""
        added = kept = skipped = 0
        with self._lock:
            items = [] if replace else self._load()
            have = {(d.get("tab"), d.get("filter"), d.get("pattern") or "") for d in items}
            ids = {d.get("id") for d in items}
            for e in entries or []:
                if not isinstance(e, dict):
                    skipped += 1
                    continue
                tab, flt = str(e.get("tab") or "").strip(), str(e.get("filter") or "").strip()
                pattern = str(e.get("pattern") or "").strip()
                pattern = "" if pattern == "*" else pattern
                if not tab or not flt or len(pattern) > 2000:
                    skipped += 1
                    continue
                if (tab, flt, pattern) in have:
                    kept += 1
                    continue
                if len(items) >= MAX_ENTRIES:
                    skipped += 1
                    continue
                entry_id = str(e.get("id") or "")
                if not re.match(r"^[0-9a-f]{12}$", entry_id) or entry_id in ids:
                    entry_id = uuid.uuid4().hex[:12]
                created = str(e.get("created_at") or "")
                if not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", created):
                    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                items.append({"id": entry_id, "tab": tab, "filter": flt, "pattern": pattern,
                              "note": " ".join(str(e.get("note") or "").split())[:NOTE_LIMIT],
                              "name": " ".join(str(e.get("name") or "").split())[:200], "created_at": created})
                have.add((tab, flt, pattern))
                ids.add(entry_id)
                added += 1
            if added or replace:
                self._save(items)
        return {"added": added, "kept": kept, "skipped": skipped}

    def apply(self, index, issues: Optional[List[dict]] = None) -> dict:
        """Issues overview without the dismissed URLs. `issues` defaults to index.issues. Returns
        {"issues": [issue rows with "urls"/"pct" recounted and "dismissed_urls": n],
         "dismissed": [every dismissal with "urls" (covered this crawl), "sample_urls", "issue_name",
                       "fully_dismissed": bool],
         "dismissed_urls": {"tab:filter": [urls]}, "summary": {"dismissals", "issues_hidden", "urls_hidden"}}"""
        entries = self.list()
        return apply_dismissals(index, entries, issues)


def _pct_base(tr, filter_id) -> int:
    """The base of a filter's "% of total" (registry.filter_base: the rows its check covers, e.g. the internal
    URLs for Response Codes' Internal filters), so a dismissal keeps the % every other view shows."""
    try:
        from .seo.registry import filter_base
        f = next((f for f in tr.tab.filters if f.id == filter_id), None)
        if f is not None:
            return filter_base(tr, f)
    except Exception:
        pass
    return len(tr.rows)


def apply_dismissals(index, entries: List[dict], issues: Optional[List[dict]] = None) -> dict:
    """DismissStore.apply() for a given list of dismissals (works without a store)."""
    rows = [dict(i) for i in (issues if issues is not None else getattr(index, "issues", None) or [])]
    by_key: Dict[str, List[dict]] = {}
    for d in entries:
        by_key.setdefault(f"{d.get('tab')}:{d.get('filter')}", []).append(d)
    covered: Dict[str, set] = {d["id"]: set() for d in entries}
    issue_names: Dict[str, str] = {}
    hidden: Dict[str, List[str]] = {}
    kept_issues, gone_issues = [], []
    for issue in rows:
        key = f"{issue.get('tab')}:{issue.get('filter')}"
        issue_names[key] = issue.get("name") or key
        ds = by_key.get(key)
        if not ds:
            kept_issues.append(issue)
            continue
        tr = (getattr(index, "tabs", None) or {}).get(issue.get("tab")) if index is not None else None
        hits = list(tr.filter_rows.get(issue.get("filter"), [])) if tr is not None else []
        total_rows = _pct_base(tr, issue.get("filter")) if tr is not None else 0
        kept = 0
        drop: List[str] = []
        for h in hits:
            url = str(getattr(h, "url", "") or "")
            match = [d for d in ds if url_matches(url, d.get("pattern") or "")]
            if match:
                for d in match:
                    covered[d["id"]].add(url)
                if url not in drop:
                    drop.append(url)
            else:
                kept += 1
        if not hits:   # an issue row without its hit list (no index): a whole-filter dismissal still applies
            whole = any(not (d.get("pattern") or "") for d in ds)
            kept = 0 if whole else int(issue.get("urls") or 0)
        dropped = int(issue.get("urls") or 0) - kept if not hits else len(hits) - kept
        if drop:
            hidden[key] = drop
        issue["dismissed_urls"] = dropped
        issue["urls"] = kept
        if total_rows:
            issue["pct"] = round(kept / max(1, total_rows) * 100, 2)
        (kept_issues if kept > 0 else gone_issues).append(issue)

    dismissed = []
    for d in entries:
        key = f"{d.get('tab')}:{d.get('filter')}"
        urls = sorted(covered.get(d["id"], set()))
        dismissed.append(dict(d, urls=len(urls), sample_urls=urls[:SAMPLE_URLS],
                              issue_name=d.get("name") or issue_names.get(key, key),
                              fully_dismissed=any(f"{g.get('tab')}:{g.get('filter')}" == key for g in gone_issues),
                              match_note="" if urls else "Matched no URL in this crawl."))
    return {
        "issues": kept_issues, "dismissed": dismissed, "dismissed_urls": hidden,
        "fully_dismissed_issues": gone_issues,
        "summary": {"dismissals": len(entries), "issues_hidden": len(gone_issues),
                    "urls_hidden": sum(len(v) for v in hidden.values())},
    }


def store_for(reports_dir: Optional[str], start_url: str) -> DismissStore:
    """The DismissStore of the site a crawl started from."""
    return DismissStore(reports_dir, start_url)
