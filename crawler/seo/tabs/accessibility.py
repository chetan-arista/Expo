"""
Accessibility tab (Screaming Frog "Accessibility"): automated accessibility checks for every internal HTML page
that loaded (2xx). It is not a WCAG / ADA audit or certificate: automated rules find many barriers, but keyboard
use, focus order, meaning and some contrast cases can only be judged by a person.

Where the data comes from:
  - axe-core (crawler/vendor/axe, Deque Systems, MPL-2.0), run in Chrome (PageData["axe"], render_capture.run_axe):
    on every rendered page of a crawl with "Render every page in Chrome", and on the pages the Accessibility audit
    option checks. Every rule ARACHNIX runs (axe_core.run_ids(): 93 of axe-core 4.14's rules, the AAA ones and target
    size included; deprecated and experimental rules and frame-tested left out) is a filter: axe_<rule id>, named
    and described from axe-core's own rule data (rules.json), grouped by WCAG level and impact (Filter.group, e.g.
    "WCAG AA / serious"). Issue type and priority follow the impact (critical: Issue High, serious: Issue Medium,
    moderate: Warning Medium, minor: Warning Low; AAA rules are Opportunities). A rule counts only pages axe-core
    checked ("% of total" = those pages); when no page was checked it is "Not checked" with the reason
    (needs JavaScript rendering, or why axe-core could not run). axe-core checks the page in the crawl's rendering
    device window (Configuration > Rendering: desktop, phone or tablet; PageData["axe"]["device"]); the axe-core
    column says so for a phone or tablet ("Checked (93 rules, phone window)").
  - Basic checks for every page: the HTML (PageData a11y, extract.py, after siteone-crawler's careful label and
    name rules): html lang, form fields without a label, links and buttons without an accessible name, images
    without an alt attribute, duplicate ids, aria-labelledby / -describedby / -controls / -owns and <label for>
    pointing at ids that do not exist, no <main> landmark, skipped heading levels (h2 -> h4), positive tabindex;
    and, on pages rendered in Chrome, Chrome's accessibility tree (rendered["a11y_rendered"]): the same name
    checks as assistive technology gets them, plus custom widgets and frames without a name. When a page was
    rendered, the tree's values replace the HTML ones.

The basic checks keep their filter ids. Each one that axe-core also covers is decided by axe-core on the pages it
checked and by the basic check on the others (AXE_MATCH), so no rule is counted twice: those axe rules have no
axe_ filter of their own. Duplicate IDs and Broken ARIA / Label References stay basic checks on every page
(axe-core's duplicate-id-aria and aria-valid-attr-value rules are narrower and wider, and have their own filters).

Accessibility Score (our own, documented here; not Lighthouse's), 0-100, bands as in Lighthouse (Good 90-100,
Needs Improvement 50-89, Poor 0-49; the three bands add up to the pages that were scored):
  - a page axe-core checked ("Scored From" = axe-core): the share of rule weights the page passes over the rules
    that applied to it (passed or failed; inapplicable and needs-review rules left out), with the weights from
    axe-core's impact levels (critical 10, serious 7, moderate 3, minor 1). A page with any critical or serious
    violation scores at most 89 (never Good); one with 3 or more critical, or 5 or more critical / serious rules
    failed, at most 49 (Poor).
  - any other page ("Basic checks"): the share of the basic checks' weights it passes (critical 10, serious 7,
    moderate 3).
"""
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...vendor import axe_core
from ..registry import (Column, Filter, Tab, register, register_requirement, rendered_content, requirement_reason,
                        seo)
from .internal import (ADDRESS, CONTENT_TYPE, INDEXABILITY, INDEXABILITY_STATUS, STATUS, STATUS_CODE,
                       category_of, is_html_doc, memo)
from .page_titles import content_rows

GOOD_MIN = 90
POOR_BELOW = 50
AXE_WEIGHTS = {"critical": 10, "serious": 7, "moderate": 3, "minor": 1}
POOR_CRITICAL = 3                # this many critical rules failed -> Poor at best
POOR_HEAVY = 5                   # this many critical / serious rules failed -> Poor at best
AXE_SOURCE = "axe-core"
BASIC_SOURCE = "Basic checks"


# ---------------------------------------------------------------------------------------------
# Page data
# ---------------------------------------------------------------------------------------------
def static_a11y(r) -> Optional[dict]:
    v = seo(r).get("a11y")
    return v if isinstance(v, dict) else None


def rendered_a11y(r) -> Optional[dict]:
    """The accessibility tree Chrome read (None when not rendered, or when the render ended on another page)."""
    rd = rendered_content(r)
    v = rd.get("a11y_rendered") if rd else None
    return v if isinstance(v, dict) else None


def axe_data(r) -> Optional[dict]:
    """PageData["axe"] (ran or not; None when axe-core was not asked to check the page)."""
    v = seo(r).get("axe")
    return v if isinstance(v, dict) else None


def axe_result(r) -> Optional[dict]:
    """PageData["axe"] of a page axe-core checked (None otherwise)."""
    v = axe_data(r)
    return v if v is not None and v.get("ran") else None


def axe_ran(r) -> bool:
    return axe_result(r) is not None


def _violations(r) -> Dict[str, dict]:
    ax = axe_result(r)
    return {str(v.get("id")): v for v in (ax or {}).get("violations") or [] if isinstance(v, dict)}


def _violated(r, site) -> frozenset:
    """Rule ids axe-core found on the page (memoised per crawl snapshot: every rule filter asks)."""
    if site is None:
        return frozenset(_violations(r))
    return memo(site, "axe:" + r.url, lambda: frozenset(_violations(r)))


def _num(d: Optional[dict], key: str) -> Optional[int]:
    if not d or d.get(key) is None:
        return None
    try:
        return int(d.get(key) or 0)
    except (TypeError, ValueError):
        return None


def _count(r, key: str) -> Optional[int]:
    """A count from the rendered accessibility tree when the page was rendered, else from the HTML."""
    v = _num(rendered_a11y(r), key)
    return v if v is not None else _num(static_a11y(r), key)


