"""KDSpy Pro extension manager.

The owner has a legitimate lifetime KDSpy Pro license. Resurrección loads the
actual unpacked extension into Chromium — it never fakes KDSpy data or scrapes
an imagined API. This module:

- validates the owner-supplied unpacked extension directory (manifest.json),
- records extension state/version metadata durably,
- produces the Chromium launch arguments required to load it,
- exposes hooks used by the live browser manager to verify the extension's
  service worker / background page actually started.

The extension directory is expected at $KDSPY_EXTENSION_PATH (default:
$DATA_DIR/extensions/kdspy). The owner drops their unpacked KDSpy Pro build
there; `validate_installation` checks it before any browser launch.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.browser_store import (
    EXT_FAILED,
    EXT_INSTALLED,
    EXT_NOT_CONFIGURED,
    EXT_VALIDATED,
    ExtensionStore,
)
from app.config import Config

KDSPY_EXTENSION_NAME = "kdspy"


class KDSpyError(Exception):
    pass


@dataclass
class ExtensionManifestInfo:
    name: str
    version: str
    manifest_version: int
    has_service_worker: bool
    has_background_page: bool
    raw: dict[str, Any]

    def to_safe_dict(self) -> dict[str, Any]:
        """Safe metadata only — no manifest internals that might embed keys."""
        return {
            "name": self.name,
            "version": self.version,
            "manifest_version": self.manifest_version,
            "background": "service_worker" if self.has_service_worker else ("page" if self.has_background_page else "none"),
        }


def _parse_manifest(path: Path) -> ExtensionManifestInfo:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KDSpyError(f"manifest.json unreadable at {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise KDSpyError("manifest.json is not a JSON object")
    name = str(raw.get("name", "")).strip()
    version = str(raw.get("version", "")).strip()
    mv = int(raw.get("manifest_version", 0) or 0)
    if not name or not version or mv not in (2, 3):
        raise KDSpyError("manifest.json missing name/version or has unsupported manifest_version")
    bg = raw.get("background") or {}
    has_sw = isinstance(bg, dict) and bool(bg.get("service_worker"))
    has_bg_page = isinstance(bg, dict) and bool(bg.get("page") or bg.get("scripts"))
    return ExtensionManifestInfo(
        name=name,
        version=version,
        manifest_version=mv,
        has_service_worker=has_sw,
        has_background_page=has_bg_page,
        raw=raw,
    )


class KDSpyManager:
    """Validates and registers the KDSpy Pro unpacked extension."""

    def __init__(self, config: Config, store: ExtensionStore) -> None:
        self.config = config
        self.store = store
        self.profile = "kdspy"  # the shared research profile

    # ------------------------------------------------------------- inspection
    @property
    def extension_path(self) -> Path:
        return self.config.kdspy_extension_path

    def is_configured(self) -> bool:
        return self.extension_path.is_dir()

    def inspect_local(self) -> ExtensionManifestInfo:
        """Parse and validate the on-disk extension (no browser required)."""
        path = self.extension_path
        if not path.is_dir():
            raise KDSpyError(
                f"KDSpy extension not found at {path}. "
                "Set KDSPY_EXTENSION_PATH or upload the unpacked extension there."
            )
        manifest_path = path / "manifest.json"
        if not manifest_path.is_file():
            raise KDSpyError(f"manifest.json missing under {path}")
        info = _parse_manifest(manifest_path)
        min_ver = self.config.kdspy_expected_min_version
        if min_ver and _version_tuple(info.version) < _version_tuple(min_ver):
            raise KDSpyError(
                f"KDSpy version {info.version} is below required minimum {min_ver}"
            )
        return info

    # ------------------------------------------------------------------ state
    def validate_installation(self) -> ExtensionState:
        """Full local validation and durable state record (pre-launch)."""
        if not self.is_configured():
            return self.store.upsert(
                KDSPY_EXTENSION_NAME,
                self.profile,
                status=EXT_NOT_CONFIGURED,
                path=str(self.extension_path),
                detail={"reason": "extension directory absent"},
            )
        try:
            info = self.inspect_local()
        except KDSpyError as exc:
            return self.store.upsert(
                KDSPY_EXTENSION_NAME,
                self.profile,
                status=EXT_FAILED,
                path=str(self.extension_path),
                detail={"error": str(exc)},
            )
        return self.store.upsert(
            KDSPY_EXTENSION_NAME,
            self.profile,
            status=EXT_INSTALLED,
            version=info.version,
            path=str(self.extension_path),
            detail={"manifest": info.to_safe_dict()},
        )

    def record_runtime_validated(self, extension_id: str, service_worker_url: str) -> ExtensionState:
        """Called by the browser manager once the SW is live in Chromium."""
        current = self.store.get(KDSPY_EXTENSION_NAME, self.profile)
        version = current.version if current else ""
        return self.store.upsert(
            KDSPY_EXTENSION_NAME,
            self.profile,
            status=EXT_VALIDATED,
            version=version,
            extension_id=extension_id,
            path=str(self.extension_path),
            detail={"service_worker_url": service_worker_url},
        )

    def record_runtime_failure(self, reason: str) -> ExtensionState:
        current = self.store.get(KDSPY_EXTENSION_NAME, self.profile)
        return self.store.upsert(
            KDSPY_EXTENSION_NAME,
            self.profile,
            status=EXT_FAILED,
            version=current.version if current else "",
            extension_id=current.extension_id if current else "",
            path=str(self.extension_path),
            detail={"error": reason},
        )

    def state(self) -> ExtensionState:
        s = self.store.get(KDSPY_EXTENSION_NAME, self.profile)
        if s is None:
            return self.validate_installation()
        return s

    # ------------------------------------------------------------- launch args
    def chromium_args(self) -> list[str]:
        """Chromium args to load ONLY this extension (Playwright persistent ctx)."""
        if not self.is_configured():
            return []
        return [
            f"--disable-extensions-except={self.extension_path}",
            f"--load-extension={self.extension_path}",
        ]


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)
