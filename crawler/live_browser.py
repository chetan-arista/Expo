"""
The live login window: a server-side Chrome that a person drives from the dashboard, like Screaming Frog's
built-in login browser. The page is shown as JPEG frames; clicks, typing, keys and scrolling are replayed
through the DevTools protocol. The person passes any CAPTCHA, 2FA or SSO step themselves; finish() then
keeps the login (every cookie, HttpOnly ones included, plus localStorage/sessionStorage) under the session
id, and a crawl started with auth_type "live" and that live_session_id uses it.

Ported from the sibling project's live_browser.py; sessions expire after 10 idle minutes, at most 2 at a time.
"""
import base64
import ipaddress
import socket
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from .browser import chrome_available, chrome_error, close_chrome, export_session, new_chrome, storage_origins
from .config import DEFAULT_USER_AGENT

WIDTH, HEIGHT = 1280, 800
MIN_WIDTH, MIN_HEIGHT, MAX_HEIGHT = 320, 480, 1400   # viewer-sized windows (see _viewport)
PHONE_WIDTH = 700             # narrower than this: the page is laid out as on a phone
IDLE_SECONDS = 600           # a window nobody touched for 10 minutes is closed
MAX_SESSIONS = 2              # Chrome is memory-hungry: at most two people log in at once
STATE_KEEP_SECONDS = 3 * 3600  # how long a finished login waits for its crawl

# Non-printable keys forwarded from the viewer: name -> Windows virtual key code
_SPECIAL_KEYS = {
    "Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46,
    "ArrowLeft": 37, "ArrowUp": 38, "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35,
}

_finished: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_finished_lock = threading.Lock()


class LiveBrowserError(RuntimeError):
    """A live window could not be started or used (message is shown to the person)."""


def session_state_for(session_id: str) -> Optional[Dict[str, Any]]:
    """The login kept by finish() for `session_id`, or None when unknown or expired."""
    with _finished_lock:
        item = _finished.get(session_id or "")
        if not item:
            return None
        if time.time() - item[0] > STATE_KEEP_SECONDS:
            _finished.pop(session_id, None)
            return None
        return item[1]


def _purge_finished():
    with _finished_lock:
        for sid in [s for s, (t, _) in _finished.items() if time.time() - t > STATE_KEEP_SECONDS]:
            _finished.pop(sid, None)


def _check_url(url: str) -> str:
    """http(s) URL with a scheme added when missing; cloud metadata addresses are refused."""
    url = (url or "").strip()
    if not url:
        raise LiveBrowserError("Enter the address of the login page.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    host = urlparse(url).hostname or ""
    if not host:
        raise LiveBrowserError("That address is not a web page.")
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host))
        if addr.is_link_local or host in ("metadata.google.internal",):
            raise LiveBrowserError("That address is not allowed in the login window.")
    except (socket.gaierror, ValueError, UnicodeError):
        pass
    return url


def _viewport(width: int = 0, height: int = 0) -> Tuple[int, int, bool]:
    """(width, height, phone) of the window: the viewer's screen size when given (a phone gets a phone-sized
    page it can read and tap, instead of a desktop page shrunk to a third), else WIDTH x HEIGHT."""
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        w, h = 0, 0
    if w <= 0:
        return WIDTH, HEIGHT, False
    w = max(MIN_WIDTH, min(WIDTH, w))
    h = max(MIN_HEIGHT, min(MAX_HEIGHT, h or HEIGHT))
    return w, h, w < PHONE_WIDTH