def _lang(r) -> Optional[str]:
    st = static_a11y(r)
    if st is None:
        return None
    rd = rendered_content(r)
    if rd is not None and str(rd.get("html_lang") or "").strip():
        return str(rd.get("html_lang")).strip()
    return str(st.get("html_lang") or "").strip()


def _list(r, key: str) -> List[str]:
    st = static_a11y(r) or {}
    return [str(x) for x in st.get(key) or []]


def _main(r) -> Optional[bool]:
    ra = rendered_a11y(r)
    if ra is not None and ra.get("has_main_landmark") is not None:
        return bool(ra.get("has_main_landmark"))
    st = static_a11y(r)
    return None if st is None else bool(st.get("has_main_landmark"))


def _skips(r) -> List[str]:
    ra = rendered_a11y(r)
    if ra is not None and ra.get("heading_skips") is not None:
        return [str(s).replace("->", " -> ").replace("  ", " ") for s in ra.get("heading_skips") or []]
    return _list(r, "heading_skips")


# ---------------------------------------------------------------------------------------------
# Basic checks (HTML and Chrome's accessibility tree) and the axe-core rules that decide them
# ---------------------------------------------------------------------------------------------
# check id -> (label, weight, failed(r) -> Optional[bool] (None = not measured), rendered only)
CHECKS: List[Tuple[str, str, int, Any, bool]] = [
    ("html_lang", "Missing HTML lang", 7, lambda r: None if _lang(r) is None else not _lang(r), False),
    ("labels", "Form field without label", 10,
     lambda r: None if _count(r, "inputs_without_label") is None else _count(r, "inputs_without_label") > 0, False),
    ("link_names", "Link without accessible name", 7,
     lambda r: None if _count(r, "empty_links") is None else _count(r, "empty_links") > 0, False),
    ("button_names", "Button without accessible name", 10,
     lambda r: None if _count(r, "empty_buttons") is None else _count(r, "empty_buttons") > 0, False),
    ("control_names", "Other control without accessible name", 7,
     lambda r: None if _num(rendered_a11y(r), "controls_without_name") is None
     else _num(rendered_a11y(r), "controls_without_name") > 0, True),
    ("image_alt", "Image without alt", 10,
     lambda r: None if _count(r, "images_without_alt") is None else _count(r, "images_without_alt") > 0, False),
    ("frame_titles", "Frame without title", 7,
     lambda r: None if _num(rendered_a11y(r), "frames_without_title") is None
     else _num(rendered_a11y(r), "frames_without_title") > 0, True),
    ("duplicate_ids", "Duplicate ids", 3, lambda r: None if static_a11y(r) is None else bool(_list(r, "duplicate_ids")),
     False),
    ("aria_refs", "Broken ARIA / label reference", 7,
     lambda r: None if static_a11y(r) is None else bool(_list(r, "aria_broken_refs")), False),
    ("main_landmark", "No main landmark", 3, lambda r: None if _main(r) is None else not _main(r), False),
    ("heading_order", "Skipped heading level", 3, lambda r: None if static_a11y(r) is None and rendered_a11y(r) is None
     else bool(_skips(r)), False),
    ("tabindex", "Positive tabindex", 7,
     lambda r: None if _num(static_a11y(r), "tabindex_positive") is None
     else _num(static_a11y(r), "tabindex_positive") > 0, False),
]
CHECK_LABELS = {cid: label for cid, label, _w, _t, _r in CHECKS}
CHECK_WEIGHTS = {cid: w for cid, _l, w, _t, _r in CHECKS}

# Basic check -> the axe-core rules that decide it on a page axe-core checked
AXE_MATCH: Dict[str, Tuple[str, ...]] = {
    "html_lang": ("html-has-lang",),
    "labels": ("label", "select-name", "aria-input-field-name", "aria-toggle-field-name"),
    "link_names": ("link-name",),
    "button_names": ("button-name", "input-button-name", "input-image-alt"),
    "control_names": ("aria-command-name", "aria-tab-name", "aria-treeitem-name"),
    "image_alt": ("image-alt", "role-img-alt", "svg-img-alt"),
    "frame_titles": ("frame-title",),
    "main_landmark": ("landmark-one-main",),
    "heading_order": ("heading-order",),
    "tabindex": ("tabindex",),
}
# Basic checks that stay basic on every page, with the closest axe-core rule (for its WCAG level in exports)
BASIC_ONLY: Dict[str, str] = {"duplicate_ids": "duplicate-id-aria", "aria_refs": "aria-valid-attr-value"}
RULE_CHECK: Dict[str, str] = {rid: cid for cid, rules in AXE_MATCH.items() for rid in rules}


def _basic(r) -> Optional[Dict[str, Any]]:
    """The basic checks of one page: {"score", "band", "failed": [labels], "checks": {id: True/False}}, or None
    when nothing could be measured."""
    if static_a11y(r) is None and rendered_a11y(r) is None:
        return None
    passed = total = 0
    failed: List[str] = []
    results: Dict[str, bool] = {}
    for cid, label, weight, test, _ in CHECKS:
        try:
            bad = test(r)
        except Exception:
            bad = None
        if bad is None:
            continue
        results[cid] = bool(bad)
        total += weight
        if bad:
            failed.append(label)
        else:
            passed += weight
    if not total:
        return None
    score = int(round(100.0 * passed / total))
    return {"score": score, "band": band_of(score), "failed": failed, "checks": results}


def band_of(score: int) -> str:
    return "Good" if score >= GOOD_MIN else ("Poor" if score < POOR_BELOW else "Needs Improvement")


def rule_name(rule_id: str) -> str:
    meta = axe_core.rule(rule_id)
    return str(meta.get("help") or rule_id) if meta else rule_id


def axe_score(ax: Dict[str, Any]) -> int:
    """Our score of a page axe-core checked (see the module docstring)."""
    known = axe_core.rules()
    violations = [v for v in ax.get("violations") or [] if isinstance(v, dict)]
    failed_ids = {str(v.get("id")) for v in violations}
    fail_w = sum(AXE_WEIGHTS.get(str(v.get("impact")), 3) for v in violations)
    pass_w = sum(AXE_WEIGHTS.get(str((known.get(rid) or {}).get("impact")), 3)
                 for rid in ax.get("passes") or [] if rid not in failed_ids)
    total = fail_w + pass_w
    score = 100 if not total else int(round(100.0 * pass_w / total))
    critical = sum(1 for v in violations if v.get("impact") == "critical")
    heavy = sum(1 for v in violations if v.get("impact") in ("critical", "serious"))
    if critical >= POOR_CRITICAL or heavy >= POOR_HEAVY:
        score = min(score, POOR_BELOW - 1)
    elif heavy:
        score = min(score, GOOD_MIN - 1)
    return score


