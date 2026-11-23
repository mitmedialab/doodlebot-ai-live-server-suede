# Sketch Server

> [!NOTE]
> This folder is a [suede](https://github.com/pmalacho-mit/suede) dependency.

A FastAPI backend for a live, interactive drawing installation. Phones submit
sketches → a human curator moderates and merges them with AI image models → the
merged drawing is vectorized into pen strokes → physical **Doodlebot** robots
poll the server and draw the result onto a shared physical canvas without
overlapping each other.

The server is the single source of truth: it owns the moderation queue, the AI
combine step, the vectorizer, and a live model of every physical canvas
(including per-region occupancy so two robots never draw over each other).

---

## The big picture

```
 PHONE (/)                 CURATOR (/gallery)              BIG SCREEN (/display)
   │ draw/text                │ moderate + combine             │ shows approved art
   ▼                          ▼                                ▲
 POST /api/submit   ──▶  pending/  ──approve──▶  sketches/  ──▶│ (via SSE)
                                                    │
                                                    │ POST /api/combine (2–4 sketches)
                                                    ▼
                                          OpenAI / Gemini image model
                                                    │
                                                    ▼  combined/  (a single merged line drawing)
                                                    │
                                          POST /vectorize  (image → arc/line/spin commands)
                                                    │
                                                    ▼
                                          robots coordinator (queue + placement)
                                                    │  POST /api/robots/checkin (~1 Hz)
                                                    ▼
                                          DOODLEBOTS draw on the physical canvas
```

Every state change is pushed to browsers over a single **Server-Sent Events**
stream (`GET /stream`), so all three pages stay live without polling.

---

## Request flow & modules

`app.py` is the entry point: it builds the `FastAPI` app, adds CORS (locked to
`https://mitmedialab.github.io`), and mounts one `APIRouter` per feature module.
Each module below is self-contained and owns its own routes.

| Module | Responsibility | Key endpoints |
|---|---|---|
| `pages.py` | Serves the 3 HTML front-ends (fetched from `STATIC_HOST`, cached on disk) | `GET /`, `/gallery`, `/display`, `*/static/bust` |
| `stream.py` | SSE subscription endpoint | `GET /stream` |
| `submit.py` | Phone posts a drawing/text sketch → `pending/` | `POST /api/submit` |
| `moderation.py` | Curator lists / approves / rejects pending sketches | `GET /api/pending`, `POST /api/pending/{f}/approve`, `/reject` |
| `sketches.py` | Approved sketches: list, delete, broadcast selection | `GET /api/sketches`, `DELETE …`, `POST /api/select` |
| `models.py` | Lists image models whose provider key is configured | `GET /api/models` |
| `presets.py` | List / save prompt presets | `GET /api/presets`, `POST /api/presets` |
| `combine.py` | Merges 2–4 sketches via image model(s) → `combined/` | `POST /api/combine` |
| `vectorize.py` | Image → robot drawing commands (+ comparison SVG) | `POST /vectorize` |
| `robots.py` | Robot polling protocol, job queue, placement coordinator | `GET/POST /api/robots/*`, `POST /api/robots/checkin` |

Shared infrastructure (no routes):

- **`config.py`** — env vars (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`,
  `STATIC_HOST`), on-disk data dirs (`pending/`, `sketches/`, `combined/`,
  `static/`), the model registry (`MODELS`), and built-in prompt presets.
- **`common.py`** — shared types, auth (`require_admin`), preset persistence, and
  the **SSE broadcast broker** (`broadcast`, `add_listener`, `remove_listener`).
- **`llms.py`** — lazily-constructed singleton OpenAI / Gemini API clients.
- **`canvas.py`** — the geometry/occupancy/placement engine (see below).

---

## State & persistence

There is **no database**. State lives in two places:

1. **The filesystem** — each sketch is a `.png` plus a sibling `.json` metadata
   file (`phoneId`, `phoneColor`, `kind`, `created`, `status`). Approval simply
   *moves* the pair from `pending/` to `sketches/`. Combined outputs land in
   `combined/`. Custom presets persist to `presets.json`. These dirs are
   bind-mounted as Docker volumes so they survive restarts.
2. **In-memory** — the SSE listener list (`common.py`) and the robot
   coordinator's placed drawings + job queue (`robots.py`). These reset on
   restart, which is fine: they model the *current* live session. (Drawings can be
   loaded back with `POST /api/robots/canvases`; their space is reserved from the
   next placement on.)

### Auth

Mutating/admin endpoints call `require_admin(req)`, which checks an
`X-Admin-Token` header **or** `?token=` query param against `ADMIN_TOKEN`.
Public (no-auth) endpoints are intentionally limited to the page loads,
`/api/submit`, `/api/sketches` (display needs it), `/api/models`, `/stream`, and
the robot endpoints `GET /api/robots/markers` + `POST /api/robots/checkin`
(the bots have no token).

### Real-time events (SSE)

`broadcast(event, payload)` fan-outs a typed event to every connected browser.
Each listener is a bounded `queue.Queue` (max 50 msgs); a client that falls
behind is dropped rather than blocking the broker. Events:
`new_pending`, `approved_sketch`, `deleted_sketch`, `selection_changed`,
`combining`, `combined`. The stream sends a `: ping` keep-alive every 25 s.

---

## The combine step (`combine.py`)

Takes 2–4 approved filenames, a prompt (explicit `prompt` > named `preset` >
fallback), and one or more model IDs. For each model it dispatches to the right
provider helper (`Combine.openai` uses the images-edit API; `Combine.gemini`
uses `generate_content` with `IMAGE` modality), saves the resulting PNG to
`combined/`, and returns the base64 image. Blocking provider calls are pushed to
threads via `asyncio.to_thread`. `models.py` only advertises a model if its
provider's API key is set.

---

## The robot subsystem (`robots.py` + `canvas.py`)

This is the most involved part. The design principle: **the server owns the
canvas model**, not the bots.

### The bot loop — Locate → Poll → Draw

1. **Locate** — bot fetches known global aruco-marker positions
   (`GET /api/robots/markers`), sees some with its camera, and solves its own
   pose in the shared global frame (mm, x-right, y-down).
2. **Poll** — ~1 Hz `POST /api/robots/checkin` with `{name, status, pose}`. The
   server replies `wait` (nothing to do) or `draw` (a job).
3. **Draw** — bot drives to `navigateTo` (the drawing's first ink point + an
   approach heading), runs `commands`, follows `exitPath` off-canvas, then loops.

### The coordinator (`_Coordinator`, one process-wide singleton)

Thread-safe matchmaker between a **job queue** and the **ready-bot pool**.

- `enqueue_drawing(commands, …)` — called after a combined sketch is vectorized.
  It pre-computes placement inputs once (lead-in split, start heading, the ink's
  native size) and queues the job.
- On every enqueue **and** every check-in, `_assign_locked()` tries to place each
  queued job. For each job it ranks ready bots (most idle, then most free canvas)
  and runs a placement search in that bot's region. First fit wins: it records the
  drawing on the canvas — which is what reserves its space — and **stages** the
  resolved start pose for delivery on the bot's next check-in. Jobs that fit
  nowhere stay queued and are retried later (a region only ever fills up).

Each robot is assigned exactly one **region** of a canvas (one region = one bot's
private area), configured via `DEFAULT_CANVASES` or `POST /api/robots/canvases`.

### Placement engine (`canvas.py`) — collision-free packing

The search runs in
[`canvas_placement`](https://github.com/mitmedialab/suede.canvas-placement), a
Rust (PyO3) engine; `canvas.py` holds the canvas data model, the turtle geometry,
and three thin wrappers around it: `place`, `free_fractions` and `occupancy`.

The engine is **stateless**: every call rebuilds a region's occupancy from the
canvas's list of placed drawings (`_Coordinator._drawings`), so that list is the
only record of what space is taken. Recording a drawing reserves it; clearing the
list frees it. Nothing else to keep in sync. The flow:

1. **Strip lead-in** (`split_lead_in`, at enqueue): drop the initial pen-up "drive
   to the first ink" so placement only constrains the actual drawing. Rotation is
   free for a turtle path — rotating the ink just rides on the bot's approach
   heading, so the drawing commands are sent **unchanged**.
2. **Occupancy** (engine): rasterize every drawing in the region (cells within
   `pen_width/2 + clearance` of the ink), grow it by the canvas's
   `general_buffer`, and block what neighbouring robots that are drawing right now
   sweep with their chassis.
3. **Search** (engine): for each candidate rotation, get every collision-free
   offset in one shot via **FFT cross-correlation**, then pick a pose by strategy —
   `origin` (dense, packs toward the corner) or `scatter` (random valid pose,
   organic spread; seeded from the coordinator's RNG). Near a live robot, a pose
   must also pass an exact check of our own chassis sweep.
4. **Size** (engine): try the canvas's `targetFootprintMm` first, then shrink as
   little as needed (down to `minFootprintScale`).

`coordinator.free_fractions(canvas_id)` gives each region's fill for the admin
view; `coordinator.occupancy(canvas_id, region_id)` returns the grids a search
would see, for debugging. `python bench_placement.py --against main` (repo root)
times placement against another branch.

Like the vectorizer, the extension isn't on PyPI: `pyproject.toml` pins each
platform's prebuilt wheel by release URL + sha256. To upgrade, swap the version +
hashes for a newer
[release](https://github.com/mitmedialab/suede.canvas-placement/releases).

> Extension point: the `origin` ranking is a simple heuristic. For tighter
> packing, swap in a contact-point or No-Fit-Polygon score — in the engine's
> search step; the rest of the pipeline is unchanged.

---

## The vectorizer (`vectorize.py`)

`POST /vectorize` accepts an uploaded image and runs it through
[`arcline_vectorize`](https://github.com/pmalacho-mit/suede.doodlebot-vectorize),
a Rust (PyO3) port of the original Python `arc_line_vectorization_suede`
pipeline (skeletonize → segment → graph → low/high-geometry vectorize → route
optimize). It returns two command lists — `low_geometry` (consolidated) and
`high_geometry` — plus a side-by-side comparison `svg`. These `DrawingCommand`s
(`line`/`spin`/`arc`, discriminated union) are exactly what `robots.py` consumes,
closing the loop from pixels to pen strokes.

The extension isn't on PyPI: `pyproject.toml` pins each platform's prebuilt
wheel (Linux x86_64/aarch64, macOS) by release URL + sha256, so the Docker build
needs no Rust toolchain. To upgrade, swap the version + hashes for a newer
[release](https://github.com/pmalacho-mit/suede.doodlebot-vectorize/releases).
`svg.py` keeps a pure-Python `commands_to_svg` for re-rendering admin-edited
commands.

---

## Running it

The app reads/writes paths relative to its CWD, so it runs from inside the package
dir as a module (`python -m server.app`), binding `0.0.0.0:5000`.

**Docker (recommended)** — use the `cli.sh` wrapper around `compose.yml`:

```bash
./cli.sh start      # build + (re)start detached (safe on a running stack)
./cli.sh logs       # follow logs
./cli.sh status     # container + port mappings
./cli.sh stop       # tear down container + network
./cli.sh restart    # full reset (down, then start)
```

The container publishes host **5001 → container 5000** (macOS AirPlay squats on
5000). Env vars (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ADMIN_TOKEN`,
`STATIC_HOST`) are read from the host; `ADMIN_TOKEN` defaults to `test` if unset.
`combined/`, `pending/`, `sketches/`, and `presets.json` are bind-mounted for
persistence.

### Front-end pages

The HTML is **not** shipped with the server. On first request, `pages.py` fetches
each page (`draw.html`, `gallery.html`, `display.html`) from `STATIC_HOST` and
caches it on disk. After redeploying the front-end, clear the cache with
`POST /static/bust` (or just visit `/static/bust?token=<ADMIN_TOKEN>` in a
browser).
