"""
Google sign-in for Search Console, GA4 and Google Sheets: OAuth 2.0 authorization code flow with PKCE (S256)
and offline access (a refresh token), plus Google service accounts as a no-browser alternative for a team server.

Flow (web/app.py wires the routes):
  begin(redirect_uri, sources) -> {"auth_url", "state"}      the browser opens auth_url (Google's consent page);
                                                              sources: gsc, ga4, sheets (SIGN_IN_SOURCES)
  Google redirects to {base}/api/integrations/google/callback?state=..&code=..
  finish(state, code, error) -> public account                the code + PKCE verifier become tokens
  finish_from_url(pasted)                                     same, from the address the user pasted (for a
                                                              redirect URI the browser cannot reach, see below)
  access_token(account_id) -> str                              refreshed when within a minute of expiry
  GoogleClient(account_id).get/post(...)                       Bearer calls; a 401 refreshes once and retries;
                                                              429/5xx back off (common.call)
  revoke(account_id)                                          revokes at Google, then deletes the account
  add_service_account(json_text)                              a service account key (JWT bearer grant, RS256)

The refresh token (and a service account's private key) live only in <reports_dir>/integrations/google.json;
access tokens only in memory. Nothing secret is ever returned, logged or put in an error message.

Redirect URI note: Google accepts only https:// redirect URIs, or http://localhost / http://127.0.0.1;
a private IP such as http://10.10.6.56:8000 is refused. On an office server either (a) connect once from the
server itself at http://localhost:8000 (the token is server-wide, everyone uses it afterwards), (b) register
http://localhost:8000/api/integrations/google/callback and paste the address the browser ends on (it shows
"cannot connect" on another PC; the address still carries the code), or (c) use a service account.
"""
import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from . import store
from .common import (AuthExpired, IntegrationError, NotConnected, call, endpoint, google_error, new_session,
                     now_iso)

SCOPE_GSC = "https://www.googleapis.com/auth/webmasters.readonly"
SCOPE_GA4 = "https://www.googleapis.com/auth/analytics.readonly"
SOURCE_SCOPES = {"gsc": SCOPE_GSC, "ga4": SCOPE_GA4}
# Google Sheets (integrations/sheets.py): drive.file creates Sheets and touches only the files ARACHNIX created;
# spreadsheets.readonly reads a Sheet the user points ARACHNIX at (a URL list). Asked for with the source "sheets".
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_SHEETS_READ = "https://www.googleapis.com/auth/spreadsheets.readonly"
SHEETS_SCOPES = (SCOPE_DRIVE_FILE, SCOPE_SHEETS_READ)
SIGN_IN_SOURCES = ("gsc", "ga4", "sheets")
IDENTITY_SCOPES = ["openid", "email"]
STATE_TTL = 600                   # seconds a consent page may stay open
REFRESH_MARGIN = 60               # refresh an access token this many seconds before it expires
CALLBACK_PATH = "/api/integrations/google/callback"

_pending: Dict[str, Dict[str, Any]] = {}            # state -> {"verifier", "redirect_uri", "sources", "created"}
_tokens: Dict[str, Tuple[str, float]] = {}          # account id -> (access token, expires at epoch)
_lock = threading.RLock()


# ---------------------------------------------------------------------------------------------
# Set-up
# ---------------------------------------------------------------------------------------------
def configured() -> bool:
    cid, secret, _ = store.client()
    return bool(cid and secret)


