"""Canvas occupancy + drawing placement engine.

The server owns a model of every physical canvas: its size, its aruco markers,
and the regions it is carved into (one region drawn by one robot). As drawings
are committed to a region we must guarantee a new drawing never overlaps an
existing one. This module is the algorithm-heavy half of that:

* **Occupancy** is a raster *occupancy grid* per region — the robotics-standard
  representation (cf. ROS costmaps). One cell ≈ a couple of millimetres; a 1 m²
  region is only ~250 KB as ``uint8``, so resolution/memory is a non-issue at
  the scale a pen plotter cares about.

* **Footprint** of a drawing is computed by turtle-integrating its line/spin/arc
  commands into pen-down polylines, then rasterizing those strokes (dilated by
  pen width + a clearance margin) into a small boolean mask.

* **Placement** is the irregular-nesting problem. We do a raster placement
  search: for each candidate rotation we slide the footprint mask over the grid
  and accept the first collision-free pose. A *summed-area table* (integral
  image) makes the common case — the footprint's bounding box landing on blank
  canvas — an O(1) test, so we only pay for an exact mask test near existing ink.

Rotation is essentially free for a turtle path: rotating a drawing by θ is
identical to starting the pen at heading θ. So placement first strips the lead-in
(the pen-up drive to the first ink) via ``split_lead_in``, packs and rotates only
the ink, and reports the first ink point as the start pose with an approach
heading of ``θ0 + angle`` — the drawing commands themselves are sent unchanged.

This module is pure geometry + numpy/PIL and has no FastAPI/pydantic deps; the
wire models and routing live in ``robots.py``.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import (
    Iterable,
    Literal,
    Optional,
    Protocol,
    Sequence,
    TypeAlias,
    TypeVar,
    Tuple,
    cast,
)

import numpy as np
import numpy.typing as npt
from PIL import Image, ImageDraw
from scipy.fft import next_fast_len
import cv2

# --------------------------------------------------------------------------- #
# Command protocol (duck-typed against robots.py's pydantic models)
# --------------------------------------------------------------------------- #


class LineCommand(Protocol):
    kind: Literal["line"]
    distance: float
    penDown: bool


class SpinCommand(Protocol):
    kind: Literal["spin"]
    degrees: float


class ArcCommand(Protocol):
    kind: Literal["arc"]
    radius: float
    degrees: float


Command: TypeAlias = LineCommand | SpinCommand | ArcCommand

# Bound to the command protocol so functions that slice/return commands (e.g.
# ``split_lead_in``) preserve the caller's *concrete* type — robots.py passes its
# pydantic models in and gets pydantic models back, not bare protocol objects.
CommandT = TypeVar("CommandT", bound=Command)


Point = tuple[float, float]
Stroke = list[Point]  # a contiguous pen-down polyline, in drawing-local mm

BoolMask: TypeAlias = npt.NDArray[np.bool_]
"""A boolean raster over grid cells — ``True`` = covered/blocked.

Footprint masks, keep-out maps and free-offset maps are all this shape of thing;
they differ in what they're indexed by, which the parameter names carry.
"""

Grid: TypeAlias = npt.NDArray[np.uint8]
"""A region's committed occupancy grid: 0/1 per cell.

``uint8`` rather than ``bool`` because ``commit`` stamps footprints in with
``|=`` and callers treat it as counts-adjacent (``grid.sum()``).
"""

Spectrum: TypeAlias = npt.NDArray[np.complex128]
"""Half-spectrum of a real 2-D grid, as returned by ``np.fft.rfft2``.

