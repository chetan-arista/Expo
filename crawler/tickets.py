"""
Developer tickets (GET /api/export/tickets?format=csv|jira|markdown).

build_tickets(intel_result, evidence, fmt, site_url="") -> (file name, bytes, mime)

One ticket per root-cause group of the error intelligence (crawler.intel.root_cause.analyse, the
/api/intel/errors shape), in fix-first order (the group's rank): title, summary (root cause), why it
matters, affected URLs (count + sample), where on the page and the element's HTML, fix steps, platform
steps, how to verify, how to prevent it, priority, labels and the screenshot(s) of the pinpointed evidence.
Priority is the higher of the Issues tab's priority for the group's issue filters (a High-priority Issue such
as broken pages is a High ticket, whatever its impact score) and the band of the group's impact (70+ High,
40+ Medium). Groups the intelligence marks as probably intentional are kept (nothing silently disappears)
with priority Low and the label "probably-intentional".

evidence: what the crawl captured, any of
  {(tab, filter): [{"url", "shot", ...}]} or {"tab/filter": [...]}   pinpointed SEO evidence (seo_evidence.py)
  {url: {"parent_shot", "error_shot", ...}}                         broken-link evidence (evidence.py)
  {"seo": <first form>, "links": <second form>}                     both

Formats:
  csv       one row per ticket, every field in its own column (UTF-8 with BOM, Excel-safe)
  jira      Jira CSV import: Summary, Issue Type, Priority, Description (Jira wiki markup), Labels (one
            "Labels" column per label, as the importer expects; labels have no spaces)
  markdown  one document, a section per ticket (for GitHub / GitLab issues, Notion, email)
  zip       the three files above plus a screenshots/ folder with the pictures the tickets name (the server
            keeps them only for the current crawl and a few days), the tickets pointing at screenshots/<name>
            and the Markdown showing them inline. The plain files name the pictures and say they are in the zip.

evidence_dir: the folder holding the screenshot files (the crawl job's evidence folder). When it is not given,
the newest crawl_<site>_*_evidence folder in the temp folder that has the file is used.
"""
import csv
import glob
import io
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

FORMATS = ("csv", "jira", "markdown", "zip")
SAMPLE_URLS = 5
ELEMENT_LIMIT = 1500
MAX_SHOTS = 3
CSV_MIME = "text/csv; charset=utf-8"
PRIORITIES = ("Low", "Medium", "High")
SHOT_NAME = re.compile(r"[\w.-]+\.(?:jpg|jpeg|png)", re.I)
SHOT_DIR = "screenshots"
ZIP_HINT = "the pictures are in the tickets .zip export"
CSV_HEADER = ["Ticket", "Title", "Priority", "Priority reason", "Category", "Effort", "Impact", "Fix order",
              "Verified", "Affected URLs", "Sample URLs", "Issues", "Where", "Element", "Root cause",
              "Why it matters", "Fix steps", "Platform steps", "How to verify", "Prevent", "Screenshot", "Labels"]


# ---------------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------------
def _text(v: Any) -> str:
    return " ".join(str(v or "").split()).strip()


