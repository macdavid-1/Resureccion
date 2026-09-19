"""KDP risk screening: policy, category, and account-health checks.

KDP publishing has real content-policy, category-placement, and account-health
risks. Phase 8's methodology explicitly asks whether a candidate is a risky
KDP category. This module screens candidates and their metadata (keywords,
titles) deterministically against a named, documented rule set — not vibes:

Rules (each produces a flag; blocking flags reject the candidate):
  - public_domain       claiming/repackaging public-domain content is policy-risky
  - trademarked_terms   niche/title built around trademarked brand names
  - medical_legal_fin   medical/legal/financial advice positioning needs
                        credible-author positioning; uncited advice claims
                        in low-authority niches are account-health risks
  - thin_content        mass-produced low-content pattern with no differentiation
  - ai_content_misrep   positioning that hides AI-generated content (policy)
  - adult_minors        content sexualizing minors — absolute reject
  - misleading_claims   guaranteed-outcome promises ("cure", "get rich")

Concern levels (owner-facing, deliberately NOT legal certainty):
    safe          no flags
    concern       one non-blocking flag — allowed with explanation in the report
    high_concern  multiple non-blocking flags, or blocking flags caused only by
                  optional metadata (not the niche itself)
    reject        blocking flags on the niche/positioning itself

Every screening outcome is persisted as a `kdp_risk` integrity assessment so
the owner can audit exactly why a candidate was rejected or allowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.integrity import KIND_KDP_RISK, KIND_KEYWORD_INTEGRITY, IntegrityAssessmentStore

# --------------------------------------------------------------------------- #
# Rule tables (documented, deterministic; extend deliberately)
# --------------------------------------------------------------------------- #

# Trademark-shaped tokens that must not carry a niche/title. Conservative:
# matches whole words only. Extensible via KDP_RISK_EXTRA_TERMS (comma-sep).
_TRADEMARK_TERMS = {
    "kindle", "amazon", "kdp", "google", "apple", "disney", "pixar",
    "marvel", "star wars", "harry potter", "minecraft", "roblox",
    "lego", "cocomelon", "peppa pig", "nasa", "olympics",
}

# Guaranteed-outcome promise patterns (misleading claims policy).
_PROMISE_PATTERNS = (
    r"\bcure\b", r"\bcures\b", r"\bmiracle\b", r"\bguaranteed\b",
    r"\bget rich\b", r"\blose \d+\s?(lbs|pounds|kg)\b", r"\b100% effective\b",
)

# Advice-domain markers that demand credible-author positioning.
_ADVICE_DOMAINS = (
    ("medical", (r"\bcancer\b", r"\bdiabet\w*\b", r"\bautism\b", r"\bdepression\b", r"\banxiety\b",
                 r"\bpsoriasis\b", r"\bmenopause\b", r"\bthyroid\b")),
    ("legal", (r"\blawsuit\b", r"\bcustody\b", r"\bdivorce\b", r"\bbankruptcy\b", r"\bimmigration\b")),
    ("financial", (r"\bstock picking\b", r"\bday trading\b", r"\bcrypto\b", r"\bforex\b", r"\btax advice\b")),
)

# Absolute reject.
_MINOR_SAFETY = (r"\b(erotic|sexy|sexual)\b.{0,24}\b(minors?|teens?|child(?:ren)?|kids?|toddlers?|preteens?)\b",
                 r"\b(minors?|teens?|child(?:ren)?|kids?|toddlers?|preteens?)\b.{0,24}\b(erotic|sexy|sexual)\b")

_PUBLIC_DOMAIN = (r"\bpublic domain\b", r"\bout of copyright\b", r"\broald dahl\b", r"\bsherlock holmes\b")

_THIN_CONTENT = (r"\blow[- ]content\b", r"\bno[- ]content\b", r"\bblank (journal|notebook)\b",
                 r"\bpassword (book|log)\b")

_AI_MISREP = (r"\b(AI|chatgpt|gpt)\b.{0,30}\b(passive income|money|profit)s?\b",)


# Terms that are fine in a book's interior/topic but must NEVER carry a
# keyword or title (metadata-level trademark risk — different rule, different
# severity than niche-level use).
_KEYWORD_TRADEMARK_TERMS = _TRADEMARK_TERMS

# Keyword shapes that deceive buyers: mismatched intent, bait, platform abuse.
_KEYWORD_DECEPTIVE_PATTERNS = (
    (r"\bfree\b", "'free' promises in metadata mislead buyers"),
    (r"\bbestseller\b", "self-referential 'bestseller' keyword is misleading metadata"),
    (r"\b#1\b", "rank-bait keyword is misleading metadata"),
    (r"\bpdf\b|\bebook download\b|\bfull book\b", "format-bait keyword implies pirated-content intent"),
    (r"\blike \w+ journal\b|\balternative to\b", "competitor-bait keyword rides another product's brand"),
)


@dataclass
class KdpRiskScreening:
    flags: list[str] = field(default_factory=list)       # e.g. ["trademarked_terms:marvel"]
    blocking: list[str] = field(default_factory=list)    # subset that must reject
    warnings: list[str] = field(default_factory=list)    # advisory text

    @property
    def clean(self) -> bool:
        return not self.flags

    @property
    def level(self) -> str:
        """The four-level owner-facing concern classification."""
        if not self.flags:
            return "safe"
        if self.blocking:
            return "reject"
        if len(self.flags) >= 2:
            return "high_concern"
        return "concern"
    def to_dict(self) -> dict[str, Any]:
        return {
            "flags": self.flags,
            "blocking": self.blocking,
            "warnings": self.warnings,
            "level": self.level,
        }


def _norm(candidate: Any) -> str:
    parts = [
        str(getattr(candidate, "niche", "") or ""),
        str(getattr(candidate, "rationale", "") or ""),
    ]
    meta = getattr(candidate, "meta", {}) or {}
    if isinstance(meta, dict):
        parts.append(str(meta.get("parent_market") or ""))
        chain = meta.get("chain")
        if isinstance(chain, list):
            parts.append(" ".join(str(c) for c in chain))
    return " ".join(parts).lower()


def _evidence_text(evidence_records: list[dict[str, Any]]) -> str:
    out = []
    for e in evidence_records or []:
        out.append(str(e.get("title") or ""))
        out.append(str(e.get("url") or ""))
    return " ".join(out).lower()


def screen_candidate(
    candidate: Any,
    evidence_records: list[dict[str, Any]] | None = None,
) -> KdpRiskScreening:
    """Screen one candidate's niche/rationale/meta against the rule set."""
    text = _norm(candidate)
    result = KdpRiskScreening()

    if any(re.search(p, text) for p in _MINOR_SAFETY):
        result.flags.append("adult_minors")
        result.blocking.append("adult_minors")
        result.warnings.append("content may sexualize minors — absolute reject per content policy")

    for term in _TRADEMARK_TERMS:
        if re.search(rf"\b{re.escape(term)}\b", text):
            result.flags.append(f"trademarked_terms:{term}")
            result.blocking.append(f"trademarked_terms:{term}")
            result.warnings.append(f"niche/title built around trademarked term {term!r}")

    for domain, patterns in _ADVICE_DOMAINS:
        hits = [p for p in patterns if re.search(p, text)]
        if hits:
            # Only blocking when there's no credible-author signal anywhere.
            author_ok = re.search(
                r"\b(md|dr\.?|doctor|phd|rn|rd|cpa|attorney|lawyer|clinician|therapist)\b", text
            )
            flag = f"medical_legal_fin:{domain}"
            result.flags.append(flag)
            if author_ok:
                result.warnings.append(f"{domain}-adjacent niche; credible-author signal present")
            else:
                result.blocking.append(flag)
                result.warnings.append(
                    f"{domain}-adjacent niche without credible-author positioning — "
                    "needs an author-credential angle or must be rejected"
                )

    if any(re.search(p, text) for p in _PROMISE_PATTERNS):
        result.flags.append("misleading_claims")
        result.blocking.append("misleading_claims")
        result.warnings.append("guaranteed-outcome promise language violates KDP metadata policy")

    if any(re.search(p, text) for p in _THIN_CONTENT):
        result.flags.append("thin_content")
        # Blocking only when there is no differentiation story at all.
        if not re.search(r"\b(differenti\w*|unique|angle|specific|structure|guided|system)\b", text):
            result.blocking.append("thin_content")
            result.warnings.append("mass-produced low-content pattern without differentiation")
        else:
            result.warnings.append("low-content-adjacent niche; differentiation present — allowed with note")

    if any(re.search(p, text) for p in _AI_MISREP):
        result.flags.append("ai_content_misrep")
        result.warnings.append("positioning markets AI-generated content as income; disclose per policy")

    if any(re.search(p, text) for p in _PUBLIC_DOMAIN):
        result.flags.append("public_domain")
        result.warnings.append("public-domain repackaging flagged; verify rights status before recommending")

    # Deduplicate while preserving order.
    result.flags = list(dict.fromkeys(result.flags))
    result.blocking = list(dict.fromkeys(result.blocking))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def screen_keywords(
    keywords: list[str],
    *,
    niche: str = "",
) -> KdpRiskScreening:
    """Metadata/keyword integrity screen.

    Keywords are rejected as recommendations when they are: trademark-risky,
    deceptive/bait-shaped, unrelated to the niche, or promise-shaped. The
    objective is high-intent, relevant discoverability — not manipulation.
    Each problem is recorded with a concrete reason the report can quote.
    """
    result = KdpRiskScreening()

    def _words(text: str) -> set[str]:
        # Light plural normalization so 'adults' matches 'adult' and
        # 'journals' matches 'journal' — relatedness, not exact spelling.
        return {w[:-1] if w.endswith("s") and len(w) > 3 else w
                for w in re.findall(r"[a-z0-9]+", (text or "").lower())}

    niche_words = _words(niche)
    for kw_raw in keywords:
        kw = str(kw_raw or "").strip().lower()
        if not kw:
            continue
        for term in _KEYWORD_TRADEMARK_TERMS:
            if re.search(rf"\b{re.escape(term)}\b", kw):
                result.flags.append(f"keyword_trademark:{kw}")
                result.blocking.append(f"keyword_trademark:{kw}")
                result.warnings.append(
                    f"keyword {kw!r} uses trademarked term {term!r} — must not be "
                    "used in metadata even if search volume is high"
                )
        for pattern, reason in _KEYWORD_DECEPTIVE_PATTERNS:
            if re.search(pattern, kw):
                result.flags.append(f"keyword_deceptive:{kw}")
                result.blocking.append(f"keyword_deceptive:{kw}")
                result.warnings.append(f"keyword {kw!r}: {reason}")
        # Unrelated keyword: shares no content word with the niche. Only
        # judged when we have a niche to compare against.
        kw_words = _words(kw)
        kw_words -= {"for", "with", "and", "the", "a", "an", "of", "in", "to", "book", "journal", "notebook", "planner"}
        if niche_words and kw_words and not (kw_words & niche_words):
            result.flags.append(f"keyword_unrelated:{kw}")
            result.warnings.append(
                f"keyword {kw!r} shares no term with the niche {niche!r} — unrelated "
                "metadata creates poor buyer expectations"
            )
    result.flags = list(dict.fromkeys(result.flags))
    result.blocking = list(dict.fromkeys(result.blocking))
    result.warnings = list(dict.fromkeys(result.warnings))
    return result


