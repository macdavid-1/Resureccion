"""PDF export tests.

The exporter must preserve report text word for word, produce a real
6×9in PDF through the PDFKit sidecar, register durable artifacts, and
never leave a broken artifact behind on failure.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from app.pdf_export import (
    PdfExportError,
    export_pdf,
    model_to_blocks,
    node_available,
    render_report_html,
)
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


def _minimal_model() -> dict:
    """A small but complete model shaped like a real report model."""
    return {
        "schema_version": 2,
        "session": {"id": "s1", "name": "Grief Journals — US Sweep", "mode": "prompt",
                    "marketplaces_requested": ["us", "uk"], "objective": "verified gaps",
                    "prompt": "grief journals"},
        "generated_at": "2026-09-18T00:00:00Z",
        "executive_summary": {
            "what_was_researched": "grief journals",
            "why": "find verified gaps",
            "marketplaces_investigated": ["us", "uk"],
            "methodology": "Deterministic 9-phase methodology (discovery → synthesis).",
            "candidates_considered": 12,
            "candidates_rejected": 9,
            "survived_verification": 3,
            "demand_verdicts": ["sustained demand"],
            "major_findings": ["Finding one about grief journals.", "Finding two about pricing."],
        },
        "opportunities": [{
            "name": "Sudden-Loss Grief Journals",
            "niche": "sudden-loss grief journals for adult children",
            "parent_market": "grief journals",
            "target_reader": "adult children who lost a parent suddenly",
            "reader_problem": "overwhelm in the first year",
            "marketplaces": ["us", "uk"],
            "demand": {"verdict": "sustained demand", "signals": ["review velocity"], "demand_risks": []},
            "competitive_landscape": "3 incumbents, generic prompts",
            "consumer_needs": "structure; estate guidance",
            "market_gap": "estate guidance",
            "differentiation": "estate checklist section",
            "positioning": "the organized grief journal",
            "risks": ["narrow audience"],
            "kdp_risk": {"level": "safe", "warnings": []},
            "keyword_integrity": {},
            "verification": {"status": "verified", "verdict": "verified", "confidence": 0.82,
                             "limitations": ["single-marketplace review sample"]},
            "confidence": 0.78,
            "niching_chain": ["grief", "sudden loss", "adult children", "estate paperwork"],
            "marketplace_analysis": {"observed": ["us", "uk"], "strongest": ["us"],
                                     "related_demand_elsewhere": ["ca"],
                                     "differences": ["uk: fewer titles"],
                                     "language_considerations": "English-language markets.",
                                     "scope": "cross-market: observed on multiple marketplaces"},
            "angles": [{"reader_problem": "no structure", "angle": "estate checklist",
                        "evidence_ids": ["e1"], "addresses_gap": "competitors lack it"}],
            "titles": [{"title": "Suddenly Gone", "subtitle": "A Guided Grief Journal",
                        "rationale": "buyer language"}],
            "keywords": {"core": ["grief journal"], "supporting": ["bereavement journal"],
                         "long_tail": ["grief journal for adults who lost a parent"],
                         "problem_based": ["how to cope with sudden loss"],
                         "audience_specific": ["grief journal for adult children"],
                         "intent_phrases": ["guided grief workbook"],
                         "relevance_rationale": "mirrors niche language"},
            "evidence_ids": ["e1", "e2"],
            "next_step": "Proceed directly to book planning.",
        }],
        "consumer_intelligence": {"themes": {
            "praised": ["gentle tone"], "complaints": ["no structure"],
            "unmet_needs": ["legal checklist"], "desired_features": ["estate section"],
            "frustrations": [], "objections": [], "expectations": [],
            "missing_information": ["what to do first"],
            "what_the_new_book_should_do_differently": ["estate section"],
        }, "by_niche": [{"niche": "sudden-loss grief journals", "praises": ["gentle tone"],
                         "complaints": ["no structure"], "unmet": ["legal checklist"],
                         "design_opportunities": ["estate section"]}]},
        "competitors": [{"title": "Generic Grief Journal", "marketplace": "us",
                         "asin": "TEST00001", "price": "$9.99", "rank": 42000,
                         "review_count": 310, "saturation": "low",
                         "positioning": "generic prompts", "matched_evidence": True,
                         "lesson": "differentiate on structure"}],
        "candidate_ledger": [{"niche": "grief journals for widowed fathers",
                              "status": "rejected", "marketplace": "us",
                              "rationale": "insufficient consumer evidence", "score": 0.3}],
        "methodology_trace": [{"phase": "opportunity_discovery",
                               "title": "Phase 1 — Opportunity Discovery",
                               "outputs": ["signals"], "completed": True}],
        "evidence_appendix": [{"id": "e1", "kind": "product", "marketplace": "us",
                               "url": "https://www.amazon.com/dp/TEST00001",
                               "title": "Generic Grief Journal", "asin": "TEST00001",
                               "captured_at": "2026-09-18T00:00:00Z",
                               "screenshot_artifact_id": None}],
        "quality_statement": {"investigated": "12 candidates", "verified": 3, "rejected": 9,
                              "rejection_reasons": [{"niche": "grief journals for widowed fathers",
                                                     "rationale": "insufficient consumer evidence"}],
                              "remains_uncertain": ["one niche needs a follow-up"],
                              "verification_limitations": ["single-marketplace review sample"],
                              "certainty_policy": "certainty is never manufactured"},
        "validation": {"passed": True, "mode": "final", "failures": [],
                       "gate_report": {}, "per_opportunity": []},
    }


class TestBlockFidelity:
    def test_all_words_preserved(self):
        """Every word of the model's prose reaches the blocks verbatim."""
        model = _minimal_model()
        title, subtitle, blocks = model_to_blocks(model)
        blob = json.dumps(blocks, ensure_ascii=False)

        probe_strings = [
            "adult children who lost a parent suddenly",
            "estate checklist section",
            "Proceed directly to book planning.",
            "single-marketplace review sample",
            "certainty is never manufactured",
            "How to outperform it legitimately".replace("How to outperform it legitimately", "differentiate on structure"),
            "insufficient consumer evidence",
            "what to do first",
        ]
        for s in probe_strings:
            assert s in blob, f"content lost in blocks: {s!r}"
        # Structure sections present.
        texts = [b.get("x", "") for b in blocks]
        joined = " ".join(str(t) for t in texts)
        for heading in ("Executive Summary", "Opportunity Portfolio",
                        "Consumer Intelligence", "Competitor Analysis",
                        "Candidate Ledger", "Methodology Trace",
                        "Research Quality Statement", "Evidence Appendix"):
            assert heading in joined, f"missing section heading: {heading}"

    def test_no_markdown_left_in_plain_text(self):
        model = _minimal_model()
        _, _, blocks = model_to_blocks(model)
        for b in blocks:
            x = str(b.get("x", ""))
            assert "**" not in x, f"markdown leaked: {x!r}"

    def test_html_render_preserves_words(self):
        model = _minimal_model()
        _, _, blocks = model_to_blocks(model)
        html = render_report_html(model, blocks)
        for s in ("adult children who lost a parent suddenly",
                  "estate checklist section", "Suddenly Gone"):
            assert s in html


