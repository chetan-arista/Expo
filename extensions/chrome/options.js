// ARACHNIX Companion options: the ARACHNIX server, the team access key (stored on this computer only and never shown
// again), appearance, and the access the browser has given the extension (with a way to take it back).
import {apiBase, classifyServer, loadSettings, parseServerUrl, saveSettings, serverLabel} from './lib/settings.js';
import {applyAppearance, el} from './lib/theme.js';
import {callApi, failureText} from './lib/api.js';

const $ = (id) => document.getElementById(id);
const state = {settings: null, held: {origins: [], permissions: []}};

/** Status lines: [[ok | warn | bad | info | busy, text], ...] */
function status(items) {
  const nodes = items.map(([kind, text]) => {
    if (kind === 'busy') {
      const p = el('div', 'small muted', text);
      p.dataset.kind = 'busy';
      return p;
    }
    const m = el('div', `msg ${kind}`);
    m.dataset.kind = kind;
    m.append(el('div', 'grow', text));
    return m;
  });
  $('status').replaceChildren(...nodes);
}

function pressSeg(segId, value) {
  for (const b of $(segId).querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.v === value));
}

async function refreshGrants() {
  try {
    state.held = await chrome.permissions.getAll();
  } catch (e) {
    state.held = {origins: [], permissions: []};
  }
  const server = parseServerUrl(state.settings.server_url);
  const rows = [];
  const add = (label, detail, perms) => {
    const row = el('div', 'grant');
    const left = el('div');
    left.append(el('div', 'mono', label), el('div', 'small muted', detail));
    const btn = el('button', 'btn btn-sm', 'Remove');
    btn.type = 'button';
    btn.addEventListener('click', async () => {
      try {
        await chrome.permissions.remove(perms);
      } catch (e) {
        // required by policy or already gone
      }
      await refreshGrants();
    });
    row.append(left, btn);
    rows.push(row);
  };
  const hostOf = (pattern) => (/^[^:]+:\/\/([^/]+)\//.exec(pattern) || [])[1] || '';
  for (const o of state.held.origins || []) {
    const isServer = server.ok && hostOf(o).toLowerCase() === server.host.toLowerCase();
    add(o, isServer ? 'Your ARACHNIX server' : 'A site address (normally given back right after use)', {origins: [o]});
  }
  if ((state.held.permissions || []).includes('cookies')) {
    add('Cookies', 'Normally given back right after "Use my session in ARACHNIX"', {permissions: ['cookies']});
  }
  if (!rows.length) rows.push(el('div', 'muted', 'None at the moment.'));
  $('grants').replaceChildren(...rows);
}

async function refresh() {
  state.settings = await loadSettings();
  const s = state.settings;
  applyAppearance(s);
  $('server').value = s.server_url;
  $('dash').value = s.dashboard_url;
  $('key').value = '';
  const keySet = Boolean(s.access_key);
  $('keyState').classList.toggle('hidden', !keySet);
  $('key').placeholder = keySet ? 'Set. Type a new key to replace it.' : 'Leave empty if ARACHNIX does not ask for one';
  pressSeg('themeSeg', s.theme);
  pressSeg('glassSeg', s.glass);
  await refreshGrants();
}

function onSave(ev) {
  ev.preventDefault();
  const p = parseServerUrl($('server').value);
  if (!p.ok) {
    status([['bad', p.error]]);
    return;
  }
  const dashText = $('dash').value.trim();
  const d = dashText ? parseServerUrl(dashText) : {ok: true, url: ''};
  if (!d.ok) {
    status([['bad', `Dashboard address: ${d.error}`]]);
    return;
  }
  const keyTyped = $('key').value.trim();
  if (/[^\x20-\x7e]/.test(keyTyped)) {
    status([['bad', 'The access key can hold plain letters, digits and symbols only (it travels in a request header). '
      + 'Copy it again from whoever runs your ARACHNIX.']]);
    return;
  }
  const old = parseServerUrl(state.settings.server_url);
  // Asked first, while the click still counts as the user's gesture
  const ask = (state.held.origins || []).includes(p.pattern) ? Promise.resolve(true)
    : chrome.permissions.request({origins: [p.pattern]});
  ask.catch(() => false).then(async (granted) => {
    const values = {server_url: p.url, dashboard_url: d.url};
    if (keyTyped) values.access_key = keyTyped;
    await saveSettings(values);
    $('key').value = '';
    if (old.ok && old.pattern !== p.pattern) {
      try {
        await chrome.permissions.remove({origins: [old.pattern]});
      } catch (e) {
        // nothing to give back
      }
    }
    await refresh();
    const msgs = [['ok', `Saved. ARACHNIX Companion uses ARACHNIX at ${serverLabel(state.settings)}.`
      + (keyTyped ? ' The access key is stored on this computer.' : '')]];
    if (!granted) {
      msgs.push(['warn', 'The browser did not give access to this address. ARACHNIX Companion can still reach it when '
        + 'the server lets other sites call it (CORS); if a test fails, press Save again and allow access.']);
    }
    const safety = classifyServer(p.url);
    if (safety.refusal) msgs.push(['warn', safety.refusal]);
    else if (safety.warning) msgs.push(['warn', safety.warning]);
    status(msgs);
  });
}

async function onTest() {
  const typed = $('server').value.trim();
  const p = parseServerUrl(typed || state.settings.server_url);
  if (!p.ok) {
    status([['bad', p.error]]);
    return;
  }
  const label = p.url.replace(/^https?:\/\//, '');
  const keyTyped = $('key').value.trim();
  if (/[^\x20-\x7e]/.test(keyTyped)) {
    status([['bad', 'The access key can hold plain letters, digits and symbols only (it travels in a request header).']]);
    return;
  }
  // the saved key goes only to the saved server, never to an address typed but not saved
  const key = keyTyped || (p.url === apiBase(state.settings) ? state.settings.access_key : '');
  const safety = classifyServer(p.url);
  status([['busy', `Testing ${label}...`]]);
  const health = await callApi(p.url, '/api/health', {timeoutMs: 10000});
  if (!health.ok || !health.data || health.data.status !== 'ok') {
    const text = health.ok || health.status === 404
      ? `${label} answered, but it does not look like a ARACHNIX server. Check the address (the one you open ARACHNIX at).`
      : failureText(health, 'connect', label);
    status([['bad', text]]);
    return;
  }
  const msgs = [['ok', `Connected: ARACHNIX is running at ${label}.`]];
  const sendKey = safety.sendSecrets ? key : '';
  const access = await callApi(p.url, '/api/access', {key: sendKey, timeoutMs: 10000});
  if (access.ok && access.data) {
    if (!access.data.required) {
      msgs.push(['ok', 'This server does not ask for an access key.' + (key ? ' The saved key is not needed.' : '')]);
    } else if (access.data.valid) {
      msgs.push(['ok', 'The access key is accepted.']);
    } else if (key && !sendKey) {
      msgs.push(['warn', 'This server asks for the access key, but ARACHNIX Companion will not send it over plain '
        + 'http:// to a public address.']);
    } else {
      msgs.push(['warn', 'This server asks for the team access key. Enter it above and press Save.']);
    }
  } else if (access.status === 401) {
    const left = access.header('X-Access-Tries-Left');
    msgs.push(['bad', 'The access key is wrong.' + (left ? ` ${left} more wrong ${left === '1' ? 'try' : 'tries'} `
      + 'before a short lock-out.' : '')]);
  } else {
    msgs.push(['bad', failureText(access, 'check the access key', label)]);
  }
  if (safety.refusal) msgs.push(['warn', safety.refusal]);
  else if (safety.warning) msgs.push(['warn', safety.warning]);
  status(msgs);
}

function bindSeg(segId, key) {
  $(segId).addEventListener('click', async (ev) => {
    const b = ev.target.closest('button[data-v]');
    if (!b) return;
    await saveSettings({[key]: b.dataset.v});
    state.settings = await loadSettings();
    applyAppearance(state.settings);
    pressSeg(segId, b.dataset.v);
  });
}

async function init() {
  $('version').textContent = `Version ${chrome.runtime.getManifest().version}`;
  $('serverForm').addEventListener('submit', onSave);
  $('test').addEventListener('click', onTest);
  $('keyClear').addEventListener('click', async () => {
    await saveSettings({access_key: ''});
    await refresh();
    status([['ok', 'The access key was removed from this computer.']]);
  });
  bindSeg('themeSeg', 'theme');
  bindSeg('glassSeg', 'glass');
  await refresh();
  document.body.dataset.ready = '1';
}

init();
