"""Follow-up research continues the WHOLE previous run.

The follow-up session must inherit the parent's complete record — the brief
the owner sent, every message they typed during it, the full report, verified
and rejected candidates, measured keywords and categories, the competitors it
saw, all of its notes and all of its reasoning, its operational trace and the
threads it left unfunded — and the owner must be able to add a prompt, images,
or nothing at all.
"""
from __future__ import annotations

import re

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
    # measurement + the numbers themselves (Amazon's OVERALL total, not the
    # number of rows a first page happened to show)
    assert "BACKEND KEYWORDS IT MEASURED" in text and "2,000 total results" in text
    assert "119" in text
    assert "STORE CATEGORIES IT MAPPED" in text and "Death & Dying" in text
    # how it thought and what it did
    assert "ITS WORKING THOUGHTS" in text and "unwinnable" in text
    assert "WHAT IT ACTUALLY DID" in text and "Entering Niche Discovery" in text
    assert "NOTES IT LEFT FOR ITSELF" in text and "seasonal" in text
    # dead ends and loose ends
    assert "CANDIDATES IT REJECTED" in text and "no differentiation" in text
    assert "OPEN THREADS IT DID NOT FINISH" in text and "widowers" in text
    assert "LIMITATIONS IT RECORDED" in text and "4 of 10" in text
    # no instructions is a valid request and says what to do instead
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in text
    assert "Do not rebuild a keyword pool" in text


def test_owner_instructions_take_precedence_when_given() -> None:
    brief = ContinuationBrief(parent_session_id="p1", parent_name="Run A")
    text = brief.render("Focus on the UK store and price the top three titles.")
    assert "NEW INSTRUCTIONS FROM THE OWNER" in text
    assert "price the top three titles" in text
    assert "NO NEW INSTRUCTIONS" not in text


def test_an_ordinary_long_run_is_carried_whole() -> None:
    """Nothing about a normal run may be condensed: a report far larger than
    the old 1,600-char summary, long notes, long reasoning and a long brief
    all arrive intact."""
    report = "# Report\n" + ("Paragraph of the previous report.\n" * 2_000)
    brief = ContinuationBrief(
        parent_session_id="p1",
        parent_prompt="Original brief. " * 400,
        report_markdown=report,
        report_summary="short summary",
        notes=[{"phase": "demand_validation", "text": "note " + str(i) + " " + "z" * 1_500}
               for i in range(30)],
        thoughts=[{"label": "Thinking", "excerpt": "reasoning " + "q" * 3_000}
                  for _ in range(25)],
    )
    text = brief.render("")
    assert len(text) > 60_000
    # the whole report, its last paragraph included
    assert text.count("Paragraph of the previous report.") == 2_000
    # every note, not the newest 14 clipped to 240 chars
    assert "note 29 " in text and "note 0 " in text
    assert len(re.findall(r"z{1500}", text)) == 30
    # every reasoning row, uncondensed
    assert len(re.findall(r"q{3000}", text)) == 25
    # and the original brief, verbatim
    assert text.count("Original brief.") == 400
    assert "truncated" not in text


def test_no_amount_of_parent_material_is_ever_dropped() -> None:
    """There is NO size limit on what a follow-up inherits: not a character
    budget, not a top-N, not an excerpt. A gigantic parent arrives whole."""
    report = "# Huge report\n" + "| keyword | results |\n" * 40_000  # ~640k chars
    brief = ContinuationBrief(
        parent_session_id="p1",
        parent_prompt="original brief " * 40_000,
        thoughts=[{"label": "Thinking", "excerpt": "reasoning " + "q" * 30_000}
                  for _ in range(40)],
        notes=[{"phase": "x", "text": "n" * 20_000} for _ in range(40)],
        memory=[{"kind": "decision", "text": "m" * 50_000} for _ in range(20)],
        report_markdown=report,
        actions=[{"kind": "browser", "text": "a" * 500} for _ in range(2_000)],
        open_threads=["thread " + "t" * 200] * 500,
    )
    text = brief.render("Look only at the UK store")
    assert len(text) > 2_000_000
    assert text.count("| keyword | results |") == 40_000
    assert text.count("q" * 30_000) == 40
    assert "thread " + "t" * 200 in text
    assert text.count("original brief") == 40_000
    assert "NOT REPEATED HERE" not in text and "truncated" not in text
    assert text.rstrip().endswith("Look only at the UK store")


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


