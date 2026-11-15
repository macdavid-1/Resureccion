"""Follow-up research continues the WHOLE previous run.

The follow-up session must inherit the parent's complete record — its report,
verified and rejected candidates, measured keywords and categories, the
competitors it saw, its own notes and reasoning excerpts, its operational
trace and the threads it left unfunded — and the owner must be able to add a
prompt, images, or nothing at all.
"""
from __future__ import annotations

import pytest

from app.continuation import ContinuationBrief
from app.continuation_build import build_continuation_brief
from app.research_data import (
    VER_PASSED,
    CandidateStore,
    OpportunityStore,
    ResearchNoteStore,
    VerificationStore,
)
from app.trace import ActivityTrace


# ------------------------------------------------------------------ rendering
def test_the_brief_carries_thoughts_actions_and_measurements() -> None:
    brief = ContinuationBrief(
        parent_session_id="p1",
        parent_name="Grief journals",
        parent_status="completed",
        parent_mode="prompt",
        parent_objective="find low-competition grief niches",
        elapsed_minutes=95,
        marketplaces=["us"],
        counts={"verified": 2, "rejected": 5},
        report_title="Market intelligence report",
        report_summary="Two verified niches with thin competition.",
        opportunities=[{
            "title": "Grief journals for adult children", "niche": "grief journals",
            "confidence": 0.82, "verdict": "verified", "angle": "guided prompts",
        }],
        candidates=[{"niche": "grief journals", "status": "verified"}],
        rejected_candidates=[{"niche": "generic journals", "why": "no differentiation"}],
        keywords=[{"keyword": "grief journals for adult children",
                   "total_results": 2000, "competition": "low",
                   "top10_review_median": 119, "marketplace": "us"}],
        categories=[{"store": "books", "category": "Death & Dying",
                     "breadcrumb": "Books > … > Death & Dying"}],
        thoughts=[{"label": "Thinking", "excerpt": "the broad terms are unwinnable…"}],
        actions=[{"kind": "phase", "text": "Entering Niche Discovery"},
                 {"kind": "verification", "text": "Verified 'grief journals'"}],
        notes=[{"phase": "demand_assessment", "text": "demand is real but seasonal"}],
        open_threads=["sympathy gifts for widowers — unfunded"],
        limitations=["category map stopped at 4 of 10"],
    )
    text = brief.render("")

    assert "CONTINUATION OF AN EARLIER RESEARCH RUN" in text
    assert "Grief journals" in text and "95 min" in text
    # conclusions
    assert "WHAT IT RECOMMENDED" in text and "confidence 0.82" in text
    # measurement + the numbers themselves
    assert "BACKEND KEYWORDS IT MEASURED" in text and "2000 results" in text
    assert "119" in text
    assert "STORE CATEGORIES IT MAPPED" in text and "Death & Dying" in text
    # how it thought and what it did
    assert "ITS WORKING THOUGHTS" in text and "unwinnable" in text
    assert "WHAT IT ACTUALLY DID" in text and "Entering Niche Discovery" in text
    assert "NOTES IT LEFT FOR ITSELF" in text and "seasonal" in text
    # dead ends and loose ends
    assert "CANDIDATES IT REJECTED" in text and "no differentiation" in text
    assert "OPEN THREADS IT DID NOT RESEARCH" in text and "widowers" in text
    assert "LIMITATIONS IT RECORDED" in text and "4 of 10" in text
    # no instructions is a valid request and says what to do instead
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in text
    assert "Do not repeat measurement" in text


def test_owner_instructions_take_precedence_when_given() -> None:
    brief = ContinuationBrief(parent_session_id="p1", parent_name="Run A")
    text = brief.render("Focus on the UK store and price the top three titles.")
    assert "NEW INSTRUCTIONS FROM THE OWNER" in text
    assert "price the top three titles" in text
    assert "NO NEW INSTRUCTIONS" not in text


