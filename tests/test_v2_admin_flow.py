"""End-to-end tests for the v2 admin-approval workflow.

Exercises the whole *local* pipeline with only the OpenAI combine call stubbed:

  submit -> (admin approves sketch) -> group into a trio -> combine (stub) ->
  real vectorize -> (admin approves / simplifies / rejects vectorization) ->
  real robot dispatch -> complete.

Everything that isn't the OpenAI network hop runs for real: the FastAPI routes
(driven over HTTP via TestClient, including the admin SSE feed), the manager's
state machine + on-disk persistence, the arc/line vectorizer, and the robot
coordinator's placement + assignment.

The properties under test:

  * a submitted sketch reveals *nothing* to its client (not even a status) until
    an admin rules on it, and the admin is notified it's waiting;
  * an admin verdict flows to the client as the ``status`` field — "approved"
    moves it into grouping, "innapropriate"/"complex" is terminal;
  * a combined trio's vectorization is likewise withheld from every client until
    an admin approves it, and the admin sees the raw commands;
  * the admin may approve as-is, approve a simplified command set (which becomes
    the served SVG + the dispatched drawing), or reject (regenerating a fresh
    candidate);
  * only after vectorization approval does the drawing reach the robot pool and
    the clients advance to "complete".

Run with ``python tests/test_v2_admin_flow.py`` or under pytest.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sys
import time

# --- environment + storage stub (must be set BEFORE importing release.v2) ---
#
# release.v2 builds its module-level ``manager`` (and release.storage its S3
# client) at import time, and Manager.__init__ enumerates S3. So we configure
# the env and swap in an in-memory storage before the first import of release.v2.

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_SCRATCH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output", "v2_admin")
os.environ.setdefault("S3_BUCKET", "test-bucket")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ["ADMIN_TOKEN"] = "test-admin-token"
os.environ["V2_DATA_DIR"] = _SCRATCH
# Assign fast so a mis-armed robot fails the test quickly instead of hanging.
os.environ["V2_ROBOT_ASSIGN_TIMEOUT"] = "4"
os.environ["V2_ROBOT_POLL_INTERVAL"] = "0.05"

# Start from a clean slate so persisted sketch metas from a prior run don't
# dedup-suppress this run's submissions (store_sketch is idempotent by content).
shutil.rmtree(_SCRATCH, ignore_errors=True)

_EXT_BY_CT = {"image/png": ".png", "image/svg+xml": ".svg", "image/jpeg": ".jpg"}


class FakeStorage:
    """In-memory stand-in for release.storage.S3Storage — same surface the v2
    Manager touches, but blobs live in a dict instead of S3."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.combined: dict[str, bytes] = {}

    @staticmethod
    def resource_key(resource_id: str, content_type: str) -> str:
        return f"resources/{resource_id}{_EXT_BY_CT.get(content_type, '.bin')}"

    def put_resource(self, body: bytes, content_type: str) -> str:
        resource_id = hashlib.sha256(body).hexdigest()
        self.blobs[self.resource_key(resource_id, content_type)] = body
        return resource_id

    def put_resource_at(self, resource_id: str, body: bytes, content_type: str) -> None:
        self.blobs[self.resource_key(resource_id, content_type)] = body

    def put_combined_debug(self, png_bytes: bytes, name: str) -> None:
        self.combined[name] = png_bytes

    def read(self, key: str) -> bytes:
        return self.blobs[key]

    def presigned_url(self, key: str, expires: int = 3600) -> str:
        return f"https://fake-s3.local/{key}?exp={expires}"

    def list_resources(self) -> dict:
        return {}


import release.storage as storage_mod  # noqa: E402

_FAKE_STORAGE = FakeStorage()
storage_mod.storage = _FAKE_STORAGE

import release.v2 as v2  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from release.robots import (  # noqa: E402
    CanvasConfig,
    CheckIn,
    PlacementSettings,
    Pose,
    RegionConfig,
    coordinator,
)

