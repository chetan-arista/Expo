// Google result pixel widths, measured exactly as ARACHNIX's crawl does (crawler/seo/text_metrics.py pixel_width):
// the hinted Roboto Regular width tables (titles at 20px, descriptions at 14px) from data/widths.json. A canvas
// font would not match: the system fonts differ from computer to computer.

function inRanges(cp, ranges) {
  let lo = 0;
  let hi = ranges.length - 1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (cp < ranges[mid][0]) hi = mid - 1;
    else if (cp > ranges[mid][1]) lo = mid + 1;
    else return true;
  }
  return false;
}

/** {width(text, kind), length(text), words(text), collapse(text)} from widths.json. */
export function makeMeasurer(data) {
  const kinds = {};
  for (const kind of ['title', 'description']) {
    const src = data[kind];
    const table = new Map();
    for (const [start, widths] of src.runs) {
      let i = 0;
      for (const ch of widths) {
        table.set(start + i, ch.codePointAt(0) - 48);
        i += 1;
      }
    }
    kinds[kind] = {table, fallback: src.fallback, em: src.em};
  }
  const space = new Set(data.space);

  /** Python's str.split(): runs of whitespace (Python's set, not JavaScript's) separate the words. */
  function words(text) {
    const out = [];
    let cur = '';
    for (const ch of String(text || '')) {
      if (space.has(ch.codePointAt(0))) {
        if (cur) out.push(cur);
        cur = '';
      } else {
        cur += ch;
      }
    }
    if (cur) out.push(cur);
    return out;
  }

  const collapse = (text) => words(text).join(' ');

  function charWidth(cp, k) {
    const w = k.table.get(cp);
    if (w !== undefined) return w;
    if (inRanges(cp, data.zero)) return 0;       // accents and invisible characters
    if (inRanges(cp, data.wide)) return k.em;    // CJK and other wide characters
    return k.fallback;
  }

  /** Width in pixels as Google would draw `text` in a result: kind "title" or "description". */
  function width(text, kind = 'title') {
    const k = kinds[kind === 'description' || kind === 'meta_description' ? 'description' : 'title'];
    let sum = 0;
    for (const ch of collapse(text)) sum += charWidth(ch.codePointAt(0), k);
    return sum;
  }

  /** Length in characters as ARACHNIX counts it (code points, like Python's len). */
  const length = (text) => [...String(text || '')].length;

  return {width, length, words, collapse};
}