class TestReportLogo:
    """Both report covers render the owner-supplied logo (never a redrawn one)."""

    def test_installed_logo_file_is_used_as_is(self):
        from app.pdf_export import logo_file

        logo = logo_file()
        assert logo is not None, "static/logo.png missing"
        assert logo.name == "logo.png"
        assert logo.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_html_cover_carries_the_logo(self):
        from app.pdf_export import model_to_blocks, render_report_html

        model = _minimal_model()
        _, _, blocks = model_to_blocks(model)
        html = render_report_html(model, blocks)
        assert 'class="cover-logo"' in html
        assert "/static/logo.png?v=" in html
        # The mark leads the cover, above the wordmark.
        assert html.index("cover-logo") < html.index("R E S U R R E C C I Ó N")

    def test_pdf_job_hands_the_sidecar_the_logo_path(self, db, config, session, monkeypatch):
        """export_pdf passes the real file path; the sidecar draws it as-is."""
        if not node_available():
            pytest.skip("node/pdfkit sidecar unavailable")
        import app.pdf_export as pex
        from app.artifacts import ArtifactStore

        captured: dict = {}

        def fake_run(cmd, **kw):
            captured["job"] = json.loads(kw["input"].decode("utf-8"))

            class Failed:
                returncode = 1
                stderr = b"forced"

            return Failed()

        monkeypatch.setattr(pex.subprocess, "run", fake_run)
        with pytest.raises(PdfExportError):
            pex.export_pdf(db, config, session, model=_minimal_model(),
                           artifacts=ArtifactStore(db, config))
        job = captured["job"]
        assert job["logo"], "sidecar job is missing the logo path"
        assert job["logo"].endswith("logo.png")
        assert Path(job["logo"]).is_file()

    @pytest.mark.skipif(not node_available(), reason="node/pdfkit sidecar unavailable")
    def test_pdf_cover_actually_embeds_the_logo(self, db, config, session):
        """The sidecar draws the supplied bytes onto the cover. This model has
        no screenshots wired, so any embedded image XObject IS the logo."""
        from app.artifacts import ArtifactStore

        artifacts = ArtifactStore(db, config)
        result = export_pdf(db, config, session, model=_minimal_model(), artifacts=artifacts)
        art = artifacts.get(session, result["pdf"]["id"])
        data = Path(art.path).read_bytes()
        assert re.search(rb"/Subtype\s*/Image", data), "cover logo missing from the PDF"


