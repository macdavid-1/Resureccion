"""End-to-end hardening of the research → report pipeline.

Every test here pins a failure mode that a real long session can hit and that
used to cost analysis, stall the run, or downgrade the deliverable for a
cosmetic reason:

- a thin final reply discarding richer earlier analysis of the same phase,
- one malformed model reply pausing an otherwise healthy multi-hour run,
- a genuinely broken/model-rejecting endpoint burning the whole budget,
- a model writing marketplaces in its own words (or omitting them) making a
  complete report PROVISIONAL,
- a render (or structured-build) defect delivering no report at all,
- a re-rendered report losing its FINAL / PROVISIONAL status.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.model_client import ModelError, ModelRequestError
from tests.test_research_runner import (
    PHASE_OUTPUTS,
    ScriptedModel,
    make_runner,
    session,  # noqa: F401 — pytest fixture reuse
)


def _script() -> dict:
    import copy

    return copy.deepcopy(PHASE_OUTPUTS)


def _final_report(db, config, session_id: str):
    from app.reports import ReportStore

    store = ReportStore(db, config)
    final = [r for r in store.list(session_id) if r.kind == "final"]
    assert final, "no final report was persisted"
    return final[0], store.read_markdown(final[0]), store.read_model_json(final[0])


def _events(db, session_id: str, action: str) -> list:
    from app.events import EventLog

    log = EventLog(db)
    rows: list = []
    after = 0
    while True:
        batch = log.tail(session_id, after_id=after, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        after = batch[-1].id
    return [e for e in rows if e.action == action]


class TrackingModel(ScriptedModel):
    """Counts CALL ATTEMPTS (including the ones that fail)."""

    def __init__(self, script: dict) -> None:
        super().__init__(script)
        self.attempts = 0

    async def call(self, session_id: str, *, system: str, user: str, **k):
        self.attempts += 1
        return await super().call(session_id, system=system, user=user, **k)


# --------------------------------------------------------------- phase commit
class ThinCommitModel(TrackingModel):
    """Synthesis explores with a full analysis, then commits an empty list.

    Real models do this: the first reply carries the opportunities plus a
    bookkeeping action, the follow-up "commit" reply repeats nothing because
    it believes the work is already reported.
    """

    def __init__(self, script: dict) -> None:
        super().__init__(script)
        self.synthesis_calls = 0

    def _reply_for(self, phase: str, user: str):
        reply = super()._reply_for(phase, user)
        if phase == "opportunity_synthesis":
            self.synthesis_calls += 1
            if self.synthesis_calls == 1:
                reply.content["actions"] = [
                    {"action": "record_observation",
                     "args": {"note": "synthesis in progress"}},
                ]
            else:
                reply.content = {"opportunities": []}
        return reply


@pytest.mark.asyncio
async def test_thin_final_reply_does_not_discard_earlier_analysis(db, config, session):
    from app.research_data import OpportunityStore
    from app.sessions import SessionStore

    model = ThinCommitModel(_script())
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    assert model.synthesis_calls >= 2, "the thin commit reply never happened"
    # The opportunity the FIRST synthesis reply produced must survive the
    # empty commit reply.
    opps = OpportunityStore(db).list(session)
    assert len(opps) == 1, [o.niche for o in opps]
    assert "grief" in opps[0].niche


def test_phase_output_keeps_analysis_and_drops_control_keys():
    from app.research_runner import _phase_content

    cleaned = _phase_content({
        "niches": [{"niche": "grief journals for adult daughters"}],
        "actions": [{"action": "search_marketplace"}],
        "activity_note": "looking around",
        "notes": ["a note"],
        "goto_phase": "opportunity_discovery",
    })
    assert cleaned == {"niches": [{"niche": "grief journals for adult daughters"}]}


# ------------------------------------------------------------- model failures
class FailingModel(TrackingModel):
    """Fails a number of call attempts before behaving normally."""

    def __init__(self, script: dict, failures: int, exc_factory) -> None:
        super().__init__(script)
        self.failures = failures
        self.exc_factory = exc_factory

    async def call(self, session_id: str, *, system: str, user: str, **k):
        if self.failures > 0:
            self.failures -= 1
            self.attempts += 1
            raise self.exc_factory()
        return await super().call(session_id, system=system, user=user, **k)


@pytest.mark.asyncio
async def test_one_bad_reply_is_absorbed_not_paused(db, config, session):
    """A truncated/unparseable reply is a failed iteration, not a dead run."""
    from app.sessions import SessionStore

    model = FailingModel(_script(), failures=2, exc_factory=lambda: ModelError("truncated JSON"))
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    assert _events(db, session, "model_iteration_failed"), "the failure was not journaled"
    assert _events(db, session, "final_report_written")


@pytest.mark.asyncio
async def test_sustained_model_outage_pauses_instead_of_burning_the_budget(db, config, session):
    """A dead endpoint must stop the session for the owner, not fake progress."""
    from app.sessions import SessionStore

    model = FailingModel(_script(), failures=10_000, exc_factory=lambda: ModelError("model call failed"))
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "paused", s.status
    assert "model failure" in (s.error or "")
    assert model.attempts <= 8, model.attempts  # bounded, not the full budget
    assert _events(db, session, "model_unreachable_pausing")
    # The session stays resumable from its durable cursor.
    assert s.status in ("paused", "interrupted")


@pytest.mark.asyncio
async def test_request_rejection_pauses_immediately(db, config, session):
    """401/402/403/404 is deterministic: retrying or continuing cannot help."""
    from app.sessions import SessionStore

    model = FailingModel(
        _script(), failures=1,
        exc_factory=lambda: ModelRequestError("model request rejected (401): bad key"),
    )
    runner = make_runner(db, config, model)
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "paused", s.status
    assert model.attempts == 1, "a rejected request must not be retried"
    assert "bad key" in (s.error or "")


def test_provider_rejection_is_a_request_error():
    from app.model_client import _REQUEST_REJECTED

    assert 401 in _REQUEST_REJECTED and 402 in _REQUEST_REJECTED
    assert 403 in _REQUEST_REJECTED and 404 in _REQUEST_REJECTED
    # Rate limits and server faults stay transient.
    assert 429 not in _REQUEST_REJECTED and 500 not in _REQUEST_REJECTED


@pytest.mark.asyncio
async def test_unauthorized_call_raises_request_error():
    from app.config import Config
    from app.model_client import ModelClient

    cfg = Config()
    cfg.model_api_key = "test-key"
    cfg.model_base_url = "https://fake.invalid/api/v1"
    cfg.model_stream_enabled = False
    cfg.model_vision_enabled = False

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key")

    client = ModelClient(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(ModelRequestError):
        await client.call("s1", system="sys", user="usr")


@pytest.mark.asyncio
async def test_truncated_reply_is_retried_with_more_room(db, config, session):
    """finish_reason='length' means the JSON was cut in half, not that the
    model was wrong: the call retries once with double the token budget."""
    from app.config import Config
    from app.events import EventLog
    from app.model_client import ModelClient

    cfg = Config()
    cfg.model_api_key = "test-key"
    cfg.model_base_url = "https://fake.invalid/api/v1"
    cfg.model_stream_enabled = False
    cfg.model_vision_enabled = False
    cfg.model_max_tokens = 1000
    budgets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        budgets.append(_json.loads(request.content)["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json={
                "choices": [{
                    "message": {"content": '{"opportunities": [{"niche": "trunc'},
                    "finish_reason": "length",
                }],
                "usage": {},
            })
        return httpx.Response(200, json={
            "choices": [{
                "message": {"content": '{"opportunities": []}'},
                "finish_reason": "stop",
            }],
            "usage": {},
        })

    events = EventLog(db)
    client = ModelClient(cfg, events, transport=httpx.MockTransport(handler))
    reply = await client.call(session, system="sys", user="usr")

    assert reply.content == {"opportunities": []}
    assert budgets == [1000, 2000], budgets
    logged = [
        e for e in events.tail(session, limit=200)
        if e.action == "model_output_truncated"
    ]
    assert logged, "the truncation retry was not journaled"


def test_prior_phase_decisions_are_rendered_per_phase():
    """One large phase must not crowd every later phase out of the prompt."""
    from app.methodology import PhaseTiming
    from app.prompts import build_user_prompt

    prompt = build_user_prompt(
        "opportunity_synthesis",
        mode="prompt",
        objective="",
        prompt_text="brief",
        marketplaces=["us"],
        marketplace_mode="explicit",
        evidence_digest=[],
        candidates_digest=[],
        prior_phase_outputs={
            "opportunity_discovery": {"signals": [{"topic": "x" * 3000}]},
            "demand_validation": {"demand_assessments": [{"verdict": "sustained demand"}]},
            "adversarial_verification": {"verdicts": [{"verdict": "pass"}]},
        },
        timing=PhaseTiming(phase="opportunity_synthesis", started_at="now"),
    )
    assert "[Phase 5 — Demand Validation]" in prompt
    assert "sustained demand" in prompt
    assert "[Phase 8 — Adversarial Verification]" in prompt
    # The big discovery phase is capped; the small decisive ones survive.
    assert len(prompt) < 60_000


def test_extract_json_tolerates_chatter_and_trailing_braces():
    from app.model_client import _extract_json

    raw = 'Here is the analysis {not json} you asked for:\n{"signals": [1], "observations": []}\n(see {evidence} above)'
    assert _extract_json(raw) == {"signals": [1], "observations": []}
    fenced = '```json\n{"niches": ["a"]}\n```'
    assert _extract_json(fenced) == {"niches": ["a"]}
    with pytest.raises(ModelError):
        _extract_json("no object here at all")


# ----------------------------------------------------------- marketplaces
@pytest.mark.parametrize(
    "written,expected",
    [
        (["Amazon.com", "United Kingdom"], ["us", "uk"]),
        (["US", "amazon.co.uk"], ["us", "uk"]),
        (["us, uk"], ["us", "uk"]),
        (["United States of America"], ["us"]),
        (["Neverland"], []),
    ],
)
def test_marketplace_labels_normalize_to_codes(written, expected):
    from app.marketplace import normalize_marketplace_codes

    assert normalize_marketplace_codes(written) == expected


@pytest.mark.asyncio
async def test_shortened_opportunity_niche_still_reaches_its_candidate(db, config, session):
    """Synthesis often names the niche shorter than niching did.

    The dossier must attach to the verified candidate it describes (the same
    containment rule claims and the ledger use) instead of being rejected as
    an orphan — which used to leave a healthy run with zero opportunities.
    """
    from app.research_data import OpportunityStore
    from app.sessions import SessionStore

    script = _script()
    script["opportunity_synthesis"][0]["opportunities"][0]["niche"] = (
        "sudden-loss grief journals"
    )
    runner = make_runner(db, config, ScriptedModel(script))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    opps = OpportunityStore(db).list(session)
    assert len(opps) == 1
    assert "sudden-loss grief" in opps[0].niche
    final = _final_report(db, config, session)[0]
    assert (final.meta or {}).get("validation", {}).get("passed") is True


@pytest.mark.asyncio
async def test_opportunity_marketplaces_in_the_models_own_words(db, config, session):
    """'Amazon.com' / 'United Kingdom' must not cost the report its FINAL status."""
    from app.research_data import OpportunityStore
    from app.sessions import SessionStore

    script = _script()
    script["opportunity_synthesis"][0]["opportunities"][0]["marketplaces"] = [
        "Amazon.com", "United Kingdom",
    ]
    runner = make_runner(db, config, ScriptedModel(script))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    opps = OpportunityStore(db).list(session)
    assert opps[0].marketplace == "us,uk", opps[0].marketplace
    _, _, _ = _final_report(db, config, session)
    final = _final_report(db, config, session)[0]
    assert (final.meta or {}).get("validation", {}).get("mode") == "final", (
        final.meta or {}
    ).get("validation", {}).get("failures")


@pytest.mark.asyncio
async def test_omitted_opportunity_marketplace_falls_back_to_the_candidate_market(
    db, config, session
):
    """An omitted market is an omission, not a reason to drop a verified dossier."""
    from app.marketplace import normalize_marketplace_codes
    from app.research_data import OpportunityStore
    from app.sessions import SessionStore

    script = _script()
    script["opportunity_synthesis"][0]["opportunities"][0]["marketplaces"] = []
    runner = make_runner(db, config, ScriptedModel(script))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    opps = OpportunityStore(db).list(session)
    assert len(opps) == 1
    assert normalize_marketplace_codes(opps[0].marketplace) == ["us"]


# ------------------------------------------------------------- report delivery
@pytest.mark.asyncio
async def test_render_failure_still_delivers_a_report(db, config, session, monkeypatch):
    from app import report_model
    from app.sessions import SessionStore

    def boom(model):
        raise RuntimeError("injected render defect")

    monkeypatch.setattr(report_model, "render_markdown", boom)
    runner = make_runner(db, config, ScriptedModel(_script()))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    _rep, body, _model = _final_report(db, config, session)
    assert "delivered in compact form" in body
    assert "injected render defect" in body
    assert _events(db, session, "report_render_failed")
    # The report is still validated and labeled.
    assert "Report status:" in body


@pytest.mark.asyncio
async def test_report_model_failure_still_delivers_a_report(db, config, session, monkeypatch):
    from app import report_model
    from app.sessions import SessionStore

    def boom(*a, **k):
        raise RuntimeError("injected model-build defect")

    monkeypatch.setattr(report_model, "build_report_model", boom)
    runner = make_runner(db, config, ScriptedModel(_script()))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    _rep, body, _model = _final_report(db, config, session)
    assert "delivered in compact form" in body
    assert _events(db, session, "report_model_failed")


class NarrativeModel(TrackingModel):
    """Writes notes during research, then quotes a NOTE-ONLY figure as fact."""

    def _reply_for(self, phase: str, user: str):
        reply = super()._reply_for(phase, user)
        reply.content["notes"] = [
            (
                "A single keyword shows 42,777 competing titles, which felt "
                "softer than the category average."
            ),
        ]
        return reply

    async def call(self, session_id: str, *, system: str, user: str, **k):
        if "senior market-intelligence analyst" in system:
            self.attempts += 1
            return FakeReply({
                "overview": "The niche is thin: 42,777 competing titles suggest room.",
                "opportunity_analyses": {},
                "risks": "Not measured in this session.",
            })
        return await super().call(session_id, system=system, user=user, **k)


class FakeReply:
    def __init__(self, content: dict) -> None:
        self.content = content
        self.model = "fake"
        self.elapsed_seconds = 0.1
        self.raw = "{}"


@pytest.mark.asyncio
async def test_a_note_only_figure_never_becomes_analyst_prose(db, config, session):
    """Notes are memory, not measurements: their numbers are not grounded."""
    from app.sessions import SessionStore

    runner = make_runner(db, config, NarrativeModel(_script()))
    await runner.run(session)

    s = SessionStore(db, config).require(session)
    assert s.status == "completed", s.error
    _rep, body, model = _final_report(db, config, session)
    # The note itself is preserved, verbatim, in the notes section.
    assert "42,777" in body
    # …but the analyst prose may not repeat it as a measured figure.
    narrative = (model or {}).get("narrative") or {}
    assert "42,777" not in json.dumps(narrative), narrative


def test_redaction_scan_separates_prose_from_real_secrets():
    from app.report_validation import ReportValidator

    prose = (
        "Top incumbents: **Password: A Memoir**, **Cookie: 100 Recipes**.\n"
        "| authorization | none recorded |\n"
    )
    assert ReportValidator._redaction_scan(prose) == []

    leaked = 'headers: {"Authorization": "Bearer sk-live-9f8a7b6c5d4e3f2a1b0c"}'
    assert "authorization" in ReportValidator._redaction_scan(leaked)
    assert "password" in ReportValidator._redaction_scan(
        "password=correcthorsebatterystaple123"
    )


def test_report_status_survives_a_stored_model_round_trip():
    from app.report_model import ReportModel, render_markdown

    model = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        validation={"passed": True, "failures": [], "mode": "final"},
    )
    first = render_markdown(model)
    assert "**Report status: FINAL**" in first
    # Persisted → parsed back → rendered again: the status must not vanish.
    again = ReportModel.from_dict(json.loads(model.to_json()))
    assert "**Report status: FINAL**" in render_markdown(again)

    failed = ReportModel(
        session={"id": "s1", "name": "Run", "mode": "prompt"},
        executive_summary={},
        validation={"passed": False, "mode": "provisional",
                    "failures": ["quality gate failed: candidate_gate — too few candidates"]},
    )
    body = render_markdown(failed)
    assert "**Report status: PROVISIONAL**" in body
    assert "quality gate failed: candidate_gate" in body