def test_the_full_report_owner_messages_and_original_brief_travel(db, config) -> None:
    """What the owner asked for when the run started, what they said WHILE it
    ran, and the report it ended with all arrive verbatim.

    Regression: the report was read off the row (which stores only a path), so
    it was always empty, and the brief/owner messages were either clipped or
    missing entirely.
    """
    from app.owner_messages import OwnerMessageStore
    from app.reports import ReportStore
    from app.sessions import SessionStore

    repo = SessionStore(db, config)
    long_brief = (
        "THE ORIGINAL BRIEF. " + "detail about the audience. " * 200
        + "LAST-BRIEF-LINE"
    )
    s = repo.create(
        mode="prompt", prompt=long_brief, objective="low competition grief",
        marketplaces=["us"],
    )
    reports = ReportStore(db, config)
    body = (
        "# Final report\n\n" + "| keyword | results |\n|---|---|\n| grief | 2,000 |\n" * 800
        + "\nTAIL-OF-THE-REPORT\n"
    )
    reports.save(s.id, kind="final", title="Grief audit", body_markdown=body)
    msgs = OwnerMessageStore(db)
    msgs.post(s.id, "only the UK store matters from now on")
    msgs.post(s.id, "do not touch the prayer niche again")

    brief = build_continuation_brief(
        s.id,
        sessions=repo,
        candidates=CandidateStore(db),
        opportunities=OpportunityStore(db),
        verifications=VerificationStore(db),
        notes=ResearchNoteStore(db),
        reports=reports,
        owner_messages=msgs,
    )
    assert brief.report_markdown.strip() == body.strip()
    assert len(brief.owner_messages) == 2

    text = brief.render("")
    assert "TAIL-OF-THE-REPORT" in text
    assert "EVERY INSTRUCTION THE OWNER GAVE DURING IT" in text
    assert "only the UK store matters from now on" in text
    assert "do not touch the prayer niche again" in text
    assert "LAST-BRIEF-LINE" in text
    assert "----- BEGIN REPORT -----" in text


def test_every_note_is_carried_not_the_newest_few(db, config, parent) -> None:
    repo, sid = parent
    notes = ResearchNoteStore(db)
    for i in range(30):
        notes.add(sid, text=f"note number {i} " + "y" * 300, phase="demand_validation")

    brief = build_continuation_brief(
        sid,
        sessions=repo,
        candidates=CandidateStore(db),
        opportunities=OpportunityStore(db),
        verifications=VerificationStore(db),
        notes=notes,
    )
    assert len(brief.notes) == 30
    text = brief.render("")
    assert "note number 0 " in text and "note number 29 " in text
    assert len(re.findall(r"y{300}", text)) == 30


def test_a_continuation_owes_discovery_nothing(db, config) -> None:
    """The fresh-run floors ("discovery must emit a signal", "niching must
    emit a niche") must not force a continuation to start a new pool."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    from app.methodology import SPECS

    runner = make_runner(db, config, ScriptedModel(PHASE_OUTPUTS))
    assert runner._output_floor("opportunity_discovery", {}, 0) == 1
    assert runner._item_floor(SPECS["opportunity_discovery"], 0) == 1
    assert runner._output_floor(
        "opportunity_discovery", {}, 0, continuation=True
    ) == 0
    assert runner._item_floor(
        SPECS["opportunity_discovery"], 0, continuation=True
    ) == 0
    assert runner._output_floor("aggressive_niching", {}, 0, continuation=True) == 0
    # Analysis phases keep their floor when the continuation has candidates.
    assert runner._output_floor(
        "competitive_landscape", {}, 3, continuation=True
    ) == 1


@pytest.mark.asyncio
async def test_a_continuation_run_skips_the_fresh_market_walk(db, config) -> None:
    """No census, no autocomplete frontier, no forced first phase: the model
    gets its previous run's record and decides where to continue."""
    from test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    from app.events import EventLog
    from app.sessions import SessionStore

    sessions = SessionStore(db, config)
    parent = sessions.create(mode="prompt", prompt="research grief journals")
    child = sessions.create(
        mode="prompt",
        prompt="CONTINUATION OF AN EARLIER RESEARCH RUN — record here.",
        objective="Continuation of the grief run",
        parent_session_id=parent.id,
    )
    model = ScriptedModel(PHASE_OUTPUTS)
    runner = make_runner(db, config, model)
    await runner.run(child.id)
    assert sessions.require(child.id).status == "completed"

    cp = runner.checkpoints.get_safe(child.id, "discovery_preflight")
    assert cp is not None and cp.payload.get("skipped") == "continuation"
    assert not any(
        e.action == "discovery_preflight_started"
        for e in EventLog(db).tail(child.id)
    )

    first = model.user_prompts["opportunity_discovery"][0]
    assert "HOW TO READ THIS RUN" in first
    assert "Do NOT generate a broad pool" in first
    assert "'goto_phase'" in first
    assert "YOU decide what this run does first" in first
    assert "MARKET CENSUS" not in first
    assert "AUTOCOMPLETE FRONTIER" not in first
    # The phase contract tells the model the phase may legitimately owe
    # nothing: no floor forcing a fresh signal list.
    assert "must hold at least 0 item(s)" in first
    # …and it really is free to commit at once: no forcing loop, no
    # mandatory evidence hunt before it may hand over.
    assert len(model.user_prompts["opportunity_discovery"]) == 1


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


