"""Auth routes: login, logout, whoami."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.routes.deps import AUTH_COOKIE_NAME, get_auth, require_owner_sync
from app.security import AuthError

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginRequest, request: Request, response: Response) -> dict:
    service = get_auth(request)
    try:
        token = service.login(body.username, body.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    ttl = service.config.auth_session_ttl_hours * 3600
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=ttl,
        httponly=True,
        samesite="lax",
        secure=False,  # set True behind TLS in production if desired
    )
    return {"ok": True, "token": token}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    service = get_auth(request)
    token = request.cookies.get(AUTH_COOKIE_NAME) or request.headers.get("X-Auth-Token")
    if token:
        service.logout(token)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    try:
        require_owner_sync(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    service = get_auth(request)
    return {"username": service.config.owner_username}
