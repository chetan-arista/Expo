# ARACHNIX

**ARACHNIX** (formerly *SlickUP*) is the internal SEO tool of Arista Systems' SEO team. It crawls a website the way
**Screaming Frog SEO Spider** does (the same menus, tabs, filters, thresholds, issue types and priorities) and goes
further: pictures that point at each problem, live re-checks of every reported error, a fix plan with platform steps,
import-ready fix files, developer tickets, saved crawls with comparisons, scheduled crawls with email, signed-in crawls
(also behind bot checks), optional Google connections and an optional free AI layer. Built for the US e-commerce brands
Arista supports on **Shopify, WordPress / WooCommerce and Magento**.

- **Version 1.0** (28 October 2026): what is in it, **[RELEASE_NOTES_1.0.md](RELEASE_NOTES_1.0.md)**; what comes next, **[ROADMAP_2.0.md](ROADMAP_2.0.md)**; one-page overview, **[ARACHNIX-Feature-Guide.pdf](ARACHNIX-Feature-Guide.pdf)**
- Full guide (every feature, menu, setting, API, environment variable): **[DOCUMENTATION.md](DOCUMENTATION.md)**, also in the dashboard under **Help > User Guide**
- Running it on an office PC, Render (backend) and Vercel (dashboard): **[DEPLOY.md](DEPLOY.md)**

**Product rules:** visual first and plain language (details behind "more info"); a URL's category comes from its HTTP
status only and every count adds up (a 404 is always *Broken 4xx*); a check that did not run is *Not checked* and a
Google source that is not connected is *Not connected*, never a pass or a 0; limits never cut a crawl silently; the page
a URL was found on is called **Parent link** everywhere; AMP is deprecated and not shown; passwords, cookies, tokens and
API keys are never shown, logged or exported.

---

## Features

