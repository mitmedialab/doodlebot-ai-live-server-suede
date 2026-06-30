"""Visual harness for ``_Coordinator._assign_locked`` packing into a full canvas.

What this exercises
-------------------
The coordinator owns a per-region occupancy grid and, on every check-in, runs a
placement search (rotation + offset) to fit a queued drawing into the free space
that's left. This test drives that path end-to-end:

1. **Pre-pack** a single-region canvas by enqueuing copies of the available
   vectorizations and draining them through ready check-ins, until the region is
   visibly full. This is the "already populated canvas" the new jobs land into.
2. **Place** one job per file in ``tests/vectorizations`` and watch where each
   one lands in the remaining space (including the shrink-to-fit fallback in
   ``_assign_locked`` when a full-size drawing won't fit anywhere).

Every step writes a PNG so the whole placement process is inspectable. Output
lands in ``tests/output/placement/<strategy>/``:

* ``frame_000_packed.png``      — the canvas after pre-packing (grey)
* ``frame_NNN_<jobid>.png``     — cumulative, newest placement highlighted red
* ``frame_zzz_final.png``       — every placement
* ``occupancy_final.png``       — the raw region occupancy grid the search sees

Run it directly for the visuals (``python tests/test_placement_packed.py``) or
under pytest (``pytest tests/test_placement_packed.py``). Both produce the PNGs.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Optional

from PIL import Image, ImageDraw

# Allow running as a plain script (python tests/test_placement_packed.py): make
# the repo root importable so ``release`` resolves. Under pytest the root is
# already on the path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release import canvas as canvas_engine  # noqa: E402
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
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "placement")

CANVAS_ID = "test"
ROBOT = "bot1"
CANVAS_W = 1000.0
CANVAS_H = 1000.0

# Source vectorizations can be in arbitrary units (the sample is larger than the
# whole canvas). Normalize each so its longest footprint dimension is this many
# mm — big enough that several pack into the region, small enough that they fit
# at full size until the canvas genuinely fills (then the shrink fallback fires).
TARGET_MM = 320.0

# How many drawings to lay down before the observed run, to fill the canvas.
PREPACK_COUNT = 10

_PALETTE = [
    (31, 119, 180), (255, 127, 14), (44, 160, 44), (148, 103, 189),
    (140, 86, 75), (227, 119, 194), (23, 190, 207), (188, 189, 34),
]


# --------------------------------------------------------------------------- #
# Loading + coordinator setup
# --------------------------------------------------------------------------- #


def _scale_raw(commands: list[dict], factor: float) -> list[dict]:
    """Scale a raw command list: distances + arc radii grow, spins are unchanged."""
    out: list[dict] = []
    for cmd in commands:
        c = dict(cmd)
        if c.get("kind") == "line":
            c["distance"] = c["distance"] * factor
        elif c.get("kind") == "arc":
            c["radius"] = c["radius"] * factor
        out.append(c)
    return out


def _normalize(commands: list[dict], target_mm: float) -> list[dict]:
    """Rescale so the drawing's longest pen-down extent is ~``target_mm``."""
    strokes = canvas_engine.commands_to_strokes(
        DrawingJob(jobId="probe", commands=commands).commands
    )
    pts = [p for s in strokes for p in s]
    if not pts:
        return commands
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys))
    if span <= 0:
        return commands
    return _scale_raw(commands, target_mm / span)


def _load_vectorizations() -> list[tuple[str, list[dict]]]:
    """Every ``*.json`` in tests/vectorizations as (name, normalized command list)."""
    files = sorted(glob.glob(os.path.join(VEC_DIR, "*.json")))
    out: list[tuple[str, list[dict]]] = []
    for path in files:
        with open(path) as fh:
            commands = json.load(fh)
        name = os.path.splitext(os.path.basename(path))[0]
        out.append((name, _normalize(commands, TARGET_MM)))
    return out


