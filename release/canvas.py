"""Canvas model + the bridge to the Rust placement engine.

The server owns a model of every physical canvas: its size, its aruco markers,
and the regions it is carved into (one region drawn by one robot). A new drawing
must never overlap an existing one, nor drive into a robot that is drawing next
door. Finding where it fits is the job of the ``canvas_placement`` extension
(https://github.com/mitmedialab/suede.canvas-placement); this module holds the canvas data model, the turtle
geometry the coordinator needs around a placement, and three thin wrappers that
turn these objects into engine requests:

* ``place``          — where does this drawing go in this region? (or None)
* ``free_fractions`` — how full is each region?
* ``occupancy``      — the grids a placement is searched against (tests/debug)

The engine is **stateless**. Each call rebuilds the region's occupancy grid from
the list of drawings already on the canvas, so that list (``robots.py``'s
``_drawings``) is the only record of what's been reserved. There is no grid to
keep in sync, clear, or restore after a restart.

Rotation is essentially free for a turtle path: rotating a drawing by θ is
identical to starting the pen at heading θ. So placement first strips the lead-in
(the pen-up drive to the first ink) via ``split_lead_in``, packs and rotates only
the ink, and reports the first ink point as the start pose with an approach
heading of ``θ0 + angle`` — the drawing commands themselves are sent unchanged.

No FastAPI/pydantic route models here; those live in ``robots.py``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import (
    Iterable,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeAlias,
    TypeVar,
    cast,
)

import canvas_placement as _engine
import numpy as np
import numpy.typing as npt
from pydantic import BaseModel


@dataclass
class PlacedDrawing:
    job_id: str
    robot_name: str
    anchor_x: float
    anchor_y: float
    angle_deg: float
    commands: list
    strokes: list
    exit_pose_x: float
    exit_pose_y: float
    exit_pose_deg: float


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
"""A boolean raster over a region's grid cells — ``True`` = covered/blocked."""

ROBOT_LENGTH_MM: float = 226.0
"""Nose to tail, along the direction of travel."""

ROBOT_WIDTH_MM: float = 149.0
"""Across the direction of travel."""

ROBOT_PEN_FROM_NOSE_MM: float = 76.0
"""Nose to pen centre.

Well forward of centre, which is the whole reason the body model is worth having:
only 76mm of robot leads the ink, while 150mm of it trails behind.
"""


@dataclass(frozen=True)
class RobotBody:
    """A robot's physical footprint, relative to its pen.

    A robot mid-drawing is a body, not a point — and its ink tells us both where
    its pen is and, from the stroke's direction, which way it is pointing. The
    engine blocks the space the chassis actually sweeps along a neighbour's live
    strokes, which is far tighter than "somewhere within body-length of the pen".
    """

    length_mm: float = ROBOT_LENGTH_MM
    width_mm: float = ROBOT_WIDTH_MM
    pen_from_nose_mm: float = ROBOT_PEN_FROM_NOSE_MM

    @property
    def pen_from_tail_mm(self) -> float:
        """How much body trails behind the pen. The dominant direction, here."""
        return self.length_mm - self.pen_from_nose_mm

    @property
    def reach_mm(self) -> float:
        """Farthest any part of the body can sit from the pen, over all headings.

        The far tail corner, so ``hypot(pen_from_tail, width/2)``. The body is
        always *inside* ``disc(pen, reach_mm)``, so a pen further than this from an
        obstacle is safe no matter which way the robot points.
        """
        along = max(self.pen_from_nose_mm, self.pen_from_tail_mm)
        return math.hypot(along, self.width_mm / 2.0)

    @property
    def min_overhang_mm(self) -> float:
        """Nearest any part of the body's *edge* can sit to the pen, over all headings.

        The pen sits inside the chassis rectangle, so the disc of this radius is
        inscribed in the body — the body *contains* ``disc(pen, min_overhang_mm)``
        at every heading. Within it a collision is certain whichever way the robot
        points, which makes it the engine's broad-phase bound.
        """
        return min(self.pen_from_nose_mm, self.pen_from_tail_mm, self.width_mm / 2.0)


