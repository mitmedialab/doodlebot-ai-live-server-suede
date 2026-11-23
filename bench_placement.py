"""How long does placing a drawing take? Optionally, compared with another branch.

    python bench_placement.py                  # this checkout
    python bench_placement.py --against main   # this checkout vs. main, side by side

Runs the checkout's own ``tests/test_placement_packed.py`` and
``tests/test_multi_robot.py`` and times every placement
(``_Coordinator._place_scaled``: one drawing, shrink-to-fit included) and every
robot check-in (which also covers the exit pose). Only coordinator methods both
the Python and the Rust engine have are timed, so any two versions compare.

``--against <ref>`` exports that git ref (``git archive``, so your working tree is
untouched) to a temporary folder and benchmarks it the same way. Needs the
dependencies of both versions installed (``pip install -r requirements.txt``).
The tests write their PNGs into each checkout's ``tests/output/``.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import io
import json
import os
import runpy
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TESTS = ("test_placement_packed.py", "test_multi_robot.py")


def measure(root: str) -> dict:
    """Benchmark the checkout at ``root``. Call in a fresh process: it imports
    that checkout's ``release`` package."""
    sys.path[:0] = [root, os.path.join(root, "tests")]
    import release.robots as robots

    times: dict[str, list[float]] = {"place": [], "checkin": []}

    def timed(name, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                times[name].append((time.perf_counter() - start) * 1000)

        return wrapper

    robots._Coordinator._place_scaled = timed("place", robots._Coordinator._place_scaled)
    robots._Coordinator.check_in = timed("checkin", robots._Coordinator.check_in)

    start = time.perf_counter()
    for test in TESTS:
        with contextlib.redirect_stdout(io.StringIO()):
            runpy.run_path(os.path.join(root, "tests", test), run_name="__main__")
    result = {"wall_s": time.perf_counter() - start}
    for name, xs in times.items():
        result[name] = {"n": len(xs), "mean": statistics.mean(xs), "median": statistics.median(xs)}
    return result


def measure_in_subprocess(root: str) -> dict:
    out = subprocess.run(
        [sys.executable, os.path.abspath(__file__), "--measure", root],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--against", metavar="REF", help="git ref to compare with, e.g. main")
    ap.add_argument("--measure", metavar="DIR", help=argparse.SUPPRESS)  # internal
    args = ap.parse_args()

    if args.measure:
        print(json.dumps(measure(args.measure)))
        return

    runs = {"this checkout": measure_in_subprocess(HERE)}
    if args.against:
        with tempfile.TemporaryDirectory() as tmp:
            archive = subprocess.run(
                ["git", "-C", HERE, "archive", args.against, "release", "tests"],
                check=True, capture_output=True,
            ).stdout
            subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
            runs[args.against] = measure_in_subprocess(tmp)

    names = list(runs)
    print(f"{'':24s}" + "".join(f"{n:>16s}" for n in names))
    rows = [
        ("placement, mean", lambda r: r["place"]["mean"], "ms"),
        ("placement, median", lambda r: r["place"]["median"], "ms"),
        ("check-in, mean", lambda r: r["checkin"]["mean"], "ms"),
        ("whole run", lambda r: r["wall_s"], "s"),
    ]
    for label, get, unit in rows:
        line = f"{label:24s}" + "".join(f"{get(runs[n]):13.1f} {unit:2s}" for n in names)
        if len(names) == 2:
            line += f"   {get(runs[names[1]]) / get(runs[names[0]]):5.1f}x faster"
        print(line)
    print("placements: " + ", ".join(f"{runs[n]['place']['n']} ({n})" for n in names))


if __name__ == "__main__":
    main()
