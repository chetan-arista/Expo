# ARACHNIX: User Guide and Technical Documentation

**Version 1.0** (28 October 2026). Release notes: [RELEASE_NOTES_1.0.md](RELEASE_NOTES_1.0.md); planned for 2.0: [ROADMAP_2.0.md](ROADMAP_2.0.md).

**ARACHNIX** is the internal SEO tool of Arista Systems' SEO team. It crawls a website the
way Screaming Frog SEO Spider does (the same menus, tabs, filters, thresholds, issue types and priorities) and goes
further: pictures that point at each problem, live re-checks of every reported error, root-cause fix plans with
platform steps, import-ready fix files, developer tickets, saved crawls with comparisons, scheduled crawls with email,
signed-in crawls (also behind bot checks, through your own browser's cookies), optional Google connections (Search
Console, Analytics 4, PageSpeed Insights) and an optional free AI layer.

Where ARACHNIX runs:

- **Office server:** a PC on the office network runs `run.py --host 0.0.0.0 --port 8000`; everyone opens its address in the browser (for example http://10.10.6.56:8000).
- **Hosted:** the dashboard on Vercel (`frontend/index.html` + `frontend/config.js`) and the backend on Render (`web/app.py` + `crawler/`, Docker). Steps in DEPLOY.md.
- **Your own PC:** `venv\Scripts\python.exe run.py --port 8000`, then open http://127.0.0.1:8000.

This file is also the dashboard's **Help > User Guide** (with a search box). Sections 1 to 6 explain the screen and
the menus; the later sections go into each feature, then the technical reference (API, server settings, CLI).

Contents

1. What ARACHNIX does
2. Quick start: your first audit
3. The screen at a glance
4. Menus
5. Settings (File > Settings)
6. Crawl Config (Configuration menu)
7. Crawler engine
8. Link discovery
9. JavaScript sites and rendering
10. Signed-in crawls and bot checks
11. Subdomains
12. SEO tabs and filters
13. Site graph and visualisations
14. After the crawl: checks, re-checks, fix plan, insights
15. Saved crawls, history and comparison
16. Segments
17. Reports and exports
18. Google connections and PageSpeed (API Access)
19. Scheduling and email notifications
20. AI layer (free providers)
21. REST API reference
22. Server settings and environment variables
23. Local development, CLI and tests
24. Troubleshooting
25. ARACHNIX 2.0 (in development)

---

## 1. What ARACHNIX does

**Product rules** (every screen, file and endpoint follows them):

- **Visual first, plain language.** Each issue says what is wrong, where it sits on the page and how to fix it. Details (headers, selectors, raw HTML) sit behind "more info".
- **Category = HTTP status, and every count reconciles.** A URL's category comes from its own status code only: a 404 is always *Broken 4xx* (4xx Client Error), however the link was found (menu, sitemap, JavaScript). How it was found is explained inside the issue, never by moving it to another bucket. The category tiles always add up to the URLs crawled, in the dashboard, the Excel file and the PDF; folder trees, segments, response-time buckets and the subdomain list add up the same way.
- **Passed / Failed / Not checked are honest.** A check that did not run (feature off, rendering off, nothing to check, site did not answer) is *Not checked* with its reason, never a pass and never "0". A Google source that is not connected says *Not connected*, never 0.
- **Nothing is cut silently.** Max URLs, Max depth, the server's memory guard, crawl traps, page-size cuts and links skipped while signed in are counted and named in a "Partial crawl" banner, the Excel summary and the PDF.
- **"Parent link"** is the page a URL was found on. It has this name everywhere you see it: grid, details pane, Excel and CSV headers, PDF and tickets.
- **AMP is deprecated.** ARACHNIX adds no AMP checks; the old AMP tab stays hidden from the tab bar, Overview, reports and the PDF.
- **One crawl per server.** Everyone using a server shares the crawl on screen. Opening a saved crawl, a List mode crawl, a scheduled run or a Crawl Analysis waits or is refused while a crawl runs, and says why.
- **Secrets never leave the server.** Passwords, cookie values, header tokens, SMTP passwords, OAuth tokens and API keys are masked as `***` in logs, API answers, exports, saved crawls, profiles and screenshot captions. Settings answer `{"set": true}` for a secret, never its value.

**What a crawl produces**

| Area | What you get |
| --- | --- |
| Crawl | Every internal URL (pages, redirects, errors, images, scripts, stylesheets, files), every external link checked once (never crawled), robots.txt and XML sitemaps; Spider mode (follow links), List mode (your URLs only), SERP mode (titles and descriptions without crawling) |
| SEO audit | 36 tabs: Screaming Frog's tabs with their filters (incl. JavaScript, Accessibility, PageSpeed, Search Console, Analytics, Custom Search / Extraction / JavaScript) plus our own (AI Crawlers, Robots.txt, Hygiene, Parameters, Performance, Social, Third Parties); Issues overview; URL details pane |
| Evidence | Screenshots of broken links on their pages (the link boxed), the broken page itself, and pinpointed pictures for SEO issues (the element boxed, its HTML and position); cookie and newsletter pop-ups are hidden first |
| Follow-up | Passed / Failed / Not checked per check, live re-check of reported errors (Validation agent), login guard, root-cause fix plan, content intent checks, business insights |
| Tracking | Saved crawls (open, rename, lock, export, import), crawl-to-crawl comparison, dismissed issues, segments, scheduled crawls with email |
| Visuals | Site graph (By depth, Clusters, Drill-down tree), Crawl Tree, Directory Tree, Force-Directed Directory Tree, word clouds, Site Structure, Response Times |
| Files | Excel workbook, Website Audit Report PDF, Business Insights PDF, 30 reports, bulk exports and Multi-Export, XML and image sitemaps, fix files (Yoast, Rank Math, Shopify, .htaccess, Next.js, CSV), developer tickets (CSV, Jira, Markdown, .zip with screenshots) |

**Architecture**

```
Browser - dashboard (index.html, vanilla JS, served by Vercel or by the backend)
   |  REST + polling (/api/crawl/status every ~1 s while crawling)
   v
FastAPI web/app.py - one CrawlSession: the current CrawlJob + background JobRunners
   |                  (validate, intel, pinpoint, bot check, business, analysis, integrations, exports)
   |- crawler/engine.py          WebsiteCrawler: queue, workers, scope, auth, limits, results
   |- crawler/render_capture.py  Render every page in Chrome (pool of Chromes), Rendered Page data
   |- crawler/seo/               analysis (Site), registry + tabs/*.py (filters), compare, graph, structure
   |- crawler/auth.py, cookie_import.py, live_browser.py   sign-in methods, cookie paste, bot-check guidance
   |- crawler/subdomains.py      Find subdomains, Subdomains report
   |- crawler/evidence.py, seo_evidence.py   screenshots and pinpointed pictures (Selenium + Chrome)
   |- crawler/validate.py, login_guard.py    live re-checks
   |- crawler/intel/             platform detection, root-cause groups, checks, content intent
   |- crawler/integrations/      Google OAuth, Search Console, GA4, PageSpeed Insights
   |- crawler/store.py, settings_store.py, scheduler.py, notify.py, segments.py   saved crawls, settings, schedules, email
   |- crawler/ai/                free AI client, business insights
   `- crawler/exporter.py, reports.py, sitemap_gen.py, serp.py, pdf_report.py, business_pdf.py, fixfiles.py, tickets.py
```

Long work (crawl, analysis, screenshots, re-checks, AI, Google fetches, Multi-Export, business report) always runs in
background threads; API requests only start it and report its state, so no request waits minutes (except the first
PDF download, see 17.2).

---

## 2. Quick start: your first audit

1. Open ARACHNIX: the office server's address, or http://127.0.0.1:8000 on your own PC. On a server with a team access key, enter the key once.
2. Type the website address in the top bar (for example `https://www.example.com/`).
3. Pick the **crawl scope** next to it: *Subdomain* (the start host only, the default), *Subfolder*, *All Subdomains* or *Exact URL*. **Subdomains** next to it finds other hosts of the site (admin., shop., blog.) to crawl too (section 11).
4. Optional: **Options** (Crawl Config) for Max URLs, speed, User-Agent, Include / Exclude, **Render every page in Chrome**, and **Authentication** for members-only pages (section 6).
5. Press **Start**. The progress bar, the tiles and the status bar ("Average: 3.20 URL/s. Current: 4.10 URL/s." and "Completed 812 of 1,240 (65%) 428 Remaining") update about every second, and the tabs fill while the crawl runs.
6. When the crawl ends, ARACHNIX builds the reports and runs **Crawl Analysis** (Link Score, sitemaps, duplicates, pagination, hreflang, bot check). Tabs that depend on it say so until it is done.
7. Read the results: the right panel's **Overview** (every filter with its count), **Issues** (what to fix, most important first) and **Fix plan** (root causes in fix-first order). Click a filter to list its URLs, click a row to see its details below the grid.
8. Press **Validate errors** to re-check every broken URL live before you report it.
9. Download what you need: **PDF report**, **Excel**, the **Reports** and **Bulk Export** menus, fix files and tickets (Fix plan).
10. The finished crawl is saved by itself (**File > Crawls**). Next time, compare the two crawls (**Mode > Compare**) to see what was fixed, what is new and what came back.

---

## 3. The screen at a glance

### Menu bar

File, View, Mode, Configuration, Bulk Export, Reports, Sitemaps, Visualisations, Crawl Analysis and Help, as in
Screaming Frog (section 4). Arrow keys move between menus and items. SERP mode hides the crawl menus; Compare mode hides
Sitemaps and Crawl Analysis. A greyed-out item says why on hover (for example "Crawl a website first").

### Top bar

- **ARACHNIX logo:** click it for About. The ARACHNIX logo pulses gently; while a crawl runs, the tab icon animates and a large logo shows in the middle until the first rows arrive, then a check mark (done) or red dot (failed).
- **Website address:** the × clears it, the ▾ lists recently crawled sites. In List mode an **Upload** button with the number of listed URLs takes its place.
- **Crawl scope** and **Subdomains** (Spider mode only), **Start**, **Pause** / **Stop**, **Clear** (asks first; it removes the crawl on screen for everyone; disabled before a crawl).
- **Options:** the Crawl Config drawer, with a short summary of what differs from the defaults.
- **History ▾:** earlier crawls of the site, to compare.
- **Server chip:** online / offline (checked every 15 s while the tab is visible), "Server refuses this page (CORS)" when the server does not allow the address the dashboard was opened from.
- **Key ▾** (servers with a team access key): Change key, Lock.

### Visual strip and banners

- **Health score:** the HTML pages that load (2xx, or a redirect) out of all HTML pages crawled ("N of M pages load").
- **Category tiles:** 2xx, 3xx, 4xx, 5xx, no response and blocked by robots.txt; they add up to the URLs crawled. A re-check tile appears after a live re-check.
- **Report buttons:** **Validate errors**, **PDF report** (prepares the pictures with progress, then downloads), **Excel ▾** (full workbook or one category), **Bulk export ▾**.
- **Banners:** login needed, login failed, a bot check in the way (with the routes that work, section 10.5), partial crawl, memory stop, the site's firewall slowing or blocking the crawl, site did not answer, server offline, crawl cleared. Each says what happened and what to do, with buttons.

### Tabs and views

The tab bar holds the SEO tabs (section 12; the ☰ button lists them all) and our views: **Broken Links**, **Verified**
(live re-check results and the login guard, once a re-check ran), **Content insights**, **Site graph**,
**Visualisation** (while one is open), **SERP** (SERP mode), **Compare** (after a comparison) and **Logs**. When there
are more tabs than room, the strip fades at the cut edge and the mouse wheel scrolls it sideways. A tab whose
checks did not run shows "–" and says why on hover; **Focus Mode** (View menu) hides such tabs. **View > Configure
Tabs** shows, hides and orders them.

### Grid

- **Filter control (several filters at once):** a dropdown with a checkbox and count per filter, a search box for long lists, **All** (clears the selection), **Select all issues**, and **Any** (URLs in at least one filter, the default) / **All** (URLs in every filter). Selected filters show as removable chips; the grid, the *Filter Total* footer, the tab badge and the Overview count the distinct URLs of the selection. Filters that did not run are shown disabled with the reason. Keys: ↓ opens it, ↑ ↓ Home End move, Space ticks, ← → switch Any / All, Enter or Esc close. Clicking a filter in Overview / Issues selects just that one. The selection is remembered per tab in this browser.
- **Response Codes scope:** Internal & External, Internal or External; it applies to every selected status.
- **Segment:** shows the URLs of one segment (section 16), when the site has segments.
- **List / tree view:** the tree view groups the URLs by folder (with counts that add up).
- **Search:** the address or any column (`/` jumps to it).
- **Export ▾:** the grid exactly as shown (the same rows, several filters too), as CSV or Excel.
- **Columns:** the **+** at the end of the header row chooses and orders the columns (remembered per table; View > Reset Columns for All Tables undoes it).
- **Filter description:** the line under the toolbar says what the selected filter means; click it for the full explanation and the fix.
- **Footer:** *Selected* and *Filter Total*.
- **Ticking rows:** Ctrl+click or Space ticks a row, Shift ticks a range. The bar that appears offers **Recheck selected URLs** (after the crawl; their status and the counts update when done), **Copy URLs**, **Select all shown** and **Clear**.

### URL details pane

Below the grid (drag the splitter to resize). It opens when a row or a graph node is picked. Its tabs:

| Tab | What it shows |
| --- | --- |
| URL Details | Status, indexability, title, description, headings, canonical, depth, Link Score, re-check verdict and what it means |
| Inlinks / Outlinks | Every link to and from the URL with anchor text, type, position, nofollow, status |
| Image Details / Resources | Images (alt text, size, dimensions) and the files the page loads |
| SERP Snippet | How the title and description look in Google, with their pixel widths |
| Rendered Page | The page as Chrome shows it: rendered title, links, blocked and failed resources, mobile and accessibility checks, a screenshot. Without a stored render, **Render this page now** renders it (this never changes the crawl's data) |
| Chrome Console Log | Errors and warnings of a rendered page (needs rendering) |
| View Source | The HTML as crawled and, for a rendered page, the rendered HTML (kept when "Keep each page's HTML for View Source" is on; not on serverless hosts) |
| HTTP Headers / Cookies | Response headers (cookie values masked) and the cookies the URL sets (names and attributes only; values are never stored) |
| Duplicate Details | Pages with the same title, description, h1 or content, and near duplicates |
| Lighthouse Details | PageSpeed Insights results for the URL (when it was tested) |
| Issues / Why & Fix | Every issue of the URL with what it means and how to fix it |
| Link Source | Where the link sits: the Parent link, the clicked element and its HTML, source line, page region, CSS selector, JavaScript file, the href as written |

Table tabs have a search box and an **Export** (CSV or Excel). External URLs show URL Details, Inlinks and Issues.

### Right panel

Tabs **Overview** (the Summary rows, HTML pages, a **Checks** line with Passed / Failed / Not checked / not applicable /
dismissed chips, every filter of every tab with counts, and a chart of the selected tab: a donut when its filters split
the URLs, bars otherwise), **Issues** (the Issues overview with
dismiss buttons), **Site Structure** (folder tree, crawl depth histogram, top folders and the **Subdomains** list of
section 11.3), **Segments** (URLs per segment), **Response Times** (URLs per response-time bucket, average, median, 90th
percentile and slowest; click a bucket to list its URLs) and **Fix plan** (fix-first cards with root cause, steps,
platform steps, effort, impact, where the verdict came from, Export tickets, Export fix files). Drag its left edge to
resize it (arrow keys on the edge too, Enter or a double-click for the default width); » hides it so the grid gets the
full width, and the rail on the right brings it back. Every number adds up to the crawl's URLs.

- **Overview Summary rows** (Total URLs Encountered, Internal / External Blocked by robots.txt, Total URLs Crawled, Total Internal URLs, Total External URLs): a click lists exactly that many URLs in the grid (Response Codes, Internal or External tab) and marks the row. The Indexable / Non-Indexable rows have no list of their own; their tooltip points to the Internal tab's Indexability column.
- **One mark per filter**, the same in the Overview and the filter control: **✓** Passed, the issue type's own mark when it failed (red ● issue, amber ▲ warning, blue ◆ opportunity), a dashed **–** Not checked (hover says why), **·** not applicable to this site. A flagged check the team dismissed counts as *dismissed*, not failed (section 15.4); the *Not checked* chip lists the checks that did not run and why.
- The six panel tabs sit in two rows of three at any panel width.

### Status bar, dock and phone layout

- **Status bar** (bottom): the mode and what it is doing ("Spider Mode: Crawling", "Building reports", "Crawl Analysis", "Idle"), average and current URLs per second, and "Completed X of Y (Z%) N Remaining".
- **Business insights** dock at the bottom (section 14.6).
- **Phone layout:** one column, nothing wider than the screen; the live login window opens in a phone layout.

### Keyboard shortcuts (Help > Keyboard Shortcuts)

| Keys | Action |
| --- | --- |
| Ctrl+O | File > Crawls |
| Ctrl+, | File > Settings |
| Ctrl+; | Configuration > Crawl Config |
| Alt+Shift+N / Alt+Shift+W / Alt+Shift+T | File > New / Close / Scheduling (Ctrl+N, W and T belong to the browser) |
| / | Search the grid |
| ↑ ↓, Page Up, Page Down | Move through the grid |
| Space | Tick a row (several rows) |
| Enter | Go to the details of the selected row |
| Esc | Close a menu or dialog |
| Tab / Shift+Tab | Move through the controls of the open dialog (focus stays in it) |
| Arrow keys in the menu bar | Move between menus and items |
| ← → on the right panel's left edge | Resize the right panel (Enter: default width) |

---

## 4. Menus

### 4.1 File

| Item | What it does |
| --- | --- |
| New (Alt+Shift+N) | Closes the crawl on screen and starts an empty one (a saved crawl stays under File > Crawls) |
| Crawls... (Ctrl+O) | The saved crawls: open, rename, project, lock, duplicate, delete, import, export, compare (section 15.1) |
| Open Recent | The saved crawls this browser opened last |
| Close (Alt+Shift+W) | Takes the crawl off the screen without deleting it; asks first when it is not saved |
| Save... | Keeps this crawl under File > Crawls (finished crawls are saved by themselves while Crawl Retention > Save every finished crawl is on) |
| Rename... | Renames the saved crawl |
| Delete | Removes the crawl from the screen and deletes its saved copy and History entry (asks first; everyone using the server loses it) |
| Import... / Export... | A crawl as one .zip (results, settings without secrets, page sources, pictures), to move it to another ARACHNIX |
| Settings > | The Settings dialog and each of its pages (section 5) |
| Crawl Recent > | Starts a new Spider crawl of a recently crawled address |
| Scheduling (Alt+Shift+T) | Scheduled crawls (section 19) |

### 4.2 View

| Item | What it does |
| --- | --- |
| Reset Columns for All Tables | Every grid back to its default columns |
| Reset Tabs / Configure Tabs... | Every tab shown in the default order / show, hide and order the tabs |
| Focus Mode | Hides tabs with nothing to show: checks that did not run in this crawl, and empty tabs without issues |
| Theme > | **Light**, **Dark** or **System default** (follows the computer's setting, also when it changes) |
| Glass effects > | **On**, **Reduced** or **Off** (section 5.2); kept in step with Settings > User Interface |
| User Interface settings... | Opens Settings > User Interface |

Theme, accent colour, Focus Mode and Glass effects are saved per browser and applied before the page is drawn, so
the screen never flashes.

### 4.3 Mode

- **Spider** (default): crawls the site from its start URL, following links within the crawl scope.
- **List:** crawls only the URLs you upload or paste. The **Upload** button in the top bar takes a .txt, .csv or .xlsx file, an XML sitemap file, a pasted list, or a sitemap's address, and shows the addresses read (in order, without repeats) and the lines that are not addresses. At most 10,000 URLs per list (`CRAWLER_MAX_LIST_URLS`, lowered to the server's URL cap). Their links are recorded, not followed (Configuration > Crawl Config > List mode can follow them for a few clicks); redirects are followed so chains resolve. robots.txt is not obeyed in List mode unless you tick it there (as in Screaming Frog). **Bulk Export > Listed URLs** gives the list in upload order with each URL's status, redirect, indexability and title.
- **SERP:** checks page titles and meta descriptions without crawling: **Upload File** (.csv, .txt or Excel with URL, Title and Description columns), **Paste…**, or **Use the crawl's titles**. Each row shows its length in characters and pixels and the Page Titles / Meta Description filters it falls under (the crawl's thresholds). Export as CSV or Excel (the SERP Summary layout, so it can be edited and uploaded again). Useful to check proposed titles before a release.
- **Compare:** compares two saved crawls (section 15.3).

### 4.4 Configuration

| Item | What it does |
| --- | --- |
| Crawl Config (Ctrl+;) | The Crawl Config drawer (section 6), also opened by **Options** |
| Spider > Crawl, Limits, Rendering, Advanced; robots.txt; URL Rewriting; Include; Exclude; Speed; User-Agent; HTTP Header | Jump to that part of Crawl Config |
| Custom > Custom Search, Custom Extraction, Custom JavaScript | Your own searches, extractors and snippets (section 6) |
| API Access > Overview, Google Analytics 4, Google Search Console, PageSpeed Insights, Majestic, Ahrefs, Moz | Optional data connections (section 18) |
| Authentication > Email + password, Browser window, Cookies, Header | Sign-in for members-only pages (section 10) |
| Segments | Named groups of URLs (section 16) |
| Crawl Analysis | What is calculated after the crawl (section 14.2) |
| Profiles > Load..., Load Recent, Save As..., Save Current Configuration as Default, Clear Default Configuration | Named crawl configurations shared by the team (section 6.2) |

### 4.5 Bulk Export

Downloads lists of URLs (CSV or Excel; **Format** at the bottom of the menu, remembered per browser):

- **Multi-Export:** many reports and bulk exports in one go: one workbook (an Index sheet first) or a .zip of CSVs, built in the background; "skip empty" leaves out exports without rows.
- **Listed URLs (upload order)** in List mode.
- **Queued URLs:** found but not crawled yet (paused or stopped crawl), and those left out by the limits.
- **Links:** All Inlinks, All Outlinks, All Anchor Text, External Links, then the Links tab's filters.
- **Web:** All HTTP Headers, All Cookies (names and attributes only), All Page Source (the raw HTML of every page, one file each, always a .zip).
- **Path Type:** links written as Absolute, Protocol-Relative, Root-Relative or Path-Relative.
- **Every tab > every filter:** its **URLs**, or its **Inlinks** (every page linking to each URL of the filter: the pages to edit).
- **Issues:** All Issues (every URL of every flagged issue), Issues Overview, and one export per flagged issue.

A filter that did not run is greyed out with the reason; an empty one says "No URLs in this filter".

### 4.6 Reports

| Report | What it lists |
| --- | --- |
| Crawl Overview | Every tab and filter with its URL count and % of the tab, plus the crawl summary |
| Issues Overview | Every flagged issue: type, priority, URLs, % of total, what it means and how to fix it |
| Segments Overview | URLs, status classes and issue counts per segment |
| Redirects > All Redirects, Redirect Chains, Redirect & Canonical Chains | Every redirect with each hop and the final URL; URLs that redirect two or more times or in a loop; chains of redirects and canonicals mixed |
| Canonicals > Canonical Chains, Non-Indexable Canonicals | Canonicals pointing to a page that is canonicalised again, or to a URL that cannot be indexed |
| Pagination > Non-200 Pagination URLs, Unlinked Pagination URLs | rel=next / prev links to URLs that do not answer 200, or that no crawled page links to |
| Hreflang > All hreflang URLs, Non-200, Unlinked, Missing Return Links, Inconsistent Language & Region Return Links, Non Canonical Return Links, Noindex Return Links | Every hreflang annotation, and each kind of hreflang problem |
| Insecure Content | HTTPS pages that load or link to HTTP addresses |
| SERP Summary | URL, title and description with lengths and pixel widths (can be uploaded again in SERP mode) |
| Orphan Pages | URLs found in XML sitemaps (and in Search Console or Analytics when connected) that no crawled page links to |
| Structured Data > Validation Errors & Warnings (Summary), Google Rich Result Features (Summary) | Structured data problems and rich result types per page |
| Javascript > JavaScript Console Log Summary | Console errors and warnings of the rendered pages |
| PageSpeed > PageSpeed Summary | The PageSpeed filters (needs a PageSpeed connection) |
| Mobile > Mobile Summary; Accessibility > Accessibility Violations Summary | The Mobile and Accessibility filters per page |
| HTTP Headers > HTTP Header Summary | Each response header and how many URLs sent it |
| Cookies > Cookie Summary | Every cookie the site sets (server answers and, with rendering, scripts); names and attributes, never values |

Below them: **PDF audit report**, **Excel workbooks** (full workbook, all broken links, 4xx, 5xx, no response, 3xx,
2xx, blocked by robots.txt, external links), **Business report (PDF)** and **Format** (CSV or Excel). A report with
several tables is a .zip in CSV format.

### 4.7 Sitemaps

**XML Sitemap** and **Images Sitemap** build a sitemap from the finished crawl, with the options of Screaming Frog's
export dialog and its defaults: only internal HTML pages that answered 2xx and are indexable go in; noindex,
canonicalised and paginated pages, PDFs and 3xx / 4xx / 5xx URLs can be added one by one. lastmod comes from each
page's Last-Modified header, a fixed date, or nothing; priority and changefreq are off by default and can follow the
crawl depth; hreflang alternates can be included. A preview says how many URLs go in and why the others were left out.
Files follow sitemaps.org: at most 50,000 URLs and 50 MB per file; a bigger sitemap is split into numbered files plus a
sitemap index, downloaded as one .zip. Images use Google's image extension.

### 4.8 Visualisations

Crawl Tree Graph, Directory Tree Graph, Force-Directed Crawl Diagram (the site graph), Drill-down Tree,
Force-Directed Directory Tree Diagram, Inlink Anchor Text Word Cloud and Body Text Word Cloud (section 13).

### 4.9 Crawl Analysis

**Start** re-runs the analysis of the crawl on screen (after changing its steps, or on an opened crawl), **Stop** stops
it, **Configure** chooses the steps (section 14.2). Refused while a crawl runs.

### 4.10 Help

**User Guide** (this document, with search), **Third-party notices**, **Keyboard Shortcuts**, **Usage Stats** (crawls
and URLs crawled per month and the most crawled sites on this server), **Debug...** (environment, versions, settings,
storage, scheduler, connections, the crawl's stats and recent log lines, with a JSON download; secrets only as
"set" / "not set") and **About ARACHNIX** (version, platform, Chrome status).

---

## 5. Settings (File > Settings)

Settings has a search box and one page per topic. **User Interface** is saved in this browser only. **Proxy**, **Crawl
Retention**, **Embedded Browser** and **Notifications** are server-wide (shared by everyone using the server, applied to
the next crawl). **Storage** and **Memory** are read-only facts about the server.

Who may change server-wide settings (and the Google connections, section 18): anyone who passed the team access key
when the server has one (`CRAWLER_ACCESS_KEY`); otherwise only a browser on the server's own computer
(http://localhost:8000), or everyone when the server sets `CRAWLER_SETTINGS_EDITABLE=1`. Other browsers see them
read-only, with the reason.

### 5.1 Pages

| Page | Settings |
| --- | --- |
| Access Key | Whether this server asks for a team access key; Change key, Lock (forget the key on this device) |
| User Interface | Theme, Accent Colour, Focus Mode, Glass effects (5.2) |
| Proxy | Use Proxy Server, Address, Port, Use Proxy Credentials, Username, Password, **Test proxy**. One proxy for all traffic: pages, external link checks, sitemaps, robots.txt and the Chrome used for rendering |
| Storage | The folder holding saved crawls (`CRAWLER_REPORTS_DIR`), saved crawls and locked ones, space used, disk free, crawls kept per site, largest import, and a warning when the folder is the system temp folder (a redeploy or clean-up can wipe it) |
| Crawl Retention | Automatically delete crawls after: Never, 1, 7, 30, 90 days or Custom. Save every finished crawl (on by default). Locked crawls are never deleted |
| Memory | The server's URL cap per crawl, the longest crawl a serverless host allows, whether Chrome is available |
| Embedded Browser | Enable Embedded Browser: Chrome for rendering, password logins, the live login window and screenshots |
| Notifications | SMTP server (host, port, encryption TLS / SSL / none, username, password, from address and name, the dashboard address put in the email), **Send Test Email**, Email on Crawl Complete with its recipients, attach the exports (zip, up to 10 MB). Section 19.3 |

Secret fields (proxy password, SMTP password) show "saved" or "not set", never the value; leave them empty to keep
them, or tick "Remove the saved value". A value set by the server's environment is labelled with the variable's name.

### 5.2 User Interface: theme, accent colour, Focus Mode, Glass effects

- **Theme:** Light, Dark or System default (follows the computer's light or dark setting live). Also under View > Theme. Status colours keep their meaning in both themes.
- **Accent Colour:** a slider recolours ARACHNIX's teal (buttons, focus rings, the logo); **Reset to Default** brings the teal back. Status colours (2xx, 3xx, 4xx, 5xx) never change with the accent.
- **Focus Mode:** the same switch as View > Focus Mode.
- **Glass effects: On (default) / Reduced / Off.** A translucent "glass" material for the controls and the layers that float over the content: buttons, segmented switches, tab strips, chips, the filter control, the top bar and menu bar, menus, popovers, tooltips, toasts, dialogs, drawers and the status bar. Buttons light up where the pointer is, lift slightly on hover and spring back when pressed; menus open with a short scale-and-fade. Content (grid rows, charts, long text, View Source, the site graph, the PDF) stays solid and fully readable. Also under View > Glass effects (kept in step).
  - **Reduced:** the blur material on menus, dialogs and the top bar only: no pointer highlight, no refraction, no spring.
  - **Off:** solid surfaces (the plain look).
  - Dialogs keep the glass on their header and footer; their content (tables, forms) sits on a solid sheet.
  - ARACHNIX respects the computer's settings: "reduce transparency" acts as Off until you choose a level yourself; "reduce motion" means no pointer light, lift, spring or scaling; high-contrast (forced colours) mode is always Off (system colours). A browser that cannot blur gets Reduced. A computer with 2 processor cores or fewer, or 2 GB of memory or less, gets Reduced until you choose On yourself. When the computer chose less than your setting, View > Glass effects shows "reduced here" / "off here" and Settings > User Interface says why. On Chrome and Edge, On also bends the frosted backdrop of menus and dialogs slightly (refraction). Text on glass keeps WCAG AA contrast (4.5:1) in both themes, over any background.

---

## 6. Crawl Config (Configuration menu)

The **Options** button and **Configuration > Crawl Config** open the Crawl Config drawer. Its sections, top to bottom:

| Section | Settings |
| --- | --- |
| Crawl scope | **Subdomain** (the start host only; www and the bare name count as one), **Subfolder** (only below the start folder), **All Subdomains** (every subdomain linked from the site), **Exact URL** (only the start URL, with its resources and redirects). **Subdomains crawled too:** hosts added under Find subdomains, a box to type one, Find subdomains… (section 11) |
| Speed | Parallel requests (1-50, default 6), Max URLs per second (0 = no limit) |
| Limits | Max URLs (0 = no limit; the server's cap lowers it), Max depth (clicks from the start page, default 50), response timeout (15 s), delay between requests, Max page size (5,120 KB), Max links per page (10,000) |
| User-Agent | Chrome (a normal visitor), Googlebot desktop / smartphone, Bingbot, GPTBot, ChatGPT-User, OAI-SearchBot, ClaudeBot, PerplexityBot, Applebot, DuckDuckBot, YandexBot, or Custom. Each bot preset obeys its own robots.txt group |
| HTTP Header | Extra request headers (for example Accept-Language, or a secret header your firewall allows). Sent to the crawled site only; secret values are never saved in this browser |
| Include / Exclude | Regexes, one per line: only / never crawl matching URLs. **Test URL** shows how Include, Exclude and URL Rewriting treat an address, exactly as the crawl will |
| URL Rewriting | Remove Parameters (tracking parameters by default; `utm_*` removes every name starting with utm_), Remove all parameters, Regex Replace (top to bottom, `$1` = group 1), Lowercase discovered URLs, Sort parameters (on), Percent Encoding Mode (upper or lower case). Applies to discovered URLs, never to the start URL or listed URLs |
| Custom Search | Up to 10 searches: pages that contain (or do not contain) a text or regex, in the HTML or the visible text. Each becomes a filter and a column of the Custom Search tab |
| Custom Extraction | Up to 10 extractors: XPath, CSS selector or regex; text, inner HTML, the element or an attribute. Each becomes columns of the Custom Extraction tab |
| Custom JavaScript | Up to 10 snippets run in Chrome on every rendered page; an extraction snippet returns a value per page (Custom JavaScript tab). Needs rendering |
| Rendering | **Render every page in Chrome** (slower, about 2-3 s per page): fills the JavaScript tab, JavaScript redirects, the Chrome Console Log and Rendered Page views, rendered accessibility and mobile checks, incorrectly sized images and Custom JavaScript. Off: those checks say "Not checked" |
| What to crawl | Check images, CSS and JavaScript; Check external links; Respect robots.txt; JavaScript app mode (React, Next.js, Vue); Find links inside JavaScript files; Capture screenshots; Read XML sitemaps |
| Advanced | Check SSL certificates (off: a site with a broken certificate is still crawled); Skip endless URL loops (on, 7.7); Keep each page's HTML for View Source (on) |
| List mode | Respect robots.txt in List mode (off); Follow links from the listed URLs (clicks, 0 = only the list) |
| Crawl Analysis | Auto analyse at the end of the crawl; Link Score; Sitemaps and orphan pages; Near duplicates; Semantically similar and low relevance content; Pagination; Hreflang; robots.txt audit and crawl traps; Search and AI bot check (section 14.2) |
| Authentication | Log in before crawling: Email + password, Browser window, Paste cookies, Header; Test login (section 10) |
| More | API Access, Segments, Proxy |

The drawer's choices are remembered in this browser (secret values never are). A bad regex or value stops the start
with a message that names it (nothing is crawled); `POST /api/config/validate` checks the options without starting.

### 6.1 Defaults and limits

A server can cap the URLs per crawl (`CRAWLER_MAX_URLS`); the Limits section then says "This server crawls at most N
URLs per crawl". The server's memory guard stops queuing new URLs when free memory runs low (section 7.8).

### 6.2 Profiles

**Configuration > Profiles** keeps named crawl configurations on the server, shared by the team: **Save As...**,
**Load...** (the list of profiles, with Load, Export as a file, Import... and Delete), **Load Recent**, **Save Current Configuration as
Default** (new browsers start with it) and **Clear Default Configuration** (back to the factory defaults). Passwords,
cookie values, header values, tokens and keys are always removed before a profile is stored or exported. A scheduled
task can use a profile (section 19).

---

## 7. Crawler engine

### 7.1 Breadth-first traversal
The crawl engine (`crawler/engine.py`) uses a thread-safe breadth-first queue (`collections.deque` guarded by a lock):

- **Queue entry:** `(url, parent_url, depth, discovery_source)`.
- **Depth 0:** the start URL, the domain root, the signed-in landing page and, in Spider mode, the home page of every host added under Find subdomains. In List mode: the listed URLs.
- **Depth N + 1:** every child URL found on pages at depth N.
- **Deduplication:** two sets (`enqueued_urls`, `visited_urls`) make sure every normalised URL is queued and fetched exactly once.
- **Counts always reconcile:** a URL handed to a worker is either a result or, when the crawl is stopped before its request went out, put back in the queue. After a stop the requests already under way finish and their results are kept, so `total_crawled` equals the results and `discovered = crawled + queue`.

### 7.2 Concurrency and workers
- **Workers:** 6 by default (1 to 50), in a `ThreadPoolExecutor`. Parsing and analysis of pages run in a limited number of slots (`CRAWLER_CPU_SLOTS`, default 1): Python runs one thread at a time anyway, and many workers parsing at once made every page cost 2-3 times the CPU, so the others wait while their requests are under way.
- **Speed limit:** `max_urls_per_second` (0 = unlimited) caps requests to the crawled site across all workers (token bucket).
- **Per-page limits:** `max_page_size_kb` (5,120 KB, decompressed; parsing peaks at roughly 70 × the HTML size in RAM, so this keeps a 512 MB container safe) and `max_links_per_page` (10,000), like Screaming Frog's "Max Page Size" and "Max Links per URL". Bodies are streamed, so a huge (or gzip-bomb) page is cut at the limit; the crawl log and the report limitations say how many pages were cut. 0 = no limit.
- **Retries:** the HTTP adapter retries only throttling / 5xx answers and one failed connect; timeouts are retried once by the crawler itself, so a URL that never answers gives up after about 2 × the timeout (connect timeout capped at 10 s).
- **Network guards** (`crawler/net_guard.py`): a 429 / 503 answer pauses the host (Retry-After honoured), slows the crawl down and puts the URL back in the queue (at most 3 times); a firewall or bot-challenge answer pauses the host (10 s the first time) and slows it down hard (more with each further challenge); normal answers bring the speed back gradually. Private and internal network addresses are never requested.
- **Stop and pause:** stop ends the crawl at once (workers waiting for the speed limit or for Chrome give up; a render already running finishes); pause halts the workers without losing the queue. External link checks pause too.
- **SEO index while crawling:** the tabs (`site_index()`) are rebuilt when the results change, at most every 3 s or every 4 × the last rebuild's duration (whichever is longer). While one request rebuilds, other requests get the previous index straight away. Once the crawl has finished the index is rebuilt once.

### 7.3 URL normalisation (`crawler/url_utils.py`)
1. **Schemes:** only `http` and `https`; `mailto:`, `tel:`, `javascript:`, `whatsapp:`, `data:`, `sms:` are ignored.
2. **Relative URLs** are resolved against the page the way a browser does (`url_utils.join_url`; Python's `urljoin` drops empty path segments).
3. **Fragments** (`#team`) are dropped, so one page is not requested twice.
4. **URL Rewriting** (Configuration > URL Rewriting): by default the tracking parameters `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content` (and every other `utm_` name), `fbclid`, `gclid`, `msclkid`, `dclid`, `_ga`, `_gl`, `mc_cid`, `mc_eid`, `ref`, `ref_src`, `source`, `share`, `spm` and `replytocom` are removed and the remaining parameters sorted. The rewriting options can remove other parameters (or all), apply regex replacements, lowercase discovered URLs and choose upper- or lower-case percent encoding.
5. **Paths:** `.` and `..` are resolved as a browser does. Repeated slashes (`/x//y`) and parameters without a value (`?print`) are kept as written (servers can answer them differently, and the URL tab's *Multiple Slashes* filter needs them). Trailing slashes are kept: `/about` and `/about/` are different URLs (one usually redirects to the other), so both are crawled and reported, as Screaming Frog does.
6. **User name and password in the start URL** (`https://user:pass@staging.example.com/`): they are sent to the crawled site as HTTP Basic authentication (for that site only) and taken out of every URL, so no crawled URL, export, file name, log line or saved crawl shows the password.

### 7.4 robots.txt
URLs that robots.txt disallows are not fetched; they are reported as **Blocked by robots.txt** with the rule that
blocked them. Rules are matched the way Google matches them (`crawler/robots.py`): the `Googlebot` group if present,
else the group for every crawler (a bot User-Agent preset obeys its own group; Applebot follows the Googlebot group when
there is none for it); wildcards and `$` work as for Google; the longest matching rule wins and `Allow` wins a tie. Turn it off with **Respect robots.txt**,
`respect_robots: false` (API) or `--ignore-robots` (CLI). A robots.txt that returns 4xx means no rules; 5xx or a timeout
is logged and the crawl continues without rules. List mode ignores robots.txt unless asked.

### 7.5 Crawl scope and hosts
- **Subdomain** (default): the start host; `www.` and the bare name count as one site.
- **Subfolder:** only URLs below the start URL's folder.
- **All Subdomains:** every subdomain of the site's registrable domain that the crawl finds a link to (the older `subdomains` / `--subdomains` switch means the same).
- **Exact URL:** only the start URL (its resources and redirects are still checked).
- **Extra hosts** (Find subdomains, section 11): each host's home page is an extra start URL and the host is in scope whatever the scope says (Exact URL: only its home page). Hosts of other domains, without DNS or on a private network address are left out.
- A redirect from the start URL (or from a URL it redirects to) makes the target host part of the site, so `http://example.com` → `https://www.example.com/` crawls the www site. Anything else on another host is **External**.

### 7.6 Files: fast HEAD inspection
Non-HTML files (`.pdf`, `.zip`, `.png`, `.jpg`, `.mp4`, `.docx`, ...) are checked with a `HEAD` request (status and
latency) instead of being downloaded. When `HEAD` answers an error the file is requested with `GET` and that answer is
kept (some servers refuse `HEAD` only). The Content-Type is recorded exactly as sent: a file without one shows a blank
type (Internal > Other, as Screaming Frog does).

### 7.7 Redirects, loops and traps
- **Every redirect hop is a URL of its own.** A `301` / `302` / `307` / `308` answer is the URL's own result (3xx, with `redirect_destination`); the target is queued and crawled itself. `/old` → 301 → `/gone` (404) gives one 3xx and one 4xx; nothing is counted twice. Chains (`/a` → `/b` → `/c`) and loops are real rows, flagged under *Internal Redirect Chain* / *Internal Redirect Loop*. A redirect target lists the redirecting URLs as inlinks of type **HTTP Redirect**. With rendering on, JavaScript redirects are found too (*Redirection (JavaScript)*).
- **Crawl traps** (`skip_crawl_traps`, on): URLs with a folder repeated 3+ times, endless calendar pages, page-builder pagination, session ids or more than 2,000 characters are skipped, logged and counted (not errors).
- **Loop guard** (Advanced > Skip endless URL loops, on): once 5 URLs ending in the same path part answered with an error, more such URLs linked only from error pages are skipped (listed as skipped, never as crawled). A broken link on a page that loads is always crawled and reported.

### 7.8 Character encoding and limits
- **Encoding:** the `Content-Type` charset; else a byte-order mark; else `<meta charset>` in the first 4 KB; else UTF-8 when the bytes are valid UTF-8 and windows-1252 when they are not (so an undeclared Latin-1 page shows "Café").
- **Limits are always disclosed:** URLs found beyond Max URLs or Max depth are counted (`stats.not_crawled_max_urls`, `stats.not_crawled_max_depth`, `stats.limit_reached`), the first entry of `stats.limitations` says so in plain words, the dashboard shows a "Partial crawl" banner, the Excel summary and the PDF say so. The same applies to the memory guard (`CRAWLER_MIN_FREE_MB`: below it no more URLs are queued, `stats.not_crawled_memory`) and to the server-wide cap (`CRAWLER_MAX_URLS`). A crawl in which no URL got an HTTP answer sets `stats.no_answer` and says "The site did not answer".

---

## 8. Link discovery

### 8.1 HTML parsing (`crawler/page_parser.py`)
Links are read from six HTML surfaces: `<a href>` (with anchor text or aria-label); `<link rel="canonical | alternate |
next | prev">`; elements with `data-href`, `data-url`, `data-link` or `data-target`; `<form action>` of forms sent with GET
(search forms; forms that POST are never crawled); `<area href>` image maps; and internal paths written in inline
JavaScript (`href`, `url`, `path`, `location` assignments). Images, scripts, stylesheets, iframes, CSS background images
and `srcset` candidates are recorded as resources.

### 8.2 XML sitemaps (`crawler/sitemap_parser.py`)
A background thread reads the `Sitemap:` lines of robots.txt and the usual addresses (`/sitemap.xml`,
`/sitemap_index.xml`, `/wp-sitemap.xml`, `/sitemap/sitemap.xml`), follows sitemap indexes up to 8 levels deep and queues
new URLs with the source "XML Sitemap". Each URL's `<lastmod>` is kept for the Sitemaps tab's lastmod checks. At most 25
sitemap files are read per crawl; the checks say how many were left unread.

### 8.3 External links: checked once, never crawled
Every link (or image, script, stylesheet, frame) to another website is recorded with its Parent link, anchor text and
discovery source. With **Check external links** on (the default) each unique external URL is requested **once** (a GET
whose body is not downloaded, redirects not followed, the other site's robots.txt obeyed) for its status code,
Content-Type and redirect target; an error is confirmed with a browser User-Agent before it is kept. External pages are
never parsed or queued. Broken external links are listed with every page that links to them (Broken Links tab, Excel
sheet **Broken External Links**, PDF, bulk exports). With the check off, every report says their status was not checked.

---

## 9. JavaScript sites and rendering

- **Route scanner:** first-party JavaScript bundles (`/_next/static/...`, `/assets/index-<hash>.js`, `main.<hash>.js`) are read for route strings; third-party scripts (analytics, chat widgets, pixels) are skipped. A path found only in code is crawled; if it answers 4xx it is still *Broken 4xx*, diagnosed as "Found in code, not clickable" (low severity) when no element links to it. Paths ending in `/` (built as `"/task/" + id`) are prefixes and are never crawled.
- **JavaScript app mode** (`enable_spa_mode`) and **Find links inside JavaScript files** (`use_dynamic_js`): pages that look like apps (React / Next.js, Vue, Nuxt, Angular, Svelte) or have very few links are rendered in headless Chrome, scrolled and their menus opened by clicks, so click-only menus are explored. On signed-in, server-built sites the first page that shows a new click-only menu is rendered too (at most 40 per crawl).
- **Render every page in Chrome** (`render_all`), like Screaming Frog's JavaScript rendering mode: every internal HTML page is rendered by a pool of Chromes (`RENDER_POOL_SIZE`, else one per spare CPU core, at most 4, never more than the crawl's workers, one fewer than `MAX_CHROME_INSTANCES`). Titles, meta description, headings, words, readability, duplicate fingerprints, canonical, pagination, hreflang, structured data and Open Graph / Twitter values are read from the rendered HTML (a value only the raw HTML has is kept), and a noindex / nofollow in either version counts. The raw-HTML values stay available: the JavaScript tab compares them with the rendered page and the Rendered Page tab shows both. Links, resources, images and response headers keep their raw-HTML values (links found only after rendering are crawled too). Rendering also records the console log, blocked and failed resources, Chrome issues, image display sizes, a 412 × 823 phone-viewport measurement (tap targets, content width, small fonts) and the accessibility tree, and runs Custom JavaScript.
- **Chrome:** one shared Chrome per crawl with a slot limit (`MAX_CHROME_INSTANCES`); a memory watchdog closes Chrome before the server runs out of memory (`CHROME_MIN_FREE_MB`, `CHROME_KILL_FREE_MB`). Chrome profile folders are removed at exit. Settings > Embedded Browser switches Chrome off for every crawl.

---

## 10. Signed-in crawls and bot checks

Signed-in crawls audit member areas, customer accounts and admin-only content. Configuration > Authentication, or the
**Authentication** section of Crawl Config: switch on **Log in before crawling**, pick a method, then **Test login**.
Code: `crawler/auth.py`, `crawler/cookie_import.py`, `crawler/live_browser.py`, `crawler/browser.py`.

### 10.1 Methods

| Method (`auth_type`) | How it works | Best for |
| --- | --- | --- |
| **Email + password** (`form`) | Finds the login form (also inside an iframe), fills it, keeps CSRF fields, submits over HTTP; when the page needs JavaScript it logs in inside Chrome. Optional CSS selectors for unusual forms (Advanced: login form fields) | WordPress, WooCommerce, Magento, Shopify customer accounts, most apps |
| **Browser window** (`live`) | A real Chrome opens on the server and you drive it from the dashboard (clicks, typing, scrolling). **Done** keeps only its cookies and site storage, for the next crawl (3 hours) | One-time codes (MFA), single sign-on, "check your email" logins |
| **Paste cookies** (`cookies`) | You sign in with your own browser, copy its cookies and paste them (10.2) | Sites whose sign-in has a bot check (CAPTCHA), or when you are already signed in |
| **Header** (`header`) | Sends one header on every request to this site (`Authorization: Bearer ...`, `X-Api-Key: ...`) | Staging sites behind a token or basic auth |

Passwords, cookies and header values are used for the crawl only: never saved in the browser, in reports, in saved
crawls or in profiles.

### 10.2 Paste cookies: step by step
The Paste cookies tab has a short guide for **Chrome / Edge** and **Firefox** (it picks your browser), with a small
picture of the developer tools for each way:

1. **Cookies table:** sign in to the site, press F12 (Mac: Cmd+Option+I), open the **Application** tab (Firefox: **Storage**), then **Cookies** > the site's address. Select the cookie rows and copy them (Ctrl+C), or copy just the sign-in cookie as `name=value`.
2. **Cookie header** (quickest): F12 > **Network**, reload the page (F5), click the first request (the page itself, type *document*; Firefox: *html*), and copy the value of the **cookie** request header (Firefox: switch **Raw** on first).
3. **Copy as cURL:** F12 > **Network**, reload, right-click the first request > **Copy** > **Copy as cURL** (Firefox: Copy Value > Copy as cURL) and paste the whole command. ARACHNIX reads only its cookies and User-Agent and never runs it.

The one paste box accepts any of these: a Cookie header (`a=1; b=2`, with or without `Cookie:`), rows of the DevTools
cookie table, a JSON export (array of cookie objects from Cookie-Editor, EditThisCookie, DevTools, Selenium,
Puppeteer / Playwright, also with browser storage), a Netscape **cookies.txt**, a **cURL** command (bash or cmd) or a
**Copy as PowerShell** snippet.

- **Preview** (as soon as you paste): what was read and in which format, how many cookies will be sent, and a table of each cookie (name, domain, Sign-in hint, Secure, HttpOnly, expiry). Values are always shown as `***`. Expired cookies and cookies of another site are greyed out and not sent; a cookie pasted without a domain is sent only to the site's host and its www twin, as a browser keeps it.
- **Sign-in cookie:** a green "✓ sign-in cookie: *name*" chip (and the Sign-in column of the table) marks names that look like a session cookie; "no sign-in cookie spotted" is only a hint, the test tells for sure.
- **User-Agent:** when the paste carried one (cURL or PowerShell), ARACHNIX offers it as the crawl's User-Agent (sites often accept a session only from the browser that made it): **Use this User-Agent** or keep the current one. Options > User-Agent then shows it as Custom.
- **Test these cookies:** opens one page that needs sign-in (found automatically, or the page you type, for example `/dashboard`) with the cookies, and once without them to compare. The answer is **Signed in**, **Not signed in (sent to the login page)** or **Couldn't tell**, with the page tested, its status, where it ended and the User-Agent used. Cookies expire, and signing out ends them: when the test fails, sign in again and copy fresh cookies. A sign-out or data-changing address is never opened.

### 10.3 How a login is confirmed
A login only counts when a page that turns signed-out visitors away opens with the session (`member_check` =
verified). Apps that keep the login in browser storage are compared in a second Chrome. One-time-code, locked-account
and "check your email" pages fail with a plain message. Expired cookies and wrong headers are rejected even when the
start page is public. If no members-only page can be found, the crawl still runs and says the login is *not
confirmed*. A wrong password costs one attempt on the client's site.

**Test login** (`POST /api/auth/test`) logs in, opens a protected page, closes everything and answers
`{ok, method, landing_url, cookies_count, storage_origins, message, screenshot, challenge}`.

### 10.4 Safety while signed in
A signed-in crawl never opens links that would sign out or change data: sign-out links (`/logout`, WooCommerce
`customer-logout`, `?logout=1`, OpenCart `route=account/logout`), WordPress `/wp-admin/` row actions, links with a
`_wpnonce` or `form_key` token, cart changes, delete / remove / cancel links. They are counted
(`stats.not_crawled_unsafe`), listed in the Logs and named in a limitation note; Chrome blocks them too. Forms that POST
are never crawled as pages.

**Session ending mid-crawl:** a 401, or a login form shown with HTTP 200, on a members-only page means the session
ended: the crawler logs in again (at most 2 times per crawl). When it cannot (pasted session, failed re-login, limit
reached) the crawl records a limitation, sets `stats.session_lost` and marks each affected page "Logged out here"; the
category still comes from the HTTP status. **Login failures stop the crawl:** nothing is crawled and the status is
`auth_failed` with a plain `stats.auth_error`.

**Pre-check before a public crawl** (`POST /api/auth/inspect`): a 401 / 403, a redirect to a login page or a password
form on the start page asks for a login before crawling. A login link on a public page is only a hint.

**Login guard** (Validation agent, signed-in crawls only): members-only pages are requested again without the login;
any that open for everyone are listed as **exposed**.

### 10.5 Bot checks (CAPTCHA, Cloudflare and other challenges)
ARACHNIX **never tries to pass, solve or get around** a CAPTCHA or bot challenge (Cloudflare Turnstile, Google
reCAPTCHA, hCaptcha, firewall challenge pages...) and never uses a solving service. It recognises one and says so
plainly, instead of a vague failure: "This site's sign-in uses a bot check that ARACHNIX can't complete for you." Where:
the login pre-check and Test login, form logins, the live browser window (also when a check appears without a click),
Test these cookies, and crawl responses.

The guidance always shows the two routes that work:

1. **Use your own browser's sign-in:** sign in as usual in Chrome, Edge or Firefox (you pass the check there yourself), then **Paste cookies** (10.2).
2. **You own the site? Allow ARACHNIX:** add an allow (skip) rule in your firewall / CDN (or the CAPTCHA plugin of the sign-in form) for your office IP address, or for a secret request header. **Create a secret header** adds `X-ARACHNIX-Key` with a long random value under Options > HTTP Header and shows the value once to copy into the rule (it is not saved in the browser). The steps name the place for common providers: Cloudflare (Security > WAF > Custom rules, action Skip), Sucuri, Wordfence, AWS WAF and Vercel.

**During a crawl** that keeps getting challenged, ARACHNIX pauses and slows down hard (retrying fast only makes a block last longer),
and a one-line banner says how many pages a bot check or firewall answered instead of the page, by which provider,
and that they keep the status code the site sent. **What works** opens the two routes above in a dialog, **First page**
opens the first page that was answered by the check, and × hides the banner (it comes back only if more pages are challenged). When the sign-in itself
met the check, nothing is crawled and the banner offers **Paste cookies** and **I own this site**. The numbers stay honest: each page keeps the status code the site sent (a 403 stays a 403, in its
category), records which bot protection answered, and its diagnosis says it was blocked by that bot protection (it
may load fine in a real browser); nothing is hidden. The API reports it as
`challenge: {seen, provider, count, first_url, advice, owner_steps}` in `POST /api/auth/test` and `GET /api/crawl/status`.

---

## 11. Subdomains

### 11.1 Why a subdomain may be missing
ARACHNIX follows links. A separate app on another host (for example `admin.example.com`) is crawled only when a crawled
page links to it, and only when the scope allows it. **Find subdomains** finds such hosts so you can add them.

### 11.2 Find subdomains
The **Subdomains** button in the top bar (and Crawl Config > Crawl scope > Find subdomains…) opens a dialog for the
site's registrable domain. Tick where to look:

- **Links on the site's pages:** hosts the crawled pages (or the home page) link to or load files from.
- **Sitemaps and robots.txt.**
- **Certificate logs:** public HTTPS certificate records (crt.sh; can take up to 20 seconds; a failure or timeout is shown, not hidden).
- **Search Console:** your verified properties, when connected (section 18).
- **Hosts you know:** type them (one per line).

The home page of each host found is also read for links to more hosts of the domain; those are checked in one more
round and marked **home page link**, naming the host whose home page links to them ("linked from the home page of
shop.example.com"). They are never counted as links on the site's own pages.

Each host found is listed with where it was found, whether it resolves in DNS, and its home page status and title (one
DNS lookup and one request per host). Hosts outside the audited domain are never requested; hosts without DNS or on a
private network address cannot be ticked. **Tick those that answer**, then **Add N hosts to the crawl**: each host's home
page becomes an extra start URL and the host is crawled with the same rules (robots.txt, sign-in, limits, network
guard). The top bar's Subdomains button shows how many hosts are added. Added hosts are used in Spider mode only and are
remembered per domain in this browser. At most two searches run at once on a server.

### 11.3 The Subdomains report
After a crawl, **Site Structure > Subdomains** (right panel; `GET /api/crawl/subdomains`) lists every host of the domain
ARACHNIX saw (in links, resources, redirects, sitemaps, the discovery or the hosts you added), crawled or not, with the
URLs crawled on it and a plain reason:

| Status | Meaning |
| --- | --- |
| Crawled | URLs of this host were crawled (with how many crawled pages link to it) |
| Needs sign-in | The host asks for a login: **Add a sign-in…** |
| Blocked by robots.txt | robots.txt keeps the crawler out |
| Not reached | In scope but the crawl ended before it got there |
| Outside the scope | Linked from the site, but the crawl scope leaves it out: **Crawl it too** |
| Not linked | Found only through certificate logs, sitemaps or typing: no crawled page links to it: **Crawl it too** |
| Doesn't resolve / Private address | No DNS record, or an internal network address: never requested |

`www.` and the bare name share one row. A check shows that the URLs per host add up to the Internal URLs (URLs on other
domains included). **Show its URLs** lists a host's URLs in the grid.

---

## 12. SEO tabs and filters

During and after the crawl, `crawler/seo/analysis.build_site()` computes site-wide facts (inlinks, indexability, Link
Score, duplicates, near duplicates, sitemap orphans, crawl depth) and `crawler/seo/registry.run_all()` evaluates every
tab's filters. Each filter that is an issue has Screaming Frog's issue type and priority and plain-language "what it
means" and "how to fix" text; flagged filters form the **Issues overview**. Content filters run on indexable pages only,
as in Screaming Frog. Thresholds are Screaming Frog's defaults (titles over 60 characters / 561 px, meta descriptions
over 155 / 985 px, h1 / h2 over 70 characters, images over 100 KB, crawl depth over 4, more than 10 external outlinks,
low content under 200 words...).

| Tab | Filters (issue type / priority) | Needs |
| --- | --- | --- |
| Internal | HTML; JavaScript; CSS; Images; Media; Fonts; XML; PDF; Plugins; Other; Unknown |  |
| External | HTML; JavaScript; CSS; Images; Media; Fonts; XML; PDF; Plugins; Other; Unknown; Blocked by Robots.txt; No Response; Success (2xx); Redirection (3xx); Client Error (4xx); Server Error (5xx); Not Checked |  |
| Security | HTTP URLs (Issue/High); HTTPS URLs; Mixed Content (Issue/High); Form URL Insecure (Issue/High); Form on HTTP URL (Issue/High); Unsafe Cross-Origin Links (Warning/Low); Protocol-Relative Resource Links (Warning/Low); Missing HSTS Header (Warning/Low); Missing Content-Security-Policy Header (Warning/Low); Missing X-Content-Type-Options Header (Warning/Low); Missing X-Frame-Options Header (Warning/Low); Missing Secure Referrer-Policy Header (Warning/Low); Bad Content Type (Warning/Low) |  |
| Response Codes | Blocked by Robots.txt; Blocked Resource; No Response; Success (2xx); Redirection (3xx); Redirection (JavaScript); Redirection (Meta Refresh); Redirection (HTTP Refresh); Client Error (4xx); Server Error (5xx); Internal All; Internal Blocked by Robots.txt (Warning/High); Internal Blocked Resource (Warning/High); Internal No Response (Issue/High); Internal Success (2xx); Internal Redirection (3xx) (Warning/Low); Internal Redirection (JavaScript) (Warning/Low); Internal Redirection (Meta Refresh) (Warning/Low); Internal Redirection (HTTP Refresh) (Warning/Low); Internal Redirect Chain (Warning/Medium); Internal Redirect Loop (Issue/High); Internal Client Error (4xx) (Issue/High); Internal Server Error (5xx) (Issue/High); External All; External Blocked by Robots.txt; External Blocked Resource; External No Response (Warning/Low); External Success (2xx); External Redirection (3xx); External Redirection (JavaScript); External Client Error (4xx) (Warning/Low); External Server Error (5xx) (Warning/Low); External Not Checked | Rendering for the two JavaScript redirect filters |
| URL | Non ASCII Characters (Warning/Low); Underscores (Opportunity/Low); Uppercase (Warning/Low); Multiple Slashes (Issue/Low); Repetitive Path (Warning/Low); Contains Space (Issue/Low); Internal Search (Warning/Low); Parameters (Warning/Low); Broken Bookmark (Issue/Low); GA Tracking Parameters (Warning/Low); Over 115 Characters (Opportunity/Low) |  |
| Page Titles | Missing (Issue/High); Duplicate (Opportunity/Medium); Over 60 Characters (Opportunity/Medium); Below 30 Characters (Opportunity/Medium); Over 561 Pixels (Opportunity/Medium); Below 200 Pixels (Opportunity/Medium); Same as H1 (Opportunity/Low); Multiple (Issue/High); `Outside <head>` (Issue/High) |  |
| Meta Description | Missing (Opportunity/Low); Duplicate (Opportunity/Low); Over 155 Characters (Opportunity/Low); Below 70 Characters (Opportunity/Low); Over 985 Pixels (Opportunity/Low); Below 400 Pixels (Opportunity/Low); Multiple (Issue/Medium); `Outside <head>` (Issue/Medium) |  |
| Meta Keywords | Missing; Duplicate; Multiple |  |
| H1 | Missing (Issue/Medium); Duplicate (Opportunity/Low); Over 70 Characters (Opportunity/Low); Multiple (Warning/Medium); Alt Text in H1 (Warning/Low); Non-Sequential (Warning/Low) |  |
| H2 | Missing (Warning/Low); Duplicate (Opportunity/Low); Over 70 Characters (Opportunity/Low); Multiple (Warning/Low); Non-Sequential (Warning/Low) |  |
| Content | Exact Duplicates (Issue/High); Near Duplicates (Warning/Medium); Semantically Similar (Warning/Low); Low Relevance Content (Opportunity/Low); Low Content Pages (Opportunity/Medium); Soft 404 Pages (Warning/High); Readability Difficult (Opportunity/Low); Readability Very Difficult (Opportunity/Low); Lorem Ipsum Placeholder (Warning/High) | Crawl Analysis (near duplicates, semantic similarity) |
| Images | Over 100 kB (Opportunity/Medium); Missing Alt Text (Issue/Low); Missing Alt Attribute (Issue/Low); Alt Text Over 100 Characters (Opportunity/Low); Background Images; Incorrectly Sized Images (Opportunity/Medium); Missing Size Attributes (Opportunity/Low) | Rendering for Incorrectly Sized Images |
| Canonicals | Contains Canonical; Self Referencing; Canonicalised (Warning/High); Missing (Warning/Medium); Multiple (Warning/Low); Multiple Conflicting (Issue/High); Non-Indexable Canonical (Issue/High); Canonical Is Relative (Warning/Low); Unlinked (Warning/Medium); Invalid Attribute In Annotation (Issue/High); Contains Fragment URL (Issue/High); `Outside <head>` (Issue/High) |  |
| Pagination | Contains Pagination; First Page; Paginated 2+ Pages; Pagination URL Not in Anchor Tag (Issue/High); Non-200 Pagination URLs (Issue/High); Unlinked Pagination URLs (Issue/Medium); Non-Indexable (Warning/High); Multiple Pagination URLs (Issue/Low); Pagination Loop (Issue/Low); Sequence Error (Issue/Low) | Crawl Analysis (pagination) for 3 filters |
| Directives | Index; Noindex (Warning/High); Follow; Nofollow (Warning/High); None (Warning/High); NoArchive; NoSnippet (Warning/Low); Max-Snippet; Max-Image-Preview; Max-Video-Preview; NoODP (Warning/Low); NoYDIR (Warning/Low); NoImageIndex (Issue/Low); NoTranslate (Warning/Low); Unavailable_After (Warning/Medium); Refresh; `Outside <head>` (Issue/High) |  |
| Hreflang | Contains hreflang; Non-200 hreflang URLs (Issue/High); Unlinked hreflang URLs (Issue/Medium); Missing Return Links (Issue/High); Inconsistent Language & Region Return Links (Issue/High); Non-Canonical Return Links (Issue/High); Noindex Return Links (Issue/High); Incorrect Language & Region Codes (Issue/High); Multiple Entries (Issue/High); Missing Self Reference (Warning/Low); Not Using Canonical (Issue/High); Missing X-Default (Warning/Low); Missing; `Outside <head>` (Issue/High) | Crawl Analysis (hreflang) for 5 filters |
| JavaScript | Pages with Blocked Resources (Warning/High); Contains JavaScript Links (Warning/Low); Contains JavaScript Content (Warning/Low); Noindex Only in Original HTML (Issue/High); Nofollow Only in Original HTML (Issue/High); Canonical Only in Rendered HTML (Warning/Medium); Canonical Mismatch (Issue/High); Page Title Only in Rendered HTML (Warning/Medium); Page Title Updated by JavaScript (Warning/Low); Meta Description Only in Rendered HTML (Warning/Medium); Meta Description Updated by JavaScript (Warning/Low); H1 Only in Rendered HTML (Warning/Medium); H1 Updated by JavaScript (Warning/Low); Uses Old AJAX Crawling Scheme URLs (Warning/Medium); Uses Old AJAX Crawling Scheme Meta Fragment Tag (Warning/Medium); Pages with JavaScript Errors (Warning/Medium); Pages with JavaScript Warnings (Warning/Low); Pages with Chrome Issues (Warning/Low) | Rendering (all but the AJAX-scheme filters) |
| Links | Pages With Uncrawlable Internal Outlinks (Warning/High); Pages With High Crawl Depth (Opportunity/Medium); Pages Without Internal Outlinks (Warning/High); Internal Nofollow Outlinks (Warning/Low); Internal Outlinks With No Anchor Text (Opportunity/Low); Non-Descriptive Anchor Text In Internal Outlinks (Opportunity/Low); Pages With High External Outlinks (Warning/Low); Pages With High Internal Outlinks (Warning/Low); Follow & Nofollow Internal Inlinks To Page (Warning/Low); Internal Nofollow Inlinks Only (Warning/Low); Outlinks To Localhost (Issue/High); Non-Indexable Page Inlinks Only (Warning/High) |  |
| Structured Data | Contains Structured Data; Missing (Opportunity/Low); Validation Errors (Issue/High); Validation Warnings (Warning/Low); Rich Result Validation Errors (Issue/High); Rich Result Validation Warnings (Warning/Low); Parse Errors (Issue/High); Microdata URLs; JSON-LD URLs; RDFa URLs; Rich Result Feature Detected; Schema Values Not On Page (Warning/Medium); Schema Price Not On Page (Issue/High) |  |
| Sitemaps | URLs in Sitemap; URLs not in Sitemap (Issue/Medium); Orphan URLs (Issue/Medium); Non-Indexable URLs in Sitemap (Issue/Medium); URLs in Multiple Sitemaps (Warning/Low); XML Sitemap with over 50k URLs (Issue/High); Invalid Lastmod (Warning/Low); Lastmod In The Future (Warning/Low); Missing Lastmod (Opportunity/Low); Lastmod Older Than Last-Modified (Warning/Low); XML Sitemap over 50MB (Issue/High) | Read XML sitemaps + Crawl Analysis (sitemaps) |
| PageSpeed | Core Web Vitals Assessment Failed (Issue/High); Document Request Latency (Opportunity/High); LCP Request Discovery (Opportunity/Medium); Render Blocking Requests (Opportunity/High); Network Dependency Tree (Opportunity/Low); Use Efficient Cache Lifetimes (Opportunity/Medium); Layout Shift Culprits (Opportunity/Medium); Optimize DOM Size (Opportunity/Low); Improve Image Delivery (Opportunity/High); Forced Reflow (Opportunity/Low); Legacy JavaScript (Opportunity/Low); Duplicated JavaScript (Opportunity/Low); Font Display (Opportunity/Low); Avoid Enormous Network Payloads (Opportunity/Medium); Minify CSS (Opportunity/Low); Minify JavaScript (Opportunity/Low); Reduce Unused CSS (Opportunity/Medium); Reduce Unused JavaScript (Opportunity/Medium); Reduce JavaScript Execution Time (Opportunity/Medium); Minimize Main-Thread Work (Opportunity/Medium); Request Errors | PageSpeed Insights connection |
| Mobile | Viewport Not Set (Issue/High); Target Size (Warning/Medium); Content Not Sized Correctly (Warning/High); Illegible Font Size (Warning/Medium); Contains Unsupported Plugins (Issue/High); Mobile Alternate Link (Warning/High) | Rendering (or PageSpeed) for Target Size, Content Not Sized Correctly, Illegible Font Size |
| Accessibility | Accessibility Score Poor; Accessibility Score Needs Improvement; Accessibility Score Good; Missing HTML Lang (Issue/Medium); Form Fields Without Label (Issue/High); Links Without Accessible Name (Issue/Medium); Buttons Without Accessible Name (Issue/High); Other Controls Without Accessible Name (Issue/Medium); Images Without Alt Attribute (Issue/Medium); Frames Without Title (Warning/Medium); Duplicate IDs (Warning/Low); Broken ARIA / Label References (Warning/Medium); No Main Landmark (Opportunity/Low); Skipped Heading Levels (Opportunity/Low); Positive Tabindex (Warning/Low) | Rendering for 2 filters |
| Custom Search | one filter per rule (up to 10): the pages that contain (or lack) its text | Custom Search rules |
| Custom Extraction | one column set per extractor (up to 10): the values found on each page | Custom Extraction rules |
| Custom JavaScript | one column per snippet (up to 10): the value it returned on each page | Custom JavaScript snippets + Rendering |
| Analytics | Sessions Above 0; Bounce Rate Above 70% (Warning/Low); No GA Data (Warning/Low); Non-Indexable with GA Data (Issue/Medium); Orphan URLs (Warning/Medium) | Google Analytics 4 connection |
| Search Console | Clicks Above 0; No Search Analytics Data (Warning/Low); Non-Indexable with Search Analytics Data (Issue/High); Orphan URLs (Warning/Medium); URL Is Not on Google (Issue/High); Indexable URL Not Indexed (Issue/High); URL is on Google But Has Issues (Warning/Medium); User-Declared Canonical Not Selected (Warning/Medium); Page Is Not Mobile Friendly (Warning/Medium); Rich Result Invalid (Issue/Medium) | Search Console connection (URL Inspection for 6 filters) |
| Validation | `Invalid HTML Elements In <head>` (Warning/High); `<body> Element Preceding <html>` (Warning/High); `<head> Not First In <html> Element` (Warning/High); `Missing <head> Tag` (Issue/High); `Multiple <head> Tags` (Issue/High); `Missing <body> Tag` (Issue/High); `Multiple <body> Tags` (Issue/High); HTML Document Over 2MB (Issue/High); Resource Over 2MB (Issue/High) |  |
| AI Crawlers | Search Engines Blocked Or Challenged (Issue/High); AI Assistants Blocked Or Challenged (Issue/Medium); AI Training Crawlers Blocked; Noindex For One Crawler (Issue/High); Different Content For Crawlers (Warning/Medium); Crawlers Redirected Elsewhere (Warning/Medium); Thinner Page For Crawlers (Warning/Low); Googlebot Mobile And Desktop Differ (Warning/Medium); Content Needs JavaScript (Warning/Medium); Noindex Removed By JavaScript (Issue/High); Disallowed In robots.txt For A Crawler; Browser Refused Too; Same For Every Crawler | Crawl Analysis (bot check) |
| Robots.txt | Search Engines Blocked (Issue/High); AI Assistants Blocked (Issue/Medium); AI Training Opt-Out; Content-Signal Set; AI Crawlers Kept Out Of Sections; No Sitemap Line (Opportunity/Low); robots.txt Server Error Or Unreachable (Issue/High); robots.txt Serves A Web Page (Warning/High); robots.txt Over 500 KB (Warning/Medium); No robots.txt; Disallowed For AI Crawlers (Warning/Medium); data-nosnippet Sections (Warning/Low); Noindex Only For Googlebot Or Bingbot (Warning/High); Max-Snippet Under 160 (Warning/Medium); Unavailable_After Date Passed (Issue/High); Conflicting Index And Noindex (Warning/Medium) |  |
| Hygiene | Links To Staging Or Private Hosts (Issue/High); Broken Same-Page Anchors (Warning/Low); Internal Links To HTTP (Warning/Medium); Malformed Links (Issue/Medium); JavaScript Pseudo-Links (Warning/Low); Large Page Without Compression (Warning/Medium); HTML Without Cache-Control (Opportunity/Low); Missing Doctype (Warning/Medium); No Charset Declared (Warning/Low); Non-UTF-8 Charset (Warning/Low); DOM Over 1,500 Elements (Warning/Low); Canonical Points To Another Host (Warning/High); Crawl Trap URL Pattern (Warning/Medium); Unknown URLs Answer 200 (Soft 404) (Issue/High); Infinite URL Space (Issue/High); Unknown URLs Redirect Home (Warning/Medium); Unknown URLs Cause Server Error (Warning/Low) |  |
| Parameters | Session IDs In URLs (Issue/High); Cart And Action URLs (Issue/Medium); Indexable Sort, Filter Or Search Variants (Warning/Medium); Page-Builder Pagination Traps (Warning/Medium); Session ID; Action link (cart, wishlist, reply); Tracking tag; Calendar / date; Internal search; Sort / view; Filter (faceted navigation); Language / currency; Pagination; Other parameter |  |
| Performance | Response Time Over 1 Second (Warning/Medium); Response Time Over 3 Seconds (Issue/High); Slowest 10% Of Pages; Page Weight Over 2.5 MB (Warning/Medium); Over 75 Files Loaded (Opportunity/Low); Uncacheable Files (Warning/Low); Short Cache Lifetime (Under 1 Day) (Opportunity/Low) | Check images, CSS and JavaScript for the cache filters |
| Social | Missing og:title (Opportunity/Low); Missing og:description (Opportunity/Low); Missing og:image (Opportunity/Medium); og:image Not Absolute (Warning/Low); og:url Differs From Canonical (Warning/Low); Missing twitter:card (Opportunity/Low); Duplicate og:title (Opportunity/Low) |  |
| Third Parties | Loads Over 10 Third-Party Hosts (Opportunity/Low); Tracking Tag Missing (Warning/Medium) |  |

### 12.1 When a filter says "Not checked"
A filter that could not run shows "–" instead of a count, is disabled in the filter control and says why, with a button
that fixes it when one exists: **Turn on rendering…** (Configuration > Rendering), **Connect…** / **Fetch now…** (API
Access), **Configure…** (Custom Search / Extraction / JavaScript), **Crawl Analysis settings…** (a switched-off
analysis step), or "read by an older version of the crawler: run a new crawl". The same reason appears in the
Overview, the Passed / Failed / Not checked list, the Excel workbook (a tab that did not run is written as a
"Not checked" table) and the PDF.

### 12.2 Notes on matching Screaming Frog
Checked against a Screaming Frog 24.3 crawl in `research/`:

- **Security headers** (HSTS, CSP, X-Content-Type-Options, X-Frame-Options, Referrer-Policy) are checked on every internal URL that answered 2xx, files included; X-Frame-Options passes only with `DENY` or `SAMEORIGIN`. **Protocol-Relative Resource Links** looks at resources only (images, scripts, CSS, frames), not at `<a href="//...">` links.
- **Images** lists URLs that answered with an image Content-Type (plus `<img>` sources never requested); a file without a Content-Type is *Other*, a 404 HTML page at a `.png` address is *HTML*. **Background Images** lists CSS background images; **Incorrectly Sized Images** compares each image's real size with its displayed size in Chrome.
- **Links:** a page's links to itself count as inlinks (Screaming Frog counts them too). **Inlinks / Unique Inlinks** count every hyperlink, resource reference, CSS background image and HTTP redirect.
- **Content > Near Duplicates** compares the content area's word 3-shingles (Jaccard similarity estimated with minhash, as Screaming Frog) at 90 % or more; exact duplicates are left out. Each page keeps its 50 closest matches for the details pane. **Semantically Similar** and **Low Relevance Content** are a word-based approximation (TF-IDF), no AI needed. **Soft 404 Pages** is deliberately stricter: a page about "404 cleanup" is not an error page.
- **Mobile:** Target Size, Content Not Sized Correctly and Illegible Font Size are measured in our own Chrome at a 412 × 823 phone viewport (tap targets under 24 × 24 px that crowd another target, a page wider than the screen, more than 40 % of the text under 12 px). When PageSpeed Insights tested a page as a phone, its Lighthouse verdict is used for that page instead.
- **Accessibility:** our own static checks of the HTML (and Chrome's accessibility tree when pages are rendered), with our own 0-100 score.
- **Structured Data > Schema Values Not On Page / Schema Price Not On Page:** structured data that promises something the visible page does not show.
- **Not reproduced:** Spelling & Grammar. **Link Metrics** (Majestic, Ahrefs, Moz) are prepared under API Access and come later; nothing is fetched or estimated until then. **AMP** is deprecated and not shown.

### 12.3 Beyond Screaming Frog
**AI Crawlers** (what search and AI bots can read, from the bot check), **Robots.txt** (the AI-policy audit, including
Claude-User, Perplexity-User and Meta's crawler, with Applebot following the Googlebot group), **Hygiene** (staging
links, broken anchors, malformed links, missing compression or caching, crawl-trap patterns, servers that answer 200 for
pages that do not exist), **Parameters** (what each URL parameter does and which variants waste crawl time),
**Performance** (slow pages, page weight, uncacheable files; no API key needed), **Social** (Open Graph and Twitter
Card tags) and **Third Parties** (outside services each page loads, tracking tags missing from some pages), plus a
**Crawl budget** report (`GET /api/crawl/crawl-budget`).

---

## 13. Site graph and visualisations

### 13.1 Site graph (Force-Directed Crawl Diagram)
The **Site graph** tab draws the crawled pages and their links. Three views (remembered in this browser):

- **By depth:** one band per click depth from the start page.
- **Clusters:** pages that link to each other are drawn close together; pages of one folder are pulled together a little, so a shop whose pages barely link still shows its sections. The layout settles, then stays still (a drag still moves a node); the same crawl gives the same picture.
- **Drill-down:** the collapsible tree of 13.2.

How to read it: **size** = Link Score; **colour and shape** = HTTP status (2xx green circle, 3xx amber diamond, 4xx red
triangle, 5xx purple star, no response grey, blocked by robots.txt brown hexagon), so colour is never the only cue; a
dashed rim marks a 2xx page that is not indexable; redirects are dashed arrows; above the node limit, smaller pages are
grouped by host and folder (rounded squares; click to list them).

**Names never overlap.** Each page's name (its path, shortened in the middle when long, with " · 404" for a non-2xx
status) is drawn about 11 px tall at any zoom, next to its node (below, right, above or left, whichever has room), and
only where it covers no other name, no node and no band title and is not cut by the edge of the box. The most important
names come first: the picked, hovered or found page, the start page, folder groups, the strongest pages, problem pages.
In the Clusters view every named node also keeps room for its name while the layout settles. Zoom in to see more
names; any page's full address is in its tooltip.

Controls: **Find a page in the graph** (Enter: next match, Shift+Enter: previous), **Up to 200 / 500 / 1,000 / 2,000
nodes**, zoom − / +, **Fit**, reload, **Full** screen (Esc leaves it). Hover lights a page's links; a click fades
everything but the page and its neighbours and opens its details. Large sites: at most min(3000, max(600, 4 × nodes))
links are drawn (each page keeps the link from its shallowest linking page, every redirect, then the links shared by
the most pages); "N of M links drawn" says so.

### 13.2 Drill-down tree
**Site graph > Drill-down** (also Visualisations > Drill-down Tree) shows the crawl as a horizontal, collapsible tree,
like Screaming Frog's tree graphs: the **registrable domain**, then its **hosts** (subdomains, with sub-subdomains nested
by name), then **folders**, then **pages**.

- Each node shows its name, the number of pages, a small stacked bar of the status classes (2xx / 3xx / 4xx / 5xx / no response / blocked) and how many are indexable.
- **Click** opens or closes a branch (animated); **Ctrl+click** (or the G key) lists the host's or folder's URLs in the grid.
- **Find a host, folder or page** highlights the matches and opens the way to each one (‹ › or Enter / Shift+Enter).
- **Expand** opens every branch (up to 1,500 items; deeper ones open on click), **Collapse** goes back to the domain and its hosts.
- **Pages / All URLs:** count the pages of the site graph, or every internal URL (images, scripts and PDFs too, the Internal tab's All).
- Zoom − / + (or scroll), drag to move, **Fit** (0), **Download…** as PNG or SVG (as it is open now), reload (open branches stay open), full screen.
- Keyboard: arrow keys move, Enter opens or closes, G lists the branch in the grid, + and − zoom, / finds.
- Large sites load a slice at a time: at most 50 children per node, then a "+ N more" pill. The counts always reconcile with the crawl: every page sits in exactly one leaf or "more" pill.

### 13.3 Other visualisations (Visualisations menu)
- **Crawl Tree Graph:** every crawled URL under the page it was first found on (its Parent link), depth by depth.
- **Directory Tree Graph:** the URL folders with their URL counts and the worst status inside.
- **Force-Directed Directory Tree Diagram:** the folder tree as a force layout.
- **Inlink Anchor Text Word Cloud** and **Body Text Word Cloud:** the words of the anchors pointing to the selected URL, or of its body text (select a URL in the grid first).

Each can be shown as a graph or as a list. All graphs redraw in the colours of the current theme.

---

## 14. After the crawl: checks, re-checks, fix plan, insights

### 14.1 Post-crawl checks
Run automatically after the crawl (`crawler/seo/postcrawl.py`): the robots.txt AI-policy audit
(`GET /api/crawl/robots-audit`), a random-URL probe for soft-404 servers and URL traps, and in the background a **bot
check** that fetches a sample of pages (10 by default) as a browser and as search / AI crawlers and compares what each
sees (`GET /api/crawl/bots`, the AI Crawlers tab). A stopped or replaced crawl says the bot check did not run.

### 14.2 Crawl Analysis
What is calculated after the crawl, chosen under Configuration > Crawl Analysis (or Crawl Analysis > Configure): Auto
analyse at the end of the crawl, Link Score, Sitemaps and orphan pages, Near duplicates, Semantically similar and low
relevance content, Pagination, Hreflang, robots.txt audit and crawl traps, Search and AI bot check. A switched-off step
makes its filters *Not checked*. **Crawl Analysis > Start** runs it again on the crawl on screen (for example on an
opened saved crawl, or after switching a step on); Configure can also save the steps as the server's defaults for new
crawls.

### 14.3 Passed / Failed / Not checked (`GET /api/crawl/checks`)
One row per Screaming Frog issue filter plus checks this tool cannot run (always *Not checked* with the reason).
`urls` counts only URLs the check could judge. A check the team dismissed counts as *dismissed*, not failed; a partly
dismissed check counts only the URLs left. If no URL answered, every check is *Not checked* ("the site did not
answer"). A check about something the site does not use (hreflang on a one-language site) is *Not applicable*. The
summary counts them the way the dashboard chips do: `passed + failed + not_checked + not_applicable + dismissed` =
every check.

### 14.4 Validation agent (live re-checks)
**Validate errors** (`POST /api/validate/start` with scope `errors`: every 4xx / 5xx / no-response / blocked internal URL
plus broken external links; `issues`: errors plus one sample URL per SEO issue; `urls`: the given URLs). Each URL is
requested again 3 times a few seconds apart, over HTTP and, when a site refuses automated checkers, in a real browser.
Verdicts: **confirmed** (still broken), **not reproduced** (works now), **intermittent**, **fixed** (works now or the
link is gone from the linking page). Each result says what it was checked as, what the verdict means for that URL and
whether only bots are refused. An SEO sample whose page loads but still has the issue shows **Issue still there**. A
check that needs other pages (orphans, duplicates) is *Not checked*, never "confirmed". **Recheck selected URLs** (grid)
updates those URLs' rows in the crawl (status and category) and rebuilds the tabs; the crawl's own history entry keeps
what the crawl found and the re-checked state is saved as its own entry.

### 14.5 Fix plan (`GET /api/intel/errors`)
Root-cause groups (`crawler/intel/root_cause.py`): issues that share a cause (one template, one plugin, a CMS setting,
the server, content, links, configuration) become one group with the URLs it clears, the root cause, why it happens,
how to prevent it, fix steps, platform steps for the detected CMS (WordPress, WooCommerce, Shopify, Magento, ...), effort,
impact 0-100 and a fix-first rank. Groups add up exactly to the Issues overview's URL counts. Broken pages take the
importance of the pages linking to them; with Search Console or Analytics connected, pages that get clicks or visits rank
higher. Each group says where its verdict came from ("Seen in crawl data", "Still failing on live re-check", "Gone on
live re-check"). Platform detection names a CMS only on strong evidence. **Explain with AI** (Fix plan) and **Review with AI**
(Content insights) (`POST /api/intel/run`) rewrite the text with AI when it is on (counts, ranks and verification never
change); with AI off the text stays rule-based.

### 14.6 Content insights and business insights
- **Content insights** (`GET /api/intel/content`): pages whose address promises one type of page while the page is built as another (for example a services URL that is a contact form), thin indexable pages, and pages off-topic for their section. A mismatch needs structural evidence (forms, schema, embeds, headings).
- **Business insights** (dock at the bottom, `POST /api/business/start {url}`): a compact company profile with or without a crawl: name, what they do, offerings, audience, market and positioning, competitors, contacts and socials, tech stack, an SEO snapshot from the crawl, opportunities, risks and the source of every claim. Founding year and head count come only from the company's own statements, schema.org or Wikipedia / Wikidata. Download it as the Business Insights PDF. Works without AI (site facts only).

---

## 15. Saved crawls, history and comparison

### 15.1 File > Crawls (saved crawls)
Every finished crawl is saved on the server by itself (Settings > Crawl Retention > Save every finished crawl, on by
default), or with **File > Save**. **File > Crawls** (Ctrl+O) lists them (newest first, with name, site, mode, start
time, duration, URLs, pages OK, issues, status and size) with a search box, and offers **Open**, **Rename...**,
**Project...** (group crawls; a locked project protects all its crawls), **Lock** / **Unlock** (a locked crawl is never
deleted by retention), **Duplicate**, **Delete**, **Export...** (one .zip), **Import...** and **Compare** (two ticked
crawls).

- **Open** replaces the crawl on screen: every tab, report, export, PDF and re-check works on it. It is refused while a crawl runs. The login is not saved, so re-checks of a signed-in crawl run signed out (the crawl says so).
- **What a saved crawl holds:** the settings without secrets, every URL with its SEO data and response headers (cookie values masked), every external link, sitemaps and post-crawl reports, re-check results, the site's dismissals, Google data fetched for it, page sources (View Source) and screenshots. Secret values are replaced by `***` wherever they appear.
- **Where:** `<CRAWLER_REPORTS_DIR>/crawls/<host>/<time>/`. Retention deletes crawls older than its period, and a safety cap keeps the newest 20 per site (`CRAWLER_MAX_CRAWLS`, 0 = no cap). Locked crawls, crawls of locked projects and the crawl on screen are never deleted. An import may be up to 500 MB (`CRAWLER_MAX_IMPORT_MB`).
- Without a lasting reports folder (the system temp folder, or a hosted server without a disk) saved crawls disappear at a restart or redeploy; Settings > Storage warns about it (DEPLOY.md).

### 15.2 History (snapshots)
A small snapshot of every finished crawl is kept under `<CRAWLER_REPORTS_DIR>/snapshots/<host>/` (the last 20 per site,
`CRAWLER_MAX_SNAPSHOTS`): pages and how many load, health score, issues, every filter's URLs, and Passed / Failed /
Not checked. The top bar's **History ▾** and `GET /api/crawls` list them with the saved crawls. A re-checked crawl adds
its own entry and keeps the original.

### 15.3 Compare (Mode > Compare)
Tick two crawls (the older one is the previous crawl), choose the **Change detection** elements (page title, meta
description, h1, word count, crawl depth, internal inlinks, indexability, status code, canonical, structured data
types, content similarity) with their thresholds, and optionally a **URL Mapping** (regex rules that map old addresses
to new ones, for a site migration or a staging site). The Compare view shows:

- per tab and filter: **Current**, **Previous**, **Change**, **Change %**, **Added** (URL in both crawls, newly in the filter), **New** (URL only in the current crawl), **Removed** (URL in both, no longer in the filter) and **Missing** (URL not in the current crawl). Click a number to list its URLs.
- **Issues:** new, fixed, regressed (came back after being fixed) and unchanged issue URLs. A URL that was not crawled again, or a check that did not run in the newer crawl, is **not re-checked**, never "fixed".
- **Change detection:** per element how many URLs were compared, changed, not changed or not checked; plus status code changes, title changes and URLs added and removed.

### 15.4 Dismissed issues
In the Issues panel the team can dismiss an issue, or only its URLs matching a pattern (a pattern starting with `/`
matches the path, anything else the full address), with a note. Dismissed URLs drop out of the issue counts, the fix
plan, the PDF evidence and the Excel Issue URLs sheet, and are listed as dismissed; they stay in the tabs. Stored per
site (`<CRAWLER_REPORTS_DIR>/dismissed/<host>.json`), saved with a crawl, and undone at any time.

---

## 16. Segments

**Configuration > Segments** defines named, coloured groups of URLs for a site (for example Blog, Products, Category
pages, Checkout) by rules over the crawl data: address, path, host, query, content type, status code, category,
indexability, crawl depth, title, h1, meta description, word count, response time, inlinks, Link Score, structured data,
"in filter" (any tab filter, e.g. Page Titles > Missing) or "segment" (an earlier segment). Operators: contains, does not
contain, equals, starts / ends with, regex, greater / less than, empty, in a list. Rules combine with all / any and can be
grouped. **Suggest segments** proposes segments from the crawl's page types and top folders, with their counts.

Segments are evaluated top to bottom and **the first matching segment wins**, so every URL is in exactly one segment or in
"(No segment)", and the counts always add up. A segment never changes a URL's status category. They are used by the
grid's **Segment** dropdown and column, the right panel's **Segments** tab (URLs, indexable / non-indexable, status
classes, issues by priority, average depth and response time per segment), **Reports > Segments Overview** and the
comparison. Stored per site on the server and re-applied to the crawl on screen when saved.

---

## 17. Reports and exports

### 17.1 Excel workbook (`crawler/exporter.py`)
`GET /api/crawl/report/excel?category=all` (also written at the end of every crawl): Crawl Summary (counts that add up,
limitations), Issues Overview, the follow-up sheets that have data (Fix Plan, Validation, Login Check, Checks, Content
Intelligence, Dismissed Issues), Internal URLs, Broken Links & Errors (with every page linking to each broken URL),
Broken External Links, Redirects (3xx), Blocked by robots.txt, External Links, Issue URLs (one row per SEO issue and URL,
without dismissed URLs), then one sheet per non-empty SEO tab. A tab that did not run is a short "Not checked" sheet with
the reason. Other categories (`2xx`, `3xx`, `4xx`, `5xx`, `error`, `issues`, `blocked`, `external`) give the summary plus
that scope. The page a URL was found on is the **Parent Link** column. Workbooks are written in openpyxl write-only mode
(memory stays flat), and a second click reuses the saved workbook while nothing changed.

### 17.2 Website Audit Report PDF (`crawler/pdf_report.py`)
**PDF report** (`GET /api/crawl/report/pdf`): direct and compact, every finding kept, long lists left to the Excel file.

- **Page 1:** KPI tiles that add up to the internal URLs, the status bar and its equation (2xx + 3xx + 4xx + ... = internal URLs, then the URLs crawled as the dashboard counts them), a "Partial crawl" note when it applies, **Fix first** (the five most valuable root causes with the page that explains each) and one line per area "at a glance" (broken links, SEO checks, re-check, content, connected data).
- **Then:** every SEO issue on one line (type, priority, URLs, %, a short fix, an example, the page of its picture); every broken internal URL and every broken link to another site on one line each with its re-check verdict; the fix plan (two lines per root cause); content findings; one compact table per connected Google source (Search Console, Analytics, PageSpeed); then the evidence: broken links grouped by cause and place (the site's error page shown once) and one picture per SEO issue type. It ends with a pointer to the Excel workbook.
- Dismissed issues and URLs are left out. "Parent link" names the page that holds a link.
- The first PDF after a crawl with screenshots needs the pinpointed pictures: the **PDF report** button starts them (`POST /api/crawl/pinpoint`), shows "Preparing PDF pictures n of m" and downloads the PDF when it is built. A direct request waits up to `CRAWLER_PDF_PINPOINT_WAIT` seconds (240) for them.

### 17.3 Business Insights PDF (`crawler/business_pdf.py`)
`GET /api/business/pdf`: the compact company profile of 14.6.

### 17.4 Reports, bulk exports and Multi-Export (`crawler/reports.py`)
The Reports menu (4.6) and Bulk Export menu (4.5) give each report or list as CSV or Excel (`GET /api/reports` lists
them with their row counts; `GET /api/crawl/report/{id}`, `GET /api/crawl/export/bulk?id=`). **Multi-Export** builds many
of them at once into one workbook (with an Index sheet) or a .zip of CSVs, in the background. Every grid and every
table of the URL details pane also has its own **Export** (CSV or Excel), with exactly the rows shown.

### 17.5 Sitemaps
Sitemaps > XML Sitemap / Images Sitemap (4.7), `POST /api/crawl/sitemap` (and `/preview`).

### 17.6 Fix files (`GET /api/export/fixes?format=`)
Import-ready drafts of titles, meta descriptions, image alt text and redirects: `yoast` (WP All Import CSV), `rankmath`,
`shopify` (.zip: products, collections, redirects), `htaccess`, `nextjs` (`redirects()` JSON), `csv`. With AI on, the
text is drafted by AI (for the 100 most linked pages; the request waits up to `CRAWLER_FIXES_AI_WAIT` seconds); without
it, rule-based placeholders are clearly marked `TODO`. A broken URL is never redirected to a page that links to it from
its content; the file then says which link to edit.

### 17.7 Developer tickets (`GET /api/export/tickets?format=`)
One ticket per fix-plan group: title, where, element HTML, steps, how to verify, priority with its reason, and
screenshot file names. Formats: `csv`, `jira` (Jira CSV import), `markdown`, and `zip` (all three plus a `screenshots/`
folder; download it while the crawl is current, screenshots are kept for `CRAWLER_REPORT_RETENTION_HOURS`).

---

## 18. Google connections and PageSpeed (API Access)

**Configuration > API Access** connects ARACHNIX to Google Search Console, Google Analytics 4 and PageSpeed Insights.
Nothing has to be connected. When a source is connected and its data was fetched for the crawl, its tab fills; when it
is not, every filter of that tab says **Not connected - connect under Configuration > API Access** (or "not fetched
yet"), never a fake 0. Majestic, Ahrefs and Moz are shown as *Coming later* (their Link Metrics need paid
subscriptions; nothing is fetched or estimated).

### 18.1 What each source adds

| Source | What you get | Fills |
| --- | --- | --- |
| Google Search Console | Clicks, impressions, CTR and average position per URL (Search Analytics), and Google's own index status per URL (URL Inspection: on Google or not, coverage, last crawl, the canonical Google picked, rich results) | The Search Console tab, Orphan Pages, the Fix plan ranking, a compact PDF table |
| Google Analytics 4 | Sessions, engaged sessions, engagement rate, views, key events, event count and revenue per landing page (or per page viewed), for the date range and channel you choose (for example Organic Search) | The Analytics tab, Orphan Pages, the Fix plan ranking, a compact PDF table |
| PageSpeed Insights | Core Web Vitals from real Chrome users (CrUX: LCP, INP, CLS, FCP, TTFB) and a Lighthouse lab test of each page (performance score, speed metrics, opportunities and diagnostics) | The PageSpeed tab (20 Lighthouse filters), the Lighthouse versions of the Mobile checks, Lighthouse Details per URL, a compact PDF table |

### 18.2 Connect Search Console and Analytics (Google sign-in)
1. Configuration > API Access > Google Search Console (or Google Analytics 4) > **Account Information** > **Sign in with Google**. Google's consent page opens; allow read-only access.
2. Pick the **property** (Search Console) or the **account, property and web data stream** (GA4), and press **Use this property**.
3. Choose the options: date range (default the last 30 days; Search Console data lags 1-3 days), device, search type, country and query filter, URL Inspection (with a daily limit, default 2,000, cached), URL matching rules (trailing slash, case, http / https, www) for Search Console; date range, metrics, dimension (landing page or page path) and filters for GA4.
4. After a crawl press **Fetch now** (progress is shown and can be stopped). With **Auto Connect on Start** the fetch runs after every crawl by itself. URLs that Google knows but the crawl did not find are listed as **Orphan URLs** (in the tab and in Reports > Orphan Pages); `GET /api/integrations/{source}/discovered` returns them, to crawl them in List mode. The option **Crawl New URLs Discovered In Google Search Console / Google Analytics 4** is saved with the settings but does not add those URLs to a crawl by itself yet (the dialog says so): export the Orphan URLs and crawl them with Mode > List.

**The Google sign-in needs an OAuth client** (once per server). Without one, the dialog shows these steps and the
redirect address to register:

1. Open console.cloud.google.com and pick or create a project.
2. APIs & Services > Library: enable **Google Search Console API**, **Google Analytics Admin API** and **Google Analytics Data API** (and **PageSpeed Insights API** for a PageSpeed key).
3. APIs & Services > OAuth consent screen: user type *Internal* (Google Workspace) or *External* with your team as test users; add the scopes `webmasters.readonly` and `analytics.readonly`.
4. APIs & Services > Credentials > Create credentials > **OAuth client ID** > *Web application*; add the authorised redirect URI `<server address>/api/integrations/google/callback` (for example `https://website-crawler-z8qf.onrender.com/api/integrations/google/callback`, or `http://localhost:8000/api/integrations/google/callback` on a PC).
5. Put the client ID and secret in the server's environment as `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` (then restart the server), or paste them in the dialog (**Save client**). The environment wins over the dialog.

**Office server note:** Google accepts only `https://` redirect addresses, or `http://localhost` / `http://127.0.0.1`. A
private address such as `http://10.10.6.56:8000` is refused. On the office server, either (a) connect once from the
server PC itself at http://localhost:8000 (the connection is server-wide, everyone uses it afterwards), (b) register the
localhost redirect address, sign in from another PC and paste the address the browser ends on (it shows "cannot
connect", but the address still carries the code), or (c) use a **service account**: create one in Google Cloud,
download its JSON key, add its e-mail as a user of the Search Console property and as a Viewer of the GA4 property,
then add the key in the dialog with **Use a service-account key…**. Behind a proxy or custom domain, set
`CRAWLER_PUBLIC_URL` to the address the browser uses.

### 18.3 Connect PageSpeed Insights (free API key)
1. In Google Cloud (the same project is fine): APIs & Services > Library > enable **PageSpeed Insights API**, then Credentials > Create credentials > **API key** (restrict it to the PageSpeed Insights API).
2. Paste it under API Access > PageSpeed Insights > Account Information and press **Connect**, or set `PSI_API_KEY` in the server's environment (it wins over the saved key). (`POST /api/integrations/psi/test` tests a key with one real request.)
3. Choose mobile or desktop and the metric groups, then **Fetch now** (all indexable pages up to the URL cap, default 500, or a selection). Google allows 25,000 tests a day and 240 a minute per project; ARACHNIX stays under the per-minute limit, backs off on errors and stops cleanly when the daily quota is used up (the URLs left are "Not checked: daily quota reached"). Google's keyless quota is effectively zero, so a key is needed.

Pages behind a login cannot be tested by Google: they show "Not checked: page needs login".

### 18.4 Who may connect, and secrets
Connecting, disconnecting and saving keys change the connection for everyone using the server, so they follow the
settings rule of section 5 (team access key, the server's own computer, or `CRAWLER_SETTINGS_EDITABLE=1`). Choosing a
property and fetching are open to all. OAuth refresh tokens, service-account keys and the PageSpeed key are kept on the
server only (`<CRAWLER_REPORTS_DIR>/integrations/`, owner-only file permissions where the system allows), never returned
to the browser, logged or exported; access tokens live in memory only. **Disconnect** revokes the sign-in at Google and
deletes it; data already fetched for crawls stays with those crawls.

---

## 19. Scheduling and email notifications

### 19.1 File > Scheduling
A task runs a crawl at a set time and then produces its exports, emails the team and compares with the previous crawl.
The dialog lists the tasks (task, interval, next run, last run, valid) with **+ Add**, **Edit**, **Duplicate**,
**Delete**, **Run now**, **History**, **Enable** / **Disable**, **Export...** and **Import...**. A task has four pages:

- **General:** task name, project, first run (date and time on the server; without a time zone it is the server's local time, daylight saving included), interval (once, hourly, daily, weekly, monthly; every N of them), **Wait up to (minutes)** while another crawl runs before that run is skipped (0-1440, default 60), Enabled.
- **Start options:** Spider (start URL) or List (URLs, one per line); the crawl configuration: the current Crawl Config of this browser (copied when the task is saved) or a saved profile; an optional login (below); **Auto Compare Crawls** (compare with the previous crawl of the site; the changes go into the email).
- **Exports:** save the crawl (File > Crawls), the Excel workbook, the PDF audit report, the XML sitemap, and any reports and bulk exports (Excel or CSV, skip empty ones). Files go to `<CRAWLER_REPORTS_DIR>/scheduled/<task>/<time>/`.
- **Notifications:** email addresses, attach the exports, email on failure too.

A task that cannot run (a missing profile or login variable) says why. **Run now** answers 409 with the reason while
the server is busy (a crawl, a Crawl Analysis, or a scheduled run still building its exports, at most 30 minutes) and
400 when the task is not valid. The history keeps every run (newest 1,000) with its status, notes and comparison.

### 19.2 How the scheduler runs
- One crawl runs per server: a task that is due while a crawl runs waits (up to its wait minutes, then that run is skipped and the history says so). One task starts per check (every 30 seconds).
- **The server must be awake.** A task fires only while the server runs. If the server (or the PC) was off or asleep at the due time, the task runs once as soon as the server is back and its next run is the next future time; the history notes how many runs were missed. A host that sleeps when idle (Render's free tier, serverless hosts) cannot keep a schedule; the Scheduling dialog warns about it. DEPLOY.md explains how to keep a PC or server awake.
- Only one server process per reports folder runs the tasks (a lock file); a second server on the same folder only edits them. `CRAWLER_SCHEDULER=0` switches the scheduler off on a server. Serverless hosts never run it.
- Tasks live in `<CRAWLER_REPORTS_DIR>/schedules.json`; on a host without a lasting disk they are lost at every deploy.
- Settings > Crawl Retention also prunes the scheduled-run folders (by age, and at most `CRAWLER_MAX_SCHEDULED_RUNS` per task, default 100, `0` = no cap).

**Logins in scheduled tasks** are never stored. A task names a server environment variable that holds the cookie string
or header value, and only variables whose name starts with `SITE_` can be used (for example `SITE_SESSION_COOKIE`): the
server's own keys and passwords are never sent to a website. A password-protected (HTTP Basic) site: login *Header*,
name `Authorization`, with a `SITE_` variable holding `Basic <base64 of user:password>`. Adding, importing or pointing a
task with a login at another site is allowed only by whoever may change the server's settings (section 5); changing its
time, exports or emails, or switching it on and off, stays open to everyone.

### 19.3 Email notifications (Settings > Notifications)
ARACHNIX sends email through your own SMTP server. Set it up in **Settings > Notifications** (host, port, encryption
TLS / SSL / none, username, password, from address and name, the dashboard address put in the email) and press **Send
Test Email**; or set it in the server's environment (`CRAWLER_SMTP_HOST`, `_PORT`, `_USER`, `_PASSWORD`, `_ENCRYPTION`,
`_FROM`, plus `CRAWLER_DASHBOARD_URL`): while the dialog has no SMTP host every environment value is used, otherwise the
environment only fills empty fields. Examples: Microsoft 365 `smtp.office365.com`, port 587, TLS; Gmail
`smtp.gmail.com`, port 587, TLS, with an app password.

- **Email on Crawl Complete** sends a summary to its recipients when a crawl started from the dashboard finishes: URLs by status (adding up to the total), the health score, every issue, and what changed since the previous crawl of the site; optionally the exports as a zip (up to 10 MB).
- Scheduled tasks send to their own recipients (File > Scheduling > Notifications).
- The password is never shown again, never sent to the browser and never written in an error message.

---

## 20. AI layer (free providers)

ARACHNIX works fully without AI ("AI off - site facts only"). With a free key it rewrites fix-plan text, reviews page
intent, drafts fix files and writes the business profile. Code: `crawler/ai/client.py`.

| Provider | Key (environment variable) | Notes |
| --- | --- | --- |
| Google Gemini free tier | `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) | First choice. Get a key at Google AI Studio (aistudio.google.com, "Get API key") |
| Groq free tier | `GROQ_API_KEY` | Used when Gemini's quota runs out (console.groq.com, API Keys) |
| OpenRouter free models | `OPENROUTER_API_KEY` | Very low daily limits; used last |
| Ollama (local) | `OLLAMA_BASE_URL` | Fully private |
| Anthropic (paid, optional) | `ANTHROPIC_API_KEY` | Only when the SDK is installed |

- `CRAWLER_AI_PROVIDER` = `auto` (every configured provider in the order above, falling through on quota or errors), a name or comma list, or `off`. `CRAWLER_AI_MODEL` / `CRAWLER_AI_BULK_MODEL` choose the first provider's models.
- Budget per crawl: `CRAWLER_AI_MAX_CALLS` (40) requests, `CRAWLER_AI_RPM` (8) requests per minute. A new crawl starts a new budget and stops AI work of the previous one.
- **Privacy:** free tiers may use what they receive. Text from signed-in crawls is never sent to a free cloud provider unless `CRAWLER_AI_ALLOW_PRIVATE=1`; page text is wrapped as data, never as instructions. Keys never appear in logs or errors.
- `GET /api/ai/status` returns `{enabled, model, bulk_model, reason, usage}`; the dock's AI chip shows it.
- On Windows set the keys as **user** environment variables, then open a new terminal before starting `run.py`; on Render add them in the service's Environment tab.

---

## 21. REST API reference

All endpoints return valid empty shapes when there is no crawl or job; long work runs in background threads. With
`CRAWLER_ACCESS_KEY` set, every `/api/*` endpoint except `/api/health`, `/api/access` and Google's return address
`/api/integrations/google/callback` needs the team access key (22.2). Endpoints that change server-wide settings or connections need the right to edit settings (section 5).

### 21.1 Server and crawl

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/healthz`, `/api/health` | Health check (always open) |
| GET | `/api/access` | Whether this server wants the team access key, and whether the key sent is valid (always open) |
| GET | `/api/environment` | Platform, Chrome status, the server's URL cap, serverless or not |
| GET | `/api/about`, `/api/help/guide`, `/api/help/notices`, `/api/debug`, `/api/usage` | Help menu: About, this guide, third-party notices, Debug (secrets masked; `download=1` as a file), Usage Stats |
| POST | `/api/crawl/start` | Starts a crawl (body in 21.9). 400 for a bad option or while another crawl runs; `force: true` replaces a running crawl |
| GET | `/api/crawl/status?since=&ext_since=` | Live state, stats, new rows after the cursors, logs, `jobs`, `snapshot_id`, `challenge` (10.5) |
| POST | `/api/crawl/pause`, `/resume`, `/stop` | Pause / resume / stop (stop during analysis skips the remaining screenshots) |
| POST | `/api/crawl/reset`, `/api/crawl/close` | Clear the crawl on screen for everyone / take it off the screen without deleting it |
| POST | `/api/crawl/direct` | Synchronous small crawl for serverless hosts (100 URLs at most); 409 while another crawl runs |
| POST | `/api/list/parse` | List mode: a pasted list, an uploaded file (.txt, .csv, .xlsx, sitemap .xml) or a sitemap address into the URLs to crawl |
| POST | `/api/config/test-url`, `/api/config/validate` | Test a URL against Include / Exclude / URL Rewriting; check every crawl option without starting |
| GET | `/api/crawl/analysis`; PUT `/api/crawl/analysis/config`; POST `/api/crawl/analysis/start`, `/stop` | Crawl Analysis state, steps (and server defaults), start, stop |

**`stats` highlights:** `status_2xx` … `status_err`, `status_blocked` (add up to the URLs crawled), `health_score`,
`pages_total`, `pages_ok`, `files_total`, `auth_used`, `auth_status`, `auth_error`, `session_lost`,
`not_crawled_unsafe`, `not_crawled_max_urls`, `not_crawled_max_depth`, `not_crawled_memory`, `not_crawled_traps`,
`limit_reached`, `no_answer`, `waf`, `throttle`, `limitations` (plain sentences).

### 21.2 SEO tabs, URL details and structure

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/api/crawl/tabs` | Tab bar and filter tree with counts, issue types, priorities and not-checked reasons |
| GET | `/api/crawl/tab/{tab}?filter=&match=&q=&sort=&dir=&offset=&limit=&view=&segment=` | One page of a tab grid. `filter=a,b,c` combines filters: `match=any` (default) or `match=all`; `filter_total` is the distinct count |
| GET | `/api/crawl/url?u=` | Everything about one URL (details pane) |
| GET | `/api/crawl/source`, `/api/crawl/render`, `/api/crawl/console`, `/api/crawl/cookies` (each `?u=`) | View Source, Rendered Page (`refresh=1` renders now), Chrome Console Log, cookies of one URL |
| GET | `/api/crawl/issues-overview`, `/api/crawl/checks`, `/api/crawl/issues` | Issues overview; Passed / Failed / Not checked; broken URLs and links with diagnosis and screenshots |
| GET | `/api/crawl/bots`, `/api/crawl/robots-audit`, `/api/crawl/crawl-budget` | Post-crawl reports |
| GET | `/api/crawl/structure`, `/api/crawl/response-times`, `/api/crawl/response-times/urls`, `/api/crawl/wordcloud` | Site Structure, Response Times (buckets add up to the total) and its URLs, word clouds |
| GET | `/api/crawl/graph?limit=500&type=force` | Site graph (`type=crawl_tree` or `dir_tree` for the tree visualisations) |
| GET / POST | `/api/crawl/graph/tree?node=&depth=2&limit=50&offset=&files=` | Drill-down tree slice: node with `count`, `categories`, `worst_category`, `indexable_count`, `children`, `more` |
| GET | `/api/crawl/graph/tree/search?q=` | Drill-down search: matches with the path of node ids to open |
| GET | `/api/crawl/evidence/{name}`; GET / POST `/api/crawl/pinpoint` | One screenshot; state of / start the pinpointed issue pictures |

### 21.3 Sign-in, cookies and subdomains

| Method | Endpoint | Answer |
| --- | --- | --- |
| POST | `/api/auth/inspect` | Login pre-check of a URL |
| POST | `/api/auth/test` `{start_url, auth}` | `{ok, method, landing_url, cookies_count, storage_origins, message, screenshot, challenge}` |
| POST | `/api/auth/cookies/preview` `{cookies, start_url}` | `{format, count, usable, cookies: [{name, domain, path, secure, http_only, expires, expired, other_site, kind, value: "***"}], user_agent, warnings, error, storage_origins}`; no request is made |
| POST | `/api/auth/cookies/test` `{cookies, start_url, test_url?, user_agent?}` | `{signed_in: true / false / null, status, final_url, reason, test_url, user_agent_used, challenge}` |
| POST | `/api/live/start` `{url, width?, height?}` | `{session_id, width, height, challenge}` (at most 2 windows at once) |
| GET | `/api/live/frame`, `/api/live/challenge` (header `X-Live-Session`) | JPEG of the window; whether it shows a bot check now |
| POST | `/api/live/action`, `/api/live/finish`, `/api/live/close` | Drive the window; keep its login for the next crawl; close it |
| POST | `/api/subdomains/discover` `{url, sources, extra_hosts}` | `{job_id, domain}`; sources: `links`, `sitemaps`, `ct`, `gsc` |
| GET / POST | `/api/subdomains/discover/{job_id}`, `.../stop` | `{state, domain, hosts: [{host, sources, resolves, status, title, note}], errors, sources}`; stop a search |
| GET | `/api/subdomains/known`, `/api/crawl/subdomains` | What earlier searches found; the Subdomains report `{domain, hosts: [{host, crawled, linked_from, in_scope, reason_code, reason}], totals}` |

The `auth` object: `enabled`, `auth_type` (`none`, `form`, `cookies`, `header`, `live`), `login_url`, `username`,
`password`, `cookies` (any paste format of 10.2, or a list / object), `custom_header_name`, `custom_header_value`,
`live_session_id`, optional `username_selector`, `password_selector`, `submit_selector`, and `user_agent` (the browser
User-Agent kept from a paste). A header value with a line break is refused (400).

### 21.4 Follow-up jobs

| Method | Endpoint | Answer |
| --- | --- | --- |
| POST | `/api/validate/start` `{scope, urls}`; `/api/crawl/recheck` `{urls}` | `{job_id}`; a recheck updates the crawl rows when it finishes |
| GET | `/api/validate/status` (`merged=1`: every run of this crawl) | `{state, done, total, summary, results {url: {verdict, attempts, link_on_parent, parent, checked_at, note, checked_as, meaning, bot_wall}}, skipped, login_guard}` |
| GET | `/api/intel/errors`, `/api/intel/content` | Fix plan groups; content insights |
| POST / GET | `/api/intel/run`, `/api/intel/status` | AI enrichment job |
| POST / GET | `/api/business/start` `{url?}`, `/api/business/status`, `/api/business/pdf` | Business insights job, its report, the PDF |
| GET | `/api/ai/status` | AI provider state |

### 21.5 Saved crawls, history, compare, dismissals, segments

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/api/crawls?site=` | History: snapshots and saved crawls, newest first |
| GET | `/api/crawls/saved?q=&site=&project=`, `/api/crawls/storage` | File > Crawls list; Settings > Storage |
| POST | `/api/crawls/save`, `/api/crawls/import` | Save the crawl on screen; import a crawl .zip |
| GET / POST | `/api/crawls/{host}/{ts}`, `.../open`, `.../rename`, `.../project`, `.../lock`, `.../duplicate` | One saved crawl and its actions (`open` is refused while a crawl runs) |
| DELETE / GET | `/api/crawls/{host}/{ts}`, `.../export` | Delete (locked: refused unless `force=1`); export as .zip |
| POST | `/api/crawls/projects/lock` | Lock or unlock a project |
| POST | `/api/crawl/compare` `{a, b, url_map?, change_detection?, thresholds?}` | Per filter Current / Previous / Added / New / Removed / Missing, change detection, issue changes (`b` may be `current`) |
| GET / POST | `/api/crawl/compare/urls`, `/api/crawl/compare/options` | The URLs behind one number; the change detection elements and default thresholds |
| POST / DELETE / GET | `/api/issues/dismiss`, `/api/issues/dismiss?id=`, `/api/issues/dismissed` | Dismissals of the site |
| GET / PUT / DELETE | `/api/crawl/segments` | The site's segments and URLs per segment; save (re-segments the crawl); delete |
| GET | `/api/crawl/segments/suggest`, `/issues`, `/counts` | Suggestions; issues × segments; one filter's URLs per segment |

### 21.6 Files

| Method | Endpoint | File |
| --- | --- | --- |
| GET | `/api/crawl/report/excel?category=` | Excel workbook (17.1) |
| GET | `/api/crawl/report/pdf` | Website Audit Report PDF (17.2) |
| GET | `/api/reports` | The Reports, Bulk Export and Multi-Export catalogue, with row counts and not-checked reasons |
| GET | `/api/crawl/report/{id}?format=csv` (or `xlsx`) | One report of the Reports menu |
| GET | `/api/crawl/export/bulk?id=` | One bulk export (`queued_urls`, `links_all_inlinks`, ..., `filter:<tab>:<filter>`, `inlinks:<tab>:<filter>`, `issue:<tab>:<filter>`) |
| POST / GET | `/api/crawl/export/multi`, `/api/crawl/export/multi/file` | Multi-Export job and its file |
| GET | `/api/crawl/export/tab?tab=&filter=&match=&q=&sort=&dir=&segment=&format=` | One grid exactly as shown (CSV or Excel) |
| GET | `/api/crawl/export/detail?u=&part=` | A details-pane table (inlinks, outlinks, images, resources, headers, cookies) |
| GET | `/api/crawl/export/issues`, `/all-issues`, `/inlinks`, `/filter-inlinks?tab=&filter=`, `/queued`, `/list` | Older bulk exports, queued URLs, List mode URLs in upload order |
| POST / GET | `/api/crawl/sitemap`, `/api/crawl/sitemap/preview`, `/api/crawl/sitemap/options`, `/api/crawl/export/sitemap` | XML or image sitemap with options; preview; defaults; plain download |
| POST / GET | `/api/serp/measure`, `/api/serp/upload`, `/api/serp/export`, `/api/serp/crawl` | SERP mode: measure rows, upload a file, export, the crawl's titles as rows |
| GET | `/api/export/fixes?format=`, `/api/export/tickets?format=` | Fix files (17.6), developer tickets (17.7) |
| GET | `/api/crawl/export`, `/api/crawl/download/{filename}` | The saved workbook / a saved report by name |

### 21.7 Settings, profiles, scheduling

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/api/settings` | Every server-wide settings page; secrets only as `{"set", "source"}`; whether this viewer may edit |
| PUT / DELETE | `/api/settings/{page}` | Save one page (400 with `{"errors": {field: message}}`); restore its defaults |
| POST | `/api/settings/proxy/test`, `/api/settings/notifications/test` | Test the proxy; send a test email |
| GET / POST | `/api/profiles`, `/api/profiles/import` | List; import a profile file |
| GET / PUT / DELETE | `/api/profiles/{name}`, `/api/profiles/{name}/export`, `/api/profiles/_default` | One profile; export it; the default configuration |
| GET / POST | `/api/schedules`, `/api/schedules/export`, `/api/schedules/import`, `/api/schedules/history` | Tasks with next run and validity; add; export / import; every run |
| GET / PUT / DELETE | `/api/schedules/{id}` | One task |
| POST | `/api/schedules/{id}/duplicate`, `/enable`, `/run` | Duplicate, switch on / off, run now (409 while busy) |
| GET | `/api/schedules/{id}/history` | The runs of one task |

### 21.8 Integrations

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/api/integrations` | What the API Access dialog shows (no secrets): per source connected or why not, accounts, chosen property, options, running fetch, Google set-up steps and the redirect address |
| GET | `/api/integrations/google/connect`; `/api/integrations/google/callback` | Start "Sign in with Google" (consent page address); where Google sends the browser back |
| POST | `/api/integrations/google/paste`, `/api/integrations/google/service-account` | The address landed on after consent; a service-account key |
| PUT | `/api/integrations/google/client` | Save the OAuth client (the secret is never returned) |
| PATCH / DELETE | `/api/integrations/google/accounts/{id}` | Rename, auto-connect; disconnect (revokes at Google) |
| GET | `/api/integrations/gsc/properties`, `/api/integrations/ga4/accounts`, `/api/integrations/ga4/streams` | Property pickers |
| POST | `/api/integrations/{source}/select` | Choose the account / property / data stream |
| PUT / DELETE / POST | `/api/integrations/psi/key`, `/api/integrations/psi/test` | Save / remove the PageSpeed key; test it with one real request |
| POST | `/api/integrations/{source}/fetch`, `/stop`; `/api/psi/run` | Fetch the source's data for the crawl on screen (background job); stop it |
| GET | `/api/integrations/jobs`, `/api/psi/status`, `/api/integrations/{source}/discovered` | Fetch progress; URLs Google knows that the crawl did not find |

### 21.9 Crawl start options
`POST /api/crawl/start` body (dashboard control in brackets):

| Field | Default | Meaning |
| --- | --- | --- |
| `url` | — | Start URL (List mode: optional) |
| `mode`, `urls`, `list_max_depth` | `spider`, —, 0 | List mode: the URLs (a list or one per line) and the clicks followed from them |
| `crawl_scope` | `subdomain` | `subdomain`, `subfolder`, `all_subdomains`, `exact_url` (`subdomains: true` = all_subdomains) |
| `extra_hosts` | — | Hosts of the domain to crawl too (Find subdomains; Spider mode) |
| `max_urls`, `max_depth` | 0, 50 | Max URLs (0 = no limit; the server cap lowers it), Max depth |
| `concurrency`, `max_urls_per_second`, `delay`, `timeout` | 6, 0, 0, 15 | Speed and timeouts |
| `user_agent`, `custom_user_agent` | `chrome` | A preset (`chrome`, `googlebot_desktop`, `googlebot_smartphone`, `bingbot`, `gptbot`, `chatgpt_user`, `oai_searchbot`, `claudebot`, `perplexitybot`, `applebot`, `duckduckbot`, `yandexbot`) or `custom` |
| `include_patterns`, `exclude_regex` | — | Regexes (list or one per line) |
| `url_rewrite` | tracking parameters removed, sorted | `{remove_params, remove_all_params, regex_replace, lowercase, sort_params, percent_encoding}` |
| `request_headers` | — | `[{name, value}]` sent to the crawled site only |
| `custom_searches`, `custom_extractors`, `custom_js` | — | Up to 10 each |
| `render_all`, `store_source` | false, true | Render every page in Chrome; keep the HTML for View Source |
| `respect_robots`, `use_sitemaps`, `crawl_resources`, `collect_external`, `check_external_links` | true (robots off in List mode) | What to crawl |
| `enable_spa_mode`, `use_dynamic_js` | false, true | JavaScript app mode; find links in JavaScript files |
| `verify_ssl`, `screenshots`, `loop_guard`, `bot_check` | false, true, true, true | Advanced switches |
| `analysis` | the server's defaults | Crawl Analysis steps |
| `max_page_size_kb`, `max_links_per_page` | 5120, 10000 | Per-page limits (0 = none) |
| `name`, `project` | — | Name and project of the saved crawl |
| `force` | false | Replace a running crawl |
| `auth` | — | Sign-in (21.3) |

---

## 22. Server settings and environment variables

Step-by-step deployment, instance sizes, checks and troubleshooting: **DEPLOY.md**. In short: the backend runs on
Render as a Docker web service (`Dockerfile`, `render.yaml`; exactly one instance, Standard 1c-2g recommended because
Chrome needs about 1 GB), or on an office PC; the dashboard on Vercel (`frontend/`, `vercel.json`) or served by the
backend itself.

### 22.1 Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL` | — | AI providers (section 20) |
| `CRAWLER_AI_PROVIDER`, `CRAWLER_AI_ALLOW_PRIVATE`, `CRAWLER_AI_MAX_CALLS`, `CRAWLER_AI_RPM` | auto, 0, 40, 8 | AI behaviour |
| `CRAWLER_AI_MODEL`, `CRAWLER_AI_BULK_MODEL`, `CRAWLER_AI_TIMEOUT`, `CRAWLER_AI_MAX_WAIT`, `CRAWLER_FIX_AI_CALLS`, `CRAWLER_FIXES_AI_WAIT` | provider's, 60, 60, 5, 90 | AI models and timing |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | — | Google OAuth client for Search Console and GA4 (18.2); wins over the client saved in the dialog. The secret is a secret |
| `PSI_API_KEY` | — | PageSpeed Insights key (18.3); wins over the saved key. A secret |
| `CRAWLER_PUBLIC_URL` | — | The address browsers use for this server, when a proxy or custom domain hides it (used for the Google redirect address) |
| `CRAWLER_SMTP_HOST`, `CRAWLER_SMTP_PORT`, `CRAWLER_SMTP_USER`, `CRAWLER_SMTP_PASSWORD`, `CRAWLER_SMTP_ENCRYPTION`, `CRAWLER_SMTP_FROM` | —, 587, —, —, tls, — | Email server (19.3; `SMTP_*` names are read too). The password is a secret |
| `CRAWLER_DASHBOARD_URL` | — | Dashboard address put in notification emails |
| `CRAWLER_REPORTS_DIR` | system temp folder (`/var/data` in the Docker image) | Where saved crawls, history, settings, profiles, schedules, segments, dismissals, Google connections and page sources are kept (22.3) |
| `CRAWLER_MAX_CRAWLS`, `CRAWLER_MAX_SNAPSHOTS` | 20, 20 | Saved crawls / history entries kept per site (0 = no cap) |
| `CRAWLER_MAX_IMPORT_MB`, `CRAWLER_MAX_UPLOAD_MB`, `CRAWLER_MAX_LIST_URLS` | 500, 20, 10000 | Largest crawl import; largest List / SERP upload; URLs per List mode crawl |
| `CRAWLER_SCHEDULER` | on | `0` switches the scheduler off on this server |
| `CRAWLER_MAX_SCHEDULED_RUNS` | 100 | Scheduled-run export folders kept per task (0 = no cap) |
| `CRAWLER_SETTINGS_EDITABLE` | — | `1` = every browser may change server-wide settings and connections even without an access key |
| `MAX_CHROME_INSTANCES` | no limit | Chromes at once (2 on a 2 GB server, 1 on 512 MB) |
| `RENDER_POOL_SIZE` | automatic | Chromes used by Render every page in Chrome |
| `CRAWLER_NO_CHROME` | — | `1` = no Chrome at all (512 MB servers) |
| `CHROME_MIN_FREE_MB`, `CHROME_KILL_FREE_MB` | 300, 60 | Chrome is not started below the first, and closed below the second, amount of free memory |
| `CRAWLER_MAX_URLS` | 0 | Server-wide URL cap per crawl (0 = none); shown in Crawl Config |
| `CRAWLER_MIN_FREE_MB` | 400 | Memory guard: below this much free memory no more URLs are queued (the crawl says so) |
| `CRAWLER_CPU_SLOTS` | 1 | Pages parsed and analysed at the same time |
| `CRAWLER_BOT_CHECK_SAMPLE` | 10 | Pages fetched as each crawler by the bot check |
| `CRAWLER_REPORT_RETENTION_HOURS` | 72 | Excel / PDF files and screenshots of the current crawl are deleted after this |
| `CRAWLER_MAX_SCREENSHOTS`, `CRAWLER_PINPOINT_WORKERS` | 25, automatic | Broken links screenshotted per crawl; Chromes for the pinpointed pictures |
| `CRAWLER_PDF_PINPOINT_WAIT` | 240 | Seconds a PDF request waits for the pinpointed pictures |
| `CRAWLER_ACCESS_KEY` | — | Team access key for `/api/*` (22.2); strongly recommended on any public server |
| `CRAWLER_CORS_ORIGINS` | — (every origin) | Comma-separated websites allowed to call the API from a browser, e.g. the Vercel dashboard |
| `CRAWLER_TRUST_PROXY`, `CRAWLER_CLIENT_IP_HEADER` | — | Which visitor address counts for wrong-key lock-outs behind a proxy |
| `APP_VERSION` | git commit | Version shown in Help > About |
| `SITE_...` | — | Login values for scheduled tasks (19.2); only names starting with `SITE_` can be used |

Already set in the Docker image: `CHROME_BIN`, `CHROMEDRIVER_PATH`, `PYTHONUNBUFFERED=1`, `MALLOC_ARENA_MAX=2`,
`CRAWLER_REPORTS_DIR=/var/data`. Render sets `PORT` itself.

### 22.2 Team access key
Set `CRAWLER_ACCESS_KEY` on a public server (steps in DEPLOY.md 2.3). Unset, as on a PC, nothing changes (and
server-wide settings can only be changed from the server's own computer). Set, every request to an `/api/` address needs it in the
`X-Access-Key` header (compared in constant time); a missing or wrong key answers `401 {"detail": "Access key
required"}`. `/healthz`, `/api/health`, `/api/access`, Google's return address `/api/integrations/google/callback`
(protected by its own one-time state) and the dashboard page stay open. The addresses the browser opens
itself (PDF, Excel, exports, fix files, tickets, screenshots, the live window's frame) also accept `?key=`; the access
log masks its value. After 10 different wrong keys from one address within 10 minutes that address gets `429` for 10
minutes (in memory; an IPv6 address counts as its /64). The address is the direct peer, or on Render
`CF-Connecting-IP`, with `CRAWLER_TRUST_PROXY=1` the last `X-Forwarded-For` entry, with `CRAWLER_CLIENT_IP_HEADER` that
header. With the right key `/api/access` also returns a **pass** (an HMAC of the key with a secret made at start-up)
that works like the key, also while the address is locked out, and ends when the key changes or the server restarts.
The dashboard asks for the key in a **Team access key** dialog (**Remember on this device**: kept in the browser,
otherwise until the tab closes), then sends the pass with every call.

### 22.3 What the reports folder holds
`CRAWLER_REPORTS_DIR` keeps everything that should survive a restart: `crawls/` (saved crawls), `snapshots/`
(history), `dismissed/`, `segments/`, `profiles/`, `settings.json` and `secrets.json` (server-wide settings; secrets kept
apart), `schedules.json` and `schedules_history.json`, `scheduled/` (scheduled-run exports), `integrations/` (Google
sign-ins, the PageSpeed key, quotas), `sources/` (page HTML for View Source) and `scheduler.lock`. Use a folder that
lasts: on a PC a normal folder (not the temp folder), on Render a persistent disk (DEPLOY.md 2.4). Back it up like any
other team data; `secrets.json` and `integrations/` hold secrets.

---

## 23. Local development, CLI and tests

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe run.py --port 8000                      # this PC only
.\venv\Scripts\python.exe run.py --host 0.0.0.0 --port 8000       # also reachable from the office network
```

Open http://127.0.0.1:8000. Chrome must be installed for screenshots, browser logins and rendering. `run.py` prints
the network address to share when it listens on `0.0.0.0`.

**Dashboard edits:** `index.html` is one file (vanilla JavaScript, no build). Copy every change to
`frontend/index.html` and `web/static/index.html` (the three must stay identical) and check the inline script with
`node --check`:

```powershell
Copy-Item index.html frontend\index.html -Force; Copy-Item index.html web\static\index.html -Force
# after changing a brand file in web\static\brand:
Copy-Item web\static\brand\* frontend\static\brand\ -Force
```

The dashboard reads the backend address from `window.CRAWLER_API_BASE_URL` (`config.js`) when hosted; served by the
backend itself it uses its own origin. A copy of the crawl stays in the browser if the server restarts.

**CLI** (`cli.py`, also `python run.py --cli ...`):

```powershell
.\venv\Scripts\python.exe cli.py https://example.com/ --max-urls 500 --pdf
.\venv\Scripts\python.exe cli.py https://example.com/ --username me@example.com --password - --validate --intel
.\venv\Scripts\python.exe cli.py https://app.example.com/ --cookies cookies.txt --test-cookies /dashboard
.\venv\Scripts\python.exe cli.py https://example.com/ --find-subdomains
.\venv\Scripts\python.exe cli.py --list urls.txt --report redirect_chains --report orphan_pages --format csv
```

Flags: `-o/--output`, `-m/--max-urls` (default 1000), `-d/--max-depth` (10), `-t/--threads` (8), `--rate`,
`--user-agent`, `--include`, `--exclude-regex`, `--no-resources`, `--no-external-check`, `--no-sitemap`,
`--no-external`, `--dynamic`, `--delay`, `--subdomains`, `--verify-ssl`, `--ignore-robots`, `--category`, `--pdf`,
`--no-screenshots`, `--username`, `--password` (`-` or leaving it out reads `CRAWLER_PASSWORD`, so the password stays out
of the shell history), `--login-url`, `--cookies FILE` (`-` = stdin; any paste format of 10.2; values never printed),
`--use-pasted-user-agent`, `--test-cookies [PAGE]` (only test the cookies, then exit), `--subdomain HOST` (repeatable),
`--find-subdomains`, `--scope`, `--list FILE`, `--render-all`, `--header "NAME: VALUE"` (repeatable), `--search TEXT`,
`--extract-css NAME=SELECTOR`, `--report ID` (repeatable; `--list-reports` shows the ids), `--format xlsx` or `csv`,
`--sitemap`, `--save-crawl` (open it later under File > Crawls), `--no-source`, `--validate`, `--intel`, `--business`,
`--bot-check`.

**Parity check against Screaming Frog:** `python -m tools.parity --sf "<export folder>" --live https://site/` compares
coverage, status codes, redirect targets and titles with a Screaming Frog `internal_all.csv` export.

**Tests** are kept outside the repository (scratch scripts). Each starts its own fixture site and, where needed, the API
in-process on a free port: engine and reports, API end to end, the feature suite (logins, cookie paste, logout traps,
validation, intel, AI with a fake provider, compare, dismiss, exports, saved crawls, scheduling, segments) and
integrations against local fake Google endpoints (their base addresses can be overridden by environment variables for
tests, and `INTEGRATIONS_BACKOFF_BASE` shortens the back-off).

---

## 24. Troubleshooting

| Problem | What to do |
| --- | --- |
| "Server offline · reconnecting…" | Render's free tier sleeps; the dashboard reconnects by itself (up to a minute). A restart or deploy also clears the crawl on screen (a saved crawl can be opened again from File > Crawls) |
| "The site did not answer" | No URL got an HTTP answer: check the address, that the site is up and that it does not block the server's IP |
| "A bot check is in the way" | The site (or its sign-in) shows a CAPTCHA or challenge. Sign in with your own browser and use **Paste cookies**, or, if you own the site, allow ARACHNIX with an IP or secret-header rule (10.5) |
| Pasted cookies: "Not signed in" | The cookies expired or you signed out: sign in again, copy fresh cookies right away, test again. Try **Use this User-Agent** when the paste carried one |
| Login fails with a one-time code or SSO | Use **Browser window** (live login) or **Paste cookies** |
| "Login not confirmed" | No members-only page was found to prove the login; the crawl still ran. Set the login URL, or check that the crawl reaches a members-only page |
| A subdomain is missing | No crawled page links to it, or the scope leaves it out: use **Find subdomains**, or see Site Structure > Subdomains for the reason |
| Partial crawl | Raise Max URLs / Max depth, or crawl one section at a time with an Include pattern; on a small server the memory guard may stop queuing |
| Few pages found on a React / Vue / Next.js site | Turn on **JavaScript app mode**, or **Render every page in Chrome** |
| A tab or filter says "Not checked" | Its reason says what it needs: rendering, a connection, custom rules or a Crawl Analysis step; the button next to it fixes it (12.1) |
| "Not connected" in Search Console / Analytics / PageSpeed | Connect under Configuration > API Access, then **Fetch now** (section 18) |
| Settings or API Access are read-only | This browser may not change server-wide settings: use the server's own computer (http://localhost:8000), set a team access key, or set `CRAWLER_SETTINGS_EDITABLE=1` (section 5) |
| Many 403s mid-crawl | The site's firewall blocked the crawler (a banner says so): lower the speed, change the User-Agent or ask the site owner to allow it, then **Validate errors** |
| A scheduled crawl did not run | The server or PC was off or asleep (it runs once when the server is back), another crawl ran longer than the task's wait minutes, or the task is not valid; see File > Scheduling > History |
| No email arrives | Settings > Notifications > **Send Test Email** shows the SMTP error in plain words; check the spam folder |
| Saved crawls or schedules vanished | The reports folder was not lasting (temp folder, or a hosted server without a disk): set `CRAWLER_REPORTS_DIR` to a lasting folder (DEPLOY.md) |
| AI shows "AI off" | Set `GEMINI_API_KEY` or `GROQ_API_KEY` (section 20) and restart the server; check `GET /api/ai/status` |
| PDF takes long the first time | The pinpointed pictures are made first (progress in the toast); later downloads are fast |

## 25. ARACHNIX 2.0 (in development)

Everything in this section is built and tested, and switched on by default (`ARACHNIX_V2_FEATURES=0` hides the
new tabs again). With every new option at its default, a crawl gives the same results as in 1.0, plus the new tabs.

### 25.1 Where to find it (dashboard)

| Menu | What it does |
|---|---|
| Configuration > **Connections** | Slack, Teams, Discord, webhooks, Jira, GitHub Issues, IndexNow, LanguageTool, Green Web, Moz / Majestic / Ahrefs, Google Sheets and custom MCP servers: Connect, Test, Disconnect. Secrets are stored on the server and shown only as "set". Admin only (this PC, or anyone with the team access key). |
| Configuration > **AI agents (MCP)** | Turn on the MCP endpoint for Claude, Cursor and VS Code, create tokens (shown once), copy the client snippet, download the stdio bridge, test the connection. |
| Configuration > **Preferences & Spider options** | Thresholds (title, description, URL, H1 / H2, alt text, image size, outlinks, crawl depth, file sizes, bounce rate, carbon rating), follow switches (canonicals, pagination, hreflang, refreshes, JavaScript redirects, iframes, mobile alternates, nofollow), resource types, limits (per depth, folder depth, query strings, per host, redirect hops), cookie storage, assume HTML, ignore non-indexable URLs for on-page issues, carbon rating on / off, the axe-core accessibility audit, extra soft 404 phrases and generic anchors, extra sitemaps, a custom robots.txt and its tester. Kept in this browser and sent with every crawl you start (and saved in profiles). |
| Help > **Ask ARACHNIX** | A chat about the crawl on screen. It calls ARACHNIX's own tools; anything that changes something (start or stop a crawl, build a report) waits for your Allow. 20 questions per person per hour. Needs the free AI layer (section 20). |
| Help > **Site trend history** | Every saved crawl of the site: URLs per status class (they add up), indexable pages, issues by type, health score. A crawl saved without a snapshot is a gap, never 0. |
| Help > **Browser extension** | Download the ARACHNIX Companion for Chrome / Edge and the install steps. It hands your signed-in session for one site to the next crawl (10 minutes, used once, that site only). |
| Configuration > **AI prompts** | Your own AI questions for every page (7 presets or your own); Run fills the AI tab. A signed-in crawl is never sent to a free cloud AI. |
| Configuration > **Log files (server logs)** | Upload an access log (Apache / Nginx / IIS, .gz, up to 200 MB): Googlebot, Bingbot and AI bot hits per URL, fake bots, Log Orphans. Counts only, the file is deleted after reading. |
| Configuration > **SEO tools** | Semantic search, n-grams of a page, topic clusters, segment tester, profile compare, Google Sheets export of the tab on screen, JavaScript snippet library. |
| File > **Scheduling** | New: API data fetched and waited for before the exports; List-mode tasks can read their list from an XML sitemap or a Google Sheet at every run. |
| Lower pane | New tabs for the selected URL: Structured Data Details, Accessibility Details, Spelling & Grammar (with "Add to dictionary"), Carbon, PDF Details. A section that did not run says why. |
| Lower pane > **SERP Snippet** | Desktop, Tablet and Mobile previews, and a box to try your own title and description (re-measured as you type, not saved). Tablet and mobile are estimates. |
| Lower pane > **View Source** | **Line diff**: the lines JavaScript added or removed (original against rendered HTML), when the page was rendered in Chrome. |
| Configuration > **Custom Search** | Search in HTML, page text, head, body, page text without links or one element (CSS or XPath); **Bulk add** (one search per line: the text, or a name and the text separated by ` \| ` or a tab). |
| Configuration > **Custom Extraction** | **Pick from a page…**: the crawled page without its scripts (an iframe sandboxed without `allow-scripts`); click a value to get its CSS selector and XPath, "every element like this one", and a preview made by the crawl's own extractor. Regex extractors: read the HTML or the page text, match case or not. |
| Configuration > **Custom JavaScript** | Add a ready snippet from the library. |
| Mode > **Compare** | Download the comparison report (Excel), submit the new and changed URLs to IndexNow (a dry run says how many and which engines first), five more change checks (unique inlinks, internal and external outlinks and their unique counts) and the reason a check is "Not comparable". |
| Visualisations > **Crawl / Directory Tree Graph** | **Colour by** and **Size by**: crawl depth, indexability, Link Score, unique inlinks, response time, word count (folders: URLs, indexable share). The shape stays the status; the legend's counts add up to the URLs (or folders) of the tree, a missing value is "Not checked". |

### 25.2 New checks and data

- **Structured data**: every JSON-LD item is checked against the schema.org vocabulary (unknown types or properties,
  a date that is not a date, text where an object is expected, Google's numeric prices) down through nested items
  (`offers.price`). The vocabulary file `crawler/seo/data/schema_vocab.json` is rebuilt with
  `python tools/build_schema_vocab.py`.
- **Carbon**: CO2 per page view and a rating A+ to F (Sustainable Web Design model v4) in the Validation tab; "High
  Carbon Rating" flags the rating chosen under Preferences (default E) or worse.
- **PDF files**: title, author, subject, pages, dates and word count (Internal tab columns, PDF Details, the "All PDF
  Text" export). Password-protected and broken PDFs say why.
- **Reports**: Crawl Path, Small Tap Targets, PageSpeed Opportunities; bulk exports Screenshots, All Page Text, All PDF
  Text and Structured Data Properties.
- **White-label PDFs**: Settings > Branding (company name, logo, accent colour, cover line, footer) on the Website Audit
  and Business Insights PDFs.
- **Refused addresses**: crawls AI agents start, and every crawl on Render, never open cloud-metadata addresses (agents
  also no private networks unless allowed); such a URL is "No Response, blocked address" and counted like any row.
- **Memory guard on Windows**: new URLs stop being queued when the PC's commit memory (RAM + page file) falls below
  `CRAWLER_MIN_FREE_MB` (default 400 MB); the report says so.

### 25.3 API (all under the team access key; admin = section 22.2)

| Group | Endpoints |
|---|---|
| AI agents | `ANY /mcp` (bearer token), `GET/PUT /api/mcp`, `POST /api/mcp/tokens`, `DELETE /api/mcp/tokens/{id}`, `GET /api/mcp/client-config`, `GET /api/mcp/bridge`, `POST /api/mcp/test`, `POST /api/export/link`, `GET /api/export/link/{id}` |
| Connections | `GET /api/connectors[/{id}]`, `POST /api/connectors/{id}/connect\|test\|disconnect`, `POST /api/connectors/custom-mcp`, `GET/PUT /api/connectors/{id}/tools`, `GET/PUT /api/alerts`, `POST /api/tickets/push`, `GET /api/tickets/pushed`, `GET /api/indexnow/key-check`, `POST /api/indexnow/submit`, `POST /api/linkmetrics/fetch`, `GET /api/linkmetrics/status`, `POST /api/grammar/run`, `GET /api/grammar/status`, `POST /api/green-web/check`, `POST /api/sheets/export` |
| Ask ARACHNIX | `GET /api/assistant/status`, `POST /api/assistant/chat`, `GET /api/assistant/turn/{id}`, `POST /api/assistant/confirm`, `POST /api/assistant/stop`, `DELETE /api/assistant/conversations/{id}` |
| Content | `GET/PUT /api/ai/prompts`, `POST /api/ai/run`, `GET /api/ai/run/{id}`, `POST /api/ai/run/{id}/stop`, `GET/PUT /api/spelling/dictionary`, `POST /api/embeddings/run`, `GET /api/crawl/semantic-search`, `GET /api/crawl/clusters`, `GET /api/crawl/ngrams` |
| Extension | `POST /api/extension/session`, `POST /api/extension/session/read\|delete`, `GET /api/extension/download`; crawl start and `/api/auth/cookies/preview\|test` accept `handoff_id` |
| Logs | `POST /api/logs/upload` (body = the log file, at most 200 MB), `GET /api/logs/status`, `DELETE /api/logs` (Log Files tab: 2.0 switch) |
| Scheduling | `GET /api/schedules/{id}/runs/{run}/files[/{name}]`, `GET /api/schedules/{id}/command` |
| Other | `GET /api/crawls/trend`, `POST /api/crawl/segments/test`, `POST /api/profiles/compare`, `POST /api/robots/test`, `GET /api/custom-js/snippets`; `GET /api/crawl/url` adds `sections`, link `rel` / `type`, `other_inlinks`; `/api/crawl/export/detail?part=<section>` |
| Custom rules | `POST /api/crawl/extract-preview` (`{url, source: original \| rendered, extractors, searches}`: the rules' values on one crawled page, by the crawl's own code on the stored HTML, logins masked) |
| Visualisations | `GET /api/crawl/graph?type=crawl_tree&metrics=1` adds `metrics` (Link Score, unique inlinks, response time, word count; `null` = not checked) to every node; without `metrics` the answer is as in 1.0 |
| Reports | `GET /api/crawl/report/compare_overview?a=<crawl>&b=<crawl or current>&elements=<checks>` (the Comparison Overview of the two crawls of Mode > Compare) |

Crawl start (`POST /api/crawl/start`) accepts every new option (PLAN4 10.7: `preferences`, `follow`, `crawl_types`,
`limits`, `cookie_storage`, `assume_html`, `ignore_non_indexable_issues`, `carbon`, `a11y_audit`, `max_redirects`,
`sitemap_urls`, `custom_robots_txt`, `soft_404_phrases`, ...) with a plain 400 for a bad value. **Max depth now defaults
to "no limit"** (0), as in Screaming Frog (also on the command line). New command-line options: `--profile`, `--config FILE`,
`--a11y-audit`, `--max-url-length`, `--retries`, `--max-redirects`, `--search-where`; `cli.py mcp` runs the stdio
bridge for AI agents.

**Security notes.** Admin changes sent from another website are refused when the server has no access key (this closes
a cross-site gap of 1.0). AI agents are never admin, never see the team access key and get the content of a signed-in
crawl only when an admin allows it.