ROBOT_BODY = RobotBody()
"""The fleet's body geometry."""


class Pose(BaseModel):
    """A position plus a heading (degrees, CCW positive, matching the vectorizer)."""

    x: float
    y: float
    headingDegrees: float = 0.0


# --------------------------------------------------------------------------- #
# Geometry: drawing commands -> pen-down strokes
# --------------------------------------------------------------------------- #


def commands_to_strokes(
    commands: Sequence[Command], arc_step_deg: float = 6.0
) -> list[Stroke]:
    strokes, _ = commands_to_strokes_with_pose(
        commands, Pose(x=0, y=0, headingDegrees=0), arc_step_deg
    )
    return strokes


def commands_to_strokes_with_pose(
    commands: Sequence[Command], start: Pose, arc_step_deg: float = 6.0
) -> tuple[list[Stroke], Pose]:
    """Same turtle-integration as commands_to_strokes, but also returns the
    final local pose (x, y, headingDegrees). Uses the exact same math as the
    stroke generation (including chord-flattened arcs), so the returned pose
    is guaranteed consistent with the last point of the last stroke.

    The Rust engine integrates commands the same way (``placement/src/geometry.rs``);
    keep the two in step."""

    x, y, heading = start.x, start.y, start.headingDegrees
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
                flush()
            x, y = nx, ny
        elif cmd.kind == "spin":
            heading += cmd.degrees
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
    return strokes, Pose(x=x, y=y, headingDegrees=heading)


def replay_to_world(
    commands: Sequence[Command], start_x: float, start_y: float, heading_deg: float
) -> list[Stroke]:
    """Run the (lead-in-stripped) commands from the given start pose and return
    pen-down polylines in global mm — the ink the robot actually lays down."""
    local = commands_to_strokes(commands)  # local frame, first ink at (0,0)
    rad = math.radians(heading_deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)
    return [
        [
            (px * cos_t - py * sin_t + start_x, px * sin_t + py * cos_t + start_y)
            for px, py in stroke
        ]
        for stroke in local
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
# Canvas model
# --------------------------------------------------------------------------- #


@dataclass
class PlacementConfig:
    cell_mm: float = 5.0
    """Occupancy-grid resolution. ~2mm is sub-pen-width; finer just costs memory."""

    pen_mm: float = 5.0
    """Drawn line width."""

    clearance_mm: float = 8.0
    """Keep-out margin enforced between separate drawings."""

    search_step_cells: int = 2
    """Stride of the sliding-window search (in cells). Larger = faster, coarser."""

    angles_deg: tuple[float, ...] = tuple(float(a) for a in range(0, 360, 15))
    """Candidate rotations, tried in order (0° first = prefer upright)."""

    strategy: Literal["origin", "scatter"] = "scatter"
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
    """Desired size of a placed drawing given ample free space: the drawing is
    uniformly scaled (preserving aspect ratio) so its *longest* footprint
    dimension is this many mm, then shrunk below it only if it won't fit.
    """

    min_footprint_scale: float = 0.4
    """Floor for the shrink-to-fit fallback, as a fraction of ``target_footprint_mm``.
    A drawing that won't fit even at this size is left queued rather than drawn
    illegibly small.
    """

    scatter_candidates: int = 8
    """``scatter`` only: draw this many valid poses and keep the one with the most
    room around it (best-candidate sampling). 1 is a plain uniform pick, under
    which drawings often land right beside each other with open space elsewhere;
    8 spreads them out while still looking hand-placed, at about the same
    capacity when the canvas fills up.
    """

    edge_weight: float = 0.25
    """``scatter`` with more than one candidate: how much the region's edges count
    as obstacles when measuring that room — 0 not at all (drawings end up on the
    edges as often as with a plain pick), 1 as much as ink (the edges dominate and
    the spreading mostly vanishes). 0.25 keeps drawings off the edges and keeps
    most of the spreading.
    """


@dataclass
class Placement:
    """Where a drawing goes, as found by ``place``."""

    angle_deg: float
    """Rotation on top of the drawing's own start heading (``heading0``)."""
    anchor_x: float  # global mm — where the first ink point (P0) lands
    anchor_y: float
    scale: float
    """Absolute scale applied to the drawing's commands (distance, radius)."""
    scale_fraction: float = 1.0
    """``scale`` as a fraction of the region's target size (1.0 = full size)."""
    attempts: int = 1
    """Placement searches run (1 = fitted at full size, more = shrink-to-fit)."""
    free_fraction_after: float = 1.0
    """The region's free fraction once this drawing is added."""


@dataclass
class Region:
    """A rectangular area of a canvas drawn by one robot.

    Plain data: what's been drawn here lives in the canvas's drawings list, and
    the engine derives the occupancy from that on every call.
    """

    id: str
    x: float  # min corner in global/canvas mm
    y: float
    width: float
    height: float
    robot: Optional[str] = None
    color: Optional[str] = "#000"
    config: PlacementConfig = field(default_factory=PlacementConfig)


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

    general_buffer: float
    """mm of clearance to leave between separate drawings."""

    markers: list[Marker] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)

    def region_for_robot(self, robot_name: str) -> Optional[Region]:
        for region in self.regions:
            if region.robot == robot_name:
                return region
        return None


