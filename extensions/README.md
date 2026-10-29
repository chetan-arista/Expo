# ARACHNIX Companion (browser extension for Chrome and Edge)

> **First, check `chrome://policy` (Edge: `edge://policy`).** Company-managed browsers can block "Load unpacked"
> extensions (look for `ExtensionInstallBlocklist`, `ExtensionInstallAllowlist` or `DeveloperToolsAvailability`). If
> yours does, you can still bring a signed-in session to ARACHNIX without the extension: open ARACHNIX's sign-in dialog
> and use **Paste cookies** (Build 3). Everything else ARACHNIX does works without the extension.

ARACHNIX Companion is a small Manifest V3 extension for the SEO team. From the toolbar it gives you:

- **A quick on-page check** of the page you are looking at: title and meta description (characters, and pixel width as
  Google shows it), H1s, canonical, meta robots, hreflang links, structured data types and word count, each with a
  plain Good / To check / Problem chip. It is worked out inside your browser and nothing is sent anywhere. The numbers
  are the ones a ARACHNIX crawl gives: the pixel widths use ARACHNIX's own Roboto width tables, and the word count and
  texts are read the same way the crawler reads them.
- **Audit this site in ARACHNIX**: opens your ARACHNIX with the site's address filled in. You press Start there; the
  extension never starts a crawl by itself.
- **Use my session in ARACHNIX**: the one-click version of Paste cookies. When you are signed in to a site, it hands
  that site's cookies to your ARACHNIX server once, so ARACHNIX can crawl the signed-in pages. You see which cookies were
  sent (names only) and whether ARACHNIX could sign in with them.

## Install (Chrome or Edge)

1. Get the extension: download the zip from ARACHNIX (**Help > Browser extension**), or build it (see Build below; it
   writes `extensions/dist/arachnix-companion-<version>.zip`), and unzip it into a folder you keep (the browser loads it
   from there). From a copy of the repository you can also use the `extensions/chrome` folder as is.
2. Open `chrome://extensions` (Edge: `edge://extensions`) and switch on **Developer mode**.
3. Click **Load unpacked** and choose the folder that holds `manifest.json`.
4. Pin ARACHNIX Companion to the toolbar (the puzzle-piece menu) so it is one click away.

The options page opens by itself after installing.

## Set up

In the options page (right-click the toolbar icon > **Options**, or the **Options** button in the popup):

1. **ARACHNIX address**: the address you open ARACHNIX at, for example `http://10.10.6.56:8000`.
2. **Team access key**: only if your ARACHNIX asks for one. It is kept on this computer only (never synced to your
   Google or Microsoft account) and is never shown again; the page only says "Access key set".
3. Press **Save**. The browser asks to let the extension talk to that address: allow it.
4. Press **Test connection**. You should see "Connected" and, on a server with a key, "The access key is accepted".

**Advanced > Dashboard address** is only for a dashboard hosted somewhere else than the server (for example on
Vercel in front of Render). Appearance follows ARACHNIX's: Light, Dark or System, with Glass effects On or Off.

## Use it

Click the toolbar icon on any web page.

- The top card shows the site, a summary (how many checks are good, to check, or problems) and the two actions. The
  details follow below. The bar under the title and the description shows the pixel width; the shaded band is the
  range ARACHNIX calls good.
- The check reads the page as your browser shows it now, after JavaScript. A ARACHNIX crawl reads the HTML the server
  sends (unless "Render every page" is on), and it also checks what the popup cannot see: the HTTP status and the
  response headers (X-Robots-Tag, Link canonical / hreflang).
- Pages the browser does not let extensions read (browser settings pages, the Chrome Web Store, PDFs) show
  "Not checked".

**Use my session in ARACHNIX**, step by step:

1. Sign in to the site in this browser and open a page that needs the sign-in (for example the dashboard).
2. Click **Use my session in ARACHNIX**. The browser asks to let ARACHNIX Companion read that site's cookies: allow it.
3. The extension reads the cookies the browser would send to that page, sends them once to your ARACHNIX server, and
   gives the access back straight away.
4. You see the cookie names (never the values), which one looks like the sign-in cookie, and ARACHNIX's test: "Signed
   in", "Not signed in" (with the reason) or "Couldn't tell".
