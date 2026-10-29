// Settings of ARACHNIX Companion and the rules for where it may send the access key and cookies.
// Everything is kept in chrome.storage.local (this computer only, never chrome.storage.sync). The access key is
// written once and never read back into a page: the options page only says whether one is set.

export const DEFAULTS = Object.freeze({
  server_url: '',       // the ARACHNIX server (API and, unless dashboard_url is set, the dashboard)
  dashboard_url: '',    // only when the dashboard is hosted somewhere else (e.g. Vercel in front of Render)
  access_key: '',       // the team access key (X-Access-Key); '' when the server does not ask for one
  theme: 'system',      // system | light | dark
  glass: 'on',          // on | off
});
const KEYS = Object.keys(DEFAULTS);

/** The stored settings, with defaults filled in. */
export async function loadSettings(storage = chrome.storage.local) {
  const got = await storage.get(KEYS);
  const out = {...DEFAULTS};
  for (const k of KEYS) if (typeof got[k] === 'string') out[k] = got[k];
  return out;
}

/** Saves only the given keys (an empty access_key removes the key). */
export async function saveSettings(values, storage = chrome.storage.local) {
  const clean = {};
  for (const k of KEYS) if (k in values) clean[k] = String(values[k] ?? '');
  await storage.set(clean);
}

/** What a page may show: never the key itself. */
export function publicView(settings) {
  return {server_url: settings.server_url, dashboard_url: settings.dashboard_url,
    key_set: Boolean(settings.access_key), theme: settings.theme, glass: settings.glass};
}

// ---------------------------------------------------------------------------------------------------------------
// Addresses
// ---------------------------------------------------------------------------------------------------------------
const PRIVATE_SUFFIXES = ['.local', '.lan', '.home.arpa', '.internal', '.intranet', '.corp', '.localdomain'];

function ipv4(host) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(host);
  if (!m) return null;
  const parts = m.slice(1).map(Number);
  return parts.every((n) => n <= 255) ? parts : null;
}

function bare(host) {
  return String(host || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
}

/** localhost, *.localhost, 127.0.0.0/8 or ::1. */
export function isLoopback(host) {
  const h = bare(host);
  if (h === 'localhost' || h.endsWith('.localhost') || h === '::1') return true;
  const v4 = ipv4(h);
  return Boolean(v4 && v4[0] === 127);
}

/** A private-network address or name: 10/8, 172.16/12, 192.168/16, 169.254/16 (link-local), 100.64/10 (carrier-grade
 * NAT, used by VPNs), fc00::/7, fe80::/10, a one-label name (an intranet host such as "seo-server") or a name under
 * .local / .lan / .home.arpa / .internal / .intranet / .corp / .localdomain. */
export function isPrivateHost(host) {
  const h = bare(host);
  if (!h) return false;
  const v4 = ipv4(h);
  if (v4) {
    const [a, b] = v4;
    return a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 169 && b === 254)
      || (a === 100 && b >= 64 && b <= 127);
  }
  if (h.includes(':')) return /^f[cd][0-9a-f]{0,2}:/.test(h) || /^fe[89ab][0-9a-f]?:/.test(h);
  if (!h.includes('.')) return true;
  return PRIVATE_SUFFIXES.some((s) => h.endsWith(s));
}

/** {ok, url, origin, host, pattern, error}: a typed server address made canonical (scheme added when missing:
 * http:// for this computer and private addresses, https:// otherwise), without query, fragment or trailing slash.
 * `pattern` is the host permission that lets the extension reach it (any port). */
export function parseServerUrl(input) {
  let text = String(input || '').trim();
  if (!text) return {ok: false, error: 'Enter the address of your ARACHNIX server.'};
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) {
    const host = text.split(/[/:?#]/)[0];
    text = (isLoopback(host) || isPrivateHost(host) || ipv4(host) ? 'http://' : 'https://') + text;
  }
  let u;
  try {
    u = new URL(text);
  } catch (e) {
    return {ok: false, error: 'That is not a web address. Example: http://10.10.6.56:8000'};
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    return {ok: false, error: 'The address must start with http:// or https://'};
  }
  if (u.username || u.password) {
    return {ok: false, error: 'Leave the user name and password out of the address.'};
  }
  const path = u.pathname.replace(/\/+$/, '');
  return {ok: true, url: u.origin + path, origin: u.origin, host: u.hostname,
    pattern: `${u.protocol}//${u.hostname}/*`, error: ''};
}

/** Whether secrets (the access key, cookies) may travel to this server:
 * {kind: https | loopback | private | public-http | invalid, sendSecrets, warning, refusal}. */
export function classifyServer(serverUrl) {
  const p = parseServerUrl(serverUrl);
  if (!p.ok) return {kind: 'invalid', sendSecrets: false, warning: '', refusal: p.error};
  if (p.url.startsWith('https://')) return {kind: 'https', sendSecrets: true, warning: '', refusal: ''};
  if (isLoopback(p.host)) return {kind: 'loopback', sendSecrets: true, warning: '', refusal: ''};
  if (isPrivateHost(p.host)) {
    return {kind: 'private', sendSecrets: true, refusal: '',
      warning: 'This server uses http://, so what you send travels unencrypted over your network. Use it only '
        + 'on a network you trust.'};
  }
  return {kind: 'public-http', sendSecrets: false, warning: '',
    refusal: 'This server is a public address on plain http://. ARACHNIX Companion sends the access key and '
      + 'sign-in cookies only over https://, or over http:// to this computer or a private network address '
      + '(such as 10.x.x.x or 192.168.x.x). Use the https:// address of your ARACHNIX.'};
}

/** The API base of the configured server ('' when none). */
export function apiBase(settings) {
  const p = parseServerUrl(settings.server_url);
  return p.ok ? p.url : '';
}

/** Where the dashboard opens: dashboard_url when set, else the server itself. */
export function dashboardBase(settings) {
  const d = parseServerUrl(settings.dashboard_url);
  return d.ok ? d.url : apiBase(settings);
}

/** "10.10.6.56:8000" for showing which server is configured. */
export function serverLabel(settings) {
  const p = parseServerUrl(settings.server_url);
  if (!p.ok) return '';
  return p.url.replace(/^https?:\/\//, '');
}
