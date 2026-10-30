"""
More reports and bulk exports (P20), registered into crawler/reports.py when it is imported:

  Reports   crawl_path          Crawl Path: how the crawler reached each URL (start page > ... > URL), by the first
                                Parent link of every step; ctx["url"] limits it to one URL
            mobile_tap_targets  Mobile > Small Tap Targets: one row per small target measured at phone size
            psi_opportunities   PageSpeed > Opportunities: one table per Lighthouse opportunity with each resource's
                                total and wasted bytes (stored PageSpeed Insights data)
  Bulk      screenshots         every stored screenshot of the crawl (.zip)
            all_page_text       the readable text of every page whose HTML was stored (.zip of .txt)

Missing data is a one-row "Not checked" table / file with the reason, never an empty export.
"""
import os
from typing import Any, Dict, List, Optional, Tuple

from .exporter import PARENT_LINK, Table, not_checked_table

MAX_PATH_STEPS = 50
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def _rows(site) -> List[Any]:
    return list((getattr(site, "by_url", None) or {}).values())


# ---------------------------------------------------------------------------------------------
# Crawl Path
# ---------------------------------------------------------------------------------------------
def _path_of(site, url: str) -> Tuple[List[str], bool]:
    """[start page, ..., url] by first-found parents, and whether the chain reached a URL with no parent."""
    by = getattr(site, "by_url", None) or {}
    chain, seen = [url], {url}
    cur = by.get(url)
    while cur is not None and len(chain) <= MAX_PATH_STEPS:
        parent = str(getattr(cur, "parent_url", "") or "")
        if not parent.lower().startswith(("http://", "https://")):
            return list(reversed(chain)), True      # the start page ("Direct Input"), a sitemap, a list upload
        if parent in seen:
            return list(reversed(chain)), False
        chain.append(parent)
        seen.add(parent)
        cur = by.get(parent)
        if cur is None:                  # the parent was not crawled itself (an extra start URL, a sitemap)
            return list(reversed(chain)), True
    return list(reversed(chain)), False


def crawl_path(site, index, crawler, ctx: dict) -> List[Table]:
    title = "Crawl Path"
    rows = _rows(site)
    if not rows:
        return [not_checked_table(title, "No URL was crawled.")]
    only = str(ctx.get("url") or "").strip()
    if only:
        rows = [r for r in rows if r.url == only]
        if not rows:
            return [not_checked_table(title, f"This URL is not in the crawl: {only[:200]}")]
    out = []
    for r in rows:
        chain, rooted = _path_of(site, r.url)
        source = str(getattr(r, "discovery_source", "") or "")
        out.append([r.url, len(chain) - 1, " > ".join(chain), chain[-2] if len(chain) > 1 else "",
                    chain[0], "" if rooted else "Loop or limit: the path was cut", source])
    return [Table(title, ["Address", "Steps", "Crawl Path", PARENT_LINK, "Reached From", "Note", "Found Via"], out,
                  url_cols=(1, 4, 5))]


# ---------------------------------------------------------------------------------------------
# Mobile tap targets
# ---------------------------------------------------------------------------------------------
def mobile_tap_targets(site, index, crawler, ctx: dict) -> List[Table]:
    title = "Small Tap Targets"
    from .seo.tabs.mobile import mobile_metrics
    tr = (getattr(index, "tabs", None) or {}).get("mobile")
    measured = [r for r in _rows(site) if mobile_metrics(r)]
    if not measured:
        why = ""
        if tr is not None:
            why = tr.not_checked.get("small_tap_targets") or tr.not_checked.get("all") or ""
        return [not_checked_table(title, why or "No page was measured at phone size (render the pages in Chrome: "
                                               "Configuration > Rendering).")]
    out = []
    for r in measured:
        m = mobile_metrics(r)
        examples = m.get("tap_target_examples") or []
        small = int(m.get("tap_targets_small") or 0)
        for ex in examples:
            if isinstance(ex, dict):
                out.append([r.url, small, ex.get("tag") or "", ex.get("text") or "", ex.get("w"), ex.get("h")])
        if small and not examples:
            out.append([r.url, small, "", "(examples not recorded)", None, None])
    return [Table(title, ["Address", "Small Targets On Page", "Element", "Text / Label", "Width (px)", "Height (px)"],
                  out, url_cols=(1,))]