``complex128`` (not ``complex64``) is load-bearing — see ``_free_offsets``.
"""


# --------------------------------------------------------------------------- #
# Geometry: drawing commands -> pen-down strokes
# --------------------------------------------------------------------------- #


def commands_to_strokes(
    commands: Sequence[Command], arc_step_deg: float = 6.0
) -> list[Stroke]:
    """Turtle-integrate drawing commands into pen-down polylines (local mm frame).

    Conventions follow the vectorizer (``arc_line_vectorization_suede.commands``):
    heading 0 points along +x, angles are degrees, ``line`` carries an explicit
    ``penDown`` flag (pen-up lines are travel moves), ``spin`` rotates in place,
    and ``arc`` is always a pen-down stroke flattened into short segments.
    """

    x, y, heading = 0.0, 0.0, 0.0
    strokes: list[Stroke] = []
    current: Stroke = []

    def extend(nx: float, ny: float) -> None:
        nonlocal current
        if not current:
            current = [(x, y)]
        current.append((nx, ny))

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            strokes.append(current)
        current = []

    for cmd in commands:
        if cmd.kind == "line":
            rad = math.radians(heading)
            nx = x + cmd.distance * math.cos(rad)
            ny = y + cmd.distance * math.sin(rad)
            if cmd.penDown:
                extend(nx, ny)
            else:
                flush()  # travel move ends the current stroke
            x, y = nx, ny
        elif cmd.kind == "spin":
            heading += cmd.degrees  # in-place rotation; does not move or break a stroke
        elif cmd.kind == "arc":
            steps = max(1, int(math.ceil(abs(cmd.degrees) / arc_step_deg)))
            dtheta = cmd.degrees / steps
            seg_len = cmd.radius * math.radians(abs(dtheta))
            for _ in range(steps):
                rad = math.radians(heading)
                nx = x + seg_len * math.cos(rad)
                ny = y + seg_len * math.sin(rad)
                extend(nx, ny)
                x, y = nx, ny
                heading += dtheta
        else:
            raise ValueError(f"Unknown drawing command kind: {cmd.kind!r}")

    flush()
    return strokes


def rotate_strokes(strokes: Sequence[Stroke], degrees: float) -> list[Stroke]:
    """Rotate strokes about (0, 0). Rigid, so it only changes the footprint's
    orientation; the placement search re-rasterizes per angle so the pivot choice
    doesn't matter to the mask — only to how we map the anchor back to mm."""
    if degrees == 0.0:
        return [list(s) for s in strokes]
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    return [
        [(px * c - py * s, px * s + py * c) for px, py in stroke] for stroke in strokes
    ]


def split_lead_in(
    commands: Sequence[CommandT],
) -> tuple[list[CommandT], list[CommandT], Point, float]:
    """Separate a drawing's lead-in from its actual ink.

    A vectorization starts at the pen origin and typically *drives pen-up* (and
    maybe spins) to reach the first place it draws. That lead-in is an artifact of
    the vectorizer's origin and shouldn't constrain placement: if we rotated it
    along with the ink, the robot's start pose could end up off-canvas.

    Returns ``(lead_in, drawing, start_point, start_heading)`` where ``drawing``
    begins at the first pen-down command, ``start_point`` is the local-mm point
    where the first ink begins, and ``start_heading`` is the heading (degrees)
    there. The robot is told to navigate straight to ``start_point`` (placed +
    rotated) facing ``start_heading + angle``, then run ``drawing`` — so the
    lead-in is recomputed as a plain "drive to the start" rather than rotated.
    """

    x, y, heading = 0.0, 0.0, 0.0
    for i, cmd in enumerate(commands):
        # Read attributes through the union view: the checkers narrow ``Command``
        # on ``.kind`` but not the ``CommandT`` type var, so cast to the union for
        # the geometry; slice ``commands`` itself so the returned lists keep the
        # caller's concrete command type.
        c = cast(Command, cmd)
        if c.kind == "line":
            if c.penDown:
                return list(commands[:i]), list(commands[i:]), (x, y), heading
            rad = math.radians(heading)
            x += c.distance * math.cos(rad)
            y += c.distance * math.sin(rad)
        elif c.kind == "spin":
            heading += c.degrees
        elif c.kind == "arc":  # arcs are pen-down ink — drawing starts here
            return list(commands[:i]), list(commands[i:]), (x, y), heading
        else:
            raise ValueError(f"Unknown drawing command kind: {c.kind!r}")

    return list(commands), [], (x, y), heading  # no ink at all


