"""
Adapters: the connections ARACHNIX had before the Connections page, listed there with the same status / test /
disconnect as every other connector, but kept by their own modules (nothing moves):

  google_search_console, google_analytics   crawler/integrations (Google sign-in in Configuration > API Access;
                                            connect() points there: connect_flow "api_access:gsc" / "api_access:ga4")
  pagespeed                                 crawler/integrations (API key; connect() saves it like the API Access
                                            dialog does)
  email_smtp                                Settings > Notifications (crawler/settings_store + crawler/notify)

Test makes one real call: Search Console lists the account's properties, GA4 lists its accounts, PageSpeed runs
one small test of TEST_URL (one unit of the 25,000 a day), Email connects and signs in without sending. The test
result is remembered in <reports_dir>/connectors/<id>.json like any connector's.
"""
from typing import Any, Dict, List

from .base import Connector, ConnectorError, Field, Masker, Status, TestResult, NOT_TESTED
from .registry import register

PSI_TEST_URL = "https://www.google.com/"


def _integrations():
    from .. import integrations
    return integrations


class _Adapter(Connector):
    """A connector whose settings live in an existing module: state comes from there."""

    def live(self) -> Dict[str, Any]:
        return {}

    def is_set_up(self) -> bool:
        return bool(self.live().get("connected"))

    def secret_flags(self) -> Dict[str, bool]:
        return {}

    def secret_values(self) -> List[str]:
        return []

    def masker(self) -> Masker:
        return Masker([v for v in self.secret_values() if v])

    def public_config(self) -> Dict[str, Any]:
        return {}

    def connected_detail(self) -> str:
        return "Connected"

    def status(self) -> Status:
        live = self.live()
        meta = self.meta()
        mask = self.masker()
        last_test = meta.get("last_test") if isinstance(meta.get("last_test"), dict) else None
        if last_test:
            last_test = dict(last_test, detail=mask(last_test.get("detail")))
        tested = bool(meta.get("tested_live")) or bool(live.get("used_live"))
        base = dict(secrets=self.secret_flags(), config=self.public_config(), last_test=last_test,
                    tested_live=tested)
        if live.get("needs_reconnect"):
            return Status("needs_reconnect", mask(live.get("reason") or "Connect again."), **base)
        if not live.get("connected"):
            return Status("not_connected", mask(live.get("reason") or "Not connected"), **base)
        if last_test and not last_test.get("ok"):
            return Status("error", f"The last test failed: {last_test.get('detail')}", **base)
        if not tested:
            return Status("connected", NOT_TESTED, **base)
        return Status("connected", mask(self.connected_detail()), **base)


# ---------------------------------------------------------------------------------------------
# Google Search Console / Google Analytics 4
# ---------------------------------------------------------------------------------------------
class _GoogleAdapter(_Adapter):
    source = ""
    kind = "google"
    sends = ["urls"]

    def live(self) -> Dict[str, Any]:
        try:
            c = _integrations().connection(self.source)
        except Exception as e:
            return {"connected": False, "reason": f"The connection could not be read ({type(e).__name__})."}
        acc = c.get("account") or {}
        reason = str(c.get("reason") or "")
        return {"connected": bool(c.get("connected")), "reason": reason,
                "needs_reconnect": bool(acc.get("needs_reconnect")),
                "used_live": bool(acc.get("last_used_at")), "account": acc.get("email") or acc.get("name") or "",
                "property": ((c.get("selection") or {}).get("property_name") or (c.get("selection") or {}).get("property")
                             or "")}

    def public_config(self) -> Dict[str, Any]:
        live = self.live()
        return {"account": live.get("account") or "", "property": live.get("property") or ""}

    def connected_detail(self) -> str:
        live = self.live()
        parts = [p for p in (live.get("account"), live.get("property")) if p]
        return "Connected" + (f" - {' · '.join(parts)}" if parts else "")

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        raise ConnectorError(f"{self.name} connects with a Google sign-in: open Configuration > API Access > "
                             f"{self.name}.", "invalid")

    def disconnect(self) -> Status:
        """Clears this source's property choice (the Google account stays for the other source; remove the
        account itself in Configuration > API Access)."""
        integ = _integrations()
        integ.store.set_selection(self.source, {})
        self._store().delete(self.id)
        integ._changed()
        return self.status()


