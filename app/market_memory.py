"""Cross-session market memory — one owner-scoped knowledge base of measured
market facts, so later runs stop paying twice for the same numbers.

What is remembered (metadata about markets only — never raw pages, never
credentials, everything passes through `redact()` before it is stored):

- `verified_niche`     — a niche that survived the integrity pipeline, with
                          its whitespace score and evidence ids.
- `near_miss`          — candidates that were downgraded or left unresolved:
                          the "almost" pile where the next run should look.
- `keyword_measurement`— a keyword search actually MEASURED on a marketplace
                          (result count + top-10 review barrier + timestamp).
- `competitor`         — an ASIN observed in a niche with its review depth.
- `category_stat`      — census aggregates for one browse node (how strong
                          demand/competition looked there, when measured).

Freshness contract (mirrors the 7-day staleness rule already enforced by
`app.filters`):
- entries younger than `memory_fresh_days` are reused as FACT (no re-paying
  for them),
- older entries are carried as STALE HINTS the model must cheaply re-verify —
  they still save orientation time without ever being presented as current.

Session isolation is preserved: memory rows carry the session they came from,
and the digest handed to a run never mixes in another session's raw state —
only these distilled, owner-scoped facts.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from app.db import Database
from app.redact import redact
from app.timeutil import iso_now, parse_iso, utcnow

# Memory kinds (stable strings — they are the namespace of the table).
KIND_VERIFIED_NICHE = "verified_niche"
KIND_NEAR_MISS = "near_miss"
KIND_KEYWORD = "keyword_measurement"
KIND_COMPETITOR = "competitor"
KIND_CATEGORY = "category_stat"

MEMORY_KINDS: tuple[str, ...] = (
    KIND_VERIFIED_NICHE,
    KIND_NEAR_MISS,
    KIND_KEYWORD,
    KIND_COMPETITOR,
    KIND_CATEGORY,
)

OWNER = "owner"  # single-user product: exactly one memory namespace


def normalize_subject(text: str) -> str:
    """Canonical key for a niche/keyword/asin (case- and space-insensitive)."""
    return " ".join(str(text or "").lower().split())[:200]


@dataclass
class MemoryEntry:
    id: str
    kind: str
    subject: str
    marketplace: str
    payload: dict[str, Any]
    captured_at: str
    source_session_id: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "subject": self.subject,
            "marketplace": self.marketplace,
            "payload": redact(self.payload),
            "captured_at": self.captured_at,
            "source_session_id": self.source_session_id,
            "updated_at": self.updated_at,
        }


class MarketMemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------- write
    def remember(
        self,
        kind: str,
        subject: str,
        payload: dict[str, Any],
        *,
        marketplace: str = "",
        session_id: str = "",
        captured_at: str | None = None,
    ) -> MemoryEntry:
        """Upsert one measured fact (latest measurement wins)."""
        if kind not in MEMORY_KINDS:
            raise ValueError(f"unknown market-memory kind {kind!r}")
        key = normalize_subject(subject)
        if not key:
            raise ValueError("market-memory subject must be non-empty")
        now = iso_now()
        stamp = captured_at or now
        mem_id = uuid.uuid4().hex
        safe_payload = json.dumps(redact(payload or {}))
        with self.db.tx() as conn:
            conn.execute(
                """
                INSERT INTO market_memory
                    (id, owner, kind, subject, marketplace, payload, captured_at,
                     source_session_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner, kind, subject, marketplace) DO UPDATE SET
                    payload = excluded.payload,
                    captured_at = excluded.captured_at,
                    source_session_id = excluded.source_session_id,
                    updated_at = excluded.updated_at
                """,
                (
                    mem_id, OWNER, kind, key, str(marketplace or ""),
                    safe_payload, stamp, str(session_id or ""), now,
                ),
            )
        entry = self.get(kind, key, marketplace=marketplace)
        assert entry is not None
        return entry

    # -------------------------------------------------------------- read
    def get(self, kind: str, subject: str, *, marketplace: str = "") -> MemoryEntry | None:
        with self.db.read() as conn:
            row = conn.execute(
                "SELECT * FROM market_memory WHERE owner = ? AND kind = ? "
                "AND subject = ? AND marketplace = ?",
                (OWNER, kind, normalize_subject(subject), str(marketplace or "")),
            ).fetchone()
        return self._row(row) if row else None

    def list(
        self,
        *,
        kind: str | None = None,
        kinds: Iterable[str] | None = None,
        limit: int = 500,
    ) -> list[MemoryEntry]:
        q = "SELECT * FROM market_memory WHERE owner = ?"
        params: list[Any] = [OWNER]
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        if kinds is not None:
            kinds = list(kinds)
            if not kinds:
                return []
            q += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        q += " ORDER BY captured_at DESC LIMIT ?"
        params.append(limit)
        with self.db.read() as conn:
            rows = conn.execute(q, params).fetchall()
        return [self._row(r) for r in rows]

    def fresh(
        self,
        *,
        kinds: Iterable[str] | None = None,
        max_age_days: int = 7,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        """Entries younger than `max_age_days` — reusable as fact."""
        cutoff = utcnow() - timedelta(days=max(1, int(max_age_days)))
        return [
            e for e in self.list(kinds=kinds, limit=limit)
            if _age_days(e.captured_at) is not None
            and parse_iso(e.captured_at) >= cutoff
        ]

    def stale(
        self,
        *,
        kinds: Iterable[str] | None = None,
        max_age_days: int = 7,
        limit: int = 200,
    ) -> list[MemoryEntry]:
        """Entries older than the freshness window: orientation hints only."""
        cutoff = utcnow() - timedelta(days=max(1, int(max_age_days)))
        return [
            e for e in self.list(kinds=kinds, limit=limit)
            if parse_iso(e.captured_at) < cutoff
        ]

    def count(self, *, kind: str | None = None) -> int:
        q = "SELECT COUNT(*) AS n FROM market_memory WHERE owner = ?"
        params: list[Any] = [OWNER]
        if kind is not None:
            q += " AND kind = ?"
            params.append(kind)
        with self.db.read() as conn:
            row = conn.execute(q, params).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ digest
    def digest(
        self,
        *,
        fresh_days: int = 7,
        limit: int = 40,
        kinds: Iterable[str] | None = None,
        exclude_session: str = "",
    ) -> list[dict[str, Any]]:
        """Model-facing digest: fresh facts first, stale hints marked.

        Every row carries `fresh: true|false`. The prompt rule is
        deterministic: reuse fresh rows as fact; re-verify stale ones cheaply.
        """
        entries = self.list(kinds=kinds, limit=max(limit, 1) * 3)
        rows: list[dict[str, Any]] = []
        for e in entries:
            if exclude_session and e.source_session_id == exclude_session:
                continue  # never hand a run back its own facts as "memory"
            age = _age_days(e.captured_at)
            if age is None:
                continue
            row = {
                "kind": e.kind,
                "subject": e.subject,
                "marketplace": e.marketplace,
                "captured_at": e.captured_at[:19],
                "age_days": round(age, 1),
                "fresh": age <= max(1, int(fresh_days)),
                "payload": redact(e.payload),
            }
            rows.append(row)
            if len(rows) >= limit:
                break
        return rows

    def _row(self, row: sqlite3.Row) -> MemoryEntry:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            payload = {}
        return MemoryEntry(
            id=row["id"],
            kind=row["kind"],
            subject=row["subject"],
            marketplace=row["marketplace"],
            payload=payload,
            captured_at=row["captured_at"],
            source_session_id=row["source_session_id"],
            updated_at=row["updated_at"],
        )


def _age_days(ts: str) -> float | None:
    try:
        return (utcnow() - parse_iso(ts)).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Harvesting: distill one session's durable state into memory facts.
# ---------------------------------------------------------------------------
def _median(values: list[int]) -> int | None:
    vals = sorted(v for v in values if isinstance(v, int) and v >= 0)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return int(round((vals[mid - 1] + vals[mid]) / 2))


def _top10_median(results: list[Any]) -> int | None:
    counts: list[int] = []
    for r in results[:10]:
        if isinstance(r, dict) and isinstance(r.get("review_count"), int):
            counts.append(int(r["review_count"]))
    return _median(counts)


def harvest_session(
    store: MarketMemoryStore,
    session_id: str,
    *,
    candidates: Any,
    browser_evidence: Any,
    whitespace_by_candidate: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Write this session's measured facts into cross-session memory.

    Deterministic and best-effort: memory is an accelerator, never a
    dependency — a failure here must never affect the research outcome, so
    every section is independently guarded. `whitespace_by_candidate` maps
    candidate id -> the score dict computed by app.whitespace (optional).
    """
    ws = whitespace_by_candidate or {}
    written = {k: 0 for k in MEMORY_KINDS}

    # 1) Candidates: verified niches and near-misses (never rejections —
    #    those are final verdicts about THIS session's focus).
    try:
        for c in candidates.list(session_id, limit=500):
            if c.status == "rejected":
                continue
            score = ws.get(c.id) or {}
            payload = {
                "niche": c.niche,
                "marketplace": c.marketplace,
                "status": c.status,
                "whitespace": score.get("score"),
                "rationale": (c.rationale or "")[:300],
                "meta": {
                    k: (c.meta or {}).get(k)
                    for k in ("parent_market", "via")
                    if (c.meta or {}).get(k)
                },
            }
            kind = KIND_VERIFIED_NICHE if c.status == "verified" else KIND_NEAR_MISS
            store.remember(
                kind,
                c.niche,
                payload,
                marketplace=c.marketplace,
                session_id=session_id,
            )
            written[kind] += 1
    except Exception:
        pass

    # 2-4) Measured facts from captured evidence. Timestamps come from the
    #      evidence itself: freshness must reflect WHEN the number was
    #      measured, not when the harvest ran.
    try:
        rows = browser_evidence.list(session_id, limit=5000)
    except Exception:
        rows = []

    for e in rows:
        try:
            data = e.data if isinstance(e.data, dict) else {}
            if e.kind == "search_results":
                query = str(data.get("query") or "").strip()
                if not query:
                    continue
                store.remember(
                    KIND_KEYWORD,
                    query,
                    {
                        "query": query,
                        "result_count": data.get("result_count"),
                        "top10_review_median": _top10_median(
                            [r for r in (data.get("results") or []) if isinstance(r, dict)]
                        ),
                        "marketplace": e.marketplace,
                    },
                    marketplace=e.marketplace,
                    session_id=session_id,
                    captured_at=e.captured_at,
                )
                written[KIND_KEYWORD] += 1
                for r in (data.get("results") or [])[:20]:
                    if not isinstance(r, dict):
                        continue
                    asin = str(r.get("asin") or "").strip()
                    if not asin:
                        continue
                    store.remember(
                        KIND_COMPETITOR,
                        asin,
                        {
                            "asin": asin,
                            "title": str(r.get("title") or "")[:200],
                            "review_count": r.get("review_count"),
                            "rating": r.get("rating"),
                        },
                        marketplace=e.marketplace,
                        session_id=session_id,
                        captured_at=e.captured_at,
                    )
                    written[KIND_COMPETITOR] += 1
            elif e.kind == "product_page":
                asin = str(data.get("asin") or "").strip()
                if asin:
                    store.remember(
                        KIND_COMPETITOR,
                        asin,
                        {
                            "asin": asin,
                            "title": str(data.get("title") or "")[:200],
                            "review_count": data.get("review_count"),
                            "best_seller_rank": data.get("best_seller_rank"),
                            "category_path": data.get("category_path"),
                        },
                        marketplace=e.marketplace,
                        session_id=session_id,
                        captured_at=e.captured_at,
                    )
                    written[KIND_COMPETITOR] += 1
            elif e.kind == "census":
                category = str(data.get("category") or "").strip()
                if not category:
                    continue
                store.remember(
                    KIND_CATEGORY,
                    f"{category} @ {e.marketplace}",
                    {
                        "category": category,
                        "node_id": data.get("node_id"),
                        "items": data.get("item_count"),
                        "top10_review_median": data.get("top10_review_median"),
                        "priced_items": data.get("priced_items"),
                        "marketplace": e.marketplace,
                    },
                    marketplace=e.marketplace,
                    session_id=session_id,
                    captured_at=e.captured_at,
                )
                written[KIND_CATEGORY] += 1
        except Exception:
            continue  # one bad row must never abort the harvest
    return written
