"""Routes for events, uploads, artifacts, reports, exports."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
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
    model = _report_model(request, session_id, rep)
    from app.pdf_export import PdfExportError, export_pdf

    try:
        # Rendered in a worker thread: PDFKit blocks for seconds on a large
        # report, and doing that on the event loop froze the whole app —
        # frames, SSE and every other request stalled for the duration.
        result = await run_in_threadpool(
            export_pdf,
            st.db, st.config, session_id,
            model=model, artifacts=st.artifacts,
        )
    except PdfExportError as exc:
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
            models.append(_report_model(request, session_id, r))
        except HTTPException as exc:
            if exc.status_code == 404:
                continue  # nothing to render from this report
            raise
        chosen.append(r)
    if not chosen:
        raise HTTPException(status_code=404, detail="no reports with a stored model")
    from app.pdf_export import PdfExportError, export_reports

    try:
        result = await run_in_threadpool(
            export_reports,
            st.db, st.config, session_id,
            models=models, artifacts=st.artifacts, separate=body.separate,
        )
    except PdfExportError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    st.events.append(
        session_id, level="info", actor="system", action="reports_pdf_exported",
        detail={"reports": len(chosen), "separate": body.separate,
                "bytes": result.get("bytes")},
    )
    result["reports"] = [{"id": r.id, "title": r.title} for r in chosen]
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
