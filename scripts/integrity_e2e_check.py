"""E2E integrity verification: full scripted research run, then inspect the
final report's integrity surface (gates, risk, keyword integrity, evidence).

Run: .venv/bin/python scripts/integrity_e2e_check.py

Uses an isolated temp DATA_DIR; never touches the real data directory.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("OWNER_USERNAME", "owner")

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.integrity import IntegrityAssessmentStore  # noqa: E402
from app.research_data import CandidateStore, OpportunityStore  # noqa: E402
from app.reports import ReportStore  # noqa: E402
from app.sessions import SessionStore  # noqa: E402


def main() -> int:
    cfg = Config()
    cfg.data_dir = Path(tempfile.mkdtemp(prefix="resurrect_e2e_"))
    cfg.db_path = cfg.data_dir / "e2e.db"
    cfg.ensure_dirs()
    db = Database(cfg.db_path)
    db.connect()

    import tests.test_research_runner as trr

    sessions = SessionStore(db, cfg)
    s = sessions.create(
        mode="prompt", prompt="research grief journals for adults", objective="find niches"
    )
    model = trr.ScriptedModel(trr.PHASE_OUTPUTS)
    runner = trr.make_runner(db, cfg, model)
    asyncio.run(runner.run(s.id))

    sess = sessions.require(s.id)
    print("SESSION STATUS:", sess.status, "| error:", sess.error)

    print("\nCANDIDATES:")
    for c in CandidateStore(db).list(s.id):
        print(f"  [{c.status}] {c.niche}")

    opps = OpportunityStore(db).list(s.id)
    print(f"\nOPPORTUNITIES: {len(opps)}")
    for o in opps:
        print(
            f"  {o.title!r} verification={o.meta.get('verification_status')} "
            f"keywords={o.keywords}"
        )

    a = IntegrityAssessmentStore(db)
    print("\nASSESSMENTS BY KIND:")
    for k, n in sorted(Counter(x.kind for x in a.list(s.id, limit=1000)).items()):
        print(f"  {k}: {n}")

    final = next(r for r in ReportStore(db, cfg).list(s.id) if r.kind == "final")
    print("\nREPORT MODE:", final.meta.get("mode"))
    body = ReportStore(db, cfg).read_markdown(final)
    print("\n--- REPORT EXCERPTS ---")
    for line in body.splitlines():
        if any(
            t in line
            for t in (
                "Report status",
                "KDP risk",
                "Keyword integrity",
                "Evidence trail",
                "Verification status",
            )
        ):
            print(" ", line[:170])

    db.close()
    return 0 if sess.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
