"""
GitHub Issues (kind "tickets"): push Fix plan groups and Issues as GitHub issues (tickets_push.py runs the push).
GitHub's REST API cannot take picture uploads, so a ticket names the screenshot ARACHNIX keeps and until when; it
never carries a link with the access key.

Provider documents (request and response shapes, read 2026-10-28):
  https://docs.github.com/en/rest/issues/issues#create-an-issue
      POST /repos/{owner}/{repo}/issues {"title", "body" (Markdown), "labels": [...], "assignees": [...]}
      -> 201 {"id", "number", "html_url", ...}; 403, 404, 410 (issues turned off), 422 (validation failed).
      Labels and assignees are dropped by GitHub when the token's user has no push access.
  https://docs.github.com/en/rest/repos/repos#get-a-repository
      GET /repos/{owner}/{repo} -> {"full_name", "private", "visibility": "public" | "private" | "internal",
      "has_issues", ...}
  https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api
      Authorization: Bearer <token>; Accept: application/vnd.github+json; X-GitHub-Api-Version: 2022-11-28.
      A fine-grained token needs this repository with "Issues: read and write" (and "Metadata: read").
  https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api
      wait at least one second between requests that create content.

Signed-in crawls: the repository's visibility is read before every push; a public repository receives nothing of a
signed-in crawl (no URLs, no titles, no screenshots). Text from crawled pages cannot notify GitHub users: "@name"
outside code is written with an invisible break.
"""
import re
from typing import Any, Dict, List, Optional

from .base import ConnectorError, Field, Masker, TestResult
from .http import http_error, safe_request
from .registry import register
from .tickets_push import TicketConnector, merged_labels, screenshot_note, throttle

API = "https://api.github.com"
TIMEOUT = (5.0, 30.0)
MAX_ANSWER = 1_000_000
MAX_TITLE = 250
MAX_BODY = 60_000
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
PAUSE = 1.0                      # GitHub: at least one second between content-creating requests (tests set 0)
_MENTION = re.compile(r"(?<![\w.`/])@(?=[A-Za-z0-9])")
PUBLIC_REFUSAL = ("This crawl was signed in and {repo} is a public repository: ARACHNIX does not send the URLs, "
                  "titles or screenshots of a signed-in crawl to a public repository. Push to a private repository "
                  "or to Jira instead.")


_MD_SPECIAL = re.compile(r"([\\`*_\[\]|~!#])")


def no_mentions(text: str) -> str:
    """@name -> @<zero-width space>name, so page text never pings a GitHub user or team."""
    return _MENTION.sub("@​", str(text or ""))


def _md(text: Any) -> str:
    """Plain text for the Markdown body: HTML tags shown as text (<h1> must not become a heading), Markdown
    marks escaped (page text cannot add links, images or tables), no @mentions."""
    flat = " ".join(str(text or "").replace("\r", "").split())
    flat = _MD_SPECIAL.sub(r"\\\1", flat).replace("<", "&lt;").replace(">", "&gt;")
    return no_mentions(flat)


def _url(text: Any) -> str:
    """A URL line (GitHub links it): only line breaks and mentions handled."""
    return no_mentions(" ".join(str(text or "").split()))


def issue_markdown(t: Dict[str, Any], site_url: str = "", shots: str = "", dashboard_url: str = "") -> str:
    """The ticket as a GitHub issue body (our own layout; the same content as the ticket exports)."""
    impact = int(t.get("impact") or 0)
    lines = [f"**Priority:** {t.get('priority')} ({_md(t.get('priority_reason'))}) | **Effort:** "
             f"{_md(t.get('effort')) or '-'} | **Impact:** {f'{impact}/100' if impact else '-'} | **Cause:** "
             f"{_md(t.get('verified')) or '-'}", ""]
    if t.get("root_cause"):
        lines += [f"**Root cause:** {_md(t['root_cause'])}", ""]
    if t.get("why"):
        lines += [f"**Why it matters:** {_md(t['why'])}", ""]
    if site_url:
        lines += [f"**Site:** {_url(site_url)}", ""]
    lines += [f"**Affected:** {int(t.get('urls_count') or 0):,} URL(s) - {_md('; '.join(t.get('issues') or []))}", ""]
    if t.get("sample_urls"):
        lines += ["**Sample URLs:**", ""] + [f"- {_url(u)}" for u in t["sample_urls"]] + [""]
    if t.get("where"):
        lines += [f"**Where:** {_md(t['where'])}", ""]
    if t.get("element"):
        fence = "```html" if str(t["element"]).lstrip().startswith("<") else "```"
        lines += ["**Element:**", "", fence, str(t["element"]).replace("```", "'''"), "```", ""]
    if t.get("steps"):
        lines += ["**Steps to fix:**", ""] + [f"{i}. {_md(s)}" for i, s in enumerate(t["steps"], 1)] + [""]
    if t.get("platform_steps"):
        lines += ["**Platform steps:**", ""] + [f"- {_md(s)}" for s in t["platform_steps"]] + [""]
    if t.get("verify"):
        lines += [f"**How to verify:** {_md(t['verify'])}", ""]
    if t.get("prevent"):
        lines += [f"**Prevent it:** {_md(t['prevent'])}", ""]
    if shots:
        lines += [f"**Screenshot:** {_md(shots)}", ""]
    lines.append(f"_Ticket {_md(t.get('id'))} from the ARACHNIX audit._")
    if dashboard_url:
        lines += ["", f"Open ARACHNIX: {dashboard_url}"]
    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY - 40].rstrip() + "\n\n_(shortened to fit GitHub)_\n"
    return body


