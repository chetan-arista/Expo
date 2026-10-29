# Deploying ARACHNIX

ARACHNIX has two parts:

| Part | What it is | Where it runs |
| --- | --- | --- |
| **Backend** | The Python server (`web/app.py`): crawls, password logins in headless Chrome, screenshots, reports, saved crawls, the scheduler, email, Google connections, AI | An **office PC** (`run.py`), or **Render** as a Docker service (`Dockerfile`, `render.yaml`) |
| **Dashboard** | One HTML page (`index.html`) plus `config.js`, which holds the backend's address | Served by the backend itself, or **Vercel** as plain static files |

```
 your browser ──> Vercel (index.html + config.js) ──API calls──> Render backend (crawler + Chromium)
 your browser ──────────────────────────────────────────────────> office PC (run.py serves the dashboard too)
```

The backend serves the same dashboard too (open its address directly), so Vercel is optional.

**Why the backend is not on Vercel:** a crawl runs for minutes in background threads and needs Chrome, and the
scheduler needs a server that stays up. Vercel functions stop after a time limit and have no Chrome. Vercel only
serves the dashboard files; `.vercelignore` makes sure the old `api/index.py` entry point is never deployed there.

Sections: 1. Run on a Windows PC (and share it on the office network) · 2. Backend on Render (2.1 instance size,
2.2 create the service, 2.3 environment variables, 2.4 keep saved crawls, schedules and settings, 2.5 checks,
2.6 Google Search Console, GA4 and PageSpeed, 2.7 email notifications, 2.8 scheduled crawls and sleeping hosts) ·
3. Dashboard on Vercel · 4. Pre-flight checklist · 5. Troubleshooting

---

## 1. Run on a Windows PC

You need **Python 3.11 or newer** (python.org) and **Google Chrome**. Commands are for PowerShell.

1. Open PowerShell in the project folder:
   ```powershell
   cd $HOME\Downloads\website-crawler
   ```
2. Create the virtual environment and install the packages (first time only):
   ```powershell
   python -m venv venv
   .\venv\Scripts\python.exe -m pip install --upgrade pip
   .\venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
3. Optional: a **free** AI key. Without one, everything still works and the AI parts say
   "AI off - site facts only".
   - Google Gemini: sign in at https://aistudio.google.com, choose **Get API key** and create a key
     (free tier, no card needed).
   - Groq (a second free provider, used when Gemini's quota runs out): https://console.groq.com, **API Keys**.
   - Save each key as a Windows *user* environment variable: Start menu, type **environment**, open
     **Edit environment variables for your account**, **New...**, name `GEMINI_API_KEY` (or `GROQ_API_KEY`),
     paste the key as the value, **OK**.
   - Close PowerShell and open a new window. Windows that were already open do not see new variables.
   - Never paste a key into a file in this folder, a chat or a ticket.
4. Start the server:
   ```powershell
   .\venv\Scripts\python.exe run.py --port 8000
   ```
5. Open http://127.0.0.1:8000 in the browser.
6. Check the AI: open http://127.0.0.1:8000/api/ai/status. `"enabled": true` means a key was found;
   `reason` says which provider is used, or why AI is off.
7. Stop the server with **Ctrl+C** in the PowerShell window.

On a PC, Selenium finds the installed Chrome by itself; nothing else to set up. Free-tier quotas are
small: the tool batches its AI requests and stops at 40 calls per crawl (`CRAWLER_AI_MAX_CALLS`).
No access key is needed on a PC: without `CRAWLER_ACCESS_KEY` the dashboard never asks for one (2.3).

### 1.1 Share it on the office network

1. Start the server for the network instead of this PC only:
   ```powershell
   .\venv\Scripts\python.exe run.py --host 0.0.0.0 --port 8000
   ```
   It prints the address to share, for example `From the network: http://10.10.6.56:8000`.
