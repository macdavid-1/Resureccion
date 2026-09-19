"""Tests for research-domain data stores."""
from __future__ import annotations

import pytest

from app.research_data import (
    CandidateStore,
    EvidenceStore,
    ObservationStore,
    OpportunityStore,
    VerificationStore,
)


@pytest.fixture()
def sid(db) -> str:
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO research_sessions (id, name, status, mode, created_at, updated_at, last_activity_at) VALUES ('sess', 'n', 'running', 'auto', '2025-01-01', '2025-01-01', '2025-01-01')"
        )
    return "sess"


def test_observation_roundtrip(db, sid) -> None:
    store = ObservationStore(db)
    obs = store.create(sid, source="amazon", kind="search_page", content="rows...", meta={"page": 1})
    fetched = store.get(sid, obs.id)
    assert fetched is not None
    assert fetched.meta == {"page": 1}
    assert len(store.list(sid)) == 1
    # isolation
    assert store.get("other-session", obs.id) is None


def test_evidence_links_to_observation(db, sid) -> None:
    obs_store = ObservationStore(db)
    ev_store = EvidenceStore(db)
    obs = obs_store.create(sid, source="k.dspy", kind="bsr", content="BSR 12,301")
    ev = ev_store.create(
        sid,
        kind="screenshot",
        uri="artifacts/x.png",
        summary="BSR captured",
        observation_id=obs.id,
        confidence=0.9,
    )
    linked = ev_store.for_observation(sid, obs.id)
    assert len(linked) == 1 and linked[0].id == ev.id


def test_candidate_lifecycle(db, sid) -> None:
    store = CandidateStore(db)
    c = store.create(sid, niche="bouldering log book", score=0.42)
    assert c.status == "discovered"
    c = store.set_status(sid, c.id, "verifying")
    assert c.status == "verifying"
    c = store.update_score(sid, c.id, 0.87)
    assert c.score == 0.87
    c = store.set_status(sid, c.id, "rejected")
    assert c.status == "rejected"
    listed = store.list(sid, status="rejected")
    assert [x.id for x in listed] == [c.id]
    with pytest.raises(ValueError):
        store.set_status(sid, c.id, "bogus")


def test_verification_flow(db, sid) -> None:
    cand = CandidateStore(db)
    vers = VerificationStore(db)
    c = cand.create(sid, niche="meditation trackers")
    v = vers.create(sid, candidate_id=c.id)
    assert v.status == "pending"
    v = vers.complete(
        sid, v.id, status="passed", verdict="demand confirmed", checks={"bsr": 9000}, confidence=0.8
    )
    assert v.status == "passed" and v.checks["bsr"] == 9000
    with pytest.raises(ValueError):
        vers.complete(sid, v.id, status="weird")
    assert len(vers.for_candidate(sid, c.id)) == 1


def test_opportunity_creation(db, sid) -> None:
    cand = CandidateStore(db)
    opps = OpportunityStore(db)
    c = cand.create(sid, niche="kayaking journals")
    o = opps.create(
        sid,
        candidate_id=c.id,
        title="Kayak Journey Log",
        niche="kayaking journals",
        keywords=["kayak log book", "paddle journal"],
        confidence=0.77,
    )
    assert opps.count(sid) == 1
    assert o.keywords == ["kayak log book", "paddle journal"]