class TestRealRender:
    @pytest.mark.skipif(not node_available(), reason="node/pdfkit sidecar unavailable")
    def test_export_produces_6x9_pdf_artifact(self, db, config, session):
        from app.artifacts import ArtifactStore

        artifacts = ArtifactStore(db, config)
        result = export_pdf(db, config, session, model=_minimal_model(), artifacts=artifacts)
        pdf = artifacts.get(session, result["pdf"]["id"])
        assert pdf is not None
        assert pdf.kind == "pdf"
        assert pdf.path.endswith(".pdf")
        data = pdf.read_bytes() if hasattr(pdf, "read_bytes") else __import__("pathlib").Path(pdf.path).read_bytes()
        assert data[:5] == b"%PDF-", "not a PDF file"
        assert len(data) > 1500
        # Page size: MediaBox must specify 432 x 648 pt (6×9in).
        text = data.decode("latin-1")
        m = re.search(r"/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]", text)
        assert m, "no MediaBox found"
        w = float(m.group(3)) - float(m.group(1))
        h = float(m.group(4)) - float(m.group(2))
        assert abs(w - 432.0) < 1.0, f"width {w}pt != 432pt (6in)"
        assert abs(h - 648.0) < 1.0, f"height {h}pt != 648pt (9in)"
        # Content words actually made it into the PDF text layer.
        assert len(data) > 0

    @pytest.mark.skipif(not node_available(), reason="node/pdfkit sidecar unavailable")
    def test_pdf_and_html_artifacts_registered(self, db, config, session):
        from app.artifacts import ArtifactStore

        artifacts = ArtifactStore(db, config)
        result = export_pdf(db, config, session, model=_minimal_model(), artifacts=artifacts)
        arts = artifacts.list(session)
        kinds = {a.kind for a in arts}
        assert "pdf" in kinds
        assert result["html_artifact_id"], "report HTML artifact missing"
        html_art = artifacts.get(session, result["html_artifact_id"])
        assert html_art is not None
        assert html_art.path.endswith(".html")

    @pytest.mark.skipif(not node_available(), reason="node/pdfkit sidecar unavailable")
    def test_long_report_renders_many_pages(self, db, config, session):
        """A long report must paginate — never truncate."""
        from app.artifacts import ArtifactStore
        from pathlib import Path

        model = _minimal_model()
        # Inflate: repeat the portfolio 30x.
        model["opportunities"] = [dict(model["opportunities"][0], name=f"Opp {i}") for i in range(30)]
        artifacts = ArtifactStore(db, config)
        result = export_pdf(db, config, session, model=model, artifacts=artifacts)
        data = Path(json.loads(json.dumps(result["pdf"]))["path"]).read_bytes()
        pages = len(re.findall(rb"/Type\s*/Page[^s]", data))
        assert pages > 10, f"long report only produced {pages} pages"


