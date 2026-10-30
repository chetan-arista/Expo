"""
AI tab: the answers of the team's AI prompts per page (crawler/ai/prompts.py, run by crawler/ai/runner.py on the
free AI). Rows are the internal HTML pages that answered 2xx (the pages prompts can target). One column and one
filter per prompt, ids prompt_1 .. prompt_10 by the prompt's slot; a filter lists the pages with an answer.

Values come from site.integrations["ai"] (saved with the crawl). A cell is the answer, "Not checked: <why>" for a
page the prompt targeted but did not answer (over its page cap, quota, stopped...), or empty for a page the prompt
does not target. A prompt with no answer at all is "Not checked" with the reason: AI off, a signed-in crawl and a
free cloud AI, not run for this crawl yet, or why every page was skipped. These are plain filters, not issues.
A prompt edited after its run keeps showing what was asked then (the details say it changed).
"""
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ..registry import Column, Filter, Tab, register, register_details
from .internal import ADDRESS, INDEXABILITY, category_of, is_html_doc

SLOTS = 10
TAB_ID = "ai"


def ai_rows(site) -> List[Any]:
    from ...ai.runner import content_pages
    return content_pages(site)


def entry(site) -> Dict[str, Any]:
    e = (getattr(site, "integrations", None) or {}).get("ai")
    return e if isinstance(e, dict) else {}


def _saved_prompts() -> List[Dict[str, Any]]:
    try:
        from ...ai import prompts
        return prompts.load_prompts()
    except Exception:
        return []


def prompts_for(site) -> Dict[int, Dict[str, Any]]:
    """slot -> the prompt shown in that column: the one that ran for this crawl ("ran": True, its snapshot), else
    the saved prompt that has not run yet ("ran": False)."""
    out: Dict[int, Dict[str, Any]] = {}
    for snap in (entry(site).get("prompts") or {}).values():
        if isinstance(snap, dict) and 1 <= int(snap.get("slot") or 0) <= SLOTS:
            out[int(snap["slot"])] = dict(snap, ran=True)
    for p in _saved_prompts():
        slot = int(p.get("slot") or 0)
        if 1 <= slot <= SLOTS and slot not in out:
            out[slot] = dict(p, ran=False)
    return dict(sorted(out.items()))


