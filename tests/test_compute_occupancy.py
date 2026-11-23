"""Visual + behavioural check of the ``general_buffer`` keep-out.

What this exercises
-------------------
Between the committed ink and the keep-out map a placement is searched against,
the engine grows the occupied side by the canvas's ``general_buffer`` mm and
crops back to the region's own bounds. This test puts a handful of drawings on a
region at known positions, then renders the engine's grids (``canvas.occupancy``)
at a range of buffers so the growth is inspectable:

* ``occupancy_buffer_NNN.png``  — the keep-out map on its own (black = keep out)
* ``overlay_buffer_NNN.png``    — the same map (orange) with the committed ink
                                  (black) drawn back on top, so you can see
                                  exactly how far the buffer pushed past the ink

Output lands in ``tests/output/compute_occupancy/``.

The drawings sit at hand-picked positions rather than wherever the search would
put them, so the layout is fixed and each interesting case is visible in
isolation: two drawings sit close enough that their halos merge at the larger
buffers, and the dot sits near the right edge so you can see the halo get cropped
at the region bound instead of extending the region.

Run it directly for the visuals (``python tests/test_compute_occupancy.py``) or
under pytest (``pytest tests/test_compute_occupancy.py``). Both produce the PNGs.
"""

from __future__ import annotations

import math
import os
import sys

import cv2
import numpy as np
from PIL import Image

# Allow running as a plain script (python tests/test_compute_occupancy.py): make
# the repo root importable so ``release`` resolves. Under pytest the root is
# already on the path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release.canvas import (  # noqa: E402
    Canvas,
    Occupancy,
    PlacedDrawing,
    PlacementConfig,
    Region,
    Stroke,
    occupancy,
)

OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "compute_occupancy"
)

REGION_W = 600.0
REGION_H = 400.0
CELL_MM = 2.0
ROBOT = "bot1"

# Buffers to render, in mm. 0 is the degenerate case (occupancy == bare ink); the
# rest are large enough that the growth is obvious at this cell size, and 60 is
# big enough to merge the two neighbouring drawings' halos.
BUFFERS = (0, 10, 25, 60)

RENDER_SCALE = 3  # grid cells -> screen px, so a 200x300 grid is legible


# --------------------------------------------------------------------------- #
# Source drawings (local mm, origin at the drawing's own top-left-ish corner)
# --------------------------------------------------------------------------- #


def _square(side: float) -> list[Stroke]:
    return [[(0.0, 0.0), (side, 0.0), (side, side), (0.0, side), (0.0, 0.0)]]


def _circle(radius: float, segments: int = 48) -> list[Stroke]:
    pts = [
        (
            radius + radius * math.cos(2 * math.pi * i / segments),
            radius + radius * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments + 1)
    ]
    return [pts]


def _zigzag(width: float, height: float, teeth: int = 5) -> list[Stroke]:
    step = width / (2 * teeth)
    pts = [(i * step, 0.0 if i % 2 == 0 else height) for i in range(2 * teeth + 1)]
    return [pts]


def _dot() -> list[Stroke]:
    return [[(0.0, 0.0)]]  # single-point stroke — rasterizes to a disc


# --------------------------------------------------------------------------- #
# Region setup
# --------------------------------------------------------------------------- #


def _region() -> Region:
    return Region(
        id="occ",
        x=0.0,
        y=0.0,
        width=REGION_W,
        height=REGION_H,
        robot=ROBOT,
        config=PlacementConfig(cell_mm=CELL_MM),
    )


def _at(strokes: list[Stroke], at_mm: tuple[float, float]) -> PlacedDrawing:
    """A drawing already on the canvas, with its local origin at ``at_mm``.

    Only the strokes matter to the engine: it reserves each drawing's ink in its
    robot's region, wherever that ink is.
    """
    ax, ay = at_mm
    return PlacedDrawing(
        job_id=f"at_{ax:g}_{ay:g}",
        robot_name=ROBOT,
        anchor_x=ax,
        anchor_y=ay,
        angle_deg=0.0,
        commands=[],
        strokes=[[(x + ax, y + ay) for x, y in s] for s in strokes],
        exit_pose_x=0.0,
        exit_pose_y=0.0,
        exit_pose_deg=0.0,
    )


