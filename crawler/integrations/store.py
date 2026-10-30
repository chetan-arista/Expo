"""
Server-side store for the integrations' secrets and connection choices, under <reports_dir>/integrations/
(reports_dir = env CRAWLER_REPORTS_DIR, else the system temp folder). Files are written atomically and
made owner-only (mode 600) where the system allows it. Nothing in here is ever returned to the browser:
public_*() views give {"set": bool, "source": ...} for secrets and plain fields for the rest.

  google.json   {"accounts": [{"id", "kind": "oauth"|"service_account", "email", "name", "auto_connect",
                               "scopes", "created_at", "last_used_at", "needs_reconnect",
                               "refresh_token" (oauth) | "sa_key" (service account: client_email, private_key,
                               token_uri)}],
                 "selection": {"gsc": {"account_id", "property", "properties"?},
                               "ga4": {"account_id", "account", "property", "property_name", "stream",
                                       "stream_name", "stream_uri"}},
                 "client": {"client_id", "client_secret"}}      (OAuth client saved from the dialog; env wins)
  psi.json      {"api_key", "auto_connect", "source": "remote"}
  keys.json     {"majestic"|"ahrefs"|"moz": key}                 (Link Metrics keys; env <PROVIDER>_TOKEN wins)
  quota.json    {"gsc_inspect": {property: {"date", "count"}}, "psi": {"date", "count"},
                 "link_units": {provider: {"month": "YYYY-MM", "count"}}}   (link-metrics units this month)
Access tokens are kept in memory only (google_oauth), never written.
"""
import copy
import json
import os
import tempfile
import threading
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .common import now_iso

DIR_NAME = "integrations"
_lock = threading.RLock()
LINK_PROVIDERS = ("majestic", "ahrefs", "moz")


def reports_dir(override: Optional[str] = None) -> str:
    return override or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def integrations_dir(override: Optional[str] = None) -> str:
    return os.path.join(reports_dir(override), DIR_NAME)


def _path(name: str, override: Optional[str] = None) -> str:
    return os.path.join(integrations_dir(override), f"{name}.json")


