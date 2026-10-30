"""
Raw HTML of every crawled internal HTML page, kept on disk for the View Source detail tab.

One gzip file per URL under <reports_dir>/sources/<crawl id>/, named by a hash of the URL, so memory holds only a
set of short digests (a few bytes per page). Bodies are what the crawler read: at most CrawlConfig.max_page_size_kb
(the engine cuts larger pages before parsing). Disabled on serverless hosts (no lasting disk) and when
CrawlConfig.store_source is off. Old crawls' folders are removed when a new store starts (KEEP_CRAWLS newest kept,
none older than MAX_AGE_HOURS), so the folder never grows without limit.

    store = SourceStore.for_crawl(config, start_url)   # None when disabled
    store.put(url, html)                               # from the crawl's worker threads
    store.get(url) -> str | None                       # the page's HTML, None when not stored
"""
import gzip
import hashlib
import os
import re
import shutil
import tempfile
import threading
import time
from typing import Optional, Set

KEEP_CRAWLS = 5          # newest crawl folders kept (one crawl runs per server; saved crawls copy what they need)
MAX_AGE_HOURS = 72       # older folders are removed whatever their number
GZIP_LEVEL = 5           # good ratio for HTML at a fraction of level 9's CPU time
_SAFE = re.compile(r"[^a-z0-9.-]+")


def _serverless() -> bool:
    return bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def default_root(reports_dir: str = "") -> str:
    return os.path.join(reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir(), "sources")


def _digest(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8", "surrogatepass")).hexdigest()[:24]


class SourceStore:
    """Gzip HTML files of one crawl. Thread-safe; a failed write never breaks the crawl (get() then answers None)."""

    def __init__(self, directory: str, max_bytes: int = 0):
        self.dir = directory
        self.max_bytes = max(0, int(max_bytes or 0))   # 0 = no cut (the engine already cut the body)
        self._keys: Set[str] = set()
        self._lock = threading.Lock()
        self.pages = 0
        self.bytes_stored = 0          # compressed bytes on disk
        self.errors = 0
        os.makedirs(self.dir, exist_ok=True)

    @classmethod
    def for_crawl(cls, config, start_url: str) -> Optional["SourceStore"]:
        """A new store for a crawl (cleaning up old crawls' folders), or None when sources are not kept."""
        if not getattr(config, "store_source", True) or _serverless():
            return None
        root = default_root(getattr(config, "reports_dir", "") or "")
        try:
            prune(root)
            host = _SAFE.sub("-", (re.sub(r"^https?://", "", start_url or "").split("/")[0] or "site").lower())[:60]
            name = f"{host}_{time.strftime('%Y%m%d_%H%M%S')}_{os.urandom(3).hex()}"
            limit = int(float(getattr(config, "max_page_size_kb", 0) or 0) * 1024)
            return cls(os.path.join(root, name), limit)
        except OSError:
            return None

    def _path(self, key: str) -> str:
        return os.path.join(self.dir, key[:2], key + ".html.gz")

    def put(self, url: str, html: str) -> bool:
        """Stores the page's HTML (replacing an earlier copy of the same URL); False when it could not be written."""
        if not url or html is None:
            return False
        data = html.encode("utf-8", "replace")
        if self.max_bytes and len(data) > self.max_bytes:
            data = data[:self.max_bytes]
        key = _digest(url)
        path = self._path(key)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            packed = gzip.compress(data, GZIP_LEVEL)
            tmp = f"{path}.{threading.get_ident()}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(packed)
            os.replace(tmp, path)
        except OSError:
            with self._lock:
                self.errors += 1
            return False
        with self._lock:
            if key not in self._keys:
                self._keys.add(key)
                self.pages += 1
            self.bytes_stored += len(packed)
        return True

    @classmethod
    def open(cls, directory: str) -> Optional["SourceStore"]:
        """A store over a folder written earlier (a saved crawl that copied it); None when it does not exist."""
        if not directory or not os.path.isdir(directory):
            return None
        return cls(directory)

    def has(self, url: str) -> bool:
        if not url:
            return False
        key = _digest(url)
        return key in self._keys or os.path.exists(self._path(key))

    def get(self, url: str) -> Optional[str]:
        """The stored HTML of `url` (as crawled, normalized), or None."""
        if not url:
            return None
        try:
            with open(self._path(_digest(url)), "rb") as fh:
                return gzip.decompress(fh.read()).decode("utf-8", "replace")
        except (OSError, EOFError, ValueError):
            return None

    def delete(self) -> None:
        """Removes this crawl's folder (the store answers None afterwards)."""
        with self._lock:
            self._keys.clear()
        shutil.rmtree(self.dir, ignore_errors=True)

    def summary(self) -> dict:
        return {"pages": self.pages, "bytes": self.bytes_stored, "errors": self.errors, "dir": self.dir}


def prune(root: str, keep: int = KEEP_CRAWLS, max_age_hours: float = MAX_AGE_HOURS) -> int:
    """Removes crawl folders under `root` beyond the `keep` newest and any older than max_age_hours (only
    folders this module made: name "<host>_<date>_<time>_<hex>"). Returns how many were removed."""
    try:
        names = [n for n in os.listdir(root) if re.match(r"^[a-z0-9.-]+_\d{8}_\d{6}_[0-9a-f]{6}$", n)]
    except OSError:
        return 0
    paths = sorted((os.path.join(root, n) for n in names), key=lambda p: os.path.getmtime(p), reverse=True)
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for i, path in enumerate(paths):
        try:
            if i >= keep or os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
