"""Routes for events, uploads, artifacts, reports, exports."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.exports import ExportError
from app.reports import ReportError
from app.routes.deps import require_owner_sync
from app.security import AuthError
from app.uploads import UploadError

router = APIRouter(prefix="/api", tags=["events-uploads-artifacts-reports-exports"])


def _methodology(request: Request) -> Any:
    return _st(request).methodology


class ExportRequest(BaseModel):
    format: str = "tar_gz"


class ReportsPdfRequest(BaseModel):
    # Default (None): every report of the session that has a stored model.
    report_ids: list[str] | None = None
    # False (default): all selected reports in ONE combined PDF.
    # True: one PDF artifact per report (the owner's opt-in).
    separate: bool = False


class ReportRequest(BaseModel):
    kind: str = "intermediate"
    title: str
    body_markdown: str


class ChatRequest(BaseModel):
    message: str


def _auth(request: Request) -> None:
    try:
        require_owner_sync(request)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def _st(request: Request) -> Any:
    return request.app.state


def _session_or_404(request: Request, session_id: str) -> None:
    if _st(request).sessions.get(session_id) is None:
        raise HTTPException(status_code=404, detail="session not found")


def _log_export_failure(st: Any, session_id: str, action: str, reason: Any) -> None:
    """Record WHY an export did not produce a PDF, in the session's own trace.

    The owner only ever sees this through the UI, so a failed export must be
    as auditable as a successful one: the reason (renderer refused, timed out,
    busy) lands next to the research it belongs to.
    """
    try:
        st.events.append(
            session_id, level="warn", actor="system", action=action,
            detail={"reason": str(reason)[:400]},
        )
    except Exception:
        pass  # never let bookkeeping replace the real error


def _report_model(request: Request, session_id: str, rep: Any) -> dict[str, Any]:
    """The report's structured model, rebuilt from durable state if missing.

    The report body is a RENDERING of durable research state, so a missing or
    half-written `.model.json` (an interrupted save, a pruned file) must never
    make a finished report un-exportable — the phase outputs, candidates,
    verifications and evidence are all still in the database. A session with
    no research state to render keeps the original 404.
    """
    st = _st(request)
    model = st.reports.read_model_json(rep)
    if model:
        return model
    if not (
        st.candidates.list(session_id, limit=1)
        or st.opportunities.list(session_id, limit=1)
    ):
        raise HTTPException(status_code=404, detail="no structured model stored for this report")

    from app.methodology import PHASE_ORDER
    from app.report_model import build_report_model

    outputs: dict[str, Any] = {}
    for phase in PHASE_ORDER:
        cp = st.checkpoints.get(session_id, f"phase_output:{phase}")
        if cp is not None:
            outputs[phase] = (cp.payload or {}).get("output") or {}
    pivots_cp = st.checkpoints.get(session_id, "focus_pivots")
    pivots = [
        {k: v for k, v in p.items() if k != "directive"}
        for p in ((pivots_cp.payload or {}).get("pivots") or [])
        if isinstance(p, dict)
    ] if pivots_cp else []
    rebuilt = build_report_model(
        session_id,
        sessions=st.sessions,
        candidates=st.candidates,
        opportunities=st.opportunities,
        verifications=st.verifications,
        claims=st.claims,
        browser_evidence=st.browser_evidence,
        observations=st.observations,
        assessments=st.integrity,
        phase_outputs=outputs,
        focus_pivots=pivots,
    ).to_dict()
    # Persist the rebuild so the next export (and re-render) reads it back.
    try:
        st.reports.save_model_json(
            session_id, report=rep, model_json=json.dumps(rebuilt)
        )
    except Exception:
        pass  # the rebuild itself is what matters; persistence is additive
    return rebuilt


# ------------------------------------------------------------- methodology
@router.get("/methodology")
async def get_methodology(request: Request) -> dict:
    """The deterministic 9-phase research methodology (owner-facing spec)."""
    _auth(request)
    return {"methodology": _methodology(request).describe()}


# ------------------------------------------------------------- system summary
@router.get("/system")
async def system_summary(request: Request) -> dict:
    """Setup readiness for the Settings screen.

    Reports WHICH capabilities are configured without ever exposing secret
    values — only booleans and non-secret model identifiers. This is how the
    owner sees "the model key is missing" instead of a bare paused session.
    """
    _auth(request)
    cfg = _st(request).config
    return {
        "model": {
            "api_key_set": bool(cfg.model_api_key),
            "model": cfg.model_name,
            "base_url": cfg.model_base_url,
            "vision": bool(cfg.model_vision_enabled),
        },
    }


# ---------------------------------------------------------------- integrity
@router.get("/sessions/{session_id}/integrity")
async def get_integrity(
    session_id: str,
    request: Request,
    kind: str | None = None,
    outcome: str | None = None,
    limit: int = 300,
) -> dict:
    """Durable integrity assessments: filters, verification, quality gates,
    KDP risk screenings, report validation — the audit trail of WHY every
    candidate survived or died."""
    _auth(request)
    _session_or_404(request, session_id)
    rows = _st(request).integrity.list(session_id, kind=kind, outcome=outcome, limit=limit)
    return {"assessments": [a.to_dict() for a in rows]}


@router.get("/sessions/{session_id}/claims")
async def get_claims(
    session_id: str,
    request: Request,
    candidate_id: str | None = None,
    status: str | None = None,
    limit: int = 300,
) -> dict:
    """The claim ledger: evidence -> claim -> interpretation chain."""
    _auth(request)
    _session_or_404(request, session_id)
    rows = _st(request).claims.list(
        session_id, candidate_id=candidate_id, status=status, limit=limit
    )
    return {"claims": [c.to_dict() for c in rows]}


# ------------------------------------------------------------------ events
@router.get("/sessions/{session_id}/events")
async def tail_events(session_id: str, request: Request, after_id: int = 0, limit: int = 200) -> dict:
    _auth(request)
    _session_or_404(request, session_id)
    events = _st(request).events.tail(session_id, after_id=after_id, limit=limit)
    return {"events": [e.to_dict() for e in events]}


# ------------------------------------------------------------------ uploads
@router.post("/sessions/{session_id}/uploads")
async def upload_image(session_id: str, request: Request, file: UploadFile) -> dict:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    data = await file.read()
    try:
        up = st.uploads.save(
            session_id,
            filename=file.filename or "upload",
            content_type=file.content_type or "",
            data=data,
        )
    except UploadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"upload": up.to_dict()}


@router.get("/sessions/{session_id}/uploads")
async def list_uploads(session_id: str, request: Request) -> dict:
    _auth(request)
    _session_or_404(request, session_id)
    ups = _st(request).uploads.list(session_id)
    return {"uploads": [u.to_dict() for u in ups]}


@router.get("/sessions/{session_id}/uploads/{upload_id}/file")
async def get_upload_file(session_id: str, upload_id: str, request: Request) -> FileResponse:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    up = st.uploads.get(session_id, upload_id)
    if up is None:
        raise HTTPException(status_code=404, detail="upload not found")
    from pathlib import Path

    path = Path(up.stored_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="upload file missing")
    return FileResponse(path, media_type=up.content_type, filename=up.filename)


# ---------------------------------------------------------------- artifacts
@router.get("/sessions/{session_id}/artifacts")
async def list_artifacts(session_id: str, request: Request, kind: str | None = None) -> dict:
    _auth(request)
    _session_or_404(request, session_id)
    arts = _st(request).artifacts.list(session_id, kind=kind)
    return {"artifacts": [a.to_dict() for a in arts]}


@router.get("/sessions/{session_id}/artifacts/{artifact_id}/file")
async def get_artifact_file(session_id: str, artifact_id: str, request: Request) -> FileResponse:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    art = st.artifacts.get(session_id, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    from pathlib import Path

    path = Path(art.path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="artifact file missing")
    return FileResponse(path, media_type=art.content_type)


def _evidence_page(ev: Any) -> str:
    """A small, self-contained page for one evidence record.

    Report citations link here: the owner clicks a claim and sees the actual
    captured page data (and the screenshot, when one was taken), so every
    statement in the report is auditable without reading a JSON API.
    """
    import html as _html

    h = _html.escape
    data = ev.data or {}
    scalars = []
    blocks = []
    for key, value in data.items():
        label = str(key).replace("_", " ").strip().capitalize()
        if isinstance(value, (dict, list)):
            blocks.append((label, value))
        elif value not in (None, ""):
            scalars.append((label, str(value)))
    shot = ""
    if ev.screenshot_artifact_id:
        src = f"/api/sessions/{h(ev.session_id)}/artifacts/{h(ev.screenshot_artifact_id)}/file"
        shot = (
            f'<figure><img src="{src}" alt="captured screenshot">'
            "<figcaption>Capture taken by the research browser</figcaption></figure>"
        )
    rows = "".join(
        f'<div class="row"><dt>{h(k)}</dt><dd>{h(v)}</dd></div>' for k, v in scalars
    )
    blocks_html = "".join(
        f'<h2>{h(k)}</h2><pre>{h(json.dumps(v, indent=1, ensure_ascii=False, default=str))}</pre>'
        for k, v in blocks
    )
    url = h(ev.url or "")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evidence · {h(ev.title or ev.kind)}</title>
<style>
body{{margin:0;background:#0d0f12;color:#e8e6e1;font:15px/1.6 ui-sans-serif,system-ui,sans-serif}}
main{{max-width:860px;margin:0 auto;padding:28px 18px 60px}}
h1{{font-size:20px;line-height:1.3;margin:0 0 6px}}h2{{font-size:14px;letter-spacing:.08em;text-transform:uppercase;color:#9aa0a6;margin:26px 0 8px}}
.meta{{color:#9aa0a6;font-size:13px;margin-bottom:18px}}.meta a{{color:#c9a227;word-break:break-all}}
dl{{margin:0;border-top:1px solid #24262b}}.row{{display:grid;grid-template-columns:180px 1fr;gap:12px;padding:9px 0;border-bottom:1px solid #24262b}}
dt{{color:#9aa0a6}}dd{{margin:0;word-break:break-word}}
pre{{background:#14171b;border:1px solid #24262b;border-radius:8px;padding:12px;overflow:auto;font-size:12.5px;max-height:60vh}}
figure{{margin:0 0 18px}}img{{width:100%;border-radius:10px;border:1px solid #24262b;display:block}}
figcaption{{color:#9aa0a6;font-size:12.5px;padding-top:6px}}
.badge{{display:inline-block;border:1px solid #33373d;border-radius:999px;padding:1px 9px;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:#c9a227;margin-right:6px}}
</style></head><body><main>
<div class="meta"><span class="badge">{h(ev.kind)}</span><span class="badge">{h(ev.marketplace or "—")}</span>{h(ev.captured_at or "")}</div>
<h1>{h(ev.title or "Captured evidence")}</h1>
<div class="meta">{f'<a href="{url}" target="_blank" rel="noopener">{url}</a>' if url else ''}</div>
{shot}
<dl>{rows}</dl>
{blocks_html}
</main></body></html>"""


