"""Owner-only browser infrastructure routes.

All endpoints require single-owner auth. Every response passes through
`redact()` — no cookies, tokens, headers, or profile contents are ever
returned. Login-window endpoints additionally require the shared
BROWSER_LOGIN_SECRET when configured, because they open an interactive
browser session on the server.
"""
from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import quote_plus

from fastapi import APIRouter, File, Form, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from app.amazon_auth import AmazonAuthError, AmazonAuthManager
from app.book_scout import BookScoutManager
from app.browser_manager import BrowserManager, BrowserManagerError
from app.browser_store import BrowserEvidenceStore, LoginWindowStore
from app.interactive import InteractiveError, InteractiveSessionManager
from app.kdspy import ExtensionInstallError, KDSpyError, KDSpyManager
from app.marketplace import (
    AMAZON_MARKETPLACES,
    MarketplaceError,
    get_marketplace,
    resolve_plan,
    signin_marketplaces,
)
from app.redact import redact, scrub_url
from app.routes.deps import require_owner_sync
from app.security import AuthError

router = APIRouter(prefix="/api/browser", tags=["browser"])

# Extension uploads accept larger multipart bodies than the JSON default.
_MAX_EXT_FILES = 200


def _auth(request: Request) -> None:
    try:
        require_owner_sync(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _browser(request: Request) -> BrowserManager:
    return request.app.state.browser_manager


def _amazon_auth(request: Request) -> AmazonAuthManager:
    return request.app.state.amazon_auth


def _kdspy(request: Request) -> KDSpyManager:
    return request.app.state.kdspy


def _book_scout(request: Request) -> BookScoutManager:
    return request.app.state.book_scout


def _interactive(request: Request) -> InteractiveSessionManager:
    return request.app.state.interactive


def _require_login_secret(request: Request, provided: str | None) -> None:
    expected = request.app.state.config.browser_login_window_secret
    if expected and not provided:
        raise HTTPException(status_code=403, detail="browser login secret required")
    if expected and provided and not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="invalid browser login secret")


def _marketplace_or_400(identifier: str):
    try:
        return get_marketplace(identifier)
    except MarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ------------------------------------------------------------------- status
@router.get("/status")
async def status(request: Request) -> dict:
    _auth(request)
    mgr = _browser(request)
    return {"status": mgr.status().to_dict()}


@router.post("/launch")
async def launch(request: Request) -> dict:
    _auth(request)
    try:
        st = await _browser(request).launch()
    except BrowserManagerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": st.to_dict()}


@router.post("/shutdown")
async def shutdown(request: Request) -> dict:
    _auth(request)
    await _browser(request).shutdown()
    return {"status": _browser(request).status().to_dict()}


# -------------------------------------------------------------------- auth
@router.get("/auth/amazon")
async def amazon_auth_state(request: Request) -> dict:
    _auth(request)
    return {"auth": _amazon_auth(request).current_state().to_dict()}


@router.get("/auth/amazon/signin-options")
async def amazon_signin_options(request: Request) -> dict:
    """The seven marketplaces Settings offers a Sign-in button for.

    Base domains come from the catalog (never hardcoded in the frontend), so
    a button can never open the wrong store.
    """
    _auth(request)
    return {
        "options": [
            {"code": m.code, "name": m.name, "domain": m.domain, "base_url": m.base_url}
            for m in signin_marketplaces()
        ]
    }


@router.post("/auth/amazon/check")
async def amazon_auth_check(request: Request, body: MarketplaceRequest) -> dict:
    _auth(request)
    mkt = _marketplace_or_400(body.marketplace)
    try:
        check = await _amazon_auth(request).check(mkt)
    except BrowserManagerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"check": redact(check.to_dict())}


@router.post("/auth/amazon/login-window")
async def open_login_window(
    request: Request,
    body: MarketplaceRequest,
    x_login_secret: str | None = Header(default=None),
) -> dict:
    _auth(request)
    _require_login_secret(request, x_login_secret)
    mkt = _marketplace_or_400(body.marketplace)
    try:
        result = await _amazon_auth(request).open_login_window(mkt)
    except AmazonAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return redact(result)


