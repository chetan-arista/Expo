// The on-page check's verdicts: one row per element, with a plain chip. Thresholds are ARACHNIX's defaults (from
// data/rules.json) and the chip follows the kind ARACHNIX gives the same finding: an Issue is "fail", a Warning or an
// Opportunity is "warn", a fine value is "pass" and a plain fact (hreflang count, data types) is "info".

/** Known robots directives in meta robots values, scoped like ARACHNIX (crawler/seo/extract.py directive_tokens):
 * "bingbot: noindex" says nothing to Google. */
export function directiveTokens(values, text) {
  const known = new Set(text.directiveTokens);
  const valueDirectives = new Set(text.valueDirectives);
  const found = [];
  const scopeHead = (token) => {
    const i = token.indexOf(':');
    if (i >= 0) {
      const head = token.slice(0, i).trim();
      const rest = token.slice(i + 1).trim();
      if (head && !valueDirectives.has(head) && !head.includes(' ') && !/^\d+$/.test(head)) return [head, rest];
    }
    return ['', token];
  };
  const forGoogle = (scope) => !scope || text.robotPrefixes.some((p) => scope.startsWith(p));
  for (const value of values) {
    let scope = '';
    for (const part of String(value || '').split(',')) {
      let token = part.trim().toLowerCase();
      if (!token) continue;
      const [head, rest] = scopeHead(token);
      if (head) {
        scope = head;
        token = rest;
        if (!token) continue;
      }
      if (!forGoogle(scope)) continue;
      if (token.includes(':')) {
        const name = token.split(':', 1)[0].trim();
        if (!valueDirectives.has(name)) continue;
        token = name;
      }
      token = token.split(/\s+/).filter(Boolean)[0] || '';
      if (known.has(token) && !found.includes(token)) found.push(token);
    }
  }
  return found;
}

