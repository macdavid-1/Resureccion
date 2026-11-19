"""Amazon's completion stream: the one call the MODEL uses to find phrases.

Amazon's search box completes with what buyers actually type. The backend no
longer mines that stream on its own — keyword and niche discovery is the
model's work, done through the search box (`autocomplete` action, typing and
reading the suggestion list, KDSpy's own keyword panel). What survives here is
the plumbing that makes that possible and the screening that keeps a phrase
from being mistaken for demand:

    SUGGEST_JS / ALIAS / mid_for   the same-origin completion call
    screen_phrase / filter_demand_terms   root- and drift-screening
    phrase_score / topical_tokens / seeds_from_text   ranking helpers

Those helpers are deterministic utilities used by the deep dive and by the
keyword screener; nothing here queries Amazon unless the model asks it to.
"""
from __future__ import annotations

from collections.abc import Iterable

# Amazon's books alias (the KDP-relevant completion stream).
ALIAS = "stripbooks"

# Marketplace ids the completion API needs. Amazon RETIRED the legacy
# `/suggestion` path — it now answers 404 on amazon.com while the storefront's
# own search box calls `/api/2017/suggestions` — so the legacy path is only a
# fallback for a marketplace that still serves it. Without this, autocomplete
# (discovery AND the finalists' backend keywords) silently mined NOTHING.
MARKETPLACE_IDS: dict[str, str] = {
    "amazon.com": "ATVPDKIKX0DER",
    "amazon.co.uk": "A1F83G8C2ARO7P",
    "amazon.de": "A1PA6795UKMFR9",
    "amazon.fr": "A13V1IB3VIYZZH",
    "amazon.it": "APJ6JRA9NG5V4",
    "amazon.es": "A1RKKUPIHCS9HS",
    "amazon.ca": "A2EUQ1WTGCTBG2",
    "amazon.com.au": "A39IBJ37TRP1C6",
    "amazon.co.jp": "A1VC38T7YXB528",
    "amazon.in": "A21TJRUUN4KGV",
    "amazon.com.mx": "A1AM78C64UM0Y8",
    "amazon.com.br": "A2Q3Y263D00KWC",
    "amazon.nl": "A1805IZSGTT6HS",
    "amazon.se": "A2NODRKZP88ZB9",
    "amazon.pl": "A1C3SOZRARQ6R3",
    "amazon.com.tr": "A33AVAJ2PDY3EV",
    "amazon.ae": "A2VIGQ35RCS4UG",
    "amazon.sa": "A17E79C6D8DWNP",
    "amazon.sg": "A19VAU5U5O7RUS",
    "amazon.eg": "ARBP9OOSHTCHU",
    "amazon.com.be": "AMEN7PMS3EDWL",
}
DEFAULT_MARKETPLACE_ID = "ATVPDKIKX0DER"


def mid_for(domain: str) -> str:
    """Marketplace id for a domain (US when the marketplace is unknown)."""
    d = str(domain or "").strip().lower().lstrip(".")
    if d in MARKETPLACE_IDS:
        return MARKETPLACE_IDS[d]
    for known, mid in MARKETPLACE_IDS.items():
        if d.endswith("." + known):
            return mid
    return DEFAULT_MARKETPLACE_ID


# Same-origin completion call, executed inside the already-open marketplace
# page (Firefox blocks cross-origin fetch; this is same-domain). The modern
# endpoint is tried FIRST and the retired one second, so a marketplace that
# still serves `/suggestion` keeps working. Both response shapes are read
# (`["", [..]]` legacy, `{"suggestions": [{"value": ..}]}` current).
SUGGEST_JS = """async ([alias, mid, prefix]) => {
  const paths = [];
  if (mid) {
    paths.push('/api/2017/suggestions?limit=11&prefix=' + encodeURIComponent(prefix)
      + '&suggestion-type=WIDGET&alias=' + encodeURIComponent(alias)
      + '&mid=' + encodeURIComponent(mid)
      + '&lop=en_US&site-variant=desktop&client=amazon-search-ui&fb=1&plain-mid=1');
  }
  paths.push('/suggestion?alias=' + encodeURIComponent(alias)
    + '&prefix=' + encodeURIComponent(prefix)
    + (mid ? '&mid=' + encodeURIComponent(mid) : ''));
  for (const path of paths) {
    try {
      const res = await fetch(path, {headers: {'accept': 'application/json'}});
      if (!res.ok) continue;
      const data = await res.json();
      // Every suggestion the endpoint returns, unfiltered: which ones to
      // pursue is the model's judgement, never this script's.
      if (Array.isArray(data) && Array.isArray(data[1])) {
        return data[1].filter((t) => typeof t === 'string');
      }
      if (data && Array.isArray(data.suggestions)) {
        return data.suggestions
          .map((s) => (s && typeof s === 'object' ? s.value : s))
          .filter((t) => typeof t === 'string' && t.trim());
      }
    } catch (e) { /* try the next endpoint */ }
  }
  return [];
}"""

# Backwards-compatible alias for callers that only need the script.
_SUGGEST_JS = SUGGEST_JS

_STOPWORDS = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "how", "i", "in", "is", "it", "its", "of", "on", "or", "our", "that", "the", "their", "to", "was", "we", "what", "when", "which", "who", "will", "with", "you", "your", "about", "into", "over", "under", "best", "new", "free", "book", "books", "kindle", "amazon"]
)

