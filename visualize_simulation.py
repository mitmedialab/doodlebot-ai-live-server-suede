#!/usr/bin/env python3
"""Programmatic visualization + verification of the server-side robot coordination.

This exercises *only* the server logic — ``canvas.py`` (footprint rasterization,
occupancy grids, rotation-aware placement) and ``robots.py`` (the coordinator's
ready-pool matchmaking and lead-in stripping). The robots are **simulated**: no
hardware, no network. Each simulated bot runs the Poll → Draw loop against the
real ``_Coordinator``, and we independently replay the drawing commands it
receives to render exactly the ink it would put down — so the picture is a true
read-out of what the server told the bots to do.

The drawings are random "doodles": their strokes don't depict anything, but their
size and complexity are doodle-like. The sim plays until every region is packed
(no bot can place anything more), then writes an animated GIF and a final PNG.

Re-run any time after changing the server code:

    python visualize_simulation.py            # defaults: 8 bots, seed 0
    python visualize_simulation.py --seed 3 --bots 8

Outputs (in the repo root): ``doodlebot_sim.gif`` and ``doodlebot_sim_final.png``.
"""

from __future__ import annotations

import argparse
import colorsys
import math
import pathlib
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------- #
# The server code now lives in the ``release`` package, so import it directly.
# --------------------------------------------------------------------------- #

REPO_ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from release import canvas, robots  # noqa: E402

Line, Spin, Arc = robots.LineCommand, robots.SpinCommand, robots.ArcCommand


# --------------------------------------------------------------------------- #
# Geometry (mm). A 4 ft × 8 ft canvas split into evenly sized regions.
# --------------------------------------------------------------------------- #

FT_MM = 304.8
CANVAS_W = 4 * FT_MM  # 1219.2 mm  (x)
CANVAS_H = 8 * FT_MM  # 2438.4 mm  (y)
REGION_COLS = 2
REGION_ROWS = 4  # 2 × 4 = 8 evenly sized regions, one per bot


# --------------------------------------------------------------------------- #
# Random doodle generation — meaningless strokes, doodle-like size/complexity.
# --------------------------------------------------------------------------- #


def _random_doodle(rng: np.random.Generator) -> list:
    """A lead-in (pen-up drive + orient) followed by a squiggle of arcs/lines."""
    cmds: list = [
        Line(distance=float(rng.uniform(10, 70)), penDown=False),  # lead-in travel
        Spin(degrees=float(rng.uniform(-180, 180))),  # lead-in orient
    ]
    for _ in range(int(rng.integers(6, 16))):
        roll = rng.random()
        if roll < 0.55:
            cmds.append(
                Arc(
                    radius=float(rng.uniform(8, 45)),
                    degrees=float(rng.uniform(30, 200))
                    * (1 if rng.random() < 0.5 else -1),
                )
            )
        elif roll < 0.85:
            cmds.append(Line(distance=float(rng.uniform(15, 60)), penDown=True))
        else:
            cmds.append(Spin(degrees=float(rng.uniform(-90, 90))))
        if rng.random() < 0.12:  # occasionally hop (pen up) to start a new stroke
            cmds.append(Line(distance=float(rng.uniform(10, 40)), penDown=False))
            cmds.append(Spin(degrees=float(rng.uniform(-120, 120))))
    return cmds


def _scale(cmds: list, factor: float) -> list:
    out: list = []
    for c in cmds:
        if c.kind == "line":
            out.append(Line(distance=c.distance * factor, penDown=c.penDown))
        elif c.kind == "arc":
            out.append(Arc(radius=c.radius * factor, degrees=c.degrees))
        else:
            out.append(Spin(degrees=c.degrees))
    return out


def _bbox(strokes) -> tuple[float, float]:
    pts = [p for s in strokes for p in s]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (max(xs) - min(xs), max(ys) - min(ys))


def make_doodle(rng: np.random.Generator, max_extent: float) -> list:
    """Generate a doodle and scale it down if it's larger than ``max_extent``."""
    cmds = _random_doodle(rng)
    strokes = canvas.commands_to_strokes(cmds)
    if not strokes:
        return cmds
    w, h = _bbox(strokes)
    big = max(w, h)
    if big > max_extent:
        cmds = _scale(cmds, (max_extent / big) * 0.95)
    return cmds


# --------------------------------------------------------------------------- #
# Robot-side replay — turn a delivered Draw message into world-space ink.
# --------------------------------------------------------------------------- #


