"""The model can EDIT and REMOVE its own research records.

A research record is not write-once. These tests pin the contract the model
needs: correct/rename/re-status/delete a candidate, edit an opportunity, fix a
wrong competition figure on a capture, and correct or drop a claim — with an
auditable refusal instead of a silent no-op when the target is ambiguous.
"""
from __future__ import annotations

import pytest

from app.browser_store import BrowserEvidenceStore
from app.claims import ClaimStore
from app.record_edits import RecordEditError, RecordEditor
from app.research_data import CandidateStore, OpportunityStore, VerificationStore
from app.search_totals import search_total
from tests.test_research_runner import session  # noqa: F401 — pytest fixture


@pytest.fixture()
def editor(db) -> RecordEditor:
    return RecordEditor(
        candidates=CandidateStore(db),
        opportunities=OpportunityStore(db),
        claims=ClaimStore(db),
        browser_evidence=BrowserEvidenceStore(db),
    )


class TestCandidates:
    def test_the_model_edits_any_candidate_field(self, db, session, editor) -> None:
        cands = CandidateStore(db)
        cand = cands.create(
            session, niche="grief journals", marketplace="us",
            rationale="first read", meta={"chain": ["grief", "journals"]},
        )
        out = editor.apply(
            session, "update_candidate",
            {
                "niche": "grief journals",
                "marketplace": "uk",
                "rationale": "measured: crowding on amazon.co.uk",
                "status": "rejected",
                "score": -0.5,
                "meta": {"reviewed_by": "model"},
            },
        )
        assert out["ok"] is True
        row = cands.get(session, cand.id)
        assert row.marketplace == "uk"
        assert row.status == "rejected"
        assert row.score == -0.5
        assert "crowding" in row.rationale
        # meta MERGES: unrelated bookkeeping survives the edit
        assert row.meta["chain"] == ["grief", "journals"]
        assert row.meta["reviewed_by"] == "model"

    def test_a_niche_found_mid_browse_can_be_added(self, db, session, editor) -> None:
        out = editor.apply(
            session, "add_candidate",
            {
                "niche": "estate planning workbook for carers",
                "marketplace": "US",
                "rationale": "found in a competitor's also-bought row",
                "score": 0.4,
                "meta": {"found_on": "asin B0TEST0001"},
            },
        )
        assert out["ok"] is True
        row = CandidateStore(db).get(session, out["target"]["candidate_id"])
        assert row.niche == "estate planning workbook for carers"
        assert row.marketplace == "us"
        assert row.score == 0.4
        assert row.meta["source"] == "model_action"
        assert row.meta["found_on"] == "asin B0TEST0001"

    def test_adding_a_niche_it_already_has_points_at_the_existing_row(
        self, db, session, editor
    ) -> None:
        existing = CandidateStore(db).create(
            session, niche="sudden-loss grief memoirs"
        )
        with pytest.raises(RecordEditError, match=existing.id):
            editor.apply(
                session, "add_candidate", {"niche": "Sudden-Loss  Grief Memoirs"}
            )

    def test_candidate_id_is_the_safe_target(self, db, session, editor) -> None:
        cands = CandidateStore(db)
        a = cands.create(session, niche="grief journals for men")
        b = cands.create(session, niche="grief journals for women")
        out = editor.apply(
            session, "update_candidate", {"candidate_id": b.id, "status": "verified"}
        )
        assert out["target"]["candidate_id"] == b.id
        assert cands.get(session, a.id).status == "discovered"
        assert cands.get(session, b.id).status == "verified"

    def test_an_ambiguous_niche_is_refused_with_the_ids(self, db, session, editor) -> None:
        cands = CandidateStore(db)
        cands.create(session, niche="grief journals for men")
        cands.create(session, niche="grief journals for women")
        with pytest.raises(RecordEditError, match="candidate_id"):
            editor.apply(session, "delete_candidate", {"niche": "grief journals"})

    def test_a_target_with_no_fields_is_refused(self, db, session, editor) -> None:
        cand = CandidateStore(db).create(session, niche="grief journals")
        with pytest.raises(RecordEditError, match="at least one field"):
            editor.apply(session, "update_candidate", {"candidate_id": cand.id})

    def test_deleting_a_candidate_takes_its_children(self, db, session, editor) -> None:
        cands = CandidateStore(db)
        opps = OpportunityStore(db)
        claims = ClaimStore(db)
        cand = cands.create(session, niche="grief journals")
        VerificationStore(db).create(session, candidate_id=cand.id)
        opps.create(session, candidate_id=cand.id, title="Grief Journal", niche="grief")
        claim = claims.create(
            session, kind="other", statement="measured demand", candidate_id=cand.id
        )

        out = editor.apply(session, "delete_candidate", {"candidate_id": cand.id})

        assert out["removed"] == {"verifications": 1, "opportunities": 1}
        assert cands.get(session, cand.id) is None
        assert opps.list(session) == []
        # A claim is about evidence, not about this line of research: it stays.
        survivor = claims.get(session, claim.id)
        assert survivor is not None and survivor.candidate_id is None


