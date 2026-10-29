"""
The connector registry (PLAN4_build.md 10.2): every connector the Connections page lists.

  register(c) -> c ; all_connectors() -> [Connector] ; get(cid) -> Connector | None ; by_kind(kind) -> [Connector]
  instances(base_id) -> [Connector] ; create_instance(base_id, name) -> Connector   (multi connectors)

The built-in connectors register themselves when their module is imported; the registry imports them on first
use (BUILTIN_MODULES), each on its own, so one module that fails to import never hides the others. A "multi"
connector (webhook, custom_mcp) is a template: its instances are "<id>:<slug>", one per saved instance.
"""
import importlib
import re
import threading
from collections import OrderedDict
from typing import List, Optional

from .base import Connector, ConnectorError

# Modules that register built-in connectors (later batches append theirs)
BUILTIN_MODULES = ("adapters", "webhooks", "languagetool", "linkmetrics", "indexnow", "green_web", "custom_mcp",
                   "jira", "github_issues", "google_sheets")
_CONNECTORS: "OrderedDict[str, Connector]" = OrderedDict()
_lock = threading.RLock()
_loaded = {"done": False, "errors": {}}


def register(c: Connector) -> Connector:
    """Adds (or replaces) a connector by its id."""
    with _lock:
        _CONNECTORS[c.id] = c
    return c


def _ensure() -> None:
    if _loaded["done"]:
        return
    with _lock:                         # other threads wait here until every module is in
        if _loaded["done"] or _loaded.get("loading"):
            return                      # done, or this thread is importing them (a module asked the registry)
        _loaded["loading"] = True
        try:
            for name in BUILTIN_MODULES:
                try:
                    importlib.import_module(f"{__package__}.{name}")
                except Exception as e:      # one broken module must not hide the other connectors
                    _loaded["errors"][name] = f"{type(e).__name__}"
        finally:
            _loaded["done"] = True
            _loaded["loading"] = False


def load_errors() -> dict:
    _ensure()
    return dict(_loaded["errors"])


def instances(base_id: str) -> List[Connector]:
    """The saved instances of a multi connector, in name order."""
    from . import store
    _ensure()
    tmpl = _CONNECTORS.get(base_id)
    if tmpl is None or not tmpl.multi:
        return []
    return [tmpl.instance(cid.split(":", 1)[1]) for cid in store.instance_ids(base_id)]


def all_connectors() -> List[Connector]:
    """Every connector: each template followed by its instances."""
    _ensure()
    out: List[Connector] = []
    with _lock:
        items = list(_CONNECTORS.values())
    for c in items:
        out.append(c)
        if c.multi:
            out.extend(instances(c.id))
    return out


def get(cid: str) -> Optional[Connector]:
    """The connector with this id ("slack", "webhook:team-alerts"), or None."""
    from . import store
    _ensure()
    cid = str(cid or "").strip()
    if ":" in cid:
        base, _, slug = cid.partition(":")
        tmpl = _CONNECTORS.get(base)
        if tmpl is None or not tmpl.multi or not store.valid_id(cid) or not store.exists(cid):
            return None
        return tmpl.instance(slug)
    return _CONNECTORS.get(cid)


def by_kind(kind: str) -> List[Connector]:
    return [c for c in all_connectors() if c.kind == kind]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return s[:40].strip("-")


def create_instance(base_id: str, name: str) -> Connector:
    """A new (not yet connected) instance of a multi connector, named `name` (slug made unique)."""
    from . import store
    _ensure()
    tmpl = _CONNECTORS.get(base_id)
    if tmpl is None or not tmpl.multi:
        raise ConnectorError("That connector cannot have several instances.", "invalid")
    name = " ".join(str(name or "").split())[:60]
    if not name or not re.match(r"^[A-Za-z0-9 ._:()-]{1,60}$", name):
        raise ConnectorError("Give it a name of 1-60 letters, digits, spaces, dots or dashes.", "invalid",
                             errors={"name": "Use 1-60 letters, digits, spaces, dots or dashes."})
    base_slug = slugify(name) or "item"
    if len(store.instance_ids(base_id)) >= tmpl.max_instances:
        raise ConnectorError(f"At most {tmpl.max_instances} of these.", "invalid")
    slug, n = base_slug, 2
    while store.exists(f"{base_id}:{slug}"):
        slug = f"{base_slug[:36]}-{n}"
        n += 1
    return tmpl.instance(slug, name)