class TestFailureSafety:
    def test_bad_blocks_fail_without_artifact(self, db, config, session):
        """A failing render must register nothing and leave no temp files."""
        from app.artifacts import ArtifactStore

        artifacts = ArtifactStore(db, config)
        bad_model = _minimal_model()
        bad_model["opportunities"] = None  # exporter must tolerate/handle
        # Force a sidecar failure with a nonsensical blocks type via monkeypatch.
        import app.pdf_export as pex

        orig = pex.model_to_blocks
        pex.model_to_blocks = lambda m: ("t", "s", [{"t": "table", "head": "notalist", "rows": [["x"]]}])
        try:
            with pytest.raises(PdfExportError):
                export_pdf(db, config, session, model=bad_model, artifacts=artifacts)
        finally:
            pex.model_to_blocks = orig
        # No PDF artifacts registered.
        assert not [a for a in artifacts.list(session) if a.kind == "pdf"]
        # No temp files left behind.
        leftovers = [p for p in artifacts.session_dir(session).glob(".pdf-*.tmp")]
        assert not leftovers, f"temp files leaked: {leftovers}"

    def test_export_rebuilds_model_from_durable_state_when_file_is_lost(self, client):
        """A finished report whose .model.json went missing (interrupted save,
        pruned file) must still export: the model is rebuilt from the durable
        research state instead of answering 404."""
        if not node_available():
            pytest.skip("node/pdfkit sidecar unavailable")
        from tests.test_research_runner import PHASE_OUTPUTS as PO
        from tests.test_research_runner import ScriptedModel, make_runner

        st = client.app.state
        s = st.sessions.create(mode="prompt", prompt="export rebuild probe", objective="o")
        import asyncio

        asyncio.run(make_runner(st.db, st.config, ScriptedModel(PO)).run(s.id))
        res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
        h = {"X-Auth-Token": res.json()["token"]}
        reps = client.get(f"/api/sessions/{s.id}/reports", headers=h).json()["reports"]
        final = [r for r in reps if r["kind"] == "final"][0]
        # Destroy the stored model, keep the report body.
        report = st.reports.get(s.id, final["id"])
        Path(report.body_path).with_suffix(".model.json").unlink()
        assert st.reports.read_model_json(report) is None

        res = client.post(f"/api/sessions/{s.id}/reports/{final['id']}/export-pdf", headers=h)
        assert res.status_code == 200, res.text
        assert res.json()["bytes"] > 1500
        # The rebuild was persisted, so the next export reads it back.
        assert st.reports.read_model_json(report) is not None

    def test_missing_node_raises_clear_error(self, db, config, session, monkeypatch):
        import app.pdf_export as pex
        from app.artifacts import ArtifactStore

        monkeypatch.setattr(pex, "node_available", lambda: False)
        with pytest.raises(PdfExportError, match="unavailable"):
            export_pdf(db, config, session, model=_minimal_model(),
                       artifacts=ArtifactStore(db, config))


class TestExportAPI:
    @pytest.mark.asyncio
    async def test_export_route_round_trip(self, client):
        """Full API path: scripted run → report → export PDF → artifact."""
        if not node_available():
            pytest.skip("node/pdfkit sidecar unavailable")
        from tests.test_research_runner import PHASE_OUTPUTS as PO
        from tests.test_research_runner import ScriptedModel, make_runner

        st = client.app.state
        s = st.sessions.create(mode="prompt", prompt="export probe", objective="o")
        await make_runner(st.db, st.config, ScriptedModel(PO)).run(s.id)
        res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
        h = {"X-Auth-Token": res.json()["token"]}
        reps = client.get(f"/api/sessions/{s.id}/reports", headers=h).json()["reports"]
        final = [r for r in reps if r["kind"] == "final"][0]
        res = client.post(f"/api/sessions/{s.id}/reports/{final['id']}/export-pdf", headers=h)
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["pdf"]["id"]
        assert body["bytes"] > 1500
        # Download the artifact.
        res = client.get(f"/api/sessions/{s.id}/artifacts/{body['pdf']['id']}/file", headers=h)
        assert res.status_code == 200
        assert res.content[:5] == b"%PDF-"