@router.post("/auth/amazon/login-window/{window_id}/complete")
async def complete_login_window(request: Request, window_id: str, body: MarketplaceRequest) -> dict:
    _auth(request)
    mkt = _marketplace_or_400(body.marketplace)
    mgr: AmazonAuthManager = _amazon_auth(request)
    try:
        state = await mgr.complete_login_window(window_id, mkt)
    except AmazonAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"auth": state.to_dict()}


@router.post("/auth/amazon/login-window/{window_id}/cancel")
async def cancel_login_window(request: Request, window_id: str) -> dict:
    _auth(request)
    try:
        _amazon_auth(request).cancel_login_window(window_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.get("/auth/amazon/login-windows")
async def list_login_windows(request: Request) -> dict:
    _auth(request)
    windows: LoginWindowStore = request.app.state.login_windows
    return {"windows": [w.to_dict() for w in windows.list()]}


@router.post("/auth/amazon/cookies")
async def import_cookies(request: Request, body: CookieImportRequest) -> dict:
    _auth(request)
    mkt = _marketplace_or_400(body.marketplace)
    try:
        state = await _amazon_auth(request).import_cookies(mkt, body.cookies)
    except AmazonAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BrowserManagerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"auth": state.to_dict()}


# -------------------------------------------------------------------- kdspy
@router.get("/extensions/kdspy")
async def kdspy_state(request: Request) -> dict:
    _auth(request)
    mgr = _kdspy(request)
    return {
        "extension": mgr.state().to_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        "engine": request.app.state.config.browser_engine,
    }


@router.post("/extensions/kdspy/validate")
async def kdspy_validate(request: Request) -> dict:
    _auth(request)
    mgr = _kdspy(request)
    try:
        state = mgr.validate_installation()
    except KDSpyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"extension": state.to_dict()}


@router.post("/extensions/kdspy/install-xpi")
async def kdspy_install_xpi(request: Request, file: UploadFile = File(...)) -> dict:
    """Install the KDSpy Firefox add-on (XPI) for the camoufox engine.

    The engine loads extracted add-ons natively at launch; the browser is
    restarted (when research is idle) so the new add-on is picked up.
    """
    _auth(request)
    mgr = _kdspy(request)
    data = await file.read()
    try:
        info = mgr.install_firefox_xpi(data)
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "source": "firefox_xpi",
        "manifest": info.to_safe_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        "note": "Firefox add-on installed — loads on the camoufox engine at next launch",
    }


@router.post("/extensions/kdspy/install-amo")
async def kdspy_install_amo(request: Request) -> dict:
    """One-tap install: fetch the KDSpy Firefox add-on from Mozilla Add-ons.

    Downloads the owner-verified XPI from addons.mozilla.org (URL pinned in
    config), validates it with the same zip-slip / manifest / version
    defenses as an upload, installs atomically, and relaunches the browser.
    The UI then opens the setup browser for the kdspy.com license sign-in.
    """
    _auth(request)
    mgr = _kdspy(request)
    try:
        info = await mgr.install_from_amo()
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "source": "mozilla_addons",
        "manifest": info.to_safe_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        "note": "Firefox add-on installed — loads on the camoufox engine; sign in to kdspy.com next",
    }


@router.post("/extensions/kdspy/install")
async def kdspy_install_zip(request: Request, file: UploadFile = File(...)) -> dict:
    """Install the KDSpy Pro extension from a ZIP upload.

    The browser must be restarted afterwards to load the new extension; the
    endpoint restarts it automatically when idle.
    """
    _auth(request)
    mgr = _kdspy(request)
    data = await file.read()
    try:
        info = mgr.install_from_zip(data)
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "manifest": info.to_safe_dict(),
        "extension": mgr.state().to_dict(),
        "note": "restart the browser (or reload the setup browser) to load the extension",
    }