# --------------------------------------------------------------------------- #
# Rasterization: strokes -> boolean footprint mask
# --------------------------------------------------------------------------- #


@dataclass
class Footprint:
    mask: BoolMask  # shape (rows, cols) — True = covered
    min_x: float  # local-mm coords of the mask's (0,0) cell corner
    min_y: float
    pad: int
    cell_mm: float

    def to_px(self, point: Point) -> tuple[float, float]:
        """Map a local-mm point to (col, row) within the mask."""
        return (
            (point[0] - self.min_x) / self.cell_mm + self.pad,
            (point[1] - self.min_y) / self.cell_mm + self.pad,
        )


def rasterize(
    strokes: Sequence[Stroke], cell_mm: float, half_width_cells: int
) -> Footprint:
    """Rasterize pen-down strokes into a dilated boolean mask.

    ``half_width_cells`` is half the drawn line thickness in cells — i.e. the
    pen radius plus the clearance margin — so the mask already encodes the
    keep-out buffer around the ink.
    """

    pts = [p for stroke in strokes for p in stroke]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    min_x, min_y = min(xs), min(ys)
    max_x, max_y = max(xs), max(ys)

    pad = half_width_cells + 1
    width_cells = int(math.ceil((max_x - min_x) / cell_mm)) + 2 * pad + 1
    height_cells = int(math.ceil((max_y - min_y) / cell_mm)) + 2 * pad + 1

    img = Image.new("1", (width_cells, height_cells), 0)
    draw = ImageDraw.Draw(img)

    def to_px(p: Point) -> tuple[float, float]:
        return ((p[0] - min_x) / cell_mm + pad, (p[1] - min_y) / cell_mm + pad)

    line_width = 2 * half_width_cells + 1
    for stroke in strokes:
        if len(stroke) >= 2:
            draw.line(
                [to_px(p) for p in stroke], fill=1, width=line_width, joint="curve"
            )
        elif len(stroke) == 1:
            px, py = to_px(stroke[0])
            r = half_width_cells
            draw.ellipse([px - r, py - r, px + r, py + r], fill=1)

    mask: BoolMask = np.array(img, dtype=np.bool_)
    return Footprint(mask=mask, min_x=min_x, min_y=min_y, pad=pad, cell_mm=cell_mm)


# --------------------------------------------------------------------------- #
# Placement search over an occupancy grid
# --------------------------------------------------------------------------- #


@dataclass
class PlacementConfig:
    cell_mm: float = 2.0
    """Occupancy-grid resolution. ~2mm is sub-pen-width; finer just costs memory."""

    pen_mm: float = 3.0
    """Drawn line width."""

    clearance_mm: float = 8.0
    """Keep-out margin enforced between separate drawings."""

    search_step_cells: int = 2
    """Stride of the sliding-window search (in cells). Larger = faster, coarser."""

    angles_deg: tuple[float, ...] = tuple(float(a) for a in range(0, 360, 15))
    """Candidate rotations, tried in order (0° first = prefer upright)."""

    strategy: Literal["origin", "scatter"] = "origin"
    """How to choose among valid poses.

    ``origin`` packs every drawing toward the region's (0,0) corner — the
    classic Bottom-Left-Fill heuristic, though in this screen-like y-down frame
    (0,0) is the top-left. Dense, but it looks like a print head filling a page.
    ``scatter`` picks a random valid pose (position + rotation), so drawings
    appear spread across the region like several artists working it at once; it
    jams at lower coverage (random sequential packing always does), trading
    density for that organic look.
    """

    target_footprint_mm: float = 200.0
    """Desired size of a placed drawing given ample free space: the coordinator
    uniformly scales each drawing (preserving aspect ratio) so its *longest*
    footprint dimension is this many mm, then shrinks below it only if it won't
    fit. Carried here for convenience; the scaling itself lives in ``robots.py``.
    """

    min_footprint_scale: float = 0.4
    """Floor for the shrink-to-fit fallback, as a fraction of ``target_footprint_mm``.
    A drawing that won't fit even at this size is left queued rather than drawn
    illegibly small. Also carried for convenience; applied in ``robots.py``.
    """


