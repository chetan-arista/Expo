// ARACHNIX Companion popup: the on-page check of the current tab (computed in the tab, nothing sent anywhere),
// "Audit this site in ARACHNIX" and "Use my session in ARACHNIX". Every text from a page or from the server is put on
// screen with textContent.
import {apiBase, classifyServer, dashboardBase, loadSettings, parseServerUrl, serverLabel} from './lib/settings.js';
import {applyAppearance, el} from './lib/theme.js';
import {collectPage} from './lib/collect.js';
import {makeMeasurer} from './lib/widths.js';
import {buildChecks, tally} from './lib/checks.js';
import {makeDomains} from './lib/domains.js';
import {callApi, failureText} from './lib/api.js';
import {auditUrl, describePreview, describeTest, giveBack, handoffUrl, permissionRequest, readSiteCookies,
  sessionPlan} from './lib/session.js';

const $ = (id) => document.getElementById(id);
const state = {settings: null, tab: null, rules: null, widths: null, measure: null, domains: null, held: null,
  handoff: null, busy: false};

async function loadJson(path) {
  const res = await fetch(chrome.runtime.getURL(path));
  return res.json();
}

const isWebUrl = (u) => /^https?:\/\//i.test(u || '');
const plural = (n, one, many) => `${n} ${n === 1 ? one : (many || one + 's')}`;

/** The tab to check: the active tab of this window (the popup), or ?tab=<id> when this page is opened in a tab of
 * its own (the test harness does that, since a script cannot click the toolbar button). */
async function targetTab() {
  const q = new URLSearchParams(location.search).get('tab');
  if (q && /^\d+$/.test(q)) {
    try {
      return await chrome.tabs.get(Number(q));
    } catch (e) {
      return null;
    }
  }
  const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  return tab || null;
}

// ---------------------------------------------------------------------------------------------------------------
// On-page check
// ---------------------------------------------------------------------------------------------------------------
function showPage(tab) {
  if (!tab || !tab.url) {
    $('pageHost').textContent = 'No page to check';
    return;
  }
  try {
    const u = new URL(tab.url);
    $('pageHost').textContent = u.host || u.protocol;
    $('pagePath').textContent = isWebUrl(tab.url) ? u.pathname + u.search : '';
  } catch (e) {
    $('pageHost').textContent = tab.url;
  }
}

function notChecked(text) {
  $('summary').replaceChildren(el('span', 'chip info', 'Not checked'));
  const p = el('p', 'note', text);
  p.dataset.k = 'not_checked';
  $('checks').replaceChildren(p);
  $('checkNote').classList.add('hidden');
}

function pageRules() {
  const t = state.rules.text;
  return {blockTags: t.blockTags, hiddenTextTags: t.hiddenTextTags, contentExcludedTags: t.contentExcludedTags,
    robotsMetaNames: t.robotsMetaNames, invisible: t.invisible, space: state.widths.space};
}

function bar(b, warn) {
  const scale = b.max * 1.25;
  const pct = (v) => `${Math.max(0, Math.min(100, (v / scale) * 100)).toFixed(2)}%`;
  const wrap = el('div', 'bar');
  wrap.setAttribute('role', 'img');
  wrap.setAttribute('aria-label', `${b.px} px wide; good is ${b.min} to ${b.max} px`);
  const zone = el('span', 'zone');
  zone.style.left = pct(b.min);
  zone.style.width = pct(b.max - b.min);
  const fill = el('span', 'fill' + (warn ? ' warn' : ''));
  fill.style.width = pct(b.px);
  const t1 = el('span', 'tick');
  t1.style.left = pct(b.min);
  const t2 = el('span', 'tick');
  t2.style.left = pct(b.max);
  wrap.append(zone, fill, t1, t2);
  return wrap;
}