class GoogleSearchConsole(_GoogleAdapter):
    id = "google_search_console"
    name = "Google Search Console"
    source = "gsc"
    icon = "search_console"
    connect_flow = "api_access:gsc"
    description = "Clicks, impressions and Google's index status for the crawled pages."
    docs = "https://developers.google.com/webmaster-tools/v1/api_reference_index (read 2026-10-28)"

    def _test(self) -> TestResult:
        integ = _integrations()
        try:
            props = integ.gsc_properties()
        except integ.IntegrationError as e:
            return TestResult(False, f"Google answered: {e}")
        chosen = self.live().get("property") or ""
        names = {p.get("site_url") for p in props}
        if chosen and chosen not in names:
            return TestResult(False, f"Google lists {len(props)} properties for this account, but not the chosen "
                                     "one: choose it again in Configuration > API Access.")
        return TestResult(True, f"Google answered: {len(props)} Search Console "
                                f"propert{'y' if len(props) == 1 else 'ies'} for this account.")


class GoogleAnalytics(_GoogleAdapter):
    id = "google_analytics"
    name = "Google Analytics 4"
    source = "ga4"
    icon = "analytics"
    connect_flow = "api_access:ga4"
    description = "Sessions, engagement and conversions for the crawled pages."
    docs = "https://developers.google.com/analytics/devguides/config/admin/v1 (read 2026-10-28)"

    def _test(self) -> TestResult:
        integ = _integrations()
        try:
            accounts = integ.ga4_accounts()
        except integ.IntegrationError as e:
            return TestResult(False, f"Google answered: {e}")
        props = sum(len(a.get("properties") or []) for a in accounts)
        return TestResult(True, f"Google answered: {len(accounts)} Analytics account(s) with {props} GA4 "
                                f"propert{'y' if props == 1 else 'ies'}.")


# ---------------------------------------------------------------------------------------------
# PageSpeed Insights
# ---------------------------------------------------------------------------------------------
class PageSpeed(_Adapter):
    id = "pagespeed"
    name = "PageSpeed Insights"
    kind = "performance"
    icon = "speed"
    connect_flow = "api_access:psi"
    sends = ["urls"]
    description = "Core Web Vitals and Lighthouse lab data for the crawled pages (a free Google API key)."
    docs = "https://developers.google.com/speed/docs/insights/v5/get-started (read 2026-10-28)"
    fields = [
        Field("api_key", "API key", "password", required=True, secret=True,
              help="A free PageSpeed Insights API key from Google Cloud (env PSI_API_KEY wins when it is set)."),
        Field("auto_connect", "Run after every crawl", "bool", default=False,
              help="Test the crawled pages automatically when a crawl finishes."),
    ]

    def live(self) -> Dict[str, Any]:
        try:
            c = _integrations().connection("psi")
        except Exception as e:
            return {"connected": False, "reason": f"The connection could not be read ({type(e).__name__})."}
        return {"connected": bool(c.get("connected")), "reason": str(c.get("reason") or ""),
                "key": c.get("key") or {}, "auto_connect": bool(c.get("auto_connect"))}

    def secret_flags(self) -> Dict[str, bool]:
        return {"api_key": bool((self.live().get("key") or {}).get("set"))}

    def secret_values(self) -> List[str]:
        try:
            key, _ = _integrations().store.psi_key()
            return [key] if key else []
        except Exception:
            return []

    def public_config(self) -> Dict[str, Any]:
        live = self.live()
        return {"auto_connect": bool(live.get("auto_connect")), "key_source": (live.get("key") or {}).get("source", "")}

    def connected_detail(self) -> str:
        src = (self.live().get("key") or {}).get("source")
        return "Connected - key from the server environment" if src == "env" else "Connected - key saved on the server"

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        integ = _integrations()
        key = (secrets or {}).get("api_key", (config or {}).get("api_key"))
        key = "" if isinstance(key, dict) or key in (None, "***") else str(key)
        if not key and not self.secret_flags().get("api_key"):
            raise ConnectorError("API key is required.", "invalid", errors={"api_key": "API key is required."})
        auto = (config or {}).get("auto_connect")
        try:
            integ.psi_connect(key, None if auto is None else bool(auto))
        except integ.IntegrationError as e:
            raise ConnectorError(Masker([key])(str(e)), "invalid", errors={"api_key": Masker([key])(str(e))})
        self._set_meta(tested_live=None, last_test=None)
        return self.status()

    def disconnect(self) -> Status:
        _integrations().psi_disconnect()
        self._store().delete(self.id)
        return self.status()

    def _test(self) -> TestResult:
        from ..integrations import psi
        from ..integrations.common import IntegrationError, new_session
        key, _ = _integrations().store.psi_key()
        opts = psi.options({"categories": ["performance"], "device": "mobile"})
        session = new_session()
        try:
            res = psi.fetch_one(session, PSI_TEST_URL, opts, key)
        except IntegrationError as e:
            return TestResult(False, str(e))
        finally:
            session.close()
        if res.get("status") == "success":
            score = (res.get("scores") or {}).get("performance")
            return TestResult(True, "PageSpeed Insights answered with a Lighthouse result"
                                    + (f" (performance {score})." if score is not None else "."))
        return TestResult(False, f"PageSpeed Insights could not test the sample page: {res.get('message') or 'no detail'}")


