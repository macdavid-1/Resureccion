"""KDSpy Pro Firefox add-on: the concrete add-on Resurrección loads.

The owner has a legitimate KDSpy Pro subscription. Resurrección loads the
actual KDSpy Firefox add-on into the Camoufox engine — it never fakes KDSpy
data or scrapes an imagined API. All the install/validate/launch machinery
lives in `app.firefox_addon` (shared with other add-ons, e.g. Scrivy Book
Scout); this module is the KDSpy configuration of it.

The add-on directory is $KDSPY_FIREFOX_PATH (default:
$DATA_DIR/extensions/kdspy-firefox). A browser restart is required after
install so Camoufox picks the add-on up at launch.

The module keeps its historical public names (`KDSpyManager`, `KDSpyError`,
`ExtensionInstallError`, `fetch_amo_xpi`, `_version_tuple`, …) so every caller
and test is unchanged.
"""
from __future__ import annotations

from app.browser_store import ExtensionState, ExtensionStore
from app.config import Config
from app.firefox_addon import (
    MAX_EXTENSION_BYTES,
    MAX_XPI_BYTES,
    AmoError,
    ExtensionInstallError,
    ExtensionManifestInfo,
    FirefoxAddonError,
    FirefoxAddonManager,
    _parse_manifest,
    _version_tuple,
    fetch_amo_xpi,
)

# Durable state row name and on-disk directory for KDSpy's Firefox add-on.
KDSPY_FIREFOX_NAME = "kdspy-firefox"
KDSPY_FIREFOX_DIRNAME = "kdspy-firefox"

# Historical name for the base error class.
KDSpyError = FirefoxAddonError


class KDSpyManager(FirefoxAddonManager):
    """Validates and registers the KDSpy Pro Firefox add-on."""

    def __init__(self, config: Config, store: ExtensionStore) -> None:
        super().__init__(
            config,
            store,
            name=KDSPY_FIREFOX_NAME,
            label="KDSpy",
            profile="kdspy",
            addon_path=config.kdspy_firefox_path,
            # Resolved per read: the config value is the source of truth and
            # can change without rebuilding the manager.
            min_version=lambda: config.kdspy_expected_min_version,
            amo_url=lambda: config.kdspy_amo_url,
        )

    # Kept so older call sites that only know KDSpy still work unchanged.
    def firefox_addon_state_stored(self) -> ExtensionState:
        return super().firefox_addon_state_stored()


__all__ = [
    "KDSPY_FIREFOX_DIRNAME",
    "KDSPY_FIREFOX_NAME",
    "MAX_EXTENSION_BYTES",
    "MAX_XPI_BYTES",
    "AmoError",
    "ExtensionInstallError",
    "ExtensionManifestInfo",
    "ExtensionState",
    "KDSpyError",
    "KDSpyManager",
    "_parse_manifest",
    "_version_tuple",
    "fetch_amo_xpi",
]