function renderChecks(rows) {
  const t = tally(rows);
  // a zero is a plain chip: only real findings get the warning and problem colours
  const sum = [el('span', `chip ${t.pass ? 'pass' : 'plain'}`, `${t.pass} good`),
    el('span', `chip ${t.warn ? 'warn' : 'plain'}`, `${t.warn} to check`),
    el('span', `chip ${t.fail ? 'fail' : 'plain'}`, plural(t.fail, 'problem'))];
  sum.forEach((c, i) => { c.dataset.k = ['sum_pass', 'sum_warn', 'sum_fail'][i]; });
  $('summary').replaceChildren(...sum);

  const nodes = rows.map((r) => {
    const row = el('div', 'row');
    row.dataset.check = r.key;
    row.dataset.state = r.state;
    const head = el('div', 'rowhead');
    const chip = el('span', `chip ${r.state}`, r.chip);
    chip.dataset.k = `${r.key}_chip`;
    head.append(el('span', 'label', r.label), chip);
    row.append(head);
    if (['title', 'desc', 'h1', 'canonical', 'robots'].includes(r.key) || (r.key === 'hreflang' && r.value)) {
      const v = el('div', 'value' + (r.key === 'canonical' ? ' url' : '') + (r.value ? '' : ' empty'),
        r.value || (r.key === 'canonical' ? 'None' : '(none)'));
      v.dataset.k = r.key;
      if (r.value) v.title = r.value;
      row.append(v);
    }
    (r.values || []).forEach((v, i) => row.append(el('div', 'extra', `H1 ${i + 2}: ${v || '(empty)'}`)));
    if (r.types && r.types.length) {
      const box = el('div', 'types');
      box.dataset.k = 'types';
      for (const ty of r.types) box.append(el('span', 'chip plain', ty));
      row.append(box);
    }
    if (r.facts && r.facts.length) {
      const facts = el('div', 'facts');
      for (const [key, label, n] of r.facts) {
        const span = el('span');
        const b = el('b', 'tnum', String(n));
        b.dataset.k = key;
        span.append(b, ` ${label}`);
        facts.append(span);
      }
      if (r.bar) facts.append(el('span', 'range', `good: ${r.bar.min}–${r.bar.max} px`));
      row.append(facts);
    }
    if (r.bar) row.append(bar(r.bar, r.bar.px < r.bar.min || r.bar.px > r.bar.max));
    if (r.notes && r.notes.length) {
      const ul = el('ul', 'notes');
      for (const n of r.notes) ul.append(el('li', '', n));
      row.append(ul);
    }
    return row;
  });
  $('checks').replaceChildren(...nodes);
  $('checkNote').classList.remove('hidden');
}

async function runCheck() {
  const tab = state.tab;
  if (!tab || !tab.url) {
    notChecked('Not checked: open ARACHNIX Companion on the page you want to check.');
    return;
  }
  if (!isWebUrl(tab.url)) {
    notChecked('Not checked: ARACHNIX Companion checks web pages (http:// or https://) only.');
    return;
  }
  let data = null;
  try {
    const [res] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: collectPage, args: [pageRules()]});
    data = res && res.result;
  } catch (e) {
    notChecked('Not checked: the browser does not let extensions read this page (for example the Chrome Web '
      + 'Store, a browser settings page or a PDF).');
    return;
  }
  if (!data) {
    notChecked('Not checked: the page did not answer. Reload it and open ARACHNIX Companion again.');
    return;
  }
  renderChecks(buildChecks(data, state.measure, state.rules));
}

// ---------------------------------------------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------------------------------------------
function setupActions() {
  const s = state.settings;
  const base = apiBase(s);
  const web = Boolean(state.tab && isWebUrl(state.tab.url));
  $('noServer').classList.toggle('hidden', Boolean(base));
  if (base) {
    const kind = classifyServer(s.server_url).kind;
    const tail = kind === 'private' ? ' (http: unencrypted on your network)'
      : kind === 'public-http' ? ' (public http: sessions are not sent)' : '';
    $('serverLine').textContent = `ARACHNIX: ${serverLabel(s)}${tail}`;
    $('serverLine').classList.remove('hidden');
  }
  $('auditBtn').disabled = !(base && web);
  $('sessionBtn').disabled = !(base && web);
}

function onAudit() {
  if (!state.tab || !isWebUrl(state.tab.url)) return;
  chrome.tabs.create({url: auditUrl(dashboardBase(state.settings), state.tab.url)});
}