class _LiveSession:
    def __init__(self, url: str, user_agent: str, width: int = 0, height: int = 0):
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.start_url = url
        self.width, self.height, phone = _viewport(width, height)
        self.driver = new_chrome(user_agent, 20, eager=True, interactive=True, width=self.width,
                                 height=self.height, wait=5)
        if self.driver is None:
            raise LiveBrowserError(chrome_error() or "The browser could not start on this server.")
        if phone:
            # The page lays itself out for a phone-wide screen (same User-Agent, so the saved login stays the
            # crawl's); frames are drawn at twice the size so text stays sharp on the phone
            try:
                self._cdp("Emulation.setDeviceMetricsOverride", {"width": self.width, "height": self.height,
                                                                 "deviceScaleFactor": 2, "mobile": True})
            except Exception:
                pass
        self._known_handles = set(self.driver.window_handles)
        self._get(url)

    def _cdp(self, cmd: str, params: Optional[dict] = None) -> dict:
        return self.driver.execute_cdp_cmd(cmd, params or {}) or {}

    def _get(self, url: str):
        try:
            self.driver.get(url)
        except Exception:
            pass  # page-load timeout: show whatever has loaded so far

    def _follow_tabs(self):
        """SSO buttons may open a new tab or close the current one: always show the newest."""
        handles = self.driver.window_handles
        new = [h for h in handles if h not in self._known_handles]
        self._known_handles = set(handles)
        try:
            current = self.driver.current_window_handle
        except Exception:
            current = None
        if new:
            self.driver.switch_to.window(new[-1])
        elif current not in handles and handles:
            self.driver.switch_to.window(handles[-1])

    def current_url(self) -> str:
        try:
            return self.driver.current_url or ""
        except Exception:
            return ""