def _list(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        return [_text(x) for x in v if _text(x)]
    return [_text(v)] if _text(v) else []


@lru_cache(maxsize=1024)
def _filter_priority(tab: str, filter_id: str) -> Tuple[str, str]:
    """(priority, "Tab: Filter") the Issues tab shows for one issue filter; ("", "") for a plain filter."""
    try:
        from .seo.registry import get_tab
        t = get_tab(tab)
        for f in (t.filters if t else []):
            if f.id == filter_id and f.issue_type and f.priority in PRIORITIES:
                return f.priority, f"{t.name}: {f.name}"
    except Exception:
        pass
    return "", ""


def priority_and_reason(group: dict) -> Tuple[str, str]:
    """The ticket priority and why: the higher of the Issues tab's priority and the impact band."""
    try:
        impact = int(group.get("impact") or 0)
    except Exception:
        impact = 0
    if group.get("intentional"):
        return "Low", "probably intentional: check it before changing anything"
    by_impact = "High" if impact >= 70 else "Medium" if impact >= 40 else "Low"
    best, name = "", ""
    for k in group.get("issue_keys") or []:
        if not isinstance(k, dict):
            continue
        p, n = _filter_priority(str(k.get("tab") or ""), str(k.get("filter") or ""))
        if p and (not best or PRIORITIES.index(p) > PRIORITIES.index(best)):
            best, name = p, n
    if best and PRIORITIES.index(best) >= PRIORITIES.index(by_impact):
        return best, f"\"{name}\" is {best} priority in the Issues tab"
    return by_impact, f"impact {impact}/100"


def priority_of(group: dict) -> str:
    return priority_and_reason(group)[0]


def _label(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")[:40]


def _split_evidence(evidence: Any) -> Tuple[Dict[Tuple[str, str], List[dict]], Dict[str, dict]]:
    seo: Dict[Tuple[str, str], List[dict]] = {}
    links: Dict[str, dict] = {}
    if not isinstance(evidence, dict):
        return seo, links
    if "seo" in evidence or "links" in evidence:
        a, _ = _split_evidence(evidence.get("seo") or {})
        _, b = _split_evidence(evidence.get("links") or {})
        return a, b
    for k, v in evidence.items():
        if isinstance(k, tuple) and len(k) == 2 and isinstance(v, list):
            seo[(str(k[0]), str(k[1]))] = v
        elif isinstance(k, str) and isinstance(v, list) and "/" in k and not k.startswith(("http://", "https://")):
            tab, filt = k.split("/", 1)
            seo[(tab, filt)] = v
        elif isinstance(k, str) and isinstance(v, dict):
            links[k] = v
    return seo, links


def _shots(group: dict, seo: Dict[Tuple[str, str], List[dict]], links: Dict[str, dict]) -> List[str]:
    """Screenshot file names for a group: pictures of its own sample URLs first."""
    samples = set(group.get("sample_urls") or [])
    found: List[Tuple[int, str]] = []
    for k in group.get("issue_keys") or []:
        for item in seo.get((str(k.get("tab")), str(k.get("filter")))) or []:
            shot = item.get("shot") if isinstance(item, dict) else None
            if shot:
                found.append((0 if item.get("url") in samples or item.get("page") in samples else 1, str(shot)))
    for u in group.get("sample_urls") or []:
        ev = links.get(u) or {}
        for key in ("parent_shot", "error_shot"):
            if ev.get(key):
                found.append((0, str(ev[key])))
    out: List[str] = []
    for _, shot in sorted(found, key=lambda x: x[0]):
        name = os.path.basename(shot)
        if name and name not in out:
            out.append(name)
    return out[:MAX_SHOTS]


def _verify_text(group: dict) -> str:
    own = _text(group.get("how_to_verify"))
    if own:
        return own
    issues = ", ".join(_list(group.get("issues"))[:4]) or "the issues above"
    return (f"After the fix, open the sample URLs and check the problem is gone, then re-check them in the "
            f"dashboard (or re-crawl): {issues} should no longer list these URLs.")


def make_tickets(intel_result: Optional[dict], evidence: Any = None) -> List[dict]:
    """The ticket dicts in fix-first order (shared by every format)."""
    groups = list((intel_result or {}).get("groups") or [])
    platform = (intel_result or {}).get("platform") or {}
    cms = _text(platform.get("cms"))
    seo, links = _split_evidence(evidence)

    def order(g: dict) -> tuple:
        rank = int(g.get("rank") or 0)
        return (bool(g.get("intentional")), rank if rank > 0 else 10 ** 6, -int(g.get("impact") or 0))

    tickets = []
    for n, g in enumerate(sorted(groups, key=order), 1):
        title = _text(g.get("title")) or "SEO issue"
        if g.get("intentional"):
            title = f"Check (probably intentional): {title}"
        labels = ["seo", _label(g.get("category") or ""), _label(f"effort-{g.get('effort') or 'medium'}")]
        if cms:
            labels.append(_label(cms))
        if g.get("intentional"):
            labels.append("probably-intentional")
        verified = _text(g.get("verified")) or "unverified"
        labels.append(_label(f"verified-{verified}"))
        priority, priority_reason = priority_and_reason(g)
        tickets.append({
            "id": f"SEO-{n:02d}", "group_id": g.get("id") or "", "title": title[:200],
            "priority": priority, "priority_reason": priority_reason, "category": _text(g.get("category")), "effort": _text(g.get("effort")),
            "impact": int(g.get("impact") or 0), "rank": int(g.get("rank") or 0), "verified": verified,
            "urls_count": int(g.get("urls_count") or 0), "sample_urls": list(g.get("sample_urls") or [])[:SAMPLE_URLS],
            "issues": _list(g.get("issues")), "where": _text(g.get("where")),
            "element": str(g.get("element") or "").strip()[:ELEMENT_LIMIT],
            "root_cause": _text(g.get("root_cause")), "why": _text(g.get("why")), "prevent": _text(g.get("prevent")),
            "steps": _list(g.get("fix_steps")), "platform_steps": _list(g.get("platform_steps")),
            "verify": _verify_text(g), "screenshots": _shots(g, seo, links), "shot_files": {},
            "labels": [x for i, x in enumerate(labels) if x and x not in labels[:i]],
        })
    return tickets


# ---------------------------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------------------------
def _safe_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _numbered(items: List[str]) -> str:
    return "\n".join(f"{i}. {s}" for i, s in enumerate(items, 1))


def _shot_text(t: dict, zipped: bool) -> str:
    """Where the ticket's pictures are: screenshots/<name> inside the zip; in a plain file the names and
    where to get them (a bare server file name alone cannot be opened)."""
    names = t["screenshots"]
    if not names:
        return ""
    if not zipped:
        return f"{', '.join(names)} ({ZIP_HINT})"
    have = [f"{SHOT_DIR}/{n}" for n in names if n in t["shot_files"]]
    gone = [n for n in names if n not in t["shot_files"]]
    note = f"(no longer on the server, crawl again to capture: {', '.join(gone)})" if gone else ""
    return " ".join(x for x in (", ".join(have), note) if x)


def _csv_file(tickets: List[dict], zipped: bool = False) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for t in tickets:
        w.writerow([_safe_cell(v) for v in [
            t["id"], t["title"], t["priority"], t["priority_reason"], t["category"], t["effort"], t["impact"],
            t["rank"] or "", t["verified"], t["urls_count"], "\n".join(t["sample_urls"]), "\n".join(t["issues"]),
            t["where"], t["element"], t["root_cause"], t["why"], _numbered(t["steps"]),
            "\n".join(t["platform_steps"]), t["verify"], t["prevent"], _shot_text(t, zipped), " ".join(t["labels"])]])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def _jira_escape(text: str) -> str:
    """Characters Jira wiki markup would read as formatting / macros."""
    return re.sub(r"([{}\[\]|])", r"\\\1", str(text or ""))


def _jira_description(t: dict, site_url: str, zipped: bool = False) -> str:
    e = _jira_escape
    parts = [f"*Root cause:* {e(t['root_cause'])}" if t["root_cause"] else "",
             f"*Why it matters:* {e(t['why'])}" if t["why"] else "",
             f"*Site:* {site_url}" if site_url else "",
             f"*Affected:* {t['urls_count']} URL(s) - issues: {e('; '.join(t['issues']))}",
             "*Sample URLs:*\n" + "\n".join(f"* {u}" for u in t["sample_urls"]) if t["sample_urls"] else "",
             f"*Where:* {e(t['where'])}" if t["where"] else "",
             "*Element:*\n{noformat}\n" + t["element"].replace("{noformat}", "") + "\n{noformat}" if t["element"] else "",
             "*Steps to fix:*\n" + "\n".join(f"# {e(s)}" for s in t["steps"]) if t["steps"] else "",
             "*Platform steps:*\n" + "\n".join(f"* {e(s)}" for s in t["platform_steps"]) if t["platform_steps"] else "",
             f"*How to verify:* {e(t['verify'])}",
             f"*Prevent it:* {e(t['prevent'])}" if t["prevent"] else "",
             f"*Screenshot:* {e(_shot_text(t, zipped))}" if t["screenshots"] else "",
             f"_Priority {t['priority']}: {e(t['priority_reason'])}. Effort {t['effort'] or '-'}, impact "
             f"{t['impact']}/100, cause {t['verified']} by the crawl data. Ticket {t['id']} from the ARACHNIX audit._"]
    return "\n\n".join(p for p in parts if p)


def _jira_file(tickets: List[dict], site_url: str, zipped: bool = False) -> bytes:
    width = max([len(t["labels"]) for t in tickets] + [1])
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["Summary", "Issue Type", "Priority", "Description"] + ["Labels"] * width)
    for t in tickets:
        labels = t["labels"] + [""] * (width - len(t["labels"]))
        w.writerow([f"[SEO] {t['title']}"[:250], "Task", t["priority"], _jira_description(t, site_url, zipped)]
                   + labels)
    return buf.getvalue().encode("utf-8")


def _md(text: str) -> str:
    return str(text or "").replace("\r", "")


def _markdown_file(tickets: List[dict], site_url: str, zipped: bool = False) -> bytes:
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# SEO developer tickets{' - ' + site_url if site_url else ''}", "",
             f"{len(tickets)} ticket(s), in the order to fix them. Generated {when} from the ARACHNIX audit.", ""]
    if not zipped and any(t["screenshots"] for t in tickets):
        lines += [f"Screenshots are named by file: {ZIP_HINT}, which shows them in each ticket.", ""]
    if not tickets:
        lines.append("No issue groups were found, so there is nothing to ticket.")
    for t in tickets:
        lines += [f"## {t['id']} - {_md(t['title'])}", "",
                  f"**Priority:** {t['priority']} ({_md(t['priority_reason'])}) | **Effort:** {t['effort'] or '-'} | "
                  f"**Impact:** {t['impact']}/100 | **Cause:** {t['verified']} | **Labels:** {', '.join(t['labels'])}", ""]
        if t["root_cause"]:
            lines += [f"**Root cause:** {_md(t['root_cause'])}", ""]
        if t["why"]:
            lines += [f"**Why it matters:** {_md(t['why'])}", ""]
        lines += [f"**Affected:** {t['urls_count']} URL(s) - {_md('; '.join(t['issues']))}", ""]
        lines += [f"- {u}" for u in t["sample_urls"]] + ([""] if t["sample_urls"] else [])
        if t["where"]:
            lines += [f"**Where:** {_md(t['where'])}", ""]
        if t["element"]:
            fence = "```html" if t["element"].lstrip().startswith("<") else "```"
            lines += ["**Element:**", "", fence, t["element"].replace("```", "'''"), "```", ""]
        if t["steps"]:
            lines += ["**Steps to fix:**", ""] + [f"{i}. {_md(s)}" for i, s in enumerate(t["steps"], 1)] + [""]
        if t["platform_steps"]:
            lines += ["**Platform steps:**", ""] + [f"- {_md(s)}" for s in t["platform_steps"]] + [""]
        lines += [f"**How to verify:** {_md(t['verify'])}", ""]
        if t["prevent"]:
            lines += [f"**Prevent it:** {_md(t['prevent'])}", ""]
        if t["screenshots"]:
            lines += [f"**Screenshot:** {_shot_text(t, zipped)}", ""]
            have = [n for n in t["screenshots"] if zipped and n in t["shot_files"]]
            lines += [f"![{t['id']} picture {i}]({SHOT_DIR}/{n})" for i, n in enumerate(have, 1)] + ([""] if have else [])
    return ("\n".join(lines).rstrip() + "\n").encode("utf-8")