def test_the_brief_is_bounded_even_for_a_huge_run() -> None:
    brief = ContinuationBrief(
        parent_session_id="p1",
        thoughts=[{"label": "Thinking", "excerpt": "x" * 700}] * 40,
        actions=[{"kind": "browser", "text": "y" * 400}] * 200,
        keywords=[{"keyword": f"kw {i}", "total_results": i} for i in range(60)],
    )
    text = brief.render("")
    assert len(text) <= 9_000
    assert "truncated" in text


# ---------------------------------------------------------------- from stores
@pytest.fixture()
def parent(db, config):
    from app.sessions import SessionStore

    repo = SessionStore(db, config)
    s = repo.create(
        mode="prompt",
        prompt="research grief journals for adult children",
        objective="find low competition grief niches",
        marketplaces=["us"],
    )
    repo.update(s.id, status="completed", phase="reporting")
    return repo, s.id


def test_the_brief_reads_the_parent_run_out_of_its_stores(db, config, parent) -> None:
    repo, sid = parent
    cands = CandidateStore(db)
    opps = OpportunityStore(db)
    vers = VerificationStore(db)
    notes = ResearchNoteStore(db)

    c1 = cands.create(
        session_id=sid, niche="grief journals", marketplace="us",
        rationale="thin competition, real demand", score=0.8,
    )
    cands.set_status(sid, c1.id, "verified")
    c2 = cands.create(
        session_id=sid, niche="generic journals", marketplace="us",
        rationale="no differentiation", score=0.2,
    )
    cands.set_status(sid, c2.id, "rejected")
    opps.create(
        session_id=sid, candidate_id=c1.id, title="Grief journals",
        niche="grief journals", marketplace="us", angle="guided prompts",
        keywords=["grief journal"], projected_demand=0.7,
        projected_competition=0.3, confidence=0.81,
    )
    ver = vers.create(sid, candidate_id=c1.id)
    vers.complete(sid, ver.id, status=VER_PASSED, verdict="verified", checks={})
    stored, _outcome = notes.add(
        sid, text="demand is seasonal", phase="demand_assessment"
    )
    assert stored is not None

    trace = ActivityTrace(db)
    trace.record(sid, "phase", "Entering Niche Discovery")
    trace.record(sid, "verification", "Candidate 'grief journals' verified")

    from app.thoughts import ThoughtStore

    thoughts = ThoughtStore(db)
    tid = thoughts.begin(sid, label="Thinking")
    thoughts.append(tid, "broad terms are unwinnable; niche them down")
    thoughts.finish(tid)

    brief = build_continuation_brief(
        sid,
        sessions=repo,
        candidates=cands,
        opportunities=opps,
        verifications=vers,
        notes=notes,
        trace=trace,
        thoughts=thoughts,
        adjacent_rows=[{"niche": "sympathy gifts for widowers", "why": "unfunded"}],
    )

    assert brief.parent_name
    assert [c["niche"] for c in brief.candidates] == ["grief journals"]
    assert [c["niche"] for c in brief.rejected_candidates] == ["generic journals"]
    assert brief.opportunities and brief.opportunities[0]["verdict"] == "verified"
    assert brief.notes and brief.notes[0]["phase"] == "demand_assessment"
    assert brief.thoughts and "unwinnable" in brief.thoughts[0]["excerpt"]
    assert {a["text"] for a in brief.actions} >= {
        "Entering Niche Discovery", "Candidate 'grief journals' verified",
    }
    assert brief.open_threads == ["sympathy gifts for widowers — unfunded"]

    text = brief.render("")
    assert "grief journals" in text and "unwinnable" in text
    assert "sympathy gifts for widowers" in text
    assert "generic journals" in text


# ------------------------------------------------------------------ the API
def _login(client) -> dict:
    res = client.post("/api/auth/login", json={"username": "owner", "password": "hunter2"})
    assert res.status_code == 200
    return {"X-Auth-Token": res.json()["token"]}