function withoutHash(u) {
  try {
    const x = new URL(u);
    x.hash = '';
    return x.href;
  } catch (e) {
    return String(u || '').split('#')[0];
  }
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

/** Length / pixel findings of one text value (title or description). */
function sizeFindings(value, m, kind, min, max, minPx, maxPx) {
  const chars = m.length(value);
  const px = m.width(value, kind);
  const out = [];
  if (chars > max) out.push(`Over ${max} characters`);
  if (chars < min) out.push(`Under ${min} characters`);
  if (px > maxPx) out.push(`Over ${maxPx} px`);
  if (px < minPx) out.push(`Under ${minPx} px`);
  return {chars, px, out};
}

/** Rows for the popup from collectPage()'s data. Each row: {key, label, state, chip, value, values, facts,
 * bar, notes}. `facts` are [data-key, label, number] shown under the value. */
export function buildChecks(page, m, rules) {
  const th = rules.thresholds;
  const rows = [];
  const stored = (list) => list.slice(0, th.stored);

  // Title
  {
    const all = page.titles.map((v) => m.collapse(v));
    const present = stored(all).filter(Boolean);
    const t1 = all[0] || '';
    const row = {key: 'title', label: 'Title', value: t1, notes: []};
    if (!all.some(Boolean)) {
      Object.assign(row, {state: 'fail', chip: all.length ? 'Empty' : 'Missing',
        notes: ['Search engines then write their own headline for the result.']});
    } else {
      const f = sizeFindings(t1, m, 'title', th.titleMinChars, th.titleMaxChars, th.titleMinPx, th.titleMaxPx);
      const issues = [...f.out];
      for (const v of present.slice(1)) {
        for (const x of sizeFindings(v, m, 'title', th.titleMinChars, th.titleMaxChars, th.titleMinPx, th.titleMaxPx).out) {
          if (!issues.includes(x)) issues.push(x + ' (title 2)');
        }
      }
      row.facts = [['title_chars', 'characters', f.chars], ['title_px', 'px', f.px]];
      row.bar = {px: f.px, min: th.titleMinPx, max: th.titleMaxPx};
      if (all.length > 1) {
        Object.assign(row, {state: 'fail', chip: plural(all.length, 'title')});
        row.notes.push('Keep one <title>: search engines may pick the wrong one.');
      } else {
        row.state = issues.length ? 'warn' : 'pass';
        row.chip = issues.length ? (issues[0].startsWith('Over') ? 'Too long' : 'Too short') : 'Good';
      }
      row.notes.push(...issues);
    }
    rows.push(row);
  }

  // Meta description
  {
    const all = page.descriptions.map((v) => m.collapse(v));
    const present = stored(all).filter(Boolean);
    const d1 = all[0] || '';
    const row = {key: 'desc', label: 'Meta description', value: d1, notes: []};
    if (!all.some(Boolean)) {
      Object.assign(row, {state: 'warn', chip: all.length ? 'Empty' : 'Missing',
        notes: ['Google then builds the snippet from text on the page.']});
    } else {
      const f = sizeFindings(d1, m, 'description', th.descMinChars, th.descMaxChars, th.descMinPx, th.descMaxPx);
      const issues = [...f.out];
      for (const v of present.slice(1)) {
        for (const x of sizeFindings(v, m, 'description', th.descMinChars, th.descMaxChars, th.descMinPx, th.descMaxPx).out) {
          if (!issues.includes(x)) issues.push(x + ' (description 2)');
        }
      }
      row.facts = [['desc_chars', 'characters', f.chars], ['desc_px', 'px', f.px]];
      row.bar = {px: f.px, min: th.descMinPx, max: th.descMaxPx};
      if (all.length > 1) {
        Object.assign(row, {state: 'fail', chip: plural(all.length, 'description')});
        row.notes.push('Keep one meta description: search engines may use the wrong one.');
      } else {
        row.state = issues.length ? 'warn' : 'pass';
        row.chip = issues.length ? (issues[0].startsWith('Over') ? 'Too long' : 'Too short') : 'Good';
      }
      row.notes.push(...issues);
    }
    rows.push(row);
  }

  // H1
  {
    const all = page.h1.map((v) => m.collapse(v));
    const row = {key: 'h1', label: 'H1', value: all[0] || '', values: all.slice(1, 3), notes: [],
      facts: [['h1_count', all.length === 1 ? 'heading' : 'headings', all.length]]};
    if (!all.some(Boolean)) {
      Object.assign(row, {state: 'fail', chip: all.length ? 'Empty' : 'Missing',
        notes: ['Add one H1 that says what the page is about.']});
    } else if (all.length > 1) {
      Object.assign(row, {state: 'warn', chip: plural(all.length, 'H1')});
      row.notes.push('More than one H1: fine in HTML5, but one clear main heading is easier to read.');
    } else {
      row.state = 'pass';
      row.chip = 'Good';
    }
    const long = stored(all).filter(Boolean).some((v) => m.length(v) > th.h1MaxChars);
    if (long) {
      row.notes.push(`Over ${th.h1MaxChars} characters`);
      if (row.state === 'pass') Object.assign(row, {state: 'warn', chip: 'Too long'});
    }
    rows.push(row);
  }

  // Canonical
  {
    const list = page.canonicals;
    const unique = [...new Set(list.map(withoutHash))];
    const row = {key: 'canonical', label: 'Canonical', value: list[0] || '', notes: []};
    if (!list.length) {
      Object.assign(row, {state: 'warn', chip: 'Missing',
        notes: ['No canonical link: search engines choose the main version of this page themselves.']});
    } else if (unique.length > 1) {
      Object.assign(row, {state: 'fail', chip: 'Conflicting',
        notes: [`${list.length} canonical links point to different addresses.`]});
    } else if (list[0].includes('#')) {
      Object.assign(row, {state: 'fail', chip: 'Has #fragment',
        notes: ['A canonical address should not contain a #fragment.']});
    } else if (withoutHash(list[0]) === withoutHash(page.url)) {
      Object.assign(row, {state: 'pass', chip: 'Self-referencing'});
    } else {
      Object.assign(row, {state: 'warn', chip: 'Points elsewhere',
        notes: ['This page names another address as its main version, so it is usually left out of search results.']});
    }
    if (list.length > 1 && unique.length === 1) row.notes.push(`${list.length} canonical links (same address).`);
    if (page.canonicalsRaw.some((raw) => !/^[a-z][a-z0-9+.-]*:/i.test(raw))) {
      row.notes.push('Relative address: write the full https:// address.');
      if (row.state === 'pass') Object.assign(row, {state: 'warn', chip: 'Relative'});
    }
    rows.push(row);
  }

  // Meta robots
  {
    const tokens = directiveTokens(page.robots, rules.text);
    const noindex = tokens.includes('noindex') || tokens.includes('none');
    const nofollow = tokens.includes('nofollow') || tokens.includes('none');
    const row = {key: 'robots', label: 'Meta robots', value: page.robots.join(' | '), notes: []};
    if (noindex || nofollow) {
      const words = [noindex && 'noindex', nofollow && 'nofollow'].filter(Boolean).join(' + ');
      Object.assign(row, {state: 'warn', chip: words.charAt(0).toUpperCase() + words.slice(1)});
      if (noindex) row.notes.push('Asks search engines not to show this page.');
      if (nofollow) row.notes.push('Asks search engines not to follow its links.');
    } else {
      Object.assign(row, {state: 'pass', chip: 'Index, follow',
        value: page.robots.length ? row.value : 'No robots meta tag (index, follow is the default)'});
    }
    rows.push(row);
  }

  // Hreflang
  rows.push({key: 'hreflang', label: 'Hreflang', state: 'info',
    chip: page.hreflangCount ? plural(page.hreflangCount, 'link') : 'None',
    value: page.hreflang.slice(0, 12).map((h) => h.lang).join(', '),
    facts: [['hreflang', page.hreflangCount === 1 ? 'language link' : 'language links', page.hreflangCount]],
    notes: []});

  // Structured data
  {
    const row = {key: 'types', label: 'Structured data', value: '', types: page.types, notes: [],
      state: 'info', chip: page.typeCount ? plural(page.typeCount, 'type') : 'None found'};
    if (page.jsonldErrors) {
      Object.assign(row, {state: 'fail', chip: 'Unreadable JSON-LD'});
      row.notes.push(`${plural(page.jsonldErrors, 'JSON-LD block')} could not be read (invalid JSON).`);
    }
    rows.push(row);
  }

  // Word count
  rows.push({key: 'words', label: 'Word count', value: '',
    facts: [['words', page.wordCount === 1 ? 'word' : 'words', page.wordCount]],
    state: page.wordCount < th.lowContentWords ? 'warn' : 'pass',
    chip: page.wordCount < th.lowContentWords ? 'Low content' : 'Good',
    notes: page.wordCount < th.lowContentWords ? [`Under ${th.lowContentWords} words in the main content (menus and footer left out).`] : []});

  return rows;
}

/** {pass, warn, fail} counts for the summary line. */
export function tally(rows) {
  const out = {pass: 0, warn: 0, fail: 0, info: 0};
  for (const r of rows) out[r.state] = (out[r.state] || 0) + 1;
  return out;
}
