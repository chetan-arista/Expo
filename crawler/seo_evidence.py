"""
Pinpointed evidence for every SEO issue type (not only broken links), for the PDF and the dashboard.

capture_issue_evidence(crawler, index, out_dir) picks up to `per_issue` example pages per flagged
issue filter and makes one picture per example:

  dom      the page in Chrome, scrolled to the element, with the element boxed in red and the page
           area it sits in (header, menu, main content, footer ...) boxed in blue: images without alt
           text or size, H1/H2 problems, weak or nofollow links, unsafe target=_blank, http:// files
           on an https page, forms, placeholder text, soft-404 wording, thin content.
  link     the page that links to a broken / redirected / blocked URL with that link boxed, plus a
           screenshot of what the visitor gets (errors only).
  serp     a search-result preview for title and meta description problems (<head> is invisible).
  source   a view-source panel with the offending tag boxed, or a dashed line where a missing tag
           belongs: canonicals, robots directives, hreflang, AMP, pagination, structured data,
           viewport, <head>/<body> validation.
  headers  the response headers with the missing or wrong header marked (security headers).
  url      the page address with the offending characters boxed (URL tab, plain HTTP).

Chrome comes from crawler.browser_lease() (logged in when the crawl was); without Chrome the dom and
link pictures are skipped and every other kind is still drawn. Sign-out URLs are never opened.
Returns {(tab, filter): [{"url", "page", "shot", "raw", "boxes", "shot_boxes", "note", "mode",
"label", "error_shot"}]}: "shot" is the annotated picture (a file name in out_dir), "boxes" are in
pixels of "raw" (the full viewport screenshot) for dom/link pictures and of "shot" otherwise.

Speed: one picture per issue type (every broken internal URL up to BROKEN_SAMPLES gets its own), waits
that end when the page has painted and the element stopped moving (no fixed sleeps), two Chrome
workers when the host has the memory (not on LOW_MEMORY hosts), page source from the crawl's stored copy
(`html_provider`, default crawler.source_store.get) or downloaded in parallel, and one "what visitors
see" screenshot per distinct error page. Cookie-consent and newsletter pop-ups are hidden before each
screenshot (nothing is clicked). link_evidence() turns the broken-link pictures into the
{url: {"parent_shot", "error_shot", "position"}} shape of the crawl job's broken-link evidence.
"""
import bisect
import hashlib
import io
import os
import re
import threading
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from . import annotate

MAX_SOURCE_FETCHES = 40          # pages whose HTML is downloaded for view-source pictures
SOURCE_FETCH_THREADS = 8         # downloads at a time
MAX_SOURCE_BYTES = 3_000_000
MAX_BOXES = 6                    # problem boxes per picture (the rest are counted in the note)
PAGE_TIMEOUT = 20
SOURCE_LINES = 9                 # lines of page source per picture (about 4 above and 4 below the tag)
READY_CAP = 6.0                  # seconds a page may take to fire its load event before it is pictured anyway
SETTLE_CAP = 1.5                 # seconds the element may keep moving (scroll, lazy images, animations)
SCRIPT_TIMEOUT = 10              # seconds for the waiting scripts (above both caps)
MIN_PAGES_FOR_TWO = 12           # a second Chrome (2-4 s to start) only pays off from this many pages

Key = Tuple[str, str]


@dataclass
class _Task:
    tab: str
    filter: str
    mode: str                    # dom | link | serp | source | headers | url
    url: str                     # the flagged URL (the tab row)
    page: str = ""               # the page shown (dom/link: loaded in Chrome)
    spec: Dict[str, Any] = field(default_factory=dict)
    label: str = ""
    note: str = ""


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------
def _seo(r) -> dict:
    from .seo.registry import seo
    return seo(r)


