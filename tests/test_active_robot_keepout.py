"""Visual + behavioural check of the *active robot* keep-out.

What this exercises
-------------------
``tests/test_compute_occupancy.py`` covers the ``general_buffer`` growth around
committed ink. This file covers the other source of keep-out: a **neighbouring
robot that is drawing right now**. That robot is not ink to steer around, it's a
moving body, and its stroke tells us both where its pen is *and* which way it is
pointing — so the engine blocks the space the chassis actually sweeps
(``RobotBody``: 226mm nose-to-tail, 149mm wide, pen 76mm back from the nose)
rather than a disc around every ink point.

The interesting part is *which* robots count. For an active robot to matter it
must be on **our canvas** and drawing in a region that **shares an edge** with
ours. Everything else is out of the picture:

* a robot on another canvas entirely (even at identical coordinates)
* a region that only meets ours at a corner — nothing reaches through a point
* a robot drawing further than the body's reach from our region
* a robot that isn't drawing (``robots.py`` parks a ``None`` in the dict)

Two canvases exist here precisely so the "shares a canvas" gate is demonstrable
rather than asserted: the ``other_canvas`` scenario feeds in *the very same
strokes* as ``east_neighbour``, and you can see them block ``annex/west`` while
leaving ``studio/tl`` untouched.

Layout (all mm, global canvas frame)::

    studio (1000x1000)              annex (1000x500)
    +-----------+-----------+       +-----------+-----------+
    |    tl     |    tr     |       |   west    |   east    |
    |  (botA)   |  (botB)   |       |  (botE)   |  (botF)   |
    +-----------+-----------+       +-----------+-----------+
    |    bl     |    br     |
    |  (botC)   |  (botD)   |
    +-----------+-----------+

``tl`` is the subject on ``studio``, ``west`` on ``annex``. Relative to ``tl``:
``tr`` and ``bl`` share an edge, ``br`` only touches at a corner.

The grids come from the engine itself (``canvas.occupancy``), the same ones a
placement is searched against; the placements come from ``canvas.place``.

Output lands in ``tests/output/active_robot_keepout/`` — one PNG per scenario,
both canvases side by side:

* black    — committed ink
* orange   — keep-out grown from that ink by ``general_buffer``
* red      — keep-out added because a neighbouring robot is drawing there
* blue     — the active robot's strokes
* the subject region is the one with the heavy border; red only ever appears
  inside it, because occupancy is always computed *for* one region

Run it directly for the visuals (``python tests/test_active_robot_keepout.py``)
or under pytest. Both produce the PNGs.
"""

from __future__ import annotations

import glob
import json
import math
import os
import sys
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Allow running as a plain script: make the repo root importable so ``release``
# resolves. Under pytest the root is already on the path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release.canvas import (  # noqa: E402
    ROBOT_BODY,
    Canvas,
    Occupancy,
    PlacedDrawing,
    Placement,
    PlacementConfig,
    Point,
    Region,
    RobotBody,
    Stroke,
    commands_to_strokes,
    occupancy,
    place,
    replay_to_world,
    split_lead_in,
)

VEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectorizations")

OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "active_robot_keepout"
)

CELL_MM = 2.0
GENERAL_BUFFER = 10  # kept small so the red (active) keep-out reads separately

# Colours, all chosen to stay distinguishable where they overlap.
C_FREE = (255, 255, 255)
C_INK = (20, 20, 20)
C_BUFFER = (255, 176, 80)  # keep-out grown from committed ink
C_ACTIVE = (226, 74, 74)  # keep-out grown from a live robot
C_STROKE = (40, 90, 220)  # the live robot's strokes
C_BORDER = (170, 170, 170)
C_SUBJECT = (60, 60, 60)
C_TEXT = (20, 20, 20)


# --------------------------------------------------------------------------- #
# Source drawings: the real vectorizations in tests/vectorizations
# --------------------------------------------------------------------------- #


@dataclass
class Source:
    """One vectorization, prepared the way the coordinator prepares a job."""

    name: str
    commands: list  # lead-in stripped
    heading0: float
    strokes: list[Stroke]  # native ink, local mm

    @property
    def span(self) -> float:
        x0, y0, x1, y1 = _span(self.strokes)
        return max(x1 - x0, y1 - y0)