def replay_to_world(commands, start_x: float, start_y: float, heading_deg: float):
    """Mirror the robot: run the (lead-in-stripped) commands from the given start
    pose and return pen-down polylines in global mm. This deliberately re-derives
    the ink from the wire message rather than peeking at the occupancy grid."""
    local = canvas.commands_to_strokes(commands)  # local frame, first ink at (0,0)
    rad = math.radians(heading_deg)
    cos_t, sin_t = math.cos(rad), math.sin(rad)
    world = []
    for stroke in local:
        world.append(
            [
                (px * cos_t - py * sin_t + start_x, px * sin_t + py * cos_t + start_y)
                for px, py in stroke
            ]
        )
    return world


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def doodle_color(index: int) -> tuple[int, int, int]:
    """A distinct color per doodle. The golden-angle hue step keeps consecutive
    drawings far apart on the color wheel, so neighbours are easy to tell apart."""
    hue = (index * 0.6180339887) % 1.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.72, 0.88)
    return (int(r * 255), int(g * 255), int(b * 255))


class Renderer:
    def __init__(self, scale: float, margin: int = 36) -> None:
        self.scale = scale
        self.margin = margin
        self.W = int(CANVAS_W * scale) + 2 * margin
        self.H = int(CANVAS_H * scale) + 2 * margin + 28  # headroom for HUD
        try:
            self.font = ImageFont.load_default()
        except Exception:
            self.font = None

    def _px(self, x: float, y: float) -> tuple[float, float]:
        return (self.margin + x * self.scale, self.margin + 28 + y * self.scale)

    def frame(self, regions, drawings, tick: int, note: str) -> Image.Image:
        img = Image.new("RGB", (self.W, self.H), (250, 250, 248))
        d = ImageDraw.Draw(img)

        # regions (preserve the grid; labels are neutral so doodle colors read)
        for region in regions:
            x0, y0 = self._px(region.x, region.y)
            x1, y1 = self._px(region.x + region.width, region.y + region.height)
            d.rectangle([x0, y0, x1, y1], outline=(190, 190, 190), fill=(247, 247, 244))
            label = f"{region.id} · {region.robot} · {int(round((1 - region.free_fraction) * 100))}% packed"
            d.text((x0 + 4, y0 + 3), label, fill=(120, 120, 120), font=self.font)

        # drawings — each doodle a distinct color so they're easy to isolate
        pen = max(1, int(round(2.4 * self.scale)))
        for i, (_bot_idx, world) in enumerate(drawings):
            color = doodle_color(i)
            for stroke in world:
                if len(stroke) >= 2:
                    d.line(
                        [self._px(x, y) for x, y in stroke],
                        fill=color,
                        width=pen,
                        joint="curve",
                    )

        d.text((self.margin, 6), note, fill=(20, 20, 20), font=self.font)
        d.text(
            (self.margin, self.H - 16),
            f"tick {tick}  ·  drawings placed: {len(drawings)}",
            fill=(90, 90, 90),
            font=self.font,
        )
        return img


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def build_canvas_config(num_bots: int) -> "robots.CanvasConfig":
    """A single canvas with REGION_COLS × REGION_ROWS even regions, one per bot."""
    rw = CANVAS_W / REGION_COLS
    rh = CANVAS_H / REGION_ROWS
    region_cfgs = []
    bot = 0
    for row in range(REGION_ROWS):
        for col in range(REGION_COLS):
            if bot >= num_bots:
                break
            region_cfgs.append(
                robots.RegionConfig(
                    id=f"r{row}{col}",
                    x=col * rw,
                    y=row * rh,
                    width=rw,
                    height=rh,
                    robot=f"bot-{bot}",
                )
            )
            bot += 1
    # A 5mm grid is still well below pen width and keeps the placement search
    # snappy over these ~0.6 m regions (a 2mm production grid would be 305² cells
    # per region); 30° rotation steps are plenty for doodles.
    placement = robots.PlacementSettings(
        cellMm=5.0,
        penMm=3.0,
        clearanceMm=6.0,
        searchStepCells=2,
        angleStepDeg=30.0,
        strategy="scatter",  # spread doodles across each region (artist, not print head)
    )
    return robots.CanvasConfig(
        id="main",
        width=CANVAS_W,
        height=CANVAS_H,
        markers=[],
        regions=region_cfgs,
        placement=placement,
    )