def evaluate(r) -> Optional[Dict[str, Any]]:
    """{"score", "band", "failed": [names], "checks": {basic check id: True/False}, "source": axe-core | Basic
    checks} or None when the page was not checked. On a page axe-core checked, the score, the problems and the
    matched basic checks (AXE_MATCH) come from axe-core; the BASIC_ONLY checks from the basic checks."""
    basic = _basic(r)
    ax = axe_result(r)
    if ax is None:
        if basic is not None:
            basic["source"] = BASIC_SOURCE
        return basic
    found = _violations(r)
    checks = {cid: any(rid in found for rid in rules) for cid, rules in AXE_MATCH.items()}
    failed = [rule_name(rid) for rid in found]
    for cid in BASIC_ONLY:
        state = (basic or {}).get("checks", {}).get(cid)
        if state is not None:
            checks[cid] = state
            if state:
                failed.append(CHECK_LABELS[cid])
    score = axe_score(ax)
    return {"score": score, "band": band_of(score), "failed": failed, "checks": checks, "source": AXE_SOURCE}


def result(site, r) -> Optional[Dict[str, Any]]:
    if site is None:
        return evaluate(r)
    return memo(site, "a11y:" + r.url, lambda: evaluate(r))


# ---------------------------------------------------------------------------------------------
# "Not checked" reasons
# ---------------------------------------------------------------------------------------------
AXE_RENDER_REASON = ("Needs JavaScript rendering: axe-core checks pages in Chrome, and no page of this crawl was "
                     "rendered (turn on 'Render every page in Chrome' or the Accessibility audit under "
                     "Configuration).")
AXE_OLD_REASON = ("These pages were rendered by an older version of the crawler, before the axe-core checks; run a "
                  "new crawl to check this.")
AXE_PAGE_REASON = ("This page was not rendered in Chrome, so axe-core did not check it (it needs 'Render every page "
                   "in Chrome' or the Accessibility audit).")
AXE_OLD_PAGE_REASON = "Rendered by an older version of the crawler, before the axe-core checks."


def _sentence(text: str) -> str:
    text = str(text or "").strip()
    return text if not text or text[-1] in ".!?)" else text + "."


def axe_reason(rows) -> str:
    """Why no page of `rows` was checked with axe-core ("" when at least one was)."""
    reasons: Counter = Counter()
    rendered_any = False
    for r in rows:
        ax = axe_data(r)
        if ax is not None:
            if ax.get("ran"):
                return ""
            reasons[_sentence(ax.get("reason") or "axe-core could not run.")] += 1
        elif seo(r).get("rendered") is not None:
            rendered_any = True
    if reasons:
        why, n = reasons.most_common(1)[0]
        return f"axe-core could not check any page. {why}" + (
            f" (and {sum(reasons.values()) - n} page(s) for other reasons)" if sum(reasons.values()) > n else "")
    return AXE_OLD_REASON if rendered_any else AXE_RENDER_REASON


def _axe_requirement(site, arg: str, rows) -> str:
    pages = [r for r in rows if seo(r).get("is_html")] or list(getattr(site, "pages", None) or [])
    return axe_reason(pages) if pages else ""


def _named_requirement(site, arg: str, rows) -> str:
    """Basic checks that need Chrome (unnamed widgets, untitled frames): met by a page axe-core checked or by the
    rendered page's `arg` (the accessibility tree); otherwise the rendering requirement's own reason (the kind's
    name starts with "rendering" so crawler/intel/checks.py judges these rows as rendered checks)."""
    if any(axe_ran(r) for r in rows):
        return ""
    return requirement_reason("rendering:" + arg, site, rows)


register_requirement("axe", _axe_requirement)
register_requirement("rendering_or_axe", _named_requirement)


def page_reason(r) -> str:
    """Why axe-core did not check this page ("" when it did)."""
    ax = axe_data(r)
    if ax is not None:
        return "" if ax.get("ran") else _sentence(ax.get("reason") or "axe-core could not run.")
    return AXE_OLD_PAGE_REASON if seo(r).get("rendered") is not None else AXE_PAGE_REASON


# ---------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------
_NOTE = " Automated check, not a full accessibility audit."
LEVEL_TEXT = {
    "A": "WCAG level A: the basic level every site should meet.",
    "AA": "WCAG level AA: the level most accessibility policies ask for.",
    "AAA": "WCAG level AAA: the strictest level, beyond what most policies ask for.",
    "best-practice": "Best practice: not a WCAG requirement, but it makes the page easier to use with assistive "
                     "technology.",
}
_LEVEL_ORDER = {lv: i for i, lv in enumerate(axe_core.LEVELS)}
_IMPACT_ORDER = {im: i for i, im in enumerate(axe_core.IMPACTS)}