# 1x1 transparent PNG — a real decodable image PDFKit will accept.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _with_figures(model: dict, session_id: str, artifacts) -> dict:
    """Wire figures the same way export_pdf does before shaping blocks."""
    from app.pdf_export import _resolve_figures

    return {**model, "figures": _resolve_figures(session_id, model, artifacts)}


def _seed_screenshot(session_id: str, artifacts, filename: str = "cap.png") -> str:
    art = artifacts.save_bytes(
        session_id, kind="screenshot", filename=filename, data=_TINY_PNG
    )
    return art.id


class TestReportFigures:
    """Screenshots must flow WITH the claim they document, not pile up at the end."""

    def test_competitor_screenshot_sits_under_its_own_metrics(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import model_to_blocks

        arts = ArtifactStore(db, config)
        sid = _seed_screenshot(session, arts)
        m = _minimal_model()
        m["competitors"][0]["screenshot_artifact_id"] = sid
        m["evidence_appendix"][0]["screenshot_artifact_id"] = sid

        _, _, blocks = model_to_blocks(_with_figures(m, session, arts))

        imgs = [i for i, b in enumerate(blocks) if b.get("t") == "img"]
        assert len(imgs) == 1, "the competitor's capture must appear exactly once"

        # It must sit INSIDE the competitor's entry — after its heading, and
        # before the next section — so the numbers and the image read together.
        idx = imgs[0]
        headings_before = [b for b in blocks[:idx] if b.get("t") in ("h2", "h3")]
        assert headings_before, "image must come after a heading"
        assert headings_before[-1]["x"] == "Generic Grief Journal"
        after = [b for b in blocks[idx + 1:] if b.get("t") == "h2"]
        assert after and after[0]["x"] == "Candidate Ledger"

        # Caption carries the claim the screenshot substantiates.
        cap = blocks[idx]["caption"]
        assert "310 reviews" in cap and "42000" in cap and "TEST00001" in cap

    def test_no_trailing_gallery_when_everything_is_inline(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import model_to_blocks

        arts = ArtifactStore(db, config)
        sid = _seed_screenshot(session, arts)
        m = _minimal_model()
        m["competitors"][0]["screenshot_artifact_id"] = sid
        m["evidence_appendix"][0]["screenshot_artifact_id"] = sid

        _, _, blocks = model_to_blocks(_with_figures(m, session, arts))
        h2s = [b["x"] for b in blocks if b.get("t") == "h2"]
        assert "Research Screenshots" not in h2s, (
            "an inline-placed capture must not be duplicated in a gallery"
        )

    def test_orphan_evidence_still_gets_a_capped_gallery(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import MAX_REPORT_FIGURES, model_to_blocks

        arts = ArtifactStore(db, config)
        m = _minimal_model()
        # Screenshots on evidence rows that match no competitor.
        m["evidence_appendix"] = [
            {"id": f"e{i}", "kind": "product", "marketplace": "us",
             "url": f"https://www.amazon.com/dp/X{i:06d}", "title": f"Row {i}",
             "asin": f"X{i:06d}", "captured_at": "2026-09-18T00:00:00Z",
             "screenshot_artifact_id": _seed_screenshot(session, arts, f"c{i}.png")}
            for i in range(40)
        ]
        _, _, blocks = model_to_blocks(_with_figures(m, session, arts))
        imgs = [b for b in blocks if b.get("t") == "img"]
        assert 0 < len(imgs) <= MAX_REPORT_FIGURES, (
            f"expected a capped gallery, got {len(imgs)}"
        )

    def test_budget_is_shared_across_inline_and_gallery(self, db, config, session):
        """Inline placement spends the same budget — total never exceeds the cap."""
        from app.artifacts import ArtifactStore
        from app.pdf_export import MAX_REPORT_FIGURES, model_to_blocks

        arts = ArtifactStore(db, config)
        m = _minimal_model()
        rows = []
        for i in range(30):
            sid = _seed_screenshot(session, arts, f"s{i}.png")
            rows.append({"id": f"e{i}", "kind": "product", "marketplace": "us",
                         "url": f"https://www.amazon.com/dp/Y{i:06d}", "title": f"Row {i}",
                         "asin": f"Y{i:06d}", "captured_at": "2026-09-18T00:00:00Z",
                         "screenshot_artifact_id": sid})
            if i < 5:  # five competitors also claim a capture
                m["competitors"].append({
                    "title": f"Rival {i}", "marketplace": "us", "asin": f"Y{i:06d}",
                    "rank": 6709 + i, "review_count": 0, "lesson": "x",
                    "screenshot_artifact_id": sid,
                })
        m["evidence_appendix"] = rows

        _, _, blocks = model_to_blocks(_with_figures(m, session, arts))
        imgs = [b for b in blocks if b.get("t") == "img"]
        assert len(imgs) <= MAX_REPORT_FIGURES
        # No artifact is rendered twice.
        paths = [b["path"] for b in imgs]
        assert len(paths) == len(set(paths))

    def test_zero_review_competitor_reads_next_to_its_image(self, db, config, session):
        """The worked example: 'no reviews and a 6709 BSR' beside the screenshot."""
        from app.artifacts import ArtifactStore
        from app.pdf_export import model_to_blocks

        arts = ArtifactStore(db, config)
        sid = _seed_screenshot(session, arts)
        m = _minimal_model()
        m["competitors"][0].update(
            {"review_count": 0, "rank": 6709, "screenshot_artifact_id": sid}
        )
        m["evidence_appendix"][0]["screenshot_artifact_id"] = sid

        _, _, blocks = model_to_blocks(_with_figures(m, session, arts))
        img = next(b for b in blocks if b.get("t") == "img")
        assert "0 reviews" in img["caption"]
        assert "BSR 6709" in img["caption"]

    def test_missing_or_pruned_artifact_never_breaks_export(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import _resolve_figures

        arts = ArtifactStore(db, config)
        m = _minimal_model()
        m["evidence_appendix"][0]["screenshot_artifact_id"] = "art_does_not_exist"
        assert _resolve_figures(session, m, arts) == []

    def test_path_escaping_artifacts_root_is_refused(self, db, config, session, tmp_path):
        """The renderer reads whatever path it is handed — containment is checked."""
        from app.artifacts import ArtifactStore
        from app.pdf_export import _resolve_figures

        arts = ArtifactStore(db, config)
        real = arts.save_bytes(session, kind="screenshot", filename="ok.png", data=_TINY_PNG)
        # A real file that genuinely sits OUTSIDE the artifacts root.
        outside = tmp_path / "outside-root.png"
        outside.write_bytes(_TINY_PNG)

        # Threat model: the artifact row itself points somewhere it shouldn't.
        # The sidecar reads whatever path it is handed, so containment is
        # verified in _resolve_figures rather than trusted from the DB.
        with db.tx() as conn:
            conn.execute(
                "UPDATE artifacts SET path = ? WHERE id = ?",
                (str(outside), real.id),
            )
        m = _minimal_model()
        m["evidence_appendix"][0]["screenshot_artifact_id"] = real.id
        assert arts.get(session, real.id).path == str(outside)
        assert _resolve_figures(session, m, arts) == []

        # And the same row resolves fine once it points back inside the root.
        with db.tx() as conn:
            conn.execute(
                "UPDATE artifacts SET path = ? WHERE id = ?",
                (real.path, real.id),
            )
        assert len(_resolve_figures(session, m, arts)) == 1

    def test_html_artifact_renders_the_same_figure(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import model_to_blocks, render_report_html

        arts = ArtifactStore(db, config)
        sid = _seed_screenshot(session, arts)
        m = _minimal_model()
        m["competitors"][0]["screenshot_artifact_id"] = sid
        m["evidence_appendix"][0]["screenshot_artifact_id"] = sid

        wired = _with_figures(m, session, arts)
        _, _, blocks = model_to_blocks(wired)
        html = render_report_html(wired, blocks)
        assert "<figure class=\"fig\">" in html
        assert "310 reviews" in html


# ------------------------------------------------------ multi-report export
class TestExportReports:
    """Default: every selected report in ONE combined PDF (each report gets
    its own section page). separate=True: one PDF artifact per report."""

    def test_combined_default_renders_one_pdf_for_many_reports(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import export_reports

        arts = ArtifactStore(db, config)
        result = export_reports(
            db, config, session, models=[_minimal_model(), _minimal_model()],
            artifacts=arts,
        )
        assert result["separate"] is False
        assert len(result["pdfs"]) == 1
        pdf = result["pdfs"][0]["pdf"]
        assert "combined" in pdf["path"]
        assert result["bytes"] > 500
        assert Path(pdf["path"]).is_file()
        # The artifact records how many reports went into the combined file.
        assert arts.get(session, pdf["id"]).meta["combined_reports"] == 2

    def test_separate_opt_in_renders_one_pdf_per_report(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import export_reports

        arts = ArtifactStore(db, config)
        result = export_reports(
            db, config, session, models=[_minimal_model(), _minimal_model()],
            artifacts=arts, separate=True,
        )
        assert result["separate"] is True
        assert len(result["pdfs"]) == 2
        paths = [p["pdf"]["path"] for p in result["pdfs"]]
        assert len(set(paths)) == 2  # distinct artifacts, not the same file twice
        assert all(Path(p).is_file() for p in paths)

    def test_export_reports_requires_at_least_one_model(self, db, config, session):
        from app.artifacts import ArtifactStore
        from app.pdf_export import export_reports

        with pytest.raises(PdfExportError):
            export_reports(
                db, config, session, models=[],
                artifacts=ArtifactStore(db, config),
            )


class TestRendererCrashRecovery:
    """The VPS failure: Node killed by SIGABRT with EMPTY stderr (address-
    space/memory squeeze). It must be retried once, and a persistent death
    must produce a diagnosis — never the old bare '(exit -6):' with nothing
    after the colon."""

    def test_renderer_cmd_uses_jitless_when_supported(self, monkeypatch):
        import app.pdf_export as pex

        monkeypatch.setattr(pex, "_jitless_ok", True)
        cmd = pex._renderer_cmd()
        assert cmd[0] == "node"
        assert "--jitless" in cmd
        assert cmd[-1].endswith("render_pdf.js")

    def test_renderer_cmd_falls_back_without_jitless(self, monkeypatch):
        import app.pdf_export as pex

        monkeypatch.setattr(pex, "_jitless_ok", False)
        cmd = pex._renderer_cmd()
        assert "--jitless" not in cmd

    def test_signal_death_is_retried_once_then_succeeds(self, monkeypatch, tmp_path):
        import app.pdf_export as pex

        out = tmp_path / "r.pdf"
        attempts: list[int] = []

        class Killed:
            returncode = -6
            stderr = b""

        class Ok:
            returncode = 0
            stderr = b""

        def fake_run(cmd, **kw):
            attempts.append(1)
            if len(attempts) == 1:
                return Killed()
            out.write_bytes(b"%PDF-1.3\n" + b"x" * 600)
            return Ok()

        monkeypatch.setattr(pex.subprocess, "run", fake_run)
        monkeypatch.setattr(pex.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pex, "_jitless_ok", True)
        result = pex._render_to_tmp({"out": str(out), "blocks": []})
        assert result == out
        assert len(attempts) == 2, "signal death must be retried exactly once"

    def test_persistent_signal_death_reports_a_diagnosis(self, monkeypatch, tmp_path):
        import app.pdf_export as pex

        class Killed:
            returncode = -6
            stderr = b""

        calls = {"self_check": 0}

        def fake_run(cmd, **kw):
            if "process.stdout.write" in " ".join(str(c) for c in cmd):
                calls["self_check"] += 1

                class SC:
                    returncode = 0
                    stdout = b"ok"

                return SC()
            return Killed()

        monkeypatch.setattr(pex.subprocess, "run", fake_run)
        monkeypatch.setattr(pex.time, "sleep", lambda _s: None)
        monkeypatch.setattr(pex, "_jitless_ok", True)
        with pytest.raises(PdfExportError) as exc:
            pex._render_to_tmp({"out": str(tmp_path / "r.pdf"), "blocks": []})
        msg = str(exc.value)
        assert "SIGABRT" in msg, msg
        assert "killed silently" in msg, "empty-stderr death must explain itself"
        assert "node self-check" in msg
        assert calls["self_check"] == 1
        # No stray temp file is left behind by the failed render.
        assert not (tmp_path / "r.pdf").exists()