# ---------------------------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------------------------
class EmailSmtp(_Adapter):
    id = "email_smtp"
    name = "Email (SMTP)"
    kind = "notify"
    icon = "email"
    connect_flow = "settings:notifications"
    sends = ["counts", "urls"]
    description = "Crawl summaries and alerts by email through your own mail server."
    docs = "https://www.rfc-editor.org/rfc/rfc5321 (SMTP; read 2026-10-28)"
    fields = [
        Field("smtp_host", "SMTP server", "text", required=True, help="Host name of your mail server."),
        Field("smtp_port", "Port", "number", default=587, min=1, max=65535),
        Field("encryption", "Encryption", "select", options=["tls", "ssl", "none"], default="tls",
              option_labels={"tls": "STARTTLS (587)", "ssl": "SSL (465)", "none": "None"}),
        Field("smtp_username", "Username", "text"),
        Field("smtp_password", "Password", "password", secret=True),
        Field("from_address", "From address", "text"),
    ]

    def _settings(self):
        from .. import settings_store
        return settings_store

    def live(self) -> Dict[str, Any]:
        from .. import notify
        st = notify.status()
        if not st.get("configured"):
            return {"connected": False, "reason": "Not connected - enter your mail server here or under Settings > "
                                                  "Notifications."}
        return {"connected": True, "host": st.get("host"), "port": st.get("port"), "encryption": st.get("encryption"),
                "from": st.get("from"), "auth": st.get("auth")}

    def secret_flags(self) -> Dict[str, bool]:
        page = self._settings().get_page("notifications", True)
        flag = page.get("smtp_password")
        return {"smtp_password": bool(flag.get("set")) if isinstance(flag, dict) else bool(flag)}

    def secret_values(self) -> List[str]:
        try:
            return [str(self._settings().effective("notifications").get("smtp_password") or "")]
        except Exception:
            return []

    def public_config(self) -> Dict[str, Any]:
        page = self._settings().get_page("notifications", True)
        return {k: page.get(k) for k in ("smtp_host", "smtp_port", "encryption", "smtp_username", "from_address")}

    def connected_detail(self) -> str:
        live = self.live()
        return f"Connected - {live.get('host')}:{live.get('port')}"

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        values = {k: v for k, v in (config or {}).items() if k in {f.key for f in self.fields} and not
                  self._field(k).secret}
        pw = (secrets or {}).get("smtp_password", (config or {}).get("smtp_password"))
        if pw is not None:
            values["smtp_password"] = pw
        if not str(values.get("smtp_host") or self.public_config().get("smtp_host") or "").strip():
            raise ConnectorError("SMTP server is required.", "invalid", errors={"smtp_host": "SMTP server is required."})
        errors = self._settings().update("notifications", values)
        if errors:
            mask = Masker([str(pw)] if isinstance(pw, str) else [])
            errors = {k: mask(v) for k, v in errors.items()}
            raise ConnectorError(next(iter(errors.values())), "invalid", errors=errors)
        self._set_meta(tested_live=None, last_test=None)
        return self.status()

    def disconnect(self) -> Status:
        """Removes the mail server, its sign-in and Email on Crawl Complete (env settings stay)."""
        self._settings().update("notifications", {"smtp_host": "", "smtp_username": "", "smtp_password": {"clear": True},
                                                  "email_on_complete": False})
        self._store().delete(self.id)
        return self.status()

    def _test(self) -> TestResult:
        from .. import notify
        res = notify.check_smtp()
        return TestResult(bool(res.get("ok")), res.get("detail") if res.get("ok") else res.get("error") or "Failed.")


for _c in (GoogleSearchConsole(), GoogleAnalytics(), PageSpeed(), EmailSmtp()):
    register(_c)