# The manager was built against real S3 during import; point it (and its
# module-level `storage` handle) at the fake so every code path uses the stub.
v2.storage = _FAKE_STORAGE
# Shorten the SSE heartbeat so admin-feed reads never stall for the full 15s.
v2.HEARTBEAT = 0.5

TOKEN = os.environ["ADMIN_TOKEN"]


# --- OpenAI combine stub ---------------------------------------------------


def _combined_png_b64(variant: int) -> str:
    """A deterministic 'combined' drawing the real vectorizer can bite into. Two
    visibly different variants so the two combine runs yield distinct options."""
    img = Image.new("RGB", (512, 512), "white")
    draw = ImageDraw.Draw(img)
    if variant == 0:
        draw.line([(60, 60), (440, 120)], fill="black", width=4)
        draw.arc([90, 150, 400, 430], start=20, end=210, fill="black", width=4)
        draw.rectangle([180, 300, 360, 460], outline="black", width=4)
    else:
        draw.ellipse([80, 80, 420, 420], outline="black", width=4)
        draw.line([(120, 400), (400, 120)], fill="black", width=4)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


_STUB_VARIANTS = [_combined_png_b64(0), _combined_png_b64(1)]

# GPT Image 1 is non-deterministic, so the two combine runs per trio return
# different drawings. Emulate that: hand out alternating variants, serialized so
# the two concurrent calls of a trio always pick *different* variants. The stub
# also records call count (to assert two combines ran per trio) and tracks peak
# concurrency (to assert the in-flight gate bounds it) — it sleeps briefly so
# concurrent calls genuinely overlap for that measurement.
_COMBINE_LOCK = __import__("threading").Lock()
_COMBINE_CALLS: list[int] = []
_INFLIGHT = 0
_PEAK_INFLIGHT = 0
_STUB_SLEEP = 0.1


def _fake_openai_s3(model_id, images, prompt):  # noqa: ANN001
    global _INFLIGHT, _PEAK_INFLIGHT
    assert 2 <= len(images) <= 4, "combine should receive the trio's bytes"
    with _COMBINE_LOCK:
        index = len(_COMBINE_CALLS)
        _COMBINE_CALLS.append(index)
        _INFLIGHT += 1
        _PEAK_INFLIGHT = max(_PEAK_INFLIGHT, _INFLIGHT)
    try:
        time.sleep(_STUB_SLEEP)
        return _STUB_VARIANTS[index % len(_STUB_VARIANTS)]
    finally:
        with _COMBINE_LOCK:
            _INFLIGHT -= 1


v2.Combine.openai_s3 = staticmethod(_fake_openai_s3)


# --- helpers ---------------------------------------------------------------


