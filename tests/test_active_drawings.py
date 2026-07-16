"""Fast, focused check: once a robot is *assigned* a drawing, do other
robots' placements avoid overlapping those committed strokes?

This is the same occupancy mechanism ``test_placement_packed.py`` already
exercises (assignment commits a drawing's footprint into the region's
occupancy grid; ``_assign_locked``'s placement search treats occupied cells
as unavailable) — just stripped down to the minimum needed to see it happen:

* ONE shared region, used by both ``bot1`` and ``bot2``.
* ``bot1`` checks in ready exactly once and gets assigned a single drawing.
  That drawing's strokes are now committed occupancy in the region.
* ``bot2`` then polls "ready" repeatedly with a bunch of queued jobs. Each
  placement is checked against bot1's drawing's bounding box for overlap and
  rendered with bot1's drawing highlighted, so overlap (if it ever happens)
  is obvious both visually and in the printed summary.

ASSUMPTION FLAGGED: I made the shared region's ``RegionConfig`` list both
robot names (``robot=["bot1", "bot2"]``) so both can be assigned jobs in it.
I don't actually know if your ``RegionConfig`` accepts a list there, or if
"shared region, multiple robots" is expressed some other way in your real
schema — if this doesn't match, tell me the right shape and I'll fix it.
"""

from __future__ import annotations

import glob
import json
import os
import sys

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
OUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "boundary_avoidance"
)

CANVAS_ID = "test"
CANVAS_W = 1000.0
CANVAS_H = 1000.0
TARGET_MM = 220.0
N_BOT2_ATTEMPTS = 5


def _load_vectorizations() -> list[tuple[str, list[dict]]]:
    files = sorted(glob.glob(os.path.join(VEC_DIR, "*.json")))
    out = []
    for path in files:
        with open(path) as fh:
            commands = json.load(fh)
        out.append((os.path.splitext(os.path.basename(path))[0], commands))
    return out


def _make_canvas(strategy: str, active_buffer: int) -> CanvasConfig:
    """A single full-bleed region shared by both robots — no per-robot split."""
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
            RegionConfig(
                id="shared", x=0, y=0, width=CANVAS_W, height=CANVAS_H, robot="bot1"
            ),
            RegionConfig(
                id="shared1", x=0, y=0, width=CANVAS_W, height=CANVAS_H, robot="bot2"
            ),
        ],
        placement=PlacementSettings(strategy=strategy, targetFootprintMm=TARGET_MM),
        general_buffer=0,
    )


def _checkin(coord, robot, status, pose=(CANVAS_W / 2, CANVAS_H / 2)):
    resp = coord.check_in(
        CheckIn.Request(
            name=robot, status=status, pose=Pose(x=pose[0], y=pose[1], headingDegrees=0)
        )
    )
    return resp


def _enqueue(coord, commands) -> str:
    job = DrawingJob(jobId=coord.next_job_id(), commands=commands)
    coord.enqueue(job)
    return job.jobId


def _placed(coord) -> list:
    return list(coord._drawings.get(CANVAS_ID, []))


def _bbox(dr):
    xs = [p[0] for stroke in dr.strokes for p in stroke]
    ys = [p[1] for stroke in dr.strokes for p in stroke]
    return (min(xs), min(ys), max(xs), max(ys))


def _bboxes_overlap(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1


def _render(path, drawings, bot1_job_id, title, highlight_job=None):
    scale = 640.0 / max(CANVAS_W, CANVAS_H)
    margin = 32
    W = int(CANVAS_W * scale) + 2 * margin
    H = int(CANVAS_H * scale) + 2 * margin + 16
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)

    def to_px(p):
        return (margin + p[0] * scale, margin + 16 + p[1] * scale)

    draw.text((margin, 4), title, fill=(0, 0, 0))
    draw.rectangle(
        [to_px((0, 0)), to_px((CANVAS_W, CANVAS_H))], outline=(0, 0, 0), width=2
    )

    for dr in drawings:
        if dr.job_id == bot1_job_id:
            color, width = (230, 130, 0), 4  # bot1's committed drawing — orange, thick
        elif dr.job_id == highlight_job:
            color, width = (220, 20, 20), 4  # newest bot2 placement — red
        else:
            color, width = (31, 119, 180), 2  # earlier bot2 placements — blue
        for stroke in dr.strokes:
            if len(stroke) >= 2:
                draw.line(
                    [to_px(p) for p in stroke], fill=color, width=width, joint="curve"
                )
        ax, ay = to_px((dr.anchor_x, dr.anchor_y))
        draw.ellipse([ax - 3, ay - 3, ax + 3, ay + 3], fill=color)

    img.save(path)


