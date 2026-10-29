"""
The connector contract (PLAN4_build.md 10.2): Field, Status, TestResult, ConnectorError, Masker and the Connector
base class every connector (Slack, Teams, LanguageTool, the Google adapters...) builds on.

A connector keeps its plain settings in <reports_dir>/connectors/<id>.json and its secrets (webhook URLs, API keys,
signing secrets) in the "connectors" section of <reports_dir>/secrets.json (store.py); public() and status() never
carry a secret value: a secret field is {"set": bool}. Every message a connector shows passes its Masker, which
hides each secret value (also percent-encoded) and the path and query of each secret URL, so an error that echoes
a webhook address shows only "https://hooks.slack.com/***".

Status states: connected | not_connected | error (the last test or delivery failed) | needs_reconnect | off
(connected, but switched to send nothing). A connected connector whose Test never got a real 2xx answer from the
service reads "Connected - not tested with the live service yet".
"""
import copy
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, quote, quote_plus, urlsplit

NOT_TESTED = "Connected - not tested with the live service yet"
KINDS = ("text", "url", "password", "select", "bool", "number", "list", "textarea", "oauth")
MAX_TEXT = 500
MAX_SECRET = 4000
MAX_TEXTAREA = 20_000
MAX_LIST = 100
KEEP_VALUES = (None, "", "***")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Field:
    key: str
    label: str
    kind: str = "text"        # text | url | password | select | bool | number | list | textarea | oauth
    required: bool = False
    secret: bool = False      # stored in <reports_dir>/secrets.json ("connectors" section), shown as {"set": bool}
    options: List[str] = field(default_factory=list)
    default: Any = None
    help: str = ""            # one plain sentence
    option_labels: Dict[str, str] = field(default_factory=dict)   # option id -> plain label (select / list)
    min: Optional[float] = None                                   # number range
    max: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        out = asdict(self)
        if self.secret:
            out["default"] = None
        return out


@dataclass
class Status:
    state: str                # "connected" | "not_connected" | "error" | "needs_reconnect" | "off"
    detail: str = ""          # plain sentence, never a secret (masked: secret URL paths / queries and values)
    secrets: Dict[str, bool] = field(default_factory=dict)
    config: Dict[str, Any] = field(default_factory=dict)      # public config only
    last_test: Optional[Dict[str, Any]] = None              # {"ok", "detail", "at", "ms"}
    tested_live: bool = False                               # False -> "Connected - not tested with the live service yet"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TestResult:
    ok: bool
    detail: str
    ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ConnectorError(Exception):
    """A plain message (shown with HTTP 400): built from an error kind and a host only, never a URL path, a query
    or an exception text. `errors` = {field: message} for a form; `kind` = dns | ssl | timeout | refused | reset |
    too_many_redirects | too_large | refused_address | redirect | http | invalid | other."""

    def __init__(self, message: str, kind: str = "invalid", host: str = "", errors: Optional[Dict[str, str]] = None,
                 status: int = 0):
        super().__init__(message)
        self.kind = kind
        self.host = host
        self.errors = dict(errors or {})
        self.status = status


# ---------------------------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------------------------
_URL_IN_TEXT = re.compile(r"(https?://[^\s/?#\"'<>]+)([^\s\"'<>]*)", re.I)


def _tokenish(seg: str) -> bool:
    """A path segment that can be a credential (has digits or mixed case, or is long), not a plain word such as
    "services" or "webhooks" (masking those would garble ordinary messages; the whole URL is masked anyway)."""
    if len(seg) < 6:
        return False
    return any(ch.isdigit() for ch in seg) or len(seg) >= 16 or (seg != seg.lower() and seg != seg.upper())


