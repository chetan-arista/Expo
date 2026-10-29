"""
Fetches the pinned axe-core release into crawler/vendor/axe/ (run once after a checkout, and after changing the
pins in axe_core.py):

    python -m crawler.vendor.fetch_axe [--force] [--tarball PATH]

  1. downloads the npm tarball of AXE_VERSION (or reads PATH, e.g. on a server without internet access) and
     checks it against AXE_INTEGRITY (npm's sha512); nothing is written when it does not match
  2. writes axe.min.js exactly as shipped (checked against axe-core's own published hash), LICENSE (MPL-2.0) and
     LICENSE-3RD-PARTY.txt (the licences of code bundled inside axe.min.js)
  3. writes rules.json: data about every rule read from that same file with axe.getRules() (Node.js, else Chrome)
  4. writes README.md naming the version, the source, the hashes and where the source code form is

Idempotent: when the files already match the pins nothing is downloaded or written (exit code 0).
"""
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from . import axe_core
from .axe_core import (AXE_DIR, AXE_FILE, AXE_INTEGRITY, AXE_MIN_SHA256, AXE_TARBALL, AXE_VERSION, RULES_FILE,
                       file_sri, wcag_level)

MAX_TARBALL_BYTES = 20 * 1024 * 1024
FILES = {                                   # tarball member -> file in crawler/vendor/axe/
    "package/axe.min.js": "axe.min.js",
    "package/LICENSE": "LICENSE",
    "package/LICENSE-3RD-PARTY.txt": "LICENSE-3RD-PARTY.txt",
}
README_FILE = os.path.join(AXE_DIR, "README.md")
SOURCE_URL = f"https://github.com/dequelabs/axe-core/tree/v{AXE_VERSION}"

# Read in Node.js: the rule list as axe-core describes it (our own script; the data comes from axe.min.js)
_NODE_JS = r"""
const axe = require(process.argv[2]);
const meta = {};
for (const r of axe.getRules()) meta[r.ruleId] = r;
const rules = axe._audit.rules.map(r => ({
  id: r.id, help: (meta[r.id] || {}).help || '', description: (meta[r.id] || {}).description || '',
  help_url: (meta[r.id] || {}).helpUrl || '', tags: r.tags || [], impact: r.impact || null,
  enabled: r.enabled !== false}));
process.stdout.write(JSON.stringify({version: axe.version, rules: rules}));
"""
# The same in a Chrome page, after axe.min.js was injected (window.axe)
_CHROME_JS = r"""
const axe = window.axe;
const meta = {};
for (const r of axe.getRules()) meta[r.ruleId] = r;
return {version: axe.version, rules: axe._audit.rules.map(r => ({
  id: r.id, help: (meta[r.id] || {}).help || '', description: (meta[r.id] || {}).description || '',
  help_url: (meta[r.id] || {}).helpUrl || '', tags: r.tags || [], impact: r.impact || null,
  enabled: r.enabled !== false}))};
"""


class FetchError(Exception):
    """The release could not be fetched or did not match the pins (the message says why)."""


def _sri(data: bytes, algo: str) -> str:
    return f"{algo}-" + base64.b64encode(hashlib.new(algo, data).digest()).decode("ascii")