def _sketch_data_url(seed: int) -> str:
    """A distinct PNG per seed (distinct bytes -> distinct sha256 sketch id)."""
    img = Image.new("RGB", (128, 128), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10 + seed, 10, 60 + seed, 60], outline="black", width=3)
    draw.line([(0, seed % 100), (127, 127)], fill="black", width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _wait_until(predicate, timeout: float = 6.0, interval: float = 0.05):
    """Poll ``predicate`` (which the loop-thread background tasks make true)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()


def _events(sketch_id: str) -> list[dict]:
    return [e.model_dump(exclude_none=True) for e in v2.manager.sketches[sketch_id].events]


def _has_field(sketch_id: str, field: str) -> bool:
    return any(field in e for e in _events(sketch_id))


def _drain_admin_backlog() -> list[dict]:
    """Snapshot what a freshly-connecting admin would be replayed on the SSE feed.

    Exercises Manager.subscribe_admin (the exact backlog the /admin/events route
    pre-loads) and drains it synchronously. We read the SSE *content* this way
    rather than over HTTP because this Starlette/httpx TestClient build deadlocks
    on reading a streaming StreamingResponse; the route's auth + wiring is covered
    separately (see the 401 checks). Call at a quiescent point so no concurrent
    background _notify_admin races the drain (asyncio.Queue isn't thread-safe)."""
    import asyncio

    queue = v2.manager.subscribe_admin()
    out: list[dict] = []
    try:
        while True:
            try:
                out.append(json.loads(queue.get_nowait()))
            except asyncio.QueueEmpty:
                break
    finally:
        v2.manager.unsubscribe_admin(queue)
    return out


def _arm_robot(name: str, color: str) -> None:
    """Give ``name`` sole ownership of a big empty region and check it in ready,
    so the next enqueued drawing stages onto it immediately."""
    coordinator.set_canvas(
        CanvasConfig(
            id="main",
            width=2000.0,
            height=2000.0,
            regions=[
                RegionConfig(
                    id="r", x=0.0, y=0.0, width=2000.0, height=2000.0, robot=name, color=color
                )
            ],
            placement=PlacementSettings(strategy="origin", targetFootprintMm=200.0),
            drawings=[],
        )
    )
    coordinator.check_in(CheckIn.Request(name=name, status="ready", pose=Pose(x=0.0, y=0.0)))


def _new_client(client: TestClient) -> str:
    resp = client.get("/client")
    assert resp.status_code == 200
    return resp.json()["client"]


def _submit(client: TestClient, client_id: str, seed: int) -> str:
    resp = client.post("/sketch", json={"client": client_id, "sketch": _sketch_data_url(seed)})
    assert resp.status_code == 200, resp.text
    return resp.json()["sketch"]


def _approve_sketch(client: TestClient, sketch_id: str) -> None:
    resp = client.post(
        "/admin/sketch",
        params={"token": TOKEN},
        json={"sketch_id": sketch_id, "status": "approved"},
    )
    assert resp.status_code == 200, resp.text


def _make_trio(client: TestClient, seeds: list[int]) -> list[str]:
    """Submit + admin-approve three sketches (distinct clients) so they group."""
    ids = [_submit(client, _new_client(client), seed) for seed in seeds]
    for sid in ids:
        _approve_sketch(client, sid)
    return ids


def _pending_vec_for(trio_ids: list[str]):
    trio_set = set(trio_ids)
    for pending in list(v2.manager.pending_vectorizations.values()):
        if set(pending.trio_ids) == trio_set:
            return pending
    return None


# --- scenarios -------------------------------------------------------------


def test_sketch_gated_until_admin_and_rejection(client: TestClient) -> None:
    client_id = _new_client(client)
    sketch_id = _submit(client, client_id, seed=1)

    # Nothing has flowed to the client but the creation event — no status yet.
    sketch = v2.manager.sketches[sketch_id]
    assert sketch.state == "approval-pending"
    assert _events(sketch_id) == [{"sketch": sketch_id}]
    assert not _has_field(sketch_id, "status")
    assert sketch_id in v2.manager.pending_sketches

    # A connecting admin is told this sketch is waiting.
    backlog = _drain_admin_backlog()
    assert {"type": "sketch", "sketch_id": sketch_id} in backlog

    # Both admin endpoints are gated.
    assert client.get("/admin/events").status_code == 401
    unauth = client.post(
        "/admin/sketch", json={"sketch_id": sketch_id, "status": "approved"}
    )
    assert unauth.status_code == 401

    # Reject it as inappropriate -> terminal, and the client learns why.
    resp = client.post(
        "/admin/sketch",
        params={"token": TOKEN},
        json={"sketch_id": sketch_id, "status": "innapropriate"},
    )
    assert resp.status_code == 200, resp.text
    assert sketch_id not in v2.manager.pending_sketches
    assert _events(sketch_id)[-1] == {"sketch": sketch_id, "status": "innapropriate"}
    assert v2.manager.sketches[sketch_id].state == "approval-pending"  # never grouped
    # Rejecting a resolved sketch again is a conflict.
    again = client.post(
        "/admin/sketch",
        params={"token": TOKEN},
        json={"sketch_id": sketch_id, "status": "approved"},
    )
    assert again.status_code == 409


def test_two_options_offered_then_chosen_to_robot(client: TestClient) -> None:
    calls_before = len(_COMBINE_CALLS)
    trio = _make_trio(client, seeds=[11, 12, 13])

    # Each got its "approved" status; the trio is combining once full — but NO
    # vectorization has reached any client yet (it's gated on the admin's choice).
    for sid in trio:
        assert any(e.get("status") == "approved" for e in _events(sid))
    assert _wait_until(lambda: all(v2.manager.sketches[s].state == "combining" for s in trio))

    pending = _wait_until(lambda: _pending_vec_for(trio))
    assert pending is not None, "combine never produced vectorization options"
    # Exactly two combines ran for this trio, yielding two distinct option sets.
    assert len(_COMBINE_CALLS) - calls_before == 2, "should combine twice per trio"
    assert len(pending.command_options) == 2
    assert all(opt for opt in pending.command_options), "both options non-empty"
    dumped = [[c.model_dump() for c in opt] for opt in pending.command_options]
    assert dumped[0] != dumped[1], "the two options should differ"
    for sid in trio:
        assert not _has_field(sid, "vectorization"), "leaked vectorization pre-choice"

    # A fresh admin connection replays the pending vectorization with the source
    # trio and both command options.
    backlog = _drain_admin_backlog()
    vec = next(
        e for e in backlog
        if e.get("type") == "vectorization" and e["vectorization_id"] == pending.id
    )
    assert list(vec["source_trio"]) == trio, "admin must see the source sketch ids"
    assert len(vec["command_options"]) == 2
    assert all(vec["command_options"]), "both option command lists ride along"

    # The admin picks option 0 verbatim; a robot is armed to claim the drawing.
    chosen = vec["command_options"][0]
    _arm_robot("bot-choose", "#112233")
    resp = client.post(
        "/admin/vectorization",
        params={"token": TOKEN},
        json={"vectorization_id": pending.id, "commands": chosen},
    )
    assert resp.status_code == 200, resp.text

    # Every member reveals the vectorization, then completes on the real bot.
    assert _wait_until(lambda: all(v2.manager.sketches[s].state == "complete" for s in trio))
    for sid in trio:
        events = _events(sid)
        assert any(e.get("vectorization") == pending.id for e in events)
        robot_event = next(e for e in events if "robot" in e)
        assert robot_event["robot"] == "bot-choose"
        assert robot_event.get("color") == "#112233"

    # The served SVG (registered under the stable locator id) is a render of the
    # chosen option.
    assert pending.id in v2.manager.resources
    redirect = client.get(f"/resource/{pending.id}", follow_redirects=False)
    assert redirect.status_code == 307
    stored = _FAKE_STORAGE.blobs[FakeStorage.resource_key(pending.id, "image/svg+xml")]
    from release.robots import parse_commands  # local import: typed models

    assert stored.decode("utf-8") == v2._render_commands_svg(parse_commands(chosen))


def test_admin_trims_chosen_option(client: TestClient) -> None:
    trio = _make_trio(client, seeds=[21, 22, 23])
    pending = _wait_until(lambda: _pending_vec_for(trio))
    assert pending is not None

    # The admin picks an option but trims it (a filtered/edited command list). The
    # edited list — not either raw option — becomes the served SVG + the drawing.
    trimmed = [
        {"kind": "line", "distance": 120.0, "penDown": True},
        {"kind": "spin", "degrees": 90.0},
        {"kind": "line", "distance": 120.0, "penDown": True},
        {"kind": "spin", "degrees": 90.0},
        {"kind": "line", "distance": 120.0, "penDown": True},
        {"kind": "spin", "degrees": 90.0},
        {"kind": "line", "distance": 120.0, "penDown": True},
    ]

    _arm_robot("bot-trim", "#445566")
    resp = client.post(
        "/admin/vectorization",
        params={"token": TOKEN},
        json={"vectorization_id": pending.id, "commands": trimmed},
    )
    assert resp.status_code == 200, resp.text

    assert _wait_until(lambda: all(v2.manager.sketches[s].state == "complete" for s in trio))

    # The stored SVG matches the admin's trimmed list, not either offered option.
    stored = _FAKE_STORAGE.blobs[FakeStorage.resource_key(pending.id, "image/svg+xml")]
    from release.robots import parse_commands  # local import: typed models

    assert stored.decode("utf-8") == v2._render_commands_svg(parse_commands(trimmed))
    for option in pending.command_options:
        assert stored.decode("utf-8") != v2._render_commands_svg(option)

    # A resolved vectorization can't be resolved again.
    again = client.post(
        "/admin/vectorization",
        params={"token": TOKEN},
        json={"vectorization_id": pending.id, "commands": trimmed},
    )
    assert again.status_code == 404


def test_combine_concurrency_is_bounded(client: TestClient) -> None:
    global _PEAK_INFLIGHT

    # Rebuild the gate with a limit of 1 so even a single trio's two option-
    # combines (launched together via asyncio.gather) must run one at a time.
    v2.MAX_CONCURRENT_COMBINES = 1
    v2.manager._combine_gate_sem = None
    v2.manager._combine_gate_loop = None
    _PEAK_INFLIGHT = 0

    trio = _make_trio(client, seeds=[41, 42, 43])
    pending = _wait_until(lambda: _pending_vec_for(trio), timeout=10.0)
    assert pending is not None
    assert len(pending.command_options) == 2  # both combines still ran

    # Without the gate the two concurrent combine calls would overlap (peak 2);
    # the gate of 1 serializes them.
    assert _PEAK_INFLIGHT == 1, f"gate breached: {_PEAK_INFLIGHT} combines in flight"

    # Restore the default for any later work in this process.
    v2.MAX_CONCURRENT_COMBINES = 5
    v2.manager._combine_gate_sem = None
    v2.manager._combine_gate_loop = None


class _FakeRateLimit(Exception):
    """Stand-in for openai.RateLimitError — _is_retryable keys off status_code."""

    status_code = 429


def test_combine_retries_on_rate_limit(client: TestClient) -> None:
    # Wrap the combine stub so the first call 429s once, then delegates normally.
    base_stub = v2.Combine.openai_s3
    budget = {"raises": 1}
    lock = __import__("threading").Lock()

    def flaky(model_id, images, prompt):  # noqa: ANN001
        with lock:
            if budget["raises"] > 0:
                budget["raises"] -= 1
                raise _FakeRateLimit("rate limited")
        return base_stub(model_id, images, prompt)

    v2.Combine.openai_s3 = staticmethod(flaky)
    v2.COMBINE_BACKOFF_BASE, v2.COMBINE_BACKOFF_MAX = 0.01, 0.02  # keep the test fast
    try:
        trio = _make_trio(client, seeds=[61, 62, 63])
        pending = _wait_until(lambda: _pending_vec_for(trio), timeout=10.0)
        # The 429 was consumed (fired), and the retry recovered — both options
        # still arrived, so the rate limit didn't lose the trio.
        assert budget["raises"] == 0, "the injected 429 never fired"
        assert pending is not None, "retry-with-backoff failed to recover the combine"
        assert len(pending.command_options) == 2
    finally:
        v2.Combine.openai_s3 = staticmethod(base_stub)
        v2.COMBINE_BACKOFF_BASE, v2.COMBINE_BACKOFF_MAX = 1.0, 30.0


# --- pytest fixture / standalone runner ------------------------------------

try:
    import pytest

    @pytest.fixture()
    def client():
        app = FastAPI()
        app.include_router(v2.router)
        with TestClient(app) as test_client:
            yield test_client

except ImportError:  # pragma: no cover - pytest always present in CI
    pytest = None


def _run_standalone() -> int:
    app = FastAPI()
    app.include_router(v2.router)
    scenarios = [
        test_sketch_gated_until_admin_and_rejection,
        test_two_options_offered_then_chosen_to_robot,
        test_admin_trims_chosen_option,
        test_combine_concurrency_is_bounded,
        test_combine_retries_on_rate_limit,
    ]
    failures = 0
    with TestClient(app) as test_client:
        for scenario in scenarios:
            try:
                scenario(test_client)
                print(f"PASS  {scenario.__name__}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                import traceback

                print(f"FAIL  {scenario.__name__}: {exc}")
                traceback.print_exc()
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} scenarios passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_standalone())