@dataclass
class Placement:
    angle_deg: float
    anchor_x: float  # global mm — where the first ink point (P0) lands
    anchor_y: float
    _top: int  # grid row of the mask's top-left (internal, for stamping)
    _left: int
    _footprint: Footprint


def _fft_shape(grid_shape: tuple[int, int]) -> tuple[int, int]:
    """Transform size to use for a grid of ``grid_shape`` — the next FFT-friendly size.

    An FFT is only fast when its length factors into small primes; a prime length
    degrades toward the O(n²) DFT. Grid sizes here are ``ceil(width_mm / cell_mm)``
    — an arbitrary integer chosen by whoever posted the canvas — so they land on
    hostile lengths regularly (a 1000mm canvas at 1.5mm cells gives 667 = 23·29,
    which transforms ~2x slower than the 675 next to it). Rounding *up* to a
    5-smooth length costs a little zero-padding and buys that back.

    Padding is safe for the wraparound argument in ``_free_offsets``: enlarging the
    period only adds zeros past the grid, and the valid offsets we read back
    (``t <= rows - mh``) stay well inside the un-aliased range.
    """

    return (
        next_fast_len(grid_shape[0], real=True),  # type: ignore
        next_fast_len(grid_shape[1], real=True),
    )


def _free_offsets(
    grid_fft: Spectrum,
    mask: BoolMask,
    grid_shape: tuple[int, int],
    fft_shape: tuple[int, int],
) -> BoolMask:
    """Boolean map of collision-free top-left offsets, via FFT cross-correlation.

    ``corr[t, l]`` is the number of occupied cells the footprint would overlap if
    its mask's top-left sat at grid cell ``(t, l)``. The cross-correlation theorem
    gives every offset at once from one inverse FFT —

        corr = irfft2( FFT(grid) · conj(FFT(mask)) )

    — replacing the per-position sweep that used to dominate the runtime. Overlap
    counts are integers, so a free cell is exactly where ``corr`` rounds to zero
    (``< 0.5`` absorbs FFT floating-point error — which is also why this stays in
    float64: the transform's DC term runs to ~1e8 here, and float32's epsilon
    against that is far wider than the 0.5 the threshold allows).

    ``grid_fft`` is precomputed once per placement and reused across rotations
    (only the mask changes per angle); it must have been transformed at
    ``fft_shape``, which ``_fft_shape`` derives from ``grid_shape``.

    Returns an array over valid top-left offsets, shape
    ``(rows - mh + 1, cols - mw + 1)``; ``True`` means that offset is collision-free.
    """
    rows, cols = grid_shape
    mh, mw = mask.shape
    mask_fft = np.fft.rfft2(mask.astype(np.float64), fft_shape)
    corr = np.fft.irfft2(grid_fft * np.conj(mask_fft), fft_shape)
    valid = corr[: rows - mh + 1, : cols - mw + 1]
    return valid < 0.5


FootprintCacheKey: TypeAlias = tuple[
    float, float, int
]  # (angle, cell_mm, half_width_cells)

FootprintCacheValue: TypeAlias = tuple[
    Optional[Footprint], Optional[Point]
]  # (footprint, first-ink point)


