"""Tests for the KDSpy extension manager."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.browser_store import (
    EXT_FAILED,
    EXT_INSTALLED,
    EXT_NOT_CONFIGURED,
    EXT_VALIDATED,
    ExtensionStore,
)
from app.kdspy import KDSpyError, KDSpyManager, _version_tuple


MV3_MANIFEST = {
    "name": "KDSpy Pro",
    "version": "4.2.1",
    "manifest_version": 3,
    "background": {"service_worker": "background.js"},
}


@pytest.fixture()
def kdspy(db, config) -> KDSpyManager:
    return KDSpyManager(config, ExtensionStore(db))


def test_not_configured_when_absent(kdspy) -> None:
    state = kdspy.validate_installation()
    assert state.status == EXT_NOT_CONFIGURED
    assert kdspy.chromium_args() == []


def test_missing_manifest_is_failure(kdspy) -> None:
    kdspy.extension_path.mkdir(parents=True, exist_ok=True)
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED


def test_valid_manifest_registers_installed(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    state = kdspy.validate_installation()
    assert state.status == EXT_INSTALLED
    assert state.version == "4.2.1"
    assert state.detail["manifest"]["background"] == "service_worker"
    args = kdspy.chromium_args()
    assert len(args) == 2
    assert "--load-extension=" in args[1]


def test_version_minimum_enforced(kdspy, config) -> None:
    config.kdspy_expected_min_version = "5.0.0"
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED
    assert "minimum" in state.detail["error"]


def test_corrupt_manifest_is_failure(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text("{not json")
    state = kdspy.validate_installation()
    assert state.status == EXT_FAILED


def test_runtime_validation_roundtrip(kdspy, config) -> None:
    config.kdspy_extension_path.mkdir(parents=True, exist_ok=True)
    (config.kdspy_extension_path / "manifest.json").write_text(json.dumps(MV3_MANIFEST))
    kdspy.validate_installation()
    state = kdspy.record_runtime_validated("abcdeffedcba", "chrome-extension://abcdeffedcba/bg.js")
    assert state.status == EXT_VALIDATED
    assert state.extension_id == "abcdeffedcba"
    fail = kdspy.record_runtime_failure("sw missing")
    assert fail.status == EXT_FAILED


def test_version_tuple() -> None:
    assert _version_tuple("4.10.2") == (4, 10, 2)
    assert _version_tuple("junk") == (0,)
