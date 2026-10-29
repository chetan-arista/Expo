// "Use my session in ARACHNIX": on the user's click, read the cookies this browser would send to the current site
// and hand them to the configured ARACHNIX server once (POST /api/extension/session), so ARACHNIX can crawl the
// signed-in pages. The cookie values are never shown, stored or logged here; only the configured server gets them.
//
// Which cookies: exactly the ones the browser would send to the tab's address and to its www / bare twin
// (chrome.cookies.getAll({url}) for each), never getAll({domain}), which would collect every subdomain's cookies.
// Chrome only returns a domain-wide cookie (Domain=example.com) to an extension with access to example.com itself,
// so the permission asked for names the tab's host, its twin and its parent domains down to the registrable domain,
// each as one exact host (never *.example.com). The access is given back right after the cookies are read.

/** What the session hand-off needs for a tab address: {ok, error, startUrl, host, hosts, origins, readUrls}.
 * startUrl is the page without its query and fragment (they can hold tokens). */
export function sessionPlan(tabUrl, domains) {
  let u;
  try {
    u = new URL(tabUrl);
  } catch (e) {
    return {ok: false, error: 'Open the website you are signed in to, then click the button again.'};
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    return {ok: false, error: 'This works on websites (http or https pages) only.'};
  }
  const host = u.hostname.toLowerCase();
  const twin = domains.twin(host);
  const hosts = [];
  for (const h of [host, twin, ...domains.parents(host)]) if (h && !hosts.includes(h)) hosts.push(h);
  const origins = [];
  for (const h of hosts) origins.push(`https://${h}/*`, `http://${h}/*`);
  const port = u.port ? `:${u.port}` : '';
  const readUrls = [];
  for (const x of [u.origin + u.pathname, u.origin + '/', twin ? `${u.protocol}//${twin}${port}/` : '']) {
    if (x && !readUrls.includes(x)) readUrls.push(x);
  }
  return {ok: true, error: '', startUrl: u.origin + u.pathname, host, hosts, origins, readUrls};
}

/** One cookie in the JSON export shape ARACHNIX's Paste cookies reads (crawler/cookie_import.py): the hostOnly flag
 * keeps a host-only cookie on its host. */
export function shapeCookie(c) {
  const out = {name: c.name, value: c.value, domain: c.domain, path: c.path || '/', secure: Boolean(c.secure),
    httpOnly: Boolean(c.httpOnly), hostOnly: Boolean(c.hostOnly), sameSite: c.sameSite || 'unspecified',
    session: Boolean(c.session)};
  if (!c.session && typeof c.expirationDate === 'number') out.expirationDate = c.expirationDate;
  return out;
}

/** The cookies this browser would send to the plan's addresses (de-duplicated), from the tab's cookie store. */
export async function readSiteCookies(api, tabId, plan) {
  let storeId;
  try {
    const stores = await api.cookies.getAllCookieStores();
    const store = stores.find((s) => (s.tabIds || []).includes(tabId));
    storeId = store ? store.id : undefined;
  } catch (e) {
    storeId = undefined;
  }
  const seen = new Set();
  const out = [];
  for (const url of plan.readUrls) {
    const list = await api.cookies.getAll(storeId ? {url, storeId} : {url});
    for (const c of list) {
      const key = `${c.name}\u0000${c.domain}\u0000${c.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(shapeCookie(c));
    }
  }
  return out;
}

/** The permissions to ask for, and which of them to give back afterwards (those not already held). */
export function permissionRequest(plan, held, keepOrigins = []) {
  const heldOrigins = new Set((held && held.origins) || []);
  const heldPerms = new Set((held && held.permissions) || []);
  const request = {permissions: ['cookies'], origins: plan.origins.slice()};
  const giveBack = {
    permissions: heldPerms.has('cookies') ? [] : ['cookies'],
    origins: plan.origins.filter((o) => !heldOrigins.has(o) && !keepOrigins.includes(o)),
  };
  return {request, giveBack};
}

/** Gives back the access that was only needed for this read (never throws). */
export async function giveBack(api, perms) {
  if (!perms.permissions.length && !perms.origins.length) return;
  try {
    await api.permissions.remove(perms);
  } catch (e) {
    // a permission the browser treats as required (e.g. granted by policy) cannot be removed: nothing to do
  }
}

/** Plain lines for the masked preview ARACHNIX answered with (names only: the server masks every value). */
export function describePreview(preview) {
  const p = preview || {};
  const rows = Array.isArray(p.cookies) ? p.cookies : [];
  return {
    count: Number(p.count) || rows.length,
    usable: Number(p.usable) || 0,
    names: rows.map((r) => ({name: String(r.name || ''), signin: r.kind === 'signin', expired: Boolean(r.expired),
      otherSite: Boolean(r.other_site)})),
    signinNames: rows.filter((r) => r.kind === 'signin').map((r) => String(r.name || '')),
    warnings: (Array.isArray(p.warnings) ? p.warnings : []).map(String),
    error: p.error ? String(p.error) : '',
  };
}

/** {state: pass | fail | info, text} for the sign-in test ARACHNIX ran. */
export function describeTest(test) {
  if (!test || typeof test !== 'object') return {state: 'info', text: 'Not tested.'};
  const reason = test.reason ? String(test.reason) : '';
  if (test.signed_in === true) {
    return {state: 'pass', text: 'Signed in: ARACHNIX opened a page that needs sign-in with these cookies.'
      + (reason ? ` ${reason}` : '')};
  }
  if (test.signed_in === false) {
    return {state: 'fail', text: 'Not signed in: ' + (reason || 'the page that needs sign-in did not open with these cookies.')};
  }
  return {state: 'info', text: 'Couldn\'t tell whether these cookies sign in' + (reason ? `: ${reason}` : '.')};
}

/** The dashboard address that picks up the hand-off: the id travels in the #fragment, which browsers never send
 * to a server. */
export function handoffUrl(dashboard, handoffId, startUrl) {
  return `${dashboard}/#handoff=${encodeURIComponent(handoffId)}&url=${encodeURIComponent(startUrl)}`;
}

/** "Audit this site": the dashboard with the site's home page filled in (the user presses Start there). */
export function auditUrl(dashboard, tabUrl) {
  const u = new URL(tabUrl);
  return `${dashboard}/?url=${encodeURIComponent(u.origin + '/')}`;
}
