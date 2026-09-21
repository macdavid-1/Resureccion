"""Tests for frame-aware click resolution (CAPTCHA iframes).

The failure mode: CAPTCHA widgets render their checkbox inside a
cross-origin iframe. The top document only sees the iframe element, so a
naive resolver clicks the iframe's center or drops the tap entirely — the
owner "taps the box and nothing happens". These tests verify that a tap
over an iframe is re-resolved inside the owning frame and mapped back to
page coordinates.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.interactive import InteractiveSessionManager


class _FakeMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((float(x), float(y)))


class _FakeFrameElement:
    def __init__(self, box: dict[str, float]) -> None:
        self._box = box

    async def bounding_box(self) -> dict[str, float]:
        return self._box


class _FakeFrame:
    def __init__(self, box: dict[str, float], snap_result: dict[str, Any]) -> None:
        self._el = _FakeFrameElement(box)
        self._snap_result = snap_result
        self.evaluated: list[Any] = []

    async def frame_element(self) -> _FakeFrameElement:
        return self._el

    async def evaluate(self, script: str, arg: Any) -> Any:
        self.evaluated.append(arg)
        return self._snap_result


class _FakePage:
    def __init__(self, top_snap: dict[str, Any], frames: list[_FakeFrame]) -> None:
        self.mouse = _FakeMouse()
        self._top_snap = top_snap
        self.frames = frames

    @property
    def main_frame(self) -> object:
        return object()

    async def evaluate(self, script: str, arg: Any) -> Any:
        return self._top_snap


def _mgr_with(page: _FakePage) -> InteractiveSessionManager:
    class _B:
        pass

    m = InteractiveSessionManager.__new__(InteractiveSessionManager)
    m.browser = _B()
    return m


@pytest.mark.asyncio
async def test_tap_over_frame_with_checkbox_snaps_inside_frame() -> None:
    # Top document: tap lands on the IFRAME element itself (not interactive).
    # Inside the frame: the point sits on a role=checkbox → snap to its center.
    frame_box = {"x": 600, "y": 300, "width": 300, "height": 60}
    frame = _FakeFrame(
        frame_box,
        {"interactive": True, "x": 30, "y": 30, "area": 28 * 28, "hit": "div"},
    )
    page = _FakePage({"interactive": False, "hit": "iframe"}, frames=[frame])
    m = _mgr_with(page)

    await m._click_snapped(page, 630, 330)

    # Resolver ran in the frame with frame-local coords…
    assert frame.evaluated == [[30, 30]]
    # …and the physical click landed at frame-center + inner offset in PAGE
    # coordinates (600+30, 300+30) — the checkbox center, not the raw tap.
    assert page.mouse.clicks == [(630.0, 330.0)]


@pytest.mark.asyncio
async def test_tap_inside_frame_without_control_clicks_raw_point() -> None:
    # A bare widget frame with no interactive element: the tap goes through
    # at the exact point instead of being dropped.
    frame_box = {"x": 600, "y": 300, "width": 300, "height": 60}
    frame = _FakeFrame(frame_box, {"interactive": False, "hit": "canvas"})
    page = _FakePage({"interactive": False, "hit": "iframe"}, frames=[frame])
    m = _mgr_with(page)

    await m._click_snapped(page, 700, 320)

    assert frame.evaluated == [[100, 20]]
    assert page.mouse.clicks == [(700.0, 320.0)]


@pytest.mark.asyncio
async def test_tap_outside_all_frames_is_raw_click() -> None:
    frame_box = {"x": 600, "y": 300, "width": 300, "height": 60}
    frame = _FakeFrame(frame_box, {"interactive": True, "x": 0, "y": 0, "area": 1, "hit": "a"})
    page = _FakePage({"interactive": False, "hit": "div"}, frames=[frame])
    m = _mgr_with(page)

    await m._click_snapped(page, 100, 100)

    # No frame contains the point; nothing evaluated inside the frame.
    assert frame.evaluated == []
    assert page.mouse.clicks == [(100.0, 100.0)]


@pytest.mark.asyncio
async def test_top_page_still_snaps_normally() -> None:
    # Regression: ordinary top-document snapping is untouched.
    page = _FakePage(
        {"interactive": True, "x": 1224, "y": 64, "area": 2600, "hit": "a"},
        frames=[],
    )
    m = _mgr_with(page)

    await m._click_snapped(page, 1224, 64)
    assert page.mouse.clicks == [(1224.0, 64.0)]
