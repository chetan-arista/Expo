"""
Alert rules (Connections > Alerts; admin only): checked after every dashboard and scheduled crawl against the
previous crawl of the site, and sent by email and / or to chat and webhook connectors.

Rule kinds (KINDS):
  home_5xx         the home page (start URL, after its redirects inside the crawl) answers 5xx or does not answer
  home_4xx         the home page answers 4xx
  noindex_added    at least N pages that were crawled before without noindex now have it (needs the previous crawl)
  status_class_up  internal URLs of a status class (3xx, 4xx, 5xx, blocked, no response) went up by X % and at
                   least N URLs (from 0: any rise of N or more) (needs the previous crawl)
  new_high_issues  at least N new or returning URLs in High-priority issues (needs the previous crawl)
  filter_above     a filter's count (tab + filter, as in the tab's dropdown) is above N

A rule that cannot be judged says why ("No previous crawl of this site to compare with", "Not checked: ..."): it
never counts as passed or fired. Each rule fires at most once per crawl (crawl_key). Channels: "email" (the rule's
addresses, else the Settings > Notifications recipients) and connector ids (slack, teams, discord, webhook:<slug>);
a rule without channels goes to every chat / webhook connector that has Alerts ticked.

Stored in <reports_dir>/connectors/alerts.json: {"rules": [...], "fired": [keys], "last_run": {...}}.

  KINDS ; state(reports_dir) -> {"rules", "kinds", "channels", "last_run"} ; save_rules(rules) -> (rules, errors)
  validate_rules(rules) -> (clean, errors) ; evaluate(summary, compare_result, rules) -> [fired]
  evaluate_all(...) -> {"fired", "not_judged", "passed"} ; run_after_crawl(summary, compare_result, ...) -> run
"""
import copy
import os
import re
import uuid
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import messages, store
from .base import now_iso

FILE_NAME = "alerts.json"
MAX_RULES = 50
MAX_EMAILS = 20
MAX_FIRED_KEYS = 500
STATUS_CLASSES = ("3xx", "4xx", "5xx", "blocked", "error")
NO_PREVIOUS = "No previous crawl of this site to compare with, so this rule could not be judged."
KINDS: "OrderedDict[str, Dict[str, Any]]" = OrderedDict([
    ("home_5xx", {"label": "Home page is down (5xx or no answer)", "needs_previous": False, "params": {}}),
    ("home_4xx", {"label": "Home page answers 4xx", "needs_previous": False, "params": {}}),
    ("noindex_added", {"label": "noindex added on pages", "needs_previous": True,
                       "params": {"min_pages": {"kind": "number", "default": 1, "min": 1, "max": 10_000_000,
                                                "label": "At least this many pages"}}}),
    ("status_class_up", {"label": "URLs in a status class go up", "needs_previous": True,
                         "params": {"status_class": {"kind": "select", "default": "4xx", "options": list(STATUS_CLASSES),
                                                     "label": "Status class"},
                                    "percent": {"kind": "number", "default": 20, "min": 1, "max": 100_000,
                                                "label": "By at least this many %"},
                                    "min_urls": {"kind": "number", "default": 1, "min": 1, "max": 10_000_000,
                                                 "label": "And at least this many URLs"}}}),
    ("new_high_issues", {"label": "New High-priority issues", "needs_previous": True,
                         "params": {"min_urls": {"kind": "number", "default": 1, "min": 1, "max": 10_000_000,
                                                 "label": "At least this many new or returning URLs"}}}),
    ("filter_above", {"label": "A filter's count is above a number", "needs_previous": False,
                      "params": {"tab": {"kind": "text", "default": "", "label": "Tab"},
                                 "filter": {"kind": "text", "default": "", "label": "Filter"},
                                 "count": {"kind": "number", "default": 0, "min": 0, "max": 10_000_000,
                                           "label": "Fires when the count is above"}}}),
])
CLASS_TEXT = dict(messages.CATEGORY_TEXT)


def _path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(store.folder(reports_dir or None), FILE_NAME)