def setup_help(redirect_uri: str = "") -> Dict[str, Any]:
    """What to do when no OAuth client is set (shown by the Connect button)."""
    ruri = redirect_uri or f"http://localhost:8000{CALLBACK_PATH}"
    return {
        "configured": configured(),
        "redirect_uri": ruri,
        "steps": [
            "Open console.cloud.google.com, pick or create a project.",
            "APIs & Services > Library: enable 'Google Search Console API', 'Google Analytics Admin API' and "
            "'Google Analytics Data API' (and 'PageSpeed Insights API' for a PageSpeed key).",
            "APIs & Services > OAuth consent screen: user type Internal (Google Workspace) or External with your "
            "team as test users; add the scopes webmasters.readonly and analytics.readonly.",
            "For Google Sheets (export to a Sheet, read a URL list from a Sheet): also enable 'Google Sheets API' and "
            "'Google Drive API' and add the scopes drive.file and spreadsheets.readonly; then use Connect with Google "
            "(Sheets).",
            f"APIs & Services > Credentials > Create credentials > OAuth client ID > Web application; add the "
            f"authorised redirect URI {ruri}",
            "Put the client ID and secret in the server environment as GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET (then restart), or paste them in this dialog.",
        ],
        "redirect_rules": "Google accepts https:// redirect addresses, or http://localhost. A private IP address "
                          "(http://10.x.x.x) is refused: connect once from the server at http://localhost:8000, "
                          "or paste the address you land on, or use a service account.",
        "service_account": "Alternative without a browser sign-in: create a service account, download its JSON key, "
                           "add its e-mail as a user of the Search Console property and as a Viewer of the GA4 "
                           "property, then upload the key here.",
    }


def redirect_uri_for(base_url: str) -> str:
    return base_url.rstrip("/") + CALLBACK_PATH


# ---------------------------------------------------------------------------------------------
# Authorization code + PKCE
# ---------------------------------------------------------------------------------------------
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def pkce_pair() -> Tuple[str, str]:
    """(code_verifier, code_challenge) for S256 (RFC 7636)."""
    verifier = _b64url(secrets.token_bytes(48))           # 64 characters, all unreserved
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _clean_pending() -> None:
    now = time.time()
    for st in [s for s, p in _pending.items() if now - p["created"] > STATE_TTL]:
        _pending.pop(st, None)


def scopes_for(sources) -> List[str]:
    """The scopes one sign-in asks for: gsc / ga4 / sheets (none known = Search Console and GA4). Google keeps
    the scopes granted before (include_granted_scopes), so asking for Sheets later adds to them."""
    sources = list(sources or SOURCE_SCOPES)
    wanted = [SOURCE_SCOPES[s] for s in sources if s in SOURCE_SCOPES]
    if "sheets" in sources:
        wanted += list(SHEETS_SCOPES)
    return IDENTITY_SCOPES + (wanted or list(SOURCE_SCOPES.values()))


