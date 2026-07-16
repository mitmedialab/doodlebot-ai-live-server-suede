"""Checks on the two time budgets guarding ``_Coordinator._assign_locked``.

Why they exist
--------------
``_assign_locked`` runs inside the check-in lock, so every second it overruns is a
second every robot in the fleet is blocked polling. Placement cost isn't bounded
by anything natural — a tight canvas means more rotations searched, more
shrink-to-fit steps, more candidate bots — so the pass is capped instead:

* ``assign_budget_s`` — the pass stops *starting* new work once it's out of time.
  Anything untouched stays queued for the next check-in a second later.
* ``job_budget_s`` — one job's share. Exceed it without placing and the job goes
  to the *back* of the queue, so a drawing that is merely expensive to fail at
  can't monopolise every pass and starve everything behind it.

Both are checked between units of work (never mid-search), so what they really
bound is when new work *starts*. These tests pin the queue behaviour rather than
wall-clock, which would be flaky — with one exception that only asserts a very
loose ceiling.

Run with ``python tests/test_assign_budgets.py`` or under pytest.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from release.robots import (  # noqa: E402
    CanvasConfig,
    CheckIn,
    DrawingJob,
    PlacementSettings,
    Pose,
    RegionConfig,
    _Coordinator,
)

VEC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vectorizations")


def _vectorizations() -> list[list[dict]]:
    out = []
    for path in sorted(glob.glob(os.path.join(VEC_DIR, "*.json"))):
        with open(path) as fh:
            out.append(json.load(fh))
    assert out, f"no vectorizations in {VEC_DIR}"
    return out


def _canvas() -> CanvasConfig:
    return CanvasConfig(
        id="c",
        width=1000.0,
        height=1000.0,
        regions=[RegionConfig(id="r", x=0.0, y=0.0, width=1000.0, height=1000.0, robot="bot")],
        placement=PlacementSettings(strategy="origin", targetFootprintMm=300.0),
    )


def _coord(**budgets) -> _Coordinator:
    coord = _Coordinator([_canvas()], seed=0)
    for k, v in budgets.items():
        setattr(coord, k, v)
    return coord


def _enqueue_many(coord: _Coordinator, n: int) -> list[str]:
    vecs = _vectorizations()
    ids = []
    for i in range(n):
        job = DrawingJob(jobId=coord.next_job_id(), commands=vecs[i % len(vecs)])
        coord.enqueue(job)
        ids.append(job.jobId)
    return ids


def _poll(coord: _Coordinator):
    return coord.check_in(
        CheckIn.Request(name="bot", status="ready", pose=Pose(x=500.0, y=500.0))
    )


def _queue_ids(coord: _Coordinator) -> list[str]:
    return [qj.job.jobId for qj in coord._queue]


# --------------------------------------------------------------------------- #


def test_exhausted_pass_budget_leaves_the_queue_intact():
    """A pass with no time left must place nothing and lose nothing.

    The degenerate end of the budget: zero seconds. Every job should still be
    queued afterwards, in its original order — the budget defers work, it never
    drops it.
    """
    coord = _coord(assign_budget_s=0.0)
    ids = _enqueue_many(coord, 4)

    resp = _poll(coord)

    assert resp.action == "wait", "placed a drawing despite having no time budget"
    assert _queue_ids(coord) == ids, "the queue was reordered or lost jobs"


def test_a_generous_budget_still_places():
    """Sanity: the budget machinery isn't blocking the normal path."""
    coord = _coord(assign_budget_s=60.0, job_budget_s=60.0)
    _enqueue_many(coord, 1)

    assert _poll(coord).action == "draw"
    assert not coord._queue


def test_a_job_that_blows_its_budget_goes_to_the_back():
    """The anti-starvation rule.

    A job that spends its whole budget without placing must not keep its place at
    the head of the queue, or it will be re-tried first on every future pass and
    the jobs behind it will never be looked at. ``job_budget_s=0`` makes every job
    "too slow" deterministically, without depending on how fast the machine is.
    """
    coord = _coord(assign_budget_s=60.0, job_budget_s=0.0)
    ids = _enqueue_many(coord, 3)

    resp = _poll(coord)

    assert resp.action == "wait", "nothing should place with a zero job budget"
    # Every job timed out, so every job got deferred — order preserved among them.
    assert sorted(_queue_ids(coord)) == sorted(ids), "jobs were dropped"
    assert _queue_ids(coord) == ids, "relative order changed unexpectedly"


def test_a_slow_job_does_not_starve_the_ones_behind_it():
    """The case the deferral exists for.

    Job A is unplaceable-and-slow; B and C are fine. With A pinned at the head of
    the queue forever, B and C would never be reached. After A is deferred, the
    next pass must get to them.
    """
    coord = _coord(assign_budget_s=60.0, job_budget_s=60.0)
    ids = _enqueue_many(coord, 3)

    # Make only the first job burn its budget: patch _place_scaled so the first
    # call sleeps past the job deadline and fails, and later calls behave normally.
    real = coord._place_scaled
    seen: list[str] = []

    def slow_first(region, qj, context, *a, **k):
        if qj.job.jobId == ids[0]:
            seen.append(qj.job.jobId)
            time.sleep(0.05)
            return None, qj.drawing  # unplaceable
        return real(region, qj, context, *a, **k)

    coord._place_scaled = slow_first  # type: ignore[method-assign]
    coord.job_budget_s = 0.01  # the sleep alone exceeds this

    resp = _poll(coord)

    assert seen == [ids[0]], f"the slow job wasn't tried exactly once: {seen}"
    # It must not still be at the head, blocking the others next time round.
    assert _queue_ids(coord)[0] != ids[0], (
        f"the slow job kept the head of the queue: {_queue_ids(coord)}"
    )
    assert ids[0] in _queue_ids(coord) or resp.action == "draw", "the slow job vanished"


def test_pass_budget_bounds_the_wall_clock():
    """A loose ceiling: the pass must not run wildly past its budget.

    Deliberately generous — the budget is checked *between* jobs, so a pass can
    overrun by however long one job attempt takes. This only catches the budget
    being ignored outright.
    """
    coord = _coord(assign_budget_s=0.2, job_budget_s=0.05)
    _enqueue_many(coord, 12)

    started = time.monotonic()
    _poll(coord)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"pass took {elapsed:.1f}s against a 0.2s budget"
    # Nothing is ever dropped, whatever the budget did. A job that placed is
    # already delivered — check_in stages it and hands it back in the same call —
    # so it lives in _drawings, not in staged.
    drawn = len(coord._drawings.get("c", []))
    assert len(coord._queue) + drawn == 12, (
        f"{len(coord._queue)} queued + {drawn} drawn != 12 — a job was dropped"
    )


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
