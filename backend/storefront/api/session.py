"""POST /voice/token: the phone's ticket to a LiveKit room with the voice agent in it."""
import logging
import uuid

from fastapi import APIRouter, HTTPException
from livekit import api as livekit_api

from storefront import config
from storefront.models import VoiceTokenRequest, VoiceTokenResponse

log = logging.getLogger("storefront.session")

router = APIRouter()


@router.post("/voice/token", response_model=VoiceTokenResponse, status_code=201)
async def voice_token_route(req: VoiceTokenRequest | None = None):
    """In LiveKit's standard token-endpoint format, so the app can use its SDK's endpoint token source.
    Every call opens a new room: LiveKit dispatches the agent when a room is created."""
    if not config.livekit_ready():
        raise HTTPException(503, "voice isn't set up: LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are unset")
    req = req or VoiceTokenRequest()
    room = "storefront-" + uuid.uuid4().hex[:8]
    token = (livekit_api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
             .with_identity(req.participant_identity or "phone-" + uuid.uuid4().hex[:6])
             .with_name(req.participant_name or "Storefront user")
             .with_grants(livekit_api.VideoGrants(room_join=True, room=room))
             .with_room_config(livekit_api.RoomConfiguration(
                 agents=[livekit_api.RoomAgentDispatch(agent_name=config.VOICE_AGENT)])))
    log.info("voice: token for room %s", room)
    return VoiceTokenResponse(server_url=config.LIVEKIT_URL, participant_token=token.to_jwt())
