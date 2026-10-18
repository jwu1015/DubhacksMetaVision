"""The voice agent's worker: groundwork for a voice assistant over LiveKit, with no model or tools yet.

The backend brings it into the session's room when the phone asks for voice (RPC voice {on: true}). It
follows the backend's messages there (seen.py); resolve.py and actions.py hold the pieces its tools will
use. It says nothing yet.

    python -m storefront.voice.agent start   # from backend/; needs LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET
"""
import logging

from livekit import agents
from livekit.agents import AgentServer

from storefront import config
from storefront.voice.seen import Seen

log = logging.getLogger("storefront.voice")

server = AgentServer(num_idle_processes=1)  # one phone at a time: one warm process, not one per CPU core


@server.rtc_session(agent_name=config.VOICE_AGENT)
async def entry(ctx: agents.JobContext):
    seen = Seen()
    seen.listen(ctx.room)
    await ctx.connect()
    log.info("voice: joined room %s", ctx.room.name)


if __name__ == "__main__":
    agents.cli.run_app(server)