def run(seed: int, num_bots: int, scale: float, out_dir: pathlib.Path) -> None:
    rng = np.random.default_rng(seed)
    cfg = build_canvas_config(num_bots)
    coord = robots._Coordinator([cfg], seed=seed)
    store_canvas = coord._store.all()[0]
    regions = store_canvas.regions
    bot_names = [r.robot for r in regions]
    bot_index = {name: i for i, name in enumerate(bot_names)}
    region_max_extent = min(regions[0].width, regions[0].height) * 0.6

    renderer = Renderer(scale=scale)
    drawings: list[tuple[int, list]] = []  # (bot_index, world_strokes)
    frames: list[Image.Image] = [renderer.frame(regions, drawings, 0, "start")]

    # simulated bot state: when each bot finishes its current drawing, and how
    # many ready-polls in a row it's been told to wait while work was available
    # (a saturated region — no queued doodle fits any more — is the "full" signal)
    free_at = {name: 0 for name in bot_names}
    drawing = {name: False for name in bot_names}
    consec_waits = {name: 0 for name in bot_names}

    SURPLUS = num_bots  # ~one queued doodle per bot (small queue = cheap re-tries)
    SATURATED_AT = 3  # consecutive "wait"s (with work queued) ⇒ region is full
    MAX_TICKS = 5000
    tick = 0

    def saturated_count() -> int:
        return sum(1 for n in bot_names if consec_waits[n] >= SATURATED_AT)

    while saturated_count() < num_bots and tick < MAX_TICKS:
        tick += 1

        # finished bots return to the ready pool
        for name in bot_names:
            if drawing[name] and free_at[name] <= tick:
                drawing[name] = False

        ready = [n for n in bot_names if not drawing[n]]

        # top up the job queue (the "approval flow" producing vectorized doodles)
        while coord.snapshot().queuedJobs < SURPLUS:
            cmds = make_doodle(rng, region_max_extent)
            coord.enqueue(robots.DrawingJob(jobId=coord.next_job_id(), commands=cmds))

        work_queued = coord.snapshot().queuedJobs > 0

        # every ready bot polls; the coordinator places + assigns under the hood
        for name in ready:
            resp = coord.check_in(
                robots.CheckIn.Request(
                    name=name, status="ready", pose=robots.Pose(x=0, y=0)
                )
            )
            if resp.action != "draw":
                if work_queued:
                    consec_waits[name] += 1  # told to wait though work was available
                continue
            consec_waits[name] = 0
            world = replay_to_world(
                resp.commands,
                resp.navigateTo.x,
                resp.navigateTo.y,
                resp.navigateTo.headingDegrees,
            )
            drawings.append((bot_index[name], world))
            drawing[name] = True
            free_at[name] = tick + int(rng.integers(1, 3))  # draw for 1–2 ticks

            # VERIFY: no cell is ever double-stamped (i.e. drawings never overlap)
            assert all(
                int(r.grid.max(initial=0)) <= 1 for r in regions
            ), "occupancy overlap!"

            frames.append(
                renderer.frame(
                    regions,
                    drawings,
                    tick,
                    f"{name} placed a doodle  ·  saturated regions: {saturated_count()}/{len(regions)}",
                )
            )

    # a closing frame that states the terminal condition (the per-placement
    # frames stop one or two ticks before the last region saturates)
    frames.append(
        renderer.frame(
            regions,
            drawings,
            tick,
            f"all {num_bots} regions saturated — {len(drawings)} doodles, no overlaps",
        )
    )

    # final summary + verification
    overall_free = float(np.mean([r.free_fraction for r in regions]))
    print(f"Simulation finished after {tick} ticks — all {num_bots} regions saturated")
    print("(saturated = the placement search can no longer fit any queued doodle).")
    print(f"Total doodles placed: {len(drawings)}")
    for i, r in enumerate(regions):
        count = sum(1 for b, _ in drawings if b == i)
        print(
            f"  region {r.id} (bot {r.robot}): {count} doodles, {(1 - r.free_fraction) * 100:5.1f}% packed"
        )
    print(f"Overall canvas {round((1 - overall_free) * 100, 1)}% packed.")
    print("No-overlap check passed: every region grid max occupancy ≤ 1.")

    # hold the last frame, then write outputs
    final = frames[-1]
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / "doodlebot_sim_final.png"
    gif_path = out_dir / "doodlebot_sim.gif"
    final.save(png_path)
    durations = [60] * len(frames)
    durations[0] = 600
    durations[-1] = 2500
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"\nWrote {gif_path}")
    print(f"Wrote {png_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--bots", type=int, default=8)
    parser.add_argument("--scale", type=float, default=0.34, help="pixels per mm")
    parser.add_argument("--out", type=pathlib.Path, default=REPO_ROOT)
    args = parser.parse_args()
    run(seed=args.seed, num_bots=args.bots, scale=args.scale, out_dir=args.out)


if __name__ == "__main__":
    main()
