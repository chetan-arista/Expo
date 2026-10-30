# Third-party notices

This tool's code is its own. Some features were designed after studying open-source SEO crawlers; the
ideas were re-implemented in this project's structure (no files were copied). The projects are credited
here under their licences.

## open-seo-crawler

- Source: https://github.com/puneetindersingh/open-seo-crawler
- Licence: MIT License, Copyright (c) 2026 Puneet Inder Singh (full licence text: https://opensource.org/license/mit)
- Ideas used:
  - `crawler/seo/bot_check.py`, `crawler/seo/tabs/ai_crawlers.py`: search / AI bot readability check (sample pages
    fetched as a browser and as crawlers, compared on status, challenge pages, redirects, noindex, title, H1,
    canonical and 3-word-shingle text similarity; mobile vs desktop Googlebot; content that needs JavaScript).
  - `crawler/seo/robots_audit.py`, `crawler/seo/tabs/robots_txt.py`: robots.txt audit for AI crawlers (answer vs
    training-only crawlers, Content-Signal lines) and the AI meaning of robots directives (data-nosnippet,
    max-snippet, unavailable_after, crawler-specific noindex).
  - `crawler/seo/trap_probe.py`: random-URL probe for soft-404 servers and infinite URL spaces.
  - `crawler/seo/tabs/hygiene.py`, PageData keys in `crawler/seo/extract.py`: page hygiene checks (staging /
    private-network links, dead same-page anchors, compression, cache headers, doctype, charset, DOM size,
    cross-host canonicals, malformed hrefs, crawl-trap URL patterns).
  - `crawler/seo/crawl_budget.py`, `crawler/seo/tabs/parameters.py`: URL-parameter classification and the
    robots.txt Disallow suggestion; redirect type labels.
  - `crawler/net_guard.py`, `crawler/engine.py`: per-host adaptive backoff for 429/503 answers (Retry-After,
    requeue), firewall block-page detection, and crawl-trap URL guards (repeated path segments, page-builder
    pagination parameters).

## LibreCrawl

- Source: https://github.com/PhialsBasement/LibreCrawl
- Licence: MIT License, Copyright (c) 2025 Phiality (full licence text: https://opensource.org/license/mit)
- Ideas used:
  - `crawler/net_guard.py`: classifying a failed request by its root cause (DNS, SSL, timeout, refused
    connection) from the exception chain.

## Software this tool uses (not modified)

These are installed as dependencies, unmodified, and keep their own licences. Python packages come from
`requirements.txt`; the system packages are installed into the hosted server image by the `Dockerfile`.
Inside that image each Debian package's full copyright and licence file is at
`/usr/share/doc/<package>/copyright`.

| Component | Used for | Licence |
| --- | --- | --- |
| requests | HTTP crawling | Apache-2.0 |
| urllib3 | retries, network errors | MIT |
| beautifulsoup4 | HTML parsing | MIT |
| lxml | fast HTML parser | BSD-3-Clause |
| pycountry | ISO language / country codes | LGPL-2.1-only (used as an unmodified library) |
| openpyxl | Excel export | MIT |
| reportlab | PDF reports | BSD |
| Pillow | screenshot annotation | MIT-CMU (HPND) |
| FastAPI, Starlette | web API | MIT; BSD-3-Clause |
| uvicorn | web server | BSD-3-Clause |
| pydantic | request and AI-answer validation | MIT |
| Selenium | driving headless Chrome | Apache-2.0 |
| Chromium, chromium-driver (Debian) | browser logins, rendering, screenshots | BSD-3-Clause and others (see the package's copyright file) |
| DejaVu fonts (fonts-dejavu-core) | Unicode text in PDF reports | Bitstream Vera licence, public domain changes |
| Liberation fonts (fonts-liberation) | page text in screenshots | SIL Open Font License 1.1 |
| Noto Color Emoji (fonts-noto-color-emoji) | emoji in screenshots | SIL Open Font License 1.1 |
| tini | init process in the server image | MIT |

## Software shipped with this tool (vendored, not modified)

These files are kept in the repository exactly as their authors publish them, each next to its own licence.

### axe-core

- Files: `crawler/vendor/axe/axe.min.js` (axe-core 4.14.0, the npm package's browser build, unmodified),
  `crawler/vendor/axe/LICENSE` (its licence text), `crawler/vendor/axe/LICENSE-3RD-PARTY.txt` (the licences of
  the code bundled inside `axe.min.js`, MIT and others) and `crawler/vendor/axe/rules.json` (rule data read from
  `axe.min.js`, under the same licence). `crawler/vendor/axe/README.md` lists the version, source and hashes.
- Author: Deque Systems, Inc. (https://github.com/dequelabs/axe-core)
- Licence: Mozilla Public License 2.0 (MPL-2.0). The source code form of this version is at
  https://github.com/dequelabs/axe-core/tree/v4.14.0 and in the same npm package (`axe.js`).
- Fetched by `crawler/vendor/fetch_axe.py` from https://registry.npmjs.org/axe-core/-/axe-core-4.14.0.tgz (npm
  integrity checked; `axe.min.js` checked against the hash axe-core publishes). ARACHNIX's own code
  (`crawler/render_capture.py`, `crawler/seo/tabs/accessibility.py`) only injects and runs the file and is not
  covered by the MPL.
- Used for: the Accessibility tab's axe-core rules, run in pages rendered in Chrome.

## Build 4 dependencies

Checked on 2026-10-28. Each licence below was read from the package's own metadata (`License-Expression`,
`License` or its classifiers) in this project's environment; pypdf, which the PDF task installs later, was read
from the metadata of the exact release on PyPI (`pip install --dry-run --report`). Every package is used unmodified
and keeps its own licence; none of their code is copied into ARACHNIX.

### Python packages

| Component | Version | Used for | Licence |
| --- | --- | --- | --- |
| mcp, mcp-types | 2.3.0 | MCP server and test client, so AI agents can operate ARACHNIX | MIT |
| jsonschema, referencing, jsonschema-specifications, rpds-py | 4.26.0, 0.37.0, 2025.9.1, 2026.9.1 | checking MCP tool arguments (installed with mcp) | MIT |
| sse-starlette | 3.6.0 | MCP streaming responses (installed with mcp) | BSD-3-Clause |
| PyJWT | 2.15.1 | token handling inside mcp (installed with mcp) | MIT |
| cryptography | 50.0.2 | signatures for PyJWT (installed with mcp) | Apache-2.0 OR BSD-3-Clause |
| python-multipart | 0.0.32 | form uploads (installed with mcp; the report logo upload) | Apache-2.0 |
| httpx2, httpcore2 | 2.16.0 | HTTP client inside mcp (installed with mcp) | BSD-3-Clause |
| truststore | 0.10.5 | the system's certificate store for httpx2 (installed with mcp) | MIT |
| pywin32 (Windows only) | 312 | Windows support inside mcp (installed with mcp on Windows) | PSF |
| opentelemetry-api | 1.45.1 | required by mcp (and FastAPI); nothing is sent anywhere unless a tracing exporter is set up, and ARACHNIX sets none | Apache-2.0 |
| typing-inspection | 0.4.4 | required by mcp (and pydantic) | MIT |
| pyspellchecker | 0.9.1 | offline spelling check (Content > Spelling Errors) | MIT |
| pypdf | 6.21.3 | PDF title, author, page count and text (PDF details) | BSD-3-Clause |

- attrs (MIT), cffi (MIT-0) and pycparser (BSD-3-Clause) are also needed by jsonschema and cryptography; they were
  already installed with Selenium.
- pyspellchecker's word lists (`spellchecker/resources/*.json.gz`) ship inside the package under its MIT licence. Its
  author built them as word frequencies from the OpenSubtitles corpus (OPUS; P. Lison and J. Tiedemann, 2016) with
  the package's own script; ARACHNIX uses them as they are and adds only its own site dictionary.
- Sources: https://github.com/modelcontextprotocol/python-sdk, https://github.com/python-jsonschema (jsonschema,
  referencing, jsonschema-specifications), https://github.com/crate-py/rpds, https://github.com/sysid/sse-starlette, https://github.com/jpadilla/pyjwt, https://github.com/pyca/cryptography,
  https://github.com/Kludex/python-multipart, https://github.com/pydantic/httpx2,
  https://github.com/sethmlarson/truststore, https://github.com/mhammond/pywin32,
  https://github.com/open-telemetry/opentelemetry-python, https://github.com/pydantic/typing-inspection,
  https://github.com/barrust/pyspellchecker, https://github.com/py-pdf/pypdf.

### Data and methods (no code copied)

- **schema.org vocabulary**: the structured-data property checks use type and property data generated by a script from
  the schema.org vocabulary. Schema.org is licensed under Creative Commons Attribution-ShareAlike 3.0 (CC BY-SA 3.0);
  attribution: "Schema.org vocabulary, https://schema.org". The generated data keeps this licence and attribution.
- **Sustainable Web Design model**: the High Carbon Rating and the grams of CO2 per page view follow the Sustainable
  Web Design model as published at https://sustainablewebdesign.org and in The Green Web Foundation's CO2.js
  (https://github.com/thegreenwebfoundation/co2.js, Apache-2.0). ARACHNIX re-implements the published method in its
  own code and cites it; no CO2.js code is copied.
- **Roboto character widths**: the title and meta description pixel widths (`crawler/seo/text_metrics.py`, and the
  `widths.json` the browser extension uses) are advance widths measured from the hinted Roboto Regular font
  (https://github.com/googlefonts/roboto, Apache-2.0). Only the width numbers are kept; the font is not shipped.

### Services ARACHNIX can connect to (nothing bundled)

- **LanguageTool** (grammar check): a LanguageTool server the team runs itself, or the public LanguageTool API after
  an explicit opt-in. The server software is LGPL-2.1 (https://github.com/languagetool-org/languagetool); ARACHNIX only
  sends HTTP requests to it and ships none of it. The public API is used under its own terms.
- **mcp-remote** (npm): only shown as an optional configuration line for AI clients that need it; ARACHNIX neither
  ships nor installs it. Licence MIT (npm registry metadata, version 0.15.2, checked 2026-10-28).
- **Google Search Console, Google Analytics 4, PageSpeed Insights, Green Web Foundation, IndexNow, Moz, Majestic,
  Ahrefs, Slack, Microsoft Teams, Discord, Jira and GitHub**: called over HTTPS with the team's own accounts and keys,
  under each service's own terms. No code from these services is included.
