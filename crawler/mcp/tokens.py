"""
MCP tokens and the switches of Connections > AI agents.

A token is "slk_<id>_<secret>": the id (8 hex characters) finds the record, the secret is 32 random bytes. It is shown
once, when it is created; <reports_dir>/mcp/tokens.json keeps only a salted SHA-256 hash of it (owner-only file) and
the secret is compared in constant time. Each token has a name ([A-Za-z0-9 ._:-], 1-40 characters: it appears in the
crawl log as the actor "mcp:<name>"), can_write (fixed at creation, default false), an expiry (default 90 days, or
never) and the time it was last used.

Settings in <reports_dir>/mcp/settings.json, every switch off until an admin turns it on:
  enabled                 the MCP endpoint answers (otherwise 403)
  allow_write             the master switch for write tools ("Allow AI agents to start crawls")
  share_private_crawls    agents may read the content of a signed-in crawl
  allow_private_network   agent crawls may reach private networks (never link-local / metadata)
A write tool needs allow_write AND the token's can_write.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

NAME_RE = re.compile(r"^[A-Za-z0-9 ._:-]{1,40}$")
TOKEN_RE = re.compile(r"^slk_([0-9a-f]{8})_([A-Za-z0-9_\-]{20,100})$")
PREFIX = "slk_"
DEFAULT_EXPIRES_DAYS = 90
MAX_EXPIRES_DAYS = 3650
MAX_TOKENS = 50
LAST_USED_WRITE_S = 60.0        # last_used_at is written to disk at most this often per token
SETTINGS = ("enabled", "allow_write", "share_private_crawls", "allow_private_network")
DIR_NAME = "mcp"

_lock = threading.RLock()
_cache: Dict[str, Any] = {"path": "", "mtime": None, "doc": None}
_last_used: Dict[str, float] = {}       # token id -> time last written


class TokenError(ValueError):
    """A plain message for the admin (bad name, too many tokens, unknown setting)."""


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------
def mcp_dir(reports_dir: Optional[str] = None) -> str:
    return os.path.join(reports_dir or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir(), DIR_NAME)


def tokens_path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(mcp_dir(reports_dir), "tokens.json")


def settings_path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(mcp_dir(reports_dir), "settings.json")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse(value: Any) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if value else None
    except ValueError:
        return None


def _write_json(path: str, data: Any) -> None:
    """Atomic write, readable by the server's user only where the OS allows it."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.chmod(os.path.dirname(path), 0o700)
    except OSError:
        pass
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------------------------
def load_settings(reports_dir: Optional[str] = None) -> Dict[str, bool]:
    doc = _read_json(settings_path(reports_dir))
    doc = doc if isinstance(doc, dict) else {}
    return {k: doc.get(k) is True for k in SETTINGS}


def save_settings(changes: Dict[str, Any], reports_dir: Optional[str] = None) -> Dict[str, bool]:
    """Applies {switch: bool} and returns every switch. TokenError for an unknown switch or a value that is not
    true / false."""
    if not isinstance(changes, dict):
        raise TokenError("Send the switches as an object.")
    unknown = [k for k in changes if k not in SETTINGS]
    if unknown:
        raise TokenError(f"Unknown setting: {', '.join(map(str, unknown[:5]))}.")
    bad = [k for k, v in changes.items() if not isinstance(v, bool)]
    if bad:
        raise TokenError(f"{', '.join(bad)} must be true or false.")
    with _lock:
        current = load_settings(reports_dir)
        current.update(changes)
        _write_json(settings_path(reports_dir), dict(current, updated_at=_iso(_now())))
        return current


# ---------------------------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------------------------
def clean_name(name: Any) -> str:
    text = str(name or "").strip()
    if not NAME_RE.match(text):
        raise TokenError("Name the token with 1-40 letters, digits, spaces or . _ : - (e.g. \"Claude Desktop - Asha\").")
    return text


def _hash(salt: str, secret: str) -> str:
    return hashlib.sha256((salt + ":" + secret).encode("utf-8")).hexdigest()


def _load(reports_dir: Optional[str]) -> Dict[str, Any]:
    """The tokens document {"tokens": {id: record}}, cached while the file is unchanged."""
    path = tokens_path(reports_dir)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    with _lock:
        if _cache["path"] == path and _cache["mtime"] == mtime and _cache["doc"] is not None:
            return _cache["doc"]
        doc = _read_json(path) if mtime is not None else None
        if not isinstance(doc, dict) or not isinstance(doc.get("tokens"), dict):
            doc = {"tokens": {}}
        _cache.update(path=path, mtime=mtime, doc=doc)
        return doc


def _save(doc: Dict[str, Any], reports_dir: Optional[str]) -> None:
    path = tokens_path(reports_dir)
    _write_json(path, doc)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    _cache.update(path=path, mtime=mtime, doc=doc)