def _host(site_url: str) -> str:
    host = urlparse(site_url).netloc.lower() if site_url else ""
    host = host[4:] if host.startswith("www.") else host
    return re.sub(r"[^a-z0-9.-]+", "_", host).strip("._") or "site"


# ---------------------------------------------------------------------------------------------
# Zip with the screenshots
# ---------------------------------------------------------------------------------------------
def _evidence_dirs(site_url: str, evidence_dir: str = "") -> List[str]:
    """Folders that may hold the screenshot files: the given one, then this site's crawl evidence folders in
    the temp folder (where the server saves them), newest first."""
    dirs = [evidence_dir] if evidence_dir and os.path.isdir(evidence_dir) else []
    host = _host(site_url)
    if host == "site":
        return dirs
    try:
        found = [d for d in glob.glob(os.path.join(tempfile.gettempdir(), "crawl_*_evidence"))
                 if host in os.path.basename(d).lower() and os.path.isdir(d)]
        dirs += [d for d in sorted(found, key=os.path.getmtime, reverse=True) if d not in dirs]
    except Exception:
        pass
    return dirs


def _locate_shots(tickets: List[dict], dirs: List[str]) -> None:
    """Fill each ticket's shot_files {name: path} with the pictures that are still on disk."""
    for t in tickets:
        for name in t["screenshots"]:
            if not SHOT_NAME.fullmatch(name):
                continue
            path = next((os.path.join(d, name) for d in dirs if os.path.isfile(os.path.join(d, name))), "")
            if path:
                t["shot_files"][name] = path