def _load_drawings() -> list[Source]:
    """Every ``*.json`` in tests/vectorizations.

    These are the genuine article — turtle command lists straight from the
    vectorizer, arcs and all — run through the same ``split_lead_in`` +
    ``commands_to_strokes`` path the coordinator uses. Ink geometry matters here in
    a way it doesn't for the buffer: the keep-out is swept along each segment, so a
    real drawing's arcs and direction reversals exercise the sweep in ways a hand
    -drawn box never would.
    """
    drawings: list[Source] = []
    for path in sorted(glob.glob(os.path.join(VEC_DIR, "*.json"))):
        with open(path) as fh:
            raw = json.load(fh)
        # SimpleNamespace duck-types canvas.py's Command protocol (.kind/.distance/
        # .penDown/.degrees/.radius) without dragging robots.py's pydantic in.
        commands = [SimpleNamespace(**c) for c in raw]
        _lead_in, drawing, _start, heading0 = split_lead_in(commands)
        strokes = commands_to_strokes(drawing)
        if strokes:
            name = os.path.splitext(os.path.basename(path))[0].split(".")[0][:24]
            drawings.append(Source(name, drawing, heading0, strokes))
    return drawings


DRAWINGS = _load_drawings()


def _span(strokes: list[Stroke]) -> tuple[float, float, float, float]:
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _fit(strokes: list[Stroke], target_mm: float) -> list[Stroke]:
    """Uniformly scale so the drawing's longest side is ``target_mm``."""
    x0, y0, x1, y1 = _span(strokes)
    longest = max(x1 - x0, y1 - y0)
    f = target_mm / longest if longest else 1.0
    return [[((px - x0) * f, (py - y0) * f) for px, py in s] for s in strokes]


def _at(strokes: list[Stroke], x: float, y: float) -> list[Stroke]:
    """Translate so the drawing's top-left corner lands at global ``(x, y)``."""
    x0, y0, _, _ = _span(strokes)
    return [[(px - x0 + x, py - y0 + y) for px, py in s] for s in strokes]


def _reversed(strokes: list[Stroke]) -> list[Stroke]:
    """Same ink, driven the other way round — so every heading flips."""
    return [list(reversed(s)) for s in reversed(strokes)]


# --------------------------------------------------------------------------- #
# Active strokes — GLOBAL canvas mm, the frame robots.py records them in
# --------------------------------------------------------------------------- #

ACTIVE_MM = 190.0  # footprint the live robot's drawing is sized to

_D0 = _fit(DRAWINGS[0].strokes, ACTIVE_MM)
_D1 = _fit(DRAWINGS[1].strokes, ACTIVE_MM)
_D2 = _fit(DRAWINGS[2].strokes, ACTIVE_MM)

# A real drawing just east of the x=500 divide: in `tr` on studio, `east` on annex.
NEAR_EDGE: list[Stroke] = _at(_D0, 515.0, 60.0)
# A real drawing just south of the y=500 divide: in `bl` on studio.
SOUTH_EDGE: list[Stroke] = _at(_D1, 60.0, 515.0)
# Deep inside `br`, which only touches `tl` at the (500,500) corner.
CORNER_ONLY: list[Stroke] = _at(_D2, 530.0, 530.0)
# In `tr`, but far enough right that no part of the chassis can reach `tl`.
OUT_OF_REACH: list[Stroke] = _at(_D0, 800.0, 60.0)
# The same real drawing driven in reverse. Identical ink, every heading flipped —
# so a disc model would give an identical keep-out and the swept body must not.
NEAR_EDGE_REVERSED: list[Stroke] = _reversed(NEAR_EDGE)


# --------------------------------------------------------------------------- #
# Canvas setup
# --------------------------------------------------------------------------- #


@dataclass
class World:
    """A canvas plus what's been drawn on it — everything the engine is given."""

    canvas: Canvas
    drawings: list[PlacedDrawing] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.canvas.id

    def region(self, region_id: str) -> Region:
        return next(r for r in self.canvas.regions if r.id == region_id)


def _cfg() -> PlacementConfig:
    # ``origin`` keeps every placement in this file deterministic.
    return PlacementConfig(cell_mm=CELL_MM, strategy="origin")


COMMIT_MM = 200.0  # footprint the committed drawings are sized to


