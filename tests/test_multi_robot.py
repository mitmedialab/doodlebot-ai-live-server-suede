"""Visual + correctness harness for multiple robots drawing across canvases.

Where ``test_placement_packed`` exercised one region in isolation, this drives the
*coordinator* the way the real fleet does: several robots, each owning a region on
one of several canvases, all polling (`check_in`) on a shared ~1 Hz tick while
drawings are enqueued in bursts. That deliberately creates moments where more than
one robot is ready and waiting at once, so the matchmaker has to hand different
jobs to different bots in the same pass.

The drawing pool is pulled from ``tests/vectorizations``. Each tick renders one
frame showing every canvas, its regions (one per robot), which robot is drawing
vs. waiting, and every committed drawing coloured by the robot that made it — so
you can watch work flow across the fleet.

It also runs three automated checks that would surface real bugs the eye might
miss:

* **containment** — every placed drawing lies within its owning robot's region,
* **unique placement** — no job is committed to two robots,
* **conservation** — drawn + still-queued == enqueued.

Outputs land in ``tests/output/multi_robot/``:
  ``frame_NN.png``            — the fleet at tick NN
  ``occupancy_<canvas>_<region>.png`` — each region's final occupancy grid
Run directly (``python tests/test_multi_robot.py``) or under pytest.
"""

from __future__ import annotations

import glob
import itertools
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image, ImageDraw

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release.robots import (  # noqa: E402
    ArucoMarker,
    CanvasConfig,
    CheckIn,
    DrawingJob,
    PlacementSettings,
    Point,
    Pose,
    RegionConfig,
    _Coordinator,
)

VEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectorizations")
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "multi_robot")

TARGET_MM = 200.0   # canvas target footprint size (server scales each drawing to it)
DRAW_TICKS = 2      # ticks a robot spends "drawing" a job before it's ready again
TICKS = 18          # total simulation ticks

# tick -> how many drawings to enqueue at the start of that tick. Tick 0 is left
# empty on purpose so the run opens with every robot ready-and-waiting; tick 1
# dumps 8 (2x the 4-robot fleet) so all bots go busy at once with a real backlog.
ENQUEUE_SCHEDULE = {1: 8, 5: 5, 9: 4, 12: 3}

# Two canvases, each split into two regions (one robot each) — 4 robots total.
# alpha splits left/right, beta splits top/bottom, so both region shapes get tested.
LAYOUT = [
    {"id": "alpha", "w": 900.0, "h": 640.0, "split": "vertical", "robots": ["R1", "R2"]},
    {"id": "beta", "w": 640.0, "h": 900.0, "split": "horizontal", "robots": ["R3", "R4"]},
]

ROBOT_COLORS = {
    "R1": (31, 119, 180),
    "R2": (255, 127, 14),
    "R3": (44, 160, 44),
    "R4": (214, 39, 40),
}


# --------------------------------------------------------------------------- #
# Vectorizations
# --------------------------------------------------------------------------- #


def _load_vectorizations() -> list[list[dict]]:
    """Raw command lists — the coordinator sizes each to the canvas target."""
    files = sorted(glob.glob(os.path.join(VEC_DIR, "*.json")))
    out = []
    for path in files:
        with open(path) as fh:
            out.append(json.load(fh))
    assert out, f"no vectorizations in {VEC_DIR}"
    return out


# --------------------------------------------------------------------------- #
# Canvas / robot layout
# --------------------------------------------------------------------------- #


@dataclass
class RobotInfo:
    name: str
    canvas_id: str
    region_id: str
    home: tuple[float, float]


