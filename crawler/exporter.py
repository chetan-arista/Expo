import os
import time
from typing import List, Optional
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .engine import CrawlResult, ExternalLink
from .issues import CATEGORY_LABELS, ISSUE_CATEGORIES, classify_result, diagnose, issue_sort_key

# Downloadable report scopes: key -> (sheet title, filter, use issue layout)
EXPORT_CATEGORIES = {
    "all": ("Internal URLs", lambda r: True, False),
    "2xx": ("Healthy (2xx)", lambda r: classify_result(r) == "2xx", False),
    "3xx": ("Redirects (3xx)", lambda r: classify_result(r) == "3xx", False),
    "4xx": ("Broken (4xx)", lambda r: classify_result(r) == "4xx", True),
    "5xx": ("Server Errors (5xx)", lambda r: classify_result(r) == "5xx", True),
    "error": ("Network Errors", lambda r: classify_result(r) == "error", True),
    "issues": ("All Issues", lambda r: classify_result(r) in ISSUE_CATEGORIES, True),
    "external": ("External Links", None, False),
}

NAVY_FILL = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
PURPLE_FILL = PatternFill(start_color="4F46E5", end_color="4F46E5", fill_type="solid")
ORANGE_FILL = PatternFill(start_color="D97706", end_color="D97706", fill_type="solid")
RED_HEADER_FILL = PatternFill(start_color="991B1B", end_color="991B1B", fill_type="solid")
LABEL_FILL = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")

WHITE_BOLD_FONT = Font(name="Segoe UI", size=11, bold=True, color="FFFFFF")
REGULAR_FONT = Font(name="Segoe UI", size=10)
BOLD_FONT = Font(name="Segoe UI", size=10, bold=True)
LINK_FONT = Font(name="Segoe UI", size=10, color="1D4ED8", underline="single")

CATEGORY_FILLS = {
    "2xx": PatternFill(start_color="DEF7EC", end_color="DEF7EC", fill_type="solid"),
    "3xx": PatternFill(start_color="FEF08A", end_color="FEF08A", fill_type="solid"),
    "4xx": PatternFill(start_color="FDE8E8", end_color="FDE8E8", fill_type="solid"),
    "5xx": PatternFill(start_color="FFE4E6", end_color="FFE4E6", fill_type="solid"),
    "error": PatternFill(start_color="FFE4E6", end_color="FFE4E6", fill_type="solid"),
}

THIN_BORDER = Border(
    left=Side(style='thin', color='E2E8F0'),
    right=Side(style='thin', color='E2E8F0'),
    top=Side(style='thin', color='E2E8F0'),
    bottom=Side(style='thin', color='E2E8F0')
)

INTERNAL_HEADERS = [
    "Index", "Page Title", "URL", "Found On (Parent Page)", "Link Text (Clicked Element)",
    "Link Location", "HTTP Status", "Status Code", "Category", "Redirect Destination",
    "Depth", "Discovery Source", "Content Type", "Latency (ms)", "Error Notes",
]
ISSUE_HEADERS = [
    "Index", "HTTP Status", "Issue", "Severity", "Broken URL", "Found On (Parent Page)",
    "Link Text (Clicked Element)", "Link Location", "Element HTML", "Why It Happens",
    "Likely Causes", "How To Fix", "Latency (ms)",
]
EXTERNAL_HEADERS = [
    "Index", "External Destination URL", "External Domain", "Parent Page (Found On)",
    "Anchor Text / Label", "Validation Status", "Discovery Source",
]
LINK_COLUMNS = {
    "internal": ("URL", "Found On (Parent Page)", "Redirect Destination"),
    "issue": ("Broken URL", "Found On (Parent Page)"),
    "external": ("External Destination URL", "Parent Page (Found On)"),
}
CENTERED = {"Index", "Status Code", "Depth", "Latency (ms)", "Severity", "Category"}


def _location(r: CrawlResult) -> str:
    parts = [p for p in (r.link_region, r.link_selector) if p]
    return " › ".join(parts) or "-"


def _internal_row(idx: int, r: CrawlResult) -> list:
    return [
        idx, r.page_title, r.url, r.parent_url, r.anchor_text or "-", _location(r),
        r.http_status, r.status_code if r.status_code is not None else "N/A",
        CATEGORY_LABELS.get(classify_result(r), ""), r.redirect_destination or "-",
        r.depth, r.discovery_source, r.content_type or "-", r.response_time_ms, r.error_message or "-",
    ]