**Crawl**
- Every internal URL: pages, redirects (each hop is its own row), errors, images, scripts, stylesheets and files; XML sitemaps and robots.txt (Google's matching rules); external links checked once each, never crawled.
- **Modes:** Spider (follow links), **List** (only the URLs you upload or paste: .txt, .csv, .xlsx, a sitemap), **SERP** (title and description lengths and pixel widths, no crawling) and **Compare** (two saved crawls).
- **Crawl scope:** Subdomain, Subfolder, All Subdomains or Exact URL. **Find subdomains** looks for other hosts of the site (links, sitemaps, robots.txt, public certificate logs, Search Console, hosts you type) and crawls the ones you tick; after the crawl a **Subdomains** list explains every host that was not crawled (outside the scope, not linked, needs sign-in, no DNS...).
- **Crawl Config:** 1-50 parallel requests, speed limit, User-Agent presets (Chrome, Googlebot, Bingbot and AI crawlers), extra HTTP headers, Include / Exclude with a URL tester, **URL Rewriting**, Max URLs / depth, per-page limits, crawl-trap and endless-loop protection, firewall and 429/503 pacing. Anything left out is counted and named ("Partial crawl"). **Profiles** save and share configurations.
- **Custom Search, Custom Extraction** (XPath, CSS, regex) and **Custom JavaScript**, up to 10 each.
- **JavaScript sites:** routes read from the site's scripts, app pages rendered in headless Chrome with click-only menus opened, and **Render every page in Chrome** (JavaScript tab, JavaScript redirects, Chrome console log, Rendered Page and View Source, rendered mobile and accessibility checks).

**Signed-in crawls**
- Four methods: email + password (HTTP or in Chrome), a live **browser window** you drive from the dashboard (one-time codes, SSO), **Paste cookies** and a header token. **Test login** before crawling.
- **Paste cookies** with a step-by-step guide for Chrome / Edge and Firefox (cookies table, Cookie header or Copy as cURL); one box reads any format (header, JSON export, cookies.txt, cURL, PowerShell), shows a masked preview, offers the pasted browser User-Agent, and **Test these cookies** says "Signed in" / "Not signed in" / "Couldn't tell".
- **Bot checks:** ARACHNIX never tries to pass a CAPTCHA or challenge. It says so plainly and shows the routes that work: your own browser's cookies, or (site owner) an allow rule for the office IP or a secret header it can create for you.
- A login counts only when a members-only page opens. Sign-out and data-changing links are never opened; an ended session is renewed automatically or the affected pages are marked "Logged out here". **Login guard** finds members-only pages open to everyone.

**SEO audit**
- 36 tabs: Internal, External, Security, Response Codes, URL, Page Titles, Meta Description, Meta Keywords, H1, H2, Content, Images, Canonicals, Pagination, Directives, Hreflang, JavaScript, Links, Structured Data, Sitemaps, PageSpeed, Mobile, Accessibility, Custom Search, Custom Extraction, Custom JavaScript, Analytics, Search Console, Validation, plus our own AI Crawlers, Robots.txt, Hygiene, Parameters, Performance, Social and Third Parties.
- Several filters at once (Any / All), segment and Internal / External scopes, directory tree view, column chooser, grid and detail exports (CSV or Excel), Issues overview with what each issue means and how to fix it, **Passed / Failed / Not checked** for every check, and a URL details pane (inlinks, outlinks, images, resources, SERP snippet, Rendered Page, Chrome Console Log, View Source, headers, cookies, duplicates, Lighthouse, link source).
- **Crawl Analysis** (Link Score, sitemaps and orphans, near duplicates, semantic similarity, pagination, hreflang, robots.txt audit, search / AI bot check) with Start / Stop / Configure.

**Beyond Screaming Frog**
- **Pinpointed evidence:** screenshots with the broken link or the SEO problem boxed on the page, its position, selector and HTML (cookie and newsletter pop-ups hidden first).
- **Validation agent:** every reported error re-checked live (3 attempts, real browser when only bots are refused): confirmed, not reproduced, intermittent or fixed. **Recheck selected URLs** updates the crawl.
- **Fix plan:** issues grouped by root cause (template, plugin, CMS setting, server, content, links), ranked fix-first by impact and effort (pages with Google traffic first when connected), with platform-specific steps for the detected CMS.
- **Content insights** (pages whose address promises one thing while the page delivers another, thin pages, off-topic pages) and **Business insights** (company profile, offerings, market, competitors, contacts, tech stack, SEO snapshot, each claim with its source; own PDF).

**Visuals**
- **Site graph** (By depth or Clusters): size = link strength, colour and shape = HTTP status, names that never cover a node or another name, find, zoom, full screen.
- **Drill-down tree** (domain > hosts > folders > pages) with status bars per node, search, expand / collapse, "show in grid", PNG / SVG download; counts always add up.
- Crawl Tree, Directory Tree, Force-Directed Directory Tree, anchor-text and body-text word clouds; right panel Site Structure, Segments and Response Times.

**Saved crawls and tracking**
- Every finished crawl saved on the server (**File > Crawls**): open, rename, projects, lock, duplicate, delete, export / import as .zip, retention rules.
- **Compare** two crawls: per filter Added / New / Removed / Missing, new / fixed / regressed issues, change detection (titles, descriptions, h1, word count, depth, inlinks, indexability, status, canonical, structured data, content) and URL mapping for migrations.
- **Segments** (named URL groups by rules; first match wins, counts add up), **dismissed issues** per site.
- **Scheduling:** crawls that start by themselves (once, hourly, daily, weekly, monthly), build their exports, compare with the previous crawl and **email** the team through your SMTP server.

**Google connections (optional)**
- **Search Console** (clicks, impressions, CTR, position; URL Inspection index status), **Google Analytics 4** (sessions, engagement, key events, revenue) and **PageSpeed Insights** (Core Web Vitals and 20 Lighthouse filters) under Configuration > API Access. Not connected: the tabs say so, never 0. Tokens and keys stay on the server.

**Reports and exports**
- **Excel** workbook (summary, issues, fix plan, validation, checks, every URL, broken links with every Parent link, one sheet per SEO tab) or one category at a time.
- **Website Audit Report PDF:** compact and fast, every finding on one line, fix-first summary, pinpointed pictures, Validation, Fix plan and Google data.
- **30 reports** (Crawl Overview, Issues Overview, redirect and canonical chains, hreflang, orphan pages, insecure content, structured data, SERP Summary, HTTP headers, cookies, segments...), **Bulk Export** for every tab and filter (URLs and inlinks), links, headers, cookies, page source, and **Multi-Export**.
- **XML and image sitemaps** built from the crawl (split at 50,000 URLs with an index).
- **Fix files** ready to import: Yoast, Rank Math, Shopify (.zip), Apache .htaccess, Next.js redirects, CSV. **Developer tickets:** CSV, Jira, Markdown, or a .zip with the screenshots.

**Dashboard**
- Screaming Frog's menu bar (File, View, Mode, Configuration, Bulk Export, Reports, Sitemaps, Visualisations, Crawl Analysis, Help), a visual strip with tiles that add up, banners that say what to do, a resizable and collapsible right panel, a status bar with URL/s and progress, keyboard shortcuts, a phone layout.
- **Theme** Light / Dark / System default, accent colour, Focus Mode and **Glass effects** (On / Reduced / Off: translucent, pointer-lit buttons, menus and dialogs; content stays solid) under View and File > Settings > User Interface.
- **File > Settings:** Proxy, Crawl Retention, Storage, Memory, Embedded Browser, Notifications (SMTP), Access Key.

**AI (optional, free)**
- Google Gemini or Groq free tiers (OpenRouter, local Ollama and Anthropic also supported). Without a key the tool runs fully rule-based ("AI off - site facts only"). Text from signed-in crawls is not sent to free cloud AI.

---

## Quick start (Windows)

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe run.py --port 8000
```

Open http://127.0.0.1:8000, enter a website address and press **Start**. Chrome must be installed for screenshots,
browser logins and JavaScript rendering. To share it on the office network start it with
`run.py --host 0.0.0.0 --port 8000` (it prints the address to share) and set `CRAWLER_REPORTS_DIR` to a lasting folder
so saved crawls, schedules and settings survive (DEPLOY.md, section 1).

**Free AI (optional):** create a key at Google AI Studio (`GEMINI_API_KEY`) and/or Groq (`GROQ_API_KEY`), set them as
Windows **user** environment variables, open a new terminal and start `run.py` again.
`http://127.0.0.1:8000/api/ai/status` shows whether AI is on.

**Google data and email (optional):** Search Console / GA4 need a Google OAuth client (`GOOGLE_OAUTH_CLIENT_ID`,
`GOOGLE_OAUTH_CLIENT_SECRET`), PageSpeed a free key (`PSI_API_KEY`), email an SMTP server (`CRAWLER_SMTP_*`, or
Settings > Notifications). Steps: DEPLOY.md, sections 2.6 and 2.7.

**Command line:**

```powershell
.\venv\Scripts\python.exe cli.py https://example.com/ --max-urls 500 --pdf
.\venv\Scripts\python.exe cli.py https://example.com/ --username me@example.com --password - --validate --intel
.\venv\Scripts\python.exe cli.py https://app.example.com/ --cookies cookies.txt --test-cookies /dashboard
```

`--password -` reads the password from the `CRAWLER_PASSWORD` environment variable. All flags (List mode, reports,
sitemaps, subdomains, saved crawls...): [DOCUMENTATION.md, section 23](DOCUMENTATION.md#23-local-development-cli-and-tests).

---

## Hosting & Deployment

Full step-by-step guide, checks and troubleshooting: **[DEPLOY.md](DEPLOY.md)**.

| Part | Where | Files |
| --- | --- | --- |
| Backend (crawler, headless Chromium for password logins and screenshots, reports, scheduler, AI) | An office PC (`run.py --host 0.0.0.0`), or **Render**, Docker web service, **Standard** instance (2 GB RAM; Chrome does not fit in 512 MB), exactly 1 instance | `run.py`, `Dockerfile`, `.dockerignore`, `render.yaml` |
| Dashboard (static `index.html` + `config.js`) | Served by the backend itself, or **Vercel**, Root Directory `frontend`, preset Other, no build | `frontend/`, `vercel.json`, `.vercelignore`, `config.js` |

- **Backend on Render:** switch the existing service to the Docker runtime (keeps its address), or create it from the `render.yaml` Blueprint. Health check: `/healthz`. Set `MAX_CHROME_INSTANCES=2`; AI keys, Google and SMTP settings are optional.
- **Keep your data:** saved crawls, history, schedules, settings, profiles, segments and Google connections live in `CRAWLER_REPORTS_DIR`. On Render add a persistent disk at `/var/data` (DEPLOY.md 2.4); without it they are lost at every deploy.
- **Scheduled crawls** run only while the server is awake: use an always-on instance (not Render's free tier) or keep the office PC from sleeping (DEPLOY.md 2.8).
- **Big sites:** the server keeps every crawled URL in memory. `render.yaml` caps a crawl at 2,500 URLs on Standard (`CRAWLER_MAX_URLS`) and a memory guard (`CRAWLER_MIN_FREE_MB`) stops queuing new URLs before the server runs out of memory; both say so in the report. For bigger sites, crawl one section at a time with an Include pattern.
- **Dashboard on Vercel:** `config.js` and `frontend/config.js` hold the backend address (`window.CRAWLER_API_BASE_URL`). Vercel serves static files only; the backend never runs there, because crawls take minutes and need Chrome.
- **Access key:** on any public backend set `CRAWLER_ACCESS_KEY` (team key for every API call; the dashboard asks for it once) and `CRAWLER_CORS_ORIGINS` (the Vercel address). Steps in DEPLOY.md 2.3.
- **Check:** `https://<backend>/healthz`, `/api/environment` and `/api/ai/status` (commands in DEPLOY.md).
- **Dashboard edits:** `index.html` must be copied to `frontend/index.html` and `web/static/index.html` (the three stay identical).