def build_config(strategy: str) -> tuple[list[CanvasConfig], dict[str, RobotInfo]]:
    canvases: list[CanvasConfig] = []
    robots: dict[str, RobotInfo] = {}
    for c in LAYOUT:
        w, h = c["w"], c["h"]
        r0, r1 = c["robots"]
        if c["split"] == "vertical":
            rects = [(r0, 0.0, 0.0, w / 2, h), (r1, w / 2, 0.0, w / 2, h)]
        else:
            rects = [(r0, 0.0, 0.0, w, h / 2), (r1, 0.0, h / 2, w, h / 2)]
        regions = []
        for name, rx, ry, rw, rh in rects:
            rid = f"{c['id']}-{name}"
            regions.append(
                RegionConfig(id=rid, x=rx, y=ry, width=rw, height=rh, robot=name)
            )
            robots[name] = RobotInfo(name, c["id"], rid, (rx + rw / 2, ry + rh / 2))
        canvases.append(
            CanvasConfig(
                id=c["id"],
                width=w,
                height=h,
                markers=[
                    ArucoMarker(id=0, position=Point(x=0.0, y=0.0)),
                    ArucoMarker(id=1, position=Point(x=w, y=0.0)),
                    ArucoMarker(id=2, position=Point(x=w, y=h)),
                    ArucoMarker(id=3, position=Point(x=0.0, y=h)),
                ],
                regions=regions,
                placement=PlacementSettings(strategy=strategy, targetFootprintMm=TARGET_MM),
            )
        )
    return canvases, robots


# --------------------------------------------------------------------------- #
# Simulated robot state machine
# --------------------------------------------------------------------------- #


@dataclass
class BotSim:
    info: RobotInfo
    status: str = "ready"       # ready | drawing
    ticks_left: int = 0
    job: Optional[str] = None
    badge: str = "WAIT"         # WAIT | START | DRAW  (for the current frame)


def _find_drawing(coord: _Coordinator, canvas_id: str, job_id: str):
    for d in coord._drawings.get(canvas_id, []):
        if d.job_id == job_id:
            return d
    return None


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #

PANEL_MAX_PX = 360
HEADER_H = 84
MARGIN = 24
GAP = 56


def _render_tick(path: str, tick: int, coord: _Coordinator, bots: dict[str, BotSim],
                 queued: int, waiting: int, busy: int) -> None:
    canvases = coord.canvases()
    scale = PANEL_MAX_PX / max(max(c.width, c.height) for c in canvases)

    # panel geometry
    panels = []
    x = MARGIN
    for c in canvases:
        pw, ph = c.width * scale, c.height * scale
        panels.append((c, x, HEADER_H, pw, ph))
        x += pw + GAP
    total_w = int(x - GAP + MARGIN)
    panel_area = max(ph for _, _, _, _, ph in panels)
    footer_h = 34 + 20 * len(bots)
    total_h = int(HEADER_H + panel_area + footer_h)

    img = Image.new("RGB", (total_w, total_h), "white")
    d = ImageDraw.Draw(img)

    # header
    d.text((MARGIN, 12), f"tick {tick:02d}", fill=(0, 0, 0))
    d.text((MARGIN, 30), f"queued: {queued}   drawn: "
           f"{sum(len(v) for v in coord._drawings.values())}", fill=(0, 0, 0))
    if busy >= len(bots):
        d.text((MARGIN, 52), f"** all {busy} robots busy — {queued} job(s) queued & waiting **",
               fill=(200, 80, 0))
    elif waiting >= 2:
        d.text((MARGIN, 52), f"** {waiting} robots ready & waiting (simultaneous check-ins) **",
               fill=(200, 0, 0))

    def bot_on(canvas_id: str) -> list[BotSim]:
        return [b for b in bots.values() if b.info.canvas_id == canvas_id]

    for c, ox, oy, pw, ph in panels:
        def to_px(mx: float, my: float) -> tuple[float, float]:
            return (ox + mx * scale, oy + my * scale)

        d.text((ox, oy - 16), f"canvas '{c.id}'", fill=(0, 0, 0))
        d.rectangle([to_px(0, 0), to_px(c.width, c.height)], outline=(0, 0, 0), width=2)

        # regions + their owning-robot label/status
        for r in c.regions:
            d.rectangle(
                [to_px(r.x, r.y), to_px(r.x + r.width, r.y + r.height)],
                outline=(170, 170, 170), width=1,
            )
            bot = bots.get(r.robot or "")
            col = ROBOT_COLORS.get(r.robot or "", (0, 0, 0))
            label = f"{r.robot} [{bot.badge}]" if bot else r.robot
            lx, ly = to_px(r.x + 4, r.y + 4)
            d.text((lx, ly), label, fill=col)
            d.text((lx, ly + 12), f"free {r.free_fraction:.0%}", fill=(150, 150, 150))

        # committed drawings, coloured by the robot that made them
        for dr in coord._drawings.get(c.id, []):
            col = ROBOT_COLORS.get(dr.robot_name, (120, 120, 120))
            active = any(b.job == dr.job_id and b.status == "drawing"
                         for b in bots.values())
            width = 3 if active else 1
            for stroke in dr.strokes:
                if len(stroke) >= 2:
                    d.line([to_px(px, py) for px, py in stroke], fill=col,
                           width=width, joint="curve")

        # robot markers at their pose (drawing → job anchor, else home)
        for b in bot_on(c.id):
            col = ROBOT_COLORS[b.info.name]
            if b.status == "drawing" and b.job is not None:
                dr = _find_drawing(coord, c.id, b.job)
                pos = (dr.anchor_x, dr.anchor_y) if dr else b.info.home
            else:
                pos = b.info.home
            mx, my = to_px(*pos)
            d.ellipse([mx - 6, my - 6, mx + 6, my + 6], fill=col, outline=(255, 255, 255))

    # footer: per-robot status table
    fy = int(HEADER_H + panel_area + 10)
    d.text((MARGIN, fy), "robots:", fill=(0, 0, 0))
    for i, b in enumerate(bots.values()):
        col = ROBOT_COLORS[b.info.name]
        y = fy + 18 + i * 20
        d.rectangle([MARGIN, y + 2, MARGIN + 12, y + 14], fill=col)
        job = b.job or "-"
        d.text((MARGIN + 20, y), f"{b.info.name}  {b.info.region_id:14s}  "
               f"{b.status:8s}  {b.badge:6s}  job={job}", fill=(0, 0, 0))

    img.save(path)