# axe-core category -> (how to fix it, in our words; one-line fix)
CATEGORY_FIX: Dict[str, Tuple[str, str]] = {
    "cat.aria": ("Use ARIA only as the ARIA specification allows: a valid role, the attributes that role requires "
                 "or allows, valid values, and the parent and child roles it needs. A native HTML element needs no "
                 "ARIA at all and is usually the better fix.",
                 "Fix the ARIA role or attribute, or use a native element"),
    "cat.color": ("Make text stand out from its background: at least 4.5:1 for normal text and 3:1 for large text "
                  "(7:1 and 4.5:1 for AAA), and do not mark links by colour alone.",
                  "Raise the contrast between text and its background"),
    "cat.forms": ("Give every form field one clear label that assistive technology can read, and valid "
                  "autocomplete values.", "Give every form field one clear, valid label"),
    "cat.keyboard": ("Make everything usable with the keyboard alone: scroll areas that can take focus, no "
                     "focusable elements hidden from screen readers, unique access keys and a way to skip repeated "
                     "blocks.", "Make every part of the page usable with the keyboard"),
    "cat.language": ("Set a valid language code on the <html> tag and on any part written in another language, "
                     "e.g. lang=\"en\" or lang=\"de-CH\".", "Use valid language codes in lang attributes"),
    "cat.name-role-value": ("Give every control and widget a name assistive technology can announce (visible "
                            "text, aria-label or aria-labelledby) and the role that says what it is.",
                            "Give each control and widget an accessible name"),
    "cat.parsing": ("Use each id once on the page, so labels and ARIA references point at the right element.",
                    "Use each id only once on the page"),
    "cat.semantics": ("Structure the page with the right elements: one main, banner and contentinfo landmark at "
                      "the top level, labelled landmarks when one kind repeats, and headings with text.",
                      "Use landmarks and headings for the page structure"),
    "cat.sensory-and-visual-cues": ("Let visitors zoom and change text spacing, keep touch targets big enough, and "
                                    "do not rely on look or position alone.",
                                    "Allow zoom and text spacing; keep targets big enough"),
    "cat.structure": ("Mark up lists, definition lists, regions and skip links with the elements and nesting "
                      "assistive technology expects.", "Use correct list, region and skip-link markup"),
    "cat.tables": ("Give data tables header cells (<th>) that describe data cells, valid scope and headers "
                   "attributes, and a caption that differs from the summary.", "Give data tables proper header cells"),
    "cat.text-alternatives": ("Give images, image buttons, image maps, SVGs and embedded objects a short text "
                              "alternative that says what they show or do (alt=\"\" when purely decorative), and do "
                              "not repeat the text next to them.", "Add text alternatives to images and objects"),
    "cat.time-and-media": ("Caption videos, do not play sound on its own, and avoid moving, blinking or "
                           "self-refreshing content.", "Caption videos; avoid autoplay, motion and refresh"),
}
# One-line fixes of common rules, in our words (others use their category's)
RULE_SHORT_FIX: Dict[str, str] = {
    "color-contrast": "Darken the text or lighten its background (4.5:1)",
    "color-contrast-enhanced": "Raise text contrast to 7:1 (4.5:1 for large text)",
    "link-in-text-block": "Underline links in text, or give them 3:1 contrast",
    "region": "Put all content inside landmarks (header, nav, main...)",
    "landmark-unique": "Give repeated landmarks distinct aria-labels",
    "list": "Put only <li> (or script/template) directly in <ul>/<ol>",
    "listitem": "Place every <li> inside a <ul> or <ol>",
    "definition-list": "Put only <dt>/<dd> groups directly in <dl>",
    "dlitem": "Place every <dt> and <dd> inside a <dl>",
    "html-lang-valid": "Use a valid language code in <html lang>, e.g. en-GB",
    "valid-lang": "Use valid language codes in lang attributes",
    "html-xml-lang-mismatch": "Give lang and xml:lang on <html> the same language",
    "aria-roles": "Use only valid ARIA role names",
    "aria-allowed-attr": "Remove ARIA attributes the element's role does not allow",
    "aria-allowed-role": "Use a role the element allows, or none",
    "aria-required-attr": "Add the ARIA attributes the role requires",
    "aria-required-children": "Add the child roles this ARIA role needs",
    "aria-required-parent": "Place the element inside the parent role it needs",
    "aria-valid-attr": "Fix misspelled aria-* attribute names",
    "aria-valid-attr-value": "Give aria-* attributes valid values and real ids",
    "aria-hidden-focus": "Remove focusable elements from aria-hidden content",
    "aria-hidden-body": "Remove aria-hidden from the <body>",
    "aria-prohibited-attr": "Remove ARIA naming the element's role forbids",
    "aria-deprecated-role": "Replace the deprecated ARIA role",
    "aria-dialog-name": "Name dialogs with aria-label or aria-labelledby",
    "aria-text": "Do not put focusable elements inside role=text",
    "presentation-role-conflict": "Remove focus or ARIA from role=none elements",
    "nested-interactive": "Do not put controls inside other controls",
    "scrollable-region-focusable": "Make scrollable areas focusable (tabindex=\"0\")",
    "bypass": "Add a skip link or landmarks to skip repeated blocks",
    "skip-link": "Point skip links at an element that exists",
    "accesskeys": "Use each accesskey value only once per page",
    "meta-viewport": "Remove user-scalable=no and maximum-scale below 2",
    "meta-viewport-large": "Let visitors zoom to at least 500%",
    "target-size": "Make touch targets 24x24 px or space them out",
    "avoid-inline-spacing": "Do not lock text spacing with !important styles",
    "empty-heading": "Give every heading text, or remove it",
    "empty-table-header": "Give every table header cell text",
    "page-has-heading-one": "Add one <h1> to the page",
    "document-title": "Add a non-empty <title> to the page",
    "duplicate-id-aria": "Use each id that ARIA or a label points at once",
    "form-field-multiple-labels": "Give each form field a single label",
    "label-title-only": "Add a visible label; a title alone is not enough",
    "label-content-name-mismatch": "Start the accessible name with the visible label",
    "autocomplete-valid": "Use valid autocomplete values (email, tel...)",
    "image-redundant-alt": "Do not repeat the nearby text in the alt text",
    "object-alt": "Add a text alternative to each <object>",
    "area-alt": "Add alt text to every clickable <area>",
    "server-side-image-map": "Replace the server-side image map with links",
    "video-caption": "Add a captions <track> to every video",
    "no-autoplay-audio": "Do not autoplay sound over 3 s; add controls",
    "marquee": "Replace <marquee> with static text",
    "blink": "Replace <blink> with static text",
    "meta-refresh": "Remove the timed meta refresh; redirect on the server",
    "meta-refresh-no-exceptions": "Remove every timed meta refresh",
    "frame-title-unique": "Give each iframe its own title",
    "frame-focusable-content": "Do not set tabindex=-1 on frames with controls",
    "th-has-data-cells": "Make each table header describe data cells",
    "td-headers-attr": "Point headers attributes at header cells of the table",
    "scope-attr-valid": "Use scope=\"row\" or scope=\"col\" on <th> only",
    "table-duplicate-name": "Make the table caption and summary differ",
    "summary-name": "Give each <summary> element visible text",
    "identical-links-same-purpose": "Give links with the same text the same target",
}