def _scaled(commands: list, scale: float) -> list:
    """robots.py ``scale_commands``: lengths scale, angles don't."""
    out = []
    for c in commands:
        if c.kind == "line":
            out.append(SimpleNamespace(kind="line", distance=c.distance * scale, penDown=c.penDown))
        elif c.kind == "arc":
            out.append(SimpleNamespace(kind="arc", radius=c.radius * scale, degrees=c.degrees))
        else:
            out.append(c)
    return out


def _world_strokes(source: Source, p: Placement) -> list[Stroke]:
    """The ink the robot lays down for this placement (robots.py ``add_drawing``)."""
    return replay_to_world(
        _scaled(source.commands, p.scale), p.anchor_x, p.anchor_y, source.heading0 + p.angle_deg
    )


def _place(
    world: World,
    region: Region,
    source: Source,
    size_mm: float,
    active: Optional[dict] = None,
    seed: int = 0,
) -> Optional[tuple[Placement, list[Stroke]]]:
    """Ask the engine to place ``source`` (sized to ``size_mm``) in ``region``.

    Returns the placement and its world-mm ink, or None. Doesn't record it.
    """
    sized = replace(region, config=replace(region.config, target_footprint_mm=size_mm))
    canvas = replace(
        world.canvas, regions=[sized if r.id == region.id else r for r in world.canvas.regions]
    )
    p = place(
        canvas, sized, world.drawings, source.commands, source.heading0, source.span,
        active=active, seed=seed,
    )
    return None if p is None else (p, _world_strokes(source, p))


def _record(world: World, region: Region, p: Placement, strokes: list[Stroke]) -> None:
    world.drawings.append(
        PlacedDrawing(
            job_id=f"job{len(world.drawings)}",
            robot_name=region.robot or "",
            anchor_x=p.anchor_x,
            anchor_y=p.anchor_y,
            angle_deg=p.angle_deg,
            commands=[],
            strokes=strokes,
            exit_pose_x=0.0,
            exit_pose_y=0.0,
            exit_pose_deg=0.0,
        )
    )


def _commit(world: World, region_id: str, sources: list[Source]) -> None:
    """Put real drawings into a region via the actual placement search.

    Not hand-placed: each drawing is sized to ``COMMIT_MM`` and run through
    ``place``, so the region ends up holding ink at poses the search genuinely
    chose. That makes the committed side of the keep-out real too.
    """
    region = world.region(region_id)
    for source in sources:
        hit = _place(world, region, source, COMMIT_MM)
        assert hit is not None, f"a {COMMIT_MM:g}mm drawing would not fit {region.id}"
        _record(world, region, *hit)


def _studio(with_ink: bool = True) -> World:
    """1000x1000 carved into four 500x500 quadrants. Subject is ``tl``.

    ``with_ink=False`` leaves the regions empty, so the keep-out is the
    active-robot layer and nothing else — which is what the geometry tests want
    to compare against ground truth.
    """
    quads = [
        ("tl", 0.0, 0.0, "botA"),
        ("tr", 500.0, 0.0, "botB"),
        ("bl", 0.0, 500.0, "botC"),
        ("br", 500.0, 500.0, "botD"),
    ]
    world = World(
        Canvas(
            id="studio",
            width=1000.0,
            height=1000.0,
            general_buffer=GENERAL_BUFFER,
            regions=[
                Region(id=i, x=x, y=y, width=500.0, height=500.0, robot=bot, config=_cfg())
                for i, x, y, bot in quads
            ],
        )
    )
    if with_ink:
        _commit(world, "tl", [DRAWINGS[0], DRAWINGS[2]])
    return world


def _annex(with_ink: bool = True) -> World:
    """1000x500 split into two 500x500 halves. Subject is ``west``.

    Deliberately overlaps ``studio``'s coordinate space: ``east`` covers the same
    mm as ``studio/tr``, so the same strokes are meaningful on both canvases and
    the canvas gate is the only thing telling them apart.
    """
    world = World(
        Canvas(
            id="annex",
            width=1000.0,
            height=500.0,
            general_buffer=GENERAL_BUFFER,
            regions=[
                Region(id="west", x=0.0, y=0.0, width=500.0, height=500.0, robot="botE", config=_cfg()),
                Region(id="east", x=500.0, y=0.0, width=500.0, height=500.0, robot="botF", config=_cfg()),
            ],
        )
    )
    if with_ink:
        _commit(world, "west", [DRAWINGS[1]])
    return world


SUBJECT_OF = {"studio": "tl", "annex": "west"}


