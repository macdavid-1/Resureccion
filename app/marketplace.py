"""Marketplace abstraction.

Research never hardcodes a single Amazon domain. A `Marketplace` describes a
retail marketplace (domain, currency, language, region) and a session's
marketplace plan is either:

- **explicit**: the owner listed marketplaces in the session request — the
  agent must obey exactly, or
- **auto**: no restriction, so the research methodology may select ~4-5
  marketplaces via `select_auto` based on research relevance (language of the
  prompt, region hints, default spread across major English + EU marketplaces).

All Amazon domains supported by KDSpy-style research are cataloged here.
"""
from __future__ import annotations

from dataclasses import dataclass


class MarketplaceError(Exception):
    pass


@dataclass(frozen=True)
class Marketplace:
    code: str          # short code, e.g. "us"
    name: str          # human name, e.g. "Amazon US"
    domain: str        # bare domain, e.g. "amazon.com"
    base_url: str      # https://domain
    currency: str
    language: str
    region: str


AMAZON_MARKETPLACES: dict[str, Marketplace] = {
    m.code: m
    for m in (
        Marketplace("us", "Amazon US", "amazon.com", "https://www.amazon.com", "USD", "en-US", "NA"),
        Marketplace("uk", "Amazon UK", "amazon.co.uk", "https://www.amazon.co.uk", "GBP", "en-GB", "EU"),
        Marketplace("ca", "Amazon Canada", "amazon.ca", "https://www.amazon.ca", "CAD", "en-CA", "NA"),
        Marketplace("au", "Amazon Australia", "amazon.com.au", "https://www.amazon.com.au", "AUD", "en-AU", "APAC"),
        Marketplace("de", "Amazon Germany", "amazon.de", "https://www.amazon.de", "EUR", "de-DE", "EU"),
        Marketplace("fr", "Amazon France", "amazon.fr", "https://www.amazon.fr", "EUR", "fr-FR", "EU"),
        Marketplace("it", "Amazon Italy", "amazon.it", "https://www.amazon.it", "EUR", "it-IT", "EU"),
        Marketplace("es", "Amazon Spain", "amazon.es", "https://www.amazon.es", "EUR", "es-ES", "EU"),
        Marketplace("jp", "Amazon Japan", "amazon.co.jp", "https://www.amazon.co.jp", "JPY", "ja-JP", "APAC"),
        Marketplace("in", "Amazon India", "amazon.in", "https://www.amazon.in", "INR", "en-IN", "APAC"),
        Marketplace("mx", "Amazon Mexico", "amazon.com.mx", "https://www.amazon.com.mx", "MXN", "es-MX", "LATAM"),
        Marketplace("br", "Amazon Brazil", "amazon.com.br", "https://www.amazon.com.br", "BRL", "pt-BR", "LATAM"),
        Marketplace("nl", "Amazon Netherlands", "amazon.nl", "https://www.amazon.nl", "EUR", "nl-NL", "EU"),
        Marketplace("se", "Amazon Sweden", "amazon.se", "https://www.amazon.se", "SEK", "sv-SE", "EU"),
        Marketplace("pl", "Amazon Poland", "amazon.pl", "https://www.amazon.pl", "PLN", "pl-PL", "EU"),
        Marketplace("sg", "Amazon Singapore", "amazon.sg", "https://www.amazon.sg", "SGD", "en-SG", "APAC"),
        Marketplace("ae", "Amazon UAE", "amazon.ae", "https://www.amazon.ae", "AED", "en-AE", "MEA"),
        Marketplace("tr", "Amazon Turkey", "amazon.com.tr", "https://www.amazon.com.tr", "TRY", "tr-TR", "MEA"),
    )
}


