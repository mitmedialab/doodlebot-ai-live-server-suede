"""``compute_exit_pose`` picks the same pose as the original loop, much faster,
and the coordinator computes it once per job.

The original implementation (a Python loop testing each candidate against the
ink buffered by the robot's radius) is kept below, verbatim apart from its
debug-plot stub, as the reference. Real drawings from ``tests/vectorizations``
are scattered over a canvas with markers spread across it — on the edges, at
the corners and in the interior — and every pose must match.

Run with ``python tests/test_compute_exit_pose.py`` or under pytest.
"""

from __future__ import annotations

import glob
import io
import json
import math
import os
import random
import sys
import time
from contextlib import redirect_stdout
from types import SimpleNamespace

import numpy as np
from shapely.geometry import LineString, Point as ShapelyPoint
from shapely.ops import unary_union

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release import robots  # noqa: E402
from release.canvas import (  # noqa: E402
    Marker,
    PlacementConfig,
    Region,
    commands_to_strokes,
    edge_yaw,
    split_lead_in,
)
from release.robots import (  # noqa: E402
    CanvasConfig,
    CheckIn,
    DrawingJob,
    PlacementSettings,
    Pose,
    RegionConfig,
    _Coordinator,
    compute_exit_pose,
)

VEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectorizations")
W = H = 1000.0


# --------------------------------------------------------------------------- #
# The reference: the original loop
# --------------------------------------------------------------------------- #


def reference_exit_pose(
    strokes,
    markers,
    allowed_region,
    robot_radius=80,
    min_marker_distance=300,
    max_marker_distance=700,
    distance_step=5,
    boundary=80,
    marker_weight=1.0,
    center_weight=1.0,
    drawing_weight=2.0,
):
    drawing_lines = unary_union([LineString(s) for s in strokes if len(s) >= 2])
    drawing = drawing_lines.buffer(robot_radius)
    best_pose, best_score = None, float("inf")
    robot = np.array(strokes[-1][-1])
    region_center = np.array(
        [allowed_region.x + allowed_region.width / 2, allowed_region.y + allowed_region.height / 2]
    )
    for marker in markers:
        if marker.yaw is None:
            continue
        marker_pos = np.array([marker.x, marker.y])
        normal = np.array([math.cos(marker.yaw), math.sin(marker.yaw)])
        for d in np.arange(min_marker_distance, max_marker_distance + distance_step, distance_step):
            candidate = marker_pos + d * normal
            point = ShapelyPoint(*candidate)
            if not (
                allowed_region.x <= candidate[0] <= allowed_region.x + allowed_region.width
                and allowed_region.y <= candidate[1] <= allowed_region.y + allowed_region.height
            ):
                continue
            distance_to_boundary = min(
                candidate[0] - allowed_region.x,
                allowed_region.x + allowed_region.width - candidate[0],
                candidate[1] - allowed_region.y,
                allowed_region.y + allowed_region.height - candidate[1],
            )
            if distance_to_boundary < boundary:
                continue
            if drawing.contains(point):
                continue
            score = (
                marker_weight * np.linalg.norm(candidate - marker_pos)
                + center_weight * np.linalg.norm(candidate - region_center)
                + drawing_weight * drawing_lines.distance(point)
            )
            if score < best_score:
                heading = math.atan2(marker.y - candidate[1], marker.x - candidate[0])
                best_score = score
                best_pose = Pose(
                    x=float(candidate[0]), y=float(candidate[1]), headingDegrees=math.degrees(heading)
                )
    if best_pose is not None:
        return best_pose
    fallback_center = region_center if region_center is not None else robot
    heading_degrees = 0.0
    if markers:
        nearest = min(
            markers, key=lambda m: (m.x - fallback_center[0]) ** 2 + (m.y - fallback_center[1]) ** 2
        )
        heading_degrees = math.degrees(
            math.atan2(nearest.y - fallback_center[1], nearest.x - fallback_center[0])
        )
    return Pose(x=float(fallback_center[0]), y=float(fallback_center[1]), headingDegrees=heading_degrees)


# --------------------------------------------------------------------------- #
# Scenes
# --------------------------------------------------------------------------- #


def _drawings() -> list[list]:
    """Each vectorization's ink, normalised to a 1mm-span local frame."""
    out = []
    for path in sorted(glob.glob(os.path.join(VEC_DIR, "*.json"))):
        with open(path) as fh:
            commands = [SimpleNamespace(**c) for c in json.load(fh)]
        _, ink, _, _ = split_lead_in(commands)
        strokes = commands_to_strokes(ink)
        pts = np.array([p for s in strokes for p in s])
        lo, span = pts.min(0), (pts.max(0) - pts.min(0)).max()
        out.append([[tuple((np.array(p) - lo) / span) for p in s] for s in strokes])
    return out


def _markers(points) -> list[Marker]:
    return [Marker(id=i, x=x, y=y, yaw=edge_yaw(x, y, W, H)) for i, (x, y) in enumerate(points)]


# Markers spread over the whole canvas: edges, corners and interior.
EVERYWHERE = _markers(
    [(x, 0) for x in (250, 500, 750)]
    + [(x, H) for x in (250, 500, 750)]
    + [(0, y) for y in (250, 500, 750)]
    + [(W, y) for y in (250, 500, 750)]
    + [(0, 0), (W, 0), (W, H), (0, H)]
    + [(300, 300), (700, 300), (500, 650)]
)
CORNERS_ONLY = _markers([(0, 0), (W, 0), (W, H), (0, H)])

