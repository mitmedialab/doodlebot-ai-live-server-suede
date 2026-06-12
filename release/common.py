"""Shared config, types, persistence, auth, and the SSE broadcast broker."""

import json, queue, threading
from typing import Literal, Optional, TypeAlias, overload

from fastapi import Request, HTTPException
from pydantic import BaseModel

from .config import PRESETS_FILE, ADMIN_TOKEN, PROMPT_PRESETS

Kind: TypeAlias = Literal["drawing", "text"]
Provider: TypeAlias = Literal["openai", "gemini"]


def load_presets() -> dict[str, str]:
    saved: dict[str, str] = {}
    if PRESETS_FILE.exists():
        saved = json.loads(PRESETS_FILE.read_text())
    merged = {**PROMPT_PRESETS, **saved}
    merged["Custom"] = ""  # always last
    return merged


def save_preset(name: str, prompt: str) -> None:
    saved: dict[str, str] = {}
    if PRESETS_FILE.exists():
        saved = json.loads(PRESETS_FILE.read_text())
    saved[name] = prompt
    PRESETS_FILE.write_text(json.dumps(saved, indent=2))


def require_admin(req: Request) -> None:
    token = req.headers.get("X-Admin-Token") or req.query_params.get("token")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


class SuccessResponse(BaseModel):
    success: bool = True


class Phone(BaseModel):
    phoneId: Optional[str] = None
    phoneColor: Optional[str] = None


BroadcastEvent: TypeAlias = Literal[
    "new_pending",
    "approved_sketch",
    "deleted_sketch",
    "selection_changed",
    "combining",
    "combined",
]


class Broadcast:
    """Payload models for each server-sent event, keyed by event name."""

    class Sketch(BaseModel):
        filename: str
        dataUrl: str
        phoneId: str = "unknown"
        phoneColor: str = "#888888"
        kind: Kind = "drawing"
        created: str = ""
        status: str = "pending"

    class DeletedSketch(BaseModel):
        filename: str

    class SelectionChanged(BaseModel):
        selected: list[str]

    class Combining(BaseModel):
        filenames: list[str]
        phones: list[Phone]

    class Combined(BaseModel):
        filenames: list[str]
        phones: list[Phone]


_listeners: list[queue.Queue[str]] = []
_listeners_lock = threading.Lock()


# fmt: off
@overload
def broadcast(event: Literal["new_pending"], data: Broadcast.Sketch) -> None: ...
@overload
def broadcast(event: Literal["approved_sketch"], data: Broadcast.Sketch) -> None: ...
@overload
def broadcast(event: Literal["deleted_sketch"], data: Broadcast.DeletedSketch) -> None: ...
@overload
def broadcast(event: Literal["selection_changed"], data: Broadcast.SelectionChanged) -> None: ...
@overload
def broadcast(event: Literal["combining"], data: Broadcast.Combining) -> None: ...
@overload
def broadcast(event: Literal["combined"], data: Broadcast.Combined) -> None: ...
# fmt: on
def broadcast(event: BroadcastEvent, data: BaseModel) -> None:
    msg = f"event: {event}\ndata: {data.model_dump_json()}\n\n"
    with _listeners_lock:
        dead = []
        for q in _listeners:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _listeners.remove(q)


def add_listener() -> queue.Queue[str]:
    q: queue.Queue[str] = queue.Queue(maxsize=50)
    with _listeners_lock:
        _listeners.append(q)
    return q


def remove_listener(q: queue.Queue[str]) -> None:
    with _listeners_lock:
        try:
            _listeners.remove(q)
        except ValueError:
            pass
