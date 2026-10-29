# syntax=docker/dockerfile:1
#
# Website Audit Tool - backend image (FastAPI + headless Chromium) for Render or any Docker host.
# Chromium powers password logins on JavaScript login pages, the live login window, JavaScript page
# rendering and the screenshots in the PDF. See DEPLOY.md.
#
#   docker build -t website-audit .
#   docker run --rm -p 8000:8000 -e GEMINI_API_KEY website-audit      (then open http://127.0.0.1:8000)
#
# Python 3.11 on Debian 13 "trixie" (current Debian stable, which still gets Chromium security updates).
FROM python:3.11-slim-trixie

# CHROME_BIN / CHROMEDRIVER_PATH are read by crawler/browser.py. Debian ships Chromium and its driver at the
# same version, so they always match and Selenium never has to download a driver at runtime.
# MAX_CHROME_INSTANCES caps Chromes running at once and turns on the low-memory Chrome flags and Chrome's
# free-memory guard; render.yaml raises it to 2 on the 2 GB plan. That guard only closes Chrome: the crawl
# itself keeps every URL in memory and has no limit of its own, so big sites need Max URLs (DEPLOY.md 2.1).
# MALLOC_ARENA_MAX=2: by default glibc gives the crawl's many worker threads up to 8 memory pools per CPU
# it sees (in a container usually the host's CPUs), and each pool keeps freed memory; two pools keep the
# server's memory use closer to what the crawl really holds.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER_PATH=/usr/bin/chromedriver \
    MAX_CHROME_INSTANCES=1 \
    MALLOC_ARENA_MAX=2 \
    CRAWLER_REPORTS_DIR=/var/data

# chromium + chromium-driver: the browser and its WebDriver.
# fonts-dejavu-core: Unicode text in the PDF reports (crawler/pdf_report.py looks for DejaVuSans).
# fonts-liberation: Arial/Times/Courier-compatible fonts, so pages and the search-result previews render
#   like they do on a PC. fonts-noto-color-emoji: emoji in page screenshots.
# tini: a tiny init (PID 1) that reaps Chrome's helper processes and passes stop signals on.
# Versions are not pinned on purpose: every build picks up Debian's latest Chromium security fixes.
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-dejavu-core \
        fonts-liberation \
        fonts-noto-color-emoji \
        tini \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user that runs the web server and Chrome. /var/data holds crawl snapshots and dismissed
# issues (CRAWLER_REPORTS_DIR); mount a persistent disk there to keep them across deploys.
RUN groupadd --system app \
 && useradd --system --gid app --create-home --home-dir /home/app --shell /usr/sbin/nologin app \
 && mkdir -p /var/data \
 && chown app:app /var/data

WORKDIR /app

# Dependencies first, so code changes do not reinstall them (layer cache)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
# Bytecode compiled at build time: the app user cannot (and need not) write into /app
RUN python -m compileall -q crawler web cli.py run.py

# Start-up: as root, make the reports folder writable for "app" (a freshly attached Render disk is owned by
# root), then drop to "app" for the actual server. Started as any other user it just runs the command.
# (The sed below strips CRLF line endings, which a Windows checkout of this Dockerfile may have.)
COPY <<"EOF" /usr/local/bin/start-app
#!/bin/sh
set -e
if [ "$(id -u)" = "0" ]; then
    dir="${CRAWLER_REPORTS_DIR:-}"
    if [ -n "$dir" ]; then
        if ! { mkdir -p "$dir" && find "$dir" ! -user app -exec chown app:app {} +; }; then
            echo "start-app: warning: could not make $dir writable; crawl history will not be saved" >&2
        fi
    fi
    export HOME=/home/app USER=app
    exec setpriv --reuid=app --regid=app --init-groups -- "$@"
fi
exec "$@"
EOF

RUN sed -i 's/\r$//' /usr/local/bin/start-app && chmod 0755 /usr/local/bin/start-app

# Documentation only: Render sends traffic to $PORT (10000 there); locally the server listens on 8000
EXPOSE 8000

# For `docker run` and other Docker hosts; Render checks healthCheckPath from render.yaml
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT', '8000'), timeout=4)"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/start-app"]
# One worker process on purpose: crawl jobs and their results live in this process's memory. uvicorn would
# otherwise start $WEB_CONCURRENCY workers (Render sets it from the CPU count) and split the jobs between them.
CMD ["sh", "-c", "exec uvicorn web.app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