def issue_kind(level: str, impact: str) -> Tuple[str, str]:
    """(issue type, priority) of an axe-core rule from its impact (AAA rules are Opportunities)."""
    if level == "AAA":
        return "Opportunity", ("Medium" if impact in ("critical", "serious") else "Low")
    return {"critical": ("Issue", "High"), "serious": ("Issue", "Medium"), "moderate": ("Warning", "Medium"),
            "minor": ("Warning", "Low")}.get(impact, ("Warning", "Low"))


def group_of(level: str, impact: str) -> str:
    return f"{axe_core.level_label(level)} / {impact}"


def _set_group(f: Filter, group: str) -> Filter:
    f.group = group          # Filter.group (registry, P1); set as an attribute so an older registry still loads
    return f


def _band(name: str):
    return lambda r, site: (result(site, r) or {}).get("band") == name


def _failed(cid: str):
    return lambda r, site: bool((result(site, r) or {}).get("checks", {}).get(cid))


def _rule_test(rule_id: str):
    return lambda r, site: rule_id in _violated(r, site)


def _decided_by(cid: str) -> str:
    rules = AXE_MATCH.get(cid) or ()
    if not rules:
        return ""
    if len(rules) == 1:
        return f" On pages axe-core checked, its {rules[0]} rule decides."
    return f" On pages axe-core checked, its {', '.join(rules)} rules decide."


def _matched_group(cid: str) -> str:
    rid = (AXE_MATCH.get(cid) or (BASIC_ONLY.get(cid),))[0]
    meta = axe_core.rule(rid or "") if rid else None
    if cid in BASIC_ONLY or not meta:
        return BASIC_SOURCE
    return group_of(meta["level"], meta["impact"])


BASIC_FILTERS: Dict[str, str] = {           # filter id -> basic check id
    "missing_lang": "html_lang", "unlabelled_fields": "labels", "unnamed_links": "link_names",
    "unnamed_buttons": "button_names", "unnamed_controls": "control_names", "images_no_alt": "image_alt",
    "untitled_frames": "frame_titles", "duplicate_ids": "duplicate_ids", "broken_aria_refs": "aria_refs",
    "no_main_landmark": "main_landmark", "skipped_headings": "heading_order", "positive_tabindex": "tabindex",
}
CHECK_FILTER: Dict[str, str] = {cid: fid for fid, cid in BASIC_FILTERS.items()}

SCORE_FILTERS = [
    Filter("score_poor", "Accessibility Score Poor", _band("Poor"),
           description=f"Pages scoring under {POOR_BELOW} on our accessibility score: they fail several of the "
                       "weightiest checks (labels, names, alt text, language, contrast). The score is our own (see "
                       "the tab notes), not Lighthouse's." + _NOTE),
    Filter("score_needs_improvement", "Accessibility Score Needs Improvement", _band("Needs Improvement"),
           description=f"Pages scoring {POOR_BELOW} to {GOOD_MIN - 1}: they fail at least one important check."
                       + _NOTE),
    Filter("score_good", "Accessibility Score Good", _band("Good"),
           description=f"Pages scoring {GOOD_MIN} or more: they pass the important automated checks." + _NOTE),
]
BASIC_FILTER_LIST = [
    Filter("missing_lang", "Missing HTML Lang", _failed("html_lang"), "Issue", "Medium",
           description="The <html> tag has no lang attribute (or an empty one). Screen readers then guess the "
                       "language and may read the page with the wrong pronunciation; translation tools guess too."
                       + _NOTE + _decided_by("html_lang"),
           fix="Add the page language to the <html> tag, e.g. <html lang=\"en\">.",
           short_fix="Add lang to <html>, e.g. <html lang=\"en\">"),
    Filter("unlabelled_fields", "Form Fields Without Label", _failed("labels"), "Issue", "High",
           description="Form fields (inputs, selects, text areas) with no label a screen reader can announce: no "
                       "<label>, aria-label, aria-labelledby, title or placeholder. Blind visitors cannot tell "
                       "what to type." + _NOTE + _decided_by("labels"),
           fix="Give every field a visible <label for=\"id\"> (or an aria-label when a visible label is not "
               "possible).",
           short_fix="Give every form field a <label> or aria-label"),
    Filter("unnamed_links", "Links Without Accessible Name", _failed("link_names"), "Issue", "Medium",
           description="Links with no text a screen reader can announce (icon-only links, empty <a> tags, images "
                       "without alt inside links)." + _NOTE + _decided_by("link_names"),
           fix="Add link text, an aria-label, or alt text on the image inside the link that says where it goes.",
           short_fix="Give icon links text, aria-label or image alt"),
    Filter("unnamed_buttons", "Buttons Without Accessible Name", _failed("button_names"), "Issue", "High",
           description="Buttons with no text a screen reader can announce (icon-only buttons such as menu, "
                       "search or close)." + _NOTE + _decided_by("button_names"),
           fix="Add visible text or an aria-label to each button (e.g. aria-label=\"Open menu\").",
           short_fix="Give icon buttons text or an aria-label"),
    Filter("unnamed_controls", "Other Controls Without Accessible Name", _failed("control_names"), "Issue", "Medium",
           requires=("rendering_or_axe:a11y_rendered",),
           description="Custom widgets (checkboxes, switches, tabs, sliders, menus built from <div>s) that have no "
                       "name in Chrome's accessibility tree, so assistive technology announces them without "
                       "saying what they are for. Needs JavaScript rendering." + _NOTE + _decided_by("control_names"),
           fix="Give each widget an aria-label or aria-labelledby, or use the native HTML element.",
           short_fix="Name custom widgets with aria-label"),
    Filter("images_no_alt", "Images Without Alt Attribute", _failed("image_alt"), "Issue", "Medium",
           description="The page shows images with no alt attribute (decorative images marked role=\"presentation\""
                       " or aria-hidden are not counted). Screen readers may read out the file name instead."
                       + _NOTE + _decided_by("image_alt"),
           fix="Add alt text that describes each meaningful image, and alt=\"\" on purely decorative ones.",
           short_fix="Add alt text (alt=\"\" for decorative images)"),
    Filter("untitled_frames", "Frames Without Title", _failed("frame_titles"), "Warning", "Medium",
           requires=("rendering_or_axe:a11y_rendered",),
           description="Embedded frames (videos, maps, widgets) without a title, so screen reader users cannot "
                       "tell what the frame contains. Needs JavaScript rendering." + _NOTE
                       + _decided_by("frame_titles"),
           fix="Add a short title attribute to each <iframe>, e.g. title=\"Map of our office\".",
           short_fix="Add a title to each <iframe>"),
    Filter("duplicate_ids", "Duplicate IDs", _failed("duplicate_ids"), "Warning", "Low",
           description="The same id is used on more than one element. Labels, ARIA references and in-page links "
                       "that use the id may point at the wrong element." + _NOTE + " Read from the HTML on every "
                       "page (axe-core's narrower duplicate-id-aria rule is a filter of its own).",
           fix="Make every id unique on the page (see Duplicate IDs for the ones repeated).",
           short_fix="Make every id on the page unique"),
    Filter("broken_aria_refs", "Broken ARIA / Label References", _failed("aria_refs"), "Warning", "Medium",
           description="aria-labelledby, aria-describedby, aria-controls, aria-owns or <label for> name an id that "
                       "no element on the page has, so the label or description is lost." + _NOTE + " Read from "
                       "the HTML on every page (axe-core's wider aria-valid-attr-value rule is a filter of its own).",
           fix="Point the reference at an existing id, or add the missing element (see Broken References).",
           short_fix="Point ARIA/label references at existing ids"),
    Filter("no_main_landmark", "No Main Landmark", _failed("main_landmark"), "Opportunity", "Low",
           description="The page has no <main> element (or role=\"main\"), so screen reader users cannot jump "
                       "straight to the content past the menus." + _NOTE + _decided_by("main_landmark"),
           fix="Wrap the page's main content in a <main> element.",
           short_fix="Wrap the main content in <main>"),
    Filter("skipped_headings", "Skipped Heading Levels", _failed("heading_order"), "Opportunity", "Low",
           description="Headings jump a level (for example an h2 followed by an h4), which breaks the outline "
                       "screen reader users navigate by." + _NOTE + _decided_by("heading_order"),
           fix="Use heading levels in order (h1, h2, h3...) and style them with CSS rather than picking a level "
               "for its size.",
           short_fix="Use heading levels in order (h1 > h2 > h3)"),
    Filter("positive_tabindex", "Positive Tabindex", _failed("tabindex"), "Warning", "Low",
           description="Elements with tabindex greater than 0 change the order keyboard users move through the "
                       "page, which usually makes it confusing." + _NOTE + _decided_by("tabindex"),
           fix="Use tabindex=\"0\" or \"-1\" only, and fix the order in the HTML instead.",
           short_fix="Use tabindex 0 or -1 only"),
]
for _f in SCORE_FILTERS:
    _set_group(_f, "Score")