2. The first time, Windows asks whether Python may accept connections: allow it on **Private networks** only.
3. Colleagues open that address in their browser. Everyone shares the crawl on screen (one crawl per server).
4. Server-wide settings (Proxy, Crawl Retention, Notifications, Embedded Browser) and the Google connections change for
   everyone, so without an access key they can only be changed from the server PC itself (http://localhost:8000);
   other PCs see them read-only. To manage them from any PC, set a team access key (`CRAWLER_ACCESS_KEY`, 2.3), which also
   keeps people outside the team out; or set `CRAWLER_SETTINGS_EDITABLE=1` on a network you trust.

#### HTTPS on the office network (downloads without warnings)

Chrome and Edge treat a plain-HTTP address of another computer (`http://10.10.6.56:8000`) as insecure: every file
downloaded from it (PDF report, business PDF, Excel, CSV) is held with "download blocked / insecure" until someone
clicks **Keep**, and a download that does not follow a click closely is dropped without a word. The server's answers
are correct (`Content-Type`, `Content-Disposition`, `X-Content-Type-Options: nosniff`); only the address scheme
triggers it. `http://localhost:8000` on the server PC itself is not affected. Two ways to end it:

**Quick, per PC (no certificate):** in Chrome open `chrome://settings/content/insecureContent` (Edge:
`edge://settings/content/insecureContent`), **Allowed to show insecure content > Add**, enter
`http://10.10.6.56:8000`. Downloads from that address then complete without the warning. Each person does this once
in their browser.

**Proper: HTTPS with a certificate the team's PCs trust (mkcert).** On the server PC:

1. Install mkcert: `winget install FiloSottile.mkcert` (or download `mkcert-*-windows-amd64.exe` from
   github.com/FiloSottile/mkcert/releases and rename it to `mkcert.exe`).
2. `mkcert -install` once: makes a private certificate authority (CA) and trusts it on this PC.
3. Make the server certificate for every address people use (the IP, the PC name, localhost):
   ```powershell
   mkdir certs
   mkcert -cert-file certs\arachnix.pem -key-file certs\arachnix-key.pem 10.10.6.56 localhost 127.0.0.1 $env:COMPUTERNAME
   ```
4. Start the server with it (the default start without these flags stays plain HTTP):
   ```powershell
   .\venv\Scripts\python.exe run.py --host 0.0.0.0 --port 8000 --ssl-certfile certs\arachnix.pem --ssl-keyfile certs\arachnix-key.pem
   ```
   It prints `From the network: https://10.10.6.56:8000`; share that address. Instead of the flags you can set the
   user environment variables `CRAWLER_SSL_CERTFILE` and `CRAWLER_SSL_KEYFILE`. In the Task Scheduler task (1.3) add the
   two flags to the arguments.
5. Every team PC must trust the CA once: `mkcert -CAROOT` shows its folder on the server PC. Copy **only
   `rootCA.pem`** to each PC (never `rootCA-key.pem`: with it anyone can make certificates your PCs trust), rename it
   `rootCA.crt` and run `certutil -user -addstore Root rootCA.crt` (no admin rights; or double-click it > Install
   Certificate > Current User > Trusted Root Certification Authorities). Restart Chrome / Edge. Chrome and Edge on
   Windows use this store; Firefox has its own (Settings > Certificates > Import).
6. Open `https://10.10.6.56:8000`. A PC that skipped step 5 sees "Your connection is not private"; do step 5 there
   rather than clicking through (downloads may still be blocked then).

Google connections (2.6) use the server's own address: after switching, register
`https://localhost:8000/api/integrations/google/callback` as well and reconnect from the server PC.

### 1.2 Keep saved crawls, schedules and settings

Saved crawls (File > Crawls), crawl history, schedules, settings, profiles, segments, dismissed issues and the Google
connections are kept in the **reports folder**, `CRAWLER_REPORTS_DIR`. Without it ARACHNIX uses the Windows temp folder,
which clean-up tools and Storage Sense may empty. Set a lasting folder once:

```powershell
New-Item -ItemType Directory -Force C:\ARACHNIX-data
[Environment]::SetEnvironmentVariable("CRAWLER_REPORTS_DIR", "C:\ARACHNIX-data", "User")
```

Open a new PowerShell window and start `run.py` again. File > Settings > Storage then shows that folder (and no longer
warns about the temp folder). Back the folder up like other team data; `secrets.json` and `integrations\` in it hold
the SMTP / proxy passwords and the Google sign-ins.

### 1.3 Keep the server running (scheduled crawls)

Scheduled crawls (File > Scheduling) start only while `run.py` is running and the PC is awake. A task that was due
while the PC was off or asleep runs once when the server is back, and its history says how many runs were missed.

- **Stop the PC from sleeping:** Settings > System > Power (& battery) > Screen and sleep: set "put my device to sleep"
  to **Never** when plugged in. The screen may still turn off.
- **Start ARACHNIX with Windows:** Task Scheduler > **Create Task...** > Triggers: **At log on** (your account) > Actions:
  **Start a program**, program `C:\Users\<you>\Downloads\website-crawler\venv\Scripts\python.exe`, arguments
  `run.py --host 0.0.0.0 --port 8000`, **Start in** `C:\Users\<you>\Downloads\website-crawler`. It runs with your user
  environment variables (AI keys, `CRAWLER_REPORTS_DIR`).
- Only one ARACHNIX process per reports folder runs the scheduled tasks (a lock file); a second server on the same
  folder only edits them. `CRAWLER_SCHEDULER=0` switches the scheduler off on a server.

### 1.4 Optional: run the server image locally

Only if Docker Desktop is installed. This is exactly what Render runs.
```powershell
docker build -t website-audit .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY -e GROQ_API_KEY website-audit
```
`-e NAME` without a value passes the key from your environment, so it is never typed on the command line.
Then open http://127.0.0.1:8000.

Google Search Console / GA4 / PageSpeed and email work on a PC too: sections 2.6 and 2.7 (the environment variables are
the same; on a PC set them as Windows user variables).

---

## 2. Backend on Render

### 2.1 Choose the instance size

Chrome needs about 1 GB of memory next to the app. **Use the "Standard" instance (1 CPU, 2 GB RAM, plan id
`1c-2g`).** It is a paid instance; check the price on render.com/pricing.

| Instance | Memory | Result |
| --- | --- | --- |
| Free | 512 MB | Chrome runs out of memory. Sleeps after 15 minutes without visits (scheduled crawls do not run while it sleeps); the next visit waits about a minute. No disks. |
| Starter (`0.5c-512mb`) | 512 MB | Same memory problem as Free. |
| **Standard (`1c-2g`)** | **2 GB** | **Recommended. Everything works, including password logins and scheduling.** |
| Pro (`2c-4g`) | 4 GB | Very large crawls; `MAX_CHROME_INSTANCES` can be raised to 3. |

A 512 MB instance can only run the tool without Chrome: set `MAX_CHROME_INSTANCES=1` and
`CRAWLER_NO_CHROME=1`. Plain HTML login forms, pasted cookies and header logins still work there; JavaScript
login pages, the live login window, Render every page in Chrome and real screenshots do not.

**How many URLs fit in one crawl.** The server keeps every crawled URL in memory (its result, SEO data and
links) until the next crawl replaces it. Two settings keep a big crawl from filling the memory (if it does,
Render restarts the server and that crawl is lost):

- `CRAWLER_MAX_URLS` caps every crawl on this server (`render.yaml` sets `2500` for Standard). A dashboard
  Max URLs of `0` ("no limit") or above it is lowered to it, and Crawl Config shows the cap.
- The memory guard `CRAWLER_MIN_FREE_MB` (default `400`): when the container's free memory falls below it,
  no more URLs are queued (like the Max URLs limit) so the Excel and PDF can still be built. The report then
  says "Crawl stopped early because the server's memory was nearly full" with the number of URLs left out.
  On a PC (no container limit) it never triggers.

Set `CRAWLER_MAX_URLS` to the size of your instance:

| Instance | Max URLs per crawl (safe) |
| --- | --- |
| 512 MB, Chrome off | about 800 |
| **Standard (`1c-2g`)** | **about 2,500** |
| Pro (`2c-4g`) | about 6,000 |

These numbers leave room for heavier pages than the test site they come from. Measured with that site on a
PC (light pages: 18 KB of HTML and about 145 links each): about 140 KB per URL while crawling, about 230 KB per URL
at the peak while the Excel file is written after the crawl, and about 450 MB for each running Chrome. A
3,346-URL crawl peaked at about 830 MB without Chrome. For a bigger site, crawl one section at a time with
an Include pattern (**Crawl Config**), or use a bigger instance. **Render every page in Chrome** uses several Chromes at
once; on Standard keep `MAX_CHROME_INSTANCES=2` (one renders at a time next to the crawl's own browser).

### 2.2 Create or update the service

Pick **one** of the three ways.

**Way A - keep the current service and its address** (`https://website-crawler-z8qf.onrender.com`, which
`config.js` already points to). Recommended if that service exists.

