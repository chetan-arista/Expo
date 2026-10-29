"""
Content tab (Screaming Frog "Content"): word count, readability and duplicate checks for every
internal page that loaded (2xx). Like Screaming Frog, PDFs that loaded are listed too (they have no
measured text, so no content filter can flag them).

How the counts match Screaming Frog (checked against a Screaming Frog 24.3 crawl in research/):
  - Every filter runs on indexable pages only (Screaming Frog's default "Ignore Non-Indexable URLs
    for On-Page Filters"): the noindex tag archives under 200 words are not Low Content Pages.
  - Readability Difficult = Flesch reading ease 30 to under 50 (the "Hard" band), Very Difficult =
    under 30 ("Very Hard"); the two never overlap.
  - Word Count (extract.content_lines/text_stats): the <body> text without nav and footer, with inline
    elements joined the way a browser shows them ("<b>Hello</b>world" is one word), split at spaces,
    plus one word per list item (Screaming Frog counts the list marker). Exact on 162 of 180 pages
    of the reference crawl. Flesch uses the standard formula (206.835 - 1.015 x words per sentence -
    84.6 x syllables per word); Screaming Frog's own sentence splitter (it also breaks at some commas)
    and syllable counter are not public, so Sentence Count and the score are close, not exact.
  - Exact Duplicates use the content-area MD5 (site.duplicates["content_hash"]); Near Duplicates are
    pages whose content-area word 3-shingles overlap by >= 90 % (Jaccard similarity estimated with
    minhash like Screaming Frog, analysis.near_duplicate_index; exact duplicates left out). The
    Closest Near Duplicate Match is that estimated overlap in %; No. Near Duplicates is the exact number
    (site.near_duplicate_counts), while each page lists its NEAR_DUP_MAX_PER_URL closest matches. Crawls
    saved before pages carried a minhash signature fall back to their simhash
    (site.near_duplicate_method == "simhash").
  - Soft 404 is stricter than Screaming Frog on purpose: a page is only flagged when its title or h1
    reads like an error message, or when a very short page says "page not found" in its text. A
    service page called "404 Cleanup" is not an error page (Screaming Frog flags it).

  - Semantically Similar / Low Relevance Content compare pages by meaning when the crawl has an embeddings run
    (crawler/ai/embeddings.py: the AI turns each indexable page's main text into a vector; derived results in
    site.integrations["embeddings"]): Semantically Similar = the closest other page scores at least 0.92
    (cosine); Low Relevance = the page's closeness to the rest of the site is far below the typical page's.
    Pages the run did not embed (over its page limit, quota...), and every page of a crawl without a run, use
    the word-based approximation instead: each indexable page becomes a TF-IDF vector of its top content words
    (PageData top_terms) compared by cosine similarity (analysis.compute_semantic; similar at 0.80, low
    relevance below a quarter of the site's median similarity), worked out once the crawl has finished (Crawl
    Analysis step "Semantic Similarity"). The Similarity Method column says which one each page used.
  - Topic Cluster: the page's group of pages about the same topic (crawler/seo/semantic.py: k-means on the
    embeddings when there is a run, else on the TF-IDF vectors), named by its most typical words.

  - Low Content Pages uses the crawl's Preferences threshold (registry.pref "low_content_words", default 200);
    the filter's name shows a changed number. Soft 404 also matches the team's own phrases
    (CrawlConfig.soft_404_phrases) in the title and H1.

  - Spelling Errors (crawler/seo/spelling.py, offline): indexable pages whose main text has words that look
    misspelt, counted through spelling.site_errors (site dictionary of learned brand words and the team's words,
    50 errors per page at most), so the filter, the Spelling Errors column, the export and the URL details agree.
    Not checked when the spelling check was off or could not run (reason from the pages: no dictionary for the
    page language ...).
  - Grammar Errors: from the LanguageTool connector (site.integrations["languagetool"]: {"results": {url:
    {"matches", "count"}}, "not_checked": {url: reason}}); "Not connected" without it, never 0. Pages LanguageTool
    did not check (past its page cap, a signed-in crawl) show "Not checked" in the Grammar Checked column.
"""
import re
from collections import Counter
from typing import Any, Dict, List, Optional

from .. import semantic, spelling
from ..analysis import SEMANTIC_MIN_TERMS, SEMANTIC_SIMILAR_MIN
from ..registry import (Column, Filter, Tab, is_indexable, pref, register, register_details, register_requirement,
                        requirement_reason, seo, unmet)
