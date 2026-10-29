"""
Link metrics (kind "link_metrics"): Moz, Majestic and Ahrefs, each with the team's own API key. They fill the Link
Metrics tab (crawler/seo/tabs/link_metrics.py) through the fetch job of crawler/integrations/linkmetrics.py, which
counts every unit against an admin-set monthly cap. Keys live in the existing link-metrics key slots
(integrations/store.py keys.json, env MOZ_TOKEN / MAJESTIC_TOKEN / AHREFS_TOKEN wins), never in a second copy.

Provider documents (request and response shapes, read 2026-10-28):
  Moz       Links API v2 URL Metrics, https://moz.com/api/docs/links/url-metrics (moz.com does not allow automated
            reading; checked against the jentic.com copy of Moz's OpenAPI): POST https://lsapi.seomoz.com/v2/url_metrics
            {"targets": [url, ...]} (at most 50), HTTP Basic with Access ID : Secret Key, or the x-moz-token header
            with an API token; answer {"results": [{"page", "page_authority", "domain_authority", "spam_score",
            "root_domains_to_page", "external_pages_to_page", ...}]} in target order; one row of the monthly quota
            per target.
  Majestic  GetIndexItemInfo, https://developer-support.majestic.com/api/commands/get-index-item-info.shtml:
            POST https://api.majestic.com/api/json cmd=GetIndexItemInfo, app_api_key, items (1-100), item0..itemN,
            datasource fresh | historic; answer {"Code", "ErrorMessage", "DataTables": {"Results": {"Data": [{"ItemNum",
            "Item", "ResultCode", "Status", "ExtBackLinks", "RefDomains", "CitationFlow", "TrustFlow"}]}}}; one
            IndexItemInfo unit per item. GetSubscriptionInfo (no units): get-subscription-info.shtml.
  Ahrefs    API v3 Batch Analysis, https://docs.ahrefs.com/en/api/reference/batch-analysis/post-batch-analysis:
            POST https://api.ahrefs.com/v3/batch-analysis/batch-analysis, Authorization: Bearer <key>, {"select": [...],
            "targets": [{"url", "mode": "exact", "protocol": "both"}]} (1-100); answer {"targets": [{"url",
            "url_rating", "domain_rating", "backlinks", "refdomains", "org_traffic", ...}]}. Units:
            https://docs.ahrefs.com/api/docs/limits-consumption - max(50, rows x per-row cost), a field costs 1 unit
            (refdomains 5, org_traffic 10); the answer's x-api-units-cost-total header gives the real cost.
            GET https://api.ahrefs.com/v3/subscription-info/limits-and-usage (free) for Test.
Each fetch() raises LinkMetricsError (an HTTP error answer: "<provider> answered <status>: ...") or ConnectorError
(network: kind + host only); messages never carry a key.
"""
import base64
from typing import Any, Dict, List, Optional, Tuple

from .base import Connector, ConnectorError, Field, Masker, Status, TestResult, now_iso
from .http import http_error, safe_request
from .registry import register

TIMEOUT = (5.0, 45.0)
MAX_ANSWER = 5_000_000
METRICS: Dict[str, List[Tuple[str, str]]] = {
    "moz": [("page_authority", "Page Authority"), ("domain_authority", "Domain Authority"),
            ("spam_score", "Spam Score"), ("linking_root_domains", "Linking Root Domains"),
            ("external_links", "External Links")],
    "majestic": [("trust_flow", "Trust Flow"), ("citation_flow", "Citation Flow"),
                 ("external_backlinks", "External Backlinks"), ("referring_domains", "Referring Domains")],
    "ahrefs": [("url_rating", "URL Rating"), ("domain_rating", "Domain Rating"), ("backlinks", "Backlinks"),
               ("referring_domains", "Referring Domains"), ("organic_traffic", "Organic Traffic")],
}
NAMES = {"moz": "Moz", "majestic": "Majestic", "ahrefs": "Ahrefs"}
AHREFS_FIELDS = {"url": ("url", 1), "url_rating": ("url_rating", 1), "domain_rating": ("domain_rating", 1),
                 "backlinks": ("backlinks", 1), "referring_domains": ("refdomains", 5),
                 "organic_traffic": ("org_traffic", 10)}