class FootprintCache:
    """Memoizes one drawing's rotated, rasterized footprints across placements.

    A queued drawing is re-tested for placement on every poll and against every
    candidate bot, but its footprint at a given rotation and grid resolution never
    changes — only the occupancy it's tested against does. Caching the rotate +
    rasterize step (the expensive part) turns that repeated work into a one-time
    cost per ``(angle, cell, half)``. The cache is keyed so distinct region
    resolutions coexist correctly, and it's meant to live with the queued job, so
    it's discarded once the job is placed.
    """

    def __init__(self, strokes: Sequence[Stroke]) -> None:
        self._strokes = strokes
        self._cache: dict[FootprintCacheKey, FootprintCacheValue] = {}

    def at(
        self, angle: float, cell_mm: float, half_width_cells: int
    ) -> tuple[Optional[Footprint], Optional[Point]]:
        """Rotated footprint at ``angle``, covering the ink only.

        The general keep-out buffer is *not* applied here — it's dilated onto the
        occupancy map instead (see ``Region.compute_occupancy``), which is the same
        clearance for one dilation per placement rather than one per rotation. It
        also keeps ``Region.commit`` stamping bare ink into the grid, so the buffer
        stays a property of the test rather than something baked into the region.
        """
        key = self._key(angle, cell_mm, half_width_cells)
        hit = self._cache.get(key)
        if hit is None:
            rotated = rotate_strokes(self._strokes, angle)
            footprint = rasterize(rotated, cell_mm, half_width_cells)
            anchor = rotated[0][0]
            hit = (footprint, anchor)
            self._cache[key] = hit
        return hit

    def _key(
        self, angle: float, cell_mm: float, half_width_cells: int
    ) -> FootprintCacheKey:
        return (angle, cell_mm, half_width_cells)


# --------------------------------------------------------------------------- #
# Region + Canvas
# --------------------------------------------------------------------------- #


