"""
Google Sheets (kind "google"): export a tab view, the issues overview, a report or a bulk export to a new Google
Sheet, and read a URL list from a Sheet for Mode > List and scheduled crawls (crawler/integrations/sheets.py does the
work; this card shows its state and settings).

It uses the Google sign-in of Configuration > API Access with the Sheets permissions (connect_flow
"api_access:sheets": Connect with Google, source "sheets"). An account that signed in before has no Sheets
permission: the card says "Reconnect Google to allow Sheets". Disconnect here only stops ARACHNIX using Sheets; the
Google account itself is removed in Configuration > API Access.

Provider documents: see crawler/integrations/sheets.py (Sheets API v4 and Drive API v3, read 2026-10-28).
"""
from typing import Any, Dict

from .adapters import _Adapter
from .base import ConnectorError, Field, Status, TestResult
from .registry import register


class GoogleSheets(_Adapter):
    id = "google_sheets"
    name = "Google Sheets"
    kind = "google"
    icon = "sheets"
    connect_flow = "api_access:sheets"
    sends = ["urls", "titles"]
    description = ("Export tabs, issues and reports to a new Google Sheet in your Drive, and read URL lists from a "
                   "Sheet for List mode and scheduled crawls.")
    docs = ("https://developers.google.com/workspace/sheets/api/reference/rest and "
            "https://developers.google.com/workspace/drive/api/reference/rest/v3/permissions/create (read 2026-10-28)")
    fields = [
        Field("share_with", "Share new Sheets with", "list",
              help="Optional email addresses that can edit each new Sheet (required when the Google account is a "
                   "service account)."),
        Field("account", "Google account", "text",
              help="Optional: the email of the connected Google account to use; empty = the one that allowed Sheets."),
    ]

    def _sheets(self):
        from ..integrations import sheets
        return sheets

    def live(self) -> Dict[str, Any]:
        if self.meta().get("disconnected"):
            return {"connected": False, "reason": "Turned off here: connect Google Sheets again to use it."}
        try:
            c = self._sheets().connection(str(self.config().get("account") or ""))
        except Exception as e:
            return {"connected": False, "reason": f"The connection could not be read ({type(e).__name__})."}
        acc = c.get("account") or {}
        return {"connected": bool(c.get("connected")), "reason": str(c.get("reason") or ""),
                "needs_reconnect": bool(c.get("needs_reconnect")), "account": acc.get("email") or "",
                "account_kind": acc.get("kind") or ""}

    def public_config(self) -> Dict[str, Any]:
        cfg = self.config()
        live = self.live()
        return {"share_with": list(cfg.get("share_with") or []), "account": cfg.get("account") or "",
                "using": live.get("account") or "", "account_kind": live.get("account_kind") or ""}

    def connected_detail(self) -> str:
        live = self.live()
        return "Connected" + (f" - {live['account']}" if live.get("account") else "")

    def validate(self, config, secrets):
        from .. import settings_store
        clean, changes, errors = super().validate(config, secrets)
        ok, bad = settings_store.parse_emails(clean.get("share_with") or [])
        if bad and "share_with" not in errors:
            errors["share_with"] = "Not valid email addresses: " + ", ".join(bad[:3])
        clean["share_with"] = ok[:20]
        clean["account"] = str(clean.get("account") or "").strip()
        return clean, changes, errors

    def connect(self, config: Dict[str, Any], secrets: Dict[str, Any]) -> Status:
        """Saves the settings and turns Sheets on. The Google sign-in itself happens in Configuration > API Access
        (the status says "Reconnect Google to allow Sheets" until the account allowed it)."""
        clean, _changes, errors = self.validate(config, secrets)
        if errors:
            raise ConnectorError(next(iter(errors.values())), "invalid", errors=errors)
        self._store().save_config(self.id, clean)
        self._set_meta(disconnected=None, connected=True, tested_live=None, last_test=None)
        self.on_change()
        return self.status()

    def disconnect(self) -> Status:
        """Stops using Sheets (the Google account stays for Search Console and GA4)."""
        self._store().delete(self.id)
        self._set_meta(disconnected=True)
        self.on_change()
        return self.status()

    def _test(self) -> TestResult:
        from ..integrations.common import IntegrationError
        try:
            res = self._sheets().check(str(self.config().get("account") or ""))
        except IntegrationError as e:
            return TestResult(False, str(e))
        return TestResult(bool(res.get("ok")), str(res.get("detail") or ""))


register(GoogleSheets())
