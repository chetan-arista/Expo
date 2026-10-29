"""
schema.org vocabulary checks for JSON-LD items (P12): unknown types, unknown properties, properties used on a type
that does not have them, and values of the wrong kind (an object where text is expected, a date that is not a date,
a number that is not a number), down through nested items (offers -> Offer, location -> Place -> PostalAddress).

The vocabulary is crawler/seo/data/schema_vocab.json, built by tools/build_schema_vocab.py from schema.org's
published file (CC BY-SA 3.0). Terms of other vocabularies (a prefix or a full URL that is not schema.org) are
not judged. Microdata and RDFa items carry property names only, so only their types are checked.

validate(item) -> {"errors": [...], "warnings": [...]} for one PageData structured_data item; every message starts
with the dotted path it is about ("offers.price: ...").
"""
import difflib
import json
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "schema_vocab.json")
MAX_DEPTH = 6
MAX_MESSAGES = 40
SCHEMA_PREFIXES = ("https://schema.org/", "http://schema.org/", "schema:")
DATE_TYPES = {"Date", "DateTime", "Time"}
NUMBER_TYPES = {"Number", "Integer", "Float"}
TEXT_LIKE = {"Text", "URL", "CssSelectorType", "XPathType", "PronounceableText"}
# Properties schema.org lets be Text but Google's rich results read as numbers only ("Invalid price format")
GOOGLE_NUMBERS = {"price", "lowPrice", "highPrice", "ratingValue", "bestRating", "worstRating", "ratingCount",
                  "reviewCount", "minValue", "maxValue"}

_vocab: Optional[Dict[str, Any]] = None
_ancestors: Dict[str, Set[str]] = {}

_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_TIME = re.compile(r"^\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_NUMBER = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")


def vocab() -> Dict[str, Any]:
    global _vocab
    if _vocab is None:
        try:
            with open(DATA, encoding="utf-8") as f:
                _vocab = json.load(f)
        except (OSError, ValueError):
            _vocab = {"types": {}, "props": {}, "datatypes": [], "members": {}, "superseded": {}}
    return _vocab


def available() -> bool:
    return bool(vocab().get("types"))


def ancestors(t: str) -> Set[str]:
    """The type and every type above it."""
    if t in _ancestors:
        return _ancestors[t]
    types = vocab()["types"]
    seen, todo = set(), [t]
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        todo.extend(types.get(x, ()))
    _ancestors[t] = seen
    return seen


def _term(value: Any) -> Tuple[str, bool]:
    """(name, is_schema_org) of a type or property term; other vocabularies come back with False."""
    s = str(value or "").strip()
    for p in SCHEMA_PREFIXES:
        if s.lower().startswith(p):
            return s[len(p):], True
    if ":" in s or "/" in s:
        return s, False
    return s, True


def _suggest(name: str, pool) -> str:
    near = difflib.get_close_matches(name, pool, n=1, cutoff=0.8)
    if not near:
        low = {p.lower(): p for p in pool}
        if name.lower() in low:
            near = [low[name.lower()]]
    return f" (did you mean '{near[0]}'?)" if near else ""


def _is_datatype(t: str) -> bool:
    return t in set(vocab()["datatypes"])


def _accepts_type(ranges: List[str], t: str) -> bool:
    up = ancestors(t)
    return any(r in up for r in ranges)


class _Run:
    def __init__(self):
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def err(self, msg: str):
        if len(self.errors) < MAX_MESSAGES and msg not in self.errors:
            self.errors.append(msg)

    def warn(self, msg: str):
        if len(self.warnings) < MAX_MESSAGES and msg not in self.warnings:
            self.warnings.append(msg)


def _types_of(node: dict, run: _Run, path: str) -> List[str]:
    """schema.org types of a node (unknown ones reported); [] when it has none we can judge."""
    v = vocab()
    raw = node.get("@type")
    out: List[str] = []
    for t in (raw if isinstance(raw, list) else [raw]):
        if not t or not isinstance(t, str):
            continue
        name, ours = _term(t)
        if not ours:
            continue
        where = f"{path}: " if path else ""
        if name in v["types"]:
            if name in v["superseded"]:
                run.warn(f"{where}type '{name}' is replaced by '{v['superseded'][name]}' on schema.org")
            out.append(name)
        else:
            run.err(f"{where}unknown type '{name}'{_suggest(name, v['types'])}")
    return out