function sessionBox(kind, text) {
  const box = $('sessionOut');
  box.classList.remove('hidden');
  box.dataset.state = kind;
  const children = [];
  if (kind === 'busy') {
    const p = el('div', 'small');
    p.append(el('span', 'spin'), text);
    children.push(p);
  } else if (text) {
    const m = el('div', `msg ${kind}`);
    const g = el('div', 'grow', text);
    g.dataset.k = 'session_msg';
    m.append(g);
    children.push(m);
  }
  box.replaceChildren(...children);
  return box;
}

function setBusy(on) {
  state.busy = on;
  $('sessionBtn').disabled = on || !apiBase(state.settings);
  $('auditBtn').disabled = on || !apiBase(state.settings);
}

function showHandoff(data, plan, safety) {
  const p = describePreview(data.preview);
  const t = describeTest(data.test);
  const s = state.settings;
  state.handoff = {id: String(data.handoff_id), startUrl: plan.startUrl};
  const kind = t.state === 'fail' || p.warnings.length ? 'warn' : 'ok';
  const box = sessionBox(kind, `ARACHNIX has your session for ${plan.host}: ${plural(p.count, 'cookie')}, `
    + `${p.usable} usable. The values stay hidden.`);
  const names = el('div', 'names');
  names.dataset.k = 'cookie_names';
  for (const n of p.names.slice(0, 40)) {
    // a word, not only a colour: expired and other-site cookies are not used by ARACHNIX
    const note = n.expired ? ' (expired)' : n.otherSite ? ' (other site)' : n.signin ? ' (sign-in)' : '';
    const kind = n.expired || n.otherSite ? 'warn' : n.signin ? 'pass' : 'plain';
    names.append(el('span', `chip ${kind}`, n.name + note));
  }
  if (p.names.length > 40) names.append(el('span', 'chip plain', `+${p.names.length - 40} more`));
  box.append(names);
  if (p.signinNames.length) {
    const line = el('div', 'small', `Looks like the sign-in cookie: ${p.signinNames.slice(0, 5).join(', ')}`);
    line.dataset.k = 'signin_line';
    box.append(line);
  }
  const test = el('div', `msg ${t.state === 'pass' ? 'ok' : t.state === 'fail' ? 'bad' : 'info'}`);
  const tg = el('div', 'grow', t.text);
  tg.dataset.k = 'test_result';
  test.append(tg);
  box.append(test);
  if (p.warnings.length) {
    const w = el('div', 'msg warn');
    const g = el('div', 'grow');
    const ul = el('ul');
    for (const x of p.warnings.slice(0, 6)) ul.append(el('li', '', x));
    g.append(ul);
    w.append(g);
    box.append(w);
  }
  if (safety.warning) {
    const w = el('div', 'msg warn');
    const g = el('div', 'grow', safety.warning);
    g.dataset.k = 'transport_warning';
    w.append(g);
    box.append(w);
  }
  const mins = Math.max(1, Math.round((Number(data.expires_in) || 600) / 60));
  box.append(el('div', 'small muted', `ARACHNIX keeps it in memory for ${plural(mins, 'minute')}, until a crawl uses it. `
    + 'Open ARACHNIX and press Start to crawl signed in.'));
  const row = el('div', 'btnrow');
  const open = el('button', 'btn btn-go', 'Open in ARACHNIX');
  open.type = 'button';
  open.id = 'openHandoff';
  const discard = el('button', 'btn', 'Discard');
  discard.type = 'button';
  discard.id = 'discardHandoff';
  row.append(open, discard);
  box.append(row);
  open.addEventListener('click', () => {
    if (!state.handoff) return;
    const url = handoffUrl(dashboardBase(s), state.handoff.id, state.handoff.startUrl);
    state.handoff = null;
    chrome.tabs.create({url});
  });
  discard.addEventListener('click', async () => {
    if (!state.handoff) return;
    const id = state.handoff.id;
    state.handoff = null;
    const res = await callApi(apiBase(s), '/api/extension/session/delete', {method: 'POST', key: s.access_key,
      body: {handoff_id: id}});
    if (res.ok) sessionBox('ok', 'Discarded: ARACHNIX no longer holds this session.');
    else sessionBox('bad', failureText(res, 'discard the session', serverLabel(s)));
  });
}

