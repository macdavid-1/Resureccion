"""KDSpy Pro extension manager.

The owner has a legitimate lifetime KDSpy Pro license. Resurrección loads the
actual unpacked extension into Chromium — it never fakes KDSpy data or scrapes
an imagined API. This module:

- accepts the extension from the owner as a ZIP upload or a multi-file upload
  (mobile file pickers cannot send folders), installing it atomically into the
  unpacked extension directory,
- validates the installed directory (manifest.json),
- records extension state/version metadata durably,
- produces the Chromium launch arguments required to load it,
- exposes hooks used by the live browser manager to verify the extension's
  service worker / background page actually started.

The extension directory is $KDSPY_EXTENSION_PATH (default:
$DATA_DIR/extensions/kdspy). A browser restart is required after install so
Chromium picks the extension up at launch.
"""
from __future__ import annotations

import json
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable

from app.browser_store import (
    EXT_FAILED,
    EXT_INSTALLED,
    EXT_NOT_CONFIGURED,
    EXT_VALIDATED,
    ExtensionStore,
)
from app.config import Config

KDSPY_EXTENSION_NAME = "kdspy"
KDSPY_FIREFOX_NAME = "kdspy-firefox"

# KDSpy's Firefox add-on directory (app/camoufox_engine.py loads it into
# Camoufox). Kept beside the Chromium unpacked extension directory.
KDSPY_FIREFOX_DIRNAME = "kdspy-firefox"

# The official Chrome Web Store product. A fetched package must declare this
# name (case/punctuation-insensitive) — combined with fetching under the
# pinned listing slug, a swapped/spoofed package cannot install.
KDSPY_STORE_NAME = "kdspy"


def _webstore_id_default() -> str:
    from app.crx_store import KDSPY_WEBSTORE_ID_DEFAULT

    return KDSPY_WEBSTORE_ID_DEFAULT

# Upper bound for an extension bundle — extensions are small; anything larger
# is wrong (or hostile).
MAX_EXTENSION_BYTES = 64 * 1024 * 1024

# File types an extension may contain. Blocks scripts/archives-in-archives.
# The META-INF signature suffixes (.manifest/.sig/.mf/.sf/.rsa) are the
# Mozilla COSE/JAR signing metadata every signed Firefox add-on ships — the
# real AMO KDSpy package carries all five, so they must be allowed for the
# one-tap install to work. They are inert metadata, not executable code.
_EXTENSION_FILE_SUFFIXES = {
    ".js", ".mjs", ".json", ".html", ".htm", ".css", ".png", ".jpg", ".jpeg",
    ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
    ".map", ".txt", ".md", ".xml", ".csv", ".lic", "",
    # Mozilla signed-add-on metadata (META-INF/):
    ".manifest", ".sig", ".mf", ".sf", ".rsa",
}

class KDSpyError(Exception):
    pass