@dataclass
class Region:
    """A rectangular area of a canvas drawn by one robot, with its occupancy grid."""

    id: str
    x: float  # min corner in global/canvas mm
    y: float
    width: float
    height: float
    robot: Optional[str] = None
    config: PlacementConfig = field(default_factory=PlacementConfig)

    grid: Grid = field(init=False)

    def __post_init__(self) -> None:
        cols = max(1, int(math.ceil(self.width / self.config.cell_mm)))
        rows = max(1, int(math.ceil(self.height / self.config.cell_mm)))
        self.grid = np.zeros((rows, cols), dtype=np.uint8)

    @property
    def free_fraction(self) -> float:
        return 1.0 - float(self.grid.sum()) / float(self.grid.size)

    def compute_occupancy(self, *, general_buffer: int) -> BoolMask:
        """Snapshot this region's grid as the keep-out map ``try_place`` tests against.

        The committed grid holds bare ink; ``general_buffer`` (mm) is the keep-out
        margin every new drawing must leave around it. Growing the *occupied* side
        by that margin is equivalent to growing each candidate footprint by it
        (Minkowski sum), but costs one dilation per placement instead of one per
        candidate rotation — hence ``try_place`` takes the result rather than
        deriving it.

        Returns a boolean array the same shape as ``self.grid`` (region-grid cells,
        ``True`` = keep out), so it drops straight into the FFT correlation.
        """

        occupied: BoolMask = self.grid.astype(np.bool_)

        buffer_cells = math.ceil(general_buffer / self.config.cell_mm)
        if buffer_cells <= 0 or not occupied.any():
            return occupied

        h, w = occupied.shape

        padded = np.pad(
            occupied.astype(np.uint8),
            buffer_cells,
            mode="constant",
            constant_values=0,
        )

        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE,
            (2 * buffer_cells + 1, 2 * buffer_cells + 1),
        )

        # cv2 is untyped here, so pin the result back to a known dtype rather than
        # letting Any leak into the return.
        dilated: BoolMask = cv2.dilate(padded, kernel).astype(np.bool_)

        # Crop back to the region grid: the buffer only pushes drawings apart from
        # each other, it doesn't extend the region's own bounds.
        return dilated[buffer_cells : buffer_cells + h, buffer_cells : buffer_cells + w]

    def try_place(
        self,
        strokes: Sequence[Stroke],
        occupancy: BoolMask,
        rng: Optional[random.Random] = None,
        footprints: Optional["FootprintCache"] = None,
    ) -> Optional[Placement]:
        """Find a collision-free pose (rotation + offset) for ``strokes``, or None.

        For each candidate rotation we get the full map of collision-free offsets
        in one shot via FFT cross-correlation (see ``_free_offsets``), then select
        per ``config.strategy``: ``origin`` keeps the offset nearest the
        ``(0,0)`` corner (ties prefer the smaller angle, so drawings stay upright
        when rotating buys nothing); ``scatter`` picks a uniformly random free
        offset for an organic spread. ``search_step_cells`` subsamples the offset
        grid (coarser = faster, fewer candidate positions).

        ``occupancy`` is the keep-out map from ``compute_occupancy`` — a boolean
        array over this region's grid cells, already grown by the general buffer.
        It's a required argument rather than something derived here because a
        single placement runs this search many times (once per rotation, and
        repeatedly while binary-searching the drawing's scale), and the map is
        identical across all of them.

        ``footprints`` is an optional ``FootprintCache`` for the drawing; the same
        queued drawing is re-tested across polls and candidate bots, and its
        rotated/rasterized footprints never change, so a shared cache avoids
        recomputing them. When omitted a throwaway cache is used (single call).

        DENSITY EXTENSION POINT (origin): ranking by the *mask's corner*
        position is a standard bottom-left-fill heuristic — good enough, not optimal,
        and it leaves the canvas fragmented once it fills. If packing density
        becomes a problem, upgrade the ranking to a true minimal-waste score: a
        contact-point metric (favour poses whose perimeter touches existing ink /
        walls) or full No-Fit-Polygon nesting. Either way the rest of the pipeline
        (footprint raster, occupancy grid, lead-in stripping) is unchanged — only
        the pose selection below changes.
        """

        if not strokes:
            return None

        cell = self.config.cell_mm
        half = int(
            math.ceil((self.config.pen_mm / 2.0 + self.config.clearance_mm) / cell)
        )
        step = max(1, self.config.search_step_cells)
        rows, cols = self.grid.shape

        if occupancy.shape != self.grid.shape:
            raise ValueError(
                f"occupancy shape {occupancy.shape} does not match region "
                f"{self.id!r} grid shape {self.grid.shape}"
            )

        if footprints is None:
            footprints = FootprintCache(strokes)

        scatter = self.config.strategy == "scatter"
        if scatter and rng is None:
            rng = random.Random()
        angles = list(self.config.angles_deg)
        if scatter:
            rng.shuffle(angles)  # type: ignore[union-attr]

        # FFT of the occupancy grid: computed once and reused for every rotation.
        # An empty region needs no correlation — every offset is free.
        fft_shape = _fft_shape((rows, cols))
        grid_fft: Optional[Spectrum] = (
            np.fft.rfft2(occupancy.astype(np.float64), fft_shape)
            if occupancy.any()
            else None
        )

        best: Optional[Placement] = None
        best_key: Optional[tuple[int, int]] = None

        for angle in angles:
            footprint, anchor_local = footprints.at(angle, cell, half)
            if footprint is None:
                return None  # empty drawing — nothing to place at any angle
            assert anchor_local is not None  # set together with footprint
            mask = footprint.mask

            mh, mw = mask.shape
            if mh > rows or mw > cols:
                continue  # this rotation can't fit in the region at all

            if grid_fft is None:
                free = np.ones((rows - mh + 1, cols - mw + 1), dtype=np.bool_)
            else:
                free = _free_offsets(grid_fft, mask, (rows, cols), fft_shape)

            coords = np.argwhere(free[::step, ::step])  # row-major: corner-first
            if coords.size == 0:
                continue

            if scatter:
                row, col = coords[rng.randrange(len(coords))]  # type: ignore[union-attr]
                pos = (int(row) * step, int(col) * step)
                return self._placement(angle, anchor_local, footprint, pos, cell)

            row, col = coords[0]  # smallest (top, left) on the step grid
            pos = (int(row) * step, int(col) * step)
            if best_key is not None and pos >= best_key:
                continue  # not closer to the corner than the current best

            best_key = pos
            best = self._placement(angle, anchor_local, footprint, pos, cell)

            if best_key == (0, 0):
                # Nothing beats the corner itself, and a later angle that merely
                # ties loses to the `pos >= best_key` test above anyway — so the
                # remaining rotations cannot change the answer. Mostly this is the
                # empty-region case, where every angle is free at (0,0) and we'd
                # otherwise rasterize all of them to learn nothing.
                break

        return best

    def _placement(
        self,
        angle: float,
        anchor_local: Point,
        footprint: Footprint,
        pos: tuple[int, int],
        cell: float,
    ) -> Placement:
        top, left = pos
        anchor_col, anchor_row = footprint.to_px(anchor_local)  # first ink point
        return Placement(
            angle_deg=angle,
            anchor_x=self.x + (left + anchor_col) * cell,
            anchor_y=self.y + (top + anchor_row) * cell,
            _top=top,
            _left=left,
            _footprint=footprint,
        )

    def commit(self, placement: Placement) -> None:
        """Stamp a placed footprint into the occupancy grid (reserves the space)."""

        mask = placement._footprint.mask
        mh, mw = mask.shape
        top, left = placement._top, placement._left
        self.grid[top : top + mh, left : left + mw] |= mask.astype(np.uint8)

    def clear(self) -> None:
        self.grid[:] = 0


