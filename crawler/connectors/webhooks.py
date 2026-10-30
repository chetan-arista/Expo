"""
Chat and webhook notifications (kind "notify"): Slack, Microsoft Teams, Discord and generic webhooks (several, each
"webhook:<slug>"). Each sends the events ticked in its settings (crawl finished, crawl failed, new or returning
issues, alerts) as built by messages.py: counts that add up, issue names and the dashboard address only.

Provider documents (request and response shapes, read 2026-10-28):
  Slack     https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks : POST JSON {"text",
            "attachments"/"blocks"} to https://hooks.slack.com/services/<team>/<B id>/<secret>; 200 "ok"; errors
            are 400 / 403 / 404 / 410 with a short code (invalid_payload, invalid_token, no_service,
            channel_is_archived, action_prohibited ...).
  Discord   https://docs.discord.com/developers/resources/webhook : execute webhook POST /api/webhooks/<id>/<token>;
            the Slack-compatible endpoint is the same address + "/slack" (Slack's channel, icon_emoji, mrkdwn and
            mrkdwn_in are not supported); 429 with {"retry_after"} when rate limited.
  Teams     https://learn.microsoft.com/en-us/connectors/teams/ (Workflows "When a Teams webhook request is
            received"): POST {"type": "message", "attachments": [{"contentType":
            "application/vnd.microsoft.card.adaptive", "contentUrl": null, "content": <Adaptive Card>}]};
            the trigger answers 202 Accepted.
  Generic   ARACHNIX's own JSON (messages.generic_body). With a signing secret every request carries
            X-ARACHNIX-Timestamp and X-ARACHNIX-Signature: "sha256=" + hex HMAC-SHA256(secret, "<timestamp>.<body>").

The webhook address is a secret (it is the credential): stored in secrets.json, shown as {"set": true}, and masked
out of every error ("https://hooks.slack.com/***"). Calls go through http.safe_request (public https only unless
'Allow private network' is on for a generic webhook; no redirects to other sites; 64 KB answer cap; 5 s connect,
15 s read).
"""
import hashlib
import hmac
import json
import re
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from . import messages
from .base import Connector, ConnectorError, Field, TestResult
from .http import http_error, safe_request, short_body
from .registry import register

TIMEOUT = (5.0, 15.0)
MAX_ANSWER = 64 * 1024
# Hosts each provider's webhook addresses use (tests point them at a local catcher)
PROVIDER_HOSTS: Dict[str, Tuple[str, ...]] = {
    "slack": ("hooks.slack.com", "hooks.slack-gov.com"),
    "discord": ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"),
    "teams": (".logic.azure.com", ".api.powerplatform.com", ".webhook.office.com"),
}
SLACK_ERRORS = {
    "invalid_payload": "the message was not accepted (invalid payload)",
    "no_text": "the message had no text",
    "invalid_token": "the webhook address is no longer valid",
    "no_service": "the webhook was removed or turned off in Slack",
    "no_service_id": "the webhook address is not complete",
    "no_active_hooks": "the webhook is turned off in Slack",
    "no_team": "the Slack workspace was not found",
    "team_disabled": "the Slack workspace is no longer active",
    "channel_is_archived": "the channel is archived",
    "channel_not_found": "the channel was not found",
    "action_prohibited": "a Slack admin does not allow this app to post",
    "posting_to_general_channel_denied": "posting to #general is not allowed for this webhook",
    "too_many_attachments": "the message had too many attachments",
}
EVENT_FIELD = Field("events", "Send", "list", options=list(messages.EVENTS), default=list(messages.EVENTS),
                    option_labels=dict(messages.EVENT_LABELS),
                    help="Which events post a message here.")


def _host_ok(host: str, allowed: Tuple[str, ...]) -> bool:
    host = (host or "").lower()
    return any(host == a or (a.startswith(".") and host.endswith(a)) for a in allowed)


