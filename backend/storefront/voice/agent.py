"""The voice agent's worker: Gemini Live hears the user and sees the phone's camera, and answers about what the
tracker is on (assistant.py). The backend brings it into the session's room when the phone asks for voice (RPC
voice {on: true}), and sends it away when voice goes off.

    python -m storefront.voice.agent start   # from backend/; needs LIVEKIT_URL, LIVEKIT_API_KEY,
                                             # LIVEKIT_API_SECRET and GOOGLE_API_KEY
"""
import asyncio
import logging

from livekit import agents, rtc
from livekit.agents import AgentServer, AgentSession, room_io

from storefront import config
from storefront.rtc.tokens import PHONE
from storefront.voice.assistant import Assistant
from storefront.voice.debug import Flow, RealtimeModel, log_session
from storefront.voice.seen import Seen

log = logging.getLogger("storefront.voice")

server = AgentServer(num_idle_processes=1)  # one phone at a time: one warm process, not one per CPU core


async def phone_in(room: rtc.Room) -> rtc.RemoteParticipant:
    """The phone, once it's in the room. The backend ("vision") and projector pages are there too, and the
    session must listen to the phone, not to them."""
    joined: asyncio.Future = asyncio.get_running_loop().create_future()

    def on_join(p: rtc.RemoteParticipant):
        if p.identity.startswith(PHONE) and not joined.done():
            joined.set_result(p)

    room.on("participant_connected", on_join)
    for p in room.remote_participants.values():
        on_join(p)
    try:
        return await joined
    finally:
        room.off("participant_connected", on_join)


@server.rtc_session(agent_name=config.VOICE_AGENT)
async def entry(ctx: agents.JobContext):
    seen = Seen()
    seen.listen(ctx.room)
    await ctx.connect()
    log.info("voice: in room %s, waiting for the phone", ctx.room.name)
    phone = await phone_in(ctx.room)
    tracks = [f"{rtc.TrackSource.Name(p.source)}{' (muted)' if p.muted else ''}" for p in phone.track_publications.values()]
    log.info("voice: phone %s is in, publishing %s", phone.identity, tracks or "nothing yet")
    session = AgentSession(llm=RealtimeModel(Flow(seen), model=config.GEMINI_MODEL))
    log_session(session)
    await session.start(agent=Assistant(seen), room=ctx.room,
                        room_options=room_io.RoomOptions(participant_identity=phone.identity, video_input=True))
    log.info("voice: listening to %s in room %s (%s)", phone.identity, ctx.room.name, config.GEMINI_MODEL)


if __name__ == "__main__":
    agents.cli.run_app(server)
