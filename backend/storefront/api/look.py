"""POST /look/{uid}: pick a tracked object from this laptop, as a tap on its hotspot does."""
from fastapi import APIRouter, Depends, HTTPException, Request

from storefront.models import LookMessage

router = APIRouter()


def local_only(request: Request):
    """For this laptop only. Tunnels (ngrok, Cloudflare) connect from localhost too, but add X-Forwarded-For."""
    if not request.client or request.client.host not in ("127.0.0.1", "::1") or "x-forwarded-for" in request.headers:
        raise HTTPException(403, "local requests only")


@router.post("/look/{uid}", response_model=LookMessage, dependencies=[Depends(local_only)])
async def look_route(uid: str, request: Request):
    look = request.app.state.runner.select(uid)
    if look is None:
        raise HTTPException(404, f"the stream isn't tracking object {uid}")
    return look.message_dict()