class Masker:
    """Hides secret values (as given, percent-encoded and form-encoded) and, for each secret URL, everything after
    its host (also when a service echoes the URL in another form). Callable: masker(text) -> text."""

    def __init__(self, values: Iterable[str] = (), urls: Iterable[str] = ()):
        self._values: List[str] = []
        self._hosts: List[str] = []
        for u in urls:
            self._add_url(str(u or ""))
        for v in values:
            self._add(str(v or ""))
        self._values.sort(key=len, reverse=True)

    def _add(self, value: str, min_len: int = 4) -> None:
        value = value.strip()
        if len(value) < min_len:
            return
        for form in (value, quote(value, safe=""), quote_plus(value), quote(value, safe="/:")):
            if form and form not in self._values:
                self._values.append(form)

    def _add_url(self, url: str) -> None:
        url = url.strip()
        if not url:
            return
        try:
            parts = urlsplit(url)
        except ValueError:
            self._add(url)
            return
        host = (parts.hostname or "").lower()
        if host and host not in self._hosts:
            self._hosts.append(host)
        rest = url[url.find(parts.netloc) + len(parts.netloc):] if parts.netloc else url
        if len(rest) > 1:
            self._add(url)
            self._add(rest, 2)
        for seg in parts.path.split("/"):
            if _tokenish(seg):
                self._add(seg, 6)
        for _k, v in parse_qsl(parts.query, keep_blank_values=False):
            self._add(v, 4)
        if parts.password:
            self._add(parts.password, 1)

    def __call__(self, text: Any) -> str:
        out = str(text if text is not None else "")
        if not out:
            return out
        for v in self._values:
            if v in out:
                out = out.replace(v, "***")
        if self._hosts:
            def sub(m: "re.Match") -> str:
                base, rest = m.group(1), m.group(2)
                host = urlsplit(base).hostname or ""
                if host.lower() in self._hosts and rest and rest not in ("/", "/***"):
                    return base + "/***"
                return m.group(0)
            out = _URL_IN_TEXT.sub(sub, out)
        return out


# ---------------------------------------------------------------------------------------------
# Field values
# ---------------------------------------------------------------------------------------------
def keep_secret(given: Any) -> bool:
    """The value sent for a secret field means "leave it as it is"."""
    return given in KEEP_VALUES or (isinstance(given, dict) and not given.get("clear"))


def clean_value(f: Field, value: Any) -> Tuple[Any, str]:
    """(clean value, error) of one non-secret field value by its kind."""
    if value is None:
        value = copy.deepcopy(f.default)
    if f.kind == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on"), ""
        return bool(value), ""
    if f.kind == "number":
        if value in (None, ""):
            return (None, f"{f.label} is required.") if f.required else (f.default, "")
        try:
            n = float(value)
        except (TypeError, ValueError):
            return f.default, f"{f.label} must be a number."
        if n != n or (f.min is not None and n < f.min) or (f.max is not None and n > f.max):
            if f.min is not None and f.max is not None:
                return f.default, f"{f.label} must be between {f.min:,g} and {f.max:,g}."
            if f.min is not None:
                return f.default, f"{f.label} must be at least {f.min:,g}."
            return f.default, f"{f.label} must be at most {f.max:,g}."
        return (int(n) if float(n).is_integer() else n), ""
    if f.kind == "list":
        items = value.splitlines() if isinstance(value, str) else ([] if value is None else value)
        if not isinstance(items, (list, tuple)):
            return list(f.default or []), f"{f.label} must be a list."
        out: List[str] = []
        for v in items:
            s = str(v if v is not None else "").strip()
            if s and s not in out:
                out.append(s[:MAX_TEXT])
        if len(out) > MAX_LIST:
            return out[:MAX_LIST], f"{f.label}: at most {MAX_LIST} entries."
        if f.options:
            bad = [v for v in out if v not in f.options]
            if bad:
                return [v for v in out if v in f.options], f"{f.label}: unknown choice {bad[0][:40]!r}."
        if f.required and not out:
            return out, f"{f.label} is required."
        return out, ""
    text = "" if value is None else str(value)
    limit = MAX_TEXTAREA if f.kind == "textarea" else MAX_TEXT if f.kind not in ("url",) else 2000
    if f.kind != "textarea":
        text = text.strip()
        if any(ch in text for ch in "\r\n"):
            return text.splitlines()[0][:limit], f"{f.label}: line breaks are not allowed."
    if len(text) > limit:
        return text[:limit], f"{f.label} is too long (at most {limit:,} characters)."
    if f.kind == "select" and f.options and text not in f.options:
        return f.default, f"{f.label} must be one of: " + ", ".join(f.option_labels.get(o, o) for o in f.options) + "."
    if f.kind == "url" and text and not re.match(r"^https?://[^\s/?#]+", text, re.I):
        return text, f"{f.label} must be a web address starting with https://."
    if f.required and not text:
        return text, f"{f.label} is required."
    return text, ""