class GitHubIssues(TicketConnector):
    id = "github_issues"
    name = "GitHub Issues"
    service = "GitHub"
    icon = "github"
    sends = ["urls", "titles"]
    description = ("Push Fix plan groups and issues as GitHub issues, with fix steps and sample URLs. Screenshots "
                   "stay in ARACHNIX (GitHub's API cannot take uploads).")
    docs = ("https://docs.github.com/en/rest/issues/issues#create-an-issue and "
            "https://docs.github.com/en/rest/repos/repos#get-a-repository (read 2026-10-28)")
    fields = [
        Field("repo", "Repository", "text", required=True, help="owner/repository, e.g. acme/website"),
        Field("token", "Access token", "password", required=True, secret=True,
              help="A fine-grained personal access token with this repository and 'Issues: read and write'."),
        Field("assignees", "Assignees", "list", help="Optional: GitHub user names to assign every ticket to."),
        Field("labels", "Extra labels", "list", help="Optional: labels added to every ticket, besides ARACHNIX's own."),
        Field("api_url", "API address", "url", default=API,
              help="Leave as is for github.com; GitHub Enterprise Server: https://<your host>/api/v3"),
        Field("allow_private_network", "Allow private network", "bool", default=False,
              help="Only for a GitHub Enterprise Server inside your own network."),
    ]

    # --- settings --------------------------------------------------------------------------------
    def validate(self, config, secrets):
        clean, changes, errors = super().validate(config, secrets)
        repo = str(clean.get("repo") or "").strip().strip("/")
        repo = re.sub(r"^https?://github\.com/", "", repo, flags=re.I).removesuffix(".git")
        clean["repo"] = repo
        if "repo" not in errors and repo and not REPO_RE.match(repo):
            errors["repo"] = "Give the repository as owner/name, e.g. acme/website."
        bad = [u for u in clean.get("assignees") or [] if not USER_RE.match(u.lstrip("@"))]
        if bad and "assignees" not in errors:
            errors["assignees"] = f"Not a GitHub user name: {bad[0][:40]!r}."
        clean["assignees"] = [u.lstrip("@") for u in clean.get("assignees") or []][:10]
        bad = [x for x in clean.get("labels") or [] if len(x) > 50]
        if bad and "labels" not in errors:
            errors["labels"] = "A GitHub label has at most 50 characters."
        api = str(clean.get("api_url") or API).strip().rstrip("/") or API
        if "api_url" not in errors and (re.search(r"[?#]", api) or not re.match(r"^https?://[^/\s]+(/[^\s]*)?$", api)):
            errors["api_url"] = "Give the API address only, e.g. https://api.github.com"
        clean["api_url"] = api
        return clean, changes, errors

    def masker(self) -> Masker:
        token = self.secret("token")
        return Masker([token] if token else [])

    def target(self) -> str:
        cfg = self.config()
        host = re.sub(r"^https?://", "", str(cfg.get("api_url") or API)).split("/")[0].lower()
        return f"github:{'' if host == 'api.github.com' else host + '/'}{cfg.get('repo') or ''}"

    def target_label(self) -> str:
        return str(self.config().get("repo") or "-")

    def extra_labels(self) -> List[str]:
        return [str(x) for x in self.config().get("labels") or []]

    # --- calls ---------------------------------------------------------------------------------------
    def _call(self, method: str, path: str, **kw):
        cfg = self.config()
        base = str(cfg.get("api_url") or API).rstrip("/")
        headers = {"Authorization": f"Bearer {self.secret('token')}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        headers.update(kw.pop("headers", None) or {})
        return safe_request(method, base + path, headers=headers, timeout=TIMEOUT, max_bytes=MAX_ANSWER,
                            allow_private_network=self.allow_private(), **kw)

    def _error(self, resp, what: str) -> ConnectorError:
        code = resp.status_code
        data = _json(resp)
        message = " ".join(str(data.get("message") or "").split())[:200]
        if code == 401:
            return ConnectorError("GitHub refused the access token (401): it is wrong or has expired.", "auth",
                                  status=401)
        limited = str(resp.headers.get("x-ratelimit-remaining") or "") == "0" or "rate limit" in message.lower()
        if code == 429 or (code == 403 and limited):
            return ConnectorError("GitHub's rate limit was reached: push the rest later.", "rate", status=code)
        if code == 403:
            return ConnectorError(f"{what}: the token has no permission for this repository (403). Fine-grained "
                                  "tokens need 'Issues: read and write' for it.", "auth", status=403)
        if code == 404:
            return ConnectorError(f"{what}: repository {self.config().get('repo')} was not found, or the token "
                                  "cannot see it (404).", "auth", status=404)
        if code == 410:
            return ConnectorError(f"{what}: issues are turned off for {self.config().get('repo')} (410).", "auth",
                                  status=410)
        errs = "; ".join(f"{e.get('field', '')} {e.get('code', '')}".strip() for e in data.get("errors") or []
                         if isinstance(e, dict))
        detail = (message + (f" ({errs})" if errs else "")).strip()
        return ConnectorError(f"{what}: {http_error('GitHub', resp, self.masker(), detail=detail)}", "http",
                              status=code)

    def repository(self) -> Dict[str, Any]:
        """GET /repos/{owner}/{repo}: {"full_name", "public", "visibility", "has_issues"}. Raises ConnectorError."""
        resp = self._call("GET", f"/repos/{self.config().get('repo')}")
        if resp.status_code != 200:
            raise self._error(resp, "Reading the repository")
        data = _json(resp)
        visibility = str(data.get("visibility") or ("private" if data.get("private") else "public")).lower()
        public = visibility == "public" and not data.get("private")
        return {"full_name": str(data.get("full_name") or self.config().get("repo")), "public": public,
                "visibility": visibility, "has_issues": bool(data.get("has_issues", True))}

    def target_info(self, dry_run: bool = False) -> Dict[str, Any]:
        try:
            repo = self.repository()
        except ConnectorError as e:
            if dry_run:
                return {"public": None, "detail": f"The repository's visibility could not be read ({e}); it is "
                                                  "checked again when you push."}
            raise
        if not repo["has_issues"]:
            raise ConnectorError(f"Issues are turned off for {repo['full_name']}: turn them on in the repository "
                                 "settings, or choose another repository.", "invalid")
        return {"public": repo["public"], "detail": f"{repo['full_name']} is a {repo['visibility']} repository."}

    def refuse_private(self, info: Dict[str, Any]) -> str:
        if info.get("public") is True:
            return PUBLIC_REFUSAL.format(repo=self.config().get("repo"))
        if info.get("public") is None:
            return ("This crawl was signed in and the repository's visibility could not be read, so nothing is "
                    "sent: try again when GitHub answers.")
        return ""

    def _test(self) -> TestResult:
        repo = self.repository()
        if not repo["has_issues"]:
            return TestResult(False, f"{repo['full_name']} was found, but its issues are turned off.")
        return TestResult(True, f"GitHub answered: {repo['full_name']} ({repo['visibility']}) can receive issues.")

    def body(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> str:
        from .messages import clean_url
        return issue_markdown(t, ctx.get("site_url", ""), screenshot_note(t, False), clean_url(ctx.get("dashboard_url")))

    def create_ticket(self, t: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.config()
        labels = merged_labels(t, self)
        prio = f"priority-{str(t.get('priority') or 'medium').lower()}"
        if prio not in labels:
            labels.append(prio)
        payload: Dict[str, Any] = {"title": no_mentions(" ".join(f"[SEO] {t.get('title')}".split()))[:MAX_TITLE],
                                   "body": self.body(t, ctx), "labels": [x[:50] for x in labels][:20]}
        if cfg.get("assignees"):
            payload["assignees"] = list(cfg["assignees"])
        resp = self._call("POST", f"/repos/{cfg.get('repo')}/issues", json=payload)
        if resp.status_code != 201:
            raise self._error(resp, "Creating the issue")
        data = _json(resp)
        number = data.get("number")
        if number is None:
            raise ConnectorError("GitHub did not answer with the new issue's number.", "http")
        notes = []
        if t.get("screenshots"):
            notes.append("Screenshots stay in ARACHNIX: GitHub's API cannot take uploads.")
        if PAUSE:
            throttle(PAUSE)
        return {"key": f"#{number}", "url": str(data.get("html_url") or ""), "attachments": 0,
                "attachment_errors": [], "notes": notes}


def _json(resp) -> Dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def connector() -> GitHubIssues:
    from . import registry
    c = registry.get("github_issues")
    return c if isinstance(c, GitHubIssues) else GitHubIssues()


register(GitHubIssues())