POPULATED: list[PlacedDrawing] = [
    _at(_square(90.0), (60.0, 60.0)),
    _at(_circle(45.0), (230.0, 50.0)),  # near the square: halos merge at 60mm
    _at(_zigzag(180.0, 70.0), (80.0, 260.0)),
    _at(_dot(), (560.0, 200.0)),  # near the right edge: halo gets cropped
]


def _occupancy(drawings: list[PlacedDrawing], buffer: int) -> Occupancy:
    """The engine's grids for the region, with nobody drawing next door.

    ``tests/test_active_robot_keepout.py`` covers the live-robot half.
    """
    region = _region()
    canvas = Canvas(
        id="occ-canvas",
        width=REGION_W,
        height=REGION_H,
        general_buffer=buffer,
        regions=[region],
    )
    return occupancy(canvas, region, drawings)


def _cv2_buffer(ink: np.ndarray, buffer_mm: float) -> np.ndarray:
    """Reference: ``ink`` dilated by OpenCV's elliptical kernel of radius
    ``ceil(buffer / cell)``, with nothing outside the region contributing."""
    radius = math.ceil(buffer_mm / CELL_MM)
    if radius == 0:
        return ink.copy()
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1))
    padded = np.pad(ink.astype(np.uint8), radius)
    grown = cv2.dilate(padded, kernel).astype(bool)
    return grown[radius:-radius, radius:-radius]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _upscale(img: Image.Image) -> Image.Image:
    return img.resize(
        (img.width * RENDER_SCALE, img.height * RENDER_SCALE), Image.NEAREST
    )


def _render_occupancy(path: str, keepout: np.ndarray) -> None:
    """The keep-out map on its own: black = keep out, white = free."""
    arr = (255 - keepout.astype(np.uint8) * 255).astype(np.uint8)
    _upscale(Image.fromarray(arr, mode="L")).save(path)


def _render_overlay(path: str, occ: Occupancy) -> None:
    """The keep-out map (orange) with the committed ink (black) drawn back on top.

    Anything orange is space the buffer reserved but no pen ever touches — that
    gap is the whole point of ``general_buffer``.
    """
    rgb = np.full(occ.ink.shape + (3,), 255, dtype=np.uint8)
    rgb[occ.keepout] = (255, 176, 80)  # keep-out grown from the ink
    rgb[occ.ink] = (20, 20, 20)  # the ink itself
    _upscale(Image.fromarray(rgb, mode="RGB")).save(path)


# --------------------------------------------------------------------------- #
# The demo
# --------------------------------------------------------------------------- #


def run_demo() -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)

    ink_cells = int(_occupancy(POPULATED, 0).ink.sum())
    rendered: list[dict] = []
    for buffer in BUFFERS:
        occ = _occupancy(POPULATED, buffer)
        occ_path = os.path.join(OUT_DIR, f"occupancy_buffer_{buffer:03d}.png")
        overlay_path = os.path.join(OUT_DIR, f"overlay_buffer_{buffer:03d}.png")
        _render_occupancy(occ_path, occ.keepout)
        _render_overlay(overlay_path, occ)
        rendered.append(
            {
                "buffer": buffer,
                "occupied_cells": int(occ.keepout.sum()),
                "occupancy_png": occ_path,
                "overlay_png": overlay_path,
            }
        )

    return {"out_dir": OUT_DIR, "ink_cells": ink_cells, "frames": rendered}


# --------------------------------------------------------------------------- #
# pytest entry points
# --------------------------------------------------------------------------- #