def _public(tid: str, rec: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    exp = _parse(rec.get("expires_at"))
    return {"id": tid, "name": rec.get("name") or "", "created_at": rec.get("created_at"),
            "last_used_at": rec.get("last_used_at"), "can_write": bool(rec.get("can_write")),
            "expires_at": rec.get("expires_at"), "expired": bool(exp and exp <= (now or _now()))}


def create_token(name: Any, can_write: bool = False, expires_days: Optional[int] = DEFAULT_EXPIRES_DAYS,
                 reports_dir: Optional[str] = None, expires_seconds: Optional[int] = None) -> Dict[str, Any]:
    """A new token: {id, name, token, can_write, created_at, expires_at}. "token" is the only copy of the secret
    (show it once). expires_days 0 or None = never; expires_seconds (internal tests, e.g. the connection test) wins
    over expires_days."""
    label = clean_name(name)
    if not isinstance(can_write, bool):
        raise TokenError("can_write must be true or false.")
    now = _now()
    if expires_seconds is not None:
        if not isinstance(expires_seconds, int) or isinstance(expires_seconds, bool) or not 1 <= expires_seconds <= 86400:
            raise TokenError("expires_seconds must be 1-86400.")
        expires = now + timedelta(seconds=expires_seconds)
    else:
        if expires_days is not None and (not isinstance(expires_days, int) or isinstance(expires_days, bool)
                                         or not 0 <= expires_days <= MAX_EXPIRES_DAYS):
            raise TokenError(f"expires_days must be a whole number from 0 (never) to {MAX_EXPIRES_DAYS}.")
        expires = now + timedelta(days=expires_days) if expires_days else None
    with _lock:
        doc = _load(reports_dir)
        tokens = doc["tokens"]
        if len(tokens) >= MAX_TOKENS:
            raise TokenError(f"There are already {MAX_TOKENS} tokens. Revoke the ones no longer used first.")
        if any((r.get("name") or "").lower() == label.lower() for r in tokens.values()):
            raise TokenError(f"A token named {label!r} already exists. Choose another name.")
        tid = secrets.token_hex(4)
        while tid in tokens:
            tid = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        salt = secrets.token_hex(16)
        rec = {"name": label, "salt": salt, "hash": _hash(salt, secret), "can_write": can_write,
               "created_at": _iso(now), "expires_at": _iso(expires), "last_used_at": None}
        new_doc = {"tokens": dict(tokens, **{tid: rec})}
        _save(new_doc, reports_dir)
    out = _public(tid, rec, now)
    out.pop("expired", None)
    out["token"] = f"{PREFIX}{tid}_{secret}"
    return out


def list_tokens(reports_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every token without its secret or hash, oldest first."""
    now = _now()
    doc = _load(reports_dir)
    items = [_public(tid, rec, now) for tid, rec in doc["tokens"].items()]
    return sorted(items, key=lambda t: (t.get("created_at") or "", t["id"]))


def revoke_token(token_id: str, reports_dir: Optional[str] = None) -> bool:
    """Deletes a token for good; True when it existed. A bridge or client using it gets 401 at its next call."""
    with _lock:
        doc = _load(reports_dir)
        if token_id not in doc["tokens"]:
            return False
        tokens = dict(doc["tokens"])
        tokens.pop(token_id, None)
        _save({"tokens": tokens}, reports_dir)
        _last_used.pop(token_id, None)
        return True


def verify_token(token: str, reports_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """{id, name, can_write, expires_at, expired} for a known token (also an expired one, so the server can say
    "expired"), None otherwise. The secret is compared in constant time; last_used_at is updated (written at most
    once a minute)."""
    m = TOKEN_RE.match(str(token or "").strip())
    doc = _load(reports_dir)
    rec = doc["tokens"].get(m.group(1)) if m else None
    salt = rec.get("salt", "") if rec else "0" * 32
    expected = rec.get("hash", "") if rec else "0" * 64
    given = _hash(salt, m.group(2) if m else "x")
    ok = hmac.compare_digest(given, expected) and rec is not None
    if not ok:
        return None
    tid = m.group(1)
    now = _now()
    out = _public(tid, rec, now)
    if not out["expired"]:
        _touch(tid, now, reports_dir)
    return {k: out[k] for k in ("id", "name", "can_write", "expires_at", "expired")}


def _touch(tid: str, now: datetime, reports_dir: Optional[str]) -> None:
    t = time.time()
    if t - _last_used.get(tid, 0.0) < LAST_USED_WRITE_S:
        return
    with _lock:
        _last_used[tid] = t
        doc = _load(reports_dir)
        if tid not in doc["tokens"]:
            return
        tokens = {k: dict(v) for k, v in doc["tokens"].items()}
        tokens[tid]["last_used_at"] = _iso(now)
        try:
            _save({"tokens": tokens}, reports_dir)
        except OSError:
            pass


def summary(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """GET /api/mcp's stored part: the switches and the tokens (never a secret)."""
    return dict(load_settings(reports_dir), tokens=list_tokens(reports_dir))