def run(strategy: str = "scatter", seed: int = 0, active_buffer: int = 0):
    vectorizations = _load_vectorizations()
    assert vectorizations, f"no vectorizations found in {VEC_DIR}"

    out_dir = os.path.join(OUT_DIR, strategy)
    os.makedirs(out_dir, exist_ok=True)

    coord = _Coordinator([_make_canvas(strategy, active_buffer)], seed=seed)
    # bot1 gets assigned exactly one drawing — this commits its strokes into
    # the shared region's occupancy grid.
    name0, commands0 = vectorizations[0]
    bot1_job_id = _enqueue(coord, commands0)
    _checkin(coord, "bot1", "ready")
    placed_after_bot1 = _placed(coord)
    assert any(
        dr.job_id == bot1_job_id for dr in placed_after_bot1
    ), "bot1's job never got placed — nothing to test avoidance against"
    bot1_drawing = next(dr for dr in placed_after_bot1 if dr.job_id == bot1_job_id)
    bot1_bbox = _bbox(bot1_drawing)

    # _render(

    #     placed_after_bot1,
    #     bot1_job_id,
    #     title=f"[{strategy}] bot1 assigned {name0} (orange) — occupancy now committed",
    # )

    # bot1 stays "drawing" (busy) for the rest of the run — it should not get
    # any further jobs, only bot2 will poll from here on.
    _checkin(coord, "bot1", "drawing")

    overlaps = []
    frame = 1
    for i in range(N_BOT2_ATTEMPTS):
        name, commands = vectorizations[(i + 1) % len(vectorizations)]
        job_id = _enqueue(coord, commands)
        before = len(_placed(coord))
        resp = _checkin(coord, "bot2", "ready")
        after = _placed(coord)
        if len(after) > before:
            newest = after[-1]
            overlap = _bboxes_overlap(bot1_bbox, _bbox(newest))
            overlaps.append(overlap)
            _render(
                os.path.join(
                    out_dir,
                    f"final_buffer_{active_buffer:g}.png",
                ),
                after,
                bot1_job_id,
                title=f"[{strategy}] bot2 placed {name} "
                f"@({newest.anchor_x:.0f},{newest.anchor_y:.0f}) "
                f"overlaps_bot1={'YES !!' if overlap else 'no'}",
                highlight_job=newest.job_id,
            )
            frame += 1
        else:
            print(f"  attempt {i}: bot2 did not place ({resp.action})")

    n_overlaps = sum(overlaps)
    print(
        f"[{strategy}] bot2 placements: {len(overlaps)}/{N_BOT2_ATTEMPTS}, "
        f"overlapping bot1's drawing: {n_overlaps}"
    )
    if n_overlaps:
        print("  !! at least one bot2 placement overlapped bot1's committed strokes")

    return {
        "strategy": strategy,
        "out_dir": out_dir,
        "overlaps": overlaps,
        "n_overlaps": n_overlaps,
    }


def test_bot2_avoids_bot1_strokes():
    result = run("origin")
    assert result["n_overlaps"] == 0, (
        f"bot2 overlapped bot1's committed drawing {result['n_overlaps']} time(s) — "
        f"see {result['out_dir']}"
    )


if __name__ == "__main__":
    buffers = [0, 50, 100, 200, 500]
    for active_buffer in buffers:
        run("origin", active_buffer=active_buffer)