def edge_yaw(x: float, y: float, width: float, height: float) -> float:
    """Yaw (radians) of a marker sitting on the canvas boundary.

    Frame is screen-like (origin top-left, +x right, +y down). Yaw is the standard
    heading of the marker's inward-facing normal — ``atan2(ny, nx)`` measured from
    +x, increasing clockwise — i.e. the direction the marker faces into the canvas:
    left (x=0) faces right -> 0, top (y=0) faces down -> +pi/2,
    right (x=width) faces left -> pi, bottom (y=height) faces up -> -pi/2. The
    nearest edge wins; a corner ties two edges, and we break ties toward the
    horizontal edge (top/bottom) — consistent with the default corner markers.
    """
    d_top, d_bottom, d_left, d_right = abs(y), abs(height - y), abs(x), abs(width - x)
    nearest = min(d_top, d_bottom, d_left, d_right)
    if nearest == d_top:
        return math.pi / 2
    if nearest == d_bottom:
        return -math.pi / 2
    if nearest == d_left:
        return 0.0
    return math.pi


@dataclass
class Marker:
    id: int
    x: float
    y: float
    size_mm: Optional[float] = None
    yaw: Optional[float] = (
        None  # radians; derived from the canvas edge, not stored config
    )


@dataclass
class Canvas:
    id: str
    width: float
    height: float
    general_buffer: int
    active_buffer: int
    markers: list[Marker] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)

    def region_for_robot(self, robot_name: str) -> Optional[Region]:
        for region in self.regions:
            if region.robot == robot_name:
                return region
        return None


class CanvasStore:
    """Holds the live canvases. Occupancy lives in the Region grids in memory."""

    def __init__(self, canvases: Optional[Iterable[Canvas]] = None) -> None:
        self._canvases: dict[str, Canvas] = {}
        for canvas in canvases or []:
            self._canvases[canvas.id] = canvas

    def all(self) -> list[Canvas]:
        return list(self._canvases.values())

    def get(self, canvas_id: str) -> Optional[Canvas]:
        return self._canvases.get(canvas_id)

    def upsert(self, canvas: Canvas) -> None:
        self._canvases[canvas.id] = canvas

    def remove(self, canvas_id: str) -> None:
        # Remove the canvas; raises KeyError if not found
        if canvas_id not in self._canvases:
            raise KeyError(canvas_id)
        del self._canvases[canvas_id]

    def all_markers(self) -> list[Marker]:
        return [m for canvas in self._canvases.values() for m in canvas.markers]

    def region_for_robot(self, robot_name: str) -> Optional[Region]:
        for canvas in self._canvases.values():
            region = canvas.region_for_robot(robot_name)
            if region is not None:
                return region
        return None

    def canvas_for_robot(self, robot_name: str) -> Optional[Canvas]:
        for canvas in self._canvases.values():
            region = canvas.region_for_robot(robot_name)
            if region is not None:
                return canvas
        return None