@router.get("/sessions/{session_id}/evidence/{evidence_id}", response_class=HTMLResponse)
async def get_evidence_detail(session_id: str, evidence_id: str, request: Request) -> Any:
    """The capture behind a report citation, rendered for a human reader."""
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    ev = st.browser_evidence.get(evidence_id)
    if ev is None or ev.session_id != session_id:
        raise HTTPException(status_code=404, detail="evidence not found")
    return HTMLResponse(_evidence_page(ev))


# ------------------------------------------------------------------ reports
@router.get("/sessions/{session_id}/reports")
async def list_reports(session_id: str, request: Request) -> dict:
    _auth(request)
    _session_or_404(request, session_id)
    reps = _st(request).reports.list(session_id)
    return {"reports": [r.to_dict() for r in reps]}


@router.post("/sessions/{session_id}/reports")
async def save_report(session_id: str, body: ReportRequest, request: Request) -> dict:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    try:
        rep = st.reports.save(
            session_id, kind=body.kind, title=body.title, body_markdown=body.body_markdown
        )
    except ReportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"report": rep.to_dict()}


@router.get("/sessions/{session_id}/reports/{report_id}")
async def get_report(session_id: str, report_id: str, request: Request) -> dict:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    rep = st.reports.get(session_id, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report not found")
    try:
        body = st.reports.read_markdown(rep)
    except ReportError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"report": rep.to_dict(), "body_markdown": body}