from .internal import (ADDRESS, INDEXABILITY, INDEXABILITY_STATUS, category_of, content_kind, internal_rows,
                       is_html_doc)
from .page_titles import INDEXABLE_NOTE, content_rows, h1_of, text_list

LOW_CONTENT_WORDS = 200          # Screaming Frog's default "Low Content Word Count" (Preferences can change it)
READABILITY_DIFFICULT = 50       # Flesch reading ease below this is difficult ...
READABILITY_VERY_DIFFICULT = 30  # ... and below this very difficult
SOFT_404_MAX_WORDS = 150         # a page this short that says "page not found" is treated as an error page


def content_tab_rows(site) -> List[Any]:
    """Internal HTML pages and PDFs that answered 2xx, in crawl order."""
    html = {id(r) for r in content_rows(site)}
    return [r for r in internal_rows(site)
            if id(r) in html or (content_kind(r) == "pdf" and category_of(r) == "2xx")]


def _html(fn):
    """Cell only for HTML pages (PDFs show blank, not 0)."""
    return lambda r, site: fn(r, site) if is_html_doc(r) else ""


def word_count(r) -> int:
    return int(seo(r).get("word_count") or 0)


def flesch(r):
    v = seo(r).get("flesch_reading_ease")
    return None if v is None else float(v)


# ---------------------------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------------------------
def content_hash(r) -> str:
    return (seo(r).get("content_hash") or "").strip()


def exact_duplicate_urls(site, r) -> List[str]:
    """Other indexable pages with exactly the same content-area text."""
    h = content_hash(r)
    if not h:
        return []
    return [u for u in (site.duplicates or {}).get("content_hash", {}).get(h, []) if u != r.url]


def near_duplicates_of(site, r) -> list:
    """The page's closest near duplicates [(url, pct)], closest first (at most NEAR_DUP_MAX_PER_URL)."""
    return list((site.near_duplicates or {}).get(r.url) or [])


def near_duplicate_count(site, r) -> int:
    """How many indexable pages are near duplicates of this one (exact, not capped like the list)."""
    n = (getattr(site, "near_duplicate_counts", None) or {}).get(r.url)
    return int(n) if n is not None else len(near_duplicates_of(site, r))


def closest_near_duplicate(site, r) -> Any:
    near = near_duplicates_of(site, r)
    return max((pct for _, pct in near), default="") if near else ""


EMBED_SIMILAR_MIN = 0.92       # embeddings: closest page at or above this cosine (the run stores what it used)
METHOD_EMBEDDINGS = "Embeddings"
METHOD_WORDS = "Words"


def embedding_result(site, url: str) -> Optional[Dict[str, Any]]:
    """The embeddings run's derived result of one page (None when the crawl has no run or the page was not
    embedded)."""
    rec = ((semantic.embeddings_entry(site) or {}).get("results") or {}).get(url)
    return rec if isinstance(rec, dict) else None


def embed_similar_min(site) -> float:
    try:
        return float((semantic.embeddings_entry(site) or {}).get("similar_threshold") or EMBED_SIMILAR_MIN)
    except (TypeError, ValueError):
        return EMBED_SIMILAR_MIN


def semantic_match(site, r):
    """(closest semantically similar page, cosine) or None: by embeddings when the page has a result of the
    crawl's embeddings run, else by words (TF-IDF)."""
    rec = embedding_result(site, r.url)
    if rec is not None:
        return (rec["closest"], rec["score"]) if rec.get("closest") and rec.get("score") is not None else None
    return (getattr(site, "semantic", None) or {}).get(r.url)


def _semantically_similar(r, site) -> bool:
    m = semantic_match(site, r)
    if m is None:
        return False
    return m[1] >= (embed_similar_min(site) if embedding_result(site, r.url) is not None else SEMANTIC_SIMILAR_MIN)


def _low_relevance(r, site) -> bool:
    rec = embedding_result(site, r.url)
    if rec is not None:
        return bool(rec.get("low"))
    return r.url in (getattr(site, "low_relevance", None) or {})


def _relevance_score(r, site):
    rec = embedding_result(site, r.url)
    if rec is not None:
        return "" if rec.get("relevance") is None else rec["relevance"]
    low = getattr(site, "low_relevance", None) or {}
    return low.get(r.url, "")


WORDS_TOO_LITTLE = (f"Not checked: too little text to compare (fewer than {SEMANTIC_MIN_TERMS} different content "
                    "words)")


