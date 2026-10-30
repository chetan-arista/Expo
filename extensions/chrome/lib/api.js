// Calls to the ARACHNIX server. No cookies of the extension's own travel (credentials: 'omit'), redirects are not
// followed (a redirect could carry the access key or a cookie export to another address), nothing is cached, and
// nothing is written to the console.

/** {ok, status, data, header(name), network, timeout, redirected}. Never throws. */
export async function callApi(base, path, {method = 'GET', body = null, key = '', timeoutMs = 20000} = {}) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  const headers = {Accept: 'application/json'};
  if (body !== null) headers['Content-Type'] = 'application/json';
  if (key) headers['X-Access-Key'] = key;
  try {
    const res = await fetch(base + path, {
      method, headers, body: body === null ? undefined : JSON.stringify(body),
      credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', redirect: 'manual', signal: ctl.signal,
    });
    if (res.type === 'opaqueredirect' || (res.status >= 300 && res.status < 400)) {
      return {ok: false, status: res.status, data: null, header: () => null, network: false, timeout: false,
        redirected: true};
    }
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch (e) {
      data = null;
    }
    return {ok: res.ok, status: res.status, data, header: (n) => res.headers.get(n), network: false,
      timeout: false, redirected: false};
  } catch (e) {
    return {ok: false, status: 0, data: null, header: () => null, network: true,
      timeout: e && e.name === 'AbortError', redirected: false};
  } finally {
    clearTimeout(timer);
  }
}

/** A plain sentence for a failed call (`what`: what was being done, e.g. "send the session"). */
export function failureText(res, what, serverLabel) {
  const where = serverLabel ? ` at ${serverLabel}` : '';
  if (res.timeout) return `ARACHNIX${where} did not answer in time, so ARACHNIX Companion could not ${what}.`;
  if (res.network) {
    return `Could not reach ARACHNIX${where}. Check the address in Options, that ARACHNIX is running, and that this `
      + 'computer can reach it.';
  }
  if (res.redirected) {
    return `The server${where} answered with a redirect. Enter its final address (for example the https:// one) `
      + 'in Options.';
  }
  const detail = res.data && typeof res.data.detail === 'string' ? res.data.detail : '';
  if (res.status === 401) {
    const left = res.header('X-Access-Tries-Left');
    if (res.header('X-Access-Required')) {
      return 'ARACHNIX asks for the team access key and the saved one is missing or wrong. Open Options to enter it.'
        + (left ? ` (${left} more wrong ${left === '1' ? 'try' : 'tries'} before a short lock-out.)` : '');
    }
    return detail || `ARACHNIX refused to ${what} (401).`;
  }
  if (res.status === 429) return detail || 'Too many wrong access keys. Try again in a few minutes.';
  if (res.status === 403) {
    return detail ? `ARACHNIX refused: ${detail}` : `ARACHNIX refused to ${what} from the browser extension (403).`;
  }
  if (res.status === 404) {
    return 'This ARACHNIX server does not have the extension feature yet (update ARACHNIX), or the address in '
      + 'Options is not a ARACHNIX server. You can still use Paste cookies in ARACHNIX\'s sign-in dialog.';
  }
  if (res.status === 410) return detail || 'That hand-off has expired or was already used.';
  if (res.status === 413) return 'There are too many cookies to send. Sign out of other accounts on this site and try again.';
  if (detail) return detail;
  return `ARACHNIX could not ${what} (error ${res.status}).`;
}