AHREFS_MIN_COST = 50


class LinkMetricsError(Exception):
    def __init__(self, message: str, status: int = 0, stop: bool = False):
        super().__init__(message)
        self.status = status
        self.stop = stop            # no point going on (bad key, out of units, rate limited)


def _num(v: Any) -> Optional[float]:
    """A metric value: a number >= 0, else None (providers use -1 or null for 'unknown')."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    if n != n or n < 0:
        return None
    return int(n) if n.is_integer() else round(n, 2)


class _LinkProvider(Connector):
    kind = "link_metrics"
    sends = ["urls"]
    provider = ""
    batch = 50
    default_cap = 1000
    unit_label = "units"

    def __init__(self, instance_id: str = "", instance_name: str = ""):
        super().__init__(instance_id, instance_name)

    # --- the key lives in the integrations key slot ---------------------------------------------------
    def _slots(self):
        from ..integrations import store
        return store

    def secret(self, key: str) -> str:
        if key == "api_key":
            return self._slots().link_key(self.provider)
        return super().secret(key)

    def secret_flags(self) -> Dict[str, bool]:
        return {"api_key": bool(self._slots().link_key(self.provider))}

    def masker(self) -> Masker:
        key = self._slots().link_key(self.provider)
        values = [key] + ([p for p in key.split(":") if len(p) >= 6] if ":" in key else [])
        if ":" in key:
            values.append(base64.b64encode(key.encode("utf-8")).decode("ascii"))
        return Masker([v for v in values if v])

    def key_source(self) -> str:
        return self._slots().link_key_source(self.provider)

    def is_set_up(self) -> bool:
        if not self.secret("api_key"):
            return False
        return bool(self.meta().get("connected")) or self.key_source() == "env"

    def validate(self, config, secrets):
        clean, changes, errors = super().validate(config, secrets)
        if "api_key" in errors and "api_key" not in changes and self.secret("api_key"):
            errors.pop("api_key")
        return clean, changes, errors

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        with self._lock:
            clean, changes, errors = self.validate(config, secrets)
            new_key = changes.get("api_key") if "api_key" in changes else None
            if not errors:
                errors.update(self.check(clean, {"api_key": new_key if new_key is not None else
                                                 self.secret("api_key")}))
            if errors:
                mask = Masker([new_key] if new_key else [])
                errors = {k: mask(self.masker()(v)) for k, v in errors.items()}
                raise ConnectorError(next(iter(errors.values())), "invalid", errors=errors)
            before = self.stored()
            changed = (before.get("config") or {}) != clean or (
                "api_key" in changes and (changes["api_key"] or "") != self.secret("api_key"))
            self._store().save_config(self.id, clean)
            if "api_key" in changes:
                self._slots().set_link_key(self.provider, changes["api_key"] or "")
            meta = {"connected": True, "connected_at": (before.get("meta") or {}).get("connected_at") or now_iso()}
            if changed or not (before.get("meta") or {}).get("connected"):
                meta.update(tested_live=False, last_test=None, last_delivery=None, updated_at=now_iso())
            self._set_meta(**meta)
            self.on_change()
            return self.status()

    def disconnect(self) -> Status:
        with self._lock:
            self._slots().set_link_key(self.provider, "")
            self._store().delete(self.id)
            self.on_change()
            st = self.status()
            if self.key_source() == "env":
                st.detail = (f"The {self.name} key from the server environment ({self.provider.upper()}_TOKEN) stays "
                             "in use; remove it there.")
            return st

    # --- units ----------------------------------------------------------------------------------------
    def unit_cap(self) -> int:
        try:
            return max(0, int(self.config().get("monthly_unit_cap") or 0))
        except (TypeError, ValueError):
            return 0

    def units_used(self) -> int:
        return self._slots().link_units_used(self.provider)

    def estimate_units(self, n_urls: int) -> int:
        """Units a fetch of n_urls URLs is expected to use (batched as fetch() does)."""
        return max(0, int(n_urls))

    def public_config(self) -> Dict[str, Any]:
        out = super().public_config()
        cap, used = self.unit_cap(), self.units_used()
        out.update(key_source=self.key_source(), units_used_this_month=used, monthly_unit_cap=cap,
                   units_left=max(0, cap - used), unit_label=self.unit_label,
                   metrics=[label for _k, label in METRICS[self.provider]])
        return out

    # --- calls ----------------------------------------------------------------------------------------
    def fetch(self, targets: List[str]) -> Tuple[List[Dict[str, Any]], int]:
        """Metrics for each target (in order): {metric key: value | None} or {"not_checked": reason}; and the
        units the call used."""
        raise NotImplementedError

    def _post(self, url: str, **kw) -> Any:
        key = self.secret("api_key")
        return safe_request("POST", url, timeout=TIMEOUT, max_bytes=MAX_ANSWER, secrets=[key] if key else [],
                            allow_private_network=self.allow_private(), **kw)

    def _error(self, resp, detail: str = "") -> LinkMetricsError:
        status = resp.status_code
        stop = status in (401, 402, 403, 429)
        return LinkMetricsError(http_error(self.name, resp, self.masker(), detail=detail), status, stop)


class Moz(_LinkProvider):
    id = "moz"
    name = "Moz"
    provider = "moz"
    icon = "moz"
    batch = 25
    default_cap = 500
    unit_label = "rows"
    endpoint = "https://lsapi.seomoz.com/v2/url_metrics"
    description = "Page Authority, Domain Authority, Spam Score and linking root domains per crawled page."
    docs = ("https://moz.com/api/docs/links/url-metrics (Links API v2 URL Metrics; read 2026-10-28 via the "
            "jentic.com copy of Moz's OpenAPI)")
    fields = [
        Field("api_key", "API token", "password", required=True, secret=True,
              help="Your Moz API token, or the older Access ID and Secret Key joined by a colon (id:secret)."),
        Field("monthly_unit_cap", "Monthly row cap", "number", default=500, min=1, max=10_000_000,
              help="ARACHNIX stops fetching when this many rows (one per URL) were used this month."),
    ]

    def _headers(self) -> Dict[str, str]:
        key = self.secret("api_key")
        if ":" in key:
            return {"Authorization": "Basic " + base64.b64encode(key.encode("utf-8")).decode("ascii")}
        return {"x-moz-token": key}

    def fetch(self, targets):
        resp = self._post(self.endpoint, json={"targets": list(targets)},
                          headers=dict(self._headers(), Accept="application/json"))
        if resp.status_code != 200:
            detail = ""
            try:
                body = resp.json()
                detail = str(body.get("message") or body.get("name") or "")[:160] if isinstance(body, dict) else ""
            except ValueError:
                pass
            raise self._error(resp, detail)
        try:
            data = resp.json()
        except ValueError:
            raise LinkMetricsError("Moz sent an answer that is not JSON.", resp.status_code)
        rows = data.get("results") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise LinkMetricsError("Moz sent an answer without results.", resp.status_code)
        out = []
        for i, _t in enumerate(targets):
            r = rows[i] if i < len(rows) and isinstance(rows[i], dict) else None
            if r is None:
                out.append({"not_checked": "Moz returned no row for this URL."})
                continue
            out.append({"page_authority": _num(r.get("page_authority")),
                        "domain_authority": _num(r.get("domain_authority")),
                        "spam_score": _num(r.get("spam_score")),
                        "linking_root_domains": _num(r.get("root_domains_to_page")),
                        "external_links": _num(r.get("external_pages_to_page"))})
        return out, len(targets)

    def _test(self) -> TestResult:
        try:
            rows, _units = self.fetch(["moz.com"])
        except LinkMetricsError as e:
            return TestResult(False, str(e))
        self._slots().link_units_add(self.provider, 1)
        da = (rows[0] or {}).get("domain_authority")
        return TestResult(True, f"Moz answered: Domain Authority {da if da is not None else 'not given'} for moz.com "
                                "(1 row of your monthly quota).")


class Majestic(_LinkProvider):
    id = "majestic"
    name = "Majestic"
    provider = "majestic"
    icon = "majestic"
    batch = 100
    default_cap = 1000
    unit_label = "Index Item Info units"
    description = "Trust Flow, Citation Flow, external backlinks and referring domains per crawled page."
    docs = ("https://developer-support.majestic.com/api/commands/get-index-item-info.shtml and "
            "get-subscription-info.shtml (read 2026-10-28)")
    fields = [
        Field("api_key", "API key", "password", required=True, secret=True,
              help="Your Majestic API key (Majestic > Account > API)."),
        Field("datasource", "Index", "select", options=["fresh", "historic"], default="fresh",
              option_labels={"fresh": "Fresh Index", "historic": "Historic Index"}),
        Field("monthly_unit_cap", "Monthly unit cap", "number", default=1000, min=1, max=10_000_000,
              help="ARACHNIX stops fetching when this many Index Item Info units (one per URL) were used this month."),
        Field("endpoint", "Server", "select", options=["api", "developer"], default="api",
              option_labels={"api": "api.majestic.com (live)", "developer": "developer.majestic.com (sandbox)"}),
    ]

    def url(self) -> str:
        host = "developer.majestic.com" if self.config().get("endpoint") == "developer" else "api.majestic.com"
        return f"https://{host}/api/json"

    def _call(self, form: Dict[str, Any]) -> Dict[str, Any]:
        form = dict(form, app_api_key=self.secret("api_key"))
        resp = self._post(self.url(), data=form, headers={"Accept": "application/json"})
        if resp.status_code != 200:
            raise self._error(resp)
        try:
            data = resp.json()
        except ValueError:
            raise LinkMetricsError("Majestic sent an answer that is not JSON.", resp.status_code)
        if not isinstance(data, dict):
            raise LinkMetricsError("Majestic sent an unexpected answer.", resp.status_code)
        code = str(data.get("Code") or "")
        if code != "OK":
            msg = self.masker()(str(data.get("ErrorMessage") or ""))[:160]
            stop = code in ("InvalidAPIKey", "InsufficientIndexItemInfoResUnits", "InsufficientResUnits",
                            "AccessDenied", "RateLimitExceeded")
            raise LinkMetricsError(f"Majestic answered {code or 'an error'}" + (f": {msg}" if msg else ""), 200, stop)
        return data

    def fetch(self, targets):
        form: Dict[str, Any] = {"cmd": "GetIndexItemInfo", "items": len(targets),
                                "datasource": self.config().get("datasource") or "fresh"}
        for i, t in enumerate(targets):
            form[f"item{i}"] = t
        data = self._call(form)
        rows = ((data.get("DataTables") or {}).get("Results") or {}).get("Data") or []
        by_num = {}
        for r in rows:
            if isinstance(r, dict):
                try:
                    by_num[int(r.get("ItemNum"))] = r
                except (TypeError, ValueError):
                    continue
        out = []
        for i, _t in enumerate(targets):
            r = by_num.get(i)
            if r is None:
                out.append({"not_checked": "Majestic returned no row for this URL."})
            elif str(r.get("ResultCode") or "OK") != "OK":
                out.append({"not_checked": f"Majestic answered {str(r.get('ResultCode'))[:60]} for this URL."})
            else:
                out.append({"trust_flow": _num(r.get("TrustFlow")), "citation_flow": _num(r.get("CitationFlow")),
                            "external_backlinks": _num(r.get("ExtBackLinks")),
                            "referring_domains": _num(r.get("RefDomains"))})
        return out, len(targets)

    def _test(self) -> TestResult:
        try:
            data = self._call({"cmd": "GetSubscriptionInfo", "datasource": self.config().get("datasource") or "fresh"})
        except LinkMetricsError as e:
            return TestResult(False, str(e))
        left = data.get("TotalIndexItemInfoResUnits")
        if left is None:
            subs = ((data.get("DataTables") or {}).get("Subscriptions") or {}).get("Data") or []
            left = (subs[0] or {}).get("IndexItemInfoResUnits") if subs and isinstance(subs[0], dict) else None
        return TestResult(True, "Majestic answered: " + (f"{int(left):,} Index Item Info units left on the "
                                                          "subscription." if _num(left) is not None else
                                                          "the key works.") + " (This test uses no units.)")


class Ahrefs(_LinkProvider):
    id = "ahrefs"
    name = "Ahrefs"
    provider = "ahrefs"
    icon = "ahrefs"
    batch = 100
    default_cap = 5000
    unit_label = "API units"
    endpoint = "https://api.ahrefs.com/v3/batch-analysis/batch-analysis"
    usage_url = "https://api.ahrefs.com/v3/subscription-info/limits-and-usage"
    description = "URL Rating, Domain Rating, backlinks, referring domains and organic traffic per crawled page."
    docs = ("https://docs.ahrefs.com/en/api/reference/batch-analysis/post-batch-analysis and "
            "https://docs.ahrefs.com/api/docs/limits-consumption (read 2026-10-28)")
    fields = [
        Field("api_key", "API key", "password", required=True, secret=True,
              help="An Ahrefs API v3 key (Account settings > API keys; needs a plan with API access)."),
        Field("organic_traffic", "Include organic traffic", "bool", default=False,
              help="Adds Organic Traffic (10 more units per URL)."),
        Field("monthly_unit_cap", "Monthly unit cap", "number", default=5000, min=50, max=100_000_000,
              help="ARACHNIX stops fetching when this many API units were used this month (at least 50 per request)."),
    ]

    def selected(self) -> List[str]:
        keys = ["url", "url_rating", "domain_rating", "backlinks", "referring_domains"]
        if self.config().get("organic_traffic"):
            keys.append("organic_traffic")
        return keys

    def row_cost(self) -> int:
        return sum(AHREFS_FIELDS[k][1] for k in self.selected())

    def estimate_units(self, n_urls: int) -> int:
        n = max(0, int(n_urls))
        if not n:
            return 0
        full, rest = divmod(n, self.batch)
        cost = full * max(AHREFS_MIN_COST, self.batch * self.row_cost())
        if rest:
            cost += max(AHREFS_MIN_COST, rest * self.row_cost())
        return cost

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.secret('api_key')}", "Accept": "application/json"}

    def fetch(self, targets):
        sel = self.selected()
        body = {"select": [AHREFS_FIELDS[k][0] for k in sel],
                "targets": [{"url": t, "mode": "exact", "protocol": "both"} for t in targets]}
        resp = self._post(self.endpoint, json=body, headers=self._headers())
        if resp.status_code != 200:
            detail = ""
            try:
                b = resp.json()
                detail = str(b.get("error") or "")[:160] if isinstance(b, dict) else ""
            except ValueError:
                pass
            raise self._error(resp, detail)
        try:
            data = resp.json()
        except ValueError:
            raise LinkMetricsError("Ahrefs sent an answer that is not JSON.", resp.status_code)
        rows = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise LinkMetricsError("Ahrefs sent an answer without targets.", resp.status_code)
        out = []
        for i, _t in enumerate(targets):
            r = rows[i] if i < len(rows) and isinstance(rows[i], dict) else None
            if r is None:
                out.append({"not_checked": "Ahrefs returned no row for this URL."})
                continue
            m = {"url_rating": _num(r.get("url_rating")), "domain_rating": _num(r.get("domain_rating")),
                 "backlinks": _num(r.get("backlinks")), "referring_domains": _num(r.get("refdomains"))}
            if "organic_traffic" in sel:
                m["organic_traffic"] = _num(r.get("org_traffic"))
            out.append(m)
        units = self.estimate_units(len(targets))
        try:
            units = int(float(resp.headers.get("x-api-units-cost-total") or units))
        except ValueError:
            pass
        return out, units

    def _test(self) -> TestResult:
        key = self.secret("api_key")
        resp = safe_request("GET", self.usage_url, headers=self._headers(), timeout=TIMEOUT, max_bytes=200_000,
                            secrets=[key] if key else [], allow_private_network=self.allow_private())
        if resp.status_code != 200:
            return TestResult(False, str(self._error(resp)))
        try:
            lu = (resp.json() or {}).get("limits_and_usage") or {}
        except (ValueError, AttributeError):
            lu = {}
        used, limit = lu.get("units_usage_workspace"), lu.get("units_limit_workspace")
        if _num(used) is not None and _num(limit) is not None:
            return TestResult(True, f"Ahrefs answered: {int(used):,} of {int(limit):,} API units used this billing "
                                    "month. (This test uses no units.)")
        return TestResult(True, "Ahrefs answered: the key works. (This test uses no units.)")


PROVIDERS = ("moz", "majestic", "ahrefs")
for _c in (Moz(), Majestic(), Ahrefs()):
    register(_c)