REGIONS = [
    Region(id="full", x=0, y=0, width=W, height=H, config=PlacementConfig()),
    Region(id="tl", x=0, y=0, width=500, height=500, config=PlacementConfig()),
    Region(id="right", x=500, y=0, width=500, height=1000, config=PlacementConfig()),
]


def _scenes(n: int, seed: int = 0):
    """``n`` (strokes, region) pairs: a real drawing, sized and dropped at random
    inside a region."""
    rng = random.Random(seed)
    drawings = _drawings()
    for _ in range(n):
        region = rng.choice(REGIONS)
        size = rng.uniform(80, 0.8 * min(region.width, region.height))
        x = rng.uniform(region.x, region.x + region.width - size)
        y = rng.uniform(region.y, region.y + region.height - size)
        ink = [[(x + px * size, y + py * size) for px, py in s] for s in rng.choice(drawings)]
        yield ink, region


def _same(a: Pose, b: Pose) -> bool:
    return (
        abs(a.x - b.x) < 1e-9
        and abs(a.y - b.y) < 1e-9
        and abs(a.headingDegrees - b.headingDegrees) < 1e-9
    )


def _quiet(fn, *args):
    with redirect_stdout(io.StringIO()):
        return fn(*args)


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


def _ink_distance(ink, pose: Pose) -> float:
    return unary_union([LineString(s) for s in ink if len(s) >= 2]).distance(
        ShapelyPoint(pose.x, pose.y)
    )


def test_matches_the_original_with_markers_everywhere():
    """Same pose as the original — except where the original's pose was closer than
    ``robot_radius`` (80mm) to the ink.

    The original tested ``ink.buffer(80).contains(point)``, and a buffer rounds its
    caps and joins with chords, so it sits slightly inside the true 80mm band:
    points ~0.08mm too close still counted as clear. The new version compares the
    real distance with 80, so where the original's pick was one of those, it picks
    the best pose that is actually clear instead.
    """
    found = sliver = 0
    for ink, region in _scenes(200):
        want = _quiet(reference_exit_pose, ink, EVERYWHERE, region)
        got = _quiet(compute_exit_pose, ink, EVERYWHERE, region)
        cx, cy = region.x + region.width / 2, region.y + region.height / 2
        found += not (got.x == cx and got.y == cy)  # not the fallback
        if _same(got, want):
            continue
        assert _ink_distance(ink, want) < 80, (
            f"{region.id}: got {got}, want {want} — and the original's pose was clear"
        )
        assert 79.9 < _ink_distance(ink, want), "only the buffer's chord sliver may differ"
        if not (got.x == cx and got.y == cy):
            assert _ink_distance(ink, got) >= 80
        sliver += 1
    assert found > 100, f"only {found}/200 scenes found a pose — the comparison is too weak"
    assert sliver <= 10, f"{sliver}/200 scenes differ — more than the buffer's sliver explains"


def test_matches_the_original_fallback():
    """Corner markers face along the canvas edges, so every candidate sits within
    ``boundary`` of an edge and none survive: both fall back to the region centre."""
    for ink, region in _scenes(10, seed=1):
        want = _quiet(reference_exit_pose, ink, CORNERS_ONLY, region)
        got = _quiet(compute_exit_pose, ink, CORNERS_ONLY, region)
        assert _same(got, want)
        assert (got.x, got.y) == (region.x + region.width / 2, region.y + region.height / 2)


def test_is_much_faster_than_the_original():
    scenes = list(_scenes(20, seed=2))

    def total(fn):
        start = time.perf_counter()
        for ink, region in scenes:
            _quiet(fn, ink, EVERYWHERE, region)
        return time.perf_counter() - start

    total(compute_exit_pose)  # warm up
    old, new = total(reference_exit_pose), total(compute_exit_pose)
    assert new * 10 < old, f"only {old / new:.1f}x faster ({old * 50:.1f} vs {new * 50:.1f} ms/call)"


def test_the_bot_gets_the_pose_the_record_keeps_computed_once():
    with open(sorted(glob.glob(os.path.join(VEC_DIR, "*.json")))[0]) as fh:
        commands = json.load(fh)
    cfg = CanvasConfig(
        id="c",
        width=W,
        height=H,
        regions=[RegionConfig(id="r", x=0, y=0, width=W, height=H, robot="bot")],
        placement=PlacementSettings(strategy="origin", targetFootprintMm=250.0),
    )
    coord = _Coordinator([cfg], seed=0)
    coord._store.all()[0].markers = EVERYWHERE  # markers spread over the canvas

    calls = 0
    real = robots.compute_exit_pose

    def counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    robots.compute_exit_pose = counting
    try:
        coord.check_in(CheckIn.Request(name="bot", status="ready", pose=Pose(x=0, y=0)))
        coord.enqueue(DrawingJob(jobId=coord.next_job_id(), commands=commands))
        resp = coord.check_in(CheckIn.Request(name="bot", status="ready", pose=Pose(x=0, y=0)))
    finally:
        robots.compute_exit_pose = real

    assert resp.action == "draw"
    assert calls == 1, f"exit pose computed {calls} times for one job"
    record = coord._drawings["c"][-1]
    assert (record.exit_pose_x, record.exit_pose_y, record.exit_pose_deg) == (
        resp.exitPose.x,
        resp.exitPose.y,
        resp.exitPose.headingDegrees,
    )


if __name__ == "__main__":
    test_matches_the_original_with_markers_everywhere()
    test_matches_the_original_fallback()
    test_is_much_faster_than_the_original()
    test_the_bot_gets_the_pose_the_record_keeps_computed_once()
    print("compute_exit_pose checks passed")