def semantic_method(r, site) -> str:
    """How Semantically Similar / Low Relevance judged an indexable page: "Embeddings", "Words" (with why the
    embeddings run left it out, when it did), or "Not checked: <why>" when neither compared it ("" for other
    rows)."""
    if not is_html_doc(r) or category_of(r) != "2xx" or not is_indexable(site, r):
        return ""
    if embedding_result(site, r.url) is not None:
        return METHOD_EMBEDDINGS
    entry = (getattr(site, "integrations", None) or {}).get("embeddings")
    why = ((entry or {}).get("not_checked") or {}).get(r.url) if isinstance(entry, dict) else None
    if not why and isinstance(entry, dict) and not entry.get("results") and entry.get("reason"):
        why = entry["reason"]
    words_why = requirement_reason("analysis:semantic_similarity", site, ())
    if words_why:
        return "Not checked: " + words_why + (f" (embeddings: {why})" if why else "")
    if len(seo(r).get("top_terms") or []) < SEMANTIC_MIN_TERMS:
        return WORDS_TOO_LITTLE + (f" (embeddings: {why})" if why else "")
    return f"{METHOD_WORDS} (embeddings Not checked: {why})" if why else METHOD_WORDS


def semantic_reason(site, arg: str = "", rows=()) -> str:
    """"" when pages can be compared (an embeddings run, or the Crawl Analysis step ran), else why not
    (registered as the requirement kind "semantic")."""
    if semantic.embeddings_entry(site) is not None:
        return ""
    return requirement_reason("analysis:semantic_similarity", site, rows)


register_requirement("semantic", semantic_reason)
SEMANTIC_REQUIRES = ("semantic:similarity",)


# ---------------------------------------------------------------------------------------------
# Soft 404
# ---------------------------------------------------------------------------------------------
# A heading (title segment or h1) that reads like an error message
_ERROR_PHRASE = re.compile(
    r"\bpage\s+not\s+found\b"
    r"|\b404\s*[-:–|]?\s*(?:error\b(?!s)|not\s+found\b)"
    r"|\berror\s*(?:code\s*)?[-:]?\s*404\b"
    r"|\b(?:page|content|post|url)\s+(?:you\s+(?:are|were|['’]re)\s+looking\s+for\s+)?"
    r"(?:doesn['’]?t|does\s+not|no\s+longer)\s+exists?\b"
    r"|\b(?:can['’]?t|cannot|could\s+not|couldn['’]?t)\s+(?:be\s+)?found\b"
    r"|\bnothing\s+(?:was\s+)?found\b"
)
# A whole heading segment that is only an error label ("404", "Error 404", "Not Found", "Oops!")
_ERROR_ONLY = re.compile(
    r"^(?:(?:error\s*)?404(?:\s*(?:error|page))?|(?:oops[!.,]*\s*)?(?:page\s+)?not\s+found|oops)[!.]*$"
)
# Words that show the heading is ABOUT errors (a guide or a service), not an error page itself
_TOPIC_WORDS = re.compile(
    r"\b(?:how\s+to|fix(?:es|ed|ing)?|clean\s*-?\s*ups?|guide|services?|redirects?|monitor(?:ing)?|"
    r"checker|audit|tips|errors|solutions?|tutorial)\b"
)
_SEGMENT_SPLIT = re.compile(r"\s*[|·»]\s*|\s+[-–—]\s+|:\s+")


def heading_is_error(text: str) -> bool:
    """True when a title or h1 is phrased as an error message, e.g. "Page not found | Acme" or
    "Oops! That page can't be found." but not "404 Cleanup & Categorization Structure"."""
    for seg in _SEGMENT_SPLIT.split((text or "").strip().lower()):
        seg = seg.strip()
        if not seg or _TOPIC_WORDS.search(seg):
            continue
        if _ERROR_ONLY.match(seg) or _ERROR_PHRASE.search(seg):
            return True
    return False


def user_soft_404_phrases(site) -> List[str]:
    """The team's own soft 404 phrases for this crawl (CrawlConfig.soft_404_phrases), lower case."""
    phrases = getattr(getattr(site, "config", None), "soft_404_phrases", None) or []
    return [p for p in (" ".join(str(x).lower().split()) for x in phrases if isinstance(x, str)) if p]