def test_renders_occupancy_for_source_drawings():
    """The visual harness: renders the map at every buffer and sanity-checks it."""
    result = run_demo()
    assert result["ink_cells"] > 0, "no ink was reserved — nothing to buffer"

    by_buffer = {f["buffer"]: f for f in result["frames"]}
    assert (
        by_buffer[0]["occupied_cells"] == result["ink_cells"]
    ), "a zero buffer must leave the ink alone"

    # Every frame exists, and each larger buffer keeps out strictly more space.
    counts = [by_buffer[b]["occupied_cells"] for b in BUFFERS]
    assert counts == sorted(counts), f"occupancy shrank as the buffer grew: {counts}"
    for frame in result["frames"]:
        assert os.path.exists(frame["occupancy_png"]), frame
        assert os.path.exists(frame["overlay_png"]), frame


def test_drawings_are_reserved_where_their_ink_is():
    """Every pen-down point lies in an ink cell — the reservation sits on the
    drawing, not offset from it."""
    ink = _occupancy(POPULATED, 0).ink
    for drawing in POPULATED:
        for stroke in drawing.strokes:
            for x, y in stroke:
                assert ink[int(y // CELL_MM), int(x // CELL_MM)], (
                    f"{drawing.job_id}: ink at ({x:.0f}, {y:.0f}) is not reserved"
                )


def test_zero_buffer_is_the_bare_ink():
    """No buffer -> the keep-out map is exactly the committed ink."""
    occ = _occupancy(POPULATED, 0)
    assert occ.keepout.dtype == np.bool_
    assert occ.keepout.shape == (int(REGION_H / CELL_MM), int(REGION_W / CELL_MM))
    np.testing.assert_array_equal(occ.keepout, occ.ink)


def test_empty_region_is_entirely_free():
    """Nothing on the canvas -> nothing to grow, at any buffer."""
    for buffer in BUFFERS:
        assert not _occupancy([], buffer).keepout.any(), (
            f"empty region claimed occupancy at buffer {buffer}"
        )


def test_buffer_only_ever_grows_the_ink():
    """The map is a superset of the ink, and monotonic in the buffer."""
    ink = _occupancy(POPULATED, 0).ink
    previous = ink
    for buffer in BUFFERS:
        keepout = _occupancy(POPULATED, buffer).keepout
        assert np.all(keepout[ink]), f"buffer {buffer} un-occupied committed ink"
        assert np.all(
            keepout[previous]
        ), f"buffer {buffer} freed space a smaller buffer kept out"
        previous = keepout


def test_buffer_is_exactly_the_elliptical_dilation():
    """The contract, pinned exactly: the buffer is the ink dilated by OpenCV's
    ``MORPH_ELLIPSE`` of radius ``ceil(general_buffer / cell_mm)`` cells — not an
    approximation of it — for real shapes and for every buffer."""
    for buffer in BUFFERS + (7, 33):
        occ = _occupancy(POPULATED, buffer)
        np.testing.assert_array_equal(
            occ.buffered, _cv2_buffer(occ.ink, buffer), err_msg=f"buffer={buffer}mm"
        )


def test_halo_is_cropped_at_the_region_bounds():
    """Ink near an edge buffers up to the bound, and no further — the buffer pushes
    drawings apart, it doesn't extend the region."""
    corner = [_at(_dot(), (REGION_W - 1.0, REGION_H - 1.0))]  # bottom-right corner
    occ = _occupancy(corner, 40)
    rows, cols = occ.ink.shape
    assert occ.ink[rows - 1, cols - 1], "the dot should land in the corner cell"
    np.testing.assert_array_equal(occ.buffered, _cv2_buffer(occ.ink, 40))
    # A corner keeps out only the quarter of its halo that lands inside.
    ink_rows = np.where(occ.ink.any(axis=1))[0]
    radius = math.ceil(40 / CELL_MM)
    assert not occ.buffered[: ink_rows[0] - radius, :].any(), "halo reached past its radius"


if __name__ == "__main__":
    summary = run_demo()
    print(f"ink cells reserved: {summary['ink_cells']}")
    for frame in summary["frames"]:
        grown = frame["occupied_cells"] - summary["ink_cells"]
        print(
            f"  buffer={frame['buffer']:>3}mm  "
            f"keep-out cells={frame['occupied_cells']:>6}  "
            f"(+{grown} beyond the ink)"
        )
    print(f"wrote {2 * len(summary['frames'])} PNGs to {summary['out_dir']}")