5. Click **Open in ARACHNIX**. ARACHNIX opens with the site and the session ready; press Start to crawl signed in.
   **Discard** tells ARACHNIX to forget the session instead. ARACHNIX keeps it in memory for 10 minutes at most.

## Permissions, and why

| Permission | When | Why |
|---|---|---|
| Active tab | When you click the toolbar icon | Read the page you are on for the on-page check. Only that tab, only then. |
| Scripting | With Active tab | Run the check inside that page. It reads the page, changes nothing and sends nothing. |
| Storage | Always | Keep the settings on this computer (`chrome.storage.local`, never synced). |
| Your ARACHNIX address | Asked when you press Save | Talk to your ARACHNIX server. No other address is ever contacted. |
| Cookies + that site's address | Asked each time you click "Use my session" | Read the cookies the browser would send to that one site, send them to your ARACHNIX server once, then give the access back. |

The extension asks for no website access when it is installed. The options page lists any access the browser has
given it, with a **Remove** button for each.

Why "that site's address" can name more than one host: the browser only shows an extension a cookie set for a whole
domain (for example a single sign-on cookie for `example.com`) when the extension has access to that domain itself. So
for `app.example.com` the request names `app.example.com` and `example.com` (and the `www` / bare twin of a `www`
site), each as one exact host, never `*.example.com`. The cookies read are still only the ones the browser would send
to the page you are on: cookies of other subdomains (`admin.example.com`, `shop.example.com`) are never read.

## Privacy and security

- Cookie values are never shown, stored or logged by the extension, and only your configured ARACHNIX server receives
  them. ARACHNIX answers with a masked preview (names only).
- The access key and cookies are sent only over `https://`, or over `http://` to this computer or a private network
  address (10.x, 172.16-31.x, 192.168.x, a one-word intranet name or `.local`). Over private `http://` the popup warns
  that they travel unencrypted on your network. A public `http://` address is refused before anything is read.
- The extension does not follow redirects when it talks to ARACHNIX, so a redirect can never carry the key or the
  cookies to another address. Enter the server's final address (for example its `https://` one).
- The hand-off id that links the session to the dashboard travels in the address's `#fragment`, which browsers never
  send to a server, so it does not show up in any server log. The page's query and fragment are not sent with the
  session (they can hold tokens).
- No remote code: everything the extension runs is in the package, under a strict Content Security Policy.

## Update

Download or build the new zip, unzip it over the old folder, then press the reload arrow on the extension's card in
`chrome://extensions`. The settings are kept.

## Build (developers)

```
venv/Scripts/python.exe extensions/build.py          # regenerate data + icons, check, write the zip
venv/Scripts/python.exe extensions/build.py --check  # change nothing; fail when something is out of date
```

The build exports, as data only: `chrome/data/widths.json` (the Roboto width tables and fallbacks of
`crawler/seo/text_metrics.py`) and `chrome/data/rules.json` (the text rules of `crawler/seo/extract.py`, the SEO tabs'
default thresholds and the registrable-domain rules of `crawler/url_utils.py`), so the popup and a crawl always agree.
It also makes the icons from `web/static/brand`, checks the manifest (permissions, Content Security Policy, files),
refuses remote code, runs `node --check` on every script when Node.js is installed, and writes
`dist/arachnix-companion-<version>.zip` (the same sources always give the same zip). Run it after changing any of those
ARACHNIX files, and raise `version` in `chrome/manifest.json` for a release.

Layout: `chrome/manifest.json`, `background.js` (opens the options page after installing), `popup.*`, `options.*`,
`styles.css` (ARACHNIX's design tokens and glass), `lib/` (`collect.js` runs in the page; `checks.js`, `widths.js`,
`domains.js`, `session.js`, `settings.js`, `api.js`, `theme.js` run in the extension).

The server side is ARACHNIX's `POST /api/extension/session` (with `/read` and `/delete`), which reuses the Paste cookies
preview and test, and `GET /api/extension/download`, which serves this folder as a zip.

## Notices

The width data in `chrome/data/widths.json` is measured from the Roboto font (Apache License 2.0, Google). The icons
are the ARACHNIX logo.
