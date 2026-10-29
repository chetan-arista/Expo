"""
Mobile tab (Screaming Frog "Mobile"): mobile set-up of every internal HTML page that loaded (2xx)
(180 rows in the reference crawl, the same pages as the Links tab).

  - Viewport Not Set: no <meta name="viewport"> or one whose content has no "width=" (from the HTML).
  - Target Size, Content Not Sized Correctly, Illegible Font Size: our approximations of the Lighthouse
    checks Screaming Frog takes from PageSpeed Insights, measured in our own Chrome at a 412 x 823 CSS px
    phone viewport (crawler/render_capture.py, PageData rendered["mobile"]): tap targets smaller than
    24 x 24 CSS px that crowd another target, a page wider than the screen, and more than 40 % of the
    visible text under 12 px. They need JavaScript rendering (Configuration > Rendering); without it they
    are "Not checked". When PageSpeed Insights is connected and tested a page as a phone, its Lighthouse
    verdict is used for that page instead (Lighthouse 12+ no longer runs the content-width and font-size
    audits, so those keep the Chrome measurement).
  - Contains Unsupported Plugins: <object>, <embed> or <applet> loading Flash, Java or Silverlight, which
    no current browser runs (detected from the HTML; PageData plugins).
  - Mobile Alternate Link: a separate mobile URL via <link rel="alternate" media="...">.
"""
import re

from ..registry import Column, Filter, Tab, register, register_requirement, rendered_content, requirement_reason, seo
from .internal import ADDRESS, INDEXABILITY, INDEXABILITY_STATUS
from .page_titles import content_rows

_WIDTH = re.compile(r"\bwidth\s*=", re.I)
SMALL_FONT_SHARE = 40.0          # % of visible text under 12 px above which the font size counts as illegible
MIN_TEXT_CHARS = 100             # pages with less visible text are not judged on font size


def viewport_ok(r) -> bool:
    """The page has a viewport meta tag whose content sets width= (e.g. width=device-width)."""
    return bool(_WIDTH.search(seo(r).get("viewport") or ""))


def mobile_metrics(r) -> dict:
    """rendered["mobile"] of the page ({} when the page was not measured at phone size, or the render ended on
    another page)."""
    rd = rendered_content(r)
    m = rd.get("mobile") if rd else None
    return m if isinstance(m, dict) else {}


# Lighthouse audit names (crawler/integrations/psi.py MOBILE_AUDITS) of the three phone-size filters
LIGHTHOUSE_CHECKS = {"target_size": "target_size", "content_not_sized": "content_width",
                     "illegible_font_size": "font_size"}


def psi_mobile(site, r):
    """The page's PageSpeed Insights result when it was tested as a phone (None otherwise)."""
    if site is None or not getattr(site, "integrations", None):
        return None
    try:
        from ...integrations import psi
    except Exception:
        return None
    res = psi.result_for(site, getattr(r, "url", ""))
    if not psi.tested(res) or str(res.get("device") or "mobile").lower() != "mobile":
        return None
    return res


def lighthouse_verdict(r, site, filter_id: str):
    """Lighthouse (PageSpeed Insights, mobile) verdict for a phone-size filter: True = failed, False = passed,
    None = not tested (or this Lighthouse version no longer runs that audit)."""
    if psi_mobile(site, r) is None:
        return None
    from ...integrations import psi
    return psi.mobile_audit(site, getattr(r, "url", ""), LIGHTHOUSE_CHECKS.get(filter_id, filter_id))


def measured(r, site, filter_id: str) -> bool:
    """The filter could judge this page: Lighthouse gave a verdict or Chrome measured it at phone size."""
    return lighthouse_verdict(r, site, filter_id) is not None or bool(mobile_metrics(r))


def _measure_reason(site, arg: str, rows) -> str:
    """Requirement 'mobile_measure': met when Chrome measured pages at phone size or PageSpeed Insights
    tested some pages as a phone (its Lighthouse verdicts are preferred where they exist)."""
    why = requirement_reason("rendering:mobile", site, rows)
    if why and any(psi_mobile(site, r) is not None for r in rows):
        return ""
    return why


register_requirement("mobile_measure", _measure_reason)


def _target_size(r, site) -> bool:
    verdict = lighthouse_verdict(r, site, "target_size")
    if verdict is not None:
        return verdict
    return int(mobile_metrics(r).get("tap_targets_small") or 0) > 0


def _too_wide(r, site) -> bool:
    verdict = lighthouse_verdict(r, site, "content_not_sized")
    if verdict is not None:
        return verdict
    return bool(mobile_metrics(r).get("content_wider_than_viewport"))


def _small_font(r, site) -> bool:
    verdict = lighthouse_verdict(r, site, "illegible_font_size")
    if verdict is not None:
        return verdict
    m = mobile_metrics(r)
    if int(m.get("text_chars") or 0) < MIN_TEXT_CHARS:
        return False
    try:
        return float(m.get("small_font_pct") or 0) > SMALL_FONT_SHARE
    except (TypeError, ValueError):
        return False