def soft_404_reasons(r, site=None) -> List[str]:
    """Why a 2xx page looks like an error page (empty when it does not). With `site`, the team's own phrases
    (Configuration > Preferences) count in the title and H1 too."""
    if not is_html_doc(r) or category_of(r) != "2xx":
        return []
    phrases = user_soft_404_phrases(site) if site is not None else []

    def says_error(text: str) -> bool:
        low = " ".join(text.lower().split())
        return heading_is_error(text) or any(p in low for p in phrases)
    reasons = [f'Title reads like an error: "{t}"' for t in text_list(r, "titles") if says_error(t)]
    reasons += [f'H1 reads like an error: "{h}"' for h in h1_of(r) if says_error(h)]
    if not reasons and word_count(r) < SOFT_404_MAX_WORDS:
        body = [s for s in (seo(r).get("soft_404_signals") or []) if str(s).startswith("Page text")]
        if body:
            reasons.append(f"Very little text ({word_count(r)} words) and {body[0][0].lower()}{body[0][1:]}")
    return reasons


# ---------------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------------
def low_content_words(site) -> int:
    """The Low Content word threshold of this crawl (Preferences), else LOW_CONTENT_WORDS."""
    try:
        return int(pref(site, "low_content_words"))
    except (TypeError, ValueError):
        return LOW_CONTENT_WORDS


def _low_content(r, site) -> bool:
    return is_html_doc(r) and word_count(r) < low_content_words(site)


def _low_content_label(site) -> str:
    n = low_content_words(site)
    return "Low Content Pages" if n == LOW_CONTENT_WORDS else f"Low Content Pages (under {n:,} words)"


# ---------------------------------------------------------------------------------------------
# Spelling (crawler/seo/spelling.py) and grammar (LanguageTool connector)
# ---------------------------------------------------------------------------------------------
SPELLING_REQUIRES = ("config:spelling", "pagedata:spelling")
GRAMMAR_REQUIRES = ("grammar:languagetool",)
GRAMMAR_NOT_CONNECTED = "Not connected - connect LanguageTool under Connections"
GRAMMAR_DETAIL_MAX = 50          # grammar matches listed per URL in the details


def spelling_count(site, r) -> Optional[int]:
    """Spelling errors of a page after the site dictionary (None when its spelling was not checked)."""
    e = spelling.site_errors(site, r.url)
    return e["error_count"] if e["checked"] else None


def _spelling_checked(r) -> bool:
    sp = seo(r).get("spelling")
    return isinstance(sp, dict) and bool(sp.get("checked"))


def _spelling_errors(r, site) -> bool:
    return is_html_doc(r) and (spelling_count(site, r) or 0) > 0


def language_checked(r, site) -> str:
    """The spelling language of a page ("en", "en (assumed)") or "Not checked: <why>" ("" for files)."""
    if not is_html_doc(r) or category_of(r) != "2xx":
        return ""
    sp = seo(r).get("spelling")
    if not isinstance(sp, dict):
        return "Not checked: " + spelling.not_checked_reason(site, r)
    if not sp.get("checked"):
        return "Not checked: " + str(sp.get("reason") or "the spelling check did not run")
    lang = str(sp.get("lang") or "")
    return lang + (" (assumed: the page declares no language)" if not str(sp.get("declared") or "").strip() else "")


def languagetool_entry(site) -> Optional[Dict[str, Any]]:
    """site.integrations["languagetool"] (the 10.3 entry shape) or None."""
    e = (getattr(site, "integrations", None) or {}).get("languagetool")
    return e if isinstance(e, dict) else None


def _languagetool_live() -> Optional[bool]:
    """Whether the LanguageTool connector is connected right now (None when the connectors are not there)."""
    try:
        from ...connectors import registry as connectors
    except Exception:
        return None
    try:
        c = connectors.get("languagetool")
        if c is None:
            return None
        return getattr(c.status(), "state", "") == "connected"
    except Exception:
        return None


def grammar_reason(site, arg: str = "", rows=()) -> str:
    """"" when LanguageTool checked pages of this crawl, else why Grammar Errors is Not connected / Not checked
    (registered as the requirement kind "grammar")."""
    e = languagetool_entry(site)
    if e:
        status = str(e.get("status") or "")
        if status == "running":
            return "LanguageTool is checking the pages now; Grammar Errors fills in when it is done."
        if e.get("results"):
            return ""
        if e.get("connected") is False:
            return str(e.get("reason") or GRAMMAR_NOT_CONNECTED)
        skipped = [str(v) for v in (e.get("not_checked") or {}).values() if v]
        if skipped:
            top = Counter(skipped).most_common(1)[0][0]
            return f"LanguageTool could not check any page of this crawl: {top}"
        if status == "error":
            return str(e.get("reason") or "The last LanguageTool check failed.")
        if status:
            return "LanguageTool ran for this crawl but had no page to check."
    if _languagetool_live():
        return ("LanguageTool is connected, but grammar was not checked for this crawl yet: run Check grammar in the "
                "Content tab.")
    return GRAMMAR_NOT_CONNECTED


