"""POST /look/{track_id}: a Look on a tracked object, started from this laptop (the phone uses "select")."""
from fastapi import APIRouter, Depends, HTTPException, Request

from storefront.models import DetectResponse
from storefront.pipeline import looks

router = APIRouter()


def local_only(request: Request):
    """For this laptop only. Tunnels (ngrok, Cloudflare) connect from localhost too, but add X-Forwarded-For."""
    if not request.client or request.client.host not in ("127.0.0.1", "::1") or "x-forwarded-for" in request.headers:
        raise HTTPException(403, "local requests only")


@router.post("/look/{track_id}", response_model=DetectResponse, dependencies=[Depends(local_only)])
async def look_route(track_id: int):
    resp = await looks.look(track_id)
    if resp is None:
        raise HTTPException(404, f"the stream isn't tracking object {track_id}")
    return resp