def begin(redirect_uri: str, sources=("gsc", "ga4"), login_hint: str = "") -> Dict[str, Any]:
    cid, _, _ = store.client()
    if not cid:
        raise NotConnected("No Google OAuth client is set up on the server (GOOGLE_OAUTH_CLIENT_ID / "
                           "GOOGLE_OAUTH_CLIENT_SECRET).")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    with _lock:
        _clean_pending()
        _pending[state] = {"verifier": verifier, "redirect_uri": redirect_uri, "sources": list(sources or ()),
                           "created": time.time()}
    params = {
        "client_id": cid, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": " ".join(scopes_for(sources)), "access_type": "offline", "prompt": "consent",
        "include_granted_scopes": "true", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return {"auth_url": f"{endpoint('oauth_auth')}?{urlencode(params)}", "state": state,
            "redirect_uri": redirect_uri, "expires_in": STATE_TTL}


def _jwt_claims(id_token: str) -> Dict[str, Any]:
    """Claims of an id_token received straight from Google's token endpoint over TLS (OpenID Connect allows
    skipping the signature check in that case)."""
    try:
        part = id_token.split(".")[1]
        part += "=" * (-len(part) % 4)
        data = json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def finish(state: str, code: str = "", error: str = "") -> Dict[str, Any]:
    """Exchanges the authorization code for tokens and stores the account. Returns its public view."""
    with _lock:
        _clean_pending()
        pending = _pending.pop(state or "", None)
    if error:
        raise IntegrationError("Google sign-in was cancelled." if error == "access_denied"
                               else f"Google sign-in failed: {error[:100]}")
    if pending is None:
        raise IntegrationError("This sign-in link has expired or was already used. Press Sign in with Google again.")
    if not code:
        raise IntegrationError("Google did not send an authorization code.")
    cid, secret, _ = store.client()
    if not cid:
        raise NotConnected("No Google OAuth client is set up on the server.")
    session = new_session()
    resp = call(session, "POST", endpoint("oauth_token"), retries=2, data={
        "code": code, "client_id": cid, "client_secret": secret, "redirect_uri": pending["redirect_uri"],
        "grant_type": "authorization_code", "code_verifier": pending["verifier"]})
    if resp.status_code != 200:
        status, message, reason = google_error(resp)
        raise IntegrationError(f"Google refused the sign-in ({reason or status}): {message}", status, reason)
    tok = resp.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        raise IntegrationError("Google did not return a refresh token. Remove this app under "
                               "myaccount.google.com/permissions and sign in again.")
    granted = str(tok.get("scope") or "").split()
    claims = _jwt_claims(str(tok.get("id_token") or ""))
    email = str(claims.get("email") or "")
    access = str(tok.get("access_token") or "")
    if not email and access:
        try:
            r = call(session, "GET", endpoint("userinfo"), retries=1,
                     headers={"Authorization": f"Bearer {access}"})
            if r.status_code == 200:
                email = str(r.json().get("email") or "")
        except IntegrationError:
            pass
    acc = store.upsert_account({
        "id": uuid.uuid4().hex[:12], "kind": "oauth", "email": email, "name": email or "Google account",
        "scopes": granted, "refresh_token": refresh, "last_used_at": now_iso(),
    })
    if access:
        with _lock:
            _tokens[acc["id"]] = (access, time.time() + float(tok.get("expires_in") or 3600))
    return store.public_account(acc)


def finish_from_url(pasted: str) -> Dict[str, Any]:
    """The address the browser landed on after consent (…/callback?state=..&code=..), pasted by the user."""
    try:
        qs = parse_qs(urlsplit(str(pasted or "").strip()).query)
    except ValueError:
        qs = {}
    state = (qs.get("state") or [""])[0]
    code = (qs.get("code") or [""])[0]
    error = (qs.get("error") or [""])[0]
    if not state or not (code or error):
        raise IntegrationError("That address has no sign-in code. Paste the whole address from the browser's "
                               "address bar after choosing your Google account.")
    return finish(state, code, error)


# ---------------------------------------------------------------------------------------------
# Service accounts (JWT bearer grant, RS256 signed with the key's private key; pure Python)
# ---------------------------------------------------------------------------------------------
_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _der_read(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    """(tag, value, next position) of one DER element."""
    tag = data[pos]
    length = data[pos + 1]
    pos += 2
    if length & 0x80:
        n = length & 0x7F
        length = int.from_bytes(data[pos:pos + n], "big")
        pos += n
    return tag, data[pos:pos + length], pos + length


def _der_seq(data: bytes) -> List[Tuple[int, bytes]]:
    out, pos = [], 0
    while pos < len(data):
        tag, value, pos = _der_read(data, pos)
        out.append((tag, value))
    return out


def rsa_private_numbers(pem: str) -> Tuple[int, int, int]:
    """(n, e, d) of a PEM RSA private key (PKCS#8 "BEGIN PRIVATE KEY" or PKCS#1 "BEGIN RSA PRIVATE KEY")."""
    lines = [ln.strip() for ln in str(pem or "").replace("\\n", "\n").splitlines()]
    body = "".join(ln for ln in lines if ln and not ln.startswith("-----"))
    try:
        der = base64.b64decode(body)
        tag, seq, _ = _der_read(der, 0)
        items = _der_seq(seq)
        if len(items) >= 3 and items[1][0] == 0x30 and items[2][0] == 0x04:      # PKCS#8 wrapper
            _, inner, _ = _der_read(items[2][1], 0)
            items = _der_seq(inner)
        ints = [int.from_bytes(v, "big") for t, v in items if t == 0x02]
        n, e, d = ints[1], ints[2], ints[3]
    except Exception:
        raise IntegrationError("The service account key could not be read (expected an RSA private key).")
    return n, e, d


def rs256_sign(message: bytes, pem: str) -> bytes:
    n, _e, d = rsa_private_numbers(pem)
    k = (n.bit_length() + 7) // 8
    t = _SHA256_PREFIX + hashlib.sha256(message).digest()
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), d, n).to_bytes(k, "big")


