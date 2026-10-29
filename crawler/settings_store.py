"""
Server-wide settings (File > Settings) and crawl configuration profiles (Configuration > Profiles).

Settings live in <reports_dir>/settings.json (reports_dir = env CRAWLER_REPORTS_DIR, else the system temp
folder), one section per Settings page; every user of the server shares them:
  proxy          Use Proxy Server, Address, Port (3128), Use Proxy Credentials, Username, Password
  retention      Automatically delete crawls after: never | 1 | 7 | 30 | 90 | custom (+ custom_days 1-3650);
                 autosave: save every finished crawl (File > Crawls)
  notifications  custom SMTP (host, port 587, encryption tls | ssl | none, username, password, from address)
                 + Email on Crawl Complete with its recipients, attach exports (zip, capped), dashboard link.
                 Fields left empty fall back to env CRAWLER_SMTP_HOST / _PORT / _USER / _PASSWORD / _ENCRYPTION /
                 _FROM (SMTP_* also read), so a deployment can configure mail without the dialog.
  integrations   API Access options that are not secrets (PageSpeed device / categories / URL cap, Search
                 Console date range / device / search type / URL Inspection, GA4 date range / metrics /
                 dimension / channel filter). Keys and Google tokens are kept by crawler/integrations, never here.
  analysis       Crawl Analysis defaults (crawler.config.default_analysis + bot_check_sample)
  browser        Embedded Browser: enabled
  branding       White label for the PDFs (P23) and the emails (notify): company name, logo, accent colour, cover
                 line, footer, email subject prefix. Empty fields = ARACHNIX's own. The logo (PNG / JPEG, at most
                 200 KB) is uploaded with save_branding_logo() and kept as <reports_dir>/branding/logo.png|jpg;
                 a PUT can only remove it ({"clear": true}). branding() = the effective values with the defaults.
Secrets (proxy password, SMTP password) are kept apart in <reports_dir>/secrets.json (readable by the server's
user only where the system allows it), so settings.json itself never holds one; the same file holds other secret
sections (secret_section / update_secret_section, e.g. "connectors" of crawler/connectors), which saving a
settings page keeps; they are never logged, exported or
returned: get_settings(public=True) shows them as {"set": bool, "source": "settings" | "env" | ""}. An
update leaves a secret as it is when the value sent is missing, "" , "***" or {"set": ...}; {"clear": true}
removes it.

  get_settings(public=True) -> {page: values, "_env": {page: {field: env var}}, "_updated_at": iso}
  get_page(page, public=True) ; effective(page) -> values with secrets and env fallbacks (server use only)
  validate(page, values) -> (clean values, {field: error}) ; update(page, values) -> {field: error} ({} = saved)
  reset(page) ; proxy_config() -> CrawlConfig.proxy dict ; analysis_defaults() -> CrawlConfig.analysis dict
  branding() -> effective brand ; branding_logo() -> (bytes, mime) | None
  save_branding_logo(data) -> (logo info, {field: error}) ; logo_info() -> {"set", "type", "bytes", "width", ...}

Profiles: <reports_dir>/profiles/<slug>.json and _default.json, {"app": "site-auditor", "profile_version": 1,
"name", "updated_at", "options": {...}}. options are the dashboard's crawl options (CrawlRequest fields plus
url_rewrite, segments, analysis and the Build 4 options of CrawlConfig...). Passwords, cookie values, header
values, tokens, API keys and sign-in hand-off ids are always removed before a profile is stored or exported
(strip_secrets). validate_profile checks the known options (crawler.config.validate_options for the Build 4 ones).

  list_profiles() -> [{"name", "slug", "updated_at", "default"}]
  save_profile(name, options) -> (meta, errors) ; load_profile(name) -> profile | None ; delete_profile(name)
  get_default() / set_default(options) / clear_default() ; export_profile(name) -> (filename, bytes)
  import_profile(data, name=None) -> (meta, errors)
"""
import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

APP = "site-auditor"
PROFILE_VERSION = 1
SETTINGS_FILE = "settings.json"
SECRETS_FILE = "secrets.json"     # SECRET_FIELDS values only, never in settings.json
PROFILES_DIR = "profiles"
DEFAULT_PROFILE = "_default"
MAX_PROFILE_BYTES = 512 * 1024
MAX_PROFILES = 200
MAX_RECIPIENTS = 50
RETENTION_CHOICES = ("never", "1", "7", "30", "90", "custom")
ENCRYPTIONS = ("tls", "ssl", "none")
PSI_DEVICES = ("mobile", "desktop")
PSI_CATEGORIES = ("performance", "accessibility", "best-practices", "seo")
PSI_GROUPS = ("overview", "crux", "lighthouse", "insights", "diagnostics", "mobile_friendly", "accessibility")
PSI_DEFAULT_GROUPS = ("overview", "crux", "lighthouse", "insights", "diagnostics", "mobile_friendly")
GSC_QUERY_OPS = ("none", "contains", "not_contains", "equals", "not_equals", "regex", "not_regex")
GSC_DATA_STATES = ("final", "all")
DATE_RANGES = ("7d", "28d", "30d", "90d", "180d", "365d", "16m", "custom")
GSC_DEVICES = ("all", "desktop", "mobile", "tablet")
GSC_TYPES = ("web", "image", "video", "news", "discover", "googleNews")
GA4_DIMENSIONS = ("landingPagePlusQueryString", "pagePathPlusQueryString", "landingPage", "pagePath")
GA4_DEFAULT_METRICS = ["sessions", "screenPageViews", "engagedSessions", "engagementRate", "keyEvents",
                       "eventCount", "totalRevenue"]
_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+$")
_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
_PROFILE_NAME = re.compile(r"^[A-Za-z0-9 _.()-]{1,60}$")
_METRIC = re.compile(r"^[A-Za-z][A-Za-z0-9_:]{0,80}$")

