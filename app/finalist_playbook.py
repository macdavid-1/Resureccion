"""Per-final-candidate playbook — the model WRITES it, the backend PROVES it.

One section of the final report belongs to each final candidate. The backend
(app.report_model._finalist_sections) hands over ONLY what the browser
measured for that candidate:

  - the 10 Kindle Store categories and the 10 Books store categories,
  - up to 20 backend keywords with Amazon's printed competition total, the
    median review count of the top 10 incumbents, and the suggestion-box
    demand score,
  - the marketplace order the evidence supports,
  - verbatim review text and consumer-map findings for the incumbents.

From exactly that, the model writes:

  - 10 premium titles (each tied to a MEASURED keyword it targets),
  - which marketplaces to publish into, in ranking order, and why,
  - which of the two store category systems to target, and why,
  - how to place the keywords,
  - how to beat the incumbent books, move by move, from their reviews.

Grounding is deterministic, not hopeful: every number the model writes must
appear verbatim in that candidate's data (app.report_narrative.ground_text),
and any title whose target keyword does not match a measured phrase for this
candidate is DROPPED rather than kept with an invented justification. A
shortfall is reported honestly; nothing is ever padded.

Every failure degrades to "no prose for this candidate" — the measured
tables stand on their own, and a report never fails because a model did.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.prompts import owner_precedence_block
from app.report_narrative import allowed_numbers, ground_text

PLAYBOOK_FIELDS = (
    "titles",
    "marketplace_plan",
    "category_plan",
    "beating_incumbents",
    "keyword_plan",
)

TITLE_TARGET = 10

# Every prompt this module sends starts with this prefix. Report-writing
# calls are POST-RESEARCH (they run after all phases, from measured data), so
# the research invariants — owner guidance and the live screen attached to
# each call — deliberately do not apply to them.
PLAYBOOK_PROMPT_PREFIX = "CANDIDATE DATA (measured live"

PLAYBOOK_SYSTEM = """You are a senior publisher's market analyst writing the production playbook for ONE book opportunity, for a non-developer author.