class CanvasStore:
    """Holds the live canvases (layout only; drawings live with the coordinator)."""

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


# --------------------------------------------------------------------------- #
# The placement engine (Rust) — request builders and wrappers
# --------------------------------------------------------------------------- #

ActiveDrawings: TypeAlias = Mapping[str, Optional[Sequence[Stroke]]]
"""Robot name -> strokes (global mm) it is drawing right now, or None."""


def _canvas_spec(canvas: Canvas) -> dict:
    return {
        "width": canvas.width,
        "height": canvas.height,
        "general_buffer": float(canvas.general_buffer),
        "regions": [
            {
                "id": r.id,
                "x": r.x,
                "y": r.y,
                "width": r.width,
                "height": r.height,
                "robot": r.robot,
                "config": {
                    "cell_mm": r.config.cell_mm,
                    "pen_mm": r.config.pen_mm,
                    "clearance_mm": r.config.clearance_mm,
                    "search_step_cells": r.config.search_step_cells,
                    "angles_deg": list(r.config.angles_deg),
                    "strategy": r.config.strategy,
                    "target_footprint_mm": r.config.target_footprint_mm,
                    "min_footprint_scale": r.config.min_footprint_scale,
                    "scatter_candidates": r.config.scatter_candidates,
                    "edge_weight": r.config.edge_weight,
                },
            }
            for r in canvas.regions
        ],
    }


def _drawing_specs(drawings: Iterable[PlacedDrawing]) -> list[dict]:
    """Each drawing is reserved in its robot's region, at its recorded ink."""
    return [{"robot_name": d.robot_name, "strokes": d.strokes} for d in drawings]


def _command_spec(cmd: Command) -> dict:
    if cmd.kind == "line":
        return {"kind": "line", "distance": cmd.distance, "penDown": cmd.penDown}
    if cmd.kind == "spin":
        return {"kind": "spin", "degrees": cmd.degrees}
    if cmd.kind == "arc":
        return {"kind": "arc", "radius": cmd.radius, "degrees": cmd.degrees}
    raise ValueError(f"Unknown drawing command kind: {cmd.kind!r}")


def _body_spec(body: RobotBody) -> dict:
    return {
        "length_mm": body.length_mm,
        "width_mm": body.width_mm,
        "pen_from_nose_mm": body.pen_from_nose_mm,
    }