def record_screening(
    session_id: str,
    candidate_id: str,
    screening: KdpRiskScreening,
    assessments: IntegrityAssessmentStore,
) -> None:
    """Persist the screening as a durable kdp_risk assessment."""
    outcome = "fail" if screening.blocking else ("warn" if screening.flags else "pass")
    assessments.record(
        session_id,
        kind=KIND_KDP_RISK,
        subject_type="candidate",
        subject_id=candidate_id,
        stage="KdpRiskScreening",
        outcome=outcome,
        reason=(
            "no KDP risk flags"
            if outcome == "pass"
            else "; ".join(screening.warnings)
        ),
        metrics=screening.to_dict(),
    )


def record_keyword_screening(
    session_id: str,
    candidate_id: str,
    screening: KdpRiskScreening,
    assessments: IntegrityAssessmentStore,
) -> None:
    """Persist a keyword-integrity screening as a durable assessment."""
    outcome = "fail" if screening.blocking else ("warn" if screening.flags else "pass")
    assessments.record(
        session_id,
        kind=KIND_KEYWORD_INTEGRITY,
        subject_type="candidate",
        subject_id=candidate_id,
        stage="KeywordIntegrityScreen",
        outcome=outcome,
        reason=(
            "all keywords relevant, non-trademark, non-deceptive"
            if outcome == "pass"
            else "; ".join(screening.warnings[:6])
        ),
        metrics=screening.to_dict(),
    )