You receive CANDIDATE DATA: real, browser-measured research for this one candidate. Every number in it was measured live (Amazon's own printed result totals, top-10 review medians, suggestion-box demand, category breadcrumbs read from product pages, verbatim customer reviews).

Write ONE JSON object with exactly these keys:
  "titles":            an array of EXACTLY 10 objects. Each object:
                         "title":           the title, premium and specific (max ~90 characters). No ALL CAPS, no "#1", "best" or "ultimate" hype, no claims of awards or credentials.
                         "subtitle":        the subtitle that carries the promise and the audience (max ~180 characters).
                         "targets_keyword": copied VERBATIM from one entry in CANDIDATE DATA keywords. No other value is allowed.
                         "why_it_ranks":    1-2 sentences: why this phrasing can rank for that measured keyword (cite the measured competition/review barrier in the data).
                         "why_it_converts": 1-2 sentences: the specific reader it converts and the promise it makes.
                       All 10 must be genuinely different from each other — different angle, audience or promise, and never the same opening words.
  "marketplace_plan":  2-4 markdown paragraphs. Which marketplaces to publish into, IN THE RANKING ORDER GIVEN in CANDIDATE DATA, and why. Say plainly which one or ones are likely to perform very well (it can be more than one), and what each one's evidence is. Do not add a marketplace that is not in the data.
  "category_plan":     2-3 markdown paragraphs. Which of the two store category systems (Kindle Store / Books store) to select into, in which order, and why — using the measured categories and the incumbent review barriers. Explain the difference between the two systems because they are separate catalogues on Amazon.
  "keyword_plan":      2-3 markdown paragraphs. How to actually place these 20 keywords: title and subtitle first, the seven backend keyword slots, the description, and which low-competition phrases to lead with. Name the specific keywords from the data.
  "beating_incumbents": 3-5 markdown paragraphs, or a markdown bullet list, of concrete editorial moves that make THIS book better than the incumbent books. Every move must answer a specific complaint, unmet need, praise-to-preserve or review quote present in CANDIDATE DATA, and must name which finding it answers. Be concrete about content and structure (chapters, worksheets, checklists, tone, length, examples, indexing, accessibility) — never generic advice.

GROUND RULES (non-negotiable):
1. Never invent a number. Every figure must appear verbatim in CANDIDATE DATA. If a figure you want is absent, write "not measured in this session".
2. Never invent review text, quotes, competitor titles, prices, categories, marketplaces or keywords.
3. "targets_keyword" must be an exact copy of a measured keyword from the data. A title with any other value is discarded.
4. Numbers in a TITLE or SUBTITLE are editorial only (for example "101 Facts"). Never put a statistic in a title — no review counts, no result totals, no prices.
5. Plain language for a non-developer. Markdown body text only: no headings, no JSON, no field names, no code identifiers.
6. Reviews are the readers' words, not yours: when you use one, attribute it as a reader observation rather than restating it as a fact about the market.
7. "notes" are the notes YOU wrote earlier in this research run about this candidate or the run as a whole. Treat them as your own memory: they explain dead ends, quirks and judgments the measured rows cannot show. They are annotations, not measurements — never quote a figure that only a note mentions.

Output ONLY the JSON object."""

# The owner outranks these defaults, exactly as in the research phases.
PLAYBOOK_SYSTEM = PLAYBOOK_SYSTEM + "\n\n" + owner_precedence_block(
    notes_field=False
)


# ------------------------------------------------------------------- digest
def _slim_keyword(k: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": k.get("rank"),
        "keyword": k.get("keyword"),
        "measured": bool(k.get("measured")),
        "competition": k.get("competition"),
        "total_results": k.get("total_results"),
        "total_results_are_approximate": k.get("total_results_are_approximate"),
        "results_text": k.get("results_text"),
        "top10_review_median": k.get("top10_review_median"),
        "demand_score": k.get("demand_score"),
        "marketplace": k.get("marketplace"),
    }


def _slim_category(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "rank": r.get("rank"),
        "category": r.get("category"),
        "breadcrumb": r.get("breadcrumb"),
        "asin": r.get("asin"),
    }


def build_playbook_digest(section: dict[str, Any]) -> dict[str, Any]:
    """Exactly the measured data for ONE candidate — nothing else.

    The prompt carries only this, so the model cannot see (and therefore
    cannot cite) a figure that was not measured for this candidate.
    """
    cats = section.get("categories") or {}
    findings = dict(section.get("review_findings") or {})
    reviews = [
        {
            "asin": r.get("asin"),
            "rating": r.get("rating"),
            "title": r.get("title"),
            "body": r.get("body"),
        }
        for r in (findings.pop("reviews", None) or [])
        if isinstance(r, dict)
    ][:12]
    return {
        "candidate": {
            "niche": section.get("niche"),
            "derived_from_marketplace": section.get("marketplace_derived_from"),
            "status": section.get("status"),
            "portfolio_title": section.get("opportunity_title"),
            "positioning": section.get("positioning"),
            "market_gap": section.get("market_gap"),
            "differentiation": section.get("differentiation"),
            "deliverables": {
                "titles_required": TITLE_TARGET,
                "keywords_requested": section.get("keyword_target"),
                "categories_per_store_requested": section.get("category_target"),
                "kindle_store_categories_found": len(cats.get("kindle_store") or []),
                "books_categories_found": len(cats.get("books") or []),
                "keywords_measured": section.get("keywords_measured"),
            },
        },
        "keywords": [
            _slim_keyword(k) for k in (section.get("keywords") or [])
            if isinstance(k, dict)
        ],
        "kindle_store_categories": [
            _slim_category(r) for r in (cats.get("kindle_store") or [])
            if isinstance(r, dict)
        ],
        "books_categories": [
            _slim_category(r) for r in (cats.get("books") or [])
            if isinstance(r, dict)
        ],
        "marketplace_ranking": [
            {
                "rank": m.get("rank"),
                "code": m.get("code"),
                "name": m.get("name"),
                "language": m.get("language"),
                "currency": m.get("currency"),
                "basis": m.get("basis"),
                "signals": m.get("signals"),
            }
            for m in (section.get("marketplace_ranking") or [])
            if isinstance(m, dict)
        ],
        "review_findings": {**findings, "reviews": reviews},
        "incumbents": [
            {
                "title": c.get("title"),
                "marketplace": c.get("marketplace"),
                "price": c.get("price"),
                "review_count": c.get("review_count"),
                "rank": c.get("rank"),
            }
            for c in (section.get("incumbents") or [])
            if isinstance(c, dict)
        ],
        "title_seeds": list(section.get("title_seeds") or []),
        # The model's own notes from the research run that concern this
        # candidate (or the run as a whole).
        "notes": [
            {"phase": n.get("phase"), "text": n.get("text")}
            for n in (section.get("model_notes") or [])
            if isinstance(n, dict) and n.get("text")
        ],
        "research_notes": list(section.get("research_notes") or []),
    }


def digest_json(digest: dict[str, Any]) -> str:
    return json.dumps(digest, ensure_ascii=False, default=str)


# ---------------------------------------------------------------- validation
_NORM_RE = re.compile(r"[^a-z0-9 ]")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _norm(text: Any) -> str:
    flat = _NORM_RE.sub(" ", str(text or "").lower())
    return re.sub(r"\s+", " ", flat).strip()


def _same_phrase(a: Any, b: Any) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) < 8 or len(nb) < 8:
        return False
    return na in nb or nb in na


def _clean_section(value: Any) -> str:
    """Markdown body only: the renderer owns headings."""
    if not isinstance(value, str):
        return ""
    lines = [ln for ln in value.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(lines).strip()


def _title_figures_ok(*texts: str, allowed: set[str]) -> bool:
    """A title may carry an editorial number ("101 Facts"), never a stat.

    A formatted statistic ("1,234 reviews") or any four-digit-plus figure that
    did not come from the measured data is rejected outright, so a fabricated
    sales or review claim can never appear in a title.
    """
    for text in texts:
        for m in _NUM_RE.finditer(str(text or "")):
            token = m.group(0)
            if token.replace(",", "") in allowed:
                continue
            if "," in token:
                return False
            try:
                value = float(token)
            except ValueError:
                return False
            if value >= 1000:
                return False
    return True


def _allowed_targets(section: dict[str, Any]) -> list[str]:
    """The measured phrases a title may point at — nothing else.

    Measured phrases are preferred; when the dive measured none, the mined
    phrases still count (they came from Amazon's own suggestion box). A
    candidate with no keywords at all may only target its own niche.
    """
    keywords = [k for k in (section.get("keywords") or []) if isinstance(k, dict)]
    measured = [str(k.get("keyword")) for k in keywords if k.get("measured")]
    mined = [str(k.get("keyword")) for k in keywords]
    for pool in (measured, mined):
        usable = [k for k in pool if k.strip()]
        if usable:
            return usable
    niche = str(section.get("niche") or "").strip()
    return [niche] if niche else []


def normalize_titles(
    raw: Any, *, section: dict[str, Any], allowed: set[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate the model's titles against THIS candidate's measured data."""
    violations: list[str] = []
    targets = _allowed_targets(section)
    kept: list[dict[str, Any]] = []
    seen_full: set[str] = set()
    seen_openings: set[str] = set()
    items = raw if isinstance(raw, list) else []

    for item in items:
        if len(kept) >= TITLE_TARGET:
            break
        if isinstance(item, str):
            item = {"title": item}
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split()).strip(" -–—:")
        if not title or len(title) > 140:
            continue
        if not _title_figures_ok(title, allowed=allowed):
            violations.append(f"title carried an unmeasured statistic: {title[:60]}")
            continue
        target_raw = str(item.get("targets_keyword") or "").strip()
        target = next((t for t in targets if _same_phrase(t, target_raw)), "")
        if not target:
            violations.append(
                f"title targeted a keyword that was not measured for this "
                f"candidate: {target_raw[:60] or '(none)'}"
            )
            continue
        signature = " ".join(_norm(title).split()[:4])
        norm_full = _norm(title)
        if not norm_full or norm_full in seen_full or signature in seen_openings:
            violations.append(f"title repeated another title: {title[:60]}")
            continue
        subtitle = " ".join(str(item.get("subtitle") or "").split()).strip()
        if len(subtitle) > 240:
            subtitle = subtitle[:237].rstrip() + "…"
        if not _title_figures_ok(subtitle, allowed=allowed):
            violations.append(f"subtitle carried an unmeasured statistic: {title[:60]}")
            continue
        ranks, removed = ground_text(str(item.get("why_it_ranks") or ""), allowed)
        violations += [f"title ranks: {t}" for t in removed]
        converts, removed = ground_text(str(item.get("why_it_converts") or ""), allowed)
        violations += [f"title converts: {t}" for t in removed]
        seen_full.add(norm_full)
        seen_openings.add(signature)
        kept.append({
            "title": title,
            "subtitle": subtitle,
            "targets_keyword": target,
            "why_it_ranks": ranks.strip(),
            "why_it_converts": converts.strip(),
        })
    return kept, violations


def normalize_playbook(
    content: Any, *, allowed: set[str], section: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Validate + ground one model reply into a renderable playbook."""
    if not isinstance(content, dict):
        return {}, ["playbook reply was not an object"]
    violations: list[str] = []
    titles, title_violations = normalize_titles(
        content.get("titles"), section=section, allowed=allowed
    )
    violations += title_violations

    findings = section.get("review_findings") or {}
    has_review_material = bool(
        findings.get("reviews")
        or findings.get("praises")
        or findings.get("complaints")
        or findings.get("unmet")
        or findings.get("design_opportunities")
        or findings.get("readers_want")
    )

    playbook: dict[str, Any] = {}
    for key in (
        "marketplace_plan", "category_plan", "keyword_plan", "beating_incumbents",
    ):
        # A "beat the incumbents" plan with no reader feedback behind it would
        # be generic advice dressed up as research: refuse it.
        if key == "beating_incumbents" and not has_review_material:
            continue
        cleaned = _clean_section(content.get(key))
        grounded, removed = ground_text(cleaned, allowed)
        violations += [f"{key}: {t}" for t in removed]
        if grounded.strip():
            playbook[key] = grounded.strip()
    if titles:
        playbook["titles"] = titles
        if len(titles) < TITLE_TARGET:
            playbook["titles_shortfall"] = True
            violations.append(
                f"only {len(titles)} of {TITLE_TARGET} titles survived grounding"
            )
    if not playbook:
        return {}, violations or ["playbook was empty after grounding"]
    return playbook, violations


# ------------------------------------------------------------------- prompts
def user_prompt(digest: str) -> str:
    return (
        PLAYBOOK_PROMPT_PREFIX
        + " for this one candidate — the ONLY facts you may use):\n"
        + digest
        + "\n\nWrite this candidate's playbook now, as the JSON object "
        "specified. Exactly 10 titles, each targeting a keyword copied "
        "verbatim from the data."
    )


def correction_prompt(digest: str, violations: list[str], section: dict[str, Any]) -> str:
    targets = _allowed_targets(section)
    return (
        user_prompt(digest)
        + "\n\nCORRECTION REQUIRED. Your previous reply broke the grounding "
        "rules: " + "; ".join(dict.fromkeys(violations))[:1200]
        + "\n\nAllowed \"targets_keyword\" values (copy one verbatim per title): "
        + " | ".join(targets[:24])
        + ".\nReturn EXACTLY 10 distinct titles, no invented numbers and no "
        "statistics in titles or subtitles."
    )


__all__ = [
    "PLAYBOOK_FIELDS",
    "PLAYBOOK_PROMPT_PREFIX",
    "PLAYBOOK_SYSTEM",
    "TITLE_TARGET",
    "allowed_numbers",
    "build_playbook_digest",
    "correction_prompt",
    "digest_json",
    "normalize_playbook",
    "normalize_titles",
    "user_prompt",
]
