"""GET /health."""
from fastapi import APIRouter

from storefront import config, net
from storefront.live import stream
from storefront.models import HealthResponse
from storefront.rtc import legacy_webrtc

router = APIRouter()


async def model_service_ok(timeout: float = 3.0) -> bool:
    """The segmentation server answers its health check."""
    if not config.MODEL_SERVICE_URL:
        return False
    try:
        r = await net.client().get(
            f"{config.MODEL_SERVICE_URL}/health", timeout=timeout,
            headers=config.segmentation_headers(), follow_redirects=False,  # no login-page redirects
        )
        body = r.json()
        # Baptiste's SAM server answers {"status": "ok"}; the spec's model service {"ok": true}.
        return r.status_code == 200 and (body.get("ok") is True or body.get("status") == "ok")
    except Exception:  # unreachable, timeout, bad JSON: all just mean "not ok"
        return False


@router.get("/health", response_model=HealthResponse)
async def health_route():
    return HealthResponse(ok=True, model_service=await model_service_ok(),
                          webrtc_sessions=1 if legacy_webrtc.phones.current else 0,
                          scene_memory=stream.live.running, voice=config.livekit_ready())