# ---------------------------------------------------------------------------------------------
# PageSpeed opportunities
# ---------------------------------------------------------------------------------------------
def psi_opportunities(site, index, crawler, ctx: dict) -> List[Table]:
    title = "PageSpeed Opportunities"
    try:
        from .integrations import psi
    except Exception:
        return [not_checked_table(title, "PageSpeed Insights is not available on this server.")]
    data = psi.data_of(site) if hasattr(psi, "data_of") else {}
    results = (data or {}).get("results") or {}
    if not results:
        reason = (data or {}).get("reason") or "Not connected - connect PageSpeed Insights under Connections."
        return [not_checked_table(title, str(reason))]
    tables: Dict[str, List[list]] = {}
    names: Dict[str, str] = {}
    for url, res in results.items():
        if not isinstance(res, dict) or res.get("status") != "success":
            continue
        for aid, a in (res.get("audits") or {}).items():
            items = a.get("items") if isinstance(a, dict) else None
            if not items:
                continue
            names[aid] = str(a.get("t") or aid)
            for it in items:
                tables.setdefault(aid, []).append([url, it.get("url") or "", it.get("total"), it.get("wasted"),
                                                   it.get("wasted_pct"), it.get("wasted_ms"),
                                                   a.get("ms"), a.get("b")])
    if not tables:
        return [not_checked_table(title, "No tested page has an opportunity with resource details (all passed, or "
                                         "PageSpeed Insights gave no resource list).")]
    head = ["Address", "Resource", "Total Bytes", "Wasted Bytes", "Wasted %", "Wasted ms", "Audit Savings (ms)",
            "Audit Savings (bytes)"]
    return [Table(f"{names[aid]}"[:80], head, rows, url_cols=(1, 2)) for aid, rows in sorted(tables.items())]


# ---------------------------------------------------------------------------------------------
# Bulk exports (files)
# ---------------------------------------------------------------------------------------------
def _screenshot_files(site, crawler):
    folder = str(getattr(crawler, "evidence_dir", "") or "")
    if not folder or not os.path.isdir(folder):
        return [], "No screenshot was taken in this crawl (Chrome was not used, or screenshots were off)."
    out = []
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if f.lower().endswith(IMAGE_EXT):
                path = os.path.join(root, f)
                try:
                    with open(path, "rb") as fh:
                        out.append((os.path.relpath(path, folder).replace(os.sep, "/"), fh.read()))
                except OSError:
                    continue
            if len(out) >= 2000:
                break
    return out, "" if out else "No screenshot was taken in this crawl."


def _page_text_files(site, crawler):
    from .seo.page_text import page_text
    from .seo.tabs.page_titles import content_rows
    out, used = [], set()
    for r in content_rows(site):
        text, source = page_text(crawler, r.url, content_area=False, max_chars=500_000)
        if not source or not text:
            continue
        from urllib.parse import urlsplit
        p = urlsplit(r.url)
        base = (p.netloc + p.path).strip("/").replace("/", "_")[:120] or "page"
        name, n = f"{base}.txt", 2
        while name in used:
            name, n = f"{base} ({n}).txt", n + 1
        used.add(name)
        out.append((name, (f"Source: {r.url}\n\n" + text).encode("utf-8")))
    return out, "" if out else ("The HTML of the pages was not stored in this crawl (Settings > Storage: keep page "
                                "source), so their text cannot be exported.")


REPORTS = (
    ("crawl_path", "Crawl Path", ("Crawl Path",), crawl_path,
     "How the crawler reached each URL: start page > ... > URL by the first Parent link of every step."),
    ("mobile_tap_targets", "Small Tap Targets", ("Mobile", "Small Tap Targets"), mobile_tap_targets,
     "Every tap target smaller than 24 x 24 CSS pixels (or too close to another) measured at phone size."),
    ("psi_opportunities", "PageSpeed Opportunities", ("PageSpeed", "Opportunities"), psi_opportunities,
     "One table per Lighthouse opportunity: each resource's total and wasted bytes (PageSpeed Insights data)."),
)


def install(reports_mod) -> None:
    """Adds the reports and bulk exports above to crawler/reports.py (called at its end)."""
    have = {r["id"] for r in reports_mod.REPORTS}
    for rid, name, path, fn, about in REPORTS:
        if rid not in have:
            reports_mod.REPORTS.append(reports_mod._r(rid, name, *path, description=about))
        reports_mod._BUILDERS[rid] = fn
    reports_mod.register_bulk_export("screenshots", "Screenshots", files=_screenshot_files,
                                     description="Every screenshot the crawl stored (.zip).")
    reports_mod.register_bulk_export("all_page_text", "All Page Text", files=_page_text_files,
                                     description="The readable text of every page whose HTML was stored (.zip).")
