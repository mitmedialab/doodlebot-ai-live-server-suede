"""Visual + behavioural check of ``Region.compute_occupancy``.

What this exercises
-------------------
``compute_occupancy`` is the one thing standing between the committed grid (bare
ink) and the keep-out map ``try_place`` tests against: it takes the grid, grows
the occupied side by ``general_buffer`` mm, and crops back to the region's own
bounds. This test builds a region with a handful of source drawings committed at
known positions, then renders the occupancy map it produces at a range of
buffers so the growth is inspectable:

* ``occupancy_buffer_NNN.png``  — the keep-out map on its own (black = keep out)
* ``overlay_buffer_NNN.png``    — the same map (orange) with the committed ink
                                  (black) drawn back on top, so you can see
                                  exactly how far the buffer pushed past the ink

Output lands in ``tests/output/compute_occupancy/``.

The drawings are stamped at hand-picked positions rather than via ``try_place``
so the layout is fixed and each interesting case is visible in isolation: two
drawings sit close enough that their halos merge at the larger buffers, and the
dot sits near the right edge so you can see the halo get cropped at the region
bound instead of extending the region.

Run it directly for the visuals (``python tests/test_compute_occupancy.py``) or
under pytest (``pytest tests/test_compute_occupancy.py``). Both produce the PNGs.
"""

from __future__ import annotations

import math
import os
import sys

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
    Placement,
    PlacementConfig,
    Region,
    Stroke,
    rasterize,
)

OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "compute_occupancy"
)

REGION_W = 600.0
REGION_H = 400.0
CELL_MM = 2.0

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


def _make_region() -> Region:
    return Region(
        id="occ",
        x=0.0,
        y=0.0,
        width=REGION_W,
        height=REGION_H,
        robot="bot1",
        config=PlacementConfig(cell_mm=CELL_MM),
    )


def _occupancy(region: Region, buffer: int) -> np.ndarray:
    """``compute_occupancy`` with the neighbouring-robot machinery sat out.

    That path needs the containing canvas (to resolve which region each active
    robot draws in) and the active-drawing dict; this file is about the
    ``general_buffer`` growth in isolation, so we hand it a canvas holding only
    this region and nobody drawing. ``tests/test_active_robot_keepout.py`` covers
    the other half.
    """
    canvas = Canvas(
        id="occ-canvas",
        width=REGION_W,
        height=REGION_H,
        general_buffer=buffer,
        active_buffer=0,
        regions=[region],
    )
    return region.compute_occupancy(
        general_buffer=buffer, canvas=canvas, active_drawings={}
    )


def _half_width_cells(config: PlacementConfig) -> int:
    """The ink half-thickness ``try_place`` rasterizes at: pen radius + clearance."""
    return int(
        math.ceil((config.pen_mm / 2.0 + config.clearance_mm) / config.cell_mm)
    )


def _stamp(region: Region, strokes: list[Stroke], at_mm: tuple[float, float]) -> None:
    """Commit ``strokes`` with their local origin at ``at_mm`` in the region frame.

    Goes through the real ``Region.commit`` (via a hand-built ``Placement``) so the
    grid we hand to ``compute_occupancy`` is stamped exactly the way a placed
    drawing stamps it — we're only choosing the pose ourselves instead of letting
    the search pick it.
    """
    footprint = rasterize(strokes, region.config.cell_mm, _half_width_cells(region.config))
    # ``_top``/``_left`` are the grid cell the mask's own (0,0) lands on; the mask
    # carries ``pad`` cells of margin before the strokes' min corner.
    left = int(round(at_mm[0] / region.config.cell_mm)) - footprint.pad
    top = int(round(at_mm[1] / region.config.cell_mm)) - footprint.pad
    mh, mw = footprint.mask.shape
    rows, cols = region.grid.shape
    assert 0 <= top and top + mh <= rows and 0 <= left and left + mw <= cols, (
        f"stamp at {at_mm} would fall outside the region grid "
        f"(mask {mh}x{mw} at top={top} left={left}, grid {rows}x{cols})"
    )
    region.commit(
        Placement(
            angle_deg=0.0,
            anchor_x=region.x + at_mm[0],
            anchor_y=region.y + at_mm[1],
            _top=top,
            _left=left,
            _footprint=footprint,
        )
    )


def _populated_region() -> Region:
    """A region with four source drawings committed at fixed, hand-picked poses."""
    region = _make_region()
    _stamp(region, _square(90.0), (60.0, 60.0))
    _stamp(region, _circle(45.0), (230.0, 50.0))  # near the square: halos merge at 60mm
    _stamp(region, _zigzag(180.0, 70.0), (80.0, 260.0))
    _stamp(region, _dot(), (560.0, 200.0))  # near the right edge: halo gets cropped
    return region


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _upscale(img: Image.Image) -> Image.Image:
    return img.resize(
        (img.width * RENDER_SCALE, img.height * RENDER_SCALE), Image.NEAREST
    )


def _render_occupancy(path: str, occupancy: np.ndarray) -> None:
    """The keep-out map on its own: black = keep out, white = free."""
    arr = (255 - occupancy.astype(np.uint8) * 255).astype(np.uint8)
    _upscale(Image.fromarray(arr, mode="L")).save(path)