def _subject(world: World) -> Region:
    return world.region(SUBJECT_OF[world.id])


def _occ(world: World, active: dict, region: Optional[Region] = None) -> Occupancy:
    return occupancy(world.canvas, region or _subject(world), world.drawings, active)


# --------------------------------------------------------------------------- #
# Scenarios
# --------------------------------------------------------------------------- #

# ``expect`` is per canvas id: does the *active robot* layer block anything in
# that canvas's subject region?
SCENARIOS: list[dict] = [
    {
        "name": "idle",
        "title": "idle: nobody drawing -> only the general_buffer halo",
        "active": {},
        "expect": {"studio": "clear", "annex": "clear"},
    },
    {
        "name": "east_neighbour",
        "title": "botB (studio/tr) drawing beside the shared edge -> tl blocked",
        "active": {"botB": NEAR_EDGE},
        "expect": {"studio": "blocked", "annex": "clear"},
    },
    {
        "name": "east_neighbour_reversed",
        "title": "SAME drawing driven in reverse -> different keep-out (heading matters)",
        "active": {"botB": NEAR_EDGE_REVERSED},
        "expect": {"studio": "blocked", "annex": "clear"},
    },
    {
        "name": "south_neighbour",
        "title": "botC (studio/bl) drawing beside the shared edge -> tl blocked",
        "active": {"botC": SOUTH_EDGE},
        "expect": {"studio": "blocked", "annex": "clear"},
    },
    {
        "name": "both_neighbours",
        "title": "botB + botC both drawing -> tl keeps out the union",
        "active": {"botB": NEAR_EDGE, "botC": SOUTH_EDGE},
        "expect": {"studio": "blocked", "annex": "clear"},
    },
    {
        "name": "corner_only",
        "title": "botD (studio/br) meets tl at a corner only -> no reach",
        "active": {"botD": CORNER_ONLY},
        "expect": {"studio": "clear", "annex": "clear"},
    },
    {
        "name": "out_of_reach",
        "title": f"botB drawing >{ROBOT_BODY.reach_mm:.0f}mm away -> out of reach",
        "active": {"botB": OUT_OF_REACH},
        "expect": {"studio": "clear", "annex": "clear"},
    },
    {
        "name": "other_canvas",
        "title": "botF (annex/east): SAME strokes as east_neighbour, other canvas",
        "active": {"botF": NEAR_EDGE},
        "expect": {"studio": "clear", "annex": "blocked"},
    },
    {
        "name": "idle_none",
        "title": "botB present but not drawing (None) -> nothing",
        "active": {"botB": None},
        "expect": {"studio": "clear", "annex": "clear"},
    },
]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _layers(world: World, active: dict) -> tuple[np.ndarray, np.ndarray]:
    """(keep-out from committed ink, keep-out added by live robots) for the subject."""
    occ = _occ(world, active)
    return occ.buffered, occ.keepout & ~occ.buffered


def _canvas_image(world: World, active: dict) -> Image.Image:
    """One canvas, rendered at cell resolution in the global mm frame."""
    canvas = world.canvas
    h = int(math.ceil(canvas.height / CELL_MM))
    w = int(math.ceil(canvas.width / CELL_MM))
    rgb = np.full((h, w, 3), C_FREE, dtype=np.uint8)

    subject = _subject(world)
    static, from_active = _layers(world, active)

    def offset(region: Region) -> tuple[int, int]:
        return int(round(region.y / CELL_MM)), int(round(region.x / CELL_MM))

    # Keep-out layers live only in the subject region.
    r0, c0 = offset(subject)
    rows, cols = static.shape
    view = rgb[r0 : r0 + rows, c0 : c0 + cols]
    view[static] = C_BUFFER
    view[from_active] = C_ACTIVE

    # Committed ink for every region, drawn over the halos it produced.
    for region in canvas.regions:
        rr, cc = offset(region)
        ink = _occ(world, {}, region).ink
        rgb[rr : rr + ink.shape[0], cc : cc + ink.shape[1]][ink] = C_INK

    img = Image.fromarray(rgb, mode="RGB")
    draw = ImageDraw.Draw(img)

    # The live strokes, but only for robots that actually live on this canvas —
    # otherwise the picture would imply a reach the engine ignores.
    for name, strokes in active.items():
        if not strokes or canvas.region_for_robot(name) is None:
            continue
        for stroke in strokes:
            pts = [(px / CELL_MM, py / CELL_MM) for px, py in stroke]
            if len(pts) >= 2:
                draw.line(pts, fill=C_STROKE, width=2)
            elif pts:
                draw.point(pts[0], fill=C_STROKE)

    # Region borders; the subject gets the heavy one.
    for region in canvas.regions:
        box = [
            region.x / CELL_MM,
            region.y / CELL_MM,
            (region.x + region.width) / CELL_MM - 1,
            (region.y + region.height) / CELL_MM - 1,
        ]
        is_subject = region.id == subject.id
        draw.rectangle(
            box,
            outline=C_SUBJECT if is_subject else C_BORDER,
            width=3 if is_subject else 1,
        )

    return img