PAGES: Dict[str, Dict[str, Any]] = {
    "proxy": {"enabled": False, "host": "", "port": 3128, "use_credentials": False, "username": "", "password": ""},
    "retention": {"delete_after": "never", "custom_days": 30, "autosave": True},
    "notifications": {"smtp_host": "", "smtp_port": 587, "encryption": "tls", "smtp_username": "",
                      "smtp_password": "", "from_address": "", "from_name": "ARACHNIX",
                      "email_on_complete": False, "recipients": [], "attach_exports": False, "dashboard_url": ""},
    "integrations": {
        "psi": {"enabled": False, "device": "mobile", "categories": ["performance"], "max_urls": 500,
                "groups": list(PSI_DEFAULT_GROUPS), "concurrency": 4, "only_missing": True, "indexable_only": True},
        "gsc": {"date_range": "30d", "start_date": "", "end_date": "", "device": "all", "search_type": "web",
                "country": "", "url_inspection": False, "inspection_limit": 2000, "crawl_new_urls": False,
                "match_trailing_slash": True, "match_case": False, "match_protocol": True, "match_www": False,
                "query_filter": {"op": "none", "value": ""}, "limit_max_results": 100000, "data_state": "final",
                "inspect_skip_nonindexable": False, "multiple_properties": False, "inspection_language": "en-US"},
        "ga4": {"date_range": "30d", "start_date": "", "end_date": "", "metrics": list(GA4_DEFAULT_METRICS),
                "dimension": "landingPagePlusQueryString", "channel_filter": "", "crawl_new_urls": False,
                "match_trailing_slash": True, "match_case": False, "match_protocol": True, "match_www": False,
                "metric_dimensions": {}, "filter": {"dimension": "", "value": ""}, "limit_max_results": 100000},
    },
    "analysis": {"auto": True, "link_score": True, "sitemaps": True, "near_duplicates": True, "pagination": True,
                 "hreflang": True, "post_crawl_checks": True, "bot_check": True, "semantic_similarity": True,
                 "bot_check_sample": 10},
    "browser": {"enabled": True},
    "branding": {"company_name": "", "logo": "", "accent_color": "", "cover_line": "", "footer": "",
                 "email_subject_prefix": ""},
}
BRAND_NAME = "ARACHNIX"
BRAND_ACCENT = "#2F6B3F"          # ARACHNIX's brand green (the dashboard's --brand-tile)
BRANDING_DIR = "branding"
LOGO_MAX_BYTES = 200 * 1024
LOGO_NAMES = {"png": "logo.png", "jpeg": "logo.jpg"}
LOGO_MIN_PX, LOGO_MAX_PX = 16, 4000
_HEX = re.compile(r"^(?:#([0-9A-Fa-f]{3})|#?([0-9A-Fa-f]{6}))$")   # "#1a5", "#11AA55" or "11AA55" (not "bad")
_BRAND_LIMITS = {"company_name": 80, "cover_line": 200, "footer": 300, "email_subject_prefix": 40}
SECRET_FIELDS: Dict[str, Tuple[str, ...]] = {"proxy": ("password",), "notifications": ("smtp_password",)}
ENV_FIELDS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "notifications": {
        "smtp_host": ("CRAWLER_SMTP_HOST", "SMTP_HOST"),
        "smtp_port": ("CRAWLER_SMTP_PORT", "SMTP_PORT"),
        "smtp_username": ("CRAWLER_SMTP_USER", "CRAWLER_SMTP_USERNAME", "SMTP_USERNAME", "SMTP_USER"),
        "smtp_password": ("CRAWLER_SMTP_PASSWORD", "SMTP_PASSWORD"),
        "encryption": ("CRAWLER_SMTP_ENCRYPTION", "SMTP_ENCRYPTION"),
        "from_address": ("CRAWLER_SMTP_FROM", "SMTP_FROM"),
        "dashboard_url": ("CRAWLER_DASHBOARD_URL",),
    },
}
# Option keys never kept in a profile (exact names, lower case, at any depth)
_PROFILE_SECRET_KEYS = {"password", "passwd", "cookies", "cookie", "custom_header_value", "live_session_id",
                        "token", "access_token", "refresh_token", "api_key", "apikey", "secret", "client_secret",
                        "smtp_password", "authorization", "handoff_id", "handoff"}
_lock = threading.RLock()


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------
def default_reports_dir() -> str:
    return os.environ.get("CRAWLER_REPORTS_DIR") or tempfile.gettempdir()


