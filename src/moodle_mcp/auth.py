"""SSO login to Moodle (e.g. moodle.vse.cz) that yields a "Moodle mobile web service" token.

Ported from the Moodle VSE IntelliJ plugin (https://github.com/Michus001/vse-moodle-idea-plugin),
which logs in exactly like the official Moodle mobile app:

1. ``tool_mobile_get_public_config`` -> ``launch.php`` URL and SSO provider.
2. ``launch.php?service=moodle_mobile_app&passport=...`` stores the passport in a cookie. Sites with
   ``typeoflogin = 1`` (VSE) answer with an error page here, which is expected.
3. The browser is sent to the SSO provider and the user logs in.
4. Moodle returns to ``launch.php``, which redirects to ``moodlemobile://token=<base64>``.
5. We intercept that redirect, verify ``md5(site + passport)`` and test the token.

The browser part needs Playwright (``pip install moodle-mcp[login]``); everything else is pure Python.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote

import requests

from .logger import logger

MOBILE_SERVICE = "moodle_mobile_app"
DEFAULT_URL_SCHEME = "moodlemobile"
LOGIN_TIMEOUT_SECONDS = 300

_TOKEN_URL_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://+token=(.*)$", re.DOTALL)
_TOKEN_IN_TEXT_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*)://+token=([A-Za-z0-9+/=%_\-]+)")
_WEB_SCHEMES = {"http", "https", "about", "data", "blob", "file", "javascript"}


class LoginError(Exception):
    """Raised when the SSO login cannot be completed."""


@dataclass(frozen=True)
class MoodleToken:
    token: str
    private_token: str | None = None


def normalize_site(site_url: str) -> str:
    """``"https://moodle.vse.cz/ "`` -> ``"https://moodle.vse.cz"``."""
    return site_url.strip().rstrip("/")


def site_from_rest_url(rest_url: str) -> str:
    """``https://x/webservice/rest/server.php`` -> ``https://x``."""
    return normalize_site(rest_url.split("/webservice/")[0])


def rest_url(site_url: str) -> str:
    return f"{normalize_site(site_url)}/webservice/rest/server.php"


# ---------------------------------------------------------------------------
# Launch redirect parsing
# ---------------------------------------------------------------------------


def find_token_url(text: str | None) -> str | None:
    """First ``<scheme>://token=...`` link in ``text`` (ignoring ordinary web schemes)."""
    for m in _TOKEN_IN_TEXT_RE.finditer(text or ""):
        if m.group(1).lower() not in _WEB_SCHEMES:
            return m.group(0)
    return None


def is_launch_redirect(url: str) -> bool:
    m = _TOKEN_URL_RE.match(url.strip())
    return bool(m) and m.group(1).lower() not in _WEB_SCHEMES


def site_hash(site_url: str, passport: str) -> str:
    return hashlib.md5((site_url + passport).encode("utf-8")).hexdigest()


def _decode_base64(encoded: str) -> str:
    encoded += "=" * (-len(encoded) % 4)
    try:
        return base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception:
        try:
            return base64.urlsafe_b64decode(encoded).decode("utf-8")
        except Exception as e:
            raise LoginError("Token v přesměrování není platný Base64.") from e


def parse_launch_token(url: str, passport: str, site_urls: list[str | None]) -> MoodleToken:
    """Decode ``<scheme>://token=<base64(siteHash:::token[:::privateToken])>`` and verify the hash."""
    m = _TOKEN_URL_RE.match(url.strip())
    if not m or m.group(1).lower() in _WEB_SCHEMES:
        raise LoginError("Adresa přesměrování neobsahuje token.")
    encoded = re.split(r"[?#&]", m.group(2), maxsplit=1)[0]
    encoded = unquote(encoded).strip().rstrip("/").strip()
    if not encoded:
        raise LoginError("Token v přesměrování je prázdný.")

    parts = _decode_base64(encoded).split(":::")
    if len(parts) not in (2, 3) or not parts[0].strip() or not parts[1].strip():
        raise LoginError("Token má neočekávaný formát.")

    received = parts[0].strip().lower()
    candidates = {
        variant
        for s in site_urls
        if s and s.strip()
        for variant in (normalize_site(s), normalize_site(s) + "/")
    }
    ok = False
    for candidate in candidates:  # no early exit: constant-ish time
        ok |= hmac.compare_digest(site_hash(candidate, passport), received)
    if not ok:
        raise LoginError(
            "Token nebyl vydán pro tento Moodle nebo tento pokus o přihlášení (nesouhlasí kontrolní součet)."
        )
    private = parts[2].strip() if len(parts) == 3 and parts[2].strip() else None
    return MoodleToken(parts[1].strip(), private)


