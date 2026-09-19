"""Tests for the deterministic research methodology."""
from __future__ import annotations

import pytest

from app.methodology import (
    OPPORTUNITY_FIELDS,
    PHASE_ORDER,
    SPECS,
    STALL_LIMIT,
    Methodology,
    PhaseSpec,
    depth_priority_text,
    validate_opportunity,
)


def test_nine_phases_in_order():
    assert len(PHASE_ORDER) == 9
    assert PHASE_ORDER[0] == "opportunity_discovery"
    assert PHASE_ORDER[1] == "aggressive_niching"
    assert PHASE_ORDER[2] == "competitive_landscape"
    assert PHASE_ORDER[3] == "consumer_intelligence"
    assert PHASE_ORDER[4] == "demand_validation"
    assert PHASE_ORDER[5] == "opportunity_gap"
    assert PHASE_ORDER[6] == "cross_market_validation"
    assert PHASE_ORDER[7] == "adversarial_verification"
    assert PHASE_ORDER[8] == "opportunity_synthesis"


def test_every_phase_has_spec_with_contract():
    for phase in PHASE_ORDER:
        spec = SPECS[phase]
        assert isinstance(spec, PhaseSpec)
        assert spec.max_iterations > 0
        assert spec.allowed_actions, phase
        assert spec.output_contract, phase
        assert spec.guidance.strip(), phase


def test_adversarial_phase_requires_verdicts():
    spec = SPECS["adversarial_verification"]
    assert "verdicts" in spec.output_contract
    assert "kdspy_data" in spec.allowed_actions  # KDSpy may be used deliberately


def test_synthesis_only_consumes_verified():
    spec = SPECS["opportunity_synthesis"]
    assert spec.consumes == ("verified",)
    assert "opportunities" in spec.output_contract


def test_depth_priority_text_mentions_integrity_and_speed_last():
    text = depth_priority_text()
    assert "research integrity" in text
    assert "speed" in text


def test_stall_limit_is_small():
    assert STALL_LIMIT == 3


def _valid_opportunity() -> dict:
    return {
        "niche": "grief journals for adult children who lost a parent suddenly",
        "parent_market": "grief and bereavement journals",
        "target_reader": "adults who lost a parent within the last year",
        "reader_problem": "overwhelm and disorganization after sudden loss",
        "marketplaces": ["us", "uk"],
        "evidence_ids": ["ev1", "ev2"],
        "competitive_landscape": "3 incumbents, generic, low review depth",
        "consumer_needs": "structured prompts, legal-checklist pages",
        "market_gap": "no guided legal/estate checklist integration",
        "differentiation": "estate-paperwork companion section",
        "risks": ["narrow audience"],
        "keywords": ["grief journal for loss of parent"],
        "title_concepts": ["When a Parent Is Suddenly Gone"],
        "positioning": "the organized grief journal",
        "confidence": 0.72,
        "verification_status": "pass",
    }


def test_validate_opportunity_accepts_complete():
    ok, problems = validate_opportunity(_valid_opportunity())
    assert ok, problems


def test_validate_opportunity_rejects_missing_fields():
    obj = _valid_opportunity()
    del obj["market_gap"]
    del obj["confidence"]
    ok, problems = validate_opportunity(obj)
    assert not ok
    assert any("market_gap" in p for p in problems)
    assert any("confidence" in p for p in problems)


def test_validate_opportunity_rejects_bad_confidence_and_marketplaces():
    obj = _valid_opportunity()
    obj["confidence"] = 1.5
    obj["marketplaces"] = []
    ok, problems = validate_opportunity(obj)
    assert not ok
    assert any("confidence" in p for p in problems)
    assert any("marketplaces" in p for p in problems)


def test_opportunity_contract_matches_spec():
    assert "niche" in OPPORTUNITY_FIELDS
    assert "verification_status" in OPPORTUNITY_FIELDS


def test_methodology_facade_describe(config):
    m = Methodology(config)
    assert m.phases == PHASE_ORDER
    d = m.describe()
    assert len(d["phases"]) == 9
    assert d["priority_order"][0] == "research integrity"
    assert d["priority_order"][-1] == "speed"
    first = d["phases"][0]
    assert first["key"] == "opportunity_discovery"
    assert "allowed_actions" in first
    assert m.session_phase("opportunity_discovery") == "discovery"
    with pytest.raises(KeyError):
        m.spec("nope")


def test_session_phase_mapping_covers_all_phases():
    for phase in PHASE_ORDER:
        mapped = Methodology().session_phase(phase)
        assert mapped in ("discovery", "exploration", "verification", "synthesis")
