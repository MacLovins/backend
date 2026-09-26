"""HTTP security of the app (SPEC §1.7 «Безопасность», CO-03 P1).

- Startup check: outside dev/test the JWT secret must be real and cookies default to Secure.
- Origin check: cookie-authenticated unsafe requests (POST/PUT/PATCH/DELETE) must come from an allowed origin.
"""

import json
from urllib.parse import urlsplit

import structlog
from leadradar_auth import AuthSettings, auth_settings, insecure_jwt_secret_reason
from starlette.types import ASGIApp, Receive, Scope, Send

from leadradar_core.errors import error_body
from leadradar_core.settings import AppSettings

DEV_ENVS = frozenset({"dev", "test"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})

log = structlog.get_logger(__name__)


class InsecureConfigurationError(RuntimeError):
    """Raised at startup when the app would run outside dev with unsafe settings."""


def is_dev_env(env: str) -> bool:
    return env.strip().lower() in DEV_ENVS


def configure_security(app_settings: AppSettings, auth: AuthSettings = auth_settings) -> None:
    """Validate secrets and resolve environment-dependent auth defaults. Fails fast outside dev."""
    dev = is_dev_env(app_settings.ENV)
    if auth.COOKIE_SECURE is None:
        auth.COOKIE_SECURE = not dev
    if dev:
        return
    reason = insecure_jwt_secret_reason(auth.JWT_SECRET)
    if reason:
        raise InsecureConfigurationError(
            f"APP_ENV={app_settings.ENV}: AUTH_JWT_SECRET is not acceptable ({reason}). "
            "Set a random secret of at least 32 characters, e.g. `openssl rand -hex 32`."
        )
    if not auth.COOKIE_SECURE:
        log.warning("cookie_secure_disabled_outside_dev", env=app_settings.ENV)


def _origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def origin_allowed(origin: str, public_origin: str, env: str) -> bool:
    normalized = _origin_of(origin)
    if normalized is None:
        return False
    if normalized == _origin_of(public_origin):
        return True
    if is_dev_env(env):
        host = urlsplit(normalized).hostname or ""
        return host in _LOCAL_HOSTS or host.endswith(".localhost")
    return False


class OriginCheckMiddleware:
    """Rejects cross-origin unsafe requests authenticated by the session cookie (CSRF defence in depth).

    Requests without the session cookie (bearer tokens, login) are not affected; a browser always sends
    `Origin` on cross-origin POST, so a request with neither `Origin` nor `Referer` is not a browser CSRF.
    """

    def __init__(self, app: ASGIApp, *, app_settings: AppSettings, cookie_name: str) -> None:
        self.app = app
        self.settings = app_settings
        self.cookie_name = cookie_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not self._has_session_cookie(headers.get("cookie", "")):
            await self.app(scope, receive, send)
            return
        origin = headers.get("origin") or (_origin_of(headers["referer"]) if headers.get("referer") else None)
        if origin is None or origin_allowed(origin, self.settings.PUBLIC_ORIGIN, self.settings.ENV):
            await self.app(scope, receive, send)
            return
        log.warning("origin_rejected", origin=origin, path=scope.get("path"))
        body = json.dumps(
            error_body("origin_not_allowed", "Request origin is not allowed", {"origin": origin})
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _has_session_cookie(self, cookie_header: str) -> bool:
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == self.cookie_name and value:
                return True
        return False
