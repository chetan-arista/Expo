"""
Custom JavaScript snippet library (P15): ready snippets the team can add under Configuration > Custom JavaScript
(served by GET /api/config/js-snippets). Our own code. Each snippet is the body of an async function (as
render_capture runs it): `return` gives the value stored for the page; "action" snippets change the page before it
is read and return nothing that is stored.

    snippets() -> [{"id", "name", "type": "extraction" | "action", "description", "code"}]
    get(snippet_id) -> one of them or None
"""
from typing import Dict, List, Optional

SNIPPETS: List[Dict[str, str]] = [
    {"id": "scroll_to_bottom", "name": "Scroll to the bottom", "type": "action",
     "description": "Scrolls down the page step by step so lazy-loaded images and sections load before the page "
                    "is read.",
     "code": """const step = Math.max(200, Math.floor(innerHeight * 0.8));
for (let y = 0, i = 0; y < document.body.scrollHeight && i < 60; y += step, i++) {
  scrollTo(0, y);
  await new Promise(r => setTimeout(r, 150));
}
scrollTo(0, 0);"""},
    {"id": "accept_cookie_banner", "name": "Accept the cookie banner", "type": "action",
     "description": "Clicks the button of a cookie banner whose text says accept / agree / allow (common consent "
                    "tools), so the page is read the way a visitor sees it after consenting.",
     "code": """const words = /^(accept|accept all|i accept|agree|i agree|allow all|allow|got it|ok)$/i;
const ids = ['#onetrust-accept-btn-handler', '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
             '.cky-btn-accept', '#cookie_action_close_header', '.cc-allow', '.cmplz-accept'];
for (const sel of ids) {
  const el = document.querySelector(sel);
  if (el) { el.click(); return; }
}
for (const el of document.querySelectorAll('button, a[role=button], [role=button]')) {
  if (words.test((el.innerText || '').trim())) { el.click(); return; }
}"""},
    {"id": "datalayer", "name": "Read the dataLayer", "type": "extraction",
     "description": "The Google Tag Manager dataLayer events of the page (event names and keys), to check what "
                    "analytics sees.",
     "code": """const dl = Array.isArray(window.dataLayer) ? window.dataLayer : [];
return dl.slice(0, 50).map(e => (e && typeof e === 'object')
  ? {event: e.event || '', keys: Object.keys(e).slice(0, 20)} : String(e));"""},
    {"id": "count_elements", "name": "Count elements", "type": "extraction",
     "description": "How many elements match a CSS selector after rendering. Change the selector in the first line "
                    "(default: product cards).",
     "code": """const selector = '.product, .product-card, [data-product-id]';
return document.querySelectorAll(selector).length;"""},
    {"id": "hreflang_from_js", "name": "Hreflang added by JavaScript", "type": "extraction",
     "description": "The hreflang alternates in the rendered <head>, to compare with the ones in the HTML source "
                    "(tags added by scripts are easy to miss).",
     "code": """return [...document.querySelectorAll('link[rel=alternate][hreflang]')]
  .map(l => ({hreflang: l.getAttribute('hreflang'), href: l.href}));"""},
    {"id": "list_iframes", "name": "List iframes", "type": "extraction",
     "description": "Every iframe of the rendered page with its address and size (embedded videos, maps, forms, "
                    "ads).",
     "code": """return [...document.querySelectorAll('iframe')].slice(0, 50).map(f => ({
  src: f.src || f.getAttribute('data-src') || '', width: f.offsetWidth, height: f.offsetHeight,
  title: f.title || ''}));"""},
    {"id": "rendered_word_count", "name": "Word count after rendering", "type": "extraction",
     "description": "Words of visible text once JavaScript has run, to compare with the word count of the HTML "
                    "source.",
     "code": """const text = (document.body && document.body.innerText) || '';
return text.split(/\\s+/).filter(Boolean).length;"""},
    {"id": "wait_for_selector", "name": "Wait for an element", "type": "action",
     "description": "Waits up to 5 seconds for an element to appear before the page is read (content drawn late by "
                    "an app). Change the selector in the first line.",
     "code": """const selector = 'main';
for (let i = 0; i < 50 && !document.querySelector(selector); i++) {
  await new Promise(r => setTimeout(r, 100));
}"""},
]


def snippets() -> List[Dict[str, str]]:
    return [dict(s) for s in SNIPPETS]


def get(snippet_id: str) -> Optional[Dict[str, str]]:
    for s in SNIPPETS:
        if s["id"] == snippet_id:
            return dict(s)
    return None
