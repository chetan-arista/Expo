"""
axe-core for the Accessibility tab: the pinned release in crawler/vendor/axe/ (fetched, unmodified, by
fetch_axe.py) and the data about its rules (axe/rules.json, read from that same file).

  source()      (axe.min.js text, "") or ("", why it cannot be used); the file is checked once against the
                pinned hash, so a changed or partial file is never injected into a page
  rules()       {rule id: {"id", "help", "description", "help_url", "tags", "level", "impact", "enabled",
                "deprecated", "experimental"}} (axe-core's own rule data; empty when rules.json is missing)
  run_ids()     the rules ARACHNIX runs: every rule except deprecated and experimental ones and SKIPPED_RULES
  wcag_level()  "A" | "AA" | "AAA" | "best-practice" from a rule's tags

CRAWLER_AXE=0 turns the axe-core checks off on a server (the Accessibility tab then uses its basic checks only and
says why the axe rules were not checked).
"""
import base64
import hashlib
import json
import os
import threading
from typing import Any, Dict, Iterable, List, Optional, Tuple

AXE_VERSION = "4.14.0"
AXE_TARBALL = f"https://registry.npmjs.org/axe-core/-/axe-core-{AXE_VERSION}.tgz"
# npm's integrity value of the tarball and axe-core's own published hash of axe.min.js (its sri-history.json)
AXE_INTEGRITY = "sha512-9WTZxEjsZ7b13TH8JPmbV2z8CHbl80/2hm3XPEG4JgNdQLK81IBRXmSxHfMAOkSqQeRxT/0dwNDz2GOm3zzpcQ=="
AXE_MIN_SHA256 = "sha256-IMCf4Veoo0ow4kGqofza3mV3NPCKs3ns++t9RcxG6Hg="

AXE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "axe")
AXE_FILE = os.path.join(AXE_DIR, "axe.min.js")
RULES_FILE = os.path.join(AXE_DIR, "rules.json")
FETCH_HINT = "run: python -m crawler.vendor.fetch_axe"

# Rules axe-core has that ARACHNIX does not run, with the reason
SKIPPED_RULES: Dict[str, str] = {
    # it reports frames axe-core could not look inside; that needs axe-core injected into every frame, while
    # ARACHNIX checks the page itself (content inside frames is not checked)
    "frame-tested": "Needs axe-core inside every frame; only the page itself is checked.",
}

IMPACTS = ("critical", "serious", "moderate", "minor")
LEVELS = ("A", "AA", "AAA", "best-practice")
_LEVEL_TAGS = (("A", ("wcag2a", "wcag21a", "wcag22a")), ("AA", ("wcag2aa", "wcag21aa", "wcag22aa")),
               ("AAA", ("wcag2aaa", "wcag21aaa", "wcag22aaa")))

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def file_sri(path: str) -> str:
    """"sha256-<base64>" of a file (the form axe-core publishes for axe.min.js)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return "sha256-" + base64.b64encode(h.digest()).decode("ascii")


def turned_off() -> bool:
    return str(os.environ.get("CRAWLER_AXE") or "").strip().lower() in ("0", "false", "no", "off")


def source() -> Tuple[str, str]:
    """(the axe.min.js text, "") or ("", the plain reason axe-core cannot run here). Read and checked once."""
    if turned_off():
        return "", "The axe-core checks are turned off on this server (CRAWLER_AXE=0)."
    with _lock:
        if "source" not in _cache:
            _cache["source"] = _read_source()
        return _cache["source"]


def _read_source() -> Tuple[str, str]:
    if not os.path.isfile(AXE_FILE):
        return "", f"axe-core is not installed on this server ({FETCH_HINT})."
    try:
        if file_sri(AXE_FILE) != AXE_MIN_SHA256:
            return "", f"The axe-core file does not match version {AXE_VERSION} ({FETCH_HINT})."
        with open(AXE_FILE, "r", encoding="utf-8") as f:
            return f.read(), ""
    except OSError as e:
        return "", f"The axe-core file could not be read ({type(e).__name__})."


def available() -> bool:
    return bool(source()[0])


def wcag_level(tags: Iterable[str]) -> str:
    """The lowest WCAG conformance level a rule belongs to ("A", "AA", "AAA"), else "best-practice"."""
    t = set(tags or ())
    for level, names in _LEVEL_TAGS:
        if t.intersection(names):
            return level
    return "best-practice"


def level_label(level: str) -> str:
    return "Best practice" if level == "best-practice" else f"WCAG {level}"


def rules() -> Dict[str, Dict[str, Any]]:
    """axe-core's rule data from rules.json ({} when the file is missing or unreadable)."""
    with _lock:
        if "rules" not in _cache:
            _cache["rules"] = _read_rules()
        return _cache["rules"]


def _read_rules() -> Dict[str, Dict[str, Any]]:
    try:
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for item in data.get("rules") or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        rid = str(item["id"])
        tags = [str(t) for t in item.get("tags") or []]
        out[rid] = {
            "id": rid, "help": str(item.get("help") or rid), "description": str(item.get("description") or ""),
            "help_url": str(item.get("help_url") or ""), "tags": tags, "level": wcag_level(tags),
            "impact": str(item.get("impact") or "") if item.get("impact") in IMPACTS else "moderate",
            "enabled": bool(item.get("enabled", True)), "deprecated": "deprecated" in tags,
            "experimental": "experimental" in tags,
        }
    return out


def runs(rule: Dict[str, Any]) -> bool:
    """ARACHNIX runs this rule (every rule but deprecated, experimental and SKIPPED_RULES ones; rules axe-core
    leaves off by default, such as the AAA ones and target size, are run too)."""
    return not (rule.get("deprecated") or rule.get("experimental") or rule.get("id") in SKIPPED_RULES)


def run_ids() -> List[str]:
    """The rule ids passed to axe.run (sorted)."""
    return sorted(rid for rid, r in rules().items() if runs(r))


def rule(rule_id: str) -> Optional[Dict[str, Any]]:
    return rules().get(rule_id)


def status() -> Dict[str, Any]:
    """{"version", "available", "reason", "rules"} for logs, tests and the API."""
    js, why = source()
    return {"version": AXE_VERSION, "available": bool(js), "reason": why, "rules": len(run_ids())}


def reset_cache():
    """Forget the loaded file and rules (after fetch_axe.py wrote new ones, and in tests)."""
    with _lock:
        _cache.clear()