def _load(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    data = store.read_json(_path(reports_dir))
    data = data if isinstance(data, dict) else {}
    data["rules"] = [r for r in data.get("rules") or [] if isinstance(r, dict)]
    data["fired"] = [str(k) for k in data.get("fired") or []]
    return data


def _save(data: Dict[str, Any], reports_dir: Optional[str] = None) -> None:
    store.write_json(_path(reports_dir), data)


def load_rules(reports_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    return copy.deepcopy(_load(reports_dir)["rules"])


def channels() -> List[Dict[str, Any]]:
    """Where an alert can go: email and every chat / webhook connector (with its state)."""
    out = [{"id": "email", "name": "Email", "state": "connected" if _email_ready() else "not_connected"}]
    try:
        from . import registry
        for c in registry.by_kind("notify"):
            if c.id == "email_smtp" or c.is_template():
                continue
            st = c.status()
            out.append({"id": c.id, "name": c.name, "state": st.state, "alerts_ticked": "alert" in c.events()})
    except Exception:
        pass
    return out


def _email_ready() -> bool:
    try:
        from .. import notify
        return notify.configured()
    except Exception:
        return False


def state(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """What GET /api/alerts returns."""
    data = _load(reports_dir)
    kinds = [{"id": k, "label": v["label"], "needs_previous": v["needs_previous"],
              "params": [dict(p, key=key) for key, p in v["params"].items()]} for k, v in KINDS.items()]
    return {"rules": data["rules"], "kinds": kinds, "channels": channels(), "last_run": data.get("last_run")}


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def _filter_name(tab: str, fid: str) -> Optional[str]:
    """"Tab: Filter" of a registered filter, "" when the registry is not there, None when there is no such filter."""
    try:
        from ..seo.registry import get_tab
    except Exception:
        return ""
    t = get_tab(tab)
    if t is None:
        return None
    for f in t.filters:
        if f.id == fid:
            return f"{t.name}: {f.name}"
    return None


def _number(value: Any, spec: Dict[str, Any]) -> Tuple[Any, str]:
    if value in (None, ""):
        return spec["default"], ""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return spec["default"], f"{spec['label']} must be a number."
    if n != n or n < spec.get("min", 0) or n > spec.get("max", 10_000_000):
        return spec["default"], f"{spec['label']} must be between {spec.get('min', 0):,} and {spec.get('max'):,}."
    return (int(n) if n.is_integer() else n), ""


def validate_rules(rules: Any) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """(clean rules, {"rules.<i>.<field>": plain error})."""
    from ..settings_store import parse_emails
    errors: Dict[str, str] = {}
    if not isinstance(rules, list):
        return [], {"rules": "The rules must be a list."}
    if len(rules) > MAX_RULES:
        return [], {"rules": f"At most {MAX_RULES} rules."}
    known_channels = {c["id"] for c in channels()}
    out: List[Dict[str, Any]] = []
    seen_ids = set()
    for i, raw in enumerate(rules):
        r = raw if isinstance(raw, dict) else {}
        where = f"rules.{i}"
        kind = str(r.get("kind") or "").strip()
        if kind not in KINDS:
            errors[f"{where}.kind"] = "Choose what the rule watches."
            continue
        rid = str(r.get("id") or "").strip()
        if not re.match(r"^[a-z0-9-]{1,40}$", rid) or rid in seen_ids:
            rid = uuid.uuid4().hex[:10]
        seen_ids.add(rid)
        name = " ".join(str(r.get("name") or "").split())[:80] or KINDS[kind]["label"]
        params_in = r.get("params") if isinstance(r.get("params"), dict) else {}
        params: Dict[str, Any] = {}
        for key, spec in KINDS[kind]["params"].items():
            v = params_in.get(key, spec["default"])
            if spec["kind"] == "number":
                params[key], err = _number(v, spec)
            elif spec["kind"] == "select":
                params[key] = str(v or spec["default"]).strip()
                err = "" if params[key] in spec["options"] else f"{spec['label']} must be one of " + \
                    ", ".join(spec["options"]) + "."
            else:
                params[key] = str(v or "").strip()[:80]
                err = "" if params[key] else f"{spec['label']} is required."
            if err:
                errors[f"{where}.params.{key}"] = err
        if kind == "filter_above" and params.get("tab") and params.get("filter"):
            if _filter_name(params["tab"], params["filter"]) is None:
                errors[f"{where}.params.filter"] = "There is no such filter in that tab."
        chans = []
        for ch in r.get("channels") or []:
            ch = str(ch or "").strip()
            if not ch or ch in chans:
                continue
            if ch not in known_channels:
                errors[f"{where}.channels"] = f"Unknown channel: {ch[:40]}."
                continue
            chans.append(ch)
        emails, bad = parse_emails(r.get("emails") or [])
        if bad:
            errors[f"{where}.emails"] = "Not valid email addresses: " + ", ".join(bad[:3])
        if len(emails) > MAX_EMAILS:
            errors[f"{where}.emails"] = f"At most {MAX_EMAILS} addresses."
        out.append({"id": rid, "name": name, "kind": kind, "enabled": bool(r.get("enabled", True)),
                    "params": params, "channels": chans, "emails": emails[:MAX_EMAILS]})
    return out, errors


def save_rules(rules: Any, reports_dir: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Validates and saves every rule (PUT /api/alerts). Nothing is saved when there is an error."""
    clean, errors = validate_rules(rules)
    if errors:
        return clean, errors
    data = _load(reports_dir)
    data["rules"] = clean
    data["updated_at"] = now_iso()
    _save(data, reports_dir)
    return clean, {}


# ---------------------------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------------------------
def _usable_compare(compare_result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(compare_result, dict) or compare_result.get("error") or not compare_result.get("a"):
        return None
    return compare_result


def _judge(rule: Dict[str, Any], summary: Dict[str, Any], cmp_: Optional[Dict[str, Any]]
           ) -> Tuple[str, str, Any, Any]:
    """(outcome "fired" | "passed" | "not_judged", message or reason, value, threshold)."""
    kind, p = rule["kind"], rule.get("params") or {}
    if KINDS[kind]["needs_previous"] and cmp_ is None:
        return "not_judged", NO_PREVIOUS, None, None
    if kind in ("home_5xx", "home_4xx"):
        home = summary.get("home") or {}
        if not home.get("crawled"):
            return "not_judged", "The home page (start URL) was not crawled, so its answer is not known.", None, None
        cat = str(home.get("final_category") or home.get("category") or "")
        url = home.get("final_url") or home.get("url") or ""
        status = home.get("final_status") if home.get("final_status") is not None else home.get("status")
        if cat == "blocked":
            return "not_judged", "The home page is blocked by robots.txt, so its answer is not known.", None, None
        if kind == "home_5xx" and cat in ("5xx", "error"):
            what = f"answered {status}" if cat == "5xx" else "did not answer (no response)"
            return "fired", f"The home page {url} {what}.", status, "5xx / no response"
        if kind == "home_4xx" and cat == "4xx":
            return "fired", f"The home page {url} answered {status}.", status, "4xx"
        return "passed", f"The home page answered {status}.", status, None
    if kind == "noindex_added":
        row = next((r for r in cmp_.get("overview") or [] if r.get("key") == "directives:noindex"), None)
        if row is None:
            return "not_judged", "The comparison has no noindex counts for these crawls.", None, None
        if row.get("check_current") == "not_checked":
            return "not_judged", "Not checked: noindex was not checked in this crawl.", None, None
        if not row.get("exact") or row.get("added") is None:
            return "not_judged", ("The previous crawl was saved without its per-URL lists, so pages that gained "
                                  "noindex cannot be told apart."), None, None
        value, need = int(row.get("added") or 0), int(p.get("min_pages") or 1)
        if value >= need:
            return "fired", (f"noindex was added on {value:,} page{'s' if value != 1 else ''} that had no noindex in "
                             f"the previous crawl."), value, need
        return "passed", f"noindex added on {value:,} pages (alert from {need:,}).", value, need
    if kind == "status_class_up":
        cls = p.get("status_class") or "4xx"
        row = next((r for r in cmp_.get("totals") or [] if r.get("key") == f"category:{cls}"), None)
        prev, cur = (int(row.get("previous") or 0), int(row.get("current") or 0)) if row else (0, 0)
        change, need, pct_need = cur - prev, int(p.get("min_urls") or 1), float(p.get("percent") or 0)
        label = CLASS_TEXT.get(cls, cls)
        if change < need:
            return "passed", f"{label} internal URLs: {prev:,} -> {cur:,}.", cur, pct_need
        if prev == 0:
            return "fired", f"{label} internal URLs went up from 0 to {cur:,}.", cur, pct_need
        pct = change / prev * 100.0
        if pct >= pct_need:
            return "fired", (f"{label} internal URLs went up {pct:,.0f} %: {prev:,} -> {cur:,} (+{change:,})."), \
                round(pct, 1), pct_need
        return "passed", f"{label} internal URLs: {prev:,} -> {cur:,} (+{pct:,.0f} %).", round(pct, 1), pct_need
    if kind == "new_high_issues":
        rows = [i for i in cmp_.get("issues") or [] if i.get("priority") == "High"
                and int(i.get("new_count") or 0) + int(i.get("regressed_count") or 0) > 0]
        value = sum(int(i.get("new_count") or 0) + int(i.get("regressed_count") or 0) for i in rows)
        need = int(p.get("min_urls") or 1)
        if value >= need:
            names = ", ".join(f"{i.get('name')} ({int(i.get('new_count') or 0) + int(i.get('regressed_count') or 0):,})"
                              for i in rows[:5])
            more = f" and {len(rows) - 5} more" if len(rows) > 5 else ""
            return "fired", f"{value:,} new or returning URLs in High-priority issues: {names}{more}.", value, need
        return "passed", f"{value:,} new or returning URLs in High-priority issues.", value, need
    if kind == "filter_above":
        key = f"{p.get('tab')}:{p.get('filter')}"
        counts = summary.get("filter_counts")
        label = _filter_name(str(p.get("tab")), str(p.get("filter"))) or key
        if not isinstance(counts, dict) or not counts:
            return "not_judged", "The filter counts were not recorded for this crawl.", None, None
        why = (summary.get("filter_not_checked") or {}).get(key)
        if why:
            return "not_judged", f"Not checked: {why}", None, None
        if key not in counts:
            return "not_judged", f"This crawl has no filter {label}.", None, None
        value, limit = int(counts[key]), int(p.get("count") or 0)
        if value > limit:
            return "fired", f"{label}: {value:,} URLs (alert above {limit:,}).", value, limit
        return "passed", f"{label}: {value:,} URLs.", value, limit
    return "not_judged", "Unknown rule.", None, None


def evaluate_all(summary: Dict[str, Any], compare_result: Optional[Dict[str, Any]], rules: List[Dict[str, Any]]
                 ) -> Dict[str, List[Dict[str, Any]]]:
    """Every enabled rule judged: {"fired": [...], "not_judged": [...], "passed": [...]}, each item
    {"rule_id", "name", "kind", "message", "value", "threshold", "channels"} ("reason" for not_judged)."""
    cmp_ = _usable_compare(compare_result)
    out: Dict[str, List[Dict[str, Any]]] = {"fired": [], "not_judged": [], "passed": []}
    for rule in rules or []:
        if not isinstance(rule, dict) or not rule.get("enabled", True) or rule.get("kind") not in KINDS:
            continue
        try:
            outcome, text, value, threshold = _judge(rule, summary or {}, cmp_)
        except Exception as e:
            outcome, text, value, threshold = "not_judged", f"The rule could not be judged ({type(e).__name__}).", \
                None, None
        item = {"rule_id": rule.get("id"), "name": rule.get("name") or KINDS[rule["kind"]]["label"],
                "kind": rule["kind"], "value": value, "threshold": threshold,
                "channels": list(rule.get("channels") or [])}
        item["reason" if outcome == "not_judged" else "message"] = text
        out[outcome].append(item)
    return out


def evaluate(summary: Dict[str, Any], compare_result: Optional[Dict[str, Any]], rules: List[Dict[str, Any]]
             ) -> List[Dict[str, Any]]:
    """The rules that fire for this crawl."""
    return evaluate_all(summary, compare_result, rules)["fired"]


# ---------------------------------------------------------------------------------------------
# After a crawl
# ---------------------------------------------------------------------------------------------
def _send_email(rule: Dict[str, Any], fired: Dict[str, Any], summary: Dict[str, Any],
                mask: Optional[Callable[[str], str]], reports_dir: Optional[str]) -> Dict[str, Any]:
    from .. import notify, settings_store
    to = list(rule.get("emails") or [])
    if not to:
        try:
            to = list(settings_store.effective("notifications", reports_dir).get("recipients") or [])
        except Exception:
            to = []
    if not to:
        return {"connector": "email", "name": "Email", "ok": False,
                "detail": "No recipients: add addresses to the rule or under Settings > Notifications."}
    s = messages.public_summary(summary, mask)
    if not s.get("dashboard_url"):
        from .events import _dashboard_url
        s["dashboard_url"] = _dashboard_url(reports_dir or "")
    brand = notify.brand_info(reports_dir)          # Settings > Branding (default: ARACHNIX)
    subject = brand["subject_prefix"] + messages.title("alert", s, fired)
    text = "\n".join(messages.lines("alert", s, fired, limit=20)) + f"\n\nSent by {brand['name']}."
    if brand.get("footer"):
        text += "\n" + brand["footer"]
    if mask is not None:
        subject, text = mask(subject), mask(text)
    res = notify.send(to, subject, text, None, None, None, reports_dir, brand=brand)
    return {"connector": "email", "name": "Email", "ok": bool(res.get("ok")),
            "detail": f"Sent to {len(res.get('sent_to') or [])} recipient(s)." if res.get("ok") else
            str(res.get("error") or "Not sent.")}


def _deliver(rule: Dict[str, Any], fired: Dict[str, Any], summary: Dict[str, Any],
             mask: Optional[Callable[[str], str]], reports_dir: Optional[str]) -> List[Dict[str, Any]]:
    from .events import notify_event
    chans = list(rule.get("channels") or [])
    out: List[Dict[str, Any]] = []
    if "email" in chans:
        out.append(_send_email(rule, fired, summary, mask, reports_dir))
    others = [c for c in chans if c != "email"]
    if others or not chans:
        out += notify_event("alert", summary, reports_dir or "", only=others or None, mask=mask, alert=fired,
                            explicit=bool(others))
    return out


def run_after_crawl(summary: Dict[str, Any], compare_result: Optional[Dict[str, Any]] = None,
                    reports_dir: Optional[str] = None, mask: Optional[Callable[[str], str]] = None,
                    crawl_key: str = "") -> Dict[str, Any]:
    """Judges every enabled rule, sends each rule that fires (once per crawl_key) and remembers the run."""
    data = _load(reports_dir)
    rules = [r for r in data["rules"] if r.get("enabled", True)]
    res = evaluate_all(summary, compare_result, rules)
    key_base = crawl_key or f"{(summary or {}).get('site', '')}|{(summary or {}).get('started', '')}"
    fired_keys = list(data["fired"])
    deliveries: List[Dict[str, Any]] = []
    by_id = {r.get("id"): r for r in rules}
    for f in res["fired"]:
        key = f"{f['rule_id']}|{key_base}"
        if key in fired_keys:
            f["repeat"] = True              # already sent for this crawl
            continue
        fired_keys.append(key)
        sent = _deliver(by_id.get(f["rule_id"]) or {}, f, summary, mask, reports_dir)
        f["deliveries"] = sent
        deliveries += sent
    data["fired"] = fired_keys[-MAX_FIRED_KEYS:]
    data["last_run"] = {"at": now_iso(), "site": messages.clean_url((summary or {}).get("site")),
                        "fired": [{k: f.get(k) for k in ("rule_id", "name", "message", "repeat")} for f in res["fired"]],
                        "not_judged": [{k: f.get(k) for k in ("rule_id", "name", "reason")} for f in res["not_judged"]],
                        "passed": [{k: f.get(k) for k in ("rule_id", "name", "message")} for f in res["passed"]]}
    _save(data, reports_dir)
    return dict(res, deliveries=deliveries)