def up_to_date() -> bool:
    """Every file is there and axe.min.js and rules.json belong to AXE_VERSION."""
    paths = [os.path.join(AXE_DIR, name) for name in FILES.values()] + [RULES_FILE, README_FILE]
    if not all(os.path.isfile(p) for p in paths):
        return False
    try:
        if file_sri(AXE_FILE) != AXE_MIN_SHA256:
            return False
        with open(RULES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("version") == AXE_VERSION and len(data.get("rules") or []) > 50
    except (OSError, ValueError):
        return False


def download(url: str = AXE_TARBALL) -> bytes:
    import requests
    try:
        with requests.get(url, stream=True, timeout=(10, 60)) as resp:
            if resp.status_code != 200:
                raise FetchError(f"The npm registry answered HTTP {resp.status_code}.")
            buf = io.BytesIO()
            for chunk in resp.iter_content(1 << 16):
                buf.write(chunk)
                if buf.tell() > MAX_TARBALL_BYTES:
                    raise FetchError("The download is larger than expected; stopped.")
            return buf.getvalue()
    except requests.RequestException as e:
        raise FetchError(f"The npm registry could not be reached ({type(e).__name__}).")


def unpack(tarball: bytes) -> Dict[str, bytes]:
    """The files of FILES plus package.json, read in memory (nothing from the archive is written as-is)."""
    if _sri(tarball, "sha512") != AXE_INTEGRITY:
        raise FetchError(f"The tarball does not match the pinned integrity of axe-core {AXE_VERSION}.")
    wanted = set(FILES) | {"package/package.json"}
    out: Dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r:gz") as tar:
        for member in tar.getmembers():
            if member.name in wanted and member.isfile():
                f = tar.extractfile(member)
                if f is not None:
                    out[member.name] = f.read()
    missing = sorted(wanted - set(out))
    if missing:
        raise FetchError("The tarball lacks " + ", ".join(missing) + ".")
    pkg = json.loads(out["package/package.json"].decode("utf-8"))
    if pkg.get("version") != AXE_VERSION or pkg.get("license") != "MPL-2.0":
        raise FetchError(f"Unexpected package: {pkg.get('name')} {pkg.get('version')} ({pkg.get('license')}).")
    if _sri(out["package/axe.min.js"], "sha256") != AXE_MIN_SHA256:
        raise FetchError("axe.min.js does not match the hash axe-core publishes for this version.")
    return out


def _write(path: str, data: bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def rules_with_node(axe_path: str) -> Optional[Dict[str, Any]]:
    node = shutil.which("node")
    if not node:
        return None
    work = tempfile.mkdtemp(prefix="slickup-axe-")
    try:
        script = os.path.join(work, "rules.js")
        with open(script, "w", encoding="utf-8") as f:
            f.write(_NODE_JS)
        res = subprocess.run([node, script, os.path.abspath(axe_path)], cwd=work, capture_output=True,
                             timeout=60)
        if res.returncode != 0:
            return None
        return json.loads(res.stdout.decode("utf-8"))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def rules_with_chrome(source_js: str) -> Optional[Dict[str, Any]]:
    try:
        from ..browser import close_chrome, new_chrome
    except Exception:
        return None
    driver = new_chrome("", 30)
    if driver is None:
        return None
    try:
        driver.get("about:blank")
        driver.execute_script("var define, module, exports;\n" + source_js)
        return driver.execute_script(_CHROME_JS)
    except Exception:
        return None
    finally:
        close_chrome(driver)


def build_rules(data: Dict[str, Any]) -> Tuple[bytes, int]:
    """rules.json (one rule per line, sorted by id) and how many rules ARACHNIX runs."""
    if not isinstance(data, dict) or data.get("version") != AXE_VERSION:
        raise FetchError("The rule list read from axe.min.js has an unexpected version.")
    rules: List[Dict[str, Any]] = []
    for r in data.get("rules") or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        tags = [str(t) for t in r.get("tags") or []]
        url = str(r.get("help_url") or "").split("?", 1)[0]
        item = {"id": str(r["id"]), "help": str(r.get("help") or ""), "description": str(r.get("description") or ""),
                "help_url": url, "tags": tags, "impact": r.get("impact"), "enabled": bool(r.get("enabled", True)),
                "level": wcag_level(tags)}
        item["run"] = axe_core.runs({"id": item["id"], "deprecated": "deprecated" in tags,
                                     "experimental": "experimental" in tags})
        rules.append(item)
    if len(rules) < 50:
        raise FetchError("The rule list read from axe.min.js is too short.")
    rules.sort(key=lambda x: x["id"])
    head = {"version": AXE_VERSION, "source": AXE_TARBALL, "licence": "MPL-2.0",
            "note": "Rule data read from axe.min.js with axe.getRules() by crawler/vendor/fetch_axe.py"}
    text = json.dumps(head)[:-1] + ', "rules": [\n' + ",\n".join(json.dumps(r, ensure_ascii=False) for r in rules) \
        + "\n]}\n"
    return text.encode("utf-8"), sum(1 for r in rules if r["run"])


def readme(rule_count: int, run_count: int) -> bytes:
    lines = [
        "# axe-core (vendored, unmodified)",
        "",
        f"- Package: axe-core {AXE_VERSION} by Deque Systems, Inc. (https://github.com/dequelabs/axe-core)",
        "- Licence: Mozilla Public License 2.0 (MPL-2.0), full text in `LICENSE`. Code bundled inside",
        "  `axe.min.js` keeps its own licences, listed in `LICENSE-3RD-PARTY.txt` (MIT and others).",
        f"- Source: {AXE_TARBALL}",
        f"  (npm integrity `{AXE_INTEGRITY}`); `axe.min.js` is the file as shipped (`{AXE_MIN_SHA256}`).",
        f"- Source code form (MPL-2.0 section 3.2): {SOURCE_URL} and `axe.js` in the same npm package.",
        f"- `rules.json`: data read from `axe.min.js` with `axe.getRules()` ({rule_count} rules: ids, tags, impact,",
        "  help text and help link), covered by the same licence. `level` and `run` are added by ARACHNIX:",
        f"  ARACHNIX runs {run_count} of them (deprecated and experimental rules and `frame-tested` are left out).",
        "- Used by: `crawler/render_capture.py` injects `axe.min.js` into pages rendered in Chrome; the",
        "  Accessibility tab (`crawler/seo/tabs/accessibility.py`) names its rule filters from `rules.json`.",
        "- Update: change `AXE_VERSION`, `AXE_INTEGRITY` and `AXE_MIN_SHA256` in `crawler/vendor/axe_core.py`,",
        "  then run `python -m crawler.vendor.fetch_axe --force`.",
        f"- Fetched: {time.strftime('%Y-%m-%d')} by `crawler/vendor/fetch_axe.py`.",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def fetch(force: bool = False, tarball_path: str = "", log=print) -> bool:
    """Brings crawler/vendor/axe/ to the pinned version. True when files were written, False when they were
    already up to date. Raises FetchError (nothing half-written: each file is replaced atomically, the rules
    are read before any file is replaced)."""
    if not force and up_to_date():
        log(f"axe-core {AXE_VERSION} is already in place ({AXE_DIR}).")
        return False
    if tarball_path:
        with open(tarball_path, "rb") as f:
            tarball = f.read(MAX_TARBALL_BYTES + 1)
    else:
        log(f"Downloading axe-core {AXE_VERSION} from the npm registry...")
        tarball = download()
    files = unpack(tarball)
    work = tempfile.mkdtemp(prefix="slickup-axe-")
    try:
        tmp_axe = os.path.join(work, "axe.min.js")
        with open(tmp_axe, "wb") as f:
            f.write(files["package/axe.min.js"])
        data = rules_with_node(tmp_axe)
        if data is None:
            log("Node.js is not available; reading the rule list in Chrome...")
            data = rules_with_chrome(files["package/axe.min.js"].decode("utf-8"))
        if data is None:
            raise FetchError("The rule list could not be read (needs Node.js or Chrome).")
        rules_json, run_count = build_rules(data)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(AXE_DIR, exist_ok=True)
    for member, name in FILES.items():
        _write(os.path.join(AXE_DIR, name), files[member])
    _write(RULES_FILE, rules_json)
    _write(README_FILE, readme(len(json.loads(rules_json.decode("utf-8"))["rules"]), run_count))
    axe_core.reset_cache()
    log(f"axe-core {AXE_VERSION} written to {AXE_DIR} ({run_count} rules run by ARACHNIX).")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch the pinned axe-core release into crawler/vendor/axe/.")
    ap.add_argument("--force", action="store_true", help="fetch and write again even when up to date")
    ap.add_argument("--tarball", default="", help="use this downloaded npm tarball instead of downloading")
    args = ap.parse_args(argv)
    try:
        fetch(force=args.force, tarball_path=args.tarball)
    except (FetchError, OSError) as e:
        print(f"axe-core was not fetched: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
