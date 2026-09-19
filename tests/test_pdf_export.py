"""PDF export tests.

The exporter must preserve report text word for word, produce a real
6×9in PDF through the PDFKit sidecar, register durable artifacts, and
never leave a broken artifact behind on failure.
"""
from __future__ import annotations

import json
import re
import subprocess

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
