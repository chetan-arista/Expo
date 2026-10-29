// Which hosts a site's session belongs to, with the same registrable-domain rules as ARACHNIX
// (crawler/url_utils.py registrable_domain, exported to data/rules.json by extensions/build.py) and the same
// www / bare twin as ARACHNIX's Paste cookies (crawler/cookie_import.py).

/** Domain helpers from rules.json's "domains" section. */
export function makeDomains(rules) {
  const cc = new Set(rules.ccSecondLevel);
  const region2 = new Set(rules.ccRegion2);
  const jp = new Set(rules.jpRegions);
  const platform = new Set(rules.platformSuffixes);
  const hostIsSite = new Set(rules.hostIsSite);

  const isIp = (h) => /^[\d.]+$/.test(h) || h.includes(':') || h.startsWith('[');

  /** "example.co.uk" for shop.example.co.uk; an IP address or a one-label name is its own domain. */
  function registrable(host) {
    let text = String(host || '').trim().toLowerCase().replace(/\.$/, '');
    if (!text || isIp(text)) return text.replace(/^\[|\]$/g, '');
    const labels = text.split('.');
    if (labels.length < 2) return text;
    for (let k = Math.min(labels.length, 4); k > 0; k--) {
      const suffix = labels.slice(-k).join('.');
      if (hostIsSite.has(suffix)) return text;
      if (platform.has(suffix)) return labels.length > k ? labels.slice(-(k + 1)).join('.') : text;
    }
    const tld = labels[labels.length - 1];
    const second = labels[labels.length - 2];
    if (labels.length >= 3 && tld.length === 2 && /^[a-z]+$/.test(tld) && (
      cc.has(second) || (region2.has(tld) && second.length === 2 && /^[a-z]+$/.test(second))
      || (tld === 'jp' && jp.has(second)))) {
      return labels.slice(-3).join('.');
    }
    return labels.slice(-2).join('.');
  }

  /** The www / bare twin (www.example.com <-> example.com), '' for an IP, localhost or another subdomain. */
  function twin(host) {
    const h = String(host || '').toLowerCase();
    if (!h || isIp(h) || h === 'localhost') return '';
    if (h.startsWith('www.')) return h.slice(4);
    if (h.includes('.') && h === registrable(h)) return 'www.' + h;
    return '';
  }

  /** The parent domains of `host` down to its registrable domain (b.example.com and example.com for
   * a.b.example.com). A domain-wide cookie (Domain=example.com) belongs to one of them: Chrome lets an extension
   * read it only with access to that domain. */
  function parents(host) {
    const h = String(host || '').toLowerCase();
    const reg = registrable(h);
    if (!reg || reg === h || isIp(h) || !h.endsWith('.' + reg)) return [];
    const out = [];
    let rest = h;
    while (rest !== reg) {
      rest = rest.slice(rest.indexOf('.') + 1);
      out.push(rest);
    }
    return out;
  }

  return {registrable, twin, parents};
}