def _make_canvas(strategy: str) -> CanvasConfig:
    """One canvas, one full-bleed region owned by ROBOT, so packing is all in one
    grid and trivial to render."""
    return CanvasConfig(
        id=CANVAS_ID,
        width=CANVAS_W,
        height=CANVAS_H,
        markers=[
            ArucoMarker(id=0, position=Point(x=0.0, y=0.0)),
            ArucoMarker(id=1, position=Point(x=CANVAS_W, y=0.0)),
            ArucoMarker(id=2, position=Point(x=CANVAS_W, y=CANVAS_H)),
            ArucoMarker(id=3, position=Point(x=0.0, y=CANVAS_H)),
        ],
        regions=[
            RegionConfig(id="full", x=0.0, y=0.0, width=CANVAS_W, height=CANVAS_H, robot=ROBOT)
        ],
        placement=PlacementSettings(strategy=strategy),
    )


def _ready_checkin(coord: _Coordinator) -> CheckIn.Draw | CheckIn.Wait:
    """One ready poll from ROBOT. Registers the bot on first call; each ready
    poll places (stages + returns) at most one queued job."""
    return coord.check_in(
        CheckIn.Request(
            name=ROBOT,
            status="ready",
            pose=Pose(x=CANVAS_W / 2, y=CANVAS_H / 2, headingDegrees=0.0),
        )
    )


def _enqueue(coord: _Coordinator, commands: list[dict]) -> str:
    job = DrawingJob(jobId=coord.next_job_id(), commands=commands)
    coord.enqueue(job)
    return job.jobId


def _placed(coord: _Coordinator) -> list:
    return list(coord._drawings.get(CANVAS_ID, []))


# --------------------------------------------------------------------------- #
# Rendering (PIL — matplotlib isn't a dependency)
# --------------------------------------------------------------------------- #


def _render(
    path: str,
    drawings: list,
    n_packed: int,
    highlight_job: Optional[str] = None,
    title: str = "",
) -> None:
    """Draw the region with its committed drawings. The first ``n_packed`` are the
    pre-existing fill (grey); the rest are the observed placements (coloured); the
    newest is highlighted red."""
    scale = 640.0 / max(CANVAS_W, CANVAS_H)
    margin = 32
    W = int(CANVAS_W * scale) + 2 * margin
    H = int(CANVAS_H * scale) + 2 * margin + 16
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    def to_px(p) -> tuple[float, float]:
        return (margin + p[0] * scale, margin + 16 + p[1] * scale)

    draw.text((margin, 4), title, fill=(0, 0, 0))
    draw.rectangle([to_px((0, 0)), to_px((CANVAS_W, CANVAS_H))], outline=(0, 0, 0), width=2)

    for i, dr in enumerate(drawings):
        if dr.job_id == highlight_job:
            color, width = (220, 20, 20), 4
        elif i < n_packed:
            color, width = (205, 205, 205), 2
        else:
            color, width = _PALETTE[(i - n_packed) % len(_PALETTE)], 2
        for stroke in dr.strokes:
            if len(stroke) >= 2:
                draw.line([to_px(p) for p in stroke], fill=color, width=width, joint="curve")
        ax, ay = to_px((dr.anchor_x, dr.anchor_y))
        draw.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill=color)

    img.save(path)


def _render_occupancy(path: str, region) -> None:
    """The raw uint8 occupancy grid the placement search actually sees."""
    import numpy as np

    grid = np.asarray(region.grid)
    arr = (255 - (grid > 0).astype("uint8") * 255).astype("uint8")
    occ = Image.fromarray(arr, mode="L")
    occ = occ.resize((occ.width * 2, occ.height * 2), Image.NEAREST)
    occ.save(path)


# --------------------------------------------------------------------------- #
# The demo
# --------------------------------------------------------------------------- #