class ExtensionInstallError(KDSpyError):
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

    # ------------------------------------------------------- firefox add-on
    @property
    def firefox_addon_path(self) -> Path:
        return self.config.kdspy_firefox_path

    def firefox_addon_is_configured(self) -> bool:
        return (self.firefox_addon_path / "manifest.json").is_file()

    def firefox_addon_args(self) -> list[str]:
        """Extracted add-on dir to load on the Camoufox engine (empty = none)."""
        if not self.firefox_addon_is_configured():
            return []
        return [str(self.firefox_addon_path)]

    def install_firefox_xpi(self, data: bytes) -> ExtensionManifestInfo:
        """Install an owner-supplied KDSpy Firefox add-on (XPI/ZIP).

        The XPI is exactly a ZIP of the unpacked add-on (optionally one level
        deep), so it re-uses the identical zip-slip / file-type / manifest /
        version defenses and atomic swap as the Chromium install paths, then
        validates before swapping — a broken upload never clobbers a working
        install. Camoufox (installed v0.5.6) loads the extracted dir natively
        via its ``addons`` launch option.
        """
        if not data:
            raise ExtensionInstallError("empty upload")
        if len(data) > MAX_EXTENSION_BYTES:
            raise ExtensionInstallError("upload too large (max 64 MB)")
        try:
            zf = zipfile.ZipFile(__import__("io").BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ExtensionInstallError("not a valid XPI/ZIP file") from exc
        with zf:
            names = zf.namelist()
            if not names:
                raise ExtensionInstallError("XPI is empty")
            manifest_name = self._pick_manifest(names)
            if manifest_name is None:
                raise ExtensionInstallError("manifest.json not found in XPI")
            self._check_zip_entries_firefox(zf, names)
            target_dir = self.firefox_addon_path
            staging = target_dir.parent / f".{KDSPY_FIREFOX_DIRNAME}-staging"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            try:
                root = Path(manifest_name).parent
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    p = Path(info.filename)
                    rel = p.relative_to(root) if root != Path(".") else p
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                info_obj = _parse_manifest(staging / "manifest.json")
                # Firefox add-ons are MV2 by convention; accept MV2/MV3 and
                # still require a sane manifest (name/version/bg shape).
                if info_obj.manifest_version not in (2, 3):
                    raise ExtensionInstallError(
                        f"unsupported manifest_version {info_obj.manifest_version} for a Firefox add-on"
                    )
                min_ver = self.config.kdspy_expected_min_version
                if min_ver and _version_tuple(info_obj.version) < _version_tuple(min_ver):
                    raise ExtensionInstallError(
                        f"KDSpy version {info_obj.version} is below required minimum {min_ver}"
                    )
                self._swap_in_firefox(staging)
            except ExtensionInstallError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            except KDSpyError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(str(exc)) from exc
            except Exception as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(f"install failed: {exc}") from exc
        return info_obj

    def _check_zip_entries_firefox(self, zf: zipfile.ZipFile, names: list[str]) -> None:
        """Zip-slip defense against the Firefox add-on target dir."""
        base = self.firefox_addon_path.resolve()
        for n in names:
            p = Path(n)
            if n.startswith("/") or ".." in p.parts:
                raise ExtensionInstallError(f"unsafe path in XPI: {n!r}")
            if n.startswith("__MACOSX"):
                continue
            if p.suffix.lower() not in _EXTENSION_FILE_SUFFIXES:
                raise ExtensionInstallError(f"file type not allowed in add-on: {n!r}")
            if len(p.parts) > 6:
                raise ExtensionInstallError(f"entry nested too deep: {n!r}")
            resolved = (base / p).resolve()
            if base not in resolved.parents and resolved != base:
                raise ExtensionInstallError(f"path escapes add-on directory: {n!r}")

    def _swap_in_firefox(self, staging: Path) -> None:
        """Atomically move the staged add-on dir into place (old kept as .bak)."""
        parent = self.firefox_addon_path.parent
        backup = parent / f".{KDSPY_FIREFOX_DIRNAME}-bak"
        if backup.exists():
            shutil.rmtree(backup)
        if self.firefox_addon_path.exists():
            self.firefox_addon_path.rename(backup)
        staging.rename(self.firefox_addon_path)
        shutil.rmtree(backup, ignore_errors=True)

    # ------------------------------------------------------------- inspection
    @property
    def extension_path(self) -> Path:
        return self.config.kdspy_extension_path

    def is_configured(self) -> bool:
        return self.extension_path.is_dir()

    # ---------------------------------------------------------------- install
    def install_from_zip(self, data: bytes) -> ExtensionManifestInfo:
        """Atomically install an uploaded ZIP (chrome web-store export style)."""
        if not data:
            raise ExtensionInstallError("empty upload")
        if len(data) > MAX_EXTENSION_BYTES:
            raise ExtensionInstallError("upload too large (max 64 MB)")
        try:
            zf = zipfile.ZipFile(__import__("io").BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ExtensionInstallError("not a valid ZIP file") from exc
        with zf:
            names = zf.namelist()
            if not names:
                raise ExtensionInstallError("ZIP is empty")
            # Locate manifest.json at root or one level deep (export wrappers).
            manifest_name = self._pick_manifest(names)
            if manifest_name is None:
                raise ExtensionInstallError("manifest.json not found in ZIP")
            self._check_zip_entries(zf, names)
            staging = self.extension_path.parent / f".kdspy-staging-{self.extension_path.name}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            try:
                root = Path(manifest_name).parent
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    p = Path(info.filename)
                    rel = p.relative_to(root) if root != Path(".") else p
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                # Validate BEFORE swapping in — a broken upload never clobbers a
                # working install.
                info_obj = _parse_manifest(staging / "manifest.json")
                min_ver = self.config.kdspy_expected_min_version
                if min_ver and _version_tuple(info_obj.version) < _version_tuple(min_ver):
                    raise ExtensionInstallError(
                        f"KDSpy version {info_obj.version} is below required minimum {min_ver}"
                    )
                self._swap_in(staging)
            except ExtensionInstallError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            except KDSpyError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(str(exc)) from exc
            except Exception as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(f"install failed: {exc}") from exc
        return self.validate_installation_after_install()

    async def install_from_webstore(
        self, *, fetch: "Callable[[], Awaitable[bytes]] | None" = None
    ) -> ExtensionManifestInfo:
        """One-tap install: fetch the official CRX from Google's CDN.

        The package must be a CRX3 whose embedded public key hashes to the
        pinned store ID; the unpacked ZIP then passes the identical zip-slip,
        file-type, manifest, and version defenses as an owner upload before
        the atomic swap. KDSpy is a free public store item — this fetches the
        exact package Chrome itself installs, no license data involved.
        """
        import io as _io

        from app.crx_store import CrxError, fetch_crx, verify_crx

        ext_id = (
            self.config.kdspy_webstore_id
            or _webstore_id_default()
        )
        try:
            if fetch is not None:
                data = await fetch()
            else:
                data = await fetch_crx(ext_id)
            crx = verify_crx(data, ext_id)
        except CrxError as exc:
            raise ExtensionInstallError(f"Web Store package rejected: {exc}") from exc
        except Exception as exc:
            raise ExtensionInstallError(
                f"could not download the extension from the Chrome Web Store: {exc}"
            ) from exc
        try:
            zf = zipfile.ZipFile(_io.BytesIO(crx.zip_bytes))
            names = zf.namelist()
            if not names:
                raise ExtensionInstallError("package payload is empty")
            manifest_name = self._pick_manifest(names)
            if manifest_name is None:
                raise ExtensionInstallError("manifest.json not found in package payload")
            self._check_zip_entries(zf, names)
            staging = self.extension_path.parent / f".kdspy-staging-{self.extension_path.name}"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            try:
                root = Path(manifest_name).parent
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    p = Path(info.filename)
                    rel = p.relative_to(root) if root != Path(".") else p
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                info_obj = _parse_manifest(staging / "manifest.json")
                if _store_name_of(info_obj.name) != KDSPY_STORE_NAME:
                    raise ExtensionInstallError(
                        f"package declares {info_obj.name!r}, not the pinned "
                        f"{KDSPY_STORE_NAME.title()} store listing — refused"
                    )
                min_ver = self.config.kdspy_expected_min_version
                if min_ver and _version_tuple(info_obj.version) < _version_tuple(min_ver):
                    raise ExtensionInstallError(
                        f"KDSpy version {info_obj.version} is below required minimum {min_ver}"
                    )
                self._swap_in(staging)
            except ExtensionInstallError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            except KDSpyError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(str(exc)) from exc
            except Exception as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(f"install failed: {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ExtensionInstallError("package payload is not a valid ZIP") from exc
        return self.validate_installation_after_install()

    async def install_from_amo(
        self, *, fetch: "Callable[[], Awaitable[bytes]] | None" = None
    ) -> ExtensionManifestInfo:
        """One-tap install: fetch the KDSpy Firefox add-on from Mozilla Add-ons.

        The camoufox engine's equivalent of ``install_from_webstore``: the
        owner-verified AMO download URL is pinned in config
        (``KDSPY_AMO_URL``), the XPI is fetched from addons.mozilla.org,
        and the package then passes the identical zip-slip, file-type,
        manifest, and version defenses as an owner upload before the atomic
        swap. AMO serves the exact package Firefox itself installs; the
        browser is opened for the kdspy.com sign-in afterwards (subscription
        activation), never for the download.
        """
        import io as _io

        url = self.config.kdspy_amo_url
        if not url:
            raise ExtensionInstallError(
                "KDSPY_AMO_URL is not set — cannot fetch the Firefox add-on"
            )
        if not _AMO_URL_RE.match(url):
            raise ExtensionInstallError(
                "KDSPY_AMO_URL must point at addons.mozilla.org — refusing "
                f"{url!r}"
            )
        try:
            if fetch is not None:
                data = await fetch()
            else:
                data = await fetch_amo_xpi(url)
        except AmoError as exc:
            raise ExtensionInstallError(f"Mozilla Add-ons download rejected: {exc}") from exc
        except Exception as exc:
            raise ExtensionInstallError(
                f"could not download the add-on from Mozilla Add-ons: {exc}"
            ) from exc
        try:
            zf = zipfile.ZipFile(_io.BytesIO(data))
            names = zf.namelist()
            if not names:
                raise ExtensionInstallError("XPI payload is empty")
            manifest_name = self._pick_manifest(names)
            if manifest_name is None:
                raise ExtensionInstallError("manifest.json not found in XPI payload")
            self._check_zip_entries_firefox(zf, names)
            staging = self.firefox_addon_path.parent / f".{KDSPY_FIREFOX_DIRNAME}-staging"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            try:
                root = Path(manifest_name).parent
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    p = Path(info.filename)
                    rel = p.relative_to(root) if root != Path(".") else p
                    target = staging / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(info))
                info_obj = _parse_manifest(staging / "manifest.json")
                if info_obj.manifest_version not in (2, 3):
                    raise ExtensionInstallError(
                        f"unsupported manifest_version {info_obj.manifest_version} for a Firefox add-on"
                    )
                min_ver = self.config.kdspy_expected_min_version
                if min_ver and _version_tuple(info_obj.version) < _version_tuple(min_ver):
                    raise ExtensionInstallError(
                        f"KDSpy version {info_obj.version} is below required minimum {min_ver}"
                    )
                self._swap_in_firefox(staging)
            except ExtensionInstallError:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            except KDSpyError as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(str(exc)) from exc
            except Exception as exc:
                shutil.rmtree(staging, ignore_errors=True)
                raise ExtensionInstallError(f"install failed: {exc}") from exc
        except zipfile.BadZipFile as exc:
            raise ExtensionInstallError("XPI payload is not a valid ZIP") from exc
        # Same contract as install_from_webstore: the parsed manifest of what
        # is now on disk (durable state was recorded by firefox_addon_state).
        return _parse_manifest(self.firefox_addon_path / "manifest.json")

    def install_from_files(self, files: list[tuple[str, BinaryIO]]) -> ExtensionManifestInfo:
        """Install from a multi-file upload (folder picked file-by-file)."""
        if not files:
            raise ExtensionInstallError("no files uploaded")
        total = 0
        staging = self.extension_path.parent / f".kdspy-staging-{self.extension_path.name}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            seen_manifest = False
            for rel_name, fh in files:
                rel_name = (rel_name or "").replace("\\", "/").lstrip("/")
                if not rel_name or ".." in Path(rel_name).parts:
                    raise ExtensionInstallError(f"unsafe path in upload: {rel_name!r}")
                suffix = Path(rel_name).suffix.lower()
                if suffix not in _EXTENSION_FILE_SUFFIXES:
                    raise ExtensionInstallError(f"file type not allowed in extension: {rel_name!r}")
                target = staging / rel_name
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as out:
                    while True:
                        chunk = fh.read(1024 * 256)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_EXTENSION_BYTES:
                            raise ExtensionInstallError("upload too large (max 64 MB)")
                        out.write(chunk)
                if Path(rel_name).name == "manifest.json":
                    seen_manifest = True
            if not seen_manifest:
                raise ExtensionInstallError("manifest.json missing from upload")
            info_obj = _parse_manifest(staging / "manifest.json")
            min_ver = self.config.kdspy_expected_min_version
            if min_ver and _version_tuple(info_obj.version) < _version_tuple(min_ver):
                raise ExtensionInstallError(
                    f"KDSpy version {info_obj.version} is below required minimum {min_ver}"
                )
            self._swap_in(staging)
        except ExtensionInstallError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except KDSpyError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ExtensionInstallError(str(exc)) from exc
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise ExtensionInstallError(f"install failed: {exc}") from exc
        return self.validate_installation_after_install()

    def _pick_manifest(self, names: list[str]) -> str | None:
        roots = [n for n in names if Path(n).name == "manifest.json" and not n.startswith("__MACOSX")]
        if not roots:
            return None
        shallowest = min(roots, key=lambda n: len(Path(n).parts))
        if len(Path(shallowest).parts) > 2:
            return None  # nested too deep — ambiguous bundle
        return shallowest

    def _check_zip_entries(self, zf: zipfile.ZipFile, names: list[str]) -> None:
        """Zip-slip, depth, and file-type defense before any byte is written."""
        base = self.extension_path.resolve()
        for n in names:
            p = Path(n)
            if n.startswith("/") or ".." in p.parts:
                raise ExtensionInstallError(f"unsafe path in ZIP: {n!r}")
            if n.startswith("__MACOSX"):
                continue
            if p.suffix.lower() not in _EXTENSION_FILE_SUFFIXES:
                raise ExtensionInstallError(f"file type not allowed in extension: {n!r}")
            if len(p.parts) > 6:
                raise ExtensionInstallError(f"entry nested too deep: {n!r}")
            resolved = (base / p).resolve()
            if base not in resolved.parents and resolved != base:
                raise ExtensionInstallError(f"path escapes extension directory: {n!r}")

    def _swap_in(self, staging: Path) -> None:
        """Atomically move the staged dir into place (old install kept as .bak)."""
        parent = self.extension_path.parent
        backup = parent / f".kdspy-bak-{self.extension_path.name}"
        if backup.exists():
            shutil.rmtree(backup)
        if self.extension_path.exists():
            self.extension_path.rename(backup)
        staging.rename(self.extension_path)
        shutil.rmtree(backup, ignore_errors=True)

    def validate_installation_after_install(self) -> ExtensionManifestInfo:
        """Re-validate on-disk state and refresh the durable record."""
        self.validate_installation()
        return self.inspect_local()

    def remove(self) -> None:
        if self.extension_path.exists():
            shutil.rmtree(self.extension_path)

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

    def record_firefox_runtime_validated(self, addon_path: str) -> ExtensionState:
        """Called by the browser manager once the Camoufox engine launched with
        the Firefox add-on loaded."""
        current = self.store.get(KDSPY_FIREFOX_NAME, self.profile)
        version = current.version if current else ""
        return self.store.upsert(
            KDSPY_FIREFOX_NAME,
            self.profile,
            status=EXT_VALIDATED,
            version=version,
            path=addon_path,
            detail={"engine": "camoufox"},
        )

    def record_firefox_runtime_failure(self, reason: str) -> ExtensionState:
        current = self.store.get(KDSPY_FIREFOX_NAME, self.profile)
        return self.store.upsert(
            KDSPY_FIREFOX_NAME,
            self.profile,
            status=EXT_FAILED,
            version=current.version if current else "",
            path=str(self.firefox_addon_path),
            detail={"error": reason},
        )

    def firefox_addon_state(self) -> ExtensionState:
        """Full local validation of the Firefox add-on (pre-launch check).
        Records the result durably — a WRITE path."""
        if not self.firefox_addon_is_configured():
            return self.store.upsert(
                KDSPY_FIREFOX_NAME,
                self.profile,
                status=EXT_NOT_CONFIGURED,
                path=str(self.firefox_addon_path),
                detail={"reason": "add-on not installed — upload the KDSpy XPI from Settings"},
            )
        try:
            info = _parse_manifest(self.firefox_addon_path / "manifest.json")
            min_ver = self.config.kdspy_expected_min_version
            if min_ver and _version_tuple(info.version) < _version_tuple(min_ver):
                raise KDSpyError(
                    f"KDSpy version {info.version} is below required minimum {min_ver}"
                )
        except KDSpyError as exc:
            return self.store.upsert(
                KDSPY_FIREFOX_NAME,
                self.profile,
                status=EXT_FAILED,
                path=str(self.firefox_addon_path),
                detail={"error": str(exc)},
            )
        return self.store.upsert(
            KDSPY_FIREFOX_NAME,
            self.profile,
            status=EXT_INSTALLED,
            version=info.version,
            path=str(self.firefox_addon_path),
            detail={
                "manifest": info.to_safe_dict(),
                "engine": "camoufox",
                "note": "loads natively on the camoufox engine (Firefox add-on)",
            },
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

    def firefox_addon_state_stored(self) -> ExtensionState:
        """READ path: the durable Firefox add-on record without re-upserting.

        Mirrors ``state()`` for the chromium flavor. Validating on every read
        would clobber the post-launch ``validated`` record (written by the
        browser manager) back down to ``installed`` — reads must not mutate.
        Falls back to full validation only when nothing is recorded yet.
        """
        s = self.store.get(KDSPY_FIREFOX_NAME, self.profile)
        if s is None:
            return self.firefox_addon_state()
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


# The Firefox add-on must come from Mozilla's own CDN — never a mirror or a
# lookalike domain. Sub-paths (addon slug + file id + filename) are allowed so
# a version bump is a config change, not a code change.
_AMO_URL_RE = re.compile(r"^https://addons\.mozilla\.org/.+\.xpi(\?.*)?$")

# Browser-shaped UA: AMO serves the file regardless, but a sane UA avoids
# bot-walls on the download edge.
_AMO_UA = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:132.0) Gecko/20100101 Firefox/132.0"
)

# Add-ons are small; anything larger is wrong or hostile.
MAX_XPI_BYTES = 64 * 1024 * 1024


class AmoError(Exception):
    """The Mozilla Add-ons download failed or is not a plausible XPI."""


async def fetch_amo_xpi(url: str) -> bytes:
    """Download an XPI from a pinned addons.mozilla.org URL.

    Production fetcher for ``KDSpyManager.install_from_amo``; tests inject a
    fake ``fetch`` instead. Redirects are followed (AMO's download endpoint
    hops to its CDN), and the payload must at least look like a ZIP before
    the manager's full validation takes over.
    """
    import httpx

    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=90.0, headers={"User-Agent": _AMO_UA}
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
    except Exception as exc:
        raise AmoError(str(exc)) from exc
    if len(data) > MAX_XPI_BYTES:
        raise AmoError("downloaded add-on too large")
    if not data.startswith(b"PK\x03\x04"):
        raise AmoError("download did not return a ZIP/XPI payload")
    return data


def _version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(p) for p in parts) if parts else (0,)


def _store_name_of(name: str) -> str:
    """Normalize a store product name: 'KDSPY – Keyword Research…' → 'kdspy'."""
    first = re.split(r"[–\-—|:]", name or "", maxsplit=1)[0]
    return re.sub(r"[^a-z0-9]", "", first.strip().lower())