Check first which repository and branch the service builds from (**Settings**, Git repository). This
repository is `github.com/chetan-arista/Expo`, branch `main`. On 2026-10-23 the running services
`website-crawler-z8qf` and `website-crawler-chrome` (the one the live Vercel dashboard calls) both answered
`/api/environment` with fields this code does not have, so they were built from other code. Pointing a
service at this repository replaces that code.

1. Push this repository (with `Dockerfile` and `render.yaml`) to GitHub.
2. Render dashboard: open the service, then **Settings**. If needed, connect it to this repository and
   branch.
3. Change the runtime / language from **Python 3** to **Docker**.
4. Set **Dockerfile Path** to `./Dockerfile` and **Docker Build Context Directory** to `.`.
5. Leave **Docker Command** empty (the image's own start command is used). Remove the old Build Command and
   Start Command if they are still shown.
6. Set **Instance Type** to **Standard**.
7. Set **Health Check Path** to `/healthz`.
8. Add the environment variables from 2.3 (**Environment** tab), then **Save**.
9. Add the disk from 2.4 (strongly recommended: saved crawls, schedules and settings live there).
10. **Manual Deploy** > **Clear build cache & deploy**. The first build takes several minutes (it installs
    Chromium).

**Way B - Blueprint** (creates the service from `render.yaml`).

1. Push this repository to GitHub.
2. Render dashboard: **New** > **Blueprint**, choose the repository, then the branch `main`.
3. Render reads `render.yaml` and asks for the values marked `sync: false`: the AI keys (`GEMINI_API_KEY`,
   `GROQ_API_KEY`, `OPENROUTER_API_KEY`), `CRAWLER_ACCESS_KEY`, `CRAWLER_CORS_ORIGINS`, the Google settings
   (`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `PSI_API_KEY`) and the email settings
   (`CRAWLER_SMTP_HOST`, `CRAWLER_SMTP_USER`, `CRAWLER_SMTP_PASSWORD`, `CRAWLER_SMTP_FROM`, `CRAWLER_DASHBOARD_URL`).
   Paste what you have and leave the others empty; everything except the access key is optional. Set the access key and
   the allowed websites as in 2.3 ("Protect the backend"). (Render asks only this first time; later, change values in
   the service's **Environment** tab.)
4. **Apply**. Wait for the first deploy to finish.
5. The service gets a **new address**. If a service with the same name already exists, Render adds a suffix
   to the name. Put the new address into `config.js` (section 3) and into the Google redirect URI (2.6).
6. Add the disk (2.4): uncomment the `disk:` block in `render.yaml` and sync the Blueprint, or add it by hand.

**Way C - new Docker web service by hand.** **New** > **Web Service**, choose the repository, language
**Docker**, instance type **Standard**, then do steps 4-10 of Way A.

Never run more than **one instance**: crawls and their results live in the server's memory, so a second
instance would not see the first one's crawl (and the scheduler runs in one process only). The image also forces one
server process for the same reason.

### 2.3 Environment variables

`render.yaml` sets these for you (Way B). For Way A or C, add them by hand. Mark every secret as secret in Render
and never put one into a file in this repository.

| Name | Value | Needed? | What it does |
| --- | --- | --- | --- |
| `GEMINI_API_KEY` | your key | optional (secret) | Google Gemini free tier |
| `GROQ_API_KEY` | your key | optional (secret) | Groq free tier, used when Gemini's quota runs out |
| `OPENROUTER_API_KEY` | your key | optional (secret) | OpenRouter free models; very low daily limits, used last |
| `CRAWLER_AI_PROVIDER` | `auto` | recommended | `auto` = first provider with a key; `off` turns AI off |
| `CRAWLER_AI_ALLOW_PRIVATE` | `0` | recommended | `0` = pages from signed-in crawls are never sent to a free cloud AI |
| `CRAWLER_REPORT_RETENTION_HOURS` | `72` | recommended | Excel/PDF files and screenshots of the current crawl are deleted after this many hours |
| `MAX_CHROME_INSTANCES` | `2` | **yes** | Chromes at once. `2` on Standard (a crawl and a live login window together), `1` on 512 MB |
| `CRAWLER_MAX_URLS` | `2500` | **yes** | URLs one crawl may queue on this server (2.1): `800` on 512 MB, `6000` on Pro; `0` = no cap |
| `CRAWLER_REPORTS_DIR` | `/var/data` | recommended | Where saved crawls, history, schedules, settings, profiles, segments, dismissed issues and Google connections are kept (2.4) |
| `CRAWLER_ACCESS_KEY` | a long random key | **strongly recommended** (secret) | Team access key: every API request needs it (see below) |
| `CRAWLER_CORS_ORIGINS` | `https://website-crawler-virid.vercel.app` | recommended | Websites whose pages may call the backend; empty = every website |
| `GOOGLE_OAUTH_CLIENT_ID` | from Google Cloud | optional | Google sign-in for Search Console and GA4 (2.6) |
| `GOOGLE_OAUTH_CLIENT_SECRET` | from Google Cloud | optional (secret) | Its secret (2.6) |
| `PSI_API_KEY` | a Google API key | optional (secret) | PageSpeed Insights (2.6) |
| `CRAWLER_SMTP_HOST`, `CRAWLER_SMTP_PORT`, `CRAWLER_SMTP_ENCRYPTION` | e.g. `smtp.office365.com`, `587`, `tls` | optional | Email server for notifications (2.7) |
| `CRAWLER_SMTP_USER`, `CRAWLER_SMTP_FROM` | the mailbox | optional | Its sign-in name and the sender address (2.7) |
| `CRAWLER_SMTP_PASSWORD` | the mailbox password or app password | optional (secret) | Its password (2.7) |
| `CRAWLER_DASHBOARD_URL` | e.g. the Vercel address | optional | The dashboard link put in notification emails |
| `CRAWLER_PUBLIC_URL` | e.g. `https://seo.example.com` | only behind a custom domain or proxy | The address browsers use for the backend (used for the Google redirect address) |

Other optional settings: `CRAWLER_AI_MAX_CALLS` (AI calls per crawl, default 40), `CRAWLER_AI_RPM` (AI requests per
minute, default 8), `CRAWLER_MAX_SCREENSHOTS` (default 25), `CRAWLER_PDF_PINPOINT_WAIT` (seconds a PDF download waits
for screenshots, default 240), `CRAWLER_NO_CHROME=1` (no Chrome at all), `CRAWLER_MIN_FREE_MB` (memory guard, default
400), `CRAWLER_MAX_CRAWLS` (saved crawls kept per site, default 20), `CRAWLER_MAX_IMPORT_MB` (largest crawl import,
default 500), `CRAWLER_MAX_LIST_URLS` (URLs per List mode crawl, default 10000), `CRAWLER_MAX_UPLOAD_MB` (largest List /
SERP upload, default 20), `CRAWLER_MAX_SCHEDULED_RUNS` (export folders kept per scheduled task, default 100, `0` = no
cap), `CRAWLER_SCHEDULER=0` (no scheduler on this server), `CRAWLER_SETTINGS_EDITABLE=1` (every browser may change
server-wide settings without an access key), `RENDER_POOL_SIZE` (Chromes for Render every page in Chrome). The full list
is in DOCUMENTATION.md, section 22.

**Protect the backend (strongly recommended on any public deployment).** Without an access key, anyone
who finds the Render address can start crawls, read the results and download the reports.

1. Make a long random key on your PC (it is printed only in your own window):
   ```powershell
   .\venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Use letters, digits, `-` and `_` only (the command above does). Do not put the key in a file in this
   repository, a commit, a chat or a ticket; share it with the team through your password manager.
2. Render: service > **Environment** > **Add Environment Variable**: `CRAWLER_ACCESS_KEY` = the key.
3. Add `CRAWLER_CORS_ORIGINS` = the dashboard's address, for example
   `https://website-crawler-virid.vercel.app` (no slash at the end; several addresses separated by commas).
   Opening the Render address itself works without listing it.
4. **Save and deploy**.

What changes with a key:
- Every API request needs the key in the `X-Access-Key` header; a missing or wrong key gets
  `401 {"detail": "Access key required"}`. Download and picture addresses (PDF, Excel, CSV, fix files,
  tickets, screenshots, the live login window) also accept `?key=` because the browser opens them directly;
  the server's log shows it as `key=***` (the browser's own history can still hold such an address).
- Always open: `/healthz` and `/api/health` (Render's health check), `/api/access` (tells the dashboard
  whether to ask), Google's return address `/api/integrations/google/callback` (protected by a one-time code of its
  own) and the dashboard page itself.
