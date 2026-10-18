"""Who is who in the session's LiveKit room, and their tokens: the phone, the backend ("vision") and the
projector page."""
import uuid

from livekit import api

from storefront import config

PHONE = "phone-"    # every phone identity starts with this
VISION = "vision"   # the backend
VIEWER = "viewer-"  # a projector page
UPDATES = "updates"  # the topic of the backend's messages


def _token(room: str, identity: str, name: str, **grants) -> str:
    return (api.AccessToken(config.LIVEKIT_API_KEY, config.LIVEKIT_API_SECRET)
            .with_identity(identity).with_name(name)
            .with_grants(api.VideoGrants(room_join=True, room=room, **grants)).to_jwt())


def phone(room: str, identity: str | None = None, name: str | None = None) -> str:
    """The phone publishes video and the mic, and calls the backend's RPCs."""
    identity = identity if identity and identity.startswith(PHONE) else PHONE + (identity or uuid.uuid4().hex[:6])
    return _token(room, identity, name or "Storefront user")


def vision(room: str) -> str:
    return _token(room, VISION, "Storefront backend")


def viewer(room: str) -> str:
    """Watch only, and hidden: the phone and the agent never see it."""
    return _token(room, VIEWER + uuid.uuid4().hex[:6], "Projector", can_publish=False, can_publish_data=False,
                  hidden=True)
