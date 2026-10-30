# ARACHNIX 2.0 roadmap

## Progress (29 October 2026)

Server side, built and tested (each with its own acceptance test; the 1.0 regression suites still pass):

| Done | What it gives |
|---|---|
| P12 Structured data | Every JSON-LD item checked against the schema.org vocabulary (unknown types and properties, a date that is not a date, text where an object is expected, Google's numeric prices), nested values (offers.price), a Structured Data details section and the "Structured Data Properties" bulk export. The vocabulary file is rebuilt with `tools/build_schema_vocab.py`. |
| P14 Preferences | Thresholds per crawl (title / description / URL / H1 / H2 / alt lengths, image size, outlinks, crawl depth, file sizes, bounce rate, carbon rating); filter names show the chosen number ("Over 50 Characters"); own soft-404 phrases and generic anchors in any language. |
| P15 Custom search and extraction | Search in HTML, page text, head, body, text without links or one element (CSS / XPath); bulk add; regex case and text / HTML source; 8 ready Custom JavaScript snippets; duplicate snippet names refused. |
| P18 Carbon | CO2 per page view and a rating A+ to F (Sustainable Web Design model v4) in the Validation tab; "High Carbon Rating" is a real check now (it was "Not checked"). |
| P20 Reports | Crawl Path, Small Tap Targets, PageSpeed Opportunities; bulk exports Screenshots and All Page Text. |
| P21 PDF properties | Title, author, subject, pages, dates and word count of every PDF (Internal tab columns, details section, "All PDF Text" export); password-protected and broken PDFs say why. |
| P23 White-label PDFs | Settings > Branding (company name, logo, accent colour, cover line, footer) on the Website Audit and Business Insights PDFs; without a brand the PDFs are unchanged. |
| P24 Engine guards | Refused addresses for agent crawls and on Render (cloud metadata, private networks: "No Response, blocked address", counted like every other row); the memory guard now works on Windows too. |
| W1 AI agents (MCP) | `/mcp` for Claude, Cursor and VS Code with their own tokens (never the team key), off until an admin turns it on; client snippets, the stdio bridge download, a connection test, 15-minute download links, one crawl-log line per agent call. Agents are never admin, cannot crawl metadata or private addresses unless allowed, never replace a running crawl. Admin changes from another website are now refused (this also closes a gap that existed in 1.0). |
| W2 Connections hub | Endpoints for Slack, Teams, Discord, webhooks, Jira, GitHub Issues, IndexNow, LanguageTool, Green Web, link metrics, Google Sheets and custom MCP servers; crawl summaries and alert rules sent after every crawl. |
| W3 Ask ARACHNIX | Polled chat endpoints; changes wait for the person's confirmation; 20 questions per person per hour. |
| W4 Content | AI tab prompts and runs, the site spelling dictionary, embeddings, semantic search, topic clusters, n-grams. |
| W5 URL details | Every details section (structured data, accessibility, spelling, carbon, PDF...) and link rel / type in the URL details; section exports. |
| W6 Browser extension | Sign-in hand-off from the ARACHNIX Companion extension (10 minutes, used once, same site only); extension download. |
| W7 Crawl options | Every new crawl option accepted with plain error messages and saved in profiles; Max depth default "no limit"; the axe-core Accessibility audit without rendering every page. |
| W12 Deployment | requirements.txt (mcp, pyspellchecker, pypdf) and render.yaml. |

| P17 Crawl options | The engine honours every Spider option: follow switches, resource types, nofollow, cookie storage, assume HTML, list-mode canonicals and the limits per depth / folder depth / query strings / host; URLs left out are listed with their reason and counted. Defaults: 0 differences on the aristasystems.in mirror crawl. |
| W10 / W11 | Trend history endpoint; segment tester, profile compare, robots.txt tester, JavaScript snippet list. |
| Screens | Configuration > Connections, AI agents (MCP), Preferences & Spider options (with the robots.txt tester); Help > Ask ARACHNIX, Site trend history, Browser extension; the lower pane's Structured Data, Accessibility, Spelling & Grammar (add a word), Carbon and PDF tabs; `?url=` and the extension's `#handoff=`. Tested in headless Chrome (light / dark, desktop / phone, no console errors, untrusted text inert). |

| W8 / W9 | Scheduled runs: list and download each run's export files, and the equivalent command line of a task. Command line: `--profile`, `--config` (any new crawl option), `--a11y-audit`, `--max-url-length`, `--retries`, `--max-redirects`, `--search-where`, Max depth default no limit, and `cli.py mcp` (the stdio bridge for AI agents). |

| P22 Log files | Upload a server access log (Combined, Common, W3C / IIS, .gz; up to 200 MB, streamed, deleted after reading): Googlebot, Bingbot and AI bot hits per URL, fake bots told apart by reverse DNS, bot 4xx / 5xx, pages Googlebot never requested, URLs bots request that the crawl never found (Log Orphans), hits per bot. Counts only: no line or IP address is kept. Hidden in 1.0 with the other 2.0 tabs (`ARACHNIX_V2_FEATURES`) until its upload screen is in the dashboard. |

Done since (2026-10-29 / 30): the scheduler's API data step and sitemap / Google Sheet start lists (W8), the Log Files
upload screen, U4 (AI prompts), U9 (point-and-click extractor, Bulk add, Search in head / body / one element, regex
options, snippet library), U10 (comparison report, IndexNow, five more change checks), U12 (Colour by / Size by on the
tree graphs) and U13 (SERP preview for tablet and mobile with your own text, View Source line diff; n-grams are in
Configuration > SEO tools). The 2.0 features are on by default (`ARACHNIX_V2_FEATURES=0` hides them).
Still open: the 2.0 chapter of the User Guide PDF. Documentation: DOCUMENTATION.md section 25.

---

Everything below was planned in Build 4 but is **not in ARACHNIX 1.0**. Some of it is already built and only waits to
be connected and tested; the rest is still to build. Version 1.0 contains Build 4 rounds 1-3 only where they were
finished and tested.

## 1. Built, hidden in 1.0, ready to switch on

These are in the code today and hidden by one switch (`ARACHNIX_V2_FEATURES=1`, or `V2_FEATURES` / `V2_ONLY` in
`crawler/seo/registry.py`). Each needs its screens finished and a test pass before it ships.

| Feature | What it is | What is missing |
|---|---|---|
| AI tab | Run your own AI prompts on every page (summaries, intent, missing facts) | The prompt manager screen and its endpoints |
| Link Metrics tab | Backlink numbers per URL from Moz, Majestic or Ahrefs | The connection screens, keys and endpoints |
| Spelling and grammar | Spelling and grammar errors in the Content tab, with their columns and bulk exports | A dictionary screen ("add a word") and a grammar engine (LanguageTool) |
| Moz, Majestic, Ahrefs | Link-metric sources under Configuration > API Access | Same as Link Metrics |

## 2. Built as modules, not yet wired into the app

| Module | What it will do |
|---|---|
| MCP server | Lets an AI agent (Claude, Cursor and others) read crawls and, if allowed, start them, safely and with its own token |
| Connections hub | One place for alerts (Slack, Teams, email), developer tickets (Jira, GitHub, Linear), IndexNow, Google Sheets and link metrics |
| Ask ARACHNIX | A chat assistant in a side drawer that answers questions about your crawls using the same data as the tabs |
| Chrome extension | Hands your signed-in Chrome session to a crawl in one click, instead of pasting cookies |
| Accessibility audit | A full axe-core accessibility audit of rendered pages, with its own pane and export |
| Carbon rating | A High Carbon Rating and the CO2 per page view (Sustainable Web Design model) |

## 3. Screaming Frog parity still to build (Build 4 rounds 4-5)

| Item | Plain words |
|---|---|
| P12 Structured data | Check schema.org types and properties against the official vocabulary, show nested values |
| P14 Preferences | Let the team change thresholds (title length, thin content words and so on) per site |
| P15 Custom search and extraction | More places to search (head, body, text without links), more extraction options, a library of ready JavaScript snippets |
| P18 Carbon | The carbon rating above, wired into a tab and the reports |
| P24 Engine guards | Watch memory on Windows during big crawls, and refuse private or internal addresses for crawls started from outside |
| P17 Crawl options | Per-type crawl and follow switches, per-host and per-folder limits, retries, redirects, URL length limits |
| P20 Reports and bulk exports | Crawl path report (how the crawler reached a page) and the remaining Screaming Frog reports |
| P21 PDF properties | Read title, author, page count and the text of PDF files the crawl finds |
| P23 White-label PDFs | Company name, logo and colour of the client on the PDF reports |
| P22 Log file analysis | Upload server access logs and see which pages Googlebot, Bingbot and AI bots really visit |

## 4. Server work still to build (Build 4 api W1-W12)

| Item | Plain words |
|---|---|
| W1 | Mount the MCP server, its tokens and its safety rules |
| W2 | Endpoints for the Connections hub (alerts, tickets, IndexNow, link metrics) and their crawl hooks |
| W3 | Endpoints for Ask ARACHNIX (chat, per-person rate limit) |
| W4 | Endpoints for the AI tab, the spelling dictionary, embeddings and n-grams |
| W5 | More in the URL details (link rel and type, other inlinks) and in exports |
| W6 | Endpoints for the Chrome extension session hand-over |
| W7 | Accept every new crawl option and profile field; switch on the accessibility audit |
| W8 | Scheduler: API data, sitemap or Google Sheet as the start list, download exports |
| W9 | Command-line options for profiles, config files, max depth, accessibility audit, MCP |
| W10 | Trend data across crawls of one site |
| W11 | Small endpoints: segment tester, profile compare, robots.txt tester, JavaScript snippets, log uploads |
| W12 | Dependencies and deployment files for all of the above |

## 5. Screens still to build (Build 4 ui U1-U14)

| Item | Plain words |
|---|---|
| U1 | Connections hub screen (Configuration > Connections) |
| U2 | "Connect an AI agent" screen for the MCP server |
| U3 | Ask ARACHNIX chat panel |
| U4 | AI tab with its prompt manager |
| U5 | Spelling & Grammar details pane and dictionary |
| U6 | Accessibility and Lighthouse panes |
| U7 | Structured Data details pane |
| U8 | More configuration: preferences, robots.txt editor and tester, limits, per-type options, carbon options |
| U9 | Point-and-click extractor and the snippet library (done 2026-10-30) |
| U10 | More change-detection checks in Compare (done 2026-10-30) |
| U11 | Trend history charts per site |
| U12 | Metric picker for the tree graphs (done 2026-10-30) |
| U13 | SERP preview for mobile, content map and n-gram views in the lower and right panes (done 2026-10-30: SERP devices, View Source diff; n-grams in SEO tools) |
| U14 | Smaller flows: segment tester, profile compare, scheduler steps, ticket buttons, Sheets export, log upload, browser extension help |

## 6. Polish found while testing 1.0

- A clearer 16 px browser-tab icon.
- Colour-blind safe pairs for every status and chart colour.
- The finished-crawl badge on the browser-tab icon starting on time after very large crawls.
- More room in the compact PDF word budget for large sites.