def _render_overlay(path: str, region: Region, occupancy: np.ndarray) -> None:
    """The keep-out map (orange) with the committed ink (black) drawn back on top.

    Anything orange is space the buffer reserved but no pen ever touches — that
    gap is the whole point of ``general_buffer``.
    """
    ink = region.grid.astype(bool)
    rgb = np.full(ink.shape + (3,), 255, dtype=np.uint8)
    rgb[occupancy] = (255, 176, 80)  # keep-out grown from the ink
    rgb[ink] = (20, 20, 20)  # the ink itself
    _upscale(Image.fromarray(rgb, mode="RGB")).save(path)


# --------------------------------------------------------------------------- #
# The demo
# --------------------------------------------------------------------------- #


def run_demo() -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    region = _populated_region()

    ink_cells = int(region.grid.sum())
    rendered: list[dict] = []
    for buffer in BUFFERS:
        occupancy = _occupancy(region, buffer)
        occ_path = os.path.join(OUT_DIR, f"occupancy_buffer_{buffer:03d}.png")
        overlay_path = os.path.join(OUT_DIR, f"overlay_buffer_{buffer:03d}.png")
        _render_occupancy(occ_path, occupancy)
        _render_overlay(overlay_path, region, occupancy)
        rendered.append(
            {
                "buffer": buffer,
                "occupied_cells": int(occupancy.sum()),
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
    assert result["ink_cells"] > 0, "no ink was committed — nothing to buffer"

    by_buffer = {f["buffer"]: f for f in result["frames"]}
    assert by_buffer[0]["occupied_cells"] == result["ink_cells"], (
        "a zero buffer must leave the grid alone"
    )

    # Every frame exists, and each larger buffer keeps out strictly more space.
    counts = [by_buffer[b]["occupied_cells"] for b in BUFFERS]
    assert counts == sorted(counts), f"occupancy shrank as the buffer grew: {counts}"
    for frame in result["frames"]:
        assert os.path.exists(frame["occupancy_png"]), frame
        assert os.path.exists(frame["overlay_png"]), frame


def test_zero_buffer_is_the_bare_grid():
    """No buffer -> the keep-out map is exactly the committed ink."""
    region = _populated_region()
    occupancy = _occupancy(region, 0)
    assert occupancy.dtype == np.bool_
    assert occupancy.shape == region.grid.shape
    np.testing.assert_array_equal(occupancy, region.grid.astype(bool))


def test_empty_region_is_entirely_free():
    """Nothing committed -> nothing to grow, at any buffer."""
    region = _make_region()
    for buffer in BUFFERS:
        occupancy = _occupancy(region, buffer)
        assert occupancy.shape == region.grid.shape
        assert not occupancy.any(), f"empty region claimed occupancy at buffer {buffer}"


def test_buffer_only_ever_grows_the_ink():
    """The map is a superset of the ink, and monotonic in the buffer."""
    region = _populated_region()
    ink = region.grid.astype(bool)

    previous = ink
    for buffer in BUFFERS:
        occupancy = _occupancy(region, buffer)
        assert occupancy.shape == region.grid.shape
        assert np.all(occupancy[ink]), f"buffer {buffer} un-occupied committed ink"
        assert np.all(occupancy[previous]), (
            f"buffer {buffer} freed space a smaller buffer kept out"
        )
        previous = occupancy

    # The grid itself is untouched — compute_occupancy snapshots, it doesn't mutate.
    np.testing.assert_array_equal(region.grid.astype(bool), ink)


def test_buffer_grows_ink_by_the_kernel_radius():
    """One isolated cell of ink dilates to exactly the elliptical kernel.

    This pins the actual contract: the buffer is ``ceil(general_buffer / cell_mm)``
    cells of clearance in every direction, not an approximation of it.
    """
    region = _make_region()
    row, col = 100, 150
    region.grid[row, col] = 1

    buffer_mm = 20
    radius = math.ceil(buffer_mm / CELL_MM)
    occupancy = _occupancy(region, buffer_mm)

    halo = occupancy[row - radius : row + radius + 1, col - radius : col + radius + 1]
    # cv2's MORPH_ELLIPSE of this size is what compute_occupancy dilates with, so a
    # single cell must reproduce it exactly.
    import cv2

    kernel = cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * radius + 1, 2 * radius + 1)
    ).astype(bool)
    np.testing.assert_array_equal(halo, kernel)
    # ...and nothing outside that neighbourhood is claimed.
    assert int(occupancy.sum()) == int(kernel.sum())


def test_halo_is_cropped_at_the_region_bounds():
    """Ink near an edge buffers up to the bound, and no further — the buffer pushes
    drawings apart, it doesn't extend the region."""
    region = _make_region()
    rows, cols = region.grid.shape
    region.grid[rows - 1, cols - 1] = 1  # bottom-right corner cell

    occupancy = _occupancy(region, 40)
    assert occupancy.shape == region.grid.shape
    assert occupancy[rows - 1, cols - 1]
    # A corner keeps out only the quarter of its halo that lands inside the region.
    radius = math.ceil(40 / CELL_MM)
    assert occupancy[rows - 1 - radius, cols - 1], "halo did not reach the full radius"
    assert not occupancy[: rows - 1 - radius, :].any(), "halo reached past its radius"


if __name__ == "__main__":
    summary = run_demo()
    print(f"ink cells committed: {summary['ink_cells']}")
    for frame in summary["frames"]:
        grown = frame["occupied_cells"] - summary["ink_cells"]
        print(
            f"  buffer={frame['buffer']:>3}mm  "
            f"keep-out cells={frame['occupied_cells']:>6}  "
            f"(+{grown} beyond the ink)"
        )
    print(f"wrote {2 * len(summary['frames'])} PNGs to {summary['out_dir']}")