def settings_path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(reports_dir or default_reports_dir(), SETTINGS_FILE)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: str, data: Any, private: bool = False) -> None:
    """Atomic write; files holding secrets are made readable by the server's user only where possible."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    if private:
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


def secrets_path(reports_dir: Optional[str] = None) -> str:
    return os.path.join(reports_dir or default_reports_dir(), SECRETS_FILE)


def _stored(reports_dir: Optional[str]) -> Dict[str, Any]:
    """settings.json with the secret fields from secrets.json put back in place (server use only)."""
    data = _read_json(settings_path(reports_dir))
    data = data if isinstance(data, dict) else {}
    secrets = _read_json(secrets_path(reports_dir))
    for page, fields in SECRET_FIELDS.items():
        held = (secrets or {}).get(page) if isinstance(secrets, dict) else None
        if not isinstance(held, dict):
            continue
        for f in fields:
            if held.get(f):
                section = data.setdefault(page, {})
                if isinstance(section, dict):
                    section[f] = held[f]
    return data


def _save(reports_dir: Optional[str], data: Dict[str, Any]) -> None:
    """Writes settings.json without its secrets and secrets.json with them (both private where possible).
    Other sections of secrets.json (e.g. "connectors", kept by crawler/connectors) are left as they are."""
    plain = copy.deepcopy(data)
    with _lock:
        held = _read_json(secrets_path(reports_dir))
        secrets: Dict[str, Any] = {k: v for k, v in (held if isinstance(held, dict) else {}).items()
                                   if k not in SECRET_FIELDS}
        for page, fields in SECRET_FIELDS.items():
            section = plain.get(page)
            if not isinstance(section, dict):
                continue
            for f in fields:
                value = section.pop(f, None)
                if value:
                    secrets.setdefault(page, {})[f] = str(value)
        _write_json(settings_path(reports_dir), plain, private=True)
        if secrets or os.path.exists(secrets_path(reports_dir)):
            _write_json(secrets_path(reports_dir), secrets, private=True)


# ---------------------------------------------------------------------------------------------
# Other secret sections of secrets.json (server use only; never returned)
# ---------------------------------------------------------------------------------------------
def secret_section(name: str, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """A copy of one top-level section of secrets.json that is not a settings page (e.g. "connectors")."""
    if name in SECRET_FIELDS or name in PAGES:
        raise KeyError(name)
    with _lock:
        held = _read_json(secrets_path(reports_dir))
    section = held.get(name) if isinstance(held, dict) else None
    return copy.deepcopy(section) if isinstance(section, dict) else {}


def update_secret_section(name: str, change: Callable[[Dict[str, Any]], Dict[str, Any]],
                          reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """Applies change(section) -> new section to one non-page section of secrets.json, atomically and
    owner-only; every other section (the settings pages' secrets included) is kept. Returns the new section."""
    if name in SECRET_FIELDS or name in PAGES:
        raise KeyError(name)
    with _lock:
        held = _read_json(secrets_path(reports_dir))
        held = held if isinstance(held, dict) else {}
        current = held.get(name) if isinstance(held.get(name), dict) else {}
        new = change(copy.deepcopy(current))
        if new:
            held[name] = new
        else:
            held.pop(name, None)
        if held or os.path.exists(secrets_path(reports_dir)):
            _write_json(secrets_path(reports_dir), held, private=True)
        return copy.deepcopy(new or {})


def _merge(defaults: Any, values: Any) -> Any:
    """defaults with the stored values of the same keys (unknown keys dropped, types kept)."""
    if isinstance(defaults, dict):
        out = {}
        src = values if isinstance(values, dict) else {}
        for k, d in defaults.items():
            out[k] = _merge(d, src[k]) if k in src else copy.deepcopy(d)
        return out
    if values is None:
        return copy.deepcopy(defaults)
    if isinstance(defaults, bool):
        return bool(values) if isinstance(values, (bool, int)) else defaults
    if isinstance(defaults, int) and not isinstance(defaults, bool):
        try:
            return int(values)
        except (TypeError, ValueError):
            return defaults
    if isinstance(defaults, list):
        return list(values) if isinstance(values, list) else copy.deepcopy(defaults)
    if isinstance(defaults, str):
        return str(values) if isinstance(values, (str, int, float)) else defaults
    return values


def _env(page: str, field: str) -> Tuple[str, str]:
    """(value, variable name) of the first env variable set for a field, else ("", "")."""
    for name in (ENV_FIELDS.get(page) or {}).get(field, ()):
        v = os.environ.get(name)
        if v not in (None, ""):
            return v, name
    return "", ""


# ---------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------
def _page_values(page: str, reports_dir: Optional[str]) -> Dict[str, Any]:
    return _merge(PAGES[page], _stored(reports_dir).get(page))


def effective(page: str, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """A page's values for the server's own use: secrets included, empty fields filled from env."""
    if page not in PAGES:
        raise KeyError(page)
    values = _page_values(page, reports_dir)
    # mail set up by the deployment (no SMTP host in the dialog): every env field wins; else env only fills gaps
    env_mode = page == "notifications" and not values.get("smtp_host")
    for field in (ENV_FIELDS.get(page) or {}):
        v, _name = _env(page, field)
        if v and (env_mode or values.get(field) in ("", None)):
            values[field] = _merge(PAGES[page][field], v)
    if page == "notifications":
        values["encryption"] = str(values.get("encryption") or "tls").lower()
        if values["encryption"] not in ENCRYPTIONS:
            values["encryption"] = "tls"
    return values


def _public(page: str, values: Dict[str, Any], reports_dir: Optional[str]) -> Dict[str, Any]:
    out = copy.deepcopy(values)
    stored = _page_values(page, reports_dir)
    for field in SECRET_FIELDS.get(page, ()):
        env_v, _env_name = _env(page, field)
        source = "settings" if stored.get(field) else ("env" if env_v else "")
        out[field] = {"set": bool(source), "source": source}
    if page == "notifications" and str(out.get("from_name") or "").strip() in ("Site Auditor", "SlickUP"):
        out["from_name"] = "ARACHNIX"   # the old default sender name, shown (and sent, see notify) as the new product name
    if page == "branding":
        out["logo"] = logo_info(reports_dir)
    return out


def get_page(page: str, public: bool = True, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    if page not in PAGES:
        raise KeyError(page)
    values = _page_values(page, reports_dir)
    return _public(page, values, reports_dir) if public else values


def get_settings(public: bool = True, reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """Every settings page. public: secrets as {"set", "source"} (what GET /api/settings returns).
    "_env" names the env variables a field falls back to and that are set on this server."""
    out: Dict[str, Any] = {p: get_page(p, public, reports_dir) for p in PAGES}
    env_info: Dict[str, Dict[str, str]] = {}
    for page, fields in ENV_FIELDS.items():
        for field in fields:
            _v, name = _env(page, field)
            if name:
                env_info.setdefault(page, {})[field] = name
    out["_env"] = env_info
    out["_updated_at"] = str(_stored(reports_dir).get("_updated_at") or "")
    return out


def proxy_config(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """CrawlConfig.proxy from Settings > Proxy (credentials only when "Use Proxy Credentials" is on)."""
    p = effective("proxy", reports_dir)
    creds = bool(p.get("enabled") and p.get("use_credentials"))
    return {"enabled": bool(p.get("enabled") and p.get("host")), "host": p.get("host") or "",
            "port": int(p.get("port") or 3128), "username": p.get("username") if creds else "",
            "password": p.get("password") if creds else ""}


def analysis_defaults(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    a = effective("analysis", reports_dir)
    a.pop("bot_check_sample", None)
    return a


# ---------------------------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------------------------
def parse_emails(value: Any) -> Tuple[List[str], List[str]]:
    """(valid addresses in order without repeats, invalid entries) from a list or "a@x.com, b@y.com" text."""
    if isinstance(value, str):
        parts = re.split(r"[,;\s]+", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = []
    ok, bad, seen = [], [], set()
    for p in parts:
        p = p.strip().strip("<>")
        if not p:
            continue
        if _EMAIL.match(p) and len(p) <= 254:
            if p.lower() not in seen:
                seen.add(p.lower())
                ok.append(p)
        else:
            bad.append(p[:80])
    return ok, bad


def _int_in(values: Dict[str, Any], errors: Dict[str, str], field: str, lo: int, hi: int, label: str) -> None:
    try:
        n = int(str(values.get(field)).strip())
    except (TypeError, ValueError):
        errors[field] = f"{label} must be a whole number."
        return
    if n < lo or n > hi:
        errors[field] = f"{label} must be between {lo:,} and {hi:,}."
        return
    values[field] = n


def _choice(values: Dict[str, Any], errors: Dict[str, str], field: str, choices: Tuple[str, ...], label: str) -> None:
    v = str(values.get(field) or "").strip()
    match = next((c for c in choices if c.lower() == v.lower()), None)
    if match is None:
        errors[field] = f"{label} must be one of: {', '.join(choices)}."
    else:
        values[field] = match


def _keep_secret(given: Any) -> bool:
    """The value sent for a secret field means "leave it as it is"."""
    return given is None or given in ("", "***") or (isinstance(given, dict) and "clear" not in given)


def validate(page: str, values: Dict[str, Any], reports_dir: Optional[str] = None
             ) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(the page's full values after applying `values`, {field: plain error}). Fields not sent keep their
    current value; unknown fields are ignored. Secrets follow the rule in the module docstring."""
    if page not in PAGES:
        return {}, {"_page": f"Unknown settings page: {page}"}
    current = _page_values(page, reports_dir)
    given = values if isinstance(values, dict) else {}
    merged = copy.deepcopy(current)
    for k, v in given.items():
        if k not in PAGES[page]:
            continue
        if page == "branding" and k == "logo":
            if isinstance(v, dict) and v.get("clear"):
                merged[k] = ""          # removed (the file goes in update); uploading is save_branding_logo's
            continue
        if k in SECRET_FIELDS.get(page, ()):
            if isinstance(v, dict) and v.get("clear"):
                merged[k] = ""
            elif not _keep_secret(v):
                merged[k] = str(v)
            continue
        if isinstance(merged.get(k), dict) and isinstance(v, dict):
            merged[k] = dict(merged[k], **{a: b for a, b in v.items() if a in PAGES[page][k]})
            continue
        merged[k] = v
    errors: Dict[str, str] = {}
    _VALIDATORS[page](merged, errors)
    return merged, errors


def _v_proxy(v: Dict[str, Any], e: Dict[str, str]) -> None:
    v["enabled"] = bool(v.get("enabled"))
    v["use_credentials"] = bool(v.get("use_credentials")) and v["enabled"]
    v["host"] = str(v.get("host") or "").strip()
    host = re.sub(r"^https?://", "", v["host"], flags=re.I).rstrip("/")
    if ":" in host and not host.startswith("[") and host.count(":") == 1:
        host, _, port = host.partition(":")
        if port.isdigit() and not str(v.get("port") or "").strip():
            v["port"] = port
    v["host"] = host
    if v["enabled"] and not host:
        e["host"] = "Enter the proxy address (host name or IP)."
    elif host and not _HOST.match(host):
        e["host"] = "The proxy address is not a valid host name or IP address."
    _int_in(v, e, "port", 1, 65535, "Port")
    v["username"] = str(v.get("username") or "").strip()
    if v["use_credentials"] and not v["username"]:
        e["username"] = "Enter the proxy username, or turn off Use Proxy Credentials."
    if any(ch in (v.get("username") or "") + str(v.get("password") or "") for ch in "\r\n"):
        e["password"] = "Line breaks are not allowed."


def _v_retention(v: Dict[str, Any], e: Dict[str, str]) -> None:
    choice = str(v.get("delete_after") or "never").strip().lower().rstrip("d")
    if choice not in RETENTION_CHOICES:
        e["delete_after"] = "Choose Never, 1, 7, 30, 90 days or Custom."
    else:
        v["delete_after"] = choice
    if choice == "custom":
        _int_in(v, e, "custom_days", 1, 3650, "Custom days")
    else:
        try:
            v["custom_days"] = max(1, min(3650, int(v.get("custom_days") or 30)))
        except (TypeError, ValueError):
            v["custom_days"] = 30
    v["autosave"] = bool(v.get("autosave"))


def _v_notifications(v: Dict[str, Any], e: Dict[str, str]) -> None:
    v["smtp_host"] = str(v.get("smtp_host") or "").strip()
    if v["smtp_host"] and not _HOST.match(v["smtp_host"]):
        e["smtp_host"] = "The SMTP host is not a valid host name or IP address."
    _int_in(v, e, "smtp_port", 1, 65535, "Port")
    _choice(v, e, "encryption", ENCRYPTIONS, "Encryption")
    for f in ("smtp_username", "from_address", "from_name", "dashboard_url"):
        v[f] = str(v.get(f) or "").strip()
        if any(ch in v[f] for ch in "\r\n"):
            e[f] = "Line breaks are not allowed."
    if any(ch in str(v.get("smtp_password") or "") for ch in "\r\n"):
        e["smtp_password"] = "Line breaks are not allowed."
    if v["from_address"] and not _EMAIL.match(v["from_address"]):
        e["from_address"] = "The From address is not a valid email address."
    if v["dashboard_url"] and not re.match(r"^https?://[^\s]+$", v["dashboard_url"], re.I):
        e["dashboard_url"] = "The dashboard link must start with http:// or https://."
    ok, bad = parse_emails(v.get("recipients"))
    if bad:
        e["recipients"] = "Not valid email addresses: " + ", ".join(bad[:5])
    elif len(ok) > MAX_RECIPIENTS:
        e["recipients"] = f"At most {MAX_RECIPIENTS} recipients."
    v["recipients"] = ok
    v["email_on_complete"] = bool(v.get("email_on_complete"))
    v["attach_exports"] = bool(v.get("attach_exports"))
    if v["email_on_complete"] and not ok:
        e["recipients"] = e.get("recipients") or "Add at least one recipient, or turn off Email on Crawl Complete."
    host_env, _n = _env("notifications", "smtp_host")
    if v["email_on_complete"] and not (v["smtp_host"] or host_env):
        e["smtp_host"] = "Set the SMTP host (or env CRAWLER_SMTP_HOST) to send emails."


def _v_integrations(v: Dict[str, Any], e: Dict[str, str]) -> None:
    psi, gsc, ga4 = v.get("psi") or {}, v.get("gsc") or {}, v.get("ga4") or {}
    psi_e: Dict[str, str] = {}
    psi["enabled"] = bool(psi.get("enabled"))
    _choice(psi, psi_e, "device", PSI_DEVICES, "Device")
    cats = [str(c).strip().lower() for c in psi.get("categories") or [] if str(c).strip()]
    unknown = [c for c in cats if c not in PSI_CATEGORIES]
    if unknown:
        psi_e["categories"] = "Unknown categories: " + ", ".join(unknown[:5])
    psi["categories"] = [c for c in PSI_CATEGORIES if c in cats] or ["performance"]
    _int_in(psi, psi_e, "max_urls", 1, 25000, "URLs per run")
    groups = [str(g).strip() for g in psi.get("groups") or [] if str(g).strip()]
    bad_groups = [g for g in groups if g not in PSI_GROUPS]
    if bad_groups:
        psi_e["groups"] = "Unknown metric groups: " + ", ".join(bad_groups[:5])
    psi["groups"] = [g for g in PSI_GROUPS if g in groups] or list(PSI_DEFAULT_GROUPS)
    _int_in(psi, psi_e, "concurrency", 1, 10, "Requests at once")
    psi["only_missing"] = bool(psi.get("only_missing"))
    psi["indexable_only"] = bool(psi.get("indexable_only"))
    for key, sec in (("gsc", gsc), ("ga4", ga4)):
        se: Dict[str, str] = {}
        _choice(sec, se, "date_range", DATE_RANGES, "Date range")
        if sec.get("date_range") == "custom":
            for f in ("start_date", "end_date"):
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(sec.get(f) or "")):
                    se[f] = "Enter the date as YYYY-MM-DD."
            if not se and str(sec["start_date"]) > str(sec["end_date"]):
                se["end_date"] = "The end date is before the start date."
        sec["crawl_new_urls"] = bool(sec.get("crawl_new_urls"))
        for f in ("match_trailing_slash", "match_case", "match_protocol", "match_www"):
            sec[f] = bool(sec.get(f))
        _int_in(sec, se, "limit_max_results", 1, 10_000_000, "Limit Max Results")
        if key == "gsc":
            qf = sec.get("query_filter") if isinstance(sec.get("query_filter"), dict) else {}
            op = str(qf.get("op") or "none").strip().lower()
            if op not in GSC_QUERY_OPS:
                se["query_filter"] = "Search Query Filter must be one of: " + ", ".join(GSC_QUERY_OPS) + "."
                op = "none"
            value = str(qf.get("value") or "")[:4000]
            if op in ("regex", "not_regex") and value:
                try:
                    re.compile(value)
                except re.error as ex:
                    se["query_filter"] = f"The query filter is not a valid regex: {ex}"
            if op != "none" and not value.strip():
                se["query_filter"] = "Enter the query text to filter on, or set the filter to None."
            sec["query_filter"] = {"op": op, "value": value}
            _choice(sec, se, "data_state", GSC_DATA_STATES, "Data state")
            sec["inspect_skip_nonindexable"] = bool(sec.get("inspect_skip_nonindexable"))
            sec["multiple_properties"] = bool(sec.get("multiple_properties"))
            lang = str(sec.get("inspection_language") or "en-US").strip()
            if not re.match(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$", lang):
                se["inspection_language"] = "Use a language code such as en-US."
            sec["inspection_language"] = lang[:20]
            _choice(sec, se, "device", GSC_DEVICES, "Device")
            _choice(sec, se, "search_type", GSC_TYPES, "Search type")
            sec["country"] = str(sec.get("country") or "").strip().upper()
            if sec["country"] and not re.match(r"^[A-Z]{3}$", sec["country"]):
                se["country"] = "Use a three-letter country code (ISO 3166-1 alpha-3), or leave it empty."
            sec["url_inspection"] = bool(sec.get("url_inspection"))
            _int_in(sec, se, "inspection_limit", 1, 2000, "URL Inspection limit per day")
        else:
            _choice(sec, se, "dimension", GA4_DIMENSIONS, "Dimension")
            metrics = [str(m).strip() for m in sec.get("metrics") or [] if str(m).strip()]
            bad = [m for m in metrics if not _METRIC.match(m)]
            if bad:
                se["metrics"] = "Not valid metric names: " + ", ".join(bad[:5])
            if "sessions" not in metrics:
                metrics.insert(0, "sessions")      # always kept (Screaming Frog: cannot be unselected)
            sec["metrics"] = list(dict.fromkeys(metrics))[:50]
            sec["channel_filter"] = str(sec.get("channel_filter") or "").strip()[:100]
            md = sec.get("metric_dimensions") if isinstance(sec.get("metric_dimensions"), dict) else {}
            sec["metric_dimensions"] = {str(m): str(d) for m, d in md.items()
                                        if str(m) in sec["metrics"] and str(d) in GA4_DIMENSIONS}
            flt = sec.get("filter") if isinstance(sec.get("filter"), dict) else {}
            dim = str(flt.get("dimension") or "").strip()
            if dim and not _METRIC.match(dim):
                se["filter"] = "The filter dimension is not a valid GA4 dimension name."
                dim = ""
            sec["filter"] = {"dimension": dim, "value": str(flt.get("value") or "").strip()[:200] if dim else ""}
        for f, msg in se.items():
            e[f"{key}.{f}"] = msg
    for f, msg in psi_e.items():
        e[f"psi.{f}"] = msg
    v.update(psi=psi, gsc=gsc, ga4=ga4)


def _v_analysis(v: Dict[str, Any], e: Dict[str, str]) -> None:
    for k in PAGES["analysis"]:
        if k != "bot_check_sample":
            v[k] = bool(v.get(k))
    _int_in(v, e, "bot_check_sample", 1, 50, "Pages checked as each bot")


def _v_browser(v: Dict[str, Any], e: Dict[str, str]) -> None:
    v["enabled"] = bool(v.get("enabled"))


def _v_branding(v: Dict[str, Any], e: Dict[str, str]) -> None:
    labels = {"company_name": "Company name", "cover_line": "Cover line", "footer": "Footer",
              "email_subject_prefix": "Email subject prefix"}
    for f, limit in _BRAND_LIMITS.items():
        text = str(v.get(f) or "")
        if any(ch in text for ch in "\r\n"):
            e[f] = "Line breaks are not allowed."
        text = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())
        if len(text) > limit:
            e[f] = f"{labels[f]} has at most {limit} characters."
        v[f] = text[:limit]
    color = str(v.get("accent_color") or "").strip()
    if color:
        m = _HEX.match(color)
        if not m:
            e["accent_color"] = "Use a colour like #2F6B3F (a # and 6 hexadecimal digits)."
        else:
            h = m.group(1) or m.group(2)
            color = "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h).upper()
    v["accent_color"] = color
    if v.get("logo") not in LOGO_NAMES.values():
        v["logo"] = ""


_VALIDATORS = {"proxy": _v_proxy, "retention": _v_retention, "notifications": _v_notifications,
               "integrations": _v_integrations, "analysis": _v_analysis, "browser": _v_browser,
               "branding": _v_branding}


# ---------------------------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------------------------
def update(page: str, values: Dict[str, Any], reports_dir: Optional[str] = None) -> Dict[str, str]:
    """Validates and saves one page. Returns {} when saved, else {field: error} and nothing is saved."""
    with _lock:
        merged, errors = validate(page, values, reports_dir)
        if errors:
            return errors
        data = _stored(reports_dir)
        data[page] = merged
        data["_updated_at"] = _now()
        _save(reports_dir, data)
        if page == "branding" and not merged.get("logo"):
            _remove_logos(reports_dir)
    return {}


def reset(page: str, reports_dir: Optional[str] = None) -> None:
    """Back to the defaults (secrets of the page removed too; for branding, the logo file too)."""
    if page not in PAGES:
        raise KeyError(page)
    with _lock:
        data = _stored(reports_dir)
        data.pop(page, None)
        data["_updated_at"] = _now()
        _save(reports_dir, data)
        if page == "branding":
            _remove_logos(reports_dir)


# ---------------------------------------------------------------------------------------------
# Branding (white label): the logo file and the effective brand
# ---------------------------------------------------------------------------------------------
def _branding_dir(reports_dir: Optional[str]) -> str:
    return os.path.join(reports_dir or default_reports_dir(), BRANDING_DIR)


def _remove_logos(reports_dir: Optional[str], keep: str = "") -> None:
    for name in LOGO_NAMES.values():
        if name == keep:
            continue
        try:
            os.remove(os.path.join(_branding_dir(reports_dir), name))
        except OSError:
            pass


def _logo_path(reports_dir: Optional[str]) -> str:
    """The saved logo's path, or "" (none saved, or its file is gone)."""
    name = str(_page_values("branding", reports_dir).get("logo") or "")
    if name not in LOGO_NAMES.values():
        return ""
    path = os.path.join(_branding_dir(reports_dir), name)
    return path if os.path.isfile(path) else ""


def _image_facts(data: bytes) -> Tuple[str, int, int, str]:
    """(kind "png" | "jpeg", width, height, error) of a logo upload, checked with Pillow."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        kind = "png"
    elif data[:3] == b"\xff\xd8\xff":
        kind = "jpeg"
    else:
        return "", 0, 0, "The logo must be a PNG or JPEG image."
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").lower()
            width, height = im.size
            im.verify()
    except Exception:
        return "", 0, 0, "The image could not be read (the file is damaged or not a real PNG / JPEG)."
    if fmt not in ("png", "jpeg"):
        return "", 0, 0, "The logo must be a PNG or JPEG image."
    if fmt != kind:
        kind = fmt
    if min(width, height) < LOGO_MIN_PX or max(width, height) > LOGO_MAX_PX:
        return "", 0, 0, f"The logo must be between {LOGO_MIN_PX} and {LOGO_MAX_PX:,} pixels on each side."
    return kind, width, height, ""


def logo_info(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The public view of the saved logo: {"set": False} or {"set": True, "type", "mime", "bytes", "width",
    "height", "updated_at"} (never the path)."""
    path = _logo_path(reports_dir)
    if not path:
        return {"set": False}
    try:
        with open(path, "rb") as fh:
            data = fh.read(LOGO_MAX_BYTES + 1)
        st = os.stat(path)
    except OSError:
        return {"set": False}
    kind, width, height, why = _image_facts(data)
    if why:
        return {"set": False}
    return {"set": True, "type": kind, "mime": f"image/{kind}", "bytes": len(data), "width": width, "height": height,
            "updated_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def save_branding_logo(data: bytes, reports_dir: Optional[str] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Checks and saves an uploaded logo (POST /api/settings/branding/logo, admin): PNG or JPEG, at most 200 KB,
    16-4,000 px a side. Returns (logo_info, {}) or ({"set": ...}, {"logo": error}); nothing changes on error."""
    data = bytes(data or b"")
    if not data:
        return logo_info(reports_dir), {"logo": "Choose a PNG or JPEG file."}
    if len(data) > LOGO_MAX_BYTES:
        return logo_info(reports_dir), {"logo": f"The logo is {len(data) / 1024:,.0f} KB; at most "
                                                f"{LOGO_MAX_BYTES // 1024} KB is allowed."}
    kind, _w, _h, why = _image_facts(data)
    if why:
        return logo_info(reports_dir), {"logo": why}
    name = LOGO_NAMES[kind]
    folder = _branding_dir(reports_dir)
    with _lock:
        os.makedirs(folder, exist_ok=True)
        tmp = os.path.join(folder, f".{name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, os.path.join(folder, name))
        _remove_logos(reports_dir, keep=name)
        stored = _stored(reports_dir)
        page = _merge(PAGES["branding"], stored.get("branding"))
        page["logo"] = name
        stored["branding"] = page
        stored["_updated_at"] = _now()
        _save(reports_dir, stored)
    return logo_info(reports_dir), {}


def branding_logo(reports_dir: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
    """(bytes, mime type) of the saved logo, or None."""
    path = _logo_path(reports_dir)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = fh.read(LOGO_MAX_BYTES + 1)
    except OSError:
        return None
    kind, _w, _h, why = _image_facts(data)
    return None if why else (data, f"image/{kind}")


def branding(reports_dir: Optional[str] = None) -> Dict[str, Any]:
    """The effective brand for PDFs and emails, ARACHNIX's own where a field is empty:
    {"custom": bool (any field set), "company_name", "name" (company name or ARACHNIX), "accent_color" ("#RRGGBB"),
     "accent_custom": bool, "cover_line", "footer", "email_subject_prefix" (as set, or "[<name>]"),
     "subject_prefix" (the same + a space), "logo_path" (or ""), "logo_mime", "logo_width", "logo_height"}."""
    v = _page_values("branding", reports_dir)
    company = str(v.get("company_name") or "").strip()
    name = company or BRAND_NAME
    prefix = str(v.get("email_subject_prefix") or "").strip() or f"[{name}]"
    path = _logo_path(reports_dir)
    info = logo_info(reports_dir) if path else {"set": False}
    accent = str(v.get("accent_color") or "").strip()
    custom = bool(company or accent or v.get("cover_line") or v.get("footer") or v.get("email_subject_prefix")
                  or info.get("set"))
    return {"custom": custom, "company_name": company, "name": name,
            "accent_color": accent or BRAND_ACCENT, "accent_custom": bool(accent),
            "cover_line": str(v.get("cover_line") or ""), "footer": str(v.get("footer") or ""),
            "email_subject_prefix": prefix, "subject_prefix": prefix + " ",
            "logo_path": path if info.get("set") else "", "logo_mime": info.get("mime") or "",
            "logo_width": int(info.get("width") or 0), "logo_height": int(info.get("height") or 0)}


# ---------------------------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------------------------
def _profiles_dir(reports_dir: Optional[str]) -> str:
    return os.path.join(reports_dir or default_reports_dir(), PROFILES_DIR)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", str(name or "").strip().lower()).strip("-.")
    return s[:60]


def clean_profile_name(name: Any) -> str:
    return " ".join(str(name or "").split())[:60]


def strip_secrets(options: Any, depth: int = 0) -> Any:
    """A copy of crawl options without passwords, cookie values, header values, tokens or API keys: the login
    keeps only its kind and page, extra HTTP headers keep only their names."""
    if depth > 20:
        return None
    if isinstance(options, dict):
        out = {}
        for k, v in options.items():
            key = str(k).lower()
            if key in _PROFILE_SECRET_KEYS:
                continue
            if key == "request_headers" and isinstance(v, list):
                out[k] = [{"name": str(h.get("name") or "")[:100], "value": ""} for h in v if isinstance(h, dict)]
                continue
            if key == "auth" and isinstance(v, dict):
                out[k] = {a: v[a] for a in ("enabled", "auth_type", "login_url", "username", "custom_header_name",
                                             "username_selector", "password_selector", "submit_selector") if a in v}
                continue
            if key == "proxy" and isinstance(v, dict):
                out[k] = {a: v[a] for a in ("enabled", "host", "port") if a in v}
                continue
            out[k] = strip_secrets(v, depth + 1)
        return out
    if isinstance(options, list):
        return [strip_secrets(v, depth + 1) for v in options]
    return options


def _regex_errors(values: Any, field: str, errors: Dict[str, str]) -> None:
    lines = values.splitlines() if isinstance(values, str) else (values or [])
    for i, line in enumerate(lines):
        line = str(line).strip()
        if not line:
            continue
        try:
            re.compile(line)
        except re.error as ex:
            errors[field] = f"Line {i + 1}: invalid regex ({ex})."
            return


# Build 4 crawl options (PLAN4_build.md 10.7): (lowest, highest) when crawler.config.validate_options is missing
_BUILD4_RANGES: Dict[str, Tuple[float, float]] = {
    "near_dup_threshold": (50, 100), "max_redirects": (1, 50), "max_sitemap_files": (1, 1000),
    "max_sitemap_only_urls": (0, 10_000_000), "max_sitemap_index_urls": (1, 10_000_000),
    "a11y_audit_max_pages": (1, 5000), "a11y_audit_max_minutes": (1, 240), "render_timeout": (2, 30)}
_BUILD4_CHOICES: Dict[str, Tuple[str, ...]] = {"block_addresses": ("", "metadata", "private"),
                                               "cookie_storage": ("session", "none"),
                                               "render_device": ("desktop", "mobile", "tablet")}
_LIMIT_KEYS = ("per_depth", "folder_depth", "query_strings", "per_host")


def _build4_option_errors(options: Dict[str, Any]) -> Dict[str, str]:
    """Plain errors for the Build 4 crawl options: crawler.config.validate_options (the crawl's own checks) when
    it is there, plus the range / choice limits below (so a profile is checked even without it)."""
    e: Dict[str, str] = {}
    try:
        from .config import validate_options
        e.update(validate_options(options) or {})
    except (ImportError, AttributeError):
        pass
    except Exception as ex:          # never let a newer check break saving a profile silently
        e.setdefault("options", f"The options could not be checked ({type(ex).__name__}).")
    for f, (lo, hi) in _BUILD4_RANGES.items():
        if f in e or options.get(f) in (None, ""):
            continue
        try:
            n = float(options[f])
        except (TypeError, ValueError):
            e[f] = "Must be a number."
            continue
        if n != n or n < lo or n > hi:
            e[f] = f"Must be between {lo:,g} and {hi:,g}."
    for f, allowed in _BUILD4_CHOICES.items():
        if f not in e and f in options and options[f] is not None \
                and str(options[f]).strip().lower() not in allowed:
            e[f] = "Must be one of " + ", ".join(repr(a) for a in allowed) + "."
    lim = options.get("limits")
    if "limits" not in e and lim not in (None, ""):
        if not isinstance(lim, dict) or any(k not in _LIMIT_KEYS for k in lim):
            e["limits"] = "Unknown limit; use " + ", ".join(_LIMIT_KEYS) + "."
        else:
            for k, v in lim.items():
                if v in (None, ""):
                    continue
                try:
                    n = float(v)
                except (TypeError, ValueError):
                    e["limits"] = f"{k.replace('_', ' ')}: Must be a number."
                    break
                if n != n or n < 0 or n > 10_000_000:
                    e["limits"] = f"{k.replace('_', ' ')}: Must be between 0 and 10,000,000."
                    break
    return e


def validate_profile(options: Any) -> Dict[str, str]:
    """Plain errors for the options a profile carries ({} = valid). Unknown options are allowed (a newer
    dashboard may send more); known ones are checked."""
    e: Dict[str, str] = {}
    if not isinstance(options, dict):
        return {"options": "The profile options must be an object."}
    try:
        size = len(json.dumps(options, default=str))
    except (TypeError, ValueError):
        return {"options": "The profile options cannot be stored."}
    if size > MAX_PROFILE_BYTES:
        return {"options": "The profile is too large."}
    _regex_errors(options.get("include_patterns"), "include_patterns", e)
    _regex_errors(options.get("exclude_regex"), "exclude_regex", e)
    rw = options.get("url_rewrite")
    if isinstance(rw, dict):
        for i, rule in enumerate(rw.get("regex_replace") or []):
            try:
                re.compile(str((rule or {}).get("regex") or ""))
            except re.error as ex:
                e["url_rewrite"] = f"Regex Replace rule {i + 1}: invalid regex ({ex})."
                break
    limits = {"max_urls": (0, 10_000_000), "max_depth": (0, 1000), "concurrency": (1, 50), "timeout": (1, 300),
              "delay": (0, 600), "max_urls_per_second": (0, 1000), "max_page_size_kb": (0, 1_000_000),
              "max_links_per_page": (0, 1_000_000), "list_max_depth": (0, 1000), "bot_check_sample": (1, 50)}
    for f, (lo, hi) in limits.items():
        if f in options and options[f] not in (None, ""):
            try:
                n = float(options[f])
            except (TypeError, ValueError):
                e[f] = "Must be a number."
                continue
            if n < lo or n > hi:
                e[f] = f"Must be between {lo:,} and {hi:,}."
    for f in ("custom_searches", "custom_extractors", "custom_js"):
        if isinstance(options.get(f), list) and len(options[f]) > 10:
            e[f] = "At most 10 rules."
    for f, msg in _build4_option_errors(options).items():
        e.setdefault(f, msg)
    if isinstance(options.get("segments"), list):
        try:
            from .segments import validate_segments
            _clean, seg_errors = validate_segments(options["segments"])
            if seg_errors:
                e["segments"] = "; ".join(f"Segment {k}: {', '.join(v.values())}" for k, v in
                                          list(seg_errors.items())[:3])
        except ImportError:
            pass
    return e


def _profile_path(name_or_slug: str, reports_dir: Optional[str]) -> Optional[str]:
    s = DEFAULT_PROFILE if name_or_slug == DEFAULT_PROFILE else slug(name_or_slug)
    if not s:
        return None
    return os.path.join(_profiles_dir(reports_dir), f"{s}.json")


def _profile_doc(name: str, options: Dict[str, Any]) -> Dict[str, Any]:
    return {"app": APP, "profile_version": PROFILE_VERSION, "name": name, "updated_at": _now(),
            "options": strip_secrets(options)}


def list_profiles(reports_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    folder = _profiles_dir(reports_dir)
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith(".json") and not n.startswith("."))
    except OSError:
        return []
    default = _read_json(os.path.join(folder, DEFAULT_PROFILE + ".json"))
    default_name = default.get("name") if isinstance(default, dict) else None
    out = []
    for n in names:
        if n == DEFAULT_PROFILE + ".json":
            continue
        doc = _read_json(os.path.join(folder, n))
        if not isinstance(doc, dict) or doc.get("app") != APP:
            continue
        out.append({"name": doc.get("name") or n[:-5], "slug": n[:-5], "updated_at": doc.get("updated_at") or "",
                    "default": bool(default_name and default_name == doc.get("name"))})
    out.sort(key=lambda p: p["name"].lower())
    return out


def save_profile(name: str, options: Dict[str, Any], reports_dir: Optional[str] = None
                 ) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    """Saves (or replaces) a named profile. Returns (meta, {}) or (None, errors)."""
    name = clean_profile_name(name)
    if not name or not _PROFILE_NAME.match(name) or name.startswith("_") or not slug(name):
        return None, {"name": "Use 1-60 letters, digits, spaces, dots, dashes, brackets or underscores "
                              "(not starting with _)."}
    errors = validate_profile(options)
    if errors:
        return None, errors
    path = _profile_path(name, reports_dir)
    with _lock:
        if not os.path.isfile(path) and len(list_profiles(reports_dir)) >= MAX_PROFILES:
            return None, {"name": f"At most {MAX_PROFILES} profiles."}
        doc = _profile_doc(name, options)
        _write_json(path, doc)
    return {"name": name, "slug": slug(name), "updated_at": doc["updated_at"]}, {}


def load_profile(name: str, reports_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = _profile_path(name, reports_dir)
    doc = _read_json(path) if path else None
    if not isinstance(doc, dict) or doc.get("app") != APP or not isinstance(doc.get("options"), dict):
        return None
    doc["options"] = strip_secrets(doc["options"])
    return doc


def profile_exists(name: str, reports_dir: Optional[str] = None) -> bool:
    return load_profile(name, reports_dir) is not None


def delete_profile(name: str, reports_dir: Optional[str] = None) -> bool:
    path = _profile_path(name, reports_dir)
    if not path or name == DEFAULT_PROFILE:
        return False
    with _lock:
        try:
            os.remove(path)
            return True
        except OSError:
            return False


def get_default(reports_dir: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The server default profile (applied when a dashboard opens without saved options), or None."""
    return load_profile(DEFAULT_PROFILE, reports_dir)


def set_default(options: Dict[str, Any], reports_dir: Optional[str] = None, name: str = "Default"
                ) -> Dict[str, str]:
    """Save Current Configuration as Default. Returns {} or errors."""
    errors = validate_profile(options)
    if errors:
        return errors
    with _lock:
        _write_json(_profile_path(DEFAULT_PROFILE, reports_dir), _profile_doc(clean_profile_name(name) or "Default",
                                                                            options))
    return {}


def clear_default(reports_dir: Optional[str] = None) -> bool:
    with _lock:
        try:
            os.remove(_profile_path(DEFAULT_PROFILE, reports_dir))
            return True
        except OSError:
            return False


def export_profile(name: str, reports_dir: Optional[str] = None) -> Optional[Tuple[str, bytes]]:
    doc = load_profile(name, reports_dir)
    if doc is None:
        return None
    fname = f"site-auditor-profile_{slug(doc.get('name') or name) or 'profile'}.json"
    return fname, json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")


def import_profile(data: Any, name: Optional[str] = None, reports_dir: Optional[str] = None
                   ) -> Tuple[Optional[Dict[str, Any]], Dict[str, str]]:
    """Adds a profile from an exported .json (bytes, text or dict). A bare options object is accepted too.
    Secrets inside are dropped. Returns (meta, {}) or (None, errors)."""
    if isinstance(data, (bytes, bytearray)):
        if len(data) > MAX_PROFILE_BYTES:
            return None, {"file": "The file is too large for a profile."}
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None, {"file": "The file is not text (expected a .json profile)."}
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None, {"file": "The file is not valid JSON."}
    if not isinstance(data, dict):
        return None, {"file": "The file does not hold a profile."}
    if "options" in data:
        if data.get("app") not in (None, APP):
            return None, {"file": "This profile was made by another application."}
        try:
            version = int(data.get("profile_version") or 1)
        except (TypeError, ValueError):
            version = 0
        if version < 1 or version > PROFILE_VERSION:
            return None, {"file": f"Unsupported profile version {data.get('profile_version')}."}
        options = data.get("options")
        name = name or data.get("name")
    else:
        options = data
    if not isinstance(options, dict):
        return None, {"file": "The profile has no options."}
    return save_profile(name or "Imported profile", options, reports_dir)
