"""The voice agent's worker: groundwork for a voice assistant over LiveKit, with no model or tools yet.

It joins each room POST /voice/token opens and follows the stream (seen.py); resolve.py and actions.py
hold the pieces its tools will use. It says nothing yet.

    python -m storefront.voice.agent start   # from backend/; needs LIVEKIT_* and the main server on :8000
"""
import asyncio
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
    follower = asyncio.create_task(seen.follow(lambda text: log.info("voice: would say %r", text)))
    ctx.add_shutdown_callback(follower.cancel)
    await ctx.connect()
    log.info("voice: joined room %s", ctx.room.name)


if __name__ == "__main__":
    agents.cli.run_app(server)