# ---------------------------------------------------------------------------
# Moodle calls
# ---------------------------------------------------------------------------


def get_public_config(site_url: str) -> dict:
    site = normalize_site(site_url)
    rsp = requests.post(
        f"{site}/lib/ajax/service-nologin.php",
        json=[{"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}],
        timeout=30,
    )
    rsp.raise_for_status()
    data = rsp.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], dict) or data[0].get("error"):
        raise LoginError(f"Moodle nevrátil veřejnou konfiguraci: {str(data)[:200]}")
    config = data[0]["data"]
    if config.get("enablewebservices") != 1 or config.get("enablemobilewebservice") != 1:
        raise LoginError("Na tomto Moodle nejsou zapnuté mobilní webové služby, SSO přihlášení není možné.")
    return config


def verify_token(site_url: str, token: str) -> dict:
    """Return ``core_webservice_get_site_info`` or raise ``LoginError`` if Moodle rejects the token."""
    rsp = requests.post(
        f"{normalize_site(site_url)}/webservice/rest/server.php",
        data={"wstoken": token, "wsfunction": "core_webservice_get_site_info", "moodlewsrestformat": "json"},
        timeout=30,
    )
    rsp.raise_for_status()
    info = rsp.json()
    if isinstance(info, dict) and ("exception" in info or "errorcode" in info):
        raise LoginError(f"Moodle token odmítl: {info.get('errorcode')} {info.get('message')}")
    return info


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _LoginRequest:
    site: str
    passport: str
    launch_url: str
    launch_path: str
    login_url: str
    hash_sites: list[str | None]


def _build_request(site_url: str, config: dict) -> _LoginRequest:
    site = normalize_site(site_url)
    passport = secrets.token_hex(16)
    launch_base = config.get("launchurl") or f"{site}/admin/tool/mobile/launch.php"
    sep = "&" if "?" in launch_base else "?"
    launch_url = (
        f"{launch_base}{sep}service={MOBILE_SERVICE}&passport={passport}"
        f"&urlscheme={quote(DEFAULT_URL_SCHEME)}"
    )
    providers = [
        p for p in config.get("identityproviders") or [] if str(p.get("url", "")).startswith(("http://", "https://"))
    ]
    # With a single SSO provider go straight to it; otherwise let the user choose on the login page.
    login_url = providers[0]["url"] if len(providers) == 1 else f"{site}/login/index.php"
    return _LoginRequest(
        site=site,
        passport=passport,
        launch_url=launch_url,
        launch_path=launch_base.split("?")[0],
        login_url=login_url,
        hash_sites=[site, config.get("wwwroot")],
    )


def _open_browser(playwright, channel: str | None):
    """Prefer the user's installed Chrome: Playwright's bundled Chromium is more easily flagged by SSO."""
    candidates = [channel] if channel else ["chrome", "msedge", None]
    last: Exception | None = None
    for ch in candidates:
        try:
            return playwright.chromium.launch(headless=False, channel=ch)
        except Exception as e:  # browser not installed
            last = e
            logger.info(f"Browser channel {ch!r} unavailable: {e}")
    raise LoginError(
        "Nepodařilo se spustit prohlížeč. Nainstaluj Google Chrome nebo spusť "
        "`python -m playwright install chromium`."
    ) from last


