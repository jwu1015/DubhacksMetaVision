"""LiveKit tokens, in LiveKit's standard token-endpoint format so the clients can use their SDK's endpoint
token source.

    POST /session/token      the phone: a new room with the backend in it (rtc/room.py)
    GET  /stream/viewer-token  a projector page: the current room, watch only
    POST /voice/token        the current app's voice assistant, until it moves to /session/token: a new room
                             with the backend and the voice agent in it
"""
import logging

from fastapi import APIRouter, HTTPException, Request

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
async def voice_token_route(request: Request, req: TokenRequest | None = None):
    """Every call opens a new room with the backend and the voice agent in it, so the agent hears what's
    tracked (Rooms.open_voice)."""
    ready()
    req = req or TokenRequest()
    room = await request.app.state.rooms.open_voice()
    log.info("voice: token for room %s", room)
    return TokenResponse(server_url=config.LIVEKIT_URL,
                         participant_token=tokens.phone(room, req.participant_identity, req.participant_name))