for _f in BASIC_FILTER_LIST:
    _set_group(_f, _matched_group(BASIC_FILTERS[_f.id]))


def filter_id_of(rule_id: str) -> str:
    """The filter that counts an axe-core rule: the basic check's filter when the rule decides one, else
    axe_<rule id with _>."""
    cid = RULE_CHECK.get(rule_id)
    return CHECK_FILTER[cid] if cid else "axe_" + rule_id.replace("-", "_")


def _axe_filter(meta: Dict[str, Any]) -> Filter:
    rid, level, impact = meta["id"], meta["level"], meta["impact"]
    cat = next((t for t in meta["tags"] if t.startswith("cat.")), "")
    how, short = CATEGORY_FIX.get(cat, ("Fix each element listed in the Accessibility Details pane.",
                                        "Fix the elements listed in Accessibility Details"))
    short = RULE_SHORT_FIX.get(rid, short)
    issue_type, priority = issue_kind(level, impact)
    text = meta.get("description") or meta.get("help") or rid
    f = Filter(
        filter_id_of(rid), meta.get("help") or rid, _rule_test(rid), issue_type, priority,
        description=f"{text.rstrip('.')}. {LEVEL_TEXT.get(level, '')} axe-core rule {rid} (impact: {impact}), "
                    "checked in pages rendered in Chrome; pages it did not check are not counted." + _NOTE,
        fix=f"{short}. {how}" + (f" Rule details: {meta['help_url']}" if meta.get("help_url") else ""),
        short_fix=short, requires=("axe:",), of_rows=axe_ran)
    return _set_group(f, group_of(level, impact))


def axe_filters() -> List[Filter]:
    """One filter per axe-core rule ARACHNIX runs and no basic check covers, ordered by WCAG level (A, AA, AAA,
    best practice), then impact (critical first), then name."""
    rules = [axe_core.rule(rid) for rid in axe_core.run_ids()]
    rules = [m for m in rules if m and m["id"] not in RULE_CHECK]
    rules.sort(key=lambda m: (_LEVEL_ORDER.get(m["level"], 9), _IMPACT_ORDER.get(m["impact"], 9),
                              str(m.get("help") or m["id"]).lower()))
    return [_axe_filter(m) for m in rules]


FILTERS = SCORE_FILTERS + BASIC_FILTER_LIST + axe_filters()
FILTER_NAMES: Dict[str, str] = {f.id: f.name for f in FILTERS}


def page_checked(r, filter_id: str) -> bool:
    """For crawler/intel/checks.py: whether this page was checked for the filter (an axe_ rule only on pages
    axe-core checked; a basic check when it or its axe-core rules measured the page)."""
    if filter_id.startswith("axe_"):
        return axe_ran(r)
    ev = evaluate(r)
    if ev is None:
        return False
    cid = BASIC_FILTERS.get(filter_id)
    return cid is None or cid in ev.get("checks", {})


