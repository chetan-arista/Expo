"""
AI tab prompts: ready-made presets (our own wording) and the team's saved prompts.

Saved prompts live in <reports_dir>/ai_prompts.json (reports_dir = env CRAWLER_REPORTS_DIR, else the system temp
folder), shared by everyone using this server. Up to MAX_PROMPTS prompts, one per slot 1-10; slot N is the AI
tab's filter and column "prompt_N".

A prompt: {"id", "slot", "name", "preset", "instruction", "source", "extractor", "output", "labels",
"max_pages", "tab", "filters", "match", "enabled", "updated_at"}
  source     what is sent for each page (SOURCES): the page text (content area), title, meta description, H1,
             a page overview (title + description + H1 + start of the text), the HTML (capped), one Custom
             Extraction value (extractor = its name) or the images without alt text (file names; the AI does
             not see the images)
  output     text | label (one of `labels`, or a short free label when none are set) | yes_no | number
  max_pages  page cap per run (DEFAULT_PAGES, at most MAX_PAGES); pages past it are "Not checked"
  tab, filters, match   optional: only the pages of these tab filters (any / all), in crawl order

Public: PRESETS, presets(), load_prompts(), save_prompts(), validate_prompt(), from_preset(), limits(),
public_view(), prompt_hash(), PromptError.
"""
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

FILE_NAME = "ai_prompts.json"
MAX_PROMPTS = 10
DEFAULT_PAGES = 25
MAX_PAGES = 500
MAX_INSTRUCTION = 2000
MAX_NAME = 60
MAX_LABELS = 20
MAX_LABEL = 40
MAX_FILTERS = 10

SOURCES = ("page_text", "page_overview", "title", "meta_description", "h1", "html", "custom_extraction",
           "images_missing_alt")
SOURCE_LABELS = {
    "page_text": "Page text (main content)", "page_overview": "Title, description, H1 and the start of the text",
    "title": "Page title", "meta_description": "Meta description", "h1": "H1 headings",
    "html": "HTML (first part)", "custom_extraction": "A Custom Extraction value",
    "images_missing_alt": "Images without alt text (file names)"}
OUTPUTS = ("text", "label", "yes_no", "number")
OUTPUT_LABELS = {"text": "Text", "label": "Label", "yes_no": "Yes / No", "number": "Number"}

# Presets in our own words. The AI sees only what the source sends (never the images themselves).
PRESETS: List[Dict[str, Any]] = [
    {"preset": "alt_text", "name": "Alt text suggestion", "source": "images_missing_alt", "output": "text",
     "labels": [],
     "description": "Suggests alt text for images that have none, from the file name and the page topic. The AI "
                    "does not see the images, so check each suggestion before using it.",
     "instruction": "For each image listed, suggest alt text of at most 125 characters that says what the image most "
                    "likely shows, judging from its file name and the page topic. Write one line per image as "
                    "'file name: suggestion'. If the file name gives no clue, write 'file name: unclear'."},
    {"preset": "page_intent", "name": "Page intent", "source": "page_overview", "output": "label",
     "labels": ["informational", "commercial", "transactional", "navigational"],
     "description": "Which kind of search the page answers: learning something, comparing options, buying or "
                    "signing up, or finding a specific place on the site.",
     "instruction": "Decide which search intent this page mainly serves: informational (explains or teaches), "
                    "commercial (helps compare products or services before a decision), transactional (lets the "
                    "visitor buy, book, sign up or contact) or navigational (helps reach a specific page, account "
                    "or brand)."},
    {"preset": "primary_language", "name": "Primary language", "source": "page_text", "output": "label",
     "labels": [],
     "description": "The main language of the page text, to compare with the page's lang attribute and hreflang.",
     "instruction": "Name the main language the page text is written in, as its English name (for example "
                    "'English', 'Spanish', 'Hindi'). If two languages are mixed, name the one used most."},
    {"preset": "sentiment", "name": "Sentiment", "source": "page_text", "output": "label",
     "labels": ["positive", "neutral", "negative", "mixed"],
     "description": "The overall tone of the page text.",
     "instruction": "Judge the overall tone of the page text towards its subject: positive, neutral, negative or "
                    "mixed."},
    {"preset": "title_rewrite", "name": "Title rewrite", "source": "page_overview", "output": "text", "labels": [],
     "description": "A suggested title tag of about 50-60 characters that matches what the page offers.",
     "instruction": "Write a better title tag for this page: at most 60 characters, the main topic first, specific "
                    "to this page, no clickbait and no quotation marks. Keep a brand name at the end only if the "
                    "current title has one."},
    {"preset": "meta_description_rewrite", "name": "Meta description rewrite", "source": "page_overview",
     "output": "text", "labels": [],
     "description": "A suggested meta description of about 140-155 characters.",
     "instruction": "Write a meta description for this page: 120 to 155 characters, one or two plain sentences that "
                    "say what the visitor gets on the page and why it helps, with a natural call to action. No "
                    "quotation marks."},
    {"preset": "summary", "name": "Summary", "source": "page_text", "output": "text", "labels": [],
     "description": "Two plain sentences about what the page covers.",
     "instruction": "Summarise what this page covers in at most two short sentences of plain language."},
]
_PRESETS = {p["preset"]: p for p in PRESETS}
_lock = threading.RLock()