@router.get("/sessions/{session_id}/reports/{report_id}/file")
async def download_report(session_id: str, report_id: str, request: Request) -> FileResponse:
    """Download the raw markdown report file."""
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    rep = st.reports.get(session_id, report_id)
    if rep is None or not rep.body_path:
        raise HTTPException(status_code=404, detail="report not found")
    from pathlib import Path

    path = Path(rep.body_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report file missing")
    return FileResponse(path, media_type="text/markdown", filename=path.name)


@router.get("/sessions/{session_id}/reports/{report_id}/model")
async def get_report_model(session_id: str, report_id: str, request: Request) -> dict:
    """The structured report model (JSON) — the editable source of the report."""
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    rep = st.reports.get(session_id, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report not found")
    model = st.reports.read_model_json(rep)
    if model is None:
        raise HTTPException(status_code=404, detail="no structured model stored for this report")
    return {"model": model}


@router.post("/sessions/{session_id}/reports/{report_id}/rerender")
async def rerender_report(session_id: str, report_id: str, request: Request) -> dict:
    """Re-render the markdown body from the stored structured model.

    A model-authored report is NEVER re-rendered by the backend: the stored
    markdown IS the report, so rerendering an AI report returns it unchanged
    (the structured model is kept for PDF figures and evidence indices, not as
    a source of prose). Only legacy sessions, whose report was generated from
    the template, are rendered again.
    """
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    rep = st.reports.get(session_id, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report not found")
    data = st.reports.read_model_json(rep)
    if data is None:
        raise HTTPException(status_code=404, detail="no structured model stored for this report")
    ai = data.get("ai_report") if isinstance(data, dict) else None
    if isinstance(ai, dict) and str(ai.get("markdown") or "").strip():
        return {"report": rep.to_dict(), "body_markdown": str(ai.get("markdown"))}
    # Legacy report (generated before the model wrote the whole document).
    from app.report_model import ReportModel, safe_render_markdown

    body = safe_render_markdown(ReportModel.from_dict(data))
    updated = st.reports.save(
        session_id, kind="final", title=rep.title, body_markdown=body,
        meta={**(rep.meta or {}), "rerendered_from_model": True},
    )
    return {"report": updated.to_dict(), "body_markdown": body}


@router.post("/sessions/{session_id}/reports/{report_id}/export-pdf")
async def export_report_pdf(session_id: str, report_id: str, request: Request) -> dict:
    """Export the report as a designed 6×9in PDF (PDFKit) + report HTML.

    The textual report content is preserved word for word; only the visual
    design is applied. Both artifacts persist under the session.
    """
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    rep = st.reports.get(session_id, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report not found")
    # The model may have to be rebuilt from durable state; that is heavy
    # synchronous work, so it runs in the worker thread too — a slow rebuild
    # must never freeze the event loop (and every other tab with it).
    model = await run_in_threadpool(_report_model, request, session_id, rep)
    from app.pdf_export import EXPORT_DEADLINE_SECONDS, PdfExportError, export_pdf

    try:
        # Rendered in a worker thread: PDFKit blocks for seconds on a large
        # report, and doing that on the event loop froze the whole app —
        # frames, SSE and every other request stalled for the duration. The
        # deadline means the request ALWAYS answers: a renderer that will not
        # finish is reported, never waited on forever.
        result = await asyncio.wait_for(
            run_in_threadpool(
                export_pdf,
                st.db, st.config, session_id,
                model=model, artifacts=st.artifacts,
            ),
            timeout=EXPORT_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        _log_export_failure(st, session_id, "report_pdf_export_timeout", EXPORT_DEADLINE_SECONDS)
        raise HTTPException(
            status_code=503,
            detail=(
                f"PDF export took longer than {EXPORT_DEADLINE_SECONDS}s and was "
                "reported as failed so it could not hang the app. Retry when "
                "nothing else is running on the server."
            ),
        ) from exc
    except PdfExportError as exc:
        _log_export_failure(st, session_id, "report_pdf_export_failed", str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    st.events.append(
        session_id, level="info", actor="system", action="report_pdf_exported",
        detail={"report_id": report_id, "bytes": result["bytes"]},
    )
    return result


@router.post("/sessions/{session_id}/reports/export-pdf")
async def export_reports_pdf(body: ReportsPdfRequest, session_id: str, request: Request) -> dict:
    """Export the session's reports as designed 6×9in PDFs.

    All selected reports go into ONE combined PDF by default (each report
    introduced by its own section page); the owner opts into separate=true
    for one PDF per report. Optional report_ids narrows the selection.
    """
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    reports = st.reports.list(session_id)
    if body.report_ids:
        wanted = set(body.report_ids)
        reports = [r for r in reports if r.id in wanted]
        if not reports:
            raise HTTPException(status_code=404, detail="no matching reports")
    chosen = []
    models = []
    for r in reports:
        try:
            models.append(await run_in_threadpool(_report_model, request, session_id, r))
        except HTTPException as exc:
            if exc.status_code == 404:
                continue  # nothing to render from this report
            raise
        chosen.append(r)
    if not chosen:
        raise HTTPException(status_code=404, detail="no reports with a stored model")
    from app.pdf_export import EXPORT_DEADLINE_SECONDS, PdfExportError, export_reports

    try:
        result = await asyncio.wait_for(
            run_in_threadpool(
                export_reports,
                st.db, st.config, session_id,
                models=models, artifacts=st.artifacts, separate=body.separate,
            ),
            timeout=EXPORT_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        _log_export_failure(st, session_id, "reports_pdf_export_timeout", EXPORT_DEADLINE_SECONDS)
        raise HTTPException(
            status_code=503,
            detail=(
                f"PDF export took longer than {EXPORT_DEADLINE_SECONDS}s and was "
                "reported as failed so it could not hang the app. Retry when "
                "nothing else is running on the server."
            ),
        ) from exc
    except PdfExportError as exc:
        _log_export_failure(st, session_id, "reports_pdf_export_failed", str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    st.events.append(
        session_id, level="info", actor="system", action="reports_pdf_exported",
        detail={"reports": len(chosen), "separate": body.separate,
                "bytes": result.get("bytes")},
    )
    result["reports"] = [{"id": r.id, "title": r.title} for r in chosen]
    return result


# ------------------------------------------------------- talking to the analyst
class MiniSessionRequest(BaseModel):
    goal: str
    title: str = ""


def _chat_dossier(st: Any, session_id: str, report: Any) -> dict:
    """The whole research record for one conversation (shared with minis)."""
    from app.report_chat import chat_dossier

    return chat_dossier(st, session_id, report)


def _report_or_404(request: Request, session_id: str, report_id: str) -> Any:
    _session_or_404(request, session_id)
    rep = _st(request).reports.get(session_id, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="report not found")
    return rep


@router.get("/sessions/{session_id}/reports/{report_id}/chat")
async def get_report_chat(session_id: str, report_id: str, request: Request) -> dict:
    """The owner↔analyst conversation attached to this report."""
    _auth(request)
    st = _st(request)
    _report_or_404(request, session_id, report_id)
    from app.report_chat import ChatStore

    messages = ChatStore(st.db).list(session_id, report_id)
    return {"report_id": report_id, "messages": [m.to_dict() for m in messages]}


@router.post("/sessions/{session_id}/reports/{report_id}/chat")
async def ask_report_chat(
    session_id: str, report_id: str, body: ChatRequest, request: Request
) -> dict:
    """Ask the analyst a question about this report, in the ongoing thread.

    The model receives the ENTIRE research record (every capture, candidate,
    verification, note, reasoning line, screenshot, earlier run and the report
    itself, nothing summarised) plus the whole conversation so far, so
    follow-ups keep their meaning. Answers are prose, grounded the same way the
    report is: citations resolve to real evidence or are dropped.
    """
    _auth(request)
    st = _st(request)
    rep = _report_or_404(request, session_id, report_id)
    question = (body.message or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="message is required")
    if len(question) > 8_000:
        raise HTTPException(status_code=400, detail="message is too long (8000 characters max)")

    from app.report_chat import ChatStore, answer, extract_mini_session, link_citations

    chats = ChatStore(st.db)
    # A mini session for this report is WORKING: a message typed into the same
    # conversation goes to it, and its answer arrives in the thread when it is
    # done. Nothing starts, stops or queues: the message is delivered as it is
    # typed, on the mini session's next step.
    minis = getattr(st, "mini_sessions", None)
    if minis is not None:
        delivered_to = minis.deliver_owner_message(report_id, question)
        if delivered_to:
            chats.add(session_id, report_id, role="owner", text=question)
            return {
                "report_id": report_id,
                "delivered_to_mini_session": delivered_to,
                "messages": [m.to_dict() for m in chats.list(session_id, report_id)],
            }
    history = chats.list(session_id, report_id)
    owner_msg = chats.add(session_id, report_id, role="owner", text=question)
    st.events.append(
        session_id, level="info", actor="owner", action="report_question_asked",
        detail={"report_id": report_id, "chars": len(question)},
    )

    dossier = await run_in_threadpool(_chat_dossier, st, session_id, rep)
    model = getattr(st, "chat_model", None)
    owned_client = None
    if model is None:
        from app.model_client import ModelClient

        owned_client = ModelClient(st.config, st.events, thoughts=getattr(st, "thoughts", None))
        model = owned_client
    timeout = float(getattr(st.config, "chat_timeout_seconds", 240) or 240)
    try:
        text, thought_id = await asyncio.wait_for(
            answer(
                model=model,
                session_id=session_id,
                dossier=dossier,
                history=history,
                question=question,
                max_tokens=int(getattr(st.config, "chat_max_tokens", 4_000) or 4_000),
                reasoning_effort=getattr(st.config, "chat_reasoning_effort", "low"),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        st.events.append(
            session_id, level="warn", actor="model", action="report_question_timeout",
            detail={"report_id": report_id, "timeout_seconds": timeout},
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"the analyst took longer than {int(timeout)}s and was stopped. "
                "The question is saved — ask again to retry."
            ),
        ) from exc
    except Exception as exc:
        st.events.append(
            session_id, level="warn", actor="model", action="report_question_failed",
            detail={"report_id": report_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
        )
        raise HTTPException(
            status_code=502,
            detail=f"the analyst could not answer: {type(exc).__name__}: {exc}",
        ) from exc
    finally:
        if owned_client is not None:
            try:
                await owned_client.aclose()
            except Exception:
                pass
    if not text:
        raise HTTPException(
            status_code=502,
            detail="the analyst returned an empty answer; the question is saved — ask again",
        )

    # The analyst can decide to go and look: a fenced `mini-session` block at
    # the end of its answer. It is split out of the reply (never shown as text)
    # and started immediately — the owner's message in the conversation was the
    # instruction, so there is no card to approve. The block's prose is what
    # the owner reads now; the session posts its own findings back into the
    # thread when it is done.
    prose, mini_request = extract_mini_session(text)
    known = {row.get("id") for row in (dossier.get("evidence") or []) if isinstance(row, dict)}
    linked, cited, dropped = link_citations(prose, session_id, known)
    meta: dict[str, Any] = {"citations": cited, "dropped_citations": dropped}
    if thought_id:
        meta["thought_id"] = int(thought_id)
    started_mini = None
    if mini_request and minis is not None:
        try:
            started_mini = minis.start(
                session_id=session_id,
                report_id=report_id,
                goal=mini_request["goal"],
                title=mini_request.get("title") or "",
            )
            meta["mini_session"] = {
                "id": started_mini.id,
                "goal": started_mini.goal,
                "title": started_mini.title,
            }
        except Exception as exc:
            st.events.append(
                session_id, level="warn", actor="agent", action="mini_session_start_failed",
                detail={"report_id": report_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
    analyst_msg = chats.add(
        session_id, report_id, role="analyst", text=linked, meta=meta,
    )
    st.events.append(
        session_id, level="info", actor="agent", action="report_question_answered",
        detail={"report_id": report_id, "chars": len(linked), "citations": len(cited)},
    )
    body_out: dict[str, Any] = {
        "report_id": report_id,
        "question": owner_msg.to_dict(),
        "answer": analyst_msg.to_dict(),
        "messages": [m.to_dict() for m in chats.list(session_id, report_id)],
    }
    if started_mini is not None:
        body_out["mini_session"] = started_mini.to_dict()
    return body_out


# ------------------------------------------------------------- mini sessions
@router.post("/sessions/{session_id}/reports/{report_id}/mini-sessions")
async def start_mini_session(
    session_id: str, report_id: str, body: MiniSessionRequest, request: Request
) -> dict:
    """Start a mini session on this report's conversation.

    The goal comes from the analyst's proposal (or straight from the owner).
    A mini session is focused work with the live browser, free of the research
    methodology's phases: it chooses what to do and answers back into the chat.
    There is no limit on how many may run in one conversation.
    """
    _auth(request)
    st = _st(request)
    _report_or_404(request, session_id, report_id)
    minis = getattr(st, "mini_sessions", None)
    if minis is None:
        raise HTTPException(status_code=503, detail="mini sessions are not available")
    try:
        mini = minis.start(
            session_id=session_id,
            report_id=report_id,
            goal=body.goal,
            title=body.title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"mini_session": mini.to_dict()}


@router.get("/sessions/{session_id}/reports/{report_id}/mini-sessions")
async def list_mini_sessions(session_id: str, report_id: str, request: Request) -> dict:
    """Every mini session this conversation has run, newest first."""
    _auth(request)
    st = _st(request)
    _report_or_404(request, session_id, report_id)
    from app.mini_session import MiniSessionStore

    rows = MiniSessionStore(st.db).list(session_id, report_id)
    return {"mini_sessions": [m.to_dict() for m in rows]}


@router.get("/sessions/{session_id}/mini-sessions/{mini_id}")
async def get_mini_session(session_id: str, mini_id: str, request: Request) -> dict:
    """One mini session with its complete step log (the live view of it)."""
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    from app.mini_session import MiniSessionStore

    store = MiniSessionStore(st.db)
    mini = store.get(mini_id)
    if mini is None or mini.session_id != session_id:
        raise HTTPException(status_code=404, detail="mini session not found")
    return {
        "mini_session": mini.to_dict(),
        "steps": [s.to_dict() for s in store.steps(mini_id)],
    }


@router.post("/sessions/{session_id}/mini-sessions/{mini_id}/cancel")
async def cancel_mini_session(session_id: str, mini_id: str, request: Request) -> dict:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    from app.mini_session import MiniSessionStore

    store = MiniSessionStore(st.db)
    mini = store.get(mini_id)
    if mini is None or mini.session_id != session_id:
        raise HTTPException(status_code=404, detail="mini session not found")
    minis = getattr(st, "mini_sessions", None)
    if minis is None or not minis.cancel(mini_id):
        raise HTTPException(status_code=400, detail="this mini session is not running")
    return {"mini_session": store.get(mini_id).to_dict()}  # type: ignore[union-attr]


@router.get("/sessions/{session_id}/recordings/{artifact_id}/file")
async def download_recording(session_id: str, artifact_id: str, request: Request) -> FileResponse:
    """Download a session recording artifact (webm or html bundle)."""
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    art = st.artifacts.get(session_id, artifact_id)
    if art is None or art.kind != "recording":
        raise HTTPException(status_code=404, detail="recording not found")
    from pathlib import Path

    path = Path(art.path)

    if not path.is_file():
        raise HTTPException(status_code=404, detail="recording file missing")
    media = "video/webm" if path.suffix == ".webm" else "text/html"
    return FileResponse(path, media_type=media, filename=path.name)


# ------------------------------------------------------------------ exports
@router.post("/sessions/{session_id}/exports")
async def create_export(session_id: str, body: ExportRequest, request: Request) -> dict:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    try:
        job = st.exports.create(session_id, format=body.format)
        job = st.exports.run(session_id, job.id)
    except ExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"export": job.to_dict()}


@router.get("/sessions/{session_id}/exports")
async def list_exports(session_id: str, request: Request) -> dict:
    _auth(request)
    _session_or_404(request, session_id)
    jobs = _st(request).exports.list(session_id)
    return {"exports": [j.to_dict() for j in jobs]}


@router.get("/sessions/{session_id}/exports/{export_id}/file")
async def download_export(session_id: str, export_id: str, request: Request) -> FileResponse:
    _auth(request)
    st = _st(request)
    _session_or_404(request, session_id)
    job = st.exports.get(session_id, export_id)
    if job is None or not job.result_path:
        raise HTTPException(status_code=404, detail="export not found")
    from pathlib import Path

    path = Path(job.result_path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="export file missing")
    media = "application/gzip" if job.format == "tar_gz" else "application/json"
    return FileResponse(path, media_type=media, filename=path.name)
