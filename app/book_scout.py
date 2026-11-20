"""Scrivy Book Scout: the add-on that puts BSR on Amazon search results.

Amazon prints a book's Best Sellers Rank only on its product page, so a search
page is blind: to rank ten results you must open ten pages. Scrivy Book Scout
(Scrivy, addons.mozilla.org) reads each result's product page and injects a
badge under the book — BSR, estimated sales per month, estimated revenue — plus
a niche snapshot bar and a details table for the whole page.

Resurrección loads that add-on — the real, AMO-signed package — into the
Camoufox browser and reads what it renders (`app.evidence_capture` →
`capture_book_scout`), so a search-results capture can carry each result's BSR
without opening a page per book. Nothing is faked: if the add-on is not
installed or has not rendered, the capture says so and the model falls back to
opening product pages.

Everything about installing/validating/launching is shared with the other
Firefox add-ons (`app.firefox_addon`); this module is the Book Scout
configuration, plus the small amount of parse logic that is specific to its
DOM.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.browser_store import EXT_INSTALLED, EXT_VALIDATED, ExtensionStore
from app.config import Config
from app.firefox_addon import ExtensionInstallError, FirefoxAddonManager

# Durable state row name and on-disk directory for the add-on.
BOOK_SCOUT_ADDON_NAME = "scrivy-book-scout"
BOOK_SCOUT_DIRNAME = "scrivy-book-scout"

# The add-on's own DOM markers. These are the add-on's public page surface: a
# badge per result (`data-asin`), a niche snapshot bar, and the details table.
BADGE_SELECTOR = ".scrivy-badge"
NICHE_BAR_SELECTOR = ".scrivy-niche-bar"
TABLE_ROW_SELECTOR = "table.scrivy-table tr[data-asin]"

# "BSR #1,234" / "Est. sales ~120/mo" / "Est. revenue ~$1.1K/mo" — the label is
# what makes a number mean something, so both are kept together.
_STAT_RE = re.compile(r"^\s*(?P<label>[^#~$£€]{1,40}?)\s*(?P<value>[#~$£€].*?)\s*$")


class BookScoutManager(FirefoxAddonManager):
    """Validates and registers the Scrivy Book Scout Firefox add-on."""

    def __init__(self, config: Config, store: ExtensionStore) -> None:
        super().__init__(
            config,
            store,
            name=BOOK_SCOUT_ADDON_NAME,
            label="Scrivy Book Scout",
            profile="kdspy",  # the shared research browser profile
            addon_path=config.book_scout_firefox_path,
            min_version=lambda: config.book_scout_expected_min_version,
            amo_url=lambda: config.book_scout_amo_url,
        )

    # ------------------------------------------------------ bundled package
    @property
    def bundled_xpi(self) -> Path:
        """The vendor package shipped in the repo (no account, no network)."""
        return Path(self.config.book_scout_bundled_xpi)

    def install_bundled(self) -> Any:
        """Install the add-on from the vendored, AMO-signed package.

        Same validation path as an upload or an AMO download — the bytes are
        just already on disk, so the feature works in an offline deploy and
        the owner never has to fetch or upload anything.
        """
        path = self.bundled_xpi
        if not path.is_file():
            raise ExtensionInstallError(
                f"the bundled Book Scout package is missing at {path} — "
                "reinstall from Mozilla Add-ons instead"
            )
        return self.install_firefox_xpi(path.read_bytes())

    def ensure_installed(self) -> bool:
        """Install the bundled package unless this browser already has it.

        Called at startup: the add-on ships with the app, so "install the
        extension first" is not a setup step the owner can forget. Never
        raises — a failure is recorded in the add-on state (Settings shows it)
        and the capture still fails precisely at research time.
        """
        try:
            state = self.validate_installation()
        except Exception:
            state = None
        if state is not None and state.status in (EXT_VALIDATED, EXT_INSTALLED):
            return True
        try:
            self.install_bundled()
            return True
        except Exception:
            return False


# --------------------------------------------------------------------- parse
def _stat(label: str, value: str) -> dict[str, str]:
    return {"label": " ".join(label.split()), "value": " ".join(value.split())}


def parse_badge(raw: dict[str, Any]) -> dict[str, Any]:
    """One rendered badge → {asin, bsr_text, stats, indie, unavailable}.

    Kept deliberately verbatim: the badge shows "BSR #1,234" as text, and the
    model/report quote those strings rather than a re-derived number.
    """
    stats = [
        _stat(str(s.get("label") or ""), str(s.get("value") or ""))
        for s in (raw.get("stats") or [])
        if isinstance(s, dict) and (s.get("label") or s.get("value"))
    ]
    bsr = ""
    for s in stats:
        if "bsr" in s["label"].lower() or "rank" in s["label"].lower():
            bsr = s["value"]
            break
    text = " ".join(str(raw.get("text") or "").split())
    unavailable = bool(
        raw.get("unavailable")
        or not stats
        or re.search(r"rank unavailable|not analyzed|fetching rank", text, re.IGNORECASE)
    )
    return {
        "asin": str(raw.get("asin") or "").strip().upper(),
        "title": " ".join(str(raw.get("title") or "").split())[:400],
        "bsr": bsr,
        "stats": stats,
        "indie": bool(raw.get("indie")),
        "unavailable": unavailable,
        "text": text[:400],
    }


def summarise(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts a capture can stand behind — never an invented aggregate."""
    with_bsr = [i for i in items if i.get("bsr")]
    return {
        "badges": len(items),
        "with_bsr": len(with_bsr),
        "unavailable": sum(1 for i in items if i.get("unavailable")),
        "indie": sum(1 for i in items if i.get("indie")),
    }