class TestOpportunities:
    def test_the_model_edits_and_removes_an_opportunity(self, db, session, editor) -> None:
        cands = CandidateStore(db)
        opps = OpportunityStore(db)
        cand = cands.create(session, niche="grief journals")
        opp = opps.create(
            session, candidate_id=cand.id, title="Grief Journal", niche="grief journals",
            angle="old angle", keywords=["grief"],
        )
        out = editor.apply(
            session, "update_opportunity",
            {
                "opportunity_id": opp.id,
                "title": "The Organised Grief Journal",
                "angle": "estate checklist inside",
                "keywords": ["grief journal", "estate checklist"],
                "confidence": 0.71,
            },
        )
        assert out["ok"] is True
        row = opps.get(session, opp.id)
        assert row.title == "The Organised Grief Journal"
        assert row.keywords == ["grief journal", "estate checklist"]
        assert row.confidence == 0.71

        removed = editor.apply(
            session, "delete_opportunity", {"title": "The Organised Grief Journal"}
        )
        assert removed["ok"] is True
        assert opps.get(session, opp.id) is None


class TestEvidence:
    def test_a_wrong_total_is_correctable_without_losing_the_rows(self, db, session, editor) -> None:
        store = BrowserEvidenceStore(db)
        rows = [{"asin": f"B0TEST{i:04d}"} for i in range(16)]
        rec = store.create(
            session_id=session, marketplace="us", kind="search_results",
            url="https://www.amazon.com/s?k=grief", title="search: grief",
            data={"query": "grief", "result_count": 16, "results": rows},
        )
        assert search_total(rec.data) is None, "16 rows is not a result total"

        out = editor.apply(
            session, "update_evidence",
            {"evidence_id": rec.id, "total_results": 30000,
             "total_results_are_approximate": True},
        )
        assert out["ok"] is True
        fixed = store.get(rec.id)
        assert fixed.data["result_count"] == 30000
        assert search_total(fixed.data) == 30000
        assert fixed.data["total_results_source"] == "model-correction"
        assert len(fixed.data["results"]) == 16, "the captured rows must survive"

    def test_the_model_can_delete_a_capture_it_judged_wrong(self, db, session, editor) -> None:
        store = BrowserEvidenceStore(db)
        rec = store.create(
            session_id=session, marketplace="us", kind="search_results",
            url="u", title="t", data={"query": "q"},
        )
        assert editor.apply(session, "delete_evidence", {"evidence_id": rec.id})["ok"]
        assert store.get(rec.id) is None
        with pytest.raises(RecordEditError, match="not in this session"):
            editor.apply(session, "delete_evidence", {"evidence_id": rec.id})


class TestClaims:
    def test_a_claim_can_be_rewritten_by_its_wording(self, db, session, editor) -> None:
        claims = ClaimStore(db)
        claim = claims.create(
            session, kind="demand", statement="grief journals have sustained demand"
        )
        out = editor.apply(
            session, "update_claim",
            {"statement": "sustained demand", "kind": "other",
             "interpretation": "narrowed after the uk check"},
        )
        assert out["ok"] is True
        row = claims.get(session, claim.id)
        assert row.kind == "other"
        assert "narrowed" in row.interpretation

    def test_a_claim_with_no_support_can_be_deleted(self, db, session, editor) -> None:
        claims = ClaimStore(db)
        claim = claims.create(session, kind="other", statement="unsupported claim")
        assert editor.apply(session, "delete_claim", {"claim_id": claim.id})["ok"]
        assert claims.get(session, claim.id) is None

    def test_two_matching_claims_are_refused_with_the_ids(self, db, session, editor) -> None:
        claims = ClaimStore(db)
        claims.create(session, kind="other", statement="demand is falling")
        claims.create(session, kind="other", statement="demand is falling fast")
        with pytest.raises(RecordEditError, match="claim_id"):
            editor.apply(session, "delete_claim", {"statement": "demand is falling"})


def test_every_phase_can_edit_records_and_drive_the_browser() -> None:
    """No phase is a read-only phase: both surfaces exist everywhere (and so
    they exist in follow-up runs, which use the same methodology and runner)."""
    from app.methodology import BROWSER_ACTIONS, RECORD_EDIT_ACTIONS, SPECS

    for phase, spec in SPECS.items():
        allowed = set(spec.allowed_actions)
        assert set(RECORD_EDIT_ACTIONS) <= allowed, phase
        assert set(BROWSER_ACTIONS) <= allowed, phase


@pytest.mark.asyncio
async def test_a_live_run_applies_the_edit_and_reports_it_back(db, config, session) -> None:
    """End to end: the model requests an edit during a phase, the runner
    applies it, journals it, and the NEXT prompt tells the model what happened.
    """
    from app.methodology import P1_DISCOVERY
    from tests.test_research_runner import PHASE_OUTPUTS, ScriptedModel, make_runner

    cands = CandidateStore(db)
    seeded = cands.create(session, niche="probe niche", marketplace="us")

    class EditingModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__(PHASE_OUTPUTS)
            self.edited = False

        def _reply_for(self, phase: str, user: str):
            reply = super()._reply_for(phase, user)
            if phase == P1_DISCOVERY and not self.edited:
                self.edited = True
                reply.content["actions"] = [{
                    "action": "update_candidate",
                    "args": {
                        "candidate_id": seeded.id,
                        "status": "rejected",
                        "rationale": "measured: crowded at the top",
                    },
                }]
            return reply

    model = EditingModel()
    await make_runner(db, config, model).run(session)

    row = cands.get(session, seeded.id)
    assert row is not None, "the edit must not delete what it only re-statused"
    assert row.status == "rejected"
    assert "crowded" in row.rationale

    prompts = model.user_prompts[P1_DISCOVERY]
    assert any(
        "WHAT YOUR LAST ACTIONS DID" in p and "updated candidate" in p
        for p in prompts[1:]
    ), "the model was never told its edit landed"