def grammar_result(site, url: str) -> Optional[Dict[str, Any]]:
    """{"matches", "count"} LanguageTool gave for one URL (None when it did not check it)."""
    res = ((languagetool_entry(site) or {}).get("results") or {}).get(url)
    return res if isinstance(res, dict) else None


def grammar_count(site, r) -> Optional[int]:
    res = grammar_result(site, r.url)
    if res is None:
        return None
    try:
        return int(res.get("count") if res.get("count") is not None else len(res.get("matches") or []))
    except (TypeError, ValueError):
        return len(res.get("matches") or [])


def grammar_skip_reason(site, url: str) -> str:
    """Why LanguageTool did not check this URL ("" when it did)."""
    if grammar_result(site, url) is not None:
        return ""
    why = grammar_reason(site)
    if why:
        return why
    nc = ((languagetool_entry(site) or {}).get("not_checked") or {}).get(url)
    return str(nc) if nc else "This page was not part of the LanguageTool check."


def grammar_checked(r, site) -> str:
    """"Checked", "Not connected - ..." or "Not checked: <why>" ("" for files)."""
    if not is_html_doc(r) or category_of(r) != "2xx":
        return ""
    why = grammar_skip_reason(site, r.url)
    if not why:
        return "Checked"
    return why if why.startswith("Not connected") else "Not checked: " + why


def _grammar_errors(r, site) -> bool:
    return is_html_doc(r) and (grammar_count(site, r) or 0) > 0


register_requirement("grammar", grammar_reason)


def _readability_difficult(r, site) -> bool:
    f = flesch(r)
    return is_html_doc(r) and f is not None and READABILITY_VERY_DIFFICULT <= f < READABILITY_DIFFICULT


def _readability_very_difficult(r, site) -> bool:
    f = flesch(r)
    return is_html_doc(r) and f is not None and f < READABILITY_VERY_DIFFICULT