def _render_scenario(scenario: dict, worlds: list[World]) -> str:
    """Both canvases side by side, titled, into one PNG."""
    font = ImageFont.load_default()
    images = [(wd, _canvas_image(wd, scenario["active"])) for wd in worlds]

    gap, pad, header, caption = 24, 12, 30, 18
    body_w = sum(img.width for _, img in images) + gap * (len(images) - 1)
    body_h = max(img.height for _, img in images)
    sheet = Image.new(
        "RGB", (body_w + 2 * pad, header + caption + body_h + 2 * pad), (245, 245, 245)
    )
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, pad), scenario["title"], fill=C_TEXT, font=font)
    draw.text(
        (pad, pad + 12),
        "black=ink | orange=general_buffer | red=active-robot keep-out | blue=live strokes",
        fill=(110, 110, 110),
        font=font,
    )

    x = pad
    for wd, img in images:
        verdict = scenario["expect"][wd.id]
        draw.text(
            (x, pad + header),
            f"{wd.id} (subject: {_subject(wd).id}) - {verdict}",
            fill=C_TEXT,
            font=font,
        )
        sheet.paste(img, (x, pad + header + caption))
        x += img.width + gap

    path = os.path.join(OUT_DIR, f"scenario_{scenario['name']}.png")
    sheet.save(path)
    return path


# --------------------------------------------------------------------------- #
# The demo
# --------------------------------------------------------------------------- #


def run_demo() -> dict:
    os.makedirs(OUT_DIR, exist_ok=True)
    worlds = [_studio(), _annex()]

    results = []
    for scenario in SCENARIOS:
        path = _render_scenario(scenario, worlds)
        per_canvas = {}
        for wd in worlds:
            static, from_active = _layers(wd, scenario["active"])
            per_canvas[wd.id] = {
                "buffer_cells": int(static.sum()),
                "active_cells": int(from_active.sum()),
                "expect": scenario["expect"][wd.id],
            }
        results.append({"name": scenario["name"], "png": path, "canvases": per_canvas})

    return {"out_dir": OUT_DIR, "scenarios": results}


# --------------------------------------------------------------------------- #
# Independent geometry (ground truth for the engine's grids)
# --------------------------------------------------------------------------- #


def _shape(region: Region) -> tuple[int, int]:
    cell = region.config.cell_mm
    return int(math.ceil(region.height / cell)), int(math.ceil(region.width / cell))