class _Notifier(Connector):
    """A connector that posts messages to a webhook address (the secret field "webhook_url" or "url")."""

    kind = "notify"
    sends = ["counts", "issue_names", "hostnames"]
    url_key = "webhook_url"
    provider = ""

    def events(self) -> List[str]:
        return [e for e in (self.config().get("events") or []) if e in messages.EVENTS]

    def off_reason(self) -> str:
        return "" if self.events() else "No event is ticked, so nothing is sent."

    def address(self) -> str:
        return self.secret(self.url_key)

    def target(self) -> str:
        return self.address()

    def body(self, event: str, s: Dict[str, Any], alert: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        raise NotImplementedError

    def test_body(self) -> Dict[str, Any]:
        raise NotImplementedError

    def check_address(self, url: str) -> str:
        """"" when `url` looks like this provider's webhook address, else the problem."""
        return ""

    def check(self, config: Dict[str, Any], secrets: Dict[str, str]) -> Dict[str, str]:
        from .http import check_url
        url = secrets.get(self.url_key) or ""
        why = self.check_address(url)
        if why:
            return {self.url_key: why}
        try:
            check_url(url, bool(config.get("allow_private_network")) or self.allow_private_default)
        except ConnectorError as e:
            return {self.url_key: str(e)}
        return {}

    def _post(self, body: Dict[str, Any]) -> Tuple[bool, str]:
        url = self.target()
        if not url:
            return False, "The webhook address is not set."
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(self.extra_headers(body, data))
        resp = safe_request("POST", url, data=data, headers=headers, allow_private_network=self.allow_private(),
                            timeout=TIMEOUT, max_bytes=MAX_ANSWER, secrets=[self.address()],
                            redirect_hosts=self.redirect_hosts)
        if resp.status_code == 429:
            wait = self._retry_after(resp)
            if wait is not None and wait <= 5:
                time.sleep(wait)
                resp = safe_request("POST", url, data=data, headers=headers,
                                    allow_private_network=self.allow_private(), timeout=TIMEOUT,
                                    max_bytes=MAX_ANSWER, secrets=[self.address()], redirect_hosts=self.redirect_hosts)
        if 200 <= resp.status_code < 300:
            return True, f"{self.provider} accepted the message ({resp.status_code})."
        return False, self.error_text(resp)

    @staticmethod
    def _retry_after(resp) -> Optional[float]:
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("retry_after") is not None:
                return float(data["retry_after"])
        except ValueError:
            pass
        try:
            return float(resp.headers.get("Retry-After") or "")
        except ValueError:
            return None

    def extra_headers(self, body: Dict[str, Any], data: bytes) -> Dict[str, str]:
        return {}

    def error_text(self, resp) -> str:
        return http_error(self.provider, resp, self.masker())

    def send(self, event: str, summary: Dict[str, Any], alert: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        """Posts one event (summary = messages.public_summary). (ok, plain detail); raises ConnectorError for
        network problems (kind + host only)."""
        return self._post(self.body(event, summary, alert))

    def _test(self) -> TestResult:
        ok, detail = self._post(self.test_body())
        return TestResult(ok, detail if not ok else f"{detail} Check the channel for the test message.")


class Slack(_Notifier):
    id = "slack"
    name = "Slack"
    icon = "slack"
    provider = "Slack"
    description = "Post crawl results, new issues and alerts to a Slack channel (incoming webhook)."
    docs = "https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks (read 2026-10-28)"
    fields = [
        Field("webhook_url", "Webhook address", "url", required=True, secret=True,
              help="Slack > your app > Incoming Webhooks: the https://hooks.slack.com/services/... address."),
        EVENT_FIELD,
    ]

    def check_address(self, url: str) -> str:
        p = urlsplit(url)
        if not _host_ok(p.hostname or "", PROVIDER_HOSTS["slack"]) or not p.path.startswith("/services/"):
            return "That is not a Slack incoming-webhook address (it starts with hooks.slack.com/services/)."
        return ""

    def body(self, event, s, alert):
        return messages.slack_body(event, s, alert)

    def test_body(self):
        return messages.slack_test_body(self.events())

    def error_text(self, resp) -> str:
        code = short_body(resp, 60).strip().lower()
        if code in SLACK_ERRORS:
            return f"Slack answered {resp.status_code}: {SLACK_ERRORS[code]} ({code})."
        return http_error("Slack", resp, self.masker())


class Discord(_Notifier):
    id = "discord"
    name = "Discord"
    icon = "discord"
    provider = "Discord"
    description = "Post crawl results, new issues and alerts to a Discord channel (webhook)."
    docs = "https://docs.discord.com/developers/resources/webhook (Slack-compatible execute; read 2026-10-28)"
    fields = [
        Field("webhook_url", "Webhook address", "url", required=True, secret=True,
              help="Discord > channel settings > Integrations > Webhooks: Copy Webhook URL."),
        EVENT_FIELD,
    ]

    def check_address(self, url: str) -> str:
        p = urlsplit(url)
        if not _host_ok(p.hostname or "", PROVIDER_HOSTS["discord"]) or \
                not re.match(r"^/api(?:/v\d+)?/webhooks/\d+/[\w-]+(?:/slack)?/?$", p.path):
            return "That is not a Discord webhook address (it starts with discord.com/api/webhooks/)."
        return ""

    def target(self) -> str:
        """The Slack-compatible endpoint of the webhook (address + "/slack")."""
        url = self.address()
        if not url:
            return ""
        p = urlsplit(url)
        path = p.path.rstrip("/")
        if not path.endswith("/slack"):
            path += "/slack"
        return urlunsplit((p.scheme, p.netloc, path, p.query, ""))

    def body(self, event, s, alert):
        return messages.slack_body(event, s, alert)

    def test_body(self):
        return messages.slack_test_body(self.events())

    def error_text(self, resp) -> str:
        detail = ""
        try:
            data = resp.json()
            if isinstance(data, dict) and data.get("message"):
                detail = str(data["message"])[:160]
        except ValueError:
            pass
        return http_error("Discord", resp, self.masker(), detail=detail)


class Teams(_Notifier):
    id = "teams"
    name = "Microsoft Teams"
    icon = "teams"
    provider = "Microsoft Teams"
    description = ("Post crawl results, new issues and alerts to a Teams channel (a Workflows webhook: "
                   "'When a Teams webhook request is received').")
    docs = "https://learn.microsoft.com/en-us/connectors/teams/ (Workflows webhook + Adaptive Card; read 2026-10-28)"
    fields = [
        Field("webhook_url", "Workflow address", "url", required=True, secret=True,
              help="Teams > channel > Workflows > 'Post to a channel when a webhook request is received': copy its "
                   "address."),
        EVENT_FIELD,
    ]

    def check_address(self, url: str) -> str:
        p = urlsplit(url)
        if not _host_ok(p.hostname or "", PROVIDER_HOSTS["teams"]):
            return ("That is not a Teams Workflows webhook address (it is on logic.azure.com or "
                    "api.powerplatform.com).")
        return ""

    def body(self, event, s, alert):
        return messages.teams_body(event, s, alert)

    def test_body(self):
        return messages.teams_test_body(self.events())


class Webhook(_Notifier):
    id = "webhook"
    name = "Webhook"
    icon = "webhook"
    provider = "The webhook"
    multi = True
    url_key = "url"
    description = ("Send crawl results, new issues and alerts as JSON to any address (your own service, Zapier, "
                   "Make, n8n...), optionally signed with HMAC-SHA256.")
    docs = "ARACHNIX's own JSON (crawler/connectors/messages.py generic_body); no provider document"
    fields = [
        Field("name", "Name", "text", required=True, help="A name for this webhook, e.g. 'Team alerts'."),
        Field("url", "Address", "url", required=True, secret=True,
              help="The https:// address that receives a POST with the JSON."),
        Field("signing_secret", "Signing secret", "password", secret=True,
              help="Optional: each request then carries X-ARACHNIX-Signature (HMAC-SHA256 of '<timestamp>.<body>')."),
        EVENT_FIELD,
        Field("allow_private_network", "Allow private network", "bool", default=False,
              help="Allow an address inside your own network (http:// is then allowed too)."),
    ]

    def body(self, event, s, alert):
        return messages.generic_body(event, s, alert)

    def test_body(self):
        return messages.generic_test_body(self.events())

    def extra_headers(self, body: Dict[str, Any], data: bytes) -> Dict[str, str]:
        headers = {"X-ARACHNIX-Event": str(body.get("event") or ""),
                   "X-ARACHNIX-Delivery": str(body.get("id") or uuid.uuid4().hex)}
        secret = self.secret("signing_secret")
        if secret:
            ts = str(int(time.time()))
            headers["X-ARACHNIX-Timestamp"] = ts
            headers["X-ARACHNIX-Signature"] = "sha256=" + sign(secret, ts, data)
        return headers


def sign(secret: str, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of "<timestamp>.<body>" with the signing secret (what a receiver recomputes)."""
    return hmac.new(secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


for _c in (Slack(), Teams(), Discord(), Webhook()):
    register(_c)
