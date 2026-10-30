"""
PageSpeed tab (Screaming Frog "PageSpeed"): Google PageSpeed Insights results for the crawl's internal HTML pages
that loaded (2xx). Optional: it needs the PageSpeed Insights connection (Configuration > API Access, a free
Google API key) and a run for this crawl; until then every filter is "Not connected" / "not run yet", never 0.

Rows: every internal 2xx HTML page; pages PageSpeed did not test show a blank PSI Request Status.
Columns: PSI Request Status, scores, Core Web Vitals from real visitors (Chrome UX Report at URL level; "Origin"
when Google only has data for the whole site), Lighthouse lab metrics, page size and requests, savings.
Filters: Screaming Frog's 20 (19 Lighthouse insights / diagnostics mapped in crawler/integrations/psi.py
AUDIT_FILTERS, plus Request Errors) and "Core Web Vitals Assessment Failed" (URL-level real-visitor data;
replaces the virtual "Core Web Vitals" check). A page counts in a Lighthouse filter only when PageSpeed tested
it and Lighthouse judged that check; untested pages are never counted as passing (see integrations.judge).
"""
from typing import List

from ...integrations import psi
from ..registry import Column, Filter, Tab, register
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import content_rows


def _res(r, site):
    return psi.result_for(site, getattr(r, "url", ""))


def _ok(fn):
    """A cell that is blank unless PageSpeed tested the page."""
    def get(r, site):
        res = _res(r, site)
        return fn(res) if psi.tested(res) else ""
    return get


def _field(metric: str, origin_ok: bool = True):
    def get(res):
        fd = res.get("field") or (res.get("origin_field") if origin_ok else {}) or {}
        return fd.get(metric, "")
    return get


def _field_cat(metric: str):
    def get(res):
        fd = res.get("field") or res.get("origin_field") or {}
        return psi.CRUX_LABEL.get(fd.get(f"{metric}_cat") or "", "")
    return get


def _resources(kind: str, idx: int):
    return lambda res: ((res.get("resources") or {}).get(kind) or ["", ""])[idx]


COLUMNS: List[Column] = [
    ADDRESS, INDEXABILITY, INDEXABILITY_STATUS,
    Column("psi_status", "PSI Request Status", lambda r, site: psi.request_status(_res(r, site))),
    Column("device", "Device", _ok(lambda res: (res.get("device") or "").title())),
    Column("performance_score", "Performance Score", _ok(lambda res: (res.get("scores") or {}).get("performance", "")),
           "num"),
    Column("cwv", "Core Web Vitals Assessment", _ok(lambda res: psi.cwv_assessment(res, allow_origin=True))),
    Column("crux_scope", "CrUX Data", _ok(psi.field_scope)),
    Column("crux_lcp", "CrUX LCP (ms)", _ok(_field("lcp")), "num"),
    Column("crux_lcp_cat", "CrUX LCP Category", _ok(_field_cat("lcp"))),
    Column("crux_inp", "CrUX INP (ms)", _ok(_field("inp")), "num"),
    Column("crux_inp_cat", "CrUX INP Category", _ok(_field_cat("inp"))),
    Column("crux_cls", "CrUX CLS", _ok(_field("cls")), "num"),
    Column("crux_cls_cat", "CrUX CLS Category", _ok(_field_cat("cls"))),
    Column("crux_fcp", "CrUX FCP (ms)", _ok(_field("fcp")), "num"),
    Column("crux_ttfb", "CrUX TTFB (ms)", _ok(_field("ttfb")), "num"),
    Column("lab_fcp", "First Contentful Paint (ms)", _ok(lambda res: (res.get("lab") or {}).get("fcp_ms", "")), "num"),
    Column("lab_lcp", "Largest Contentful Paint (ms)", _ok(lambda res: (res.get("lab") or {}).get("lcp_ms", "")),
           "num"),
    Column("lab_si", "Speed Index (ms)", _ok(lambda res: (res.get("lab") or {}).get("si_ms", "")), "num"),
    Column("lab_tbt", "Total Blocking Time (ms)", _ok(lambda res: (res.get("lab") or {}).get("tbt_ms", "")), "num"),
    Column("lab_cls", "Cumulative Layout Shift", _ok(lambda res: (res.get("lab") or {}).get("cls", "")), "num"),
    Column("lab_ttfb", "Server Response Time (ms)", _ok(lambda res: (res.get("lab") or {}).get("ttfb_ms", "")),
           "num"),
    Column("page_size", "Total Size Transferred (bytes)",
           _ok(lambda res: (res.get("lab") or {}).get("total_bytes", "") or _resources("total", 1)(res)), "num"),
    Column("requests", "Total Requests", _ok(_resources("total", 0)), "num"),
    Column("time_savings", "Time Savings (ms)", _ok(lambda res: psi.total_savings(res)[0]), "num"),
    Column("size_savings", "Size Savings (bytes)", _ok(lambda res: psi.total_savings(res)[1]), "num"),
    Column("opportunities", "Opportunities", _ok(psi.opportunities), "list"),
    Column("accessibility_score", "Lighthouse Accessibility Score",
           _ok(lambda res: (res.get("scores") or {}).get("accessibility", "")), "num"),
]