- With a key, whoever passed it may change server-wide settings and the Google connections from any browser.
- After 10 different wrong keys from one address within 10 minutes, that address gets `429` for 10 minutes
  (also with the right key). The same wrong key sent again counts once. Counted per visitor address, in
  memory (a restart clears it); an IPv6 address counts as its /64 network. The dialog says how many wrong
  keys are left once 5 or fewer remain. Dashboards that are already open keep working during a lock-out
  (see "pass" below), so one person's typos do not stop the colleagues behind the same office address.
- Which address counts: on Render, `CF-Connecting-IP` (Cloudflare in front of Render sets it, so a visitor
  cannot choose it; without it, the last `X-Forwarded-For` entry). Behind another proxy (nginx, Caddy,
  Traefik) set `CRAWLER_TRUST_PROXY=1` to use the last `X-Forwarded-For` entry, the one your proxy appends,
  or `CRAWLER_CLIENT_IP_HEADER` to a header your proxy sets itself (for example `X-Real-IP`). Do not name a
  header the visitor can send unchanged through your proxy. Without either, the direct peer counts.
- The dashboard asks for the key once (**Team access key** dialog). **Remember on this device** keeps it in
  the browser; otherwise it is forgotten when the tab closes. **Key** > **Lock** in the top bar removes it,
  **Change key** enters another one. Once the key is accepted, the server hands the dashboard a **pass**
  (kept in the page's memory only) that it sends instead of the key. A pass ends when the key changes or the
  server restarts; the dashboard then gets a new one with the stored key, without asking.
- To change the key: set the new value in Render and deploy. Open dashboards ask for the new key on their next
  request.
- On a PC nothing changes: without `CRAWLER_ACCESS_KEY` the dashboard never asks.

Already set inside the image; do not change: `CHROME_BIN=/usr/bin/chromium`,
`CHROMEDRIVER_PATH=/usr/bin/chromedriver`, `PYTHONUNBUFFERED=1`, `MALLOC_ARENA_MAX=2` (less memory held
back by the crawl's many threads), `CRAWLER_REPORTS_DIR=/var/data`. Render sets `PORT` itself.

**Scheduled crawls of a site that needs a login** read the cookie string or header value from an environment
variable whose name starts with `SITE_` (for example `SITE_SESSION_COOKIE`; add it as a secret like the keys
above). Other variables, such as the AI keys or the access key, can never be used by a task. Tasks with a login
can be added or pointed at another site only from the server itself, with the team access key, or with
`CRAWLER_SETTINGS_EDITABLE=1` (DOCUMENTATION.md 19.2).

A deploy (also the one after saving environment variables) restarts the server and stops any crawl that
is running.

### 2.4 Keep saved crawls, schedules and settings across deploys

Without a disk, everything ARACHNIX keeps between crawls is lost at every deploy or restart: saved crawls (File >
Crawls), crawl history (used to compare crawls), **scheduled tasks** and their history, server-wide settings
(Proxy, Crawl Retention, Notifications), profiles, segments, dismissed issues and the **Google connections** (sign-ins
and the PageSpeed key saved in the dialog). Excel / PDF files and screenshots of the current crawl are always temporary.

1. Service > **Disks** > **Add disk**: name `crawler-data`, mount path `/var/data`, size 1 GB (raise it if you keep many
   large crawls; File > Settings > Storage shows the space used and free).
   (With the Blueprint, uncomment the `disk:` block in `render.yaml` instead.)
2. Keep `CRAWLER_REPORTS_DIR=/var/data`.

What the folder holds: `crawls/` (saved crawls), `snapshots/` (history), `schedules.json`, `schedules_history.json`
and `scheduled/` (scheduled-run exports), `settings.json` and `secrets.json` (settings; SMTP and proxy passwords kept
apart, owner-only), `profiles/`, `segments/`, `dismissed/`, `integrations/` (Google sign-ins, the PageSpeed key; owner-
only), `sources/` (page HTML for View Source) and `scheduler.lock`. Settings > Crawl Retention and
`CRAWLER_MAX_CRAWLS` keep it from growing without limit.

Disks need a paid instance. With a disk, Render stops the old server before starting the new one, so each
deploy has a few seconds of downtime, and the service cannot run more than one instance (it must not anyway).

### 2.5 Check that it works

Replace the address with yours. These commands work in PowerShell. `Read-Host` asks for the team access
key, so it is not typed into the command history (without `CRAWLER_ACCESS_KEY`, just press Enter).

1. The server answers:
   ```powershell
   $API = "https://website-crawler-z8qf.onrender.com"
   $H = @{ "X-Access-Key" = (Read-Host "Team access key") }
   Invoke-RestMethod "$API/healthz"
   ```
   Expected: `status` is `ok`.
2. The key is required, and yours works:
   ```powershell
   Invoke-RestMethod "$API/api/access"
   Invoke-RestMethod "$API/api/access" -Headers $H
   curl.exe -s -o NUL -w "%{http_code}`n" "$API/api/crawl/status"
   ```
   Expected: `required : True` and `valid : False`, then `valid : True` with the key, then `401` (no key).
   `required : False` means `CRAWLER_ACCESS_KEY` is not set.
3. It runs on Render:
   ```powershell
   Invoke-RestMethod "$API/api/environment" -Headers $H | Format-List
   (Invoke-RestMethod "$API/api/environment" -Headers $H).chrome
   ```
   Expected: `platform : Render Persistent Container`, `chrome_available : True` (Selenium and a Chromium
   binary were found), `chrome` with `available : True` and an empty `error` (otherwise `error` says what
   is wrong, for example a `CHROME_BIN` that points to a missing file or the last failed Chrome start; it is
   checked at most every 5 minutes; step 5 proves Chrome actually starts) and `max_urls_cap : 2500`
   (`CRAWLER_MAX_URLS`).
4. AI (only if you added a key):
   ```powershell
   Invoke-RestMethod "$API/api/ai/status" -Headers $H | Format-List enabled, provider, model, reason
   ```
   Expected: `enabled : True` and the provider name. `False` shows the reason.
5. Chrome works (this opens and closes the live login window once):
   ```powershell
   $live = Invoke-RestMethod -Method Post -Uri "$API/api/live/start" -Headers $H -ContentType "application/json" -Body '{"url": "https://example.com"}'
   $live | Format-List
   Invoke-RestMethod -Method Post -Uri "$API/api/live/close" -Headers $H -ContentType "application/json" -Body (@{ session_id = $live.session_id } | ConvertTo-Json) | Format-List
   ```
   Expected: a `session_id`, then `ok : True`. An error message here means Chrome is not working (section 5).
6. Only the dashboard's address may call the backend from a browser (with `CRAWLER_CORS_ORIGINS` set):
   ```powershell
   curl.exe -s -D - -o NUL -X OPTIONS "$API/api/crawl/status" -H "Origin: https://website-crawler-virid.vercel.app" -H "Access-Control-Request-Method: GET" -H "Access-Control-Request-Headers: x-access-key"
   curl.exe -s -D - -o NUL -X OPTIONS "$API/api/crawl/status" -H "Origin: https://example.com" -H "Access-Control-Request-Method: GET"
   ```
   Expected: the first answers `200` with `access-control-allow-origin: https://website-crawler-virid.vercel.app`,
   the second `400` without that header.
7. Saved data lasts (with the disk of 2.4):
   ```powershell
   Invoke-RestMethod "$API/api/crawls/storage" -Headers $H | Format-List dir, env_set, warning, disk_free
   Invoke-RestMethod "$API/api/schedules" -Headers $H | Select-Object -ExpandProperty scheduler
   ```
   Expected: `dir : /var/data`, `env_set : True`, an empty `warning`, and the scheduler `running : True`.
8. Render **Logs** show `Uvicorn running on http://0.0.0.0:10000` and `Application startup complete`.

With Git Bash or macOS/Linux, the same checks with `curl` (`read -s` asks for the key without showing it):
```bash
API=https://website-crawler-z8qf.onrender.com
read -rsp "Team access key: " KEY; echo
curl -s "$API/healthz"
curl -s "$API/api/access"
curl -s -H "X-Access-Key: $KEY" "$API/api/access"
curl -s -o /dev/null -w "%{http_code}\n" "$API/api/crawl/status"                        # 401
curl -s -H "X-Access-Key: $KEY" "$API/api/environment"
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Access-Key: $KEY" "$API/api/crawl/status"   # 200
curl -s -H "X-Access-Key: $KEY" "$API/api/crawls/storage"
```

### 2.6 Optional: Google Search Console, GA4 and PageSpeed Insights

Nothing has to be connected; without a connection the Search Console, Analytics and PageSpeed tabs say "Not
connected". The connections are made in the dashboard (Configuration > API Access, DOCUMENTATION.md section 18); the
server only needs a Google OAuth client for Search Console / GA4, and optionally a PageSpeed key.

**A. Google OAuth client (Search Console and GA4)**

1. Open https://console.cloud.google.com and pick or create a project (for example "ARACHNIX").
2. **APIs & Services > Library:** enable **Google Search Console API**, **Google Analytics Admin API** and
   **Google Analytics Data API**.
3. **APIs & Services > OAuth consent screen:** user type **Internal** (Google Workspace accounts of your company) or
   **External** with the team's Google accounts added as test users. Add the scopes `.../auth/webmasters.readonly` and
   `.../auth/analytics.readonly`.
4. **APIs & Services > Credentials > Create credentials > OAuth client ID**, application type **Web application**.
   Under **Authorised redirect URIs** add the backend's address followed by `/api/integrations/google/callback`:
   - Render: `https://website-crawler-z8qf.onrender.com/api/integrations/google/callback` (your service's address).
   - Office PC: `http://localhost:8000/api/integrations/google/callback`. Google refuses private addresses such as
     `http://10.10.6.56:8000`, so connect once **from the server PC itself** at http://localhost:8000 (the connection
     is server-wide; everyone uses it afterwards), or sign in from another PC and paste the address the browser ends on
     into the dialog (it shows "cannot connect", but the address still carries the code).
   - Behind a custom domain or proxy: that address, and set `CRAWLER_PUBLIC_URL` to it.
   The API Access dialog shows the exact redirect address to register for this server.
5. Copy the **Client ID** and **Client secret** into the server's environment as `GOOGLE_OAUTH_CLIENT_ID` and
   `GOOGLE_OAUTH_CLIENT_SECRET` (Render: Environment tab, then deploy; PC: Windows user variables, then restart
   `run.py`). Or paste them in Configuration > API Access (**Save client**); the environment wins.
6. In the dashboard: Configuration > API Access > Google Search Console > **Sign in with Google**, pick the property;
   the same for Google Analytics 4 (account, property, data stream).

**No browser sign-in (alternative):** create a **service account** in the same project, add a JSON key, add the service
account's e-mail as a user of the Search Console property and as a Viewer of the GA4 property, then add the key in the
dialog (**Use a service-account key…**). The key is stored on the server only.

**B. PageSpeed Insights key**

1. In the same Google Cloud project: **APIs & Services > Library** > enable **PageSpeed Insights API**.
2. **Credentials > Create credentials > API key**; restrict it to the PageSpeed Insights API.
3. Set it as `PSI_API_KEY` in the server's environment, or paste it under Configuration > API Access > PageSpeed
   Insights and press **Connect** (the environment wins). Google allows 25,000 tests a day and 240 a minute per project;
   without a key the shared quota is effectively zero.

Who may connect: changing a connection changes it for everyone, so it needs the right to change settings (the team
access key, the server's own computer, or `CRAWLER_SETTINGS_EDITABLE=1`). Refresh tokens, service-account keys and the
PageSpeed key are kept in `<CRAWLER_REPORTS_DIR>/integrations/` (owner-only files) and never sent to a browser; keep the
disk of 2.4, or the connections are lost at the next deploy.

### 2.7 Optional: email notifications

ARACHNIX emails a summary when a crawl finishes (File > Settings > Notifications > Email on Crawl Complete) and after
scheduled crawls, through your own SMTP server. Set the server either in the dialog (File > Settings > Notifications,
**Send Test Email**) or in the environment, which suits a hosted server:

| Name | Example | Notes |
| --- | --- | --- |
| `CRAWLER_SMTP_HOST` | `smtp.office365.com` (Microsoft 365), `smtp.gmail.com` (Gmail) | Required for email |
| `CRAWLER_SMTP_PORT` | `587` | 587 with TLS (STARTTLS), 465 with SSL |
| `CRAWLER_SMTP_ENCRYPTION` | `tls` | `tls`, `ssl` or `none` |
| `CRAWLER_SMTP_USER` | `seo-bot@example.com` | The mailbox that sends |
| `CRAWLER_SMTP_PASSWORD` | (secret) | Its password; Gmail needs an app password (2-step verification on) |
| `CRAWLER_SMTP_FROM` | `seo-bot@example.com` | The sender address |
| `CRAWLER_DASHBOARD_URL` | `https://website-crawler-virid.vercel.app` | The dashboard link in the email |

While the dialog has no SMTP host, every `CRAWLER_SMTP_*` value is used; once a host is saved in the dialog, the
environment only fills the fields left empty. The recipients and **Email on Crawl Complete** are set in the dialog (they
are kept in the reports folder, so keep the disk of 2.4); scheduled tasks list their own recipients. The password is
never shown, logged or sent to a browser. Test it with **Send Test Email**; an error says what the mail server answered.

### 2.8 Scheduled crawls and sleeping hosts

The scheduler is part of the backend: it checks every 30 seconds whether a task is due, but only while the server is
running and awake.

- **Render Standard (paid)** instances do not sleep: schedules run on time. Keep the disk of 2.4, otherwise the tasks
  themselves disappear at the next deploy.
- **Render Free** sleeps after 15 minutes without visits and has no disk: schedules do not run while it sleeps and are
  lost at every deploy. The Scheduling dialog warns about it. Do not use it for scheduling.
- **Serverless hosts** (Vercel functions) never run the scheduler.
- **An office PC** must keep `run.py` running and must not sleep (1.3).
- A task that was due while the server was off or asleep runs **once** when the server is back (within about 30
  seconds); its history says how many runs were missed. A task that is due while another crawl runs waits up to its
  "wait" minutes, then that run is skipped (recorded in the history).
- A deploy restarts the server and stops a scheduled crawl that is running; avoid deploying at the scheduled times.

---

## 3. Dashboard on Vercel

1. Put the backend address into **both** copies of `config.js` (`config.js` and `frontend/config.js`), with
   no slash at the end:
   ```js
   window.CRAWLER_API_BASE_URL = "https://website-crawler-z8qf.onrender.com";
   ```
2. Make sure the dashboard copies match the main file (after any dashboard change). In Git Bash:
   ```bash
   cp index.html frontend/index.html && cp index.html web/static/index.html
   cp web/static/brand/* frontend/static/brand/    # logo and favicon files (only after changing them)
   ```
   In PowerShell:
   ```powershell
   Copy-Item index.html frontend\index.html; Copy-Item index.html web\static\index.html
   ```
3. Commit and push.
4. Vercel: **Add New...** > **Project**, import the repository. Settings:
   - **Framework Preset:** Other
   - **Root Directory:** `frontend` (recommended: that folder holds only the dashboard, `index.html`,
     `config.js` and the logo / favicon files in `static/brand/`, plus `vercel.json` and `.vercelignore`). The
     repository root also works: the root `.vercelignore` uploads only `index.html`, `config.js` and
     `vercel.json` from there (the browser then uses the built-in SVG favicon; the PNG icons are not uploaded).
   - **Build Command**, **Output Directory**, **Install Command:** leave empty (no build step).
   - **Environment variables:** none needed. Never put the team access key, a Google client secret or any other key
     into `config.js` or Vercel: those files are public. Each person enters the access key in the dashboard; the Google
     settings belong to the backend (2.6).
5. **Deploy**. Open the Vercel address; the dashboard loads and talks to the Render backend.
6. Render: set `CRAWLER_CORS_ORIGINS` to the Vercel address (for example
   `https://website-crawler-virid.vercel.app`, no slash at the end) and `CRAWLER_ACCESS_KEY` (2.3,
   "Protect the backend"), then **Save and deploy**. A custom domain or a second Vercel address goes into
   the same list, separated by commas. Vercel's per-commit preview addresses are not on the list, so the
   browser refuses their calls: test with the production address.
7. Open the Vercel address again: the **Team access key** dialog appears. Enter the key; tick **Remember on
   this device** on your own computer only.
8. For an existing Vercel project, check **Root Directory** and the connected Git repository in the
   project's **Settings**, and that the deployment lists no Functions (nothing from `api/` may be deployed).
   On 2026-10-23 the live dashboard (`website-crawler-virid.vercel.app`) served a `config.js` that points
   to `website-crawler-chrome.onrender.com`, not to the address in this repository's `config.js`; it was
   deployed from other code.

Every push to `main` redeploys the dashboard. `config.js` is served with `Cache-Control: no-cache`, so a
new backend address is picked up on the next page load. "Sign in with Google" works from the Vercel dashboard too: the
consent page opens in a new tab (the same tab when pop-ups are blocked), Google sends it to the backend's callback
address (2.6), which confirms the connection and closes itself (or says to go back to the dashboard).

**CORS:** without `CRAWLER_CORS_ORIGINS` the backend accepts browser requests from any website (as before).
With it, only the listed addresses may call it from a browser; it also allows the `X-Access-Key` header.
The dashboard sends no cookies (the key travels in that header), so credentials are never allowed. CORS only
stops other websites' pages; `curl` and scripts are stopped by the access key.

**Security note:** the access key is the backend's only lock. Without `CRAWLER_ACCESS_KEY`, anyone who
knows the Render address can use it. Share the key only inside the team, and change it when someone leaves.

---

## 4. Pre-flight checklist

- [ ] `requirements.txt` lists `selenium` (not commented out).
- [ ] Render: runtime **Docker**, instance **Standard** (`1c-2g`), **1** instance, health check `/healthz`.
- [ ] Render environment: `MAX_CHROME_INSTANCES=2`, `CRAWLER_MAX_URLS=2500`, `CRAWLER_AI_PROVIDER=auto`,
      `CRAWLER_AI_ALLOW_PRIVATE=0`, `CRAWLER_REPORTS_DIR=/var/data`, AI keys if you have them.
- [ ] A disk is mounted at `/var/data` (2.4); check 7 of 2.5 shows `env_set : True` and no warning.
- [ ] `CRAWLER_ACCESS_KEY` is set to a long random key (2.3), and the team has it in the password manager.
- [ ] `CRAWLER_CORS_ORIGINS` holds the Vercel address (no slash at the end).
- [ ] The deploy log ends with `Uvicorn running on http://0.0.0.0:10000`.
- [ ] Checks 1-8 in 2.5 pass (health, key required, Render and Chrome status, AI, Chrome start, CORS, storage).
- [ ] `config.js` and `frontend/config.js` hold the Render address; `frontend/index.html` and
      `web/static/index.html` match `index.html`.
- [ ] Vercel: Root Directory `frontend` (or root with `.vercelignore`), preset Other, no Functions.
- [ ] The Vercel dashboard asks for the team access key; a wrong key says "Wrong key", the right one opens it.
- [ ] From the Vercel dashboard: a small crawl (for example 25 URLs) finishes, and the Excel and PDF
      downloads work; File > Crawls lists it.
- [ ] `CRAWLER_MAX_URLS` matches the instance size (2.1); Crawl Config shows "This server crawls at
      most N URLs per crawl".
- [ ] A password-login test (the dashboard's **Test login**) succeeds on a test account.
- [ ] Optional: Google OAuth client set (2.6); API Access shows Search Console / GA4 "Connected" after signing in.
- [ ] Optional: email set (2.7); **Send Test Email** arrives.
- [ ] Optional: a test task in File > Scheduling (Run now) finishes and appears in its History.

---

## 5. Troubleshooting

**Chrome is missing** - logins on JavaScript login pages fail, the live login window does not open, the PDF
shows drawn sketches instead of screenshots, or messages such as "Selenium is not installed",
"Headless Chrome could not start".
- The service is still running the old Python runtime: switch it to **Docker** (2.2, Way A).
- `CRAWLER_NO_CHROME` must not be `1`, and File > Settings > Embedded Browser must be enabled.
- `/api/environment` > `chrome` > `error` says what is wrong (checked at most every 5 minutes).
- Do not override `CHROME_BIN` or `CHROMEDRIVER_PATH`. A `CHROME_BIN` that points to a missing file is
  reported there ("CHROME_BIN points to ..., which does not exist") and the server uses another installed
  Chromium if it finds one; fix or remove the variable.

**401 "Access key required"** - the request had no key or a wrong one.
- In the dashboard: the **Team access key** dialog asks again ("Wrong key" when the stored key no longer
  works, for example after the key was changed on Render). Ask for the current key.
- With `curl` or PowerShell: send the header `X-Access-Key: <key>` (2.5). `?key=` works only on the
  download and picture addresses.
- Check the value in Render (**Environment**): no extra quotes; spaces at the start or end are ignored.
- `/api/access` without a key says whether the server wants one (`required`).

**429 "Too many wrong access keys"** - 10 different wrong keys came from your address within 10 minutes
(colleagues behind the same office or VPN address count together). Wait 10 minutes (the answer's
`Retry-After` header says how long), then enter the right key. Dashboards that were already open keep
working; other addresses are not affected. A deploy or restart also clears it.

**Settings or API Access are read-only ("Server settings are read-only from other computers")** - the server has no
access key and this browser is not on the server's own computer. Change them at http://localhost:8000 on the server,
set `CRAWLER_ACCESS_KEY` (2.3), or set `CRAWLER_SETTINGS_EDITABLE=1` on a trusted network.

**The chip says "Server refuses this page (CORS)"** - the server answers, but `CRAWLER_CORS_ORIGINS` does not
list the address the dashboard was opened from (shown after about 45 seconds, so a waking server is not
mistaken for it). See the next entry.

**CORS error in the browser console after setting `CRAWLER_CORS_ORIGINS`** ("blocked by CORS policy",
"No 'Access-Control-Allow-Origin' header")
- The dashboard's address must be in the list exactly: `https://`, the full host name, no slash or path at
  the end, the same `www.` or none. Open the dashboard and copy its address from the browser's address bar.
- Preview deployments of Vercel have other addresses; add them or test with the production address.
- Changed the list? **Save and deploy**: the list is read when the server starts.
- Unsure: remove `CRAWLER_CORS_ORIGINS` (every website allowed, as before) and check again; the access key
  still protects the backend.

**"The server's browser is busy with another task"** - every Chrome slot is in use. The crawl keeps its
browser for 60 seconds after the last use. Wait a minute, or use `MAX_CHROME_INSTANCES=2` on Standard.

**Out of memory** - Render shows an "out of memory" event and restarts the server; the running crawl is
lost. Or the tool says "Only N MB of memory free on this server" (it refuses to start Chrome rather than
crash).
- Most often the crawl was too big: set `CRAWLER_MAX_URLS` to the size for your instance (2.1), or crawl
  one section at a time with an include pattern. Keep `CRAWLER_MIN_FREE_MB` at 400 or more so the memory
  guard stops queuing in time (the report then says the crawl stopped early because memory was nearly full).
- Render every page in Chrome needs more memory: keep `MAX_CHROME_INSTANCES=2` on Standard, or crawl fewer URLs with it.
- Use the Standard instance or bigger; on 512 MB turn Chrome off (2.1).
- Lower `MAX_CHROME_INSTANCES` to `1`.

**Deploy fails or the service keeps restarting**
- Read the deploy log. The server must listen on Render's `PORT`; leave **Docker Command** empty.
- Render restarts the server when `/healthz` does not answer within 5 seconds for 60 seconds in a row.

**CORS error in the browser console** (Vercel dashboard)
- `config.js` holds the wrong address: check for typos, a trailing `/`, or `http` instead of `https`.
- The backend is down or still deploying. Render's own error page usually has no CORS headers, so the
  browser reports it as a CORS error. Open `<address>/healthz` directly.

**Slow first request**
- Free instances sleep after 15 minutes without visits and take about a minute to wake up. Paid instances
  do not sleep.
- After a deploy, the first crawl starts Chrome, which takes a few seconds.

**Timeouts**
- Crawls run in the background; the dashboard only polls for progress, so long crawls do not time out.
- The first PDF after a crawl needs the issue screenshots. The dashboard's **PDF report** button prepares
  them first ("Preparing PDF pictures n of m") and then downloads the PDF; a direct request to
  `/api/crawl/report/pdf` waits up to 240 seconds for them (`CRAWLER_PDF_PINPOINT_WAIT`). If it fails, wait
  until the screenshots are ready and download again.

**A crawl disappeared** - the crawl on screen lives in the server's memory; a deploy or a restart clears it. A finished
crawl is also saved (File > Crawls) when the reports folder lasts (2.4 on Render, 1.2 on a PC). Avoid pushing to `main`
or editing environment variables during a crawl, or turn **Auto-Deploy** off in Render's settings.

**Saved crawls, schedules or settings vanished after a deploy or restart** - the reports folder is not lasting: add
the disk of 2.4 (Render) or set `CRAWLER_REPORTS_DIR` to a normal folder (1.2, PC). File > Settings > Storage warns
when the folder is the system temp folder.

**A scheduled crawl did not run**
- The server was asleep or off at that time (2.8): it runs once when the server is back. Render Free cannot keep a
  schedule.
- Another crawl ran longer than the task's wait minutes: the run was skipped (File > Scheduling > History says so).
- The task is not valid (missing profile or `SITE_` login variable): the task list says why.
- `CRAWLER_SCHEDULER=0` is set, or a second ARACHNIX process on the same reports folder holds the scheduler lock
  (`GET /api/schedules` > `scheduler` gives the reason).

**Google sign-in fails**
- "redirect_uri_mismatch": the redirect URI registered in Google Cloud must match the one the API Access dialog shows,
  character for character (`https`, host, no trailing slash, the `/api/integrations/google/callback` path).
- On the office server Google refuses `http://10.x.x.x` addresses: connect from the server PC at http://localhost:8000,
  or paste the address you land on, or use a service account (2.6).
- "Access blocked" or an error 403 from Google: with user type External in testing, add your Google account as a test
  user on the OAuth consent screen, or use user type Internal (Google Workspace).
- "Not connected" after a deploy: the connection is kept on the disk (2.4); without it, connect again.
- No properties listed: the Google account (or service account) has no access to that Search Console property / GA4
  property; add it there.

**PageSpeed: "daily quota reached" or 429** - Google allows 25,000 tests a day and 240 a minute per project; the URLs
left say "Not checked: daily quota reached" and can be fetched the next day. Without a key the shared quota is
effectively zero: add `PSI_API_KEY` (2.6).

**No notification email**
- File > Settings > Notifications > **Send Test Email** shows the mail server's answer in plain words.
- Microsoft 365: the mailbox needs SMTP AUTH allowed. Gmail: use an app password.
- Check the spam folder, and that **Email on Crawl Complete** is on with recipients (or the task's own recipients).

**Login failures**
- Use **Test login** in the dashboard first; it says what went wrong and takes a screenshot.
- JavaScript login pages need Chrome (see "Chrome is missing").
- A bot check (CAPTCHA, Cloudflare): ARACHNIX never passes it. Sign in with your own browser and use **Paste cookies**
  (the guide in the dashboard shows where to copy them), or allow ARACHNIX in the site's firewall.
- Two-step login: use the **Browser window** (live login) or paste the session cookies.
- The tool cannot find the fields: fill in the username, password and submit selectors.
- Sign-out links are never followed, so a crawl does not log itself out.

**AI is off**
- `<address>/api/ai/status` gives the reason.
- No key is set, or it was saved without a deploy: save environment variables with **Save and deploy**
  (or deploy by hand afterwards).
- "AI quota reached - showing rule-based result": the free daily quota is used up. Results stay rule-based;
  the free quotas reset daily. A Groq key as well as a Gemini key gives a fallback.
- Signed-in crawls are never sent to a free cloud AI. That is intended (`CRAWLER_AI_ALLOW_PRIVATE=0`).

**Chinese, Japanese or Korean text shows as boxes in screenshots** - the image ships Latin fonts and emoji
only. Add `fonts-noto-cjk` to the `apt-get install` line in the `Dockerfile` (the image gets about 90 MB
larger).

**"start-app: warning: could not make /var/data writable"** - check that the disk's mount path and
`CRAWLER_REPORTS_DIR` are the same folder. Crawls still work, but saved crawls, history, schedules and settings are not
kept.