def test_follow_up_needs_no_new_input_and_links_the_parent(client) -> None:
    headers = _login(client)
    res = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals",
              "marketplaces": ["us"]},
    )
    parent = res.json()["session"]["id"]
    client.app.state.sessions.update(parent, status="completed", phase="reporting")

    res = client.post(f"/api/sessions/{parent}/follow-up", headers=headers, json={})
    assert res.status_code == 200, res.text
    child = res.json()["session"]
    assert child["parent_session_id"] == parent
    assert child["marketplaces"] == ["us"]
    assert "CONTINUATION OF AN EARLIER RESEARCH RUN" in child["prompt"]
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in child["prompt"]
    assert child["objective"].startswith("Continuation of")

    # The record is durable, not just baked into the prompt text.
    cp = client.app.state.checkpoints.get_safe(child["id"], "continuation:parent")
    assert cp is not None
    assert cp.payload["parent_session_id"] == parent
    assert cp.payload["instructions"] == ""
    assert "record" in cp.payload


def test_follow_up_carries_the_owner_prompt_and_images(client) -> None:
    headers = _login(client)
    parent = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    ).json()["session"]["id"]

    res = client.post(
        f"/api/sessions/{parent}/follow-up", headers=headers,
        json={"prompt": "Go deeper on the UK store", "has_images": True},
    )
    assert res.status_code == 200, res.text
    child = res.json()["session"]
    assert "Go deeper on the UK store" in child["prompt"]
    assert "NEW INSTRUCTIONS FROM THE OWNER" in child["prompt"]
    assert "NO NEW INSTRUCTIONS" not in child["prompt"]

    # An image-only follow-up is accepted too (no text at all).
    res = client.post(
        f"/api/sessions/{parent}/follow-up", headers=headers,
        json={"has_images": True},
    )
    assert res.status_code == 200, res.text
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in res.json()["session"]["prompt"]


def test_follow_up_on_an_unknown_session_is_404(client) -> None:
    headers = _login(client)
    res = client.post("/api/sessions/nope/follow-up", headers=headers, json={})
    assert res.status_code == 404


def test_the_continuation_preview_describes_what_will_be_carried(client) -> None:
    headers = _login(client)
    parent = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    ).json()["session"]["id"]
    res = client.get(f"/api/sessions/{parent}/continuation", headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert "opportunit" in body["digest"]
    assert isinstance(body["open_threads"], list)


def test_the_dashboard_offers_prompt_images_or_nothing() -> None:
    """The dialog must expose all three choices — a prompt, images, or neither."""
    from pathlib import Path

    js = Path("static/app.js").read_text()
    assert "/follow-up`" in js
    assert "Continue without new instructions" in js
    assert "Start follow-up" in js
    assert "fu-images" in js and "fu-prompt" in js
    # The old one-click seed that ignored the previous run is gone.
    assert "api(/api/sessions`, {" not in js


def test_the_whole_brief_reaches_the_phase_prompt() -> None:
    """Regression: the prompt's generic 2400-char digest cap used to cut the
    session brief, which would have silently dropped most of a continuation
    record before the model ever saw it."""
    from app.methodology import PhaseTiming
    from app.prompts import build_user_prompt

    brief = ContinuationBrief(
        parent_session_id="p1",
        parent_name="Previous run",
        thoughts=[{"label": "Thinking", "excerpt": "reasoning " + "a" * 300}] * 6,
        actions=[{"kind": "browser", "text": "searched " + "b" * 200}] * 20,
        open_threads=["thread " + "c" * 100] * 10,
    )
    text = brief.render("")
    assert 2_400 < len(text) <= 9_000

    prompt = build_user_prompt(
        phase="opportunity_discovery",
        mode="prompt",
        objective="continuation",
        prompt_text=text,
        marketplaces=["us"],
        marketplace_mode="explicit",
        evidence_digest=[],
        candidates_digest=[],
        prior_phase_outputs=None,
        timing=PhaseTiming(phase="opportunity_discovery", started_at="now"),
    )
    # The END of the record is what proves nothing was truncated: it only
    # survives if the whole brief made it through.
    assert "OPEN THREADS IT DID NOT RESEARCH" in prompt
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in prompt