@router.post("/extensions/kdspy/install-files")
async def kdspy_install_files(
    request: Request,
    files: list[UploadFile] = File(...),
    paths: str = Form(default=""),
) -> dict:
    """Install from a multi-file upload (mobile folder pickers).

    `paths` is a newline-separated list of relative paths matching `files`
    in order (mobile FormData cannot express subdirectories natively).
    """
    _auth(request)
    mgr = _kdspy(request)
    if not files or len(files) > _MAX_EXT_FILES:
        raise HTTPException(status_code=400, detail="provide between 1 and 200 files")
    rels = [p for p in (paths or "").splitlines() if p.strip()]
    if len(rels) != len(files):
        # Fall back to bare filenames when path counts mismatch.
        rels = [f.filename or f"file{i}" for i, f in enumerate(files)]
    payload: list[tuple[str, Any]] = []
    try:
        for rel, f in zip(rels, files):
            payload.append((rel, f.file))
        info = mgr.install_from_files(payload)
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        for f in files:
            await f.close()
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "manifest": info.to_safe_dict(),
        "extension": mgr.state().to_dict(),
        "note": "restart the browser (or reload the setup browser) to load the extension",
    }


# --------------------------------------------------------------- book scout
@router.get("/extensions/book-scout")
async def book_scout_state(request: Request) -> dict:
    """Scrivy Book Scout (BSR on search results): installed/loaded state."""
    _auth(request)
    mgr = _book_scout(request)
    st = mgr.firefox_addon_state_stored()
    return {
        "extension": st.to_dict(),
        "firefox_addon": st.to_dict(),
        "engine": request.app.state.config.browser_engine,
        "amo_url": mgr.amo_url,
        "addon": mgr.name,
        "purpose": "BSR for every result on an Amazon book search page",
        # The vendor package ships in the repo: install needs no account and
        # no network, which is the path the Settings button takes.
        "bundled": {
            "available": mgr.bundled_xpi.is_file(),
            "path": str(mgr.bundled_xpi),
        },
    }


@router.post("/extensions/book-scout/install-bundled")
async def book_scout_install_bundled(request: Request) -> dict:
    """Install the add-on from the package shipped with the app."""
    _auth(request)
    mgr = _book_scout(request)
    try:
        info = mgr.install_bundled()
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "source": "bundled",
        "manifest": info.to_safe_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        "note": "Scrivy Book Scout installed from the bundled package",
    }


@router.post("/extensions/book-scout/install-amo")
async def book_scout_install_amo(request: Request) -> dict:
    """Install/update the add-on from Mozilla Add-ons, falling back to the
    bundled package when the network is unavailable."""
    _auth(request)
    mgr = _book_scout(request)
    try:
        info = await mgr.install_from_amo()
    except ExtensionInstallError as exc:
        if not mgr.bundled_xpi.is_file():
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        try:
            info = mgr.install_bundled()
        except ExtensionInstallError as exc2:
            raise HTTPException(status_code=502, detail=f"{exc}; bundled: {exc2}") from exc2
        await _maybe_restart_browser(request)
        return {
            "installed": True,
            "source": "bundled_fallback",
            "manifest": info.to_safe_dict(),
            "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        }
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "source": "mozilla_addons",
        "manifest": info.to_safe_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
        "note": "Scrivy Book Scout installed — loads on the camoufox engine at next launch",
    }


@router.post("/extensions/book-scout/install-xpi")
async def book_scout_install_xpi(request: Request, file: UploadFile = File(...)) -> dict:
    _auth(request)
    mgr = _book_scout(request)
    data = await file.read()
    try:
        info = mgr.install_firefox_xpi(data)
    except ExtensionInstallError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await _maybe_restart_browser(request)
    return {
        "installed": True,
        "manifest": info.to_safe_dict(),
        "firefox_addon": mgr.firefox_addon_state_stored().to_dict(),
    }


@router.delete("/extensions/book-scout")
async def book_scout_remove(request: Request) -> dict:
    _auth(request)
    mgr = _book_scout(request)
    try:
        mgr.remove()
    except KDSpyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = mgr.validate_installation()
    await _maybe_restart_browser(request)
    return {"removed": True, "extension": state.to_dict()}


class BookScoutVerifyRequest(BaseModel):
    query: str = "grief memoir"
    wait_seconds: float = Field(default=60.0, ge=5.0, le=180.0)