def _sa_assertion(sa: Dict[str, Any], scopes: List[str], token_url: str) -> str:
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    if sa.get("private_key_id"):
        header["kid"] = sa["private_key_id"]
    claims = {"iss": sa["client_email"], "scope": " ".join(scopes), "aud": token_url, "iat": now, "exp": now + 3600}
    signing_input = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "." +
                     _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    return signing_input + "." + _b64url(rs256_sign(signing_input.encode("ascii"), sa["private_key"]))


def add_service_account(json_text: str, name: str = "") -> Dict[str, Any]:
    """Stores a service account key (the JSON Google lets you download) after one test token request."""
    try:
        data = json.loads(json_text) if isinstance(json_text, str) else dict(json_text or {})
    except ValueError:
        raise IntegrationError("That is not a service account JSON key file.")
    if data.get("type") != "service_account" or not data.get("client_email") or not data.get("private_key"):
        raise IntegrationError("That is not a service account JSON key file (type, client_email and private_key "
                               "are needed).")
    sa = {k: data.get(k) for k in ("client_email", "private_key", "private_key_id", "token_uri")}
    rsa_private_numbers(sa["private_key"])           # readable?
    acc = {"id": uuid.uuid4().hex[:12], "kind": "service_account", "email": sa["client_email"],
           "name": name or sa["client_email"], "scopes": service_scopes(), "sa_key": sa,
           "last_used_at": now_iso()}
    _service_token(acc, force=True)                  # raises when Google refuses the key
    saved = store.upsert_account(acc)
    with _lock:
        if acc["id"] in _tokens and saved["id"] != acc["id"]:
            _tokens[saved["id"]] = _tokens.pop(acc["id"])
    return store.public_account(saved)


def service_scopes() -> List[str]:
    """Every scope a service account's token asks for (no consent screen: the token carries them all; each API
    still has to be enabled in the Google Cloud project and the file / property shared with the account)."""
    return list(SOURCE_SCOPES.values()) + list(SHEETS_SCOPES)


