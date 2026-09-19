"""Routes for events, uploads, artifacts, reports, exports."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

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


class ReportRequest(BaseModel):
    kind: str = "intermediate"
    title: str
    body_markdown: str


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


# ------------------------------------------------------------- methodology
@router.get("/methodology")
async def get_methodology(request: Request) -> dict:
    """The deterministic 9-phase research methodology (owner-facing spec)."""
    _auth(request)
    return {"methodology": _methodology(request).describe()}


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

    The model is the source of truth; edits to the model (e.g. owner polish)
    become new renderings without touching durable research state.
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
    from app.report_model import ReportModel, render_markdown

    body = render_markdown(ReportModel.from_dict(data))
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
    model = st.reports.read_model_json(rep)
    if model is None:
        raise HTTPException(status_code=404, detail="no structured model stored for this report")
    from app.pdf_export import PdfExportError, export_pdf

    try:
        result = export_pdf(
            st.db, st.config, session_id, model=model, artifacts=st.artifacts,
        )
    except PdfExportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    st.events.append(
        session_id, level="info", actor="system", action="report_pdf_exported",
        detail={"report_id": report_id, "bytes": result["bytes"]},
    )
    return result


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