def _brute_sweep(region: Region, strokes: list[Stroke], body: RobotBody) -> np.ndarray:
    """Ground truth: a cell is covered iff its corner lies under some body pose.

    Derived from the geometry directly, so it's an independent check. Working in
    the heading frame, the body at pen position P covers offsets ``along in
    [-pen_from_tail, +pen_from_nose]`` and ``|across| <= width/2``; sweeping P
    along the segment just widens the along-interval by the segment's length.
    """
    cell = region.config.cell_mm
    rows, cols = _shape(region)
    rr, cc = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    px = region.x + cc * cell
    py = region.y + rr * cell

    covered = np.zeros((rows, cols), dtype=bool)
    for stroke in strokes:
        for a, b in zip(stroke[:-1], stroke[1:]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            seg = math.hypot(dx, dy)
            if seg == 0:
                continue
            ux, uy = dx / seg, dy / seg
            nx, ny = -uy, ux
            ax, ay = px - a[0], py - a[1]
            along = ax * ux + ay * uy
            across = ax * nx + ay * ny
            covered |= (
                (along >= -body.pen_from_tail_mm)
                & (along <= seg + body.pen_from_nose_mm)
                & (np.abs(across) <= body.width_mm / 2.0)
            )
    return covered


def _body_rect(a: Point, b: Point, body: RobotBody, margin: float) -> Optional[list[Point]]:
    """The rectangle our chassis sweeps while the pen goes a -> b, inflated by
    ``margin`` — the same model the engine uses, written out independently."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    dist = math.hypot(dx, dy)
    if dist == 0.0:
        return None
    ux, uy = dx / dist, dy / dist
    nx, ny = -uy, ux
    back = body.pen_from_tail_mm + margin
    front = body.pen_from_nose_mm + margin
    rx, ry = a[0] - ux * back, a[1] - uy * back
    fx, fy = b[0] + ux * front, b[1] + uy * front
    hw = body.width_mm / 2.0 + margin
    return [
        (rx + nx * hw, ry + ny * hw),
        (fx + nx * hw, fy + ny * hw),
        (fx - nx * hw, fy - ny * hw),
        (rx - nx * hw, ry - ny * hw),
    ]


def _swept(region: Region, strokes: list[Stroke], body: RobotBody) -> np.ndarray:
    """Our own body swept along ``strokes`` (global mm), in region cells (PIL)."""
    rows, cols = _shape(region)
    cell = region.config.cell_mm
    img = Image.new("1", (cols, rows), 0)
    d = ImageDraw.Draw(img)
    for s in strokes:
        for a, b in zip(s[:-1], s[1:]):
            poly = _body_rect(a, b, body, cell)
            if poly:
                d.polygon(
                    [((p[0] - region.x) / cell, (p[1] - region.y) / cell) for p in poly],
                    fill=1,
                )
    return np.array(img, dtype=bool)


def _ink(region: Region, strokes: list[Stroke]) -> np.ndarray:
    """What a placement effectively tests: the pen path plus pen/2 + clearance."""
    cfg = region.config
    rows, cols = _shape(region)
    img = Image.new("1", (cols, rows), 0)
    d = ImageDraw.Draw(img)
    w = int(round(2 * (cfg.pen_mm / 2 + cfg.clearance_mm) / cfg.cell_mm)) + 1
    for s in strokes:
        d.line(
            [((p[0] - region.x) / cfg.cell_mm, (p[1] - region.y) / cfg.cell_mm) for p in s],
            fill=1,
            width=w,
        )
    return np.array(img, dtype=bool)


# --------------------------------------------------------------------------- #
# pytest entry points
# --------------------------------------------------------------------------- #


def test_renders_every_scenario():
    """The visual harness: every scenario renders and matches its stated verdict."""
    result = run_demo()
    assert len(result["scenarios"]) == len(SCENARIOS)

    for scenario in result["scenarios"]:
        assert os.path.exists(scenario["png"]), scenario
        for canvas_id, stats in scenario["canvases"].items():
            blocked = stats["active_cells"] > 0
            want = stats["expect"] == "blocked"
            assert blocked == want, (
                f"{scenario['name']} / {canvas_id}: active keep-out "
                f"{'appeared' if blocked else 'did not appear'} but scenario "
                f"expects {stats['expect']} ({stats['active_cells']} cells)"
            )


def test_adjacency_is_what_gates_reach():
    """A neighbour sharing an edge reaches us; one sharing only a corner cannot —
    identical ink, one region over, opposite outcome."""
    studio = _studio()
    _, from_edge = _layers(studio, {"botB": NEAR_EDGE})
    _, from_south_edge = _layers(studio, {"botC": SOUTH_EDGE})
    _, from_corner = _layers(studio, {"botD": CORNER_ONLY})
    assert from_edge.any()
    assert from_south_edge.any()
    assert not from_corner.any()


def test_same_strokes_only_block_their_own_canvas():
    """The canvas gate: identical strokes, two canvases, only the owner is affected.

    ``annex/east`` covers the same mm as ``studio/tr``, so coordinates alone can't
    distinguish these — only "is this robot on my canvas" can.
    """
    studio, annex = _studio(), _annex()

    # botB lives on studio, botF on annex. Same strokes either way.
    _, studio_from_botB = _layers(studio, {"botB": NEAR_EDGE})
    _, studio_from_botF = _layers(studio, {"botF": NEAR_EDGE})
    _, annex_from_botF = _layers(annex, {"botF": NEAR_EDGE})
    _, annex_from_botB = _layers(annex, {"botB": NEAR_EDGE})

    assert studio_from_botB.any(), "studio/tl should feel its own canvas's neighbour"
    assert not studio_from_botF.any(), "a robot on another canvas reached across"
    assert annex_from_botF.any(), "annex/west should feel its own canvas's neighbour"
    assert not annex_from_botB.any(), "a robot on another canvas reached across"


def test_multiple_active_robots_union():
    """Two neighbours drawing keep out exactly the union of what each would alone."""
    studio = _studio()
    _, east = _layers(studio, {"botB": NEAR_EDGE})
    _, south = _layers(studio, {"botC": SOUTH_EDGE})
    _, both = _layers(studio, {"botB": NEAR_EDGE, "botC": SOUTH_EDGE})

    assert east.any() and south.any()
    np.testing.assert_array_equal(both, east | south)


def test_keepout_is_the_swept_body_and_never_under_covers():
    """The live keep-out is the body swept along the stroke — and never leaves a hole.

    Checked against an independently-derived ground truth. Over-covering is fine
    (the sweep is inflated by a cell so rasterizing can't leave a sliver of chassis
    unblocked); under-covering would be a collision.
    """
    for strokes in (NEAR_EDGE, NEAR_EDGE_REVERSED, CORNER_ONLY):
        studio = _studio(with_ink=False)
        tl = _subject(studio)
        # The bare swept body, before the engine grows it by the query robot's
        # overhang — that growth is the broad-phase margin, tested below.
        full = _occ(studio, {"botB": strokes}).bodies
        truth = _brute_sweep(tl, strokes, ROBOT_BODY)

        missed = truth & ~full
        assert (
            not missed.any()
        ), f"{int(missed.sum())} cells the chassis actually covers were left free"
        # ...and the over-cover is only the one-cell margin, not slop.
        extra = int((full & ~truth).sum())
        assert (
            extra <= int(truth.sum()) * 0.15 + 200
        ), f"keep-out over-covers by {extra} cells — more than the margin explains"


def test_heading_changes_the_keepout():
    """The pen is 76mm from the nose but 150mm from the tail, so direction matters.

    Same real drawing, driven forwards and then in reverse: identical ink, every
    heading flipped. A disc around the ink would score these identically — that
    they differ is the whole reason for modelling the body.
    """
    studio = _studio(with_ink=False)
    forward = _occ(studio, {"botB": NEAR_EDGE}).keepout
    backward = _occ(studio, {"botB": NEAR_EDGE_REVERSED}).keepout

    assert forward.any() and backward.any(), "test geometry proves nothing"
    assert not np.array_equal(forward, backward), (
        "reversing the drawing left the keep-out identical — the heading is being "
        "ignored, which is exactly what the disc model got wrong"
    )


def test_broad_phase_is_sound_and_narrow_phase_is_necessary():
    """The contract the two-phase split rests on.

    ``min_overhang_mm`` is the inscribed disc of the chassis, so the body contains
    it at *every* heading. Growing a live robot's body by it therefore blocks only
    placements that are *certain* to collide — a sound broad phase. But it is a
    filter, not a gate: between ``min_overhang_mm`` and ``reach_mm`` whether we
    touch depends on which way we point, so the engine still sweeps our own body
    for the survivors. This pins both halves at once.
    """
    studio = _studio(with_ink=False)
    tl = _subject(studio)
    occ = _occ(studio, {"botB": NEAR_EDGE})
    broad, their_body = occ.keepout, occ.bodies

    sound = 0
    needed_narrow = 0
    # Sweep a probe stroke across the region at a heading whose 150mm tail points
    # back at the neighbour — the orientation the broad phase cannot see.
    for x in range(120, 460, 20):
        probe = [[(float(x) + 90.0, 250.0), (float(x), 250.0)]]  # driving west
        rejected = bool((_ink(tl, probe) & broad).any())
        collides = bool((_swept(tl, probe, ROBOT_BODY) & their_body).any())

        if rejected:
            # Soundness: the broad phase must never reject a placement that would
            # actually have been fine.
            assert collides, (
                f"broad phase rejected a safe placement at x={x} — "
                "min_overhang is supposed to bound *certain* collisions only"
            )
            sound += 1
        elif collides:
            needed_narrow += 1

    assert sound, "no placement was rejected — this probe proves nothing"
    assert needed_narrow, (
        "every collision was caught by the broad phase, so this probe never "
        "reaches the ambiguous band the narrow phase exists for"
    )


def _chassis(strokes: list[Stroke]):
    """The exact area a chassis sweeps along ``strokes`` (shapely, no raster)."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union

    return unary_union(
        [
            Polygon(rect)
            for s in strokes
            for a, b in zip(s[:-1], s[1:])
            if (rect := _body_rect(a, b, ROBOT_BODY, 0.0)) is not None
        ]
    )


def test_place_never_returns_a_colliding_pose():
    """The headline guarantee: a pose ``place`` hands back is one we can draw.

    Drives it against live neighbours from several directions, with the region
    progressively filling so the search is pushed toward the contested edge, and
    checks every returned pose against the exact geometry: our chassis swept along
    our ink must not touch theirs swept along theirs. (Exact rather than
    rasterized, so the check doesn't depend on how either side samples cells.)
    """
    for label, active in (
        ("east", {"botB": NEAR_EDGE}),
        ("south", {"botC": SOUTH_EDGE}),
        ("both", {"botB": NEAR_EDGE, "botC": SOUTH_EDGE}),
        ("reversed", {"botB": NEAR_EDGE_REVERSED}),
    ):
        studio = _studio(with_ink=False)
        tl = _subject(studio)
        theirs = [_chassis(strokes) for strokes in active.values()]

        placed = 0
        for i in range(6):  # keep placing so the region fills toward the neighbour
            hit = _place(studio, tl, DRAWINGS[i % len(DRAWINGS)], 150.0, active=active)
            if hit is None:
                break
            placement, world = hit
            placed += 1
            ours = _chassis(world)
            for body in theirs:
                gap = ours.distance(body)
                assert gap > 0, (
                    f"[{label}] place returned a pose whose chassis touches a live "
                    f"robot's"
                )
            _record(studio, tl, placement, world)
        assert placed, f"[{label}] nothing placed — this proves nothing"


def test_placement_lands_on_the_anchor():
    """The world ink for a placement starts exactly at its anchor (the pose the
    robot is sent to), and stays inside the region."""
    studio = _studio(with_ink=False)
    tl = _subject(studio)
    hit = _place(studio, tl, DRAWINGS[0], 170.0)
    assert hit is not None
    placement, world = hit

    first = world[0][0]
    assert abs(first[0] - placement.anchor_x) < 1e-6
    assert abs(first[1] - placement.anchor_y) < 1e-6
    x0, y0, x1, y1 = _span(world)
    assert tl.x <= x0 and x1 <= tl.x + tl.width and tl.y <= y0 and y1 <= tl.y + tl.height


def test_place_is_unaffected_when_nobody_is_drawing():
    """Robots that can't reach us must change nothing.

    Same seed, same drawings: an idle dict and a dict full of robots that can't
    reach us must produce identical placements.
    """

    def run(active):
        studio = _studio(with_ink=False)
        tl = _subject(studio)
        out = []
        for i in range(4):
            hit = _place(studio, tl, DRAWINGS[i % len(DRAWINGS)], 150.0, active=active, seed=7)
            if hit is None:
                break
            placement, world = hit
            out.append((placement.angle_deg, placement.anchor_x, placement.anchor_y))
            _record(studio, tl, placement, world)
        return out

    idle = run({})
    assert idle, "nothing placed"
    for active in ({"botB": None}, {"botD": CORNER_ONLY}, {"botB": OUT_OF_REACH}):
        assert (
            run(active) == idle
        ), f"{active!r} perturbed placement despite being out of reach"


def test_active_keepout_composes_with_the_buffer():
    """The two sources OR together — neither replaces the other."""
    studio = _studio()
    occ = _occ(studio, {"botB": NEAR_EDGE})

    assert occ.keepout.dtype == np.bool_
    assert occ.active.any() and occ.buffered.any()
    np.testing.assert_array_equal(occ.keepout, occ.buffered | occ.active)
    assert np.all(occ.keepout[occ.ink]), "committed ink stopped being kept out"


if __name__ == "__main__":
    summary = run_demo()
    for scenario in summary["scenarios"]:
        print(f"{scenario['name']}:")
        for canvas_id, stats in scenario["canvases"].items():
            print(
                f"    {canvas_id:7s} buffer keep-out={stats['buffer_cells']:>6} "
                f"active keep-out={stats['active_cells']:>6}  ({stats['expect']})"
            )
    print(f"\nwrote {len(summary['scenarios'])} PNGs to {summary['out_dir']}")
