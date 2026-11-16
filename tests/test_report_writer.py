"""The model writes the report from the whole research record.

Covers the architecture, not a prompt: the dossier carries the run's phases,
reasoning, captures, competitor metadata, rejections and screenshots; each mode
is framed differently; the model's markdown is normalised into a grounded,
auditable, renderable document with inline screenshot evidence; the runner
persists it and can re-render and PDF it without falling back to a template;
and a failed/gutted reply degrades to the deterministic report instead of
costing the owner the deliverable.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.report_narrative import allowed_numbers
from app.report_writer import (
    MODE_FRAMING,
    REPORT_SYSTEM,
    WritableReport,
    allowed_for,
    build_report_context,
    correction_prompt,
    normalize_report,
    prompt_context,
    screenshot_map,
    user_prompt,
)
from tests.test_research_runner import session  # noqa: F401 — pytest fixture reuse


# --------------------------------------------------------------------- stubs
class _ListStore:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list(self, session_id: str, limit: int = 500) -> list[Any]:
        return list(self.rows)[:limit]


class _Sessions:
    def __init__(self, mode: str = "prompt", brief: str = "write a grief journal") -> None:
        self.session = SimpleNamespace(
            id="sess-1",
            name="probe",
            mode=mode,
            objective="find a niche",
            prompt=brief,
            marketplaces=["us"],
            status="running",
            elapsed_seconds=1200,
        )

    def require(self, session_id: str) -> Any:
        return self.session

    def counts(self, session_id: str) -> dict[str, Any]:
        return {"total": 3, "rejected": 1, "verified": 1, "opportunities": 1}


class _Thoughts:
    def recent(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        return [{"id": 1, "label": "Phase 2 — Aggressive Niching"}]

    def get(self, thought_id: int) -> Any:
        return SimpleNamespace(detail="I checked the review depth on the top listings.")


class _Trace:
    def tail(self, session_id: str, limit: int = 40) -> list[Any]:
        return [SimpleNamespace(
            created_at="2026-11-16T10:00:00Z",
            kind="action",
            text="Opened the top listing and read twelve reviews",
        )]


class _Notes:
    def list(self, session_id: str, limit: int = 500) -> list[Any]:
        return [SimpleNamespace(
            phase="consumer_intelligence",
            niche="grief journals",
            text="Reviewers keep asking for a sudden-loss section",
        )]


class _Owner:
    def journal(self, session_id: str) -> list[Any]:
        return [SimpleNamespace(
            created_at="2026-11-16T10:05:00Z", text="focus on adult children",
        )]


class _Artifacts:
    def get(self, session_id: str, artifact_id: str) -> Any:
        return SimpleNamespace(id=artifact_id) if artifact_id == "art-1" else None


def _evidence() -> list[Any]:
    return [
        SimpleNamespace(
            id="ev-product",
            kind="product_page",
            marketplace="us",
            title="Grief Journal for Adult Children",
            url="https://www.amazon.com/dp/B0TEST",
            captured_at="2026-11-16T10:01:00Z",
            screenshot_artifact_id="art-1",
            data={
                "asin": "B0TEST",
                "price": "$9.99",
                "review_count": 812,
                "best_seller_rank": 6709,
                "best_seller_ranks": [
                    {"rank": 6709, "category": "Books"},
                    {"rank": 44, "category": "Grief & Loss"},
                ],
                "publication_date": "March 3, 2023",
                "publisher": "Independently published",
                "print_length": "210 pages",
                "formats": [{"format": "Kindle", "price": "$0.99"}],
                "reviews": [{"rating": 5, "body": "wanted a sudden-loss chapter"}],
            },
        ),
        SimpleNamespace(
            id="ev-search",
            kind="search_results",
            marketplace="us",
            title="Search: grief journals",
            url="https://www.amazon.com/s?k=grief+journals",
            captured_at="2026-11-16T10:00:00Z",
            screenshot_artifact_id=None,
            data={"query": "grief journals", "total_results": 2000,
                  "total_results_are_approximate": True},
        ),
    ]


def _model_dict() -> dict[str, Any]:
    return {
        "executive_summary": {"candidates_considered": 3, "candidates_rejected": 1},
        "quality_statement": {"rejected": 1, "remains_uncertain": "review velocity"},
        "opportunities": [{"niche": "sudden-loss grief journals", "confidence": 0.7}],
        "competitors": [{"title": "Grief Journal for Adult Children", "price": "$9.99"}],
        "consumer_intelligence": {"themes": {"complaints": ["no structure"]}},
        "finalist_sections": [{"candidate_id": "cand-1", "niche": "grief journals"}],
        "candidate_ledger": [],
        "methodology_trace": [],
        "research_notes": [],
        "validation": {"mode": "final"},
    }


def _ctx(mode: str = "prompt") -> dict[str, Any]:
    return build_report_context(
        "sess-1",
        sessions=_Sessions(mode=mode),
        candidates=_ListStore([SimpleNamespace(
            id="cand-1", niche="sudden-loss grief journals", status="verified",
            marketplace="us", score=0.8, rationale="verified by the model",
            meta={"parent_market": "grief journals"},
        )]),
        opportunities=_ListStore([SimpleNamespace(to_dict=lambda: {"niche": "x"})]),
        verifications=_ListStore([SimpleNamespace(
            candidate_id="cand-1", verdict="present", confidence=0.8,
            checks={"demand": "sustained"}, error=None,
        )]),
        claims=_ListStore([]),
        browser_evidence=_ListStore(_evidence()),
        observations=_ListStore([SimpleNamespace(
            source="browser", kind="pattern", content="review depth is low",
            created_at="2026-11-16T10:02:00Z",
        )]),
        notes=_Notes(),
        thoughts=_Thoughts(),
        trace=_Trace(),
        owner_messages=_Owner(),
        artifacts=_Artifacts(),
        model=_model_dict(),
        phase_outputs={"aggressive_niching": {"niches": [{"niche": "grief journals"}]}},
    )


# ------------------------------------------------------------------ dossier
def test_dossier_carries_the_whole_research_journey():
    ctx = _ctx()
    session = ctx["session"]
    assert session["owner_brief"] == "write a grief journal"
    assert session["objective"] == "find a niche"

    journey = ctx["research_journey"]
    assert journey["phases"][0]["phase"] == "aggressive_niching"
    assert journey["phases"][0]["output"]["niches"][0]["niche"] == "grief journals"
    assert "review depth" in journey["reasoning"][0]["excerpt"]
    assert journey["notes"][0]["text"].startswith("Reviewers keep asking")
    assert journey["owner_guidance"][0]["text"] == "focus on adult children"
    assert "read twelve reviews" in journey["actions"][0]["text"]

    # Measured page data survives intact: dates, formats, every BSR, reviews.
    product = next(e for e in ctx["evidence"] if e["id"] == "ev-product")
    assert product["data"]["publication_date"] == "March 3, 2023"
    assert product["data"]["best_seller_ranks"][1]["category"] == "Grief & Loss"
    assert product["data"]["formats"][0] == {"format": "Kindle", "price": "$0.99"}
    assert product["data"]["reviews"][0]["rating"] == 5
    assert product["data"]["print_length"] == "210 pages"

    # Candidates (with their parent market), the verdict and its checks, the
    # rejections, and the measured finalist data all travel with it.
    assert ctx["candidates"][0]["parent_market"] == "grief journals"
    assert ctx["verifications"][0]["checks"]["demand"] == "sustained"
    assert ctx["finalists"][0]["candidate_id"] == "cand-1"
    assert ctx["accounting"]["candidates_rejected"] == 1
    assert ctx["accounting"]["remains_uncertain"] == "review velocity"

    # Screenshots are selectable by id, with a real, authenticated URL.
    shots = ctx["screenshots"]
    assert len(shots) == 1
    assert shots[0]["evidence_id"] == "ev-product"
    assert shots[0]["image_url"] == "/api/sessions/sess-1/artifacts/art-1/file"
    assert "BSR 6,709" in shots[0]["caption"]
    assert screenshot_map(ctx) == {"ev-product": shots[0]["image_url"]}


def test_a_screenshot_whose_artifact_is_gone_is_not_offered():
    rows = _evidence()
    for ev in rows:
        ev.screenshot_artifact_id = "missing-artifact"
    ctx = build_report_context(
        "sess-1",
        sessions=_Sessions(),
        candidates=_ListStore([]),
        opportunities=_ListStore([]),
        verifications=_ListStore([]),
        claims=_ListStore([]),
        browser_evidence=_ListStore(rows),
        observations=_ListStore([]),
        notes=_Notes(),
        artifacts=_Artifacts(),
        model=_model_dict(),
    )
    assert ctx["screenshots"] == []
    assert all(not e["has_screenshot"] for e in ctx["evidence"])


@pytest.mark.parametrize("mode", ["prompt", "keywords", "auto"])
def test_each_mode_is_framed_differently(mode: str):
    ctx = _ctx(mode)
    assert ctx["session"]["mode"] == mode
    assert ctx["mode_instruction"] == MODE_FRAMING[mode]
    prompt = user_prompt(ctx)
    assert MODE_FRAMING[mode].split(".")[0] in prompt


def test_the_three_mode_framings_are_genuinely_different():
    texts = [MODE_FRAMING[m] for m in ("prompt", "keywords", "auto")]
    assert len(set(texts)) == 3
    assert "brief" in MODE_FRAMING["prompt"]
    assert "keywords" in MODE_FRAMING["keywords"]
    assert "found on its own" in MODE_FRAMING["auto"]


def test_prompt_hides_internal_plumbing_but_keeps_the_material():
    ctx = _ctx()
    view = prompt_context(ctx)
    assert "evidence_index" not in view
    assert set(view["screenshots"][0]) == {"evidence_id", "caption", "captured_at"}
    text = user_prompt(ctx)
    assert "RESEARCH DOSSIER" in text
    assert "artifact-1" not in text  # no artifact ids to leak into prose
    # The observed data itself IS in the prompt.
    assert "March 3, 2023" in text
    assert "Grief & Loss" in text


def test_the_prompt_does_not_ask_for_a_fixed_section_list():
    system = REPORT_SYSTEM
    assert "There is no required section list" in system
    assert "[ev:ID]" in system and "shot:EVIDENCE_ID" in system
    # The private-reasoning rule travels with the prompt.
    assert "Never quote them" in system


# --------------------------------------------------------------- normalising
def _screenshot_map() -> dict[str, str]:
    return {"ev-product": "/api/sessions/sess-1/artifacts/art-1/file"}


def _index() -> dict[str, dict[str, Any]]:
    return {
        "ev-product": {"title": "Grief Journal", "kind": "product_page", "marketplace": "us"},
        "ev-search": {"title": "Search: grief journals", "kind": "search_results", "marketplace": "us"},
    }


def test_normalise_resolves_screenshots_and_citations_and_drops_invented_ones():
    raw = (
        "```markdown\n"
        "# The opening nobody is serving\n\n"
        "The leading listing sits at 6,709 with 812 reviews [ev:ev-product]. "
        "A citation to nothing [ev:made-up].\n\n"
        "![BSR 6,709 and 812 reviews on the leading listing](shot:ev-product)\n\n"
        "![A screenshot that was never taken](shot:ghost)\n\n"
        "Demand is printed at over 2,000 results [ev:ev-search].\n\n"
        "That combination is the opening: real demand, a thin review moat, and "
        "an incumbent that has not updated its promise since it launched.\n"
        "```"
    )
    report = normalize_report(
        raw, session_id="sess-1",
        allowed={"6709", "812", "2000"},
        screenshots=_screenshot_map(), evidence_index=_index(),
    )
    assert isinstance(report, WritableReport)
    assert report.title == "The opening nobody is serving"
    assert "# The opening nobody is serving" not in report.markdown
    assert "![BSR 6,709 and 812 reviews on the leading listing]" \
           "(/api/sessions/sess-1/artifacts/art-1/file)" in report.markdown
    assert "/api/sessions/sess-1/evidence/ev-product" in report.markdown
    assert "shot:ghost" not in report.markdown
    assert "[ev:made-up]" not in report.markdown
    assert report.figures == ["ev-product"]
    assert report.removed_figures == ["ghost"]
    assert report.removed_evidence == ["made-up"]
    assert report.words > 20


def test_a_figure_that_was_never_measured_never_reaches_the_report():
    raw = (
        "# Findings\n\n"
        "The measured total is 1,234 competing titles.\n\n"
        "The category holds 987,654 titles and grows 42% a year.\n\n"
        "The narrow niche still rewards a book written for one reader with one "
        "problem, because no incumbent speaks to that reader directly.\n\n"
        "Competitors position themselves as general grief workbooks, which is "
        "why their reviews ask for the sudden-loss structure nobody publishes.\n"
    )
    report = normalize_report(
        raw, session_id="sess-1",
        allowed=allowed_numbers(json.dumps({"total_results": 1234})),
        screenshots={}, evidence_index={},
    )
    assert report is not None
    assert "1,234" in report.markdown
    assert "987,654" not in report.markdown
    assert "42%" not in report.markdown
    assert report.removed_numbers


def test_a_gutted_draft_is_rejected_so_the_fallback_report_ships():
    raw = "# Title\n\nThis sentence invents 5,555,555 titles and 77% growth.\n"
    assert normalize_report(
        raw, session_id="sess-1", allowed=set(), screenshots={}, evidence_index={}
    ) is None
    assert normalize_report("", session_id="sess-1", allowed=set(), screenshots={},
                            evidence_index={}) is None


def test_citations_survive_grounding_even_though_their_ids_contain_digits():
    raw = (
        "# Findings\n\n"
        "The leading listing shows 812 reviews [ev:ev-product], a real moat but "
        "not an unbeatable one for a more specific book.\n\n"
        "Its closest rival has fewer reviews and a broader promise, which is "
        "where a focused entry can take share.\n"
    )
    report = normalize_report(
        raw, session_id="sess-1", allowed={"812"},
        screenshots=_screenshot_map(), evidence_index=_index(),
    )
    assert report is not None
    assert "/api/sessions/sess-1/evidence/ev-product" in report.markdown
    assert report.removed_numbers == []


def test_notes_and_reasoning_numbers_are_not_quotable_measurements():
    ctx = _ctx()
    ctx["research_journey"]["reasoning"].append({"label": "x", "excerpt": "42,777 titles"})
    ctx["research_journey"]["notes"].append({"phase": "x", "text": "42,777 titles"})
    ctx["research_notes"] = [{"phase": "x", "text": "42,777 titles"}]
    allowed = allowed_for(ctx)
    assert "42777" not in allowed
    # …while a measured 812 reviews from the evidence page still is.
    assert "812" in allowed


def test_correction_prompt_names_what_went_wrong():
    ctx = _ctx()
    prompt = correction_prompt(ctx, ["987,654"], ["ghost"])
    assert "CORRECTION REQUIRED" in prompt
    assert "987,654" in prompt
    assert "ghost" in prompt
    # A wholly unusable reply still gets a diagnosable correction.
    assert "empty" in correction_prompt(ctx, [], [])


# ------------------------------------------------------------------- runner
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prompt", "keywords", "auto"])
async def test_the_model_authors_the_report_in_every_mode(db, config, mode):
    from app.reports import ReportStore
    from app.sessions import MODE_AUTO, MODE_KEYWORDS, MODE_PROMPT, SessionStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    sessions = SessionStore(db, config)
    created = sessions.create(
        mode={"prompt": MODE_PROMPT, "keywords": MODE_KEYWORDS, "auto": MODE_AUTO}[mode],
        prompt=(
            "research grief journals for adults" if mode == "prompt"
            else "grief journals, prayer journals" if mode == "keywords"
            else ""
        ),
        objective="find niches",
    )
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(created.id)

    s = sessions.require(created.id)
    assert s.status == "completed", s.error
    assert model.report_prompts, "the model was never asked to write the report"
    dossier = model.report_prompts[0]
    assert MODE_FRAMING[mode].split(".")[0] in dossier

    store = ReportStore(db, config)
    final = next(r for r in store.list(created.id) if r.kind == "final")
    body = store.read_markdown(final)
    meta = final.meta or {}
    assert meta["authored_by"] == "model"
    assert meta["generator"] == "report_writer.v1"
    assert meta["validation"]["mode"] in ("final", "provisional")

    # The body is the model's document: its own title/sections, not the
    # generated layout.
    assert body.startswith("## The opening")
    assert "# Resurrección — Market Intelligence Report" not in body
    assert "## Opportunity portfolio" not in body
    assert "## Methodology trace" not in body

    # It is persisted inside the model so re-rendering and PDF export
    # reproduce it instead of reverting to a template.
    stored = store.read_model_json(final)
    assert stored["ai_report"]["markdown"] == body
    assert stored["ai_report"]["title"] == "What the research found"
    assert meta["words"] > 20


@pytest.mark.asyncio
async def test_inline_screenshot_evidence_reaches_the_report_and_renders(db, config, session):
    """A captured screenshot → selectable evidence → inline <img> URL that the
    authenticated artifact route actually serves."""
    from app.artifacts import ArtifactStore
    from app.browser_store import BrowserEvidenceStore
    from app.reports import ReportStore
    from app.sessions import SessionStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08"
        b"\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00"
        b"\x03\x01\x01\x00\x18\xdd\x8d\xb0\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    art = ArtifactStore(db, config).save_bytes(
        session, kind="screenshot", filename="capture.png", data=png,
    )
    ev = BrowserEvidenceStore(db).create(
        session_id=session, kind="search_results", marketplace="us",
        url="https://www.amazon.com/s?k=grief+journals",
        title="Search: grief journals",
        data={"query": "grief journals", "total_results": 2000,
              "total_results_are_approximate": True},
        screenshot_artifact_id=art.id,
    )

    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    dossier = model.report_prompts[0]
    assert f'"evidence_id": "{ev.id}"' in dossier
    assert f"/artifacts/{art.id}/file" not in dossier  # ids only, backend resolves

    store = ReportStore(db, config)
    final = next(r for r in store.list(session) if r.kind == "final")
    body = store.read_markdown(final)
    url = f"/api/sessions/{session}/artifacts/{art.id}/file"
    assert f"![Search results](shot:{ev.id})" not in body
    assert f"]({url})" in body, body
    assert (final.meta or {})["figures"] == [ev.id]




@pytest.mark.asyncio
async def test_a_useless_report_reply_falls_back_to_the_deterministic_report(
    db, config, session
):
    from app.reports import ReportStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    class MuteforReport(ScriptedModel):
        def _report_reply(self, user: str):  # type: ignore[override]
            reply = super()._report_reply(user)
            reply.raw = ""
            return reply

    runner = make_runner(db, config, MuteforReport(PHASE_OUTPUTS))
    await runner.run(session)

    store = ReportStore(db, config)
    final = next(r for r in store.list(session) if r.kind == "final")
    body = store.read_markdown(final)
    assert (final.meta or {})["authored_by"] == "deterministic_fallback"
    assert "# Resurrección — Market Intelligence Report" in body


@pytest.mark.asyncio
async def test_a_draft_with_invented_figures_gets_one_corrective_retry(db, config, session):
    from app.reports import ReportStore
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    class Inventive(ScriptedModel):
        def __init__(self, script):
            super().__init__(script)
            self.report_calls = 0

        def _report_reply(self, user: str):  # type: ignore[override]
            self.report_calls += 1
            reply = super()._report_reply(user)
            if self.report_calls == 1:
                reply.raw = (
                    "# First attempt\n\nThe market holds 8,888,888 titles and grows "
                    "63% a year.\n\nThis is a paragraph long enough to look like a "
                    "real report but it is built on figures nobody measured.\n"
                )
            return reply

    model = Inventive(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(session)

    assert model.report_calls == 2, "the report was not retried after inventing figures"
    store = ReportStore(db, config)
    final = next(r for r in store.list(session) if r.kind == "final")
    body = store.read_markdown(final)
    assert "8,888,888" not in body
    assert "63%" not in body


def test_pdf_and_re_render_reproduce_the_model_report():
    from app.pdf_export import model_to_blocks
    from app.report_model import ReportModel, render_markdown

    markdown = (
        "## The opening\n\n"
        "The leading listing sits at 6,709 [ev:ev-product].\n\n"
        "- One gap\n- Another gap\n\n"
        "| Book | Price |\n| --- | --- |\n| A | $9.99 |\n\n"
        "![BSR 6,709](__IMG__)\n"
    )
    aid = "art-1"
    markdown = markdown.replace("__IMG__", f"/api/sessions/sess-1/artifacts/{aid}/file")
    model = ReportModel(
        session={"name": "probe", "mode": "prompt", "marketplaces_requested": ["us"]},
        ai_report={"title": "What the research found", "markdown": markdown},
    )
    assert render_markdown(model) == markdown

    blocks = model_to_blocks({**model.to_dict(), "figures": [
        {"artifact_id": aid, "path": "/tmp/capture.png", "caption": "x"},
    ]})[2]
    kinds = [b["t"] for b in blocks]
    assert kinds[0] == "h2"
    assert "li" in kinds and "table" in kinds and "img" in kinds
    img = next(b for b in blocks if b["t"] == "img")
    assert img["path"] == "/tmp/capture.png"
    assert img["caption"] == "BSR 6,709"
    # URLs are not clickable on paper: the citation keeps its id only.
    prose = "\n".join(b.get("x", "") for b in blocks if isinstance(b.get("x"), str))
    assert "[ev:ev-product]" in prose
    assert "/api/sessions" not in prose


def test_the_artifact_route_serves_the_screenshot_an_img_will_request(client):
    """The URL rendered into the report serves real bytes, authenticated."""
    st = client.app.state
    sid = st.sessions.create(mode="prompt", prompt="probe", objective="probe").id
    png = b"\x89PNG\r\n\x1a\ncapture-bytes"
    art = st.artifacts.save_bytes(sid, kind="screenshot", filename="c.png", data=png)
    url = f"/api/sessions/{sid}/artifacts/{art.id}/file"
    assert client.get(url).status_code == 401
    client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    resp = client.get(url)
    assert resp.status_code == 200
    assert resp.content == png


def test_evidence_detail_page_shows_the_capture_behind_a_citation(client):
    st = client.app.state
    sid = st.sessions.create(mode="prompt", prompt="probe", objective="probe").id
    art = st.artifacts.save_bytes(
        sid, kind="screenshot", filename="c.png", data=b"\x89PNG\r\n\x1a\nx",
    )
    ev = st.browser_evidence.create(
        session_id=sid, marketplace="us", kind="product_page",
        url="https://www.amazon.com/dp/B0TEST",
        title="<Grief Journal> for Adult Children",
        data={"price": "$9.99", "best_seller_rank": 6709, "review_count": 812},
        screenshot_artifact_id=art.id,
    )
    client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    resp = client.get(f"/api/sessions/{sid}/evidence/{ev.id}")
    assert resp.status_code == 200
    page = resp.text
    assert "&lt;Grief Journal&gt; for Adult Children" in page  # escaped, not markup
    assert "$9.99" in page and "6709" in page and "812" in page
    assert f"/api/sessions/{sid}/artifacts/{art.id}/file" in page
    # Another session cannot read it, and an unknown id is a clean 404.
    other = st.sessions.create(mode="prompt", prompt="other", objective="other").id
    assert client.get(f"/api/sessions/{other}/evidence/{ev.id}").status_code == 404
    assert client.get(f"/api/sessions/{sid}/evidence/nope").status_code == 404


def test_ai_figures_are_resolvable_for_pdf_export():
    from app.pdf_export import ai_figure_artifacts

    model = {"ai_report": {"markdown": (
        "![A]( /api/sessions/s1/artifacts/abc123/file )\n"
        "![B](/api/sessions/s1/artifacts/def456/file)\n"
    )}}
    assert ai_figure_artifacts(model) == {"abc123": "A", "def456": "B"}
    assert ai_figure_artifacts({"ai_report": {}}) == {}
