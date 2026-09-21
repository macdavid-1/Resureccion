"""Tests for anti-automation-detection (stealth) hardening.

The owner-facing failure these guard: anti-bot systems (reCAPTCHA and
friends) refuse to let a checkbox be checked when they detect automation.
These tests verify the patch is installed on every page of the persistent
context and that the injected script removes the automation tells.
"""
from __future__ import annotations

import asyncio

import pytest

from app import stealth


class _FakeContext:
    """Records init scripts added at context level."""

    def __init__(self) -> None:
        self.init_scripts: list[str] = []

    def add_init_script(self, script: str) -> None:
        self.init_scripts.append(script)


class _BrokenContext:
    def add_init_script(self, script: str) -> None:
        raise RuntimeError("context closing")


def test_stealth_installs_exactly_one_script() -> None:
    ctx = _FakeContext()
    stealth.apply(ctx)
    assert len(ctx.init_scripts) == 1
    script = ctx.init_scripts[0]
    # The primary automation tell must be patched out.
    assert "webdriver" in script
    # Headless UA must be normalized without pretending to be another browser.
    assert "HeadlessChrome" in script
    assert "Chrome" in script


def test_stealth_apply_is_never_fatal() -> None:
    # A context that refuses init scripts must not break the launch path.
    stealth.apply(_BrokenContext())


def test_stealth_script_shape() -> None:
    # The script must be self-contained and idempotent-safe (IIFE, no top-
    # level returns). It runs before page scripts in every frame.
    assert stealth.STEALTH_SCRIPT.strip().startswith("(() =>")
    assert "defineProperty" in stealth.STEALTH_SCRIPT