class PromptError(ValueError):
    """A prompt (or the list) cannot be saved; the message says why in plain words."""


def presets() -> List[Dict[str, Any]]:
    return copy.deepcopy(PRESETS)


def limits() -> Dict[str, Any]:
    return {"max_prompts": MAX_PROMPTS, "default_pages": DEFAULT_PAGES, "max_pages": MAX_PAGES,
            "max_instruction": MAX_INSTRUCTION, "max_name": MAX_NAME, "max_labels": MAX_LABELS,
            "sources": [{"id": s, "label": SOURCE_LABELS[s]} for s in SOURCES],
            "outputs": [{"id": o, "label": OUTPUT_LABELS[o]} for o in OUTPUTS]}


def _reports_dir(reports_dir: Optional[str] = None) -> str:
    return reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def _path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(_reports_dir(reports_dir), FILE_NAME)


def prompt_hash(p: Dict[str, Any]) -> str:
    """Changes whenever the answer could change: instruction, source, extractor, output kind or labels."""
    key = json.dumps([p.get("instruction", ""), p.get("source", ""), p.get("extractor", ""), p.get("output", ""),
                      list(p.get("labels") or [])], ensure_ascii=False)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _text(v: Any, limit: int) -> str:
    return " ".join(str(v or "").split())[:limit]


def validate_prompt(p: Any, slot: Optional[int] = None) -> Tuple[Dict[str, Any], List[str]]:
    """(clean prompt, problems in plain words). Fills defaults from the preset it names."""
    errors: List[str] = []
    if not isinstance(p, dict):
        return {}, ["A prompt must be an object."]
    base = _PRESETS.get(str(p.get("preset") or ""), {})
    name = _text(p.get("name") or base.get("name"), MAX_NAME)
    instruction = str(p.get("instruction") or base.get("instruction") or "").strip()
    source = str(p.get("source") or base.get("source") or "page_text")
    output = str(p.get("output") or base.get("output") or "text")
    labels_in = p.get("labels") if p.get("labels") is not None else base.get("labels") or []
    labels: List[str] = []
    for lab in labels_in if isinstance(labels_in, list) else []:
        lab = _text(lab, MAX_LABEL)
        if lab and lab.lower() not in (x.lower() for x in labels):
            labels.append(lab)
    try:
        max_pages = int(p.get("max_pages") if p.get("max_pages") not in (None, "") else DEFAULT_PAGES)
    except (TypeError, ValueError):
        max_pages = DEFAULT_PAGES
        errors.append("The page cap must be a whole number.")
    filters = [str(f).strip() for f in (p.get("filters") or []) if str(f).strip()] \
        if isinstance(p.get("filters"), list) else []
    try:
        slot = int(p.get("slot") or slot or 0)
    except (TypeError, ValueError):
        slot = 0
    if not name:
        errors.append("Give the prompt a name.")
    if not instruction:
        errors.append(f"'{name or 'The prompt'}' needs an instruction.")
    elif len(instruction) > MAX_INSTRUCTION:
        errors.append(f"'{name}': the instruction is longer than {MAX_INSTRUCTION} characters.")
    if source not in SOURCES:
        errors.append(f"'{name}': unknown source '{source}'.")
    if output not in OUTPUTS:
        errors.append(f"'{name}': unknown answer type '{output}'.")
    if len(labels) > MAX_LABELS:
        errors.append(f"'{name}': at most {MAX_LABELS} labels.")
    if not 1 <= max_pages <= MAX_PAGES:
        errors.append(f"'{name}': the page cap must be 1 to {MAX_PAGES}.")
    extractor = _text(p.get("extractor"), 100)
    if source == "custom_extraction" and not extractor:
        errors.append(f"'{name}': choose the Custom Extraction rule whose value is sent.")
    if len(filters) > MAX_FILTERS:
        errors.append(f"'{name}': at most {MAX_FILTERS} filters.")
    if filters and not str(p.get("tab") or "").strip():
        errors.append(f"'{name}': choose the tab of the filters.")
    match = str(p.get("match") or "any").lower()
    clean = {"id": _text(p.get("id"), 40) or "p_" + uuid.uuid4().hex[:10], "slot": slot, "name": name,
             "preset": base.get("preset", ""), "instruction": instruction[:MAX_INSTRUCTION], "source": source,
             "extractor": extractor if source == "custom_extraction" else "", "output": output,
             "labels": labels[:MAX_LABELS] if output == "label" else [], "max_pages": max(1, min(max_pages, MAX_PAGES)),
             "tab": _text(p.get("tab"), 60) if filters else "", "filters": filters[:MAX_FILTERS],
             "match": match if match in ("any", "all") else "any", "enabled": bool(p.get("enabled", True)),
             "updated_at": str(p.get("updated_at") or "")}
    if not re.match(r"^[A-Za-z0-9_-]{1,40}$", clean["id"]):
        clean["id"] = "p_" + uuid.uuid4().hex[:10]
    return clean, errors