# Phrases that are noise rather than demand language.
_NOISE = (
    "amazon.com", "amazon ", " books by ", "paperback", "hardcover",
    "kindle edition", " AUD", " mp3 ",
)

# Words that name the FORMAT, not the subject. A phrase built only from these
# ("journal for women", "self help book", "kids activity book") is a category
# root, not hidden demand: it competes with the whole store and describes no
# reader. Suggestions are stripped of them before anything is researched, so
# the model starts from specific demand language instead of a pile of roots.
GENERIC_TOKENS = frozenset(
    ["book", "books", "ebook", "ebooks", "workbook", "workbooks", "journal", "journals", "notebook", "notebooks", "planner", "planners", "log", "logs", "diary", "diaries", "guide", "guides", "handbook", "handbooks", "manual", "manuals", "checklist", "checklists", "tracker", "trackers", "worksheet", "worksheets", "coloring", "colouring", "activity", "activities", "gift", "gifts", "idea", "ideas", "best", "top", "new", "free", "amazon", "kindle", "paperback", "hardcover", "audiobook", "audible", "pdf", "epub", "volume", "vol", "edition", "series", "set", "bundle", "box", "kit", "pack", "printable", "downloads", "download", "ebook-only", "self", "help", "how", "to"]
)

# Whole-phrase filler: audience/format with no subject of its own.
TRAILING_FILLER = frozenset(
    ["for", "with", "and", "the", "a", "an", "of", "to", "in", "on", "or", "by", "from", "at", "your", "my", "our", "is", "are"]
)

GENERIC_FILLER: tuple[str, ...] = (
    "for women", "for men", "for kids", "for adults", "for beginners",
    "for teens", "for children", "for seniors", "for girls", "for boys",
    "for dummies", "in 30 days", "step by step", "large print", "all ages",
)


def topical_tokens(text: str, *, min_len: int = 3) -> set[str]:
    """The SUBJECT words of a phrase — format/audience filler removed."""
    out: set[str] = set()
    for raw in str(text or "").lower().replace("-", " ").replace("/", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum())
        if len(w) < min_len or w in _STOPWORDS or w in GENERIC_TOKENS:
            continue
        out.add(w)
    return out


def screen_phrase(phrase: str, topic_tokens: set[str]) -> tuple[bool, str]:
    """Deterministic accept/reject for one demand phrase, with the reason.

    The suggestion stream happily returns roots ("book for women") and drift
    ("therapy worksheets for ptsd" for a grief niche). Researching those wastes
    searches and, worse, presents unwinnable generic terms as validated. This
    is the same screen the finalist dive applies before it measures a phrase,
    so discovery and the report agree on what a usable keyword is.
    """
    import re

    t = " ".join(str(phrase or "").lower().split())
    if not t:
        return False, "empty"
    if any(n.lower() in t for n in _NOISE):
        return False, "noise"
    if re.search(r"https?://|\bwww\.|\d{4,}", t):
        return False, "noise"
    words = t.split()
    if len(words) < 2:
        return False, "too_broad"
    if len(words) > 12 or len(t) > 70:
        return False, "too_abstract"
    # A truncated prefix ("grief journals for") is not demand language; it is
    # what an unfinished query looks like. Only FUNCTION words count here: a
    # phrase may legitimately end in a format word ("grief journal workbook").
    if words[-1] in TRAILING_FILLER:
        return False, "incomplete"
    topical = topical_tokens(t)
    if not topical:
        return False, "too_generic"
    filler = any(f in t for f in GENERIC_FILLER)
    if filler and len(topical) < 2:
        return False, "too_generic"
    if topic_tokens and not (topical & topic_tokens):
        return False, "off_niche"
    return True, ""


def filter_demand_terms(
    terms: Iterable[str],
    seeds: Iterable[str],
    *,
    limit: int = 60,
) -> tuple[list[str], dict[str, int]]:
    """Keep the demand phrases worth researching; count why the rest went.

    Returns (kept, rejected-by-reason). The kept list is what the model sees
    as STARTING POINTS — never as its research boundary.
    """
    topics = set()
    for seed in seeds:
        topics |= topical_tokens(seed)
    kept: list[str] = []
    seen: set[str] = set()
    rejected: dict[str, int] = {}
    for term in terms:
        key = " ".join(str(term or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        ok, reason = screen_phrase(key, topics)
        if not ok:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        kept.append(key)
        if len(kept) >= limit:
            break
    return kept, rejected


def seeds_from_text(text: str, *, limit: int = 6) -> list[str]:
    """Deterministic seed extraction from the owner's brief/objective."""
    words: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").lower().replace("?", " ").replace(",", " ").split():
        w = "".join(ch for ch in raw if ch.isalnum() or ch in ("-", "'"))
        if len(w) < 4 or w in _STOPWORDS or w in seen:
            continue
        seen.add(w)
        words.append(w)
        if len(words) >= limit:
            break
    return words


def phrase_score(term: str, *, frequency: int = 1) -> int:
    """Deterministic 'how promising is this demand phrase' ranking.

    Longer, repeated completions are the strongest signal: a phrase that
    completes out of MANY different prefixes is language buyers really type.
    """
    t = " ".join(str(term).lower().split())
    if not t or any(n.lower() in t for n in _NOISE):
        return -1
    words = len(t.split())
    if words < 2:
        return 0
    score = min(words, 6) * 10 + min(frequency, 20) * 4
    if words >= 3:
        score += 15
    if len(t) > 60:
        score -= 20
    return score
