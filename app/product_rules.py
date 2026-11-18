"""Hard rules for what this research may target.

Two of them, and they outrank everything else — the model's creativity, a
screenshot's convenience, a market's appeal:

1. **No journals/logbooks.** No journal, logbook, notebook or diary product, in
   any wording (`grief journal`, `5-minute journaling prompts`, `gratitude log
   book`, `daily diary for men`). They are low-content books: the market is
   flooded with near-identical covers, the work is a template rather than a
   book, and a new one competes on price alone.
2. **No generic niches.** A niche must name WHO has WHAT problem in WHICH
   context. `self help`, `books for women`, `mindfulness` and `anxiety workbook
   for teen girls` (audience only, no situation) are markets, not niches: they
   compete with an entire store and give the report nothing to be specific
   about.

These are enforced in two places, on purpose:

- `app.filters` rejects a candidate that slips through (a durable, visible
  verdict with its reason), and
- the runner and the record-edit actions refuse to CREATE such a candidate at
  all, so an hour of research is never spent on a book nobody may write.

Both call the same predicates here, so the rule cannot drift between them.
"""
from __future__ import annotations

import re

# Product types the owner will not publish. Matched as whole words on the
# niche (case-insensitive): "journal", "journals", "journaling", "logbook",
# "log book", "notebook", "diary", "diaries".
BANNED_PRODUCT_RE = re.compile(
    r"\b(?:journals?|journaling|log\s?books?|notebooks?|diar(?:y|ies))\b",
    re.IGNORECASE,
)

# Words that name a broad market rather than a specific reader/context. A niche
# whose remaining content words are ALL drawn from here (plus a generic
# audience) is a market, not a niche.
BROAD_MARKERS: frozenset[str] = frozenset({
    "productivity", "self", "help", "health", "fitness", "diet", "cooking",
    "parenting", "finance", "money", "business", "marketing", "romance",
    "thriller", "fantasy", "fiction", "horror", "history", "biography",
    "meditation", "mindfulness", "hobbies", "crafts", "travel", "spirituality",
    "childrens", "kids", "religion", "education", "psychology", "wellness",
    "cookbook", "poetry", "mystery", "adventure", "science",
})

# Audience words that add no specificity on their own.
GENERIC_AUDIENCE: frozenset[str] = frozenset({
    "women", "woman", "men", "man", "adults", "adult", "kids", "children",
    "teens", "teen", "beginners", "beginner", "everyone", "anyone", "people",
    "girls", "boys", "seniors", "students", "moms", "dads", "parents",
    "families", "couples",
})

# Filler that carries no subject either.
_FILLER: frozenset[str] = frozenset({
    "for", "with", "and", "the", "a", "an", "of", "in", "to", "on", "your",
    "my", "that", "this", "book", "books", "ebook", "ebooks", "kindle",
    "guide", "guides", "best", "top", "new", "how", "about",
})

_WORD_RE = re.compile(r"[a-z0-9']+")


def banned_product_term(text: str) -> str:
    """The banned product word in `text`, or "" when there is none."""
    match = BANNED_PRODUCT_RE.search(text or "")
    return match.group(0).lower() if match else ""


def generic_niche_reason(niche: str) -> str:
    """Why `niche` is a generic market, or "" when it is specific enough.

    Specific means at least three content words that are not a broad marker, a
    generic audience or filler — i.e. an actual topic plus a situation.
    """
    text = " ".join((niche or "").lower().split())
    if not text:
        return "empty niche"
    words = [w for w in _WORD_RE.findall(text) if w not in _FILLER]
    if len(words) < 3:
        return (
            f"{niche!r} has {len(words)} content word(s): it names a market, "
            "not WHO has WHAT problem in WHICH context"
        )
    specific = [w for w in words if w not in BROAD_MARKERS and w not in GENERIC_AUDIENCE]
    if not specific:
        return (
            f"{niche!r} is built only from broad markers and a generic "
            "audience — it describes no reader and no situation"
        )
    return ""


# Format/product words that describe a shelf rather than a reader. A phrase
# built only from these is a store root, and autocomplete will happily complete
# it with more store roots.
GENERIC_FORMAT: frozenset[str] = frozenset({
    "journal", "journals", "journaling", "logbook", "logbooks", "notebook",
    "notebooks", "diary", "diaries", "planner", "planners", "workbook",
    "workbooks", "guide", "guides", "handbook", "handbooks", "checklist",
    "checklists", "tracker", "trackers", "worksheet", "worksheets", "gift",
    "gifts", "coloring", "activity", "activities", "series", "edition",
    "ebook", "audiobook", "paperback", "hardcover", "volume",
})


def generic_seed_reason(phrase: str) -> str:
    """Why `phrase` is too generic to seed autocomplete with, or "".

    Autocomplete answers a generic seed with generic completions: "grief"
    completes to "grief books", "grief journal", "grief gifts" — the whole
    store, not a reader. A seed has to carry a reader and a situation before
    Amazon's suggestions tell you anything a book can win.
    """
    text = " ".join((phrase or "").lower().split())
    if not text:
        return "no phrase"
    words = [w for w in _WORD_RE.findall(text) if w not in _FILLER]
    if not words:
        return f"{phrase!r} is a format word, not a subject"
    if len(words) < 2:
        return (
            f"{phrase!r} is a single word: Amazon completes it with store "
            "roots (\"… books\", \"… journal\", \"… gifts\"), not with demand"
        )
    if all(w in BROAD_MARKERS or w in GENERIC_AUDIENCE or w in GENERIC_FORMAT
           for w in words):
        return (
            f"{phrase!r} is only a broad market, a format or a generic "
            "audience — it names no reader and no situation"
        )
    if len(words) <= 2 and any(w in GENERIC_FORMAT for w in words):
        return (
            f"{phrase!r} is a subject plus a format (a shelf, not a reader): "
            "add who buys it and the situation they are in"
        )
    return ""


def generic_completion(phrase: str) -> bool:
    """True when a completion is itself a store root and carries no signal.

    "journal", "grief journals for women", "self help books", "books for men" —
    none of them say who has what problem, so keeping them as research targets
    only sends the run back to the whole store.
    """
    text = " ".join((phrase or "").lower().split())
    words = [w for w in _WORD_RE.findall(text) if w not in _FILLER]
    if len(words) < 2:
        return True
    if len(words) <= 2 and any(w in GENERIC_FORMAT for w in words):
        return True  # "grief gifts", "prayer journal": a subject plus a shelf
    return all(
        w in BROAD_MARKERS or w in GENERIC_AUDIENCE or w in GENERIC_FORMAT
        for w in words
    )


def screen_niche(niche: str) -> str:
    """The hard-rule reason to refuse `niche`, or "" when it may be researched."""
    term = banned_product_term(niche)
    if term:
        return (
            f"{niche!r} is a journal/logbook product ({term!r}): journals, "
            "logbooks, notebooks and diaries are excluded by rule"
        )
    return generic_niche_reason(niche)
