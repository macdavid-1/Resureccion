"""E2E: final intelligence layer — full scripted run, then inspect the
persisted structured model and the rendered market-intelligence report.

Run: timeout 110 .venv/bin/python scripts/report_e2e_check.py
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

from app.config import Config
from app.db import Database
from app.reports import ReportStore
from app.sessions import SessionStore


async def main() -> int:
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="resurrect_report_e2e_"))
    cfg.db_path = cfg.data_dir / "e2e.db"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.connect()

    import tests.test_research_runner as trr

    sessions = SessionStore(db, cfg)
    s = sessions.create(mode="auto", prompt="", objective="find verified KDP gaps")
    runner = trr.make_runner(db, cfg, trr.ScriptedModel(trr.PHASE_OUTPUTS))
    await runner.run(s.id)
    sess = sessions.require(s.id)
    print(f"session: {sess.status} | name: {sess.name!r}")

    store = ReportStore(db, cfg)
    final = [r for r in store.list(s.id) if r.kind == "final"]
    assert final, "no final report"
    rep = final[0]
    body = store.read_markdown(rep)
    model = store.read_model_json(rep)

    print(f"report: {len(body)} chars | model: {len(model and model or {})} sections")
    assert model["schema_version"] == 2

    sections_ok = all(k in model for k in (
        "executive_summary", "opportunities", "consumer_intelligence",
        "competitors", "candidate_ledger", "methodology_trace",
        "evidence_appendix", "quality_statement", "validation",
    ))
    print("all 9 model sections present:", sections_ok)

    ex = model["executive_summary"]
    print(f"exec summary: considered={ex['candidates_considered']} "
          f"rejected={ex['candidates_rejected']} verified={ex['survived_verification']}")

    o = model["opportunities"][0]
    print("\n--- OPPORTUNITY DOSSIER ---")
    for k in ("name", "niche", "parent_market", "target_reader", "market_gap",
              "differentiation", "next_step"):
        print(f"  {k}: {str(o[k])[:80]}")
    print(f"  niching_chain: {' -> '.join(o['niching_chain'])}")
    print(f"  marketplaces: {o['marketplace_analysis']['observed']} | {o['marketplace_analysis']['scope']}")
    print(f"  angles: {len(o['angles'])} | titles: {len(o['titles'])} | core kw: {o['keywords']['core']}")
    print(f"  kdp_risk: {o['kdp_risk']['level']} | verification: {o['verification']['status']}")
    print(f"  evidence trail: {len(o['evidence_ids'])} ids")

    q = model["quality_statement"]
    print(f"\nquality: rejected={q['rejected']} reasons={len(q['rejection_reasons'])} "
          f"uncertain={len(q['remains_uncertain'])}")

    # The report body IS the model's document: the backend no longer writes a
    # section of it, re-renders it, or stamps a status onto it.
    ai = model.get("ai_report") or {}
    ai_body = str(ai.get("markdown") or "")
    print(f"\nmodel-authored report: {len(ai_body)} chars, {ai.get('words')} words, "
          f"{len(ai.get('figures') or [])} figures, {ai.get('prior_runs')} prior runs")
    print("body equals the model's markdown:", body.strip() == ai_body.strip())
    print("backend status stamp present:", any(
        l.startswith("**Report status") for l in body.splitlines()))

    ok = (
        sess.status == "completed"
        and sections_ok
        and ex["survived_verification"] == 1
        and bool(ai_body.strip())
        and body.strip() == ai_body.strip()
        and not any(l.startswith("**Report status") for l in body.splitlines())
        and o["verification"]["status"] == "verified"
    )
    print("\nREPORT E2E RESULT:", "PASS" if ok else "FAIL")
    db.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