def _check_value(prop: str, value: Any, ranges: List[str], run: _Run, path: str, depth: int) -> None:
    v = vocab()
    data = [r for r in ranges if _is_datatype(r)]
    objects = [r for r in ranges if not _is_datatype(r)]
    if isinstance(value, dict):
        if set(value) <= {"@id"} or "@value" in value:
            return                                         # a reference to another node, or a typed literal
        types = _types_of(value, run, path)
        if types and objects and not any(_accepts_type(objects, t) for t in types):
            run.warn(f"{path}: '{prop}' expects {' or '.join(objects[:4])}, not {' / '.join(types)}")
        elif not objects:
            run.err(f"{path}: '{prop}' should be {'text' if set(data) & TEXT_LIKE else ' or '.join(data[:3])}, "
                    f"not an object")
        _check_node(value, types or objects[:1], run, path, depth + 1)
        return
    if isinstance(value, bool):
        if "Boolean" not in ranges and not set(data) & TEXT_LIKE:
            run.warn(f"{path}: '{prop}' does not take true / false")
        return
    if isinstance(value, (int, float)):
        if data and not set(data) & (NUMBER_TYPES | TEXT_LIKE | {"Quantity", "Duration", "Distance", "Energy",
                                                                 "Mass", "Boolean"}) and not objects:
            run.err(f"{path}: '{prop}' should be {' or '.join(data[:3])}, not a number")
        return
    if not isinstance(value, str):
        return
    s = value.strip()
    if prop in GOOGLE_NUMBERS and s and not _NUMBER.match(s.replace(",", "")):
        run.err(f"{path}: '{prop}' should be a number for Google's rich results (dot for decimals, no currency "
                f"sign or words), not '{s[:40]}'")
        return
    if set(data) & TEXT_LIKE or not ranges:
        return                                             # text is fine
    if objects:
        name, ours = _term(s)
        enum_types = {v["members"].get(name)} if ours and name in v["members"] else set()
        if enum_types and not any(_accepts_type(objects, e) for e in enum_types if e):
            run.warn(f"{path}: '{s[:60]}' is not a value '{prop}' takes")
        return                                             # Google accepts text for most object properties
    if set(data) <= DATE_TYPES:
        ok = (("Date" in data and _DATE.match(s)) or ("DateTime" in data and (_DATETIME.match(s) or _DATE.match(s)))
              or ("Time" in data and _TIME.match(s)))
        if not ok:
            want = "a date (YYYY-MM-DD)" if data == ["Date"] else "an ISO 8601 date or date and time"
            run.err(f"{path}: '{prop}' should be {want}, not '{s[:40]}'")
    elif set(data) <= NUMBER_TYPES:
        if not _NUMBER.match(s.replace(",", "")) or ("," in s and "." in s and s.index(",") > s.index(".")):
            run.err(f"{path}: '{prop}' should be a number (dot for decimals, no currency sign), not '{s[:40]}'")
    elif set(data) <= {"Boolean"} and s.lower().rsplit("/", 1)[-1] not in ("true", "false"):
        run.err(f"{path}: '{prop}' should be true or false, not '{s[:40]}'")


def _check_node(node: dict, types: List[str], run: _Run, path: str, depth: int) -> None:
    if depth > MAX_DEPTH or not isinstance(node, dict):
        return
    v = vocab()
    up: Set[str] = set()
    for t in types:
        up |= ancestors(t)
    for key, value in node.items():
        if not isinstance(key, str) or key.startswith("@"):
            if key == "@graph" and isinstance(value, list):
                for i, member in enumerate(value):
                    if isinstance(member, dict):
                        _check_node(member, _types_of(member, run, f"@graph[{i}]"), run, f"@graph[{i}]", depth + 1)
            continue
        name, ours = _term(key)
        if not ours:
            continue
        here = f"{path}.{name}" if path else name
        base = name.rsplit("-", 1)[0] if name.endswith(("-input", "-output")) else ""
        if base in v["props"]:
            continue                       # Potential Actions annotation ("query-input" of a SearchAction)
        spec = v["props"].get(name)
        if spec is None:
            run.err(f"{here}: unknown property '{name}'{_suggest(name, v['props'])}")
            continue
        if name in v["superseded"]:
            run.warn(f"{here}: '{name}' is replaced by '{v['superseded'][name]}' on schema.org")
        domains, ranges = spec
        if up and domains and not any(d in up for d in domains):
            run.warn(f"{here}: '{name}' is not a property of {' / '.join(types)} on schema.org")
        for item in (value if isinstance(value, list) else [value]):
            _check_value(name, item, ranges, run, here, depth)


def validate(item: dict) -> Dict[str, List[str]]:
    """{"errors": [...], "warnings": [...]} for one structured_data item (JSON-LD with `values`: full checks;
    other items: the type only)."""
    run = _Run()
    if not available() or not isinstance(item, dict):
        return {"errors": [], "warnings": []}
    values = item.get("values")
    if isinstance(values, dict):
        node = dict(values)
        node.setdefault("@type", item.get("type"))
        _check_node(node, _types_of({"@type": node["@type"]}, run, ""), run, "", 0)
    else:
        _types_of({"@type": item.get("type")}, run, "")
    return {"errors": run.errors, "warnings": run.warnings}