def _audit_test(f: psi.AuditFilter):
    return lambda r, site: psi.filter_state(_res(r, site), f) is True


def _cwv_failed(r, site) -> bool:
    return psi.cwv_assessment(_res(r, site)) == "Failed"


def _request_error(r, site) -> bool:
    res = _res(r, site)
    return bool(res) and res.get("status") == "error"


FILTERS: List[Filter] = [
    Filter("core_web_vitals", "Core Web Vitals Assessment Failed", _cwv_failed, "Issue", "High",
           "Real visitors (Chrome UX Report, last 28 days, 75th percentile) had a slow or jumpy experience on this "
           "page: Largest Contentful Paint over 2.5 s, Interaction to Next Paint over 200 ms or Cumulative Layout "
           "Shift over 0.1. Only pages with enough real-visitor data of their own are judged.",
           "Open the page's Lighthouse Details: fix the slowest metric first (the main image and server time for "
           "LCP, heavy scripts for INP, missing image sizes and late banners for CLS).",
           short_fix="Fix the failing Core Web Vital (LCP, INP or CLS)"),
]
FILTERS += [Filter(f.id, f.name, _audit_test(f), f.issue_type, f.priority, f.description + " (Lighthouse lab test.)",
                   f.fix, short_fix=f.short_fix) for f in psi.AUDIT_FILTERS]
FILTERS.append(Filter("request_errors", "Request Errors", _request_error, None, None,
                      "PageSpeed Insights could not test these pages (Lighthouse failed to load them, timed out or "
                      "the request was refused); the PSI Request Status column says why.",
                      "Check the page opens for a logged-out visitor, then run PageSpeed again for these URLs."))


def _skip(site, filter_id: str) -> str:
    """Why a filter with no hits was not judged although PageSpeed ran: Lighthouse did not report that check, or
    no tested page has real-visitor data of its own."""
    results = [r for r in (psi.data_of(site).get("results") or {}).values() if psi.tested(r)]
    if not results:
        return ""
    if filter_id == "core_web_vitals":
        if not any(psi.cwv_assessment(r) for r in results):
            return ("Not enough real-visitor (Chrome UX Report) data for any tested page on its own, so Core Web "
                    "Vitals could not be assessed per page (the CrUX Data column shows when only origin data exists).")
        return ""
    f = psi.FILTERS_BY_ID.get(filter_id)
    if f is not None and not any(psi.filter_state(r, f) is not None for r in results):
        return "Lighthouse did not report this check for any tested page (not part of the Lighthouse version used)."
    return ""


register(Tab(id="pagespeed", name="PageSpeed", order=205, columns=COLUMNS, filters=FILTERS, rows=content_rows,
             requires=("integration:psi",), skip_reason=_skip,
             description="Google PageSpeed Insights: Core Web Vitals and Lighthouse checks (optional connection)."))