def _browser_login(req: _LoginRequest, channel: str | None, timeout: int) -> str:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise LoginError("Chybí Playwright: nainstaluj `pip install 'moodle-mcp[login]'`.") from e

    state: dict = {"token_url": None, "launch_hits": 0}

    def capture(value: str | None) -> None:
        if value and not state["token_url"]:
            found = find_token_url(value)
            if found:
                state["token_url"] = found

    def handle_launch(route) -> None:
        # Fetch launch.php ourselves without following redirects so the Location header
        # (moodlemobile://token=...) is readable before the browser discards it.
        state["launch_hits"] += 1
        try:
            resp = route.fetch(max_redirects=0)
            capture(resp.headers.get("location", ""))
            if not state["token_url"] and resp.status == 200:
                capture(resp.text())
            if state["token_url"]:
                route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<h2>Token získán, okno se zavře.</h2>",
                )
            else:
                route.fulfill(response=resp)
            return
        except Exception as e:
            logger.debug(f"launch.php handler error: {e!r}")
        try:
            route.continue_()
        except Exception:
            pass

    with sync_playwright() as p:
        browser = _open_browser(p, channel)
        try:
            context = browser.new_context()  # clean profile: launch.php only issues a token right after a fresh login
            context.route(lambda u: u.split("?")[0] == req.launch_path, handle_launch)
            context.on("request", lambda r: capture(r.url))
            context.on("requestfailed", lambda r: capture(r.url))
            page = context.new_page()
            page.on("framenavigated", lambda frame: capture(frame.url))

            # The second launch.php call arrives as a redirect from /auth/oidc/, which Playwright's
            # route does not see. The raw Location header is read through the DevTools protocol, which
            # also reports 3xx responses the browser drops because of the custom scheme.
            cdp = context.new_cdp_session(page)
            cdp.send("Network.enable")

            def location_of(headers):
                return {str(k).lower(): v for k, v in (headers or {}).items()}.get("location")

            cdp.on("Network.responseReceivedExtraInfo", lambda params: capture(location_of(params.get("headers"))))

            def on_request_will_be_sent(params):
                capture(params.get("request", {}).get("url"))
                redirect = params.get("redirectResponse")
                if redirect:
                    capture(location_of(redirect.get("headers")))

            cdp.on("Network.requestWillBeSent", on_request_will_be_sent)

            try:
                page.goto(req.launch_url, wait_until="load")
            except PlaywrightError:
                pass  # navigation to moodlemobile:// is cancelled, which is fine

            if not state["token_url"]:
                # launch.php refuses to start the login itself on VSE but stores the passport in a cookie;
                # after the SSO login Moodle sends the browser back to launch.php, which then issues the token.
                logger.info("Waiting for the user to log in through the opened browser window")
                try:
                    page.goto(req.login_url, wait_until="load")
                except PlaywrightError:
                    pass

            deadline = time.monotonic() + timeout
            while not state["token_url"] and time.monotonic() < deadline:
                if page.is_closed():
                    raise LoginError("Okno prohlížeče bylo zavřeno před získáním tokenu.")
                page.wait_for_timeout(500)
        finally:
            browser.close()

    if not state["token_url"]:
        raise LoginError("Token se nepodařilo získat (vypršel čas).")
    return state["token_url"]


def sso_login(site_url: str, channel: str | None = None, timeout: int = LOGIN_TIMEOUT_SECONDS) -> tuple[MoodleToken, dict]:
    """Run the interactive SSO login and return the verified token plus site info."""
    config = get_public_config(site_url)
    req = _build_request(site_url, config)
    token_url = _browser_login(req, channel, timeout)
    token = parse_launch_token(token_url, req.passport, req.hash_sites)
    info = verify_token(req.site, token.token)
    return token, info


# ---------------------------------------------------------------------------
# .env persistence
# ---------------------------------------------------------------------------


def write_env(path: Path, site_url: str, token: str) -> None:
    """Set ``MOODLE_TOKEN`` (and ``MOODLE_URL`` if missing) in ``path``, keeping other lines."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def upsert(key: str, value: str, overwrite: bool) -> None:
        line = f'{key}="{value}"'
        for i, existing in enumerate(lines):
            if existing.strip().startswith(f"{key}="):
                if overwrite:
                    lines[i] = line
                return
        lines.append(line)

    upsert("MOODLE_TOKEN", token, overwrite=True)
    upsert("MOODLE_URL", rest_url(site_url), overwrite=False)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)  # the token is as good as a password
    except OSError:
        pass
