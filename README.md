# Universal Website Link Crawler & Excel Exporter

> 📖 **Looking for the complete technical guide?** Read the full **[Comprehensive Technical Documentation (DOCUMENTATION.md)](./DOCUMENTATION.md)** covering BFS traversal mechanics, SPA route recovery, authentication bypass, full REST API schema, and Excel export specifications.

- 🌐 **Live Frontend (Vercel CDN Edge):** [https://website-crawler-virid.vercel.app/](https://website-crawler-virid.vercel.app/)
- ⚙️ **Live Backend API (Render Container):** [https://website-crawler-z8qf.onrender.com](https://website-crawler-z8qf.onrender.com)

A high-performance, recursive website crawler designed to map and extract **every internal URL** belonging to any given website—including navigation menus, footer links, blog posts, case studies, services, nested sections, and sitemaps—while also capturing **external outbound links** (without crawling or exploring external websites).

Export all discovered links into a professionally styled, multi-tab Excel (`.xlsx`) report with page titles, parent referrers, HTTP status codes, latency, external domains, and redirect destinations.

---

## Key Features

1. **Multi-Source Recursive Internal Crawling**:
   - **Page Navigation & Content**: Parses `<a>` tags, `<link>` tags, `data-href`/`data-url` attributes, `<form>` actions, image maps, and inline JavaScript path declarations.
   - **XML Sitemaps Auto-Detection**: Auto-detects `robots.txt` and probes `/sitemap.xml`, `/sitemap_index.xml`, `/wp-sitemap.xml`, following nested sitemap indexes recursively.
   - **Dynamic JS Link Extraction**: Integrated headless Selenium Chrome mode to render dynamic single-page applications (React, Angular, Vue, Next.js) and lazy-loaded links.

2. **External Outbound Links Collection (No External Crawling)**:
   - When any external link is discovered on the target website (e.g. Instagram, LinkedIn, Facebook, Twitter, partner sites, external resources, maps), the crawler captures:
     - Full destination URL
     - Target domain (e.g. `instagram.com`, `linkedin.com`)
     - Parent page / referrer where it was found
     - Anchor text / accessibility label
     - Discovery source
   - **Strict Non-Exploration Guarantee**: The crawler **never** visits, requests, or follows external links. It collects the link and immediately stops there, preserving crawling speed and domain isolation.

3. **Strict Internal Domain Filtering & Deduplication**:
   - Canonicalizes relative and absolute URLs.
   - Strips hash anchors (`#top`, `#section`) to avoid duplicate hits on the same page.
   - Cleans marketing tracking parameters (`utm_*`, `fbclid`, `gclid`, etc.).
   - Discards non-HTTP protocols (`mailto:`, `tel:`, `javascript:`, etc.).
   - Detects non-HTML static assets (`.pdf`, `.zip`, `.png`) to log their status without attempting HTML link extraction.

4. **Status & Redirect Intelligence**:
   - Follows redirect chains and records initial HTTP code, final target URL, and redirect path (e.g. `301 -> 200`).
   - Categorizes broken links (4xx, 5xx) and connection timeouts with their parent referrers.

5. **Multi-Tab Styled Excel (.xlsx) Report**:
   - **Tab 1: Crawl Summary**: Executive audit dashboard with status breakdowns, total internal pages discovered vs crawled, total external links collected, unique external domains, and duration.
   - **Tab 2: All Internal URLs**: Complete catalog sorted and color-coded (Green = 2xx, Yellow = 3xx, Red = 4xx/5xx).
   - **Tab 3: External Links**: Dedicated sheet listing all discovered outbound external links, their destination domain, anchor text, and referrer page on the target website.
   - **Tab 4: Broken Links & Errors**: Instant actionable sheet for developers and SEO teams to fix broken links.
   - **Tab 5: Redirects (3xx)**: Maps original URLs to final destination URLs.
   - Pre-formatted with auto-filters, frozen top rows, auto-fitted column widths, and clean typography.

6. **Two Convenient Interfaces**:
   - **Interactive Web Dashboard**: Modern responsive UI with live streaming progress, status cards, dual internal/external tabs, searchable table, log terminal, and 1-click Excel download.
   - **Rich Command-Line Interface (CLI)**: For terminal automation and headless scripts.

---

## Quick Start

### 1. Launch the Interactive Web Dashboard
Run the launcher:
```bash
python run.py
```
Open your browser at:
```
http://127.0.0.1:8000
```
Enter any website URL (e.g. `https://aristasystems.in/`), configure options if desired, and click **Start Crawl**. Switch between the **Internal URLs** and **External Outbound Links** tabs in the live table, and click **Download Excel (.xlsx)** when done.

---

### 2. Run via Command Line (CLI)

#### Standard Crawl:
```bash
python run.py --cli --url https://aristasystems.in/
```

#### With Custom Options:
```bash
python run.py --cli --url https://aristasystems.in/ --threads 10 --max-urls 500 --output arista_full.xlsx
```

#### Disable External Links Collection (Optional):
```bash
python run.py --cli --url https://aristasystems.in/ --no-external
```

#### CLI Flags:
- `url`: Target URL to crawl (e.g. `https://aristasystems.in/`)
- `-o`, `--output`: Custom output path for Excel file
- `-m`, `--max-urls`: Maximum internal URLs to discover/crawl (`0` for unlimited, default `1000`)
- `-d`, `--max-depth`: Max recursion depth (default `10`)
- `-t`, `--threads`: Concurrent worker threads (default `8`)
- `--no-external`: Disable collecting outbound external links
- `--dynamic`: Enable Selenium Chrome to render JavaScript dynamic links
- `--no-sitemap`: Disable XML sitemap discovery
- `--delay`: Delay between requests per thread in seconds (e.g. `0.2`)
- `--subdomains`: Treat subdomains (e.g. `blog.domain.com`) as internal
- `--category`: Excel scope: `all` (default), `2xx`, `3xx`, `4xx`, `5xx`, `error`, `issues`, `external`
- `--pdf`: Also write `<output>_issues.pdf` with screenshots (`--no-screenshots` to skip them)

---

## Reports

| Report | What you get |
|---|---|
| **Full Excel** | Summary, Internal URLs, Broken Links & Errors, Redirects, External Links. URL and parent-page cells are clickable, and each row shows the link text and where the link sits (e.g. `Footer › body > footer > a:nth-of-type(2)`) |
| **Excel by category** | One workbook per scope: `2xx`, `3xx`, `4xx`, `5xx`, network errors, all issues, or external links. Issue sheets add *Why it happens*, *Likely causes* and *How to fix* |
| **Issue report (PDF)** | Summary page (health score, category bar, top issues, pages with most broken links), then one card per issue with cause, fix, link position, a screenshot of the parent page with the broken link highlighted, and a screenshot of the error page |

Screenshots need `pip install selenium` and Chrome on the machine running the backend. Without them the PDF shows a page-position sketch instead.

In the dashboard, click any category card (Healthy, Redirects, Broken, Server errors, Network) to show only that category. Click an issue row to see the screenshots, cause and fix.

---

## Hosting & Deployment (Recommended Architecture)

To bypass Vercel's 60-second serverless execution ceiling and enable deep, unlimited-time crawling with real-time progress updates, we use a **Split-Stack Architecture**:
- **Frontend on Vercel**: Hosted as a fast, edge-cached static web dashboard on Vercel CDN.
- **Backend on Render**: Hosted as a persistent Python Web Service on Render with multi-threaded background workers, live status streaming, and Excel generation.

---

### Step 1: Deploy the Backend on Render

1. Sign up or log in at [render.com](https://render.com).
2. Click **New +** -> **Web Service**.
3. Connect your GitHub repository (`website_crawler`).
4. Configure the Web Service settings:
   - **Name**: `website-crawler-backend`
   - **Runtime**: `Python 3`
   - **Branch**: `main`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn web.app:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: `Free`
5. Click **Deploy Web Service**.
6. Once deployed, copy your Render service URL (e.g., `https://website-crawler-backend.onrender.com`).

---

### Step 2: Deploy the Frontend on Vercel

1. Sign up or log in at [vercel.com](https://vercel.com).
2. Click **Add New...** -> **Project**.
3. Select your GitHub repository (`website_crawler`).
4. In Project Settings:
   - **Framework Preset**: `Other`
   - **Root Directory**: `./` (leave default, or set to `frontend`)
5. Click **Deploy**. Vercel will deploy your static dashboard in seconds.
6. Open your new Vercel URL (e.g. `https://website-crawler-frontend.vercel.app`).

---

### Step 3: Connect Frontend to Backend

You have two easy ways to link your Vercel frontend to your Render backend:

- **Method 1 (Instant in UI)**: Open your Vercel dashboard, click the **Backend Status** button in the top navigation bar, paste your Render URL (e.g., `https://website-crawler-backend.onrender.com`), click **Test Connection** to verify, then click **Save & Connect**. This is stored in your browser's `localStorage` and takes effect immediately.
- **Method 2 (In Code)**: Open `frontend/config.js` and set:
  ```javascript
  window.CRAWLER_API_BASE_URL = "https://your-backend.onrender.com";
  ```
  Commit and push to GitHub; Vercel will auto-deploy the change.

---

### Testing Locally

Run the complete app locally on your machine with one command:
```bash
python run.py
```
Open [http://localhost:8000](http://localhost:8000). The frontend automatically connects to the local backend.