FILTERS = [
    Filter("exact_duplicates", "Exact Duplicates", lambda r, site: bool(exact_duplicate_urls(site, r)),
           issue_type="Issue", priority="High", only_indexable=True,
           description="The main text of the page is word-for-word the same as on at least one other "
                       "indexable page. Search engines pick one version to show and may ignore the rest, "
                       "and the pages compete with each other." + INDEXABLE_NOTE,
           fix="Keep one page per piece of content. Remove or merge the copies, redirect them (301) to the "
               "page you keep, or point their canonical tag at it."),
    Filter("near_duplicates", "Near Duplicates", lambda r, site: bool(near_duplicates_of(site, r)),
           issue_type="Warning", priority="Medium", only_indexable=True, requires=("analysis:near_duplicates",),
           description="The main text of the page is at least 90% the same as another indexable page "
                       "(for example template pages where only a word or two changes). Such pages add "
                       "little on their own and can be seen as thin or duplicate." + INDEXABLE_NOTE,
           fix="Open the pages listed as near duplicates and either make each one clearly different and "
               "useful, or combine them into one page and redirect or canonicalise the others."),
    Filter("semantically_similar", "Semantically Similar", _semantically_similar,
           issue_type="Warning", priority="Low", only_indexable=True, requires=SEMANTIC_REQUIRES,
           description=f"The page covers much the same topic as another indexable page. After an embeddings run "
                       f"(AI, Content tab) pages are compared by meaning, and the closest page scores "
                       f"{EMBED_SIMILAR_MIN:.2f} or more (0 to 1); otherwise by their main topic words (TF-IDF), "
                       f"scoring {SEMANTIC_SIMILAR_MIN:.2f} or more. The Similarity Method column says which one "
                       "was used. Pages that target the same thing compete with each other in search "
                       "(cannibalisation)." + INDEXABLE_NOTE,
           fix="Compare the page with its Closest Semantically Similar Address: merge them and redirect one, "
               "or give each a clearly different focus (different queries, audience or intent).",
           short_fix="Merge with the similar page or give each a distinct topic"),
    Filter("low_relevance", "Low Relevance Content", _low_relevance,
           issue_type="Opportunity", priority="Low", only_indexable=True, requires=SEMANTIC_REQUIRES,
           description="The page has little in common with the rest of the site, far less than a typical page: by "
                       "meaning after an embeddings run (AI, Content tab), otherwise by its main topic words "
                       "(TF-IDF; the Similarity Method column says which). It may be off-topic, thin, or a page "
                       "that does not belong. It needs at least 10 comparable pages." + INDEXABLE_NOTE,
           fix="Check that the page belongs on the site and serves its audience; tie it into the site's topic, "
               "move it, or remove it if it is off-topic.",
           short_fix="Check the page fits the site's topic; rework or remove"),
    Filter("low_content_pages", "Low Content Pages", _low_content,
           issue_type="Opportunity", priority="Medium", only_indexable=True, label=_low_content_label,
           description=f"The page has fewer than {LOW_CONTENT_WORDS} words in its main content (menus and "
                       "footer are not counted; the number can be changed under Configuration > Preferences, and "
                       "the filter's name then shows it). There is no official minimum, but very short pages give "
                       "visitors and search engines little to understand. Use this as a rough pointer: "
                       "contact or product pages can be short on purpose." + INDEXABLE_NOTE,
           fix="Where it helps the visitor, add useful text that explains what the page offers, answers "
               "common questions or gives more detail."),
    Filter("soft_404_pages", "Soft 404 Pages", lambda r, site: bool(soft_404_reasons(r, site)),
           issue_type="Warning", priority="High", only_indexable=True,
           description="The page answers 200 OK, but it looks like an error page: its title or main heading "
                       "reads like \"Page not found\" / \"404\", or a very short page says so in its text. "
                       "Pages that merely talk about 404 errors (a guide or a service) are not counted." +
                       INDEXABLE_NOTE,
           fix="If the page really is an error page, make the server return a 404 (or 410) status for it, "
               "and update or redirect the links that point to it. If it is a real page, reword the title "
               "or heading so it does not read like an error."),
    Filter("readability_difficult", "Readability Difficult", _readability_difficult,
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description=f"The text scores {READABILITY_VERY_DIFFICULT} to {READABILITY_DIFFICULT - 1} on the "
                       "Flesch reading ease scale, which means it is hard to read (roughly college level). "
                       "Long sentences and long words lower the score." + INDEXABLE_NOTE,
           fix="If the page is meant for a general audience, use shorter sentences, simpler words, and "
               "break long paragraphs into lists or sub-headings."),
    Filter("readability_very_difficult", "Readability Very Difficult", _readability_very_difficult,
           issue_type="Opportunity", priority="Low", only_indexable=True,
           description=f"The text scores below {READABILITY_VERY_DIFFICULT} on the Flesch reading ease "
                       "scale, which means it is very hard to read (roughly graduate level)." + INDEXABLE_NOTE,
           fix="Rewrite the hardest passages: split long sentences, swap jargon for everyday words and "
               "explain terms the reader may not know."),
    Filter("lorem_ipsum_placeholder", "Lorem Ipsum Placeholder",
           lambda r, site: is_html_doc(r) and bool(seo(r).get("lorem_ipsum")),
           issue_type="Warning", priority="High", only_indexable=True,
           description="The page still contains \"Lorem ipsum\" dummy text, which usually means a template "
                       "or draft section went live by mistake." + INDEXABLE_NOTE,
           fix="Replace the placeholder text with real content, or remove the section."),
    Filter("spelling_errors", "Spelling Errors", _spelling_errors,
           issue_type="Issue", priority="Low", only_indexable=True, requires=SPELLING_REQUIRES,
           of_rows=_spelling_checked,
           description="The main text of the page has words that look misspelt: each is one letter away from a "
                       "known word (a letter missing, added or changed, or two letters swapped), checked offline "
                       "in the page's language. Names, brand and product words found across the site, and words "
                       "the team added to the site dictionary are not counted; at most 50 words are listed per "
                       "page. Typos make a page look careless to visitors." + INDEXABLE_NOTE,
           fix="Open the page's Spelling details, correct the words in the CMS, and add real brand or product "
               "names to the site dictionary so they are not flagged again.",
           short_fix="Correct the typos; add brand words to the site dictionary"),
    Filter("grammar_errors", "Grammar Errors", _grammar_errors,
           issue_type="Issue", priority="Low", only_indexable=True, requires=GRAMMAR_REQUIRES,
           description="LanguageTool found grammar or style problems in the main text of the page, such as "
                       "words that do not agree, a missing or repeated word, or wrong punctuation. Needs the "
                       "LanguageTool connector (Connections); pages it did not check are shown as Not checked."
                       + INDEXABLE_NOTE,
           fix="Open the page's Grammar details, read each message in its context and correct the text in the "
               "CMS where the suggestion is right.",
           short_fix="Fix the grammar problems LanguageTool lists"),
]