@router.post("/extensions/book-scout/verify")
async def book_scout_verify(
    request: Request, body: BookScoutVerifyRequest | None = None
) -> dict:
    """Live check: open a real Amazon book search and read the add-on's badges.

    This is the only honest way to answer "is it working" — it launches the
    browser, loads the add-on and reads what it actually renders. Nothing is
    cached or inferred: the response carries the counts, a few real badges and
    the niche snapshot the add-on produced.
    """
    _auth(request)
    req = body or BookScoutVerifyRequest()
    query = " ".join((req.query or "").split()) or "grief memoir"
    mgr = _browser(request)
    from app.bsr_scan import scan_page_bsr
    from app.evidence_capture import (
        EvidenceCaptureError,
        book_scout_present,
        read_book_scout,
    )

    mkt = get_marketplace("us")
    page = None
    counts: dict[str, int] = {}
    sample: list[dict] = []
    niche: list[dict] = []
    source = "addon"
    note = ""
    try:
        await mgr.launch()
        page = await mgr.open_marketplace(
            mkt, f"/s?k={quote_plus(query)}&i=stripbooks"
        )
        # The add-on is a convenience, not a requirement: when it is not
        # running, the same BSR is read from each result's product page in this
        # same browser session. The response says which path produced it.
        if await book_scout_present(page):
            data = await read_book_scout(page, wait_seconds=float(req.wait_seconds))
            counts = data.get("counts") or {}
            sample = [b for b in (data.get("badges") or []) if b.get("bsr")][:8]
            niche = (data.get("niche") or {}).get("stats") or []
            note = data.get("note") or ""
            url = data.get("url")
        else:
            native = await scan_page_bsr(page, max_items=12)
            if not native.get("with_bsr"):
                raise EvidenceCaptureError(
                    "The add-on is not loaded and Amazon returned no product "
                    "page for the results"
                    + (f" ({native.get('blocked')} robot check)" if native.get("blocked") else "")
                    + " — nothing was measured. Retry once the page loads "
                    "normally, or install the add-on."
                )
            source = "product_pages"
            counts = {
                "badges": native["scanned"],
                "with_bsr": native["with_bsr"],
                "unavailable": native["failed"],
                "indie": sum(1 for r in native["rows"] if r.get("indie")),
            }
            sample = [
                {
                    "asin": r["asin"],
                    "bsr": f"#{r['rank']:,}" if r.get("rank") else "",
                    "title": r.get("title") or "",
                    "category": r.get("category") or "",
                    "stats": [{"label": "BSR", "value": f"#{r['rank']:,}"}]
                    if r.get("rank")
                    else [],
                }
                for r in native["rows"]
                if r.get("rank")
            ][:8]
            url = scrub_url(page.url)
            note = (
                "Add-on not loaded: these ranks were read from each result's "
                "own product page (no estimated sales/revenue)."
            )
    except BrowserManagerError as exc:
        raise HTTPException(status_code=503, detail=f"browser unavailable: {exc}") from exc
    except EvidenceCaptureError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if page is not None:
            try:
                await mgr.close_page(page)
            except Exception:
                pass
    return {
        "ok": True,
        "query": query,
        "marketplace": mkt.code,
        "source": source,
        "url": url,
        "counts": counts,
        "niche": niche,
        "sample": sample,
        "note": note,
    }


@router.delete("/extensions/kdspy")
async def kdspy_remove(request: Request) -> dict:
    _auth(request)
    mgr = _kdspy(request)
    try:
        mgr.remove()
    except KDSpyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    state = mgr.validate_installation()
    await _maybe_restart_browser(request)
    return {"removed": True, "extension": state.to_dict()}


async def _maybe_restart_browser(request: Request) -> None:
    """Relaunch the shared Camoufox browser so a newly installed/removed
    Firefox add-on is picked up. Only restarts when no research page is open
    (never yank the browser out from under a running session)."""
    mgr: BrowserManager = _browser(request)
    interactive: InteractiveSessionManager = _interactive(request)
    if interactive.is_research_busy():
        return
    try:
        await mgr.launch(force=True)
    except BrowserManagerError:
        pass  # launch errors surface on the next status check