_BSR_CELL_RE = re.compile(r"^#\s?\d[\d,.]*$")
_BAD_FLAG_RE = re.compile(r"^(\u2014|-|)$")


def parse_table_row(raw: dict[str, Any]) -> dict[str, Any]:
    """One details-table row → {asin, title, bsr, cells} (shape-agnostic).

    The add-on renders two different tables (search results, series); rather
    than hard-code column meanings twice, the cells are kept verbatim and the
    one unambiguous column — the printed `#rank` — is lifted out.
    """
    cells = [" ".join(str(c).split()) for c in (raw.get("cells") or [])]
    bsr = next((c for c in cells if _BSR_CELL_RE.match(c)), "")
    title = ""
    for c in cells:
        if c and len(c) > 8 and not _BSR_CELL_RE.match(c) and not c.startswith(("~", "$", "\u00a3", "\u20ac")):
            title = c
            break
    return {
        "asin": str(raw.get("asin") or "").strip().upper(),
        "title": (title or " ".join(str(raw.get("title") or "").split()))[:400],
        "bsr": bsr,
        "cells": cells[:16],
    }


def parse_page_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalise the raw page read into the stored evidence shape."""
    data = payload or {}
    items = [parse_badge(x) for x in (data.get("badges") or []) if isinstance(x, dict)]
    niche = data.get("niche") or {}
    table_rows = [
        parse_table_row(row)
        for row in (niche.get("rows") or []) if isinstance(row, dict)
    ]
    return {
        "addon": BOOK_SCOUT_ADDON_NAME,
        "addon_version": str(data.get("addon_version") or ""),
        "url": str(data.get("url") or ""),
        "marketplace": str(data.get("marketplace") or ""),
        "query": str(data.get("query") or ""),
        "badges": items,
        "niche": {
            "stats": [
                _stat(str(s.get("label") or ""), str(s.get("value") or ""))
                for s in (niche.get("stats") or []) if isinstance(s, dict)
            ],
            "text": " ".join(str(niche.get("text") or "").split())[:1_200],
            "rows": table_rows,
        },
        "counts": summarise(items),
        "note": (
            "Third-party add-on data: BSR as the add-on read it from each "
            "book's page; sales/revenue are the add-on's ESTIMATES."
        ),
    }


def json_blob(payload: dict[str, Any]) -> str:
    """Compact JSON for evidence metadata (bounded per badge)."""
    return json.dumps(payload, ensure_ascii=False)[:200_000]