COLUMNS = [
    ADDRESS,
    Column("word_count", "Word Count", _html(lambda r, site: word_count(r)), "num"),
    Column("sentence_count", "Sentence Count", _html(lambda r, site: int(seo(r).get("sentence_count") or 0)), "num"),
    Column("avg_words_per_sentence", "Average Words Per Sentence",
           _html(lambda r, site: round(float(seo(r).get("avg_words_per_sentence") or 0.0), 3)), "num"),
    Column("flesch_reading_ease", "Flesch Reading Ease Score",
           lambda r, site: "" if flesch(r) is None else round(flesch(r), 3), "num"),
    Column("readability", "Readability", lambda r, site: seo(r).get("readability") or ""),
    Column("closest_near_duplicate", "Closest Near Duplicate Match",
           lambda r, site: closest_near_duplicate(site, r), "num"),
    Column("near_duplicates", "No. Near Duplicates",
           lambda r, site: near_duplicate_count(site, r) or "", "num"),
    Column("closest_semantic", "Closest Semantically Similar Address",
           lambda r, site: (semantic_match(site, r) or ("", ""))[0], "url"),
    Column("semantic_score", "Semantic Similarity Score",
           lambda r, site: (semantic_match(site, r) or ("", ""))[1], "num"),
    Column("relevance_score", "Semantic Relevance Score", _relevance_score, "num"),
    Column("semantic_method", "Similarity Method", semantic_method),
    Column("topic_cluster", "Topic Cluster", lambda r, site: semantic.topic_cluster(site, r)),
    Column("text_ratio", "Text Ratio", _html(lambda r, site: round(float(seo(r).get("text_ratio") or 0.0), 3)), "num"),
    Column("language", "Language", lambda r, site: seo(r).get("language") or ""),
    Column("spelling_errors", "Spelling Errors",
           lambda r, site: (lambda n: "" if n is None else n)(spelling_count(site, r)) if is_html_doc(r) else "",
           "num"),
    Column("language_checked", "Language Checked", language_checked),
    Column("grammar_errors", "Grammar Errors",
           lambda r, site: (lambda n: "" if n is None else n)(grammar_count(site, r)) if is_html_doc(r) else "",
           "num"),
    Column("grammar_checked", "Grammar Checked", grammar_checked),
    Column("hash", "Hash", lambda r, site: content_hash(r)),
    INDEXABILITY, INDEXABILITY_STATUS,
]

register(Tab(id="content", name="Content", order=110, columns=COLUMNS, filters=FILTERS, rows=content_tab_rows))


# ---------------------------------------------------------------------------------------------
# Spelling / grammar outputs: URL details ("grammar"; "spelling" is spelling.details), right-panel summary,
# bulk exports. Each lists exactly the pages the filters count.
# ---------------------------------------------------------------------------------------------
def _counted_pages(site, test) -> List[Any]:
    """The pages a filter of this tab counts (indexable HTML pages of the tab passing `test`)."""
    out = []
    for r in content_tab_rows(site):
        if not is_indexable(site, r):
            continue
        try:
            if test(r, site):
                out.append(r)
        except Exception:
            continue
    return out


def spelling_not_checked(site) -> str:
    """"" when spelling ran for this crawl, else why the Spelling Errors filter is Not checked."""
    return unmet(SPELLING_REQUIRES, site, content_tab_rows(site))


def grammar_details(site, url: str) -> Optional[Dict[str, Any]]:
    """The Grammar section of the URL details: LanguageTool's messages with their context, or
    {"not_checked": reason}; None for a URL that is not a 2xx HTML page."""
    r = (getattr(site, "by_url", None) or {}).get(url)
    if r is None or not is_html_doc(r) or category_of(r) != "2xx":
        return None
    res = grammar_result(site, url)
    if res is None:
        return {"not_checked": grammar_skip_reason(site, url)}
    matches = [m for m in res.get("matches") or [] if isinstance(m, dict)]
    shown = [{k: m.get(k) for k in ("message", "context", "offset", "length", "rule")}
             for m in matches[:GRAMMAR_DETAIL_MAX]]
    count = grammar_count(site, r) or 0
    indexable = is_indexable(site, r)
    return {"count": count, "matches": shown, "capped": len(matches) > GRAMMAR_DETAIL_MAX, "indexable": indexable,
            "counted": indexable and count > 0}