def load(name: str, override: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        try:
            with open(_path(name, override), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}


def save(name: str, data: Dict[str, Any], override: Optional[str] = None) -> None:
    """Atomic write, owner-only permissions where the OS supports them."""
    with _lock:
        folder = integrations_dir(override)
        os.makedirs(folder, exist_ok=True)
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass
        target = _path(name, override)
        fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------------------------------------
# Google accounts
# ---------------------------------------------------------------------------------------------
_PUBLIC_ACCOUNT_FIELDS = ("id", "kind", "email", "name", "auto_connect", "scopes", "created_at", "last_used_at",
                          "needs_reconnect")


def _google(override: Optional[str] = None) -> Dict[str, Any]:
    data = load("google", override)
    data.setdefault("accounts", [])
    data.setdefault("selection", {})
    return data


def accounts(override: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every stored Google account WITH its secrets (server use only)."""
    return copy.deepcopy(_google(override)["accounts"])


def public_account(acc: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: acc.get(k) for k in _PUBLIC_ACCOUNT_FIELDS}
    out["name"] = out.get("name") or out.get("email") or "Google account"
    out["scopes"] = list(out.get("scopes") or [])
    out["auto_connect"] = bool(out.get("auto_connect"))
    out["needs_reconnect"] = bool(out.get("needs_reconnect"))
    return out


def public_accounts(override: Optional[str] = None) -> List[Dict[str, Any]]:
    return [public_account(a) for a in _google(override)["accounts"]]


def get_account(account_id: str, override: Optional[str] = None) -> Optional[Dict[str, Any]]:
    for a in _google(override)["accounts"]:
        if a.get("id") == account_id:
            return copy.deepcopy(a)
    return None


def upsert_account(entry: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    """Adds the account, or updates the stored one with the same id or (same kind and) same email."""
    with _lock:
        data = _google(override)
        for a in data["accounts"]:
            if a.get("id") == entry.get("id") or (entry.get("email") and a.get("email") == entry.get("email")
                                                   and a.get("kind") == entry.get("kind")):
                keep = {k: a.get(k) for k in ("id", "name", "auto_connect", "created_at") if a.get(k) is not None}
                a.update(entry)
                a.update(keep)
                a["needs_reconnect"] = False
                save("google", data, override)
                return copy.deepcopy(a)
        entry.setdefault("created_at", now_iso())
        entry.setdefault("auto_connect", False)
        data["accounts"].append(entry)
        save("google", data, override)
        return copy.deepcopy(entry)


def update_account(account_id: str, override: Optional[str] = None, **fields: Any) -> Optional[Dict[str, Any]]:
    with _lock:
        data = _google(override)
        for a in data["accounts"]:
            if a.get("id") == account_id:
                a.update(fields)
                save("google", data, override)
                return copy.deepcopy(a)
    return None


def delete_account(account_id: str, override: Optional[str] = None) -> bool:
    with _lock:
        data = _google(override)
        before = len(data["accounts"])
        data["accounts"] = [a for a in data["accounts"] if a.get("id") != account_id]
        for source, sel in list(data["selection"].items()):
            if isinstance(sel, dict) and sel.get("account_id") == account_id:
                data["selection"][source] = {}
        save("google", data, override)
        return len(data["accounts"]) < before


def selection(source: str, override: Optional[str] = None) -> Dict[str, Any]:
    sel = _google(override)["selection"].get(source)
    return dict(sel) if isinstance(sel, dict) else {}


def set_selection(source: str, values: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        data = _google(override)
        data["selection"][source] = dict(values or {})
        save("google", data, override)
        return dict(data["selection"][source])


def client(override: Optional[str] = None) -> Tuple[str, str, str]:
    """(client_id, client_secret, source "env" | "settings" | "") of the Google OAuth client."""
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret, "env"
    saved = _google(override).get("client") or {}
    cid = str(saved.get("client_id") or "").strip()
    secret = str(saved.get("client_secret") or "").strip()
    if cid and secret:
        return cid, secret, "settings"
    return "", "", ""


def set_client(client_id: str, client_secret: str, override: Optional[str] = None) -> None:
    with _lock:
        data = _google(override)
        data["client"] = {"client_id": str(client_id or "").strip(), "client_secret": str(client_secret or "").strip()}
        save("google", data, override)


def clear_client(override: Optional[str] = None) -> None:
    with _lock:
        data = _google(override)
        data.pop("client", None)
        save("google", data, override)


# ---------------------------------------------------------------------------------------------
# PageSpeed Insights key
# ---------------------------------------------------------------------------------------------
def psi_key(override: Optional[str] = None) -> Tuple[str, str]:
    """(key, source "env" | "settings" | "") - env PSI_API_KEY wins over the saved key."""
    env = os.environ.get("PSI_API_KEY", "").strip()
    if env:
        return env, "env"
    saved = str(load("psi", override).get("api_key") or "").strip()
    return (saved, "settings") if saved else ("", "")


def psi_settings(override: Optional[str] = None) -> Dict[str, Any]:
    data = load("psi", override)
    return {"auto_connect": bool(data.get("auto_connect")), "source": data.get("source") or "remote",
            "connected": bool(data.get("connected", True))}


def set_psi(override: Optional[str] = None, **fields: Any) -> None:
    with _lock:
        data = load("psi", override)
        data.update(fields)
        save("psi", data, override)


def clear_psi_key(override: Optional[str] = None) -> None:
    with _lock:
        data = load("psi", override)
        data.pop("api_key", None)
        save("psi", data, override)


# ---------------------------------------------------------------------------------------------
# Link metrics key slots (the Moz / Majestic / Ahrefs connectors keep their keys here; env wins)
# ---------------------------------------------------------------------------------------------
def link_key(provider: str, override: Optional[str] = None) -> str:
    env = os.environ.get(f"{provider.upper()}_TOKEN", "").strip()
    return env or str(load("keys", override).get(provider) or "").strip()


def link_key_source(provider: str, override: Optional[str] = None) -> str:
    """"env" | "settings" | "" for a link-metrics key."""
    if os.environ.get(f"{provider.upper()}_TOKEN", "").strip():
        return "env"
    return "settings" if str(load("keys", override).get(provider) or "").strip() else ""


def _month(today: Optional[str] = None) -> str:
    return (today or date.today().isoformat())[:7]


def link_units_used(provider: str, override: Optional[str] = None, today: Optional[str] = None) -> int:
    """Units a link-metrics provider used this calendar month (as counted by ARACHNIX)."""
    entry = (load("quota", override).get("link_units") or {}).get(provider) or {}
    return int(entry.get("count") or 0) if entry.get("month") == _month(today) else 0


def link_units_add(provider: str, n: int, override: Optional[str] = None, today: Optional[str] = None) -> int:
    """Adds n units to this month's count of a provider; returns the new count."""
    month = _month(today)
    with _lock:
        data = load("quota", override)
        bucket = data.setdefault("link_units", {})
        entry = bucket.get(provider) or {}
        count = int(entry.get("count") or 0) if entry.get("month") == month else 0
        bucket[provider] = {"month": month, "count": count + max(0, int(n))}
        save("quota", data, override)
        return count + max(0, int(n))


def set_link_key(provider: str, key: str, override: Optional[str] = None) -> None:
    if provider not in LINK_PROVIDERS:
        raise ValueError(f"Unknown provider: {provider}")
    with _lock:
        data = load("keys", override)
        if key:
            data[provider] = str(key).strip()
        else:
            data.pop(provider, None)
        save("keys", data, override)


# ---------------------------------------------------------------------------------------------
# Daily quota counters (survive restarts; Google counts per Pacific day, we count per local day)
# ---------------------------------------------------------------------------------------------
def quota_used(kind: str, key: str = "_", override: Optional[str] = None, today: Optional[str] = None) -> int:
    today = today or date.today().isoformat()
    entry = (load("quota", override).get(kind) or {}).get(key) or {}
    return int(entry.get("count") or 0) if entry.get("date") == today else 0


def quota_add(kind: str, key: str = "_", n: int = 1, override: Optional[str] = None,
              today: Optional[str] = None) -> int:
    today = today or date.today().isoformat()
    with _lock:
        data = load("quota", override)
        bucket = data.setdefault(kind, {})
        entry = bucket.get(key) or {}
        count = int(entry.get("count") or 0) if entry.get("date") == today else 0
        bucket[key] = {"date": today, "count": count + int(n)}
        save("quota", data, override)
        return count + int(n)


def quota_fill(kind: str, key: str, limit: int, override: Optional[str] = None,
               today: Optional[str] = None) -> None:
    """Google said the daily quota is used up: mark it so no more calls are made today."""
    today = today or date.today().isoformat()
    with _lock:
        data = load("quota", override)
        used = quota_used(kind, key, override, today)
        data.setdefault(kind, {})[key] = {"date": today, "count": max(int(limit), used)}
        save("quota", data, override)
