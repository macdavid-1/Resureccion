"""Shared FastAPI helpers: auth guard used by every route module."""
from __future__ import annotations

from fastapi import Request

from app.security import AuthError, AuthService

AUTH_COOKIE_NAME = "resurreccion_session"


def get_auth(request: Request) -> AuthService:
    return request.app.state.auth


def require_owner_sync(request: Request) -> None:
    """Raise AuthError unless the request carries a valid owner token.

    Accepts the token via the `X-Auth-Token` header (API clients) or the
    HttpOnly auth cookie (browser dashboard).
    """
    auth = get_auth(request)
    token = request.headers.get("X-Auth-Token") or request.cookies.get(AUTH_COOKIE_NAME)
    if not token or not auth.verify_token(token):
        raise AuthError("Authentication required", status=401)