def _render_occupancy(path: str, region) -> None:
    import numpy as np

    grid = np.asarray(region.grid)
    arr = (255 - (grid > 0).astype("uint8") * 255).astype("uint8")
    Image.fromarray(arr, mode="L").resize(
        (arr.shape[1] * 2, arr.shape[0] * 2), Image.NEAREST
    ).save(path)


def _build_gif(frame_paths: list[str], gif_path: str, duration_ms: int = 700) -> None:
    """Stitch the per-tick PNGs into a looping GIF of the whole run.

    All frames share one palette (quantized against a busy mid-run frame, which
    carries every colour) so the robot colours stay stable across ticks instead
    of flickering. The last frame holds ~3x longer to make the end state readable.
    """
    if not frame_paths:
        return
    pal_src = Image.open(frame_paths[len(frame_paths) // 2]).convert("RGB")
    palette = pal_src.convert("P", palette=Image.ADAPTIVE, colors=256)
    frames = [Image.open(p).convert("RGB").quantize(palette=palette) for p in frame_paths]
    durations = [duration_ms] * (len(frames) - 1) + [duration_ms * 3]
    frames[0].save(
        gif_path,
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,  # restore to background between frames — no ghosting
    )


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def run_sim(strategy: str = "scatter", seed: int = 0) -> dict:
    out_dir = os.path.join(OUT_DIR, strategy)
    os.makedirs(out_dir, exist_ok=True)

    vectorizations = _load_vectorizations()
    vec_cycle = itertools.cycle(vectorizations)

    canvases, robot_info = build_config(strategy)
    coord = _Coordinator(canvases, seed=seed)
    bots = {name: BotSim(info) for name, info in robot_info.items()}

    enqueued = 0
    max_simultaneous_waiting = 0
    max_simultaneous_drawing = 0
    frame_paths: list[str] = []

    for tick in range(TICKS):
        # scripted enqueue burst (approvals arriving)
        for _ in range(ENQUEUE_SCHEDULE.get(tick, 0)):
            coord.enqueue(DrawingJob(jobId=coord.next_job_id(), commands=next(vec_cycle)))
            enqueued += 1

        waiting = 0
        for b in bots.values():
            # advance a drawing bot; it becomes ready when it finishes
            if b.status == "drawing":
                b.ticks_left -= 1
                if b.ticks_left <= 0:
                    b.status, b.job = "ready", None

            resp = coord.check_in(
                CheckIn.Request(
                    name=b.info.name,
                    status=b.status,
                    pose=Pose(x=b.info.home[0], y=b.info.home[1]),
                )
            )
            if resp.action == "draw":
                b.status, b.ticks_left, b.job, b.badge = "drawing", DRAW_TICKS, resp.jobId, "START"
            elif b.status == "drawing":
                b.badge = "DRAW"
            else:
                b.badge, waiting = "WAIT", waiting + 1

        busy = sum(1 for b in bots.values() if b.status == "drawing")
        max_simultaneous_waiting = max(max_simultaneous_waiting, waiting)
        max_simultaneous_drawing = max(max_simultaneous_drawing, busy)
        frame_path = os.path.join(out_dir, f"frame_{tick:02d}.png")
        _render_tick(frame_path, tick, coord, bots, len(coord.queued()), waiting, busy)
        frame_paths.append(frame_path)

    # animate the whole run
    gif_path = os.path.join(out_dir, "fleet.gif")
    _build_gif(frame_paths, gif_path)

    # final occupancy per region
    for c in coord.canvases():
        for r in c.regions:
            _render_occupancy(
                os.path.join(out_dir, f"occupancy_{c.id}_{r.id}.png"), r
            )

    checks = _run_checks(coord, robot_info, enqueued)
    return {
        "strategy": strategy,
        "out_dir": out_dir,
        "gif": gif_path,
        "enqueued": enqueued,
        "drawn": sum(len(v) for v in coord._drawings.values()),
        "queued": len(coord.queued()),
        "max_simultaneous_waiting": max_simultaneous_waiting,
        "max_simultaneous_drawing": max_simultaneous_drawing,
        **checks,
    }


# --------------------------------------------------------------------------- #
# Automated correctness checks
# --------------------------------------------------------------------------- #


def _run_checks(coord: _Coordinator, robot_info: dict[str, RobotInfo],
                enqueued: int) -> dict:
    eps = 1.0  # mm tolerance for containment
    containment_violations: list[str] = []
    seen_jobs: dict[str, str] = {}
    duplicate_jobs: list[str] = []

    for canvas_id, drawings in coord._drawings.items():
        for dr in drawings:
            # unique placement
            if dr.job_id in seen_jobs:
                duplicate_jobs.append(f"{dr.job_id} on {seen_jobs[dr.job_id]} & {dr.robot_name}")
            seen_jobs[dr.job_id] = dr.robot_name

            # containment within the owning robot's region
            region = coord._store.region_for_robot(dr.robot_name)
            if region is None:
                containment_violations.append(f"{dr.job_id}: robot {dr.robot_name} has no region")
                continue
            for stroke in dr.strokes:
                for x, y in stroke:
                    if not (region.x - eps <= x <= region.x + region.width + eps
                            and region.y - eps <= y <= region.y + region.height + eps):
                        containment_violations.append(
                            f"{dr.job_id} ({dr.robot_name}) point ({x:.0f},{y:.0f}) "
                            f"outside {region.id} "
                            f"[{region.x:.0f},{region.y:.0f},"
                            f"{region.width:.0f}x{region.height:.0f}]"
                        )
                        break
                else:
                    continue
                break

    drawn = sum(len(v) for v in coord._drawings.values())
    return {
        "containment_violations": containment_violations,
        "duplicate_jobs": duplicate_jobs,
        "conserved": (drawn + len(coord.queued()) == enqueued),
    }


# --------------------------------------------------------------------------- #
# pytest entry point
# --------------------------------------------------------------------------- #


def test_multi_robot_fleet():
    for strategy in ("scatter", "origin"):
        r = run_sim(strategy)
        assert r["max_simultaneous_waiting"] >= 2, f"never forced concurrent waits: {r}"
        assert r["max_simultaneous_drawing"] == len(ROBOT_COLORS), \
            f"never forced all bots busy at once: {r}"
        assert not r["duplicate_jobs"], r["duplicate_jobs"]
        assert not r["containment_violations"], r["containment_violations"][:5]
        assert r["conserved"], r


if __name__ == "__main__":
    for strategy in ("scatter", "origin"):
        r = run_sim(strategy)
        print(f"[{strategy}] enqueued={r['enqueued']} drawn={r['drawn']} "
              f"queued={r['queued']} max_wait={r['max_simultaneous_waiting']} "
              f"max_busy={r['max_simultaneous_drawing']} "
              f"conserved={r['conserved']} dup={len(r['duplicate_jobs'])} "
              f"containment_viol={len(r['containment_violations'])}")
        print(f"   gif -> {r['gif']}")
        for v in r["containment_violations"][:5]:
            print("   containment:", v)
        for v in r["duplicate_jobs"][:5]:
            print("   duplicate:", v)
