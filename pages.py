"""Static HTML pages: phone drawing UI, curator gallery, big-screen display."""

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter()


@router.get("/")
async def draw_page() -> FileResponse:
    return FileResponse("static/draw.html")


@router.get("/gallery")
async def gallery_page() -> FileResponse:
    return FileResponse("static/gallery.html")


@router.get("/display")
async def display_page() -> FileResponse:
    return FileResponse("static/display.html")