def test_follow_up_inherits_the_previous_candidate_ledger(client) -> None:
    """The previous run's live candidates become this run's own rows, so the
    digests, headroom ordering and synthesis links continue the investigation
    instead of starting from an empty ledger."""
    from app.research_data import CandidateStore

    headers = _login(client)
    parent = client.post(
        "/api/sessions", headers=headers,
        json={"mode": "prompt", "prompt": "research grief journals"},
    ).json()["session"]["id"]
    st = client.app.state
    cands = CandidateStore(st.db)
    keep = cands.create(
        parent, niche="grief journals for adult children", marketplace="us",
        rationale="thin competition", score=0.8,
    )
    cands.set_status(parent, keep.id, "verified")
    dead = cands.create(parent, niche="generic journals", rationale="crowded")
    cands.set_status(parent, dead.id, "rejected")

    res = client.post(f"/api/sessions/{parent}/follow-up", headers=headers, json={})
    assert res.status_code == 200, res.text
    child = res.json()["session"]["id"]
    seeded = cands.list(child, limit=50)
    assert [c.niche for c in seeded] == ["grief journals for adult children"]
    assert seeded[0].status == "discovered"  # found, not falsely verified
    assert seeded[0].meta["continued_from"] == {
        "session_id": parent, "candidate_id": keep.id,
        "status": "verified", "score": 0.8,
    }
    cp = st.checkpoints.get_safe(child, "continuation:parent")
    assert cp.payload["seeded_candidates"][0]["niche"] == seeded[0].niche


def test_the_system_prompt_says_this_is_a_continuation() -> None:
    from app.prompts import build_system_prompt

    cont = build_system_prompt("opportunity_discovery", continuation=True)
    assert "THIS RUN IS A CONTINUATION" in cont
    assert "do not open a fresh autocomplete/keyword sweep" in cont
    assert "THIS RUN IS A CONTINUATION" not in build_system_prompt(
        "opportunity_discovery"
    )
    assert "THIS RUN IS A CONTINUATION" in build_system_prompt(
        "aggressive_niching", continuation=True
    )


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


def test_the_whole_record_reaches_the_phase_prompt_verbatim() -> None:
    """Regression: the prompt's generic 2400-char digest cap cut the session
    brief, and the 16k JSON cap then cut it again — so most of a continuation
    record never reached the model."""
    from app.methodology import PhaseTiming
    from app.prompts import build_user_prompt

    brief = ContinuationBrief(
        parent_session_id="p1",
        parent_name="Previous run",
        report_markdown="# Report\n" + "row | value\n" * 3_000,
        thoughts=[{"label": "Thinking", "excerpt": "reasoning " + "a" * 300}] * 6,
        memory=[{"kind": "finding", "text": "MEMORY-LINE " + "m" * 5_000}],
        actions=[{"kind": "browser", "text": "searched " + "b" * 200}] * 20,
        open_threads=["thread " + "c" * 100] * 10,
    )
    text = brief.render("")
    assert len(text) > 40_000

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
        continuation=True,
        brief_limit=len(text) + 1_000,
    )
    # The END of the record is what proves nothing was truncated: it only
    # survives if the whole record made it through.
    assert "OPEN THREADS IT DID NOT FINISH" in prompt
    assert "NO NEW INSTRUCTIONS WERE GIVEN" in prompt
    # The parent's memory is delivered by the follow-up's own memory journal
    # (seeded verbatim), never duplicated inside the record.
    assert "THE MEMORY IT KEPT" not in prompt
    assert len(brief.memory) == 1
    # Handed over as text, not as one escaped JSON string.
    assert "----- END REPORT -----" in prompt
    assert "HOW TO READ THIS RUN" in prompt
    assert "YOU decide what this run does first" in prompt
    # A continuation is told not to rebuild a pool of new markets.
    assert "Do NOT generate a broad pool" in prompt