def _service_token(acc: Dict[str, Any], force: bool = False) -> str:
    sa = acc.get("sa_key") or {}
    token_url = endpoint("oauth_token")
    assertion = _sa_assertion(sa, service_scopes(), sa.get("token_uri") or token_url)
    resp = call(new_session(), "POST", token_url, retries=2, data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
    if resp.status_code != 200:
        status, message, reason = google_error(resp)
        raise AuthExpired(f"Google refused the service account key ({reason or status}): {message}", status, reason)
    tok = resp.json()
    access = str(tok.get("access_token") or "")
    with _lock:
        _tokens[acc["id"]] = (access, time.time() + float(tok.get("expires_in") or 3600))
    return access


# ---------------------------------------------------------------------------------------------
# Access tokens
# ---------------------------------------------------------------------------------------------
def access_token(account_id: str, force_refresh: bool = False) -> str:
    acc = store.get_account(account_id)
    if acc is None:
        raise NotConnected("That Google account is no longer connected.")
    with _lock:
        cached = _tokens.get(account_id)
    if cached and not force_refresh and cached[1] - REFRESH_MARGIN > time.time():
        return cached[0]
    if acc.get("kind") == "service_account":
        return _service_token(acc)
    cid, secret, _ = store.client()
    if not cid:
        raise NotConnected("No Google OAuth client is set up on the server.")
    resp = call(new_session(), "POST", endpoint("oauth_token"), retries=3, data={
        "client_id": cid, "client_secret": secret, "refresh_token": acc.get("refresh_token") or "",
        "grant_type": "refresh_token"})
    if resp.status_code != 200:
        status, message, reason = google_error(resp)
        if reason in ("invalid_grant", "unauthorized_client", "invalid_client") or status in (400, 401):
            store.update_account(account_id, needs_reconnect=True)
            with _lock:
                _tokens.pop(account_id, None)
            raise AuthExpired("The Google sign-in for this account has expired or was removed. Connect it again "
                              "under Configuration > API Access.", status, reason)
        raise IntegrationError(f"Google could not refresh the sign-in ({reason or status}): {message}", status, reason)
    tok = resp.json()
    access = str(tok.get("access_token") or "")
    with _lock:
        _tokens[account_id] = (access, time.time() + float(tok.get("expires_in") or 3600))
    if tok.get("refresh_token"):                      # Google may rotate it
        store.update_account(account_id, refresh_token=tok["refresh_token"])
    return access


def has_scope(account: Dict[str, Any], source: str) -> bool:
    """The account allowed what `source` needs (gsc, ga4, or sheets = both Sheets scopes). A service account's
    token asks for every scope (service_scopes), so it has them all."""
    if source == "sheets":
        return account.get("kind") == "service_account" or all(
            s in (account.get("scopes") or []) for s in SHEETS_SCOPES)
    want = SOURCE_SCOPES.get(source)
    return bool(want) and want in (account.get("scopes") or [])


def missing_sheets_scopes(account: Dict[str, Any]) -> List[str]:
    """The Sheets scopes an OAuth account did not allow (Google's consent screen lets people untick them)."""
    if account.get("kind") == "service_account":
        return []
    return [s for s in SHEETS_SCOPES if s not in (account.get("scopes") or [])]


class GoogleClient:
    """Bearer-authorised calls for one stored account: 401 refreshes the token once and retries; 429 / 5xx back
    off (common.call)."""

    def __init__(self, account_id: str, stop: Optional[threading.Event] = None, on_wait=None):
        self.account_id = account_id
        self.stop = stop
        self.on_wait = on_wait
        self.session = new_session()

    def request(self, method: str, url: str, **kwargs):
        token = access_token(self.account_id)
        extra = dict(kwargs.pop("headers", None) or {})
        resp = None
        for attempt in range(2):
            headers = dict(extra)
            headers["Authorization"] = f"Bearer {token}"
            resp = call(self.session, method, url, stop=self.stop, on_wait=self.on_wait, headers=headers, **kwargs)
            if resp.status_code != 401 or attempt:
                return resp
            token = access_token(self.account_id, force_refresh=True)
        return resp

    def get(self, url: str, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.request("POST", url, **kwargs)

    def json(self, method: str, url: str, what: str, **kwargs) -> Dict[str, Any]:
        """The JSON answer of a call that must succeed; IntegrationError (or AuthExpired on 401/403 auth
        problems) with Google's message otherwise."""
        resp = self.request(method, url, **kwargs)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                raise IntegrationError(f"{what}: Google sent an answer that is not JSON.", 200)
            return data if isinstance(data, dict) else {}
        status, message, reason = google_error(resp)
        if status == 401:
            store.update_account(self.account_id, needs_reconnect=True)
            raise AuthExpired(f"{what}: Google no longer accepts this sign-in; connect the account again.",
                              status, reason)
        if status == 403:
            raise IntegrationError(f"{what}: permission denied ({message}). Check that the account can see this "
                                   "property and that the API is enabled in the Google Cloud project.", status, reason)
        raise IntegrationError(f"{what}: {message or 'HTTP ' + str(status)}", status, reason)


# ---------------------------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------------------------
def revoke(account_id: str) -> Dict[str, Any]:
    """Revokes the refresh token at Google (OAuth accounts) and deletes the account and its tokens here.
    The account is deleted even when Google cannot be reached."""
    acc = store.get_account(account_id)
    if acc is None:
        return {"deleted": False, "revoked": False, "message": "No such account."}
    revoked, message = False, ""
    if acc.get("kind") == "oauth" and acc.get("refresh_token"):
        try:
            resp = call(new_session(), "POST", endpoint("oauth_revoke"), retries=1,
                        data={"token": acc["refresh_token"]},
                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            revoked = resp.status_code == 200
            if not revoked:
                _, msg, reason = google_error(resp)
                message = f"Google did not confirm the revoke ({reason or msg}); the account was removed here."
        except IntegrationError:
            message = "Google could not be reached to revoke access; the account was removed here."
    elif acc.get("kind") == "service_account":
        message = "Service account removed here. To cut its access completely, delete its key in Google Cloud."
    with _lock:
        _tokens.pop(account_id, None)
    store.delete_account(account_id)
    return {"deleted": True, "revoked": revoked, "message": message}