def place(
    canvas: Canvas,
    region: Region,
    drawings: Iterable[PlacedDrawing],
    commands: Sequence[Command],
    heading0: float,
    native_span: float,
    *,
    active: Optional[ActiveDrawings] = None,
    seed: int = 0,
    budget_s: Optional[float] = None,
    robot_body: RobotBody = ROBOT_BODY,
    scale_tol: float = 0.02,
    max_iters: int = 12,
) -> Optional[Placement]:
    """Find a pose for a drawing in ``region``, or None if it fits nowhere.

    ``commands`` is the lead-in-stripped drawing (see ``split_lead_in``) and
    ``heading0`` its start heading; ``native_span`` is the longest side of its
    unscaled ink. The drawing is sized to the region's ``target_footprint_mm``
    and placed at full size if it fits, otherwise shrunk as little as possible
    (down to ``min_footprint_scale``); ``budget_s`` caps that shrink search, the
    full-size attempt always runs.

    The pose is collision-free twice over: our ink clears every drawing in
    ``drawings`` (on this region) by the canvas's ``general_buffer``, and our
    chassis clears every robot in ``active`` drawing in an adjoining region.

    Stateless: ``drawings`` is everything already on the canvas, and nothing is
    remembered between calls. The same arguments and ``seed`` give the same
    answer.
    """
    result = _engine.place(
        {
            "canvas": _canvas_spec(canvas),
            "drawings": _drawing_specs(drawings),
            "active": dict(active or {}),
            "robot_body": _body_spec(robot_body),
            "region_id": region.id,
            "job": {
                "commands": [_command_spec(c) for c in commands],
                "heading0": heading0,
                "native_span": native_span,
            },
            "seed": seed,
            "budget_ms": None if budget_s is None else max(0.0, budget_s * 1000.0),
            "scale_tol": scale_tol,
            "max_iters": max_iters,
        }
    )
    return None if result is None else Placement(**result)


def free_fractions(
    canvas: Canvas, drawings: Iterable[PlacedDrawing]
) -> dict[str, float]:
    """Fraction of each region's cells not covered by ink, keyed by region id."""
    return _engine.free_fractions(
        {"canvas": _canvas_spec(canvas), "drawings": _drawing_specs(drawings)}
    )


@dataclass
class Occupancy:
    """One region's grids as the engine sees them, each ``(rows, cols)`` bool."""

    ink: BoolMask
    """Committed ink."""
    buffered: BoolMask
    """Ink grown by the canvas's ``general_buffer``."""
    active: BoolMask
    """Live neighbours' swept bodies grown by ``max(general_buffer,
    robot_body.min_overhang_mm)`` — the poses certain to collide."""
    bodies: BoolMask
    """Live neighbours' swept bodies, ungrown."""
    keepout: BoolMask
    """What a candidate footprint is tested against: ``buffered | active``."""


def occupancy(
    canvas: Canvas,
    region: Region,
    drawings: Iterable[PlacedDrawing],
    active: Optional[ActiveDrawings] = None,
    robot_body: RobotBody = ROBOT_BODY,
) -> Occupancy:
    """The grids a placement in ``region`` would be searched against.

    For tests, debugging and visualization; ``place`` builds the same grids
    internally."""
    raw = _engine.occupancy(
        {
            "canvas": _canvas_spec(canvas),
            "drawings": _drawing_specs(drawings),
            "active": dict(active or {}),
            "robot_body": _body_spec(robot_body),
            "region_id": region.id,
        }
    )
    shape = (raw["rows"], raw["cols"])

    def grid(name: str) -> BoolMask:
        return np.frombuffer(raw[name], dtype=np.uint8).reshape(shape).astype(bool)

    return Occupancy(
        ink=grid("ink"),
        buffered=grid("buffered"),
        active=grid("active"),
        bodies=grid("bodies"),
        keepout=grid("keepout"),
    )