class LiveBrowserManager:
    """Live login windows by session id (see the module docstring)."""

    def __init__(self, user_agent: str = DEFAULT_USER_AGENT, idle_seconds: int = IDLE_SECONDS,
                 max_sessions: int = MAX_SESSIONS):
        self.user_agent = user_agent
        self.idle_seconds = idle_seconds
        self.max_sessions = max_sessions
        self._sessions: Dict[str, _LiveSession] = {}
        self._starting = 0            # windows whose Chrome is still starting (they hold a slot)
        self._lock = threading.Lock()
        self._reaper: Optional[threading.Thread] = None

    def _session(self, session_id: str) -> _LiveSession:
        with self._lock:
            sess = self._sessions.get(session_id or "")
        if sess is None:
            raise LiveBrowserError("This login window has closed (it expires after 10 idle minutes). Open it again.")
        sess.last_used = time.time()
        return sess

    def _reap(self):
        while True:
            time.sleep(15)
            with self._lock:
                idle = [sid for sid, s in self._sessions.items() if time.time() - s.last_used > self.idle_seconds]
            for sid in idle:
                self.close(sid)
            _purge_finished()

    def start(self, url: str, width: int = 0, height: int = 0) -> Dict[str, Any]:
        """Opens `url` in a new window, sized to the viewer's screen when `width`/`height` are given (see
        _viewport). Returns {"session_id", "width", "height"}. The slot is reserved before Chrome starts, so
        requests arriving together cannot open more than max_sessions windows."""
        url = _check_url(url)
        if not chrome_available():
            raise LiveBrowserError("The login window needs Chrome, which is not installed on this server.")
        with self._lock:
            if len(self._sessions) + self._starting >= self.max_sessions:
                raise LiveBrowserError("Two login windows are already open. Close one, or wait a few minutes.")
            self._starting += 1
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, daemon=True, name="live-browser-reaper")
                self._reaper.start()
        try:
            sess = _LiveSession(url, self.user_agent, width, height)
            sid = uuid.uuid4().hex
            with self._lock:
                self._sessions[sid] = sess
        finally:
            with self._lock:
                self._starting -= 1
        width, height = sess.width, sess.height
        try:
            size = sess.driver.execute_script("return [innerWidth, innerHeight];") or []
            width, height = int(size[0]) or sess.width, int(size[1]) or sess.height
        except Exception:
            pass
        return {"session_id": sid, "width": width, "height": height}

    def frame(self, session_id: str) -> bytes:
        """The window as a JPEG image."""
        sess = self._session(session_id)
        with sess.lock:
            try:
                sess._follow_tabs()
                shot = sess._cdp("Page.captureScreenshot", {"format": "jpeg", "quality": 60})
                return base64.b64decode(shot.get("data", ""))
            except Exception as e:
                raise LiveBrowserError(f"The window could not be drawn: {str(e).splitlines()[0][:120]}")

    def current_url(self, session_id: str) -> str:
        """The address the window shows now ('' when unknown)."""
        sess = self._session(session_id)
        with sess.lock:
            return sess.current_url()

    def action(self, session_id: str, act: Dict[str, Any]) -> Dict[str, Any]:
        """Replays one input: {"type": "click"|"type"|"key"|"scroll"|"navigate"|"back", "x", "y", "text",
        "key", "dy", "url"}. Returns {"ok", "current_url"}."""
        sess = self._session(session_id)
        kind = str(act.get("type") or "")
        x, y = float(act.get("x") or 0), float(act.get("y") or 0)
        with sess.lock:
            try:
                sess._follow_tabs()
                if kind == "click":
                    # Move first: some widgets (e.g. Turnstile) ignore clicks without pointer movement
                    sess._cdp("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                    for phase in ("mousePressed", "mouseReleased"):
                        sess._cdp("Input.dispatchMouseEvent",
                                  {"type": phase, "x": x, "y": y, "button": "left", "clickCount": 1})
                elif kind == "type" and act.get("text"):
                    sess._cdp("Input.insertText", {"text": str(act["text"])})
                elif kind == "key":
                    key = str(act.get("key") or "")
                    if key in _SPECIAL_KEYS:
                        code = _SPECIAL_KEYS[key]
                        down = {"type": "rawKeyDown", "key": key, "code": key, "windowsVirtualKeyCode": code}
                        if key == "Enter":
                            down.update(type="keyDown", text="\r")  # a "char" Enter is what submits forms
                        sess._cdp("Input.dispatchKeyEvent", down)
                        sess._cdp("Input.dispatchKeyEvent",
                                  {"type": "keyUp", "key": key, "code": key, "windowsVirtualKeyCode": code})
                    elif len(key) == 1:
                        sess._cdp("Input.insertText", {"text": key})
                elif kind == "scroll":
                    sess._cdp("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x or sess.width / 2,
                                                           "y": y or sess.height / 2, "deltaX": 0,
                                                           "deltaY": float(act.get("dy") or 0)})
                elif kind == "navigate":
                    sess._get(_check_url(str(act.get("url") or act.get("text") or "")))
                elif kind == "back":
                    sess.driver.back()
                elif kind == "reload":
                    sess.driver.refresh()
                else:
                    return {"ok": False, "current_url": sess.current_url()}
                time.sleep(0.15)
                return {"ok": True, "current_url": sess.current_url()}
            except LiveBrowserError:
                raise
            except Exception:
                return {"ok": False, "current_url": sess.current_url()}

    def finish(self, session_id: str) -> Dict[str, Any]:
        """Keeps the window's login for the next crawl (auth_type "live", live_session_id) and closes the
        window. Returns {"ok", "cookies_count", "storage_origins", "current_url"}."""
        sess = self._session(session_id)
        with sess.lock:
            current = sess.current_url()
            try:
                state = export_session(sess.driver, current or sess.start_url)
                if sess.start_url and current and urlparse(current).hostname != urlparse(sess.start_url).hostname:
                    # SSO may end on another host; keep the cookies of the site the window was opened for too
                    extra = export_session(sess.driver, sess.start_url)
                    names = {(c["name"], c["domain"]) for c in state["cookies"]}
                    state["cookies"] += [c for c in extra["cookies"] if (c["name"], c["domain"]) not in names]
                    for kind in ("local", "session"):
                        state[kind].update(extra.get(kind) or {})
            except Exception:
                state = {"cookies": [], "local": {}, "session": {}}
        self.close(session_id)
        ok = bool(state["cookies"]) or bool(storage_origins(state))
        if ok:
            with _finished_lock:
                _finished[session_id] = (time.time(), state)
        return {"ok": ok, "cookies_count": len(state["cookies"]), "storage_origins": storage_origins(state),
                "current_url": current}

    def close(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sess = self._sessions.pop(session_id or "", None)
        if sess is not None:
            close_chrome(sess.driver)
        return {"ok": sess is not None}

    def active(self) -> int:
        with self._lock:
            return len(self._sessions)


_manager: Optional[LiveBrowserManager] = None
_manager_lock = threading.Lock()


def get_live_manager() -> LiveBrowserManager:
    """The process-wide manager (the API endpoints share it)."""
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = LiveBrowserManager()
        return _manager