def judge(tab_id: str, filter_id: str, site) -> Optional[Dict[str, Any]]:
    """For crawler/intel/checks.py (same shape as integrations.judge): which pages a check of this tab could judge.
    {"test": fn(r) -> bool, "reason": str (none could be judged), "caveat": str with {unknown} / {total}}."""
    if tab_id != "accessibility":
        return None
    if filter_id.startswith("axe_"):
        rows = content_rows(site) if site is not None else []
        return {"test": axe_ran, "reason": axe_reason(rows) or AXE_RENDER_REASON,
                "caveat": "{unknown} of {total} pages were not checked with axe-core (not rendered in Chrome, or "
                          "axe-core could not run there), so they were not checked."}
    return {"test": lambda r: page_checked(r, filter_id),
            "reason": "No page could be read for this check.",
            "caveat": "{unknown} of {total} pages could not be read for this check."}


# ---------------------------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------------------------
def _cell(fn):
    return lambda r, site: fn(r, site) if result(site, r) is not None else ""


def _axe_cell(fn):
    return lambda r, site: fn(axe_result(r)) if axe_ran(r) else ""


def _axe_nodes(r, cid: str) -> int:
    found = _violations(r)
    return sum(int(found[rid].get("nodes") or 0) for rid in AXE_MATCH.get(cid, ()) if rid in found)


def _check_count(cid: str, key: str):
    """A basic count column: axe-core's failing elements for the matched rules on a page it checked."""
    return _cell(lambda r, site: _axe_nodes(r, cid) if axe_ran(r) else (_count(r, key) or 0))


def checked_from(r) -> str:
    if axe_ran(r):
        return AXE_SOURCE
    return "Rendered page" if rendered_a11y(r) else "HTML"


DEVICE_WORDS = {"mobile": "phone", "tablet": "tablet"}     # rendering device (Configuration > Rendering) -> words


def axe_device(r) -> str:
    """The window axe-core checked the page in: "desktop" (also older crawls), "mobile" or "tablet"."""
    ax = axe_data(r) or {}
    device = str(ax.get("device") or "desktop")
    return device if device in DEVICE_WORDS else "desktop"


def axe_status(r) -> str:
    """"Checked (N rules)" (", phone window" / ", tablet window" when the crawl rendered on that device) or
    "Not checked: <why>"."""
    ax = axe_result(r)
    if ax is not None:
        where = DEVICE_WORDS.get(axe_device(r))
        return (f"Checked ({int(ax.get('rules') or 0) or len(axe_core.run_ids())} rules"
                + (f", {where} window)" if where else ")"))
    return "Not checked: " + page_reason(r)


def _main_cell(r, site) -> str:
    if axe_ran(r):
        return "No" if "landmark-one-main" in _violated(r, site) else "Yes"
    return "" if _main(r) is None else ("Yes" if _main(r) else "No")


def lighthouse_score(r, site):
    """Lighthouse accessibility score 0-100 from PageSpeed Insights ("" unless it tested the page with the
    accessibility category; our own score above does not depend on it)."""
    if site is None or not getattr(site, "integrations", None):
        return ""
    try:
        from ...integrations import psi
        v = psi.accessibility_score(site, getattr(r, "url", ""))
    except Exception:
        return ""
    return "" if v is None else v


COLUMNS = [
    ADDRESS, CONTENT_TYPE, STATUS_CODE, STATUS,
    Column("a11y_score", "Accessibility Score", _cell(lambda r, site: result(site, r)["score"]), "num"),
    Column("a11y_band", "Score Band", _cell(lambda r, site: result(site, r)["band"])),
    Column("a11y_scored_from", "Scored From", _cell(lambda r, site: result(site, r).get("source", BASIC_SOURCE))),
    Column("a11y_source", "Checked From", _cell(lambda r, site: checked_from(r))),
    Column("a11y_problems", "Problems", _cell(lambda r, site: result(site, r)["failed"]), "list"),
    Column("axe_status", "axe-core", lambda r, site: axe_status(r)),
    Column("axe_violations", "Rules Failed", _axe_cell(lambda ax: len(ax.get("violations") or [])), "num"),
    Column("axe_elements", "Elements Failing",
           _axe_cell(lambda ax: sum(int(v.get("nodes") or 0) for v in ax.get("violations") or [])), "num"),
    Column("axe_review", "Needs Review", _axe_cell(lambda ax: [rule_name(i) for i in ax.get("incomplete") or []]),
           "list"),
    Column("html_lang", "HTML Lang", lambda r, site: _lang(r) or ""),
    Column("unlabelled", "Fields Without Label", _check_count("labels", "inputs_without_label"), "num"),
    Column("unnamed_links", "Links Without Name", _check_count("link_names", "empty_links"), "num"),
    Column("unnamed_buttons", "Buttons Without Name", _check_count("button_names", "empty_buttons"), "num"),
    Column("images_no_alt", "Images Without Alt", _check_count("image_alt", "images_without_alt"), "num"),
    Column("duplicate_ids", "Duplicate IDs", lambda r, site: _list(r, "duplicate_ids"), "list"),
    Column("broken_refs", "Broken References", lambda r, site: _list(r, "aria_broken_refs"), "list"),
    Column("main_landmark", "Main Landmark", _cell(_main_cell)),
    Column("heading_skips", "Heading Skips", lambda r, site: _skips(r), "list"),
    Column("lighthouse_a11y", "Lighthouse Accessibility Score", lighthouse_score, "num"),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="accessibility", name="Accessibility", order=212, columns=COLUMNS, filters=FILTERS,
             rows=content_rows, requires=("pagedata:a11y",),
             description="axe-core's accessibility rules on pages rendered in Chrome, basic checks from the HTML "
                         "on the others, and our own 0-100 score."))


# ---------------------------------------------------------------------------------------------
# Accessibility Details (one row per failing element) and the bulk export
# ---------------------------------------------------------------------------------------------
_IMPACT_OF_WEIGHT = {10: "critical", 7: "serious", 3: "moderate"}