def clean_secret(f: Field, value: Any) -> Tuple[str, str]:
    text = str(value if value is not None else "").strip()
    if any(ch in text for ch in "\r\n\x00"):
        return "", f"{f.label}: line breaks are not allowed."
    if len(text) > MAX_SECRET:
        return "", f"{f.label} is too long."
    if f.kind == "url" and text and not re.match(r"^https?://[^\s/?#]+", text, re.I):
        return "", f"{f.label} must be a web address starting with https://."
    return text, ""


# ---------------------------------------------------------------------------------------------
# The base class
# ---------------------------------------------------------------------------------------------
class Connector:
    """One connector. Subclasses set the class attributes, implement _test() (and, for sending connectors, their
    own calls) and may override check() for extra validation before connect() saves."""

    id: str = ""
    name: str = ""
    kind: str = ""
    icon: str = ""
    description: str = ""
    docs: str = ""                      # provider document URL + the date its shapes were read
    fields: List[Field] = []
    admin_only: bool = True
    multi: bool = False                 # instances "<id>:<slug>" (custom_mcp, webhook)
    sends: List[str] = []               # crawl data it sends: urls, titles, screenshots, page_text, hostnames, counts
    redirect_hosts: List[str] = []      # hosts a cross-origin redirect may go to (no secrets sent there)
    connect_flow: str = ""              # an existing dialog that connects it ("api_access:gsc"); "" = these fields
    allow_private_default: bool = False  # tests and self-hosted relays: private addresses allowed without the field
    max_instances: int = 20             # multi connectors

    def __init__(self, instance_id: str = "", instance_name: str = ""):
        if instance_id:
            self.id = instance_id
        if instance_name:
            self.name = instance_name
        self._lock = threading.RLock()

    # --- multi connectors -------------------------------------------------------------------------
    def is_template(self) -> bool:
        return self.multi and ":" not in self.id

    @property
    def base_id(self) -> str:
        return self.id.split(":", 1)[0]

    def instance(self, slug: str, name: str = "") -> "Connector":
        """The instance "<id>:<slug>" of a multi connector (its name from its saved config)."""
        cid = f"{self.base_id}:{slug}"
        inst = self.__class__(cid)
        saved = inst.stored().get("config") or {}
        inst.name = name or str(saved.get("name") or "") or slug
        return inst

    # --- storage ------------------------------------------------------------------------------
    def _store(self):
        from . import store
        return store

    def stored(self) -> Dict[str, Any]:
        """{"config": {...}, "meta": {...}} as saved ({} parts when nothing is)."""
        return self._store().load(self.id)

    def config(self) -> Dict[str, Any]:
        """Every non-secret field with its saved value, else its default."""
        saved = self.stored().get("config") or {}
        out: Dict[str, Any] = {}
        for f in self.fields:
            if f.secret:
                continue
            out[f.key] = copy.deepcopy(saved[f.key]) if f.key in saved else copy.deepcopy(f.default)
        for k in ("name",):
            if k in saved and k not in out:
                out[k] = saved[k]
        return out

    def meta(self) -> Dict[str, Any]:
        return dict(self.stored().get("meta") or {})

    def secret(self, key: str) -> str:
        return str(self._store().secrets(self.id).get(key) or "")

    def secret_flags(self) -> Dict[str, bool]:
        held = self._store().secrets(self.id)
        return {f.key: bool(held.get(f.key)) for f in self.fields if f.secret}

    def masker(self) -> Masker:
        held = self._store().secrets(self.id)
        urls = [held.get(f.key) for f in self.fields if f.secret and f.kind == "url" and held.get(f.key)]
        values = [held.get(f.key) for f in self.fields if f.secret and f.kind != "url" and held.get(f.key)]
        return Masker(values, urls)

    def allow_private(self) -> bool:
        return bool(self.config().get("allow_private_network")) or self.allow_private_default

    def _set_meta(self, **values: Any) -> None:
        self._store().update_meta(self.id, values)

    # --- state ----------------------------------------------------------------------------------
    def is_set_up(self) -> bool:
        """Saved as connected with every required field and secret present."""
        if not self.meta().get("connected"):
            return False
        cfg, flags = self.config(), self.secret_flags()
        for f in self.fields:
            if not f.required:
                continue
            if f.secret and not flags.get(f.key):
                return False
            if not f.secret and cfg.get(f.key) in (None, "", []):
                return False
        return True

    def public_config(self) -> Dict[str, Any]:
        """The non-secret config the dashboard may show (plus the host of each secret URL, as a hint)."""
        out = self.config()
        if self.multi and not self.is_template():
            out["id"] = self.id
        held = self._store().secrets(self.id)
        for f in self.fields:
            if f.secret and f.kind == "url" and held.get(f.key):
                try:
                    out[f"{f.key}_host"] = (urlsplit(held[f.key]).hostname or "").lower()
                except ValueError:
                    out[f"{f.key}_host"] = ""
        return out

    def off_reason(self) -> str:
        """A plain reason when the connector is connected but set to do nothing ("" = on)."""
        return ""

    def status(self) -> Status:
        if self.is_template():
            from . import store
            n = len(store.instance_ids(self.id))
            return Status("connected" if n else "not_connected",
                          f"{n} set up" if n else "Not connected: add one with its own name", config={"count": n})
        meta = self.meta()
        mask = self.masker()
        last_test = meta.get("last_test") if isinstance(meta.get("last_test"), dict) else None
        if last_test:
            last_test = dict(last_test, detail=mask(last_test.get("detail")))
        base = dict(secrets=self.secret_flags(), config=self.public_config(), last_test=last_test,
                    tested_live=bool(meta.get("tested_live")))
        if not self.is_set_up():
            return Status("not_connected", "Not connected", **base)
        off = self.off_reason()
        if off:
            return Status("off", off, **base)
        problem = self._last_problem(meta)
        if problem:
            return Status("error", mask(problem), **base)
        if not base["tested_live"]:
            return Status("connected", NOT_TESTED, **base)
        at = (last_test or {}).get("at") or meta.get("tested_at") or ""
        return Status("connected", f"Connected - tested with the live service{(' on ' + at[:10]) if at else ''}",
                      **base)

    @staticmethod
    def _last_problem(meta: Dict[str, Any]) -> str:
        """The newer of the last test and the last delivery, when it failed."""
        events = [e for e in (meta.get("last_test"), meta.get("last_delivery")) if isinstance(e, dict)]
        if not events:
            return ""
        latest = max(events, key=lambda e: str(e.get("at") or ""))
        if latest.get("ok"):
            return ""
        what = "The last test failed" if latest is meta.get("last_test") else "The last message could not be sent"
        return f"{what}: {latest.get('detail') or 'no detail'}"

    # --- connect / test / disconnect ---------------------------------------------------------------
    def validate(self, config: Optional[Dict[str, Any]], secrets: Optional[Dict[str, Any]]
                 ) -> Tuple[Dict[str, Any], Dict[str, Optional[str]], Dict[str, str]]:
        """(clean config, secret changes {key: value | None = remove}, {field: error}). Secrets left as
        "", "***" or {"set": ...} keep the saved value."""
        config = config if isinstance(config, dict) else {}
        secrets = secrets if isinstance(secrets, dict) else {}
        saved_cfg = self.config()
        held = self._store().secrets(self.id)
        clean: Dict[str, Any] = {}
        changes: Dict[str, Optional[str]] = {}
        errors: Dict[str, str] = {}
        for f in self.fields:
            if f.secret:
                given = secrets.get(f.key, config.get(f.key))
                if isinstance(given, dict) and given.get("clear"):
                    changes[f.key] = None
                elif not keep_secret(given):
                    value, err = clean_secret(f, given)
                    if err:
                        errors[f.key] = err
                    else:
                        changes[f.key] = value
                will_have = changes.get(f.key) if f.key in changes else held.get(f.key)
                if f.required and not will_have:
                    errors.setdefault(f.key, f"{f.label} is required.")
                continue
            given = config[f.key] if f.key in config else saved_cfg.get(f.key)
            value, err = clean_value(f, given)
            clean[f.key] = value
            if err:
                errors[f.key] = err
        return clean, changes, errors

    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        """Extra checks before saving: {field: error}. `secrets` = the secrets as they will be (server use)."""
        return {}

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        """Validate and save. A changed setting resets "tested with the live service". Raises ConnectorError.
        On a multi template it creates a new instance from config["name"] (its id is in Status.config["id"])."""
        if self.is_template():
            from . import registry
            inst = registry.create_instance(self.id, (config or {}).get("name") or "")
            return inst.connect(dict(config or {}, name=inst.name), secrets)
        with self._lock:
            clean, changes, errors = self.validate(config, secrets)
            if not errors:
                will = {k: v for k, v in self._store().secrets(self.id).items() if v}
                for k, v in changes.items():
                    if v is None:
                        will.pop(k, None)
                    else:
                        will[k] = v
                try:
                    errors.update(self.check(clean, will))
                except ConnectorError as e:
                    errors["_"] = str(e)
            if errors:
                mask = Masker([v for v in changes.values() if v],
                              [v for k, v in changes.items() if v and self._field(k) and self._field(k).kind == "url"])
                errors = {k: mask(self.masker()(v)) for k, v in errors.items()}
                first = next(iter(errors.values()))
                raise ConnectorError(first, "invalid", errors=errors)
            before = self.stored()
            changed = (before.get("config") or {}) != clean or any(
                changes.get(k) != self._store().secrets(self.id).get(k) for k in changes)
            self._store().save_config(self.id, clean)
            if changes:
                self._store().set_secrets(self.id, changes)
            meta = {"connected": True, "connected_at": (before.get("meta") or {}).get("connected_at") or now_iso()}
            if changed or not (before.get("meta") or {}).get("connected"):
                meta.update(tested_live=False, last_test=None, last_delivery=None, updated_at=now_iso())
            self._set_meta(**meta)
            self.on_change()
            return self.status()

    def _field(self, key: str) -> Optional[Field]:
        return next((f for f in self.fields if f.key == key), None)

    def _test(self) -> TestResult:
        raise ConnectorError("This connector has no test.", "invalid")

    def test(self) -> TestResult:
        """Runs the connector's own test (a real call to the service); the result is remembered, and a real 2xx
        marks the connector "tested with the live service". Never raises; the detail is masked."""
        if self.is_template():
            return TestResult(False, "Choose one of them to test.", 0)
        if not self.is_set_up():
            return TestResult(False, "Not connected: connect it first.", 0)
        mask = self.masker()
        state_before = self.status().state
        started = time.monotonic()
        try:
            res = self._test()
        except ConnectorError as e:
            res = TestResult(False, str(e))
        except Exception as e:          # a bug must not leak a URL: name the type only
            res = TestResult(False, f"The test failed unexpectedly ({type(e).__name__}).")
        res.ms = res.ms or int((time.monotonic() - started) * 1000)
        res.detail = mask(res.detail)[:500]
        entry = {"ok": bool(res.ok), "detail": res.detail, "at": now_iso(), "ms": res.ms}
        values: Dict[str, Any] = {"last_test": entry}
        if res.ok:
            values.update(tested_live=True, tested_at=entry["at"])
        self._set_meta(**values)
        if self.status().state != state_before:
            self.on_change()            # e.g. connected <-> error: the tabs that read the state refresh
        return res

    def record_delivery(self, ok: bool, detail: str) -> None:
        """Remembers the last automatic message (a failed one shows as the "error" state)."""
        self._set_meta(last_delivery={"ok": bool(ok), "detail": self.masker()(detail)[:300], "at": now_iso()})

    def disconnect(self) -> Status:
        """Removes the config and the secrets (of a multi template: nothing; remove each instance)."""
        if self.is_template():
            return self.status()
        with self._lock:
            self._store().delete(self.id)
            self.on_change()
            return self.status()

    def on_change(self) -> None:
        """Called after connect / disconnect (connectors whose state the tabs read refresh them)."""
        try:
            from .. import integrations
            integrations._changed()
        except Exception:
            pass

    def public(self) -> Dict[str, Any]:
        """Fields + status for the dashboard, never a secret value."""
        st = self.status()
        cfg = st.config
        flds = []
        for f in self.fields:
            d = f.public()
            if f.secret:
                d["value"] = {"set": bool(st.secrets.get(f.key))}
            else:
                d["value"] = copy.deepcopy(cfg.get(f.key, f.default))
            flds.append(d)
        out = {"id": self.id, "name": self.name, "kind": self.kind, "icon": self.icon,
               "description": self.description, "docs": self.docs, "admin_only": self.admin_only,
               "multi": self.multi, "sends": list(self.sends), "connect_flow": self.connect_flow,
               "fields": flds, "status": st.to_dict()}
        if self.multi:
            out["template"] = self.is_template()
            out["base_id"] = self.base_id
            if self.is_template():
                from . import store
                out["instances"] = store.instance_ids(self.id)
                for d in out["fields"]:
                    d["value"] = {"set": False} if d.get("secret") else copy.deepcopy(d.get("default"))
        return out