def from_preset(preset_id: str, slot: int = 0, **overrides: Any) -> Dict[str, Any]:
    """A new prompt built from a preset (PromptError for an unknown preset)."""
    if preset_id not in _PRESETS:
        raise PromptError(f"Unknown preset '{preset_id}'.")
    clean, errors = validate_prompt(dict({"preset": preset_id, "slot": slot}, **overrides), slot)
    if errors:
        raise PromptError(" ".join(errors))
    return clean


_loaded: Dict[str, Tuple[int, List[Dict[str, Any]]]] = {}    # path -> (mtime_ns, prompts): the AI tab reads them
                                                               # on every rebuild of the tabs


def load_prompts(reports_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """The saved prompts, by slot ([] when none were saved)."""
    path = _path(reports_dir)
    with _lock:
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return []
        hit = _loaded.get(path)
        if hit is not None and hit[0] == mtime:
            return copy.deepcopy(hit[1])
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        items = data.get("prompts") if isinstance(data, dict) else None
        out = []
        for p in items if isinstance(items, list) else []:
            clean, errors = validate_prompt(p)
            if not errors and 1 <= clean["slot"] <= MAX_PROMPTS:
                out.append(clean)
        out.sort(key=lambda p: p["slot"])
        _loaded[path] = (mtime, out)
        return copy.deepcopy(out)


def save_prompts(prompts: Any, reports_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Replace the saved prompts. Slots are kept when given and free, else the first free slot is used.
    PromptError (nothing saved) lists every problem."""
    if not isinstance(prompts, list):
        raise PromptError("Send the prompts as a list.")
    if len(prompts) > MAX_PROMPTS:
        raise PromptError(f"At most {MAX_PROMPTS} prompts (one per AI tab column).")
    cleaned: List[Dict[str, Any]] = []
    problems: List[str] = []
    for p in prompts:
        clean, errors = validate_prompt(p)
        problems += errors
        cleaned.append(clean)
    if problems:
        raise PromptError(" ".join(problems))
    used: set = set()
    for p in cleaned:                              # keep requested slots that are valid and free
        if 1 <= p["slot"] <= MAX_PROMPTS and p["slot"] not in used:
            used.add(p["slot"])
        else:
            p["slot"] = 0
    free = [s for s in range(1, MAX_PROMPTS + 1) if s not in used]
    for p in cleaned:
        if not p["slot"]:
            p["slot"] = free.pop(0)
    ids = set()
    for p in cleaned:
        while p["id"] in ids:
            p["id"] = "p_" + uuid.uuid4().hex[:10]
        ids.add(p["id"])
    previous = {p["id"]: p for p in load_prompts(reports_dir)}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for p in cleaned:
        old = previous.get(p["id"])
        p["updated_at"] = old["updated_at"] if old and prompt_hash(old) == prompt_hash(p) and \
            old.get("updated_at") else now
    cleaned.sort(key=lambda p: p["slot"])
    path = _path(reports_dir)
    with _lock:
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".ai_prompts.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "prompts": cleaned}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        _loaded.pop(path, None)
    return copy.deepcopy(cleaned)


def public_view(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """GET /api/ai/prompts: {"presets", "prompts", "limits"} (nothing secret is stored here)."""
    return {"presets": presets(), "prompts": load_prompts(reports_dir), "limits": limits()}
