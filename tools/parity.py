"""
Parity check: compares a crawl by this tool with a Screaming Frog crawl of the same site.

    python -m tools.parity --sf "research/Report csv's" --live https://aristasystems.in/
    python -m tools.parity --sf "research/Report csv's" --xlsx crawl_aristasystems.in_....xlsx

Compares HTML pages only (Screaming Frog also lists images, CSS and JS as URLs), using
internal_all.csv from the Screaming Frog export folder. Prints coverage, status-code,
redirect-target and title agreement, the mismatches, and whether the Phase 1 gate holds.
"""
import argparse
import csv
import html
import os
import re
import sys
import time
from typing import Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from crawler.url_utils import normalize_url  # noqa: E402

GATE = 0.98  # share that must agree for the Phase 1 gate


def _norm_title(t: Optional[str]) -> str:
    return re.sub(r"\s+", " ", html.unescape(t or "")).strip()


def load_sf(folder: str) -> Dict[str, dict]:
    """{normalized url: {status, redirect, title}} for internal HTML URLs in internal_all.csv."""
    path = os.path.join(folder, "internal_all.csv")
    pages = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if "html" not in (row.get("Content Type") or "").lower():
                continue
            url = normalize_url(row["Address"])
            if not url:
                continue
            code = row.get("Status Code") or ""
            pages[url] = {
                "status": int(code) if code.isdigit() else None,
                "redirect": normalize_url(row.get("Redirect URL") or "") or None,
                "title": _norm_title(row.get("Title 1")),
            }
    return pages


def _ours_from_results(results) -> Dict[str, dict]:
    """Our crawl reports the final status after redirects; Screaming Frog reports the first hop."""
    pages = {}
    for r in results:
        if getattr(r, "resource_type", "page") != "page" or getattr(r, "blocked", False):
            continue
        first = r.http_status.split(" -> ")[0] if r.http_status else ""
        status = int(first) if first.isdigit() else r.status_code
        pages[r.url] = {
            "status": status,
            "redirect": normalize_url(r.redirect_destination) if r.redirect_destination else None,
            "title": _norm_title(r.page_title) if r.status_code and r.status_code < 400 else "",
        }
    return pages


def load_ours_xlsx(path: str) -> Dict[str, dict]:
    import openpyxl
    from types import SimpleNamespace
    ws = openpyxl.load_workbook(path, read_only=True)["Internal URLs"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[0]
    results = []
    for row in rows[1:]:
        d = dict(zip(hdr, row))
        ctype = str(d.get("Content Type") or "")
        redirect = d.get("Redirect Destination")
        results.append(SimpleNamespace(
            url=d["URL"], http_status=str(d.get("HTTP Status") or ""),
            status_code=d["Status Code"] if isinstance(d.get("Status Code"), int) else None,
            redirect_destination=None if redirect in (None, "-") else redirect,
            page_title=d.get("Page Title") or "",
            resource_type="page" if ("html" in ctype.lower() or ctype in ("", "-")) else "file",
            blocked=str(d.get("HTTP Status")) == "Blocked by robots.txt",
        ))
    return _ours_from_results(results)


def crawl_live(url: str, out_xlsx: str, concurrency: int) -> Dict[str, dict]:
    from crawler.config import CrawlConfig
    from crawler.engine import WebsiteCrawler
    from crawler.exporter import ExcelExporter
    cfg = CrawlConfig(start_url=url, concurrency=concurrency, use_dynamic_js=False)
    crawler = WebsiteCrawler(cfg, on_log=lambda m: None)
    t = time.time()
    results = crawler.run()
    print(f"Crawled {len(results)} URLs in {time.time() - t:.0f}s; saved {out_xlsx}")
    ExcelExporter.export(results, out_xlsx, stats=crawler.get_stats(), external_links=crawler.external_links_list)
    return _ours_from_results(results)


def _pct(n: int, d: int) -> str:
    return f"{n}/{d} ({n / d:.1%})" if d else "n/a"


def compare(sf: Dict[str, dict], ours: Dict[str, dict], show: int = 10) -> bool:
    both = sorted(set(sf) & set(ours))
    only_sf = sorted(set(sf) - set(ours))
    only_ours = sorted(set(ours) - set(sf))
    status_ok = [u for u in both if sf[u]["status"] == ours[u]["status"]]
    redirects = [u for u in both if sf[u]["redirect"]]
    redirect_ok = [u for u in redirects if sf[u]["redirect"] == ours[u]["redirect"]]
    titled = [u for u in both if sf[u]["status"] == 200 and ours[u]["status"] == 200]
    title_ok = [u for u in titled if sf[u]["title"] == ours[u]["title"]]

    coverage = len(both) / len(sf) if sf else 1
    status_rate = len(status_ok) / len(both) if both else 1
    redirect_rate = len(redirect_ok) / len(redirects) if redirects else 1
    title_rate = len(title_ok) / len(titled) if titled else 1

    print("\nScreaming Frog vs this tool (HTML pages)")
    print(f"  Screaming Frog pages : {len(sf)}")
    print(f"  Our pages            : {len(ours)}")
    print(f"  Coverage of SF pages : {_pct(len(both), len(sf))}")
    print(f"  Status codes agree   : {_pct(len(status_ok), len(both))}")
    print(f"  Redirect targets     : {_pct(len(redirect_ok), len(redirects))}")
    print(f"  Titles agree         : {_pct(len(title_ok), len(titled))}")

    def listing(label, urls, fmt=lambda u: u):
        if urls:
            print(f"\n{label} ({len(urls)}):")
            for u in urls[:show]:
                print("  " + fmt(u))
            if len(urls) > show:
                print(f"  ... {len(urls) - show} more")

    listing("Only in Screaming Frog", only_sf, lambda u: f"{u}  [{sf[u]['status']}]")
    listing("Only in our crawl", only_ours, lambda u: f"{u}  [{ours[u]['status']}]")
    listing("Status differs", [u for u in both if u not in status_ok],
            lambda u: f"{u}  SF {sf[u]['status']} vs ours {ours[u]['status']}")
    listing("Redirect target differs", [u for u in redirects if u not in redirect_ok],
            lambda u: f"{u}  SF -> {sf[u]['redirect']} vs ours -> {ours[u]['redirect']}")
    listing("Title differs", [u for u in titled if u not in title_ok],
            lambda u: f"{u}\n    SF:   {sf[u]['title']!r}\n    ours: {ours[u]['title']!r}")

    passed = min(coverage, status_rate, redirect_rate, title_rate) >= GATE
    print(f"\nPhase 1 gate (all four >= {GATE:.0%}): {'PASS' if passed else 'NOT YET'}")
    return passed


def main():
    ap = argparse.ArgumentParser(description="Compare this tool's crawl with a Screaming Frog export")
    ap.add_argument("--sf", required=True, help="Folder with the Screaming Frog CSV exports (needs internal_all.csv)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--live", help="Crawl this URL now and compare")
    src.add_argument("--xlsx", help="Compare an Excel report this tool already produced")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="parity_crawl.xlsx", help="Where --live saves its Excel report")
    ap.add_argument("--show", type=int, default=10, help="Mismatches to list per section")
    args = ap.parse_args()

    sf = load_sf(args.sf)
    ours = crawl_live(args.live, args.out, args.concurrency) if args.live else load_ours_xlsx(args.xlsx)
    sys.exit(0 if compare(sf, ours, args.show) else 1)


if __name__ == "__main__":
    main()