def plugins(r) -> list:
    return list(seo(r).get("plugins") or [])


def _psi_status(r, site) -> str:
    """Success / Error / Not checked of the page's PageSpeed Insights request ("" = not requested)."""
    if site is None or not getattr(site, "integrations", None):
        return ""
    try:
        from ...integrations import psi
        return psi.request_status(psi.result_for(site, getattr(r, "url", "")))
    except Exception:
        return ""


def _measured(fn):
    """A rendered-measurement cell: blank when the page was not measured."""
    return lambda r, site: fn(mobile_metrics(r)) if mobile_metrics(r) else ""


COLUMNS = [
    ADDRESS, INDEXABILITY, INDEXABILITY_STATUS,
    Column("viewport", "Viewport", lambda r, site: seo(r).get("viewport") or ""),
    Column("mobile_alternate", "Mobile Alternate Link", lambda r, site: seo(r).get("mobile_alternate") or "", "url"),
    Column("small_targets", "Small Tap Targets", _measured(lambda m: int(m.get("tap_targets_small") or 0)), "num"),
    Column("content_width", "Content Width (px)", _measured(lambda m: m.get("content_width") or ""), "num"),
    Column("small_font_pct", "Text Under 12px (%)",
           _measured(lambda m: round(float(m.get("small_font_pct") or 0), 1)), "num"),
    Column("plugins", "Plugins", lambda r, site: plugins(r), "list"),
    Column("psi_status", "PSI Request Status", lambda r, site: _psi_status(r, site)),
]

FILTERS = [
    Filter("viewport_not_set", "Viewport Not Set", lambda r, site: not viewport_ok(r), "Issue", "High",
           "Pages without a viewport meta tag, or with one that does not set a width. Phones then show the "
           "page at desktop width and shrink it, so text is tiny and visitors have to zoom. Google indexes "
           "the mobile version of pages, so this hurts both users and rankings.",
           "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> to the <head> of "
           "every page and make sure the layout adapts to small screens.",
           short_fix="Add <meta name=viewport content=\"width=device-width\">"),
    Filter("target_size", "Target Size", _target_size, "Warning", "Medium",
           "On a phone-sized screen, some links or buttons are smaller than 24 x 24 pixels and sit so close to "
           "another one that a finger can easily hit the wrong one. Measured in our own Chrome at phone size "
           "(an approximation of the Lighthouse check).",
           "Make tap targets at least 24 x 24 CSS pixels (48 x 48 is comfortable) or add space around them; "
           "the Small Tap Targets column says how many were found.",
           short_fix="Make links/buttons at least 24x24 px with space between",
           requires=("mobile_measure",)),
    Filter("content_not_sized", "Content Not Sized Correctly", _too_wide, "Warning", "High",
           "On a phone-sized screen the page is wider than the screen, so visitors have to scroll sideways. "
           "Usually a fixed-width element, a wide table or an image without max-width. Measured in our own "
           "Chrome at phone size (an approximation of the Lighthouse check).",
           "Find the element wider than the screen (compare Content Width with 412 px) and give it a fluid "
           "width (max-width: 100%), or let wide tables scroll inside their own box.",
           short_fix="Stop elements overflowing the phone screen (max-width:100%)",
           requires=("mobile_measure",)),
    Filter("illegible_font_size", "Illegible Font Size", _small_font, "Warning", "Medium",
           f"On a phone-sized screen more than {SMALL_FONT_SHARE:.0f}% of the visible text is smaller than 12 "
           "pixels, which is hard to read without zooming. Measured in our own Chrome at phone size (an "
           "approximation of the Lighthouse check).",
           "Use a base font size of at least 16 px on mobile and avoid shrinking body text below 12 px.",
           short_fix="Use at least 12 px text on mobile (16 px for body text)",
           requires=("mobile_measure",)),
    Filter("unsupported_plugins", "Contains Unsupported Plugins", lambda r, site: bool(plugins(r)), "Issue", "High",
           "The page embeds Flash, Java applets or Silverlight (<object>, <embed> or <applet>). No current "
           "browser or phone runs them, so that part of the page shows nothing. Detected from the HTML.",
           "Replace the plugin content with HTML5 (video, canvas, JavaScript) or remove it; the Plugins column "
           "lists what was found.",
           short_fix="Replace Flash/Java/Silverlight with HTML5 content",
           requires=("pagedata:plugins",)),
    Filter("mobile_alternate", "Mobile Alternate Link", lambda r, site: bool(seo(r).get("mobile_alternate")),
           "Warning", "High",
           "Pages that point to a separate mobile version with <link rel=\"alternate\" media=\"...\">. This "
           "setup still works, but running two URLs per page is harder to keep in sync than one responsive "
           "page, and each pair must be annotated correctly.",
           "Consider moving to a responsive design on a single URL. Until then, make sure each mobile page "
           "has a canonical back to its desktop page and the content matches.",
           short_fix="Move to one responsive URL; canonical mobile pages to desktop"),
]

register(Tab(id="mobile", name="Mobile", order=210, columns=COLUMNS, filters=FILTERS, rows=content_rows))