/** "Use my session in ARACHNIX". The permission request comes first, while the click still counts as the user's
 * gesture; everything it needs was loaded when the popup opened. */
function onSession() {
  if (state.busy || !state.tab) return;
  const s = state.settings;
  const safety = classifyServer(s.server_url);
  if (!safety.sendSecrets) {
    sessionBox('bad', safety.refusal);
    return;
  }
  const plan = sessionPlan(state.tab.url, state.domains);
  if (!plan.ok) {
    sessionBox('bad', plan.error);
    return;
  }
  const server = parseServerUrl(s.server_url);
  const perms = permissionRequest(plan, state.held, [server.pattern]);
  setBusy(true);
  sessionBox('busy', `Asking Chrome for access to ${plan.host}'s cookies...`);
  chrome.permissions.request(perms.request).then(async (granted) => {
    if (!granted) {
      sessionBox('warn', 'Nothing was sent: the browser did not allow reading this site\'s cookies.');
      return;
    }
    let cookies = [];
    try {
      sessionBox('busy', 'Reading this site\'s cookies...');
      cookies = await readSiteCookies(chrome, state.tab.id, plan);
    } catch (e) {
      sessionBox('bad', 'Could not read this site\'s cookies. Nothing was sent.');
      return;
    } finally {
      await giveBack(chrome, perms.giveBack);
      try {
        state.held = await chrome.permissions.getAll();
      } catch (e) {
        state.held = null;
      }
    }
    if (!cookies.length) {
      sessionBox('warn', `This browser has no cookies for ${plan.host}. Sign in to the site here first, then try `
        + 'again. Nothing was sent.');
      return;
    }
    sessionBox('busy', `Sending ${plural(cookies.length, 'cookie')} to ARACHNIX and testing the sign-in...`);
    const body = {cookies, start_url: plan.startUrl, user_agent: navigator.userAgent, test: true};
    cookies = null;
    const res = await callApi(apiBase(s), '/api/extension/session', {method: 'POST', key: s.access_key, body,
      timeoutMs: 90000});
    body.cookies = null;
    if (!res.ok || !res.data || !res.data.handoff_id) {
      sessionBox('bad', failureText(res, 'receive the session', serverLabel(s)));
      return;
    }
    const preview = describePreview(res.data.preview);
    if (!preview.usable) {
      await callApi(apiBase(s), '/api/extension/session/delete', {method: 'POST', key: s.access_key,
        body: {handoff_id: String(res.data.handoff_id)}});
      sessionBox('bad', 'ARACHNIX could not use any of these cookies' + (preview.error ? `: ${preview.error}` : '.')
        + ' Nothing was kept.');
      return;
    }
    showHandoff(res.data, plan, safety);
  }).catch(() => {
    sessionBox('bad', 'Something went wrong before anything was sent. Close this window and try again.');
  }).finally(() => setBusy(false));
}

async function init() {
  $('optBtn').addEventListener('click', () => chrome.runtime.openOptionsPage());
  $('setupBtn').addEventListener('click', () => chrome.runtime.openOptionsPage());
  $('auditBtn').addEventListener('click', onAudit);
  $('sessionBtn').addEventListener('click', onSession);
  const [settings, rules, widths] = await Promise.all([loadSettings(), loadJson('data/rules.json'),
    loadJson('data/widths.json')]);
  state.settings = settings;
  applyAppearance(settings);
  state.rules = rules;
  state.widths = widths;
  state.measure = makeMeasurer(widths);
  state.domains = makeDomains(rules.domains);
  state.tab = await targetTab();
  showPage(state.tab);
  setupActions();
  try {
    state.held = await chrome.permissions.getAll();
  } catch (e) {
    state.held = null;
  }
  await runCheck();
  document.body.dataset.ready = '1';
}

init();
