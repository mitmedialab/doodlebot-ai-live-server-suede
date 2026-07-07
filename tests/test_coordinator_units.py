"""Focused unit checks for two coordinator behaviors (no visuals):

* ``clear_drawings`` frees the occupancy grid, not just the record (bug #1), and
* a placed drawing is uniformly scaled to the canvas ``targetFootprintMm`` —
  longest side hits the target, aspect ratio preserved (feature #3).

Run with ``python tests/test_coordinator_units.py`` or under pytest.
"""

from __future__ import annotations

import glob
import json
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release import canvas as canvas_engine  # noqa: E402
from release.robots import (  # noqa: E402
    CanvasConfig,
    CheckIn,
    DrawingJob,
    PlacementSettings,
    Pose,
    RegionConfig,
    _Coordinator,
)

VEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectorizations")


def _a_vectorization() -> list[dict]:
    files = sorted(glob.glob(os.path.join(VEC_DIR, "*.json")))
    assert files, f"no vectorizations in {VEC_DIR}"
    with open(files[0]) as fh:
        return json.load(fh)


def _canvas(target_mm: float, w: float = 2000.0, h: float = 2000.0) -> CanvasConfig:
    """One big empty region owned by 'bot' — big enough that a drawing places at
    full target size (no shrink)."""
    return CanvasConfig(
        id="c",
        width=w,
        height=h,
        regions=[RegionConfig(id="r", x=0.0, y=0.0, width=w, height=h, robot="bot")],
        placement=PlacementSettings(strategy="origin", targetFootprintMm=target_mm),
    )


def _place_one(coord: _Coordinator, commands: list[dict]) -> "object":
    coord.enqueue(DrawingJob(jobId=coord.next_job_id(), commands=commands))
    resp = coord.check_in(CheckIn.Request(name="bot", status="ready", pose=Pose(x=0, y=0)))
    assert resp.action == "draw", "expected the drawing to place on an empty canvas"
    return coord._drawings["c"][-1]


def _bbox_span(strokes) -> tuple[float, float]:
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs), max(ys) - min(ys))


def test_clear_drawings_frees_occupancy():
    coord = _Coordinator([_canvas(target_mm=300.0, w=1000.0, h=1000.0)], seed=0)
    region = coord._store.region_for_robot("bot")
    assert region is not None

    _place_one(coord, _a_vectorization())
    assert region.free_fraction < 1.0, "placing should consume occupancy"

    cleared = coord.clear_drawings("c")
    assert cleared == 1
    assert coord._drawings["c"] == []
    assert region.free_fraction == 1.0, "clear_drawings must reset the occupancy grid"


def test_target_footprint_scaling_preserves_aspect():
    target = 300.0
    raw = _a_vectorization()

    # native (lead-in-stripped) shape, the basis the coordinator scales from
    _lead, native_drawing, _p0, _h0 = canvas_engine.split_lead_in(
        DrawingJob(jobId="probe", commands=raw).commands
    )
    nw, nh = _bbox_span(canvas_engine.commands_to_strokes(native_drawing))

    coord = _Coordinator([_canvas(target_mm=target)], seed=0)
    placed = _place_one(coord, raw)

    # measure the placed drawing at orientation 0 (its stored, scaled commands)
    sw, sh = _bbox_span(canvas_engine.commands_to_strokes(placed.commands))

    # longest side sized to the target
    assert abs(max(sw, sh) - target) < 1e-3 * target, (max(sw, sh), target)
    # uniform scale => aspect ratio unchanged (no stretching of one dimension)
    assert abs((sw / sh) - (nw / nh)) < 1e-6, (sw / sh, nw / nh)


if __name__ == "__main__":
    test_clear_drawings_frees_occupancy()
    test_target_footprint_scaling_preserves_aspect()
    print("coordinator unit checks passed")