# ----------------------------------------------------------------- interactive
@router.post("/interactive/start")
async def interactive_start(request: Request, body: InteractiveStartRequest) -> dict:
    """Open an owner-controlled browser tab (persistent research profile)."""
    _auth(request)
    mgr: InteractiveSessionManager = _interactive(request)
    purpose = body.purpose if body.purpose in ("amazon_signin", "kdspy_setup", "manual") else "manual"
    try:
        s = await mgr.start(purpose=purpose, marketplace=body.marketplace or "us", ttl_seconds=body.ttl_seconds)
    except InteractiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"session": s.to_dict()}


@router.get("/interactive/frame")
async def interactive_frame(request: Request) -> dict:
    import base64

    _auth(request)
    mgr: InteractiveSessionManager = _interactive(request)
    data = await mgr.frame()
    if data is None:
        raise HTTPException(status_code=404, detail="no interactive frame available")
    return {"frame": base64.b64encode(data).decode("ascii")}


@router.post("/interactive/action")
async def interactive_action(request: Request, body: InteractiveActionRequest) -> dict:
    _auth(request)
    mgr: InteractiveSessionManager = _interactive(request)
    try:
        s = await mgr.act(body.action, body.args or {})
    except InteractiveError as exc:
        code = 409 if "expired" in str(exc) or "no open" in str(exc) else 400
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    return {"session": s.to_dict()}


@router.post("/interactive/complete")
async def interactive_complete(request: Request, body: InteractiveEndRequest) -> dict:
    _auth(request)
    mgr: InteractiveSessionManager = _interactive(request)
    try:
        s = await mgr.complete(outcome=body.outcome or "completed")
    except InteractiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"session": s.to_dict()}


@router.get("/interactive/state")
async def interactive_state(request: Request) -> dict:
    _auth(request)
    s = _interactive(request).current()
    return {"session": s.to_dict() if s else None}


# -------------------------------------------------------------- marketplaces
@router.get("/marketplaces")
async def list_marketplaces(request: Request) -> dict:
    _auth(request)
    return {
        "marketplaces": [
            {
                "code": m.code,
                "name": m.name,
                "domain": m.domain,
                "currency": m.currency,
                "language": m.language,
                "region": m.region,
            }
            for m in AMAZON_MARKETPLACES.values()
        ]
    }


@router.post("/marketplaces/resolve")
async def resolve_marketplaces(request: Request, body: MarketplacePlanRequest) -> dict:
    _auth(request)
    try:
        mode, marketplaces = resolve_plan(body.marketplaces or [])
    except MarketplaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "mode": mode,
        "marketplaces": [
            {"code": m.code, "name": m.name, "domain": m.domain} for m in marketplaces
        ],
    }


# ----------------------------------------------------------------- evidence
@router.get("/evidence")
async def list_browser_evidence(request: Request, session_id: str, limit: int = 100) -> dict:
    _auth(request)
    store: BrowserEvidenceStore = request.app.state.browser_evidence
    try:
        request.app.state.sessions.require(session_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="session not found") from exc
    rows = store.list(session_id, limit=min(limit, 500))
    return {"evidence": [r.to_dict() for r in rows]}


# ------------------------------------------------------------------- models
class MarketplaceRequest(BaseModel):
    marketplace: str = Field(description="marketplace code or domain, e.g. 'us' or 'amazon.com'")

class MarketplacePlanRequest(BaseModel):
    marketplaces: list[str] | None = None

class CookieImportRequest(BaseModel):
    marketplace: str
    cookies: list[dict] = Field(description="Netscape/JSON cookie objects for amazon.* domains only")

class InteractiveStartRequest(BaseModel):
    purpose: str = Field(default="manual", description="amazon_signin | kdspy_setup | manual")
    marketplace: str = Field(default="us", description="marketplace code for amazon_signin")
    ttl_seconds: int | None = Field(default=None, ge=60, le=2700)

class InteractiveActionRequest(BaseModel):
    action: str = Field(description="click | type | key | scroll | navigate")
    args: dict = Field(default_factory=dict)

class InteractiveEndRequest(BaseModel):
    outcome: str = Field(default="completed", description="completed | cancelled")
