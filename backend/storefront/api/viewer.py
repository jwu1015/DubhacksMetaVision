"""The projector page: it joins the session's LiveKit room, watch only (GET /stream/viewer-token)."""
from fastapi import APIRouter
from fastapi.responses import FileResponse

from storefront import config

router = APIRouter(prefix="/stream")


@router.get("/viewer", include_in_schema=False)
async def viewer_route():
    return FileResponse(config.STATIC / "viewer.html")