def _issue_row(idx: int, r: CrawlResult) -> list:
    d = diagnose(r) or {}
    return [
        idx, r.http_status, d.get("title", ""), (d.get("severity") or "").upper(), r.url, r.parent_url,
        r.anchor_text or "-", _location(r), r.link_html or "-", d.get("summary", ""),
        "\n".join(f"• {c}" for c in d.get("causes", [])), d.get("fix", ""), r.response_time_ms,
    ]


def _external_row(idx: int, e: ExternalLink) -> list:
    return [idx, e.url, e.domain, e.parent_url, e.anchor_text,
            e.validation_status or "Recorded (Not Followed)", e.discovery_source]


class ExcelExporter:
    @staticmethod
    def export(
        results: List[CrawlResult],
        filepath: str,
        stats: dict = None,
        external_links: Optional[List[ExternalLink]] = None,
        category: str = "all"
    ) -> str:
        """
        Exports crawl results into a styled Excel report.
        category="all" builds the full workbook:
          Crawl Summary, Internal URLs, Broken Links & Errors, Redirects (3xx), External Links.
        Any other key of EXPORT_CATEGORIES builds a focused workbook: Crawl Summary + that category.
        URL and parent-page cells are clickable hyperlinks.
        """
        if category not in EXPORT_CATEGORIES:
            raise ValueError(f"Unknown export category: {category}")
        wb = Workbook()
        wb.remove(wb.active)
        stats = stats or {}
        external_links = external_links or []

        ExcelExporter._summary_sheet(wb, results, stats, external_links, category)

        if category == "all":
            ExcelExporter._sheet(wb, "Internal URLs", INTERNAL_HEADERS, NAVY_FILL, "internal",
                                 [(r, _internal_row(i, r)) for i, r in enumerate(results, start=1)])
            issues = sorted([r for r in results if classify_result(r) in ISSUE_CATEGORIES], key=issue_sort_key)
            ExcelExporter._sheet(wb, "Broken Links & Errors", ISSUE_HEADERS, RED_HEADER_FILL, "issue",
                                 [(r, _issue_row(i, r)) for i, r in enumerate(issues, start=1)])
            redirects = [r for r in results if classify_result(r) == "3xx"]
            ExcelExporter._sheet(wb, "Redirects (3xx)", INTERNAL_HEADERS, ORANGE_FILL, "internal",
                                 [(r, _internal_row(i, r)) for i, r in enumerate(redirects, start=1)])
            ExcelExporter._sheet(wb, "External Links", EXTERNAL_HEADERS, PURPLE_FILL, "external",
                                 [(None, _external_row(i, e)) for i, e in enumerate(external_links, start=1)])
        elif category == "external":
            ExcelExporter._sheet(wb, "External Links", EXTERNAL_HEADERS, PURPLE_FILL, "external",
                                 [(None, _external_row(i, e)) for i, e in enumerate(external_links, start=1)])
        else:
            title, keep, issue_layout = EXPORT_CATEGORIES[category]
            subset = [r for r in results if keep(r)]
            if issue_layout:
                subset.sort(key=issue_sort_key)
                ExcelExporter._sheet(wb, title, ISSUE_HEADERS, RED_HEADER_FILL, "issue",
                                     [(r, _issue_row(i, r)) for i, r in enumerate(subset, start=1)])
            else:
                fill = ORANGE_FILL if category == "3xx" else NAVY_FILL
                ExcelExporter._sheet(wb, title, INTERNAL_HEADERS, fill, "internal",
                                     [(r, _internal_row(i, r)) for i, r in enumerate(subset, start=1)])

        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        wb.save(filepath)
        return filepath

    @staticmethod
    def _sheet(wb, title: str, headers: list, header_fill, kind: str, rows: list):
        ws = wb.create_sheet(title=title)
        ws.append(headers)
        for col_num in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=col_num)
            cell.fill = header_fill
            cell.font = WHITE_BOLD_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 28

        link_cols = {headers.index(h) + 1 for h in LINK_COLUMNS[kind] if h in headers}
        status_cols = {headers.index(h) + 1 for h in ("HTTP Status", "Status Code", "Category", "Issue") if h in headers}
        centered = {headers.index(h) + 1 for h in CENTERED if h in headers}
        wrap_cols = {headers.index(h) + 1 for h in ("Likely Causes", "Why It Happens", "How To Fix") if h in headers}

        for row_num, (result, values) in enumerate(rows, start=2):
            ws.append(values)
            fill = CATEGORY_FILLS.get(classify_result(result)) if result is not None else None
            for col_idx in range(1, len(headers) + 1):
                cell = ws.cell(row=row_num, column=col_idx)
                cell.font = REGULAR_FONT
                cell.border = THIN_BORDER
                cell.alignment = Alignment(
                    horizontal="center" if col_idx in centered else "left",
                    vertical="top" if wrap_cols else "center",
                    wrap_text=col_idx in wrap_cols,
                )
                if col_idx in status_cols and fill:
                    cell.fill = fill
                if col_idx in link_cols:
                    value = str(cell.value or "")
                    # Excel rejects hyperlinks over ~2000 chars and caps them at 65,530 per sheet
                    if value.startswith(("http://", "https://")) and len(value) < 2000 and row_num < 65000:
                        cell.hyperlink = value
                        cell.font = LINK_FONT

        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for col in ws.columns:
            col_letter = get_column_letter(col[0].column)
            max_len = max((len(str(c.value or "").split("\n")[0]) for c in col), default=0)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 10), 65)
        return ws

    @staticmethod
    def _summary_sheet(wb, results: List[CrawlResult], stats: dict, external_links: list, category: str):
        ws = wb.create_sheet(title="Crawl Summary")
        ws.column_dimensions['A'].width = 36
        ws.column_dimensions['B'].width = 60

        ws.merge_cells("A1:B1")
        top_title = ws.cell(row=1, column=1)
        scope = "" if category == "all" else f" — {EXPORT_CATEGORIES[category][0]}"
        top_title.value = f"Website Crawler Audit Report{scope}"
        top_title.fill = NAVY_FILL
        top_title.font = Font(name="Segoe UI", size=14, bold=True, color="FFFFFF")
        top_title.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 36

        total_crawled = len(results)
        counts = {k: 0 for k in CATEGORY_LABELS}
        for r in results:
            counts[classify_result(r)] += 1

        def pct(n: int) -> str:
            return f"{n} ({round(n / max(1, total_crawled) * 100, 1)}%)"

        limitations = stats.get("limitations", [])
        summary_rows = [
            ("Target Start URL", stats.get("start_url", results[0].url if results else "N/A")),
            ("Crawl Date & Time", time.strftime("%Y-%m-%d %H:%M:%S")),
            ("Completion Status", stats.get("status", "completed").capitalize()),
            ("Authentication Used", "Yes - Authenticated" if stats.get("auth_used") else "No (Public Crawl)"),
            ("Authentication Details", stats.get("auth_status", "Not required")),
            ("Total Unique Internal URLs Discovered", stats.get("total_discovered", total_crawled)),
            ("Total Internal URLs Crawled", total_crawled),
            ("Healthy Pages (2xx)", pct(counts["2xx"])),
            ("Redirected URLs (3xx)", pct(counts["3xx"])),
            ("Broken Links (4xx)", pct(counts["4xx"])),
            ("Server Errors (5xx)", pct(counts["5xx"])),
            ("Network / Timeout Errors", pct(counts["error"])),
            ("External Links Discovered", f"{len(external_links)} (Recorded only; external sites NOT crawled)"),
            ("Unique External Domains", len(set(e.domain for e in external_links if e.domain))),
            ("Crawl Duration", f"{stats.get('elapsed_seconds', 'N/A')}s"),
            ("Identified Limitations & Notes", "; ".join(limitations) if limitations else "None encountered"),
        ]

        for row_i, (k, v) in enumerate(summary_rows, start=3):
            cell_k = ws.cell(row=row_i, column=1, value=k)
            cell_v = ws.cell(row=row_i, column=2, value=str(v))
            cell_k.font = BOLD_FONT
            cell_v.font = REGULAR_FONT
            cell_k.border = THIN_BORDER
            cell_v.border = THIN_BORDER
            cell_k.fill = LABEL_FILL
            ws.row_dimensions[row_i].height = 22
        return ws
