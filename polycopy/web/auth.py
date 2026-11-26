"""Cookie-session authentication for the dashboard.

Credentials come from WEB_USERNAME / WEB_PASSWORD (Coolify env panel).
Sessions live in memory: a restart invalidates every session.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass

from fastapi import Request, Response

SESSION_COOKIE = "polycopy_session"
DEFAULT_TTL_S = 7 * 24 * 3600

_MAX_FAILURES = 10
_FAILURE_WINDOW_S = 300.0


@dataclass
class Session:
    user: str
    csrf: str
    expires_at: float


_sessions: dict[str, Session] = {}
_failures: dict[str, list[float]] = {}


DEFAULT_WEB_USERNAME = "admin"
# No default password is shipped: the dashboard refuses login until
# WEB_PASSWORD is explicitly configured (Coolify env / .env.docker).
DEFAULT_WEB_PASSWORD = ""


def web_username() -> str:
    return os.environ.get("WEB_USERNAME") or DEFAULT_WEB_USERNAME


def web_password() -> str:
    return os.environ.get("WEB_PASSWORD") or DEFAULT_WEB_PASSWORD


def credentials_configured() -> bool:
    return bool(web_password())


def session_ttl_s() -> int:
    try:
        return max(60, int(os.environ.get("WEB_SESSION_TTL_S", DEFAULT_TTL_S)))
    except (TypeError, ValueError):
        return DEFAULT_TTL_S


def verify_credentials(username: str, password: str) -> bool:
    if not credentials_configured():
        return False
    return (
        secrets.compare_digest(username or "", web_username())
        and secrets.compare_digest(password or "", web_password())
    )


def create_session(user: str) -> str:
    now = time.time()
    _prune(now)
    token = secrets.token_urlsafe(32)
    _sessions[token] = Session(
        user=user, csrf=secrets.token_urlsafe(24), expires_at=now + session_ttl_s()
    )
    return token


def get_session(token: str | None) -> Session | None:
    if not token:
        return None
    session = _sessions.get(token)
    if session is None:
        return None
    if session.expires_at <= time.time():
        _sessions.pop(token, None)
        return None
    return session


def delete_session(token: str | None) -> None:
    if token:
        _sessions.pop(token, None)


def _prune(now: float) -> None:
    for token in [t for t, s in _sessions.items() if s.expires_at <= now]:
        _sessions.pop(token, None)


def current_session(request: Request) -> Session | None:
    return get_session(request.cookies.get(SESSION_COOKIE))


def is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "")
    first = forwarded.split(",")[0].strip().lower()
    return request.url.scheme == "https" or first == "https"


def set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=session_ttl_s(),
        httponly=True,
        samesite="lax",
        secure=is_https(request),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    first = forwarded.split(",")[0].strip()
    if first:
        return first
    return request.client.host if request.client else "unknown"


def login_blocked(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in _failures.get(ip, []) if now - t < _FAILURE_WINDOW_S]
    _failures[ip] = stamps
    return len(stamps) >= _MAX_FAILURES


def record_login_failure(ip: str) -> None:
    _failures.setdefault(ip, []).append(time.time())


def reset_login_failures(ip: str) -> None:
    _failures.pop(ip, None)


def safe_next(value: str | None, default: str = "/") -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return default