def get_marketplace(code_or_domain: str) -> Marketplace:
    """Look up a marketplace by code ('us') or domain ('amazon.com')."""
    key = (code_or_domain or "").strip().lower()
    if not key:
        raise MarketplaceError("empty marketplace identifier")
    m = AMAZON_MARKETPLACES.get(key)
    if m is not None:
        return m
    for m in AMAZON_MARKETPLACES.values():
        if m.domain == key or m.domain.removeprefix("www.") == key.removeprefix("www.") or m.domain.replace(".", "-") == key:
            return m
    raise MarketplaceError(f"unknown marketplace {code_or_domain!r}")


def resolve_explicit(identifiers: list[str]) -> list[Marketplace]:
    """Resolve an owner-specified list. Raises on any unknown entry."""
    if not identifiers:
        raise MarketplaceError("explicit marketplace list is empty")
    out: list[Marketplace] = []
    seen: set[str] = set()
    for ident in identifiers:
        m = get_marketplace(ident)
        if m.code not in seen:
            out.append(m)
            seen.add(m.code)
    return out


# Language keyword hints for auto-selection: words in the research prompt that
# suggest relevance of a non-English marketplace.
_LANGUAGE_HINTS: dict[str, tuple[str, ...]] = {
    "de": ("german", "germany", "deutsch", "deutschland", "berlin", "bavaria"),
    "fr": ("french", "france", "français", "paris"),
    "it": ("italian", "italy", "italiano", "rome"),
    "es": ("spanish", "spain", "español", "madrid", "mexico", "mexican", "latino"),
    "jp": ("japanese", "japan", "tokyo", "anime", "manga"),
    "in": ("india", "indian", "hindi", "bollywood", "cricket"),
    "pt": ("brazil", "brazilian", "portuguese", "portugal"),
    "nl": ("dutch", "netherlands", "holland"),
    "sv": ("swedish", "sweden", "nordic", "scandinavian"),
    "pl": ("polish", "poland"),
    "tr": ("turkish", "turkey"),
    "ar": ("arabic", "uae", "dubai", "middle east"),
}


def select_auto(prompt: str, objective: str = "", max_count: int = 5) -> list[Marketplace]:
    """Auto-select ~4-5 marketplaces based on research relevance.

    Policy:
    - Always include Amazon US (largest KDP market).
    - Always include Amazon UK (second-largest English market).
    - Add Canada + Australia (Anglosphere spread) when count allows.
    - Add marketplaces whose language hints appear in the prompt/objective.
    - Fill remaining slots with the highest-priority remaining catalog entries.
    """
    text = f"{prompt} {objective}".lower()
    picked: list[str] = []

    # 1. Language/region hints from the prompt.
    for lang, hints in _LANGUAGE_HINTS.items():
        if any(h in text for h in hints):
            code = {"ar": "ae"}.get(lang, lang)
            if code in AMAZON_MARKETPLACES and code not in picked:
                picked.append(code)

    # 2. Always-relevant core marketplaces.
    for code in ("us", "uk", "ca", "au"):
        if code not in picked:
            picked.append(code)

    # 3. Fill to target with remaining priority entries.
    target = max(4, min(max_count, 5))
    for code in AMAZON_MARKETPLACES:
        if len(picked) >= target:
            break
        if code not in picked:
            picked.append(code)

    return [AMAZON_MARKETPLACES[c] for c in picked[:target]]


def resolve_plan(identifiers: list[str] | None) -> tuple[str, list[Marketplace]]:
    """Resolve a session's marketplace plan.

    Returns (mode, marketplaces) where mode is 'explicit' or 'auto'.
    Explicit lists are obeyed exactly. Empty/None means auto-selection.
    """
    if identifiers:
        return "explicit", resolve_explicit(identifiers)
    return "auto", select_auto("")


def marketplace_from_url(url: str) -> Marketplace | None:
    """Best-effort detection of the marketplace a URL belongs to."""
    lowered = (url or "").lower()
    for m in AMAZON_MARKETPLACES.values():
        if m.domain in lowered:
            return m
    return None
