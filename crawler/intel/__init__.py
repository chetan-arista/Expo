"""
Crawl intelligence on top of the SEO index:

  platform.detect_platform(site_or_crawler)       CMS / shop system, plugins and apps, with evidence
  root_cause.analyse(site, index, crawler)        GET /api/intel/errors   (rule-based, instant)
  root_cause.enrich_with_ai(result, site)         sharper wording for the top groups (one AI call)
  content.analyse(site)                           GET /api/intel/content  (rule-based, instant)
  content.enrich_with_ai(result, site, crawler)   AI review of flagged / borderline pages (1-2 calls)
  checks.check_statuses(index, crawler)           GET /api/crawl/checks   (passed / failed / not checked)

run_intel() is what the POST /api/intel/run background job calls: platform detection with one home
page fetch, both analyses and both AI enrichments, reporting progress as it goes. Every function works
with AI off (rule-based results, labelled by "ai_note") and returns valid empty shapes without a crawl.
"""
from typing import Any, Callable, Optional

from . import checks, content, platform, root_cause
from .checks import check_statuses
from .platform import detect_platform

__all__ = ["checks", "content", "platform", "root_cause", "check_statuses", "detect_platform", "empty_errors",
           "empty_content", "run_intel"]


def empty_errors() -> dict:
    return root_cause.empty_result()


def empty_content() -> dict:
    return content.empty_result()


def run_intel(site, index, crawler=None, progress: Optional[Callable[[int, str], Any]] = None,
              use_ai: bool = True, fetch: bool = True, validation: Optional[dict] = None) -> dict:
    """{"errors": <analyse result>, "content": <analyse result>} with AI enrichment when available.
    progress(pct, message) is called with 0-100. Never raises."""
    def step(pct: int, msg: str):
        if progress is not None:
            try:
                progress(pct, msg)
            except Exception:
                pass

    if site is None or index is None:
        step(100, "No crawl to analyse.")
        return {"errors": empty_errors(), "content": empty_content()}
    step(5, "Detecting the platform")
    plat = detect_platform(site, fetch=False)
    if fetch:
        fetched = detect_platform(crawler if crawler is not None else site, fetch=True)
        if fetched.get("cms") and (not plat.get("cms") or fetched.get("confidence", 0) >= plat.get("confidence", 0)
                                   or len(fetched.get("plugins") or []) > len(plat.get("plugins") or [])):
            plat = fetched
    step(20, "Grouping issues by root cause")
    errors = root_cause.analyse(site, index, crawler, platform=plat, validation=validation)
    step(40, "Checking what each page delivers")
    cont = content.analyse(site)
    if use_ai:
        step(55, "AI: explaining root causes")
        try:
            errors = root_cause.enrich_with_ai(errors, site, crawler)
        except Exception:
            pass
        step(75, "AI: reviewing flagged pages")
        try:
            cont = content.enrich_with_ai(cont, site, crawler, fetch=fetch)
        except Exception:
            pass
    step(100, "Done")
    return {"errors": errors, "content": cont}
