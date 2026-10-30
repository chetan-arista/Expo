# ARACHNIX 1.0 release notes

**Released 28 October 2026.** ARACHNIX (formerly *SlickUP*, before that *Site Auditor*) is Arista Systems' internal
SEO crawler. Version 1.0 freezes everything that was finished and tested in Builds 1-3, plus the first three rounds of
Build 4. Unfinished Build 4 work moves to version 2.0 (see [ROADMAP_2.0.md](ROADMAP_2.0.md)).

## What ARACHNIX 1.0 does

**Crawling**
- Spider, List and SERP modes; JavaScript rendering in Chrome; crawl scopes (subdomain, folder, all subdomains, one URL).
- robots.txt with Google's matching rules, XML sitemaps, URL rewriting, include / exclude rules, user-agent presets,
  custom headers and proxy.
- Signed-in crawls: email and password, pasted cookies (guided, any format, tested before the crawl) or a header
  token; login guard and session safety (never clicks sign-out or data-changing links).
- Bot-check guidance: ARACHNIX never tries to pass a CAPTCHA; it explains the ways that work.
- Find subdomains: finds the domain's other hosts and says why each was or was not crawled.

**Audit**
- The Screaming Frog tabs and filters (Internal, External, Response Codes, Titles, Meta Description, H1, H2, Content,
  Images, Canonicals, Directives, Hreflang, Pagination, Structured Data, Security, URL, Links, JavaScript, Mobile,
  Accessibility, Sitemaps, Custom Search / Extraction / JavaScript, Validation) plus ARACHNIX's own tabs (AI Crawlers,
  Robots.txt audit, Hygiene, Parameters, Performance, Social, Third Parties).
- Numbers always reconcile: the category of a URL is its HTTP status, so a 404 is always counted as Broken 4xx.
- Passed / Failed / Not checked on every check, never a misleading zero.

**Analysis and views**
- Issues overview with priorities, plain explanations and fixes; URL details pane; Crawl Analysis (Link Score,
  near-duplicates); segments; tree graphs and the drill-down site graph; compare two crawls.

**Proof and fixes**
- Screenshots with the problem boxed on the page; live re-checks of every reported error (confirmed, intermittent,
  already fixed).
- Fix plan grouped by root cause with platform steps for Shopify, WordPress / WooCommerce and Magento; import-ready fix
  files (Yoast, Rank Math, Shopify, .htaccess, Next.js); developer tickets (Jira, CSV, Markdown).

**Output**
- Saved crawls (open, rename, lock, export, import); scheduled crawls with email; profiles.
- Excel and CSV for any tab or filter; bulk exports and ready-made reports; sitemap generator.
- Website Audit Report PDF (compact, client-ready) and Business Insights PDF.
- Optional Google connections (Search Console, GA4, PageSpeed Insights) under Configuration > API Access, and an
  optional free AI layer (Gemini / Groq); everything works without them.

## The new look

- **Teal palette:** deep teal for buttons and the active tab, light teal for selected rows, blue for links, tangerine
  for warnings, green for OK statuses, on an ivory background.
- **Dark mode** that is easy on the eyes; it follows the computer's setting or can be chosen by hand.
- **Clear screen areas:** visible separators and distinct panels instead of one long page.
- **Readable everywhere:** every measured text element passes the WCAG AA contrast standard in light and dark mode.
- **Glass effects** On / Reduced / Off under Settings > User Interface.
- **Animated logo:** the ARACHNIX spider-and-magnifier mark, animated in the browser-tab icon, in the name logo and
  as the loading animation while a crawl starts. It stays still when the computer asks for reduced motion.

## The rename

SlickUP is now **ARACHNIX** everywhere a user sees the name: the dashboard, page title, browser-tab icon, Help > About
("ARACHNIX 1.0"), PDF and Excel reports, emails (sender `arachnix@`), the debug download
(`arachnix_debug_<time>.json`), the User Guide and the Feature Guide (`ARACHNIX-Feature-Guide.pdf`).

## Hidden in 1.0 (kept for 2.0)

Build 4 pieces that exist but are not finished are **hidden, not deleted**: the AI tab, the Link Metrics tab, the
Content tab's Spelling and Grammar filters and columns, their bulk exports, and Moz / Majestic / Ahrefs under API
Access. One switch turns them back on: start the server with `ARACHNIX_V2_FEATURES=1` (or `V2_FEATURES` in
`crawler/seo/registry.py`). AMP stays hidden for good (AMP is deprecated).

## Tested for this release

- Python compile check of every module; syntax check of every dashboard script; the three dashboard copies identical.
- Build 3 regression suite: 290 passed, 0 failed. Priority suite (cookies, bot checks, subdomains, site graph, new
  dashboard states): 225 of 227 on the full run; the 2 misses were one on-screen timing check in the Subdomains
  section, which passed on the re-run of that part (70 passed, 0 failed).
- Contrast measurement in light and dark mode: 71 text elements each, 0 below AA.

## Known limits in 1.0

- The 16 px browser-tab icon is small; the mark reads best at 32 px and up.
- Some status and chart colour pairs are close for colour-blind users; codes and legends carry the meaning.
- The finished-crawl badge on the browser-tab icon can start a moment late after a very large crawl.
- The compact PDF for a large site sits close to its 3,500-word target.