def _basic_rows(r, cid: str) -> List[Dict[str, Any]]:
    """Rows of a failed basic check: one per example element when known (duplicate / missing ids, the
    accessibility tree's examples), else one row for the page."""
    rid = (AXE_MATCH.get(cid) or (BASIC_ONLY.get(cid, ""),))[0]
    meta = axe_core.rule(rid) or {}
    base = {"rule": "", "name": CHECK_LABELS[cid], "filter": CHECK_FILTER[cid],
            "filter_name": FILTER_NAMES.get(CHECK_FILTER[cid], ""), "impact": _IMPACT_OF_WEIGHT[CHECK_WEIGHTS[cid]],
            "level": meta.get("level", "best-practice"), "elements": None, "selector": "", "snippet": "",
            "help_url": meta.get("help_url", ""), "source": BASIC_SOURCE}
    if cid == "duplicate_ids":
        ids = _list(r, "duplicate_ids")
        return [dict(base, elements=len(ids), selector=f"#{i}") for i in ids[:5]] or [base]
    if cid == "aria_refs":
        ids = _list(r, "aria_broken_refs")
        # the id that ARIA or <label for> names, which no element has
        return [dict(base, elements=len(ids), selector=f"#{i} (no such element)") for i in ids[:5]] or [base]
    examples = ((rendered_a11y(r) or {}).get("examples") or {})
    key = {"labels": "inputs_without_label", "link_names": "empty_links", "button_names": "empty_buttons",
           "control_names": "controls_without_name", "image_alt": "images_without_alt",
           "frame_titles": "frames_without_title"}.get(cid, "")
    count = _count(r, key) if key else None
    snippets = [str(s) for s in examples.get(key) or []][:5] if key else []
    return [dict(base, elements=count, snippet=s) for s in snippets] or [dict(base, elements=count)]


def violation_rows(site, r) -> List[Dict[str, Any]]:
    """Every failing element of one page: {"rule", "name", "filter", "filter_name", "impact", "level", "elements"
    (failing elements on the page for this rule), "selector", "snippet", "help_url", "source"} - axe-core's
    violations on a page it checked (plus the BASIC_ONLY checks), else the failed basic checks. Each filter of
    the tab that lists the page has at least one row here, so the export reconciles with the filter counts."""
    ev = result(site, r)
    if ev is None:
        return []
    out: List[Dict[str, Any]] = []
    ax = axe_result(r)
    if ax is not None:
        for v in ax.get("violations") or []:
            rid = str(v.get("id") or "")
            meta = axe_core.rule(rid) or {}
            fid = filter_id_of(rid)
            targets = list(v.get("targets") or [])
            snippets = list(v.get("snippets") or [])
            for i in range(max(1, len(targets))):
                out.append({"rule": rid, "name": meta.get("help") or rid, "filter": fid,
                            "filter_name": FILTER_NAMES.get(fid, ""), "impact": str(v.get("impact") or ""),
                            "level": str(v.get("level") or meta.get("level") or ""),
                            "elements": int(v.get("nodes") or 0),
                            "selector": targets[i] if i < len(targets) else "",
                            "snippet": snippets[i] if i < len(snippets) else "",
                            "help_url": meta.get("help_url", ""), "source": AXE_SOURCE})
    for cid, bad in (ev.get("checks") or {}).items():
        if bad and (ax is None or cid in BASIC_ONLY):
            out.extend(_basic_rows(r, cid))
    return out


def _in_tab(r) -> bool:
    return is_html_doc(r) and category_of(r) == "2xx"


def details(site, url: str) -> Optional[Dict[str, Any]]:
    """The Accessibility Details section of one URL (registry.details): None for URLs the tab does not list."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not _in_tab(r):
        return None
    ev = result(site, r)
    if ev is None:
        return {"not_checked": "The page's HTML could not be read for these checks, and axe-core did not check it."}
    ax = axe_data(r) or {}
    out: Dict[str, Any] = {
        "scored_from": ev.get("source", BASIC_SOURCE), "score": ev["score"], "band": ev["band"],
        "axe": {"version": str(ax.get("version") or axe_core.AXE_VERSION), "ran": bool(ax.get("ran")),
                "reason": page_reason(r), "ms": int(ax.get("ms") or 0),
                "rules": int(ax.get("rules") or 0) if ax.get("ran") else 0, "device": axe_device(r)},
        "violations": violation_rows(site, r),
        "needs_review": [{"rule": i, "name": rule_name(i), "help_url": (axe_core.rule(i) or {}).get("help_url", "")}
                         for i in (ax.get("incomplete") or []) if ax.get("ran")],
        "rules_passed": len(ax.get("passes") or []) if ax.get("ran") else 0,
        "rules_failed": len(ax.get("violations") or []) if ax.get("ran") else 0,
    }
    return out


EXPORT_HEADERS = ["Address", "Filter", "Rule ID", "Rule", "Impact", "WCAG Level", "Elements On Page", "Selector",
                  "HTML Snippet", "Help", "Checked With"]


def export_rows(site, crawler=None):
    """Bulk export accessibility_violations: one row per failing element of every page the tab lists."""
    for r in content_rows(site):
        for row in violation_rows(site, r):
            yield [r.url, row["filter_name"], row["rule"], row["name"], row["impact"],
                   axe_core.level_label(row["level"]) if row["level"] else "",
                   "" if row["elements"] is None else row["elements"], row["selector"], row["snippet"],
                   row["help_url"], row["source"]]


def export_not_checked(site) -> str:
    rows = content_rows(site)
    if not rows:
        return "No HTML page loaded in this crawl."
    if not any(result(site, r) is not None for r in rows):
        return "No page could be checked for accessibility (no page's HTML was read and axe-core did not run)."
    return ""


def _register_outputs():
    """The details section and the bulk export (registry.register_details and reports.register_bulk_export,
    P1); skipped while those helpers are not there."""
    try:
        from ..registry import register_details
        register_details("accessibility", details)
    except ImportError:
        pass
    try:
        from ...reports import register_bulk_export
    except ImportError:
        return
    kw = dict(headers=EXPORT_HEADERS, rows=export_rows, not_checked=export_not_checked)
    about = ("One row per failing element: the axe-core rule (or basic check), its impact and WCAG level, the "
             "element's CSS selector and HTML, and the rule's help page.")
    try:
        register_bulk_export("accessibility_violations", "Accessibility Violations (every element)",
                             description=about, **kw)
    except TypeError:           # a register_bulk_export without description=
        register_bulk_export("accessibility_violations", "Accessibility Violations (every element)", **kw)
    except Exception:
        pass


_register_outputs()