def answer(site, url: str, prompt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The stored record of one page for a prompt that ran (None when the prompt does not target the page)."""
    if not prompt.get("ran"):
        return None
    rec = ((entry(site).get("results") or {}).get(url) or {}).get(prompt["id"])
    return rec if isinstance(rec, dict) else None


def _display(value: Any, output: str) -> Any:
    if output == "yes_no":
        return {"yes": "Yes", "no": "No"}.get(str(value), value)
    return value


def cell(site, r, prompt: Dict[str, Any]) -> Any:
    rec = answer(site, r.url, prompt)
    if rec is None:
        return ""
    if "value" in rec:
        return _display(rec["value"], prompt.get("output", "text"))
    return "Not checked: " + str(rec.get("not_checked") or "")


def _ai_off_reason() -> str:
    try:
        from ...ai import client
        st = client.ai_status()
        return "" if st.get("enabled") else str(st.get("reason") or client.AI_OFF_NOTE)
    except Exception:
        return "AI off - the AI client could not be loaded."


def prompt_reason(site, prompt: Optional[Dict[str, Any]]) -> str:
    """Why a prompt's filter has no answers ("" when it has some)."""
    if prompt is None:
        return "No AI prompt is set for this column: add one in the AI tab."
    if not prompt.get("ran"):
        off = _ai_off_reason()
        if off:
            return off
        try:
            from ...ai.runner import private_refused
            refused = private_refused(site)
        except Exception:
            refused = ""
        if refused:
            return refused
        return "Not run for this crawl yet: press Run in the AI tab."
    if prompt.get("reason"):
        return str(prompt["reason"])
    results = entry(site).get("results") or {}
    recs = [per.get(prompt["id"]) for per in results.values() if isinstance(per, dict)]
    if any(isinstance(rec, dict) and "value" in rec for rec in recs):
        return ""
    reasons = [str(rec.get("not_checked")) for rec in recs if isinstance(rec, dict) and rec.get("not_checked")]
    if reasons:
        return "No page was answered: " + Counter(reasons).most_common(1)[0][0]
    return "No page matched this prompt's pages (its tab filters chose none)."


def _test(prompt: Dict[str, Any]):
    return lambda r, site: (lambda rec: rec is not None and "value" in rec)(answer(site, r.url, prompt))


def _dynamic(site) -> Tuple[List[Column], List[Filter]]:
    cols: List[Column] = [ADDRESS]
    filters: List[Filter] = []
    for slot, p in prompts_for(site).items():
        fid = f"prompt_{slot}"
        name = str(p.get("name") or f"Prompt {slot}")
        cols.append(Column(fid, name, lambda r, site, p=p: cell(site, r, p)))
        filters.append(Filter(fid, name, _test(p),
                              description=f"Pages answered by the AI prompt \"{name}\" "
                                          f"({p.get('output', 'text')} answers). Pages it targets but did not "
                                          "answer show Not checked with the reason in its column."))
    cols.append(INDEXABILITY)
    return cols, filters


def _skip_reason(site, filter_id: str) -> str:
    if not filter_id.startswith("prompt_"):
        return ""
    try:
        slot = int(filter_id.split("_", 1)[1])
    except ValueError:
        return ""
    return prompt_reason(site, prompts_for(site).get(slot))


def details(site, url: str) -> Optional[Dict[str, Any]]:
    """The AI section of the URL details: one entry per prompt (answered / Not checked / not targeted / not run);
    None for a URL that is not a 2xx HTML page or when no prompt exists."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not is_html_doc(r) or category_of(r) != "2xx":
        return None
    shown = prompts_for(site)
    if not shown:
        from ..registry import v2_hidden
        if v2_hidden("tabs", "ai"):
            return None                  # ARACHNIX 1.0: the AI tab is hidden, so is its section
        return {"not_checked": "No AI prompt is set up for this crawl (AI tab > Prompts)."}
    saved = {p["id"]: p for p in _saved_prompts()}
    out = []
    for slot, p in shown.items():
        item = {"slot": slot, "id": p.get("id"), "name": p.get("name"), "output": p.get("output")}
        if not p.get("ran"):
            item.update(state="not_run", reason=prompt_reason(site, p))
        else:
            rec = answer(site, url, p)
            if rec is None:
                item.update(state="not_targeted", reason="This page is not one of the pages the prompt targets.")
            elif "value" in rec:
                item.update(state="answered", value=_display(rec["value"], p.get("output", "text")),
                            at=rec.get("at"), model=rec.get("model", ""), cached=bool(rec.get("cached")))
            else:
                item.update(state="not_checked", reason=rec.get("not_checked"), at=rec.get("at"))
            cur = saved.get(p.get("id"))
            if cur is not None:
                try:
                    from ...ai.prompts import prompt_hash
                    if prompt_hash(cur) != p.get("hash"):
                        item["changed"] = "The prompt was edited after this run: run it again for new answers."
                except Exception:
                    pass
        out.append(item)
    return {"prompts": out}


STATIC_FILTERS = [Filter(f"prompt_{i}", f"Prompt {i}", lambda r, site: False) for i in range(1, SLOTS + 1)]

register(Tab(id=TAB_ID, name="AI", order=300, columns=[ADDRESS, INDEXABILITY], filters=STATIC_FILTERS, rows=ai_rows,
             dynamic=_dynamic, skip_reason=_skip_reason,
             description="Answers of the team's AI prompts for each page (free AI; capped pages per prompt)."))
register_details("ai", details)
