"""Tests for artifacts, uploads, reports, and exports."""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

from app.artifacts import ArtifactError, ArtifactStore
from app.exports import ExportManager
from app.reports import ReportError, ReportStore
from app.research_data import CandidateStore, OpportunityStore
from app.uploads import UploadError, UploadStore


@pytest.fixture()
def sid(db) -> str:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('sess', 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    return "sess"


def test_artifact_save_and_read(db, config, sid) -> None:
    store = ArtifactStore(db, config)
    art = store.save_bytes(sid, kind="screenshot", filename="shot.png", data=b"\x89PNG fake")
    assert Path(art.path).is_file()
    assert art.size_bytes == len(b"\x89PNG fake")
    assert art.content_type == "image/png"
    got = store.read_bytes(store.require(sid, art.id))
    assert got == b"\x89PNG fake"
    assert store.get("other", art.id) is None


def test_artifact_kind_validation(db, config, sid) -> None:
    store = ArtifactStore(db, config)
    with pytest.raises(ArtifactError):
        store.save_bytes(sid, kind="hologram", filename="x.bin", data=b"")


def test_artifact_external_path_guard(db, config, sid) -> None:
    store = ArtifactStore(db, config)
    outside = config.data_dir / "outside.png"
    outside.write_bytes(b"x")
    with pytest.raises(ArtifactError):
        store.register_external_file(sid, kind="screenshot", path=outside)


def test_upload_validation(db, config, sid) -> None:
    store = UploadStore(db, config)
    up = store.save(sid, filename="cover.png", content_type="image/png", data=b"\x89PNG")
    assert up.size_bytes == 4
    with pytest.raises(UploadError):
        store.save(sid, filename="x.exe", content_type="application/exe", data=b"MZ")
    with pytest.raises(UploadError):
        store.save(sid, filename="empty.png", content_type="image/png", data=b"")
    with pytest.raises(UploadError):
        store.save(sid, filename="big.png", content_type="image/png", data=b"x" * (21 * 1024 * 1024))


def test_report_save_and_read(db, config, sid) -> None:
    store = ReportStore(db, config)
    rep = store.save(sid, kind="final", title="Market Report: Pet Journals", body_markdown="# Findings\n...")
    body = store.read_markdown(store.require_latest(rep.id) if False else store.get(sid, rep.id))
    assert body.startswith("# Findings")
    latest = store.latest(sid, kind="final")
    assert latest.id == rep.id
    with pytest.raises(ReportError):
        store.save(sid, kind="bogus", title="t", body_markdown="b")


def test_export_bundle_json_and_tar(db, config, sid) -> None:
    reports = ReportStore(db, config)
    cand = CandidateStore(db)
    opps = OpportunityStore(db)
    c = cand.create(sid, niche="bouldering journals")
    opps.create(sid, candidate_id=c.id, title="Boulder Log", niche="bouldering journals")
    reports.save(sid, kind="intermediate", title="Interim", body_markdown="data")

    mgr = ExportManager(db, config, reports, cand, opps)
    job = mgr.create(sid, format="json")
    job = mgr.run(sid, job.id)
    assert job.status == "succeeded"
    import json

    payload = json.loads(Path(job.result_path).read_text())
    assert payload["session_id"] == sid
    assert len(payload["opportunities"]) == 1

    job2 = mgr.create(sid, format="tar_gz")
    job2 = mgr.run(sid, job2.id)
    assert job2.status == "succeeded"
    with tarfile.open(job2.result_path) as tar:
        assert "export.json" in tar.getnames()

    with pytest.raises(Exception):
        mgr.create(sid, format="zip")


def test_export_bundle_includes_full_research_context(db, config, sid) -> None:
    """Regression: exports must carry observations/evidence/events, redacted."""
    from app.events import EventLog
    from app.recovery import CheckpointStore
    from app.research_data import EvidenceStore, ObservationStore

    reports = ReportStore(db, config)
    cand = CandidateStore(db)
    opps = OpportunityStore(db)
    obs = ObservationStore(db)
    ev = EvidenceStore(db)
    events = EventLog(db)
    cps = CheckpointStore(db)

    obs.create(sid, source="amazon", kind="search_page", content="rows")
    ev.create(sid, kind="screenshot", uri="a.png", summary="shot")
    events.append(sid, action="job_created", detail={"job_id": "j1"})
    cps.save(sid, "phase", {"step": 1})
    c = cand.create(sid, niche="n")
    opps.create(sid, candidate_id=c.id, title="T", niche="n")
    reports.save(sid, kind="final", title="F", body_markdown="body")

    mgr = ExportManager(
        db, config, reports, cand, opps,
        observations=obs, evidence=ev, events=events, checkpoints=cps,
    )
    job = mgr.create(sid, format="json")
    job = mgr.run(sid, job.id)
    assert job.status == "succeeded"
    import json

    payload = json.loads(Path(job.result_path).read_text())
    assert len(payload["observations"]) == 1
    assert len(payload["evidence"]) == 1
    assert len(payload["events"]) >= 1
    assert len(payload["checkpoints"]) == 1
    assert payload["session_id"] == sid


def test_export_requeue_interrupted(db, config, sid) -> None:
    reports = ReportStore(db, config)
    mgr = ExportManager(db, config, reports, CandidateStore(db), OpportunityStore(db))
    job = mgr.create(sid, format="json")
    # Simulate a crash mid-run.
    with db.tx() as conn:
        conn.execute("UPDATE export_jobs SET status = 'running' WHERE id = ?", (job.id,))
    assert mgr.requeue_interrupted() == 1
    row = mgr.get(sid, job.id)
    assert row.status == "failed"
    assert "restart" in row.error