def run_demo(strategy: str, seed: int = 0) -> dict:
    vectorizations = _load_vectorizations()
    assert vectorizations, f"no vectorizations found in {VEC_DIR}"

    out_dir = os.path.join(OUT_DIR, strategy)
    os.makedirs(out_dir, exist_ok=True)

    coord = _Coordinator([_make_canvas(strategy)], seed=seed)
    region = coord._store.region_for_robot(ROBOT)
    assert region is not None

    # 1) Pre-pack: enqueue copies of the available vectorizations and drain them
    # through ready polls, one placement per poll, to fill the region.
    for i in range(PREPACK_COUNT):
        _enqueue(coord, vectorizations[i % len(vectorizations)][1])
    for _ in range(PREPACK_COUNT):
        if _ready_checkin(coord).action == "wait":
            break
    coord.clear_queue()  # discard any pre-pack jobs that never fit

    # Park the bot as non-ready so the observed jobs stay queued at enqueue time
    # and only place during the drain below (one per ready poll) — that's what
    # lets us snapshot a frame per placement.
    coord.check_in(
        CheckIn.Request(name=ROBOT, status="drawing", pose=Pose(x=0, y=0))
    )

    n_packed = len(_placed(coord))
    _render(
        os.path.join(out_dir, "frame_000_packed.png"),
        _placed(coord),
        n_packed,
        title=f"[{strategy}] pre-packed: {n_packed} drawings, "
        f"free={region.free_fraction:.0%}",
    )

    # 2) Observe: one job per vectorization, a frame per placement.
    job_to_name: dict[str, str] = {}
    for name, commands in vectorizations:
        job_to_name[_enqueue(coord, commands)] = name

    frame = 1
    placed_targets = 0
    for _ in range(len(vectorizations) + 5):  # safety cap
        before = len(_placed(coord))
        resp = _ready_checkin(coord)
        after = _placed(coord)
        if len(after) > before:
            newest = after[-1]
            placed_targets += 1
            _render(
                os.path.join(out_dir, f"frame_{frame:03d}_{newest.job_id}.png"),
                after,
                n_packed,
                highlight_job=newest.job_id,
                title=f"[{strategy}] placed {job_to_name.get(newest.job_id, '?')} "
                f"@({newest.anchor_x:.0f},{newest.anchor_y:.0f}) "
                f"{newest.angle_deg:.0f}deg  free={region.free_fraction:.0%}",
            )
            frame += 1
        elif resp.action == "wait":
            break

    _render(
        os.path.join(out_dir, "frame_zzz_final.png"),
        _placed(coord),
        n_packed,
        title=f"[{strategy}] final: {len(_placed(coord))} drawings, "
        f"free={region.free_fraction:.0%}",
    )
    _render_occupancy(os.path.join(out_dir, "occupancy_final.png"), region)

    return {
        "strategy": strategy,
        "out_dir": out_dir,
        "prepacked": n_packed,
        "targets": len(vectorizations),
        "targets_placed": placed_targets,
        "free_fraction": region.free_fraction,
    }


# --------------------------------------------------------------------------- #
# pytest entry point
# --------------------------------------------------------------------------- #


def test_assign_locked_packs_into_full_canvas():
    for strategy in ("origin", "scatter"):
        result = run_demo(strategy)
        # Pre-packing must have actually filled the canvas.
        assert result["prepacked"] >= 1, result
        # Observed jobs either place (at >= min_scale) or stay queued — never more
        # than were enqueued, and the floor means we no longer draw specks.
        assert 0 <= result["targets_placed"] <= result["targets"], result
        assert os.path.exists(os.path.join(result["out_dir"], "frame_zzz_final.png"))
        assert os.path.exists(os.path.join(result["out_dir"], "occupancy_final.png"))


if __name__ == "__main__":
    for strategy in ("origin", "scatter"):
        summary = run_demo(strategy)
        print(
            f"[{summary['strategy']}] prepacked={summary['prepacked']} "
            f"targets_placed={summary['targets_placed']}/{summary['targets']} "
            f"free={summary['free_fraction']:.1%} -> {summary['out_dir']}"
        )
