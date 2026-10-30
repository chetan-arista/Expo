"""
Connections: the connectors hub (PLAN4_build.md 10.2). One registry lists every connector with its fields, status
and Connect / Test / Disconnect:

  google_search_console, google_analytics (kind google), pagespeed (performance), email_smtp (notify): adapters
      over the existing API Access / Settings flows (adapters.py)
  slack, teams, discord, webhook:<slug> (notify): crawl finished / failed, new or returning issues and alerts, with
      numbers that reconcile (webhooks.py; events.notify_event)
  languagetool (language): grammar check of crawled public pages (languagetool.py)
  moz, majestic, ahrefs (link_metrics): the Link Metrics tab, with a monthly unit cap each (linkmetrics.py; the
      fetch job is crawler/integrations/linkmetrics.py)
  indexnow (indexing): submit new / changed indexable pages to the IndexNow engines (indexnow.py)
  green_web (sustainability): green-hosting lookup of the crawled hosts for the carbon rating (green_web.py)
  custom_mcp:<slug> (mcp): another MCP server whose enabled tools Ask ARACHNIX may use (custom_mcp.py)
  jira, github_issues (tickets): push Fix plan groups / Issues as tickets, remembered by fingerprint
      (jira.py, github_issues.py; the push itself is tickets_push.push)
  google_sheets (google): export to a new Google Sheet and read URL lists from a Sheet (google_sheets.py; the work
      is crawler/integrations/sheets.py)

Modules: base (Field, Status, TestResult, ConnectorError, Masker, Connector), registry, store (config files and the
"connectors" section of secrets.json), http (check_url, safe_request: address rules, no automatic redirects, size
caps, errors with the kind and host only), events (notify_event, after_crawl), alerts (alert rules), assistant (Ask
ARACHNIX: the chat backend over ARACHNIX's MCP tools and the enabled custom MCP tools; not a connector itself),
tickets_push (POST /api/tickets/push and GET /api/tickets/pushed: tickets from the Fix plan or Issues, dry run,
fingerprints, the signed-in crawl confirm).

Only an admin may connect, test or disconnect (web/app.py _require_admin). Secrets never leave the server: public()
shows {"set": bool}; every message passes the connector's masker.
"""
from .base import ConnectorError, Field, Status, TestResult  # noqa: F401
from .registry import all_connectors, by_kind, get, register  # noqa: F401