def locate_screenshots(tickets: List[dict], site_url: str = "", evidence_dir: str = "") -> None:
    """Fills each ticket's shot_files {name: path} with its pictures that are still on the server (the zip export
    and the Jira / GitHub push of crawler/connectors/tickets_push.py)."""
    _locate_shots(tickets, _evidence_dirs(site_url, evidence_dir))


def _readme(tickets: List[dict], site_url: str) -> bytes:
    shots = len({n for t in tickets for n in t["shot_files"]})
    gone = len({n for t in tickets for n in t["screenshots"] if n not in t["shot_files"]})
    lines = [f"SEO developer tickets{' for ' + site_url if site_url else ''}", "",
             f"{len(tickets)} ticket(s), in the order to fix them, and {shots} screenshot(s).", "",
             "tickets.md        every ticket, with its pictures shown (keep it next to the screenshots folder)",
             "tickets.csv       one row per ticket, for any tracker or spreadsheet",
             "tickets_jira.csv  the Jira CSV import",
             f"{SHOT_DIR}/       the pinpointed pictures; each ticket names its own in the Screenshot field", "",
             "A Jira CSV import cannot upload local files: after the import, attach to each ticket the picture",
             "it names from the screenshots folder (or share the folder on a drive and link it)."]
    if gone:
        lines += ["", f"{gone} picture(s) named in the tickets are no longer on the server (they are kept for a",
                  "few days only): crawl the site again to capture them."]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _zip_file(tickets: List[dict], site_url: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", _readme(tickets, site_url))
        z.writestr("tickets.md", _markdown_file(tickets, site_url, zipped=True))
        z.writestr("tickets.csv", _csv_file(tickets, zipped=True))
        z.writestr("tickets_jira.csv", _jira_file(tickets, site_url, zipped=True))
        written = set()
        for t in tickets:
            for name, path in t["shot_files"].items():
                if name in written:
                    continue
                try:
                    z.write(path, f"{SHOT_DIR}/{name}", compress_type=zipfile.ZIP_STORED)   # already compressed
                    written.add(name)
                except Exception:
                    continue
    return buf.getvalue()


def build_tickets(intel_result: Optional[dict], evidence: Any = None, fmt: str = "csv",
                  site_url: str = "", evidence_dir: str = "") -> Tuple[str, bytes, str]:
    """(file name, bytes, mime type) for the tickets in `fmt` (csv | jira | markdown | zip; anything else =
    csv). A valid file with no tickets when there is no crawl or nothing to fix. Never raises."""
    fmt = (fmt or "csv").lower().strip()
    fmt = {"md": "markdown"}.get(fmt, fmt)
    fmt = fmt if fmt in FORMATS else "csv"
    try:
        tickets = make_tickets(intel_result, evidence)
    except Exception:
        tickets = []
    host = _host(site_url)
    try:
        if fmt == "jira":
            return f"seo_tickets_{host}_jira.csv", _jira_file(tickets, site_url), CSV_MIME
        if fmt == "markdown":
            return f"seo_tickets_{host}.md", _markdown_file(tickets, site_url), "text/markdown; charset=utf-8"
        if fmt == "zip":
            _locate_shots(tickets, _evidence_dirs(site_url, evidence_dir))
            return f"seo_tickets_{host}.zip", _zip_file(tickets, site_url), "application/zip"
    except Exception:
        pass
    return f"seo_tickets_{host}.csv", _csv_file(tickets), CSV_MIME
