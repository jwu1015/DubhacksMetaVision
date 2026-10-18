"""LiveKit tokens, in LiveKit's standard token-endpoint format so the clients can use their SDK's endpoint
token source.

    POST /session/token      the phone: a new room with the backend in it (rtc/room.py)
    GET  /stream/viewer-token  a projector page: the current room, watch only
    POST /voice/token        the current app's voice assistant, until it moves to /session/token
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Request
from livekit import api as livekit_api

from storefront import config
from storefront.models import SessionTokenRequest, TokenRequest, TokenResponse
from storefront.rtc import tokens

log = logging.getLogger("storefront.session")

router = APIRouter()


def ready():
    if not config.livekit_ready():
        raise HTTPException(503, "LiveKit isn't set up: LIVEKIT_URL, LIVEKIT_API_KEY and LIVEKIT_API_SECRET are unset")


@router.post("/session/token", response_model=TokenResponse, status_code=201)
async def session_token_route(request: Request, req: SessionTokenRequest | None = None):
    ready()
    req = req or SessionTokenRequest()
    room = await request.app.state.rooms.open(req.category)
    return TokenResponse(server_url=config.LIVEKIT_URL,
                         participant_token=tokens.phone(room, req.participant_identity, req.participant_name))


@router.get("/stream/viewer-token", response_model=TokenResponse)
async def viewer_token_route(request: Request):
    ready()
    session = request.app.state.rooms.current
    if session is None:
        raise HTTPException(404, "no session yet")
    return TokenResponse(server_url=config.LIVEKIT_URL, participant_token=tokens.viewer(session.name))


@router.post("/voice/token", response_model=TokenResponse, status_code=201)
async def voice_token_route(req: TokenRequest | None = None):
    """Every call opens a new room with the voice agent dispatched into it."""
    ready()
    req = req or TokenRequest()
    room = "storefront-voice-" + uuid.uuid4().hex[:8]
    token = (livekit_api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
             .with_identity(req.participant_identity or "phone-" + uuid.uuid4().hex[:6])
             .with_name(req.participant_name or "Storefront user")
             .with_grants(livekit_api.VideoGrants(room_join=True, room=room))
             .with_room_config(livekit_api.RoomConfiguration(
                 agents=[livekit_api.RoomAgentDispatch(agent_name=config.VOICE_AGENT)])))
    log.info("voice: token for room %s", room)
    return TokenResponse(server_url=config.LIVEKIT_URL, participant_token=token.to_jwt())
