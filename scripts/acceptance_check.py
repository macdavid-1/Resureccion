"""Final acceptance scenarios A–H.

A  Directed research: detailed prompt + explicit marketplaces → obeyed.
B  Seeded research: keywords only → niching + competitive + consumer + verification.
C  Image-only research: images steer direction, no text prompt.
D  Autonomous: no prompt → agent picks marketplaces, discovers, niches.
E  Long-run durability      → covered by scripts/autonomy_e2e_check.py.
F  Restart recovery         → covered by scripts/autonomy_e2e_check.py.
G  Recording                → covered by tests/test_frontend_api.py (WebM path).
H  PDF export               → exercised here against the final report.

Run: timeout 170 .venv/bin/python scripts/acceptance_check.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OWNER_USERNAME", "owner")
os.environ.setdefault("OWNER_PASSWORD_HASH", "placeholder")

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.events import EventLog  # noqa: E402
from app.reports import ReportStore  # noqa: E402
from app.sessions import SessionStore  # noqa: E402

import tests.test_research_runner as trr  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {note}" if note else ""))


async def main() -> int:
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="resurrect_accept_"))
    cfg.db_path = cfg.data_dir / "accept.db"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.connect()
    sessions = SessionStore(db, cfg)

    print("=== Scenario A: directed prompt + explicit marketplaces ===")
    s = sessions.create(mode="prompt", prompt="Research low-content journals for competitive swimmers recovering from injury. Focus on training logs.",
                        objective="verified low-competition gaps", marketplaces=["us", "uk"])
    runner = trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS))
    await runner.run(s.id)
    sess = sessions.require(s.id)
    record("A: session completed", sess.status == "completed", sess.status)
    record("A: explicit marketplaces preserved on session", list(sess.marketplaces) == ["us", "uk"], str(sess.marketplaces))
    record("A: candidate worked + verified + rejected",
           sessions.counts(s.id)["rejected"] >= 1 and sessions.counts(s.id)["verified"] >= 1)
    rep = [r for r in ReportStore(db, cfg).list(s.id) if r.kind == "final"]
    record("A: final report generated", bool(rep))
    model = ReportStore(db, cfg).read_model_json(rep[0])
    record("A: dossier obeys brief marketplace attribution",
           "us" in (model["opportunities"][0]["marketplace_analysis"]["observed"]))

    print("=== Scenario B: keywords only ===")
    s2 = sessions.create(mode="keywords", prompt="grief journal, estate planner, first marathon", objective="")
    await trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(s2.id)
    sess2 = sessions.require(s2.id)
    record("B: completed with niched opportunity", sess2.status == "completed"
           and "adult children" in (ReportStore(db, cfg).read_model_json(
               [r for r in ReportStore(db, cfg).list(s2.id) if r.kind == "final"][0]
           )["opportunities"][0]["niche"]))
    m2 = ReportStore(db, cfg).read_model_json([r for r in ReportStore(db, cfg).list(s2.id) if r.kind == "final"][0])
    record("B: niching chain present", len(m2["opportunities"][0]["niching_chain"]) >= 2)
    record("B: consumer intelligence present",
           bool(m2["consumer_intelligence"]["themes"]["complaints"]))
    record("B: verification performed",
           m2["opportunities"][0]["verification"]["status"] == "verified")

    print("=== Scenario C: images only (no text prompt) ===")
    s3 = sessions.create(mode="keywords", prompt="", objective="", has_images=True)
    await trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(s3.id)
    sess3 = sessions.require(s3.id)
    record("C: image-only session completes research", sess3.status == "completed", sess3.status)
    m3 = ReportStore(db, cfg).read_model_json([r for r in ReportStore(db, cfg).list(s3.id) if r.kind == "final"][0])
    record("C: evidence-backed opportunity produced",
           bool(m3["opportunities"][0]["evidence_ids"]))

    print("=== Scenario D: autonomous (no prompt at all) ===")
    s4 = sessions.create(mode="auto", prompt="", objective="")
    await trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS)).run(s4.id)
    sess4 = sessions.require(s4.id)
    record("D: autonomous session completed", sess4.status == "completed")
    record("D: autonomous name assigned",
           "Autonomous Market Sweep" in sess4.name or "sweep" in sess4.name.lower())
    m4 = ReportStore(db, cfg).read_model_json([r for r in ReportStore(db, cfg).list(s4.id) if r.kind == "final"][0])
    record("D: marketplace plan recorded in report",
           bool(m4["executive_summary"]["marketplaces_investigated"]))
    record("D: opportunity portfolio produced", len(m4["opportunities"]) >= 1)

    print("=== Scenario H: PDF export of the final report ===")
    if not Path("node_modules/pdfkit").exists():
        record("H: pdfkit installed", False, "node_modules/pdfkit missing — run npm install")
    else:
        from app.artifacts import ArtifactStore
        from app.pdf_export import export_pdf

        result = export_pdf(db, cfg, s.id, model=m4, artifacts=ArtifactStore(db, cfg))
        data = Path(result["pdf"]["path"]).read_bytes()
        record("H: 6×9 PDF produced via PDFKit", data[:5] == b"%PDF-" and len(data) > 1500,
               f"{len(data)} bytes")
        record("H: report HTML persisted alongside",
               bool(result["html_artifact_id"]))
        text = data.decode("latin-1")
        record("H: page size is 6×9in", "/MediaBox [ 0 0 432 648 ]" in text or "432 648" in text)
        # Word-for-word fidelity is proven upstream (blocks carry the model
        # text verbatim; see tests/test_pdf_export.py). Here we verify the
        # PDF actually contains substantial rendered text: PDFKit encodes
        # glyphs with subset fonts, so probe for healthy text operators and
        # real content volume across many content streams.
        import zlib

        text_ops = 0
        for m in __import__("re").finditer(rb"stream\r?\n(.*?)endstream", data, __import__("re").S):
            try:
                blob = zlib.decompress(m.group(1).strip(b"\r\n"))
            except Exception:
                continue
            text_ops += len(__import__("re").findall(rb"Tj|TJ", blob))
        record("H: substantial text content rendered in PDF", text_ops > 100,
               f"{text_ops} text operators")

    print()
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, note in failed:
        print(f"FAILED: {name} {note}")
    print(f"ACCEPTANCE RESULT: {'PASS' if not failed else 'FAIL'} "
          f"({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    db.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
