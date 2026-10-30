// The on-page check's reader. collectPage runs INSIDE the checked tab (chrome.scripting.executeScript, isolated
// world): it reads the page as the browser shows it now and returns plain data to the popup. It changes nothing
// on the page and sends nothing anywhere. It must stay self-contained: executeScript copies only this function's
// text, so it can use nothing from outside it (the rules arrive as its argument).
//
// It reads each value the way ARACHNIX's crawl does (crawler/seo/extract.py), so the popup and a crawl agree:
// titles and headings like _el_text, meta values like clean_text (which unescapes HTML once more), word count like
// content_lines + text_stats (body without nav / footer, one extra word per list item), structured data types like
// _structured_data (JSON-LD, microdata, RDFa).

/** rules: {blockTags, hiddenTextTags, contentExcludedTags, robotsMetaNames, invisible: [[lo, hi]], space: [cp]} */
export function collectPage(rules) {
  const SPACE = new Set(rules.space);
  const BLOCK = new Set(rules.blockTags);
  const HIDDEN = new Set(rules.hiddenTextTags);
  const EXCLUDED = new Set(rules.contentExcludedTags);
  const ROBOTS = new Set(rules.robotsMetaNames);
  const invisible = (cp) => rules.invisible.some(([lo, hi]) => cp >= lo && cp <= hi);

  const isSpace = (ch) => SPACE.has(ch.codePointAt(0));
  function words(text) {                         // Python str.split()
    const out = [];
    let cur = '';
    for (const ch of String(text || '')) {
      if (isSpace(ch)) {
        if (cur) out.push(cur);
        cur = '';
      } else {
        cur += ch;
      }
    }
    if (cur) out.push(cur);
    return out;
  }
  function strip(text) {                         // Python str.strip()
    const chars = [...String(text || '')];
    let a = 0;
    let b = chars.length;
    while (a < b && isSpace(chars[a])) a += 1;
    while (b > a && isSpace(chars[b - 1])) b -= 1;
    return chars.slice(a, b).join('');
  }
  let parser = null;
  function unescapeHtml(text) {                  // Python html.unescape (the browser's own character references)
    if (text.indexOf('&') < 0) return text;
    try {
      parser = parser || new DOMParser();
      const doc = parser.parseFromString('<title>' + text.replace(/</g, '&lt;') + '</title>', 'text/html');
      const t = doc.querySelector('title');
      return t ? t.textContent : text;
    } catch (e) {
      return text;
    }
  }
  const cleanText = (v) => (v === null || v === undefined ? '' : words(unescapeHtml(String(v))).join(' '));
  const nameOf = (el) => String(el.localName || '').toLowerCase();

  function elText(el) {                          // extract._el_text
    const parts = [];
    const stack = [];
    for (let i = el.childNodes.length - 1; i >= 0; i -= 1) stack.push(el.childNodes[i]);
    while (stack.length) {
      const node = stack.pop();
      if (node === null) {
        parts.push(' ');
        continue;
      }
      if (node.nodeType === 3) {
        parts.push(node.data);
        continue;
      }
      if (node.nodeType !== 1) continue;         // comments, CDATA, processing instructions
      const name = nameOf(node);
      if (name === 'script' || name === 'style') continue;
      if (BLOCK.has(name) || name === 'br') parts.push(' ');
      if (BLOCK.has(name)) stack.push(null);
      for (let i = node.childNodes.length - 1; i >= 0; i -= 1) stack.push(node.childNodes[i]);
    }
    const kept = [...parts.join('')].filter((ch) => !invisible(ch.codePointAt(0))).join('');
    return words(kept).join(' ');
  }

  function inSvg(el) {
    for (let p = el.parentElement; p; p = p.parentElement) if (nameOf(p) === 'svg') return true;
    return false;
  }

  const all = Array.from(document.getElementsByTagName('*'));
  const byName = (n) => all.filter((el) => nameOf(el) === n);
  const attr = (el, a) => el.getAttribute(a);

  // Titles (an SVG <title> is an icon label, not the page title)
  const titles = byName('title').filter((t) => !inSvg(t)).map(elText);

  // Meta description and meta robots
  const descriptions = [];
  const robots = [];
  for (const m of byName('meta')) {
    const name = strip(attr(m, 'name') || '').toLowerCase();
    if (name === 'description') descriptions.push(cleanText(attr(m, 'content')));
    else if (ROBOTS.has(name)) robots.push(cleanText(attr(m, 'content')).toLowerCase());
  }

  // H1s; an h1 holding only an image counts its alt text (as ARACHNIX's H1 tab does)
  const h1Raw = [];
  const h1AltOnly = [];
  for (const h of byName('h1')) {
    const text = elText(h);
    h1Raw.push(text);
    const alts = Array.from(h.getElementsByTagName('img')).map((img) => cleanText(attr(img, 'alt'))).filter(Boolean);
    if (alts.length && !text) h1AltOnly.push(alts.join(' '));
  }
  const h1 = h1Raw.map((v) => strip(v) || (h1AltOnly.length ? h1AltOnly.shift() : v));

  // <link>: canonical and hreflang
  const absolute = (href) => {
    try {
      return new URL(href, document.baseURI).href;
    } catch (e) {
      return href;
    }
  };
  const canonicals = [];
  const canonicalsRaw = [];
  const hreflang = [];
  for (const lk of byName('link')) {
    const rels = words(attr(lk, 'rel') || '').map((r) => r.toLowerCase());
    const href = strip(attr(lk, 'href') || '');
    if (!href) continue;
    if (rels.includes('canonical')) {
      canonicalsRaw.push(href);
      canonicals.push(absolute(href));
    }
    if (rels.includes('alternate') && attr(lk, 'hreflang')) {
      hreflang.push({lang: strip(attr(lk, 'hreflang')).toLowerCase(), url: absolute(href)});
    }
  }

  // Structured data types
  const truthy = (v) => Boolean(v) && !(Array.isArray(v) && !v.length)
    && !(typeof v === 'object' && !Array.isArray(v) && !Object.keys(v).length);
  const schemaType = (value) => {
    const v = strip(value === null || value === undefined ? '' : String(value));
    for (const prefix of ['https://schema.org/', 'http://schema.org/', 'schema:']) {
      if (v.toLowerCase().startsWith(prefix)) return v.slice(prefix.length);
    }
    return v;
  };
  const jsonldTypes = (data, depth = 0) => {
    const out = [];
    if (depth > 50) return out;
    for (const item of (Array.isArray(data) ? data : [data])) {
      if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
      const t = item['@type'];
      for (const v of (Array.isArray(t) ? t : [t])) if (truthy(v)) out.push(schemaType(v));
      if (truthy(item['@graph'])) out.push(...jsonldTypes(item['@graph'], depth + 1));
    }
    return out;
  };
  const parseLoose = (raw) => {                  // json.loads(strict=False): control characters allowed in strings
    try {
      return JSON.parse(raw);
    } catch (e) {
      let out = '';
      let inString = false;
      let escaped = false;
      for (const ch of raw) {
        const code = ch.codePointAt(0);
        if (inString) {
          if (escaped) escaped = false;
          else if (ch === '\\') escaped = true;
          else if (ch === '"') inString = false;
          else if (code < 0x20) {
            out += '\\u' + code.toString(16).padStart(4, '0');
            continue;
          }
        } else if (ch === '"') {
          inString = true;
        }
        out += ch;
      }
      return JSON.parse(out);
    }
  };
  const jsonld = [];
  let jsonldErrors = 0;
  for (const s of byName('script')) {
    if (strip(attr(s, 'type') || '').toLowerCase() !== 'application/ld+json') continue;
    let raw = strip(s.textContent || '');
    if (raw.startsWith('<!--')) raw = raw.slice(4);
    if (raw.endsWith('-->')) raw = raw.slice(0, -3);
    raw = strip(raw);
    if (!raw) continue;
    try {
      jsonld.push(...jsonldTypes(parseLoose(raw)));
    } catch (e) {
      jsonldErrors += 1;
    }
  }
  const microdata = [];
  const rdfa = [];
  for (const el of all) {
    if (el.hasAttribute('itemtype') && !el.hasAttribute('itemprop')) {
      for (const t of words(attr(el, 'itemtype'))) {
        microdata.push(schemaType(t.includes('/') ? t.replace(/\/+$/, '').split('/').pop() : t));
      }
    }
    if (el.hasAttribute('typeof') && !el.hasAttribute('property')) {
      for (const t of words(attr(el, 'typeof'))) {
        rdfa.push(schemaType(t.includes(':') && !t.includes('//') ? t.slice(t.indexOf(':') + 1) : t));
      }
    }
  }
  const types = [];
  for (const t of [...jsonld, ...microdata, ...rdfa]) if (t && !types.includes(t)) types.push(t);

  // Word count of the content area: <body> without nav and footer (extract.content_lines + text_stats)
  const root = document.body || document.documentElement;
  const lines = [[]];
  let listItems = 0;
  const stack = [root];
  while (stack.length) {
    const node = stack.pop();
    const last = lines[lines.length - 1];
    if (node === null) {
      if (last.length) lines.push([]);
      continue;
    }
    if (node.nodeType === 3) {
      last.push(node.data);
      continue;
    }
    if (node.nodeType !== 1) continue;
    const name = nameOf(node);
    if (HIDDEN.has(name) || EXCLUDED.has(name)) continue;
    if ((BLOCK.has(name) || name === 'br') && last.length) lines.push([]);
    if (name === 'li') listItems += 1;
    if (BLOCK.has(name)) stack.push(null);
    for (let i = node.childNodes.length - 1; i >= 0; i -= 1) stack.push(node.childNodes[i]);
  }
  let wordCount = 0;
  for (const parts of lines) wordCount += words(cleanText(parts.join(''))).length;
  if (wordCount) wordCount += listItems;

  return {
    url: location.href,
    titles, descriptions, robots, h1, canonicals, canonicalsRaw,
    hreflang: hreflang.slice(0, 200), hreflangCount: hreflang.length,
    types: types.slice(0, 50), typeCount: types.length, jsonldErrors,
    wordCount, lang: strip((document.documentElement && attr(document.documentElement, 'lang')) || ''),
  };
}
