"""
Where connectors keep their settings (PLAN4_build.md 10.2 / 10.7):
  <reports_dir>/connectors/<id>.json   {"id", "config": {plain fields}, "meta": {"connected", "connected_at",
                                        "tested_live", "tested_at", "last_test", "last_delivery", "updated_at"}}
                                       (":" of an instance id is written "__": webhook:team -> webhook__team.json)
  <reports_dir>/secrets.json           section "connectors": {id: {field: secret}} through crawler.settings_store
                                       (owner-only; saving a settings page keeps this section)
  <reports_dir>/connectors/alerts.json alert rules and their last run (alerts.py)
reports_dir = env CRAWLER_REPORTS_DIR, else the system temp folder (as the other server settings). Files are written
atomically and owner-only where the system allows it. Nothing here is ever returned to the browser as is.
"""
import copy
import json
import os
import re
import tempfile
import threading
from typing import Any, Dict, List, Optional

DIR_NAME = "connectors"
SECRET_SECTION = "connectors"
_ID = re.compile(r"^[a-z][a-z0-9_]{1,40}(?::[a-z0-9][a-z0-9-]{0,40})?$")
_lock = threading.RLock()


def reports_dir(override: Optional[str] = None) -> str:
    return override or os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def folder(override: Optional[str] = None) -> str:
    return os.path.join(reports_dir(override), DIR_NAME)


def valid_id(cid: str) -> bool:
    return bool(_ID.match(str(cid or "")))


def _path(cid: str, override: Optional[str] = None) -> str:
    if not valid_id(cid):
        raise ValueError(f"Not a connector id: {cid!r}")
    return os.path.join(folder(override), cid.replace(":", "__") + ".json")


def _read(path: str) -> Optional[Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_json(path: str, data: Any) -> None:
    """Atomic, owner-only write."""
    with _lock:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.chmod(os.path.dirname(path), 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix=".c.", suffix=".tmp", dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


def read_json(path: str) -> Optional[Any]:
    with _lock:
        return _read(path)


# ---------------------------------------------------------------------------------------------
# Config + meta
# ---------------------------------------------------------------------------------------------
def load(cid: str, override: Optional[str] = None) -> Dict[str, Any]:
    with _lock:
        data = _read(_path(cid, override))
    data = data if isinstance(data, dict) else {}
    return {"config": dict(data.get("config") or {}), "meta": dict(data.get("meta") or {})}


def exists(cid: str, override: Optional[str] = None) -> bool:
    try:
        return os.path.isfile(_path(cid, override))
    except ValueError:
        return False


def save_config(cid: str, config: Dict[str, Any], override: Optional[str] = None) -> None:
    with _lock:
        data = load(cid, override)
        data["config"] = copy.deepcopy(config)
        write_json(_path(cid, override), {"id": cid, **data})


def update_meta(cid: str, values: Dict[str, Any], override: Optional[str] = None) -> Dict[str, Any]:
    """Merges values into the meta (a None value removes the key). Returns the new meta."""
    with _lock:
        data = load(cid, override)
        meta = data["meta"]
        for k, v in values.items():
            if v is None:
                meta.pop(k, None)
            else:
                meta[k] = copy.deepcopy(v)
        write_json(_path(cid, override), {"id": cid, **data})
        return dict(meta)


def delete(cid: str, override: Optional[str] = None) -> None:
    """Removes the config file and the secrets of one connector."""
    with _lock:
        try:
            os.remove(_path(cid, override))
        except OSError:
            pass
        set_secrets(cid, None, override)


def instance_ids(prefix: str, override: Optional[str] = None) -> List[str]:
    """Saved instance ids "<prefix>:<slug>" in name order."""
    try:
        names = os.listdir(folder(override))
    except OSError:
        return []
    out = []
    head = prefix + "__"
    for n in sorted(names):
        if n.startswith(head) and n.endswith(".json"):
            cid = n[:-5].replace("__", ":", 1)
            if valid_id(cid):
                out.append(cid)
    return out


# ---------------------------------------------------------------------------------------------
# Secrets (secrets.json, section "connectors")
# ---------------------------------------------------------------------------------------------
def _settings():
    from .. import settings_store
    return settings_store


def secrets(cid: str, override: Optional[str] = None) -> Dict[str, str]:
    """The secrets of one connector (server use only)."""
    held = _settings().secret_section(SECRET_SECTION, override).get(cid)
    return {k: str(v) for k, v in (held or {}).items() if v} if isinstance(held, dict) else {}


def set_secrets(cid: str, changes: Optional[Dict[str, Optional[str]]], override: Optional[str] = None) -> None:
    """Applies {field: value | None = remove}; changes=None removes every secret of the connector."""
    def change(section: Dict[str, Any]) -> Dict[str, Any]:
        if changes is None:
            section.pop(cid, None)
            return section
        mine = dict(section.get(cid) or {})
        for k, v in changes.items():
            if v:
                mine[k] = str(v)
            else:
                mine.pop(k, None)
        if mine:
            section[cid] = mine
        else:
            section.pop(cid, None)
        return section
    _settings().update_secret_section(SECRET_SECTION, change, override)