def spelling_grammar_summary(site, top: int = 20) -> Dict[str, Any]:
    """Right-panel numbers for Spelling & Grammar: spelling.summary() (or its Not checked reason) and the grammar
    totals (pages checked / with problems / not checked, most frequent rules), or Grammar's Not connected /
    Not checked reason. pages_with_errors equals the filter counts."""
    why = spelling_not_checked(site)
    out: Dict[str, Any] = {"spelling": {"not_checked": why} if why else spelling.summary(site, top)}
    gwhy = grammar_reason(site)
    if gwhy:
        out["grammar"] = {"not_checked": gwhy, "not_connected": gwhy.startswith("Not connected")}
        return out
    rules: Counter = Counter()
    checked = with_errors = not_checked = 0
    for r in content_tab_rows(site):
        if not is_html_doc(r):
            continue
        res = grammar_result(site, r.url)
        if res is None:
            not_checked += 1
            continue
        checked += 1
        n = grammar_count(site, r) or 0
        if n and is_indexable(site, r):
            with_errors += 1
            for m in res.get("matches") or []:
                if isinstance(m, dict) and m.get("rule"):
                    rules[str(m["rule"])] += 1
    out["grammar"] = {"pages_checked": checked, "pages_with_errors": with_errors, "pages_not_checked": not_checked,
                      "top_rules": [{"rule": k, "matches": v} for k, v in rules.most_common(top)]}
    return out


SPELLING_EXPORT_HEADERS = ["Address", "Language", "Misspelt Word", "Occurrences", "Suggestions", "Error Count",
                           "Capped", "Indexability"]
GRAMMAR_EXPORT_HEADERS = ["Address", "Message", "Context", "Rule", "Offset", "Length", "Indexability"]


def spelling_export_rows(site, crawler=None):
    """One row per misspelt word of every page the Spelling Errors filter counts."""
    pages = _counted_pages(site, _spelling_errors)
    errors = {r.url: spelling.site_errors(site, r.url) for r in pages}
    by_lang: Dict[str, set] = {}
    for e in errors.values():
        by_lang.setdefault(e["lang"] or spelling.DEFAULT_LANG, set()).update(x["word"] for x in e["errors"])
    sugg = {lang: spelling.suggestions(sorted(words), lang) for lang, words in by_lang.items()}
    for r in pages:
        e = errors[r.url]
        lang = e["lang"] or spelling.DEFAULT_LANG
        for x in e["errors"]:
            yield [r.url, lang, x["word"], x["count"], ", ".join(sugg.get(lang, {}).get(x["word"]) or []),
                   e["error_count"], "Yes" if e["capped"] else "No", INDEXABILITY.get(r, site)]


def grammar_export_rows(site, crawler=None):
    """One row per LanguageTool message of every page the Grammar Errors filter counts."""
    for r in _counted_pages(site, _grammar_errors):
        for m in (grammar_result(site, r.url) or {}).get("matches") or []:
            if isinstance(m, dict):
                yield [r.url, m.get("message", ""), m.get("context", ""), m.get("rule", ""), m.get("offset", ""),
                       m.get("length", ""), INDEXABILITY.get(r, site)]


def _register_outputs() -> None:
    """URL details section "grammar" and the two bulk exports (registry / reports helpers of P1)."""
    register_details("grammar", grammar_details)
    try:
        from ...reports import register_bulk_export
    except ImportError:
        return
    items = (("spelling_errors", "Spelling Errors (every misspelt word)", SPELLING_EXPORT_HEADERS,
              spelling_export_rows, spelling_not_checked,
              "One row per misspelt word of each page in Content > Spelling Errors, with likely intended words."),
             ("grammar_errors", "Grammar Errors (every LanguageTool message)", GRAMMAR_EXPORT_HEADERS,
              grammar_export_rows, grammar_reason,
              "One row per LanguageTool message of each page in Content > Grammar Errors, with its context."))
    for export_id, title, headers, rows, why, about in items:
        try:
            register_bulk_export(export_id, title, headers=headers, rows=rows, not_checked=why, description=about)
        except TypeError:           # a register_bulk_export without description=
            register_bulk_export(export_id, title, headers=headers, rows=rows, not_checked=why)
        except Exception:
            pass


_register_outputs()