def _path(url: str) -> str:
    p = urlparse(url or "")
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def _short(text: str, n: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _slug(url: str, key: Key, extra: str = "") -> str:
    """File name stem for a picture of `url` (the page shown) for issue `key`."""
    p = urlparse(url or "")
    base = re.sub(r"[^A-Za-z0-9]+", "-", (p.path + "-" + p.query).strip("/-"))[:40] or "home"
    digest = hashlib.md5(f"{url}|{extra}|{key[0]}|{key[1]}".encode("utf-8")).hexdigest()[:8]
    return f"pin-{key[0]}-{key[1]}-{base}-{digest}"[:110]


def _status(r) -> str:
    code = getattr(r, "status_code", None)
    return str(code) if code else (getattr(r, "http_status", "") or "no response")


def _is_http(url: str) -> bool:
    return (url or "").startswith(("http://", "https://"))


# --------------------------------------------------------------------------------------------
# Locating elements in the live page
# --------------------------------------------------------------------------------------------
_LOCATE_JS = r"""
const spec = arguments[0] || {};
const MAXB = spec.max || 6;
function absu(u) { try { const x = new URL(u, location.href); x.hash = ''; return x.href; } catch (e) { return u || ''; } }
function norm(u) { return absu((u || '').trim()).replace(/\/$/, '').toLowerCase(); }
function vis(e) {
  if (!e || !e.getBoundingClientRect) return false;
  const r = e.getBoundingClientRect(); if (r.width < 2 || r.height < 2) return false;
  const cs = getComputedStyle(e);
  return cs.visibility !== 'hidden' && cs.display !== 'none' && parseFloat(cs.opacity || '1') > 0.05;
}
function visAnc(e) { let p = e; while (p && p !== document.body && p !== document.documentElement) { if (vis(p)) return p; p = p.parentElement; } return null; }
function txt(e) { return ((e.innerText || e.textContent) || '').replace(/\s+/g, ' ').trim(); }
function anchorText(a) { let t = txt(a); if (!t) { const im = a.querySelector('img[alt]'); if (im) t = (im.getAttribute('alt') || '').trim(); } return t; }
const base = (spec.base || location.hostname).toLowerCase().replace(/^www\./, '');
function host(u) { try { return new URL(u, location.href).hostname.toLowerCase().replace(/^www\./, ''); } catch (e) { return ''; } }
function internal(u) { const h = host(u); return !!h && (h === base || h.endsWith('.' + base)); }
function relOf(a) { return (a.getAttribute('rel') || '').toLowerCase().split(/\s+/); }
const urlset = new Set((spec.urls || []).map(norm));
const rawset = new Set((spec.raw || []).filter(Boolean));
function refsOf(e) {
  // every address an element points at: lazy-loaded images keep theirs in data-src until scrolled to
  const out = [];
  ['href', 'src', 'action', 'data-href', 'data-url', 'data-src', 'data-lazy-src', 'data-original'].forEach(a => {
    const v = (e.getAttribute(a) || '').trim(); if (v && !v.startsWith('data:')) out.push(v); });
  ['srcset', 'data-srcset'].forEach(a => (e.getAttribute(a) || '').split(',').forEach(part => {
    const v = part.trim().split(/\s+/)[0]; if (v && !v.startsWith('data:')) out.push(v); }));
  if (e.currentSrc && !e.currentSrc.startsWith('data:')) out.push(e.currentSrc);
  return out;
}
function hrefMatch(e) {
  return refsOf(e).some(v => rawset.has(v) || urlset.has(norm(v)));
}
let els = [];
const k = spec.kind;
const lvl = spec.level || 1;
if (k === 'img') {
  const srcs = new Set((spec.srcs || []).map(norm));
  const imgs = Array.from(document.images);
  const bySrc = srcs.size ? imgs.filter(i => srcs.has(norm(i.getAttribute('src') || '')) || srcs.has(norm(i.currentSrc || '')) ||
                                         srcs.has(norm(i.getAttribute('data-src') || ''))) : [];
  const preds = {
    no_alt_attr: i => !i.hasAttribute('alt'),
    empty_alt: i => i.hasAttribute('alt') && !(i.getAttribute('alt') || '').trim(),
    long_alt: i => (i.getAttribute('alt') || '').trim().length > (spec.n || 100),
    no_size: i => !(i.hasAttribute('width') && i.hasAttribute('height')),
    any: i => true,
  };
  const pred = preds[spec.pred || 'any'] || preds.any;
  els = (bySrc.length ? bySrc : imgs).filter(pred);
  if (!els.length) els = bySrc;
} else if (k === 'heading') {
  els = Array.from(document.querySelectorAll('h' + lvl));
  if (spec.long) els = els.filter(h => txt(h).length > spec.long);
} else if (k === 'heading_img') {
  els = Array.from(document.querySelectorAll('h' + lvl + ' img[alt]')).filter(i => (i.getAttribute('alt') || '').trim());
} else if (k === 'no_heading') {
  const main = document.querySelector('main,[role=main]') || document.querySelector('article') || document.body;
  const hs = Array.from(main.querySelectorAll('h1,h2,h3,h4,h5,h6')).filter(vis);
  if (hs.length) els = [hs[0]];
  else {
    const blocks = Array.from(main.querySelectorAll('p,section,div')).filter(e => vis(e) && txt(e).length > 20);
    els = blocks.length ? [blocks[0]] : [main];
  }
} else if (k === 'nonseq') {
  const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  const lv = h => +h.tagName[1];
  if (lvl === 1) { if (hs.length) els = [hs[0]]; const h1 = hs.find(h => lv(h) === 1); if (h1 && h1 !== hs[0]) els.push(h1); }
  else { const i = hs.findIndex(h => lv(h) === 2); if (i >= 0) { els = [hs[i]]; if (i > 0) els.push(hs[i - 1]); } }
} else if (k === 'links') {
  const as = Array.from(document.querySelectorAll('a[href],area[href]'));
  const words = new Set((spec.words || []).map(w => w.toLowerCase()));
  const EDGE = /^[\s.,;:!?'"()\[\]‘’“”]+|[\s.,;:!?'"()\[\]‘’“”]+$/g;
  const preds = {
    nofollow_internal: a => internal(a.href) && relOf(a).includes('nofollow'),
    no_anchor: a => internal(a.href) && !anchorText(a),
    nondesc: a => internal(a.href) && words.has(anchorText(a).toLowerCase().replace(/\s+/g, ' ').replace(EDGE, '')),
    unsafe_blank: a => /^https?:/i.test(a.href) && !internal(a.href) && (a.getAttribute('target') || '').toLowerCase() === '_blank' &&
                       !relOf(a).some(x => x === 'noopener' || x === 'noreferrer'),
    localhost: a => { const h = host(a.href); return h === 'localhost' || h.endsWith('.localhost') || h.startsWith('127.') || h === '0.0.0.0' || h === '[::1]'; },
    external: a => /^https?:/i.test(a.href) && !internal(a.href) && !relOf(a).includes('nofollow'),
    internal: a => internal(a.href),
    to_url: a => hrefMatch(a),
    nofollow_to_url: a => hrefMatch(a) && relOf(a).includes('nofollow'),
  };
  const pred = preds[spec.pred] || (a => false);
  els = as.filter(pred);
} else if (k === 'link_to') {
  const cands = Array.from(document.querySelectorAll('a[href],area[href],form[action],[data-href],[data-url],img,source,iframe[src]'));
  els = cands.filter(e => refsOf(e).some(v => rawset.has(v)));
  if (!els.length) els = cands.filter(hrefMatch);
  if (!els.length && spec.selector) { try { const s = document.querySelector(spec.selector); if (s) els = [s]; } catch (e) {} }
  if (spec.nofollow) { const nf = els.filter(e => e.tagName === 'A' && relOf(e).includes('nofollow')); if (nf.length) els = nf; }
} else if (k === 'mixed') {
  els = Array.from(document.querySelectorAll('[src],link[href],object[data]')).filter(e => {
    const v = (e.getAttribute('src') || e.getAttribute('data') || (e.tagName === 'LINK' ? e.getAttribute('href') : '') || '').trim();
    return /^http:\/\//i.test(v) && (!urlset.size || urlset.has(norm(v)));
  });
} else if (k === 'protocol_relative') {
  els = Array.from(document.querySelectorAll('[src],link[href]')).filter(e =>
    (e.getAttribute('src') || (e.tagName === 'LINK' ? e.getAttribute('href') : '') || '').trim().startsWith('//'));
} else if (k === 'form') {
  els = Array.from(document.forms).filter(f => spec.pred === 'insecure' ? /^http:\/\//i.test(absu(f.getAttribute('action') || location.href)) : true);
} else if (k === 'text') {
  const words = (spec.words || []).map(w => w.toLowerCase()).filter(Boolean);
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set(); let n;
  while ((n = walker.nextNode()) && els.length < MAXB) {
    const t = (n.nodeValue || '').toLowerCase();
    if (!words.some(w => t.includes(w))) continue;
    const p = n.parentElement;
    if (p && !seen.has(p) && !['SCRIPT', 'STYLE', 'NOSCRIPT', 'TITLE', 'TEMPLATE'].includes(p.tagName)) { seen.add(p); els.push(p); }
  }
  if (!els.length) {
    els = Array.from(document.querySelectorAll('h1,h2,h3,p,li,span,div')).filter(e => vis(e) && words.some(w => txt(e).toLowerCase().includes(w)))
      .sort((a, b) => txt(a).length - txt(b).length).slice(0, 1);
  }
} else if (k === 'main') {
  const m = document.querySelector('main,[role=main]') || document.querySelector('article') ||
            document.querySelector('#content,#main,.content,.main');
  els = [m && vis(m) ? m : document.body];
} else if (k === 'longest_p') {
  // the page's own text: header, menus, footer and sidebars (an address block, a link list) are not what the
  // readability score is about, so they are used only when the page has no other text
  const chrome = 'header,nav,footer,aside,[role=banner],[role=navigation],[role=contentinfo],[role=complementary]';
  let best = null, bw = 0, alt = null, aw = 0;
  for (const e of document.querySelectorAll('p,li,td,blockquote')) {
    if (!vis(e)) continue; const w = txt(e).split(' ').length;
    if (e.closest(chrome)) { if (w > aw) { aw = w; alt = e; } continue; }
    if (w > bw) { bw = w; best = e; }
  }
  if (best || alt) els = [best || alt];
}
els = els.filter((e, i, a) => e && a.indexOf(e) === i);
// a lazy-loaded image (a data: placeholder in src, the real address in data-src / data-srcset) loads now, so the
// picture shows the image itself; one that then fails to load is hatched as missing below or when it is boxed
els.forEach(e => {
  if (e.tagName !== 'IMG') return;
  try {
    const cur = e.getAttribute('src') || '';
    const real = e.getAttribute('data-src') || e.getAttribute('data-lazy-src') || e.getAttribute('data-original') || '';
    const rs = e.getAttribute('data-srcset') || e.getAttribute('data-lazy-srcset') || '';
    e.loading = 'eager';
    if ((!cur || cur.startsWith('data:')) && real && !real.startsWith('data:')) e.setAttribute('src', real);
    if (rs && !rs.startsWith('data:') && (!(e.getAttribute('srcset') || '') || (e.getAttribute('srcset') || '').startsWith('data:'))) e.setAttribute('srcset', rs);
    const pic = e.parentElement && e.parentElement.tagName === 'PICTURE' ? e.parentElement : null;
    if (pic) pic.querySelectorAll('source[data-srcset]').forEach(s => s.setAttribute('srcset', s.getAttribute('data-srcset')));
  } catch (x) { /* an attribute the page locked: keep it as it is */ }
});
// a missing image (alt="") collapses to 0 x 0: it gets its hatched placeholder now, so the image itself is boxed
// rather than its container
els.forEach(e => {
  if (e.tagName !== 'IMG' || e.dataset.crawlerShown || !e.complete || e.naturalWidth) return;
  const cs = getComputedStyle(e);
  if (cs.display === 'none' || cs.visibility === 'hidden') return;
  e.dataset.crawlerShown = '1';
  e.style.cssText += ';display:inline-block;min-width:160px;min-height:90px;box-sizing:border-box;' +
                     'border:1px dashed #9CA3AF;background:repeating-linear-gradient(45deg,#F3F4F6 0 10px,#E5E7EB 10px 20px);';
});
const total = els.length;
// visible elements first; one hidden in a collapsed menu or slider is shown by its visible container,
// and only when nothing visible has the problem
let shown = els.filter(vis), hidden = 0, focusEl = shown[0] || null;
if (!shown.length) {
  for (const e of els) { const a = visAnc(e); if (a) { if (!focusEl) focusEl = e; shown.push(a); hidden++; } }
}
shown = shown.filter((e, i, a) => a.indexOf(e) === i);
const page_height = document.documentElement.scrollHeight;
if (!shown.length) { window.__crawlerPin = null; return {found: false, total: total, page_height: page_height}; }
const focus = shown[0];
// the boxed element's own CSS path, written like the crawler's (page_parser._css_path): anchored at an id or body
function cssPath(e) {
  const parts = [];
  let n = e;
  while (n && n.nodeType === 1 && n !== document.documentElement && parts.length < 12) {
    const tag = n.tagName.toLowerCase(), id = n.getAttribute('id') || '';
    if (id && /^[A-Za-z][\w-]*$/.test(id)) { parts.push(tag + '#' + id); break; }
    if (tag === 'body') { parts.push('body'); break; }
    const p = n.parentElement;
    const same = p ? Array.from(p.children).filter(c => c.tagName === n.tagName) : [n];
    parts.push(same.length > 1 ? tag + ':nth-of-type(' + (same.indexOf(n) + 1) + ')' : tag);
    n = p;
  }
  return parts.reverse().join(' > ');
}
// is the copy the crawl recorded (its first in the HTML) the boxed one? A hidden mobile menu often holds it
let recorded = '';
if (spec.kind === 'link_to' && spec.selector && focusEl) {
  try {
    const s0 = document.querySelector(spec.selector);
    recorded = !s0 || els.indexOf(s0) < 0 ? 'missing' : s0 === focusEl ? 'same' : vis(s0) ? 'other' : 'other_hidden';
  } catch (x) { recorded = ''; }
}
function regionOf(e) {
  const pairs = [['nav,[role=navigation]', 'Navigation menu'], ['header,[role=banner]', 'Header'], ['footer,[role=contentinfo]', 'Footer'],
                 ['aside,[role=complementary]', 'Sidebar'], ['form', 'Form'], ['main,[role=main],article', 'Main content']];
  for (const [sel, name] of pairs) { try { if (e.closest(sel)) return name; } catch (x) {} }
  return 'Page body';
}
function pickParent(e) {
  const er = e.getBoundingClientRect(); const ea = Math.max(1, er.width * er.height);
  const SEM = new Set(['HEADER', 'NAV', 'MAIN', 'FOOTER', 'ASIDE', 'FORM', 'SECTION', 'ARTICLE', 'TABLE', 'UL', 'OL', 'FIGURE', 'DL']);
  const ROLES = new Set(['navigation', 'banner', 'main', 'contentinfo', 'complementary', 'dialog', 'menu', 'menubar', 'list', 'region']);
  let p = e.parentElement, block = null;
  while (p && p !== document.body && p !== document.documentElement) {
    const r = p.getBoundingClientRect();
    if (r.height > innerHeight * 0.85) break;
    const big = r.width * r.height >= 1.5 * ea && r.width >= er.width && r.height >= er.height &&
                (r.height >= er.height + 8 || r.width >= er.width + 40);
    if (big && vis(p)) {
      if (SEM.has(p.tagName) || ROLES.has((p.getAttribute('role') || '').toLowerCase())) return p;
      // a plain block only when it is clearly more than the line the element sits on
      if (!block && r.width >= 160 && r.height >= er.height + 24) block = p;
    }
    p = p.parentElement;
  }
  return block;
}
const parent = spec.no_parent ? null : pickParent(focus);
const r = focus.getBoundingClientRect();
const absTop = r.top + scrollY;
let y = r.height > innerHeight * 0.6 ? absTop - 60 : absTop - innerHeight * 0.3;
if (parent) {
  const pr = parent.getBoundingClientRect(); const pTop = pr.top + scrollY;
  if (pr.height < innerHeight * 0.8 && absTop - pTop < innerHeight * 0.5) y = Math.min(y, pTop - 50);
}
// 'instant' overrides a site's CSS scroll-behavior: smooth (an animated scroll would move the boxes)
window.scrollTo({top: Math.max(0, y), left: 0, behavior: 'instant'});
window.__crawlerPin = {els: shown, parent: parent, focus: focus};
return {found: true, total: total, shown: shown.length, hidden: hidden, region: regionOf(focus), page_height: page_height,
        fixed: getComputedStyle(focus).position === 'fixed', selector: focusEl ? cssPath(focusEl) : '',
        text: focusEl ? (anchorText(focusEl) || '').slice(0, 120) : '',
        html: focusEl ? (focusEl.outerHTML || '').replace(/\s+/g, ' ').slice(0, 300) : '', recorded: recorded};
"""

_MEASURE_JS = r"""
const P = window.__crawlerPin; if (!P) return null;
const vw = window.innerWidth, vh = window.innerHeight;
function rect(e) {
  // an inline link or button wrapping an icon reports only its text line: take its visible children too
  const r = e.getBoundingClientRect();
  let x0 = r.left, y0 = r.top, x1 = r.right, y1 = r.bottom;
  if (['A', 'AREA', 'BUTTON', 'LABEL'].includes(e.tagName)) {
    const kids = e.querySelectorAll('img,svg,picture,video,canvas,span,i,div');
    for (let i = 0; i < kids.length && i < 20; i++) {
      const k = kids[i].getBoundingClientRect();
      if (k.width < 2 || k.height < 2) continue;
      x0 = Math.min(x0, k.left); y0 = Math.min(y0, k.top); x1 = Math.max(x1, k.right); y1 = Math.max(y1, k.bottom);
    }
  }
  if (e.tagName === 'IMG' && !e.dataset.crawlerShown && (x1 - x0 < 2 || y1 - y0 < 2 || (e.complete && !e.naturalWidth) ||
                                                        (e.getAttribute('src') || '').startsWith('data:'))) {
    // a missing image shows as empty space (or collapses to 0 x 0 with alt=""): hatch the space it takes so the
    // picture shows where the image should be instead of a blank area
    e.dataset.crawlerShown = '1';
    e.style.cssText += ';display:inline-block;min-width:160px;min-height:90px;box-sizing:border-box;' +
                       'border:1px dashed #9CA3AF;background:repeating-linear-gradient(45deg,#F3F4F6 0 10px,#E5E7EB 10px 20px);';
    return rect(e);
  }
  if (x1 - x0 < 2 || y1 - y0 < 2) {
    // still collapsed: the nearest ancestor of a sensible size, else a small marker at its place
    let p = e.parentElement;
    while (p && p !== document.body) {
      const q = p.getBoundingClientRect();
      if (q.width >= 8 && q.height >= 8 && q.width * q.height < 0.25 * vw * vh) return [q.left, q.top, q.width, q.height];
      p = p.parentElement;
    }
    return [x0 - 12, y0 - 12, 24, 24];
  }
  return [x0, y0, x1 - x0, y1 - y0];
}
function inView(r) { return r[0] < vw && r[1] < vh && r[0] + r[2] > 0 && r[1] + r[3] > 0; }
const issues = P.els.map(rect).filter(inView);
const imgs = P.els.filter(e => e.tagName === 'IMG');
const loading = imgs.some(i => inView(rect(i)) && !i.complete);
// A fixed or sticky bar over the element: how far to scroll so it is clear (negative = up)
let covered = 0;
try {
  const fr = P.focus.getBoundingClientRect();
  const cx = Math.min(vw - 1, Math.max(0, fr.left + fr.width / 2)), cy = Math.min(vh - 1, Math.max(0, fr.top + Math.min(fr.height / 2, 20)));
  const top = document.elementFromPoint(cx, cy);
  if (top && !(P.focus.contains(top) || top.contains(P.focus))) {
    let t = top;
    while (t && t !== document.body) {
      const p = getComputedStyle(t).position;
      if (p === 'fixed' || p === 'sticky') {
        const cr = t.getBoundingClientRect();
        covered = cr.top < vh / 2 ? -(cr.bottom - fr.top + 24) : (fr.bottom - cr.top + 24);
        break;
      }
      t = t.parentElement;
    }
  }
} catch (e) {}
return {dpr: window.devicePixelRatio || 1, vw: vw, vh: vh, sx: window.scrollX, sy: window.scrollY,
        issues: issues, total: P.els.length, parent: P.parent ? rect(P.parent) : null, loading: loading, covered: covered};
"""

_CLEAR_JS = "window.__crawlerPin = null;"

# Resolves once the located element has stopped moving: the scroll position, its box and its opacity are
# the same for `need` animation frames and the flagged images in view have loaded (at most `cap` seconds).
# Replaces fixed sleeps: a static page settles in a few frames, a page with lazy images or scroll
# animations gets the time it needs.
_SETTLE_JS = r"""
const done = arguments[arguments.length - 1];
const cap = (arguments[0] || 1.5) * 1000, need = arguments[1] || 3, t0 = Date.now();
const P = window.__crawlerPin;
if (!P || !P.focus) { done(0); return; }
const vw = innerWidth, vh = innerHeight;
function key() {
  const r = P.focus.getBoundingClientRect();
  let op = '';
  try { op = getComputedStyle(P.focus).opacity; } catch (e) {}
  return [Math.round(scrollY), Math.round(r.top), Math.round(r.left), Math.round(r.width), Math.round(r.height), op].join(',');
}
function pending() {
  return P.els.some(e => {
    if (e.tagName !== 'IMG' || e.complete) return false;
    const r = e.getBoundingClientRect();
    return r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw;
  });
}
let last = null, same = 0, finished = false;
const end = () => { if (!finished) { finished = true; done(Date.now() - t0); } };
setTimeout(end, cap + 150);
function tick() {
  if (finished) return;
  const k = key();
  if (k === last) same++; else { same = 0; last = k; }
  if ((same >= need && !pending()) || Date.now() - t0 > cap) { end(); return; }
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);
"""


# --------------------------------------------------------------------------------------------
# View-source windows
# --------------------------------------------------------------------------------------------
_TAG_RE = re.compile(r"<!--.*?-->|<(/?)([A-Za-z][\w:-]*)((?:\"[^\"]*\"|'[^']*'|[^'\">])*)>", re.S)
_ATTR_RE = re.compile(r"([\w:@.-]+)\s*(?:=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")
_TOKEN_RE = re.compile(r"<!--.*?-->|<[^>]*>|[^<]+", re.S)


def _attrs(raw: str) -> Dict[str, str]:
    out = {}
    for k, v in _ATTR_RE.findall(raw or ""):
        out.setdefault(k.lower(), (v or "").strip("\"'").strip())
    return out


def _rel(a: Dict[str, str]) -> List[str]:
    return (a.get("rel") or "").lower().split()


_TAG_TESTS: Dict[str, Callable[[str, Dict[str, str]], bool]] = {
    "canonical": lambda n, a: n == "link" and "canonical" in _rel(a),
    "robots": lambda n, a: n == "meta" and (a.get("name") or "").lower() in ("robots", "googlebot"),
    "hreflang": lambda n, a: n == "link" and "hreflang" in a,
    "amphtml": lambda n, a: n == "link" and "amphtml" in _rel(a),
    "next_prev": lambda n, a: n == "link" and bool({"next", "prev", "previous"} & set(_rel(a))),
    "ldjson": lambda n, a: n == "script" and "ld+json" in (a.get("type") or "").lower(),
    "viewport": lambda n, a: n == "meta" and (a.get("name") or "").lower() == "viewport",
    "mobile_alt": lambda n, a: n == "link" and "alternate" in _rel(a) and "media" in a,
    "refresh": lambda n, a: n == "meta" and (a.get("http-equiv") or "").lower() == "refresh",
    "title": lambda n, a: n == "title",
    "description": lambda n, a: n == "meta" and (a.get("name") or "").lower() == "description",
    "head": lambda n, a: n == "head",
    "body": lambda n, a: n == "body",
    "html": lambda n, a: n == "html",
    "form": lambda n, a: n == "form",
    "http_resource": lambda n, a: n in ("img", "script", "link", "iframe", "source", "video", "audio", "embed")
                                  and (a.get("src") or (a.get("href") if n == "link" else "") or "").lower().startswith("http://"),
    "protocol_relative": lambda n, a: (a.get("src") or (a.get("href") if n == "link" else "") or "").startswith("//"),
    "localhost": lambda n, a: bool(re.search(r"//(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0)\b",
                                             (a.get("href") or a.get("src") or "").lower())),
}


def _same_url(a: str, b: str) -> bool:
    return a.split("#")[0].rstrip("/").lower() == b.split("#")[0].rstrip("/").lower()


def find_tags(html: str, test: str, names: Optional[List[str]] = None, limit: int = 8,
              before: Optional[int] = None, href: str = "", target: str = "",
              page_url: str = "") -> List[Tuple[int, int]]:
    """(start, end) of the tags in `html` that pass the named test (a <title> span runs to </title>).
    `names`: for test "names", the element names to find; `before`: only tags that start before it.
    Test "href_to": tags whose href/src is `href` as written or resolves (against `page_url`) to `target`."""
    from urllib.parse import urljoin
    from html import unescape
    out = []
    fn = _TAG_TESTS.get(test)
    wanted = {n.lower() for n in names or []}
    for m in _TAG_RE.finditer(html or ""):
        if m.group(0).startswith("<!--") or m.group(1):
            continue
        if before is not None and m.start() >= before:
            break
        name = (m.group(2) or "").lower()
        if test == "href_to":
            a = _attrs(m.group(3))
            val = unescape(a.get("href") or a.get("src") or "")
            ok = bool(val) and ((href and val == href) or (target and _same_url(urljoin(page_url, val), target)))
        else:
            ok = (name in wanted) if test == "names" else bool(fn and fn(name, _attrs(m.group(3))))
        if not ok:
            continue
        end = m.end()
        if name == "title":
            close = html.lower().find("</title>", end)
            if 0 <= close < end + 600:
                end = close + len("</title>")
        out.append((m.start(), end))
        if len(out) >= limit:
            break
    return out


SOURCE_GAP = 6000      # spans further apart than this are shown as two excerpts joined by a "…" line


def source_window(html: str, spans: List[Tuple[int, int]], anchor: int = -1, max_lines: int = 22,
                  line_chars: int = 150) -> Tuple[List[str], List[Optional[int]], List[dict], int]:
    """Display lines of `html` around `spans`, one tag per line (minified sources are split at tags),
    with the original line number of each line. Returns (lines, line_numbers, highlights, anchor_line):
    highlights are {"line", "start", "end"} spans in the display lines; anchor_line is the display line
    holding offset `anchor` (where a missing tag belongs), -1 when not shown. When the spans lie far apart
    (a second <title> or meta description deep in a long <head>), the first and the last are shown as two
    excerpts separated by a "…" line, so both tags are on the picture."""
    spans = sorted(spans or [])
    short = max_lines < 15     # a short excerpt (the PDF's tiles): about as many lines above the tag as below
    if anchor < 0 and len(spans) > 1 and spans[-1][0] - spans[0][0] >= SOURCE_GAP:
        near = [sp for sp in spans if sp[0] - spans[0][0] < SOURCE_GAP]
        far = [sp for sp in spans if sp[0] - spans[-1][0] > -SOURCE_GAP and sp not in near]
        least = 4 if short else 6
        top = max(least, max_lines // 2)
        l1, n1, h1, _ = _source_window(html, near, -1, top, line_chars, after=2 if short else 3)
        l2, n2, h2, _ = _source_window(html, far, -1, max(least, max_lines - len(l1) - 1), line_chars,
                                       after=2 if short else 3)
        shift = len(l1) + 1
        return (l1 + ["…"] + l2, n1 + [None] + n2,
                h1 + [dict(h, line=h["line"] + shift) for h in h2], -1)
    return _source_window(html, spans, anchor, max_lines, line_chars,
                          after=max(2, (max_lines - 1) // 2) if short else 6)


def _source_window(html: str, spans: List[Tuple[int, int]], anchor: int = -1, max_lines: int = 22,
                   line_chars: int = 150, after: int = 6) -> Tuple[List[str], List[Optional[int]], List[dict], int]:
    """source_window() for spans close together."""
    html = html or ""
    if spans:
        first = spans[0][0]
        spans = [s for s in spans if s[0] - first < 6000]
        lo, hi = spans[0][0], spans[-1][1]
    elif anchor >= 0:
        lo = hi = anchor
    else:
        lo, hi = 0, min(len(html), 2000)
    lo = max(0, lo - 3000)
    hi = min(len(html), hi + 1500)
    k = html.find("<", lo)
    lo = k if 0 <= k < hi else lo
    newlines = [m.start() for m in re.finditer("\n", html)] if len(html) < 4_000_000 else []

    lines: List[str] = []
    offsets: List[int] = []
    pieces: List[Tuple[int, int, int, int]] = []    # (source start, source end, line, char start)
    opened: List[str] = []                          # element opened at the start of each line
    cur = None
    for m in _TOKEN_RE.finditer(html, lo, hi):
        tok = m.group(0)
        flat = " ".join(tok.split())
        if not flat:
            continue
        is_open = tok.startswith("<") and not tok.startswith("</")
        closing = tok.startswith("</")
        hl = any(s <= m.start() < e for s, e in spans)
        limit = line_chars * 3 if hl else line_chars
        if len(flat) > limit:
            flat = flat[: limit - 1] + "…"
        # a closing tag stays on the line that opened its element (<title>...</title>); others start a line
        closes_own = bool(closing and cur is not None and opened[cur]
                          and flat[2:].split(">")[0].strip().lower() == opened[cur])
        if cur is None or is_open or (closing and not closes_own) or len(lines[cur]) + len(flat) > line_chars:
            name = re.match(r"<([A-Za-z][\w:-]*)", flat) if is_open else None
            opened.append(name.group(1).lower() if name else "")
            lines.append(flat)
            offsets.append(m.start())
            cur = len(lines) - 1
            pieces.append((m.start(), m.end(), cur, 0))
        else:
            pieces.append((m.start(), m.end(), cur, len(lines[cur])))
            lines[cur] += flat

    highlights = []
    for s, e in spans:
        hit = [(ln, cs) for ps, pe, ln, cs in pieces if s <= ps < e]
        if not hit:
            continue
        ln, start = hit[0]
        end = start
        for ps, pe, l2, cs in pieces:
            if s <= ps < e and l2 == ln:
                end = max(end, cs + len(" ".join(html[ps:pe].split())[: line_chars * 3]))
        highlights.append({"line": ln, "start": start, "end": min(end, len(lines[ln]))})

    anchor_line = -1
    if anchor >= 0:
        anchor_line = next((p[2] for p in pieces if p[0] >= anchor), len(lines))
    # Window around the highlights (or the anchor)
    focus = [h["line"] for h in highlights] or ([anchor_line] if anchor_line >= 0 else [0])
    b = min(len(lines), max(focus) + after)
    a = max(0, min(focus) - max(2, min(7, max_lines - (b - max(focus)) - 1)))
    if b - a > max_lines:
        b = a + max_lines
    known = bool(newlines) or "\n" not in html
    nums = [bisect.bisect_right(newlines, off) + 1 if known else None for off in offsets[a:b]]
    out_h = [dict(h, line=h["line"] - a) for h in highlights if a <= h["line"] < b]
    shown = [mask_secrets(ln) for ln in lines[a:b]]
    for h in out_h:   # masking can shorten a line
        h["end"] = min(h["end"], len(shown[h["line"]]))
        h["start"] = min(h["start"], h["end"])
    return shown, nums, out_h, (anchor_line - a if a <= anchor_line <= b else -1)


_SECRET_WORDS = r"(?:csrf|xsrf|token|nonce|secret|passw|session|sessid|api[-_]?key|auth(?!or)|bearer|signature)"
_SECRET_ATTR_RE = re.compile(r"(\b[\w:-]*" + _SECRET_WORDS + r"[\w:-]*\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_SECRET_META_RE = re.compile(r"(<meta\b[^>]*\b(?:name|property|http-equiv)\s*=\s*[\"'][^\"']*" + _SECRET_WORDS +
                             r"[^\"']*[\"'][^>]*\bcontent\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_HIDDEN_VALUE_RE = re.compile(r"(<input\b[^>]*type\s*=\s*[\"']?hidden[^>]*\bvalue\s*=\s*)(\"[^\"]*\"|'[^']*')", re.I)
_JWT_RE = re.compile(r"eyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}")


def mask_secrets(line: str) -> str:
    """Hide token-like values (CSRF tokens, nonces, session ids, API keys, JWTs) before a line of page
    source is drawn into a picture: pages crawled while logged in carry them in <head> and forms."""
    def star(m):
        q = m.group(2)[0]
        return f"{m.group(1)}{q}***{q}"
    line = _SECRET_META_RE.sub(star, line)
    line = _HIDDEN_VALUE_RE.sub(star, line)
    line = _SECRET_ATTR_RE.sub(lambda m: star(m) if "keyword" not in m.group(1).lower() else m.group(0), line)
    return _JWT_RE.sub("***", line)


_SENSITIVE_HEADER = re.compile(r"cookie|authorization|token|secret|session|api-key|x-amz|signature", re.I)


def _mask_headers(headers: Dict[str, str]) -> List[Tuple[str, str]]:
    out = []
    for k, v in sorted((headers or {}).items()):
        v = "***" if _SENSITIVE_HEADER.search(k) else " ".join(str(v).split())
        out.append((k.lower(), v[:120] + ("…" if len(v) > 120 else "")))
    return out


# --------------------------------------------------------------------------------------------
# What each issue filter shows
# --------------------------------------------------------------------------------------------
_HEADER_FIX = {
    "missing_hsts": ("strict-transport-security", "max-age=31536000; includeSubDomains"),
    "missing_csp": ("content-security-policy", "default-src 'self'; ..."),
    "missing_xcto": ("x-content-type-options", "nosniff"),
    "missing_xfo": ("x-frame-options", "SAMEORIGIN"),
    "missing_referrer_policy": ("referrer-policy", "strict-origin-when-cross-origin"),
}

_SOURCE_LABELS = {
    ("canonicals", "canonicalised"): "Canonical points to another URL",
    ("canonicals", "multiple"): "More than one canonical tag",
    ("canonicals", "multiple_conflicting"): "Canonical tags disagree",
    ("canonicals", "non_indexable_canonical"): "Canonical target is not indexable",
    ("canonicals", "canonical_is_relative"): "Relative canonical URL",
    ("canonicals", "unlinked"): "Canonical target has no links to it",
    ("canonicals", "invalid_attribute"): "Invalid attribute on the canonical",
    ("canonicals", "contains_fragment"): "#fragment in the canonical URL",
    ("canonicals", "outside_head"): "Canonical sits outside <head>",
    ("directives", "noindex"): "noindex: kept out of Google",
    ("directives", "nofollow"): "nofollow: links not followed",
    ("directives", "none"): "none = noindex + nofollow",
    ("directives", "nosnippet"): "nosnippet: no text shown in results",
    ("directives", "noodp"): "noodp (obsolete)",
    ("directives", "noydir"): "noydir (obsolete)",
    ("directives", "noimageindex"): "noimageindex: images kept out",
    ("directives", "notranslate"): "notranslate",
    ("directives", "unavailable_after"): "unavailable_after date",
    ("directives", "outside_head"): "Robots tag outside <head>",
    ("hreflang", "non_200"): "hreflang URL does not load (non-200)",
    ("hreflang", "unlinked"): "hreflang URL has no links to it",
    ("hreflang", "missing_return_links"): "Other page does not link back",
    ("hreflang", "inconsistent_return_links"): "Return link uses another language code",
    ("hreflang", "non_canonical_return_links"): "hreflang points to a non-canonical URL",
    ("hreflang", "noindex_return_links"): "hreflang points to a noindex page",
    ("hreflang", "incorrect_codes"): "Invalid language / region code",
    ("hreflang", "multiple_entries"): "Same language listed twice",
    ("hreflang", "not_using_canonical"): "hreflang on a non-canonical URL",
    ("hreflang", "outside_head"): "hreflang outside <head>",
    ("pagination", "not_in_anchor"): "Pagination URL not linked with <a>",
    ("pagination", "non_200"): "Next/prev URL does not load",
    ("pagination", "unlinked"): "Next/prev URL has no links to it",
    ("pagination", "non_indexable"): "Paginated page is not indexable",
    ("pagination", "multiple"): "More than one next/prev tag",
    ("pagination", "loop"): "Pagination points back to itself",
    ("pagination", "sequence_error"): "Next/prev chain is broken",
    ("amp", "non_200"): "AMP URL does not load",
    ("amp", "missing_non_amp_return"): "No link back to the non-AMP page",
    ("amp", "non_indexable_canonical"): "Canonical is not indexable",
    ("amp", "indexable"): "AMP page is indexable",
    ("structured_data", "validation_errors"): "Structured data with errors",
    ("structured_data", "parse_errors"): "JSON-LD that does not parse",
    ("validation", "body_before_html"): "<body> before <html>",
    ("validation", "head_not_first"): "<head> is not first in <html>",
    ("validation", "multiple_head"): "More than one <head>",
    ("validation", "multiple_body"): "More than one <body>",
    ("mobile", "mobile_alternate"): "Separate mobile URL",
    ("page_titles", "multiple"): "More than one <title>",
    ("page_titles", "outside_head"): "<title> outside <head>",
    ("meta_description", "multiple"): "More than one meta description",
    ("meta_description", "outside_head"): "Meta description outside <head>",
    ("response_codes", "internal_meta_refresh"): "Meta refresh redirect",
}

_SOURCE_TEST = {"canonicals": "canonical", "directives": "robots", "hreflang": "hreflang", "pagination": "next_prev",
                "amp": "amphtml", "structured_data": "ldjson"}

_MISSING_LINES = {
    ("canonicals", "missing"): ('<link rel="canonical" href="{url}">', "No canonical tag"),
    ("hreflang", "missing_self_reference"): ('<link rel="alternate" hreflang="{lang}" href="{url}">', "No hreflang for the page itself"),
    ("hreflang", "missing_x_default"): ('<link rel="alternate" hreflang="x-default" href="{url}">', "No x-default"),
    ("amp", "missing_canonical"): ('<link rel="canonical" href="{url}">', "AMP page without a canonical"),
    ("amp", "missing_canonical_to_non_amp"): ('<link rel="canonical" href="(the non-AMP page)">', "Canonical must point to the non-AMP page"),
    ("amp", "invalid_viewport"): ('<meta name="viewport" content="width=device-width">', "Missing or invalid viewport"),
    ("amp", "missing_amp_script"): ('<script async src="https://cdn.ampproject.org/v0.js"></script>', "AMP script missing"),
    ("amp", "missing_head"): ("<head>", "No <head> tag"),
    ("amp", "missing_body"): ("<body>", "No <body> tag"),
    ("structured_data", "missing"): ('<script type="application/ld+json">{ ... }</script>', "No structured data"),
    ("mobile", "viewport_not_set"): ('<meta name="viewport" content="width=device-width, initial-scale=1">', "No viewport tag"),
    ("validation", "missing_head"): ("<head> ... </head>", "No <head> tag"),
    ("validation", "missing_body"): ("<body> ... </body>", "No <body> tag"),
}


def _url_spans(tab_filter: str, url: str) -> Tuple[List[Tuple[int, int]], str]:
    """Character spans of `url` to box for a URL-tab filter, and the label."""
    p = urlparse(url)
    path_start = url.find(p.path, len(p.scheme) + 3) if p.path else len(url)
    path_end = path_start + len(p.path or "")
    q_start = url.find("?")

    def runs(pattern: str, start: int = 0, end: Optional[int] = None) -> List[Tuple[int, int]]:
        return [(start + m.start(), start + m.end()) for m in re.finditer(pattern, url[start:end])][:6]

    def segments(pattern: str) -> List[Tuple[int, int]]:
        """Whole path segments holding a match (one readable box instead of many one-letter boxes)."""
        out, pos = [], path_start
        for seg in (url[path_start:path_end] or "").split("/"):
            if seg and re.search(pattern, seg):
                out.append((pos, pos + len(seg)))
            pos += len(seg) + 1
        return out[:6]

    f = tab_filter
    if f == "non_ascii":
        return segments(r"[^\x00-\x7f]") or runs(r"[^\x00-\x7f]+"), "Non-ASCII characters"
    if f == "underscores":
        return segments(r"_") or runs(r"_"), "Underscores instead of hyphens"
    if f == "uppercase":
        return segments(r"[A-Z]") or runs(r"[A-Z]+"), "Capital letters in the address"
    if f == "multiple_slashes":
        return runs(r"/{2,}", path_start, path_end), "Double slash"
    if f == "contains_space":
        return runs(r"(?:%20| |\+)+"), "Space in the address"
    if f == "ga_tracking":
        return runs(r"[?&](?:utm_[a-z]+|_ga|_gl|gclid)=[^&#]*"), "Tracking parameter"
    if f in ("parameters", "internal_search") and q_start >= 0:
        return [(q_start, len(url))], ("Internal search URL" if f == "internal_search" else "Query parameters")
    if f == "internal_search":
        return runs(r"search|/s/", path_start, path_end), "Internal search URL"
    if f == "repetitive_path":
        segs = [s for s in (p.path or "").split("/") if s]
        dup = next((s for i, s in enumerate(segs) if s in segs[:i]), "")
        return (runs(re.escape("/" + dup), path_start, path_end) if dup else []), "Repeated folder"
    if f == "over_115":
        return ([(115, len(url))] if len(url) > 115 else []), f"{len(url)} characters (over 115)"
    if f == "http_urls":
        return [(0, len("http://"))], "Plain HTTP, not HTTPS"
    return [], ""


class _Planner:
    """Turns issue rows into tasks (what to show, on which page)."""

    def __init__(self, site, index):
        self.site = site
        self.index = index
        self.base = getattr(site, "base_domain", "") or urlparse(getattr(site, "start_url", "") or "").hostname or ""
        self._occ = None
        self._flagged: Dict[Key, set] = {}

    # -- helpers --
    def flagged(self, tab: str, flt: str) -> set:
        """URLs flagged by one filter (cached)."""
        if (tab, flt) not in self._flagged:
            tr = (getattr(self.index, "tabs", None) or {}).get(tab)
            self._flagged[(tab, flt)] = {getattr(r, "url", "")
                                         for r in (tr.filter_rows.get(flt, []) if tr is not None else [])}
        return self._flagged[(tab, flt)]

    def occurrences(self) -> Dict[str, List[Tuple[str, dict]]]:
        if self._occ is None:
            try:
                from .seo.tabs.images import occurrences
                self._occ = occurrences(self.site)
            except Exception:
                self._occ = {}
        return self._occ

    def linking(self, r) -> List[str]:
        out = []
        parent = getattr(r, "parent_url", "") or ""
        if _is_http(parent):
            out.append(parent)
        try:
            from .exporter import linking_pages
            out += [p for p in linking_pages(self.site, r.url) if p not in out]
        except Exception:
            pass
        return out

    def inlink_sources(self, r, nofollow: Optional[bool] = None, non_indexable: bool = False) -> List[str]:
        out = []
        for i in (getattr(self.site, "inlinks", None) or {}).get(r.url, []) or []:
            src = i.get("source") or ""
            if not src or src == r.url or src in out:
                continue
            if nofollow is not None and bool(i.get("nofollow")) != nofollow:
                continue
            if non_indexable and (getattr(self.site, "indexable", {}) or {}).get(src):
                continue
            out.append(src)
        return out

    # -- per filter --
    def tasks_for(self, tab: str, flt: str, rows: List[Any], want: int) -> List[_Task]:
        out: List[_Task] = []
        seen_pages = set()
        flagged = {getattr(r, "url", "") for r in rows}
        for r in rows[: max(30, want * 6)]:
            try:
                t = self.task(tab, flt, r, flagged)
            except Exception:
                t = None
            if t is None:
                continue
            page = t.page or t.url
            if page in seen_pages and (tab, flt) not in BROKEN_FILTERS:   # each broken URL has a card of its own
                continue
            seen_pages.add(page)
            out.append(t)
            if len(out) >= want * 3:
                break
        return out

    def task(self, tab: str, flt: str, r, flagged: set) -> Optional[_Task]:
        url = getattr(r, "url", "") or ""
        p = _seo(r)
        key = (tab, flt)

        def dom(kind: str, label: str, page: str = "", **spec) -> _Task:
            spec.update(kind=kind, base=self.base)
            return _Task(tab, flt, "dom", url, page or url, spec, label)

        def source(test: str, label: str = "", names=None) -> _Task:
            return _Task(tab, flt, "source", url, url, {"test": test, "names": names or []},
                         label or _SOURCE_LABELS.get(key, ""))

        # Response codes: the link on the page that points at the URL
        if tab == "response_codes":
            if flt == "internal_meta_refresh":
                return source("refresh")
            if flt == "internal_http_refresh":
                return _Task(tab, flt, "headers", url, url, {"header": "refresh"}, "Refresh header redirect")
            if getattr(r, "link_tag", "") in ("bundle", "script"):
                # a path read from JavaScript has no element on the page to box, unless the page shows its
                # menu item greyed out (not clickable)
                greyed = getattr(r, "code_greyed", None) or {}
                if not (greyed.get("selector") and _is_http(greyed.get("page", ""))):
                    return None
                return _Task(tab, flt, "link", url, greyed["page"],
                             {"kind": "link_to", "urls": [url], "raw": [], "selector": greyed["selector"],
                              "base": self.base, "error_shot": True},
                             f"Greyed out, not clickable · {_status(r)} if opened")
            parents = self.linking(r)
            if not parents:
                return None
            code = _status(r)
            region = (getattr(r, "link_region", "") or "").lower()
            if getattr(r, "link_tag", "") == "link" or "<head>" in region:
                # a <link> in <head> (hreflang, canonical, alternate ...): box it in the parent's source
                return _Task(tab, flt, "source", url, parents[0],
                             {"test": "href_to", "href": getattr(r, "raw_href", "") or "", "target": url},
                             f"Points to {code}")
            if flt in ("internal_client_error", "internal_server_error", "internal_no_response"):
                image = (getattr(r, "link_tag", "") or "").lower() in ("img", "source", "picture")
                label, err = (f"Missing image → {code}" if image else f"Broken link → {code}"), True
            elif flt.startswith("external_"):
                label, err = f"Link to another site → {code}", False
            elif flt == "internal_blocked_robots":
                label, err = "Link → blocked by robots.txt", False
            else:
                label, err = f"Link → {code} redirect", False
            return _Task(tab, flt, "link", url, parents[0],
                         {"kind": "link_to", "urls": [url], "raw": [getattr(r, "raw_href", "") or ""],
                          "selector": getattr(r, "link_selector", "") or "", "base": self.base, "error_shot": err},
                         label)
        if tab == "url" or (tab == "security" and flt == "http_urls"):
            spans, label = _url_spans(flt, url)
            return _Task(tab, flt, "url", url, url, {"spans": spans}, label) if spans else None
        if tab == "security":
            if flt == "mixed_content":
                return dom("mixed", "Loaded over http://", urls=list(p.get("mixed_content") or [])[:20],
                           fallback="http_resource")
            if flt == "form_url_insecure":
                return dom("form", "Form sends data to http://", pred="insecure", fallback="form")
            if flt == "form_on_http":
                return dom("form", "Form on an http:// page", pred="all", fallback="form")
            if flt == "unsafe_cross_origin":
                return dom("links", 'target="_blank" without noopener', pred="unsafe_blank")
            if flt == "protocol_relative":
                return dom("protocol_relative", "Address starts with //", fallback="protocol_relative")
            if flt in _HEADER_FIX:
                name, value = _HEADER_FIX[flt]
                # the other security headers this URL is flagged for go on the same picture
                also = [list(_HEADER_FIX[f]) for f in _HEADER_FIX if f != flt and url in self.flagged("security", f)]
                return _Task(tab, flt, "headers", url, url, {"missing": name, "value": value, "also": also},
                             f"No {name} header")
            if flt == "bad_content_type":
                return _Task(tab, flt, "headers", url, url, {"header": "content-type"}, "Wrong Content-Type")
            return None
        if tab in ("page_titles", "meta_description"):
            if flt in ("multiple", "outside_head"):
                return source("title" if tab == "page_titles" else "description")
            return _Task(tab, flt, "serp", url, url, {}, "")
        if tab in ("h1", "h2"):
            lvl = 1 if tab == "h1" else 2
            hs = [h for h in (p.get(tab) or []) if h is not None]
            if flt == "missing":
                return dom("no_heading", f"No H{lvl}: the page starts here", level=lvl, no_parent=True)
            if flt == "multiple":
                return dom("heading", f"{len(hs)} H{lvl} headings", level=lvl)
            if flt == "duplicate":
                return dom("heading", f"H{lvl} also used on other pages", level=lvl)
            if flt == "over_70_characters":
                long = max((len(h) for h in hs), default=0)
                return dom("heading", f"H{lvl} is {long} characters", level=lvl, long=70)
            if flt == "alt_text_in_h1":
                return dom("heading_img", "Image alt text inside the H1", level=1)
            if flt == "non_sequential":
                return dom("nonseq", "First heading is not an H1" if lvl == 1 else "H2 without an H1 before it",
                           level=lvl)
            return None
        if tab == "images":
            preds = {"missing_alt_text": ("empty_alt", "Empty alt text"),
                     "missing_alt_attribute": ("no_alt_attr", "No alt attribute"),
                     "alt_text_over_100_characters": ("long_alt", "Alt text over 100 characters"),
                     "missing_size_attributes": ("no_size", "No width / height"),
                     "over_100_kb": ("any", "Image over 100 KB")}
            if flt not in preds:
                return None
            pred, label = preds[flt]
            for page, _img in self.occurrences().get(url, []):
                srcs = [i.get("src") for i in (_seo(self.site.by_url.get(page)).get("images") or [])
                        if i.get("src") in flagged] if getattr(self.site, "by_url", None) else [url]
                if flt == "over_100_kb":
                    kb = 0
                    try:
                        kb = int((getattr(r, "seo", None) or {}).get("size_bytes") or 0) // 1024
                    except Exception:
                        pass
                    label = f"Image {kb:,} KB" if kb else label
                return dom("img", label, page=page, pred=pred, srcs=list(dict.fromkeys(srcs or [url]))[:20])
            return None
        if tab == "content":
            words = int(p.get("word_count") or 0)
            if flt == "exact_duplicates":
                try:
                    from .seo.tabs.content import exact_duplicate_urls
                    other = (exact_duplicate_urls(self.site, r) or [""])[0]
                except Exception:
                    other = ""
                return dom("main", f"Same text as {_path(other)}" if other else "Same text as another page",
                           no_parent=True)
            if flt == "near_duplicates":
                near = list((getattr(self.site, "near_duplicates", {}) or {}).get(url) or [])
                if near:
                    o, pct = max(near, key=lambda x: x[1])
                    return dom("main", f"{pct}% the same as {_path(o)}", no_parent=True)
                return dom("main", "Almost the same as another page", no_parent=True)
            if flt == "low_content_pages":
                return dom("main", f"Only {words:,} words", no_parent=True)
            if flt == "soft_404_pages":
                phrases = [w for w in ("page not found", "not found", "404", "doesn't exist", "does not exist",
                                       "no longer exists", "can't be found", "cannot be found", "nothing found")]
                return dom("text", "Reads like an error page", words=phrases)
            if flt in ("readability_difficult", "readability_very_difficult"):
                score = p.get("flesch_reading_ease")
                lab = f"Hard to read (score {round(score)})" if isinstance(score, (int, float)) else "Hard to read"
                return dom("longest_p", lab)
            if flt == "lorem_ipsum_placeholder":
                return dom("text", "Placeholder text", words=["lorem ipsum"])
            return None
        if tab == "links":
            if flt == "internal_nofollow_outlinks":
                from .seo.tabs.links import page_nofollow
                if page_nofollow(p):
                    return source("robots", "Page-level nofollow")
                return dom("links", "nofollow on a link to this site", pred="nofollow_internal")
            if flt == "no_anchor_text":
                return dom("links", "Link without text", pred="no_anchor")
            if flt == "non_descriptive_anchor":
                from .seo.tabs.links import NON_DESCRIPTIVE_ANCHORS
                return dom("links", "Vague link text", pred="nondesc", words=sorted(NON_DESCRIPTIVE_ANCHORS))
            if flt == "high_external_outlinks":
                from .seo.tabs.links import _followed
                return dom("links", f"{len(_followed(r)[1])} links to other sites", pred="external")
            if flt == "high_internal_outlinks":
                return dom("links", "Very many internal links", pred="internal")
            if flt == "outlinks_to_localhost":
                return dom("links", "Link to localhost", pred="localhost", fallback="localhost")
            if flt == "no_internal_outlinks":
                return dom("main", "No links to other pages of this site", no_parent=True)
            if flt in ("follow_nofollow_inlinks", "nofollow_inlinks_only"):
                srcs = self.inlink_sources(r, nofollow=True)
                if srcs:
                    return dom("link_to", "nofollow link to " + _path(url), page=srcs[0], urls=[url], nofollow=True)
                return None
            if flt == "non_indexable_inlinks_only":
                srcs = self.inlink_sources(r, non_indexable=True)
                if srcs:
                    return dom("link_to", "Only linked from non-indexable pages", page=srcs[0], urls=[url])
                return None
            if flt == "high_crawl_depth":
                srcs = self.inlink_sources(r)
                depth = (getattr(self.site, "crawl_depth", {}) or {}).get(url, "")
                if srcs:
                    return dom("link_to", f"Only way in: {depth} clicks from home", page=srcs[0], urls=[url])
                return None
            return None
        if tab == "mobile" and flt == "viewport_not_set":
            return _Task(tab, flt, "source", url, url, {"missing": _MISSING_LINES[key][0]}, _MISSING_LINES[key][1])
        if tab == "mobile" and flt == "mobile_alternate":
            return source("mobile_alt")
        if tab == "validation":
            if flt in ("missing_head", "missing_body"):
                return _Task(tab, flt, "source", url, url, {"missing": _MISSING_LINES[key][0]}, _MISSING_LINES[key][1])
            if flt == "invalid_head_elements":
                names = [str(n).lower() for n in p.get("invalid_head_elements") or []][:6]
                return _Task(tab, flt, "source", url, url, {"test": "names", "names": names, "in_head": True},
                             "Not allowed inside <head>") if names else None
            if flt in ("multiple_head", "head_not_first"):
                return source("head")
            if flt in ("multiple_body", "body_before_html"):
                return source("body")
            return None
        if key == ("canonicals", "missing") and "html" not in (getattr(r, "content_type", "") or "html").lower():
            # a PDF or other file has no <head>: its canonical can only be sent as an HTTP Link header
            return _Task(tab, flt, "headers", url, url, {"missing": "Link", "value": f'<{url}>; rel="canonical"'},
                         "No canonical Link header")
        if key in _MISSING_LINES:
            line, label = _MISSING_LINES[key]
            langs = [h.get("lang") for h in p.get("hreflang") or [] if isinstance(h, dict)]
            lang = p.get("language") or (langs[0] if langs else "en")
            return _Task(tab, flt, "source", url, url,
                         {"missing": line.replace("{url}", url).replace("{lang}", str(lang))}, label)
        if tab in _SOURCE_TEST:
            if tab == "directives" and not p.get("meta_robots") and p.get("x_robots_tag"):
                return _Task(tab, flt, "headers", url, url, {"header": "x-robots-tag"},
                             _SOURCE_LABELS.get(key, "X-Robots-Tag header"))
            if tab == "canonicals" and not p.get("canonicals_raw") and p.get("http_canonical"):
                return _Task(tab, flt, "headers", url, url, {"header": "link"}, _SOURCE_LABELS.get(key, "Canonical"))
            return source(_SOURCE_TEST[tab])
        return None


# --------------------------------------------------------------------------------------------
# Capture
# --------------------------------------------------------------------------------------------
class _Sources:
    """Page HTML for the view-source pictures, shared by the capture threads: the crawl's stored copy first
    (`provider`, e.g. crawler.source_store.get), else downloaded (at most MAX_SOURCE_FETCHES pages, several
    at a time with prefetch()). Sign-out and other unsafe URLs are never requested."""

    def __init__(self, crawler, provider: Optional[Callable[[str], Optional[str]]] = None):
        self.crawler = crawler
        self.provider = provider
        self._cache: Dict[str, Optional[str]] = {}
        self._lock = threading.Lock()
        self._fetches = 0

    def get(self, url: str) -> Optional[str]:
        with self._lock:
            if url in self._cache:
                return self._cache[url]
        html = self._stored(url)
        if html is None:
            html = self._fetch(url)
        with self._lock:
            self._cache[url] = html
        return html

    def prefetch(self, urls: List[str]):
        """Fills the cache for `urls` (stored copies, else SOURCE_FETCH_THREADS downloads at a time)."""
        with self._lock:
            todo = [u for u in dict.fromkeys(urls) if u and u not in self._cache]
        if not todo:
            return
        if len(todo) == 1:
            self.get(todo[0])
            return
        with ThreadPoolExecutor(max_workers=min(SOURCE_FETCH_THREADS, len(todo)),
                                thread_name_prefix="pin-source") as pool:
            list(pool.map(self.get, todo))

    def _stored(self, url: str) -> Optional[str]:
        if self.provider is None:
            return None
        try:
            html = self.provider(url)
        except Exception:
            return None
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="replace")
        return html if isinstance(html, str) and html.strip() else None

    def _fetch(self, url: str) -> Optional[str]:
        from .url_utils import is_unsafe_url
        if not _is_http(url) or is_unsafe_url(url):
            return None
        with self._lock:
            if self._fetches >= MAX_SOURCE_FETCHES:
                return None
            self._fetches += 1
        session = getattr(self.crawler, "http_session", None) or getattr(self.crawler, "session", None)
        try:
            if session is None:
                import requests
                session = requests
            ua = getattr(getattr(self.crawler, "config", None), "user_agent", "") or "Mozilla/5.0"
            resp = session.get(url, timeout=12, headers={"User-Agent": ua}, stream=True, allow_redirects=True)
            try:
                if "html" not in (resp.headers.get("content-type") or "html").lower():
                    return None
                raw = resp.raw.read(MAX_SOURCE_BYTES, decode_content=True)
                return raw.decode(resp.encoding or "utf-8", errors="replace")
            finally:
                resp.close()
        except Exception:
            return None


def _default_provider(crawler) -> Optional[Callable[[str], Optional[str]]]:
    """crawler.source_store.get when the crawl kept its pages' HTML, else None."""
    store = getattr(crawler, "source_store", None) if crawler is not None else None
    get = getattr(store, "get", None)
    return get if callable(get) else None


class _Run:
    """What the capture threads share: output folder, log, stop flag, page sources, the login-lost note."""

    def __init__(self, crawler, site, out_dir: str, logger: Callable[[str], None],
                 cancel_event: Optional[threading.Event], sources: _Sources):
        self.crawler = crawler
        self.site = site
        self.out_dir = out_dir
        self.log = logger
        self.cancel = cancel_event or threading.Event()
        self.sources = sources
        self.login_lost = False
        self.lock = threading.Lock()

    def source(self, url: str) -> Optional[str]:
        return self.sources.get(url)


class _Capture:
    """One Chrome working through pages for capture_issue_evidence(). Several can run at once (one per
    thread); they share a _Run."""

    def __init__(self, run: _Run):
        self.run = run
        self.crawler = run.crawler
        self.site = run.site
        self.out_dir = run.out_dir
        self.log = run.log
        self.cancel = run.cancel
        self.driver = None
        self._stack: Optional[ExitStack] = None
        self._media_was_blocked = False
        self._load_timeout = None
        self._script_timeout = None
        self._overlays_done = False

    @property
    def login_lost(self) -> bool:
        return self.run.login_lost

    # -- browser --
    def start_browser(self, timeout: float = 90) -> bool:
        """A driver from crawler.browser_lease(): the crawl's shared (logged-in) browser when it frees up
        within `timeout` seconds, else a temporary Chrome with the same session."""
        lease = getattr(self.crawler, "browser_lease", None) if self.crawler is not None else None
        if lease is None:
            return False
        self._stack = ExitStack()
        try:
            self.driver = self._stack.enter_context(lease(timeout=timeout))
        except Exception:
            self.driver = None
        if self.driver is None:
            self._stack.close()
            self._stack = None
            return False
        try:
            from .browser import media_blocked, set_media_blocked
            self._media_was_blocked = media_blocked(self.driver)
            set_media_blocked(self.driver, False)      # screenshots need the images
        except Exception:
            pass
        try:
            self._load_timeout = (self.driver.timeouts.page_load or None)
            self._script_timeout = (self.driver.timeouts.script or None)
        except Exception:
            self._load_timeout = self._script_timeout = None
        try:
            self.driver.set_page_load_timeout(PAGE_TIMEOUT)
        except Exception:
            pass
        from .evidence import ensure_script_timeout
        ensure_script_timeout(self.driver, SCRIPT_TIMEOUT)
        return True

    def close(self):
        if self.driver is not None:
            try:
                self.driver.execute_script(_CLEAR_JS)
            except Exception:
                pass
            try:
                from .browser import restore_overlays
                restore_overlays(self.driver)
            except Exception:
                pass
            try:
                from .browser import set_media_blocked
                set_media_blocked(self.driver, self._media_was_blocked)
            except Exception:
                pass
            for setter, value in ((self.driver.set_page_load_timeout, self._load_timeout),
                                  (self.driver.set_script_timeout, self._script_timeout)):
                if value:
                    try:
                        setter(value)
                    except Exception:
                        pass
        if self._stack is not None:
            self._stack.close()   # the lease owns the driver: never quit it here
        self._stack = None
        self.driver = None

    def load(self, url: str) -> bool:
        from .evidence import wait_ready
        from .url_utils import is_unsafe_url, redirected_to_login
        if self.driver is None or is_unsafe_url(url):
            return False
        self._overlays_done = False
        try:
            self.driver.get(url)
        except Exception:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                return False
        if not wait_ready(self.driver, READY_CAP):
            return False
        try:
            if getattr(self.crawler, "auth_used", False) and redirected_to_login(url, self.driver.current_url):
                with self.run.lock:
                    first = not self.run.login_lost
                    self.run.login_lost = True
                if first:
                    self.log(f"[Pinpoint] The browser was sent to the login page on {url}; logged-in pages "
                             "are not captured.")
                return False
        except Exception:
            pass
        return True

    def screenshot(self):
        try:
            from PIL import Image
            return Image.open(io.BytesIO(self.driver.get_screenshot_as_png())).convert("RGB")
        except Exception:
            return None

    def _settle(self) -> Optional[dict]:
        """Waits until the located element stops moving, then measures it (None when it is gone)."""
        try:
            self.driver.execute_async_script(_SETTLE_JS, SETTLE_CAP, 3)
        except Exception:
            time.sleep(0.15)
        try:
            return self.driver.execute_script(_MEASURE_JS)
        except Exception:
            return None

    def pinpoint(self, task: _Task, name: str) -> Optional[dict]:
        """Locate the task's elements on the loaded page, scroll to the first, screenshot and annotate."""
        spec = dict(task.spec, max=MAX_BOXES)
        try:
            found = self.driver.execute_script(_LOCATE_JS, spec)
        except Exception:
            found = None
        if not found or not found.get("found"):
            return None
        if not self._overlays_done:
            # cookie-consent and newsletter pop-ups: hidden once per page (never clicked), keeping the flagged
            # element; hiding one can unlock the page's scrolling, so the element is located again
            self._overlays_done = True
            from .evidence import hide_popups
            hidden = hide_popups(self.driver, str(found.get("selector") or ""))
            if hidden.get("hidden") or hidden.get("matched"):
                try:
                    again = self.driver.execute_script(_LOCATE_JS, spec)
                except Exception:
                    again = None
                if not again or not again.get("found"):
                    return None
                found = again
        m = self._settle()
        if m and m.get("covered"):
            # a sticky header (or a bottom bar) covers the element: scroll it clear
            try:
                delta = max(-400.0, min(400.0, float(m["covered"])))
                self.driver.execute_script("window.scrollBy({top: arguments[0], left: 0, behavior: 'instant'});",
                                           delta)
                m = self._settle()
            except Exception:
                pass
        if not m or not m.get("issues"):
            return None
        img = self.screenshot()
        if img is None:
            return None
        scale = img.width / float(m.get("vw") or img.width)
        vw, vh = float(m.get("vw") or img.width), float(m.get("vh") or img.height)

        def clip(rect) -> Optional[List[float]]:
            """The part of a viewport rect that is on screen, or None when (almost) nothing is."""
            x0, y0 = max(0.0, rect[0]), max(0.0, rect[1])
            x1, y1 = min(vw, rect[0] + rect[2]), min(vh, rect[1] + rect[3])
            if x1 - x0 < 3 or y1 - y0 < 3:
                return None
            return [round(x0 * scale), round(y0 * scale), round((x1 - x0) * scale), round((y1 - y0) * scale)]

        issue_rects = [r for r in (clip(x) for x in m["issues"][:MAX_BOXES]) if r]
        if not issue_rects:
            return None
        parent = clip(m["parent"]) if m.get("parent") else None
        if parent:
            # a parent that is mostly off screen, or no longer holds the problem, would mislead
            px0, py0, pw, ph = parent
            ix0, iy0, iw, ih = issue_rects[0]
            full = m["parent"][2] * m["parent"][3] * scale * scale
            holds = px0 - 4 <= ix0 and py0 - 4 <= iy0 and ix0 + iw <= px0 + pw + 4 and iy0 + ih <= py0 + ph + 4
            if not holds or (full and pw * ph < 0.35 * full):
                parent = None
        boxes = []
        n = 1
        region = found.get("region") or ""
        if parent:
            boxes.append({"n": n, "kind": "parent", "label": f"In the {region}" if region else "Where it is",
                          "rect": parent})
            n += 1
        for i, rect in enumerate(issue_rects):
            boxes.append({"n": n, "kind": "issue", "label": task.label if i == 0 else "", "rect": rect})
            n += 1
        raw_name = f"{name}_raw.jpg"
        raw_path = os.path.join(self.out_dir, raw_name)
        try:
            img.save(raw_path, "JPEG", quality=72, optimize=True)
        except Exception:
            return None
        shot_name = f"{name}.jpg"
        drawn: List[dict] = []
        if not annotate.draw(raw_path, boxes, os.path.join(self.out_dir, shot_name), boxes_out=drawn,
                             **annotate.TILE):
            return None
        total = int(found.get("total") or 0)
        shown = len(issue_rects)
        notes = []
        if total > shown:
            notes.append(f"{total} on this page; {shown} shown here")
        if found.get("hidden"):
            notes.append("inside a collapsed menu or hidden block (its visible container is boxed)")
        first = m["issues"][0]
        # the boxed element itself (the first visible copy), so the PDF text names what the picture shows;
        # "recorded": whether the copy the crawl recorded is that element ("same") or another, hidden one
        return {"shot": shot_name, "raw": raw_name, "boxes": boxes, "shot_boxes": drawn, "note": "; ".join(notes),
                "region": region, "selector": str(found.get("selector") or "")[:300],
                "text": str(found.get("text") or "")[:120], "html": mask_secrets(str(found.get("html") or ""))[:300],
                "recorded": str(found.get("recorded") or ""), "total": total,
                "top": int(round(float(m.get("sy") or 0) + float(first[1]))),
                "page_height": int(found.get("page_height") or 0), "hidden": bool(found.get("hidden"))}

    def error_shot(self, url: str, name: str) -> str:
        """What a visitor gets at a broken URL (blank space trimmed)."""
        if not self.load(url):
            return ""
        from .evidence import hide_popups
        hide_popups(self.driver)
        img = self.screenshot()
        if img is None:
            return ""
        try:
            from .evidence import EvidenceCollector
            img = EvidenceCollector._trim_blank(img)
            width = annotate.TILE["max_width"]
            if img.width > width:
                img = img.resize((width, int(img.height * width / img.width)))
            fname = f"{name}_error.jpg"
            img.save(os.path.join(self.out_dir, fname), "JPEG", quality=annotate.TILE["quality"], optimize=True)
            return fname
        except Exception:
            return ""

    # -- page source --
    def source(self, url: str) -> Optional[str]:
        return self.run.sources.get(url)


def _rebuilt_head(p: dict, url: str) -> str:
    """A <head> rebuilt from the crawl's data when the live source cannot be fetched."""
    from xml.sax.saxutils import escape, quoteattr
    lines = ["<head>"]
    for t in p.get("titles") or []:
        lines.append(f"<title>{escape(str(t))}</title>")
    for d in p.get("meta_descriptions") or []:
        lines.append(f'<meta name="description" content={quoteattr(str(d))}>')
    if p.get("viewport"):
        lines.append(f'<meta name="viewport" content={quoteattr(p["viewport"])}>')
    for c in p.get("meta_robots") or []:
        lines.append(f'<meta name="robots" content={quoteattr(str(c))}>')
    for c in p.get("canonicals_raw") or []:
        lines.append(f'<link rel="canonical" href={quoteattr(str(c))}>')
    if p.get("rel_prev"):
        lines.append(f'<link rel="prev" href={quoteattr(p["rel_prev"])}>')
    if p.get("rel_next"):
        lines.append(f'<link rel="next" href={quoteattr(p["rel_next"])}>')
    for h in p.get("hreflang") or []:
        if isinstance(h, dict):
            lines.append(f'<link rel="alternate" hreflang={quoteattr(str(h.get("lang", "")))} '
                         f'href={quoteattr(str(h.get("url", "")))}>')
    if p.get("amphtml"):
        lines.append(f'<link rel="amphtml" href={quoteattr(p["amphtml"])}>')
    if p.get("meta_refresh"):
        lines.append(f'<meta http-equiv="refresh" content={quoteattr(p["meta_refresh"])}>')
    for t in (p.get("structured_data") or {}).get("jsonld") or []:
        lines.append(f'<script type="application/ld+json">{{"@type": "{escape(str(t))}", ...}}</script>')
    lines.append("</head>")
    return "\n".join(lines)


def _render_source(cap: "_Run", task: _Task, r, name: str) -> Optional[dict]:
    """View-source picture for a source task (the source of task.page: the flagged URL itself, or for a
    link in <head> the page holding it)."""
    page = task.page or task.url
    if page != task.url:
        r = (getattr(cap.site, "by_url", None) or {}).get(page)
    p = _seo(r) if r is not None else {}
    html = cap.source(page)
    note = ""
    if not html:
        if task.spec.get("test") == "href_to":
            return None
        html = _rebuilt_head(p, page)
        note = "Rebuilt from the crawl data (the live page source could not be downloaded)."
    lower = html.lower()
    head_end = lower.find("</head")
    spec = task.spec
    spans: List[Tuple[int, int]] = []
    anchor = -1
    if spec.get("missing"):
        anchor = head_end if head_end >= 0 else max(0, lower.find("<body"))
    else:
        test = spec.get("test") or ""
        before = head_end if spec.get("in_head") and head_end > 0 else None
        spans = find_tags(html, test, spec.get("names"), before=before, href=spec.get("href", ""),
                          target=spec.get("target", ""), page_url=page)
        if test == "names" and not spans:
            spans = find_tags(html, test, spec.get("names"))
        if task.tab == "directives" and task.filter in ("noindex", "nofollow", "none", "nosnippet", "noimageindex",
                                                        "notranslate", "unavailable_after", "noodp", "noydir"):
            token = "unavailable_after" if task.filter == "unavailable_after" else task.filter
            narrowed = [s for s in spans if token in html[s[0]:s[1]].lower()]
            spans = narrowed or spans
        if not spans:
            return None
    lines, nums, hls, anchor_line = source_window(html, spans, anchor, max_lines=SOURCE_LINES)
    for h in hls:
        h["label"] = ""
    if hls:
        # "more than one" tag: the label goes on the extra (last) tag, the first is the one to keep
        hls[-1 if task.filter == "multiple" else 0]["label"] = task.label
    drawn: List[dict] = []
    fname = f"{name}.png"
    where = "<head>" if (not spans or head_end < 0 or spans[0][0] < head_end) else "<body>"
    path = annotate.render_source_snippet(
        lines, hls, os.path.join(cap.out_dir, fname), title=f"view-source:{page}", line_numbers=nums,
        missing=spec.get("missing", ""), missing_at=anchor_line if spec.get("missing") else -1,
        label=task.label, parent_label=f"{where} of {_short(_path(page), 34)}", boxes_out=drawn, note=note,
        max_rows=SOURCE_LINES + 5, **annotate.SOURCE_TILE)
    if not path:
        return None
    return {"shot": fname, "raw": "", "boxes": drawn, "shot_boxes": drawn, "note": note, "mode": "source"}


def _render_headers(cap: "_Run", task: _Task, r, name: str) -> Optional[dict]:
    headers = dict(getattr(r, "response_headers", None) or {})
    if not headers:
        return None
    rows = _mask_headers(headers)
    code = str(getattr(r, "status_code", "") or "")
    status = getattr(r, "http_status", "") or ""
    lines = [f"HTTP/1.1 {status if status.startswith(code) else (code + ' ' + status).strip()}"]
    lines += [f"{k}: {v}" for k, v in rows]
    hls = []
    want = task.spec.get("header")
    if want:
        for i, (k, _) in enumerate(rows, start=1):
            if k == want:
                hls.append({"line": i, "start": 0, "end": len(lines[i]), "label": task.label if not hls else ""})
        if not hls:
            return None
    missing: List[str] = []
    label = task.label
    if task.spec.get("missing"):
        missing = [f"{task.spec['missing']}: {task.spec.get('value', '')}"]
        missing += [f"{n}: {v}" for n, v in task.spec.get("also") or []]
        if len(missing) > 1:
            label = f"{len(missing)} security headers missing"
    drawn: List[dict] = []
    fname = f"{name}.png"
    path = annotate.render_source_snippet(
        lines, hls, os.path.join(cap.out_dir, fname), title=f"Response headers · {task.url}",
        line_numbers=[None] * len(lines), missing=missing, missing_at=len(lines) if missing else -1,
        label=label, parent_label=f"Response headers of {_short(_path(task.url), 28)}", boxes_out=drawn,
        max_rows=10 + len(missing), **annotate.SOURCE_TILE)
    if not path:
        return None
    return {"shot": fname, "raw": "", "boxes": drawn, "shot_boxes": drawn, "note": ""}


def _render_url(cap: "_Run", task: _Task, name: str) -> Optional[dict]:
    hls = [{"line": 0, "start": s, "end": e, "label": task.label if i == 0 else ""}
           for i, (s, e) in enumerate(task.spec.get("spans") or [])]
    drawn: List[dict] = []
    fname = f"{name}.png"
    path = annotate.render_source_snippet([task.url], hls, os.path.join(cap.out_dir, fname), title="Address bar",
                                          line_numbers=[None], label=task.label, parent_label="The page address",
                                          boxes_out=drawn)
    if not path:
        return None
    return {"shot": fname, "raw": "", "boxes": drawn, "shot_boxes": drawn, "note": ""}


def _render_serp(cap: "_Run", task: _Task, r, rows: List[Any], name: str) -> Optional[dict]:
    from .seo.tabs import meta_description as md_tab
    from .seo.tabs.page_titles import TITLE_MAX_PX, TITLE_MIN_PX
    p = _seo(r)
    title = ((p.get("titles") or [""])[0] or "").strip()
    desc = ((p.get("meta_descriptions") or [""])[0] or "").strip()
    focus = "title" if task.tab == "page_titles" else "description"
    text = title if focus == "title" else desc
    n = len(text)
    try:
        from .seo.text_metrics import pixel_width
        px = pixel_width(text, focus)
    except Exception:
        px = 0
    d_max = getattr(md_tab, "DESCRIPTION_MAX_PX", 985)
    d_min = getattr(md_tab, "DESCRIPTION_MIN_PX", 400)
    word = "Title" if focus == "title" else "Description"
    labels = {
        "missing": f"No {'<title>' if focus == 'title' else 'meta description'}",
        "duplicate": f"Same {word.lower()} as another page",
        "over_60_characters": f"{n} characters: cut after about 60",
        "below_30_characters": f"Only {n} characters",
        "over_561_pixels": f"{px} px wide: cut at {TITLE_MAX_PX} px",
        "below_200_pixels": f"Only {px} px wide",
        "same_as_h1": "Title is the same as the H1",
        "over_155_characters": f"{n} characters: cut after about 155",
        "below_70_characters": f"Only {n} characters",
        "over_985_pixels": f"{px} px wide: cut at {d_max} px",
        "below_400_pixels": f"Only {px} px wide",
    }
    label = labels.get(task.filter, task.label or word)
    others = []
    if task.filter == "duplicate":
        for o in rows:
            if o is r:
                continue
            op = _seo(o)
            ot = ((op.get("titles") or [""])[0] or "").strip()
            od = ((op.get("meta_descriptions") or [""])[0] or "").strip()
            if (focus == "title" and ot == title) or (focus == "description" and od == desc):
                others.append((ot, o.url, od))
                break
    short = 0
    if task.filter in ("below_30_characters", "below_200_pixels"):
        short = TITLE_MIN_PX
    elif task.filter in ("below_70_characters", "below_400_pixels"):
        short = d_min
    drawn: List[dict] = []
    fname = f"{name}.png"
    path = annotate.render_serp_mock(title, task.url, desc, label, os.path.join(cap.out_dir, fname), focus=focus,
                                     others=others, title_limit_px=TITLE_MAX_PX, desc_limit_px=d_max,
                                     parent_label=f"How Google may show {_short(_path(task.url), 40)}",
                                     boxes_out=drawn, short_limit_px=short, label_scale=1.5)
    if not path:
        return None
    note = "Search engines read the <title> and meta description from the page source; they are not visible " \
           "on the page itself."
    return {"shot": fname, "raw": "", "boxes": drawn, "shot_boxes": drawn, "note": note, "label": label}


def _issue_list(index) -> List[dict]:
    issues = list(getattr(index, "issues", None) or [])
    if issues:
        return issues
    out = []
    for tab_id, tr in (getattr(index, "tabs", None) or {}).items():
        for f in tr.tab.filters:
            if f.issue_type and tr.filter_rows.get(f.id):
                out.append({"tab": tab_id, "filter": f.id})
    return out


# Broken internal URLs each get a picture of their own (the PDF lists them all and the dashboard shows the
# picture beside each one): up to BROKEN_SAMPLES, the same cap as the crawl's broken-link screenshots
BROKEN_FILTERS = {("response_codes", "internal_client_error"), ("response_codes", "internal_server_error"),
                  ("response_codes", "internal_no_response")}
BROKEN_SAMPLES = 25
MAX_PAGES = 40                   # pages opened in Chrome per run (broken-link parents included)


def _wanted(key: Key, per_issue: int) -> int:
    """How many examples to picture for one issue."""
    return max(per_issue, BROKEN_SAMPLES) if key in BROKEN_FILTERS else per_issue


def _workers(pages: int, wanted: int = 0) -> int:
    """Chrome workers for `pages` pages: `wanted` when given (1 or 2), else two from MIN_PAGES_FOR_TWO pages
    on when the host has the memory (CRAWLER_PINPOINT_WORKERS overrides), else one."""
    if not wanted:
        try:
            wanted = int(os.environ.get("CRAWLER_PINPOINT_WORKERS") or 0)
        except ValueError:
            wanted = 0
    if wanted:
        return max(1, min(2, wanted, pages or 1))
    try:
        from .browser import LOW_MEMORY
    except Exception:
        LOW_MEMORY = True
    return 1 if LOW_MEMORY or pages < MIN_PAGES_FOR_TWO else 2


def _error_key(r) -> Optional[tuple]:
    """Broken URLs that answer the same error page share one "what visitors see" screenshot: the same status
    and the same page text (for a file: the same type and size of the answer). None = picture it on its own
    (no response: every browser error differs)."""
    if r is None or not getattr(r, "status_code", None):
        return None
    p = _seo(r)
    if p.get("content_hash"):
        return (r.status_code, "text", p["content_hash"])
    try:
        size = int(p.get("size_bytes") or getattr(r, "size_bytes", 0) or 0)
    except (TypeError, ValueError):
        size = 0
    ctype = (getattr(r, "content_type", "") or "").split(";")[0].strip().lower()
    return (r.status_code, ctype, size) if size and ctype else None


def capture_issue_evidence(crawler, index, out_dir: str, max_pages: int = MAX_PAGES, per_issue: int = 1,
                           site=None, logger: Optional[Callable[[str], None]] = None,
                           cancel_event: Optional[threading.Event] = None,
                           on_progress: Optional[Callable[[int, int], None]] = None,
                           skip: Optional[set] = None,
                           html_provider: Optional[Callable[[str], Optional[str]]] = None,
                           workers: int = 0, only: Optional[set] = None) -> Dict[Key, List[dict]]:
    """Pinpointed pictures for the flagged SEO issues: up to `per_issue` examples per (tab, filter) (every
    broken internal URL up to BROKEN_SAMPLES), with at most `max_pages` pages opened in Chrome. `index` is the
    TabIndex (or the (site, index) pair of crawler.site_index()). `skip`: (tab, filter) keys not to capture;
    `only`: capture just these keys (e.g. BROKEN_FILTERS). `html_provider(url)` returns a page's HTML as
    crawled, or None (default crawler.source_store.get); other pages are downloaded. `workers`: Chrome
    workers (0 = automatic, see _workers). on_progress(done, total) is called as pictures are made. Never
    raises: on an unexpected error it logs it and returns what it has."""
    log = logger or (getattr(crawler, "log", None) if crawler is not None else None) or (lambda m: None)
    out: Dict[Key, List[dict]] = OrderedDict()
    try:
        _capture_all(crawler, index, out_dir, max_pages, max(1, per_issue), site, log, cancel_event, on_progress,
                     skip, out, html_provider=html_provider, workers=workers, only=only)
    except Exception as e:
        log(f"[Pinpoint] Stopped early: {e}")
    return out


def _capture_all(crawler, index, out_dir: str, max_pages: int, per_issue: int, site, log: Callable[[str], None],
                 cancel_event: Optional[threading.Event], on_progress: Optional[Callable[[int, int], None]],
                 skip: Optional[set], out: Dict[Key, List[dict]],
                 html_provider: Optional[Callable[[str], Optional[str]]] = None, workers: int = 0,
                 only: Optional[set] = None) -> None:
    """capture_issue_evidence() without the safety net; fills `out`."""
    if isinstance(index, tuple) and len(index) == 2:
        site, index = site or index[0], index[1]
    if site is None and crawler is not None and hasattr(crawler, "site_index"):
        try:
            site, idx = crawler.site_index()
            index = index or idx
        except Exception:
            site = None
    if index is None or not annotate.available():
        return
    os.makedirs(out_dir, exist_ok=True)
    planner = _Planner(site, index)
    plan: "OrderedDict[Key, List[_Task]]" = OrderedDict()
    # broken internal URLs first: their pictures are also the dashboard's broken-link evidence
    issues = sorted(_issue_list(index), key=lambda i: 0 if (i.get("tab"), i.get("filter")) in BROKEN_FILTERS else 1)
    for issue in issues:
        key = (issue.get("tab"), issue.get("filter"))
        if not key[0] or (skip and key in skip) or (only is not None and key not in only):
            continue
        tr = index.tabs.get(key[0])
        rows = list(tr.filter_rows.get(key[1], []) if tr is not None else [])
        if not rows:
            continue
        cands = planner.tasks_for(key[0], key[1], rows, _wanted(key, per_issue))
        if cands:
            plan[key] = cands

    # Pages opened in Chrome: examples on a page already chosen for another issue come first
    pages: "OrderedDict[str, List[_Task]]" = OrderedDict()
    chosen: Dict[Key, List[_Task]] = OrderedDict()
    for key, cands in plan.items():
        per_issue_k = _wanted(key, per_issue)
        picks: List[_Task] = []
        browser = [t for t in cands if t.mode in ("dom", "link")]
        other = [t for t in cands if t.mode not in ("dom", "link")]
        for t in sorted(browser, key=lambda t: 0 if t.page in pages else 1):
            if len(picks) >= per_issue_k:
                break
            if t.page in pages or len(pages) < max_pages:
                pages.setdefault(t.page, []).append(t)
                picks.append(t)
        for t in other:
            if len(picks) >= per_issue_k:
                break
            picks.append(t)
        if picks:
            chosen[key] = picks

    by_url = getattr(site, "by_url", {}) or {}
    rows_of = {key: list(index.tabs[key[0]].filter_rows.get(key[1], [])) for key in chosen}
    row_by_url: Dict[Key, Dict[str, Any]] = {key: {getattr(r, "url", ""): r for r in rows} for key, rows in rows_of.items()}

    sources = _Sources(crawler, html_provider if html_provider is not None else _default_provider(crawler))
    run = _Run(crawler, site, out_dir, log, cancel_event, sources)
    others = [(key, t) for key, tasks in chosen.items() for t in tasks if t.mode not in ("dom", "link")]
    total = len(pages) + len(others)
    state = {"done": 0}

    def tick():
        with run.lock:
            state["done"] += 1
            done = state["done"]
        if on_progress:
            try:
                on_progress(done, max(total, 1))
            except Exception:
                pass

    results: Dict[int, dict] = {}

    # 1. Pictures that need no browser (search previews, page source, headers, addresses): drawn in a thread of
    #    their own while Chrome works, after the page sources were read (stored copies) or downloaded in parallel
    def draw_others():
        sources.prefetch([t.page or t.url for _, t in others if t.mode == "source"])
        for key, t in others:
            if run.cancel.is_set():
                break
            r = row_by_url[key].get(t.url) or by_url.get(t.url)
            name = _slug(t.url, key)
            try:
                if t.mode == "serp":
                    item = _render_serp(run, t, r, rows_of[key], name)
                elif t.mode == "source":
                    item = _render_source(run, t, r, name)
                elif t.mode == "headers":
                    item = _render_headers(run, t, r, name)
                else:
                    item = _render_url(run, t, name)
            except Exception as e:
                log(f"[Pinpoint] {key[0]}/{key[1]} on {t.url}: {e}")
                item = None
            if item:
                results[id(t)] = item
            tick()

    drawer = threading.Thread(target=draw_others, name="pin-draw", daemon=True)
    drawer.start()

    # 2. Pages in Chrome
    error_shots: Dict[tuple, str] = {}

    def run_page(cap: _Capture, page: str, tasks: List[_Task]) -> None:
        if not cap.load(page):
            return
        for t in tasks:
            if run.cancel.is_set():
                return
            key = (t.tab, t.filter)
            name = _slug(t.page, key, t.url)
            item = None
            try:
                item = cap.pinpoint(t, name)
            except Exception as e:
                log(f"[Pinpoint] {key[0]}/{key[1]} on {page}: {e}")
            if item is None and t.spec.get("fallback"):
                # not visible on the page (e.g. a script in <head>): show it in the source
                st = _Task(t.tab, t.filter, "source", t.url, t.url, {"test": t.spec["fallback"]}, t.label)
                item = _render_source(run, st, by_url.get(t.url), name)
            if item is not None:
                results[id(t)] = item
        # error pages after the parent (opening them leaves the parent page); one picture per distinct error page
        for t in tasks:
            if not (t.spec.get("error_shot") and id(t) in results) or run.cancel.is_set():
                continue
            ekey = _error_key(row_by_url.get((t.tab, t.filter), {}).get(t.url) or by_url.get(t.url))
            with run.lock:
                shot = error_shots.get(ekey, "") if ekey else ""
            if not shot:
                shot = cap.error_shot(t.url, _slug(t.url, (t.tab, t.filter)))
                if shot and ekey:
                    with run.lock:
                        error_shots.setdefault(ekey, shot)
            results[id(t)]["error_shot"] = shot

    queue = deque(pages.items())

    def work(cap: _Capture):
        while not run.cancel.is_set():
            with run.lock:
                if not queue:
                    return
                page, tasks = queue.popleft()
            try:
                run_page(cap, page, tasks)
            except Exception as e:
                log(f"[Pinpoint] {page}: {e}")
            tick()

    caps: List[_Capture] = []
    try:
        if pages and not run.cancel.is_set():
            main = _Capture(run)
            caps.append(main)
            if main.start_browser():
                n = _workers(len(pages), workers)
                log(f"[Pinpoint] Locating issues on {len(pages)} page(s) in Chrome"
                    + (f" ({n} browsers)..." if n > 1 else "..."))
                helpers = []
                for i in range(n - 1):
                    helper = _Capture(run)
                    caps.append(helper)

                    def second(c: _Capture = helper):
                        # a short wait for the shared browser (main holds it), then a temporary Chrome
                        try:
                            if c.start_browser(timeout=0.5):
                                work(c)
                        finally:
                            c.close()
                    th = threading.Thread(target=second, name=f"pin-chrome-{i + 2}", daemon=True)
                    th.start()
                    helpers.append(th)
                work(main)
                for th in helpers:
                    th.join()
                # An issue whose element could not be found: try its next example page while pages are left
                opened = len(pages)
                for key, cands in plan.items():
                    if run.cancel.is_set() or opened >= max_pages:
                        break
                    picked = chosen.get(key, [])
                    browser_picks = [t for t in picked if t.mode in ("dom", "link")]
                    if not browser_picks or any(id(t) in results for t in browser_picks):
                        continue
                    if key in BROKEN_FILTERS:
                        continue          # each broken URL is its own example: there is no "next" one to try
                    log(f"[Pinpoint] {key[0]}/{key[1]}: not found on screen at {browser_picks[0].page}; "
                        "trying another page")
                    tried = {t.page for t in browser_picks}
                    for t in cands:
                        if t.mode not in ("dom", "link") or t.page in tried or t in picked:
                            continue
                        run_page(main, t.page, [t])
                        opened += 1
                        if id(t) in results:
                            chosen[key] = [x for x in picked if x not in browser_picks] + [t]
                            break
                        if opened >= max_pages:
                            break
            else:
                reason = ""
                try:
                    from .browser import chrome_error
                    reason = chrome_error()
                except Exception:
                    pass
                log("[Pinpoint] Chrome is not available" + (f" ({reason})" if reason else "") +
                    "; page screenshots are skipped, source and search-result pictures are still drawn.")
        drawer.join()
    finally:
        for c in caps:
            c.close()

    for key, tasks in chosen.items():
        items = []
        for t in tasks:
            item = results.get(id(t))
            if not item:
                continue
            rec = {"url": t.url, "page": t.page or t.url, "shot": item.get("shot", ""), "raw": item.get("raw", ""),
                   "boxes": item.get("boxes", []), "shot_boxes": item.get("shot_boxes", []),
                   "note": item.get("note", "") or t.note, "mode": item.get("mode") or t.mode, "label": item.get("label") or t.label,
                   "error_shot": item.get("error_shot", ""), "region": item.get("region", "")}
            for extra in ("selector", "text", "html", "recorded", "total", "top", "page_height", "hidden"):
                if item.get(extra):
                    rec[extra] = item[extra]
            items.append(rec)
        if items:
            out[key] = items
    with run.lock:
        state["done"] = total - 1
    tick()
    log(f"[Pinpoint] Pinpointed pictures for {len(out)} issue type(s).")


def link_evidence(evidence) -> Dict[str, dict]:
    """The broken-link pictures of capture_issue_evidence() in the shape of the crawl job's broken-link evidence
    (job.evidence, read by GET /api/crawl/issues, the URL detail, the tickets and the PDF):
    {url: {"parent_shot": file | None, "error_shot": file | None, "position": {...}}}. "parent_shot" is the boxed
    picture of the page holding the link, "error_shot" what visitors get at the URL; "position" has "found",
    "top" and "page_height" (CSS px), "hidden", "matches", "region", "selector" and "via": "pinpoint" (a link in
    <head> is pictured in the page source: found False, "in_source" True). Keys of `evidence` may be (tab,
    filter) tuples or "tab/filter" strings."""
    out: Dict[str, dict] = {}
    for key, items in from_json(evidence or {}).items():
        if tuple(key) not in BROKEN_FILTERS:
            continue
        for it in items or []:
            url = (it or {}).get("url") or ""
            if not url or url in out:
                continue
            if it.get("mode") == "source":
                pos = {"found": False, "in_source": True, "via": "pinpoint"}
            else:
                pos = {"found": True, "top": int(it.get("top") or 0), "page_height": int(it.get("page_height") or 0),
                       "hidden": bool(it.get("hidden")), "matches": int(it.get("total") or 1),
                       "region": it.get("region") or "", "selector": it.get("selector") or "", "via": "pinpoint"}
            out[url] = {"parent_shot": it.get("shot") or None, "error_shot": it.get("error_shot") or None,
                        "position": pos}
    return out


# --------------------------------------------------------------------------------------------
# JSON (the dashboard and saved jobs use "tab/filter" keys)
# --------------------------------------------------------------------------------------------
def to_json(evidence: Dict[Key, List[dict]]) -> Dict[str, List[dict]]:
    return {f"{k[0]}/{k[1]}" if isinstance(k, tuple) else str(k): v for k, v in (evidence or {}).items()}


def from_json(data: Dict[str, List[dict]]) -> Dict[Key, List[dict]]:
    out: Dict[Key, List[dict]] = {}
    for k, v in (data or {}).items():
        if isinstance(k, tuple):
            out[k] = v
        elif "/" in str(k):
            a, b = str(k).split("/", 1)
            out[(a, b)] = v
    return out
